"""Backups of the whole database and restoring them (apps/upgrades/backup.py, `backup` and `restore`), on the
database the tests run on. Each restore is compared table by table with the database before it."""

import datetime
import gzip
import io
import os
import time
import unittest
from decimal import Decimal
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.db import DatabaseError, OperationalError, connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from config import configfile
from config.version import VERSION
from apps.core import cleanup
from apps.core.testing import assert_private, mysql_only, sqlite_only
from apps.upgrades import backup

from .test_upgrades import FUTURE, MYSQL, UpgradeTestBase, log_row, snapshot, some_data

User = get_user_model()


class BackupTests(UpgradeTestBase):
    def drop_kinds(self):
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW IF EXISTS backup_kinds_view")
            cursor.execute("DROP TABLE IF EXISTS backup_kinds")

    @mysql_only
    def test_restore_gives_back_every_table_as_it_was(self):
        some_data()
        self.addCleanup(self.drop_kinds)
        with connection.cursor() as cursor:  # a column of each kind MySQL has, with values hard to write as SQL
            cursor.execute(
                "CREATE TABLE backup_kinds (id int PRIMARY KEY AUTO_INCREMENT, t longtext, b longblob, j json,"
                " dt datetime(6), d date, tm time(6), num decimal(10, 4), f double, n int, e enum('a', 'b'),"
                " s set('x', 'y'), bits bit(8), twice int GENERATED ALWAYS AS (n * 2) STORED, vb varbinary(8),"
                " ts timestamp(6) NULL, zero datetime NULL)"
            )
            cursor.execute(
                "INSERT INTO backup_kinds (t, b, j, dt, d, tm, num, f, n, e, s, bits, vb)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    "'单引号' \"双引号\" \\反斜杠\\ %s 换行\n回车\r制表\t空\x00字\x1a 😺 -- 不是注释; 也不是结尾",
                    bytes(range(256)),
                    '{"名字": "文\\"件", "列表": [1, 0.30000000000000004, null, true], "嵌套": {"x": "\\n"}}',
                    datetime.datetime(2026, 11, 4, 23, 59, 59, 123456),
                    datetime.date(1999, 12, 31),
                    "-22:59:59.999500",
                    Decimal("-12.3456"),
                    "0.30000000000000004",
                    -7,
                    "b",
                    "x,y",
                    b"\x05",
                    b"",
                ],
            )
            cursor.execute("INSERT INTO backup_kinds (t) VALUES (NULL)")  # NULL everywhere
            # What old data may hold: a zero date, which strict mode no longer lets in.
            cursor.execute("SET @strict = @@SESSION.sql_mode, SESSION sql_mode = ''")
            cursor.execute("INSERT INTO backup_kinds (zero, ts) VALUES ('0000-00-00 00:00:00', '2026-11-04 12:00:00.5')")
            cursor.execute("SET SESSION sql_mode = @strict")
        before = snapshot()
        path = self.dir / "all.sql.gz"
        with connection.cursor() as cursor:  # a session in another time zone: TIMESTAMPs are kept in UTC
            cursor.execute("SET SESSION time_zone = '+08:00'")
            self.assertEqual(backup.dump(path), len(before))
            cursor.execute("SELECT @@SESSION.time_zone, @@SESSION.transaction_isolation")
            self.assertEqual(cursor.fetchone(), ("+08:00", "READ-COMMITTED"))  # the session is as it was
            cursor.execute("SET SESSION time_zone = 'SYSTEM'")

        # Change rows, drop a table, add one: the restore undoes all of it.
        User.objects.update(nickname="改过了")
        User.objects.create_user("carol", password="pw-carol-2026")
        with connection.cursor() as cursor:
            cursor.execute("DROP TABLE backup_kinds")
            cursor.execute("CREATE TABLE upgrades_extra (id int)")
            cursor.execute("UPDATE django_migrations SET name = 'renamed' WHERE app = 'upgrades'")
        info = backup.restore(path)

        self.assertEqual(snapshot(), before)
        self.assertEqual((info.version, info.schema, info.database), (VERSION, "", connection.settings_dict["NAME"]))
        with connection.cursor() as cursor:  # MySQL computes the generated column again
            cursor.execute(
                "SELECT twice, HEX(b) = HEX(%s), CAST(tm AS CHAR), f FROM backup_kinds WHERE n = -7", [bytes(range(256))]
            )
            self.assertEqual(cursor.fetchone(), (-14, 1, "-22:59:59.999500", 0.30000000000000004))

    @sqlite_only
    def test_sqlite_gives_back_every_table_as_it_was(self):
        some_data()
        self.addCleanup(self.drop_kinds)
        with connection.cursor() as cursor:  # values that are hard to write as SQL, of each kind SQLite has
            cursor.execute(
                "CREATE TABLE backup_kinds (id integer PRIMARY KEY AUTOINCREMENT, t text, b blob, r real, n integer,"
                " d datetime, flag bool, twice integer GENERATED ALWAYS AS (n * 2) STORED, \"we\"\"ird\" text)"
            )
            cursor.execute("CREATE INDEX backup_kinds_n ON backup_kinds (n DESC, lower(t))")
            cursor.execute("CREATE VIEW backup_kinds_view AS SELECT id, n FROM backup_kinds")
            cursor.execute("CREATE TRIGGER backup_kinds_trigger AFTER DELETE ON backup_kinds BEGIN SELECT 1; END")
            cursor.execute(
                "INSERT INTO backup_kinds (t, b, r, n, d, flag, \"we\"\"ird\") VALUES (%s, %s, %s, %s, %s, %s, %s)",
                ["'单引号' \"双引号\" \\反斜杠 换行\n回车\r制表\t空\x00字 😺 -- 不是注释; 也不是结尾", bytes(range(256)),
                 0.30000000000000004, 7, "不是日期", 2, "x"],
            )
            # inf, the largest and smallest integers, and a row whose id is not used again
            cursor.execute("INSERT INTO backup_kinds (r, n) VALUES (9e999, 9223372036854775807), (-9e999, -9223372036854775808)")
            cursor.execute("INSERT INTO backup_kinds (t) VALUES (NULL)")
            cursor.execute("DELETE FROM backup_kinds WHERE id = 4")
        before = snapshot()
        path = self.dir / "all.sql.gz"
        self.assertEqual(backup.dump(path), len(connection.introspection.table_names()))

        User.objects.update(nickname="改过了")
        User.objects.create_user("carol", password="pw-carol-2026")
        with connection.cursor() as cursor:
            cursor.execute("DROP VIEW backup_kinds_view")
            cursor.execute("DROP TABLE backup_kinds")
            cursor.execute("CREATE TABLE upgrades_extra (id integer)")
            cursor.execute("UPDATE django_migrations SET name = 'renamed' WHERE app = 'upgrades'")
        info = backup.restore(path)

        self.assertEqual(snapshot(), before)  # rows as stored (types too), sqlite_master, the counters
        self.assertEqual((info.version, info.engine, info.database), (VERSION, "SQLite", backup._SQLite.database_name()))
        with connection.cursor() as cursor:
            # As stored, not as Django reads a datetime or bool column; SQLite computes `twice` again.
            cursor.execute("SELECT typeof(d), +d, +flag, twice, \"we\"\"ird\" FROM backup_kinds WHERE n = 7")
            self.assertEqual(cursor.fetchone(), ("text", "不是日期", 2, 14, "x"))
            cursor.execute("INSERT INTO backup_kinds (n) VALUES (1)")
            cursor.execute("SELECT max(id) FROM backup_kinds")
            self.assertEqual(cursor.fetchone()[0], 5)  # AUTOINCREMENT counts on from 4

    def test_the_file(self):
        some_data()
        path = self.dir / "new" / "all.sql.gz"
        backup.dump(path)
        assert_private(self, path)  # password hashes are in it
        assert_private(self, path.parent, 0o700)  # a new folder is the owner's too
        with gzip.open(path, "rt", encoding="utf-8") as file:
            lines = file.read().splitlines()
        self.assertEqual(lines[0], backup.TITLE)
        self.assertEqual(lines[1], f"-- 备份时的程序版本：{VERSION}")
        self.assertNotIn("-- 数据库的版本", "\n".join(lines[:7]))  # no upgrade recorded yet
        self.assertIn(f"-- 数据库类型：{backup.engine()}", lines[:7])
        self.assertRegex(lines[-1], r"^-- 备份结束：\d+ 张表$")
        statements = [line for line in lines if not line.startswith("--")]
        self.assertTrue(all(line.endswith(";") for line in statements))
        if MYSQL:  # before any data: no NO_BACKSLASH_ESCAPES while reading it, TIMESTAMPs in UTC
            self.assertEqual(statements[2:4], ["SET @OLD_SQL_MODE = @@SQL_MODE, SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';",
                                               "SET @OLD_TIME_ZONE = @@TIME_ZONE, TIME_ZONE = '+00:00';"])
        else:  # for the sqlite3 program: one transaction, foreign keys checked at the end
            self.assertEqual((statements[:2], statements[-1]), (["PRAGMA foreign_keys = OFF;", "BEGIN;"], "COMMIT;"))

    def test_a_failed_backup_leaves_no_file(self):
        some_data()
        with mock.patch.object(backup._dialect(), "_write_table", side_effect=OperationalError(2013, "Lost connection")):
            with self.assertRaises(OperationalError):
                backup.dump(self.dir / "all.sql.gz")
        self.assertEqual(self.files(), [])
        self.assertTrue(User.objects.exists())  # the connection still works, outside the snapshot

    def test_what_a_backup_cut_short_left_is_cleaned_up(self):
        # OPS-06: a backup cut short (kill -9, a power cut) left its .part file behind, password hashes in it,
        # and nothing removed it. The cleanup (apps.core.cleanup) removes one untouched for a day: a backup being
        # written touches its file all the time.
        left = self.dir / "hfile-20261101-020000.sql.gz.part"
        writing = self.dir / "hfile-20261113-020000.sql.gz.part"
        whole = self.dir / "hfile-20261101-010000.sql.gz"
        other = self.dir / "notes.part"
        for path in (left, writing, whole, other):
            path.write_bytes(b"x")
        two_days_ago = time.time() - 2 * 24 * 3600
        for path in (left, whole, other):
            os.utime(path, (two_days_ago, two_days_ago))
        job = {job.name: job for job in cleanup.jobs()}["backup_parts"]
        with mock.patch.dict(cleanup._jobs, {"backup_parts": job}, clear=True):
            with self.assertLogs("hfile.cleanup", "INFO"):
                self.assertEqual(cleanup.run(), [("backup_parts", 1)])
            self.assertEqual(self.files(), sorted(path.name for path in (writing, whole, other)))
            with override_settings(BACKUP_DIR=self.dir / "none yet"):
                self.assertEqual(cleanup.run(), [("backup_parts", 0)])

    def test_two_backups_never_share_a_file(self):
        path = backup.new_path()
        path.with_name(path.name + ".part").write_bytes(b"")  # another backup writing to it
        self.assertNotEqual(backup.new_path(), path)
        with self.assertRaises(FileExistsError):
            backup.dump(path)

    def test_only_whole_backups_are_restored(self):
        some_data()
        good = self.dir / "good.sql.gz"
        backup.dump(good)
        cut = self.dir / "cut.sql.gz"
        cut.write_bytes(good.read_bytes()[: good.stat().st_size // 2])
        with gzip.open(good, "rt", encoding="utf-8") as file:
            lines = file.read().splitlines()
        unfinished = self.dir / "unfinished.sql.gz"
        with gzip.open(unfinished, "wt", encoding="utf-8", newline="\n") as file:  # as dump() writes them, on Windows too
            file.write("\n".join(lines[:-1]) + "\n")
        other = self.dir / "other.sql.gz"
        with gzip.open(other, "wt", encoding="utf-8", newline="\n") as file:
            file.write("DROP TABLE accounts_user;\n")
        no_kind = self.dir / "no-kind.sql.gz"
        with gzip.open(no_kind, "wt", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(line for line in lines if not line.startswith("-- 数据库类型")) + "\n")
        plain = self.dir / "plain.sql"
        plain.write_text("\n".join(lines) + "\n", encoding="utf-8")

        before = snapshot()
        for path, problem in ((cut, "备份不完整"), (unfinished, "备份不完整"), (other, "不是 HFile"),
                              (no_kind, "没有写数据库类型"), (plain, "不是 gzip 压缩的文件")):
            with self.subTest(path.name), self.assertRaises(backup.BadBackup) as caught:
                backup.restore(path)
            self.assertIn(problem, str(caught.exception), path.name)
        self.assertEqual(snapshot(), before)  # nothing was dropped

    def crafted(self, extra_lines):
        good = self.dir / "good.sql.gz"
        backup.dump(good)
        with gzip.open(good, "rt", encoding="utf-8", newline="\n") as file:
            lines = file.read().split("\n")
        crafted = self.dir / "crafted.sql.gz"
        at = next(i for i, line in enumerate(lines) if line and not line.startswith("--")) + 2
        with gzip.open(crafted, "wt", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(lines[:at] + extra_lines + lines[at:]))
        return crafted

    @sqlite_only
    def test_a_sqlite_restore_runs_only_what_a_backup_has(self):
        some_data()
        planted = self.dir / "planted.db"
        before = snapshot()
        for lines in (["commit;"], [f"ATTACH DATABASE '{planted}' AS x;", "CREATE TABLE x.t(a);"], ["PRAGMA foreign_keys = ON;"]):
            with self.subTest(lines[0]), self.assertRaises(DatabaseError):
                backup.restore(self.crafted(lines))
        self.assertFalse(planted.exists())
        self.assertEqual(snapshot(), before)  # one transaction: nothing of it stayed

    @mysql_only
    def test_a_mysql_restore_runs_only_what_a_backup_has(self):
        some_data()
        before = snapshot()
        for lines in (["GRANT ALL ON *.* TO x;"], ["DROP TABLE IF EXISTS `other_site`.`wp_users`;"],
                      ["CREATE TABLE `stolen` SELECT * FROM `mysql`.`user`;"]):
            with self.subTest(lines[0]), self.assertRaises(backup.BadBackup):
                backup.restore(self.crafted(lines))
        self.assertEqual(snapshot(), before)  # checked before anything was dropped

    def test_a_line_too_long_is_refused_before_it_is_read_whole(self):
        crafted = self.dir / "long.sql.gz"
        with gzip.open(crafted, "wt", encoding="utf-8", newline="\n") as file:
            file.write(f"{backup.TITLE}\n" + "x" * 2000 + "\n")
        with mock.patch.object(backup, "LINE_MAX", 1000), self.assertRaises(backup.BadBackup):
            backup.read_info(crafted)

    def test_new_file_names(self):
        moment = timezone.make_aware(datetime.datetime(2026, 11, 4, 23, 5, 9))
        with mock.patch("apps.upgrades.backup.timezone.localtime", return_value=moment):
            first = backup.new_path("before-1.2.3")
            self.assertEqual(first.parent, self.dir)
            self.assertEqual(first.name, f"{backup.database_label()}-20261104-230509-before-1.2.3.sql.gz")
            self.assertEqual(backup.new_path("../../别处/x y").parent, self.dir)
            first.write_text("")
            self.assertEqual(backup.new_path("before-1.2.3").name, first.name.replace(".sql.gz", "-2.sql.gz"))


class RestoreCommandTests(UpgradeTestBase):
    def call(self, *args, **kwargs):
        out = io.StringIO()
        call_command(*args, stdout=out, **kwargs)
        return out.getvalue()

    def test_backup_then_restore(self):
        some_data()
        output = self.call("backup")
        self.assertRegex(output, r"已备份 \d+ 张表：")
        [saved] = self.dir.iterdir()
        before = snapshot()
        with self.assertRaises(CommandError):
            self.call("backup", "-o", str(self.dir / "x.sql"))

        User.objects.update(nickname="改过了")
        changed = snapshot()
        with mock.patch("builtins.input", return_value="no"), self.assertRaises(CommandError) as caught:
            self.call("restore", str(saved))
        self.assertIn("已取消，数据库没有改动", str(caught.exception))
        self.assertEqual(snapshot(), changed)

        with mock.patch("builtins.input", return_value="yes"):
            output = self.call("restore", str(saved))
        self.assertIn("已恢复", output)
        self.assertEqual(snapshot(), before)
        # What was there before the restore was saved first, and can be put back the same way.
        [kept] = [path for path in self.dir.iterdir() if "before-restore" in path.name]
        self.call("restore", str(kept), "--yes")
        self.assertEqual(snapshot(), changed)

    def test_restore_refuses_a_bad_file(self):
        bad = self.dir / "bad.sql.gz"
        bad.write_bytes(b"not gzip")
        with self.assertRaises(CommandError) as caught:
            self.call("restore", str(bad), "--yes")
        self.assertIn("这不是 HFile 的数据库备份：它不是 gzip 压缩的文件。", str(caught.exception))

    def test_what_is_wrong_with_a_file_is_said_in_chinese(self):
        saved = self.dir / "whole.sql.gz"
        backup.dump(saved)
        cut = self.dir / "cut.sql.gz"
        cut.write_bytes(saved.read_bytes()[: saved.stat().st_size // 2])  # gzip: "Compressed file ended before ..."
        broken = self.dir / "broken.sql.gz"
        data = bytearray(saved.read_bytes())
        data[len(data) // 2] ^= 0xFF
        broken.write_bytes(bytes(data))
        for path, message in (
            (cut, "备份不完整：文件在写完之前就断了。"),
            (broken, "备份文件坏了"),
            (self.dir / "missing.sql.gz", "没有这个文件。"),
            (self.dir, "这是一个文件夹，不是备份文件。"),
        ):
            with self.subTest(path=path.name), self.assertRaises(CommandError) as caught:
                self.call("restore", str(path), "--yes")
            said = str(caught.exception).split("：", 1)[1]  # after the file's name
            self.assertIn(message, said)
            # gzip's, zlib's and the system's English stays out ("Compressed file ended before the ...").
            self.assertNotRegex(said, r"(?i)error|no such|compressed|crc|not a|codec|directory|marker")

    def test_a_backup_from_a_newer_version_is_refused(self):
        # Restored, it would stop the program from starting: refused before anything changes.
        self.record_a_newer_version()
        newer = self.dir / "newer.sql.gz"
        backup.dump(newer)
        MigrationRecorder(connection).record_unapplied(*FUTURE)
        self.assertIn(FUTURE, backup.read_info(newer).migrations)
        before = snapshot()
        with self.assertRaises(CommandError) as caught:
            self.call("restore", str(newer), "--yes")
        self.assertIn("这个备份来自更新的版本", str(caught.exception))
        self.assertIn("siteconf.0099_from_a_newer_version", str(caught.exception))
        self.assertIn("没有恢复，数据库没有改动", str(caught.exception))
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.files(), ["newer.sql.gz"])  # nothing saved first either

    @unittest.skipUnless(os.name == "posix", "Windows: the running site can't be seen")
    def test_refused_while_the_site_runs(self):
        saved = self.dir / "saved.sql.gz"
        backup.dump(saved)
        # A settings file of its own: the one the tests run with is shared by the processes of --parallel,
        # and their upgrades and restores would find this "site" running.
        config = self.dir / "config.toml"
        config.touch()
        self.enterContext(override_settings(CONFIG_FILE=str(config)))
        serving = configfile.hold_while_serving(settings.CONFIG_FILE)  # what serve holds while it runs
        self.addCleanup(serving.close)
        before = snapshot()
        for args in (["restore", str(saved), "--yes"], ["upgrade"]):
            with self.subTest(args[0]), self.assertRaises(CommandError) as caught:
                self.call(*args)
            self.assertIn(f"网站还在运行（用的是同一个设置文件 {settings.CONFIG_FILE}）", str(caught.exception))
            # How to stop it, as the service is stopped on this system (docs/deploy.md).
            self.assertIn("Stop-Service HFile" if os.name == "nt" else "systemctl stop hfile", str(caught.exception))
        self.assertIn("数据库不用升级", self.call("upgrade", "--check"))  # looking is fine
        self.assertEqual(snapshot(), before)
        serving.close()
        self.assertIn("已恢复", self.call("restore", str(saved), "--yes"))

    def test_restore_refuses_the_other_kind_of_database(self):
        saved = self.dir / "mine.sql.gz"
        backup.dump(saved)
        with gzip.open(saved, "rt", encoding="utf-8") as file:
            lines = file.read().splitlines()
        other = "SQLite" if MYSQL else "MySQL"
        theirs = self.dir / "theirs.sql.gz"
        with gzip.open(theirs, "wt", encoding="utf-8", newline="\n") as file:
            file.write("\n".join(line.replace(f"-- 数据库类型：{backup.engine()}", f"-- 数据库类型：{other}") for line in lines) + "\n")
        before = snapshot()
        with self.assertRaises(CommandError) as caught:
            self.call("restore", str(theirs), "--yes")
        self.assertIn(f"这是 {other} 数据库的备份，网站现在用的是 {backup.engine()}", str(caught.exception))
        self.assertEqual(snapshot(), before)
        self.assertEqual(self.files(), ["mine.sql.gz", "theirs.sql.gz"])  # nothing saved first either

    def test_restoring_a_backup_of_another_version(self):
        # As the backup a new version takes before upgrading: the database is still the old version's.
        log_row("0.0.9")
        saved = self.dir / "before.sql.gz"
        backup.dump(saved)
        info = backup.read_info(saved)
        self.assertEqual((info.version, info.schema), (VERSION, "0.0.9"))
        output = self.call("restore", str(saved), "--yes")
        self.assertIn("里面是 0.0.9 版的数据库", output)
        self.assertIn(f"备份里是 0.0.9 版的数据库（现在运行的是 {VERSION}）。这个版本启动时会把它升级到自己的版本", output)

        log_row(VERSION, "0.0.9")
        backup.dump(saved)
        self.assertNotIn("备份里是", self.call("restore", str(saved), "--yes"))


class MySQLRestoreFilterTests(SimpleTestCase):
    """What a MySQL restore runs: only the statements a backup writes (backup._MySQL._allowed)."""

    def test_only_what_the_backup_writes(self):
        allowed = backup._MySQL._allowed
        for line in (
            "SET NAMES utf8mb4;",
            "DROP TABLE IF EXISTS `accounts_user`;",
            "CREATE TABLE `accounts_user` (`id` bigint NOT NULL AUTO_INCREMENT, `nickname` varchar(50) NOT NULL"
            " COMMENT 'it''s a \\\\ name', PRIMARY KEY (`id`), CONSTRAINT `c` CHECK ((`id` >= 0)))"
            " ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci;",
            "INSERT INTO `accounts_user` (`id`, `nickname`, `data`) VALUES (1,'文\\'件',NULL),(2,'a\\\\b',X'00ff'),(-3,'1.5e+20',1e-05);",
        ):
            self.assertTrue(allowed(line), line)
        for line in (
            "DROP TABLE IF EXISTS `other_site`.`wp_users`;",
            "DROP TABLE IF EXISTS `accounts_user`, `other_site`.`orders`;",
            "CREATE TABLE `stolen` SELECT * FROM `mysql`.`user`;",
            "CREATE TABLE `t` (`a` int) SELECT `authentication_string` FROM `mysql`.`user`;",
            "CREATE TABLE `t` (`a` int, FOREIGN KEY (`a`) REFERENCES `other`.`x` (`id`));",
            "CREATE TABLE `t` LIKE `mysql`.`user`;",
            "INSERT INTO `accounts_user` (`nickname`) SELECT `authentication_string` FROM `mysql`.`user`;",
            "INSERT INTO `other_site`.`wp_users` VALUES (1);",
            "INSERT INTO `t` (`a`) VALUES (1) ON DUPLICATE KEY UPDATE `a` = (SELECT 1);",
            "INSERT INTO `t` (`a`) VALUES (1);DROP TABLE `x`;",
            "INSERT INTO `t` (`a`) VALUES (1)",
            "GRANT ALL ON *.* TO x;",
        ):
            self.assertFalse(allowed(line), line)
