"""Helpers shared by the test modules, and `manage.py test`'s runner (settings.TEST_RUNNER)."""

import contextlib
import os
import shutil
import signal
import sqlite3
import stat
import subprocess
import tempfile
import unittest

import django
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.test import override_settings
from django.test.runner import DiscoverRunner, ParallelTestSuite, RemoteTestResult, RemoteTestRunner
from django.utils import translation

# How the API writes a time (apps.core.times.iso), in the site's zone (settings.TIME_ZONE), to the second.
TO_THE_SECOND = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00$"


class _Fresh:
    """Each test starts with an empty cache, which holds the rate limits' counters (DRF throttles,
    keyed by user id or IP). Another test has the same ids again: SQLite reuses rolled-back ones.

    And in the site's default language (settings.LANGUAGE_CODE): a request leaves the language it asked for
    active, which a later test would otherwise speak.

    And SIGTERM's handler is put back as the test found it. The upgrade command sets one for its
    process (apps.upgrades.service.stop_on_sigterm), which a test process would keep: a worker of
    --parallel that Django's pool then stops with SIGTERM goes on instead, and the run never ends."""

    def startTest(self, test):
        cache.clear()
        translation.activate(settings.LANGUAGE_CODE)
        self._sigterm = signal.getsignal(signal.SIGTERM)
        super().startTest(test)

    def stopTest(self, test):
        super().stopTest(test)
        if self._sigterm is not None and signal.getsignal(signal.SIGTERM) is not self._sigterm:
            signal.signal(signal.SIGTERM, self._sigterm)


class _FreshCacheResult(_Fresh, unittest.TextTestResult):
    pass


class _SentSubTest:
    """A subtest that failed in a worker of --parallel, as it goes to the main process (pickled): by its name,
    which is all the report there uses. Django's runner sends the subtest itself, and its test with it; one that
    keeps what pickle can't take (a driver: its client's locks, a class the test made) then shows a PicklingError
    in place of its failure."""

    def __init__(self, subtest):
        self.failureException = subtest.failureException
        self._name, self._id, self._description = str(subtest), subtest.id(), subtest.shortDescription()

    def __str__(self):
        return self._name

    def id(self):
        return self._id

    def shortDescription(self):
        return self._description


class _FreshCacheRemoteResult(_Fresh, RemoteTestResult):
    """The same in the worker processes of --parallel, which record their results with this class. A failed
    subtest goes to the main process by its name (_SentSubTest)."""

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest if err is None else _SentSubTest(subtest), err)


class _FreshCacheRemoteRunner(RemoteTestRunner):
    resultclass = _FreshCacheRemoteResult


# Passwords in the test processes: Django's PBKDF2 takes 0.3 s a password, which adds up.
FAST_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]


def _set_up_worker(*args):
    """A worker process of --parallel that starts afresh (spawn: Windows) hashes passwords with MD5 as well. One
    forked from the main process (Linux) has it from TestRunner.setup_test_environment(), which runs only there."""
    django.setup()  # the settings, before they are overridden
    override_settings(PASSWORD_HASHERS=FAST_HASHERS).enable()


class _FreshCacheParallelSuite(ParallelTestSuite):
    runner_class = _FreshCacheRemoteRunner
    process_setup = _set_up_worker


class TestRunner(DiscoverRunner):
    """`manage.py test`: Django's runner, with an empty cache for every test (see above), and passwords
    hashed with MD5 in this process: Django's PBKDF2 takes 0.3 s a password, which adds up. The servers
    the tests start in processes of their own (config/test_serve.py) keep PBKDF2.

    Finds every test*.py module under the project, Django's TestCases and plain unittest ones alike
    (apps/storages/tests/ are plain unittest, without Django).
    """

    parallel_test_suite = _FreshCacheParallelSuite

    def get_resultclass(self):
        return super().get_resultclass() or _FreshCacheResult

    def setup_test_environment(self, **kwargs):
        super().setup_test_environment(**kwargs)
        # Commands and the programs the tests start speak the system's language (config/i18n.py): the
        # default one here, whatever the machine's is. The worker processes of --parallel inherit it.
        self._language = os.environ.get("LANGUAGE")
        os.environ["LANGUAGE"] = "zh_CN"
        self._fast_hashing = override_settings(PASSWORD_HASHERS=FAST_HASHERS)
        self._fast_hashing.enable()

    def teardown_test_environment(self, **kwargs):
        self._fast_hashing.disable()
        if self._language is None:
            os.environ.pop("LANGUAGE", None)
        else:
            os.environ["LANGUAGE"] = self._language
        super().teardown_test_environment(**kwargs)


def admin_url(path=""):
    """An address in the Django admin, which lives at a random path (config/admin_path.py)."""
    return f"/{settings.ADMIN_PATH}/{path}"


mysql_only = unittest.skipUnless(connection.vendor == "mysql", "只在 MySQL 上")
sqlite_only = unittest.skipUnless(connection.vendor == "sqlite", "只在 SQLite 上")


@contextlib.contextmanager
def sqlite_file(path):
    """sqlite3.connect(path) for a with: committed at its end as sqlite3's own with does, and closed too, which
    that one leaves to the garbage collector (Windows can't delete a file that is still open)."""
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


def symlink(target, link):
    """os.symlink(); on Windows with its separators, and knowing whether the target is a folder, which a
    link there has to (one made as a file's can't be entered)."""
    target, link = os.fspath(target), os.fspath(link)
    if os.name != "nt":
        os.symlink(target, link)
        return
    target = target.replace("/", "\\")
    where = target if os.path.isabs(target) else os.path.join(os.path.dirname(link), target)
    os.symlink(target, link, target_is_directory=os.path.isdir(where))


def assert_private(test, path, mode=0o600):
    """That only this user may open `path`: its mode is `mode` (0o700 for a folder). Windows has no modes;
    there it has the access list of its own that config/private.py gives it (only this user, SYSTEM and the
    Administrators), not its folder's."""
    if os.name != "nt":
        test.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode, path)
        return
    from config import windows

    test.assertTrue(_own_access_list(path), f"{path} takes its folder's access list")
    test.assertFalse(windows.readable_by_others(path), path)


def _own_access_list(path):
    """Windows: whether the access list of `path` is its own (protected), not taken from its folder."""
    import ctypes

    from config import windows

    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    if windows.advapi32.GetNamedSecurityInfoW(str(path), windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION,
                                              None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        return False
    try:
        control, revision = ctypes.c_ushort(), ctypes.c_ulong()
        windows.advapi32.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision))
        return bool(control.value & 0x1000)  # SE_DACL_PROTECTED
    finally:
        windows.kernel32.LocalFree(descriptor)


def deny(test, path, rights):
    """Windows, where chmod can't take these away: an entry in the access list of `path` that denies everyone
    `rights`, as icacls writes them ("RD": listing a folder; "WD,AD": adding to it). Returns what takes it out
    again, which the end of the test does too."""
    def allow():
        subprocess.run(["icacls", str(path), "/remove:d", "*S-1-1-0"], check=True, capture_output=True)

    subprocess.run(["icacls", str(path), "/deny", f"*S-1-1-0:({rights})"], check=True, capture_output=True)
    test.addCleanup(allow)
    return allow


def readable_by_all(path):
    """What chmod 644 does (755 for a folder): every user of the machine may read `path`. On Windows by an
    entry in its access list for the Users group, which a folder's files and folders take too."""
    folder = os.path.isdir(path)
    if os.name != "nt":
        os.chmod(path, 0o755 if folder else 0o644)
        return
    rights = "(OI)(CI)(R)" if folder else "(R)"
    subprocess.run(["icacls", str(path), "/grant", f"*S-1-5-32-545:{rights}"], check=True, capture_output=True)


class TempMediaMixin:
    """Uploaded files go to a temporary MEDIA_ROOT that is removed after the test class."""

    @classmethod
    def setUpClass(cls):
        cls._media_dir = tempfile.mkdtemp(prefix="hfile-test-media-")
        cls._media_override = override_settings(MEDIA_ROOT=cls._media_dir)
        cls._media_override.enable()
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        cls._media_override.disable()
        shutil.rmtree(cls._media_dir, ignore_errors=True)
