"""Bunny Storage: the contract against an in-memory Bunny (fake_bunny), once as the documentation reads and once with
the other answer to each thing it leaves open; links with Advanced tokens (Bunny's own test vector), uploads with
checksums, renames by hand, errors, names and params.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_bunny
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import hmac
import io
import logging
import os
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit

import httpx

from apps.storages import config as storage_config
from apps.storages.drivers.base import (
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
    Unsupported,
)
from apps.storages.drivers.bunny import ENDPOINTS, BunnyStorageDriver, advanced_token, bunny_error
from apps.storages.drivers.s3 import content_type_of
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_bunny import FakeBunny

TOKEN_KEY = "fake-pull-zone-key"
fake: FakeBunny


def setUpModule():
    global fake
    fake = FakeBunny()
    # Retries and errors are logged as warnings; keep them out of the test output.
    logging.getLogger("hfile.storage").addHandler(logging.NullHandler())


def tearDownModule():
    fake.stop()


def no_pause(seconds):
    pass


def make(test=None, **changes) -> BunnyStorageDriver:
    """A driver of the fake's zone; retries don't wait."""
    attrs = {"api_url": fake.url, "_pause": staticmethod(no_pause), **changes.pop("attrs", {})}
    values = {"storage_zone": fake.zone, "password": fake.password, "endpoint": ENDPOINTS[0][0], "base_path": "/",
              "pull_zone": "", "token_key": ""}
    values.update(changes)
    driver = type("Local", (BunnyStorageDriver,), attrs)(values)
    if test is not None:
        test.addCleanup(driver.close)
    return driver


def put(driver, path, data):
    driver.write(path, io.BytesIO(data), len(data))


def read(driver, path, offset=0, length=None):
    with driver.open(path, offset, length) as stream:
        return b"".join(stream)


def names(driver, path):
    return sorted(entry.name for entry in driver.list(path))


class NonSeekable(io.RawIOBase):
    """Data that can only be read once, like a request body."""

    def __init__(self, data):
        self._data = io.BytesIO(data)

    def readable(self):
        return True

    def read(self, size=-1):
        return self._data.read(size)


class Changing(io.RawIOBase):
    """Seekable data that changes once it is sought back: between its checksum and its upload."""

    def __init__(self, first, later):
        self._versions = [first, later]
        self._data = io.BytesIO(first)

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self._data.tell()

    def seek(self, offset, whence=0):
        if len(self._versions) > 1:
            self._versions.pop(0)
            self._data = io.BytesIO(self._versions[0])
        return self._data.seek(offset, whence)

    def read(self, size=-1):
        return self._data.read(size)


# --- the contract -----------------------------------------------------------------------------------

class BunnyContract(DriverContract):
    """The contract, but for renaming folders: Bunny has no rename, so a folder can't be renamed (Unsupported) and
    nothing changes."""

    renames_folders = False


class DocumentedContract(BunnyContract, unittest.TestCase):
    """Bunny as its documentation and SDK read, downloads from a pull zone with token authentication."""

    def make_driver(self):
        fake.reset()
        fake.token_key = TOKEN_KEY
        return make(pull_zone=fake.cdn_url, token_key=TOKEN_KEY)


class OtherAnswersContract(BunnyContract, unittest.TestCase):
    """A base path, and the other answer to each thing the documentation leaves open: names that ignore case, a
    DESCRIBE that doesn't answer folders, a missing folder that lists as empty, errors as one object."""

    def make_driver(self):
        fake.reset()
        fake.ignore_case = fake.missing_lists_empty = fake.error_object = True
        fake.describe_folders = False
        return make(base_path="/base dir/子 文件夹")


class RefusedDescribeContract(BunnyContract, unittest.TestCase):
    """A Bunny that refuses DESCRIBE (405): files and folders are found in listings."""

    def make_driver(self):
        fake.reset()
        fake.describe_status = 405
        return make(base_path="/b")


# --- links ------------------------------------------------------------------------------------------

class LinkTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_bunnys_own_vector(self):
        """BunnyCDN.TokenAuthentication's e2e/vectors.json, "plain": key SecurityKey, /300kb.jpg, expires 1598024587."""
        self.assertEqual(advanced_token("SecurityKey", "/300kb.jpg", 1598024587),
                         "HS256-o10JRWlsAItyAsdKS6jJKjabHN4FrFsplDHPV1idcX4")

    def test_a_signed_link(self):
        fake.token_key = TOKEN_KEY
        d = make(self, base_path="/base", pull_zone=fake.cdn_url + "/", token_key=TOKEN_KEY)
        put(d, "/a dir/报告 1.pdf", b"%PDF")
        before = len(fake.requests)
        url = d.download_url("/a dir/报告 1.pdf", filename="报告 1.pdf", expires=600)
        self.assertEqual(len(fake.requests), before)  # made here, without asking Bunny
        parts = urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}",
                         f"{fake.cdn_url}/base/a%20dir/%E6%8A%A5%E5%91%8A%201.pdf")
        query = parse_qs(parts.query)
        expires = int(query["expires"][0])
        self.assertLess(abs(expires - time.time() - 600), 5)
        # The docs' formula, over the path in the zone as written, not percent-encoded.
        digest = hmac.new(TOKEN_KEY.encode(), f"/base/a dir/报告 1.pdf{expires}".encode(), hashlib.sha256).digest()
        self.assertEqual(query["token"], ["HS256-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")])
        response = httpx.get(url)
        self.assertEqual((response.status_code, response.content), (200, b"%PDF"))

    def test_links_that_are_refused(self):
        fake.token_key = TOKEN_KEY
        d = make(self, pull_zone=fake.cdn_url, token_key=TOKEN_KEY)
        put(d, "/a.txt", b"a")
        put(d, "/b.txt", b"b")
        url = d.download_url("/a.txt", filename="a.txt", expires=600)
        self.assertEqual(httpx.get(url).status_code, 200)
        token = parse_qs(urlsplit(url).query)["token"][0]
        tampered = token[:6] + ("A" if token[6] != "A" else "B") + token[7:]
        self.assertEqual(httpx.get(url.replace(token, tampered)).status_code, 403)
        self.assertEqual(httpx.get(url.replace("/a.txt?", "/b.txt?")).status_code, 403)  # another file
        self.assertEqual(httpx.get(url.split("?")[0]).status_code, 403)  # no token
        other = make(self, pull_zone=fake.cdn_url, token_key="another key").download_url("/a.txt", filename="a.txt")
        self.assertEqual(httpx.get(other).status_code, 403)
        fake.clock_skew = 700  # past its expiry
        self.assertEqual(httpx.get(url).status_code, 403)

    def test_a_pull_zone_without_tokens(self):
        d = make(self, pull_zone=fake.cdn_url)
        put(d, "/x y.txt", b"public")
        url = d.download_url("/x y.txt", filename="x y.txt")
        self.assertEqual(url, f"{fake.cdn_url}/x%20y.txt")
        self.assertEqual(httpx.get(url).content, b"public")

    def test_without_a_pull_zone(self):
        """Downloads go through the site: the instance has no links (the class may, for the form's options)."""
        d = make(self)
        self.assertFalse(d.can_direct_download)
        self.assertTrue(BunnyStorageDriver.can_direct_download)
        self.assertIsNone(d.download_url("/a.txt", filename="a.txt"))
        self.assertTrue(make(self, pull_zone=fake.cdn_url).can_direct_download)
        self.assertFalse(make(self, pull_zone="not an address").can_direct_download)


# --- uploads ----------------------------------------------------------------------------------------

class UploadTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def last_put(self):
        return [request for request in fake.requests if request.method == "PUT"][-1]

    def test_a_checksum_with_seekable_data(self):
        d = make(self)
        data = os.urandom(5000)
        put(d, "/f/a.txt", data)
        headers = self.last_put().headers
        self.assertEqual(headers["checksum"], hashlib.sha256(data).hexdigest().upper())
        self.assertEqual((headers["content-length"], headers["content-type"]), ("5000", "text/plain"))
        self.assertEqual(read(d, "/f/a.txt"), data)
        source = io.BytesIO(b"skipHELLO")
        source.seek(4)  # the data starts where the stream is
        d.write("/h.txt", source, 5)
        self.assertEqual(self.last_put().headers["checksum"], hashlib.sha256(b"HELLO").hexdigest().upper())
        self.assertEqual(read(d, "/h.txt"), b"HELLO")

    def test_no_checksum_for_a_stream(self):
        d = make(self)
        d.write("/s.bin", NonSeekable(b"stream"), 6)
        self.assertNotIn("checksum", self.last_put().headers)
        self.assertEqual(read(d, "/s.bin"), b"stream")

    def test_a_checksum_bunny_refuses(self):
        """Data that changed between its checksum and its upload: Bunny refuses it (400), nothing is stored."""
        d = make(self)
        with self.assertRaises(DriverError) as caught:
            d.write("/changed.bin", Changing(b"first", b"later"), 5)
        self.assertIn("400", str(caught.exception))
        self.assertNotIn("/changed.bin", fake.files())

    def test_failures_are_tried_again(self):
        d = make(self)
        fake.fail(503, "Service Unavailable", method="PUT")
        fake.fail(400, "Unable to process request", method="PUT")
        put(d, "/r.txt", b"retried")
        self.assertEqual(read(d, "/r.txt"), b"retried")
        fake.fail(503, "Service Unavailable", method="PUT")
        with self.assertRaises(BackendError):
            d.write("/once.bin", NonSeekable(b"once"), 4)  # can't be sent again
        self.assertNotIn("/once.bin", fake.files())

    def test_short_data(self):
        for data in (io.BytesIO(b"abc"), NonSeekable(b"abc")):
            with self.subTest(data=type(data).__name__):
                with self.assertRaises(DriverError):
                    make(self).write("/short.bin", data, 10)
        self.assertEqual(fake.files(), {})

    def test_onto_a_file_in_the_way(self):
        d = make(self)
        put(d, "/f", b"file")
        with self.assertRaises(DriverError):
            put(d, "/f/inside.txt", b"x")
        self.assertEqual(fake.files(), {"/f": b"file"})


# --- folders ----------------------------------------------------------------------------------------

class FolderTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_mkdir_puts_the_folder(self):
        d = make(self)
        d.mkdir("/p/q")
        request = [request for request in fake.requests if request.method == "PUT"][-1]
        self.assertEqual((request.path, request.size, request.headers["content-length"]), (f"/{fake.zone}/p/q/", 0, "0"))
        self.assertTrue(d.stat("/p").is_dir)
        self.assertEqual(d.list("/p/q"), [])

    def test_a_folder_goes_in_one_delete(self):
        d = make(self)
        for index in range(5):
            put(d, f"/big/sub{index % 2}/f{index}", b"%d" % index)
        fake.requests.clear()
        d.delete("/big")
        deletes = [request.path for request in fake.requests if request.method == "DELETE"]
        self.assertEqual(deletes, [f"/{fake.zone}/big/"])
        self.assertEqual((fake.files(), fake.folders()), ({}, {"/"}))

    def test_a_base_folder_that_is_not_there_yet(self):
        d = make(self, base_path="/new/base")
        self.assertEqual(d.list("/"), [])
        put(d, "/a.txt", b"a")
        self.assertEqual(fake.files(), {"/new/base/a.txt": b"a"})


# --- renames ----------------------------------------------------------------------------------------

class RenameTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_a_file_is_downloaded_uploaded_and_deleted(self):
        d = make(self)
        put(d, "/a.txt", b"aaa")
        fake.requests.clear()
        d.rename("/a.txt", "b.md")
        steps = [(request.method, request.path) for request in fake.requests
                 if request.method in ("GET", "PUT", "DELETE") and not request.path.endswith("/")]
        self.assertEqual(steps, [("GET", f"/{fake.zone}/a.txt"), ("PUT", f"/{fake.zone}/b.md"), ("DELETE", f"/{fake.zone}/a.txt")])
        self.assertEqual([request for request in fake.requests if request.method == "PUT"][0].headers["content-type"],
                         content_type_of("b.md"))
        self.assertEqual(fake.files(), {"/b.md": b"aaa"})

    def test_a_failed_upload_keeps_the_file(self):
        d = make(self)
        put(d, "/a.txt", b"aaa")
        fake.fail(500, "Internal Server Error", method="PUT", times=3)
        with self.assertRaises(BackendError):
            d.rename("/a.txt", "b.txt")
        self.assertEqual(fake.files(), {"/a.txt": b"aaa"})

    def test_a_folder(self):
        d = make(self)
        put(d, "/d/x.txt", b"x")
        before = len(fake.requests)
        with self.assertRaises(Unsupported):
            d.rename("/d", "e")
        self.assertFalse([request for request in fake.requests[before:] if request.method in ("PUT", "DELETE")])

    def test_only_the_case_where_case_is_ignored(self):
        """Uploaded straight to "report.txt", Bunny ignoring case would replace the file itself, and the delete that
        follows would remove it: the rename goes through a third name, and nothing is lost or left behind."""
        fake.ignore_case = True
        d = make(self)
        put(d, "/Report.TXT", b"r")
        d.rename("/Report.TXT", "report.txt")
        self.assertEqual(fake.files(), {"/report.txt": b"r"})
        self.assertEqual(names(d, "/"), ["report.txt"])

    def test_names_bunny_refuses(self):
        d = make(self)
        put(d, "/a.txt", b"a")
        for new_name in (" a.txt", "a.txt ", "b" * 6001):
            with self.subTest(new_name=new_name[:10]):
                with self.assertRaises(InvalidPath):
                    d.rename("/a.txt", new_name)
        self.assertEqual(fake.files(), {"/a.txt": b"a"})


# --- names and paths --------------------------------------------------------------------------------

class LookupTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_what_a_lookup_costs(self):
        """As the documentation and SDK read: a file is one DESCRIBE; nothing there, a DESCRIBE and a listing."""
        d = make(self)
        put(d, "/d/f.txt", b"f")
        fake.requests.clear()
        self.assertFalse(d.stat("/d/f.txt").is_dir)
        self.assertEqual([(request.method, request.path) for request in fake.requests], [("DESCRIBE", f"/{fake.zone}/d/f.txt")])
        fake.requests.clear()
        with self.assertRaises(NotFound):
            d.stat("/d/nope")
        self.assertEqual([(request.method, request.path) for request in fake.requests],
                         [("DESCRIBE", f"/{fake.zone}/d/nope"), ("GET", f"/{fake.zone}/d/nope/")])

    def test_a_ranged_download_through_the_site(self):
        """The site calls stat(), then open_known() with what it gave: Bunny is asked once what the file is."""
        data = bytes(range(256)) * 40
        fake.add("/video.mp4", data)
        d = make(self)
        fake.requests.clear()
        entry = d.stat("/video.mp4")
        with d.open_known("/video.mp4", entry, 100, 50) as stream:
            self.assertEqual(b"".join(stream), data[100:150])
        self.assertEqual([request.method for request in fake.requests], ["DESCRIBE", "GET"])

    def test_a_refused_describe_is_not_asked_again(self):
        fake.describe_status = 405
        d = make(self)
        put(d, "/a.txt", b"a")
        self.assertEqual(d.stat("/a.txt").size, 1)
        self.assertTrue(d.stat("/").is_dir)
        self.assertEqual(len([request for request in fake.requests if request.method == "DESCRIBE"]), 1)


class NameTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_names_bunny_refuses_are_refused_first(self):
        d = make(self)
        long_path = "/" + "/".join(["a" * 200] * 30) + "/file.txt"  # more than 6,000 characters
        before = len(fake.requests)
        for call in (lambda: put(d, "/ lead.txt", b"x"), lambda: put(d, "/trail.txt ", b"x"), lambda: d.mkdir("/dir "),
                     lambda: put(d, "/ sub/x.txt", b"x"), lambda: put(d, long_path, b"x"), lambda: d.mkdir(long_path)):
            with self.assertRaises(InvalidPath):
                call()
        self.assertEqual(len(fake.requests), before)

    def test_names_this_site_can_not_use_are_left_out(self):
        listing = [{"ObjectName": name, "IsDirectory": False, "Length": 1, "LastChanged": "2017-03-10T03:06:48.203"}
                   for name in ("ok.txt", "", "..", "...", "a/b", "nul\x00")]
        d = mock_driver(self, lambda request: httpx.Response(200, json=listing))
        self.assertEqual([entry.name for entry in d.list("/")], ["ok.txt"])

    def test_times_are_utc(self):
        listing = [{"ObjectName": "a.txt", "IsDirectory": False, "Length": 3, "LastChanged": "2017-03-10T03:06:48.203"}]
        entry = mock_driver(self, lambda request: httpx.Response(200, json=listing)).list("/")[0]
        self.assertEqual(entry.modified, datetime.datetime(2017, 3, 10, 3, 6, 48, 203000, tzinfo=datetime.timezone.utc))
        self.assertEqual(entry.size, 3)

    def test_confinement(self):
        fake.add("/base2/secret", b"s")
        d = make(self, base_path="/base")
        put(d, "/inside", b"i")
        self.assertEqual(names(d, "/"), ["inside"])
        for call in (lambda: d.stat("/../base2/secret"), lambda: d.open("/.."), lambda: d.delete("/"),
                     lambda: d.rename("/inside", "../x"), lambda: d.list("//"), lambda: d.download_url("/..", filename="x"),
                     lambda: make(self, base_path="/a/../b").list("/"), lambda: make(self, base_path="/ spaced").stat("/")):
            with self.assertRaises(InvalidPath):
                call()
        self.assertEqual(fake.files(), {"/base2/secret": b"s", "/base/inside": b"i"})

    def test_list_and_open_of_the_wrong_kind(self):
        d = make(self)
        put(d, "/f.txt", b"f")
        d.mkdir("/d")
        with self.assertRaises(NotADirectory):
            d.list("/f.txt")
        with self.assertRaises(NotFound):
            d.open("/d")
        with self.assertRaises(NotFound):
            d.list("/f.txt/inner")


# --- errors -----------------------------------------------------------------------------------------

class LoseFirstDeleteAnswer(httpx.BaseTransport):
    """The first DELETE reaches Bunny and is done there; its answer is lost (the connection drops)."""

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


class LostAnswerTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_a_delete_whose_answer_was_lost(self):
        fake.add("/report.pdf", b"x")
        fake.add("/a.txt", b"a")
        for call, left in ((lambda d: d.delete("/report.pdf"), ["/a.txt"]),
                           (lambda d: d.rename("/a.txt", "b.txt"), ["/b.txt"])):  # its last step is a DELETE
            transport = LoseFirstDeleteAnswer()
            self.addCleanup(transport.close)
            call(make(self, attrs={"transport": transport}))  # sent again, it finds nothing: that is done
            self.assertEqual(sorted(fake.files()), left)


def mock_driver(test, handler, **changes):
    """A driver whose requests go to `handler` (httpx.MockTransport); retries don't wait."""
    values = {"storage_zone": "zone", "password": "mock-password", "endpoint": "storage.bunnycdn.com", "base_path": "/"}
    values.update(changes)
    driver = type("Mocked", (BunnyStorageDriver,), {"transport": httpx.MockTransport(handler),
                                                   "_pause": staticmethod(no_pause)})(values)
    test.addCleanup(driver.close)
    return driver


class ErrorTests(unittest.TestCase):
    def setUp(self):
        fake.reset()

    def test_a_wrong_password_or_zone(self):
        for changes in ({"password": "wrong password"}, {"storage_zone": "other-zone"}):
            with self.subTest(changes=list(changes)):
                with self.assertRaises(AuthError) as caught:
                    make(self, **changes).check()
                self.assertIn("401", str(caught.exception))
                self.assertNotIn("wrong password", str(caught.exception))

    def test_busy_answers_are_tried_again(self):
        d = make(self)
        fake.fail(400, "Unable to process request", method="GET")
        fake.fail(429, "Too Many Requests", method="GET", headers={"Retry-After": "0"})
        self.assertEqual(d.list("/"), [])
        fake.fail(400, "Unable to process request", times=3)
        with self.assertRaises(BackendError):
            d.list("/")

    def test_a_long_retry_after_is_not_waited_for(self):
        fake.fail(429, "Too Many Requests", headers={"Retry-After": "120"})
        with self.assertRaises(BackendError):
            make(self).list("/")
        self.assertEqual(len(fake.requests), 1)

    def test_bunnys_messages(self):
        for error_object in (False, True):
            with self.subTest(error_object=error_object):
                fake.reset()
                fake.error_object = error_object
                fake.fail(400, "Something Bunny says")
                with self.assertRaises(DriverError) as caught:
                    make(self).list("/")
                self.assertNotIsInstance(caught.exception, (BackendError, AuthError))
                self.assertIn("Something Bunny says", str(caught.exception))

    def test_the_mapping(self):
        cases = [(401, "", AuthError), (403, "", AuthError), (404, "", NotFound), (429, "", BackendError),
                 (400, "Unable to process request", BackendError), (500, "", BackendError), (400, "Upload failed", DriverError)]
        for status, message, kind in cases:
            with self.subTest(status=status, message=message):
                error = bunny_error(status, message)
                self.assertIs(type(error), kind)
                self.assertEqual(error.status, status)
                self.assertEqual(error.busy, status == 429 or message == "Unable to process request")
        self.assertTrue(bunny_error(400, "", "Unable to process request").busy)  # the reason phrase says it

    def test_connection_errors(self):
        seen = []

        def handler(request):
            seen.append(request)
            raise httpx.ConnectError("[Errno 111] Connection refused")

        with self.assertRaises(BackendError):
            mock_driver(self, handler).list("/")
        self.assertEqual(len(seen), 3)

    def test_unexpected_answers(self):
        for response in (httpx.Response(302, headers={"Location": "https://elsewhere.example/"}),
                         httpx.Response(200, content=b"<html>not json</html>"), httpx.Response(200, json={"a": 1})):
            with self.subTest(status=response.status_code, body=response.content[:12]):
                with self.assertRaises(BackendError):
                    mock_driver(self, lambda request, response=response: response).list("/")

    def test_a_server_ignoring_range(self):
        data = bytes(range(256)) * 40

        def handler(request):
            if request.method == "DESCRIBE":
                return httpx.Response(200, json={"ObjectName": "a.bin", "IsDirectory": False, "Length": len(data)})
            return httpx.Response(200, stream=httpx.ByteStream(data), headers={"content-length": str(len(data))})

        with mock_driver(self, handler).open("/a.bin", 100, 50) as stream:
            self.assertEqual(b"".join(stream), data[100:150])
            self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, len(data)))

    def test_logs_carry_no_password(self):
        with self.assertLogs("hfile.storage.bunny-storage", "DEBUG") as logs, self.assertRaises(AuthError):
            make(self, password="secret-password").list("/")
        self.assertNotIn("secret-password", "\n".join(logs.output))


# --- threads ----------------------------------------------------------------------------------------

class ConcurrencyTests(unittest.TestCase):
    def test_one_driver_many_threads(self):
        fake.reset()
        d = make(self)
        errors = []

        def work(index):
            try:
                for step in range(4):
                    path = f"/t{index}/f{step}.bin"
                    data = os.urandom(1000 + step)
                    put(d, path, data)
                    if read(d, path) != data:
                        errors.append(path)
                    d.list(f"/t{index}")
            except Exception as exc:  # noqa: BLE001 - collected for the assertion
                errors.append(exc)

        threads = [threading.Thread(target=work, args=(index,)) for index in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(d.list("/")), 6)


# --- params -----------------------------------------------------------------------------------------

class ParamsTests(unittest.TestCase):
    good = {"storage_zone": "my-zone-1", "password": "a1b2c3d4-e5f6-7890-abcd-ef1234567890", "endpoint": "ny.storage.bunnycdn.com",
            "base_path": "/", "pull_zone": "", "token_key": ""}

    def errors(self, **changes):
        return BunnyStorageDriver.validate_config(dict(self.good, **changes))

    def test_good(self):
        self.assertEqual(self.errors(), {})
        for changes in ({"base_path": "/a b/c"}, {"pull_zone": "https://myzone.b-cdn.net/"},
                        {"pull_zone": "https://cdn.example.com", "token_key": "key"}, {"storage_zone": "Zone"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.errors(**changes), {})

    def test_bad(self):
        cases = [("storage_zone", {"storage_zone": ""}), ("storage_zone", {"storage_zone": "my zone"}),
                 ("storage_zone", {"storage_zone": "zone/x"}), ("storage_zone", {"storage_zone": "z" * 64}),
                 ("password", {"password": ""}), ("password", {"password": "has space"}), ("password", {"password": "密码"}),
                 ("base_path", {"base_path": "/a/../b"}), ("base_path", {"base_path": "/folder /x"}),
                 ("pull_zone", {"pull_zone": "myzone.b-cdn.net"}), ("pull_zone", {"pull_zone": "https://cdn.example.com/files"}),
                 ("token_key", {"token_key": "key"})]
        for key, changes in cases:
            with self.subTest(changes=changes):
                self.assertIn(key, self.errors(**changes))

    def test_params_follow_the_forms_rules(self):
        """test_config's checks of every registered driver, for this one (not registered yet)."""
        keys = [param.key for param in BunnyStorageDriver.params]
        self.assertEqual(keys, ["storage_zone", "password", "endpoint", "base_path", "pull_zone", "token_key"])
        for param in BunnyStorageDriver.params:
            with self.subTest(param=param.key):
                self.assertTrue(param.label)
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.kind == "select":
                    self.assertTrue(param.options)
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    storage_config.check_value(param, param.default)
        self.assertEqual([param.key for param in BunnyStorageDriver.params if param.secret], ["password", "token_key"])
        self.assertEqual(storage_config.setup_keys(BunnyStorageDriver), ["storage_zone", "password", "endpoint"])
        self.assertEqual(BunnyStorageDriver.defaults()["endpoint"], "storage.bunnycdn.com")
        cleaned = storage_config.clean(BunnyStorageDriver, dict(self.good, base_path="", pull_zone=""))
        self.assertEqual((cleaned["base_path"], cleaned["endpoint"]), ("/", "ny.storage.bunnycdn.com"))
        with self.assertRaises(storage_config.ConfigError):
            storage_config.clean(BunnyStorageDriver, dict(self.good, endpoint="storage.example.com"))

    def test_flags(self):
        cls = BunnyStorageDriver
        self.assertEqual((cls.type, cls.can_move, cls.can_copy, cls.can_direct_upload, cls.has_folders, cls.can_range),
                         ("bunny-storage", False, False, False, True, True))

    def test_an_endpoint_that_is_not_bunnys(self):
        """Only Bunny's endpoints get the password (a config written by hand, say)."""
        driver = BunnyStorageDriver(dict(self.good, endpoint="storage.example.com"))
        self.addCleanup(driver.close)
        with self.assertRaises(BackendError):
            driver.list("/")


if __name__ == "__main__":
    unittest.main()
