"""Upgrading the database when a new version starts (upgrades app), on the database the tests run on:
MySQL or SQLite. What only one of them has is tested on it alone.

A "new version" is siteconf with test migrations (settings.MIGRATION_MODULES): migrations_ok brings a
0002 that adds a table and changes a row; migrations_failing a 0003 after it that fails half way. The
upgrades change tables, which MySQL can't roll back, so these are TransactionTestCases (Django runs
them after the others); each test that applies a test migration takes it back when it ends.
"""

import gzip
import hashlib
import io
import os
import re
import shutil
import signal
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import certifi
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import OperationalError, connection, connections
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from config import database
from config.version import VERSION
from apps.core.testing import admin_url, assert_private, mysql_only, readable_by_all, sqlite_only
from apps.siteconf.models import SiteSettings
from apps.upgrades import backup, service, startup
from apps.upgrades.models import UpgradeLog
from apps.upgrades.startup import upgrade_on_start

User = get_user_model()
MYSQL = connection.vendor == "mysql"
FUTURE = ("siteconf", "0099_from_a_newer_version")
NEW_VERSION = {"siteconf": "apps.upgrades.tests.migrations_ok"}
FAILING_VERSION = {"siteconf": "apps.upgrades.tests.migrations_failing"}
# What the messages say to check, for a database that can't be read.
WHAT_TO_CHECK = "config.toml 里 [database] 的 host" if MYSQL else "config.toml 里 [database] 的 file"


def snapshot():
    """Every table as it is. MySQL: its CREATE TABLE (columns, indexes, AUTO_INCREMENT) and a checksum
    of its rows. SQLite: everything sqlite_master has, a checksum of each table's rows as stored, and
    the AUTOINCREMENT counters."""
    tables = {}
    with connection.cursor() as cursor:
        if MYSQL:
            for table in connection.introspection.table_names():
                cursor.execute(f"SHOW CREATE TABLE {backup.quote(table)}")
                create = cursor.fetchone()[1]
                # Not CHECKSUM TABLE: it counts the hidden columns MySQL keeps for an index on an
                # expression too, which a table made in one statement lays out in another order.
                # Times as text (the driver can't read every TIME), bytes as hex.
                cursor.execute(
                    "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS"
                    " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s ORDER BY ORDINAL_POSITION",
                    [table],
                )
                columns = [
                    f"CAST({backup.quote(name)} AS CHAR)" if kind in backup._MySQL.AS_TEXT
                    else f"HEX({backup.quote(name)})" if "blob" in kind or "binary" in kind or kind == "bit"
                    else backup.quote(name)
                    for name, kind in cursor.fetchall()
                ]
                cursor.execute(f"SELECT {', '.join(columns)} FROM {backup.quote(table)} ORDER BY 1")
                rows = hashlib.sha256(repr(cursor.fetchall()).encode()).hexdigest()
                tables[table] = (definition(create), rows)
            return tables
        cursor.execute("SELECT type, name, sql FROM sqlite_master WHERE name != 'sqlite_sequence' ORDER BY type, name")
        tables["sqlite_master"] = cursor.fetchall()
        for table in connection.introspection.table_names():
            columns = [row[1] for row in cursor.execute(f"PRAGMA table_xinfo({backup.sqlite_quote(table)})")]
            rows = cursor.execute(
                f"SELECT {', '.join('typeof(+' + backup.sqlite_quote(c) + '), +' + backup.sqlite_quote(c) for c in columns)}"
                f" FROM {backup.sqlite_quote(table)} ORDER BY rowid"
            ).fetchall()
            tables[table] = hashlib.sha256(repr(rows).encode()).hexdigest()
        if cursor.execute("SELECT 1 FROM sqlite_master WHERE name = 'sqlite_sequence'").fetchone():
            tables["sqlite_sequence"] = cursor.execute("SELECT name, seq FROM sqlite_sequence ORDER BY name").fetchall()
    return tables


def definition(create):
    """MySQL's CREATE TABLE with the indexes as a set: a table made in one statement (a restore) may
    list its unique indexes in another order than the migrations that built it up."""
    lines = [line.strip().rstrip(",") for line in create.splitlines()]
    keys = {line for line in lines if re.match(r"(PRIMARY |UNIQUE |FULLTEXT |SPATIAL )?KEY ", line)}
    return [line for line in lines if line not in keys], sorted(keys)


def log_row(version, previous=""):
    now = timezone.now()
    return UpgradeLog.objects.create(version=version, previous=previous, started_at=now, finished_at=now)


def some_data():
    """Rows with text that is hard to write as SQL."""
    User.objects.create_user("alice", password="pw-alice-2026", nickname="'单引号' \"双引号\" \\反斜杠 😺")
    User.objects.create_superuser("root", password="pw-root-2026")
    site = SiteSettings.load()
    site.site_name = "小站 -- 不是注释; 也不是结尾"
    site.save()


class StatusTests(TestCase):
    """Which migrations the database lacks or has extra. Only django_migrations rows change here."""

    def setUp(self):
        self.recorder = MigrationRecorder(connection)

    def test_up_to_date(self):
        current = service.status()
        self.assertEqual((current.pending, current.newer, current.last, current.empty, current.foreign), ([], [], None, False, []))

    def test_a_migration_not_applied_yet(self):
        with override_settings(MIGRATION_MODULES=NEW_VERSION):
            self.assertEqual(service.status().pending, ["siteconf.0002_upgrade_test"])

    def test_migrations_from_a_newer_version(self):
        self.recorder.record_applied(*FUTURE)
        # A leftover of squashed migrations, numbered no higher than what this version has: not newer.
        self.recorder.record_applied("siteconf", "0001_squashed_away")
        # An app this version doesn't have: a newer one added it, or it was removed.
        self.recorder.record_applied("notifications", "0001_initial")
        self.assertEqual(service.status().newer, ["siteconf.0099_from_a_newer_version"])
        self.recorder.record_applied("accounts", "no_number_first")  # a name without a number
        self.assertEqual(service.status().newer, ["accounts.no_number_first", "siteconf.0099_from_a_newer_version"])

    def test_migration_numbers(self):
        self.assertEqual(service._number("0008_downloads"), 8)
        self.assertEqual(service._number("0001_squashed_0008_downloads"), 8)
        self.assertIsNone(service._number("initial"))

    def test_the_latest_upgrade(self):
        log_row("0.0.9")
        log_row(VERSION, "0.0.9")
        self.assertEqual(service.status().last.version, VERSION)

    def test_whose_tables(self):
        # An install stopped after Django's first migrations is HFile's, to be finished.
        self.assertEqual(service.foreign_tables(["django_migrations", "auth_user", "django_content_type"], False), [])
        self.assertEqual(service.foreign_tables(["auth_user", "wp_users", "wp_posts"], False), ["wp_posts", "wp_users"])
        self.assertEqual(service.foreign_tables(["wp_users"], True), [])  # once installed, others' tables are fine
        # A new app is counted without being listed anywhere: its label comes from INSTALLED_APPS.
        self.assertTrue(service.own_table().fullmatch("upgrades_upgradelog"))


class VersionApiTests(TestCase):
    url = "/api/v1/manage/version/"

    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.user = User.objects.create_user("alice", password="pw-alice-2026")

    def test_admins_only(self):
        self.assertEqual(self.client.get(self.url).status_code, 401)
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_the_version_and_the_log(self):
        log_row("0.0.9")
        log_row(VERSION, "0.0.9")
        self.client.force_login(self.admin)
        data = self.client.get(self.url).json()
        self.assertEqual(data["version"], VERSION)
        self.assertEqual((data["pending"], data["newer"]), ([], []))
        self.assertEqual([(u["previous"], u["version"]) for u in data["upgrades"]], [("0.0.9", VERSION), ("", "0.0.9")])
        for key in ("django", "python", "database", "auto_upgrade", "backup_dir", "frozen"):
            self.assertIn(key, data)
        self.assertRegex(data["database"], r"^(MySQL|SQLite) \d+\.\d+")

    def test_a_database_that_does_not_match(self):
        MigrationRecorder(connection).record_applied(*FUTURE)
        self.client.force_login(self.admin)
        with override_settings(MIGRATION_MODULES=NEW_VERSION):
            data = self.client.get(self.url).json()
        self.assertEqual((data["pending"], data["newer"]), (["siteconf.0002_upgrade_test"], ["siteconf.0099_from_a_newer_version"]))
        self.assertEqual(data["upgrades"], [])

    def test_the_django_admin_only_shows_it(self):
        log_row(VERSION)
        browser = Client()
        browser.force_login(self.admin)
        self.assertContains(browser.get(admin_url("upgrades/upgradelog/")), VERSION)
        self.assertEqual(browser.get(admin_url("upgrades/upgradelog/add/")).status_code, 403)
        page = browser.get(admin_url(f"upgrades/upgradelog/{UpgradeLog.objects.get().pk}/change/")).content.decode()
        self.assertNotIn('name="version"', page)


class UpgradeTestBase(TransactionTestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hfile-test-backups-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        override = override_settings(BACKUP_DIR=self.dir)
        override.enable()
        self.addCleanup(override.disable)
        self.out = io.StringIO()

    def upgrade(self, **kwargs):
        return service.upgrade(self.out, **kwargs)

    def files(self):
        return sorted(path.name for path in self.dir.iterdir())

    def new_version(self, modules=NEW_VERSION):
        """From now on the program is a new version with siteconf's test migrations; when the test ends
        they are taken back, so the database is the real version's again. (Other settings these tests
        change are changed inside the test with `with`: a method's @override_settings would end before
        this cleanup, and take this override with it.)"""
        override = override_settings(MIGRATION_MODULES=modules)
        override.enable()

        def back():
            override.disable()
            with override_settings(MIGRATION_MODULES=modules):
                call_command("migrate", "siteconf", "0001_initial", verbosity=0)
            self.drop("siteconf_half_done")  # what a failed 0003 may have left (MySQL can't take DDL back)

        self.addCleanup(back)

    def record_a_newer_version(self):
        recorder = MigrationRecorder(connection)
        recorder.record_applied(*FUTURE)
        self.addCleanup(recorder.record_unapplied, *FUTURE)  # flushing leaves django_migrations alone

    def table_exists(self, name):
        return name in connection.introspection.table_names()

    @staticmethod
    def drop(table):
        with connection.cursor() as cursor:
            cursor.execute(f"DROP TABLE IF EXISTS {table}")


class UpgradeTests(UpgradeTestBase):
    def test_nothing_to_do_once_this_version_has_started(self):
        first = self.upgrade()  # migrated by Django's test runner: no row yet
        self.assertEqual((first.version, first.previous, first.migrations, first.backup), (VERSION, "", [], ""))
        self.assertIsNone(self.upgrade())
        self.assertEqual(UpgradeLog.objects.count(), 1)
        self.assertEqual(self.files(), [])  # nothing changed, so nothing was backed up

    def test_a_new_version_without_migrations(self):
        log_row("0.0.9")
        row = self.upgrade()
        self.assertEqual((row.previous, row.version, row.migrations, row.backup), ("0.0.9", VERSION, [], ""))
        self.assertIn("第一次在这个数据库上启动（原来是 0.0.9），数据库不用改", self.out.getvalue())
        self.assertEqual(self.files(), [])

    def test_a_new_version_with_migrations(self):
        some_data()
        log_row("0.0.9")
        self.new_version()

        row = self.upgrade()

        self.assertEqual((row.previous, row.version, row.migrations), ("0.0.9", VERSION, ["siteconf.0002_upgrade_test"]))
        self.assertEqual(service.status().pending, [])
        self.assertEqual(SiteSettings.load().site_name, "升级后的名字")
        with connection.cursor() as cursor:
            cursor.execute("SELECT note FROM siteconf_upgrade_test")
            self.assertEqual(list(cursor.fetchall()), [("升级测试",)])

        saved = Path(row.backup)
        self.assertEqual(saved.parent, self.dir)
        self.assertRegex(saved.name, rf"^{backup.database_label()}-\d{{8}}-\d{{6}}-before-{re.escape(VERSION)}\.sql\.gz$")
        assert_private(self, saved)
        self.assertEqual(self.files(), [saved.name])  # no .part left behind
        with gzip.open(saved, "rt", encoding="utf-8") as file:
            text = file.read()
        self.assertIn("小站 -- 不是注释; 也不是结尾", text)
        self.assertNotIn("siteconf_upgrade_test", text)  # taken before the upgrade

        output = self.out.getvalue()
        for line in ("先备份整个数据库", "已备份", "升级数据库到", "Applying siteconf.0002_upgrade_test... OK", "数据库已升级到"):
            self.assertIn(line, output)

    def test_a_failed_migration_puts_the_backup_back(self):
        some_data()
        log_row("0.0.9")
        before = snapshot()
        self.new_version(FAILING_VERSION)

        with self.assertRaises(service.UpgradeError) as caught:
            self.upgrade()

        # Every table as it was, django_migrations and the log too: 0002 went through and 0003 half,
        # and neither is left. The table 0002 made is gone, the row it changed is back.
        self.assertEqual(snapshot(), before)
        self.assertFalse(self.table_exists("siteconf_upgrade_test"))
        self.assertFalse(self.table_exists("siteconf_half_done"))
        self.assertEqual(SiteSettings.load().site_name, "小站 -- 不是注释; 也不是结尾")
        message = str(caught.exception)
        self.assertIn("升级失败：RuntimeError: 这个迁移故意失败（升级的测试）", message)
        self.assertIn("数据库已经恢复到升级前，原来的版本（0.0.9）可以接着用", message)
        output = self.out.getvalue()
        self.assertIn("Applying siteconf.0002_upgrade_test... OK", output)
        self.assertIn("正在把数据库恢复到升级前", output)
        self.assertEqual(len(self.files()), 1)  # the backup stays

    def test_when_putting_the_backup_back_fails_too(self):
        self.new_version(FAILING_VERSION)
        with (
            mock.patch.object(backup, "restore", side_effect=OperationalError(2006, "MySQL server has gone away")),
            self.assertRaises(service.UpgradeError) as caught,
        ):
            self.upgrade()
        message = str(caught.exception)
        self.assertIn("恢复也失败了", message)
        self.assertIn(f"恢复升级前的备份：{self.dir}", message)

    def test_a_new_empty_database(self):
        backup.drop_tables()
        self.addCleanup(call_command, "migrate", verbosity=0)  # in case the test fails half way
        self.assertTrue(service.status().empty)

        def half_then_fail(*args, **kwargs):
            with connection.cursor() as cursor:
                cursor.execute("CREATE TABLE django_migrations (id int)")
                cursor.execute("CREATE TABLE accounts_user (id int)")
            raise OperationalError(1050, "Table 'x' already exists")

        # A first install that fails leaves the database empty again ...
        with mock.patch.object(service, "call_command", side_effect=half_then_fail):
            with self.assertRaises(service.UpgradeError) as caught:
                self.upgrade()
        self.assertIn("数据库已经恢复成空的", str(caught.exception))
        self.assertEqual(connection.introspection.table_names(), [])
        self.assertEqual(self.files(), [])  # nothing to back up

        # ... and one that works creates every table.
        row = self.upgrade()
        self.assertEqual((row.previous, row.backup), ("", ""))
        self.assertIn("upgrades.0001_initial", row.migrations)
        self.assertIn(f"新的空数据库（{connection.settings_dict['NAME']}）：建表", self.out.getvalue())
        self.assertEqual(service.status().pending, [])

    def test_a_database_from_a_newer_version(self):
        log_row("9.9.0", VERSION)
        self.record_a_newer_version()
        before = snapshot()
        with self.assertRaises(service.UpgradeError) as caught:
            self.upgrade()
        message = str(caught.exception)
        self.assertIn("数据库已经被 9.9.0 版升级过", message)
        self.assertIn("siteconf.0099_from_a_newer_version", message)
        self.assertIn(str(self.dir), message)
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.files(), [])

    def test_another_programs_database(self):
        backup.drop_tables()
        self.addCleanup(call_command, "migrate", verbosity=0)
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE wp_posts (id int)")
        self.addCleanup(self.drop, "wp_posts")
        before = snapshot()
        with self.assertRaises(service.UpgradeError) as caught:
            self.upgrade()
        self.assertIn("不是 HFile 的数据库，没有升级，也没有改动", str(caught.exception))
        self.assertEqual(snapshot(), before)  # nothing made, nothing changed
        self.assertEqual(self.files(), [])

    def test_no_upgrade_without_a_backup(self):
        self.new_version()
        before = snapshot()
        (self.dir / "a-file").write_text("")
        with override_settings(BACKUP_DIR=self.dir / "a-file" / "backups"):  # can't be created
            with self.assertRaises(service.UpgradeError) as caught:
                self.upgrade()
        self.assertIn("备份失败，没有升级，数据库没有改动", str(caught.exception))
        self.assertIn("[paths] backups", str(caught.exception))
        self.assertEqual(snapshot(), before)

    def test_stopped_while_upgrading(self):
        """Ctrl+C or SIGTERM (serve raises SystemExit for it) in the middle of a migration: the backup goes back."""
        some_data()
        before = snapshot()
        self.new_version()
        real = call_command

        def interrupted(*args, **kwargs):
            real(*args, **kwargs)  # the migration went through: then the signal came
            raise SystemExit(0)

        with mock.patch.object(service, "call_command", side_effect=interrupted), self.assertRaises(service.UpgradeError) as caught:
            self.upgrade()
        self.assertIn("数据库已经恢复到升级前", str(caught.exception))
        self.assertEqual(snapshot(), before)

    @mysql_only
    def test_one_upgrade_at_a_time(self):
        other = connections.create_connection("default")  # as another process
        self.addCleanup(other.close)
        name = f"hfile-upgrade-{connection.settings_dict['NAME']}"
        with other.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0)", [name])
            self.assertEqual(cursor.fetchone()[0], 1)
        with self.assertRaises(service.UpgradeError) as caught:
            self.upgrade(lock_timeout=1)
        self.assertIn("等了 1 秒，另一个进程还在升级或恢复这个数据库", str(caught.exception))
        self.assertIn("另一个进程正在升级或恢复这个数据库，等它结束", self.out.getvalue())
        self.assertFalse(UpgradeLog.objects.exists())

        other.close()  # it finished: the lock goes with its connection
        self.assertEqual(self.upgrade().version, VERSION)

    def test_sqlite_locks_a_file(self):
        """SQLite's lock (any database here: it only needs a file): BEGIN EXCLUSIVE on a file next to it."""
        path = self.dir / "site.sqlite3.lock"
        other = sqlite3.connect(path, timeout=0, isolation_level=None)  # as another process
        self.addCleanup(other.close)
        other.execute("BEGIN EXCLUSIVE")
        with self.assertRaises(service.UpgradeError) as caught, service.file_lock(path, self.out, timeout=1):
            pass
        self.assertIn("等了 1 秒，另一个进程还在升级或恢复这个数据库", str(caught.exception))
        self.assertIn("另一个进程正在升级或恢复这个数据库，等它结束", self.out.getvalue())
        other.close()  # it finished: the system lets go of its lock
        with service.file_lock(path, self.out, timeout=1):
            third = sqlite3.connect(path, timeout=0, isolation_level=None)
            # Closed by hand: a connection is freed by the garbage collector only, and while it is open Windows doesn't
            # delete the file with the test's folder.
            self.addCleanup(third.close)
            with self.assertRaises(sqlite3.OperationalError):  # now this one has it
                third.execute("BEGIN EXCLUSIVE")
        with self.assertRaisesMessage(service.UpgradeError, f"数据库文件所在的文件夹 {self.dir / 'no-such-folder'} 不存在，或者不能写入"):
            with service.file_lock(self.dir / "no-such-folder" / "x.lock", self.out):
                pass

    def assert_ctrl_c_stops_the_wait(self, wait):
        """Ctrl+C half a second into `wait` for another process's lock stops it within a second or so, not
        once the lock comes free or the wait gives up (10 s). The SIGINT comes from another thread: Python
        handles it in the main thread, which is in the wait, once that returns from C (SQLite, the MySQL
        driver), as it does for Ctrl+C. Here it raises Interrupted, not KeyboardInterrupt."""

        class Interrupted(Exception):
            pass

        def interrupted(signum, frame):
            raise Interrupted

        self.addCleanup(signal.signal, signal.SIGINT, signal.signal(signal.SIGINT, interrupted))
        real = service._waiting
        timers = []

        def stop_timers():  # before the handler goes back: a Ctrl+C after that would stop the whole test run
            for timer in timers:
                timer.cancel()
                timer.join()

        self.addCleanup(stop_timers)

        def waiting(out):
            real(out)
            timers.append(threading.Timer(0.5, signal.raise_signal, [signal.SIGINT]))
            timers[-1].start()

        started = time.monotonic()
        with mock.patch.object(service, "_waiting", waiting), self.assertRaises(Interrupted):
            wait()
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(timers), 1)

    def test_ctrl_c_while_waiting_for_the_lock_file(self):
        path = self.dir / "site.sqlite3.lock"
        other = sqlite3.connect(path, timeout=0, isolation_level=None)  # as another process
        self.addCleanup(other.close)
        other.execute("BEGIN EXCLUSIVE")

        def wait():
            with service.file_lock(path, self.out, timeout=10):
                pass

        self.assert_ctrl_c_stops_the_wait(wait)

    @mysql_only
    def test_ctrl_c_while_waiting_for_the_mysql_lock(self):
        other = connections.create_connection("default")  # as another process
        self.addCleanup(other.close)
        with other.cursor() as cursor:
            cursor.execute("SELECT GET_LOCK(%s, 0)", [f"hfile-upgrade-{connection.settings_dict['NAME']}"])
            self.assertEqual(cursor.fetchone()[0], 1)
        try:
            self.assert_ctrl_c_stops_the_wait(lambda: self.upgrade(lock_timeout=10))
        finally:
            # The driver's answer to the interrupted query was never read: the connection can't go on
            # ("commands out of sync"). The process ends after Ctrl+C; here the next query makes a new one.
            connection.close()
        self.assertFalse(UpgradeLog.objects.exists())

    @sqlite_only
    def test_one_upgrade_at_a_time_on_sqlite(self):
        if connection.is_in_memory_db():  # the tests' database: only this process can open it
            self.assertTrue(service._memory_lock.acquire())  # as another thread
            threading.Timer(1.5, service._memory_lock.release).start()
            self.assertEqual(self.upgrade(lock_timeout=10).version, VERSION)
            self.assertIn("另一个进程正在升级或恢复这个数据库，等它结束", self.out.getvalue())
        else:
            self.assertEqual(service.lock_file(), Path(connection.settings_dict["NAME"] + ".lock"))


class LockFileTests(UpgradeTestBase):
    def test_one_this_process_cant_write_is_refused(self):
        # SQLite opens it read-only then, and "locks" it without keeping anybody out.
        lock = self.dir / "site.sqlite3.lock"
        lock.touch()
        lock.chmod(0o444)
        self.addCleanup(lock.chmod, 0o600)  # before the folder goes: Windows doesn't delete a read-only file
        if os.access(lock, os.W_OK):
            self.skipTest("running as root: every file can be written")
        with self.assertRaisesMessage(service.UpgradeError, "锁文件"):
            with service.file_lock(lock, io.StringIO(), timeout=1):
                pass

    def test_one_that_isnt_sqlites_is_a_message(self):
        lock = self.dir / "site.sqlite3.lock"
        lock.write_bytes(b"this is not a database at all, but it is long enough to look like one" * 20)
        with self.assertRaises(service.UpgradeError):
            with service.file_lock(lock, io.StringIO(), timeout=1):
                pass

    def test_a_good_one_stays_empty(self):
        lock = self.dir / "site.sqlite3.lock"
        with service.file_lock(lock, io.StringIO(), timeout=1):
            self.assertEqual(lock.stat().st_size, 0)  # the write that tests it is never committed

    def test_only_its_owner_can_open_it(self):
        # Whoever can read it can hold a lock on it, and keep upgrades and restores waiting.
        lock = self.dir / "site.sqlite3.lock"
        old = os.umask(0)
        try:
            with service.file_lock(lock, io.StringIO(), timeout=1):
                assert_private(self, lock)
            readable_by_all(lock)  # left by an older version
            with service.file_lock(lock, io.StringIO(), timeout=1):
                assert_private(self, lock)
        finally:
            os.umask(old)


class StartupTests(UpgradeTestBase):
    def test_upgrades_before_serving(self):
        self.new_version()
        with override_settings(AUTO_UPGRADE=True):
            upgrade_on_start(self.out)
        self.assertEqual(service.status().pending, [])
        row = UpgradeLog.objects.get()
        self.assertEqual((row.version, row.migrations), (VERSION, ["siteconf.0002_upgrade_test"]))
        self.assertTrue(Path(row.backup).is_file())  # backed up first

    def test_does_not_start_on_a_database_it_cannot_use(self):
        self.record_a_newer_version()
        with override_settings(AUTO_UPGRADE=True), self.assertRaises(SystemExit) as caught:
            upgrade_on_start(self.out)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn(f"HFile {VERSION} 没有启动。数据库已经被更新的版本升级过", self.out.getvalue())

    def test_does_not_start_without_the_database(self):
        down = OperationalError(2003, "Can't connect to MySQL server on '127.0.0.1:23306'")
        with (
            override_settings(AUTO_UPGRADE=True),
            mock.patch.object(service, "upgrade", side_effect=down),
            self.assertRaises(SystemExit) as caught,
        ):
            upgrade_on_start(self.out)
        self.assertEqual(caught.exception.code, 1)
        self.assertIn("读不了数据库", self.out.getvalue())
        self.assertIn(WHAT_TO_CHECK, self.out.getvalue())

    def test_without_auto_it_only_checks(self):
        with override_settings(AUTO_UPGRADE=False):
            upgrade_on_start(self.out)  # matches: starts
        self.assertFalse(UpgradeLog.objects.exists())  # and writes nothing
        self.new_version()
        with (
            override_settings(AUTO_UPGRADE=False),
            mock.patch.object(service, "upgrade") as upgrade,
            self.assertRaises(SystemExit) as caught,
        ):
            upgrade_on_start(self.out)
        upgrade.assert_not_called()
        self.assertEqual(caught.exception.code, 1)
        output = self.out.getvalue()
        self.assertIn("数据库要升级：还差 1 个迁移（siteconf.0002_upgrade_test）", output)
        self.assertIn("先运行 python -B manage.py upgrade（会先备份整个数据库）", output)
        self.assertEqual(service.status().pending, ["siteconf.0002_upgrade_test"])  # nothing was done
        with override_settings(FROZEN=True), mock.patch.object(sys, "executable", "/srv/hfile/hfile"):
            self.assertEqual(startup.upgrade_command(), "hfile upgrade")  # the single executable: its own

    def test_without_auto_a_newer_database_stops_it_too(self):
        self.record_a_newer_version()
        with override_settings(AUTO_UPGRADE=False), self.assertRaises(SystemExit):
            upgrade_on_start(self.out)
        self.assertIn("数据库已经被更新的版本升级过", self.out.getvalue())

    def test_runserver_only_warns(self):
        self.new_version()
        with override_settings(AUTO_UPGRADE=False), mock.patch.object(startup, "LENIENT", True):
            upgrade_on_start(self.out)  # no SystemExit
        self.assertIn("注意：数据库要升级", self.out.getvalue())

    def test_not_before_installing(self):
        with (
            override_settings(SETUP_MODE=True, AUTO_UPGRADE=True),
            mock.patch.object(service, "upgrade") as upgrade,
            mock.patch.object(service, "status") as status,
        ):
            upgrade_on_start(self.out)
        upgrade.assert_not_called()
        status.assert_not_called()


class UnreachableTests(SimpleTestCase):
    """What unreachable() says when MySQL can't be logged in to."""

    def say(self, error):
        with mock.patch.object(service.connection, "vendor", "mysql"):  # whichever database the tests run on
            return service.unreachable(error)

    def test_mysql_without_tls(self):
        # OPS-07: MySQL with TLS off, an account with caching_sha2_password (8.4's default) that nobody has
        # logged in with since MySQL started: it said to check host, port, name, user and password.
        message = self.say(OperationalError(
            2061, "Authentication plugin 'caching_sha2_password' reported error: Authentication requires secure connection."
        ))
        self.assertIn("MySQL 没有开 TLS", message)
        self.assertIn("用 mysql 客户端以这个用户登录一次", message)
        self.assertNotIn("host、port", message)

    def test_tls_that_cant_be_had(self):
        # SEC-08: [database] tls and ca, and a CA that didn't sign the server's certificate, say.
        message = self.say(OperationalError(2026, "SSL connection error: error:0A000086:SSL routines::certificate verify failed"))
        self.assertIn("检查设置文件 config.toml 里 [database] 的 tls 和 ca", message)
        self.assertNotIn("host、port", message)

    @mysql_only
    def test_tls_to_the_tests_mysql(self):
        # SEC-08: [database] tls reaches the driver. With "verify_ca" and a CA that didn't sign the server's
        # certificate (the public ones, certifi's), there is no connection.
        def connect(**values):
            options = database.django_settings({**settings.CONFIG["database"], **values}, settings.CONFIG_FILE)["OPTIONS"]
            other = connections["default"].__class__({**connection.settings_dict, "OPTIONS": options}, alias="tls")
            self.addCleanup(other.close)
            return other

        with connect(tls="required").cursor() as cursor:
            cursor.execute("SHOW SESSION STATUS LIKE 'Ssl_version'")
            self.assertRegex(cursor.fetchone()[1], r"^TLSv1\.[23]$")
        with self.assertRaises(OperationalError) as caught:
            connect(tls="verify_ca", ca=certifi.where()).ensure_connection()
        self.assertEqual(caught.exception.args[0], 2026)
        self.assertIn("[database] 的 tls 和 ca", service.unreachable(caught.exception))


class CommandTests(UpgradeTestBase):
    def call(self, *args, **kwargs):
        out = io.StringIO()
        call_command(*args, stdout=out, **kwargs)
        return out.getvalue()

    def check_code(self):
        try:
            self.call("upgrade", "--check")
        except SystemExit as exit:
            return exit.code
        return 0

    def test_upgrade_check(self):
        self.assertEqual(self.check_code(), 0)
        self.assertIn("数据库不用升级", self.call("upgrade", "--check"))
        with override_settings(MIGRATION_MODULES=NEW_VERSION):
            self.assertEqual(self.check_code(), 1)
            try:
                self.call("upgrade", "--check")
            except SystemExit:
                pass
        self.record_a_newer_version()
        self.assertEqual(self.check_code(), 2)

    def test_upgrade_check_on_another_programs_database(self):
        current = service.Status(pending=["accounts.0001_initial"], newer=[], last=None, empty=False, foreign=["wp_users"])
        with mock.patch.object(service, "status", return_value=current):
            self.assertEqual(self.check_code(), 3)  # not 1, "needs an upgrade": upgrade would refuse it

    def test_upgrade_check_lists_every_migration_of_an_empty_database(self):
        backup.drop_tables()
        self.addCleanup(call_command, "migrate", verbosity=0)
        out = io.StringIO()
        with self.assertRaises(SystemExit) as caught:
            call_command("upgrade", "--check", stdout=out)
        self.assertEqual(caught.exception.code, 1)
        listed = re.findall(r"^  (\w+\.\w+)$", out.getvalue(), re.MULTILINE)
        self.assertIn("accounts.0001_initial", listed)
        self.assertIn("upgrades.0001_initial", listed)
        self.assertIn("siteconf.0001_initial", listed)
        self.assertIn(f"需要升级：{len(listed)} 个迁移", out.getvalue())

    def test_upgrade(self):
        self.new_version()
        self.assertIn("数据库已升级到", self.call("upgrade"))
        self.assertIn(f"数据库已经是 {VERSION} 版的，不用升级", self.call("upgrade"))
        self.record_a_newer_version()
        with self.assertRaises(CommandError) as caught:
            self.call("upgrade")
        self.assertIn("数据库已经被更新的版本升级过", str(caught.exception))

    def test_without_the_database(self):
        down = OperationalError(2003, "Can't connect to MySQL server on '127.0.0.1:23306'")
        for args, code in ((["upgrade"], 1), (["upgrade", "--check"], 4)):  # --check: not 1, "needs an upgrade"
            with mock.patch.object(service, "status", side_effect=down), self.assertRaises(CommandError) as caught:
                self.call(*args)
            self.assertIn("读不了数据库", str(caught.exception))
            self.assertIn(WHAT_TO_CHECK, str(caught.exception))
            self.assertEqual(caught.exception.returncode, code)
