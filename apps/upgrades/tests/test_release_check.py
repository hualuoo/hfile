"""Release check (0.1.0): backups and restores, on the database the tests run on.

- backup then restore gives back the accounts (their passwords log in), the storages with their keys
  (decrypted with the same secret_key) and the site's settings;
- a MySQL restore runs its statements one by one after dropping every table (MySQL can't take DDL back):
  one that fails half way would leave the database half restored, so the restore command puts back the
  copy it saved first ("现在的数据库另存到了 ..."). A site that serves meanwhile can make that happen:
  SiteSettings.load() creates the row (get_or_create) in the moment between the restore's CREATE TABLE
  and its INSERT, and the INSERT then fails on the duplicate key.
"""

import gzip
import io
import threading
from unittest import mock

from django.contrib.auth import authenticate, get_user_model
from django.core.management import CommandError, call_command
from django.db import connections

from apps.core.testing import mysql_only
from apps.siteconf.models import SiteSettings, forget_cached
from apps.storages import config as storage_config
from apps.storages.drivers.ftp import FTPDriver
from apps.storages.models import Grant, StorageSource
from apps.upgrades import backup

from .test_upgrades import UpgradeTestBase, snapshot, some_data

User = get_user_model()
FTP_PASSWORD = "ftp-Secret-2026!"


class BackupRestoreRoundTrip(UpgradeTestBase):
    def call(self, *args, **kwargs):
        out = io.StringIO()
        call_command(*args, stdout=out, **kwargs)
        return out.getvalue()

    def test_accounts_storages_with_their_keys_and_settings_come_back(self):
        some_data()
        alice = User.objects.get(username="alice")
        storage = StorageSource.objects.create(
            key="ftp", name="FTP", type="ftp",
            config=storage_config.encode(FTPDriver, {"host": "127.0.0.1", "port": 21, "username": "u", "password": FTP_PASSWORD}))
        Grant.objects.create(user=alice, storage=storage, permissions=["preview", "download"], root_path="/a")
        site = SiteSettings.load()
        site.site_name = "备份前的名字"
        site.save()
        before = snapshot()
        self.call("backup")
        [saved] = self.dir.iterdir()

        # Everything changed or gone afterwards.
        alice.set_password("another-pass-2026")
        alice.save()
        StorageSource.objects.all().delete()
        site = SiteSettings.load()
        site.site_name = "后来的名字"
        site.save()

        self.call("restore", str(saved), "--yes")
        forget_cached()  # another process (the site) sees it within SiteSettings' CACHE_SECONDS
        self.assertEqual(snapshot(), before)
        self.assertIsNotNone(authenticate(username="alice", password="pw-alice-2026"))
        restored = StorageSource.objects.get(key="ftp")
        values, unreadable = storage_config.decode(FTPDriver, restored.config)
        self.assertEqual((values["password"], unreadable), (FTP_PASSWORD, []))
        self.assertEqual(Grant.objects.get(storage=restored).root_path, "/a")
        self.assertEqual(SiteSettings.load().site_name, "备份前的名字")
        # The secret isn't in the backup in the clear.
        with gzip.open(saved, "rt", encoding="utf-8") as file:
            self.assertNotIn(FTP_PASSWORD, file.read())


@mysql_only
class MySQLHalfRestore(UpgradeTestBase):
    def setUp(self):
        super().setUp()
        some_data()
        self.before = snapshot()
        self.good = self.dir / "good.sql.gz"
        backup.dump(self.good)
        # Whatever the test leaves, the next tests get the whole database back.
        self.addCleanup(backup.restore, self.good)

    def restore(self, path):
        """The restore command's error message, "" when it went through."""
        try:
            call_command("restore", str(path), "--yes", stdout=io.StringIO())
        except CommandError as error:
            return str(error)
        return ""

    def test_a_statement_that_fails_half_way(self):
        # A backup that passes every check (a whole file, only the statements a backup writes) and fails in
        # MySQL: here a row twice, as a lost connection, max_allowed_packet, a full disk or a lock wait would.
        with gzip.open(self.good, "rt", encoding="utf-8") as file:
            lines = file.read().splitlines()
        at = next(i for i, line in enumerate(lines) if line.startswith("INSERT INTO `django_content_type`"))
        lines.insert(at + 1, lines[at])
        bad = self.dir / "bad.sql.gz"
        with gzip.open(bad, "wt", encoding="utf-8") as file:
            file.write("\n".join(lines) + "\n")
        message = self.restore(bad)
        self.assertIn("恢复失败", message)
        self.assertEqual(snapshot(), self.before)  # as it was: put back from the copy saved first

    def test_a_request_of_the_site_meanwhile(self):
        original = backup._MySQL._statements.__func__
        passes = []

        def a_request():
            # The site's process: its cached SiteSettings ran out (CACHE_SECONDS), load() reads the row.
            try:
                SiteSettings.objects.get_or_create(pk=1)
            finally:
                connections.close_all()

        def statements(cls, path):
            passes.append(path)
            for line in original(cls, path):
                yield line  # resumed once the restore has run it
                if len(passes) == 2 and line.startswith("CREATE TABLE `siteconf_sitesettings`"):
                    thread = threading.Thread(target=a_request)
                    thread.start()
                    thread.join()

        with mock.patch.object(backup._MySQL, "_statements", classmethod(statements)):
            message = self.restore(self.good)
        # Restored, or refused and put back as it was: never half of it.
        self.assertEqual(snapshot(), self.before, message)
