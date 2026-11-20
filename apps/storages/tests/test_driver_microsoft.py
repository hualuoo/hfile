"""Tests of the Microsoft Graph drivers (OneDrive, SharePoint) against fake_graph."""

from __future__ import annotations

import io
import itertools
import os
import threading
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages.drivers import microsoft
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotFound,
    Unsupported,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_graph import OTHER_SITE_ID, SITE_ID, FakeGraph

JSON = {"Content-Type": "application/json"}

_bases = itertools.count(1)


def against(fake: FakeGraph, cls: type) -> type:
    """The driver class pointed at the fake, with short waits."""
    return type(cls.__name__, (cls,), {
        "graph_root": fake.url, "login_root": fake.url, "retry_delay": 0.01, "copy_poll": 0.01,
    })


def config(fake: FakeGraph, base: str = "/", **extra) -> dict:
    return {"client_id": "app-id", "client_secret": "app-secret", "tenant": "common",
            "refresh_token": fake.issue_refresh_token(), "base_path": base, **extra}


class _FakeMixin:
    driver_class = microsoft.OneDriveDriver
    drive_key = "me"
    fake_tenant = "common"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeGraph(tenant=cls.fake_tenant)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_base(self) -> str:
        """A fresh, empty folder of the fake drive to be the storage root."""
        name = f"t{next(_bases)} 基"
        drive = self.fake.drive(self.drive_key)
        with self.fake.lock:
            drive.add(drive.root, name, True)
        return "/" + name

    def extra_config(self) -> dict:
        return {}

    def make_driver(self, **overrides):
        settings = config(self.fake, self.new_base(), **self.extra_config())
        settings.update(overrides)
        self.updates = []
        return against(self.fake, self.driver_class)(settings, on_update=self.updates.append)


class OneDriveContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    pass


class OneDriveSessionContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """The contract again with every non-empty write going through an upload session."""

    def make_driver(self, **overrides):
        driver = super().make_driver(**overrides)
        driver.small_upload_limit = 0
        driver.upload_chunk = 320 * 1024
        return driver


class SharePointContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """A SharePoint site given by its URL; its default library is the fake's "docs" drive."""

    driver_class = microsoft.SharePointDriver
    drive_key = "docs"

    def extra_config(self):
        return {"site": "https://contoso.sharepoint.com/sites/team"}


class OneDriveAppContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """The app's own access (client credentials) to a user's OneDrive."""

    driver_class = microsoft.OneDriveAppDriver
    fake_tenant = "contoso.onmicrosoft.com"

    def extra_config(self):
        return {"tenant": self.fake_tenant, "user": "alice@contoso.onmicrosoft.com"}


class OneDriveAppTests(_FakeMixin, unittest.TestCase):
    driver_class = microsoft.OneDriveAppDriver
    fake_tenant = "contoso.onmicrosoft.com"

    def extra_config(self):
        return {"tenant": self.fake_tenant, "user": "alice@contoso.onmicrosoft.com"}

    def test_no_one_signs_in(self):
        d = self.make_driver()
        self.addCleanup(d.close)
        given = self.fake.app_tokens
        d.mkdir("/x")
        self.assertEqual([e.name for e in d.list("/")], ["x"])
        self.assertEqual(self.fake.app_tokens, given + 1)  # one token, used for both
        self.assertEqual(self.updates, [])  # nothing to save: no refresh token
        self.assertFalse(d.oauth)
        with self.assertRaises(Unsupported):
            d.authorize_url(d.config, redirect_uri="https://x.example/cb", state="s")

    def test_another_user_and_the_settings(self):
        d = self.make_driver(user="bob@contoso.onmicrosoft.com")
        self.addCleanup(d.close)
        with self.assertRaises(DriverError):
            d.check()
        check = microsoft.OneDriveAppDriver.validate_config
        good = {"tenant": self.fake_tenant, "user": "alice@contoso.onmicrosoft.com"}
        self.assertEqual(check(good), {})
        self.assertIn("tenant", check({**good, "tenant": "common"}))
        self.assertIn("user", check({**good, "user": ""}))


class RootContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """base_path "/" on a drive that is emptied before every test."""

    def make_driver(self, **overrides):
        drive = self.fake.drive("me")
        with self.fake.lock:
            for child in list(drive.root.children.values()):
                drive.remove(child)
        settings = config(self.fake, "/")
        return against(self.fake, microsoft.OneDriveDriver)(settings)


class GraphTests(_FakeMixin, unittest.TestCase):
    def setUp(self):
        self.d = self.make_driver()
        self.addCleanup(self.d.close)
        self.fake.requests.clear()

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def read(self, path, offset=0, length=None):
        return self.d.open(path, offset, length).read()

    def token_requests(self):
        return [r for r in self.fake.requests if r[1].endswith("/token")]

    # -- OAuth

    def test_authorize_url(self):
        url = microsoft.OneDriveDriver.authorize_url(
            {"client_id": "app-id", "tenant": "contoso.onmicrosoft.com"},
            redirect_uri="https://files.example.com/oauth/callback?x=1", state="st&ate")
        split = urllib.parse.urlsplit(url)
        self.assertEqual(f"{split.scheme}://{split.netloc}", microsoft.GLOBAL_LOGIN)
        self.assertEqual(split.path, "/contoso.onmicrosoft.com/oauth2/v2.0/authorize")
        query = urllib.parse.parse_qs(split.query)
        self.assertEqual(query["client_id"], ["app-id"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["redirect_uri"], ["https://files.example.com/oauth/callback?x=1"])
        self.assertEqual(query["state"], ["st&ate"])
        self.assertIn("offline_access", query["scope"][0].split())
        china = microsoft.SharePointChinaDriver.authorize_url({"client_id": "x"}, redirect_uri="https://h/cb",
                                                              state="s")
        self.assertTrue(china.startswith(microsoft.CHINA_LOGIN + "/common/oauth2/v2.0/authorize?"))
        with self.assertRaises(DriverError):
            microsoft.OneDriveDriver.authorize_url({"client_id": ""}, redirect_uri="https://h/cb", state="s")
        with self.assertRaises(DriverError):
            microsoft.OneDriveDriver.authorize_url({"client_id": "x", "tenant": "a/../b"},
                                                   redirect_uri="https://h/cb", state="s")

    def test_exchange_code(self):
        cls = against(self.fake, microsoft.OneDriveDriver)
        settings = {"client_id": "app-id", "client_secret": "app-secret"}
        code = self.fake.grant_code("https://h/cb")
        saved = cls.exchange_code(settings, code=code, redirect_uri="https://h/cb")
        self.assertEqual(list(saved), ["refresh_token"])
        self.assertIn(saved["refresh_token"], self.fake.refresh_tokens)
        # The code works once, and only with its redirect URI.
        with self.assertRaises(AuthError) as caught:
            cls.exchange_code(settings, code=code, redirect_uri="https://h/cb")
        self.assertIn("AADSTS70000", caught.exception.message)
        with self.assertRaises(AuthError):
            cls.exchange_code(settings, code=self.fake.grant_code("https://h/cb"), redirect_uri="https://h/other")

    def test_consent_page_of_the_fake(self):
        # The fake's consent page (for a browser on a site using it) gives a code the exchange takes.
        cls = against(self.fake, microsoft.OneDriveDriver)
        settings = {"client_id": "app-id", "client_secret": "app-secret"}
        url = cls.authorize_url(settings, redirect_uri="https://h/cb?x=1", state="st&ate")
        response = httpx.get(url)
        self.assertEqual(response.status_code, 302)
        location = urllib.parse.urlsplit(response.headers["Location"])
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual((location.netloc, location.path, query["x"], query["state"]), ("h", "/cb", ["1"], ["st&ate"]))
        saved = cls.exchange_code(settings, code=query["code"][0], redirect_uri="https://h/cb?x=1")
        self.assertIn(saved["refresh_token"], self.fake.refresh_tokens)
        self.assertEqual(httpx.get(url.replace("client_id=app-id", "client_id=other")).status_code, 400)
        self.fake.deny_consent = True
        self.addCleanup(setattr, self.fake, "deny_consent", False)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(httpx.get(url).headers["Location"]).query)
        self.assertEqual((query["error"], query["state"]), (["access_denied"], ["st&ate"]))
        self.assertNotIn("code", query)

    def test_wrong_secret(self):
        driver = self.make_driver(client_secret="wrong-secret-value")
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("AADSTS7000215", caught.exception.message)
        self.assertNotIn("wrong-secret-value", caught.exception.message)
        self.assertNotIn("Trace ID", caught.exception.message)
        # The refusal is remembered for a while: the next request doesn't ask Microsoft again.
        with self.assertRaises(AuthError):
            driver.stat("/")
        self.assertEqual(len(self.token_requests()), 1)

    def test_throttled_token_endpoint_is_not_an_auth_failure(self):
        driver = self.make_driver()
        self.addCleanup(driver.close)
        self.fake.throttle_tokens = 1
        self.addCleanup(setattr, self.fake, "throttle_tokens", 0)
        with self.assertRaises(BackendError) as caught:
            driver.list("/")
        self.assertNotIsInstance(caught.exception, AuthError)
        self.assertEqual(driver.list("/"), [])  # not remembered as a refusal

    def test_missing_refresh_token(self):
        driver = self.make_driver(refresh_token="")
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("授权", caught.exception.message)

    def test_refresh_token_rotation_is_saved(self):
        old = self.d.config["refresh_token"]
        self.d.list("/")
        self.assertEqual(len(self.updates), 1)
        new = self.updates[0]["refresh_token"]
        self.assertNotEqual(new, old)
        self.assertEqual(self.d.config["refresh_token"], new)
        self.d.list("/")
        self.assertEqual(len(self.token_requests()), 1)  # the access token is reused

    def test_expired_access_token_is_refreshed(self):
        self.put("/a.txt", b"a")
        self.fake.expire_access_tokens()
        self.fake.requests.clear()
        self.assertEqual(self.read("/a.txt"), b"a")
        self.assertEqual(len(self.token_requests()), 1)

    def test_one_refresh_for_many_threads(self):
        driver = self.make_driver()
        self.addCleanup(driver.close)
        self.fake.requests.clear()
        errors = []

        def work():
            try:
                driver.list("/")
            except Exception as error:  # pragma: no cover - shown below
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.token_requests()), 1)

    # -- listing

    def test_paging(self):
        self.fake.page_size = 3
        self.addCleanup(setattr, self.fake, "page_size", 1000)
        names = [f"f{i:02d}" for i in range(10)]
        for name in names:
            self.put("/" + name, name.encode())
        self.fake.requests.clear()
        self.assertEqual(sorted(e.name for e in self.d.list("/")), names)
        pages = [r for r in self.fake.requests if r[1].endswith("/children")]
        self.assertEqual(len(pages), 4)

    def test_next_link_elsewhere_is_refused(self):
        self.fake.page_size = 1
        self.fake.next_link_base = "http://127.0.0.2:9"
        self.addCleanup(setattr, self.fake, "page_size", 1000)
        self.addCleanup(setattr, self.fake, "next_link_base", None)
        self.put("/a", b"a")
        self.put("/b", b"b")
        with self.assertRaises(BackendError):
            self.d.list("/")

    def test_entries(self):
        self.put("/f.bin", b"12345")
        self.d.mkdir("/dir")
        entries = {e.name: e for e in self.d.list("/")}
        self.assertEqual((entries["f.bin"].size, entries["f.bin"].is_dir), (5, False))
        self.assertEqual((entries["dir"].size, entries["dir"].is_dir), (None, True))
        self.assertIsNotNone(entries["f.bin"].modified.tzinfo)
        root = self.d.stat("/")
        self.assertEqual((root.name, root.path, root.is_dir), ("", "/", True))

    # -- confinement and names

    def test_confined_to_base(self):
        drive = self.fake.drive("me")
        with self.fake.lock:
            outside = drive.add(drive.root, f"outside{next(_bases)}", True)
            drive.add(outside, "secret.txt", False, b"secret")
        odd = "..%2F" + outside.name
        self.put(f"/{odd}/x.txt", b"inside")
        self.assertEqual(self.d.list("/")[0].name, odd)
        self.assertEqual(self.read(f"/{odd}/x.txt"), b"inside")
        self.assertEqual(sorted(outside.children), ["secret.txt"])
        for name in ["a%20b", "50%", "x#y", "p+q", "it's", "(1)", "a&b=c", "ünïcode", "a;b"]:
            with self.subTest(name=name):
                self.put("/" + name, name.encode())
                self.assertIn(name, [e.name for e in self.d.list("/")])
                self.assertEqual(self.read("/" + name), name.encode())

    def test_graph_name_rules(self):
        for name in [" lead", "trail ", "CON", "aux", "desktop.ini", "~$lock.docx", "a_vti_b", "a:b", "a|b", 'a"b']:
            with self.subTest(name=name):
                with self.assertRaises(InvalidPath):
                    self.d.mkdir("/" + name)
                with self.assertRaises(InvalidPath):
                    self.put("/" + name, b"x")
        self.put("/ok.txt", b"x")
        with self.assertRaises(InvalidPath):
            self.d.rename("/ok.txt", "nul")

    def test_names_onedrive_cant_have_are_not_asked_for(self):
        # Graph's own reading of such segments ("\" as "/", a decoded ":" ending the path, dots
        # trimmed) is never put to the test: they give NotFound before any request.
        self.put("/real.txt", b"r")
        self.fake.requests.clear()
        for path in ["/a\\..\\..\\x", "/a:b", "/...", "/. .", "/.. ", "/x/ ../y", "/q?x", '/"', "/a|b"]:
            with self.subTest(path=path):
                with self.assertRaises(NotFound):
                    self.d.stat(path)
                with self.assertRaises(NotFound):
                    self.d.list(path)
                with self.assertRaises(NotFound):
                    self.d.open(path)
        self.assertEqual(self.fake.requests, [])

    def test_overwrite_goes_by_item_id(self):
        self.put("/same.txt", b"one")
        item_id = self.fake.drive("me").find(self.d.config["base_path"] + "/same.txt").id
        self.fake.requests.clear()
        self.put("/same.txt", b"two")
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        self.put("/same.txt", b"three" * 70000)
        puts = [p for m, p, _ in self.fake.requests if p.endswith(("/content", "/createUploadSession"))]
        self.assertEqual(puts, [f"/v1.0/me/drive/items/{item_id}/content",
                                f"/v1.0/me/drive/items/{item_id}/createUploadSession"])
        self.assertEqual(self.read("/same.txt"), b"three" * 70000)

    def test_items_of_other_drives_are_left_out(self):
        drive = self.fake.drive("me")
        base = drive.find(self.d.config["base_path"])
        with self.fake.lock:
            shared = drive.add(base, "Shared with me", True)
            shared.remote = True
        self.put("/mine.txt", b"m")
        self.assertEqual([e.name for e in self.d.list("/")], ["mine.txt"])
        self.assertEqual(self.d.search("/", "shared", 10), [])

    def test_root_is_protected(self):
        for call in (lambda: self.d.delete("/"), lambda: self.d.rename("/", "x"),
                     lambda: self.d.move("/", "/a"), lambda: self.d.copy("/", "/a")):
            with self.assertRaises(DriverError):
                call()

    def test_missing_base_path(self):
        driver = self.make_driver(base_path="/no such folder")
        self.addCleanup(driver.close)
        with self.assertRaises(NotFound) as caught:
            driver.check()
        self.assertIn("基路径", caught.exception.message)

    # -- reading

    def test_range_and_early_close(self):
        data = os.urandom(300_000)
        self.put("/r.bin", data)
        stream = self.d.open("/r.bin", 1000, 5000)
        self.assertEqual((stream.offset, stream.size, stream.total), (1000, 5000, 300_000))
        self.assertEqual(stream.read(), data[1000:6000])
        self.assertEqual(self.read("/r.bin", 299_990), data[299_990:])
        downloads = [r for r in self.fake.requests if r[1].startswith("/download/")]
        self.assertTrue(downloads)
        self.assertFalse(any(has_auth for _, _, has_auth in downloads))

    def test_download_url_and_domain(self):
        self.put("/d.txt", b"direct")
        url = self.d.download_url("/d.txt", filename="d.txt")
        response = httpx.get(url)
        self.assertEqual(response.content, b"direct")
        self.assertIn("attachment", response.headers["content-disposition"])
        driver = self.make_driver(base_path=self.d.config["base_path"], download_domain="https://dl.example.com:8443")
        self.addCleanup(driver.close)
        proxied = httpx.URL(driver.download_url("/d.txt", filename="d.txt"))
        self.assertEqual((proxied.scheme, proxied.host, proxied.port), ("https", "dl.example.com", 8443))
        self.assertTrue(proxied.path.startswith("/download/"))
        self.d.mkdir("/folder")
        with self.assertRaises(NotFound):
            self.d.download_url("/folder", filename="folder")

    def without_links_in_items(self, content_style="302"):
        """Graph as some accounts have it (seen on a real server): @microsoft.graph.downloadUrl left
        out of the item although $select names it."""
        self.fake.select_download_url = False
        self.fake.content_style = content_style
        self.addCleanup(setattr, self.fake, "select_download_url", True)
        self.addCleanup(setattr, self.fake, "content_style", "302")

    def test_a_link_left_out_of_the_item_comes_from_content(self):
        data = os.urandom(300_000)
        self.put("/n.bin", data)
        self.without_links_in_items()
        self.fake.requests.clear()
        self.assertEqual(self.read("/n.bin"), data)
        self.assertEqual(self.read("/n.bin", 1000, 5000), data[1000:6000])
        url = self.d.download_url("/n.bin", filename="n.bin")
        self.assertTrue(httpx.URL(url).path.startswith("/download/"))
        self.assertEqual(httpx.get(url).content, data)
        # /content asked with our token; the link it points to fetched without it.
        contents = [r for r in self.fake.requests if r[0] == "GET" and r[1].endswith("/content")]
        downloads = [r for r in self.fake.requests if r[1].startswith("/download/")]
        self.assertEqual(len(contents), 3)
        self.assertTrue(all(has_auth for _, _, has_auth in contents))
        self.assertEqual(len(downloads), 3)
        self.assertFalse(any(has_auth for _, _, has_auth in downloads))
        driver = self.make_driver(base_path=self.d.config["base_path"], download_domain="https://dl.example.com")
        self.addCleanup(driver.close)
        self.assertEqual(httpx.URL(driver.download_url("/n.bin", filename="n.bin")).host, "dl.example.com")
        self.d.mkdir("/folder")
        with self.assertRaises(NotFound):
            self.d.download_url("/folder", filename="folder")
        with self.assertRaises(NotFound):
            self.d.open("/folder")

    def test_content_that_sends_the_bytes_itself(self):
        data = os.urandom(100_000)
        self.put("/b.bin", data)
        self.without_links_in_items("200")
        self.assertEqual(self.read("/b.bin"), data)
        self.assertEqual(self.read("/b.bin", 10, 20), data[10:30])
        self.assertIsNone(self.d.download_url("/b.bin", filename="b.bin"))  # no link: the site sends it

    def test_content_errors(self):
        self.put("/e.bin", b"data")
        self.without_links_in_items()
        # A token that stopped working: a new one, and the request again.
        self.fake.fail_content = [(401, "InvalidAuthenticationToken", "Access token has expired.")]
        before = len(self.token_requests())
        self.assertEqual(self.read("/e.bin"), b"data")
        self.assertEqual(len(self.token_requests()), before + 1)
        self.fake.fail_content = [(403, "accessDenied", "Access denied")]
        with self.assertRaises(DriverError) as caught:
            self.d.download_url("/e.bin", filename="e.bin")
        self.assertIn("accessDenied", caught.exception.message)
        self.fake.fail_content = [(404, "itemNotFound", "Gone")]
        with self.assertRaises(NotFound):
            self.read("/e.bin")
        # A redirect to something that isn't a web address.
        self.fake.content_location = "ftp://files.example.com/e.bin"
        self.addCleanup(setattr, self.fake, "content_location", None)
        for call in (lambda: self.read("/e.bin"), lambda: self.d.download_url("/e.bin", filename="e.bin")):
            with self.assertRaises(BackendError) as caught:
                call()
            self.assertIn("下载地址不对", caught.exception.message)
        self.assertEqual(self.fake.fail_content, [])

    # -- writing

    def test_upload_session_sends_no_token(self):
        self.d.small_upload_limit = 1024
        self.d.upload_chunk = 320 * 1024
        data = os.urandom(320 * 1024 * 2 + 100)
        self.put("/big.bin", data)
        self.assertEqual(self.read("/big.bin"), data)
        chunks = [r for r in self.fake.requests if r[1].startswith("/upload/")]
        self.assertEqual(len(chunks), 3)
        self.assertFalse(any(has_auth for _, _, has_auth in chunks))

    def test_the_downloads_own_size_counts(self):
        """Graph's size can differ from what the download has (SharePoint changes Office files, OneDrive's Live
        Photos): the stream promises what the download has, not more or less."""
        data = os.urandom(300_000)
        self.put("/s.bin", data)
        self.addCleanup(setattr, self.fake, "size_delta", 0)
        for delta in (1000, -1000):
            with self.subTest(delta=delta):
                self.fake.size_delta = delta
                stream = self.d.open("/s.bin")
                self.assertEqual((stream.size, stream.total), (len(data), len(data)))
                self.assertEqual(stream.read(), data)

    def test_content_is_asked_without_the_range(self):
        """Graph: the Range goes on the link /content redirects to, "not to the request for /content"."""
        data = os.urandom(100_000)
        self.put("/r.bin", data)
        for style in ("302", "200"):
            with self.subTest(style=style):
                self.without_links_in_items(style)
                self.fake.content_ranges.clear()
                self.assertEqual(self.read("/r.bin", 1000, 5000), data[1000:6000])
                self.assertEqual(self.fake.content_ranges, [None])

    def test_a_throttled_chunk_waits_as_long_as_asked(self):
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        self.addCleanup(setattr, self.fake, "chunk_failure", (503, None))
        data = os.urandom(400 * 1024)
        waits = []
        self.fake.fail_chunks, self.fake.chunk_failure = 1, (429, "7")
        with mock.patch.object(microsoft.time, "sleep", waits.append):
            self.put("/t.bin", data)
        self.assertEqual(waits, [7.0])
        self.assertEqual(self.read("/t.bin"), data)
        # Longer than throttle_wait: given up at once, and the session deleted.
        self.fake.fail_chunks, self.fake.chunk_failure = 1, (503, "3600")
        with mock.patch.object(microsoft.time, "sleep", waits.append), self.assertRaises(BackendError):
            self.put("/u.bin", data)
        self.assertEqual(waits, [7.0])
        self.assertEqual(self.fake.sessions, {})

    def test_a_name_held_by_an_unfinished_upload(self):
        """OneDrive for Business and SharePoint: an upload session holds the file's name until it ends, and Graph
        refuses another with 409 though nothing has the name. That isn't "already exists"."""
        self.fake.session_conflicts = 1
        with self.assertRaises(DriverError) as caught:
            self.d.upload_target("/held.bin", 1000)
        self.assertNotIsInstance(caught.exception, AlreadyExists)
        target = self.d.upload_target("/held.bin", 1000)
        self.assertEqual(httpx.delete(target.url).status_code, 204)  # as the browser does when it gives up
        self.assertEqual(self.fake.sessions, {})

    def test_upload_chunk_retried(self):
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        self.fake.fail_chunks = 2
        data = os.urandom(400 * 1024)
        self.put("/retry.bin", data)
        self.assertEqual(self.read("/retry.bin"), data)

    def test_lost_chunk_answer(self):
        # The fake stores a chunk but drops the connection: the retry gets 416, the session's
        # status says the chunk arrived, and the upload goes on.
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        self.fake.lose_chunk_answers = 1
        data = os.urandom(320 * 1024 * 3 + 5)
        self.put("/lost.bin", data)
        self.assertEqual(self.read("/lost.bin"), data)
        statuses = [r for r in self.fake.requests if r[0] == "GET" and r[1].startswith("/upload/")]
        self.assertEqual(len(statuses), 1)

    def test_lost_final_answer(self):
        # The last chunk is stored but its answer is lost: the retry finds the session gone, and
        # write() looks at the file instead of reporting a failure.
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        data = os.urandom(320 * 1024 + 10)
        self.fake.lose_final_answers = 1
        self.put("/final.bin", data)
        self.assertEqual(self.read("/final.bin"), data)
        again = os.urandom(len(data))  # the same size: the eTag tells the new content apart
        self.fake.lose_final_answers = 1
        self.put("/final.bin", again)
        self.assertEqual(self.read("/final.bin"), again)
        # Gone without the file: still an error.
        self.fake.lose_final_answers = 1
        self.fake.discard_final = True
        self.addCleanup(setattr, self.fake, "discard_final", False)
        with self.assertRaises(BackendError):
            self.put("/never.bin", data)
        self.fake.lose_final_answers = 1
        with self.assertRaises(BackendError):
            self.put("/final.bin", os.urandom(len(data)))
        self.assertEqual(self.read("/final.bin"), again)

    def test_upload_failure_cancels_session(self):
        self.d.small_upload_limit = 0
        self.fake.fail_chunks = 100
        self.addCleanup(setattr, self.fake, "fail_chunks", 0)
        with self.assertRaises(BackendError):
            self.put("/fail.bin", b"x" * 1000)
        self.assertIn("DELETE", [m for m, p, _ in self.fake.requests if p.startswith("/upload/")])
        self.assertEqual(self.fake.sessions, {})
        with self.assertRaises(NotFound):
            self.d.stat("/fail.bin")

    def test_a_lost_session_starts_the_upload_over(self):
        """Graph: "Handle 404 Not Found errors when doing resumable uploads by starting the entire upload over"."""
        self.d.small_upload_limit = 0
        self.d.upload_chunk = 320 * 1024
        data = os.urandom(3 * 320 * 1024 + 5)
        self.fake.drop_sessions = 1
        self.put("/again.bin", data)
        self.assertEqual(self.read("/again.bin"), data)
        sessions = [p for m, p, _ in self.fake.requests if m == "POST" and p.endswith("/createUploadSession")]
        self.assertEqual(len(sessions), 2)
        # Once only, and only with data that can be read again.
        self.fake.drop_sessions = 2
        self.addCleanup(setattr, self.fake, "drop_sessions", 0)
        with self.assertRaises(BackendError):
            self.put("/twice.bin", data)
        self.fake.drop_sessions = 1

        class Stream(io.RawIOBase):
            def __init__(self, raw):
                self._raw = io.BytesIO(raw)

            def readable(self):
                return True

            def readinto(self, buffer):
                return self._raw.readinto(buffer)

        with self.assertRaises(BackendError):
            self.d.write("/stream.bin", Stream(data), len(data))

    def test_throttled_download_links(self):
        data = os.urandom(1000)
        self.put("/t.bin", data)
        self.fake.throttle_downloads = 1  # Retry-After: 0
        self.assertEqual(self.read("/t.bin", 10, 20), data[10:30])  # waited and asked again
        self.fake.throttle_downloads = 2
        self.addCleanup(setattr, self.fake, "throttle_downloads", 0)
        with self.assertRaises(BackendError):
            self.read("/t.bin")

    def test_short_data_is_refused(self):
        with self.assertRaises(DriverError):
            self.d.write("/short.txt", io.BytesIO(b"abc"), 10)
        self.d.small_upload_limit = 0
        with self.assertRaises(DriverError):
            self.d.write("/short2.txt", io.BytesIO(b"abc"), 10)
        for name in ("short.txt", "short2.txt"):
            with self.assertRaises(NotFound):
                self.d.stat("/" + name)

    def test_upload_target(self):
        data = os.urandom(320 * 1024 + 7)
        target = self.d.upload_target("/new/sub/up.bin", len(data))
        self.assertEqual((target.kind, target.method), ("chunks", "PUT"))
        self.assertEqual(target.chunk_size % (320 * 1024), 0)
        # What the browser does: PUT the pieces with Content-Range and no credentials.
        for start in range(0, len(data), target.chunk_size):
            piece = data[start:start + target.chunk_size]
            response = httpx.put(target.url, content=piece, headers={
                "Content-Range": f"bytes {start}-{start + len(piece) - 1}/{len(data)}"})
            self.assertIn(response.status_code, (200, 201, 202))
        self.assertEqual(self.read("/new/sub/up.bin"), data)
        self.assertIsNone(self.d.upload_target("/empty", 0))
        self.d.mkdir("/taken")
        with self.assertRaises(AlreadyExists):
            self.d.upload_target("/taken", 10)

    def test_missing_parents_created_by_us(self):
        self.fake.auto_parents = False
        self.addCleanup(setattr, self.fake, "auto_parents", True)
        self.put("/p/q/small.txt", b"small")
        self.d.small_upload_limit = 0
        self.put("/r/s/session.txt", b"session")
        self.assertEqual(self.read("/p/q/small.txt"), b"small")
        self.assertEqual(self.read("/r/s/session.txt"), b"session")

    # -- errors

    def test_throttling(self):
        self.fake.throttle = 2
        self.assertEqual(self.d.list("/"), [])
        self.fake.throttle = 5
        self.addCleanup(setattr, self.fake, "throttle", 0)
        with self.assertRaises(BackendError) as caught:
            self.d.list("/")
        self.assertIn("限流", caught.exception.message)
        self.fake.throttle = 1
        self.fake.throttle_after = "3600"
        self.addCleanup(setattr, self.fake, "throttle_after", "0")
        with self.assertRaises(BackendError):
            self.d.list("/")

    def test_error_mapping(self):
        cases = [
            ((507, "insufficientStorage", "full"), DriverError, "空间不够"),
            ((403, "accessDenied", "You can't do that"), DriverError, "accessDenied"),
            ((500, "generalException", "boom"), BackendError, "暂时出错"),
            ((423, "resourceLocked", "locked"), DriverError, "锁定"),
            ((400, "invalidRequest", "Bad thing"), DriverError, "Bad thing"),
        ]
        for failure, error_class, text in cases:
            with self.subTest(failure=failure):
                self.fake.fail_next = [failure]
                with self.assertRaises(error_class) as caught:
                    self.d.stat("/")
                self.assertIn(text, caught.exception.message)
                self.assertNotIsInstance(caught.exception, (NotFound, AlreadyExists))

    def test_a_redirect_the_api_doesnt_make(self):
        """STOR-15: a 3xx was taken for success, its body for the answer: only a proxy in the way answers one
        to a Graph call (the downloads' /content redirects, which go on as they did)."""
        self.fake.fail_next = [(302, "found", "moved")]
        with self.assertRaises(BackendError) as caught:
            self.d.list("/")
        self.assertIn("HTTP 302", caught.exception.message)
        self.assertEqual(self.d.list("/"), [])

    def test_unreachable(self):
        cls = type("Down", (microsoft.OneDriveDriver,),
                   {"graph_root": "http://127.0.0.1:9", "login_root": "http://127.0.0.1:9"})
        driver = cls(config(self.fake))
        self.addCleanup(driver.close)
        with self.assertRaises(BackendError):
            driver.list("/")

    # -- copy

    def test_copy_waits_for_the_monitor(self):
        self.put("/src/a.txt", b"a")
        self.put("/src/sub/b.txt", b"b")
        self.d.mkdir("/dst")
        for style in ("303", "200", "item"):
            with self.subTest(style=style):
                self.fake.monitor_style = style
                self.fake.copy_polls = 3
                self.addCleanup(setattr, self.fake, "copy_polls", 1)
                self.addCleanup(setattr, self.fake, "monitor_style", "303")
                self.d.copy("/src", "/dst")
                self.assertEqual(self.read("/dst/src/sub/b.txt"), b"b")
                self.d.delete("/dst/src")
        monitors = [r for r in self.fake.requests if r[1].startswith("/monitor/")]
        self.assertGreaterEqual(len(monitors), 12)
        self.assertFalse(any(has_auth for _, _, has_auth in monitors))

    def test_copy_failure_and_timeout(self):
        self.put("/a.txt", b"a")
        self.d.mkdir("/dst")
        self.fake.copy_error = ("accessDenied", "Not allowed here")
        self.addCleanup(setattr, self.fake, "copy_error", None)
        with self.assertRaises(DriverError) as caught:
            self.d.copy("/a.txt", "/dst")
        self.assertIn("Not allowed here", caught.exception.message)
        self.fake.copy_error = None
        self.fake.copy_polls = 10_000
        self.addCleanup(setattr, self.fake, "copy_polls", 1)
        self.d.copy_timeout = 0.2
        with self.assertRaises(BackendError) as caught:
            self.d.copy("/a.txt", "/dst")
        self.assertIn("后台", caught.exception.message)

    # -- search

    def test_search(self):
        drive = self.fake.drive("me")
        with self.fake.lock:
            drive.add(drive.root, f"Report outside {next(_bases)}.txt", False, b"x")
        self.put("/Report 2024.txt", b"r")
        self.put("/docs/annual report.pdf", b"r")
        self.put("/docs/deep/REPORTS/x.txt", b"x")
        self.put("/docs/notes.txt", b"this mentions report in the content only")
        self.put("/other/report-final.doc", b"r")
        self.put("/docs/it's a report.txt", b"q")
        found = {e.path: e for e in self.d.search("/", "report", 50)}
        self.assertEqual(sorted(found), ["/Report 2024.txt", "/docs/annual report.pdf", "/docs/deep/REPORTS",
                                         "/docs/it's a report.txt", "/other/report-final.doc"])
        self.assertTrue(found["/docs/deep/REPORTS"].is_dir)
        in_docs = sorted(e.path for e in self.d.search("/docs", "REPORT", 50))
        self.assertEqual(in_docs, ["/docs/annual report.pdf", "/docs/deep/REPORTS", "/docs/it's a report.txt"])
        self.assertEqual(len(self.d.search("/", "report", 2)), 2)
        self.assertEqual([e.path for e in self.d.search("/", "it's", 10)], ["/docs/it's a report.txt"])
        # OneDrive for Business leaves parentReference.path out: the driver walks up by id.
        self.fake.search_without_path = True
        self.addCleanup(setattr, self.fake, "search_without_path", False)
        self.fake.page_size = 2
        self.addCleanup(setattr, self.fake, "page_size", 1000)
        self.assertEqual(sorted(e.path for e in self.d.search("/docs", "report", 50)), in_docs)
        # Results without parentReference: each one is looked up by id.
        self.fake.search_without_parent = True
        self.addCleanup(setattr, self.fake, "search_without_parent", False)
        self.assertEqual(sorted(e.path for e in self.d.search("/docs", "report", 50)), in_docs)

    def test_search_ignores_case_of_paths(self):
        # OneDrive paths ignore case: a base path typed as /docs for the folder "Docs" lists it,
        # and search must find its files too.
        drive = self.fake.drive("me")
        name = f"Docs{next(_bases)}"
        with self.fake.lock:
            folder = drive.add(drive.root, name, True)
            sub = drive.add(folder, "Sub", True)
            drive.add(sub, "case report.txt", False, b"r")
        driver = self.make_driver(base_path="/" + name.lower())
        self.addCleanup(driver.close)
        self.assertEqual([e.name for e in driver.list("/")], ["Sub"])
        self.assertEqual([e.path for e in driver.search("/", "case report", 10)], ["/Sub/case report.txt"])
        self.assertEqual([e.path for e in driver.search("/sub", "case report", 10)], ["/sub/case report.txt"])
        self.assertEqual(driver.search("/other", "case report", 10), [])

    # -- params

    def test_validate_config(self):
        good = {"base_path": "/a", "tenant": "contoso.onmicrosoft.com", "download_domain": "https://dl.example.com"}
        self.assertEqual(microsoft.OneDriveDriver.validate_config(good), {})
        errors = microsoft.OneDriveDriver.validate_config(
            {"base_path": "/a/../b", "tenant": "x/y", "download_domain": "ftp://dl.example.com/path"})
        self.assertEqual(sorted(errors), ["base_path", "download_domain", "tenant"])
        sp = microsoft.SharePointDriver
        self.assertEqual(sp.validate_config({"site": SITE_ID, "drive_id": "b!abc-_9"}), {})
        self.assertEqual(sp.validate_config({"site": "https://contoso.sharepoint.com/sites/team"}), {})
        for site in ["contoso", "http://contoso.sharepoint.com/sites/x", "https://contoso.sharepoint.com/sites/x?y=1",
                     "https://user@contoso.sharepoint.com/", "https://contoso.sharepoint.com/sites/../x"]:
            with self.subTest(site=site):
                self.assertIn("site", sp.validate_config({"site": site}))
        self.assertIn("drive_id", sp.validate_config({"site": SITE_ID, "drive_id": "../me"}))
        for cls in (microsoft.OneDriveDriver, microsoft.OneDriveChinaDriver, microsoft.SharePointDriver,
                    microsoft.SharePointChinaDriver):
            secrets = {p.key for p in cls.params if p.secret}
            self.assertEqual(secrets, {"client_secret", "refresh_token"})
        self.assertEqual(microsoft.OneDriveChinaDriver.graph_root, "https://microsoftgraph.chinacloudapi.cn")
        self.assertEqual(microsoft.SharePointChinaDriver.login_root, "https://login.partner.microsoftonline.cn")

    def test_a_long_content_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        self.put("/e.bin", b"data")
        self.without_links_in_items()
        body = LongBody(b'{"error": {"code": "accessDenied", "message": "Access denied"}}')
        answer = lambda request: (Response(403, body, JSON, length=LONG)
                                  if request.method == "GET" and request.path.endswith("/content") else None)
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "accessDenied"):
            self.d.download_url("/e.bin", filename="e.bin")
        self.assertLess(body.sent, LONG // 4)


class SharePointTests(_FakeMixin, unittest.TestCase):
    driver_class = microsoft.SharePointDriver

    def test_helpers(self):
        cls = against(self.fake, microsoft.SharePointDriver)
        settings = config(self.fake)
        sites = cls.helper("sites", settings, {})
        self.assertEqual(sites["param"], "site")
        values = [choice["value"] for choice in sites["choices"]]
        self.assertEqual(values[0], OTHER_SITE_ID)  # the root site first, once
        self.assertEqual(sorted(values), sorted([SITE_ID, OTHER_SITE_ID]))
        self.assertTrue(any("sites/team" in choice["label"] for choice in sites["choices"]))
        drives = cls.helper("drives", {**settings, "site": "https://contoso.sharepoint.com/sites/team"}, {})
        self.assertEqual(drives["param"], "drive_id")
        self.assertEqual([c["value"] for c in drives["choices"]],
                         ["", self.fake.drive("docs").id, self.fake.drive("archive").id])
        self.assertIn("message", cls.helper("drives", {**settings, "site": ""}, {}))
        with self.assertRaises(AuthError):
            cls.helper("sites", {**settings, "refresh_token": ""}, {})
        with self.assertRaises(DriverError):
            cls.helper("nope", settings, {})

    def test_site_and_library(self):
        cls = against(self.fake, microsoft.SharePointDriver)
        by_id = cls(config(self.fake, site=SITE_ID))
        self.addCleanup(by_id.close)
        by_id.write("/in-docs.txt", io.BytesIO(b"docs"), 4)
        self.assertIn("in-docs.txt", self.fake.drive("docs").root.children)
        archive = cls(config(self.fake, site=SITE_ID, drive_id=self.fake.drive("archive").id))
        self.addCleanup(archive.close)
        archive.write("/in-archive.txt", io.BytesIO(b"arch"), 4)
        self.assertIn("in-archive.txt", self.fake.drive("archive").root.children)
        self.assertEqual([e.name for e in archive.list("/")], ["in-archive.txt"])
        missing = cls(config(self.fake, site="https://contoso.sharepoint.com/sites/nope"))
        self.addCleanup(missing.close)
        with self.assertRaises(NotFound):
            missing.list("/")
        wrong_library = cls(config(self.fake, site=SITE_ID, drive_id="b!no-such-library"))
        self.addCleanup(wrong_library.close)
        with self.assertRaises(NotFound) as caught:
            wrong_library.check()
        self.assertIn("文档库", caught.exception.message)

    def test_a_link_left_out_of_the_item_comes_from_content(self):
        cls = against(self.fake, microsoft.SharePointDriver)
        driver = cls(config(self.fake, site=SITE_ID))
        self.addCleanup(driver.close)
        driver.write("/sp-link.txt", io.BytesIO(b"from sharepoint"), 15)
        self.fake.select_download_url = False
        self.addCleanup(setattr, self.fake, "select_download_url", True)
        self.assertEqual(driver.open("/sp-link.txt").read(), b"from sharepoint")
        url = driver.download_url("/sp-link.txt", filename="sp-link.txt")
        self.assertEqual(httpx.get(url).content, b"from sharepoint")


if __name__ == "__main__":
    unittest.main()
