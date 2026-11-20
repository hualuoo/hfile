"""Tests of the Lenovo NAS share link driver (apps.storages.drivers.lenovo_nas_share) against the fake of its API.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_lenovo_nas_share
"""

from __future__ import annotations

import datetime
import time
import unittest

import httpx

from apps.storages.drivers.base import AuthError, BackendError, NotADirectory, NotFound
from apps.storages.drivers.lenovo_nas_share import LenovoNasShareDriver, share_code
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_cloud_http import FakeServer, Response
from apps.storages.tests.fake_lenovo_nas_share import DISK, TIME, FakeLenovoShare

CODE, PASSWORD = "Ab.cDeF1234", "e5eu"
LINK = f"https://siot-share.lenovo.com.cn/s/#/{CODE}"


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeLenovoShare().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, driver_class=LenovoNasShareDriver, **config):
        driver = driver_class({"share_id": CODE, "share_password": PASSWORD, "host": self.fake.url, **config})
        self.addCleanup(driver.close)
        return driver

    def calls(self, name):
        return [query for call, query in self.fake.calls if call == name]


class LenovoNasShareContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        self.fake.add_share(CODE, PASSWORD, READ_ONLY_TREE, folders=(EMPTY_FOLDER,))
        self.fake.add_share("Zz.other", "other", {"/outside.txt": b"not in this share"})
        return self.new_driver()


class LenovoNasShareTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.fake.add_share(CODE, PASSWORD, {"/a.txt": b"a", "/d/b.txt": b"bb", "/d/c+d & e#f.txt": b"ccc"})

    def names(self, driver, path):
        return sorted(entry.name for entry in driver.list(path))

    def test_listings(self):
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt", "d"])
        self.assertNotIn("path", self.calls("files")[0])  # the share's root has none
        entry = d.stat("/d/c+d & e#f.txt")
        self.assertEqual((entry.path, entry.is_dir, entry.size), ("/d/c+d & e#f.txt", False, 3))
        self.assertEqual(entry.modified, datetime.datetime.fromtimestamp(TIME, datetime.timezone.utc))
        self.assertEqual(self.calls("files")[1]["path"], f"{DISK}/{CODE}/d")  # sent as the API gave it
        d.open("/d/b.txt").read()
        d.list("/d")
        self.assertEqual(len(self.calls("files")), 2)  # the listings are cached
        with self.assertRaises(NotADirectory):
            d.list("/a.txt")
        with self.fake.lock:  # gone from the device after its folder was listed
            self.fake.disk.pop(f"{DISK}/{CODE}/d/b.txt")
        with self.assertRaises(NotFound):
            d.open("/d/b.txt")

    def test_a_folder_in_pages(self):
        self.fake.add_share("Pg.1", "pw", {f"/many/f{n}.txt": b"x" * n for n in range(7)})
        Driver = type("SmallPages", (LenovoNasShareDriver,), {"page_size": 3})
        d = self.new_driver(Driver, share_id="Pg.1", share_password="pw")
        self.assertEqual(self.names(d, "/many"), [f"f{n}.txt" for n in range(7)])
        pages = [(query["num"], query["page"]) for query in self.calls("files") if "path" in query]
        self.assertEqual(pages, [("3", "0"), ("3", "1"), ("3", "2")])
        # A server that answers every page with the first: the paging stops when one brings nothing new.
        self.fake.ignore_page = True
        self.fake.calls.clear()
        d = self.new_driver(Driver, share_id="Pg.1", share_password="pw")
        with self.assertLogs("hfile.storage", "WARNING"):  # has_more, but nothing new: the listing may be cut short
            self.assertEqual(self.names(d, "/many"), ["f0.txt", "f1.txt", "f2.txt"])
        self.assertEqual(len([query for query in self.calls("files") if "path" in query]), 2)

    def test_the_share_token_is_asked_for_again(self):
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt", "d"])
        self.assertEqual(self.calls("access"), [{"code": CODE, "password": PASSWORD}])
        self.fake.expire_tokens()  # refused before its time: another one, once
        self.assertEqual(d.open("/d/b.txt").read(), b"bb")
        self.assertEqual((len(self.calls("access")), self.fake.token_refusals), (2, 1))
        # Shortly before it expires by its expires_in, without being refused.
        self.fake.expires_in = 1
        d = self.new_driver()
        d.list("/")
        time.sleep(1.05)
        self.assertEqual(self.names(d, "/d"), ["b.txt", "c+d & e#f.txt"])
        self.assertEqual((len(self.calls("access")), self.fake.token_refusals), (4, 1))

    def test_refusals(self):
        with self.assertRaises(AuthError):
            self.new_driver(share_password="wrong").list("/")
        d = self.new_driver(share_password="wrong")
        for _ in range(2):
            with self.assertRaises(AuthError):
                d.stat("/")
        self.assertEqual(len(self.calls("access")), 2)  # a refused extraction code is remembered for a while
        with self.assertRaises(AuthError):
            self.new_driver(share_id="No.such1").list("/")
        d = self.new_driver()
        d.list("/")
        for state, error in (("expired", AuthError), ("cancelled", AuthError), ("offline", BackendError)):
            with self.subTest(state=state):
                self.fake.shares[CODE]["state"] = state
                with self.assertRaises(error):
                    self.new_driver().list("/")  # at access
                with self.assertRaises(error):
                    d.list("/d")  # with a token
        self.fake.shares[CODE]["state"] = "ok"
        with self.fake.lock:  # a folder gone from the device after the root was listed
            for path in [path for path in self.fake.disk if path.startswith(f"{DISK}/{CODE}/d")]:
                del self.fake.disk[path]
        with self.assertRaises(NotFound):
            d.stat("/d/b.txt")
        self.assertEqual(self.calls("files")[-1]["path"], f"{DISK}/{CODE}/d")

    def test_downloads(self):
        d = self.new_driver()
        url = d.download_url("/d/c+d & e#f.txt", filename="c+d & e#f.txt")
        self.assertTrue(url.startswith(f"{self.fake.url}/oneproxy/api/share/v1/file/download?code={CODE}&dtoken="), url)
        self.assertEqual(httpx.get(url).content, b"ccc")
        self.assertEqual(d.open("/d/c+d & e#f.txt", 1, 1).read(), b"c")
        self.assertEqual(self.fake.downloads[-1], "bytes=1-1")
        self.fake.ranges = False  # a server that ignores the Range: its whole file is cut down
        self.assertEqual(d.open("/d/c+d & e#f.txt", 1, 1).read(), b"c")
        self.fake.link_base = self.fake.url + "/relay"  # an absolute address is used as it is
        url = d.download_url("/a.txt", filename="a.txt")
        self.assertTrue(url.startswith(f"{self.fake.url}/relay/oneproxy/api/share/v1/file/download?"), url)
        self.assertEqual(d.open("/a.txt").read(), b"a")
        self.fake.link_with_token = True  # never handed to a browser: the site sends the file
        self.assertIsNone(d.download_url("/a.txt", filename="a.txt"))
        self.assertEqual(d.open("/a.txt").read(), b"a")
        with self.assertRaises(NotFound):
            d.download_url("/d", filename="d")

    def test_a_download_that_answers_an_error(self):
        # The API's errors come as HTTP 200 and JSON: a download answered so isn't the file.
        d = self.new_driver()
        self.fake.download_error = (-45005, "device is offline")
        with self.assertRaises(BackendError):
            d.open("/d/b.txt")
        self.fake.download_error = (0, "")  # {"result": true, "data": null}: JSON, but not the file either
        with self.assertRaises(BackendError):
            d.open("/a.txt", 0, 1)
        self.fake.download_error = None
        self.fake.download_type = "application/json"  # a file served as JSON, of its listed size: the file
        self.assertEqual(d.open("/d/b.txt").read(), b"bb")
        self.assertEqual(d.open("/d/b.txt", 1, 1).read(), b"b")

    def test_settings(self):
        check = LenovoNasShareDriver.validate_config
        self.assertEqual(check({"share_id": CODE, "share_password": PASSWORD}), {})
        self.assertEqual(check({"share_id": LINK + "/", "host": "http://192.168.1.10/"}), {})
        self.assertIn("share_id", check({"share_id": f"{LINK}/{PASSWORD}"}))  # the password would be kept in the clear
        for wrong in ("", "https://siot-share.lenovo.com.cn/s/", "Ab cd", "Ab/cd"):
            with self.subTest(share_id=wrong):
                self.assertIn("share_id", check({"share_id": wrong}))
        for wrong in ("siot-share.lenovo.com.cn", "ftp://192.168.1.10", "https://x.example/s/", "https://x.example/?a=1"):
            with self.subTest(host=wrong):
                self.assertIn("host", check({"share_id": CODE, "host": wrong}))
        self.assertEqual(share_code(f" {LINK} "), (CODE, False))
        self.assertEqual(share_code(f"{LINK}/{PASSWORD}"), (CODE, True))
        self.assertEqual(self.names(self.new_driver(share_id=LINK), "/"), ["a.txt", "d"])

    def test_a_server_that_isnt_one(self):
        for status in (200, 502):
            other = FakeServer(lambda req, status=status: Response(status, b"<html>hello</html>", {"Content-Type": "text/html"})).start()
            self.addCleanup(other.close)
            with self.subTest(status=status), self.assertRaises(BackendError):
                self.new_driver(host=other.url).list("/")


if __name__ == "__main__":
    unittest.main()
