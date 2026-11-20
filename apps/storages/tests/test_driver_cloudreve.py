"""Tests of the Cloudreve driver (apps.storages.drivers.cloudreve) against tests/fake_cloudreve.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_cloudreve
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
from apps.storages.drivers.cloudreve import CloudreveDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloudreve import FakeCloudreve


def driver_class(**attrs):
    """CloudreveDriver with other tuning (the type stays "cloudreve")."""
    return type("FakeCloudreveDriver", (CloudreveDriver,), attrs)


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
    version = 4

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeCloudreve(cls.version).start()
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


class CloudreveContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root of a new account, with the local storage policy."""

    def make_driver(self):
        self.fake.reset()
        self.new_user()
        return self.new_driver()


class CloudreveChunksContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again in a folder of the account, with small upload chunks and small listing pages, paged by offset."""

    driver_attrs = {"page_size": 5}

    def make_driver(self):
        self.fake.reset()
        self.fake.policy["chunk_size"] = 64 * 1024
        self.fake.cursor = False
        self.new_user()
        self.fake.mkdirs(self.email, "/base/here")
        self.fake.put(self.email, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here")


class CloudreveObjectStorageContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again with a storage policy whose chunks would go to S3 itself: the files go to PUT /file/content."""

    def make_driver(self):
        self.fake.reset()
        self.fake.policy.update(type="s3", relay=False)
        self.new_user()
        return self.new_driver()


def queries(calls) -> list[dict[str, list[str]]]:
    return [urllib.parse.parse_qs(call["query"]) for call in calls]


class CloudreveTests(FakeMixin, unittest.TestCase):
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
        return [(call["path"].rsplit("/", 1)[1], call["length"]) for call in self.fake.calls
                if call["method"] == "POST" and call["path"].startswith("/api/v4/file/upload/")]

    # -- logins and tokens

    def test_an_expired_access_token_is_refreshed(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        logins, refreshes = self.fake.logins, self.fake.refreshes
        self.fake.expire_access_tokens()  # Cloudreve takes them for no login: 401
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual((self.fake.logins, self.fake.refreshes), (logins, refreshes + 1))
        self.fake.expire_access_tokens()
        d.delete("/a.txt")  # a batch call: its 401 is in aggregated_error
        self.assertEqual(self.fake.trashed(self.email), ["a.txt"])
        self.assertEqual((self.fake.logins, self.fake.refreshes), (logins, refreshes + 2))
        self.fake.expire_access_tokens()
        self.fake.expire_refresh_tokens()  # the refresh is refused too: a new login
        self.assertEqual(self.names(d, "/"), [])
        self.assertEqual((self.fake.logins, self.fake.refreshes), (logins + 1, refreshes + 3))

    def test_the_access_tokens_lifetime(self):
        self.fake.access_ttl = 1234
        d = self.new_driver()
        token, lifetime = d._renew()  # a login
        self.assertTrue(token.startswith("acc-"))
        # Measured from the answer's Date header, which has whole seconds: up to a second longer than by our clock.
        self.assertTrue(1200 < lifetime < 1235.001, lifetime)
        again, _ = d._renew()  # then from the refresh token
        self.assertNotEqual(token, again)
        self.assertEqual((self.fake.logins, self.fake.refreshes), (1, 1))

    def test_a_refresh_token_past_its_end_isnt_sent(self):
        self.fake.refresh_ttl = -10
        d = self.new_driver()
        d.list("/")
        self.fake.expire_access_tokens()
        d.list("/")
        self.assertEqual((self.fake.logins, self.fake.refreshes), (2, 0))

    def test_token_lifetimes_by_cloudreves_clock(self):
        """Cloudreve gives the tokens' ends by its own clock, which may be off from ours: they are measured against
        its answer's Date header."""
        self.fake.clock = -7200.0  # two hours behind
        d = self.new_driver()
        for _ in range(3):
            d.list("/")
        self.assertEqual((self.fake.logins, self.fake.refreshes), (1, 0))
        self.fake.clock = 7200.0  # two hours ahead: the token ends after its hour all the same
        ahead = self.new_driver()
        _token, lifetime = ahead._renew()
        self.assertLess(lifetime, 3602)

    def test_a_changed_password(self):
        d = self.new_driver()
        d.list("/")
        self.fake.change_password(self.email, "a new password")
        d.list("/")  # the access token lasts until it expires
        self.fake.expire_access_tokens()
        with self.assertRaises(AuthError):  # its refresh token is refused, and so is the old password
            d.list("/")
        logins = self.fake.logins
        with self.assertRaises(AuthError):  # remembered for a while: Cloudreve isn't asked again
            d.list("/")
        self.assertEqual(self.fake.logins, logins)

    def test_refused_logins(self):
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong").list("/")
        self.fake.login_captcha = True
        with self.assertRaises(AuthError):
            self.new_driver().list("/")
        self.fake.login_captcha = False
        for kwargs in ({"two_factor": True}, {"status": "manual_banned"}, {"status": "inactive"}):
            with self.subTest(**kwargs):
                self.new_user(**kwargs)
                with self.assertRaises(AuthError):
                    self.new_driver().list("/")
        with self.assertRaises(AuthError) as caught:  # Cloudreve wants an email address
            self.new_driver(email="alice").list("/")
        self.assertIn("40001", caught.exception.message)

    def test_the_token_stays_out_of_urls_and_cookies_out_of_requests(self):
        self.fake.policy["chunk_size"] = 1000
        d = self.new_driver()
        data = os.urandom(2500)
        self.put(d, "/a/one.bin", data)
        d.write("/a/two.bin", Stream(data), len(data))
        d.mkdir("/b")
        d.copy("/a/one.bin", "/b")
        d.move("/a/two.bin", "/b")
        d.rename("/b/two.bin", "three.bin")
        self.assertEqual(read(d, "/b/one.bin", 5, 10), data[5:15])
        url = d.download_url("/b/three.bin", filename="three.bin")
        d.delete("/a")
        self.fake.expire_access_tokens()
        d.list("/")  # after a refresh too
        secrets = set(self.fake.access) | set(self.fake.refresh) | {"pw " + self.email}
        for call in self.fake.calls:
            for secret in secrets:
                self.assertNotIn(secret, urllib.parse.unquote(call["query"]))
            self.assertIsNone(call["cookie"])  # every answer sets Cloudreve's session cookie
            if call["path"] in ("/api/v4/session/token", "/api/v4/session/token/refresh"):
                self.assertIsNone(call["auth"])
            else:
                self.assertTrue(call["auth"].startswith("Bearer acc-"), call)
        self.assertTrue(self.fake.content_requests)
        for request in self.fake.content_requests:  # the links are their own permission
            self.assertEqual((request["auth"], request["cookie"]), (None, None))
        for secret in secrets:
            self.assertNotIn(secret, url)
        login = self.fake.routes("POST", "/session/token")[0]
        self.assertEqual(login["json"], {"email": self.email, "password": "pw " + self.email})

    # -- listings and paths

    def test_pages_of_a_listing(self):
        for n in range(230):
            self.fake.put(self.email, f"/big/f{n:03}.txt", b"x")
        d = self.new_driver(driver_class=driver_class(page_size=100))
        self.assertEqual(self.names(d, "/big"), [f"f{n:03}.txt" for n in range(230)])
        listed = queries(self.fake.routes("GET", "/file"))
        self.assertEqual([("next_page_token" in query, query["page_size"]) for query in listed],
                         [(False, ["100"]), (True, ["100"]), (True, ["100"])])
        self.fake.max_page_size = 40  # Cloudreve's pages may be smaller than asked
        self.assertEqual(len(d.list("/big")), 230)
        self.fake.cursor = False  # paged by offset
        self.fake.calls.clear()
        self.assertEqual(self.names(d, "/big"), [f"f{n:03}.txt" for n in range(230)])
        self.assertEqual([query["page"] for query in queries(self.fake.routes("GET", "/file"))],
                         [[str(n)] for n in range(6)])

    def test_the_base_path(self):
        self.fake.put(self.email, "/in/a.txt", b"a")
        self.assertEqual(self.names(self.new_driver("/in"), "/"), ["a.txt"])
        missing = self.new_driver("/no/such")
        for call in (lambda: missing.list("/"), lambda: missing.stat("/")):
            with self.assertRaises(NotFound) as caught:
                call()
            self.assertIn("/no/such", caught.exception.message)
        with self.assertRaises(NotADirectory):
            self.new_driver("/in/a.txt").list("/")

    def test_the_account_root(self):
        """Cloudreve's /file/info refuses the account's root (WithNotRoot): its listing tells that it is there. The site
        asks stat() of a folder before it makes something in it (apps/files/service.py _folder, apps/uploads
        missing_folders), the root too."""
        d = self.new_driver()
        root = d.stat("/")
        self.assertEqual((root.path, root.is_dir), ("/", True))
        d.mkdir("/x")
        self.assertTrue(d.stat("/").is_dir)
        self.put(d, "/a.txt", b"a")
        self.assertEqual(self.names(d, "/"), ["a.txt", "x"])
        self.assertNotIn(["cloudreve://my"], [query.get("uri") for query in queries(self.fake.routes("GET", "/file/info"))])

    def test_names_are_encoded_as_cloudreve_does(self):
        """The fake refuses a URI with anything Cloudreve's PathEscape would have escaped."""
        d = self.new_driver()
        names = ["100%.txt", "#hash", "a+b=c&d.txt", "semi;colon,comma", "[x](y)", "a b", "it's (1)!", "~tilde", "中文 名字"]
        for name in names:
            with self.subTest(name=name):
                self.put(d, "/dir/" + name, name.encode())
                self.assertEqual(read(d, "/dir/" + name), name.encode())
        self.assertEqual(self.names(d, "/dir"), sorted(names))
        uris = [call["json"]["uri"] for call in self.fake.routes("PUT", "/file/upload")]
        self.assertIn("cloudreve://my/dir/100%25.txt", uris)
        self.assertIn("cloudreve://my/dir/it's%20(1)!", uris)
        self.assertIn("cloudreve://my/dir/%E4%B8%AD%E6%96%87%20%E5%90%8D%E5%AD%97", uris)

    def test_names_cloudreve_refuses(self):
        self.assertEqual(CloudreveDriver.forbidden_chars, '\\:*?"<>|')
        self.assertEqual(CloudreveDriver.max_name_bytes, 255)
        d = self.new_driver()
        d.mkdir("/" + "a" * 255)
        for name in ("a" * 256, "a:b", "长" * 90):  # Cloudreve's own refusals, which the site checks before
            with self.subTest(name=name):
                with self.assertRaises(InvalidPath):
                    d.mkdir("/" + name)
        self.assertEqual(self.names(d, "/"), ["a" * 255])

    # -- changes

    def test_delete_puts_into_the_trash_bin(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        d.delete("/a.txt")
        self.assertEqual(self.fake.trashed(self.email), ["a.txt"])
        [call] = self.fake.routes("DELETE", "/file")
        self.assertEqual(call["json"], {"uris": ["cloudreve://my/a.txt"], "unlink": False, "skip_soft_delete": False})
        self.put(d, "/a.txt", b"again")  # its name is free again
        self.assertEqual(read(d, "/a.txt"), b"again")

    def test_a_locked_file(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.fake.locked.add("/a.txt")  # open in Cloudreve's online editor
        for change in (lambda: d.rename("/a.txt", "b.txt"), lambda: d.delete("/a.txt"), lambda: self.put(d, "/a.txt", b"b")):
            with self.assertRaises(DriverError) as caught:
                change()
            self.assertIs(type(caught.exception), DriverError)
        self.assertEqual(read(d, "/a.txt"), b"a")

    def test_mkdir_asks_for_an_error_on_conflict(self):
        d = self.new_driver()
        d.mkdir("/x/y")
        self.assertEqual([call["json"] for call in self.fake.routes("POST", "/file/create")],
                         [{"uri": "cloudreve://my/x/y", "type": "folder", "err_on_conflict": True}])
        with self.assertRaises(AlreadyExists):
            d.mkdir("/x/y")

    # -- uploads

    def test_chunks(self):
        self.fake.policy["chunk_size"] = 1000  # the storage policy's setting
        d = self.new_driver()
        data = os.urandom(3500)
        self.put(d, "/p.bin", data)
        [session] = self.fake.routes("PUT", "/file/upload")
        self.assertEqual(session["json"], {"uri": "cloudreve://my/p.bin", "size": 3500, "entity_type": "version"})
        self.assertEqual(self.chunks(), [("0", "1000"), ("1", "1000"), ("2", "1000"), ("3", "500")])
        self.assertEqual(self.fake.data(self.email, "/p.bin"), data)
        d.write("/p.bin", Stream(data[::-1]), len(data))  # data that can't seek, replacing the file
        self.assertEqual(read(d, "/p.bin"), data[::-1])
        self.assertEqual(self.names(d, "/"), ["p.bin"])
        self.assertEqual(self.fake.routes("DELETE", "/file/upload"), [])

    def test_a_relayed_upload(self):
        self.fake.policy.update(type="s3", relay=True, chunk_size=1000)  # Upload relay: all of it to Cloudreve
        d = self.new_driver()
        data = os.urandom(3500)
        self.put(d, "/r.bin", data)
        self.assertEqual(self.chunks(), [("0", "3500")])
        self.assertEqual(self.fake.data(self.email, "/r.bin"), data)
        self.assertEqual(self.fake.routes("PUT", "/file/content"), [])
        self.fake.body_max = 3000  # the web server's limit
        with self.assertRaises(DriverError):
            self.put(d, "/s.bin", data)
        self.assertIsNone(self.fake.find(self.email, "/s.bin"))  # the session and its placeholder went

    def test_an_upload_for_object_storage(self):
        self.fake.policy.update(type="oss", relay=False)  # the chunks would go to OSS itself
        d = self.new_driver()
        data = os.urandom(3500)
        self.put(d, "/o/x.bin", data)
        self.assertEqual(len(self.fake.routes("DELETE", "/file/upload")), 1)  # the session given up
        [content] = self.fake.routes("PUT", "/file/content")
        self.assertEqual((urllib.parse.parse_qs(content["query"])["uri"], content["length"]), (["cloudreve://my/o/x.bin"], "3500"))
        self.assertEqual(self.chunks(), [])
        self.assertEqual(self.fake.data(self.email, "/o/x.bin"), data)
        self.fake.max_edit_size = 1000  # Cloudreve takes larger files in itself only with Upload relay
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/o/big.bin", data)
        self.assertIn("oss", caught.exception.message)
        self.assertEqual(self.fake.names(self.email, "/o"), ["x.bin"])  # no placeholder left
        self.assertEqual(self.fake.uploading, {})

    def test_a_failed_upload_ends_its_session(self):
        self.fake.policy["chunk_size"] = 1000
        d = self.new_driver()
        old = os.urandom(1500)
        self.put(d, "/f.bin", old)
        self.fake.fail_chunk = 2
        data = os.urandom(3500)
        for path in ("/f.bin", "/new.bin"):
            with self.assertRaises(BackendError):
                self.put(d, path, data)
        self.assertEqual(self.fake.data(self.email, "/f.bin"), old)  # the old version stays
        self.assertIsNone(self.fake.find(self.email, "/new.bin"))  # the placeholder went with the session
        self.assertEqual((self.fake.uploading, len(self.fake.routes("DELETE", "/file/upload"))), ({}, 2))
        with self.assertRaises(DriverError):  # data shorter than its size
            d.write("/short.bin", io.BytesIO(b"abc"), 1500)
        self.assertIsNone(self.fake.find(self.email, "/short.bin"))
        self.fake.fail_chunk = None
        self.put(d, "/f.bin", data)  # nothing stays locked
        self.assertEqual(read(d, "/f.bin"), data)

    def test_limits(self):
        self.fake.policy["max_size"] = 100
        d = self.new_driver()
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", b"x" * 200)
        self.assertEqual(self.chunks(), [])  # refused before any data went
        self.fake.policy["max_size"] = 0
        self.fake.capacity = 150
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", b"x" * 200)
        self.assertEqual(self.names(d, "/"), [])

    def test_files_being_uploaded(self):
        """A new file's placeholder has its size before its data, and no version to read: it is left out until its
        upload ends. A file being replaced keeps its last version until then, and stays."""
        d = self.new_driver()
        self.fake.put(self.email, "/old.bin", b"old")
        self.fake.begin_upload(self.email, "/new.bin", 3500)  # another client's uploads
        self.fake.begin_upload(self.email, "/old.bin", 10)
        self.assertEqual(self.names(d, "/"), ["old.bin"])
        for call in (lambda: d.stat("/new.bin"), lambda: read(d, "/new.bin"),
                     lambda: d.download_url("/new.bin", filename="new.bin")):
            with self.assertRaises(NotFound):
                call()
        self.assertEqual((d.stat("/old.bin").size, read(d, "/old.bin")), (3, b"old"))
        self.put(d, "/mine.bin", b"m")  # the driver's own uploads don't look for their placeholders
        self.assertEqual(self.names(d, "/"), ["mine.bin", "old.bin"])

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件 1.bin", data)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertEqual(httpx.get(url).content, data)
        self.assertEqual(d.download_url("/dl/文件 1.bin", filename="文件 1.bin"), url)
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        [link] = self.fake.routes("POST", "/file/url")  # one link, reused
        self.assertEqual(link["json"], {"uris": ["cloudreve://my/dl/%E6%96%87%E4%BB%B6%201.bin"], "download": True})
        self.assertEqual([request["range"] for request in self.fake.content_requests[1:]], ["bytes=10-29", "bytes=5000-"])
        self.put(d, "/dl/文件 1.bin", b"newer")  # another version: another link
        self.assertNotEqual(d.download_url("/dl/文件 1.bin", filename="文件 1.bin"), url)
        self.assertEqual(read(d, "/dl/文件 1.bin"), b"newer")
        with self.assertRaises(NotFound):
            d.download_url("/dl", filename="dl")
        with self.assertRaises(NotFound):
            d.download_url("/dl/nope", filename="nope")

    def test_short_lived_links_arent_reused(self):
        self.fake.url_ttl = 30  # Cloudreve's "File Blob URL TTL"
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        first = d.download_url("/a.bin", filename="a.bin")
        self.assertNotEqual(d.download_url("/a.bin", filename="a.bin"), first)
        self.assertEqual(len(self.fake.routes("POST", "/file/url")), 2)

    def test_how_long_a_link_is_handed_out_again(self):
        """As long as this driver reuses it: a minute before it expires, link_ttl at most."""
        self.put(self.new_driver(), "/a.bin", b"abc")
        for ttl, reused in ((3600, 300), (200, 140), (30, 0)):
            with self.subTest(ttl=ttl):
                self.fake.url_ttl = ttl
                self.fake.url_cache.clear()
                d = self.new_driver()
                url, lasts = d.timed_download_url("/a.bin", filename="a.bin")
                self.assertTrue(url.startswith(self.fake.url))
                self.assertAlmostEqual(lasts, reused, delta=2)
                again = d.timed_download_url("/a.bin", filename="a.bin")
                self.assertEqual(again[0] == url, reused > 0)
                self.assertLess(again[1], lasts + 0.001)  # counting down; Windows' clock may not move in between
        self.put(self.new_driver(), "/empty", b"")
        self.assertEqual(self.new_driver().timed_download_url("/empty", filename="empty"), (None, None))

    def test_empty_files(self):
        d = self.new_driver()
        self.put(d, "/uploaded", b"")
        self.fake.put(self.email, "/made", None)  # made in Cloudreve's own pages: no blob to link to
        for path in ("/uploaded", "/made"):
            with self.subTest(path=path):
                self.assertEqual(d.stat(path).size, 0)
                self.assertEqual(read(d, path), b"")
                self.assertIsNone(d.download_url(path, filename="x"))
        self.assertEqual(self.fake.routes("POST", "/file/url"), [])

    def test_a_refused_link(self):
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.fake.refuse_signs = True  # Cloudreve's secret changed: its own content route answers JSON
        with self.assertRaises(DriverError) as caught:
            read(d, "/a.bin")
        self.assertIn("40020", caught.exception.message)
        self.fake.refuse_signs = False
        self.fake.url_cache.clear()
        self.assertEqual(read(d, "/a.bin"), b"abc")  # the refused link wasn't kept
        self.assertEqual(len(self.fake.routes("POST", "/file/url")), 2)

    def test_a_json_file(self):
        d = self.new_driver()
        data = json.dumps({"code": 40020, "msg": "not an answer"}).encode()
        self.put(d, "/a.json", data)
        self.assertEqual(read(d, "/a.json"), data)  # it comes as an attachment: the file

    def test_links_made_for_another_address(self):
        """Cloudreve makes its links with its Site URL, which the site's server may not reach (an internal address in
        docker): open() fetches them at the configured address, as the signature covers the path only; download_url()
        gives them as they are, for the browser."""
        self.fake.site_url = "http://127.0.0.1:9"  # nothing answers there
        d = self.new_driver()
        self.put(d, "/a.bin", b"abc")
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.assertEqual(read(d, "/a.bin", 1, 1), b"b")
        self.assertTrue(d.download_url("/a.bin", filename="a.bin").startswith("http://127.0.0.1:9/api/v4/file/content/"))
        self.assertEqual([request["range"] for request in self.fake.content_requests], [None, "bytes=1-1"])

    # -- errors

    def test_error_codes(self):
        failure = CloudreveDriver._failure
        self.assertIsInstance(failure({"code": 40016, "msg": "Path not exist"}), NotFound)
        self.assertIsInstance(failure({"code": 40077, "msg": "Entity not exist"}), NotFound)
        self.assertIsInstance(failure({"code": 40004, "msg": "Object existed"}), AlreadyExists)
        self.assertIsInstance(failure({"code": 40052, "msg": "Invalid object name"}), InvalidPath)
        self.assertIsInstance(failure({"code": 401, "msg": "Login required"}), AuthError)
        self.assertIsInstance(failure({"code": 50001, "msg": "Database operation failed."}), BackendError)
        for code in (403, 40051, 40049, 40073, 40082):
            with self.subTest(code=code):
                self.assertIs(type(failure({"code": code, "msg": "x"})), DriverError)
        batch = {"code": 40081, "msg": "failed to get target file",
                 "aggregated_error": {"cloudreve://my/a": {"code": 40016, "msg": "Path not exist"}}}
        self.assertIsInstance(failure(batch), NotFound)
        odd = failure({"code": 40099, "msg": "Something new"})
        self.assertIn("40099", odd.message)
        self.assertIn("Something new", odd.message)
        unexplained = failure({"code": -1, "msg": "", "correlation_id": "corr-1"})
        self.assertIsInstance(unexplained, BackendError)
        self.assertIn("corr-1", unexplained.message)
        inside = failure({"code": 40081, "msg": "", "aggregated_error": {"cloudreve://my/a": {"code": -1, "msg": "",
                                                                                              "correlation_id": "corr-2"}}})
        self.assertIsInstance(inside, BackendError)
        self.assertIn("corr-2", inside.message)

    def test_a_failure_without_an_error_code(self):
        """Cloudreve answers a failure that isn't one of its own errors (a database's, a storage's) with code -1 and no
        message: its log has the reason under the answer's correlation ID."""
        d = self.new_driver()
        d.list("/")
        self.fake.unexplained = {"/api/v4/file"}
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn(self.fake.last_correlation, caught.exception.message)

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
        check = CloudreveDriver.validate_config
        self.assertEqual(check({"url": "https://cloud.example.com", "email": "a@example.com", "password": "p"}), {})
        self.assertIn("url", check({"url": "cloud.example.com"}))
        self.assertIn("url", check({"url": "ftp://cloud.example.com"}))
        self.assertIn("email", check({"url": "https://cloud.example.com", "email": "alice"}))
        self.assertIn("base_path", check({"url": "https://cloud.example.com", "base_path": "/a/../b"}))
        d = CloudreveDriver({"url": " https://cloud.example.com/drive/?x=1#y ", "email": "a@example.com", "password": "p",
                             "base_path": "/a b/c"})
        self.addCleanup(d.close)
        self.assertEqual(d._url("/file"), "https://cloud.example.com/drive/api/v4/file")
        self.assertEqual(d._uri("/x%"), "cloudreve://my/a%20b/c/x%25")
        self.assertEqual(d._uri("/"), "cloudreve://my/a%20b/c")

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in CloudreveDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(CloudreveDriver), ["url", "email", "password"])
        self.assertEqual(CloudreveDriver.auth_params, ())


class CloudreveV3Tests(FakeMixin, unittest.TestCase):
    version = 3

    def test_a_v3_site(self):
        """Cloudreve v3 answers every /api/v4 path with gin's 404: an error that says this type needs v4."""
        self.email = "alice@example.com"
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            d.list("/")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("404", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
