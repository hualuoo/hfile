"""Release check: the files the site writes hold the secret key, password hashes and storage keys,
so they are for the program's user only (files 600, folders 700), whatever the umask it was started with.

Each test runs under a wide umask (000) and the usual one (022). The one marked expectedFailure (the
media) is an accepted known limit: those files are public at /media/ anyway.
"""

import os
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path

from django.core.files.base import ContentFile
from django.core.files.storage import FileSystemStorage
from django.test import SimpleTestCase, TransactionTestCase, override_settings

from config import configfile
from config.tests import KEY, ROOT, TempDirMixin, child_env
from apps.installer import token
from apps.upgrades import backup
from apps.uploads import spool

UMASKS = (0o000, 0o022)


def setUpModule():
    if os.name == "nt":
        raise unittest.SkipTest("modes and the umask are Linux's: Windows' access lists are checked where each file is made")


@contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


class ConfigAndTokenModes(TempDirMixin, SimpleTestCase):
    def test_config_toml_new_and_rewritten(self):
        for mask in UMASKS:
            with self.subTest(umask=oct(mask)), umask(mask):
                self.path.unlink(missing_ok=True)
                self.assertTrue(configfile.create(self.path))
                self.assertEqual(mode(self.path), 0o600)
                configfile.update(self.path, {"site": {"secret_key": KEY}})  # what the installer does
                self.assertEqual(mode(self.path), 0o600)
                self.assertFalse(self.path.with_name("config.toml.part").exists())

    def test_install_token(self):
        for mask in UMASKS:
            with self.subTest(umask=oct(mask)), umask(mask), override_settings(CONFIG_FILE=self.path):
                token.remove()
                token.get()
                self.assertEqual(mode(token.path()), 0o600)


class SpoolAndMediaModes(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hfile-modes-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)

    def test_upload_spool(self):
        for mask in UMASKS:
            folder = self.dir / f"spool-{mask:o}"
            with self.subTest(umask=oct(mask)), umask(mask), override_settings(UPLOAD_SPOOL_DIR=str(folder)):
                session = uuid.uuid4()
                spool.create(session)
                self.assertEqual(mode(folder), 0o700)
                self.assertEqual(mode(spool.path_of(session)), 0o600)

    # Accepted known limit: the site's own uploads (logo, icons) are public at
    # /media/, and a proxy that serves /media/ itself has to read them. FILE_UPLOAD_PERMISSIONS stays
    # Django's 0o644; the folders follow the umask.
    @unittest.expectedFailure
    def test_media(self):
        """What the site saves in [paths] uploads (siteconf's logo and icon through default_storage)."""
        for mask in UMASKS:
            root = self.dir / f"media-{mask:o}"
            with self.subTest(umask=oct(mask)), umask(mask), override_settings(MEDIA_ROOT=str(root)):
                name = FileSystemStorage().save("site/" + "0" * 32 + ".png", ContentFile(b"\x89PNG not really"))
                self.assertEqual(mode(root / name), 0o600)
                self.assertEqual(mode(root / "site"), 0o700)
                self.assertEqual(mode(root), 0o700)


class BackupModes(TransactionTestCase):
    def test_backup_file_and_new_folder(self):
        with tempfile.TemporaryDirectory(prefix="hfile-modes-") as tmp:
            for mask in UMASKS:
                path = Path(tmp) / f"backups-{mask:o}" / "all.sql.gz"
                with self.subTest(umask=oct(mask)), umask(mask):
                    backup.dump(path)
                    self.assertEqual(mode(path), 0o600)
                    self.assertEqual(mode(path.parent), 0o700)


class SqliteFileModes(TempDirMixin, SimpleTestCase):
    """The SQLite file holds the password hashes and the storages' keys. Whichever command connects first
    makes it 600: the site's SQLite backend does (config/backends/sqlite3/base.py)."""

    def setUp(self):
        super().setUp()
        configfile.create(self.path)
        configfile.update(self.path, {"site": {"secret_key": KEY}})
        self.database = self.dir / "hfile.sqlite3"

    def manage(self, *args, mask=0o022):
        return subprocess.run(
            [sys.executable, "-B", "manage.py", "--config", str(self.path), *args], cwd=ROOT, env=child_env(),
            capture_output=True, text=True, encoding="utf-8", timeout=180, preexec_fn=lambda: os.umask(mask),
        )

    def test_upgrade_makes_it_owner_only(self):
        for mask in UMASKS:
            with self.subTest(umask=oct(mask)):
                for leftover in self.dir.glob("hfile.sqlite3*"):
                    leftover.unlink()
                self.assertEqual(self.manage("upgrade", mask=mask).returncode, 0)
                self.assertEqual(mode(self.database), 0o600)

    def test_wal_and_shm_follow_the_file(self):
        self.assertEqual(self.manage("upgrade").returncode, 0)
        with umask(0o000):
            conn = sqlite3.connect(self.database)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
                conn.execute("INSERT INTO t VALUES (1)")
                conn.commit()
                for end in ("-wal", "-shm"):
                    self.assertEqual(mode(f"{self.database}{end}"), 0o600, end)
            finally:
                conn.close()

    def test_any_command_makes_it_owner_only(self):
        # `hfile check` to try a config.toml written by hand (README 安装: "也可以不用网页安装"), Django's
        # habitual `migrate`, a cron job's `cleanup` or `backup` before the first start...
        for command in (["check"], ["migrate", "--noinput"], ["backup"], ["cleanup"]):
            with self.subTest(command=command[0]):
                for leftover in self.dir.glob("hfile.sqlite3*"):
                    leftover.unlink()
                self.manage(*command)
                self.assertTrue(self.database.exists())
                self.assertEqual(mode(self.database), 0o600)
