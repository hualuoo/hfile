"""Tests of the CloudFlare ImgBed driver (apps.storages.drivers.cf_imgbed) against tests/fake_cf_imgbed.py.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_cf_imgbed
"""

from __future__ import annotations

import base64
import hashlib
import io
import os
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages import config
from apps.storages.drivers import http, paths
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.drivers.cf_imgbed import FORBIDDEN, ImgBedDriver
from apps.storages.tests.contract import UNUSUAL_NAMES, DriverContract
from apps.storages.tests.fake_cf_imgbed import NAME_UNSAFE, FakeImgBed
from apps.storages.tests.fake_cloud_http import FakeServer, Response

KIB = 1024
ALL = ("list", "upload", "delete", "manage")
SMALL = {"chunk_above": 600 * KIB, "chunk_size": 256 * KIB, "discord_chunk": 200 * KIB, "direct_from": 600 * KIB}


def driver_class(**attrs):
    """ImgBedDriver with other tuning (the type stays "cf-imgbed")."""
    return type("FakeImgBedDriver", (ImgBedDriver,), attrs)


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


class Stream(io.RawIOBase):
    """Data that can't seek."""

    def __init__(self, raw: bytes):
        self._raw = io.BytesIO(raw)

    def readable(self):
        return True

    def readinto(self, buffer):
        return self._raw.readinto(buffer)


class FakeMixin:
    driver_attrs: dict = SMALL

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeImgBed().start()
        cls.Driver = driver_class(**cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, base: str = "/base", *, token: str | None = None, **settings):
        settings = {"url": self.fake.url, "token": self.token if token is None else token, "base_path": base,
                    "channel": "telegram", **settings}
        driver = self.Driver(settings)
        self.addCleanup(driver.close)
        return driver


class ContractWithoutSpaces(DriverContract):
    """The contract, three of its tests again with "-" where they put a space: ImgBed makes "_" of spaces in new names
    (forbidden_chars has the space), and the driver refuses them."""

    def test_prefix_siblings_stay_apart(self):
        self.put("/a/in.txt", b"1")
        self.put("/a-b/out.txt", b"2")
        self.put("/ab.txt", b"3")
        self.assertEqual(self.names("/a"), ["in.txt"])
        self.d.rename("/a", "z")
        self.assertEqual(self.names("/"), ["a-b", "ab.txt", "z"])
        self.assertFile("/a-b/out.txt", b"2")

    def test_unusual_names(self):
        tried = 0
        for name in UNUSUAL_NAMES:
            if not self.allowed(name) or len(name.encode()) > self.d.max_name_bytes:
                continue
            with self.subTest(name=name):
                tried += 1
                path = paths.join("/", name)
                data = name.encode("utf-8")
                self.put(path, data)
                self.assertIn(name, self.names("/"))
                self.assertFile(path, data)
                self.assertEqual(self.d.stat(path).name, name)
                self.put(paths.join("/", "dir-" + name, "inner.txt"), b"in")
                self.assertEqual(self.names(paths.join("/", "dir-" + name)), ["inner.txt"])
                self.d.delete(path)
                self.d.delete(paths.join("/", "dir-" + name))
        self.assertEqual(self.d.list("/"), [])
        self.assertEqual(tried, 3)  # ～全角～, café and 200 a's: the others have spaces or ~

    def test_download_url(self):
        data = os.urandom(5000)
        self.put("/dl/文件-1.bin", data)
        url = self.d.download_url("/dl/文件-1.bin", filename="文件-1.bin", expires=600, user_agent=self.browser_user_agent)
        response = self.fetch_url(url, self.browser_user_agent)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.content, data)


class ImgBedContractTests(FakeMixin, ContractWithoutSpaces, unittest.TestCase):
    """The contract in a folder of a site whose files go to Telegram: large files in chunks."""

    def make_driver(self):
        self.fake.reset()
        self.token = self.fake.add_token(*ALL)
        self.fake.put("keep/out.txt", b"not in the base")
        self.fake.put("base-not/out.txt", b"nor this")
        return self.new_driver("/base/here")


class ImgBedHuggingFaceContractTests(FakeMixin, ContractWithoutSpaces, unittest.TestCase):
    """The contract at ImgBed's root, with a Hugging Face channel: large files straight to it, in parts."""

    def make_driver(self):
        self.fake.reset()
        self.fake.lfs_part_size = 256 * KIB
        self.token = self.fake.add_token(*ALL)
        return self.new_driver("/", channel="huggingface")

    def test_replacing_a_file_sent_straight_to_hugging_face(self):
        """getUploadUrl names a taken name "name(1).ext", and commitUpload refuses a fullId sanitizeUploadFolder changes
        ("(" and ")" are made "_"): the replacement goes up under a name ImgBed keeps, then takes the old one's place."""
        first, second = os.urandom(700 * KIB), os.urandom(700 * KIB)
        self.put("/big.bin", first)
        self.put("/big.bin", second)
        self.assertEqual(self.fake.ids(), ["big.bin"])
        self.assertEqual(self.get("/big.bin"), second)
        self.assertEqual(self.fake.records["big.bin"].metadata["FileName"], "big.bin")


class ImgBedTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.token = self.fake.add_token(*ALL)

    def put(self, driver, path: str, data: bytes) -> None:
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))

    def uploads(self) -> list[dict]:
        return self.fake.requests("POST", "/upload")

    # -- the token

    def test_the_token_goes_in_its_header_only(self):
        d = self.new_driver(channel="huggingface")
        self.fake.lfs_part_size = 256 * KIB
        data = os.urandom(700 * KIB)
        self.put(d, "/a/one.bin", data)  # straight to Hugging Face
        self.put(d, "/a/two.txt", b"two")
        d.rename("/a/two.txt", "three.txt")
        d.move("/a/three.txt", "/")
        self.assertEqual(read(d, "/a/one.bin", 5, 10), data[5:15])
        d.delete("/a")
        api = [call for call in self.fake.calls if not call["path"].startswith("/file/")]
        self.assertTrue(api)
        for call in api:
            self.assertEqual(call["authorization"], "Bearer " + self.token)
        for call in self.fake.calls + self.fake.hf_calls:
            self.assertNotIn(self.token, call["target"])
        self.assertTrue(self.fake.hf_calls)
        for call in self.fake.requests("GET", "/file/.*") + self.fake.hf_calls:
            self.assertIsNone(call["authorization"])

    def test_a_refused_token(self):
        d = self.new_driver()
        d.list("/")
        self.fake.tokens.pop(self.token)  # deleted in ImgBed, or expired
        with self.assertRaises(AuthError):
            d.list("/")
        with self.assertRaises(AuthError):
            self.new_driver(token="").check()
        listless = self.fake.add_token("upload", "delete")
        with self.assertRaises(AuthError) as caught:
            self.new_driver(token=listless).check()
        self.assertIn("list", caught.exception.message)

    def test_without_the_manage_permission(self):
        """New files, reads and deletes need no manage; renames, moves and replacing a file do, and a replacement
        refused leaves the old file as it was."""
        self.token = self.fake.add_token("list", "upload", "delete")
        d = self.new_driver()
        self.put(d, "/a.txt", b"first")
        self.put(d, "/d/b.txt", b"b")
        self.assertEqual(read(d, "/a.txt"), b"first")
        for change in (lambda: d.rename("/a.txt", "c.txt"), lambda: d.move("/a.txt", "/d"), lambda: d.rename("/d", "e"),
                       lambda: self.put(d, "/a.txt", b"second")):
            with self.assertRaises(AuthError) as caught:
                change()
            self.assertIn("manage", caught.exception.message)
        self.assertEqual(self.fake.ids(), ["base/a.txt", "base/d/b.txt"])  # the replacement's upload deleted again
        self.assertEqual(read(d, "/a.txt"), b"first")
        d.delete("/d")
        self.assertEqual(self.names(d, "/"), ["a.txt"])

    # -- listings

    def test_listings(self):
        self.fake.put("base/f.txt", b"12345")
        self.fake.put("base/both", b"a file")
        self.fake.put("base/both/in.txt", b"and a folder")  # ImgBed can have both: the folder is shown
        self.fake.put("base/a,b.txt", b"read as a/b.txt by ImgBed's routes")
        self.fake.put("base/old.bin", b"x" * 100, FileSizeBytes=None, **{"File-Size": "100", "TimeStamp": "1754020094217"})
        self.fake.put("other/x.txt", b"outside the base")
        d = self.new_driver()
        entries = {entry.name: entry for entry in d.list("/")}
        self.assertEqual(sorted(entries), ["both", "f.txt", "old.bin"])
        self.assertTrue(entries["both"].is_dir)
        self.assertEqual((entries["f.txt"].size, entries["old.bin"].size), (5, 100))
        self.assertEqual(round(entries["old.bin"].modified.timestamp() * 1000), 1754020094217)
        self.assertTrue(d.stat("/both").is_dir)
        with self.assertRaises(NotFound):
            read(d, "/both")
        for call in self.fake.requests("GET", "/api/manage/list"):
            self.assertEqual(call["query"]["count"], ["-1"])  # the whole folder at once
        with self.assertRaises(NotADirectory):
            d.list("/f.txt")
        with self.assertRaises(NotFound):
            d.list("/nothing")
        lookups = [call["query"] for call in self.fake.requests("GET", "/api/manage/list") if "search" in call["query"]]
        self.assertIn({"dir": ["base"], "count": ["-1"], "search": ["f.txt"]}, lookups)

    def test_a_listing_right_after_imgbed_lost_its_index(self):
        self.fake.put("base/d/x.txt", b"x")
        self.fake.put("base/d/s/y.txt", b"y")
        self.fake.index_down = True  # ImgBed lists the folder's own files from its database then
        d = self.new_driver()
        self.assertEqual(self.names(d, "/d"), ["s", "x.txt"])
        with self.assertRaises(BackendError):  # a rename would miss the files below "s"
            d.rename("/d", "e")
        self.assertEqual(self.fake.ids(), ["base/d/s/y.txt", "base/d/x.txt"])

    # -- uploads

    def test_upload_queries(self):
        d = self.new_driver(channel="cfr2", channel_name="r2-main")
        self.put(d, "/a/small.txt", b"s")
        self.put(d, "/a/big.bin", os.urandom(700 * KIB))  # in chunks, its id fixed at chunk 0 (R2)
        calls = self.uploads()
        self.assertEqual(len(calls), 1 + 1 + 3 + 1)
        for call in calls:
            query = {key: value[0] for key, value in call["query"].items()}
            self.assertLessEqual({"uploadChannel": "cfr2", "channelName": "r2-main", "uploadNameType": "origin",
                                  "serverCompress": "false", "autoRetry": "false", "uploadFolder": "base/a"}.items(), query.items())
        self.assertEqual(self.fake.ids(), ["base/a/big.bin", "base/a/small.txt"])
        self.assertEqual(self.fake.records["base/a/small.txt"].metadata["FileType"], "text/plain")

    def test_images_stay_as_they_are(self):
        """Telegram would recompress an image sent without serverCompress=false."""
        d = self.new_driver()
        data = os.urandom(3000)
        self.put(d, "/p.jpg", data)
        self.assertEqual(read(d, "/p.jpg"), data)
        self.assertEqual(self.uploads()[0]["file_type"], "image/jpeg")

    def test_chunks(self):
        d = self.new_driver()
        data = os.urandom(3 * 256 * KIB + 100)
        source = io.BytesIO(b"xx" + data)
        source.seek(2)
        d.write("/c.bin", source, len(data))
        init, *chunks, merge = self.uploads()
        self.assertEqual(init["query"]["initChunked"], ["true"])
        self.assertEqual(init["fields"], {"originalFileName": "c.bin", "originalFileType": "application/octet-stream", "totalChunks": "4"})
        self.assertEqual([call["fields"]["chunkIndex"] for call in chunks], ["0", "1", "2", "3"])
        self.assertEqual([call["file_size"] for call in chunks], [256 * KIB] * 3 + [100])
        self.assertEqual(merge["query"]["merge"], ["true"])
        self.assertEqual(self.fake.records["base/c.bin"].data, data)
        self.assertTrue(self.fake.records["base/c.bin"].metadata["IsChunked"])
        self.assertEqual(self.fake.sessions, {})
        d.write("/s.bin", Stream(data), len(data))  # data that can't seek: streamed all the same
        self.assertEqual(read(d, "/s.bin"), data)
        self.put(d, "/edge.bin", os.urandom(600 * KIB))  # not more than 600 KiB: one request
        self.assertEqual(len(self.uploads()), 6 + 6 + 1)

    def test_chunks_by_channel(self):
        data = os.urandom(700 * KIB)
        for channel, requests in (("discord", 1 + 4 + 1), ("s3", 1 + 3 + 1), ("webdav", 1)):  # Discord: 200 KiB here
            with self.subTest(channel=channel):
                self.fake.calls.clear()
                d = self.new_driver(f"/{channel}", channel=channel)
                self.put(d, "/f.bin", data)
                self.assertEqual(len(self.uploads()), requests)
                self.assertEqual(read(d, "/f.bin"), data)

    def test_a_failed_upload_is_cleaned_up(self):
        d = self.new_driver()
        self.fake.fail_chunk = 1  # ImgBed couldn't store it: the merge finds it missing
        with self.assertRaises(BackendError) as caught:
            self.put(d, "/f.bin", os.urandom(700 * KIB))
        self.assertIn("2/3", caught.exception.message)  # ImgBed's message: 2 of the 3 chunks stored
        cleanups = [call for call in self.uploads() if call["query"].get("cleanup") == ["true"]]
        self.assertEqual(len(cleanups), 1)
        self.assertEqual(cleanups[0]["query"]["totalChunks"], ["3"])
        self.fake.fail_chunk = None
        with self.assertRaises(DriverError):  # the data ends early
            d.write("/short.bin", io.BytesIO(b"abc" * 1000), 700 * KIB)
        self.assertEqual(self.fake.sessions, {})
        self.assertEqual(self.names(d, "/"), [])

    def test_direct_uploads_to_hugging_face(self):
        d = self.new_driver(channel="huggingface")
        data = os.urandom(700 * KIB)
        self.put(d, "/hf/one.bin", data)  # one PUT with the header the upload URL came with
        [asked] = [call["json"] for call in self.fake.requests("POST", "/upload/huggingface/getUploadUrl")]
        self.assertEqual(asked["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(base64.b64decode(asked["fileSample"]), data[:512])
        self.assertEqual((asked["uploadNameType"], asked["uploadFolder"], asked["fileSize"]), ("origin", "base/hf", len(data)))
        [put] = self.fake.hf_calls
        self.assertEqual((put["method"], put["content_length"]), ("PUT", str(len(data))))
        self.assertEqual(read(d, "/hf/one.bin"), data)
        self.fake.lfs_part_size = 300 * KIB  # in parts, completed through ImgBed's completeMultipart
        other = os.urandom(700 * KIB)
        d.write("/hf/two.bin", Stream(other), len(other))  # spooled for its SHA-256 first
        parts = [call for call in self.fake.hf_calls if call["method"] == "PUT"][1:]
        self.assertEqual([call["content_length"] for call in parts], [str(300 * KIB), str(300 * KIB), str(100 * KIB)])
        [completion] = self.fake.requests("POST", "/upload/huggingface/completeMultipart")
        self.assertEqual(completion["authorization"], "Bearer " + self.token)
        self.assertEqual(read(d, "/hf/two.bin"), other)
        self.fake.relay = False  # before v2.7.8 the completion URL was Hugging Face's own: no token there
        third = os.urandom(650 * KIB)
        self.put(d, "/hf/three.bin", third)
        self.assertEqual(self.fake.hf_calls[-1]["path"], "/api/complete_multipart")
        self.assertIsNone(self.fake.hf_calls[-1]["authorization"])
        before = len(self.fake.hf_calls)
        self.put(d, "/hf/again.bin", data)  # Hugging Face has these bytes already: nothing to send
        self.assertEqual(len(self.fake.hf_calls), before)
        self.assertEqual(read(d, "/hf/again.bin"), data)
        self.put(d, "/hf/small.txt", b"small")  # under 600 KiB here: through ImgBed
        self.assertEqual(self.uploads()[-1]["query"]["uploadChannel"], ["huggingface"])
        self.assertEqual(self.names(d, "/hf"), ["again.bin", "one.bin", "small.txt", "three.bin", "two.bin"])

    def test_replacing_a_file(self):
        """ImgBed never replaces a file: the upload gets "(1)", then takes the old one's place."""
        d = self.new_driver()
        self.put(d, "/o.txt", b"first version")
        self.fake.calls.clear()
        self.put(d, "/o.txt", b"second")
        self.assertEqual(self.fake.ids(), ["base/o.txt"])
        self.assertEqual(read(d, "/o.txt"), b"second")
        renames = self.fake.requests("POST", "/api/manage/rename/.*")
        self.assertEqual([call["path"] for call in renames], ["/api/manage/rename/base/o(1).txt"] * 2)
        self.assertEqual([call["json"] for call in renames], [{"newFileId": "base/o.txt"}] * 2)
        self.assertEqual([call["path"] for call in self.fake.requests("GET", "/api/manage/delete/.*")], ["/api/manage/delete/base/o.txt"])

    def test_a_replacement_that_goes_wrong_leaves_no_upload_behind(self):
        d = self.new_driver()
        # The listing doesn't show the old file yet (ImgBed updates its index after it answers): ImgBed stores the
        # upload as "n(1).txt", which proves "n.txt" is there, and it takes the old one's place all the same.
        self.put(d, "/n.txt", b"n")
        self.fake.records["base/n.txt"].metadata["Directory"] = "elsewhere/"
        self.put(d, "/n.txt", b"again")
        self.assertEqual(self.fake.ids(), ["base/n.txt"])
        self.assertEqual(read(d, "/n.txt"), b"again")
        # ImgBed can't delete the old file: the upload is deleted again, and the old file stays as it was.
        self.put(d, "/o.txt", b"old")
        self.fake.undeletable.add("base/o.txt")
        with self.assertRaises(DriverError):
            self.put(d, "/o.txt", b"new")
        self.assertEqual(self.fake.ids(), ["base/n.txt", "base/o.txt"])
        self.assertEqual(read(d, "/o.txt"), b"old")
        # ImgBed stores it in another folder: deleted again.
        with mock.patch.object(FakeImgBed, "_unique_id", lambda fake, args, name: "elsewhere/p.txt"):
            with self.assertRaises(DriverError) as caught:
                self.put(d, "/p.txt", b"p")
        self.assertIn("elsewhere/p.txt", caught.exception.message)
        self.assertEqual(self.fake.ids(), ["base/n.txt", "base/o.txt"])

    # -- names

    def test_names_imgbed_would_change(self):
        unsafe = set(NAME_UNSAFE.pattern[1:-1].replace("\\", "")) | {"\\"}  # the fake's sanitizeFileName
        self.assertEqual(set(FORBIDDEN), unsafe - {"/"})
        d = self.new_driver()
        self.put(d, "/f.txt", b"f")
        for change in (lambda: self.put(d, "/a..b.txt", b"x"), lambda: self.put(d, "/a b/c.txt", b"x"),
                       lambda: d.mkdir("/x..y"), lambda: d.rename("/f.txt", "\u3000f.txt"),
                       lambda: d.rename("/f.txt", "g(1).txt")):
            with self.assertRaises(InvalidPath):
                change()
        self.fake.put("base/old name/in.txt", b"made elsewhere")  # a name ImgBed's own routes made once
        self.assertEqual(self.names(d, "/old name"), ["in.txt"])
        self.assertEqual(read(d, "/old name/in.txt"), b"made elsewhere")
        for change in (lambda: self.put(d, "/old name/new.txt", b"x"), lambda: d.rename("/old name/in.txt", "x.txt")):
            with self.assertRaises(InvalidPath):  # the new id would be in "old_name"
                change()
        d.rename("/old name", "new")  # to a name ImgBed keeps: fine
        self.assertEqual(self.fake.ids(), ["base/f.txt", "base/new/in.txt"])

    # -- folders

    def test_folders(self):
        d = self.new_driver()
        self.assertFalse(d.has_folders)
        d.mkdir("/f")  # nothing to make: the folder shows once a file is in it
        self.assertEqual(self.uploads(), [])
        self.assertEqual(self.names(d, "/"), [])
        self.put(d, "/f/x.txt", b"x")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/f")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/f/x.txt")
        self.assertEqual(self.names(d, "/"), ["f"])

    def test_folders_change_file_by_file(self):
        d = self.new_driver()
        for path in ("/d/a.txt", "/d/s/b.txt", "/d/s/t/c.txt", "/d2/keep.txt"):
            self.put(d, path, path.encode())
        d.rename("/d", "e")
        d.move("/e", "/d2")
        self.assertEqual(self.fake.ids(), ["base/d2/e/a.txt", "base/d2/e/s/b.txt", "base/d2/e/s/t/c.txt", "base/d2/keep.txt"])
        self.assertEqual(read(d, "/d2/e/s/t/c.txt"), b"/d/s/t/c.txt")
        self.fake.put("base/d2/e/s/old.png", b"old", Channel="Telegram")  # ImgBed can't rename these
        with self.assertRaises(DriverError):
            d.rename("/d2/e", "f")
        self.assertEqual(self.names(d, "/d2"), ["e", "keep.txt"])  # nothing moved

    def test_a_folder_rename_that_stops_halfway(self):
        d = self.new_driver()
        for path in ("/d/a.txt", "/d/b.txt", "/d/c.txt"):
            self.put(d, path, b"x")
        rename = FakeImgBed._rename
        moved = []

        def refuse_the_second(fake, req, raw):
            moved.append(raw)
            return rename(fake, req, "gone" if len(moved) == 2 else raw)  # "gone": 404, File not found.

        with mock.patch.object(FakeImgBed, "_rename", refuse_the_second):
            with self.assertRaises(DriverError) as caught:
                d.rename("/d", "e")
        self.assertIn("/d", caught.exception.message)  # where the others still are
        self.assertEqual(len(self.names(d, "/e")), 1)
        self.assertEqual(len(self.names(d, "/d")), 2)

    def test_delete(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.put(d, "/d/x.txt", b"x")
        self.put(d, "/d/s/y.txt", b"y")
        self.put(d, "/d2/z.txt", b"z")
        d.delete("/a.txt")
        self.assertEqual(self.fake.requests("GET", "/api/manage/delete/.*")[-1]["query"], {})
        self.fake.undeletable.add("base/d/s/y.txt")
        with self.assertRaises(DriverError) as caught:
            d.delete("/d")
        self.assertIn("1", caught.exception.message)
        self.assertEqual(self.fake.requests("GET", "/api/manage/delete/.*")[-1]["query"], {"folder": ["true"]})
        self.assertEqual(self.fake.ids(), ["base/d/s/y.txt", "base/d2/z.txt"])
        with self.assertRaises(NotFound):
            d.delete("/nope")
        with self.assertRaises(DriverError):
            d.delete("/")

    def test_folder_deletes_and_names_imgbed_reads_otherwise(self):
        """ImgBed's folder delete lists each folder through a URL with the id pasted in (delete/[[path]].js:46): "#" ends
        the query, "&" the value, "+" is a space. A folder with such a name in it (ImgBed before 2.6 kept them) is
        deleted file by file; others with the one folder delete."""
        d = self.new_driver()
        for file_id in ("base/R&D/a", "base/R/b", "base/photos/a&b/x", "base/photos/y", "base/plain/s/t.txt",
                        "base/plain/u.txt"):
            self.fake.put(file_id, b"x")
        d.delete("/R&D")  # ImgBed itself would list and delete "base/R/"
        self.assertEqual(self.fake.ids(), ["base/R/b", "base/photos/a&b/x", "base/photos/y", "base/plain/s/t.txt",
                                           "base/plain/u.txt"])
        d.delete("/photos")  # ImgBed itself would miss "a&b", listed as "base/photos/a/"
        self.assertEqual(self.fake.ids(), ["base/R/b", "base/plain/s/t.txt", "base/plain/u.txt"])
        self.fake.put("base/plain/a,b/z.txt", b"z")  # "," is no trouble there: ImgBed deletes the files it lists by id
        before = len(self.fake.requests("GET", "/api/manage/delete/.*"))
        d.delete("/plain")
        self.assertEqual([call["query"] for call in self.fake.requests("GET", "/api/manage/delete/.*")[before:]],
                         [{"folder": ["true"]}])
        self.assertEqual(self.fake.ids(), ["base/R/b"])
        self.fake.put("base/Q#/c", b"c")
        self.fake.put("base/Q#/d", b"d")
        self.fake.undeletable.add("base/Q#/c")
        with self.assertRaises(DriverError) as caught:
            d.delete("/Q#")
        self.assertIn("1", caught.exception.message)
        self.assertEqual(self.fake.ids(), ["base/Q#/c", "base/R/b"])

    def test_a_folder_with_two_dots_in_its_name(self):
        """ImgBed's listing makes "_" of ".." in the folder asked for (list.js:57), so a folder made so before ImgBed
        2.6 can't be listed: refused with a message, rather than shown empty or renamed without a file moved."""
        d = self.new_driver()
        self.fake.put("base/v1..v2/notes.txt", b"made by ImgBed before 2.6")
        self.assertEqual(self.names(d, "/"), ["v1..v2"])
        for change in (lambda: d.list("/v1..v2"), lambda: read(d, "/v1..v2/notes.txt"), lambda: d.rename("/v1..v2", "x"),
                       lambda: d.delete("/v1..v2")):
            with self.assertRaises(DriverError) as caught:
                change()
            self.assertNotIsInstance(caught.exception, (NotFound, BackendError))
        self.assertEqual(self.fake.ids(), ["base/v1..v2/notes.txt"])

    def test_a_folder_rename_that_finds_no_file(self):
        """The index lists a folder whose files aren't below its id (as when it lags behind a change): an error, not a
        rename that moved nothing."""
        d = self.new_driver()
        self.fake.put("old/d.txt", b"x", Directory="base/d/")
        self.assertEqual(self.names(d, "/"), ["d"])
        with self.assertRaises(DriverError):
            d.rename("/d", "e")
        self.assertEqual(self.fake.ids(), ["old/d.txt"])

    # -- downloads

    def test_downloads(self):
        d = self.new_driver()
        data = bytes(range(256)) * 20
        self.put(d, "/dl/文件.bin", data)
        before = len(self.fake.calls)
        url = d.download_url("/dl/文件.bin", filename="文件.bin")
        self.assertEqual(url, f"{self.fake.url}/file/base/dl/{urllib.parse.quote('文件.bin')}")
        self.assertEqual(len(self.fake.calls), before)  # built here
        self.assertTrue(d.download_url("/hfile-csp-probe", filename="probe").startswith(self.fake.url + "/file/"))
        self.assertEqual(read(d, "/dl/文件.bin"), data)
        self.assertEqual(read(d, "/dl/文件.bin", 10, 20), data[10:30])
        self.assertEqual(read(d, "/dl/文件.bin", 5000), data[5000:])
        calls = self.fake.requests("GET", "/file/.*")
        stamp = self.fake.records["base/dl/文件.bin"].metadata["TimeStamp"]
        self.assertEqual([call["range"] for call in calls], [None, "bytes=10-29", "bytes=5000-"])
        self.assertTrue(all(call["query"] == {"v": [str(stamp)]} for call in calls))
        self.put(d, "/e", b"")
        self.assertEqual(read(d, "/e"), b"")  # nothing to fetch
        self.assertEqual(len(self.fake.requests("GET", "/file/.*")), 3)
        del self.fake.records["base/dl/文件.bin"]
        with self.assertRaises(NotFound):
            read(d, "/dl/文件.bin")

    def test_files_imgbed_wont_serve(self):
        d = self.new_driver()
        self.fake.put("base/blocked.png", b"b", ListType="Block")
        self.fake.put("base/adult.png", b"a", Label="adult")
        self.fake.put("base/white.png", b"w", ListType="White", Label="adult")
        for picture in (True, False):  # ImgBed's picture for them, or without one a redirect to its page
            self.fake.block_pictures = picture
            for path in ("/blocked.png", "/adult.png"):
                with self.subTest(path=path, picture=picture), self.assertRaises(DriverError) as caught:
                    read(d, path)
                self.assertNotIsInstance(caught.exception, (NotFound, BackendError))
                self.assertNotIn("403", caught.exception.message)  # told apart from another refusal
            self.assertEqual(read(d, "/white.png"), b"w")
        self.fake.whitelist_mode = True
        self.fake.put("base/plain.png", b"p")
        with self.assertRaises(DriverError):
            read(d, "/plain.png")
        self.assertEqual(read(d, "/white.png"), b"w")
        self.fake.challenge = True  # Cloudflare's bot check in front of ImgBed: a page, not ImgBed's picture
        with self.assertRaises(DriverError) as caught:
            read(d, "/white.png")
        self.assertIn("403", caught.exception.message)

    def test_a_file_of_the_external_channel(self):
        """ImgBed keeps a link and redirects to it: followed, without the token."""
        d = self.new_driver()
        self.fake.external["pic.png"] = b"elsewhere"
        self.fake.put("base/pic.png", b"", Channel="External", ExternalLink=f"{self.fake.url}/external/pic.png", FileSizeBytes=9)
        self.assertEqual(read(d, "/pic.png"), b"elsewhere")
        [call] = self.fake.requests("GET", "/external/.*")
        self.assertIsNone(call["authorization"])

    def test_a_link_of_the_external_channel_as_imgbed_keeps_it(self):
        """ImgBed's upload page sends an empty file with the link (new File([], ...)), so the record's FileSizeBytes is
        0: the size is unknown, and the bytes are fetched."""
        d = self.new_driver()
        self.fake.external["cat.png"] = b"the picture, elsewhere"
        self.fake.put("base/cat.png", b"", Channel="External", ChannelName="External", FileSizeBytes=0, FileSize="0.00",
                      ExternalLink=f"{self.fake.url}/external/cat.png")
        self.assertIsNone(d.stat("/cat.png").size)
        self.assertEqual(read(d, "/cat.png"), b"the picture, elsewhere")

    def test_a_replaced_file_gets_a_new_link(self):
        """ImgBed lets browsers keep a file for 30 days (Cache-Control: public, max-age=2592000): the link carries the
        TimeStamp of the file listed, which a replacement changes."""
        d = self.new_driver()
        self.put(d, "/menu.png", b"first")
        d.list("/")
        first = d.download_url("/menu.png", filename="menu.png")
        self.put(d, "/menu.png", b"second")
        d.list("/")
        second = d.download_url("/menu.png", filename="menu.png")
        self.assertNotEqual(first, second)
        stamp = self.fake.records["base/menu.png"].metadata["TimeStamp"]
        self.assertEqual(second, f"{self.fake.url}/file/base/menu.png?v={stamp}")
        self.assertEqual(httpx.get(second).content, b"second")

    def test_a_ranged_read_after_stat_lists_once(self):
        """The site reads a range right after stat() and hands its Entry to open_known(): no second listing."""
        d = self.new_driver()
        data = bytes(range(256)) * 4
        self.put(d, "/r.bin", data)
        before = len(self.fake.requests("GET", "/api/manage/list"))
        entry = d.stat("/r.bin")
        stream = d.open_known("/r.bin", entry, 10, 20)
        self.assertEqual(stream.read(), data[10:30])
        self.assertEqual(len(self.fake.requests("GET", "/api/manage/list")) - before, 1)
        stamp = self.fake.records["base/r.bin"].metadata["TimeStamp"]
        self.assertEqual(self.fake.requests("GET", "/file/.*")[-1]["query"], {"v": [str(stamp)]})

    # -- errors

    def test_errors(self):
        def failure(status, body=None, permission="list", **kwargs):
            response = httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="Error: text", **kwargs)
            return ImgBedDriver._failure(response, permission)

        self.assertIsInstance(failure(401, permission="list"), AuthError)
        self.assertIn("upload", failure(401, permission="upload").message)
        self.assertIn("manage", failure(401, permission="manage").message)
        self.assertIsInstance(failure(404, {"success": False, "message": "File not found."}), NotFound)
        self.assertIsInstance(failure(409, {"success": False, "message": "taken"}), AlreadyExists)
        self.assertIn("413", failure(413).message)
        refused = failure(400, {"success": False, "error": "Delete file failed"})
        self.assertIs(type(refused), DriverError)
        self.assertIn("Delete file failed", refused.message)
        self.assertIn("Error: text", failure(400).message)
        for status in (429, 500, 502):
            self.assertIsInstance(failure(status, {"error": "Internal server error", "message": "m"}), BackendError)
        format_refused = failure(400, {"code": "UPLOAD_FORMAT_NOT_ALLOWED", "error": "This file format is not allowed for upload"})
        self.assertIn("This file format is not allowed for upload", format_refused.message)
        with self.assertRaises(BackendError) as caught:  # the web UI's page: the address has a path too many
            self.new_driver(url=self.fake.url + "/admin").list("/")
        self.assertNotIn("404", caught.exception.message)
        with self.assertRaises(BackendError) as caught:
            self.new_driver(url=self.fake.url + "/api").list("/")
        self.assertIn("404", caught.exception.message)
        self.fake.body_limit = 100 * KIB  # Cloudflare's limit, 100 MB, in small
        with self.assertRaises(DriverError) as caught:
            self.put(self.new_driver(channel="webdav"), "/big.bin", os.urandom(200 * KIB))
        self.assertIn("413", caught.exception.message)

    def test_a_redirect(self):
        d = self.new_driver()
        response = httpx.Response(302, headers={"Location": "https://login.example.com/"})
        with mock.patch.object(d._http, "send", return_value=response):
            with self.assertRaises(BackendError):
                d.list("/")

    # -- settings

    def test_settings(self):
        check = ImgBedDriver.validate_config
        good = {"url": "https://img.example.com", "token": "imgbed_abc", "channel": "telegram"}
        self.assertEqual(check(good), {})
        self.assertIn("url", check({**good, "url": "img.example.com"}))
        self.assertIn("url", check({**good, "url": "ftp://img.example.com"}))
        self.assertIn("token", check({**good, "token": "imgbed_ab\u200bc"}))  # pasted with a zero-width space
        self.assertIn("base_path", check({**good, "base_path": "/a/../b"}))
        self.assertIn("base_path", check({**good, "base_path": "/my pictures"}))
        self.assertEqual(check({**good, "base_path": "/图片/2026"}), {})
        d = ImgBedDriver({"url": " https://img.example.com/ ", "token": " imgbed_abc ", "channel_name": " main "})
        self.addCleanup(d.close)
        self.assertEqual((d._root, d._token, d._channel, d._channel_name), ("https://img.example.com", "imgbed_abc", "telegram", "main"))

    def test_the_sizes_of_chunks(self):
        """As ImgBed's upload page sends them, within what its channels take [docs: upload.md]: R2 and S3 parts of at
        least 5 MiB but the last, Telegram's under the 20 MB its bot API downloads, Discord's under its 10 MB."""
        mib = 1024 * 1024
        self.assertEqual((ImgBedDriver.chunk_above, ImgBedDriver.chunk_size), (20 * mib, 16 * mib))
        self.assertEqual(ImgBedDriver.discord_chunk, 9 * mib)
        self.assertEqual(ImgBedDriver.direct_from, 20 * mib)
        self.assertTrue(5 * mib <= ImgBedDriver.chunk_size < 20 * 1000 * 1000)
        self.assertLess(ImgBedDriver.discord_chunk, 10 * 1000 * 1000)

    def test_params_follow_the_forms_rules(self):
        """apps.storages.tests.test_config's checks, for this driver before it is registered."""
        for param in ImgBedDriver.params:
            with self.subTest(param=param.key):
                self.assertIn(param.kind, ("text", "password", "textarea", "number", "bool", "select", "path"))
                if param.kind == "select":
                    self.assertTrue(param.options)
                if param.secret:
                    self.assertIn(param.kind, ("password", "textarea", "number"))
                if param.default is not None and param.kind != "bool":
                    config.check_value(param, param.default)
        self.assertEqual(config.setup_keys(ImgBedDriver), ["url", "token", "channel"])
        self.assertEqual(ImgBedDriver.auth_params, ())
        self.assertEqual([param.key for param in ImgBedDriver.params if param.secret], ["token"])


class TheSitesOwnNetworkTests(FakeMixin, unittest.TestCase):
    """The client is http.client(guard=True): a read of /file/<id> and of the link an External file redirects to may
    not lead into the site's own network when ImgBed is on the Internet. Every fake is on 127.0.0.1, so http.public is
    patched to count the ports of the servers named as the Internet; the rest are the site's own network."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.reached: list[str] = []
        cls.inside = FakeServer(lambda req: cls.reached.append(req.path) or Response(200, b'{"AccessKeyId": "inside-only"}'))
        cls.elsewhere = FakeServer(lambda req: Response(200, b"a picture on the Internet"))
        cls.inside.start()
        cls.elsewhere.start()

    @classmethod
    def tearDownClass(cls):
        cls.inside.close()
        cls.elsewhere.close()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.token = self.fake.add_token(*ALL)
        self.reached.clear()

    def on_the_internet(self, *servers):
        ports = {urllib.parse.urlsplit(server.url).port for server in servers}
        patcher = mock.patch.object(http, "public", lambda address, number: number in ports)
        patcher.start()
        self.addCleanup(patcher.stop)

    def link(self, name: str, target: str) -> None:
        """A file of the External channel, as ImgBed's upload page makes it."""
        self.fake.put(f"base/{name}", b"", Channel="External", ChannelName="External", FileSizeBytes=0, FileSize="0.00",
                      ExternalLink=target)

    def test_an_imgbed_on_the_internet_cant_send_the_site_into_its_network(self):
        self.on_the_internet(self.fake.server)
        d = self.new_driver()
        self.link("cat.png", self.inside.url + "/latest/meta-data/iam/security-credentials/role")
        with self.assertRaises(http.InsideNetwork):
            d.open("/cat.png")
        self.assertEqual(self.reached, [])  # nothing went there

    def test_an_imgbed_in_the_sites_network_is_trusted_with_its_links(self):
        d = self.new_driver()  # nothing patched: every server is in the site's own network
        self.link("lan.bin", self.inside.url + "/lan.bin")
        self.assertEqual(read(d, "/lan.bin"), b'{"AccessKeyId": "inside-only"}')

    def test_an_imgbed_on_the_internet_with_a_link_on_the_internet(self):
        self.on_the_internet(self.fake.server, self.elsewhere)
        d = self.new_driver()
        self.link("cat.png", self.elsewhere.url + "/cat.png")
        self.assertEqual(read(d, "/cat.png"), b"a picture on the Internet")
        d.write("/new.txt", io.BytesIO(b"new"), 3)  # the API and uploads, guarded too, go to ImgBed's own address
        self.assertEqual((self.names(d, "/"), read(d, "/new.txt")), (["cat.png", "new.txt"], b"new"))

    def names(self, driver, path: str) -> list[str]:
        return sorted(entry.name for entry in driver.list(path))


if __name__ == "__main__":
    unittest.main()
