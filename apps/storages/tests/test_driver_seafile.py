"""Tests of the Seafile driver (apps.storages.drivers.seafile) against the fake of a Seafile server.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_seafile
"""

from __future__ import annotations

import io
import os
import time
import unittest
from unittest import mock

import httpx

from apps.storages.drivers import http
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, NotFound
from apps.storages.drivers.seafile import SeafileDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_seafile import FakeSeafile


class Stream(io.RawIOBase):
    """Data that can't seek."""

    def __init__(self, raw: bytes):
        self._raw = io.BytesIO(raw)

    def readable(self):
        return True

    def readinto(self, buffer):
        return self._raw.readinto(buffer)


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeSeafile().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, driver_class=SeafileDriver, **config):
        settings = {"url": self.fake.url, "username": "alice@example.com", "password": "pw-alice", **config}
        driver = driver_class({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class SeafileContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        self.fake.add_library("文档")
        return self.new_driver(library="文档")


class SeafileBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        library_id = self.fake.add_library("lib")
        self.fake.add_file(library_id, "/keep/out.txt", b"not in the base")
        with self.fake.lock:
            self.fake._make_dirs(self.fake.libraries[library_id]["tree"], "/base/here")
        return self.new_driver(library=library_id, base_path="/base/here")


class SeafileTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def put(self, driver, path, data):
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path):
        return sorted(entry.name for entry in driver.list(path))

    def test_every_library_at_the_root(self):
        mine = self.fake.add_library("mine")
        self.fake.add_library("secret", encrypted_with="x")  # not shown: its files need the password
        shared = self.fake.add_library("shared", owner="bob@example.com", shared=True)
        self.fake.add_file(mine, "/a.txt", b"a")
        self.fake.add_file(shared, "/s.txt", b"s")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["mine", "shared"])
        self.assertEqual(self.names(d, "/mine"), ["a.txt"])
        self.put(d, "/mine/new/b.txt", b"b")
        d.move("/mine/new/b.txt", "/shared")  # between two libraries
        self.assertEqual(self.names(d, "/shared"), ["b.txt", "s.txt"])
        for change in (lambda: d.mkdir("/lib2"), lambda: d.delete("/mine"), lambda: d.rename("/mine", "x"),
                       lambda: self.put(d, "/top.txt", b"t"), lambda: d.move("/mine/a.txt", "/")):
            with self.assertRaises(DriverError):
                change()
        with self.assertRaises(NotFound):
            d.list("/secret")

    def test_a_token_instead_of_the_password(self):
        self.fake.add_library("lib")
        d = self.new_driver(username=None, password=None, token=self.fake.token_for(), library="lib")
        self.put(d, "/t.txt", b"t")
        self.assertNotIn(("POST", "/api2/auth-token/"), self.fake.calls)
        self.fake.revoke_tokens()
        with self.assertRaises(AuthError):
            d.list("/")

    def test_a_refused_token_logs_in_again(self):
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        self.put(d, "/a.txt", b"a")
        self.fake.revoke_tokens()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(self.fake.calls.count(("POST", "/api2/auth-token/")), 2)
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong", library="lib").list("/")

    def test_a_refused_login_is_remembered_for_a_while(self):
        """Seahub counts every failed login of the username; with FREEZE_USER_ON_LOGIN_FAILED on, the 5th freezes the
        account (seahub/api2/serializers.py). A refusal is kept for a while rather than asked again on every call."""
        self.fake.add_library("lib")
        d = self.new_driver(password="an old password", library="lib")
        for _ in range(8):
            with self.assertRaises(AuthError):
                d.list("/")
        self.assertEqual(self.fake.calls.count(("POST", "/api2/auth-token/")), 1)

    def test_an_encrypted_library(self):
        """Seafile doesn't encrypt names: an encrypted library lists without its password. Its file server refuses
        the files' data until the password has been set (POST /api2/repos/<id>/), and forgets it an hour after."""
        library_id = self.fake.add_library("vault", encrypted_with="open-sesame")
        self.fake.add_file(library_id, "/in.txt", b"inside")
        bare = self.new_driver(library="vault")
        self.assertEqual(self.names(bare, "/"), ["in.txt"])
        with self.assertRaises(DriverError) as caught:
            bare.open("/in.txt").read()
        self.assertIn("vault", caught.exception.message)  # give its password
        with self.assertRaises(DriverError):
            self.new_driver(library="vault", library_password="wrong").open("/in.txt").read()
        d = self.new_driver(library="vault", library_password="open-sesame")
        self.assertEqual(d.open("/in.txt").read(), b"inside")
        self.put(d, "/new.txt", b"new")
        self.assertEqual(httpx.get(d.download_url("/new.txt", filename="new.txt")).content, b"new")
        self.fake.opened.clear()  # Seafile forgot it: the file server's refusal has it sent again, once
        self.assertEqual(d.open("/in.txt", 1, 2).read(), b"ns")
        self.fake.opened.clear()
        self.put(d, "/new.txt", b"newer")  # an upload whose data can seek is sent again
        self.assertEqual(d.open("/new.txt").read(), b"newer")
        self.fake.opened.clear()
        with self.assertRaises(BackendError):  # data that can't seek can't
            d.write("/stream.txt", Stream(b"s"), 1)
        d.write("/stream.txt", Stream(b"s"), 1)  # the password was set again meanwhile
        self.assertEqual(d.open("/stream.txt").read(), b"s")

    def test_a_name_the_file_server_refuses(self):
        """The file server refuses a name of 256 bytes or more before it looks at the password, with the name in its
        message (fileop.go postMultiFiles, shouldIgnoreFile): that isn't its "Repo is encrypted", and isn't sent again."""
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        for data in (Stream(b"s"), io.BytesIO(b"s")):
            with self.assertRaises(DriverError) as caught:
                d.write("/encrypted " + "x" * 250 + ".txt", data, 1)
            self.assertIn("invalid fileName", caught.exception.message)
        self.assertEqual(len(self.fake.file_requests), 2)  # each sent once

    def test_an_encrypted_library_the_list_leaves_out(self):
        """A library chosen by its id that GET /api2/repos/ doesn't list: GET /api2/repos/<id>/ says it is encrypted."""
        library_id = self.fake.add_library("hidden", encrypted_with="pw", listed=False)
        self.fake.add_file(library_id, "/h.txt", b"h")
        d = self.new_driver(library=library_id, library_password="pw")
        self.assertEqual(d.open("/h.txt").read(), b"h")

    def test_the_library_password_is_set_again_before_seafile_forgets_it(self):
        library_id = self.fake.add_library("vault", encrypted_with="open-sesame")
        self.fake.add_file(library_id, "/in.txt", b"inside")
        d = self.new_driver(driver_class=type("ShortSeafileDriver", (SeafileDriver,), {"unlock_ttl": 0.3}),
                            library="vault", library_password="open-sesame")
        self.assertEqual(d.open("/in.txt").read(), b"inside")
        time.sleep(0.4)
        self.assertEqual(d.open("/in.txt").read(), b"inside")
        self.assertEqual(self.fake.calls.count(("POST", f"/api2/repos/{library_id}/")), 2)
        self.assertEqual(len(self.fake.file_requests), 2)  # set before the file server refused anything

    def test_names_seafile_would_change(self):
        """Seafile renames a new folder, or what is moved or copied, whose name is taken: refused before."""
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        d.mkdir("/f")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/f")
        self.put(d, "/a.txt", b"1")
        self.put(d, "/f/a.txt", b"2")
        with self.assertRaises(AlreadyExists):
            d.move("/a.txt", "/f")
        with self.assertRaises(AlreadyExists):
            d.copy("/a.txt", "/f")
        with self.assertRaises(AlreadyExists):
            d.rename("/a.txt", "f")
        self.assertEqual(self.names(d, "/"), ["a.txt", "f"])
        self.assertEqual(self.names(d, "/f"), ["a.txt"])

    def test_links_go_without_the_token(self):
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        data = bytes(range(256)) * 50
        self.put(d, "/dl/文件 1.bin", data)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertEqual(httpx.get(url).content, data)
        self.assertEqual(d.open("/dl/文件 1.bin", 10, 20).read(), data[10:30])
        self.assertEqual(self.fake.file_requests[-1], {"authorization": None, "range": "bytes=10-29"})
        self.assertTrue(all(r["authorization"] is None for r in self.fake.file_requests))  # uploads too

    def test_redirects_are_errors(self):
        """Seafile's API and file server don't redirect: a redirect is a proxy's (an http address that has to be https,
        a login page), and what was asked wasn't done. Taken for success, an upload would vanish."""
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        self.put(d, "/a.txt", b"a")
        self.fake.redirect_uploads = True
        with self.assertRaises(BackendError):
            self.put(d, "/b.txt", b"b")
        self.fake.redirect_uploads = False
        self.fake.redirect_posts = True
        with self.assertRaises(BackendError):
            d.mkdir("/x")
        self.fake.redirect_posts = False
        self.assertEqual(self.names(d, "/"), ["a.txt"])

    def test_the_upload_answer_must_list_the_file(self):
        """The file server answers an upload with ret-json's list of what it stored: anything else isn't an upload. It
        keeps names in NFC."""
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        self.fake.upload_answer = b"<html>Welcome</html>"  # a proxy's page, with HTTP 200
        with self.assertRaises(BackendError):
            self.put(d, "/a.txt", b"a")
        self.fake.upload_answer = None
        self.put(d, "/cafe\u0301.txt", b"c")  # sent in NFD, stored and answered in NFC
        self.assertEqual(self.names(d, "/"), ["caf\u00e9.txt"])

    def test_an_upload_answered_late(self):
        """Seafile answers an upload once it has stored the file's blocks and made the commit: the answer may take a
        second per MiB longer than http.TIMEOUT, as webdav.py's uploads do. Here http.TIMEOUT is 1 s and the answer
        comes 2 s after a 3 MiB file is stored."""
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        d.list("/")
        d._http.close()
        d._http = http.client(timeout=1.0)
        self.fake.upload_delay = 2.0
        data = os.urandom(3 * 1024 * 1024)
        with mock.patch.object(http, "TIMEOUT", 1.0):
            self.put(d, "/big.iso", data)
        self.fake.upload_delay = 0.0
        self.assertEqual(d.open("/big.iso").read(), data)

    def test_error_answers(self):
        """The file server's errors are {"error": ...} (its upload's) or plain text; Seahub's {"error_msg": ...}."""
        failure = SeafileDriver({"url": "https://seafile.example.com", "token": "t"})._failure
        invalid = failure(httpx.Response(400, content=b'{"error": "invalid fileName: a.\n"}\n'))
        self.assertIn("invalid fileName", invalid.message)
        too_large = failure(httpx.Response(442, content=b'{"error": "File size is too large.\n"}\n'))
        self.assertIs(type(too_large), DriverError)
        self.assertIn("max_upload_size", too_large.message)
        throttled = failure(httpx.Response(429, json={"detail": "Request was throttled. Expected available in 5 seconds."}))
        self.assertIsInstance(throttled, BackendError)
        failed = failure(httpx.Response(520, json={"error_msg": "Failed to make directory."}))
        self.assertIs(type(failed), DriverError)
        self.assertIn("Failed to make directory.", failed.message)
        self.fake.add_library("lib")
        d = self.new_driver(library="lib")
        self.fake.max_upload_size = 100
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/big.bin", b"x" * 200)
        self.assertIn("max_upload_size", caught.exception.message)
        self.fake.max_upload_size = None
        self.fake.throttle = True
        with self.assertRaises(BackendError):
            d.list("/x")
        self.fake.throttle = False

    def test_settings(self):
        check = SeafileDriver.validate_config
        self.assertEqual(check({"url": "https://seafile.example.com", "username": "a@b.c", "password": "p"}), {})
        self.assertEqual(check({"url": "https://seafile.example.com", "token": "t"}), {})
        self.assertIn("url", check({"url": "seafile.example.com", "token": "t"}))
        self.assertIn("username", check({"url": "https://seafile.example.com"}))
        self.assertIn("base_path", check({"url": "https://seafile.example.com", "token": "t", "base_path": "/x"}))
        self.assertEqual(check({"url": "https://seafile.example.com", "token": "t", "base_path": "/x", "library": "l"}), {})
        for bad in ("http://[::1", "https://seafile.example.com:port/"):  # httpx.InvalidURL
            with self.subTest(url=bad):
                self.assertIn("url", check({"url": bad, "token": "t"}))

    def test_an_address_with_a_query_or_fragment(self):
        """An address pasted from the browser, like https://seafile.example.com/#my-libs/."""
        self.fake.add_library("lib")
        for suffix in ("/#my-libs/", "/?from=menu", "/"):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.new_driver(url=self.fake.url + suffix, library="lib").list("/"), [])

    def test_a_missing_library(self):
        with self.assertRaises(NotFound) as caught:
            self.new_driver(library="nope").list("/")
        self.assertIn("nope", caught.exception.message)


if __name__ == "__main__":
    unittest.main()
