"""Tests of the Cloudreve v3 driver (apps.storages.drivers.cloudreve_v3) against tests/fake_cloudreve_v3.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_cloudreve_v3
"""

from __future__ import annotations

import io
import json
import os
import unittest
import urllib.parse
import uuid

import httpx

from apps.storages import config
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.drivers.cloudreve_v3 import CloudreveV3Driver, _session_cookie
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloudreve_v3 import FakeCloudreveV3


def driver_class(**attrs):
    """CloudreveV3Driver with other tuning (the type stays "cloudreve-v3")."""
    return type("FakeCloudreveV3Driver", (CloudreveV3Driver,), attrs)


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
    version = "3.8.3"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeCloudreveV3(cls.version).start()
        cls.Driver = driver_class(**cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_user(self, **kwargs) -> str:
        self.email = f"u-{uuid.uuid4().hex[:8]}@example.com"
        self.fake.add_user(self.email, "pw " + self.email, **kwargs)
        return self.email

    def new_driver(self, base: str = "/", driver_class=None, **settings):
        settings = {"url": self.fake.url, "email": self.email, "password": "pw " + self.email, "base_path": base, **settings}
        driver = (driver_class or self.Driver)({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class CloudreveV3ContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root of a new account, with the local storage policy."""

    def make_driver(self):
        self.fake.reset()
        self.new_user()
        return self.new_driver()


class CloudreveV3ChunksContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again in a folder of the account, with small upload chunks."""

    def make_driver(self):
        self.fake.reset()
        self.fake.policy["chunk_size"] = 64 * 1024
        self.new_user()
        self.fake.mkdirs(self.email, "/base/here")
        self.fake.put(self.email, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here")


class CloudreveV3ThroughContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again with a storage policy whose uploads would go straight to S3: files go through Cloudreve itself, and the
    download links are the storage's own."""

    def make_driver(self):
        self.fake.reset()
        self.fake.policy["type"] = "s3"
        self.new_user()
        return self.new_driver()


class CloudreveV3Tests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.new_user()

    def put(self, driver, path: str, data: bytes) -> None:
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    def chunks(self) -> list[tuple[str, str]]:
        """(index, Content-Length) of the chunks sent."""
        return [(call["path"].rsplit("/", 1)[1], call["length"])
                for call in self.fake.routes("POST", "/file/upload/", prefix=True)]

    # -- the login and its session

    def test_the_session_cookie(self):
        self.fake.policy["chunk_size"] = 1000
        d = self.new_driver()
        data = os.urandom(2500)
        self.put(d, "/a/one.bin", data)
        d.write("/a/two.bin", Stream(data), len(data))
        self.put(d, "/a/one.bin", data[::-1])  # a replacement
        d.mkdir("/b")
        d.copy("/a/one.bin", "/b")
        d.move("/a/two.bin", "/b")
        d.rename("/b/two.bin", "three.bin")
        self.assertEqual(read(d, "/b/one.bin", 5, 10), data[::-1][5:15])
        url = d.download_url("/b/three.bin", filename="three.bin")
        d.delete("/a")
        [cookie] = self.fake.sessions
        password = "pw " + self.email
        for call in self.fake.calls:
            self.assertNotIn(password, urllib.parse.unquote(call["query"]))
            self.assertNotIn(cookie, urllib.parse.unquote(call["query"]))
            if call["path"] == "/api/v3/user/session":
                self.assertIsNone(call["cookie"])
            else:
                self.assertEqual(call["cookie"], "cloudreve-session=" + cookie, call)
        self.assertTrue(self.fake.downloads)
        for request in self.fake.downloads:  # the links are their own permission
            self.assertIsNone(request["cookie"])
        self.assertNotIn(cookie, url)
        [login] = self.fake.routes("POST", "/user/session")
        self.assertEqual(login["json"], {"userName": self.email, "Password": password})
        self.assertEqual(len(d._http.cookies), 0)  # the client keeps no cookie itself

    def test_the_cookie_of_a_login(self):
        response = httpx.Response(200, headers=[("Set-Cookie", "other=1; Path=/"),
                                                ("Set-Cookie", "cloudreve-session=MTY5_a-b==; Path=/; Max-Age=5184000; HttpOnly")])
        self.assertEqual(_session_cookie(response), "MTY5_a-b==")
        self.assertIsNone(_session_cookie(httpx.Response(200, headers=[("Set-Cookie", "cloudreve-session=; Path=/")])))

    def test_a_forgotten_session_logs_in_again(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.fake.expire_sessions()  # Cloudreve restarted without its cache
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(self.fake.logins, 2)
        self.fake.expire_sessions()
        d.delete("/a.txt")
        self.assertEqual((self.fake.logins, self.names(d, "/")), (3, []))
        self.fake.ban(self.email)  # Cloudreve takes the session for no login, and refuses the new one
        with self.assertRaises(AuthError):
            d.list("/")

    def test_the_session_is_renewed_before_cloudreve_refuses_it(self):
        d = self.new_driver()
        d.list("/")
        d.list("/")
        self.assertEqual(self.fake.logins, 1)
        d = self.new_driver(driver_class=driver_class(session_lifetime=0.0))
        d.list("/")
        d.list("/")
        self.assertEqual(self.fake.logins, 3)

    def test_refused_logins(self):
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong").list("/")
        self.fake.login_captcha = True
        with self.assertRaises(AuthError):
            self.new_driver().list("/")
        self.fake.login_captcha = False
        for kwargs in ({"two_factor": True}, {"status": "banned"}, {"status": "inactive"}):
            with self.subTest(**kwargs):
                self.new_user(**kwargs)
                with self.assertRaises(AuthError):
                    self.new_driver().list("/")
        self.assertEqual(self.fake.sessions, {})  # the two-factor login's cookie wasn't taken for a login
        with self.assertRaises(AuthError) as caught:  # Cloudreve wants an email address
            self.new_driver(email="alice").list("/")
        self.assertIn("40001", caught.exception.message)

    def test_a_refused_login_is_remembered_for_a_while(self):
        d = self.new_driver(password="wrong")
        with self.assertRaises(AuthError):
            d.list("/")
        with self.assertRaises(AuthError):
            d.list("/")
        self.assertEqual(self.fake.logins, 1)

    # -- versions

    def test_a_site_before_3_5(self):
        """Cloudreve 3.4 lists and changes things as 3.5 does, but has no upload sessions."""
        self.fake.version = "3.4.2"
        self.addCleanup(setattr, self.fake, "version", "3.8.3")
        d = self.new_driver()
        d.mkdir("/a")
        self.assertEqual(self.names(d, "/"), ["a"])
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/a/b.txt", b"b")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("3.5", caught.exception.message)

    # -- listings and paths

    def test_names_are_encoded(self):
        """Names go into the listing's path percent-encoded; the fake refuses a bad escape as Go does."""
        d = self.new_driver()
        names = ["100%.txt", "#hash", "a+b=c&d.txt", "semi;colon,comma", "[x](y)", "a b", "it's (1)!", "~tilde", "中文 名字"]
        for name in names:
            with self.subTest(name=name):
                self.put(d, f"/dir {name}/{name}", name.encode())
                self.assertEqual(read(d, f"/dir {name}/{name}"), name.encode())
                self.assertEqual(self.names(d, f"/dir {name}"), [name])
        self.assertEqual(self.names(d, "/"), sorted("dir " + name for name in names))
        listed = {call["path"] for call in self.fake.routes("GET", "/directory/", prefix=True)}
        self.assertIn("/api/v3/directory/dir 100%.txt", listed)
        self.assertIn("/api/v3/directory/dir #hash", listed)
        self.assertIn("/api/v3/directory/", listed)

    def test_the_base_path(self):
        self.fake.put(self.email, "/in/a.txt", b"a")
        d = self.new_driver("/in")
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.put(d, "/b/c.txt", b"c")
        self.assertEqual(self.fake.data(self.email, "/in/b/c.txt"), b"c")
        missing = self.new_driver("/no/such")
        for call in (lambda: missing.list("/"), lambda: missing.stat("/"), lambda: missing.stat("/x")):
            with self.assertRaises(NotFound) as caught:
                call()
            self.assertIn("/no/such", caught.exception.message)
        with self.assertRaises(NotADirectory):
            self.new_driver("/in/a.txt").list("/")

    def test_a_folder_and_a_file_of_one_name(self):
        """Cloudreve keeps folders and files apart: both can be called "dup". A path finds the folder."""
        self.fake.mkdirs(self.email, "/x/dup")
        self.fake.put(self.email, "/x/dup", b"a file")
        self.fake.put(self.email, "/x/other", b"other")
        d = self.new_driver()
        [entry] = [entry for entry in d.list("/x") if entry.name == "dup"]
        self.assertTrue(entry.is_dir)
        self.assertTrue(d.stat("/x/dup").is_dir)
        with self.assertRaises(AlreadyExists):
            self.put(d, "/x/dup", b"again")
        with self.assertRaises(AlreadyExists):
            d.rename("/x/other", "dup")
        self.assertEqual(self.fake.names(self.email, "/x"), ["dup", "dup", "other"])

    # -- changes

    def test_mkdir_looks_first(self):
        """Cloudreve keeps a folder already there without an error, and makes the folders above."""
        d = self.new_driver()
        d.mkdir("/x/y")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/x/y")
        self.put(d, "/f", b"f")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/f")
        self.assertEqual([call["json"] for call in self.fake.routes("PUT", "/directory")], [{"path": "/x/y"}])
        with self.assertRaises(NotADirectory):  # a file where a folder should be
            self.put(d, "/f/g/h.txt", b"h")

    def test_names_ending_in_a_space(self):
        """Cloudreve drops the spaces at the end of a new folder's name, and refuses them in other names."""
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        for change in (lambda: d.mkdir("/x "), lambda: d.mkdir("/x /y"), lambda: self.put(d, "/x /y.txt", b"y"),
                       lambda: self.put(d, "/y.txt ", b"y"), lambda: d.rename("/a.txt", "b.txt ")):
            with self.assertRaises(InvalidPath):
                change()
        self.assertEqual(self.fake.names(self.email, "/"), ["a.txt"])
        self.assertEqual(self.fake.routes("PUT", "/directory"), [])

    def test_names_cloudreve_refuses(self):
        self.assertEqual(CloudreveV3Driver.forbidden_chars, '\\:*?"<>|')
        self.assertEqual(CloudreveV3Driver.max_name_bytes, 255)
        d = self.new_driver()
        d.mkdir("/" + "a" * 255)
        self.put(d, "/b.txt", b"b")
        for change in (lambda: d.mkdir("/" + "a" * 256), lambda: d.mkdir("/a:b"), lambda: self.put(d, "/" + "长" * 90, b"x"),
                       lambda: d.rename("/b.txt", "b|c.txt")):
            with self.assertRaises(InvalidPath):  # Cloudreve's own refusals, which the site checks before
                change()
        self.assertEqual(self.names(d, "/"), ["a" * 255, "b.txt"])

    def test_move_and_copy_look_at_the_target(self):
        """Cloudreve lets a file and a folder take one name, answers a copy onto a taken name with "not found", and
        moves a folder below itself."""
        d = self.new_driver()
        self.put(d, "/a", b"a")
        d.mkdir("/dst/a")
        for change in (lambda: d.move("/a", "/dst"), lambda: d.copy("/a", "/dst")):
            with self.assertRaises(AlreadyExists):
                change()
        self.put(d, "/dst/b", b"other")
        self.put(d, "/b", b"b")
        with self.assertRaises(AlreadyExists):
            d.copy("/b", "/dst")
        d.mkdir("/m/n/o")
        for change in (lambda: d.move("/m", "/m/n/o"), lambda: d.copy("/m", "/m/n")):
            with self.assertRaises(DriverError):
                change()
        self.assertEqual((self.fake.routes("PATCH", "/object"), self.fake.routes("POST", "/object/copy")), ([], []))
        d.move("/b", "/m")
        [move] = self.fake.routes("PATCH", "/object")
        self.assertEqual(move["json"]["src_dir"], "/")
        self.assertEqual(move["json"]["dst"], "/m")
        self.assertEqual(len(move["json"]["src"]["items"]), 1)
        self.assertEqual(move["json"]["src"]["dirs"], [])
        self.assertEqual(self.names(d, "/m"), ["b", "n"])

    def test_delete_is_for_good(self):
        d = self.new_driver()
        self.put(d, "/d/a.bin", b"a")
        source = self.fake.file(self.email, "/d/a.bin").source
        d.delete("/d")
        self.assertIsNone(self.fake.folder(self.email, "/d"))
        self.assertNotIn(source, self.fake.blobs)  # no trash bin in Cloudreve v3
        [call] = self.fake.routes("DELETE", "/object")
        self.assertEqual((call["json"]["items"], len(call["json"]["dirs"])), ([], 1))
        self.assertEqual((call["json"]["force"], call["json"]["unlink"]), (False, False))

    def test_a_delete_cloudreve_does_in_part(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.fake.delete_fails = True  # the storage couldn't delete the file: Cloudreve keeps it, with 203
        with self.assertRaises(DriverError) as caught:
            d.delete("/a.txt")
        self.assertIs(type(caught.exception), DriverError)
        self.assertEqual(self.names(d, "/"), ["a.txt"])

    # -- uploads

    def test_chunks(self):
        self.fake.policy["chunk_size"] = 1000  # the storage policy's setting
        d = self.new_driver()
        data = os.urandom(3500)
        self.put(d, "/p.bin", data)
        [session] = self.fake.routes("PUT", "/file/upload")
        self.assertEqual(session["json"], {"path": "/", "size": 3500, "name": "p.bin",
                                           "policy_id": self.fake._policy_view()["id"]})
        self.assertEqual(self.chunks(), [("0", "1000"), ("1", "1000"), ("2", "1000"), ("3", "500")])
        self.assertEqual(self.fake.data(self.email, "/p.bin"), data)
        self.assertEqual(self.fake.routes("DELETE", "/file/upload/", prefix=True), [])
        self.fake.policy["chunk_size"] = 0  # no chunks: all of it at once
        self.fake.calls.clear()
        d.write("/q.bin", Stream(data), len(data))
        self.assertEqual(self.chunks(), [("0", "3500")])
        self.assertEqual(read(d, "/q.bin"), data)

    def test_a_replaced_file(self):
        """Cloudreve refuses an upload onto a file's name: the new one goes next to it, then takes its place."""
        self.fake.policy["chunk_size"] = 1000
        d = self.new_driver()
        self.put(d, "/f.bin", b"old")
        old = self.fake.file(self.email, "/f.bin")
        data = os.urandom(2500)
        self.fake.calls.clear()
        d.write("/f.bin", Stream(data), len(data))
        [session] = self.fake.routes("PUT", "/file/upload")
        spare = session["json"]["name"]
        self.assertTrue(spare.startswith(".hfile-upload-") and spare.endswith(".bin"), spare)
        [delete] = self.fake.routes("DELETE", "/object")
        self.assertEqual(len(delete["json"]["items"]), 1)
        [rename] = self.fake.routes("POST", "/object/rename")
        self.assertEqual(rename["json"]["new_name"], "f.bin")
        self.assertEqual(self.fake.names(self.email, "/"), ["f.bin"])
        self.assertEqual(read(d, "/f.bin"), data)
        self.assertNotIn(old.id, self.fake.files)

    def test_a_failed_upload_leaves_things_as_they_were(self):
        self.fake.policy["chunk_size"] = 1000
        d = self.new_driver()
        old = os.urandom(1500)
        self.put(d, "/f.bin", old)
        self.fake.fail_chunk = 2
        data = os.urandom(3500)
        for path in ("/f.bin", "/new.bin"):
            with self.assertRaises(DriverError):
                self.put(d, path, data)
        self.assertEqual(self.fake.data(self.email, "/f.bin"), old)  # the old file stays
        self.assertEqual(self.fake.names(self.email, "/"), ["f.bin"])  # the sessions went with their placeholders
        self.assertEqual((self.fake.uploads, len(self.fake.routes("DELETE", "/file/upload/", prefix=True))), ({}, 2))
        with self.assertRaises(DriverError):  # data shorter than its size
            d.write("/short.bin", io.BytesIO(b"abc"), 1500)
        self.assertEqual(self.fake.names(self.email, "/"), ["f.bin"])
        self.fake.fail_chunk = None
        self.put(d, "/f.bin", data)  # nothing stays in the way
        self.assertEqual(read(d, "/f.bin"), data)

    def test_uploads_through_cloudreve(self):
        """A storage policy whose uploads would go straight to its storage: an empty file, then its content."""
        self.fake.policy["type"] = "oss"
        d = self.new_driver()
        data = os.urandom(3500)
        self.put(d, "/o/x.bin", data)
        self.assertEqual([call["json"] for call in self.fake.routes("POST", "/file/create")], [{"path": "/o/x.bin"}])
        [update] = self.fake.routes("PUT", "/file/update/", prefix=True)
        self.assertEqual(update["length"], "3500")
        self.assertEqual(self.fake.routes("PUT", "/file/upload"), [])
        self.assertEqual(self.fake.data(self.email, "/o/x.bin"), data)
        self.put(d, "/o/x.bin", b"newer")  # a replacement goes next to it first too
        self.assertEqual(self.fake.names(self.email, "/o"), ["x.bin"])
        self.assertEqual(read(d, "/o/x.bin"), b"newer")
        self.fake.fail_update = True
        for path in ("/o/x.bin", "/o/y.bin"):
            with self.assertRaises(DriverError):
                self.put(d, path, data)
        self.assertEqual(self.fake.names(self.email, "/o"), ["x.bin"])  # the empty files went again
        self.assertEqual(self.fake.data(self.email, "/o/x.bin"), b"newer")

    def test_empty_files(self):
        d = self.new_driver()
        self.put(d, "/local", b"")
        self.assertEqual(self.chunks(), [("0", "0")])  # one empty chunk, as Cloudreve's pages send
        self.fake.policy["type"] = "cos"
        self.put(d, "/cos", b"")
        self.assertEqual(self.fake.routes("PUT", "/file/update/", prefix=True), [])  # the empty file is all
        for path in ("/local", "/cos"):
            with self.subTest(path=path):
                self.assertEqual(d.stat(path).size, 0)
                self.assertEqual(read(d, path), b"")
                self.assertEqual(httpx.get(d.download_url(path, filename="x")).content, b"")

    def test_limits(self):
        self.fake.policy["max_size"] = 100
        d = self.new_driver()
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", b"x" * 200)
        self.assertEqual(self.fake.routes("PUT", "/file/upload"), [])  # refused before anything was sent
        self.fake.policy["max_size"] = 0
        self.fake.capacity = 150
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", b"x" * 200)
        self.fake.capacity = None
        self.fake.policy["file_type"] = ["txt"]
        with self.assertRaises(DriverError):
            self.put(d, "/a.bin", b"a")
        self.assertEqual(self.names(d, "/"), [])
        self.put(d, "/a.txt", b"a")
        self.put(d, "/a.txt", b"b")  # the spare name keeps the extension the policy takes
        self.assertEqual(read(d, "/a.txt"), b"b")

    def test_a_web_server_limit(self):
        self.fake.policy["chunk_size"] = 4000
        self.fake.body_max = 3000  # nginx's client_max_body_size
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/a.bin", os.urandom(3500))
        self.assertIs(type(caught.exception), DriverError)
        self.assertEqual(self.fake.names(self.email, "/"), [])

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件 1.bin", data)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertTrue(url.startswith(self.fake.url + "/api/v3/file/download/"), url)  # Cloudreve gives no address
        response = httpx.get(url)
        self.assertEqual(response.content, data)
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        self.assertEqual(len(self.fake.routes("PUT", "/file/download/", prefix=True)), 3)  # a link each time
        self.assertEqual([request["range"] for request in self.fake.downloads[1:]], ["bytes=10-29", "bytes=5000-"])
        for path in ("/dl", "/dl/nope", "/"):
            with self.assertRaises(NotFound):
                d.download_url(path, filename="x")

    def test_links_that_work_once(self):
        self.fake.one_time = True  # the anonymous user group's "one-time download"
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.assertEqual(read(d, "/a.bin"), b"abc")
        url = d.download_url("/a.bin", filename="a.bin")
        self.assertEqual(httpx.get(url).content, b"abc")
        self.assertEqual(httpx.get(url).json()["code"], 404)

    def test_a_refused_link(self):
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        self.fake.refuse_signs = True
        with self.assertRaises(DriverError) as caught:
            read(d, "/a.bin")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("40020", caught.exception.message)
        self.fake.refuse_signs = False
        self.assertEqual(read(d, "/a.bin"), b"abc")

    def test_a_json_file(self):
        d = self.new_driver()
        data = json.dumps({"code": 40020, "msg": "not an answer"}).encode()
        self.put(d, "/a.json", data)
        self.assertEqual(read(d, "/a.json"), data)  # it comes as an attachment: the file

    def test_links_with_an_address(self):
        """The local policy's links come with an address when it has a CDN address; the other policies' are their
        storage's."""
        self.fake.policy["base_url"] = self.fake.url
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        url = d.download_url("/a.bin", filename="a.bin")
        self.assertTrue(url.startswith(self.fake.url + "/api/v3/file/download/"), url)
        self.assertEqual(httpx.get(url).content, b"abc")
        self.fake.policy.update(type="s3", base_url="")
        self.put(d, "/b.bin", b"def")
        url = d.download_url("/b.bin", filename="b.bin")
        self.assertTrue(url.startswith(self.fake.url + "/storage/"), url)
        self.assertEqual((httpx.get(url).content, read(d, "/b.bin", 1)), (b"def", b"ef"))
        self.assertEqual(read(d, "/a.bin"), b"abc")  # a file keeps the policy it was stored with

    # -- errors

    def test_error_codes(self):
        failure = CloudreveV3Driver._failure
        for code in (40016, 404, 40044):
            self.assertIsInstance(failure({"code": code, "msg": "Path not exist"}), NotFound)
        self.assertIsInstance(failure({"code": 40004, "msg": "Object existed"}), AlreadyExists)
        self.assertIsInstance(failure({"code": 40052, "msg": "Invalid object name"}), InvalidPath)
        self.assertIsInstance(failure({"code": 401, "msg": "Login required"}), AuthError)
        self.assertIsInstance(failure({"code": 50001, "msg": "Database operation failed."}), BackendError)
        for code in (203, 40002, 40006, 40007, 40049, 40050, 40051, 40054, -1):
            with self.subTest(code=code):
                self.assertIs(type(failure({"code": code, "msg": "x"})), DriverError)
        odd = failure({"code": 40099, "msg": "Something new"})
        self.assertIn("40099", odd.message)
        self.assertIn("Something new", odd.message)

    def test_failures_of_the_server(self):
        d = self.new_driver()
        d.list("/")
        self.fake.fail_status = 502
        with self.assertRaises(BackendError):
            d.list("/")
        self.fake.fail_status = 0
        with self.assertRaises(BackendError):  # not a Cloudreve site: the address answers a web page
            self.new_driver(url=self.fake.url + "/somewhere").list("/")

    # -- settings

    def test_settings(self):
        check = CloudreveV3Driver.validate_config
        self.assertEqual(check({"url": "https://cloud.example.com", "email": "a@example.com", "password": "p"}), {})
        self.assertIn("url", check({"url": "cloud.example.com"}))
        self.assertIn("url", check({"url": "ftp://cloud.example.com"}))
        self.assertIn("email", check({"url": "https://cloud.example.com", "email": "alice"}))
        self.assertIn("base_path", check({"url": "https://cloud.example.com", "base_path": "/a/../b"}))
        d = CloudreveV3Driver({"url": " https://cloud.example.com/drive/?x=1#y ", "email": "a@example.com",
                               "password": "p", "base_path": "/a b/c"})
        self.addCleanup(d.close)
        self.assertEqual(d._url("/directory/"), "https://cloud.example.com/drive/api/v3/directory/")
        self.assertEqual(d._dir("/x%"), "/a b/c/x%")
        self.assertEqual(d._dir("/"), "/a b/c")

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in CloudreveV3Driver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(CloudreveV3Driver), ["url", "email", "password"])
        self.assertEqual(CloudreveV3Driver.auth_params, ())


class CloudreveV4SiteTests(FakeMixin, unittest.TestCase):
    version = "4.19.1"

    def test_a_v4_site(self):
        """Cloudreve v4 answers every /api/v3 path with gin's 404: an error that points to the type Cloudreve."""
        self.email = "alice@example.com"
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            d.list("/")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("404", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
