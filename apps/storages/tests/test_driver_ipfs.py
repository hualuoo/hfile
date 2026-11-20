"""Tests of the IPFS driver (apps.storages.drivers.ipfs) against the fake of a Kubo node.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_ipfs
"""

from __future__ import annotations

import io
import unittest
import urllib.parse

import httpx

from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError
from apps.storages.drivers.ipfs import IPFSDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_ipfs import FakeKubo

JSON = {"Content-Type": "application/json"}


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeKubo().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, **config):
        driver = IPFSDriver({"api_url": self.fake.api_url, "gateway": self.fake.gateway_url, **config})
        self.addCleanup(driver.close)
        return driver


class IPFSContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        return self.new_driver()


class IPFSBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        with self.fake.lock:
            self.fake._make_dirs("/hfile/files")
        return self.new_driver(base_path="/hfile/files")


class IPFSTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def test_moving_onto_a_folders_name_is_refused(self):
        """MFS's mv would put the item inside the folder that has the target's name."""
        d = self.new_driver()
        d.write("/a", io.BytesIO(b"file a"), 6)
        d.mkdir("/x/a")
        with self.assertRaises(AlreadyExists):
            d.move("/a", "/x")
        with self.assertRaises(AlreadyExists):
            d.rename("/x", "a")
        self.assertEqual(sorted(e.name for e in d.list("/x")), ["a"])

    def test_the_gateway_link(self):
        d = self.new_driver()
        d.write("/文件.txt", io.BytesIO(b"content"), 7)
        url = d.download_url("/文件.txt", filename="文件.txt")
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(url).query), {"filename": ["文件.txt"], "download": ["true"]})
        self.assertEqual(httpx.get(url).content, b"content")
        self.assertIsNone(self.new_driver(gateway="").download_url("/文件.txt", filename="文件.txt"))

    def test_an_upload_that_breaks_off(self):
        """Kubo truncates the file it writes, and keeps what arrived of a body that broke off: an upload goes to a
        hidden name first, then onto the file, and the hidden one is deleted when it fails."""
        d = self.new_driver()
        d.write("/report.txt", io.BytesIO(b"the old report"), 14)
        with self.assertRaises(DriverError):
            d.write("/report.txt", io.BytesIO(b"the new rep"), 1000)  # the data ends early: the upload breaks off
        with self.assertRaises(DriverError):
            d.write("/new.txt", io.BytesIO(b"abc"), 1000)
        self.assertEqual(d.open("/report.txt").read(), b"the old report")
        self.assertEqual([e.name for e in d.list("/")], ["report.txt"])
        with self.fake.lock:
            self.assertEqual(sorted(self.fake.tree), ["/", "/report.txt"])  # no partial file left anywhere
        d.write("/report.txt", io.BytesIO(b"the new report"), 14)
        self.assertEqual(d.open("/report.txt").read(), b"the new report")

    def test_without_a_gateway_downloads_go_through_this_site(self):
        self.assertFalse(self.new_driver(gateway="").can_direct_download)
        self.assertTrue(self.new_driver().can_direct_download)

    def test_downloads_ask_for_the_bytes_as_they_are(self):
        d = self.new_driver()
        d.write("/a.txt", io.BytesIO(b"abc"), 3)
        self.assertEqual(d.open("/a.txt").read(), b"abc")
        self.assertEqual(self.fake.read_headers, ["identity"])
        self.fake.read_encoding = "gzip"  # a proxy in front of the RPC API that compresses
        with self.assertRaises(BackendError):
            d.open("/a.txt").read()

    def test_a_token(self):
        self.fake.token = "s3cret"
        with self.assertRaises(AuthError):
            self.new_driver().list("/")
        self.assertEqual(self.new_driver(token="s3cret").list("/"), [])

    def test_settings(self):
        check = IPFSDriver.validate_config
        self.assertEqual(check({"api_url": "http://127.0.0.1:5001"}), {})
        self.assertIn("api_url", check({"api_url": "127.0.0.1:5001"}))
        self.assertIn("gateway", check({"api_url": "http://127.0.0.1:5001", "gateway": "ipfs.io"}))

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        d = self.new_driver()
        d.write("/a.txt", io.BytesIO(b"abc"), 3)
        body = LongBody(b'{"Message": "the node is busy", "Code": 0, "Type": "error"}')
        answer = lambda request: Response(500, body, JSON, length=LONG) if request.path.endswith("/files/read") else None
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "the node is busy"):
            d.open("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
