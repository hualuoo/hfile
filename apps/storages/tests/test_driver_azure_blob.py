"""Tests of the Azure Blob Storage driver (apps.storages.drivers.azure_blob) against the fake Blob service
(tests/fake_azure_blob.py), which checks Shared Key and SAS signatures on its own, and against mock answers
for errors.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_azure_blob
"""

from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import hmac
import io
import logging
import os
import threading
import time
import unittest
from unittest import mock
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from apps.storages import config as storage_config
from apps.storages.drivers.azure_blob import (
    API_VERSION,
    AzureBlobDriver,
    BlobClient,
    BlobProps,
    azure_error,
    sas_string_to_sign,
    signature,
    string_to_sign,
)
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.drivers.s3 import content_disposition
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_azure_blob import FakeAzureBlob

MiB = 1024 * 1024


def setUpModule():
    # Retries and errors are logged as warnings; keep them out of the test output.
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def with_attrs(cls, **attrs):
    """A subclass of the driver with class attributes changed (sizes, waits, transport)."""
    return type(cls.__name__, (cls,), attrs)


# Quick polls, so that pending copies don't slow the tests down.
Quick = with_attrs(AzureBlobDriver, copy_poll=0.01)
# Blocks above 1 MiB, pages of two names.
Small = with_attrs(AzureBlobDriver, copy_poll=0.01, single_put_limit=MiB, min_block_size=MiB, page_size=2, workers=3)


class FakeMixin:
    hns = False

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeAzureBlob(hns=cls.hns).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def setUp(self):
        self.fake.reset()
        self.fake.add_container("files")
        super().setUp()

    def settings(self, **changes):
        values = {"endpoint": self.fake.url, "account_name": self.fake.account, "account_key": self.fake.key,
                  "container": "files", "base_path": "/", "is_private": True, "domain": ""}
        values.update(changes)
        return values

    def new_driver(self, cls=Quick, **changes):
        driver = cls(self.settings(**changes))
        self.addCleanup(driver.close)
        return driver


def put(driver, path, data):
    driver.write(path, io.BytesIO(data), len(data))


def read(driver, path, offset=0, length=None):
    with driver.open(path, offset, length) as stream:
        return b"".join(stream)


def names(driver, path):
    return sorted(entry.name for entry in driver.list(path))


class NonSeekable(io.RawIOBase):
    """A stream that can only be read, like a request body; it breaks after `fail_after` bytes."""

    def __init__(self, data, fail_after=None):
        self._data = io.BytesIO(data)
        self._fail_after = fail_after

    def readable(self):
        return True

    def read(self, size=-1):
        if self._fail_after is not None and self._data.tell() >= self._fail_after:
            raise OSError("client went away")
        return self._data.read(size)


# --- the contract -----------------------------------------------------------------------------------

class AzureContract(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        return self.new_driver()


class AzureBlocksPagesAndPendingCopiesContract(FakeMixin, DriverContract, unittest.TestCase):
    """The 3 MiB file in blocks, listings in pages of two with an empty page before each, every copy pending."""

    def make_driver(self):
        self.fake.copy_mode, self.fake.copy_polls, self.fake.empty_pages = "pending", 2, True
        return self.new_driver(Small)


class AzureBasePathContract(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.put_raw("files", "base dir/other.txt", b"outside")
        self.fake.put_raw("files", "base dir/子 文件夹x/y", b"a sibling of the base")
        return self.new_driver(base_path="/base dir/子 文件夹")


class AzureHNSContract(FakeMixin, DriverContract, unittest.TestCase):
    """An account with a hierarchical namespace (Data Lake Storage): real directories through the Blob API."""

    hns = True

    def make_driver(self):
        self.fake.copy_mode = "pending"
        return self.new_driver(Small)


class AzureHNSBasePathContract(FakeMixin, DriverContract, unittest.TestCase):
    hns = True

    def make_driver(self):
        self.fake.put_raw("files", "base", kind="directory", metadata={"hdi_isfolder": "true"})  # empty
        self.fake.put_raw("files", "other/file.txt", b"outside")
        return self.new_driver(base_path="/base")


# --- signatures -------------------------------------------------------------------------------------

class SignatureTests(FakeMixin, unittest.TestCase):
    def test_the_docs_examples(self):
        """The strings to sign of "Authorize with Shared Key": Get Container Metadata, Create Container with
        Content-Length 0 (an empty line since version 2015-02-21), and the query of List Blobs."""
        date = {"x-ms-date": "Fri, 26 Jun 2015 23:39:12 GMT", "x-ms-version": "2015-02-21"}
        self.assertEqual(string_to_sign("GET", "myaccount", "/mycontainer",
                                        [("restype", "container"), ("comp", "metadata"), ("timeout", "20")], date),
                         "GET\n\n\n\n\n\n\n\n\n\n\n\nx-ms-date:Fri, 26 Jun 2015 23:39:12 GMT\nx-ms-version:2015-02-21\n"
                         "/myaccount/mycontainer\ncomp:metadata\nrestype:container\ntimeout:20")
        self.assertEqual(string_to_sign("PUT", "myaccount", "/mycontainer", [("restype", "container"), ("timeout", "30")],
                                        {**date, "content-length": "0"}),
                         "PUT\n\n\n\n\n\n\n\n\n\n\n\nx-ms-date:Fri, 26 Jun 2015 23:39:12 GMT\nx-ms-version:2015-02-21\n"
                         "/myaccount/mycontainer\nrestype:container\ntimeout:30")
        listing = string_to_sign("GET", "myaccount", "/mycontainer",
                                 [("restype", "container"), ("comp", "list"), ("include", "snapshots"),
                                  ("include", "metadata"), ("include", "uncommittedblobs")], date)
        self.assertTrue(listing.endswith("/myaccount/mycontainer\ncomp:list\ninclude:metadata,snapshots,"
                                         "uncommittedblobs\nrestype:container"), listing)

    def test_headers_in_their_places(self):
        text = string_to_sign("PUT", "acct", "/files/a%20b.txt", [("comp", "block"), ("blockid", "QUJD+/==")],
                              {"content-length": "5", "content-type": "text/plain", "range": "bytes=0-4",
                               "x-ms-version": API_VERSION, "x-ms-date": "D", "x-ms-meta-note": "  two   spaces "})
        lines = text.split("\n")
        self.assertEqual(lines[:13], ["PUT", "", "", "5", "", "text/plain", "", "", "", "", "", "bytes=0-4",
                                      "x-ms-date:D"])
        self.assertEqual(lines[13:], ["x-ms-meta-note:two spaces", f"x-ms-version:{API_VERSION}", "/acct/files/a%20b.txt",
                                      "blockid:QUJD+/==", "comp:block"])

    def test_signature(self):
        key = base64.b64decode(self.fake.key)
        self.assertEqual(signature(key, "text"),
                         base64.b64encode(hmac.new(key, b"text", hashlib.sha256).digest()).decode())

    def test_a_wrong_key_or_account(self):
        other_key = base64.b64encode(b"\x01" * 64).decode()
        for changes in ({"account_key": other_key}, {"account_name": "someoneelse"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(AuthError) as caught:
                    self.new_driver(**changes).list("/")
                message = str(caught.exception)
                self.assertIn("AuthenticationFailed", message)
                self.assertNotIn(other_key, message)
                self.assertNotIn("MAC", message)  # Azure's detail repeats the signature
                self.assertNotIn("string to sign", message)

    def test_a_key_that_is_not_base64(self):
        with self.assertRaises(AuthError):
            self.new_driver(account_key="not base64!").list("/")

    def test_a_clock_off_by_an_hour_is_corrected(self):
        """Azure refuses an x-ms-date more than 15 minutes from its clock (403); its Date sets ours right."""
        d = self.new_driver()
        d._blob().clock_offset = -3600.0
        put(d, "/a.txt", b"a")
        self.assertEqual(names(d, "/"), ["a.txt"])
        self.assertLess(abs(d._blob().clock_offset), 60)
        self.assertEqual([r.method for r in self.fake.requests][:2], ["HEAD", "HEAD"])  # refused, then again

    def test_sas_string_to_sign(self):
        """The 16 fields of a service SAS (version 2020-12-06 and later), written out."""
        self.assertEqual(sas_string_to_sign(permissions="r", expiry="2026-11-18T10:00:00Z",
                                            resource="/blob/acct/files/a b/文件.txt", disposition="attachment"),
                         "r\n\n2026-11-18T10:00:00Z\n/blob/acct/files/a b/文件.txt\n\n\n\n"
                         f"{API_VERSION}\nb\n\n\n\nattachment\n\n\n")


# --- download links ---------------------------------------------------------------------------------

class LinkTests(FakeMixin, unittest.TestCase):
    def test_private_link(self):
        d = self.new_driver()
        data = os.urandom(3000)
        put(d, "/a dir/报告 1.pdf", data)
        before = len(self.fake.requests)
        url = d.download_url("/a dir/报告 1.pdf", filename="报告 1.pdf", expires=600)
        self.assertEqual(len(self.fake.requests), before)  # signed here, without asking Azure
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         f"{self.fake.url}/files/a%20dir/%E6%8A%A5%E5%91%8A%201.pdf")
        query = parse_qs(parts.query)
        self.assertEqual((query["sv"], query["sr"], query["sp"]), ([API_VERSION], ["b"], ["r"]))
        self.assertNotIn("st", query)
        expiry = datetime.datetime.strptime(query["se"][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
        self.assertLess(abs(expiry.timestamp() - time.time() - 600), 5)
        self.assertEqual(query["rscd"], [content_disposition("报告 1.pdf")])
        # The signature, from the fields of the link and the docs' string to sign.
        text = "\n".join(["r", "", query["se"][0], f"/blob/{self.fake.account}/files/a dir/报告 1.pdf", "", "", "",
                          API_VERSION, "b", "", "", "", query["rscd"][0], "", "", ""])
        self.assertEqual(query["sig"], [base64.b64encode(hmac.new(base64.b64decode(self.fake.key), text.encode(),
                                                                  hashlib.sha256).digest()).decode()])
        response = httpx.get(url)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.content, data)
        self.assertEqual(response.headers["content-disposition"], content_disposition("报告 1.pdf"))

    def test_links_that_are_refused(self):
        d = self.new_driver()
        put(d, "/a.txt", b"a")
        put(d, "/b.txt", b"b")
        url = d.download_url("/a.txt", filename="a.txt", expires=600)
        self.assertEqual(httpx.get(url).status_code, 200)
        signature_value = parse_qs(urlsplit(url).query)["sig"][0]
        other = "B" if signature_value.startswith("A") else "A"  # a signature that starts with "A" stayed the same
        tampered = url.replace(quote(signature_value, safe=""), quote(other + signature_value[1:], safe=""))
        self.assertEqual(httpx.get(tampered).status_code, 403)
        self.assertEqual(httpx.get(url.replace("/a.txt?", "/b.txt?")).status_code, 403)  # another blob
        self.assertEqual(httpx.get(url.split("?")[0]).status_code, 404)  # a private container, no SAS
        self.fake.clock_skew = 700  # past its expiry
        self.assertEqual(httpx.get(url).status_code, 403)

    def test_public_container(self):
        self.fake.add_container("public", public=True)
        d = self.new_driver(container="public", is_private=False)
        put(d, "/a b.txt", b"public")
        url = d.download_url("/a b.txt", filename="a b.txt")
        self.assertEqual(url, f"{self.fake.url}/public/a%20b.txt")
        self.assertEqual(httpx.get(url).content, b"public")

    def test_download_domain(self):
        """A custom domain or CDN of the Blob endpoint: its scheme and host; a SAS isn't tied to the host."""
        d = self.new_driver(domain="https://cdn.example.com")
        put(d, "/x/y z.txt", b"via cdn")
        url = d.download_url("/x/y z.txt", filename="y z.txt", expires=600)
        self.assertTrue(url.startswith("https://cdn.example.com/files/x/y%20z.txt?sv="), url)
        self.assertEqual(httpx.get(url.replace("https://cdn.example.com", self.fake.url)).content, b"via cdn")
        d = self.new_driver(domain="https://cdn.example.com/", is_private=False)
        self.assertEqual(d.download_url("/x/y z.txt", filename="y z.txt"), "https://cdn.example.com/files/x/y%20z.txt")


# --- uploads ----------------------------------------------------------------------------------------

class UploadTests(FakeMixin, unittest.TestCase):
    def blocks(self):
        return [r for r in self.fake.requests if r.method == "PUT" and r.query.get("comp") == ["block"]]

    def test_small_files_in_one_put_blob(self):
        d = self.new_driver(Small)
        put(d, "/s.txt", b"x" * MiB)
        self.assertEqual(self.blocks(), [])
        put_blob = [r for r in self.fake.requests if r.method == "PUT"][-1]
        self.assertEqual((put_blob.headers["x-ms-blob-type"], put_blob.headers["content-type"]), ("BlockBlob", "text/plain"))

    def test_large_files_in_blocks(self):
        d = self.new_driver(Small)
        data = os.urandom(2 * MiB + 513)
        put(d, "/big/文件.txt", data)
        self.assertEqual(read(d, "/big/文件.txt"), data)
        blocks = self.blocks()
        self.assertEqual(len(blocks), 3)
        ids = [r.query["blockid"][0] for r in blocks]
        self.assertEqual(len({len(block_id) for block_id in ids}), 1)  # one length for the blob
        self.assertTrue(all(len(base64.b64decode(block_id)) <= 64 for block_id in ids))
        listing = [r for r in self.fake.requests if r.query.get("comp") == ["blocklist"]]
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0].headers["x-ms-blob-content-type"], "text/plain")
        self.assertEqual(self.fake.blob("files", "big/文件.txt").content_type, "text/plain")
        self.assertEqual(d.stat("/big/文件.txt").size, len(data))
        # Another upload of the same path has blocks of its own: two at once don't mix.
        put(d, "/big/文件.txt", data[::-1])
        self.assertTrue(set(ids).isdisjoint(r.query["blockid"][0] for r in self.blocks()[3:]))
        self.assertEqual(read(d, "/big/文件.txt"), data[::-1])

    def test_blocks_from_a_stream(self):
        d = self.new_driver(Small)
        data = os.urandom(3 * MiB)
        d.write("/stream.bin", NonSeekable(data), len(data))
        self.assertEqual(read(d, "/stream.bin", 2 * MiB, 100), data[2 * MiB:][:100])

    def test_a_failed_upload_keeps_the_old_file(self):
        """Until Put Block List the old blob stays; the uncommitted blocks are Azure's to discard."""
        d = self.new_driver(Small)
        put(d, "/f.bin", b"old")
        data = os.urandom(3 * MiB)
        with self.assertRaises(OSError):
            d.write("/f.bin", NonSeekable(data, fail_after=2 * MiB), len(data))
        self.assertEqual(read(d, "/f.bin"), b"old")
        with self.assertRaises(OSError):
            d.write("/new.bin", NonSeekable(data, fail_after=2 * MiB), len(data))
        with self.assertRaises(NotFound):
            d.stat("/new.bin")
        self.assertEqual(names(d, "/"), ["f.bin"])

    def test_short_data(self):
        for size in (10, 3 * MiB):
            with self.subTest(size=size):
                with self.assertRaises(DriverError):
                    self.new_driver(Small).write("/short.bin", NonSeekable(b"12345"), size)

    def test_md5_check(self):
        self.fake.corrupt_md5 = True
        with self.assertRaises(BackendError) as caught:
            put(self.new_driver(), "/a.bin", b"abc")
        self.assertIn("MD5", str(caught.exception))

    def test_block_size(self):
        d = AzureBlobDriver({})
        self.assertEqual(d._block_size(100 * MiB), 8 * MiB)
        for size in (400 * 1024 * MiB, 50 * 1024 ** 4 + 7):
            with self.subTest(size=size):
                block = d._block_size(size)
                self.assertEqual(block % MiB, 0)
                self.assertLessEqual(-(-size // block), 50000)
                self.assertLessEqual(block, 4000 * MiB)
        self.assertEqual(d._block_size(50000 * 4000 * MiB), 4000 * MiB)  # 190.7 TiB
        with self.assertRaises(DriverError):
            d._block_size(50000 * 4000 * MiB + 1)


class DataLakeUploadTests(FakeMixin, unittest.TestCase):
    hns = True

    def test_a_file_made_through_the_data_lake_api(self):
        """Put Block can't replace it (BlobOperationNotSupported, a known issue); Put Blob can."""
        self.fake.put_raw("files", "lake.bin", b"from dfs", dfs=True)
        d = self.new_driver(Small)
        data = os.urandom(2 * MiB)
        with self.assertRaises(DriverError) as caught:
            put(d, "/lake.bin", data)
        self.assertIn("BlobOperationNotSupported", str(caught.exception))
        self.assertEqual(read(d, "/lake.bin"), b"from dfs")
        put(d, "/lake.bin", b"small")
        self.assertEqual(read(d, "/lake.bin"), b"small")


# --- copies -----------------------------------------------------------------------------------------

class CopyTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.d = self.new_driver()
        put(self.d, "/a.txt", b"source")
        self.d.mkdir("/dst")

    def copy_requests(self):
        return [r for r in self.fake.requests if r.method == "PUT" and "x-ms-copy-source" in r.headers]

    def test_a_pending_copy_is_waited_for(self):
        self.fake.copy_mode, self.fake.copy_polls = "pending", 3
        self.d.move("/a.txt", "/dst")
        self.assertEqual(read(self.d, "/dst/a.txt"), b"source")
        with self.assertRaises(NotFound):
            self.d.stat("/a.txt")
        polls = [r for r in self.fake.requests if r.method == "HEAD" and r.name == "dst/a.txt"]
        self.assertGreaterEqual(len(polls), 3)
        source = self.copy_requests()[0].headers["x-ms-copy-source"]
        self.assertEqual(source, f"{self.fake.url}/files/a.txt")

    def test_a_failed_copy(self):
        self.fake.copy_mode = "fail"
        with self.assertRaises(BackendError) as caught:
            self.d.move("/a.txt", "/dst")
        self.assertIn("a.txt", str(caught.exception))
        self.assertEqual(read(self.d, "/a.txt"), b"source")  # a move keeps its source
        self.assertNotIn("dst/a.txt", self.fake.names("files"))  # nor the empty blob a failed copy leaves

    def test_a_copy_that_does_not_finish_is_cancelled(self):
        self.fake.copy_mode = "never"
        d = self.new_driver(with_attrs(AzureBlobDriver, copy_seconds=0.3, copy_poll=0.05))
        started = time.monotonic()
        with self.assertRaises(BackendError):
            d.move("/a.txt", "/dst")
        self.assertLess(time.monotonic() - started, 5)
        aborts = [r for r in self.fake.requests if r.query.get("comp") == ["copy"]]
        self.assertEqual(len(aborts), 1)
        self.assertEqual(aborts[0].headers["x-ms-copy-action"], "abort")
        self.assertTrue(aborts[0].query["copyid"][0])
        self.assertNotIn("dst/a.txt", self.fake.names("files"))
        self.assertEqual(read(d, "/a.txt"), b"source")

    def test_a_copy_overtaken_by_something_else(self):
        """The target's x-ms-copy-id isn't ours any more (written or copied over meanwhile): stop, and leave it."""
        self.fake.copy_mode = "never"
        real_head = BlobClient.head

        def head(client, name):
            props = real_head(client, name)
            if name == "dst/a.txt" and props is not None:
                return BlobProps(props.size, props.modified, props.etag, props.content_type, False, "someone-else", "pending")
            return props

        with mock.patch.object(BlobClient, "head", head), self.assertRaises(BackendError):
            self.d.copy("/a.txt", "/dst")
        self.assertIn("dst/a.txt", self.fake.names("files"))

    def test_a_copy_source_longer_than_2_kib(self):
        folder = "/" + "长" * 80
        path = f"{folder}{folder}{folder}/file.txt"
        put(self.d, path, b"deep")
        before = len(self.copy_requests())
        with self.assertRaises(DriverError):
            self.d.copy(path, "/dst")
        self.assertEqual(len(self.copy_requests()), before)


# --- listings ---------------------------------------------------------------------------------------

class PagingTests(FakeMixin, unittest.TestCase):
    def test_pages_follow_next_marker(self):
        d = self.new_driver(with_attrs(Quick, page_size=3))
        for index in range(8):
            put(d, f"/p/f{index}.txt", b"%d" % index)
        for index in range(4):
            d.mkdir(f"/p/d{index}")
        self.fake.requests.clear()
        self.assertEqual(names(d, "/p"), [f"d{i}" for i in range(4)] + [f"f{i}.txt" for i in range(8)])
        lists = [r.query for r in self.fake.requests if r.query.get("comp") == ["list"]]
        self.assertEqual(len(lists), 4)
        self.assertNotIn("marker", lists[0])
        self.assertTrue(all("marker" in query and query["maxresults"] == ["3"] for query in lists[1:]))

    def test_empty_pages_with_a_next_marker(self):
        """Azure may answer fewer names than maxresults, none even, and still have a NextMarker."""
        self.fake.empty_pages = True
        d = self.new_driver(with_attrs(Quick, page_size=2))
        for index in range(5):
            put(d, f"/src/s/f{index}", b"%d" % index)
        self.assertEqual(len(d.list("/src/s")), 5)
        d.copy("/src/s", "/")
        d.rename("/s", "t")
        self.assertEqual(read(d, "/t/f4"), b"4")
        d.delete("/src")
        d.delete("/t")
        self.assertEqual(self.fake.names("files"), [])

    def test_a_next_marker_that_does_not_move(self):
        page = (b'<?xml version="1.0" encoding="utf-8"?><EnumerationResults><Blobs><Blob><Name>a.txt</Name>'
                b"<Properties><Content-Length>1</Content-Length></Properties></Blob></Blobs>"
                b"<NextMarker>same</NextMarker></EnumerationResults>")
        d = mock_driver(self, lambda request: httpx.Response(200, content=page))
        with self.assertRaises(BackendError):
            d.list("/")


# --- errors (mock answers) --------------------------------------------------------------------------

MOCK_KEY = base64.b64encode(b"\x07" * 64).decode()
EMPTY_LIST = b'<?xml version="1.0" encoding="utf-8"?><EnumerationResults><Blobs /><NextMarker /></EnumerationResults>'


def mock_driver(test, handler, **changes):
    values = {"endpoint": "https://acct.blob.core.windows.net", "account_name": "acct", "account_key": MOCK_KEY,
              "container": "files", "base_path": "/", "is_private": True}
    values.update(changes)
    driver = with_attrs(AzureBlobDriver, transport=httpx.MockTransport(handler))(values)
    test.addCleanup(driver.close)
    return driver


def xml_error(status, code, message="", **headers):
    body = (f'<?xml version="1.0" encoding="utf-8"?><Error><Code>{code}</Code><Message>{message}\nRequestId:r1\n'
            f"Time:2026-11-18T00:00:00.0000000Z</Message></Error>")
    return httpx.Response(status, content=body.encode(), headers={"x-ms-error-code": code, **headers})


@mock.patch.object(BlobClient, "_pause", staticmethod(lambda attempt: None))
class ErrorTests(unittest.TestCase):
    def test_a_missing_container(self):
        d = mock_driver(self, lambda request: xml_error(404, "ContainerNotFound", "The specified container does not exist."))
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("files", str(caught.exception))
        self.assertIn("ContainerNotFound", str(caught.exception))

    def test_refused_credentials(self):
        for status, code in [(403, "AuthenticationFailed"), (403, "AuthorizationFailure"), (403, "AccountIsDisabled"),
                             (403, "KeyBasedAuthenticationNotPermitted"), (401, "InvalidAuthenticationInfo")]:
            with self.subTest(code=code):
                d = mock_driver(self, lambda request, status=status, code=code: xml_error(
                    status, code, "The MAC signature found in the HTTP request 'c2lnbmF0dXJl' is not the same"))
                with self.assertRaises(AuthError) as caught:
                    d.list("/")
                self.assertIn(code, str(caught.exception))
                self.assertNotIn("c2lnbmF0dXJl", str(caught.exception))
                self.assertNotIn(MOCK_KEY, str(caught.exception))

    def test_head_errors_have_no_body(self):
        """Get Blob Properties answers errors without a body: the code comes from x-ms-error-code."""
        d = mock_driver(self, lambda request: httpx.Response(403, headers={"x-ms-error-code": "AuthenticationFailed"}))
        with self.assertRaises(AuthError) as caught:
            d.stat("/a.txt")
        self.assertIn("AuthenticationFailed", str(caught.exception))
        d = mock_driver(self, lambda request: httpx.Response(404, headers={"x-ms-error-code": "BlobNotFound"})
                        if request.method == "HEAD" else httpx.Response(200, content=EMPTY_LIST))
        with self.assertRaises(NotFound):
            d.stat("/a.txt")

    def test_other_errors(self):
        cases = [(409, "LeaseIdMissing", BackendError), (400, "InvalidResourceName", InvalidPath),
                 (409, "BlobAlreadyExists", AlreadyExists), (501, "NotImplemented", DriverError)]
        for status, code, kind in cases:
            with self.subTest(code=code):
                error = azure_error(status, code, "There is a lease.\nRequestId:r1\nTime:t")
                self.assertIsInstance(error, kind)
                self.assertEqual((error.status, error.code), (status, code))
                self.assertNotIn("RequestId", str(error))
        self.assertIn("There is a lease.", str(azure_error(409, "LeaseIdMissing", "There is a lease.\nRequestId:r1")))

    def test_retries(self):
        answers = [xml_error(503, "ServerBusy", "Ingress is over the account limit."),
                   xml_error(500, "InternalError"), httpx.Response(200, content=EMPTY_LIST)]
        seen = []

        def handler(request):
            seen.append(request)
            return answers.pop(0)

        self.assertEqual(mock_driver(self, handler).list("/"), [])
        self.assertEqual(len(seen), 3)
        d = mock_driver(self, lambda request: xml_error(503, "ServerBusy"))
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("ServerBusy", str(caught.exception))

    def test_connection_errors(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ConnectError("[Errno 111] Connection refused")

        with self.assertRaises(BackendError):
            mock_driver(self, handler).list("/")
        self.assertEqual(len(seen), 3)

    def test_clock_skew(self):
        server_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        dates = []

        def handler(request):
            dates.append(request.headers["x-ms-date"])
            if len(dates) == 1:
                return xml_error(403, "AuthenticationFailed", date=email.utils.format_datetime(server_time, usegmt=True))
            return httpx.Response(200, content=EMPTY_LIST)

        d = mock_driver(self, handler)
        self.assertEqual(d.list("/"), [])
        sent = email.utils.parsedate_to_datetime(dates[1])
        self.assertLess(abs((sent - server_time).total_seconds()), 60)

    def test_upload_retry_needs_a_seekable_body(self):
        bodies = []

        def handler(request):
            if request.method in ("GET", "HEAD"):
                return httpx.Response(404) if request.method == "HEAD" else httpx.Response(200, content=EMPTY_LIST)
            bodies.append(request.read())
            if len(bodies) == 1:
                return xml_error(500, "InternalError")
            return httpx.Response(201, headers={"content-md5": base64.b64encode(hashlib.md5(bodies[-1]).digest()).decode()})

        d = mock_driver(self, handler)
        d.write("/a.bin", io.BytesIO(b"0123456789"), 10)
        self.assertEqual(bodies, [b"0123456789", b"0123456789"])
        bodies.clear()
        with self.assertRaises(BackendError):
            d.write("/b.bin", NonSeekable(b"0123456789"), 10)
        self.assertEqual(len(bodies), 1)

    def test_bad_xml(self):
        for body in [b"<EnumerationResults><oops", b"<!DOCTYPE x [<!ENTITY a 'b'>]><EnumerationResults/>",
                     b"<EnumerationResults><Blobs><Blob><Name>a</Name><Properties><Content-Length>x</Content-Length>"
                     b"</Properties></Blob></Blobs></EnumerationResults>"]:
            with self.subTest(body=body[:30]):
                d = mock_driver(self, lambda request, body=body: httpx.Response(200, content=body))
                with self.assertRaises(BackendError):
                    d.list("/")

    def test_server_ignoring_range(self):
        data = bytes(range(256)) * 40
        d = mock_driver(self, lambda request: httpx.Response(200, stream=httpx.ByteStream(data),
                                                             headers={"content-length": str(len(data))}))
        with d.open("/a.bin", 100, 50) as stream:
            self.assertEqual(b"".join(stream), data[100:150])
            self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, len(data)))

    def test_wrong_content_range(self):
        d = mock_driver(self, lambda request: httpx.Response(206, content=b"x" * 50, headers={
            "content-range": "bytes 0-49/1000", "content-length": "50"}))
        with self.assertRaises(BackendError):
            d.open("/a.bin", 100, 50)

    def test_logs_carry_no_secrets(self):
        d = mock_driver(self, lambda request: xml_error(403, "AuthenticationFailed", "nope"))
        with self.assertLogs("hfile.storage.azure-blob", "DEBUG") as logs, self.assertRaises(AuthError):
            d.list("/")
        text = "\n".join(logs.output)
        self.assertIn("AuthenticationFailed", text)
        self.assertNotIn(MOCK_KEY, text)
        self.assertNotIn("SharedKey", text)


class SnapshotTests(FakeMixin, unittest.TestCase):
    def test_a_blob_with_snapshots_is_deleted_with_them(self):
        self.fake.put_raw("files", "snap.txt", b"s", snapshots=2)
        d = self.new_driver()
        d.delete("/snap.txt")
        self.assertEqual(self.fake.names("files"), [])
        deletes = [r for r in self.fake.requests if r.method == "DELETE"]
        self.assertEqual([r.headers.get("x-ms-delete-snapshots") for r in deletes], [None, "include"])


class LoseFirstDeleteAnswer(httpx.BaseTransport):
    """The first DELETE reaches the server and is done there; its answer is lost (the connection drops)."""

    def __init__(self):
        self.inner = httpx.HTTPTransport()
        self.lost = False

    def handle_request(self, request):
        response = self.inner.handle_request(request)
        if request.method == "DELETE" and not self.lost:
            self.lost = True
            response.read()
            response.close()
            raise httpx.ReadError("connection reset by peer")
        return response

    def close(self):
        self.inner.close()


class LostAnswerTests(FakeMixin, unittest.TestCase):
    def test_a_delete_whose_answer_was_lost(self):
        self.fake.put_raw("files", "report.pdf", b"x")
        self.fake.put_raw("files", "a.txt", b"a")
        for call, left in ((lambda d: d.delete("/report.pdf"), ["a.txt"]),
                           (lambda d: d.rename("/a.txt", "b.txt"), ["b.txt"])):  # a copy, then a delete
            transport = LoseFirstDeleteAnswer()
            self.addCleanup(transport.close)
            d = self.new_driver(cls=with_attrs(Quick, transport=transport))
            d._blob()._pause = lambda attempt: None
            call(d)  # the DELETE is sent again, and finds nothing: that is done
            self.assertEqual(self.fake.names("files"), left)


# --- names and folders ------------------------------------------------------------------------------

class NameTests(FakeMixin, unittest.TestCase):
    def test_names_ending_with_a_dot(self):
        """Azure trims the dot at the end of a name's part ("a." is "a"): refused before a request is sent."""
        d = self.new_driver()
        put(d, "/a", b"a")
        before = len(self.fake.requests)
        for call in (lambda: put(d, "/a.", b"x"), lambda: d.mkdir("/x."), lambda: d.rename("/a", "b."),
                     lambda: put(d, "/d./x", b"x"), lambda: d.stat("/a.")):
            with self.assertRaises(InvalidPath):
                call()
        self.assertEqual(len(self.fake.requests), before)
        self.fake.put_raw("files", "odd.", b"x")  # no storage path can reach these
        self.fake.put_raw("files", "dir./inner", b"x")
        self.assertEqual(names(d, "/"), ["a"])

    def test_names_azure_sends_encoded(self):
        """U+FFFF can't be in XML: List Blobs sends such names percent-encoded, with Encoded="true"."""
        self.fake.put_raw("files", "odd\uffffname.txt", b"odd")
        d = self.new_driver()
        self.assertEqual(names(d, "/"), ["odd\uffffname.txt"])
        self.assertEqual(read(d, "/odd\uffffname.txt"), b"odd")

    def test_folders_made_by_other_programs(self):
        """Directory stubs (a blob named as the folder, hdi_isfolder=true: AzCopy, Hadoop) and markers."""
        self.fake.put_raw("files", "stub", metadata={"hdi_isfolder": "true"})
        self.fake.put_raw("files", "stub/inner.txt", b"in")
        self.fake.put_raw("files", "lonely", metadata={"HDI_ISFOLDER": "TRUE"})  # metadata names ignore case
        self.fake.put_raw("files", "marked/", metadata={"hdi_isfolder": "true"})  # rclone's and OpenList's
        d = self.new_driver()
        entries = {entry.name: entry for entry in d.list("/")}
        self.assertEqual(sorted(entries), ["lonely", "marked", "stub"])
        self.assertTrue(all(entry.is_dir for entry in entries.values()))
        self.assertEqual(d.list("/lonely"), [])
        self.assertTrue(d.stat("/lonely").is_dir)
        with self.assertRaises(NotFound):
            read(d, "/lonely")
        with self.assertRaises(AlreadyExists):
            put(d, "/lonely", b"x")
        d.rename("/lonely", "moved")
        self.assertEqual(self.fake.names("files"), ["marked/", "moved/", "stub", "stub/inner.txt"])
        d.copy("/stub", "/marked")
        self.assertEqual(read(d, "/marked/stub/inner.txt"), b"in")
        d.delete("/stub")
        d.delete("/marked")
        self.assertEqual(self.fake.names("files"), ["moved/"])

    def test_confinement(self):
        for name in ["base2/secret", "basefile", "other/x", "base/inside"]:
            self.fake.put_raw("files", name, b"data")
        d = self.new_driver(base_path="/base")
        self.assertEqual(names(d, "/"), ["inside"])
        for call in (lambda: d.stat("/../base2/secret"), lambda: d.open("/.."), lambda: d.list("/x/../.."),
                     lambda: d.delete("/"), lambda: d.rename("/inside", "../x"), lambda: d.stat("//inside"),
                     lambda: d.stat("/" + "a" * 1025)):
            with self.assertRaises(InvalidPath):
                call()
        d.delete("/inside")
        self.assertEqual(self.fake.names("files"), ["base2/secret", "basefile", "other/x"])

    def test_broken_base_paths(self):
        for base in ("/a/../b", "/x./y"):
            with self.subTest(base=base):
                d = self.new_driver(base_path=base)
                for call in (lambda: d.list("/"), lambda: d.stat("/x"), lambda: put(d, "/x", b"x")):
                    with self.assertRaises(InvalidPath):
                        call()

    def test_names_with_dot_parts_made_by_other_programs(self):
        """httpx resolves "." and ".." parts of a URL's path, while Shared Key signed it as written: such a blob can't
        be asked for. A folder that holds one isn't deleted in part; a move or a copy leaves it where it is."""
        for name in ("d/a.txt", "d/b.txt", "d/./c.txt", "keep.txt"):
            self.fake.put_raw("files", name, b"x")
        d = self.new_driver()
        with self.assertRaises(DriverError) as caught:
            d.delete("/d")
        self.assertNotIsInstance(caught.exception, AuthError)
        self.assertIn("d/./c.txt", caught.exception.message)
        self.assertEqual(self.fake.names("files"), ["d/./c.txt", "d/a.txt", "d/b.txt", "keep.txt"])
        self.fake.put_raw("files", "box/k", b"k")
        d.copy("/d", "/box")
        d.rename("/d", "e")
        self.assertEqual(self.fake.names("files"), ["box/d/a.txt", "box/d/b.txt", "box/k", "d/./c.txt", "e/a.txt",
                                                    "e/b.txt", "keep.txt"])

    def test_move_into_a_file(self):
        d = self.new_driver()
        put(d, "/a", b"a")
        put(d, "/f", b"f")
        with self.assertRaises(NotADirectory):
            d.move("/a", "/f")
        d.move("/a", "/")  # into its own folder: nothing to do
        self.assertEqual(read(d, "/a"), b"a")


class DataLakeTests(FakeMixin, unittest.TestCase):
    """What differs with a hierarchical namespace, beyond the contract."""

    hns = True

    def test_directories(self):
        d = self.new_driver()
        d.mkdir("/a/b")
        self.assertEqual(self.fake.names("files"), ["a", "a/b"])  # real directories, named without "/"
        self.assertEqual(self.fake.blob("files", "a/b").kind, "directory")
        put(d, "/a/b/c/d.txt", b"d")
        d.delete("/a/b/c/d.txt")
        self.assertEqual(d.list("/a/b/c"), [])  # a directory stays when emptied
        d.delete("/a")  # deepest first: Delete Blob takes a directory only when it is empty
        self.assertEqual(self.fake.names("files"), [])

    def test_a_listing_through_a_file(self):
        """A prefix that runs through a file fails: the driver then finds the file."""
        d = self.new_driver()
        put(d, "/f.txt", b"f")
        with self.assertRaises(NotADirectory):
            d.list("/f.txt")
        put(d, "/f.txt", b"again")  # an overwrite asks for the blob before any listing
        self.assertEqual(read(d, "/f.txt"), b"again")


# --- threads ----------------------------------------------------------------------------------------

class ConcurrencyTests(FakeMixin, unittest.TestCase):
    def test_one_driver_many_threads(self):
        d = self.new_driver()
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


# --- params -----------------------------------------------------------------------------------------

class ParamsTests(unittest.TestCase):
    good = {"endpoint": "https://mystorageaccount.blob.core.windows.net/", "account_name": "mystorageaccount",
            "account_key": base64.b64encode(os.urandom(64)).decode(), "container": "files", "base_path": "/",
            "is_private": True, "domain": ""}

    def errors(self, **changes):
        return AzureBlobDriver.validate_config(dict(self.good, **changes))

    def test_good(self):
        self.assertEqual(self.errors(), {})
        for changes in ({"endpoint": "https://mystorageaccount.blob.core.chinacloudapi.cn"},
                        {"endpoint": "https://mystorageaccount-secondary.blob.core.windows.net"},
                        {"endpoint": "http://127.0.0.1:10000", "account_name": "devstoreaccount1"},
                        {"container": "$web"}, {"container": "a-b-c"}, {"container": "a" * 63},
                        {"base_path": "/a b/c", "domain": "https://cdn.example.com/"},
                        {"account_key": " " + self.good["account_key"] + " "}):
            with self.subTest(changes=changes):
                self.assertEqual(self.errors(**changes), {})

    def test_bad(self):
        cases = [("endpoint", {"endpoint": "mystorageaccount.blob.core.windows.net"}),
                 ("endpoint", {"endpoint": "https://mystorageaccount.blob.core.windows.net/files"}),
                 ("endpoint", {"endpoint": "https://other.blob.core.windows.net"}),
                 ("account_name", {"account_name": ""}), ("account_name", {"account_name": "My-Account"}),
                 ("account_name", {"account_name": "ab"}), ("account_name", {"account_name": "a" * 25}),
                 ("account_key", {"account_key": ""}), ("account_key", {"account_key": "not base64!"}),
                 ("container", {"container": ""}), ("container", {"container": "Files"}),
                 ("container", {"container": "ab"}), ("container", {"container": "a--b"}),
                 ("container", {"container": "-ab"}), ("container", {"container": "ab-"}),
                 ("container", {"container": "$root"}), ("container", {"container": "a" * 64}),
                 ("base_path", {"base_path": "/a/../b"}), ("base_path", {"base_path": "/photos."}),
                 ("domain", {"domain": "cdn.example.com"}), ("domain", {"domain": "https://cdn.example.com/files"})]
        for key, changes in cases:
            with self.subTest(changes=changes):
                self.assertIn(key, self.errors(**changes))

    def test_params_follow_the_forms_rules(self):
        """test_config's checks of every registered driver, for this one (not registered yet)."""
        keys = [param.key for param in AzureBlobDriver.params]
        self.assertEqual(len(keys), len(set(keys)))
        for param in AzureBlobDriver.params:
            with self.subTest(param=param.key):
                self.assertTrue(param.label)
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    storage_config.check_value(param, param.default)
        self.assertEqual([param.key for param in AzureBlobDriver.params if param.secret], ["account_key"])
        self.assertEqual(storage_config.setup_keys(AzureBlobDriver), ["endpoint", "account_name", "account_key", "container"])
        self.assertEqual(AzureBlobDriver.defaults()["base_path"], "/")
        self.assertIs(AzureBlobDriver.defaults()["is_private"], True)
        cleaned = storage_config.clean(AzureBlobDriver, dict(self.good, base_path="", domain=""))
        self.assertEqual((cleaned["base_path"], cleaned["is_private"]), ("/", True))
        self.assertTrue(AzureBlobDriver.can_direct_download and not AzureBlobDriver.can_direct_upload)


if __name__ == "__main__":
    unittest.main()
