"""UpYun: the contract against an in-memory UpYun (fake_upyun), signatures from UpYun's
documentation, paging, the resumable upload, form uploads, links with token anti-leech, errors,
names and params."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import json
import logging
import os
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx

from apps.storages.drivers import driver_class, upyun
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotFound
from apps.storages.drivers.upyun import UpYunDriver, anti_leech_token, form_signature, signature, upyun_error
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_upyun import FakeUpYun

fake: FakeUpYun


def setUpModule():
    global fake
    fake = FakeUpYun()
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def tearDownModule():
    fake.stop()


def make(test=None, cls=UpYunDriver, **changes) -> UpYunDriver:
    attrs = {"api_url": fake.api.url, **changes.pop("attrs", {})}
    values = {"bucket": fake.bucket, "operator": fake.operator, "password": fake.password, "base_path": "/",
              "domain": fake.cdn.url, "token": ""}
    values.update(changes)
    driver = type("Local", (cls,), attrs)(values)
    if test is not None:
        test.addCleanup(driver.close)
    return driver


def put(driver, path, data):
    driver.write(path, io.BytesIO(data), len(data))


def read(driver, path, offset=0, length=None):
    with driver.open(path, offset, length) as stream:
        return b"".join(stream)


class UpYunRootContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        fake.reset()
        return make()


class UpYunBasePathContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        fake.reset()
        return make(base_path="/base 目录/子")


class SignatureTests(unittest.TestCase):
    """help.upyun.com/knowledge-base/object_storage_authorization"""

    password_md5 = hashlib.md5(b"password123").hexdigest()

    def test_rest_example(self):
        self.assertEqual(self.password_md5, "482c811da5d5b4bc6d497ffa98491e38")
        self.assertEqual(signature(self.password_md5, "PUT", "/upyun-temp/demo.jpg", "Wed, 09 Nov 2016 14:26:58 GMT",
                                   "7ac66c0f148de9519b8bd264312c4d64"), "YUaAZX+WNAcJdNGHS5SBlITME5A=")

    def test_form_example(self):
        policy = ("eyJidWNrZXQiOiJ1cHl1bi10ZW1wIiwic2F2ZS1rZXkiOiIvZGVtby5qcGciLCJleHBpcmF0aW9uIjoiMTQ3ODY3NDYxOCIsImRh"
                  "dGUiOiJXZWQsIDA5IE5vdiAyMDE2IDE0OjI2OjU4IEdNVCIsImNvbnRlbnQtbWQ1IjoiN2FjNjZjMGYxNDhkZTk1MTliOGJkMjY0"
                  "MzEyYzRkNjQifQ==")
        message = f"POST&/upyun-temp&Wed, 09 Nov 2016 14:26:58 GMT&{policy}&7ac66c0f148de9519b8bd264312c4d64"
        digest = hmac.new(self.password_md5.encode(), message.encode(), hashlib.sha1).digest()
        self.assertEqual(base64.b64encode(digest).decode(), "k+fHTJndCFAraoeIrd60sJ/8Vb8=")
        # Our form uploads leave out the optional Content-MD5, so the signature ends at the policy.
        self.assertEqual(form_signature(self.password_md5, "upyun-temp", "Wed, 09 Nov 2016 14:26:58 GMT", policy),
                         signature(self.password_md5, "POST", "/upyun-temp", "Wed, 09 Nov 2016 14:26:58 GMT", policy))

    def test_anti_leech_token(self):
        # help.upyun.com/knowledge-base/cdn-token-limite: MD5(secret&etime&URI), 8 characters from
        # the 13th, followed by etime.
        digest = hashlib.md5(b"secret&1370000600&/dir/pic.jpg").hexdigest()
        self.assertEqual(anti_leech_token("secret", 1370000600, "/dir/pic.jpg"), digest[12:20] + "1370000600")


class OperationTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_paging(self):
        d = make(self, attrs={"page_size": 2})
        for index in range(5):
            put(d, f"/p/f{index}", b"x")
        for index in range(2):
            d.mkdir(f"/p/d{index}")
        self.assertEqual(sorted(e.name for e in d.list("/p")), ["d0", "d1", "f0", "f1", "f2", "f3", "f4"])
        pages = [headers for method, path, headers in fake.requests if method == "GET" and path.endswith("/p/")]
        self.assertGreaterEqual(len(pages), 4)
        self.assertTrue(all(h.get("x-list-limit") == "2" for h in pages))
        self.assertTrue(any(h.get("x-list-iter") for h in pages))

    def test_folders_more_than_a_thousand_deep(self):
        """STOR-06: deleting, moving and copying a folder went down it by recursion: one more than about a thousand
        folders deep (other programs make such) hit Python's limit, a 500 with nothing deleted. A stack of
        folders goes as deep as they do."""
        d = make(self)
        depth = 1100
        with fake.lock:
            for level in range(depth + 1):
                fake.folders.add("/top" + "/d" * level)
                fake.dates["/top" + "/d" * level] = fake.now()
            fake.files["/top" + "/d" * depth + "/f.txt"] = b"x"
            fake.dates["/top" + "/d" * depth + "/f.txt"] = fake.now()
        d.mkdir("/other")
        d.copy("/top", "/other")
        with self.assertRaises(AlreadyExists):  # /top is there: refused before anything is done
            d.move("/other/top", "/")
        self.assertEqual(read(d, "/other/top" + "/d" * depth + "/f.txt"), b"x")
        d.delete("/top")
        d.move("/other/top", "/")
        self.assertEqual(read(d, "/top" + "/d" * depth + "/f.txt"), b"x")
        with fake.lock:
            self.assertFalse(any(path.startswith("/other/") for path in fake.folders | set(fake.files)))
        d.delete("/top")
        with fake.lock:
            self.assertEqual((fake.folders, fake.files), ({"/", "/other"}, {}))

    def test_resumable_upload(self):
        d = make(self, attrs={"single_put_limit": 1024 * 1024, "min_part_size": 1024 * 1024})
        data = os.urandom(2 * 1024 * 1024 + 500)
        put(d, "/deep/folder/big.bin", data)
        self.assertEqual(read(d, "/deep/folder/big.bin"), data)
        stages = [h.get("X-Upyun-Multi-Stage") for method, path, h in fake.requests if h.get("X-Upyun-Multi-Stage")]
        self.assertEqual(stages, ["initiate", "upload", "upload", "upload", "complete"])
        d.write("/stream.bin", NonSeekable(data), len(data))
        self.assertEqual(read(d, "/stream.bin", 1024 * 1024 + 10, 100), data[1024 * 1024 + 10:][:100])

    def test_form_upload(self):
        d = make(self)
        data = b"form upload data"
        target = d.upload_target("/up/文件 1.txt", len(data))
        self.assertEqual((target.kind, target.method, target.file_field), ("form", "POST", "file"))
        self.assertEqual(target.url, f"{fake.api.url}/{fake.bucket}")
        policy = json.loads(base64.b64decode(target.fields["policy"]))
        self.assertEqual((policy["save-key"], policy["content-length-range"]), ("/up/文件 1.txt", f"{len(data)},{len(data)}"))
        response = httpx.post(target.url, data=dict(target.fields), files={"file": ("文件 1.txt", data)})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(read(d, "/up/文件 1.txt"), data)
        other = d.upload_target("/up/other.txt", 3)
        response = httpx.post(other.url, data=dict(other.fields), files={"file": ("other.txt", b"too long")})
        self.assertEqual(response.status_code, 403)  # the policy fixes the size
        self.assertIsNone(d.upload_target("/huge.bin", 100 * 1024 * 1024 + 1))
        # The form API would replace {year} and {filename} (chosen by the browser) in save-key.
        self.assertIsNone(d.upload_target("/up/report{year}.pdf", 3))
        self.assertIsNone(d.upload_target("/up/{filename}{.suffix}", 3))
        # ... and in the base path, with which save-key starts.
        self.assertIsNone(make(self, base_path="/backup {year}").upload_target("/up/a.txt", 3))
        d.mkdir("/folder")
        with self.assertRaises(AlreadyExists):
            d.upload_target("/folder", 10)

    def test_links(self):
        d = make(self, base_path="/base")
        put(d, "/a dir/报告.pdf", b"%PDF")
        url = d.download_url("/a dir/报告.pdf", filename="报告.pdf")
        self.assertEqual(url, f"{fake.cdn.url}/base/a%20dir/%E6%8A%A5%E5%91%8A.pdf?_upd=%E6%8A%A5%E5%91%8A.pdf")
        response = httpx.get(url)
        self.assertEqual(response.content, b"%PDF")
        self.assertIn("%E6%8A%A5%E5%91%8A.pdf", response.headers["content-disposition"])
        d = make(self, domain="")
        self.assertTrue(d.download_url("/x", filename="").startswith(f"http://{fake.bucket}.test.upcdn.net/x"))

    def test_token_anti_leech(self):
        fake.token = "leech-key"
        self.addCleanup(setattr, fake, "token", "")
        d = make(self, base_path="/base", token="leech-key")
        put(d, "/f.txt", b"guarded")
        url = d.download_url("/f.txt", filename="f.txt", expires=600)
        upt = parse_qs(urlsplit(url).query)["_upt"][0]
        self.assertTrue(abs(int(upt[8:]) - (time.time() + 600)) < 5)
        self.assertEqual(httpx.get(url).content, b"guarded")
        self.assertEqual(httpx.get(url.replace("_upt=" + upt[:2], "_upt=zz")).status_code, 403)
        self.assertEqual(httpx.get(make(self, base_path="/base").download_url("/f.txt", filename="")).status_code, 403)

    def test_folder_operations(self):
        d = make(self, attrs={"workers": 3})
        for index in range(4):
            put(d, f"/src/a/f{index}", b"%d" % index)
        put(d, "/src/a/b/c/deep.txt", b"deep")
        d.mkdir("/src/a/empty")
        d.copy("/src/a", "/")
        d.rename("/a", "moved")
        d.mkdir("/dst")
        d.move("/moved", "/dst")
        self.assertEqual(sorted(e.name for e in d.list("/dst/moved")), ["b", "empty", "f0", "f1", "f2", "f3"])
        self.assertEqual(read(d, "/dst/moved/b/c/deep.txt"), b"deep")
        d.delete("/src")
        self.assertEqual(sorted(fake.files), ["/dst/moved/b/c/deep.txt"] + [f"/dst/moved/f{i}" for i in range(4)])
        self.assertEqual(sorted(fake.folders), ["/", "/dst", "/dst/moved", "/dst/moved/b", "/dst/moved/b/c",
                                                "/dst/moved/empty"])

    def test_names_of_dots_and_spaces_only(self):
        d = make(self, base_path="/dots")
        put(d, "/d/ok.txt", b"ok")
        with fake.lock:
            fake.files["/dots/d/..."] = b"x"
            fake.folders |= {"/dots/d/.. "}
        self.assertEqual([e.name for e in d.list("/d")], ["ok.txt"])

    def test_confinement(self):
        d = make(self, base_path="/base")
        with fake.lock:
            fake.folders |= {"/base", "/base2"}
            fake.files["/base2/secret"] = b"s"
        put(d, "/inside", b"i")
        self.assertEqual([e.name for e in d.list("/")], ["inside"])
        for call in (lambda: d.stat("/../base2/secret"), lambda: d.open("/.."), lambda: d.delete("/"),
                     lambda: d.rename("/inside", "../x"), lambda: d.list("//"), lambda: d.stat("/a\ud800b"),
                     lambda: make(self, base_path="/a/../b").list("/")):
            with self.assertRaises(InvalidPath):
                call()
        self.assertIn("/base2/secret", fake.files)


class NonSeekable(io.RawIOBase):
    def __init__(self, data):
        self._data = io.BytesIO(data)

    def readable(self):
        return True

    def read(self, size=-1):
        return self._data.read(size)


class Sleeps:
    """upyun's `time`, whose sleep() only notes how long it was asked to wait."""

    def __init__(self):
        self.slept: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)

    def __getattr__(self, name):
        return getattr(time, name)


@mock.patch.object(UpYunDriver, "_pause", staticmethod(lambda attempt: None))
class ErrorTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_wrong_password(self):
        with self.assertRaises(AuthError) as caught:
            make(self, password="wrong").check()
        self.assertIn("40100005", str(caught.exception))
        self.assertNotIn("wrong", str(caught.exception))

    def test_unknown_service(self):
        with self.assertRaises(BackendError) as caught:
            make(self, bucket="other-service").list("/")
        self.assertIn("服务名称", str(caught.exception))

    def test_transient_failures(self):
        d = make(self)
        fake.fail = [("PUT", 503), ("PUT", 429)]
        put(d, "/a.txt", b"retried")
        self.assertEqual(read(d, "/a.txt"), b"retried")
        fake.fail = [(None, 503)] * 3
        with self.assertRaises(BackendError):
            d.stat("/a.txt")
        fake.fail = [("PUT", 503)]
        with self.assertRaises(BackendError):
            d.write("/b.txt", NonSeekable(b"once"), 4)  # can't be sent again
        self.assertNotIn("/b.txt", fake.files)

    def test_a_folder_delete_waits_out_the_rate_limit(self):
        """UpYun limits how often synchronous deletes may come, and answers 429 above that. A folder delete
        sends one per file, and gave up after three tries a fraction of a second apart, leaving the folder
        half deleted. A refused delete waits seconds now, longer each time."""
        d = make(self, attrs={"workers": 1})
        for index in range(3):
            put(d, f"/big/f{index}", b"x")
        sleeps = Sleeps()
        fake.fail = [("DELETE", 429)] * 5
        with mock.patch.object(upyun, "time", sleeps):
            d.delete("/big")
        with fake.lock:
            self.assertEqual((fake.files, fake.folders), ({}, {"/"}))
        self.assertEqual(len(sleeps.slept), 5)
        self.assertGreaterEqual(sleeps.slept[0], 0.5)
        self.assertEqual(sleeps.slept, sorted(sleeps.slept))

    def test_clock_skew(self):
        d = make(self)
        d.clock_offset = -7200  # our clock two hours late
        put(d, "/a.txt", b"x")
        self.assertLess(abs(d.clock_offset), 5)

    def test_missing_and_short(self):
        d = make(self)
        with self.assertRaises(NotFound):
            d.open("/nope")
        with self.assertRaises(DriverError):
            d.write("/short", NonSeekable(b"abc"), 10)

    def test_server_errors_map(self):
        def handler(request):
            if request.method == "HEAD":
                return httpx.Response(404)
            return httpx.Response(403, json={"msg": "bucket is full", "code": 40300004, "id": "x"})

        d = make(self, attrs={"transport": httpx.MockTransport(handler)})
        with self.assertRaises(DriverError) as caught:
            d.mkdir("/x")
        self.assertIn("满了", str(caught.exception))


class DigitTests(unittest.TestCase):
    def test_an_error_code_in_ascii_digits_only(self):
        """STOR-17: a code of "²" passed isdigit(), and int() raised a ValueError instead of the storage's error."""
        error = upyun_error(500, "²", "odd")
        self.assertIsInstance(error, BackendError)
        self.assertIn("odd", error.message)


class ParamsTests(unittest.TestCase):
    def test_params(self):
        cls = driver_class("upyun")
        self.assertIs(cls, UpYunDriver)
        self.assertEqual([p.key for p in cls.params], ["bucket", "operator", "password", "base_path", "domain", "token"])
        self.assertEqual([p.key for p in cls.params if p.secret], ["password", "token"])
        good = {"bucket": "svc", "operator": "op", "password": "pw", "base_path": "/", "domain": "", "token": ""}
        self.assertEqual(cls.validate_config(good), {})
        for key, value in [("bucket", ""), ("bucket", "a/b"), ("operator", ""), ("operator", "a b"), ("operator", "操作员"),
                           ("password", ""),
                           ("base_path", "/a/../b"), ("domain", "files.example.com")]:
            with self.subTest(key=key, value=value):
                self.assertIn(key, cls.validate_config(dict(good, **{key: value})))


if __name__ == "__main__":
    unittest.main()
