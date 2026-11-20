"""DogeCloud: a fake of its token API (checking the TOKEN signature) handing out credentials for
buckets in moto; the contract through it, credential caching and refresh, errors and links."""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import logging
import threading
import time
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

import httpx

from django.utils import translation

from apps.storages.drivers import driver_class
from apps.storages.drivers.base import AuthError, BackendError, DriverError, Unsupported
from apps.storages.drivers.dogecloud import DogeCloudDriver, api_signature
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_http import Handler, Server
from apps.storages.tests.moto_s3 import MotoS3

ACCESS_KEY = "doge-access-key"
SECRET_KEY = "doge-secret-key"


class FakeDogeCloud:
    """POST /auth/tmp_token.json and GET /oss/bucket/list.json of api.dogecloud.com."""

    def __init__(self):
        self.buckets: dict[str, tuple[str, str]] = {}  # name -> (s3Endpoint, s3Bucket)
        self.lifetime = 7200
        self.delay = 0.0
        self.fail: tuple[int, object] | None = None  # (HTTP status, JSON value or raw bytes)
        self.token_calls = 0
        self.lock = threading.Lock()
        self.server = Server(DogeHandler, self)


class DogeHandler(Handler):
    def do_POST(self):
        self.handle_api()

    def do_GET(self):
        self.handle_api()

    def handle_api(self):
        fake = self.fake
        body = self.body().decode()
        expected = f"TOKEN {ACCESS_KEY}:{hmac.new(SECRET_KEY.encode(), f'{self.path}\n{body}'.encode(), hashlib.sha1).hexdigest()}"
        if self.headers.get("Authorization") != expected:
            return self.answer_json(200, {"code": 401, "msg": "ERROR_UNAUTHORIZED"})
        if fake.fail is not None:
            status, value = fake.fail
            if isinstance(value, bytes):
                return self.answer(status, value, {"Content-Type": "text/html"})
            return self.answer_json(status, value)
        if self.command == "POST" and self.path == "/auth/tmp_token.json":
            if self.headers.get("Content-Type") != "application/json":
                return self.answer_json(400, {"code": 400, "msg": "ERROR_CONTENT_TYPE"})
            request = json.loads(body)
            if request.get("channel") != "OSS_FULL" or not isinstance(request.get("scopes"), list) \
                    or not 0 < request.get("ttl", 7200) <= 7200:
                return self.answer_json(200, {"code": 400, "msg": "ERROR_PARAMS"})
            with fake.lock:
                fake.token_calls += 1
                number = fake.token_calls
            time.sleep(fake.delay)
            buckets = [{"name": name, "s3Bucket": fake.buckets[name][1], "s3Endpoint": fake.buckets[name][0]}
                       for name in request["scopes"] if name in fake.buckets]
            return self.answer_json(200, {"code": 200, "msg": "OK", "data": {
                "Credentials": {"accessKeyId": f"tmp-ak-{number}", "secretAccessKey": f"tmp-sk-{number}",
                                "sessionToken": f"session-{number}"},
                "ExpiredAt": int(time.time()) + fake.lifetime, "Buckets": buckets}})
        if self.command == "GET" and self.path == "/oss/bucket/list.json":
            return self.answer_json(200, {"code": 200, "msg": "OK", "data": {"buckets": [
                {"name": name, "ctime": 1546510930, "region": 0, "config": {}, "space": 0} for name in fake.buckets]}})
        return self.answer_json(404, {"code": 404, "msg": "ERROR_NOT_FOUND"})


moto: MotoS3
doge: FakeDogeCloud


def setUpModule():
    global moto, doge
    moto = MotoS3()
    doge = FakeDogeCloud()
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def tearDownModule():
    doge.server.stop()
    moto.stop()


def new_bucket() -> str:
    name = f"doge-{uuid.uuid4().hex[:8]}"
    doge.buckets[name] = (moto.endpoint, moto.bucket())
    return name


def make(test=None, **changes) -> DogeCloudDriver:
    attrs = {"api_url": doge.server.url, **changes.pop("attrs", {})}
    values = {"access_key": ACCESS_KEY, "secret_key": SECRET_KEY, "base_path": "/", "domain": ""}
    values["bucket"] = changes.pop("bucket") if "bucket" in changes else new_bucket()
    values.update(changes)
    driver = type("Local", (DogeCloudDriver,), attrs)(values)
    if test is not None:
        test.addCleanup(driver.close)
    return driver


class DogeCloudContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        return make()


class CredentialTests(unittest.TestCase):
    def setUp(self):
        doge.fail = None
        doge.lifetime = 7200
        doge.delay = 0.0

    def calls(self, action) -> int:
        before = doge.token_calls
        action()
        return doge.token_calls - before

    def test_documented_signature(self):
        # docs.dogecloud.com/oss/api-access-token
        self.assertEqual(api_signature("MY_SECRET_KEY", "/auth/upload.json?filename=a.mp4", ""),
                         "bf5ec167c882d6ffa8afa4a1d2c2ed8d622beadf")

    def test_cached(self):
        d = make(self)

        def work():
            d.write("/a.txt", io.BytesIO(b"abc"), 3)
            d.list("/")
            d.stat("/a.txt")
            with d.open("/a.txt") as stream:
                self.assertEqual(b"".join(stream), b"abc")

        self.assertEqual(self.calls(work), 1)
        self.assertTrue(d._s3().credentials().token.startswith("session-"))

    def test_refreshed_before_they_expire(self):
        d = make(self)
        doge.lifetime = 1000
        d.refresh_margin = 990
        d.list("/")
        first = d._s3().credentials()
        self.assertEqual(self.calls(lambda: d.list("/")), 0)
        d.refresh_margin = 1500  # now they expire "soon"
        doge.lifetime = 2000  # the new ones don't (else every call fetches again)
        self.assertEqual(self.calls(lambda: d.list("/")), 1)
        self.assertNotEqual(d._s3().credentials().token, first.token)

    def test_one_fetch_for_many_threads(self):
        d = make(self)
        doge.delay = 0.3
        threads = [threading.Thread(target=d._credentials) for _ in range(8)]

        def run():
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(self.calls(run), 1)

    def test_moved_bucket(self):
        """DogeCloud says a bucket's s3Endpoint and s3Bucket may change ("有可能发生变化", docs.dogecloud.com/oss/
        manual-tmp-token): the client kept the first ones, and the credentials that came with the new ones were sent
        to the old bucket. Requests follow the new ones from the next fetch on."""
        name = new_bucket()
        d = make(self, bucket=name)
        d.write("/old.txt", io.BytesIO(b"old"), 3)
        moved = moto.bucket()
        moto.put_raw(moved, "new.txt", b"new")
        doge.buckets[name] = (moto.endpoint, moved)
        d._forget_credentials()  # fetched again, as when they near their end
        self.assertEqual(self.calls(lambda: self.assertEqual([e.name for e in d.list("/")], ["new.txt"])), 1)
        with d.open("/new.txt") as stream:
            self.assertEqual(b"".join(stream), b"new")
        self.assertIn(f"/{moved}/a.txt?", d.download_url("/a.txt", filename="a.txt"))

    def test_rejected_credentials_are_fetched_again(self):
        tokens = []

        def s3(request):
            tokens.append(request.headers.get("x-amz-security-token"))
            if len(tokens) == 1:
                return httpx.Response(403, content=b"<Error><Code>ExpiredToken</Code><Message>expired</Message></Error>")
            return httpx.Response(200, content=b"<ListBucketResult><IsTruncated>false</IsTruncated></ListBucketResult>")

        d = make(self, attrs={"transport": httpx.MockTransport(s3)})
        self.assertEqual(self.calls(lambda: self.assertEqual(d.list("/"), [])), 2)
        self.assertEqual(len(tokens), 2)
        self.assertNotEqual(tokens[0], tokens[1])

    def test_errors(self):
        with self.assertRaises(AuthError):
            make(self, secret_key="wrong").check()
        for fail, error in [((500, {"code": 500, "msg": "ERROR_INTERNAL"}), BackendError),
                            ((200, {"code": 200, "msg": "OK", "data": {"Buckets": []}}), BackendError),
                            ((502, b"<html>bad gateway</html>"), BackendError),
                            ((403, {"code": 403, "msg": "ERROR_FORBIDDEN"}), AuthError)]:
            with self.subTest(fail=fail):
                doge.fail = fail
                with self.assertRaises(error) as caught:
                    make(self).check()
                self.assertNotIn(SECRET_KEY, str(caught.exception))
        doge.fail = None
        with self.assertRaises(BackendError) as caught:
            make(self, bucket="no-such-bucket").check()
        self.assertIn("no-such-bucket", str(caught.exception))

    def test_links(self):
        d = make(self, domain="https://cdn.example.com", base_path="/base")
        self.assertEqual(d.download_url("/a b.txt", filename="a b.txt"), "https://cdn.example.com/base/a%20b.txt")
        d = make(self)
        d.write("/a.txt", io.BytesIO(b"hello"), 5)
        url = d.download_url("/a.txt", filename="a.txt", expires=600)
        query = parse_qs(urlsplit(url).query)
        self.assertTrue(query["X-Amz-Security-Token"][0].startswith("session-"))
        self.assertEqual(query["X-Amz-Expires"], ["600"])
        self.assertEqual(httpx.get(url).content, b"hello")

    def test_link_lasts_no_longer_than_the_credentials(self):
        doge.lifetime = 1000
        d = make(self)
        expires = int(parse_qs(urlsplit(d.download_url("/a.txt", filename="a.txt", expires=3600)).query)["X-Amz-Expires"][0])
        self.assertTrue(900 < expires <= 990, expires)
        self.assertEqual(self.calls(lambda: d.download_url("/a.txt", filename="a.txt", expires=3600)), 0)

    def test_upload_target(self):
        d = make(self)
        target = d.upload_target("/up.bin", 4)
        self.assertIn("X-Amz-Security-Token", parse_qs(urlsplit(target.url).query))
        self.assertEqual(httpx.put(target.url, content=b"data", headers=dict(target.headers)).status_code, 200)
        with d.open("/up.bin") as stream:
            self.assertEqual(b"".join(stream), b"data")


class HelperAndParamTests(unittest.TestCase):
    def test_buckets(self):
        name = new_bucket()
        answer = type("Local", (DogeCloudDriver,), {"api_url": doge.server.url}).helper(
            "buckets", {"access_key": ACCESS_KEY, "secret_key": SECRET_KEY}, {})
        self.assertEqual(answer["param"], "bucket")
        self.assertIn(name, [choice["value"] for choice in answer["choices"]])
        with self.assertRaises(DriverError):
            DogeCloudDriver.helper("buckets", {"access_key": ACCESS_KEY}, {})
        with self.assertRaises(DriverError):
            DogeCloudDriver.helper("buckets", {"access_key": "密钥", "secret_key": SECRET_KEY}, {})
        with self.assertRaises(Unsupported):
            DogeCloudDriver.helper("cors", {"access_key": ACCESS_KEY, "secret_key": SECRET_KEY}, {})

    def test_params(self):
        cls = driver_class("doge-cloud")
        self.assertIs(cls, DogeCloudDriver)
        self.assertEqual([p.key for p in cls.params], ["access_key", "secret_key", "bucket", "base_path", "domain"])
        self.assertEqual([p.key for p in cls.params if p.secret], ["secret_key"])
        good = {"access_key": "a", "secret_key": "b", "bucket": "mybucket", "base_path": "/", "domain": ""}
        self.assertEqual(cls.validate_config(good), {})
        self.assertIn("bucket", cls.validate_config(dict(good, bucket="")))
        self.assertIn("secret_key", cls.validate_config(dict(good, secret_key="")))
        self.assertIn("base_path", cls.validate_config(dict(good, base_path="/../x")))
        self.assertIn("domain", cls.validate_config(dict(good, domain="ftp://x")))

    def test_help(self):
        """Basic (基础型) buckets have no SDK or API access, only standard (标准型) ones (docs.dogecloud.com/oss/
        manual-bucket-create); COS underneath doesn't serve .apk and .ipa files through the default domain of buckets
        created since 2024."""
        helps = {p.key: p.help for p in DogeCloudDriver.params}
        with translation.override("en"):
            self.assertIn("standard", str(helps["bucket"]))
        self.assertIn(".apk", str(helps["domain"]))


if __name__ == "__main__":
    unittest.main()
