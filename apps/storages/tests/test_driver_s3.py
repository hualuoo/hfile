"""The S3 driver family (s3, minio, aliyun, tencent, huawei) against moto, plus SigV4 signing
(AWS's published examples, botocore as an oracle), addressing, paging, multipart, links,
error mapping, names, confinement, params and helpers."""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import io
import logging
import os
import re
import threading
import time
import types
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from apps.storages.drivers import driver_class, s3
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotFound,
    ReadStream,
    Unsupported,
)
from apps.storages.drivers.s3 import (
    EMPTY_SHA256,
    AliyunDriver,
    Credentials,
    HuaweiDriver,
    MinIODriver,
    S3Client,
    S3Driver,
    TencentDriver,
    canonical_request,
    content_disposition,
    oss_region,
    oss_v1_signature,
    oss_v4_query,
    presign,
    sign_headers,
    string_to_sign,
    uri_encode,
    url_query,
)
from apps.storages.tests import fake_http
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.moto_s3 import MotoS3

moto: MotoS3


def setUpModule():
    global moto
    moto = MotoS3()
    # Retries and errors are logged as warnings; keep them out of the test output.
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def tearDownModule():
    moto.stop()


def config(**changes):
    values = {"endpoint": moto.endpoint, "access_key": "AKIDTEST", "secret_key": "secret-test-key",
              "bucket": changes.pop("bucket") if "bucket" in changes else moto.bucket(), "base_path": "/",
              "path_style": "path",
              "is_private": True, "domain": "", "region": ""}
    values.update(changes)
    return values


def with_attrs(cls, **attrs):
    """A subclass of a driver with class attributes changed (sizes, transport)."""
    return type(cls.__name__, (cls,), attrs)


def fetch(url, **kwargs):
    with httpx.Client(transport=moto.transport(), timeout=30) as client:
        return client.get(url, **kwargs)


class ViaLocalDownloads:
    def test_download_url(self):
        # The link's host is "<bucket>.<endpoint host>", which only ToLocal can reach.
        data = os.urandom(5000)
        self.put("/dl/文件 1.bin", data)
        url = self.d.download_url("/dl/文件 1.bin", filename="文件 1.bin", expires=600)
        response = fetch(url)
        self.assertEqual(response.status_code, 200, response.text[:300])
        self.assertEqual(response.content, data)


# --- the contract, for every type ---------------------------------------------------------------

class S3PathStyleContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        return S3Driver(config())


class S3BasePathContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        return S3Driver(config(base_path="/base dir/子 文件夹"))


class S3VirtualHostedContract(ViaLocalDownloads, DriverContract, unittest.TestCase):
    def make_driver(self):
        cls = with_attrs(S3Driver, transport=moto.transport())
        return cls(config(endpoint=f"http://s3.test:{moto.port}", path_style="virtual"))


class S3SignedContract(DriverContract, unittest.TestCase):
    """moto checks every signature (botocore recomputes it from the request it got)."""

    def make_driver(self):
        access_key, secret_key = self.enterContext(moto.checking_signatures())
        bucket = f"signed{os.urandom(8).hex()}"
        client = S3Client(endpoint=moto.endpoint, bucket=bucket, region="us-east-1", path_style=True,
                          credentials=lambda: Credentials(access_key, secret_key))
        client.request("PUT", None)  # CreateBucket, signed
        client.close()
        return S3Driver(config(bucket=bucket, access_key=access_key, secret_key=secret_key, region="us-east-1",
                               base_path="/b"))

    def test_download_url(self):
        # moto can't check query signatures; presigned URLs are compared with botocore below.
        self.skipTest("moto doesn't check presigned URLs")

    def test_wrong_secret(self):
        driver = S3Driver(dict(self.d.config, secret_key="wrong"))
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn("SignatureDoesNotMatch", str(caught.exception))
        self.assertNotIn("wrong", str(caught.exception))


class MinIOContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        return MinIODriver(config(path_style=None, region="us-east-1"))


class AliyunContract(ViaLocalDownloads, DriverContract, unittest.TestCase):
    def make_driver(self):
        cls = with_attrs(AliyunDriver, transport=moto.transport())
        return cls(config(endpoint=f"http://oss-cn-test.aliyuncs.test:{moto.port}"))


class TencentContract(ViaLocalDownloads, DriverContract, unittest.TestCase):
    def make_driver(self):
        cls = with_attrs(TencentDriver, transport=moto.transport())
        return cls(config(endpoint=f"http://cos.ap-test.myqcloud.test:{moto.port}",
                          bucket=moto.bucket(suffix="-1250000000")))


class HuaweiContract(ViaLocalDownloads, DriverContract, unittest.TestCase):
    def make_driver(self):
        cls = with_attrs(HuaweiDriver, transport=moto.transport())
        return cls(config(endpoint=f"http://obs.cn-test.myhuaweicloud.test:{moto.port}"))


# --- signing ------------------------------------------------------------------------------------

AWS_KEY = Credentials("AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
AWS_TIME = datetime.datetime(2013, 5, 24, tzinfo=datetime.timezone.utc)
AWS_HOST = "examplebucket.s3.amazonaws.com"
AWS_SCOPE = "20130524/us-east-1/s3/aws4_request"


class AwsExamplesTests(unittest.TestCase):
    """The examples of "Signature Calculations for the Authorization Header" and "Authenticating
    Requests: Using Query Parameters" in the Amazon S3 API reference."""

    def check(self, method, path, query, headers, payload, request_hash, signature):
        signed = {name.lower(): value for name, value in headers.items()}
        signed.update({"host": AWS_HOST, "x-amz-date": "20130524T000000Z", "x-amz-content-sha256": payload})
        from apps.storages.drivers.s3 import canonical_query

        request = canonical_request(method, path, canonical_query(query), signed, payload)
        # The last line of the string to sign is the hash of the canonical request.
        self.assertEqual(string_to_sign("20130524T000000Z", AWS_SCOPE, request).split("\n")[-1], request_hash)
        result = sign_headers(method, AWS_HOST, path, query, headers, payload, AWS_KEY, "us-east-1", AWS_TIME)
        self.assertTrue(result["authorization"].endswith(f"Signature={signature}"), result["authorization"])
        self.assertIn(f"Credential=AKIAIOSFODNN7EXAMPLE/{AWS_SCOPE}", result["authorization"])
        return request

    def test_get_object(self):
        request = self.check("GET", "/test.txt", [], {"Range": "bytes=0-9"}, EMPTY_SHA256,
                             "7344ae5b7ee6c3e7e6b0fe0640412a37625d1fbfff95c48bbb2dc43964946972",
                             "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41")
        self.assertEqual(request, "\n".join([
            "GET", "/test.txt", "", "host:examplebucket.s3.amazonaws.com", "range:bytes=0-9",
            "x-amz-content-sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "x-amz-date:20130524T000000Z", "", "host;range;x-amz-content-sha256;x-amz-date",
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"]))

    def test_put_object(self):
        payload = hashlib.sha256(b"Welcome to Amazon S3.").hexdigest()
        self.assertEqual(payload, "44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072")
        path = "/" + uri_encode("test$file.text", keep_slash=True)
        self.assertEqual(path, "/test%24file.text")
        self.check("PUT", path, [], {"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
                   payload, "9e0e90d9c76de8fa5b200d8c849cd5b8dc7a3be3951ddb7f6a76b4158342019d",
                   "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd")

    def test_get_bucket_lifecycle(self):
        self.check("GET", "/", [("lifecycle", "")], {}, EMPTY_SHA256,
                   "9766c798316ff2757b517bc739a67f6213b4ab36dd5da2f94eaebf79c77395ca",
                   "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543")

    def test_list_objects(self):
        self.check("GET", "/", [("max-keys", "2"), ("prefix", "J")], {}, EMPTY_SHA256,
                   "df57d21db20da04d7fa30298dd4488ba3a2b47ca3a489c74750e0f1e7df1b9b7",
                   "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7")

    def test_presigned_url(self):
        url = presign("GET", "https", AWS_HOST, "/test.txt", [], AWS_KEY, "us-east-1", AWS_TIME, 86400)
        self.assertEqual(url, "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
                              "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
                              "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
                              "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404")

    def test_uri_encode(self):
        self.assertEqual(uri_encode("a b+c/~d_e.f-g"), "a%20b%2Bc%2F~d_e.f-g")
        self.assertEqual(uri_encode("a b/中", keep_slash=True), "a%20b/%E4%B8%AD")
        self.assertEqual(uri_encode("100%"), "100%25")
        self.assertEqual(url_query([("uploads", ""), ("b", "x y"), ("a", "1/2")]), "a=1%2F2&b=x%20y&uploads")


class BotocoreOracleTests(unittest.TestCase):
    """Our signatures equal botocore's for the same requests (odd keys, queries, headers, tokens)."""

    when = datetime.datetime(2026, 3, 4, 5, 6, 7, tzinfo=datetime.timezone.utc)

    def boto(self):
        from botocore.auth import S3SigV4Auth, S3SigV4QueryAuth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials as BotoCredentials

        return S3SigV4Auth, S3SigV4QueryAuth, AWSRequest, BotoCredentials

    def patched_time(self):
        return mock.patch("botocore.auth.get_current_datetime", return_value=self.when.replace(tzinfo=None))

    cases = [
        ("GET", "bkt.s3.us-west-2.amazonaws.com", "/中文 名字.txt", [], {"range": "bytes=10-"}, None),
        ("PUT", "s3.example.com:9000", "/bkt/a+b=c&d.txt", [("partNumber", "3"), ("uploadId", "x/y+z=")],
         {"content-type": "text/plain; charset=utf-8"}, "unsigned"),
        ("POST", "bkt.s3.amazonaws.com", "/", [("delete", "")], {"content-md5": "abc==",
                                                                   "content-type": "application/xml"}, b"<Delete/>"),
        ("GET", "bkt.s3.amazonaws.com", "/", [("list-type", "2"), ("prefix", "a b/~c%d"), ("delimiter", "/"),
                                              ("encoding-type", "url"), ("continuation-token", "1==/+x")], {}, None),
        ("PUT", "bkt.s3.amazonaws.com", "/q%3Fx%3D1/%23hash", [], {"x-amz-copy-source": "/bkt/a%20b",
                                                                   "x-amz-meta-note": "  two   spaces "}, None),
        ("HEAD", "[::1]:9000", "/bkt/%F0%9F%98%80", [], {}, None),
    ]

    def test_header_signatures(self):
        S3SigV4Auth, _, AWSRequest, BotoCredentials = self.boto()
        for token in (None, "session/token+="):
            for method, host, raw_path, query, headers, body in self.cases:
                with self.subTest(method=method, path=raw_path, token=token):
                    path = "/" + "/".join(uri_encode(unquote(part)) for part in raw_path[1:].split("/")) \
                        if raw_path != "/" else "/"
                    data = body if isinstance(body, bytes) else b""
                    payload = "UNSIGNED-PAYLOAD" if body == "unsigned" else (
                        hashlib.sha256(data).hexdigest() if data else EMPTY_SHA256)
                    ours = sign_headers(method, host, path, query, headers, payload,
                                        Credentials("AKIDEXAMPLE", "secret/key+", token), "us-west-2", self.when)
                    request = AWSRequest(method=method, url=f"https://{host}{path}" + (
                        f"?{url_query(query)}" if query else ""), headers=dict(headers), data=data)
                    request.context["client_config"] = types.SimpleNamespace(
                        s3={"payload_signing_enabled": body != "unsigned"})
                    with self.patched_time():
                        S3SigV4Auth(BotoCredentials("AKIDEXAMPLE", "secret/key+", token), "s3", "us-west-2").add_auth(request)
                    self.assertEqual(ours["authorization"], request.headers["Authorization"])
                    self.assertEqual(ours["x-amz-content-sha256"], request.headers["X-Amz-Content-SHA256"])

    def test_presigned_urls(self):
        _, S3SigV4QueryAuth, AWSRequest, BotoCredentials = self.boto()
        disposition = content_disposition("文件 \"1\".bin")
        for token in (None, "session/token+="):
            for method, query, headers in [
                ("GET", [("response-content-disposition", disposition)], {}),
                ("PUT", [], {"content-type": "application/pdf", "content-length": "12345"}),
            ]:
                with self.subTest(method=method, token=token):
                    path = "/" + uri_encode("dir/中文 a+b.pdf", keep_slash=True)
                    ours = presign(method, "https", "bkt.s3.amazonaws.com", path, query,
                                   Credentials("AKIDEXAMPLE", "secret/key+", token), "us-east-1", self.when, 600, headers)
                    request = AWSRequest(method=method, url=f"https://bkt.s3.amazonaws.com{path}" + (
                        f"?{url_query(query)}" if query else ""), headers=dict(headers))
                    with self.patched_time():
                        S3SigV4QueryAuth(BotoCredentials("AKIDEXAMPLE", "secret/key+", token), "s3", "us-east-1",
                                         expires=600).add_auth(request)
                    self.assertEqual(signature_of(ours), signature_of(request.url))
                    self.assertEqual(parse_qs(urlsplit(ours).query), parse_qs(urlsplit(request.url).query))


def signature_of(url):
    return parse_qs(urlsplit(url).query)["X-Amz-Signature"][0]


def boto_signature(method, url, *, secret="secret-test-key", access="AKIDTEST", region="us-east-1", headers=None):
    """botocore's signature of a presigned URL as it arrives (host, path and query as they are),
    i.e. what a server holding the secret computes."""
    from botocore.auth import S3SigV4QueryAuth
    from botocore.awsrequest import AWSRequest
    from botocore.credentials import Credentials as BotoCredentials

    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    unsigned = "&".join(p for p in parts.query.split("&") if not p.startswith("X-Amz-Signature="))
    request = AWSRequest(method=method, url=f"{parts.scheme}://{parts.netloc}{parts.path}?{unsigned}",
                         headers=headers or {})
    request.context["timestamp"] = query["X-Amz-Date"][0]
    signer = S3SigV4QueryAuth(BotoCredentials(access, secret), "s3", region, expires=int(query["X-Amz-Expires"][0]))
    return signer.signature(signer.string_to_sign(request, signer.canonical_request(request)), request)


# --- addressing ---------------------------------------------------------------------------------

class AddressingTests(unittest.TestCase):
    def client(self, endpoint, bucket, path_style=False):
        client = S3Client(endpoint=endpoint, bucket=bucket, region="us-east-1", path_style=path_style,
                          credentials=lambda: Credentials("a", "b"))
        self.addCleanup(client.close)
        return client

    def test_virtual_and_path_style(self):
        self.assertEqual(self.client("https://s3.amazonaws.com", "bkt").target("a b/c"), ("bkt.s3.amazonaws.com", "/a%20b/c"))
        self.assertEqual(self.client("https://s3.amazonaws.com", "bkt", True).target("a b/c"),
                         ("s3.amazonaws.com", "/bkt/a%20b/c"))
        self.assertEqual(self.client("https://s3.amazonaws.com:443", "bkt").target(None), ("bkt.s3.amazonaws.com", "/"))
        self.assertEqual(self.client("http://minio:9000", "bkt").target("x"), ("bkt.minio:9000", "/x"))

    def test_falls_back_to_path_style(self):
        for endpoint, bucket in [("http://192.168.1.10:9000", "bkt"), ("http://[::1]:9000", "bkt"),
                                 ("http://localhost:9000", "bkt"), ("https://s3.amazonaws.com", "my.bucket"),
                                 ("https://s3.amazonaws.com", "Upper"), ("https://s3.amazonaws.com", "under_score")]:
            with self.subTest(endpoint=endpoint, bucket=bucket):
                self.assertTrue(self.client(endpoint, bucket).path_style)
        self.assertFalse(self.client("http://s3.example.com", "my.bucket").path_style)  # dots are fine over http

    def test_service_requests(self):
        self.assertEqual(self.client("https://s3.amazonaws.com", "bkt").target(None, service=True), ("s3.amazonaws.com", "/"))

    def test_region(self):
        cases = [("https://s3.us-west-2.amazonaws.com", "", "us-west-2"),
                 ("https://bkt.s3-eu-west-1.amazonaws.com", "", "eu-west-1"),
                 ("https://s3.cn-north-1.amazonaws.com.cn", "", "cn-north-1"),
                 ("https://s3.amazonaws.com", "", "us-east-1"),
                 ("https://abc.r2.cloudflarestorage.com", "", "auto"),
                 ("http://192.168.1.2:9000", "", "us-east-1"),
                 ("https://s3.us-west-2.amazonaws.com", "eu-central-1", "eu-central-1")]
        for endpoint, region, expected in cases:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(S3Driver({"endpoint": endpoint, "region": region, "bucket": "b"})._region(), expected)
        self.assertEqual(AliyunDriver({"endpoint": "https://oss-cn-hangzhou.aliyuncs.com"})._region(), "oss")
        self.assertEqual(TencentDriver({"endpoint": "https://cos.ap-guangzhou.myqcloud.com"})._region(), "cos")
        self.assertEqual(HuaweiDriver({"endpoint": "https://obs.cn-north-4.myhuaweicloud.com"})._region(), "obs")
        self.assertEqual(MinIODriver({"endpoint": "http://m:9000"})._region(), "us-east-1")
        self.assertEqual(MinIODriver({"endpoint": "http://m:9000", "region": "minio"})._region(), "minio")

    def test_addressing_of_types(self):
        self.assertTrue(MinIODriver({"endpoint": "http://minio.example.com:9000", "bucket": "bkt"})._s3().path_style)
        self.assertFalse(AliyunDriver({"endpoint": "https://oss-cn-hangzhou.aliyuncs.com", "bucket": "bkt",
                                       "path_style": "path"})._s3().path_style)
        self.assertTrue(S3Driver({"endpoint": "https://s3.amazonaws.com", "bucket": "bkt", "path_style": "path"})._s3().path_style)


# --- listings, multipart, copies ----------------------------------------------------------------

def put(driver, path, data):
    driver.write(path, io.BytesIO(data), len(data))


def read(driver, path, offset=0, length=None):
    with driver.open(path, offset, length) as stream:
        return b"".join(stream)


class PagingTests(unittest.TestCase):
    def setUp(self):
        self.d = with_attrs(S3Driver, page_size=3, workers=3)(config())
        self.addCleanup(self.d.close)

    def test_long_listing(self):
        for index in range(8):
            put(self.d, f"/p/f{index}.txt", b"%d" % index)
        for index in range(4):
            self.d.mkdir(f"/p/d{index}")
        names = sorted(entry.name for entry in self.d.list("/p"))
        self.assertEqual(names, [f"d{i}" for i in range(4)] + [f"f{i}.txt" for i in range(8)])

    def test_folder_operations_across_pages(self):
        for index in range(7):
            put(self.d, f"/src/s/f{index}", b"%d" % index)
        put(self.d, "/src/s/deep/x", b"x")
        self.d.copy("/src/s", "/")
        self.assertEqual(len(self.d.list("/s")), 8)
        self.d.rename("/s", "t")
        self.assertEqual(read(self.d, "/t/f6"), b"6")
        self.assertEqual(read(self.d, "/t/deep/x"), b"x")
        self.d.move("/t", "/src")
        self.assertEqual(sorted(e.name for e in self.d.list("/src")), ["s", "t"])
        self.d.delete("/src")
        self.assertEqual(self.d.list("/"), [])
        self.assertEqual(moto.keys(self.d.config["bucket"]), [])


class PagingV1Tests(PagingTests):
    """The same through ListObjects with markers (Tencent, DogeCloud), URL-encoded."""

    def setUp(self):
        self.d = with_attrs(S3Driver, page_size=3, workers=3, list_version=1)(config())
        self.addCleanup(self.d.close)


class PagingV1PlainTests(PagingTests):
    """ListObjects with markers and no encoding-type (Huawei)."""

    def setUp(self):
        self.d = with_attrs(S3Driver, page_size=3, workers=3, list_version=1, url_encoded_listing=False)(config())
        self.addCleanup(self.d.close)


class NonSeekable(io.RawIOBase):
    """A stream that can only be read, like a request body."""

    def __init__(self, data, fail_after=None):
        self._data = io.BytesIO(data)
        self._fail_after = fail_after

    def readable(self):
        return True

    def read(self, size=-1):
        if self._fail_after is not None and self._data.tell() >= self._fail_after:
            raise OSError("client went away")
        return self._data.read(size)


class MultipartTests(unittest.TestCase):
    def setUp(self):
        cls = with_attrs(S3Driver, single_put_limit=6 * 1024 * 1024, min_part_size=5 * 1024 * 1024,
                         copy_limit=6 * 1024 * 1024, min_copy_part_size=5 * 1024 * 1024)
        self.d = cls(config())
        self.addCleanup(self.d.close)
        self.data = os.urandom(11 * 1024 * 1024 + 3)

    def etag(self, path):
        return self.d._s3().head(self.d._key(path)).etag

    def test_multipart_upload(self):
        put(self.d, "/big.bin", self.data)
        self.assertEqual(read(self.d, "/big.bin"), self.data)
        self.assertTrue(self.etag("/big.bin").strip('"').endswith("-3"), self.etag("/big.bin"))

    def test_multipart_upload_from_a_stream(self):
        self.d.write("/stream.bin", NonSeekable(self.data), len(self.data))
        self.assertEqual(read(self.d, "/stream.bin", 6 * 1024 * 1024, 100), self.data[6 * 1024 * 1024:][:100])

    def test_failed_upload_is_aborted(self):
        with self.assertRaises(OSError):
            self.d.write("/fail.bin", NonSeekable(self.data, fail_after=7 * 1024 * 1024), len(self.data))
        with self.assertRaises(NotFound):
            self.d.stat("/fail.bin")
        self.assertEqual(moto.uploads(self.d.config["bucket"]), [])

    def test_short_data(self):
        with self.assertRaises(DriverError) as caught:
            self.d.write("/short.bin", NonSeekable(b"12345"), 10)
        self.assertIn("不完整", str(caught.exception))

    def test_large_copy_uses_part_copies(self):
        put(self.d, "/big.txt", self.data)
        self.d.mkdir("/dst")
        sent = []
        transport = moto.transport()
        handle = transport.handle_request

        def record(request):
            sent.append(request.headers)
            return handle(request)

        transport.handle_request = record
        recorded = with_attrs(type(self.d), transport=transport)(self.d.config)
        self.addCleanup(recorded.close)
        recorded.copy("/big.txt", "/dst")
        self.assertEqual(read(self.d, "/dst/big.txt"), self.data)
        self.assertTrue(self.etag("/dst/big.txt").strip('"').endswith("-3"))
        self.assertEqual(self.d._s3().head(self.d._key("/dst/big.txt")).content_type, "text/plain")
        copies = [h for h in sent if "x-amz-copy-source-range" in h]
        self.assertEqual(len(copies), 3)
        self.assertTrue(all(h["x-amz-copy-source-if-match"] == self.etag("/big.txt") for h in copies))
        put(self.d, "/folder/inner.bin", self.data)
        self.d.rename("/folder", "renamed")
        self.assertEqual(read(self.d, "/renamed/inner.bin"), self.data)

    def test_part_size(self):
        d = S3Driver({})
        self.assertEqual(d._part_size(100 * 1024 * 1024, d.min_part_size), 8 * 1024 * 1024)
        self.assertEqual(d._part_size(200 * 1024 ** 3, d.min_part_size), 21 * 1024 * 1024)  # 10,000 parts at most
        self.assertLessEqual(math_ceil(5 * 1024 ** 4 / d._part_size(5 * 1024 ** 4, d.min_part_size)), 10000)
        # Objects of up to 10,000 parts of 5 GiB (48.8 TiB, AWS's limit, OSS's and OBS's too); the cap was 5 TiB.
        largest = 10000 * 5 * 1024 ** 3
        for size in (5 * 1024 ** 4 + 1, 40 * 1024 ** 4 + 7, largest):
            with self.subTest(size=size):
                for minimum in (d.min_part_size, d.min_copy_part_size):
                    part = d._part_size(size, minimum)
                    self.assertLessEqual(part, 5 * 1024 ** 3)
                    self.assertLessEqual(math_ceil(size / part), 10000)
        with self.assertRaises(DriverError):
            d._part_size(largest + 1, d.min_part_size)


def math_ceil(value):
    import math

    return math.ceil(value)


# --- links --------------------------------------------------------------------------------------

class LinkTests(unittest.TestCase):
    def driver(self, cls=S3Driver, **changes):
        driver = cls(config(**changes))
        self.addCleanup(driver.close)
        return driver

    def test_private_link(self):
        d = self.driver()
        put(d, "/a dir/报告 1.pdf", b"%PDF-1.4 data")
        url = d.download_url("/a dir/报告 1.pdf", filename="报告 1.pdf", expires=600)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["X-Amz-Expires"], ["600"])
        self.assertEqual(query["response-content-disposition"], [content_disposition("报告 1.pdf")])
        self.assertTrue(url.startswith(f"{moto.endpoint}/{d.config['bucket']}/a%20dir/"))
        self.assertEqual(signature_of(url), boto_signature("GET", url))
        response = httpx.get(url)
        self.assertEqual(response.content, b"%PDF-1.4 data")
        self.assertEqual(response.headers["content-disposition"],
                         "attachment; filename=\"__ 1.pdf\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A%201.pdf")

    def test_expiry_is_limited_to_a_week(self):
        url = self.driver().download_url("/x", filename="x", expires=10 ** 9)
        self.assertEqual(parse_qs(urlsplit(url).query)["X-Amz-Expires"], ["604800"])

    def test_public_links(self):
        d = self.driver(is_private=False)
        self.assertEqual(d.download_url("/a b.txt", filename="a b.txt"), f"{moto.endpoint}/{d.config['bucket']}/a%20b.txt")
        d = self.driver(is_private=False, domain="https://cdn.example.com")
        self.assertEqual(d.download_url("/a b.txt", filename="a b.txt"), f"https://cdn.example.com/{d.config['bucket']}/a%20b.txt")
        d = self.driver(is_private=False, domain="https://cdn.example.com", endpoint="https://s3.example.com",
                        path_style="virtual", bucket="bkt")
        self.assertEqual(d.download_url("/a b.txt", filename="a b.txt"), "https://cdn.example.com/a%20b.txt")

    def test_private_link_through_a_cdn(self):
        # Virtual-hosted: the CDN pulls from the bucket with the bucket's Host, so the link is
        # signed for the bucket's host and only its scheme and host are the CDN's.
        d = self.driver(domain="https://cdn.example.com", endpoint="https://s3.example.com", path_style="virtual",
                        bucket="bkt")
        url = d.download_url("/x/y.txt", filename="y.txt")
        self.assertTrue(url.startswith("https://cdn.example.com/x/y.txt?"))
        origin_url = url.replace("https://cdn.example.com", "https://bkt.s3.example.com", 1)
        self.assertEqual(signature_of(url), boto_signature("GET", origin_url))

    def test_private_link_through_a_proxy(self):
        # Path style: the domain is the S3 server's public address, and the link is signed for it.
        d = self.driver(domain="https://minio.example.com", endpoint="http://10.0.0.5:9000", bucket="bkt")
        url = d.download_url("/x/y.txt", filename="y.txt")
        self.assertTrue(url.startswith("https://minio.example.com/bkt/x/y.txt?"))
        self.assertEqual(signature_of(url), boto_signature("GET", url))

    def test_upload_target(self):
        d = self.driver()
        data = os.urandom(3000)
        target = d.upload_target("/up/文件.txt", len(data))
        self.assertEqual((target.kind, target.method, dict(target.headers)),
                         ("single", "PUT", {"Content-Type": "text/plain"}))
        self.assertEqual(parse_qs(urlsplit(target.url).query)["X-Amz-SignedHeaders"], ["content-length;content-type;host"])
        self.assertEqual(signature_of(target.url), boto_signature(
            "PUT", target.url, headers={"content-type": "text/plain", "content-length": str(len(data))}))
        response = httpx.put(target.url, content=data, headers=dict(target.headers))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(read(d, "/up/文件.txt"), data)

    def test_upload_target_limits(self):
        d = self.driver()
        self.assertIsNone(d.upload_target("/huge.bin", 5 * 1024 ** 3 + 1))
        d.mkdir("/folder")
        with self.assertRaises(AlreadyExists):
            d.upload_target("/folder", 10)

    def test_upload_target_through_a_proxy(self):
        d = self.driver(domain="https://minio.example.com", endpoint="http://10.0.0.5:9000", bucket="bkt")
        with mock.patch.object(S3Client, "has_prefix", return_value=False):
            target = d.upload_target("/a.bin", 5)
        self.assertTrue(target.url.startswith("https://minio.example.com/bkt/a.bin?"))

    def test_aliyun_links(self):
        """V4 query signatures ("在URL中包含V4签名"): OSS stops taking V1 from accounts made since March 2025 and
        buckets made since September 2025 ("V1 signature is forbidden")."""
        d = self.driver(AliyunDriver, endpoint="https://oss-cn-hangzhou.aliyuncs.com", bucket="bkt",
                        access_key="LTAIKEY", secret_key="oss-secret")
        with mock.patch("apps.storages.drivers.s3.time.time", return_value=1700000000.0):
            url = d.download_url("/dir/中 a.txt", filename="中 a.txt", expires=900)
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         "https://bkt.oss-cn-hangzhou.aliyuncs.com/dir/%E4%B8%AD%20a.txt")
        query = parse_qs(parts.query)
        disposition = content_disposition("中 a.txt")
        self.assertEqual(query["response-content-disposition"], [disposition])
        self.assertEqual(query["x-oss-signature-version"], ["OSS4-HMAC-SHA256"])
        self.assertEqual(query["x-oss-credential"], ["LTAIKEY/20231114/cn-hangzhou/oss/aliyun_v4_request"])
        self.assertEqual(query["x-oss-date"], ["20231114T221320Z"])
        self.assertEqual(query["x-oss-expires"], ["900"])
        # The steps of OSS's documentation, written out: no headers are signed (a browser's GET has none of
        # content-type, content-md5 or x-oss-*), and the host isn't.
        canonical = ("GET\n/bkt/dir/%E4%B8%AD%20a.txt\n"
                     f"response-content-disposition={quote(disposition, safe='')}&x-oss-credential=LTAIKEY%2F20231114%2Fcn-hangzhou"
                     "%2Foss%2Faliyun_v4_request&x-oss-date=20231114T221320Z&x-oss-expires=900"
                     "&x-oss-signature-version=OSS4-HMAC-SHA256\n\n\nUNSIGNED-PAYLOAD")
        to_sign = ("OSS4-HMAC-SHA256\n20231114T221320Z\n20231114/cn-hangzhou/oss/aliyun_v4_request\n"
                   + hashlib.sha256(canonical.encode()).hexdigest())
        key = b"aliyun_v4oss-secret"
        for part in ("20231114", "cn-hangzhou", "oss", "aliyun_v4_request"):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        self.assertEqual(query["x-oss-signature"], [hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()])
        # A custom domain bound to the bucket: same signature, the domain's host.
        d = self.driver(AliyunDriver, endpoint="https://oss-cn-hangzhou.aliyuncs.com", bucket="bkt",
                        access_key="LTAIKEY", secret_key="oss-secret", domain="https://files.example.com")
        with mock.patch("apps.storages.drivers.s3.time.time", return_value=1700000000.0):
            self.assertEqual(urlsplit(d.download_url("/dir/中 a.txt", filename="中 a.txt", expires=900)).query, parts.query)
        d = self.driver(AliyunDriver, endpoint="https://oss-cn-hangzhou.aliyuncs.com", bucket="bkt",
                        domain="https://files.example.com", is_private=False)
        self.assertEqual(d.download_url("/a b", filename="a b"), "https://files.example.com/a%20b")
        # An endpoint that doesn't name the region: V1, as before.
        d = self.driver(AliyunDriver, endpoint="https://oss-accelerate.aliyuncs.com", bucket="bkt",
                        access_key="LTAIKEY", secret_key="oss-secret")
        with mock.patch("apps.storages.drivers.s3.time.time", return_value=1700000000.0):
            query = parse_qs(urlsplit(d.download_url("/dir/中 a.txt", filename="中 a.txt", expires=900)).query)
        self.assertEqual(query["OSSAccessKeyId"], ["LTAIKEY"])
        self.assertEqual(query["Expires"], ["1700000900"])
        message = f"GET\n\n\n1700000900\n/bkt/dir/中 a.txt?response-content-disposition={disposition}"
        expected = base64.b64encode(hmac.new(b"oss-secret", message.encode(), hashlib.sha1).digest()).decode()
        self.assertEqual(query["Signature"], [expected])
        self.assertEqual(oss_v1_signature("oss-secret", "GET", "bkt", "dir/中 a.txt", 1700000900,
                                          [("response-content-disposition", disposition)]), expected)

    def test_oss_v4_signature_vector(self):
        """The case of OSS's own Python SDK v2 (tests/unit/signer/test_v4.py, test_auth_query)."""
        query = oss_v4_query(
            "ak", "sk", "PUT", "bucket", "1234+-/123/1.txt",
            datetime.datetime.fromtimestamp(1702781677, datetime.timezone.utc), 599, "cn-hangzhou",
            [("param1", "value1"), ("+param1", "value3"), ("|param1", "value4"), ("+param2", ""), ("|param2", ""),
             ("param2", "")],
            {"x-oss-head1": "value", "content-type": "application/octet-stream"})
        values = dict(query)
        self.assertEqual(values["x-oss-credential"], "ak%2F20231217%2Fcn-hangzhou%2Foss%2Faliyun_v4_request")
        self.assertEqual(values["x-oss-expires"], "599")
        self.assertEqual(values["x-oss-signature"], "a39966c61718be0d5b14e668088b3fa07601033f6518ac7b523100014269c0fe")

    def test_oss_regions(self):
        for endpoint, region in [("https://oss-cn-hangzhou.aliyuncs.com", "cn-hangzhou"),
                                 ("https://oss-cn-hangzhou-internal.aliyuncs.com", "cn-hangzhou"),
                                 ("https://s3.oss-ap-southeast-1.aliyuncs.com", "ap-southeast-1"),
                                 ("https://OSS-us-west-1.aliyuncs.com/", "us-west-1"),
                                 ("https://oss-accelerate.aliyuncs.com", None),
                                 ("https://oss-accelerate-overseas.aliyuncs.com", None),
                                 ("https://minio.example.com", None), ("not a url", None)]:
            with self.subTest(endpoint=endpoint):
                self.assertEqual(oss_region(endpoint), region)

    def test_aliyun_internal_endpoint(self):
        """An internal endpoint (oss-<region>-internal) serves Alibaba Cloud's own network only: links and direct
        uploads pointed there, which browsers can't reach. They use the public endpoint, as OSS's docs say to ("将内网
        Endpoint 替换为公网 Endpoint"); the site's own requests stay internal."""
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(200, content=EMPTY_LIST)

        keys = {"access_key": "LTAIKEY", "secret_key": "oss-secret"}
        for internal, public in [("oss-cn-hangzhou-internal.aliyuncs.com", "oss-cn-hangzhou.aliyuncs.com"),
                                 ("s3.oss-cn-hangzhou-internal.aliyuncs.com", "s3.oss-cn-hangzhou.aliyuncs.com")]:
            with self.subTest(endpoint=internal):
                hosts.clear()
                d = mock_driver(self, handler, AliyunDriver, endpoint=f"https://{internal}", **keys)
                outside = mock_driver(self, handler, AliyunDriver, endpoint=f"https://{public}", **keys)
                with mock.patch("apps.storages.drivers.s3.time.time", return_value=1700000000.0):
                    url = d.download_url("/dir/a b.txt", filename="a b.txt", expires=900)
                    self.assertEqual(url, outside.download_url("/dir/a b.txt", filename="a b.txt", expires=900))
                self.assertTrue(url.startswith(f"https://bkt.{public}/dir/a%20b.txt?"), url)
                d = mock_driver(self, handler, AliyunDriver, endpoint=f"https://{internal}", is_private=False, **keys)
                self.assertEqual(d.download_url("/a b.txt", filename="a b.txt"), f"https://bkt.{public}/a%20b.txt")
                # A presigned PUT signs the host: signed for the public one.
                target = d.upload_target("/up/a.bin", 5)
                self.assertTrue(target.url.startswith(f"https://bkt.{public}/up/a.bin?"), target.url)
                self.assertEqual(signature_of(target.url), boto_signature(
                    "PUT", target.url, access="LTAIKEY", secret="oss-secret", region="oss",
                    headers={"content-type": target.headers["Content-Type"], "content-length": "5"}))
                self.assertEqual(set(hosts), {f"bkt.{internal}"})  # the upload's check that no folder is there
        # An http:// internal endpoint is the server's own choice: browsers get https:// addresses.
        d = mock_driver(self, handler, AliyunDriver, endpoint="http://oss-cn-hangzhou-internal.aliyuncs.com",
                        is_private=False, **keys)
        self.assertEqual(d.download_url("/a.txt", filename="a.txt"), "https://bkt.oss-cn-hangzhou.aliyuncs.com/a.txt")
        self.assertTrue(d.upload_target("/a.bin", 5).url.startswith("https://bkt.oss-cn-hangzhou.aliyuncs.com/a.bin?"))

    def test_content_disposition(self):
        self.assertEqual(content_disposition("a.txt"), "attachment; filename=\"a.txt\"; filename*=UTF-8''a.txt")
        self.assertEqual(content_disposition('x"y\\z%\n.txt'),
                         "attachment; filename=\"x_y_z__.txt\"; filename*=UTF-8''x%22y%5Cz%25%0A.txt")


# --- errors -------------------------------------------------------------------------------------

def mock_driver(test, handler, cls=S3Driver, **changes):
    values = {"endpoint": "https://s3.example.com", "access_key": "AKIDMOCK", "secret_key": "very-secret-key",
              "bucket": "bkt", "base_path": "/", "path_style": "virtual", "is_private": True}
    values.update(changes)
    driver = with_attrs(cls, transport=httpx.MockTransport(handler))(values)
    test.addCleanup(driver.close)
    return driver


def xml_error(status, code, message="", **extra):
    fields = "".join(f"<{k}>{v}</{k}>" for k, v in extra.items())
    return httpx.Response(status, content=f"<?xml version=\"1.0\"?><Error><Code>{code}</Code><Message>{message}"
                                          f"</Message>{fields}<RequestId>R1</RequestId></Error>".encode(),
                          headers={"content-type": "application/xml"})


EMPTY_LIST = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><IsTruncated>false</IsTruncated>'
              '<KeyCount>0</KeyCount></ListBucketResult>').encode()


@mock.patch.object(S3Client, "_pause", staticmethod(lambda attempt: None))
class ErrorTests(unittest.TestCase):
    def check_no_secret(self, error):
        self.assertNotIn("very-secret-key", str(error))
        self.assertNotIn("Signature=", str(error))

    def test_auth_errors(self):
        for code, words in [("SignatureDoesNotMatch", "SecretKey"), ("InvalidAccessKeyId", "AccessKey"),
                            ("AccessDenied", "权限"), ("ExpiredToken", "过期")]:
            with self.subTest(code=code):
                d = mock_driver(self, lambda request, code=code: xml_error(403, code, "The request signature..."))
                with self.assertRaises(AuthError) as caught:
                    d.list("/")
                self.assertIn(code, str(caught.exception))
                self.assertIn(words, str(caught.exception))
                self.check_no_secret(caught.exception)

    def test_logs_carry_no_secrets(self):
        d = mock_driver(self, lambda request: xml_error(403, "SignatureDoesNotMatch", "nope"))
        with self.assertLogs("hfile.storage.s3", "DEBUG") as logs, self.assertRaises(AuthError):
            d.list("/")
        text = "\n".join(logs.output)
        self.assertIn("SignatureDoesNotMatch", text)
        self.assertNotIn("very-secret-key", text)
        self.assertNotIn("Signature=", text)

    def test_head_forbidden_is_not_missing(self):
        d = mock_driver(self, lambda request: httpx.Response(403))
        with self.assertRaises(AuthError):
            d.stat("/a.txt")

    def test_wrong_bucket_and_region(self):
        d = mock_driver(self, lambda request: xml_error(404, "NoSuchBucket", "The specified bucket does not exist"))
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("bkt", str(caught.exception))
        d = mock_driver(self, lambda request: xml_error(301, "PermanentRedirect", "...", Endpoint="bkt.s3.eu-west-1.amazonaws.com"))
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        # AWS names the bucket's own host; the form takes the service's address, with the scheme ("Leave the bucket
        # name out of the Endpoint").
        self.assertIn("https://s3.eu-west-1.amazonaws.com", str(caught.exception))
        self.assertNotIn("bkt.s3", str(caught.exception))
        d = mock_driver(self, lambda request: xml_error(400, "AuthorizationHeaderMalformed",
                                                        "the region 'us-east-1' is wrong; expecting 'eu-west-1'"))
        with self.assertRaises(AuthError) as caught:
            d.list("/")
        self.assertIn("eu-west-1", str(caught.exception))

    def test_download_bans(self):
        """COS (buckets created since 2024, 403 DownloadForbidden) and OBS (since 2023-12-30, 400
        InsecureDownloadForbidden) don't serve .apk and .ipa files through the bucket's default domain. That isn't a
        problem of the keys, which is what COS's 403 said."""
        for status, code in [(403, "DownloadForbidden"), (400, "InsecureDownloadForbidden")]:
            with self.subTest(code=code):
                d = mock_driver(self, lambda request, status=status, code=code: xml_error(status, code, "not allowed"))
                with self.assertRaises(DriverError) as caught:
                    d.open("/app.apk")
                self.assertNotIsInstance(caught.exception, (AuthError, BackendError))
                self.assertIn(code, str(caught.exception))
                self.assertIn(".apk", str(caught.exception))

    def test_oss_continuation_tokens(self):
        """OSS URL-encodes NextContinuationToken when asked for encoding-type=url, and its SDKs decode it ("会对返回
        结果中的…NextContinuationToken…进行编码"): sent back as it came, it was encoded twice. Other services' tokens
        go back as they come: Ceph's is the next key itself, not encoded."""
        for cls, token, sent in [(AliyunDriver, "Cg%2Bi%2Fw--", "Cg+i/w--"), (S3Driver, "a%2Bb.txt", "a%2Bb.txt")]:
            with self.subTest(type=cls.type):
                queries = []

                def handler(request, token=token):
                    queries.append(parse_qs(urlsplit(str(request.url)).query))
                    more = len(queries) == 1
                    return httpx.Response(200, content=(
                        f"<ListBucketResult><IsTruncated>{str(more).lower()}</IsTruncated>"
                        "<EncodingType>url</EncodingType>"
                        + (f"<NextContinuationToken>{token}</NextContinuationToken>" if more else "")
                        + f"<Contents><Key>{len(queries)}.txt</Key><Size>1</Size></Contents>"
                        "</ListBucketResult>").encode())

                d = mock_driver(self, handler, cls)
                self.assertEqual(sorted(e.name for e in d.list("/")), ["1.txt", "2.txt"])
                self.assertEqual(queries[1]["continuation-token"], [sent])

    def test_accept_encoding_is_not_signed(self):
        """Google Cloud Storage's front end adds "gzip(gfe)" to Accept-Encoding: signed, it made every GET fail there
        (SignatureDoesNotMatch). It goes unsigned, like Content-Length."""
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(206, stream=httpx.ByteStream(b"bc"),
                                  headers={"content-length": "2", "content-range": "bytes 1-2/3"})

        with mock_driver(self, handler).open("/a.txt", 1) as stream:
            self.assertEqual(b"".join(stream), b"bc")
        request = seen[0]
        self.assertEqual(request.headers["accept-encoding"], "identity")
        signed = re.search(r"SignedHeaders=([^,]+)", request.headers["authorization"]).group(1).split(";")
        self.assertIn("range", signed)
        self.assertNotIn("accept-encoding", signed)

    def test_retries(self):
        answers = [xml_error(503, "SlowDown"), xml_error(500, "InternalError"), httpx.Response(200, content=EMPTY_LIST)]
        seen = []

        def handler(request):
            seen.append(request)
            return answers.pop(0)

        self.assertEqual(mock_driver(self, handler).list("/"), [])
        self.assertEqual(len(seen), 3)
        d = mock_driver(self, lambda request: httpx.Response(502))
        with self.assertRaises(BackendError):
            d.list("/")

    def test_connection_errors(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ConnectError("[Errno 111] Connection refused")

        with self.assertRaises(BackendError) as caught:
            mock_driver(self, handler).list("/")
        self.assertIn("连接存储失败", str(caught.exception))
        self.assertEqual(len(seen), 3)

    def test_clock_skew_is_corrected(self):
        server_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        dates = []

        def handler(request):
            dates.append(request.headers["x-amz-date"])
            if len(dates) == 1:
                response = xml_error(403, "RequestTimeTooSkewed", "The difference ... is too large.")
                response.headers["date"] = server_time.strftime("%a, %d %b %Y %H:%M:%S GMT")
                return response
            return httpx.Response(200, content=EMPTY_LIST)

        d = mock_driver(self, handler)
        self.assertEqual(d.list("/"), [])
        sent = datetime.datetime.strptime(dates[1], "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        self.assertLess(abs((sent - server_time).total_seconds()), 60)
        self.assertGreater(d._s3().clock_offset, 7000)

    def test_upload_retry_needs_a_seekable_body(self):
        bodies = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, content=EMPTY_LIST)
            bodies.append(request.read())
            if len(bodies) == 1:
                return xml_error(500, "InternalError")
            return httpx.Response(200, headers={"etag": '"%s"' % hashlib.md5(bodies[-1]).hexdigest()})

        d = mock_driver(self, handler)
        d.write("/a.bin", io.BytesIO(b"0123456789"), 10)
        self.assertEqual(bodies, [b"0123456789", b"0123456789"])
        bodies.clear()
        with self.assertRaises(BackendError):
            d.write("/b.bin", NonSeekable(b"0123456789"), 10)
        self.assertEqual(len(bodies), 1)

    def test_etag_mismatch(self):
        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, content=EMPTY_LIST)
            request.read()
            return httpx.Response(200, headers={"etag": '"00000000000000000000000000000000"'})

        with self.assertRaises(BackendError) as caught:
            mock_driver(self, handler).write("/a.bin", io.BytesIO(b"abc"), 3)
        self.assertIn("MD5", str(caught.exception))

        def kms(request):
            if request.method == "GET":
                return httpx.Response(200, content=EMPTY_LIST)
            request.read()
            return httpx.Response(200, headers={"etag": '"00000000000000000000000000000000"',
                                                "x-amz-server-side-encryption": "aws:kms"})

        mock_driver(self, kms).write("/a.bin", io.BytesIO(b"abc"), 3)  # KMS ETags aren't MD5s

        def dsse(request):
            response = kms(request)
            if request.method == "PUT":
                response.headers["x-amz-server-side-encryption"] = "aws:kms:dsse"
            return response

        mock_driver(self, dsse).write("/a.bin", io.BytesIO(b"abc"), 3)

    def test_copy_failing_with_200(self):
        def handler(request):
            if request.method == "HEAD":
                return httpx.Response(200 if request.url.path == "/a.txt" else 404, headers={"content-length": "3"})
            if request.method == "GET":
                return httpx.Response(200, content=EMPTY_LIST)
            return httpx.Response(200, content=b"<Error><Code>InternalError</Code><Message>oops</Message></Error>")

        with self.assertRaises(BackendError):
            mock_driver(self, handler).rename("/a.txt", "b.txt")

    def test_delete_objects_errors(self):
        def handler(request):
            if request.method == "HEAD":
                return httpx.Response(404)
            if request.method == "GET":
                return httpx.Response(200, content=b"<ListBucketResult><IsTruncated>false</IsTruncated><Contents>"
                                                   b"<Key>d/a</Key><Size>1</Size></Contents></ListBucketResult>")
            self.assertTrue(request.headers["content-md5"])
            return httpx.Response(200, content=b"<DeleteResult><Error><Key>d/a</Key><Code>AccessDenied</Code>"
                                               b"<Message>Access Denied</Message></Error></DeleteResult>")

        with self.assertRaises(AuthError) as caught:
            mock_driver(self, handler).delete("/d")
        self.assertIn("d/a", str(caught.exception))

    def test_url_encoded_listings(self):
        # AWS writes a space as "+" and "+" as "%2B"; without EncodingType the keys are literal.
        encoded = (b"<ListBucketResult><IsTruncated>false</IsTruncated><EncodingType>url</EncodingType>"
                   b"<Contents><Key>a+b%2Bc%25.txt</Key><Size>1</Size></Contents>"
                   b"<CommonPrefixes><Prefix>%E4%B8%AD+dir/</Prefix></CommonPrefixes></ListBucketResult>")
        d = mock_driver(self, lambda request: httpx.Response(200, content=encoded))
        self.assertEqual(sorted(e.name for e in d.list("/")), ["a b+c%.txt", "中 dir"])
        literal = (b"<ListBucketResult><IsTruncated>false</IsTruncated>"
                   b"<Contents><Key>a+b%2Bc.txt</Key><Size>1</Size></Contents></ListBucketResult>")
        d = mock_driver(self, lambda request: httpx.Response(200, content=literal))
        self.assertEqual([e.name for e in d.list("/")], ["a+b%2Bc.txt"])

    def test_bad_xml(self):
        utf16 = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE x [<!ENTITY a "b">]><ListBucketResult/>'
        for body in [b"<!DOCTYPE x [<!ENTITY a 'b'>]><ListBucketResult/>", utf16.encode("utf-16"),
                     b"<ListBucketResult><oops",
                     b"<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>a</Key><Size>x</Size>"
                     b"</Contents></ListBucketResult>",
                     b"<ListBucketResult><IsTruncated>true</IsTruncated></ListBucketResult>",
                     # STOR-04: an encoding Python doesn't know was a LookupError, a 500
                     b'<?xml version="1.0" encoding="x-no-such"?><ListBucketResult/>']:
            with self.subTest(body=body[:40]):
                d = mock_driver(self, lambda request, body=body: httpx.Response(200, content=body))
                with self.assertRaises(BackendError):
                    d.list("/")

    def test_answer_too_large(self):
        d = mock_driver(self, lambda request: httpx.Response(200, content=b"<a>" + b" " * (17 * 1024 * 1024)))
        with self.assertRaises(BackendError):
            d.list("/")

    def test_server_ignoring_range(self):
        data = bytes(range(256)) * 40
        # stream=: a body that arrives like one from the network (content= would be read already).
        d = mock_driver(self, lambda request: httpx.Response(200, stream=httpx.ByteStream(data),
                                                             headers={"content-length": str(len(data))}))
        with d.open("/a.bin", 100, 50) as stream:
            self.assertEqual(b"".join(stream), data[100:150])
            self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, len(data)))
        self.assertEqual(read(d, "/a.bin", 10000), data[10000:])

    def test_wrong_content_range(self):
        for range_ in ("bytes 0-49/1000", "bytes 100-120/1000"):
            d = mock_driver(self, lambda request, range_=range_: httpx.Response(206, content=b"x" * 50, headers={
                "content-range": range_, "content-length": "50"}))
            with self.assertRaises(BackendError):
                d.open("/a.bin", 100, 50)

    def test_v2_answered_the_v1_way(self):
        pages = [b"<ListBucketResult><IsTruncated>true</IsTruncated><NextMarker>b.txt</NextMarker>"
                 b"<Contents><Key>a.txt</Key><Size>1</Size></Contents>"
                 b"<Contents><Key>b.txt</Key><Size>1</Size></Contents></ListBucketResult>",
                 b"<ListBucketResult><IsTruncated>true</IsTruncated>"
                 b"<Contents><Key>c.txt</Key><Size>1</Size></Contents></ListBucketResult>",
                 b"<ListBucketResult><IsTruncated>false</IsTruncated>"
                 b"<Contents><Key>d.txt</Key><Size>1</Size></Contents></ListBucketResult>"]
        queries = []

        def handler(request):
            queries.append(parse_qs(urlsplit(str(request.url)).query, keep_blank_values=True))
            return httpx.Response(200, content=pages[len(queries) - 1])

        d = mock_driver(self, handler)
        self.assertEqual(sorted(e.name for e in d.list("/")), ["a.txt", "b.txt", "c.txt", "d.txt"])
        self.assertEqual(queries[0]["list-type"], ["2"])
        self.assertEqual((queries[1]["marker"], "list-type" in queries[1]), (["b.txt"], False))
        self.assertEqual(queries[2]["marker"], ["c.txt"])  # no NextMarker: the last key

    def test_broken_stream(self):
        class Broken(httpx.SyncByteStream):
            def __iter__(self):
                yield b"x" * 1000
                raise httpx.ReadError("connection reset")

        d = mock_driver(self, lambda request: httpx.Response(200, stream=Broken(), headers={"content-length": "5000"}))
        stream = d.open("/a.bin")
        with self.assertRaises(BackendError):
            b"".join(stream)
        stream.close()


# --- names and confinement ------------------------------------------------------------------------

class TimeTests(unittest.TestCase):
    def test_a_time_that_isnt_one_in_utc_is_none(self):
        """STOR-05: a Last-Modified whose offset puts it in the year 10000 in UTC (31 Dec 9999 23:59:59 -2359) was
        taken as it was, and the download couldn't write its own Last-Modified header: a 500. Such a time is no
        time, from S3 and from every driver (Entry and ReadStream drop it)."""
        far = "Fri, 31 Dec 9999 23:59:59 -2359"
        d = mock_driver(self, lambda request: httpx.Response(
            200, stream=httpx.ByteStream(b"abc"), headers={"last-modified": far, "content-length": "3", "etag": '"e"'}))
        self.assertIsNone(d.stat("/a.txt").modified)
        stream = d.open("/a.txt")
        self.assertIsNone(stream.modified)
        self.assertEqual(stream.read(), b"abc")
        moment = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone(-datetime.timedelta(hours=23, minutes=59)))
        self.assertIsNone(Entry("a", "/a", False, 1, moment).modified)
        self.assertIsNone(ReadStream([], size=0, total=0, modified=moment).modified)
        fine = datetime.datetime(9999, 12, 31, 23, 59, 59, tzinfo=datetime.timezone.utc)
        self.assertEqual(Entry("a", "/a", False, 1, fine).modified, fine)


class _Dripping(fake_http.Handler):
    """An answer of 1000 bytes, one every 50 ms: never a read that waits long, and 50 s in all."""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "1000")
        self.end_headers()
        for _ in range(1000):
            try:
                self.wfile.write(b" ")
                self.wfile.flush()
            except OSError:
                return
            time.sleep(0.05)


class CallTimeTests(unittest.TestCase):
    def test_a_call_has_its_time_in_all(self):
        """STOR-07: each read and write had 60 s, and a call nothing more: a server answering slowly but steadily
        held it (and the admin page waiting on 测试连接 or a helper button) as long as it liked. A call that reads
        its answer whole has CALL_SECONDS in all."""
        server = fake_http.Server(_Dripping, None)
        self.addCleanup(server.stop)
        d = S3Driver({"endpoint": server.url, "access_key": "AKIDTEST", "secret_key": "secret-test-key", "bucket": "bkt",
                      "base_path": "/", "path_style": "path", "is_private": True, "domain": "", "region": ""})
        self.addCleanup(d.close)
        with mock.patch.object(s3, "CALL_SECONDS", 0.5):
            started = time.monotonic()
            with self.assertRaises(BackendError) as caught:
                d.list("/")
            self.assertLess(time.monotonic() - started, 5)
        self.assertIn("秒内没有回答完", caught.exception.message)


class _Missing(fake_http.Handler):
    def do_HEAD(self):
        self.answer(404)


class ConnectionTests(unittest.TestCase):
    def test_a_missing_object_keeps_the_connection(self):
        """A HEAD's 404, which has no body, was closed unread: its connection went with it. stat() of a folder asks so
        for a file of its name first."""
        server = fake_http.Server(_Missing, None)
        self.addCleanup(server.stop)
        d = S3Driver({"endpoint": server.url, "access_key": "AKIDTEST", "secret_key": "secret-test-key", "bucket": "bkt",
                      "base_path": "/", "path_style": "path", "is_private": True, "domain": "", "region": ""})
        self.addCleanup(d.close)
        for _ in range(3):
            self.assertIsNone(d._s3().head("missing.txt"))
        self.assertEqual(server.connections, 1)


class NameTests(unittest.TestCase):
    def setUp(self):
        self.d = S3Driver(config(base_path="/base"))
        self.addCleanup(self.d.close)
        self.bucket = self.d.config["bucket"]

    def test_keys_that_are_no_paths_are_left_out(self):
        for key in ["base/x/./y", "base/x//z", "base/x/../w", "base/x/ok.txt", "base/x/sub/"]:
            moto.put_raw(self.bucket, key)
        self.assertEqual(sorted(e.name for e in self.d.list("/x")), ["ok.txt", "sub"])
        self.assertEqual([e.name for e in self.d.list("/")], ["x"])

    def test_confinement(self):
        for key in ["base2/secret", "basefile", "other/x", "base/inside"]:
            moto.put_raw(self.bucket, key, b"data")
        self.assertEqual([e.name for e in self.d.list("/")], ["inside"])
        for call in (lambda: self.d.stat("/../base2/secret"), lambda: self.d.open("/.."),
                     lambda: self.d.list("/x/../.."), lambda: self.d.delete("/"),
                     lambda: self.d.rename("/inside", "../x"), lambda: self.d.rename("/inside", ".."),
                     lambda: self.d.stat("//inside"), lambda: self.d.stat("/" + "a" * 1100)):
            with self.assertRaises(InvalidPath):
                call()
        self.d.delete("/inside")
        self.assertEqual(moto.keys(self.bucket), ["base2/secret", "basefile", "other/x"])

    def test_keys_with_dot_segments(self):
        for key in ["base/x/a.txt", "base/x/./dot", "base/x/../../escaped", "base/x/s/../up", "base/h/./only",
                    "victim\x01", "base/d/../../victim\x01"]:
            moto.put_raw(self.bucket, key)
        access_key, secret_key = self.enterContext(moto.checking_signatures())
        d = S3Driver(dict(self.d.config, access_key=access_key, secret_key=secret_key, region="us-east-1"))
        self.addCleanup(d.close)
        self.assertEqual(d.list("/h"), [])  # only hidden keys: an empty folder, not a missing one
        self.assertTrue(d.stat("/h").is_dir)
        d.rename("/x", "z")  # signed requests only for keys a URL can carry
        self.assertEqual([e.name for e in d.list("/z")], ["a.txt"])
        keys = moto.keys(self.bucket)
        self.assertNotIn("escaped", keys)
        self.assertTrue({"base/x/./dot", "base/x/../../escaped", "base/x/s/../up"} <= set(keys))
        d.delete("/x")  # DeleteObjects carries them in XML
        self.assertEqual(moto.keys(self.bucket, "base/x/"), [])
        with self.assertRaises(DriverError):
            d.delete("/d")  # a URL would reach "victim\x01", XML can't carry it
        self.assertIn("victim\x01", moto.keys(self.bucket))

    def test_names_of_dots_and_spaces_only(self):
        # Keys written by another program: such segments are left out (D46); the folder still lists.
        for key in ["base/n/ok.txt", "base/n/.../x", "base/n/.. ", "base/n/ . /y"]:
            moto.put_raw(self.bucket, key)
        self.assertEqual([e.name for e in self.d.list("/n")], ["ok.txt"])

    def test_odd_paths_and_base_paths(self):
        with self.assertRaises(InvalidPath):
            self.d.stat("/a\ud800b")  # a lone surrogate
        with self.assertRaises(NotFound):
            self.d.open("/")
        broken = S3Driver(config(base_path="/a/../b"))
        self.addCleanup(broken.close)
        for call in (lambda: broken.list("/"), lambda: broken.stat("/x"), lambda: put(broken, "/x", b"x")):
            with self.assertRaises(InvalidPath):
                call()

    def test_deleting_keys_xml_cannot_carry(self):
        for key in ["base/d/cr\rkey", "base/d/ctl\x01key", "base/d/plain"]:
            moto.put_raw(self.bucket, key)
        self.assertEqual(sorted(e.name for e in self.d.list("/d")), ["cr\rkey", "ctl\x01key", "plain"])
        self.d.delete("/d")
        self.assertEqual(moto.keys(self.bucket), [])

    def test_folders_without_markers(self):
        put(self.d, "/m/f", b"1")
        self.assertTrue(self.d.stat("/m").is_dir)
        self.d.delete("/m/f")
        with self.assertRaises(NotFound):
            self.d.stat("/m")
        self.d.mkdir("/kept")
        put(self.d, "/kept/f", b"1")
        self.d.delete("/kept/f")
        self.assertTrue(self.d.stat("/kept").is_dir)
        self.assertEqual(moto.keys(self.bucket), ["base/kept/"])

    def test_write_at_an_offset_of_the_stream(self):
        data = io.BytesIO(b"headerPAYLOAD")
        data.seek(6)
        self.d.write("/p.txt", data, 7)
        self.assertEqual(read(self.d, "/p.txt"), b"PAYLOAD")

    def test_move_into_a_file(self):
        put(self.d, "/a", b"a")
        put(self.d, "/f", b"f")
        with self.assertRaises(DriverError):
            self.d.move("/a", "/f")
        self.d.move("/a", "/")  # into its own folder: nothing to do
        self.assertEqual(read(self.d, "/a"), b"a")


class ConcurrencyTests(unittest.TestCase):
    def test_one_driver_many_threads(self):
        d = S3Driver(config())
        self.addCleanup(d.close)
        errors = []

        def work(index):
            try:
                for step in range(5):
                    path = f"/t{index}/f{step}.bin"
                    data = os.urandom(1000 + step)
                    put(d, path, data)
                    if read(d, path) != data:
                        errors.append(path)
                    d.list(f"/t{index}")
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(d.list("/")), 8)


# --- params and helpers -------------------------------------------------------------------------

class ParamsTests(unittest.TestCase):
    good = {"endpoint": "https://s3.us-east-1.amazonaws.com", "access_key": "a", "secret_key": "b",
            "bucket": "my-bucket", "base_path": "/", "path_style": "virtual", "is_private": True, "domain": ""}

    def errors(self, cls=S3Driver, **changes):
        return cls.validate_config(dict(self.good, **changes))

    def test_good(self):
        self.assertEqual(self.errors(), {})
        self.assertEqual(self.errors(endpoint="http://192.168.1.2:9000/", domain="https://cdn.example.com/"), {})
        self.assertEqual(self.errors(endpoint="http://[::1]:9000", region="us-west-2"), {})
        self.assertEqual(self.errors(endpoint="http://my_minio:9000", path_style="path"), {})

    def test_endpoint(self):
        for endpoint in ["s3.amazonaws.com", "ftp://s3.amazonaws.com", "https://s3.amazonaws.com/path",
                         "https://s3.amazonaws.com?x=1", "https://user:pw@s3.amazonaws.com", "https://s3.amazonaws.com:99999",
                         "https://-bad.com", "https://bad..com", "https://bad host.com", "", None, "https://"]:
            with self.subTest(endpoint=endpoint):
                self.assertIn("endpoint", self.errors(endpoint=endpoint))

    def test_other_fields(self):
        self.assertIn("bucket", self.errors(bucket="a b"))
        self.assertIn("bucket", self.errors(bucket="a/b"))
        self.assertIn("bucket", self.errors(bucket=""))
        self.assertIn("base_path", self.errors(base_path="/a/../b"))
        self.assertIn("domain", self.errors(domain="cdn.example.com"))
        self.assertIn("domain", self.errors(domain="https://cdn.example.com/files"))
        self.assertIn("region", self.errors(region="us east"))
        self.assertIn("access_key", self.errors(access_key="中文"))
        self.assertIn("access_key", self.errors(access_key="a b"))
        self.assertIn("secret_key", self.errors(secret_key=""))
        self.assertIn("endpoint", self.errors(endpoint="https://my-bucket.s3.amazonaws.com"))
        self.assertEqual(self.errors(endpoint="https://my-bucket.s3.amazonaws.com", path_style="path"), {})

    def test_tencent_bucket(self):
        good = dict(self.good, endpoint="https://cos.ap-guangzhou.myqcloud.com")
        self.assertIn("bucket", TencentDriver.validate_config(dict(good, bucket="examplebucket")))
        self.assertEqual(TencentDriver.validate_config(dict(good, bucket="examplebucket-1250000000")), {})

    def test_params_of_every_type(self):
        for type_ in ["s3", "minio", "aliyun", "tencent", "huawei"]:
            cls = driver_class(type_)
            with self.subTest(type=type_):
                keys = [p.key for p in cls.params]
                self.assertEqual(len(keys), len(set(keys)))
                self.assertTrue({"endpoint", "access_key", "secret_key", "bucket", "base_path"} <= set(keys))
                self.assertEqual([p.key for p in cls.params if p.secret], ["secret_key"])
                self.assertEqual(cls.defaults()["base_path"], "/")
                self.assertTrue(all(p.label for p in cls.params))
                self.assertTrue(cls.can_direct_download and cls.can_direct_upload and cls.has_folders)
        self.assertNotIn("path_style", [p.key for p in AliyunDriver.params])
        self.assertEqual([p.label for p in TencentDriver.params][1], "SecretId")

    def test_download_bans_in_the_help(self):
        """COS (buckets created since 2024) and OBS (since 2023-12-30) don't serve .apk and .ipa files through the
        bucket's default domain: the download domain's help says so."""
        for cls in (TencentDriver, HuaweiDriver):
            with self.subTest(type=cls.type):
                self.assertIn(".apk", str(next(p.help for p in cls.params if p.key == "domain")))


class HelperTests(unittest.TestCase):
    def test_buckets(self):
        bucket = moto.bucket("helper")
        answer = S3Driver.helper("buckets", config(bucket=""), {})
        self.assertEqual(answer["param"], "bucket")
        self.assertIn(bucket, [choice["value"] for choice in answer["choices"]])

    def test_cors(self):
        values = config()
        self.assertIn("还没有", S3Driver.helper("cors", values, {})["message"])
        self.assertIn("已经允许", S3Driver.helper("set_cors", values, {"origin": "https://files.example.com"})["message"])
        self.assertIn("https://files.example.com", S3Driver.helper("cors", values, {})["message"])
        self.assertIn("已经允许 https://files.example.com 跨域上传了",
                      S3Driver.helper("set_cors", values, {"origin": "https://files.example.com/"})["message"])
        with self.assertRaises(DriverError):
            S3Driver.helper("set_cors", values, {})

    def test_minio_has_no_bucket_cors(self):
        """MinIO's bucket CORS calls are stubs (GetBucketCors always NoSuchCORSConfiguration, PutBucketCors 501
        NotImplemented): the helpers said browsers couldn't upload and to try again later. MinIO sets CORS for the
        whole server (MINIO_API_CORS_ALLOW_ORIGIN, every origin by default)."""
        self.assertEqual(list(MinIODriver.helpers), ["buckets"])
        with self.assertRaises(Unsupported):
            MinIODriver.helper("set_cors", config(path_style=None, region="us-east-1"),
                               {"origin": "https://files.example.com"})
        self.assertIn("MINIO_API_CORS_ALLOW_ORIGIN", str(next(p.help for p in MinIODriver.params if p.key == "bucket")))
        # What isn't implemented won't be by trying again.
        self.assertIsInstance(s3.s3_error(501, "NotImplemented", "A header you provided implies functionality that is "
                                          "not implemented"), Unsupported)

    def test_set_cors_keeps_response_vary(self):
        """PutBucketCors replaces the whole configuration (OSS: "覆盖语义"): COS's and OSS's ResponseVary (answers
        with Vary: Origin, for CDNs) was left out, i.e. turned off, when a rule was added."""
        bodies = []

        def handler(request):
            if request.method == "GET":
                return httpx.Response(200, content=b"<CORSConfiguration><CORSRule><AllowedOrigin>https://a.example.com"
                                                   b"</AllowedOrigin><AllowedMethod>GET</AllowedMethod></CORSRule>"
                                                   b"<ResponseVary>true</ResponseVary></CORSConfiguration>")
            bodies.append(request.read())
            return httpx.Response(200)

        values = {"endpoint": "https://cos.ap-guangzhou.myqcloud.com", "access_key": "AKIDMOCK",
                  "secret_key": "very-secret-key", "bucket": "bkt-1250000000", "base_path": "/", "is_private": True}
        cls = with_attrs(TencentDriver, transport=httpx.MockTransport(handler))
        cls.helper("set_cors", values, {"origin": "https://files.example.com"})
        self.assertIn(b"<AllowedOrigin>https://a.example.com</AllowedOrigin>", bodies[0])
        self.assertIn(b"<AllowedOrigin>https://files.example.com</AllowedOrigin>", bodies[0])
        self.assertTrue(bodies[0].endswith(b"</CORSRule><ResponseVary>true</ResponseVary></CORSConfiguration>"),
                        bodies[0])

    def test_bad_input(self):
        with self.assertRaises(DriverError) as caught:
            S3Driver.helper("buckets", {"endpoint": moto.endpoint}, {})
        self.assertIn("AccessKey", str(caught.exception))
        with self.assertRaises(DriverError):
            S3Driver.helper("buckets", config(endpoint="not a url"), {})
        with self.assertRaises(Unsupported):
            S3Driver.helper("nope", config(), {})


class RegistryTests(unittest.TestCase):
    def test_types(self):
        for type_, cls in [("s3", S3Driver), ("minio", MinIODriver), ("aliyun", AliyunDriver),
                           ("tencent", TencentDriver), ("huawei", HuaweiDriver)]:
            self.assertIs(driver_class(type_), cls)

    def test_repr_hides_the_secret(self):
        self.assertNotIn("sk-123", repr(Credentials("ak", "sk-123", "token")))
        self.assertIsNone(re.search("sk-123", repr(Credentials("ak", "sk-123"))))


if __name__ == "__main__":
    unittest.main()
