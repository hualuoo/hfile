"""Tests of the example driver (apps.storages.drivers.example) against the fake of its made-up API: what the tests
of a new driver look like (docs/development.md, 添加存储源).

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_example
"""

from __future__ import annotations

import io
import unittest

import httpx

from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, NotADirectory, NotFound
from apps.storages.drivers.example import ExampleDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_example import FakeExampleDrive

JSON = {"Content-Type": "application/json"}


class FakeMixin:
    """One fake server for each test class, and drivers pointed at it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeExampleDrive().start()
        # Pages of two items, so that every listing of the tests goes through several.
        cls.Driver = type("SmallPagesExampleDriver", (ExampleDriver,), {"page_size": 2})

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, **config):
        driver = self.Driver({"url": self.fake.url, "token": self.fake.token, **config})
        self.addCleanup(driver.close)
        return driver


class ExampleContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        return self.new_driver()


class ExampleBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The same, with the storage's root in a folder of the server."""

    def make_driver(self):
        self.fake.reset()
        self.fake.make_folder("/mnt/files")
        return self.new_driver(base_path="/mnt/files")


class ExampleTests(FakeMixin, unittest.TestCase):
    """What is special about this backend: what the contract can't know."""

    def setUp(self):
        super().setUp()
        self.fake.reset()

    def put(self, driver, path, data):
        driver.write(path, io.BytesIO(data), len(data))

    def test_a_refused_token(self):
        with self.assertRaises(AuthError):
            self.new_driver(token="wrong").list("/")

    def test_a_folder_over_several_pages(self):
        d = self.new_driver()
        for i in range(5):
            self.put(d, f"/many/{i}.txt", b"x")
        del self.fake.calls[:]
        self.assertEqual(sorted(entry.name for entry in d.list("/many")), [f"{i}.txt" for i in range(5)])
        self.assertEqual(self.fake.calls, [("GET", "/api/v1/children")] * 3)

    def test_throttled_calls_are_made_again(self):
        d = self.new_driver()
        self.fake.throttle = 2
        self.assertEqual(d.list("/"), [])
        self.fake.throttle = 3  # one more than the driver waits out
        with self.assertRaises(BackendError):
            d.list("/")

    def test_a_throttled_upload_isnt_sent_again(self):
        d = self.new_driver()
        self.fake.throttle = 1
        with self.assertRaises(BackendError):
            self.put(d, "/a.txt", b"a")
        self.assertEqual(self.fake.calls, [("PUT", "/api/v1/content")])

    def test_links_need_no_token(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"abc")
        response = httpx.get(d.download_url("/a.txt", filename="名字.txt"))
        self.assertEqual(response.content, b"abc")
        self.assertEqual(response.headers["content-disposition"], "attachment; filename*=UTF-8''%E5%90%8D%E5%AD%97.txt")
        self.assertIsNone(self.fake.link_requests[-1]["authorization"])
        with self.assertRaises(NotFound):
            d.download_url("/missing.txt", filename="missing.txt")

    def test_the_servers_errors(self):
        d = self.new_driver()
        self.put(d, "/f.txt", b"f")
        with self.assertRaises(NotADirectory):
            self.put(d, "/f.txt/inner.txt", b"x")  # the folder above is a file
        with self.assertRaises(AlreadyExists):
            d.mkdir("/f.txt")

    def test_a_base_path_that_is_missing(self):
        with self.assertRaises(NotFound) as caught:
            self.new_driver(base_path="/nope").check()
        self.assertIn("/nope", caught.exception.message)

    def test_settings(self):
        check = ExampleDriver.validate_config
        self.assertEqual(check({"url": "https://cloud.example.com", "token": "t"}), {})
        self.assertIn("url", check({"url": "cloud.example.com", "token": "t"}))
        self.assertIn("base_path", check({"url": "https://cloud.example.com", "token": "t", "base_path": "/a/../b"}))

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        d = self.new_driver()
        self.put(d, "/a.txt", b"abc")
        body = LongBody(b'{"error": {"code": "locked", "message": "the file is locked"}}')
        answer = lambda request: (Response(400, body, JSON, length=LONG)
                                  if request.method == "GET" and request.path.endswith("/content") else None)
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "the file is locked"):
            d.open("/a.txt")
        self.assertLess(body.sent, LONG // 4)
