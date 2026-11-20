"""Tests of the Baidu Netdisk driver (apps.storages.drivers.baidu_netdisk) against the in-memory fake of Baidu's
open platform (apps.storages.tests.fake_baidu).

    manage.py test apps.storages.tests.test_driver_baidu_netdisk
"""

from __future__ import annotations

import io
import os
import threading
import time
import unittest
import urllib.parse
import uuid

from apps.storages.drivers import baidu_netdisk
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
    Unsupported,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_baidu import APP_FOLDER, APP_KEY, SECRET_KEY, FakeBaidu, KiB, MiB, Node

Driver = baidu_netdisk.BaiduNetdiskDriver
REDIRECT = "https://files.example.com/storages/oauth/callback/"
# Small slices for the tests (Baidu's are 4 MiB, at most 1024 of them): (largest file, largest slice) by vip_type.
SMALL = {"slice_size": 64 * KiB, "max_slices": 8,
         "memberships": {0: (512 * KiB, 64 * KiB), 1: (2 * MiB, 256 * KiB), 2: (4 * MiB, 512 * KiB)}}


def driver_class(fake: FakeBaidu, **attrs):
    """BaiduNetdiskDriver pointed at the fake (the type stays "baidu-netdisk")."""
    attrs = {"oauth_root": fake.url, "api_root": fake.url, "upload_root": fake.url, "trusted_hosts": ("127.0.0.1",),
             "secure_transport": False, "calls_per_second": 0.0, "retry_delay": 0.0, **attrs}
    return type("FakeBaiduNetdiskDriver", (Driver,), attrs)


def small_slices(fake: FakeBaidu) -> None:
    """The fake's rules for SMALL's slices."""
    fake.base_slice = SMALL["slice_size"]
    fake.max_blocks = SMALL["max_slices"]
    fake.limits = dict(SMALL["memberships"])


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


def names(entries) -> list[str]:
    return sorted(entry.name for entry in entries)


class Unseekable(io.RawIOBase):
    """Data the driver can read once only, like a request body."""

    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def readinto(self, buffer) -> int:
        piece = self._data.read(len(buffer))
        buffer[:len(piece)] = piece
        return len(piece)


class Untouchable(io.RawIOBase):
    """Data that must not be read."""

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        raise AssertionError("the data was read")


class FakeMixin:
    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeBaidu().start()
        cls.Driver = driver_class(cls.fake, **cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    @staticmethod
    def app() -> dict:
        return {"app_key": APP_KEY, "secret_key": SECRET_KEY}

    def settings(self, base: str = APP_FOLDER, **config) -> dict:
        return {**self.app(), "base_path": base, **self.fake.issue_tokens(), **config}

    def new_driver(self, base: str = APP_FOLDER, driver_class=None, **config):
        self.updates: list[dict] = []
        driver = (driver_class or self.Driver)(self.settings(base, **config), on_update=self.updates.append)
        self.addCleanup(driver.close)
        return driver


class BaiduContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract in a fresh folder in the app's folder (base_path is that folder), with Baidu's slice sizes."""

    def make_driver(self):
        base = f"{APP_FOLDER}/hfile-tests/{uuid.uuid4().hex[:10]}"
        self.fake.mkdirs(base)
        return self.new_driver(base)

    def test_empty_file(self):
        # Replaces the contract's: Baidu takes no empty files (its upload FAQ), so they are refused before any call.
        calls = self.fake.counts["precreate"]
        with self.assertRaises(Unsupported):
            self.put("/empty", b"")
        self.assertMissing("/empty")
        self.assertEqual(self.fake.counts["precreate"], calls)


class BaiduSlicedContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract again at the root of the app's folder, on a fresh account for every test, with 64 KiB slices
    and at most 8 a file, so that the larger files go up in many slices, or in larger ones of a membership."""

    driver_attrs = SMALL
    test_empty_file = BaiduContractTests.test_empty_file

    def make_driver(self):
        self.fake.reset()
        small_slices(self.fake)
        self.fake.vip_type = 2  # the contract's 3 MiB file needs a membership with these small slices
        return self.new_driver(APP_FOLDER)


class BaiduTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    # -- the admin form and authorization

    def test_params(self):
        keys = [param.key for param in Driver.params]
        self.assertEqual(Driver.type, "baidu-netdisk")
        self.assertTrue(set(Driver.setup_params) <= set(keys) and set(Driver.auth_params) <= set(keys))
        secret = {param.key for param in Driver.params if param.secret}
        self.assertEqual(secret, {"secret_key", "access_token", "refresh_token", "token_expires_at"})
        self.assertTrue(Driver.oauth)
        self.assertFalse(Driver.can_direct_download or Driver.can_direct_upload or Driver.can_search)

    def test_validate_config(self):
        good = {**self.app(), "base_path": APP_FOLDER}
        self.assertEqual(Driver.validate_config(good), {})
        self.assertEqual(Driver.validate_config({**good, "base_path": APP_FOLDER + "/sub/folder", "token_expires_at": 5}), {})
        for base in ("/", "", "/apps", "/HFile", "/apps2/HFile", "/apps/../x"):
            with self.subTest(base=base):
                self.assertIn("base_path", Driver.validate_config({**good, "base_path": base}))
        for key in ("has space", "", "a/b", None):
            with self.subTest(app_key=key):
                self.assertIn("app_key", Driver.validate_config({**good, "app_key": key}))
        self.assertIn("token_expires_at", Driver.validate_config({**good, "token_expires_at": -1}))

    def test_authorize_url(self):
        url = self.Driver.authorize_url(self.app(), redirect_uri=REDIRECT, state="s1")
        split = urllib.parse.urlsplit(url)
        self.assertEqual(split.path, "/oauth/2.0/authorize")
        self.assertEqual(dict(urllib.parse.parse_qsl(split.query)), {
            "response_type": "code", "client_id": APP_KEY, "redirect_uri": REDIRECT, "scope": "basic,netdisk", "state": "s1"})
        self.assertNotIn(SECRET_KEY, url)
        self.assertTrue(Driver.authorize_url(self.app(), redirect_uri=REDIRECT, state="s").startswith(
            "https://openapi.baidu.com/oauth/2.0/authorize?"))
        with self.assertRaises(DriverError):
            self.Driver.authorize_url({"secret_key": SECRET_KEY}, redirect_uri=REDIRECT, state="s")

    def test_exchange_code(self):
        code = self.fake.issue_code(REDIRECT)
        tokens = self.Driver.exchange_code(self.app(), code=code, redirect_uri=REDIRECT)
        self.assertEqual(set(tokens), {"access_token", "refresh_token", "token_expires_at"})
        self.assertGreater(tokens["token_expires_at"], time.time() + 29 * 24 * 3600)  # 30 days
        driver = self.Driver({**self.app(), "base_path": APP_FOLDER, **tokens})
        self.addCleanup(driver.close)
        self.assertEqual(driver.list("/"), [])
        with self.assertRaises(AuthError):  # a code works once
            self.Driver.exchange_code(self.app(), code=code, redirect_uri=REDIRECT)

    def test_exchange_code_failures(self):
        code = self.fake.issue_code(REDIRECT)
        with self.assertRaises(AuthError) as caught:
            self.Driver.exchange_code({**self.app(), "secret_key": "wrong"}, code=code, redirect_uri=REDIRECT)
        self.assertIn("invalid_client", caught.exception.message)
        with self.assertRaises(AuthError):  # another redirect URI than the authorization's
            self.Driver.exchange_code(self.app(), code=code, redirect_uri=REDIRECT + "x")
        with self.assertRaises(AuthError):  # the user didn't allow Baidu Netdisk
            self.Driver.exchange_code(self.app(), code=self.fake.issue_code(REDIRECT, scope="basic"), redirect_uri=REDIRECT)
        self.fake.fail_next["token"] = ("http", 503)
        with self.assertRaises(BackendError):
            self.Driver.exchange_code(self.app(), code=self.fake.issue_code(REDIRECT), redirect_uri=REDIRECT)
        with self.assertRaises(DriverError):
            self.Driver.exchange_code({}, code="x", redirect_uri=REDIRECT)

    # -- tokens

    def test_saved_token_used_until_it_expires(self):
        driver = self.new_driver()
        driver.list("/")
        driver.list("/")
        self.assertEqual(self.fake.counts["token"], 0)
        self.assertEqual(self.updates, [])
        self.assertEqual(self.fake.user_agents, {"pan.baidu.com"})

    def test_expired_token_refreshed_and_saved(self):
        driver = self.new_driver(token_expires_at=int(time.time()) - 10)
        old_refresh = driver.config["refresh_token"]
        driver.list("/")
        driver.list("/")
        self.assertEqual(self.fake.counts["token"], 1)
        self.assertEqual(len(self.updates), 1)
        change = self.updates[0]
        self.assertEqual(set(change), {"access_token", "refresh_token", "token_expires_at"})
        self.assertNotEqual(change["refresh_token"], old_refresh)
        self.assertIn(old_refresh, self.fake.spent)  # Baidu's refresh tokens work once
        self.assertGreater(change["token_expires_at"], time.time() + 29 * 24 * 3600)
        self.assertEqual(driver.config["refresh_token"], change["refresh_token"])

    def test_refused_token_refreshed_once_for_all_threads(self):
        driver = self.new_driver()
        self.fake.expire_access_tokens()  # the saved expiry still says it's good; Baidu answers 20016
        errors = []

        def work():
            try:
                driver.list("/")
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.counts["token"], 1)
        self.assertEqual(len(self.updates), 1)

    def test_refused_token_on_the_upload_hosts(self):
        driver = self.new_driver()
        data = os.urandom(3000)
        self.fake.fail_next["upload"] = 111  # superfile2 says the token expired (OpenAPI's error_code 111)
        driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.counts["token"], 1)
        self.assertEqual(self.fake.counts["upload"], 2)
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/a.bin").data, data)

    def test_withdrawn_authorization_stops_the_calls(self):
        driver = self.new_driver()
        driver.list("/")
        self.fake.revoke()
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("20017", caught.exception.message)
        calls, refused = self.fake.counts["list"], self.fake.counts["refused"]
        for _ in range(3):
            with self.assertRaises(AuthError):
                driver.list("/")
        self.assertEqual((self.fake.counts["list"], self.fake.counts["refused"]), (calls, refused))  # Baidu isn't asked
        self.assertEqual(self.fake.counts["token"], 0)  # and no refresh: it can't help

    def test_refused_refresh_is_final(self):
        settings = self.settings(token_expires_at=1)
        first, second = self.Driver(dict(settings)), self.Driver(dict(settings))
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.list("/")  # uses up the refresh token
        with self.assertRaises(AuthError) as caught:
            second.list("/")
        self.assertIn("expired_token", caught.exception.message)
        asked = self.fake.counts["token"]
        for _ in range(3):
            with self.assertRaises(AuthError):
                second.list("/")
        self.assertEqual(self.fake.counts["token"], asked)  # Baidu's FAQ: don't retry a refused refresh
        # The site gives an instance the tokens another instance of the storage refreshed and saved: they work.
        second.config.update({key: first.config[key] for key in ("access_token", "refresh_token", "token_expires_at")})
        self.assertEqual(second.list("/"), [])

    def test_authorization_without_netdisk(self):
        # Refused (-6), refreshed once, and the refresh gives no access to Baidu Netdisk either: no more calls.
        driver = self.Driver({**self.app(), "base_path": APP_FOLDER, **self.fake.issue_tokens(scope="basic")})
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError):
            driver.list("/")
        with self.assertRaises(AuthError):
            driver.list("/")
        self.assertEqual((self.fake.counts["token"], self.fake.counts["list"]), (1, 1))

    def test_without_tokens(self):
        driver = self.Driver({**self.app(), "base_path": APP_FOLDER})
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError):
            driver.list("/")
        self.assertEqual(self.fake.counts["list"], 0)

    # -- listings, paths and errors

    def test_paging(self):
        self.fake.page_limit = 3
        for i in range(8):
            self.fake.put_file(f"{APP_FOLDER}/many/f{i}.txt", b"x" * i)
        driver = self.new_driver(driver_class=driver_class(self.fake, page_size=3))
        entries = {entry.name: entry for entry in driver.list("/many")}
        self.assertEqual(sorted(entries), [f"f{i}.txt" for i in range(8)])
        self.assertEqual((entries["f5.txt"].size, entries["f5.txt"].path), (5, "/many/f5.txt"))
        self.assertIsNotNone(entries["f5.txt"].modified.tzinfo)
        self.assertEqual(self.fake.counts["list"], 4)  # 1 for the root, 3 pages

    def test_missing_app_folder_made_by_check(self):
        self.fake.reset(app_folder=False)
        driver = self.new_driver()
        with self.assertRaises(NotFound) as caught:
            driver.list("/")
        self.assertIn(APP_FOLDER, caught.exception.message)
        driver.check()
        self.assertTrue(self.fake.find(APP_FOLDER).is_dir)
        self.assertEqual(driver.list("/"), [])

    def test_check_makes_the_folders_down_to_the_base_path(self):
        # A base path in a new app's folder: create needs the folder above, so they're made one by one.
        self.fake.reset(app_folder=False)
        driver = self.new_driver(f"{APP_FOLDER}/sub/folder")
        driver.check()
        self.assertTrue(self.fake.find(f"{APP_FOLDER}/sub/folder").is_dir)
        self.assertEqual(driver.list("/"), [])
        driver.check()  # all there now
        self.fake.reset(app_folder=False)
        with self.fake.lock:  # no /apps either: not the app's to make
            del self.fake.nodes["/apps"]
        with self.assertRaises(NotFound) as caught:
            self.new_driver(f"{APP_FOLDER}/sub").check()
        self.assertIn(f"{APP_FOLDER}/sub", caught.exception.message)

    def test_folder_of_another_app(self):
        driver = self.new_driver("/apps/OtherApp")
        with self.assertRaises(DriverError) as caught:
            driver.check()
        self.assertIn("20020", caught.exception.message)
        self.assertIsNone(self.fake.find("/apps/OtherApp"))

    def test_confined_to_base_path(self):
        self.fake.put_file(f"{APP_FOLDER}/secret.txt", b"s")
        self.fake.mkdirs(f"{APP_FOLDER}/base")
        driver = self.new_driver(f"{APP_FOLDER}/base")
        self.assertEqual(driver.list("/"), [])
        for path in ("/secret.txt", "/..%2Fsecret.txt"):
            with self.subTest(path=path), self.assertRaises(NotFound):
                driver.stat(path)
        driver.mkdir("/a%2F..")
        self.assertTrue(self.fake.find(f"{APP_FOLDER}/base/a%2F..").is_dir)
        for call in (lambda: driver.delete("/"), lambda: driver.rename("/", "x"), lambda: driver.move("/", "/x"),
                     lambda: driver.copy("/", "/x")):
            with self.assertRaises(DriverError):
                call()
        self.assertEqual(names(driver.list("/")), ["a%2F.."])

    def test_files_are_not_folders(self):
        driver = self.new_driver()
        driver.write("/f.txt", io.BytesIO(b"x"), 1)
        with self.assertRaises(NotADirectory):
            driver.list("/f.txt")  # Baidu lists a file as an empty folder
        with self.assertRaises(NotFound):
            driver.stat("/f.txt/inner")
        with self.assertRaises(NotADirectory):
            driver.mkdir("/f.txt/inner")
        with self.assertRaises(NotADirectory):
            driver.write("/f.txt/inner/x", io.BytesIO(b"y"), 1)
        driver.mkdir("/d")
        driver.move("/f.txt", "/")  # already there: nothing to do
        self.assertEqual(names(driver.list("/")), ["d", "f.txt"])
        with self.assertRaises(NotADirectory):
            driver.move("/d", "/f.txt")

    def test_odd_entries_left_out(self):
        self.fake.mkdirs(f"{APP_FOLDER}/odd")
        self.fake.put_file(f"{APP_FOLDER}/odd/fine.txt", b"y")
        with self.fake.lock:  # a name that can't be a path segment
            self.fake.nodes[f"{APP_FOLDER}/odd/.."] = Node(f"{APP_FOLDER}/odd/..", next(self.fake.ids), True)
        driver = self.new_driver()
        self.assertEqual(names(driver.list("/odd")), ["fine.txt"])
        self.assertIsNone(baidu_netdisk._list_item({"server_filename": "a.txt", "fs_id": 5, "isdir": 0,
                                                    "path": "/apps/elsewhere/a.txt"}, APP_FOLDER))
        self.assertIsNone(baidu_netdisk._list_item({"server_filename": "a/b", "fs_id": 5, "isdir": 0}, APP_FOLDER))
        self.assertIsNotNone(baidu_netdisk._list_item({"server_filename": "a.txt", "fs_id": 5, "isdir": 0,
                                                       "path": APP_FOLDER.upper() + "/a.txt"}, APP_FOLDER))

    def test_error_codes(self):
        cases = {
            ("errno", -9): NotFound, ("errno", -3): NotFound, ("errno", -8): AlreadyExists, ("errno", 31061): AlreadyExists,
            ("errno", -7): InvalidPath, ("errno", 31062): InvalidPath, ("errno", -10): DriverError,
            ("errno", 20020): DriverError, ("errno", 31064): DriverError, ("errno", -6): AuthError,
            ("errno", 20016): AuthError, ("errno", 20017): AuthError, ("errno", 31045): AuthError,
            ("errno", 6): AuthError, ("errno", 20011): AuthError, ("errno", 20013): AuthError, ("errno", 20015): AuthError,
            ("errno", 20012): BackendError, ("errno", 31034): BackendError, ("errno", 111): BackendError,
            ("errno", 42905): BackendError, ("errno", 31363): BackendError, ("errno", 31360): BackendError,
            ("errno", -11015): baidu_netdisk._Transient,
            ("errno", 31365): DriverError, ("errno", 2): DriverError, ("errno", 9013): DriverError,
            ("error_code", 110): AuthError, ("error_code", 111): AuthError, ("error_code", 2): BackendError,
            ("error_code", 18): BackendError, ("error_code", 31299): BackendError,
        }
        for (field, code), kind in cases.items():
            with self.subTest(field=field, code=code):
                error = baidu_netdisk._error({field: code, "errmsg": "msg"})
                self.assertIs(type(error), kind)
                if kind not in (NotFound, AlreadyExists, DriverError) or code in (20020, 2, 9013):
                    self.assertIn(str(code), error.message)
        batch = {"errno": 12, "info": [{"errno": 0, "path": "/a"}, {"errno": -8, "path": "/b"}]}
        self.assertIs(type(baidu_netdisk._error(batch)), AlreadyExists)
        self.assertIsNone(baidu_netdisk._code({"errno": 0, "error_code": 0}))
        self.assertEqual(baidu_netdisk._code({"errno": "-9"}), ("errno", -9))

    def test_errors_from_the_api(self):
        driver = self.new_driver()
        driver.list("/")
        for injected, kind in ((31034, BackendError), (20012, BackendError), (("http", 502), BackendError),
                               (("http", 404), BackendError), (-7, InvalidPath)):
            with self.subTest(injected=injected):
                calls = self.fake.counts["list"]
                self.fake.fail_next["list"] = injected
                with self.assertRaises(kind):
                    driver.list("/")
                self.assertEqual(self.fake.counts["list"], calls + 1)  # not retried

    # -- uploads

    def test_single_slice_upload(self):
        driver = self.new_driver()
        data = os.urandom(5000)
        driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/a.bin").data, data)
        self.assertEqual((self.fake.counts["precreate"], self.fake.counts["locateupload"], self.fake.counts["upload"],
                          self.fake.counts["create"]), (1, 1, 1, 1))
        self.assertEqual(self.fake.uploads, {})  # create finished it
        self.assertEqual(self.fake.other_requests, [])  # locateupload's host that isn't Baidu's got nothing
        self.assertEqual(driver.stat("/a.bin").size, len(data))

    def test_rapid_upload(self):
        driver = self.new_driver()
        data = os.urandom(300 * KiB)  # more than the 256 KiB slice-md5 covers
        driver.write("/a.bin", io.BytesIO(data), len(data))
        driver.write("/copy/b.bin", io.BytesIO(data), len(data))  # Baidu has these bytes: 秒传
        self.assertEqual((self.fake.counts["locateupload"], self.fake.counts["upload"]), (1, 1))
        self.assertEqual(read(driver, "/copy/b.bin"), data)

    def test_sliced_upload(self):
        small_slices(self.fake)
        driver = self.new_driver(driver_class=driver_class(self.fake, **SMALL))
        data = os.urandom(5 * 64 * KiB + 100)
        driver.write("/big.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.slices, [(0, 64 * KiB), (1, 64 * KiB), (2, 64 * KiB), (3, 64 * KiB), (4, 64 * KiB), (5, 100)])
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/big.bin").data, data)
        self.assertEqual(self.fake.counts["uinfo"], 0)  # 4 GB or less: any membership's
        other = os.urandom(3 * 64 * KiB)
        driver.write("/pipe.bin", Unseekable(other), len(other))  # spooled first
        self.assertEqual(read(driver, "/pipe.bin"), other)

    def test_only_the_slices_baidu_lacks_are_sent(self):
        small_slices(self.fake)
        driver = self.new_driver(driver_class=driver_class(self.fake, **SMALL))
        first = os.urandom(3 * 64 * KiB)
        driver.write("/one.bin", io.BytesIO(first), len(first))
        second = first[:2 * 64 * KiB] + os.urandom(64 * KiB)  # its first two slices are Baidu's already
        self.fake.slices.clear()
        driver.write("/two.bin", io.BytesIO(second), len(second))
        self.assertEqual(self.fake.slices, [(2, 64 * KiB)])
        self.assertEqual(read(driver, "/two.bin"), second)

    def test_slices_tried_again(self):
        small_slices(self.fake)
        driver = self.new_driver(driver_class=driver_class(self.fake, **SMALL))
        data = os.urandom(3 * 64 * KiB)
        self.fake.broken_slices = 1  # an HTTP 500
        self.fake.corrupt_slices = 1  # an MD5 that isn't the slice's
        driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.counts["upload"], 5)
        self.assertEqual(read(driver, "/a.bin"), data)

    def test_slice_lost_to_a_network_failure(self):
        # Baidu's upload FAQ: -11015 is an upload that a network failure broke off; the slice is sent again.
        driver = self.new_driver()
        data = os.urandom(3000)
        self.fake.fail_next["upload"] = -11015
        driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.counts["upload"], 2)
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/a.bin").data, data)

    def test_slice_failing_for_good(self):
        small_slices(self.fake)
        driver = self.new_driver(driver_class=driver_class(self.fake, **SMALL))
        self.fake.broken_slices = 99
        data = os.urandom(2 * 64 * KiB)
        with self.assertRaises(BackendError):
            driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.counts["upload"], driver.slice_tries)
        self.assertIsNone(self.fake.find(f"{APP_FOLDER}/a.bin"))
        self.assertEqual(self.fake.counts["create"], 0)

    def test_slice_refused(self):
        small_slices(self.fake)
        self.fake.limits = {0: (512 * KiB, 32 * KiB)}  # Baidu takes smaller slices than the driver sends: 31364
        driver = self.new_driver(driver_class=driver_class(self.fake, **SMALL))
        data = os.urandom(100 * KiB)
        with self.assertRaises(BackendError) as caught:
            driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertIn("31364", caught.exception.message)
        self.assertEqual(self.fake.counts["upload"], 1)  # not tried again

    def test_membership_decides_large_files(self):
        small_slices(self.fake)
        Small = driver_class(self.fake, **SMALL)
        data = os.urandom(600 * KiB)  # more than 8 slices of 64 KiB
        with self.assertRaises(DriverError) as caught:
            self.new_driver(driver_class=Small).write("/big.bin", Untouchable(), len(data))
        self.assertIn("512 KB", caught.exception.message)
        self.assertEqual((self.fake.counts["uinfo"], self.fake.counts["precreate"]), (1, 0))
        self.fake.vip_type = 1
        driver = self.new_driver(driver_class=Small)
        driver.write("/big.bin", io.BytesIO(data), len(data))
        self.assertEqual({size for _, size in self.fake.slices[:-1]}, {128 * KiB})  # 75 KiB a slice, made a multiple of 64
        self.assertEqual(len(self.fake.slices), 5)
        self.assertEqual(read(driver, "/big.bin"), data)
        with self.assertRaises(DriverError):
            driver.write("/huge.bin", Untouchable(), 3 * MiB)  # more than this membership takes
        self.assertEqual(self.fake.counts["uinfo"], 2)  # once for each driver: it's kept for vip_ttl

    def test_overwrite_replaces_the_file(self):
        driver = self.new_driver()
        driver.write("/o.txt", io.BytesIO(b"first"), 5)
        old = self.fake.find(f"{APP_FOLDER}/o.txt").fs_id
        driver.write("/o.txt", io.BytesIO(b"second"), 6)
        node = self.fake.find(f"{APP_FOLDER}/o.txt")
        self.assertEqual(node.data, b"second")
        self.assertNotEqual(node.fs_id, old)
        self.assertEqual([child.name for child in self.fake.children(APP_FOLDER)], ["o.txt"])
        self.assertEqual(read(driver, "/o.txt"), b"second")

    def test_upload_hosts(self):
        driver = self.new_driver()
        self.fake.upload_servers = ["ftp://c3.pcs.baidu.com", "https://c3.pcs.example.com", self.fake.other_url]
        data = os.urandom(2000)
        driver.write("/a.bin", io.BytesIO(data), len(data))  # through the backup server, which is the fake's
        self.assertEqual(self.fake.other_requests, [])
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/a.bin").data, data)

    def test_secure_addresses(self):
        driver = Driver(self.settings())  # Baidu's own settings: https on Baidu's hosts only
        self.addCleanup(driver.close)
        self.assertEqual(driver._secure("http://d.pcs.baidu.com/file/x?fid=1&sign=a%3D"),
                         "https://d.pcs.baidu.com/file/x?fid=1&sign=a%3D")
        self.assertEqual(driver._secure(" https://c3.pcs.baidu.com "), "https://c3.pcs.baidu.com")
        self.assertEqual(driver._secure("https://qdall01.baidupcs.com/file/x"), "https://qdall01.baidupcs.com/file/x")
        for bad in ("https://evil.example/baidu.com", "https://baidu.com.evil.example", "https://xbaidu.com/",
                    "ftp://d.pcs.baidu.com/x", "/file/x", "", None, 5):
            with self.subTest(bad=bad):
                self.assertIsNone(driver._secure(bad))

    def test_name_taken_in_baidu_meanwhile(self):
        driver = self.new_driver()
        driver.write("/a.txt", io.BytesIO(b"a"), 1)
        driver.mkdir("/dst")
        self.assertEqual(driver.list("/dst"), [])  # cached now
        self.fake.put_file(f"{APP_FOLDER}/dst/a.txt", b"made in Baidu's app")
        with self.assertRaises(AlreadyExists):  # Baidu's filemanager says so (errno 12, the item -8)
            driver.move("/a.txt", "/dst")
        self.assertEqual(self.fake.find(f"{APP_FOLDER}/a.txt").data, b"a")
        self.assertEqual(names(driver.list("/dst")), ["a.txt"])

    # -- downloads

    def test_range_and_cached_links(self):
        driver = self.new_driver()
        data = os.urandom(10000)
        driver.write("/r.bin", io.BytesIO(data), len(data))
        self.assertEqual(read(driver, "/r.bin", 100, 50), data[100:150])
        self.assertEqual(read(driver, "/r.bin", 9990), data[9990:])
        self.assertEqual(read(driver, "/r.bin"), data)
        self.assertEqual(self.fake.counts["filemetas"], 1)  # the dlink is reused
        self.assertEqual((self.fake.counts["download"], self.fake.counts["cdn"]), (3, 3))  # Baidu's redirect followed
        self.assertEqual(self.fake.leaks, [])  # the token stayed with Baidu's link
        self.assertIsNone(driver.download_url("/r.bin", filename="r.bin", user_agent="Mozilla/5.0"))

    def test_expired_link_asked_for_again(self):
        driver = self.new_driver()
        data = os.urandom(5000)
        driver.write("/e.bin", io.BytesIO(data), len(data))
        self.assertEqual(read(driver, "/e.bin"), data)
        for status in (200, 403):  # Baidu's docs answer with HTTP 200 and errno 31360
            with self.subTest(status=status):
                self.fake.download_error_status = status
                self.fake.expire_links()
                asked = self.fake.counts["filemetas"]
                self.assertEqual(read(driver, "/e.bin", 10, 20), data[10:30])
                self.assertEqual(self.fake.counts["filemetas"], asked + 1)

    def test_refused_downloads(self):
        driver = self.new_driver()
        data = os.urandom(5000)
        driver.write("/d.bin", io.BytesIO(data), len(data))
        self.fake.fail_next["download"] = 31326  # Baidu's anti-leech
        with self.assertRaises(BackendError) as caught:
            read(driver, "/d.bin")
        self.assertIn("31326", caught.exception.message)
        self.fake.fail_next["download"] = ("http", 500)
        with self.assertRaises(BackendError):
            read(driver, "/d.bin")
        self.fake.redirect_downloads = False  # the dlink may answer with the file itself
        self.assertEqual(read(driver, "/d.bin", 1, 2), data[1:3])

    def test_download_after_the_token_was_refused(self):
        driver = self.new_driver()
        data = os.urandom(4000)
        driver.write("/t.bin", io.BytesIO(data), len(data))
        self.assertEqual(read(driver, "/t.bin"), data)  # the link is cached
        self.fake.expire_access_tokens()  # the dlink refuses the token (31045): refreshed once
        self.assertEqual(read(driver, "/t.bin"), data)
        self.assertEqual(self.fake.counts["token"], 1)

    def test_file_gone_meanwhile(self):
        driver = self.new_driver()
        driver.write("/g.bin", io.BytesIO(b"gone"), 4)
        driver.stat("/g.bin")  # cached
        with self.fake.lock:
            del self.fake.nodes[f"{APP_FOLDER}/g.bin"]
        with self.assertRaises(NotFound):
            read(driver, "/g.bin")
