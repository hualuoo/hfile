"""Tests of the FebBox driver (apps.storages.drivers.febbox) against tests/fake_febbox.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_febbox

FebBox's open platform can't upload, and the contract needs files: its tests run on UploadingFebBoxDriver, which puts a
file's bytes into the fake as FebBox's own apps would (_upload_node) and leaves the rest to IdDriver's write(), which
makes the folders above it with create_dir and deletes a file it replaces with file_delete. Everything else goes
through the API.
"""

from __future__ import annotations

import io
import time
import unittest
from unittest import mock

import httpx

from apps.storages import config
from apps.storages.drivers._cloud import exact_body
from apps.storages.drivers._ids import IdDriver, Node, now
from apps.storages.drivers.base import AuthError, BackendError, DriverError, NotFound, Unsupported
from apps.storages.drivers.febbox import FebBoxDriver
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_febbox import FakeFebBox, failure, success


class UploadingFebBoxDriver(FebBoxDriver):
    """FebBoxDriver with uploads through the fake's back door (see the module's docstring); `fake` and `user` are set
    by the tests."""

    fake: FakeFebBox
    user: int
    can_upload = True

    def write(self, path, data, size):
        IdDriver.write(self, path, data, size)

    def _upload_node(self, folder_id, name, data, size):
        item = self.fake.upload(self.user, int(folder_id), name, b"".join(exact_body(data, size)))
        return Node(id=str(item.fid), name=name, is_dir=False, size=size, modified=now())


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


class FakeMixin:
    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeFebBox().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_account(self) -> None:
        self.user = self.fake.add_user()
        self.client_id, self.secret = self.fake.add_key(self.user)

    def new_driver(self, base: str = "/", *, uploads: bool = False, attrs: dict | None = None, on_update=None,
                   **settings):
        """A driver of the account on the fake, unpaced, waiting out throttling for a moment only."""
        cls_attrs = {"api_root": self.fake.url, "rate": 0, "throttle_delay": 0.01, "throttle_wait": 0.2,
                     **self.driver_attrs, **(attrs or {})}
        if uploads:
            cls_attrs.update(fake=self.fake, user=self.user)
        cls = type("FakeFebBoxDriver", (UploadingFebBoxDriver if uploads else FebBoxDriver,), cls_attrs)
        settings = {"client_id": self.client_id, "client_secret": self.secret, "base_path": base, **settings}
        driver = cls({key: value for key, value in settings.items() if value is not None}, on_update=on_update)
        self.addCleanup(driver.close)
        return driver


class FebBoxContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root of a new account, which also lists a share it subscribed to (left out)."""

    def make_driver(self):
        self.fake.reset()
        self.new_account()
        self.fake.add_share(self.user, "Movies")
        return self.new_driver(uploads=True)


class FebBoxPagesContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """Again in a folder of the account, next to a folder outside it, with listing pages of 2 entries."""

    driver_attrs = {"page_size": 2}

    def make_driver(self):
        self.fake.reset()
        self.new_account()
        self.fake.mkdirs(self.user, "/base/here")
        self.fake.put(self.user, "/keep/out.txt", b"not in the base")
        return self.new_driver("/base/here", uploads=True)


class FebBoxTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.new_account()

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    def token_calls(self) -> list[dict]:
        return [call for call in self.fake.calls if call["path"] == "/oauth/token"]

    # -- the key and its token

    def test_the_token(self):
        updates = []
        d = self.new_driver(on_update=updates.append)
        self.fake.put(self.user, "/a/b.txt", b"b")
        self.assertEqual(self.names(d, "/a"), ["b.txt"])
        d.mkdir("/c")
        d.copy("/a/b.txt", "/c")
        d.rename("/c/b.txt", "d.txt")
        d.move("/c/d.txt", "/a")
        self.assertEqual(read(d, "/a/d.txt"), b"b")
        d.delete("/c")
        [asked] = self.token_calls()
        self.assertEqual(asked["form"], {"grant_type": ["client_credentials"], "client_id": [self.client_id],
                                         "client_secret": [self.secret]})
        self.assertIsNone(asked["authorization"])
        [token] = self.fake.tokens
        api = [call for call in self.fake.calls if call["path"] == "/oauth"]
        self.assertGreater(len(api), 5)
        for call in self.fake.calls:
            self.assertEqual(call["method"], "POST")
            self.assertTrue(call["content_type"].startswith("multipart/form-data" if call["path"] == "/oauth"
                                                            else "application/x-www-form-urlencoded"), call)
            self.assertNotIn(token, call["target"])
            self.assertNotIn(self.secret, call["target"])
        self.assertTrue(all(call["authorization"] == f"Bearer {token}" for call in api))
        self.assertTrue(self.fake.downloads)
        self.assertTrue(all(call["authorization"] is None and call["cookie"] is None for call in self.fake.downloads))
        self.assertEqual(updates, [])  # nothing to save: client_credentials again renews it

    def test_an_expired_token(self):
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.fake.expire_tokens()
        self.assertEqual(self.names(d, "/"), ["a.txt"])  # -10001: another token, and the call again
        self.assertEqual(len(self.token_calls()), 2)
        self.assertEqual(len(self.fake.requests("file_list")), 3)

    def test_refused_keys(self):
        self.fake.revoke(self.client_id)
        with self.assertRaises(AuthError) as caught:
            self.new_driver().check()
        self.assertIn("-1", caught.exception.message)  # FebBox's code and message
        self.assertIn("invalid client", caught.exception.message)
        self.new_account()
        with self.assertRaises(AuthError):  # the two values swapped, as the Secret Key page invites
            self.new_driver(client_id=self.secret, client_secret=self.client_id).check()
        with self.assertRaises(AuthError):
            self.new_driver(client_secret="").check()

    def test_a_key_deleted_while_in_use(self):
        d = self.new_driver()
        d.list("/")
        self.fake.revoke(self.client_id)  # deleted on the Secret Key page: its tokens stop working
        with self.assertRaises(AuthError):
            d.list("/")
        asked = len(self.token_calls())
        with self.assertRaises(AuthError):
            d.list("/")
        self.assertEqual(len(self.token_calls()), asked)  # the refusal is remembered for a while

    # -- listings

    def test_pages_of_a_listing(self):
        for n in range(230):
            self.fake.put(self.user, f"/big/f{n:03}.txt", b"x")
        folder = str(self.fake.find(self.user, "/big").fid)
        d = self.new_driver(attrs={"page_size": 100})
        self.assertEqual(self.names(d, "/big"), [f"f{n:03}.txt" for n in range(230)])
        calls = [form for form in self.fake.requests("file_list") if form["parent_id"] == [folder]]
        self.assertEqual([(form["page"], form["pagelimit"], form["order"]) for form in calls],
                         [([page], ["100"], ["name_asc"]) for page in ("1", "2", "3")])
        self.fake.no_has_more = True  # answers without has_more: on while a page is full
        for n in range(230, 300):
            self.fake.put(self.user, f"/big/f{n:03}.txt", b"x")
        self.assertEqual(len(d.list("/big")), 300)
        calls = [form for form in self.fake.requests("file_list") if form["parent_id"] == [folder]]
        self.assertEqual([form["page"] for form in calls[3:]], [["1"], ["2"], ["3"], ["4"]])

    def test_what_listings_leave_out(self):
        self.fake.add_share(self.user, "Movies")  # subscribed from another account's share
        self.fake.put(self.user, "/", b"slash", name="a/b.txt")
        self.fake.put(self.user, "/ok.txt", b"ok")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["ok.txt"])
        with self.assertRaises(NotFound):
            d.stat("/Movies")
        with self.assertRaises(NotFound):
            self.new_driver("/Movies").check()
        with self.assertRaises(NotFound):
            self.new_driver("/missing").check()

    # -- changes

    def test_the_calls_that_change(self):
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        d.mkdir("/f")
        file, folder = self.fake.find(self.user, "/a.txt"), self.fake.find(self.user, "/f")
        self.assertEqual(self.fake.requests("create_dir"), [{"module": ["create_dir"], "parent_id": ["0"], "name": ["f"]}])
        self.assertTrue(d.stat("/f").is_dir)
        d.copy("/a.txt", "/f")
        d.rename("/a.txt", "b.txt")
        d.move("/b.txt", "/f")
        copy = self.fake.find(self.user, "/f/a.txt")
        d.delete("/f/a.txt")
        self.assertEqual(self.fake.requests("file_copy"), [{"module": ["file_copy"], "fids[]": [str(file.fid)], "to": [str(folder.fid)]}])
        self.assertEqual(self.fake.requests("file_rename"), [{"module": ["file_rename"], "fid": [str(file.fid)], "name": ["b.txt"]}])
        self.assertEqual(self.fake.requests("file_move"), [{"module": ["file_move"], "fids[]": [str(file.fid)], "to": [str(folder.fid)]}])
        self.assertEqual(self.fake.requests("file_delete"), [{"module": ["file_delete"], "fids[]": [str(copy.fid)]}])
        self.assertEqual(self.names(d, "/f"), ["b.txt"])
        self.assertEqual(self.fake.names(self.user, "/"), ["f"])

    def test_a_new_folder_that_isnt_listed(self):
        d = self.new_driver()
        with mock.patch.object(self.fake, "_module_create_dir", lambda *args: success()):
            with self.assertRaises(BackendError):  # FebBox said yes, but its listing doesn't show it
                d.mkdir("/g")
        self.assertEqual(d.list("/"), [])

    def test_writes_are_unsupported(self):
        d = self.new_driver()
        self.assertTrue(FebBoxDriver.can_write)  # folders, renames, moves, copies and deletes work
        self.assertFalse(FebBoxDriver.can_upload)  # nobody gets the "Upload" permission
        self.assertFalse(d.can_direct_upload)
        self.assertIsNone(d.upload_target("/a.txt", 1))
        with self.assertRaises(Unsupported):
            d.write("/a.txt", io.BytesIO(b"a"), 1)
        self.assertEqual(self.fake.names(self.user, "/"), [])

    # -- downloads

    def test_downloads(self):
        data = bytes(range(256)) * 20
        self.fake.put(self.user, "/dl/文件 1.bin", data)
        d = self.new_driver()
        self.assertTrue(d.can_direct_download)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertTrue(url.startswith(self.fake.url + "/dl/"))
        self.assertEqual(d.download_url("/dl/文件 1.bin", filename="other"), url)  # reused
        self.assertEqual(httpx.get(url).content, data)  # as a browser gets it: no token
        self.assertEqual(read(d, "/dl/文件 1.bin"), data)
        self.assertEqual(read(d, "/dl/文件 1.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件 1.bin", 5000), data[5000:])
        fid = str(self.fake.find(self.user, "/dl/文件 1.bin").fid)
        self.assertEqual(self.fake.requests("file_get_play_url_user"), [{"module": ["file_get_play_url_user"], "fid": [fid]}])
        self.assertEqual([call["range"] for call in self.fake.downloads[1:]], [None, "bytes=10-29", "bytes=5000-"])
        self.assertTrue(all(call["accept_encoding"] == "identity" for call in self.fake.downloads[1:]))
        with self.assertRaises(NotFound):
            d.download_url("/dl", filename="dl")  # a folder

    def test_an_expired_link(self):
        self.fake.put(self.user, "/a.bin", b"abc")
        d = self.new_driver()
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.fake.links.clear()  # expired: FebBox answers 403
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.assertEqual(len(self.fake.requests("file_get_play_url_user")), 2)
        self.assertEqual(len(self.fake.downloads), 3)

    def test_traffic_used_up(self):
        self.fake.put(self.user, "/a.bin", b"abc")
        self.fake.set_traffic(self.user, 4)
        d = self.new_driver()
        self.assertEqual(read(d, "/a.bin"), b"abc")
        self.assertEqual(read(d, "/a.bin"), b"abc")  # the link was got before the quota ran out
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            read(d, "/a.bin")
        self.assertNotIsInstance(caught.exception, (AuthError, BackendError))
        self.assertIn("-1", caught.exception.message)
        self.assertIn("Traffic exceeded", caught.exception.message)

    def test_an_empty_file(self):
        self.fake.put(self.user, "/e", b"")
        d = self.new_driver()
        self.assertEqual(read(d, "/e"), b"")
        self.assertEqual(self.fake.requests("file_get_play_url_user"), [])  # nothing to fetch

    # -- limits and errors

    def test_throttling(self):
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.fake.throttle = 2  # -429 twice, then an answer
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(len(self.fake.requests("file_list")), 3)
        self.fake.throttle = 1000
        with self.assertRaises(BackendError):
            d.list("/")
        self.fake.throttle = 0
        self.fake.per_minute = 1  # FebBox's 120 calls a minute, made small
        d.list("/")
        with self.assertRaises(BackendError):
            d.list("/")

    def test_pacing(self):
        d = self.new_driver(attrs={"rate": 20.0})
        start = time.monotonic()
        for _ in range(4):  # a token and three listings
            d.list("/")
        self.assertGreaterEqual(time.monotonic() - start, 0.15)
        self.assertLessEqual(FebBoxDriver.rate * 60, 120)  # FebBox allows an account 120 calls a minute

    def test_a_challenge_page_is_waited_out(self):
        """local-api-limits: "Back off on -429 or HTTP 403/challenge pages"."""
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.fake.html_status, self.fake.html_calls = 403, 1  # one Cloudflare page, then FebBox's answers
        self.assertEqual(self.names(d, "/"), ["a.txt"])

    def test_a_refused_token_isnt_waited_out(self):
        """A 403 is backed off like -429, but not one that refuses the token: a new token is what helps."""
        self.fake.put(self.user, "/a.txt", b"a")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.fake.bad_token_status = 403
        self.fake.expire_tokens()
        calls = len(self.fake.calls)
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual([call["path"] for call in self.fake.calls[calls:]], ["/oauth", "/oauth/token", "/oauth"])

    def test_errors(self):
        d = self.new_driver()
        for status in (403, 502):  # Cloudflare pages that don't go away: waited out for throttle_wait, then an error
            self.fake.html_status = status
            calls = len(self.fake.calls)
            with self.assertRaises(BackendError) as caught:
                d.list("/")
            self.assertIn(str(status), caught.exception.message)
            self.assertGreater(len(self.fake.calls) - calls, 1)
        self.fake.html_status = 0
        with mock.patch.object(self.fake, "_module_file_list", lambda *args: failure("system busy", -2)):
            with self.assertRaises(DriverError) as caught:
                d.list("/")
        self.assertNotIsInstance(caught.exception, (AuthError, BackendError))
        self.assertIn("-2", caught.exception.message)
        self.assertIn("system busy", caught.exception.message)
        empty = httpx.Response(200, json={"code": 1, "msg": "success", "data": {"file_list": [], "has_more": 0}})
        with mock.patch.object(d._http, "post", side_effect=[httpx.Response(429, text="slow down"), empty]) as post:
            self.assertEqual(d.list("/"), [])  # an HTTP 429 is waited out too
        self.assertEqual(post.call_count, 2)
        for response in (httpx.Response(200, text="<html></html>"), httpx.Response(302, headers={"Location": "https://www.febbox.com/"}),
                         httpx.Response(200, json={"code": 1, "data": {"file_list": "x"}})):
            with mock.patch.object(d._http, "post", return_value=response):
                with self.assertRaises(BackendError):
                    d.list("/")

    # -- settings

    def test_settings(self):
        check = FebBoxDriver.validate_config
        self.assertEqual(check({"client_id": "a", "client_secret": "b"}), {})
        self.assertIn("base_path", check({"client_id": "a", "client_secret": "b", "base_path": "/a/../b"}))
        self.assertLessEqual(FebBoxDriver.page_size, 500)  # file_list's most
        self.assertLess(FebBoxDriver.link_ttl, 14400)  # FebBox's links last 14400 seconds

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in FebBoxDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(FebBoxDriver), ["client_id", "client_secret"])
        self.assertEqual(FebBoxDriver.auth_params, ())
        self.assertFalse(FebBoxDriver.oauth)
        self.assertEqual([param.key for param in FebBoxDriver.params if param.secret], ["client_secret"])


if __name__ == "__main__":
    unittest.main()
