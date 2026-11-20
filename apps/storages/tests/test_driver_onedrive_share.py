"""Tests of the OneDrive share link driver (apps.storages.drivers.onedrive_share) against the fake of SharePoint
(apps/storages/tests/fake_onedrive_share.py).

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_onedrive_share
"""

from __future__ import annotations

import unittest

import httpx

from apps.storages.drivers.base import AuthError, BackendError, DriverError, NotADirectory, NotFound
from apps.storages.drivers.onedrive_share import OneDriveShareDriver
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_onedrive_share import FakeOneDriveShare

JSON = {"Content-Type": "application/json"}


class FakeMixin:
    site = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeOneDriveShare(site=cls.site).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, driver_class=OneDriveShareDriver, **config):
        driver = driver_class({"url": self.fake.share_url, **config})
        self.addCleanup(driver.close)
        return driver


class OneDriveShareContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    """A folder of OneDrive for work or school, without a password."""

    def make_driver(self):
        self.fake.reset()
        self.fake.load(READ_ONLY_TREE, EMPTY_FOLDER)
        return self.new_driver()


class SharePointSiteContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    """A folder of a SharePoint site's library, with a password, whose downloads redirect to their tempauth links."""

    site = True

    def make_driver(self):
        self.fake.reset()
        self.fake.load(READ_ONLY_TREE, EMPTY_FOLDER)
        self.fake.password = " pa ss "
        self.fake.redirect_downloads = True
        return self.new_driver(password=" pa ss ")


class OneDriveShareTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.fake.add("/a.txt", b"hello")
        self.fake.add("/sub/b.txt", b"b")

    def names(self, driver, path):
        return sorted(entry.name for entry in driver.list(path))

    def calls(self, suffix):
        return [path for _, path in self.fake.calls if path.endswith(suffix)]

    def test_pages_and_the_cache(self):
        class SmallPages(OneDriveShareDriver):
            page_size = 2

        for n in range(5):
            self.fake.add(f"/many/f{n}.txt", b"x" * n)
        d = self.new_driver(SmallPages)
        self.assertEqual(self.names(d, "/many"), [f"f{n}.txt" for n in range(5)])
        self.assertEqual(len(self.calls("/RenderListDataAsStream")), 2 + 3)  # the root in 2 pages, /many in 3
        self.assertEqual(d.stat("/many/f4.txt").size, 4)
        self.assertEqual(d.open("/many/f3.txt").read(), b"xxx")
        self.assertEqual(len(self.calls("/RenderListDataAsStream")), 5)  # the listings are kept
        self.assertEqual(len(self.calls("/_api/contextinfo")), 1)  # one digest for the session
        self.assertEqual(len(self.calls(self.fake.link_path)), 1)

    def test_rows_in_list_data(self):
        self.fake.wrap = True
        self.assertEqual(self.names(self.new_driver(), "/"), ["a.txt", "sub"])

    def test_passwords(self):
        self.fake.password = "s3cret"
        with self.assertRaises(AuthError):
            self.new_driver().list("/")
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong").list("/")
        d = self.new_driver(password="s3cret")
        self.assertEqual(self.names(d, "/"), ["a.txt", "sub"])
        self.assertEqual(d.open("/sub/b.txt").read(), b"b")
        self.assertEqual(len([path for method, path in self.fake.calls
                              if method == "POST" and path.endswith("/guestaccess.aspx")]), 2)  # wrong, then right

    def test_links_that_dont_open(self):
        self.fake.sign_in = True  # not an "Anyone" link
        with self.assertRaises(AuthError):
            self.new_driver().check()
        self.fake.sign_in = False
        self.fake.removed = True
        with self.assertRaises(NotFound):
            self.new_driver().check()

    def test_redirects_on_the_links_host(self):
        self.fake.hops = 2
        self.assertEqual(self.names(self.new_driver(), "/sub"), ["b.txt"])
        self.assertEqual(len(self.calls("/hop.aspx")), 2)

    def test_an_expired_session_opens_the_link_again(self):
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt", "sub"])
        self.fake.expire_sessions()
        self.assertEqual(d.open("/a.txt").read(), b"hello")  # download.aspx sends it to sign in
        self.fake.expire_sessions()
        self.assertEqual(self.names(d, "/sub"), ["b.txt"])  # the listing is refused
        self.assertEqual(len(self.calls(self.fake.link_path)), 3)
        self.assertEqual(len(self.calls("/_api/contextinfo")), 3)

    def test_listings_and_paths(self):
        d = self.new_driver()
        entries = {entry.name: entry for entry in d.list("/")}
        self.assertEqual((entries["a.txt"].size, entries["sub"].is_dir), (5, True))
        self.assertIsNotNone(entries["a.txt"].modified.tzinfo)
        with self.assertRaises(NotADirectory):
            d.list("/a.txt")
        for missing in ("/Other", "/nope", "/sub/nope.txt", "/a.txt/x"):
            with self.subTest(path=missing), self.assertRaises(NotFound):
                d.stat(missing)

    def test_download_links(self):
        d = self.new_driver()
        url = d.download_url("/a.txt", filename="a.txt")
        self.assertIn("tempauth=", url)
        self.assertEqual(httpx.get(url).content, b"hello")
        self.assertEqual(self.fake.download_requests[-1], {"cookie": None, "range": None, "tempauth": True})
        stream = d.open("/a.txt", 1, 3)  # through the site: download.aspx with the cookie and the Range
        self.assertEqual(stream.read(), b"ell")
        self.assertEqual(self.fake.download_requests[-1]["range"], "bytes=1-3")
        self.assertIn("FedAuth=", self.fake.download_requests[-1]["cookie"])
        with self.assertRaises(NotFound):
            d.download_url("/sub", filename="sub")

    def test_no_download_link(self):
        self.fake.v2_refuses = True
        self.assertIsNone(self.new_driver().download_url("/a.txt", filename="a.txt"))
        self.fake.v2_refuses = False
        self.fake.item_urls = False
        self.assertIsNone(self.new_driver().download_url("/a.txt", filename="a.txt"))
        self.fake.item_urls = True
        self.fake.item_url_root = "http://elsewhere.invalid"  # the cookie isn't sent there
        self.assertIsNone(self.new_driver().download_url("/a.txt", filename="a.txt"))
        self.assertEqual(len([path for _, path in self.fake.calls if "/_api/v2.0/" in path]), 1)  # only v2_refuses'
        self.assertEqual(self.new_driver().open("/a.txt").read(), b"hello")

    def test_downloads_the_link_doesnt_allow(self):
        self.fake.block_downloads = True
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt", "sub"])
        with self.assertRaises(DriverError) as caught:
            d.open("/a.txt")
        self.assertNotIsInstance(caught.exception, AuthError)
        self.assertIn("403", caught.exception.message)
        self.assertEqual(len(self.calls(self.fake.link_path)), 2)  # opened again once, in case the cookie ran out

    def test_errors(self):
        d = self.new_driver()
        d.check()
        self.fake.fail = 429
        with self.assertRaises(BackendError):
            d.list("/sub")
        self.fake.fail = 500
        with self.assertRaises(BackendError):
            d.list("/sub")
        self.fake.fail = 400
        with self.assertRaises(DriverError):
            d.list("/sub")
        self.assertEqual(self.names(d, "/sub"), ["b.txt"])

    def test_a_short_retry_after_is_waited_out(self):
        # SharePoint throttles with 429 or 503 and Retry-After, and asks that it be honoured.
        self.fake.throttled = {"link": "0", "contextinfo": "0"}
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt", "sub"])
        self.fake.throttled = {"render": "0"}
        self.assertEqual(self.names(d, "/sub"), ["b.txt"])
        self.fake.throttled = {"download": "0"}
        self.assertEqual(d.open("/a.txt", 1, 3).read(), b"ell")
        self.assertEqual(self.fake.throttled, {})
        self.fake.throttled = {"download": "120"}  # longer than is waited for: an error at once
        with self.assertRaises(BackendError):
            d.open("/sub/b.txt")

    def test_settings(self):
        check = OneDriveShareDriver.validate_config
        for good in ("https://contoso-my.sharepoint.com/:f:/g/personal/user_contoso_com/EaBcD123?e=xYz",
                     "https://contoso.sharepoint.com/:f:/s/Team/EaBcD123",
                     "https://contoso-my.sharepoint.cn/:f:/g/personal/user_contoso_cn/EaBcD123",
                     "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites"):
            with self.subTest(link=good):
                self.assertEqual(check({"url": good}), {})
        personal = check({"url": "https://1drv.ms/f/c/0123456789abcdef/EaBcD123"})
        self.assertIn("url", personal)
        self.assertEqual(check({"url": "https://onedrive.live.com/?redeem=aHR0cHM6Ly8xZHJ2Lm1z"}), personal)
        for bad in ("", "contoso-my.sharepoint.com/:f:/g/x", "http://contoso-my.sharepoint.com/:f:/g/personal/u/Ea",
                    "https://example.com/:f:/g/personal/u/Ea", "https://sharepoint.com.example.com/:f:/g/u/Ea",
                    "https://contoso-my.sharepoint.com/:u:/g/personal/u/EaFile", "https://[bad"):
            with self.subTest(link=bad):
                self.assertIn("url", check({"url": bad}))
                self.assertNotEqual(check({"url": bad}), personal)

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        body = LongBody(b'{"error": {"code": "-2130575338", "message": "the item is checked out"}}')
        answer = lambda request: Response(400, body, JSON, length=LONG) if request.path.endswith("/download.aspx") else None
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "the item is checked out"):
            self.new_driver().open("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
