"""Tests of the Teldrive driver (apps.storages.drivers.teldrive) against tests/fake_teldrive.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_teldrive
"""

from __future__ import annotations

import io
import os
import unittest
import uuid
from unittest import mock

import httpx

from apps.storages import config
from apps.storages.drivers import http
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, NotFound
from apps.storages.drivers.teldrive import MIB, ROOT, TeldriveDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_teldrive import FakeTeldrive

JSON = {"Content-Type": "application/json"}

UUID = "[0-9a-f-]{36}"


def driver_class(**attrs):
    """TeldriveDriver with other tuning (the type stays "teldrive")."""
    return type("FakeTeldriveDriver", (TeldriveDriver,), attrs)


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
        cls.fake = FakeTeldrive().start()
        cls.Driver = driver_class(**cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_account(self) -> None:
        self.user, self.key = self.fake.add_user()

    def new_driver(self, base: str = "/", driver_class=None, **settings):
        """A driver of the account, with parts of 1 MiB (the least Teldrive takes) unless `part_size` says."""
        settings = {"url": self.fake.url, "api_key": self.key, "base_path": base, "part_size": 1, **settings}
        driver = (driver_class or self.Driver)({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class TeldriveContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root of a new account's drive (which has no id in Teldrive)."""

    def make_driver(self):
        self.fake.reset()
        self.new_account()
        return self.new_driver()


class TeldrivePagesContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again in a folder of the drive, with small listing pages and the server choosing parts of 256 KiB."""

    driver_attrs = {"page_size": 5}

    def make_driver(self):
        self.fake.reset()
        self.fake.chosen_part_size = 256 * 1024
        self.new_account()
        self.fake.mkdirs(self.user, "/base/here")
        self.fake.put(self.user, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here")


class TeldriveTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.new_account()

    def put(self, driver, path: str, data: bytes) -> None:
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    def uploads(self) -> list[dict]:
        return [call["json"] for call in self.fake.requests("POST", "/uploads")]

    # -- the key

    def test_the_key_goes_in_its_header_only(self):
        d = self.new_driver()
        data = os.urandom(MIB + 100)
        self.put(d, "/a/one.bin", data)
        d.write("/a/two.bin", Stream(data), len(data))
        d.mkdir("/b")
        d.copy("/a/one.bin", "/b")
        d.move("/a/two.bin", "/b")
        d.rename("/b/two.bin", "three.bin")
        self.assertEqual(read(d, "/b/one.bin", 5, 10), data[5:15])
        d.delete("/a")
        self.assertTrue(self.fake.calls)
        for call in self.fake.calls:
            self.assertEqual(call["api_key"], self.key)
            self.assertNotIn(self.key, call["target"])
            self.assertIsNone(call["authorization"])
            self.assertIsNone(call["cookie"])

    def test_a_refused_key(self):
        d = self.new_driver()
        d.list("/")
        self.fake.revoke(self.key)  # revoked in Teldrive, or past the expiry it was made with
        with self.assertRaises(AuthError):
            d.list("/")
        with self.assertRaises(AuthError):
            self.new_driver(api_key="tdk_" + "x" * 43).check()
        with self.assertRaises(AuthError):
            self.new_driver(api_key="").check()

    # -- listings

    def test_pages_of_a_listing(self):
        for n in range(230):
            self.fake.put(self.user, f"/big/f{n:03}.txt", b"x")
        folder = self.fake.find(self.user, "/big")
        d = self.new_driver(driver_class=driver_class(page_size=100))
        self.assertEqual(self.names(d, "/big"), [f"f{n:03}.txt" for n in range(230)])
        calls = [call for call in self.fake.requests("GET", "/files") if call["query"].get("parentId") == [folder.id]]
        self.assertEqual([call["query"]["limit"] for call in calls], [["100"]] * 3)
        self.assertNotIn("cursor", calls[0]["query"])
        self.assertTrue(all(call["query"]["cursor"] for call in calls[1:]))
        for n in range(230, 300):  # 300: the last page is full, the one after it empty
            self.fake.put(self.user, f"/big/f{n:03}.txt", b"x")
        self.assertEqual(len(d.list("/big")), 300)
        self.assertEqual(len([call for call in self.fake.requests("GET", "/files") if call["query"].get("parentId") == [folder.id]]), 7)

    def test_the_root_has_no_id(self):
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertNotIn("parentId", self.fake.requests("GET", "/files")[0]["query"])
        d.mkdir("/f")
        self.put(d, "/r.txt", b"r")
        d.move("/a.txt", "/f")
        d.move("/f/a.txt", "/")
        d.copy("/r.txt", "/f")
        for call in self.fake.requests("POST", "/folders") + self.fake.requests("POST", "/uploads"):
            self.assertNotIn("parentId", call["json"])
        moves = [call["json"] for call in self.fake.requests("POST", f"/files/{UUID}/move")]
        self.assertEqual(moves[0], {"conflictPolicy": "fail", "parentId": self.fake.find(self.user, "/f").id})
        self.assertEqual(moves[1], {"conflictPolicy": "fail"})
        self.assertEqual(self.names(d, "/"), ["a.txt", "f", "r.txt"])
        self.assertEqual(self.names(d, "/f"), ["r.txt"])

    def test_a_folder_trashed_in_teldrive(self):
        self.fake.put(self.user, "/a/b/c.txt", b"c")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/a/b"), ["c.txt"])
        self.fake.trash(self.user, "/a/b")  # in Teldrive's own UI
        with self.assertRaises(NotFound):
            d.list("/a/b")

    def test_names_teldrive_takes(self):
        """Names are unique as they are spelled; "/" is allowed in Teldrive, and such a name is left out."""
        self.fake.put(self.user, "/", b"slash", name="a/b.txt")
        d = self.new_driver()
        self.put(d, "/Case.txt", b"1")
        self.put(d, "/case.txt", b"2")
        self.assertEqual(self.names(d, "/"), ["Case.txt", "case.txt"])
        self.assertEqual(TeldriveDriver.forbidden_chars, "")
        self.assertEqual(TeldriveDriver.max_name_bytes, 255)
        long = "长" * 85  # 255 bytes, which Teldrive takes (255 characters)
        self.put(d, "/" + long, b"l")
        self.assertIn(long, self.names(d, "/"))

    # -- changes

    def test_delete_puts_into_the_trash(self):
        d = self.new_driver()
        self.put(d, "/d/x.txt", b"x")
        folder, file = self.fake.find(self.user, "/d"), self.fake.find(self.user, "/d/x.txt")
        d.delete("/d")
        self.assertEqual((folder.status, file.status), ("trashed", "trashed"))  # restorable from Teldrive's Trash
        self.assertEqual(self.names(d, "/"), [])

    def test_names_taken_meanwhile(self):
        """Teldrive's 409s, when a name was taken after the listing HFile went by."""
        d = self.new_driver()
        self.put(d, "/x.txt", b"1")
        d.mkdir("/dst")
        self.fake.put(self.user, "/dst/x.txt", b"other")
        self.fake.put(self.user, "/f", b"a file")
        nodes = {node.name: node for node in d._fetch(ROOT)}
        dst = nodes["dst"].id
        with self.assertRaises(AlreadyExists):
            d._create_node(ROOT, "f")
        with self.assertRaises(AlreadyExists):
            d._move_node(nodes["x.txt"], ROOT, dst)
        with self.assertRaises(AlreadyExists):
            d._copy_node(nodes["x.txt"], dst)
        with self.assertRaises(AlreadyExists):
            d._rename_node(nodes["x.txt"], "f")
        with self.assertRaises(AlreadyExists):  # an upload over a folder
            d._upload_node(ROOT, "dst", io.BytesIO(b"u"), 1)
        self.assertEqual(self.fake.find(self.user, "/dst/x.txt").data, b"other")
        self.assertTrue(all(call["json"]["conflictPolicy"] == "fail"
                            for call in self.fake.requests("POST", f"/(folders|files/{UUID}/(move|copy))")))

    def test_idempotency_keys(self):
        """The calls that make something carry a new UUID each (Teldrive answers 400 without one)."""
        d = self.new_driver()
        self.put(d, "/a/b.txt", b"b")  # a folder, an upload session, its completion
        d.mkdir("/c")
        d.copy("/a/b.txt", "/c")
        d.move("/a/b.txt", "/")
        made = self.fake.requests("POST", f"/(folders|uploads|uploads/{UUID}/complete|files/{UUID}/(move|copy))")
        keys = [call["idempotency"] for call in made]
        self.assertEqual(len(made), 6)
        self.assertEqual(len(set(keys)), len(keys))
        self.assertTrue(all(str(uuid.UUID(key)) == key for key in keys))

    # -- uploads

    def test_parts(self):
        d = self.new_driver()
        data = os.urandom(2 * MIB + MIB // 2)
        source = io.BytesIO(b"xx" + data)
        source.seek(2)
        d.write("/p.bin", source, len(data))
        [created] = self.uploads()
        self.assertEqual((created["size"], created["preferredPartSize"], created["conflictPolicy"]), (len(data), MIB, "replace"))
        parts = self.fake.requests("PUT", f"/uploads/{UUID}/parts/[0-9]+")
        self.assertEqual([call["path"].rsplit("/", 1)[1] for call in parts], ["1", "2", "3"])
        self.assertEqual([call["content_length"] for call in parts], [str(MIB), str(MIB), str(MIB // 2)])
        self.assertTrue(all(call["content_type"] == "application/octet-stream" and call["transfer_encoding"] is None
                            for call in parts))
        self.assertEqual(self.fake.find(self.user, "/p.bin").data, data)
        self.fake.chosen_part_size = 300 * 1024  # Teldrive chose another size: it is followed
        d.write("/s.bin", Stream(data), len(data))  # data that can't seek: streamed all the same
        parts = self.fake.requests("PUT", f"/uploads/{UUID}/parts/[0-9]+")[3:]
        self.assertEqual(len(parts), 9)
        self.assertEqual(parts[-1]["content_length"], str(len(data) - 8 * 300 * 1024))
        self.assertEqual(read(d, "/s.bin"), data)

    def test_the_part_size_setting(self):
        d = self.new_driver(part_size=None)  # left out: Teldrive's own default, 512 MiB
        self.put(d, "/a.txt", b"a")
        d = self.new_driver(part_size=64)
        self.put(d, "/b.txt", b"b")
        self.assertEqual([body["preferredPartSize"] for body in self.uploads()], [512 * MIB, 64 * MIB])

    def test_an_empty_file(self):
        d = self.new_driver()
        self.put(d, "/e", b"")
        self.assertEqual(self.fake.requests("PUT", f"/uploads/{UUID}/parts/[0-9]+"), [])
        self.assertEqual(len(self.fake.requests("POST", f"/uploads/{UUID}/complete")), 1)
        self.assertEqual(read(d, "/e"), b"")
        self.assertEqual(self.fake.requests("GET", f"/files/{UUID}/content/.*"), [])  # nothing to fetch

    def test_an_upload_over_a_file(self):
        d = self.new_driver()
        self.put(d, "/o.txt", b"first")
        old = self.fake.find(self.user, "/o.txt")
        self.put(d, "/o.txt", b"second")
        self.assertEqual(old.status, "deletion_pending")  # Teldrive's replace: deleted for good, not trashed
        self.assertEqual(self.names(d, "/"), ["o.txt"])
        self.assertEqual(read(d, "/o.txt"), b"second")

    def test_a_failed_upload_is_aborted(self):
        d = self.new_driver()
        self.fake.fail_part = 2  # Telegram refused the second part
        with self.assertRaises(BackendError):
            self.put(d, "/f.bin", os.urandom(3 * MIB))
        [upload] = self.fake.uploads.values()
        self.assertEqual(upload.state, "aborted")  # Teldrive's cleanup job deletes its parts from Telegram
        self.assertEqual(len(self.fake.requests("DELETE", f"/uploads/{upload.id}")), 1)
        self.fake.fail_part = None
        with self.assertRaises(DriverError):  # the data ends early
            d.write("/short.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual(sorted(upload.state for upload in self.fake.uploads.values()), ["aborted", "aborted"])
        self.assertEqual(self.names(d, "/"), [])

    def test_a_lost_answer_to_the_completion(self):
        """The completion is asked again when its answer is lost or a 5xx: Teldrive gives the file of a session it
        has completed already, and the session isn't aborted."""
        d = self.new_driver()
        self.put(d, "/o.txt", b"first")
        self.fake.drop_complete = 1  # Teldrive made the file, but its answer didn't come
        self.put(d, "/o.txt", b"second")
        self.assertEqual(read(d, "/o.txt"), b"second")
        self.fake.complete_status = 502  # a proxy's answer: not made yet
        self.put(d, "/p.txt", b"third")
        self.assertEqual(read(d, "/p.txt"), b"third")
        self.assertEqual(self.fake.requests("DELETE", f"/uploads/{UUID}"), [])
        self.assertEqual(len(self.fake.requests("POST", f"/uploads/{UUID}/complete")), 5)

    def test_the_type_by_the_name(self):
        d = self.new_driver()
        self.put(d, "/v.mp4", b"v")
        self.put(d, "/x.unknown-type", b"x")
        bodies = self.uploads()
        self.assertEqual(bodies[0]["mimeType"], "video/mp4")  # for Teldrive's own player
        self.assertNotIn("mimeType", bodies[1])

    def test_upload_limits(self):
        self.fake.body_limit = MIB // 2  # a proxy in front of Teldrive
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/big.bin", os.urandom(MIB))
        self.assertIn("413", caught.exception.message)
        self.assertNotIsInstance(caught.exception, BackendError)

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件 1.bin", data)
        self.assertFalse(d.can_direct_download)
        self.assertIsNone(d.download_url("/dl/文件 1.bin", filename="文件 1.bin"))
        self.assertEqual(read(d, "/dl/文件 1.bin"), data)
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        calls = self.fake.requests("GET", f"/files/{UUID}/content/.*")
        self.assertEqual([call["range"] for call in calls], [None, "bytes=10-29", "bytes=5000-"])
        self.assertTrue(all(call["accept_encoding"] == "identity" and call["path"].endswith("/文件 1.bin") for call in calls))
        self.fake.trash(self.user, "/dl/文件 1.bin")
        with self.assertRaises(NotFound):
            read(d, "/dl/文件 1.bin")

    # -- errors

    def test_errors(self):
        def failure(status, body=None, **kwargs):
            response = httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html></html>")
            return TeldriveDriver._failure(response, **kwargs)

        def answer(code, message="m"):
            return {"error": {"code": code, "message": message}}

        self.assertIsInstance(failure(401, answer("unauthorized")), AuthError)
        self.assertIsInstance(failure(404, answer("not_found")), NotFound)
        self.assertIsInstance(failure(409, answer("conflict")), AlreadyExists)
        self.assertIsInstance(failure(409, answer("upload_name_conflict")), AlreadyExists)
        self.assertIs(type(failure(409, answer("conflict"), taken=False)), DriverError)  # an upload part's channel
        self.assertIs(type(failure(409, answer("upload_part_busy"))), DriverError)
        self.assertIn("413", failure(413).message)
        refused = failure(422, answer("invalid_request", "request is invalid"))
        self.assertIs(type(refused), DriverError)
        self.assertIn("invalid_request", refused.message)
        self.assertIn("request is invalid", refused.message)
        self.assertIn("400", failure(400).message)
        for status in (429, 500, 503):
            self.assertIsInstance(failure(status, answer("internal_error")), BackendError)
        d = self.new_driver()
        self.fake.fail_status = 502
        with self.assertRaises(BackendError):
            d.list("/")
        self.fake.fail_status = 0
        self.fake.version1 = True  # no /api/v1: Teldrive 1.x, or not Teldrive
        with self.assertRaises(BackendError) as caught:
            self.new_driver().list("/")
        self.assertIn("404", caught.exception.message)
        self.fake.version1 = False
        with self.assertRaises(BackendError):  # its web UI's page: the address has a path too many
            self.new_driver(url=self.fake.url + "/files").list("/")

    def test_a_redirect(self):
        d = self.new_driver()
        response = httpx.Response(302, headers={"Location": "https://login.example.com/"})
        with mock.patch.object(d._http, "send", return_value=response):
            with self.assertRaises(BackendError):
                d.list("/")

    # -- settings

    def test_settings(self):
        check = TeldriveDriver.validate_config
        self.assertEqual(check({"url": "https://drive.example.com", "api_key": "tdk_abc"}), {})
        self.assertIn("url", check({"url": "drive.example.com", "api_key": "tdk_abc"}))
        self.assertIn("url", check({"url": "ftp://drive.example.com", "api_key": "tdk_abc"}))
        self.assertIn("base_path", check({"url": "https://drive.example.com", "api_key": "k", "base_path": "/a/../b"}))
        self.assertIn("api_key", check({"url": "https://drive.example.com", "api_key": "tdk_ab\u200bc"}))  # pasted with a zero-width space
        d = TeldriveDriver({"url": " https://drive.example.com/td/ ", "api_key": " tdk_abc "})
        self.addCleanup(d.close)
        self.assertEqual((d._api, d._key), ("https://drive.example.com/td/api/v1", "tdk_abc"))
        self.assertGreater(TeldriveDriver.copy_timeout, http.TIMEOUT)  # Teldrive copies in Telegram before it answers

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in TeldriveDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(TeldriveDriver), ["url", "api_key"])
        self.assertEqual(TeldriveDriver.auth_params, ())
        self.assertEqual([param.key for param in TeldriveDriver.params if param.secret], ["api_key"])

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        d = self.new_driver()
        self.put(d, "/a.txt", b"abc")
        body = LongBody(b'{"error": {"code": "forbidden", "message": "the file is locked"}}')
        answer = lambda request: Response(403, body, JSON, length=LONG) if "/content/" in request.path else None
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "the file is locked"):
            d.open("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
