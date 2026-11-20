"""Tests of the Dropbox driver against fake_dropbox."""

from __future__ import annotations

import io
import itertools
import json
import os
import threading
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages.drivers import dropbox
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_dropbox import TEAM_NS, FakeDropbox

JSON = {"Content-Type": "application/json"}

_bases = itertools.count(1)


def against(fake: FakeDropbox, cls: type = dropbox.DropboxDriver) -> type:
    """The driver class pointed at the fake, with short waits."""
    return type(cls.__name__, (cls,), {
        "api_root": fake.api_url, "content_root": fake.content_url,
        "authorize_endpoint": fake.api_url + "/oauth2/authorize", "retry_delay": 0.01,
    })


def config(fake: FakeDropbox, base: str = "/", **extra) -> dict:
    return {"app_key": fake.app_key, "app_secret": fake.app_secret, "refresh_token": fake.issue_refresh_token(),
            "base_path": base, **extra}


class _Stream:
    """Data that can't seek back, as a pipe."""

    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(size)


class _FakeMixin:
    team_space = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeDropbox(team_space=cls.team_space)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_base(self) -> str:
        """A fresh, empty folder of the fake account to be the storage root."""
        name = f"t{next(_bases)} 基"
        self.fake.put("/" + name, folder=True)
        return "/" + name

    def make_driver(self, **overrides):
        settings = config(self.fake, self.new_base())
        settings.update(overrides)
        self.updates = []
        return against(self.fake)(settings, on_update=self.updates.append)


class DropboxContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    pass


class SessionContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """The contract again with every non-empty write through an upload session, in small pieces."""

    def make_driver(self, **overrides):
        driver = super().make_driver(**overrides)
        driver.single_upload_limit = 0
        driver.upload_chunk = 256 * 1024
        return driver


class RootContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """base_path "/": the account's root, "" in the API, emptied before every test."""

    def make_driver(self, **overrides):
        with self.fake.lock:
            for child in list(self.fake.home.children.values()):
                child.detach()
        return against(self.fake)(config(self.fake, "/"))


class TeamSpaceContractTests(_FakeMixin, DriverContract, unittest.TestCase):
    """An account of a team with a team space: every call rooted at the team space (Dropbox-API-Path-Root), the
    base path a folder there, outside the member's own folder."""

    team_space = True


class DropboxTests(_FakeMixin, unittest.TestCase):
    def setUp(self):
        self.d = self.make_driver()
        self.addCleanup(self.d.close)
        self.fake.requests.clear()

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def read(self, path, offset=0, length=None):
        return self.d.open(path, offset, length).read()

    def full(self, path):
        return self.d.config["base_path"] + path

    def knob(self, name, value):
        old = getattr(self.fake, name)
        setattr(self.fake, name, value)
        self.addCleanup(setattr, self.fake, name, old)

    # -- OAuth

    def test_authorize_url(self):
        url = dropbox.DropboxDriver.authorize_url(
            {"app_key": "key123"}, redirect_uri="https://files.example.com/storages/oauth/callback/", state="st&ate")
        split = urllib.parse.urlsplit(url)
        self.assertEqual(f"{split.scheme}://{split.netloc}{split.path}", "https://www.dropbox.com/oauth2/authorize")
        self.assertEqual(urllib.parse.parse_qs(split.query), {
            "client_id": ["key123"], "response_type": ["code"], "token_access_type": ["offline"],
            "redirect_uri": ["https://files.example.com/storages/oauth/callback/"], "state": ["st&ate"]})
        with self.assertRaises(DriverError):
            dropbox.DropboxDriver.authorize_url({"app_key": " "}, redirect_uri="https://h/cb", state="s")

    def test_code_exchange(self):
        cls = against(self.fake)
        app = {"app_key": self.fake.app_key, "app_secret": self.fake.app_secret}
        callback = "https://files.example.com/storages/oauth/callback/"
        # The fake's authorize page allows at once and sends the browser back with a code.
        response = httpx.get(cls.authorize_url(app, redirect_uri=callback, state="st&ate"))
        self.assertEqual(response.status_code, 302)
        location = urllib.parse.urlsplit(response.headers["location"])
        query = urllib.parse.parse_qs(location.query)
        self.assertEqual((location.path, query["state"]), ("/storages/oauth/callback/", ["st&ate"]))
        saved = cls.exchange_code(app, code=query["code"][0], redirect_uri=callback)
        self.assertEqual(list(saved), ["refresh_token"])
        self.assertIn(saved["refresh_token"], self.fake.refresh_tokens)
        # A code works once, and only with its redirect URI.
        with self.assertRaises(AuthError) as caught:
            cls.exchange_code(app, code=query["code"][0], redirect_uri=callback)
        self.assertIn("invalid_grant", caught.exception.message)
        with self.assertRaises(AuthError):
            cls.exchange_code(app, code=self.fake.grant_code(callback), redirect_uri="https://other.example.com/cb")
        with self.assertRaises(AuthError) as caught:
            cls.exchange_code({**app, "app_secret": "wrong-secret"}, code=self.fake.grant_code(callback),
                              redirect_uri=callback)
        self.assertIn("invalid_client", caught.exception.message)
        self.assertNotIn("wrong-secret", caught.exception.message)
        # Without token_access_type=offline Dropbox gives no refresh token: nothing to save.
        with self.assertRaises(AuthError):
            cls.exchange_code(app, code=self.fake.grant_code(callback, offline=False), redirect_uri=callback)

    def test_refresh_and_saving_the_token(self):
        refresh_token = self.d.config["refresh_token"]
        self.d.list("/")
        self.d.stat("/")
        self.d.list("/")
        # One access token from the saved refresh token, used by every call after it.
        self.assertEqual(len(self.fake.token_requests()), 1)
        # Dropbox keeps the refresh token (its answer has none): nothing new to save.
        self.assertEqual(self.updates, [])
        self.assertEqual(self.d.config["refresh_token"], refresh_token)
        self.assertIn(refresh_token, self.fake.refresh_tokens)

    def test_one_refresh_for_many_threads(self):
        errors = []

        def work():
            try:
                self.d.list("/")
            except Exception as error:  # pragma: no cover - shown below
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(self.fake.token_requests()), 1)
        self.assertEqual(len(self.fake.calls("/2/users/get_current_account")), 1)

    def test_dead_refresh_token(self):
        driver = self.make_driver(refresh_token="rt-revoked")
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("invalid_grant", caught.exception.message)
        # The refusal is remembered a while: the next call doesn't ask Dropbox again.
        with self.assertRaises(AuthError):
            driver.stat("/")
        self.assertEqual(len(self.fake.token_requests()), 1)
        missing = self.make_driver(refresh_token="")
        self.addCleanup(missing.close)
        with self.assertRaises(AuthError):
            missing.list("/")
        self.assertEqual(len(self.fake.token_requests()), 1)

    def test_a_token_expiring_mid_call(self):
        self.put("/a.txt", b"abc")
        self.fake.expire_access_tokens()
        self.assertEqual(self.read("/a.txt"), b"abc")  # a content route: 401 expired_access_token, a new token
        self.assertEqual(len(self.fake.token_requests()), 2)
        self.fake.expire_access_tokens()
        self.assertEqual([e.name for e in self.d.list("/")], ["a.txt"])  # an RPC route
        self.assertEqual(len(self.fake.token_requests()), 3)
        self.fake.expire_access_tokens()
        self.put("/b.txt", b"bb")  # a streamed upload, sent again from where the data was
        self.assertEqual(self.read("/b.txt"), b"bb")
        self.assertEqual(len(self.fake.calls("/2/files/upload")), 3)
        # Data that can't seek back can't be sent again: an error, but the next call has a new token.
        self.fake.expire_access_tokens()
        with self.assertRaises(BackendError) as caught:
            self.d.write("/c.txt", _Stream(b"cc"), 2)
        self.assertNotIsInstance(caught.exception, AuthError)
        self.d.write("/c.txt", _Stream(b"cc"), 2)
        self.assertEqual(self.read("/c.txt"), b"cc")

    def test_other_refusals_of_the_token(self):
        self.d.list("/")
        self.fake.fail_next = [(401, ("invalid_access_token",))]
        with self.assertRaises(AuthError) as caught:
            self.d.list("/")
        self.assertIn("invalid_access_token", caught.exception.message)
        self.fake.fail_next = [(401, ("missing_scope",), {"required_scope": "files.content.write"})]
        with self.assertRaises(AuthError) as caught:
            self.put("/x.txt", b"x")
        self.assertIn("files.content.write", caught.exception.message)
        self.assertEqual(len(self.fake.token_requests()), 1)  # neither is an expired token

    # -- listing

    def test_cursor_paging(self):
        self.knob("page_size", 3)
        names = [f"f{i:02d}" for i in range(10)]
        for name in names:
            self.fake.put(self.full("/" + name), name.encode())
        self.assertEqual(sorted(e.name for e in self.d.list("/")), names)
        self.assertEqual(self.fake.calls("/2/files/list_folder")[0]["rpc"],
                         {"path": self.d.config["base_path"], "limit": 2000})
        self.assertEqual(len(self.fake.calls("/2/files/list_folder/continue")), 3)
        # A cursor given twice would page for ever.
        self.knob("stuck_cursor", True)
        with self.assertRaises(BackendError):
            self.d.list("/")

    def test_entries(self):
        self.put("/f.bin", b"12345")
        self.d.mkdir("/dir")
        entries = {e.name: e for e in self.d.list("/")}
        self.assertEqual((entries["f.bin"].size, entries["f.bin"].is_dir), (5, False))
        self.assertEqual((entries["dir"].size, entries["dir"].is_dir, entries["dir"].modified), (None, True, None))
        self.assertIsNotNone(entries["f.bin"].modified.tzinfo)
        self.assertEqual(self.d.stat("/f.bin").size, 5)
        root = self.d.stat("/")
        self.assertEqual((root.name, root.path, root.is_dir), ("", "/", True))

    def test_files_dropbox_cant_download_are_left_out(self):
        self.fake.put(self.full("/Plan.gdoc"), b"{}", downloadable=False)
        self.put("/plain.txt", b"p")
        self.assertEqual([e.name for e in self.d.list("/")], ["plain.txt"])
        with self.assertRaises(NotFound):
            self.d.stat("/Plan.gdoc")
        self.assertEqual(self.d.search("/", "plan", 10), [])
        with self.assertRaises(DriverError) as caught:
            self.d.open("/Plan.gdoc")
        self.assertNotIsInstance(caught.exception, (NotFound, BackendError))

    # -- names and paths

    def test_chinese_names_in_the_header(self):
        name = "中文 名字.txt"
        self.put("/目录/" + name, "内容".encode())
        self.assertEqual(self.read("/目录/" + name), "内容".encode())
        self.assertEqual([e.name for e in self.d.list("/目录")], [name])
        self.d.rename("/目录/" + name, "新 名字.txt")
        self.assertEqual(self.read("/目录/新 名字.txt"), "内容".encode())
        # The content routes' arguments went in the Dropbox-API-Arg header, ASCII with \uXXXX escapes.
        args = [r["arg"] for r in self.fake.requests if r["arg"] is not None]
        self.assertEqual(len(args), 3)  # the upload and the two downloads
        for raw in args:
            self.assertTrue(raw.isascii(), raw)
            self.assertTrue(json.loads(raw)["path"].startswith(self.full("/目录/")), raw)
        self.assertIn("\\u76ee\\u5f55/\\u4e2d\\u6587 \\u540d\\u5b57.txt", args[0])
        self.assertEqual(json.loads(args[0])["path"], self.full("/目录/" + name))
        # Dropbox's example of the encoding (docs/technical-reference/json-encoding).
        self.assertEqual(dropbox._api_arg({"field": "some_üñîcødé_and_\x7f"}),
                         '{"field":"some_\\u00fc\\u00f1\\u00eec\\u00f8d\\u00e9_and_\\u007f"}')
        # The fake refuses a header that isn't ASCII, as Dropbox asks.
        response = httpx.post(self.fake.content_url + "/2/files/download",
                              headers={"Dropbox-API-Arg": '{"path":"/中"}'.encode("utf-8")})
        self.assertEqual(response.status_code, 400)

    def test_paths_ignore_case(self):
        self.put("/Docs/Report.txt", b"r")
        self.assertEqual(self.read("/docs/REPORT.TXT"), b"r")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/DOCS")
        self.assertEqual([e.name for e in self.d.list("/")], ["Docs"])

    def test_case_only_rename(self):
        # Dropbox refuses to move a path to itself in other case (the fake does too): through a third name.
        self.put("/Report.txt", b"r")
        self.fake.requests.clear()
        self.d.rename("/Report.txt", "report.txt")
        self.assertEqual([e.name for e in self.d.list("/")], ["report.txt"])
        self.assertEqual(self.read("/report.txt"), b"r")
        self.assertEqual(len(self.fake.calls("/2/files/move_v2")), 2)
        self.d.rename("/report.txt", "report.txt")  # the same name: nothing to do
        self.assertEqual(len(self.fake.calls("/2/files/move_v2")), 2)

    def test_names_ending_with_whitespace(self):
        for call in (lambda: self.d.mkdir("/a "), lambda: self.put("/b ", b"x"),
                     lambda: self.d.upload_target("/c ", 1)):
            with self.assertRaises(InvalidPath):
                call()
        self.put("/ok", b"x")
        with self.assertRaises(InvalidPath):
            self.d.rename("/ok", "d ")
        self.put("/ e", b"leading spaces are fine")
        self.assertEqual(sorted(e.name for e in self.d.list("/")), [" e", "ok"])

    def test_root_is_protected(self):
        for call in (lambda: self.d.delete("/"), lambda: self.d.rename("/", "x"),
                     lambda: self.d.move("/", "/a"), lambda: self.d.copy("/", "/a")):
            with self.assertRaises(DriverError):
                call()
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/")
        with self.assertRaises(AlreadyExists):
            self.put("/", b"x")
        with self.assertRaises(NotFound):
            self.d.open("/")

    def test_moves_and_copies(self):
        self.put("/a.txt", b"a")
        self.d.mkdir("/dst")
        self.fake.requests.clear()
        self.d.move("/a.txt", "/dst")
        self.d.copy("/dst/a.txt", "/")
        self.assertEqual([r["rpc"] for r in self.fake.calls("/2/files/move_v2") + self.fake.calls("/2/files/copy_v2")], [
            {"from_path": self.full("/a.txt"), "to_path": self.full("/dst/a.txt"), "autorename": False},
            {"from_path": self.full("/dst/a.txt"), "to_path": self.full("/a.txt"), "autorename": False}])
        # Into the folder it is in: a move has nothing to do, a copy finds its own name taken.
        self.d.move("/a.txt", "/")
        with self.assertRaises(AlreadyExists):
            self.d.copy("/a.txt", "/")
        with self.assertRaises(NotADirectory):
            self.d.move("/a.txt", "/dst/a.txt")
        self.assertEqual(sorted(e.name for e in self.d.list("/")), ["a.txt", "dst"])

    def test_missing_base_path(self):
        driver = self.make_driver(base_path="/no such folder")
        self.addCleanup(driver.close)
        with self.assertRaises(NotFound) as caught:
            driver.check()
        self.assertIn("/no such folder", caught.exception.message)
        self.fake.put("/a file", b"x")
        driver = self.make_driver(base_path="/a file")
        self.addCleanup(driver.close)
        with self.assertRaises(NotADirectory):
            driver.check()

    # -- uploads

    def test_session_upload(self):
        self.d.single_upload_limit = 1000
        self.d.upload_chunk = 64 * 1024
        data = os.urandom(64 * 1024 * 3 + 5)
        self.put("/big.bin", data)
        self.assertEqual(self.read("/big.bin"), data)
        starts = self.fake.calls("/2/files/upload_session/start")
        appends = self.fake.calls("/2/files/upload_session/append_v2")
        finishes = self.fake.calls("/2/files/upload_session/finish")
        self.assertEqual((len(starts), len(appends), len(finishes)), (1, 4, 1))
        self.assertEqual((starts[0]["size"], finishes[0]["size"]), (0, 0))  # the data goes with the appends only
        self.assertEqual([json.loads(r["arg"])["cursor"]["offset"] for r in appends], [0, 65536, 131072, 196608])
        self.assertEqual([r["size"] for r in appends], [65536, 65536, 65536, 5])
        finish = json.loads(finishes[0]["arg"])
        self.assertEqual(finish["cursor"]["offset"], len(data))
        self.assertEqual(finish["commit"], {"path": self.full("/big.bin"), "mode": "overwrite", "autorename": False})
        # It replaces the file; a file up to the limit goes in one /files/upload.
        again = os.urandom(len(data))
        self.put("/big.bin", again)
        self.assertEqual(self.read("/big.bin"), again)
        self.fake.requests.clear()
        self.put("/small.txt", b"small")
        self.assertEqual([r["path"] for r in self.fake.requests if r["path"].startswith("/2/files/upload")],
                         ["/2/files/upload"])

    def test_lost_append_answer(self):
        self.d.single_upload_limit = 0
        self.d.upload_chunk = 64 * 1024
        self.fake.lose_appends = 1
        data = os.urandom(64 * 1024 * 2 + 1)
        self.put("/lost.bin", data)
        self.assertEqual(self.read("/lost.bin"), data)
        appends = self.fake.calls("/2/files/upload_session/append_v2")
        self.assertEqual([json.loads(r["arg"])["cursor"]["offset"] for r in appends], [0, 0, 65536, 131072])

    def test_lost_upload_answer(self):
        """An upload whose answer is lost is sent once more when the data can seek: for identical contents Dropbox
        writes nothing (WriteMode). From data that can't seek it can't be sent again."""
        self.fake.lose_uploads = 1
        self.put("/lost.txt", b"abc")
        self.assertEqual(self.read("/lost.txt"), b"abc")
        self.assertEqual(len(self.fake.calls("/2/files/upload")), 2)
        self.fake.lose_uploads = 1
        with self.assertRaises(BackendError):
            self.d.write("/stream.txt", _Stream(b"s"), 1)
        self.assertEqual(len(self.fake.calls("/2/files/upload")), 3)
        self.fake.lose_uploads = 0

    def test_a_file_larger_than_dropbox_takes(self):
        """An upload session grows to 350 GB at most (UploadSessionLookupError.too_large): not "upload again"."""
        self.d.single_upload_limit = 0
        self.d.upload_chunk = 64 * 1024
        self.fake.fail_next = [(409, ("too_large",), None, "/2/files/upload_session/append_v2")]
        with self.assertRaises(DriverError) as caught:
            self.put("/huge.bin", os.urandom(1000))
        self.assertNotIsInstance(caught.exception, BackendError)
        self.assertIn("350", caught.exception.message)

    def test_session_failures(self):
        self.d.single_upload_limit = 0
        self.d.upload_chunk = 64 * 1024
        data = os.urandom(64 * 1024 * 2)
        self.fake.fail_next = [(409, ("not_found",), None, "/2/files/upload_session/append_v2")]
        with self.assertRaises(BackendError) as caught:  # the session expired: not "the file doesn't exist"
            self.put("/expired.bin", data)
        self.assertNotIsInstance(caught.exception, NotFound)
        self.fake.fail_next = [(409, ("path", "conflict", "folder"), None, "/2/files/upload_session/finish")]
        with self.assertRaises(AlreadyExists):
            self.put("/taken.bin", data)
        for name in ("expired.bin", "taken.bin"):
            with self.assertRaises(NotFound):
                self.d.stat("/" + name)

    def test_short_data_is_refused(self):
        with self.assertRaises(DriverError):
            self.d.write("/short.txt", io.BytesIO(b"abc"), 10)
        self.d.single_upload_limit = 0
        with self.assertRaises(DriverError):
            self.d.write("/short2.txt", io.BytesIO(b"abc"), 10)
        for name in ("short.txt", "short2.txt"):
            with self.assertRaises(NotFound):
                self.d.stat("/" + name)

    def test_upload_target(self):
        data = os.urandom(10_000)
        target = self.d.upload_target("/up/中文 上传.bin", len(data))
        self.assertEqual((target.kind, target.method, dict(target.headers)),
                         ("single", "POST", {"Content-Type": "application/octet-stream"}))
        self.assertEqual(target.expires_in, self.d.upload_link_ttl)
        self.assertEqual(self.fake.calls("/2/files/get_temporary_upload_link")[0]["rpc"], {
            "commit_info": {"path": self.full("/up/中文 上传.bin"), "mode": "overwrite", "autorename": False},
            "duration": self.d.upload_link_ttl})
        self.assertFalse(any(token in target.url for token in self.fake.access_tokens))
        # What the browser does: one POST with the target's headers, no token.
        response = httpx.post(target.url, content=data, headers=dict(target.headers))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read("/up/中文 上传.bin"), data)  # the missing folder made too
        self.assertEqual(httpx.post(target.url, content=data, headers=dict(target.headers)).status_code, 410)  # once
        other = self.d.upload_target("/up/plain.txt", 5)
        self.assertEqual(httpx.post(other.url, content=b"plain", headers={"Content-Type": "text/plain"}).status_code, 400)
        # More than one request takes: none, the site sends it through write().
        self.assertIsNone(self.d.upload_target("/huge.bin", self.d.single_upload_limit + 1))

    # -- downloads

    def test_range(self):
        data = os.urandom(10_000)
        self.put("/r.bin", data)
        self.fake.requests.clear()
        stream = self.d.open("/r.bin", 1000, 50)
        self.assertEqual((stream.offset, stream.size, stream.total), (1000, 50, 10_000))
        self.assertEqual(stream.read(), data[1000:1050])
        self.assertEqual(self.read("/r.bin"), data)
        self.assertEqual([r["range"] for r in self.fake.calls("/2/files/download")], ["bytes=1000-1049", None])

    def test_temporary_link(self):
        data = os.urandom(5000)
        self.put("/dl/文件.bin", data)
        url = self.d.download_url("/dl/文件.bin", filename="another name.bin", expires=60)
        self.assertTrue(url.startswith(self.fake.content_url + "/apitl/1/"))
        self.assertEqual(self.fake.calls("/2/files/get_temporary_link")[0]["rpc"], {"path": self.full("/dl/文件.bin")})
        response = httpx.get(url)
        self.assertEqual(response.content, data)
        self.assertFalse(any(r["auth"] for r in self.fake.requests if r["path"].startswith("/apitl/")))
        self.d.mkdir("/folder")
        for path in ("/folder", "/nope.bin"):
            with self.assertRaises(NotFound):
                self.d.download_url(path, filename="x")
        # An account whose email address isn't verified gets no links: the site sends the file.
        self.knob("email_verified", False)
        self.assertIsNone(self.d.download_url("/dl/文件.bin", filename="文件.bin"))

    # -- errors

    def test_throttling(self):
        self.knob("throttle_after", "0")
        self.fake.throttle = 2
        self.assertEqual(self.d.list("/"), [])  # two short waits, then the answer
        self.fake.throttle = 3
        with self.assertRaises(BackendError):
            self.d.list("/")  # three tries
        self.fake.throttle = 0
        waits = []
        self.fake.throttle, self.fake.throttle_after = 1, "2"
        with mock.patch.object(dropbox.time, "sleep", waits.append):
            self.d.list("/")
        self.assertEqual(waits, [2.0])
        self.fake.throttle, self.fake.throttle_after = 1, "3600"  # longer than throttle_wait: no wait at all
        with mock.patch.object(dropbox.time, "sleep", waits.append), self.assertRaises(BackendError):
            self.d.list("/")
        self.assertEqual(waits, [2.0])

    def test_too_many_writes(self):
        self.fake.fail_next = [(409, ("path", "too_many_write_operations"), None, "/2/files/upload")]
        self.put("/w.txt", b"w")  # sent again
        self.assertEqual(self.read("/w.txt"), b"w")
        self.assertEqual(len(self.fake.calls("/2/files/upload")), 2)
        self.knob("throttle_reason", "too_many_write_operations")
        self.fake.throttle = 1
        with self.assertRaises(BackendError):  # streamed from data that can't seek back: not sent again
            self.d.write("/x.txt", _Stream(b"x"), 1)
        self.d.write("/x.txt", _Stream(b"x"), 1)
        self.assertEqual(self.read("/x.txt"), b"x")

    def test_error_mapping(self):
        cases = [
            ((409, ("path", "not_found")), NotFound, None),
            ((409, ("path", "conflict", "folder")), AlreadyExists, None),
            ((409, ("path", "conflict", "file_ancestor")), NotADirectory, None),
            ((409, ("path", "not_folder")), NotADirectory, None),
            ((409, ("path", "malformed_path")), InvalidPath, "malformed_path"),
            ((409, ("path", "insufficient_space")), DriverError, None),
            ((409, ("path", "no_write_permission")), DriverError, "no_write_permission"),
            ((409, ("path", "restricted_content")), DriverError, None),
            ((409, ("too_many_files",)), DriverError, "10,000"),
            ((409, ("internal_error",)), BackendError, "internal_error"),
            ((403, ("invalid_account_type", "feature")), DriverError, "invalid_account_type/feature"),
            ((409, ("path", "something_new")), DriverError, "path/something_new"),
            ((500, ()), BackendError, "HTTP 500"),
        ]
        for failure, error_class, text in cases:
            with self.subTest(failure=failure):
                self.fake.fail_next = [failure]
                with self.assertRaises(error_class) as caught:
                    self.d.stat("/")
                if error_class in (DriverError, BackendError):
                    self.assertNotIsInstance(caught.exception, (NotFound, AlreadyExists, NotADirectory, InvalidPath))
                if text:
                    self.assertIn(text, caught.exception.message)

    def test_no_path_root_for_a_personal_account(self):
        self.put("/a.txt", b"a")
        self.read("/a.txt")
        self.assertTrue(self.fake.requests)
        self.assertTrue(all(r["path_root"] is None for r in self.fake.requests))

    def test_unreachable(self):
        cls = type("Down", (dropbox.DropboxDriver,), {"api_root": "http://127.0.0.1:9", "content_root": "http://127.0.0.1:9"})
        driver = cls(config(self.fake))
        self.addCleanup(driver.close)
        with self.assertRaises(BackendError):
            driver.list("/")

    # -- search

    def test_search(self):
        self.fake.put("/Report outside the base.txt", b"x")
        for path in ("/Report 2024.txt", "/docs/annual report.pdf", "/docs/deep/REPORTS/x.txt", "/other/report-final.doc"):
            self.put(path, b"r")
        self.put("/docs/notes.txt", b"this mentions report in its content only")
        found = {e.path: e for e in self.d.search("/", "report", 50)}
        self.assertEqual(sorted(found), ["/Report 2024.txt", "/docs/annual report.pdf", "/docs/deep/REPORTS",
                                         "/other/report-final.doc"])
        self.assertTrue(found["/docs/deep/REPORTS"].is_dir)
        self.assertEqual(found["/Report 2024.txt"].size, 1)
        self.assertEqual(self.fake.calls("/2/files/search_v2")[0]["rpc"], {
            "query": "report", "options": {"max_results": 200, "filename_only": True, "path": self.d.config["base_path"]}})
        in_docs = sorted(e.path for e in self.d.search("/DOCS", "REPORT", 50))
        self.assertEqual(in_docs, ["/DOCS/annual report.pdf", "/DOCS/deep/REPORTS"])  # the folder as asked
        self.assertEqual(len(self.d.search("/", "report", 2)), 2)
        # Dropbox matches the words of names: what isn't a part of the name is left out here.
        self.assertEqual(self.d.search("/", "report annual", 10), [])
        # Page after page.
        self.d.search_page = 1
        self.assertEqual(sorted(e.path for e in self.d.search("/docs", "report", 50)), [
            "/docs/annual report.pdf", "/docs/deep/REPORTS"])
        self.assertTrue(self.fake.calls("/2/files/search/continue_v2"))

    # -- params

    def test_validate_config(self):
        cls = dropbox.DropboxDriver
        self.assertEqual(cls.validate_config({"base_path": "/a", "app_key": "abc123xyz"}), {})
        self.assertEqual(sorted(cls.validate_config({"base_path": "/a/../b", "app_key": "abc 123"})),
                         ["app_key", "base_path"])
        self.assertEqual({p.key for p in cls.params if p.secret}, {"app_secret", "refresh_token"})
        self.assertEqual((cls.type, cls.oauth, cls.setup_params, cls.auth_params),
                         ("dropbox", True, ("app_key", "app_secret"), ("refresh_token",)))

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        self.put("/a.txt", b"abc")
        body = LongBody(b'{"error_summary": "path/not_found/..", "error": {".tag": "path", "path": {".tag": "not_found"}}}')
        answer = lambda request: Response(409, body, JSON, length=LONG) if request.path == "/2/files/download" else None
        with self.fake.content.intercepting(answer), self.assertRaises(NotFound):
            self.read("/a.txt")
        self.assertLess(body.sent, LONG // 4)


class TeamSpaceTests(_FakeMixin, unittest.TestCase):
    team_space = True

    def setUp(self):
        self.fake.requests.clear()

    def test_calls_are_rooted_at_the_team_space(self):
        driver = against(self.fake)(config(self.fake, "/"))
        self.addCleanup(driver.close)
        names = {e.name for e in driver.list("/")}
        self.assertTrue({"Member", "Team Docs"} <= names, names)
        driver.write("/Member/mine.txt", io.BytesIO(b"m"), 1)
        self.assertEqual(self.fake.find("/mine.txt", self.fake.home).data, b"m")  # in the member's own folder
        self.assertEqual(driver.open("/Member/mine.txt").read(), b"m")
        file_calls = [r for r in self.fake.requests if r["path"].startswith("/2/files/")]
        self.assertTrue(file_calls)
        self.assertEqual({r["path_root"] for r in file_calls}, {'{".tag":"root","root":"%s"}' % TEAM_NS})
        self.assertEqual(len(self.fake.calls("/2/users/get_current_account")), 1)

    def test_a_changed_root_is_asked_again(self):
        driver = against(self.fake)(config(self.fake, "/"))
        self.addCleanup(driver.close)
        driver.list("/")
        self.addCleanup(setattr, self.fake, "root_ns", self.fake.root_ns)
        self.fake.change_root()
        self.fake.requests.clear()
        self.assertTrue(driver.list("/"))  # 422 invalid_root: the account asked again, the call repeated
        self.assertEqual(len(self.fake.calls("/2/users/get_current_account")), 1)
        self.assertEqual(len(self.fake.calls("/2/files/list_folder")), 2)
        self.assertEqual(self.fake.calls("/2/files/list_folder")[1]["path_root"],
                         '{".tag":"root","root":"%s"}' % self.fake.root_ns)


if __name__ == "__main__":
    unittest.main()
