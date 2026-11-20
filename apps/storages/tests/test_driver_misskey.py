"""Tests of the Misskey driver (apps.storages.drivers.misskey) against tests/fake_misskey.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_misskey
"""

from __future__ import annotations

import io
import os
import unittest
from unittest import mock

import httpx

from apps.storages import config
from apps.storages.drivers import http
from apps.storages.drivers.base import AuthError, BackendError, DriverError, InvalidPath, NotFound, Unsupported
from apps.storages.drivers.misskey import MisskeyDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_misskey import MIB, MISSKEY_DEFAULTS, FakeMisskey

PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(64))  # a type Misskey finds in the content


def driver_class(**attrs):
    """MisskeyDriver with other tuning (the type stays "misskey")."""
    return type("FakeMisskeyDriver", (MisskeyDriver,), attrs)


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
        cls.fake = FakeMisskey().start()
        cls.Driver = driver_class(**cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_account(self, **kwargs) -> None:
        self.user, self.token = self.fake.add_user(**kwargs)

    def new_driver(self, base: str = "/", driver_class=None, **settings):
        settings = {"url": self.fake.url, "access_token": self.token, "base_path": base, **settings}
        driver = (driver_class or self.Driver)({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class MisskeyContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root of a new account's drive (which has no id in Misskey)."""

    def make_driver(self):
        self.fake.reset()
        self.new_account()
        return self.new_driver()


class MisskeyPagesContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again in a folder of the drive, with listing pages of 3, and the server keeping its files in object storage."""

    driver_attrs = {"page_size": 3}

    def make_driver(self):
        self.fake.reset()
        self.fake.object_storage = True
        self.new_account()
        self.fake.mkdirs(self.user, "/base/here")
        self.fake.put(self.user, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here")


class MisskeyTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.new_account()

    def put(self, driver, path: str, data: bytes) -> None:
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    def downloads(self) -> list[dict]:
        return [call for call in self.fake.calls if not call["path"].startswith("/api/")]

    def answer(self, endpoint: str) -> str | None:
        """The error code of the last call of the endpoint."""
        return self.fake.requests(endpoint)[-1]["answer"]

    # -- the token

    def test_the_token_goes_in_its_header_only(self):
        d = self.new_driver()
        data = os.urandom(MIB + 100)
        self.put(d, "/a/one.bin", data)
        d.write("/a/two", Stream(data), len(data))  # a name Misskey changes, so renamed too
        d.mkdir("/b")
        d.move("/a/one.bin", "/b")
        d.rename("/b/one.bin", "three.bin")
        self.assertEqual(read(d, "/b/three.bin", 5, 10), data[5:15])
        d.delete("/a")
        calls = self.fake.requests(".*")
        self.assertGreater(len(calls), 10)
        for call in calls:
            self.assertEqual(call["authorization"], f"Bearer {self.token}")
            self.assertNotIn(self.token, call["target"])
            self.assertNotIn("i", call["json"] if call["json"] is not None else call["fields"])
            self.assertIsNone(call["cookie"])
        self.assertTrue(self.downloads())
        for call in self.downloads():  # the file's public address
            self.assertIsNone(call["authorization"])

    def test_a_refused_token(self):
        d = self.new_driver()
        d.list("/")
        self.fake.revoke(self.token)  # deleted under Manage access tokens
        with self.assertRaises(AuthError):
            d.list("/")
        self.assertEqual(self.answer("drive/folders"), "AUTHENTICATION_FAILED")
        with self.assertRaises(AuthError):
            self.new_driver(access_token="x" * 32).check()
        with self.assertRaises(AuthError):  # none at all
            self.new_driver(access_token="").check()
        self.assertEqual(self.answer("drive/folders"), "CREDENTIAL_REQUIRED")
        self.fake.old_auth = True  # 403, as misskey.io's /api.json says
        with self.assertRaises(AuthError):
            self.new_driver(access_token="x" * 32).check()

    def test_a_token_that_may_only_read(self):
        self.fake.put(self.user, "/a.txt", b"a")
        self.fake.mkdirs(self.user, "/dst")
        d = self.new_driver(access_token=self.fake.add_token(self.user, ("read:drive",)))
        self.assertEqual(self.names(d, "/"), ["a.txt", "dst"])
        self.assertEqual(read(d, "/a.txt"), b"a")
        for change in (lambda: d.mkdir("/f"), lambda: self.put(d, "/b.txt", b"b"), lambda: d.rename("/a.txt", "c.txt"),
                       lambda: d.move("/a.txt", "/dst"), lambda: d.delete("/a.txt")):
            with self.assertRaises(AuthError) as caught:
                change()
            self.assertIn("write:drive", caught.exception.message)
        self.assertEqual(self.fake.names(self.user, "/"), ["a.txt", "dst"])
        d = self.new_driver(access_token=self.fake.add_token(self.user, ("read:account",)))
        with self.assertRaises(AuthError) as caught:
            d.list("/")
        self.assertIn("read:drive", caught.exception.message)

    # -- listings

    def test_pages_of_a_listing(self):
        for n in range(7):
            self.fake.mkdirs(self.user, f"/big/d{n}")
        for n in range(23):
            self.fake.put(self.user, f"/big/f{n:02}.txt", b"x")
        folder = self.fake.find(self.user, "/big")
        d = self.new_driver(driver_class=driver_class(page_size=5))
        expected = [f"d{n}" for n in range(7)] + [f"f{n:02}.txt" for n in range(23)]
        self.assertEqual(self.names(d, "/big"), expected)
        folders = [item.id for item in self.fake.folders.values() if item.parent == folder.id]
        files = [item.id for item in self.fake.files.values() if item.folder == folder.id]
        for endpoint, ids in (("drive/folders", folders), ("drive/files", files)):
            ids = sorted(ids, reverse=True)
            bodies = [call["json"] for call in self.fake.requests(endpoint) if call["json"].get("folderId") == folder.id]
            # Newest first, each page below the last id of the one before; a short page is the last.
            self.assertEqual(bodies, [{"limit": 5, "folderId": folder.id, **({"untilId": ids[n * 5 - 1]} if n else {})}
                                      for n in range(len(ids) // 5 + 1)])
        for n in range(23, 25):  # 25 files: the fifth page is full, the one after it empty
            self.fake.put(self.user, f"/big/f{n:02}.txt", b"x")
        self.assertEqual(len(d.list("/big")), 32)
        self.assertEqual(len([call for call in self.fake.requests("drive/files") if call["json"].get("folderId") == folder.id]), 5 + 6)

    def test_the_root_has_no_id(self):
        a = self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual([call["json"] for call in self.fake.requests("drive/(folders|files)")], [{"limit": 100}] * 2)
        d.mkdir("/f")
        d.mkdir("/f/g")
        self.put(d, "/r.txt", b"r")
        d.move("/a.txt", "/f")
        d.move("/f/a.txt", "/")
        d.move("/f/g", "/")
        f, g = self.fake.find(self.user, "/f"), self.fake.find(self.user, "/g")
        self.assertEqual([call["json"] for call in self.fake.requests("drive/folders/create")],
                         [{"name": "f"}, {"name": "g", "parentId": f.id}])
        self.assertEqual(self.fake.requests("drive/files/create")[0]["parts"], ["name", "force", "file"])
        self.assertEqual([call["json"] for call in self.fake.requests("drive/files/update")],
                         [{"fileId": a.id, "folderId": f.id}, {"fileId": a.id, "folderId": None}])
        self.assertEqual([call["json"] for call in self.fake.requests("drive/folders/update")], [{"folderId": g.id, "parentId": None}])
        self.assertEqual(self.names(d, "/"), ["a.txt", "f", "g", "r.txt"])
        self.assertEqual(self.fake.names(self.user, "/"), ["a.txt", "f", "g", "r.txt"])

    def test_a_folder_deleted_in_misskey(self):
        file = self.fake.put(self.user, "/a/b/c.txt", b"c")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/a/b"), ["c.txt"])
        b = self.fake.find(self.user, "/a/b")
        del self.fake.files[file.id], self.fake.folders[b.id]  # in Misskey's own UI
        with self.assertRaises(NotFound):  # it lists nothing, and drive/folders/show doesn't know it
            d.list("/a/b")
        self.assertEqual(self.fake.requests("drive/folders/show")[0]["json"], {"folderId": b.id})

    def test_an_empty_folder(self):
        d = self.new_driver()
        d.mkdir("/e")
        self.assertEqual(d.list("/e"), [])
        self.assertEqual(len(self.fake.requests("drive/folders/show")), 1)  # an empty listing: is it there?
        self.put(d, "/e/x.txt", b"x")
        self.assertEqual(self.names(d, "/e"), ["x.txt"])
        self.assertEqual(len(self.fake.requests("drive/folders/show")), 1)  # one with something in it is

    def test_names(self):
        """Misskey keeps several entries of one name and tells case apart; it refuses ".." in file names, and its
        database keeps 128 characters of a folder's name."""
        self.fake.put(self.user, "/dup.txt", b"old")
        self.fake.put(self.user, "/dup.txt", b"newer")
        self.fake._add_folder(self.user, None, "a/b")  # Misskey takes "/" in a folder's name
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["dup.txt"])  # one of each name, the newest; none with "/"
        self.assertEqual(read(d, "/dup.txt"), b"newer")
        self.put(d, "/Case.txt", b"1")
        self.put(d, "/case.txt", b"2")
        self.assertEqual(self.names(d, "/"), ["Case.txt", "case.txt", "dup.txt"])
        with self.assertRaises(InvalidPath):
            self.put(d, "/new/a..b.txt", b"x")
        with self.assertRaises(InvalidPath):
            d.rename("/Case.txt", "x..y")
        self.assertIsNone(self.fake.find(self.user, "/new"))  # refused before anything was made
        self.assertEqual(len(self.fake.requests("drive/files/(create|update)")), 2)
        d.mkdir("/" + "f" * 128)
        with self.assertRaises(InvalidPath):
            d.mkdir("/" + "f" * 129)
        with self.assertRaises(InvalidPath):
            d.rename("/" + "f" * 128, "g" * 129)
        d.rename("/dup.txt", "x.y")  # of the two of that name, the one listed
        self.assertEqual(self.fake.names(self.user, "/"), sorted(["Case.txt", "a/b", "case.txt", "dup.txt", "f" * 128, "x.y"]))
        self.assertEqual(MisskeyDriver.max_name_bytes, 200)
        for name in ("a" * 196 + ".txt", "长" * 66 + ".t", "😀" * 50):  # 200 bytes: at most 200 characters for Misskey
            self.put(d, "/" + name, b"l")
            self.assertIn(name, self.names(d, "/"))

    def test_names_misskey_changes(self):
        """Misskey trims an upload's name and gives it the extension of the type it finds in the content: the file is
        renamed to the name asked for."""
        d = self.new_driver()
        cases = {"README": b"text", "photo.txt": PNG, " spaced ": b"s", "blob": b"b", "empty": b"", "a.TXT": b"t", "p.png": PNG}
        for name, data in cases.items():
            self.put(d, "/" + name, data)
        self.assertEqual(self.names(d, "/"), sorted(cases))
        self.assertEqual(self.fake.names(self.user, "/"), sorted(cases))
        renamed = sorted(call["json"]["name"] for call in self.fake.requests("drive/files/update"))
        self.assertEqual(renamed, sorted(["README", "photo.txt", " spaced ", "blob", "empty"]))  # the others kept theirs
        for name, data in cases.items():
            self.assertEqual(read(d, "/" + name), data)

    # -- uploads

    def test_the_upload_request(self):
        d = self.new_driver()
        data = os.urandom(MIB + 7)
        source = io.BytesIO(b"xx" + data)
        source.seek(2)
        d.write("/up/a.bin", source, len(data))
        d.write("/b.bin", Stream(data), len(data))  # data that can't seek: streamed all the same
        first, second = self.fake.requests("drive/files/create")
        folder = self.fake.find(self.user, "/up")
        self.assertEqual(first["parts"], ["folderId", "name", "force", "file"])  # the fields before the file
        self.assertEqual(first["fields"], {"folderId": folder.id, "name": "a.bin", "force": "true"})
        self.assertEqual(second["parts"], ["name", "force", "file"])  # the root has no id
        for call in (first, second):
            self.assertEqual(int(call["content_length"]), call["size"])
            self.assertIsNone(call["transfer_encoding"])
            self.assertTrue(call["content_type"].startswith("multipart/form-data; boundary="))
        self.assertEqual(self.fake.find(self.user, "/up/a.bin").data, data)
        self.assertEqual(read(d, "/b.bin"), data)
        self.assertGreater(MisskeyDriver.upload_timeout, http.TIMEOUT)  # Misskey answers once it stored the file

    def test_force(self):
        """Without force, Misskey answers an upload with the account's file of the same content, wherever it is."""
        d = self.new_driver()
        self.put(d, "/a/same.bin", b"same content")
        self.put(d, "/b/same.bin", b"same content")
        self.assertEqual(sorted(file.folder for file in self.fake.files.values()),
                         sorted([self.fake.find(self.user, "/a").id, self.fake.find(self.user, "/b").id]))

    def test_an_upload_over_a_file(self):
        d = self.new_driver()
        self.put(d, "/o.txt", b"first")
        old = self.fake.find(self.user, "/o.txt")
        self.put(d, "/o.txt", b"second")
        self.assertNotIn(old.id, self.fake.files)  # deleted for good, after the new one was uploaded
        self.assertEqual(self.fake.names(self.user, "/"), ["o.txt"])
        self.assertEqual(read(d, "/o.txt"), b"second")
        self.assertNotEqual(d.download_url("/o.txt", filename="o.txt"), old.url)

    def test_a_failed_upload(self):
        d = self.new_driver()
        with self.assertRaises(DriverError):  # the data ends early
            d.write("/short.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual(self.fake.files, {})
        self.assertEqual(self.names(d, "/"), [])

    def test_upload_limits(self):
        """The account's role: Misskey's default types, and here files of 1 MB, 2 MB in all."""
        self.new_account(policies={**MISSKEY_DEFAULTS, "maxFileSizeMb": 1, "driveCapacityMb": 2})
        d = self.new_driver()
        self.put(d, "/notes.txt", b"text")  # text/plain, by the name
        self.put(d, "/p.png", PNG)  # image/png, by the content
        for name, data in (("/a.bin", b"\x00binary"), ("/z.zip", b"PK\x03\x04" + bytes(100)), ("/e", b"")):
            with self.subTest(name=name), self.assertRaises(DriverError) as caught:
                self.put(d, name, data)
            self.assertNotIsInstance(caught.exception, BackendError)
            self.assertEqual(self.answer("drive/files/create"), "UNALLOWED_FILE_TYPE")
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/big.png", PNG + bytes(MIB))
        self.assertEqual(self.answer("drive/files/create"), "MAX_FILE_SIZE_EXCEEDED")
        self.put(d, "/one.png", PNG + bytes(900 * 1024))
        self.put(d, "/two.png", PNG + b"2" + bytes(900 * 1024))
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/three.png", PNG + b"3" + bytes(900 * 1024))
        self.assertEqual(self.answer("drive/files/create"), "NO_FREE_SPACE")
        self.assertNotIsInstance(caught.exception, BackendError)
        self.new_account(policies=MISSKEY_DEFAULTS, moderator=True)  # moderators have none of these limits
        d = self.new_driver()
        self.put(d, "/a.bin", b"\x00binary")
        self.fake.max_file_size = MIB  # the server's maxFileSize
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/huge.bin", os.urandom(MIB + 1))
        self.assertIn("413", caught.exception.message)
        self.assertEqual(self.fake.requests("drive/files/create")[-1]["status"], 413)

    def test_rate_limits(self):
        self.fake.rate_limits = {"drive/folders/create": 2, "drive/files/create": 1}
        d = self.new_driver()
        d.mkdir("/a")
        d.mkdir("/b")
        with self.assertRaises(BackendError) as caught:
            d.mkdir("/c")
        self.assertIn("30", caught.exception.message)  # Retry-After: 1800 seconds
        self.put(d, "/x.txt", b"x")
        with self.assertRaises(BackendError):
            self.put(d, "/y.txt", b"y")
        self.assertEqual(self.answer("drive/files/create"), "RATE_LIMIT_EXCEEDED")
        self.assertEqual(self.names(d, "/"), ["a", "b", "x.txt"])

    # -- changes

    def test_delete_a_folder_with_everything_in_it(self):
        d = self.new_driver()
        self.put(d, "/d/a.txt", b"a")
        self.put(d, "/d/s/b.txt", b"b")
        self.put(d, "/d/s/t/c.txt", b"c")
        d.mkdir("/d/empty")
        self.put(d, "/d2/keep.txt", b"k")
        folder = self.fake.find(self.user, "/d")
        d.delete("/d")
        self.assertEqual(self.names(d, "/"), ["d2"])
        self.assertEqual([file.name for file in self.fake.files.values()], ["keep.txt"])  # for good: there is no trash
        self.assertEqual([folder.name for folder in self.fake.folders.values()], ["d2"])
        deletes = self.fake.requests("drive/(files|folders)/delete")
        self.assertEqual(len(deletes), 3 + 4)  # one call each, what is in a folder first
        self.assertEqual(deletes[-1]["json"], {"folderId": folder.id})
        self.assertTrue(all(call["status"] == 204 for call in deletes))

    def test_copies_are_the_sites(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        d.mkdir("/f")
        self.assertFalse(d.can_copy)  # the site copies files with open() and write()
        with self.assertRaises(Unsupported):
            d.copy("/a.txt", "/f")
        self.assertEqual(self.fake.names(self.user, "/f"), [])

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件 1.bin", data)
        file = self.fake.find(self.user, "/dl/文件 1.bin")
        self.assertTrue(d.can_direct_download)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertEqual(url, file.url)
        self.assertEqual(httpx.get(url).content, data)  # anyone's: no token
        self.assertEqual(read(d, "/dl/文件 1.bin"), data)
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        calls = self.downloads()[1:]
        self.assertEqual([call["range"] for call in calls], [None, "bytes=10-29", "bytes=5000-"])
        self.assertTrue(all(call["accept_encoding"] == "identity" and call["authorization"] is None for call in calls))
        del self.fake.files[file.id]  # deleted in Misskey: its address with it
        with self.assertRaises(NotFound):
            read(d, "/dl/文件 1.bin")

    def test_downloads_from_elsewhere(self):
        """Files in object storage, and a server that redirects: fetched there, never with the token."""
        self.fake.object_storage = True
        d = self.new_driver()
        self.put(d, "/s.bin", b"kept elsewhere")
        self.assertTrue(d.download_url("/s.bin", filename="s.bin").startswith(self.fake.url + "/bucket/"))
        self.assertEqual(read(d, "/s.bin", 5), b"elsewhere")
        self.fake.object_storage = False
        self.fake.redirect_files = True
        self.put(d, "/r.bin", b"redirected")
        self.assertEqual(read(d, "/r.bin"), b"redirected")
        self.assertEqual([call["path"].split("/")[1] for call in self.downloads()], ["bucket", "files", "bucket"])
        self.assertTrue(all(call["authorization"] is None for call in self.downloads()))

    def test_an_empty_file(self):
        d = self.new_driver()
        self.put(d, "/e", b"")
        self.assertEqual(read(d, "/e"), b"")
        self.assertEqual(self.downloads(), [])  # nothing to fetch

    # -- errors

    def test_errors(self):
        def failure(status, code=None, endpoint="drive/files", headers=None):
            if code is None:
                return MisskeyDriver._failure(httpx.Response(status, text="<html></html>", headers=headers), endpoint)
            body = {"error": {"message": "m", "code": code, "id": "x", "kind": "client"}}
            return MisskeyDriver._failure(httpx.Response(status, json=body, headers=headers), endpoint)

        self.assertIsInstance(failure(401, "AUTHENTICATION_FAILED"), AuthError)
        self.assertIsInstance(failure(401, "CREDENTIAL_REQUIRED"), AuthError)
        self.assertIsInstance(failure(403, "AUTHENTICATION_FAILED"), AuthError)  # as misskey.io's /api.json says
        self.assertIsInstance(failure(403, "YOUR_ACCOUNT_SUSPENDED"), AuthError)
        self.assertIn("read:drive", failure(403, "PERMISSION_DENIED", "drive/folders/show").message)
        self.assertIn("write:drive", failure(403, "PERMISSION_DENIED", "drive/folders/update").message)
        for code in ("NO_SUCH_FILE", "NO_SUCH_FOLDER", "NO_SUCH_PARENT_FOLDER"):
            self.assertIsInstance(failure(400, code), NotFound)
        self.assertIsInstance(failure(400, "INVALID_FILE_NAME"), InvalidPath)
        own = {code: failure(status, code) for status, code in (
            (400, "NO_FREE_SPACE"), (413, "MAX_FILE_SIZE_EXCEEDED"), (400, "UNALLOWED_FILE_TYPE"), (400, "INAPPROPRIATE"),
            (400, "HAS_CHILD_FILES_OR_FOLDERS"), (403, "YOUR_ACCOUNT_MOVED"))}
        for code, error in own.items():
            self.assertIs(type(error), DriverError, code)
        self.assertEqual(len({error.message for error in own.values()}), len(own))  # each its own message
        self.assertIs(type(failure(413)), DriverError)  # the server's maxFileSize, or a proxy's limit
        self.assertIn("413", failure(413).message)
        limited = failure(429, "RATE_LIMIT_EXCEEDED", headers={"Retry-After": "3481"})
        self.assertIsInstance(limited, BackendError)
        self.assertIn("59", limited.message)  # minutes, rounded up
        self.assertIsInstance(failure(429), BackendError)  # a proxy's, without Retry-After
        self.assertIsInstance(failure(404), BackendError)
        self.assertIn("404", failure(404).message)
        for status in (500, 502, 503):
            self.assertIsInstance(failure(status, "INTERNAL_ERROR" if status == 500 else None), BackendError)
        refused = failure(400, "INVALID_PARAM")
        self.assertIs(type(refused), DriverError)
        self.assertIn("INVALID_PARAM", refused.message)
        self.assertIn("400", refused.message)

    def test_not_misskey(self):
        self.fake.fail_status = 502  # a proxy in front of it
        with self.assertRaises(BackendError):
            self.new_driver().list("/")
        self.fake.fail_status = 0
        with self.assertRaises(BackendError) as caught:  # no such endpoint
            self.new_driver(url=self.fake.url + "/api/v1").list("/")
        self.assertIn("404", caught.exception.message)
        with self.assertRaises(BackendError):  # the web client's page
            self.new_driver(url=self.fake.url + "/notes").list("/")

    def test_a_redirect(self):
        d = self.new_driver()
        response = httpx.Response(302, headers={"Location": "https://login.example.com/"})
        with mock.patch.object(d._http, "post", return_value=response):
            with self.assertRaises(BackendError):
                d.list("/")

    # -- settings

    def test_settings(self):
        check = MisskeyDriver.validate_config
        self.assertEqual(check({"url": "https://misskey.example", "access_token": "abc"}), {})
        self.assertIn("url", check({"url": "misskey.example", "access_token": "abc"}))
        self.assertIn("url", check({"url": "ftp://misskey.example", "access_token": "abc"}))
        self.assertIn("base_path", check({"url": "https://misskey.example", "access_token": "abc", "base_path": "/a/../b"}))
        self.assertIn("access_token", check({"url": "https://misskey.example", "access_token": "ab\u200bc"}))  # pasted with a zero-width space
        d = MisskeyDriver({"url": " https://misskey.example/ ", "access_token": " abc "})
        self.addCleanup(d.close)
        self.assertEqual((d._api, d._token), ("https://misskey.example/api", "abc"))

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in MisskeyDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(MisskeyDriver), ["url", "access_token"])
        self.assertEqual(MisskeyDriver.auth_params, ())
        self.assertEqual([param.key for param in MisskeyDriver.params if param.secret], ["access_token"])


if __name__ == "__main__":
    unittest.main()
