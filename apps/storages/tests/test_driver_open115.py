"""Tests of the 115 driver (apps.storages.drivers.open115) against the in-memory fake of 115.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_open115
"""

from __future__ import annotations

import email.message
import email.utils
import io
import os
import threading
import time
import unittest
import uuid
from unittest import mock

import httpx

from apps.storages.drivers import _ids, open115
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    NotADirectory,
    NotFound,
    Unsupported,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_open115 import APP_ID, Fail, Fake115, fake_v1_signature

BROWSER = "Mozilla/5.0 (X11; Linux x86_64) TestBrowser/1.0"


def driver_class(fake: Fake115, **attrs):
    """Open115Driver pointed at the fake (the type stays "open115")."""
    attrs = {"api_root": fake.server.url + "/proapi", "passport_root": fake.server.url + "/passport",
             "qrcode_root": fake.server.url + "/qrcode", "retry_delay": 0.0, **attrs}
    return type("FakeOpen115Driver", (open115.Open115Driver,), attrs)


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


def names(entries) -> list[str]:
    return sorted(entry.name for entry in entries)


class FrozenClock:
    """open115's `time` with a monotonic clock that moves only when a test moves it: listings
    age by the test's steps, not by how busy the machine is. Everything else is the real time."""

    def __init__(self):
        self.now = time.monotonic()

    def monotonic(self) -> float:
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


class FakeMixin:
    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = Fake115().start()
        cls.Driver = driver_class(cls.fake, **cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def settings(self, base: str = "/", **config) -> dict:
        return {"app_id": APP_ID, "qps": 1000, "base_path": base, **self.fake.issue_tokens(), **config}

    def new_driver(self, base: str = "/", driver_class=None, **config):
        self.updates: list[dict] = []
        driver = (driver_class or self.Driver)(self.settings(base, **config), on_update=self.updates.append)
        self.addCleanup(driver.close)
        return driver


class OddAnswerTests(unittest.TestCase):
    def test_what_115_and_its_oss_may_send(self):
        """STOR-04: an XML answer declaring an encoding Python doesn't know was a LookupError (a 500), not an
        answer that can't be read. STOR-17: an id of "²", which isdigit() takes, was a ValueError where
        int() read it."""
        self.assertIsNone(open115._xml(b'<?xml version="1.0" encoding="x-no-such"?><Error/>'))
        self.assertEqual(_ids.id_key("²"), (1, "²"))
        self.assertEqual(sorted(["12", "²", "3"], key=_ids.id_key), ["3", "12", "²"])


class Open115ContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract in a fresh folder of a shared account (base_path is that folder)."""

    def make_driver(self):
        base = f"/hfile-tests/{uuid.uuid4().hex[:10]}"
        self.fake.mkdirs(base)
        return self.new_driver(base)

    def test_download_url(self):
        # Replaces the contract's test, which fetches the link with httpx's own User-Agent: 115's
        # links only work with the User-Agent that asked for them, so download_url() needs the
        # browser's (user_agent=, an extension of the interface) and gives None without one.
        data = os.urandom(5000)
        self.put("/dl/文件 1.bin", data)
        self.assertIsNone(self.d.download_url("/dl/文件 1.bin", filename="文件 1.bin", expires=600))
        url = self.d.download_url("/dl/文件 1.bin", filename="文件 1.bin", expires=600, user_agent=BROWSER)
        response = httpx.get(url, headers={"User-Agent": BROWSER}, follow_redirects=True, timeout=30)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.content, data)
        self.assertEqual(httpx.get(url, timeout=30).status_code, 403)  # another User-Agent


class Open115RootContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract again with base_path "/" on a fresh account for every test, and small
    multipart thresholds, so the larger files go up to OSS in parts."""

    driver_attrs = {"single_put_limit": 1024 * 1024, "part_size": 256 * 1024}
    test_download_url = Open115ContractTests.test_download_url

    def make_driver(self):
        self.fake.reset()
        return self.new_driver("/")


class Open115Tests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    # -- OSS signing

    def test_oss_v1_signature_vectors(self):
        # Aliyun's own vectors: aliyun-oss-go-sdk, oss/conn_test.go, TestAuthV1Header.
        date = "Wed, 28 Dec 2022 10:27:41 GMT"
        put = {"Content-MD5": "eB5eJF1ptWaXm4bijSPyxw==", "Content-Type": "text/html", "x-oss-meta-author": "alice",
               "x-oss-meta-magic": "abracadabra", "x-oss-date": date, "Date": date}
        get = {"x-oss-date": date, "Date": date}
        vectors = [
            ("PUT", put, "/examplebucket/nelson", "kSHKmLxlyEAKtZPkJhG9bZb5k7M="),
            ("PUT", put, "/examplebucket/nelson?acl", "/afkugFbmWDQ967j1vr6zygBLQk="),
            ("GET", get, "/examplebucket/?resourceGroup", "vkQmfuUDyi1uDi3bKt67oemssIs="),
            ("GET", get, "/examplebucket/?acl&resourceGroup", "x3E5TgOvl/i7PN618s5mEvpJDYk="),
        ]
        for method, headers, resource, expected in vectors:
            with self.subTest(resource=resource):
                self.assertEqual(open115.oss_signature("sk", method, headers, resource), expected)
                message = email.message.Message()
                for name, value in headers.items():
                    message[name] = value
                self.assertEqual(fake_v1_signature("sk", method, message, resource), expected)
        self.assertEqual(open115._sub_resources([("uploadId", "X"), ("partNumber", "1")]), "partNumber=1&uploadId=X")

    def test_oss_endpoint_and_urls(self):
        self.assertEqual(open115._oss_endpoint("http://oss-cn-shenzhen.aliyuncs.com"),
                         "https://oss-cn-shenzhen.aliyuncs.com")
        self.assertEqual(open115._oss_endpoint("oss-cn-shenzhen.aliyuncs.com"), "https://oss-cn-shenzhen.aliyuncs.com")
        self.assertEqual(open115._oss_endpoint("http://127.0.0.1:9000/"), "http://127.0.0.1:9000")
        for bad in (None, "", "ftp://example.com", "http://"):
            with self.subTest(bad=bad), self.assertRaises(BackendError):
                open115._oss_endpoint(bad)
        self.assertTrue(open115._path_style("127.0.0.1"))
        self.assertFalse(open115._path_style("oss-cn-shenzhen.aliyuncs.com"))

        driver = self.new_driver()
        seen = []
        driver._http = httpx.Client(transport=httpx.MockTransport(
            lambda request: (seen.append(request), httpx.Response(200))[1]))
        sts = open115._Sts("https://oss-cn-shenzhen.aliyuncs.com", "STS.id", "secret", "token",
                           time.monotonic() + 100)
        driver._oss_send(sts, "PUT", "fhnfile", "a b/c+d", sub=[("uploadId", "X"), ("partNumber", "1")],
                         headers={"Content-Length": "0"}, content=b"")
        request = seen[0]
        self.assertEqual(str(request.url),
                         "https://fhnfile.oss-cn-shenzhen.aliyuncs.com/a%20b/c%2Bd?partNumber=1&uploadId=X")
        self.assertEqual(request.headers["x-oss-security-token"], "token")
        resource = "/fhnfile/a b/c+d?partNumber=1&uploadId=X"  # the raw key, sub-resources sorted
        expected = open115.oss_signature("secret", "PUT", dict(request.headers), resource)
        self.assertEqual(request.headers["authorization"], f"OSS STS.id:{expected}")

    # -- QR-code login

    def test_qr_login(self):
        config = {"app_id": APP_ID}
        start = self.Driver.helper("qrcode", config, {})
        self.assertTrue(start["image"].startswith("data:image/png;base64,"))
        self.assertEqual(start["poll"], "qrcode")
        args = start["args"]
        self.assertEqual(set(args), {"uid", "time", "sign", "code_verifier", "qrcode"})
        self.assertTrue(43 <= len(args["code_verifier"]) <= 128)

        waiting = self.Driver.helper("qrcode", config, args)
        self.assertIn("等待扫码", waiting["message"])
        self.assertEqual((waiting["poll"], waiting["args"]), ("qrcode", args))
        self.fake.qr_set(args["uid"], "scanned")
        self.assertIn("已扫码", self.Driver.helper("qrcode", config, args)["message"])
        self.fake.qr_set(args["uid"], "confirmed")
        done = self.Driver.helper("qrcode", config, args)
        self.assertNotIn("poll", done)
        changes = done["changes"]
        self.assertEqual(set(changes), {"access_token", "refresh_token", "token_expires_at"})
        self.assertGreater(changes["token_expires_at"], time.time() + 3600)
        driver = self.Driver({"app_id": APP_ID, "qps": 1000, **changes})
        self.addCleanup(driver.close)
        self.assertEqual(driver.list("/"), [])

    def test_qr_login_failures(self):
        config = {"app_id": APP_ID}
        with self.assertRaises(DriverError):
            self.Driver.helper("qrcode", {"app_id": "999"}, {})  # unknown AppID
        with self.assertRaises(DriverError):
            self.Driver.helper("qrcode", {}, {})
        args = self.Driver.helper("qrcode", config, {})["args"]
        for key, bad in (("uid", "a\nb"), ("sign", ""), ("code_verifier", None), ("time", "x" * 600)):
            with self.subTest(key=key), self.assertRaises(DriverError):
                self.Driver.helper("qrcode", config, dict(args, **{key: bad}))
        self.fake.qr_set(args["uid"], "confirmed")
        with self.assertRaises(AuthError):  # PKCE: another verifier is refused
            self.Driver.helper("qrcode", config, dict(args, code_verifier="v" * 64))
        self.fake.qr_set(args["uid"], "canceled")
        answer = self.Driver.helper("qrcode", config, args)
        self.assertIn("已取消", answer["message"])
        self.assertNotIn("poll", answer)
        self.fake.qr_set(args["uid"], "expired")
        self.assertIn("已失效", self.Driver.helper("qrcode", config, args)["message"])
        with self.assertRaises(Unsupported):
            self.Driver.helper("nope", config, {})

    def test_qr_long_poll_timeout(self):
        Impatient = driver_class(self.fake, qr_poll_timeout=0.3)
        args = Impatient.helper("qrcode", {"app_id": APP_ID}, {})["args"]
        self.fake.qr_delay = 1.0
        try:
            answer = Impatient.helper("qrcode", {"app_id": APP_ID}, args)
        finally:
            self.fake.qr_delay = 0.0
        self.assertIn("等待扫码", answer["message"])
        self.assertEqual(answer["poll"], "qrcode")

    # -- tokens

    def test_saved_token_used_until_it_expires(self):
        driver = self.new_driver()
        driver.list("/")
        driver.list("/")
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 0)
        self.assertEqual(self.updates, [])

    def test_expired_token_refreshed_and_saved(self):
        driver = self.new_driver(token_expires_at=int(time.time()) - 10)
        old_refresh = driver.config["refresh_token"]
        driver.list("/")
        driver.list("/")
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 1)
        self.assertEqual(len(self.updates), 1)
        change = self.updates[0]
        self.assertEqual(set(change), {"access_token", "refresh_token", "token_expires_at"})
        self.assertNotEqual(change["refresh_token"], old_refresh)
        self.assertGreater(change["token_expires_at"], time.time() + 3600)
        self.assertFalse(self.fake.refresh[old_refresh])  # 115 made it unusable
        self.assertEqual(driver.config["refresh_token"], change["refresh_token"])

    def test_refused_token_refreshed_once(self):
        driver = self.new_driver()
        self.fake.expire_access_tokens()  # the saved expiry still says the token is good
        self.assertEqual(driver.list("/"), [])
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 1)
        self.assertEqual(len(self.updates), 1)

        self.fake.expire_access_tokens()
        errors = []

        def work():
            try:
                driver.list("/")
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 2)  # one refresh for all threads

    def test_refused_refresh_is_remembered(self):
        # A dead authorisation: the first request asks 115, the next ones don't (for a while).
        settings = self.settings(token_expires_at=1)
        first, second = self.Driver(dict(settings)), self.Driver(dict(settings))
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.list("/")  # uses up the refresh token
        calls = []
        original = self.fake._refresh

        def counting(form):
            calls.append(form)
            return original(form)

        self.fake._refresh = counting
        self.addCleanup(delattr, self.fake, "_refresh")
        for _ in range(3):
            with self.assertRaises(AuthError):
                second.list("/")
        self.assertEqual(len(calls), 1)

    def frozen(self) -> FrozenClock:
        """open115's clock, frozen (FrozenClock) for the rest of the test."""
        clock = FrozenClock()
        patcher = mock.patch.object(open115, "time", clock)
        patcher.start()
        self.addCleanup(patcher.stop)
        return clock

    def test_a_refresh_that_doesnt_help_isnt_repeated(self):
        """115 refused every token (40101017, an account it won't serve), so every request refreshed the
        token and saved the new pair, though the one it had just got was refused too; 115 asks not to
        refresh often. Such a refresh now counts as refused, for refusal_ttl."""
        self.knob("refuse_all", Fail(40101017, "用户验证失败"))
        clock = self.frozen()
        driver = self.new_driver()
        for _ in range(5):
            with self.assertRaises(AuthError) as caught:
                driver.list("/")
            self.assertIn("40101017", caught.exception.message)
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 1)
        self.assertEqual(len(self.updates), 1)
        clock.now += driver.refusal_ttl + 1  # then a refresh is worth a try again
        with self.assertRaises(AuthError):
            driver.list("/")
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 2)

    def test_no_refresh_while_115_says_too_often(self):
        """115 refused the saved token and answered its refresh with "too often" (40140117): every
        request asked for a refresh again, in the two minutes the expiry path waits."""
        self.knob("refresh_too_often", True)
        clock = self.frozen()
        driver = self.new_driver()
        self.fake.expire_access_tokens()  # refused, though the saved expiry says it's good
        for _ in range(5):
            with self.assertRaises(BackendError) as caught:
                driver.list("/")
            self.assertIn("太频繁", caught.exception.message)
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 1)
        self.fake.refresh_too_often = False
        clock.now += 121
        self.assertEqual(driver.list("/"), [])
        self.assertEqual(self.fake.counts["passport/open/refreshToken"], 2)

    def test_refresh_token_works_once(self):
        settings = self.settings(token_expires_at=1)
        first, second = self.Driver(dict(settings)), self.Driver(dict(settings))
        self.addCleanup(first.close)
        self.addCleanup(second.close)
        first.list("/")
        with self.assertRaises(AuthError) as caught:
            second.list("/")  # the same refresh token, already used by the first
        self.assertIn("重新扫码登录", caught.exception.message)

    def test_without_tokens(self):
        driver = self.Driver({"app_id": APP_ID, "qps": 1000})
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("扫码登录", caught.exception.message)

    # -- listings and lookups

    def test_paging(self):
        self.fake.page_limit = 3
        for i in range(8):
            self.fake.put_file(f"/many/f{i}.txt", b"x" * i)
        driver = self.new_driver()
        entries = {entry.name: entry for entry in driver.list("/many")}
        self.assertEqual(sorted(entries), [f"f{i}.txt" for i in range(8)])
        self.assertEqual(entries["f5.txt"].size, 5)
        self.assertIsNotNone(entries["f5.txt"].modified.tzinfo)
        self.assertGreaterEqual(self.fake.counts["/open/ufile/files"], 4)  # 1 for "/", 3 pages

    def test_a_change_while_a_folder_is_listed(self):
        """115 sorts a listing as the account's owner chose in its apps, unless asked otherwise. Newest
        first, a file renamed while the pages of its folder were being read moved to the first page,
        already read, and the listing left it out. Listings ask for name order: a rename moves an entry
        by its new name only, and nothing else moves one."""
        self.fake.page_limit = 3
        folder_id = self.fake.mkdirs("/big")
        for i in range(8):
            self.fake.add(folder_id, f"f{i}.txt", False, b"x")
        self.fake.remembered_order = ("user_utime", False)  # newest first
        driver = self.new_driver(driver_class=driver_class(self.fake, page_size=3))
        driver.list("/")
        real = driver._call

        def call(method, path, **kwargs):
            answer = real(method, path, **kwargs)
            params = kwargs.get("params") or {}
            if path == "/open/ufile/files" and params.get("cid") == folder_id and params.get("offset") == 0:
                with self.fake.lock:  # renamed in 115's app before the next page is asked for
                    item = self.fake.find("/big/f3.txt")
                    item.name, item.mtime = "renamed.txt", self.fake.tick()
            return answer

        driver._call = call
        listed = names(driver.list("/big"))
        with self.fake.lock:
            self.assertEqual(listed, sorted(item.name for item in self.fake.children(folder_id)))

    def test_unknown_folder_id_is_not_the_root(self):
        self.fake.put_file("/sub/a.txt", b"a")
        self.fake.put_file("/root.txt", b"r")
        driver = self.new_driver()
        self.assertEqual(names(driver.list("/sub")), ["a.txt"])
        with self.fake.lock:
            self.fake.find("/sub").trashed = True  # deleted in 115's app
        with self.assertRaises(NotFound):
            driver.list("/sub")  # 115 answers with the root for the old id: not shown as /sub
        self.fake.put_file("/sub/b.txt", b"b")  # made again in 115's app, with a new id
        # Lookups believe a listing for listing_ttl seconds (a miss doesn't ask 115 again); the
        # listing of the parent, which a visitor sees first, always asks and finds it.
        self.assertEqual(names(driver.list("/")), ["root.txt", "sub"])
        self.assertEqual(names(driver.list("/sub")), ["b.txt"])

    def test_missing_base_path(self):
        driver = self.new_driver("/no/such/base")
        with self.assertRaises(NotFound) as caught:
            driver.check()
        self.assertIn("基路径", caught.exception.message)

    def test_duplicates_show_once(self):
        folder = self.fake.mkdirs("/dup")
        self.fake.add(folder, "same.txt", False, b"old")
        self.fake.add(folder, "same.txt", False, b"newer")
        driver = self.new_driver()
        [entry] = driver.list("/dup")
        self.assertEqual((entry.name, entry.size), ("same.txt", 5))
        self.assertEqual(read(driver, "/dup/same.txt"), b"newer")
        self.fake.add(folder, "same.txt", True)
        [entry] = driver.list("/dup")
        self.assertTrue(entry.is_dir)  # a folder wins
        self.assertTrue(driver.stat("/dup/same.txt").is_dir)

    def test_odd_backend_names_left_out(self):
        folder = self.fake.mkdirs("/odd")
        self.fake.add(folder, "a/b", False, b"x")
        self.fake.add(folder, "..", True)
        self.fake.add(folder, "fine.txt", False, b"y")
        driver = self.new_driver()
        self.assertEqual(names(driver.list("/odd")), ["fine.txt"])

    def test_confined_to_base_path(self):
        self.fake.put_file("/secret.txt", b"s")
        self.fake.mkdirs("/base")
        driver = self.new_driver("/base")
        self.assertEqual(driver.list("/"), [])
        for path in ("/secret.txt", "/..%2Fsecret.txt", "/%2E%2E/secret.txt"):
            with self.subTest(path=path), self.assertRaises(NotFound):
                driver.stat(path)
        driver.mkdir("/a%2F..")
        self.assertIsNotNone(self.fake.find("/base/a%2F.."))
        self.assertIsNone(self.fake.find("/a"))
        for call in (lambda: driver.delete("/"), lambda: driver.rename("/", "x"), lambda: driver.move("/", "/x"),
                     lambda: driver.copy("/", "/x")):
            with self.assertRaises(DriverError):
                call()
        self.assertEqual(names(driver.list("/")), ["a%2F.."])

    def test_list_and_stat_errors(self):
        driver = self.new_driver()
        driver.write("/f.txt", io.BytesIO(b"x"), 1)
        with self.assertRaises(NotADirectory):
            driver.list("/f.txt")
        with self.assertRaises(NotFound):
            driver.stat("/f.txt/inner")
        with self.assertRaises(NotADirectory):
            driver.mkdir("/f.txt/inner")
        driver.mkdir("/d")
        driver.move("/f.txt", "/")  # already there: nothing to do
        self.assertEqual(names(driver.list("/")), ["d", "f.txt"])
        with self.assertRaises(NotADirectory):
            driver.move("/d", "/f.txt")

    # -- uploads

    def test_fast_upload(self):
        data = os.urandom(5000)
        driver = self.new_driver()
        driver.write("/a.bin", io.BytesIO(data), len(data))
        sent = len(self.fake.oss_requests)
        driver.write("/copy/b.bin", io.BytesIO(data), len(data))  # 115 has these bytes: 秒传
        self.assertEqual(len(self.fake.oss_requests), sent)
        self.assertEqual(read(driver, "/copy/b.bin"), data)

    def test_second_check_of_fast_upload(self):
        data = os.urandom(300 * 1024)
        self.fake.put_file("/elsewhere/x.bin", data)
        self.fake.require_sign = True
        driver = self.new_driver()
        driver.write("/y.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.counts["/open/upload/init"], 2)  # asked for a range's SHA-1, got it
        self.assertEqual(self.fake.oss_requests, [])
        self.assertEqual(read(driver, "/y.bin"), data)

    def test_multipart_upload(self):
        Small = driver_class(self.fake, single_put_limit=300 * 1024, part_size=128 * 1024)
        driver = self.new_driver(driver_class=Small)
        data = os.urandom(700 * 1024 + 5)
        driver.write("/big.bin", io.BytesIO(data), len(data))
        requests = self.fake.oss_requests
        self.assertEqual(requests[0], ("POST", "sequential&uploads"))  # parts in order, as other clients ask
        self.assertEqual(len([query for _, query in requests if "partNumber=" in query]), 6)
        self.assertEqual(requests[-1][0], "POST")
        self.assertEqual(self.fake.multipart, {})
        self.assertEqual(read(driver, "/big.bin"), data)
        self.assertEqual(driver.stat("/big.bin").size, len(data))
        self.assertEqual(self.fake.leaks, [])  # 115's bearer token never went to OSS

    def test_multipart_upload_115_refused(self):
        Small = driver_class(self.fake, single_put_limit=300 * 1024, part_size=128 * 1024)
        driver = self.new_driver(driver_class=Small)
        self.fake.fail_callback = True
        data = os.urandom(400 * 1024)
        with self.assertRaises(BackendError):
            driver.write("/big.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.multipart, {})  # completed, but 115 didn't take it
        with self.assertRaises(NotFound):
            driver.stat("/big.bin")

    def test_failed_multipart_upload_is_aborted(self):
        Small = driver_class(self.fake, single_put_limit=300 * 1024, part_size=128 * 1024)
        driver = self.new_driver(driver_class=Small)
        self.fake.oss_fail_parts = {2}
        data = os.urandom(400 * 1024)
        with self.assertRaises(BackendError) as caught:
            driver.write("/big.bin", io.BytesIO(data), len(data))
        self.assertIn("HTTP 500", caught.exception.message)
        part_two = [query for _, query in self.fake.oss_requests if query.startswith("partNumber=2&")]
        self.assertEqual(len(part_two), Small.oss_retries)  # tried again before giving up
        self.assertEqual(self.fake.oss_requests[-1][0], "DELETE")  # AbortMultipartUpload
        self.assertEqual(self.fake.multipart, {})
        with self.assertRaises(NotFound):
            driver.stat("/big.bin")

    def test_expired_sts_keys_renewed(self):
        driver = self.new_driver()
        driver.write("/one.bin", io.BytesIO(os.urandom(1000)), 1000)
        self.fake.expire_sts()
        two = os.urandom(1000)
        driver.write("/two.bin", io.BytesIO(two), len(two))
        self.assertEqual(self.fake.counts["/open/upload/get_token"], 2)
        self.assertEqual(read(driver, "/two.bin"), two)

    def test_sts_keys_in_a_list(self):
        self.fake.token_as_list = True
        driver = self.new_driver()
        data = os.urandom(1000)
        driver.write("/l.bin", io.BytesIO(data), len(data))
        self.assertEqual(read(driver, "/l.bin"), data)

    def test_callback_failed(self):
        driver = self.new_driver()
        self.fake.fail_callback = True
        with self.assertRaises(BackendError) as caught:
            driver.write("/cb.bin", io.BytesIO(b"abc"), 3)
        self.assertIn("回调", caught.exception.message)
        with self.assertRaises(NotFound):
            driver.stat("/cb.bin")

    def test_a_server_clock_far_off(self):
        """OSS refuses a request dated more than 15 minutes from its clock (RequestTimeTooSkewed), and the
        upload said to try again later, which can't help. It says to correct the server's time."""
        driver = self.new_driver()

        def late(usegmt):
            return email.utils.formatdate(time.time() - 20 * 60, usegmt=usegmt)

        with mock.patch.object(open115, "formatdate", late), self.assertRaises(AuthError) as caught:
            driver.write("/t.bin", io.BytesIO(os.urandom(1000)), 1000)  # not BackendError: "try again later"
        self.assertIn("RequestTimeTooSkewed", caught.exception.message)

    def test_overwrite_deletes_the_old_copy(self):
        driver = self.new_driver()
        driver.write("/o.txt", io.BytesIO(b"first"), 5)
        old = self.fake.find("/o.txt")
        driver.write("/o.txt", io.BytesIO(b"second"), 6)
        with self.fake.lock:
            alive = [item for item in self.fake.children("0") if item.name == "o.txt"]
            self.assertEqual(len(alive), 1)
            self.assertTrue(old.trashed)  # in 115's recycle bin
        self.assertEqual(read(driver, "/o.txt"), b"second")

    def knob(self, name, value):
        old = getattr(self.fake, name)
        setattr(self.fake, name, value)
        self.addCleanup(setattr, self.fake, name, old)

    def test_overwrite_when_115_renames_the_upload(self):
        # If 115 gives the upload another name because the old file has it, the old file goes
        # and the new one gets the name back.
        self.knob("rename_on_conflict", True)
        driver = self.new_driver()
        driver.write("/r.txt", io.BytesIO(b"first"), 5)
        driver.write("/r.txt", io.BytesIO(b"second"), 6)
        with self.fake.lock:
            alive = sorted(item.name for item in self.fake.children("0"))
        self.assertEqual(alive, ["r.txt"])
        self.assertEqual(read(driver, "/r.txt"), b"second")

    def test_a_new_upload_115_gave_another_name(self):
        """STOR-19: the name was taken by a file HFile didn't know of yet (made in 115's own app meanwhile), 115 gave
        the upload another name, and the write said it was done, under the name the user had given."""
        self.knob("rename_on_conflict", True)
        driver = self.new_driver()
        self.assertEqual(driver.list("/"), [])  # the listing HFile goes by for a while (guard_ttl)
        self.fake.put_file("/s.txt", b"theirs")
        with self.assertRaises(DriverError) as caught:
            driver.write("/s.txt", io.BytesIO(b"mine!"), 5)
        self.assertIn("「s(1).txt」", caught.exception.message)
        self.assertNotIsInstance(caught.exception, BackendError)  # not the storage failing (502, "try again later")
        with self.fake.lock:
            self.assertEqual(sorted(item.name for item in self.fake.children("0")), ["s(1).txt", "s.txt"])
        self.assertEqual(read(driver, "/s.txt"), b"theirs")
        self.assertEqual(read(driver, "/s(1).txt"), b"mine!")

    def test_upload_without_a_file_id(self):
        # The callback answer doesn't say which file 115 made: the listing must show it, or the
        # write fails (and an old file is kept).
        self.knob("omit_file_id", True)
        driver = self.new_driver()
        driver.write("/n.txt", io.BytesIO(b"new"), 3)
        self.assertEqual(read(driver, "/n.txt"), b"new")
        driver.write("/n.txt", io.BytesIO(b"newer"), 5)
        self.assertEqual(read(driver, "/n.txt"), b"newer")
        with self.fake.lock:
            self.assertEqual([i.name for i in self.fake.children("0")], ["n.txt"])
        self.knob("callback_makes_nothing", True)
        with self.assertRaises(BackendError):
            driver.write("/n.txt", io.BytesIO(b"lost!!"), 6)
        self.assertEqual(read(driver, "/n.txt"), b"newer")  # the old one stays

    def test_refreshing_too_often_keeps_the_saved_token(self):
        self.knob("refresh_too_often", True)
        driver = self.new_driver(token_expires_at=int(time.time()) + 60)  # "about to expire"
        self.assertEqual(driver.list("/"), [])  # the saved token still works
        self.assertEqual(self.updates, [])

    def test_queue_has_a_limit(self):
        # At qps=1 a burst can't hold the site's threads for ever: a call whose turn is more than
        # queue_wait away fails at once.
        driver = self.new_driver(qps=1, driver_class=driver_class(self.fake, queue_wait=1.5))
        driver.list("/")  # takes the first turn
        results = []

        def call():
            try:
                driver.list("/")
                results.append("ok")
            except BackendError as error:
                results.append(error.message)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(len([r for r in results if "限速" in r]), 3)

    def test_listing_drops_folders_that_went_away(self):
        # A fresh listing forgets cached ids of child folders that are no longer there, without
        # waiting for folder_ttl.
        self.fake.mkdirs("/pub/share")
        self.fake.mkdirs("/priv")
        self.fake.put_file("/pub/share/a.txt", b"a")
        driver = self.new_driver("/pub")
        self.assertEqual(names(driver.list("/share")), ["a.txt"])
        with self.fake.lock:
            self.fake.find("/pub/share").parent = self.fake.find("/priv").id  # moved in 115's app
        self.assertEqual(driver.list("/"), [])
        with self.assertRaises(NotFound):
            driver.stat("/share/a.txt")

    def test_unseekable_data_is_spooled(self):
        class Socketish:
            """Like a request body: read() only, a few bytes at a time."""

            def __init__(self, data):
                self._data = io.BytesIO(data)

            def read(self, size=-1):
                return self._data.read(min(size, 1000) if size and size > 0 else 1000)

            def seekable(self):
                return False

        driver = self.new_driver()
        data = os.urandom(150 * 1024)
        driver.write("/s.bin", Socketish(data), len(data))
        self.assertEqual(read(driver, "/s.bin"), data)
        with self.assertRaises(DriverError):
            driver.write("/short.bin", Socketish(b"abc"), 10)
        with self.assertRaises(NotFound):
            driver.stat("/short.bin")

    def test_parallel_writes_share_new_folders(self):
        driver = self.new_driver()
        errors = []

        def work(i):
            try:
                driver.write(f"/par/sub/f{i}.txt", io.BytesIO(b"%d" % i), len(b"%d" % i))
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.fake.lock:
            self.assertEqual(len([i for i in self.fake.children("0") if i.name == "par"]), 1)
            par = self.fake.find("/par")
            self.assertEqual(len([i for i in self.fake.children(par.id) if i.name == "sub"]), 1)
        self.assertEqual(names(driver.list("/par/sub")), [f"f{i}.txt" for i in range(6)])

    def test_misses_are_cheap(self):
        # A missing name in a folder listed a moment ago doesn't list the folder again: the site
        # looking for readme files must not cost a listing (and a turn of qps) each time.
        driver = self.new_driver(driver_class=driver_class(self.fake, page_size=10))
        for i in range(35):
            driver.write(f"/big/f{i:02d}.txt", io.BytesIO(b"x"), 1)
        self.assertEqual(len(driver.list("/big")), 35)
        before = self.fake.counts["/open/ufile/files"]
        for name in ("README.md", "readme.md", "index.html"):
            with self.assertRaises(NotFound):
                driver.stat(f"/big/{name}")
        self.assertEqual(driver.stat("/big/f07.txt").size, 1)
        self.assertEqual(self.fake.counts["/open/ufile/files"], before)

    def test_parallel_writes_of_one_name_make_one_file(self):
        # 115 allows duplicate names; the lock per (folder, name) keeps HFile from making one.
        driver = self.new_driver()
        driver.mkdir("/race")
        errors = []

        def work(i):
            try:
                data = b"writer %d" % i
                driver.write("/race/x.txt", io.BytesIO(data), len(data))
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        with self.fake.lock:
            race = self.fake.find("/race")
            copies = [i for i in self.fake.children(race.id) if i.name == "x.txt"]
        self.assertEqual(len(copies), 1)
        self.assertTrue(read(driver, "/race/x.txt").startswith(b"writer "))

    def test_folder_moved_away_in_115(self):
        # Cached folder ids expire: a folder moved out of the base in 115's own app stops being
        # reachable through its old path.
        self.fake.mkdirs("/public")
        self.fake.mkdirs("/private")
        self.fake.put_file("/public/share/a.txt", b"a")
        driver = self.new_driver("/public", driver_class(self.fake, folder_ttl=0.05, listing_ttl=0.05))
        self.assertEqual(names(driver.list("/share")), ["a.txt"])
        with self.fake.lock:
            self.fake.find("/public/share").parent = self.fake.find("/private").id
        time.sleep(0.1)
        with self.assertRaises(NotFound):
            driver.list("/share")
        with self.assertRaises(NotFound):
            driver.stat("/share/a.txt")

    # -- downloads

    def test_download_links(self):
        driver = self.new_driver()
        data = os.urandom(3000)
        driver.write("/l.bin", io.BytesIO(data), len(data))
        self.assertEqual(read(driver, "/l.bin"), data)
        self.assertEqual(read(driver, "/l.bin", 100, 50), data[100:150])
        self.assertEqual(self.fake.counts["/open/ufile/downurl"], 1)  # the link is reused
        self.fake.expire_links()
        self.assertEqual(read(driver, "/l.bin", 2990), data[2990:])  # 403: asks for a new link
        self.assertEqual(self.fake.counts["/open/ufile/downurl"], 2)

        first = driver.download_url("/l.bin", filename="l.bin", user_agent="UA-1")
        self.assertEqual(driver.download_url("/l.bin", filename="l.bin", user_agent="UA-1"), first)
        second = driver.download_url("/l.bin", filename="l.bin", user_agent="UA-2")
        self.assertNotEqual(first, second)
        self.assertEqual(self.fake.counts["/open/ufile/downurl"], 4)
        self.assertEqual(httpx.get(second, headers={"User-Agent": "UA-2"}).content, data)
        self.assertEqual(httpx.get(second, headers={"User-Agent": "UA-1"}).status_code, 403)
        for odd in ("", "a\nb", "中文", "x" * 600):
            self.assertIsNone(driver.download_url("/l.bin", filename="l.bin", user_agent=odd))
        driver.mkdir("/folder")
        with self.assertRaises(NotFound):
            driver.download_url("/folder", filename="folder", user_agent="UA-1")
        self.assertEqual(self.fake.leaks, [])  # 115's bearer token never went to the CDN

    # -- search

    def test_search(self):
        self.fake.mkdirs("/base")
        self.fake.put_file("/base/docs/Report 2024.txt", b"1")
        self.fake.put_file("/base/docs/deep/report-final.pdf", b"22")
        self.fake.put_file("/base/other.txt", b"3")
        self.fake.mkdirs("/base/reports")
        self.fake.put_file("/outside report.txt", b"4")
        self.fake.fuzzy_search = ["other.txt"]  # 115 also returns a name without the keyword
        self.fake.search_everywhere = True  # and results outside the base folder
        driver = self.new_driver("/base")
        found = {entry.path: entry for entry in driver.search("/", "report", 10)}
        self.assertEqual(sorted(found), ["/docs/Report 2024.txt", "/docs/deep/report-final.pdf", "/reports"])
        self.assertEqual(found["/docs/deep/report-final.pdf"].size, 2)
        self.assertTrue(found["/reports"].is_dir)
        self.assertFalse(found["/docs/Report 2024.txt"].is_dir)
        self.assertEqual([e.path for e in driver.search("/docs/deep", "REPORT", 10)], ["/docs/deep/report-final.pdf"])
        self.assertEqual(len(driver.search("/", "report", 1)), 1)
        self.assertEqual(driver.search("/", "  ", 10), [])

    # -- pacing and errors

    def test_rate_limit(self):
        driver = self.new_driver(qps=20)
        start = time.monotonic()
        for _ in range(6):
            driver.list("/")
        self.assertGreaterEqual(time.monotonic() - start, 0.2)  # 5 waits of 1/20 s

    def test_error_mapping(self):
        driver = self.new_driver()
        driver.mkdir("/x")
        self.fake.fail_next["/open/folder/add"] = Fail(20004, "该目录名称已存在。")  # made meanwhile
        with self.assertRaises(AlreadyExists):
            driver.mkdir("/y")
        self.fake.fail_next["/open/ufile/delete"] = Fail(990009, "文件不存在或已删除")
        with self.assertRaises(NotFound):
            driver.delete("/x")
        self.fake.fail_next["/open/ufile/update"] = Fail(990001, "操作过于频繁，请稍后再试")
        with self.assertRaises(BackendError):
            driver.rename("/x", "z")
        driver.mkdir("/dst")
        self.fake.fail_next["/open/ufile/move"] = Fail(12345, "奇怪的错误")
        with self.assertRaises(DriverError) as caught:
            driver.move("/x", "/dst")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("12345", caught.exception.message)
        for injected, text in ((500, "HTTP 500"), (b"not json", "看不懂"), (429, "频繁")):
            self.fake.fail_next["/open/ufile/files"] = injected
            with self.subTest(injected=injected), self.assertRaises(BackendError) as caught:
                driver.list("/")
            self.assertIn(text, caught.exception.message)
        self.assertEqual(names(driver.list("/")), ["dst", "x"])

        Unreachable = driver_class(self.fake, api_root="http://127.0.0.1:1")
        broken = Unreachable(self.settings())
        self.addCleanup(broken.close)
        with self.assertRaises(BackendError) as caught:
            broken.list("/")
        self.assertIn("115", caught.exception.message)

    def test_validate_config(self):
        validate = open115.Open115Driver.validate_config
        self.assertEqual(validate({"app_id": APP_ID, "base_path": "/a", "qps": 1, "token_expires_at": 0}), {})
        self.assertEqual(validate({"app_id": APP_ID}), {})
        errors = validate({"app_id": "", "base_path": "/a/../b", "qps": 50, "token_expires_at": -1})
        self.assertEqual(set(errors), {"app_id", "base_path", "qps", "token_expires_at"})
        self.assertEqual(set(validate({"app_id": "abc def", "qps": True})), {"app_id", "qps"})
        self.assertEqual(set(validate({"app_id": None})), {"app_id"})

    def test_capabilities(self):
        cls = open115.Open115Driver
        self.assertEqual((cls.type, cls.label), ("open115", "115 网盘"))
        self.assertTrue(cls.can_direct_download and cls.can_search and cls.can_move and cls.can_copy)
        self.assertFalse(cls.can_direct_upload or cls.oauth)
        self.assertEqual(cls.helpers, {"qrcode": "扫码登录"})
        secrets = {param.key for param in cls.params if param.secret}
        self.assertEqual(secrets, {"access_token", "refresh_token", "token_expires_at"})


class Open115InPlaceTests(FakeMixin, unittest.TestCase):
    """Our own changes update the cached listings in place, so a batch of them in a big folder
    lists it once and not once per item (S5-09). 10 entries a page here: a listing of a folder
    of 30 is 3 calls. The listings' clock is frozen (FrozenClock); tests move it."""

    driver_attrs = {"page_size": 10}

    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.fake.page_limit = 10
        self.clock = FrozenClock()
        for module in (open115, _ids):  # 115's calls, and the listings of IdDriver
            patcher = mock.patch.object(module, "time", self.clock)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.driver = self.new_driver()

    def pages(self) -> int:
        return self.fake.counts["/open/ufile/files"]

    def folder(self, path: str, prefix: str = "e", folders: int = 15, files: int = 15) -> list[str]:
        """A folder made in 115's own app with `folders` folders, then `files` files; their names."""
        folder_id = self.fake.mkdirs(path)
        made = []
        for i in range(folders + files):
            name = f"{prefix}{i:02d}" if i < folders else f"{prefix}{i:02d}.txt"
            self.fake.add(folder_id, name, i < folders, b"" if i < folders else b"x" * i)
            made.append(name)
        return made

    def assertListingIsTheFakes(self, path: str) -> None:
        """The cached listing of the folder at `path` (what lookups use) holds what 115 holds."""
        listing = self.driver._listings.get(path)
        self.assertIsNotNone(listing, f"no cached listing of {path}")
        with self.fake.lock:
            expected = {(item.id, item.name, item.is_dir, None if item.is_dir else len(item.data))
                        for item in self.fake.children(self.fake.find(path).id)}
        self.assertEqual({(node.id, node.name, node.is_dir, node.size) for node in listing.nodes}, expected)

    def test_batch_delete_lists_the_folder_once(self):
        entries = self.folder("/big")
        self.driver.list("/")  # the ids of the top folders: only /big's own listing is counted below
        gone = entries[10:20]  # five folders, five files
        before = self.pages()
        for name in gone:  # what apps.files.service.delete() does: every item first, ...
            self.driver.stat(f"/big/{name}")
        for name in gone:  # ... then the deletes
            self.driver.delete(f"/big/{name}")
        self.assertEqual(self.pages() - before, 3)  # the folder's three pages, once (30 before this change)
        self.assertEqual(self.fake.counts["/open/ufile/delete"], 10)
        self.assertListingIsTheFakes("/big")
        for name in gone:
            with self.assertRaises(NotFound):
                self.driver.stat(f"/big/{name}")
        self.assertEqual(self.pages() - before, 3)
        self.assertEqual(names(self.driver.list("/big")), sorted(entries[:10] + entries[20:]))

    def test_batch_move_lists_each_folder_once(self):
        entries = self.folder("/big")
        self.folder("/big2", prefix="t")
        self.fake.add(self.fake.find("/big/e12").id, "inner.txt", False, b"in")
        self.driver.list("/")
        self.assertEqual(names(self.driver.list("/big/e12")), ["inner.txt"])  # its id is cached now
        self.clock.now += self.driver.listing_ttl + 1  # and the listing of /big this made is old
        moving = entries[12:17]  # three folders, two files
        before = self.pages()
        for name in moving:  # apps.files.service.relocate(): the item, and that its name is free there
            self.driver.stat(f"/big/{name}")
            with self.assertRaises(NotFound):
                self.driver.stat(f"/big2/{name}")
        for name in moving:
            self.driver.move(f"/big/{name}", "/big2")
        # Each folder's three pages, once: the duplicate-name check before each move uses the
        # listing of /big2 the checks above fetched a moment ago (34 pages before this change).
        self.assertEqual(self.pages() - before, 6)
        self.assertEqual(self.fake.counts["/open/ufile/move"], 5)
        self.assertListingIsTheFakes("/big")
        self.assertListingIsTheFakes("/big2")
        for name in moving:
            with self.assertRaises(NotFound):
                self.driver.stat(f"/big/{name}")
            self.assertEqual(self.driver.stat(f"/big2/{name}").name, name)
        self.assertEqual(self.pages() - before, 6)
        with self.assertRaises(NotFound):
            self.driver.stat("/big/e12/inner.txt")  # the moved folder's old path is gone at once
        self.assertEqual(names(self.driver.list("/big2/e12")), ["inner.txt"])
        self.assertEqual(read(self.driver, "/big2/e16.txt"), b"x" * 16)  # its pick code came along

    def test_batch_rename_lists_the_folder_once(self):
        self.folder("/big")
        self.fake.add(self.fake.find("/big/e00").id, "inner.txt", False, b"in")
        self.driver.list("/")
        self.assertEqual(names(self.driver.list("/big/e00")), ["inner.txt"])  # its id is cached now
        self.clock.now += self.driver.listing_ttl + 1  # and the listing of /big this made is old
        renames = {"e00": "r00", "e01": "r01", "e20.txt": "r20.txt", "e21.txt": "r21.txt", "e22.txt": "r22.txt"}
        before = self.pages()
        for old, new in renames.items():  # apps.files.service.rename(): the item, the new name, the rename
            self.driver.stat(f"/big/{old}")
            with self.assertRaises(NotFound):
                self.driver.stat(f"/big/{new}")
            self.driver.rename(f"/big/{old}", new)
        self.assertEqual(self.pages() - before, 3)  # the folder's three pages, once (27 before this change)
        self.assertListingIsTheFakes("/big")
        for old, new in renames.items():
            with self.assertRaises(NotFound):
                self.driver.stat(f"/big/{old}")
            self.assertEqual(self.driver.stat(f"/big/{new}").name, new)
        with self.assertRaises(NotFound):
            self.driver.stat("/big/e00/inner.txt")  # the renamed folder's old path is gone at once
        self.assertEqual(self.driver.stat("/big/r00/inner.txt").size, 2)
        self.assertEqual(read(self.driver, "/big/r21.txt"), b"x" * 21)

    def test_uploads_and_new_folders_are_put_in(self):
        self.driver.list("/")
        self.driver.mkdir("/up")
        before = self.pages()
        for i in range(12):
            data = b"file %d" % i
            self.driver.write(f"/up/f{i:02d}.txt", io.BytesIO(data), len(data))
        for i in range(3):
            self.driver.mkdir(f"/up/d{i}")
        self.driver.write("/up/d0/in.txt", io.BytesIO(b"in"), 2)
        self.assertEqual(self.pages() - before, 2)  # the two new folders' (empty) listings, once each (20 before)
        self.assertListingIsTheFakes("/")
        self.assertListingIsTheFakes("/up")
        self.assertListingIsTheFakes("/up/d0")
        self.assertEqual(read(self.driver, "/up/f03.txt"), b"file 3")  # the pick code from 115's answer
        self.assertTrue(self.driver.stat("/up/d2").is_dir)

    def test_overwrite_is_put_in(self):
        old = self.fake.rename_on_conflict
        self.fake.rename_on_conflict = True  # the new upload comes back as "o(1).txt"
        self.addCleanup(setattr, self.fake, "rename_on_conflict", old)
        self.driver.list("/")
        self.driver.write("/o.txt", io.BytesIO(b"first"), 5)
        before = self.pages()
        self.driver.write("/o.txt", io.BytesIO(b"second"), 6)
        self.assertEqual(self.pages() - before, 1)  # the listing that finds the new file, as before
        self.assertListingIsTheFakes("/")
        self.assertEqual(read(self.driver, "/o.txt"), b"second")
        with self.assertRaises(NotFound):
            self.driver.stat("/o(1).txt")

    def test_changes_made_in_115_still_show(self):
        folder_id = self.fake.mkdirs("/w")
        self.fake.add(folder_id, "a.txt", False, b"a")
        self.driver.list("/")
        self.driver.list("/w")  # 115 gives the listing now
        self.fake.add(folder_id, "outside", True)  # made in 115's own app
        before = self.pages()
        self.driver.mkdir("/w/mine")  # the name check trusts a listing this new
        self.assertEqual(self.pages(), before)
        self.clock.now += self.driver.guard_ttl + 0.5
        with self.assertRaises(AlreadyExists):
            self.driver.mkdir("/w/outside")  # older than guard_ttl: asks 115, which has it
        self.assertEqual(self.pages() - before, 1)
        with self.fake.lock:
            self.assertEqual([i.name for i in self.fake.children(folder_id)].count("outside"), 1)
        # A listing updated in place still expires listing_ttl seconds after 115 gave it.
        fetched = self.clock.now
        self.fake.add(folder_id, "late.txt", False, b"late")  # made in 115's own app
        self.clock.now = fetched + 20
        self.driver.delete("/w/a.txt")  # our change, 20 seconds after the fetch
        with self.assertRaises(NotFound):
            self.driver.stat("/w/late.txt")  # a miss in a listing 20 seconds old is believed, as before
        self.clock.now = fetched + self.driver.listing_ttl + 0.5
        self.assertEqual(self.driver.stat("/w/late.txt").size, 4)
        self.assertListingIsTheFakes("/w")

    def test_names_made_by_other_requests_are_taken(self):
        self.fake.put_file("/src/x", b"x")
        self.fake.put_file("/src/y.txt", b"y")
        self.fake.mkdirs("/dst")
        self.driver.list("/")
        self.driver.list("/dst")
        self.driver.list("/src")
        before = self.pages()
        self.driver.mkdir("/dst/x")  # one request makes x ...
        with self.assertRaises(AlreadyExists):
            self.driver.move("/src/x", "/dst")  # ... and another one can't put a second x there
        self.driver.write("/dst/y.txt", io.BytesIO(b"mine"), 4)
        with self.assertRaises(AlreadyExists):
            self.driver.copy("/src/y.txt", "/dst")
        with self.assertRaises(AlreadyExists):
            self.driver.rename("/dst/x", "y.txt")
        with self.assertRaises(AlreadyExists):
            self.driver.mkdir("/dst/y.txt")
        self.assertEqual(self.pages(), before)  # all of it from the listings, updated in place
        results = []

        def make():
            try:
                self.driver.mkdir("/dst/race")
                results.append("made")
            except AlreadyExists:
                results.append("exists")

        threads = [threading.Thread(target=make) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(results), ["exists", "made"])
        with self.fake.lock:
            in_dst = [item.name for item in self.fake.children(self.fake.find("/dst").id)]
        self.assertEqual(sorted(in_dst), ["race", "x", "y.txt"])  # no name twice
        self.assertListingIsTheFakes("/dst")

    def test_parallel_changes_of_one_folder_keep_each_other(self):
        entries = self.folder("/c", folders=6, files=6)
        self.driver.list("/")
        self.driver.list("/c")
        before = self.pages()
        real = _ids.make_listing

        def slow(*args):  # a slow in-place update: without the lock, two of them would overlap
            time.sleep(0.02)
            return real(*args)

        jobs = [
            lambda: [self.driver.delete(f"/c/{name}") for name in entries[0:3] + entries[6:9]],
            lambda: [self.driver.rename(f"/c/{name}", f"r-{name}") for name in entries[3:6] + entries[9:12]],
            lambda: [self.driver.mkdir(f"/c/new{i}") for i in range(4)],
        ]
        errors = []

        def work(job):
            try:
                job()
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        with mock.patch.object(_ids, "make_listing", slow):
            threads = [threading.Thread(target=work, args=(job,)) for job in jobs]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.pages(), before)  # nothing was listed again
        self.assertListingIsTheFakes("/c")
        expected = [f"new{i}" for i in range(4)] + [f"r-{name}" for name in entries[3:6] + entries[9:12]]
        self.assertEqual(names(self.driver.list("/c")), sorted(expected))

    def test_a_listing_fetched_during_a_change_is_not_kept(self):
        folder_id = self.fake.mkdirs("/s")
        for name in ("a.txt", "b.txt"):
            self.fake.add(folder_id, name, False, b"x")
        self.driver.list("/")
        paused, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)  # (a failure below doesn't leave the lister waiting)
        listed = []
        real = self.driver._fetch

        def fetch(fid):
            nodes = real(fid)
            if threading.current_thread() is lister:  # 115 has answered with a.txt; now the delete
                paused.set()
                resume.wait(10)
            return nodes

        self.driver._fetch = fetch
        lister = threading.Thread(target=lambda: listed.append(names(self.driver.list("/s"))))
        lister.start()
        self.assertTrue(paused.wait(10))
        self.driver.delete("/s/a.txt")  # lists /s itself, then takes a.txt out of that listing
        resume.set()
        lister.join(10)
        self.assertEqual(listed, [["a.txt", "b.txt"]])  # the lister's answer is from before the delete
        with self.assertRaises(NotFound):
            self.driver.stat("/s/a.txt")  # ... and wasn't kept
        self.assertListingIsTheFakes("/s")

    def test_a_folder_renamed_while_listed_is_not_kept_at_its_old_path(self):
        self.fake.put_file("/p/A/f.txt", b"f")
        self.driver.list("/")
        self.driver.list("/p")  # the id of /p/A is cached
        resolved, resume = threading.Event(), threading.Event()
        self.addCleanup(resume.set)
        listed = []
        real = self.driver._folder_id

        def folder_id(path):
            found = real(path)
            if threading.current_thread() is lister and path == "/p/A":  # the old id; now the rename
                resolved.set()
                resume.wait(10)
            return found

        self.driver._folder_id = folder_id
        lister = threading.Thread(target=lambda: listed.append(names(self.driver.list("/p/A"))))
        lister.start()
        self.assertTrue(resolved.wait(10))
        self.driver.rename("/p/A", "B")
        resume.set()
        lister.join(10)
        self.assertEqual(listed, [["f.txt"]])  # that one answer came from the folder's old path
        with self.assertRaises(NotFound):
            self.driver.stat("/p/A/f.txt")  # but it wasn't kept there
        self.assertEqual(self.driver.stat("/p/B/f.txt").size, 1)

    def test_a_write_keeps_the_folder_it_found_while_its_making_is_unanswered(self):
        # Parallel uploads into a new folder: the writer finds /n/sub in 115 while the request
        # making it still waits for 115's answer, so the cached listing of /n doesn't show sub
        # yet. The writer goes on with the id it found; looking the path up again said NotFound.
        self.fake.mkdirs("/n")
        self.driver.list("/")
        n_id = self.driver._folders.get("/n")
        listing_n, made, go_writer, go_maker = (threading.Event() for _ in range(4))
        for event in (go_writer, go_maker):
            self.addCleanup(event.set)
        real_fetch, real_call = self.driver._fetch, self.driver._call

        def fetch(folder_id):
            if threading.current_thread() is writer and folder_id == n_id and not listing_n.is_set():
                listing_n.set()  # the writer is about to list /n: first 115 makes sub
                go_writer.wait(10)
            return real_fetch(folder_id)

        def call(method, path, **kwargs):
            answer = real_call(method, path, **kwargs)
            if threading.current_thread() is maker and path == "/open/folder/add":
                made.set()  # 115 has made sub; its answer hasn't come back
                go_maker.wait(10)
            return answer

        self.driver._fetch, self.driver._call = fetch, call
        errors = []

        def run(job):
            try:
                job()
            except Exception as error:  # noqa: BLE001 - collected for the assertion
                errors.append(error)

        writer = threading.Thread(target=run, args=(lambda: self.driver.write("/n/sub/b.txt", io.BytesIO(b"b"), 1),))
        maker = threading.Thread(target=run, args=(lambda: self.driver.mkdir("/n/sub"),))
        writer.start()
        self.assertTrue(listing_n.wait(10))
        maker.start()  # lists /n (no sub; that listing is cached), then makes sub
        self.assertTrue(made.wait(10))
        self.driver.mkdir("/other")  # a change of ours: what the writer now gets from 115 isn't cached
        go_writer.set()
        writer.join(10)
        go_maker.set()
        maker.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(read(self.driver, "/n/sub/b.txt"), b"b")
        with self.fake.lock:
            self.assertEqual([item.name for item in self.fake.children(self.fake.find("/n").id)], ["sub"])

    def test_a_failed_change_drops_the_listings(self):
        folder_id = self.fake.mkdirs("/f")
        self.fake.add(folder_id, "a.txt", False, b"a")
        self.fake.mkdirs("/g")
        self.driver.list("/")
        changes = {
            "/open/ufile/delete": lambda: self.driver.delete("/f/a.txt"),
            "/open/ufile/move": lambda: self.driver.move("/f/a.txt", "/g"),
            "/open/ufile/update": lambda: self.driver.rename("/f/a.txt", "b.txt"),
            "/open/folder/add": lambda: self.driver.mkdir("/f/new"),
        }
        for endpoint, change in changes.items():
            with self.subTest(endpoint=endpoint):
                self.driver.list("/f")
                self.driver.list("/g")
                self.fake.fail_next[endpoint] = 502  # 115 may have done it or not
                with self.assertRaises(BackendError):
                    change()
                before = self.pages()
                self.assertEqual(self.driver.stat("/f/a.txt").size, 1)  # asked 115 again
                self.assertEqual(self.pages() - before, 1)
        # A copy's new id isn't known: the target's listing is dropped, as before.
        self.driver.list("/g")
        self.driver.copy("/f/a.txt", "/g")
        before = self.pages()
        self.assertEqual(self.driver.stat("/g/a.txt").size, 1)
        self.assertEqual(self.pages() - before, 1)


if __name__ == "__main__":
    unittest.main()
