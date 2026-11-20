"""Qiniu Kodo: the contract on moto (as Kodo's S3 API), download tokens (Qiniu's documented
example) and links through the bound domain."""

from __future__ import annotations

import io
import logging
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import httpx

from apps.storages.drivers import driver_class
from apps.storages.drivers.qiniu import QiniuDriver, private_url
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.moto_s3 import MotoS3, ToLocal

moto: MotoS3


def setUpModule():
    global moto
    moto = MotoS3()
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def tearDownModule():
    moto.stop()


def config(**changes):
    values = {"endpoint": moto.endpoint, "access_key": "QINIU_AK", "secret_key": "QINIU_SK", "base_path": "/",
              "is_private": True, "domain": ""}
    values["bucket"] = changes.pop("bucket") if "bucket" in changes else moto.bucket()
    values.update(changes)
    return values


class Recording(ToLocal):
    def __init__(self, port):
        super().__init__(port)
        self.urls = []

    def handle_request(self, request):
        self.urls.append(str(request.url))
        return super().handle_request(request)


class QiniuContract(DriverContract, unittest.TestCase):
    def make_driver(self):
        return QiniuDriver(config())


class QiniuTests(unittest.TestCase):
    def driver(self, **changes):
        driver = QiniuDriver(config(**changes))
        self.addCleanup(driver.close)
        return driver

    def test_documented_download_token(self):
        # developer.qiniu.com/kodo/1202/download-token
        url = private_url("http://78re52.com1.z0.glb.clouddn.com/resource/flower.jpg", 1451491200,
                          "MY_ACCESS_KEY", "MY_SECRET_KEY")
        self.assertEqual(url, "http://78re52.com1.z0.glb.clouddn.com/resource/flower.jpg?e=1451491200"
                              "&token=MY_ACCESS_KEY:438dd8pXocjYuF-6dTcKMtETB2g=")

    def test_private_link_through_the_domain(self):
        d = self.driver(bucket="bkt", base_path="/base", domain="https://files.example.com")
        with mock.patch("apps.storages.drivers.qiniu.time.time", return_value=1700000000.0):
            url = d.download_url("/a b/报告.pdf", filename="报告.pdf", expires=600)
        base = "https://files.example.com/base/a%20b/%E6%8A%A5%E5%91%8A.pdf?attname=%E6%8A%A5%E5%91%8A.pdf&e=1700000600"
        self.assertEqual(url, private_url(base.rsplit("&e=", 1)[0], 1700000600, "QINIU_AK", "QINIU_SK"))
        self.assertTrue(url.startswith(base + "&token=QINIU_AK:"))  # the token comes last

    def test_public_link_through_the_domain(self):
        d = self.driver(bucket="bkt", is_private=False, domain="https://files.example.com")
        self.assertEqual(d.download_url("/x.txt", filename="x.txt"), "https://files.example.com/x.txt?attname=x.txt")

    def test_link_without_a_domain_is_presigned(self):
        for private in (True, False):  # Kodo's S3 endpoints take no anonymous requests
            d = self.driver(is_private=private)
            d.write("/a.txt", io.BytesIO(b"hello"), 5)
            url = d.download_url("/a.txt", filename="a.txt", expires=60)
            self.assertIn("X-Amz-Signature", parse_qs(urlsplit(url).query))
            self.assertEqual(httpx.get(url).content, b"hello")

    def test_listing_without_encoding_type(self):
        transport = Recording(moto.port)
        cls = type("Recorded", (QiniuDriver,), {"transport": transport})
        d = cls(config())
        self.addCleanup(d.close)
        d.write("/a b+c.txt", io.BytesIO(b"1"), 1)
        self.assertEqual([e.name for e in d.list("/")], ["a b+c.txt"])
        listings = [url for url in transport.urls if "list-type=2" in url]
        self.assertTrue(listings)
        self.assertFalse([url for url in listings if "encoding-type" in url])

    def test_region_from_the_endpoint(self):
        for endpoint, region in [("https://s3.cn-east-1.qiniucs.com", "cn-east-1"),
                                 ("https://s3.ap-southeast-1.qiniucs.com", "ap-southeast-1"),
                                 ("https://s3-cn-north-1.qiniucs.com", "cn-north-1"),
                                 ("https://kodo.example.com", "kodo")]:
            self.assertEqual(QiniuDriver({"endpoint": endpoint})._region(), region)

    def test_params(self):
        cls = driver_class("qiniu")
        self.assertIs(cls, QiniuDriver)
        self.assertEqual([p.key for p in cls.params if p.secret], ["secret_key"])
        good = {"endpoint": "https://s3.cn-east-1.qiniucs.com", "access_key": "a", "secret_key": "b", "bucket": "kodo-bkt",
                "base_path": "/", "domain": "https://files.example.com"}
        self.assertEqual(cls.validate_config(good), {})
        self.assertIn("domain", cls.validate_config(dict(good, domain="files.example.com")))
        self.assertIn("endpoint", cls.validate_config(dict(good, endpoint="s3.cn-east-1.qiniucs.com")))

    def test_download_bans_in_the_help(self):
        """Buckets created since 2026-04-08 don't serve .apk, .ipa, .exe and similar files through Kodo's S3 domains
        (developer.qiniu.com/kodo/13421): the download domain's help says so."""
        help_text = str(next(p.help for p in QiniuDriver.params if p.key == "domain"))
        self.assertIn(".apk", help_text)
        self.assertIn(".exe", help_text)


if __name__ == "__main__":
    unittest.main()
