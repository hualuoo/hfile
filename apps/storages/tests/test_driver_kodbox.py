"""Tests of the KodBox driver (apps.storages.drivers.kodbox) against tests/fake_kodbox.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_kodbox
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import unittest
import uuid
from http.cookiejar import CookieJar, DefaultCookiePolicy
from unittest import mock

from apps.storages import config
from apps.storages.drivers import http, kodbox
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotFound
from apps.storages.drivers.kodbox import KodBoxDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_kodbox import UPLOAD, FakeKodBox, hash_simple


def driver_class(**attrs):
    """KodBoxDriver with other tuning (the type stays "kodbox")."""
    return type("FakeKodBoxDriver", (KodBoxDriver,), attrs)


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


class Stream(io.RawIOBase):
    """Data that can't seek."""

    def __init__(self, raw: bytes):
        self._raw = io.BytesIO(raw)

    def readable(self):
        return True

    def readinto(self, buffer):
        return self._raw.readinto(buffer)


class FakeMixin:
    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeKodBox().start()
        cls.Driver = driver_class(**cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_user(self) -> int:
        self.user = "u-" + uuid.uuid4().hex[:8]
        return self.fake.add_user(self.user, "pw " + self.user)

    def new_driver(self, base: str = "/", driver_class=None, **settings):
        settings = {"url": self.fake.url, "username": self.user, "password": "pw " + self.user, "base_path": base, **settings}
        driver = (driver_class or self.Driver)({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class NamesKodBoxKeeps:
    def allowed(self, name):
        """KodBox stores characters of 4 bytes as "-" and leaves names starting with . or ~ out of its listings: the
        driver refuses them (NameTests)."""
        return super().allowed(name) and all(ord(ch) <= 0xFFFF for ch in name) and not name.startswith((".", "~"))


class KodBoxContractTests(FakeMixin, NamesKodBoxKeeps, DriverContract, unittest.TestCase):
    """The contract at the root of a new account's own folder (which lists the safe box too)."""

    def make_driver(self):
        self.fake.reset()
        self.new_user()
        return self.new_driver()


class KodBoxPartsContractTests(FakeMixin, NamesKodBoxKeeps, DriverContract, unittest.TestCase):
    """Again in a folder of the account, with small listing pages and upload parts: the larger files go in many."""

    driver_attrs = {"page_size": 5}

    def make_driver(self):
        self.fake.reset()
        self.fake.chunk_size = 64 * 1024
        self.new_user()
        self.fake.mkdirs(self.user, "/base/here")
        self.fake.put(self.user, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here")


class KodBoxTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.home = self.new_user()

    def put(self, driver, path: str, data: bytes) -> None:
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    # -- logins

    def test_an_ended_login_logs_in_again(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"aaa")
        logins = self.fake.logins
        self.fake.expire_sessions()  # 4 hours unused
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(self.fake.logins, logins + 1)
        self.fake.expire_sessions()
        self.assertEqual(read(d, "/a.txt"), b"aaa")  # a download too
        self.fake.forget_tokens()  # KodBox can't read the token any more
        d.mkdir("/m")
        self.assertEqual(self.fake.logins, logins + 3)
        self.assertEqual(self.names(d, "/"), ["a.txt", "m"])

    def test_a_login_that_ends_during_an_upload(self):
        self.fake.chunk_size = 1000
        d = self.new_driver()
        data = os.urandom(3500)
        self.fake.expire_on_chunk = 2
        self.put(d, "/big.bin", data)  # the part is sent again after logging in
        self.assertEqual(self.fake.find(self.user, "/big.bin").data, data)
        self.assertEqual([part["chunk"] for part in self.fake.routes(UPLOAD)], ["0", "1", "2", "2", "3"])
        self.fake.expire_on_chunk = 1
        with self.assertRaises(BackendError):  # data that can't seek can't be sent again
            d.write("/stream.bin", Stream(data), len(data))
        d.write("/stream.bin", Stream(data), len(data))
        self.assertEqual(self.fake.find(self.user, "/stream.bin").data, data)

    def test_a_refused_login(self):
        d = self.new_driver(password="wrong")
        with self.assertRaises(AuthError):
            d.list("/")
        logins = self.fake.logins
        with self.assertRaises(AuthError):  # remembered for a while: KodBox isn't asked again (it locks accounts)
            d.list("/")
        self.assertEqual(self.fake.logins, logins)
        self.fake.captcha = True
        with self.assertRaises(AuthError):
            self.new_driver().list("/")
        self.fake.captcha = False
        self.fake.users[self.user].disabled = True
        with self.assertRaises(AuthError):
            self.new_driver().list("/")

    def test_an_account_disabled_after_the_login(self):
        d = self.new_driver()
        d.list("/")
        self.fake.users[self.user].disabled = True
        with self.assertRaises(AuthError):
            d.mkdir("/x")

    def test_a_session_kodbox_signs_out(self):
        """KodBox signs the oldest sessions out when the account logs in on more devices than it may (userLoginLimit,
        5 by default): their next call is code 10002, "logged in elsewhere". The driver logs in again for it."""
        d = self.new_driver()
        self.put(d, "/a.txt", b"aaa")
        logins = self.fake.logins
        self.fake.kick_sessions(self.user)
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.fake.kick_sessions(self.user)
        self.assertEqual(read(d, "/a.txt"), b"aaa")  # a download too
        self.assertEqual(self.fake.logins, logins + 2)

    def test_close_logs_out(self):
        """A closed instance ends its KodBox session, which would otherwise take one of the account's places
        (userLoginLimit) for 4 hours. Best effort: it never raises."""
        d = self.new_driver()
        d.list("/")
        self.assertEqual(len(self.fake.sessions), 1)
        d.close()
        self.assertEqual(self.fake.sessions, {})
        [logout] = self.fake.routes("user/index/logout")
        self.assertTrue(logout["accessToken"])
        self.new_driver().close()  # never logged in: nothing to end
        self.assertEqual(len(self.fake.routes("user/index/logout")), 1)
        failing = self.new_driver()
        failing.list("/")
        self.fake.fail_status = 502
        failing.close()

    def test_the_token_stays_out_of_urls_and_cookies_out_of_requests(self):
        self.fake.chunk_size = 1000
        d = self.new_driver()
        data = os.urandom(2500)
        self.put(d, "/a/one.bin", data)
        d.write("/a/two.bin", Stream(data), len(data))
        d.mkdir("/b")
        d.copy("/a/one.bin", "/b")
        d.move("/a/two.bin", "/b")
        d.rename("/b/two.bin", "three.bin")
        self.assertEqual(read(d, "/b/one.bin", 5, 10), data[5:15])
        self.fake.redirect_downloads = True
        self.assertEqual(read(d, "/b/three.bin"), data)
        d.delete("/a")
        self.assertTrue(self.fake.calls)
        for call in self.fake.calls:
            self.assertNotIn("accessToken", call["query"])
            self.assertNotIn("password", call["query"])
            self.assertEqual(call["method"], "POST")
            self.assertEqual(call["params"]["language"], "zh-CN")  # the messages the driver tells apart
            self.assertIsNone(call["cookie"])  # every answer sets KodBox's session cookie
        [storage] = self.fake.storage_requests  # where the redirect led: no token, no cookie, no body
        self.assertEqual((storage["method"], storage["body"], storage["cookie"]), ("GET", b"", None))
        self.assertNotIn("accessToken", storage["query"])

    # -- listings

    def test_pages_of_a_listing(self):
        for n in range(230):
            self.fake.put(self.user, f"/big/f{n:03}.txt", b"x")
        big = self.fake.find(self.user, "/big")
        d = self.new_driver(driver_class=driver_class(page_size=100))
        self.assertEqual(self.names(d, "/big"), [f"f{n:03}.txt" for n in range(230)])
        calls = [call for call in self.fake.routes("explorer/list/path") if call["path"] == f"{{source:{big.id}}}/"]
        self.assertEqual([(call["page"], call["pageNum"]) for call in calls], [("1", "100"), ("2", "100"), ("3", "100")])
        self.fake.max_page_num = 40  # KodBox's pages may be smaller than asked: its pageTotal is followed
        self.assertEqual(len(d.list("/big")), 230)

    def test_the_account_folder(self):
        """The root is the account's own folder; its safe box ({block:safe}/) isn't a {source:ID}/ and is left out."""
        self.fake.put(self.user, "/doc.txt", b"d")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["doc.txt"])
        d.list("/")
        self.assertEqual(len(self.fake.routes("user/view/options")), 1)  # asked once
        with self.assertRaises(NotFound):
            self.new_driver("/no/such").list("/")

    def test_a_folder_deleted_in_kodbox(self):
        self.fake.put(self.user, "/a/b/c.txt", b"c")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/a/b"), ["c.txt"])
        self.fake.find(self.user, "/a/b").deleted = True  # into its recycle bin, in KodBox's own pages
        with self.assertRaises(NotFound):
            d.list("/a/b")

    # -- names

    def test_characters_kodbox_cant_keep(self):
        d = self.new_driver()
        self.put(d, "/ok.txt", b"o")
        for change in (lambda: self.put(d, "/new/a😀.txt", b"x"), lambda: d.mkdir("/new/😀"), lambda: d.rename("/ok.txt", "😀.txt")):
            with self.assertRaises(InvalidPath):
                change()
        self.assertEqual(self.names(d, "/"), ["ok.txt"])  # nothing made, not even /new
        self.assertEqual(KodBoxDriver.forbidden_chars, '\\:*?"<>|')
        self.assertEqual(KodBoxDriver.max_name_bytes, 250)
        with self.assertRaises(InvalidPath):  # KodBox's own refusals
            d._create_node(str(self.home), "a:b")
        with self.assertRaises(InvalidPath):
            d._create_node(str(self.home), "长" * 90)

    def test_names_kodbox_leaves_out_of_its_listings(self):
        d = self.new_driver()
        for change in (lambda: self.put(d, "/.env", b"x"), lambda: self.put(d, "/a/x.TMP", b"x"), lambda: d.mkdir("/~old"),
                       lambda: self.put(d, "/Thumb.db", b"x"), lambda: d.mkdir("/.git")):
            with self.assertRaises(InvalidPath):
                change()
        d.mkdir("/x.tmp")  # a folder: the extensions are of files
        self.put(d, "/x.tmpl", b"t")
        self.assertEqual(self.names(d, "/"), ["x.tmp", "x.tmpl"])
        with self.assertRaises(InvalidPath):
            d.rename("/x.tmpl", ".x")
        self.fake.users[self.user].config["displayHideFile"] = "1"  # the account shows them
        shown = self.new_driver()
        self.put(shown, "/.env", b"e")
        shown.mkdir("/~old")
        self.assertEqual(self.names(shown, "/"), [".env", "x.tmp", "x.tmpl", "~old"])

    def test_the_hidden_names_kodbox_is_set_to(self):
        self.fake.path_hidden = "Thumbs.db,*.bak"
        d = self.new_driver()
        self.put(d, "/a.tmp", b"t")
        for path in ("/a.bak", "/Thumbs.db"):
            with self.assertRaises(InvalidPath):
                self.put(d, path, b"b")
        self.fake.path_hidden = kodbox.DEFAULT_HIDDEN
        self.fake.options_list_hidden = False  # options without pathHidden: KodBox's default
        with self.assertRaises(InvalidPath):
            self.put(self.new_driver(), "/b.tmp", b"t")

    def test_an_upload_kodbox_names_otherwise(self):
        self.fake.name_filter = lambda name: name.replace(" ", "_")
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/a b.txt", b"x")
        self.assertIn("a_b.txt", caught.exception.message)

    # -- changes

    def test_delete_puts_into_the_recycle_bin(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        source = self.fake.find(self.user, "/a.txt")
        d.delete("/a.txt")
        self.assertTrue(self.fake.sources[source.id].deleted)  # still there, in the recycle bin
        self.assertNotIn("shiftDelete", self.fake.routes("explorer/index/pathDelete")[-1])
        self.fake.users[self.user].config["recycleOpen"] = "0"  # the account's recycle bin is off: gone for good
        self.put(d, "/b.txt", b"b")
        source = self.fake.find(self.user, "/b.txt")
        d.delete("/b.txt")
        self.assertNotIn(source.id, self.fake.sources)

    def test_names_taken_meanwhile(self):
        """What KodBox answers when a name was taken after the listing HFile went by."""
        d = self.new_driver()
        self.fake.put(self.user, "/f", b"a file")
        with self.assertRaises(AlreadyExists):  # mkdir answers the parent's path
            d._create_node(str(self.home), "f")
        self.put(d, "/a/x", b"1")
        self.put(d, "/a/sub/y", b"2")
        d.list("/a")
        dst = self.fake.mkdirs(self.user, "/dst")
        self.fake.put(self.user, "/dst/x", b"other")
        self.fake.mkdirs(self.user, "/dst/sub")
        nodes = {node.name: node for node in d._fetch(str(self.fake.find(self.user, "/a").id))}
        with self.assertRaises(DriverError):  # skip: a file of the name isn't replaced
            d._move_node(nodes["x"], "", str(dst.id))
        with self.assertRaises(AlreadyExists):  # a folder of the name: KodBox answers that one
            d._move_node(nodes["sub"], "", str(dst.id))
        self.assertEqual(self.fake.names(self.user, "/a"), ["sub", "x"])
        self.assertEqual(self.fake.find(self.user, "/dst/x").data, b"other")
        self.assertTrue(all(call["fileRepeat"] == "skip" for call in self.fake.routes("explorer/index/pathCuteTo")))

    # -- uploads

    def test_a_small_file_in_one_request(self):
        d = self.new_driver()
        self.put(d, "/s.txt", b"small")
        [call] = self.fake.routes(UPLOAD)
        self.assertEqual((call["name"], call["size"], call["fileRepeat"], call["fileInfo"]), ("s.txt", "5", "replace", "1"))
        self.assertNotIn("chunk", call)
        self.put(d, "/s.txt", b"newer")  # replaced: the same file, its new version
        self.assertEqual(self.fake.names(self.user, "/"), ["s.txt"])
        self.assertEqual(read(d, "/s.txt"), b"newer")

    def test_parts(self):
        self.fake.chunk_size = 1000  # KodBox's setting, from user/view/options
        d = self.new_driver()
        data = os.urandom(3500)
        source = io.BytesIO(b"xx" + data)
        source.seek(2)
        d.write("/p.bin", source, len(data))
        calls = self.fake.routes(UPLOAD)
        self.assertEqual([call["chunk"] for call in calls], ["0", "1", "2", "3"])
        self.assertTrue(all((call["chunks"], call["chunkSize"], call["size"]) == ("4", "1000", "3500") for call in calls))
        self.assertEqual({call["checkHashSimple"] for call in calls}, {hash_simple(data)})
        self.assertEqual(self.fake.find(self.user, "/p.bin").data, data)
        d.write("/s.bin", Stream(data), len(data))  # can't seek: no hash, KodBox checks each part
        self.assertNotIn("checkHashSimple", self.fake.routes(UPLOAD)[-1])
        self.assertEqual(read(d, "/s.bin"), data)

    def test_an_unfinished_upload_of_the_name_isnt_finished_with(self):
        """KodBox keeps the parts of an upload by the user, folder, name and size unless they carry checkHashSimple:
        those of an earlier one that didn't finish would complete this one."""
        self.fake.chunk_size = 1000
        d = self.new_driver()
        d.list("/")
        data = os.urandom(3500)
        named = hashlib.md5(f"{self.user}{{source:{self.home}}}/u.bin3500".encode()).hexdigest()
        self.fake.temp[hashlib.md5(f"{named}1000".encode()).hexdigest()] = {
            "data": bytearray(b"!" * 3500), "parts": {1: 1000, 2: 1000, 3: 500}}
        self.put(d, "/u.bin", data)
        self.assertEqual(self.fake.find(self.user, "/u.bin").data, data)

    def test_kodbox_finishes_an_upload_it_has_the_rest_of(self):
        self.fake.chunk_size = 1000
        d = self.new_driver()
        d.list("/")
        data = os.urandom(3500)
        key = hashlib.md5(f"{hash_simple(data)}1000".encode()).hexdigest()
        self.fake.temp[key] = {"data": bytearray(data), "parts": {1: 1000, 2: 1000, 3: 500}}
        self.put(d, "/r.bin", data)
        self.assertEqual(len(self.fake.routes(UPLOAD)), 1)  # done after the first part
        self.assertEqual(self.fake.find(self.user, "/r.bin").data, data)

    def test_upload_limits(self):
        self.fake.chunk_size = 1000
        self.fake.upload_max = 600  # PHP's upload_max_filesize below KodBox's chunk size
        d = self.new_driver()
        data = os.urandom(1500)
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/big.bin", data)
        self.assertIn("upload_max_filesize", caught.exception.message)
        self.fake.upload_max = None
        self.fake.body_max = 900  # the web server's limit
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", data)

    def test_hash_simple(self):
        for size in (0, 1, 10000, 10001, 123457):
            data = os.urandom(size)
            with self.subTest(size=size):
                self.assertEqual(kodbox.hash_simple(io.BytesIO(b"abc" + data), 3, size), hash_simple(data))

    def test_the_last_part_waits_longer(self):
        """KodBox answers the part that ends an upload once it has stored the whole file, which takes long on object
        storage: that answer may take a second per MiB longer than http.TIMEOUT, as webdav.py's uploads do. Here
        http.TIMEOUT is 1 s and the answer comes 2 s after a 3 MiB file is stored."""
        self.fake.chunk_size = 1024 * 1024
        self.fake.slow_finish = 2.0
        d = self.new_driver()
        d.list("/")
        d._http.close()
        d._http = http.client(timeout=1.0, cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])))
        data = os.urandom(3 * 1024 * 1024)
        with mock.patch.object(http, "TIMEOUT", 1.0):
            self.put(d, "/big.bin", data)
        self.assertEqual(self.fake.find(self.user, "/big.bin").data, data)

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件 1.bin", data)
        self.put(d, "/empty", b"")
        self.assertIsNone(d.download_url("/dl/文件 1.bin", filename="文件 1.bin"))
        self.assertEqual(read(d, "/dl/文件 1.bin"), data)
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        self.assertEqual([(g["method"], g["range"], g["download"]) for g in self.fake.downloads],
                         [("POST", None, "1"), ("POST", "bytes=10-29", "1"), ("POST", "bytes=5000-", "1")])
        self.assertEqual(read(d, "/empty"), b"")
        self.assertEqual(len(self.fake.downloads), 3)  # nothing to fetch
        self.fake.redirect_downloads = True  # a KodBox storage on object storage
        self.assertEqual(read(d, "/dl/文件 1.bin", 100, 50), data[100:150])
        self.assertEqual(self.fake.storage_requests[-1]["range"], "bytes=100-149")

    def test_a_json_file(self):
        d = self.new_driver()
        data = json.dumps({"code": False, "data": "not an answer"}).encode()
        self.put(d, "/a.json", data)
        self.assertEqual(read(d, "/a.json"), data)  # it comes as a download (Content-Disposition)

    def test_a_download_kodbox_refuses(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.fake.denied = {"explorer/index/fileOut"}  # the role can't download
        with self.assertRaises(DriverError):
            read(d, "/a.txt")
        self.fake.denied = set()
        d.list("/")
        self.fake.find(self.user, "/a.txt").deleted = True
        with self.assertRaises(NotFound):
            read(d, "/a.txt")

    # -- errors

    def test_errors(self):
        failure = KodBoxDriver._failure
        self.assertIsInstance(failure({"code": False, "data": "该文档不存在！"}), NotFound)
        self.assertIsInstance(failure({"code": False, "data": "该文件在回收站中,请还原后再试!"}), NotFound)
        self.assertIsInstance(failure({"code": False, "data": "该名称已存在！"}), AlreadyExists)
        self.assertIsInstance(failure({"code": False, "data": "长度超出最大限制! (max=256)"}), InvalidPath)
        self.assertIsInstance(failure({"code": "10001", "data": "您尚未登录!请先登录"}), AuthError)
        self.assertIsInstance(failure({"code": False, "data": "token error!", "info": "10001"}), AuthError)
        self.assertIsInstance(failure({"code": "10002", "data": "账号被禁用或尚未启用！请联系管理员"}), AuthError)
        self.assertIs(type(failure({"code": False, "data": "您没有此权限，请联系管理员!", "info": 1004})), DriverError)
        self.assertIn("奇怪", failure({"code": False, "data": "奇怪的错误"}).message)
        d = self.new_driver()
        self.fake.space_full = True
        with self.assertRaises(DriverError):
            self.put(d, "/a.txt", b"a")
        self.fake.space_full = False
        self.fake.denied = {"explorer/index/mkdir"}
        with self.assertRaises(DriverError):
            d.mkdir("/m")
        self.fake.fail_status = 502
        with self.assertRaises(BackendError):
            self.new_driver().list("/")
        self.fake.fail_status = 0
        with self.assertRaises(BackendError):  # not a KodBox site
            self.new_driver(url=self.fake.url + "/storage").list("/")

    # -- settings

    def test_settings(self):
        check = KodBoxDriver.validate_config
        self.assertEqual(check({"url": "https://kod.example.com", "username": "a", "password": "p"}), {})
        self.assertIn("url", check({"url": "kod.example.com"}))
        self.assertIn("url", check({"url": "ftp://kod.example.com"}))
        self.assertIn("base_path", check({"url": "https://kod.example.com", "base_path": "/a/../b"}))
        d = KodBoxDriver({"url": " https://kod.example.com/kod/index.php?user/index#x ", "username": "a", "password": "p"})
        self.addCleanup(d.close)
        self.assertEqual(d._url("explorer/list/path"), "https://kod.example.com/kod/index.php?explorer/list/path")

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in KodBoxDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(KodBoxDriver), ["url", "username", "password"])
        self.assertEqual(KodBoxDriver.auth_params, ())


if __name__ == "__main__":
    unittest.main()
