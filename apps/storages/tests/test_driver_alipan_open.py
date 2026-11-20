"""Tests of the Alipan open platform driver (apps.storages.drivers.alipan_open) against tests/fake_alipan.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_alipan_open
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import time
import unittest
import urllib.parse
import uuid

from apps.storages.drivers import alipan_open
from apps.storages.drivers.alipan_open import AlipanOpenDriver
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, NotFound
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_alipan import CLIENT_ID, CLIENT_SECRET, MIN_PART, FakeAlipan
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response

JSON = {"Content-Type": "application/json"}


def driver_class(fake: FakeAlipan, **attrs):
    """AlipanOpenDriver pointed at the fake, without pacing or long waits (the type stays "alipan-open")."""
    attrs = {"api_root": fake.url, "list_rate": 0, "link_rate": 0, "call_rate": 0, "busy_wait": 0.01,
             "poll_interval": 0.01, "retry_delay": 0.0, **attrs}
    return type("FakeAlipanOpenDriver", (AlipanOpenDriver,), attrs)


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
        cls.fake = FakeAlipan().start()
        cls.Driver = driver_class(cls.fake, **cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def settings(self, base: str = "/", **config) -> dict:
        return {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "drive": "resource", "base_path": base,
                **self.fake.issue_tokens(), **config}

    def new_driver(self, base: str = "/", driver_class=None, **config):
        self.updates: list[dict] = []
        settings = {key: value for key, value in self.settings(base, **config).items() if value is not None}
        driver = (driver_class or self.Driver)(settings, on_update=self.updates.append)
        self.addCleanup(driver.close)
        return driver


class AlipanContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract in a fresh folder of a shared drive (base_path is that folder)."""

    def make_driver(self):
        base = f"/hfile-tests/{uuid.uuid4().hex[:10]}"
        self.fake.mkdirs(base)
        return self.new_driver(base)


class AlipanPartsContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract again on a fresh backup drive's root, with Alipan's smallest parts, so the larger files go up
    in several."""

    driver_attrs = {"part_size": MIN_PART}

    def make_driver(self):
        self.fake.drives["backup"] = "drive-b-" + uuid.uuid4().hex[:8]
        return self.new_driver("/", drive="backup")


class AuthorizationTests(FakeMixin, unittest.TestCase):
    def test_the_authorize_url_and_the_code(self):
        url = self.Driver.authorize_url(self.settings(drive="backup"), redirect_uri="https://files.example.com/storages/oauth/callback/",
                                        state="s1")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertTrue(url.startswith(self.fake.url + "/oauth/authorize?"))
        self.assertEqual(query["client_id"], [CLIENT_ID])
        self.assertEqual(query["redirect_uri"], ["https://files.example.com/storages/oauth/callback/"])
        self.assertEqual(query["scope"], ["user:base,file:all:read,file:all:write"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["state"], ["s1"])
        self.assertEqual(query["drive"], ["backup,resource"])  # both, whichever this source shows
        code = self.fake.issue_code()
        tokens = self.Driver.exchange_code(self.settings(), code=code, redirect_uri="https://x.example/cb/")
        self.assertEqual(sorted(tokens), ["access_token", "refresh_token", "token_expires_at"])
        self.assertGreater(tokens["token_expires_at"], time.time() + 7000)
        with self.assertRaises(AuthError):  # a code is good for one use
            self.Driver.exchange_code(self.settings(), code=code, redirect_uri="https://x.example/cb/")
        with self.assertRaises(AuthError):
            self.Driver.exchange_code(self.settings(client_secret="wrong"), code=self.fake.issue_code(), redirect_uri="https://x.example/cb/")
        with self.assertRaises(AuthError):
            self.Driver.authorize_url({"client_id": ""}, redirect_uri="https://x.example/cb/", state="s")

    def test_an_expired_token_is_refreshed_and_the_new_pair_saved(self):
        d = self.new_driver(token_expires_at=int(time.time()) - 10)
        old = d.config["refresh_token"]
        d.list("/")
        self.assertEqual(len(self.updates), 1)
        self.assertNotEqual(self.updates[0]["refresh_token"], old)
        self.assertNotIn(old, self.fake.refresh_tokens)  # spent
        self.assertGreater(self.updates[0]["token_expires_at"], time.time() + 7000)
        d.list("/")
        self.assertEqual(len(self.updates), 1)  # the new token is used until it expires

    def test_a_refused_token_is_refreshed_once(self):
        d = self.new_driver()
        d.list("/")
        self.fake.expire_tokens()  # Alipan says AccessTokenExpired before the saved expiry
        d.listing_ttl = 0
        self.assertIsInstance(d.list("/"), list)
        self.assertEqual(len(self.updates), 1)

    def test_a_dead_refresh_token(self):
        d = self.new_driver(refresh_token="rt-gone", token_expires_at=1)
        with self.assertRaises(AuthError):
            d.list("/")
        refreshes = self.fake.refreshes
        with self.assertRaises(AuthError):  # remembered for a while: Alipan isn't asked again
            d.list("/")
        self.assertEqual(self.fake.refreshes, refreshes)
        with self.assertRaises(AuthError):
            self.new_driver(access_token=None, refresh_token=None, token_expires_at=None).list("/")

    def test_two_sources_of_one_app_on_the_two_drives(self):
        """Alipan keeps one choice of drives per app and account, and each authorization replaces it (FAQ 47): a
        source of each drive, authorized one after the other with the same app, both go on working."""
        self.addCleanup(setattr, self.fake, "allowed", {"resource", "backup"})
        callback = "https://files.example.com/storages/oauth/callback/"

        def authorized(drive):
            config = self.settings(drive=drive)
            code = self.fake.consent(self.Driver.authorize_url(config, redirect_uri=callback, state="s"))
            return {**config, **self.Driver.exchange_code(config, code=code, redirect_uri=callback)}

        resource = authorized("resource")
        backup = authorized("backup")
        for config in (resource, backup):
            d = self.Driver(config)
            self.addCleanup(d.close)
            self.assertIsInstance(d.list("/"), list)

    def test_a_withdrawn_authorization(self):
        d = self.new_driver()
        self.fake.revoked = True
        self.addCleanup(setattr, self.fake, "revoked", False)
        with self.assertRaises(AuthError):
            d.list("/")


class AlipanTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.base = f"/t-{uuid.uuid4().hex[:10]}"
        self.fake.mkdirs(self.base)

    def path(self, rest: str) -> str:
        return self.base + rest

    def test_pages_of_a_listing(self):
        for n in range(230):
            self.fake.put(self.path(f"/big/f{n:03}.txt"), b"x")
        d = self.new_driver(self.base)
        before = len(self.fake.api_calls("/openFile/list"))
        names = sorted(e.name for e in d.list("/big"))
        self.assertEqual(names, [f"f{n:03}.txt" for n in range(230)])
        calls = self.fake.api_calls("/openFile/list")[before:]
        big = [body for body in calls if body["parent_file_id"] == self.fake.find(self.path("/big")).id]
        self.assertEqual(len(big), 3)  # 100 a page
        self.assertTrue(all(body["limit"] == 100 for body in big))

    def test_the_drives(self):
        self.fake.put("/only-in-backup.txt", b"b", drive="backup")
        d = self.new_driver("/", drive="backup")
        self.assertIn("only-in-backup.txt", [e.name for e in d.list("/")])
        self.assertNotIn("only-in-backup.txt", [e.name for e in self.new_driver("/").list("/")])
        self.fake.allowed = {"resource"}
        self.addCleanup(setattr, self.fake, "allowed", {"resource", "backup"})
        with self.assertRaises(AuthError):
            self.new_driver("/", drive="backup").list("/")

    def test_a_multipart_upload_without_content_type(self):
        d = self.new_driver(self.base, self.Driver_with(part_size=MIN_PART))
        data = os.urandom(MIN_PART * 2 + 5)
        puts = len(self.fake.puts)
        d.write("/m.bin", io.BytesIO(data), len(data))
        mine = self.fake.puts[puts:]
        self.assertEqual([put["part"] for put in mine], [1, 2, 3])
        self.assertTrue(all(put["content_type"] is None for put in mine))
        self.assertEqual(self.fake.find(self.path("/m.bin")).data, data)
        self.assertEqual(read(d, "/m.bin"), data)

    def Driver_with(self, **attrs):
        return driver_class(self.fake, **attrs)

    def test_expired_upload_urls_are_renewed(self):
        d = self.new_driver(self.base, self.Driver_with(part_size=MIN_PART))
        self.fake.url_ttl = -1  # every URL Alipan gives is expired already
        self.addCleanup(setattr, self.fake, "url_ttl", 3600.0)
        data = os.urandom(MIN_PART + 10)
        with self.assertRaises(BackendError):  # renewed once a part, and refused again
            d.write("/late.bin", io.BytesIO(data), len(data))
        self.assertTrue(self.fake.api_calls("/openFile/getUploadUrl"))
        # URLs older than upload_url_ttl are renewed before they're used. (-1, not 0: Windows' monotonic clock moves
        # in steps of about 16 ms, and URLs of this very step are 0 seconds old.)
        self.fake.url_ttl = 3600.0
        d = self.new_driver(self.base, self.Driver_with(part_size=MIN_PART, upload_url_ttl=-1))
        renewals = len(self.fake.api_calls("/openFile/getUploadUrl"))
        d.write("/late.bin", io.BytesIO(data), len(data))
        self.assertEqual(len(self.fake.api_calls("/openFile/getUploadUrl")) - renewals, 2)
        self.assertEqual(self.fake.find(self.path("/late.bin")).data, data)

    def test_rapid_uploads(self):
        data = os.urandom(5000)
        self.fake.put("/elsewhere/same.bin", data)  # Alipan has this content
        d = self.new_driver(self.base)
        puts = len(self.fake.puts)
        d.write("/rapid.bin", io.BytesIO(data), len(data))
        self.assertEqual(len(self.fake.puts), puts)  # nothing uploaded
        self.assertEqual(self.fake.find(self.path("/rapid.bin")).data, data)
        creates = self.fake.api_calls("/openFile/create")[-2:]
        self.assertEqual(creates[0]["pre_hash"], hashlib.sha1(data[:1024]).hexdigest().upper())
        self.assertEqual(creates[1]["content_hash"], hashlib.sha1(data).hexdigest().upper())
        self.assertNotIn("pre_hash", creates[1])
        # The same first KiB, another file: the second create has no match and the parts go up.
        other = data[:1024] + os.urandom(len(data) - 1024)
        d.write("/other.bin", io.BytesIO(other), len(other))
        self.assertEqual(len(self.fake.puts), puts + 1)
        self.assertEqual(read(d, "/other.bin"), other)
        # Data that can't seek goes up without the hashes.
        class Stream(io.RawIOBase):
            def __init__(self, raw):
                self._raw = io.BytesIO(raw)

            def readable(self):
                return True

            def readinto(self, buffer):
                return self._raw.readinto(buffer)

        d.write("/stream.bin", Stream(data), len(data))
        self.assertNotIn("pre_hash", self.fake.api_calls("/openFile/create")[-1])
        self.assertEqual(self.fake.find(self.path("/stream.bin")).data, data)

    def test_the_proof_code(self):
        data = b"0123456789abcdef"
        token = "at-abc"
        first = int(hashlib.md5(token.encode()).hexdigest()[:16], 16) % len(data)
        expected = base64.b64encode(data[first:min(first + 8, len(data))]).decode()
        self.assertEqual(alipan_open.proof_code(token, io.BytesIO(b"xx" + data), 2, len(data)), expected)
        self.assertEqual(alipan_open.proof_code(token, io.BytesIO(b""), 0, 0), "")

    def test_replacing_a_file(self):
        d = self.new_driver(self.base)
        d.write("/r.txt", io.BytesIO(b"old"), 3)
        old = self.fake.find(self.path("/r.txt"))
        d.write("/r.txt", io.BytesIO(b"newer"), 5)
        self.assertEqual(self.fake.names(self.path("/")), ["r.txt"])
        self.assertTrue(old.trashed)  # the old one went into the recycle bin
        self.assertEqual(read(d, "/r.txt"), b"newer")

    def test_downloads_always_have_a_range(self):
        data = os.urandom(3000)
        self.fake.put(self.path("/d.bin"), data)
        self.fake.put(self.path("/empty"), b"")
        d = self.new_driver(self.base)
        self.assertIsNone(d.download_url("/d.bin", filename="d.bin"))
        start = len(self.fake.downloads)
        self.assertEqual(read(d, "/d.bin"), data)
        self.assertEqual(read(d, "/d.bin", 100, 50), data[100:150])
        self.assertEqual(read(d, "/d.bin", 2990), data[2990:])
        self.assertEqual([g["range"] for g in self.fake.downloads[start:]], ["bytes=0-2999", "bytes=100-149", "bytes=2990-2999"])
        self.assertTrue(all(g["referer"] is None for g in self.fake.downloads[start:]))
        downloads = len(self.fake.downloads)
        self.assertEqual(read(d, "/empty"), b"")
        self.assertEqual(len(self.fake.downloads), downloads)  # nothing to fetch (no Range can ask for 0 bytes)

    def test_links_are_reused_and_renewed(self):
        data = os.urandom(100)
        self.fake.put(self.path("/l.bin"), data)
        d = self.new_driver(self.base)
        links = len(self.fake.api_calls("/getDownloadUrl"))
        read(d, "/l.bin")
        read(d, "/l.bin")
        self.assertEqual(len(self.fake.api_calls("/getDownloadUrl")) - links, 1)
        self.fake.expire_links()
        self.assertEqual(read(d, "/l.bin"), data)  # refused: another link
        self.assertEqual(len(self.fake.api_calls("/getDownloadUrl")) - links, 2)

    def test_too_many_downloads_at_once(self):
        data = os.urandom(100)
        self.fake.put(self.path("/busy.bin"), data)
        d = self.new_driver(self.base)
        self.fake.busy = 2
        self.assertEqual(read(d, "/busy.bin"), data)  # waited and tried again
        self.fake.busy = 10
        self.addCleanup(setattr, self.fake, "busy", 0)
        with self.assertRaises(BackendError):
            read(d, "/busy.bin")

    def test_a_frozen_file(self):
        self.fake.put(self.path("/frozen.mp4"), b"movie", punish=2)
        d = self.new_driver(self.base)
        self.assertEqual([e.name for e in d.list("/")], ["frozen.mp4"])
        with self.assertRaises(DriverError):
            read(d, "/frozen.mp4")

    def test_punish_flag_103(self):
        """FAQ 38: "punish_flag = 103：文件在资源盘不能下载播放，在备份盘可以"."""
        self.fake.put(self.path("/limited.mp4"), b"movie", punish=103)
        with self.assertRaises(DriverError):
            read(self.new_driver(self.base), "/limited.mp4")
        self.fake.drives["backup"] = "drive-b-" + uuid.uuid4().hex[:8]
        self.fake.put("/limited.mp4", b"movie", drive="backup", punish=103)
        self.assertEqual(read(self.new_driver("/", drive="backup"), "/limited.mp4"), b"movie")

    def test_a_live_photo(self):
        """Alipan gives a .livp no download url, only its picture and video (streamsUrl): an error that says so,
        not "try again later"."""
        self.fake.put(self.path("/IMG_0001.livp"), b"live photo")
        d = self.new_driver(self.base)
        self.assertEqual([e.name for e in d.list("/")], ["IMG_0001.livp"])
        with self.assertRaises(DriverError) as caught:
            read(d, "/IMG_0001.livp")
        self.assertNotIsInstance(caught.exception, BackendError)

    def test_throttling(self):
        d = self.new_driver(self.base)
        self.fake.throttle = 2
        self.assertEqual(d.list("/"), [])  # waited x-retry-after twice
        self.fake.throttle = 1
        self.fake.retry_after_ms = 60_000
        self.addCleanup(setattr, self.fake, "retry_after_ms", 30)
        d.listing_ttl = 0
        with self.assertRaises(BackendError):  # longer than throttle_wait: not waited for
            d.list("/")
        self.fake.throttle = 0

    def test_background_changes_are_waited_for(self):
        """Moves and deletes, until their tasks succeeded; not a folder copy, whose task can't be asked about (the
        copy doc: "暂时未开放查询task状态的接口，可以先忽略"; the fake refuses it)."""
        self.fake.async_folders = True
        self.addCleanup(setattr, self.fake, "async_folders", False)
        self.fake.put(self.path("/a/x.txt"), b"x")
        self.fake.put(self.path("/c/y.txt"), b"y")
        self.fake.mkdirs(self.path("/b"))
        d = self.new_driver(self.base)
        polls = len(self.fake.api_calls("/async_task/get"))
        d.copy("/a", "/b")
        self.assertEqual(len(self.fake.api_calls("/async_task/get")), polls)
        d.move("/c", "/b")
        d.delete("/a")
        self.assertEqual(len(self.fake.api_calls("/async_task/get")) - polls, 4)  # until each task succeeded
        self.assertEqual(self.fake.names(self.path("/")), ["b"])
        self.assertEqual(self.fake.names(self.path("/b")), ["a", "c"])
        self.assertEqual(self.fake.names(self.path("/b/a")), ["x.txt"])

    def test_names_taken_meanwhile(self):
        """What Alipan answers when a name was taken after the listing HFile went by: "exist" for a folder and a
        move with check_name_mode refuse, 409 for a rename."""
        d = self.new_driver(self.base)
        base = self.fake.find(self.base)
        self.fake.mkdirs(self.path("/taken"))
        with self.assertRaises(AlreadyExists):
            d._create_node(base.id, "taken")
        other = self.fake.put(self.path("/f.txt"), b"f")
        with self.assertRaises(AlreadyExists):
            d._rename_node(other, "taken")
        moving = self.fake.put(self.path("/elsewhere/taken"), b"t")
        with self.assertRaises(AlreadyExists):
            d._move_node(moving, moving.parent, base.id)
        self.assertEqual(self.fake.names(self.path("/elsewhere")), ["taken"])  # not moved

    def test_errors(self):
        d = self.new_driver(self.base)
        with self.assertRaises(NotFound):
            read(d, "/missing.txt")
        self.fake.put(self.path("/gone.txt"), b"g")
        d.list("/")
        self.fake.find(self.path("/gone.txt")).trashed = True  # deleted in the Alipan app
        with self.assertRaises(NotFound):
            read(d, "/gone.txt")
        self.assertEqual(AlipanOpenDriver._error(400, "QuotaExhausted.Drive", {}).__class__, DriverError)
        self.assertIsInstance(AlipanOpenDriver._error(403, "UserNotAllowedAccessDrive", {}), AuthError)
        self.assertIsInstance(AlipanOpenDriver._error(403, "PermissionDenied", {}), AuthError)
        self.assertIsInstance(AlipanOpenDriver._error(404, "NotFound.FileId", {}), NotFound)
        self.assertIsInstance(AlipanOpenDriver._error(409, "AlreadyExist.File", {}), AlreadyExists)
        self.assertIsInstance(AlipanOpenDriver._error(503, "", {}), BackendError)

    def test_settings(self):
        check = AlipanOpenDriver.validate_config
        self.assertEqual(check({"client_id": "a", "client_secret": "b", "base_path": "/x"}), {})
        self.assertIn("base_path", check({"base_path": "../x"}))
        self.assertIn("token_expires_at", check({"token_expires_at": -1}))

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        self.fake.put(self.path("/a.txt"), b"abc")
        body = LongBody(b'{"code": "InternalError", "message": "the CDN failed"}')
        answer = lambda request: Response(500, body, JSON, length=LONG) if request.path.startswith("/dl/") else None
        with self.fake.server.intercepting(answer), self.assertRaises(BackendError):
            self.new_driver(self.base).open("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
