"""The behaviour every storage driver must have, as tests.

A driver's test module mixes DriverContract into a unittest.TestCase and implements
make_driver(), which returns a driver whose root ("/") is an empty folder of a test backend
(a temporary folder, a local FTP server, moto's S3, a fake Graph API, ...):

    class LocalContractTests(DriverContract, unittest.TestCase):
        def make_driver(self):
            return LocalDriver({"root": self.enterContext(tempfile.TemporaryDirectory())})

Tests that need something the driver doesn't have (can_range, can_move, can_copy,
can_direct_download, has_folders) are skipped from its class attributes. A backend that can't
rename folders (Bunny) sets renames_folders = False in its tests. No Django here.
"""

from __future__ import annotations

import datetime
import io
import os
import unittest

from apps.storages.drivers import paths
from apps.storages.drivers.base import (
    AlreadyExists,
    DriverError,
    NotADirectory,
    NotFound,
    StorageDriver,
    Unsupported,
)

# Test servers (FTP, SFTP, WebDAV) keep their files on this machine's disk. On Windows it can't have what a
# Linux server's has: names with \ : * ? " < > | or ending in a dot or a space, links made as Linux makes
# them, modes, names that aren't UTF-8. Tests of those need a Linux one.
_WINDOWS_REFUSES = '\\:*?"<>|'
linux_server = unittest.skipIf(os.name == "nt", "the test server keeps its files on this disk, which isn't Linux's")


def on_this_disk(name: str) -> bool:
    """Whether this machine's disk can have a file named `name`, for test servers that keep theirs there. On
    Windows a long one (the contract's 200 characters) makes their paths longer than the 260 characters its
    file functions take, unless long paths are turned on."""
    return os.name != "nt" or not (any(ch in _WINDOWS_REFUSES for ch in name) or name.endswith((".", " ")) or len(name) > 100)


# Names the drivers must store and list exactly, unless they contain the driver's
# forbidden_chars (or the test server can't have them: server_on_this_disk).
UNUSUAL_NAMES = [
    "中文 名字.txt",
    "a b",
    "100%.txt",
    "a+b=c&d.txt",
    "#hash",
    "q?x=1",
    "emoji 😀.txt",
    "[x](y)",
    "semi;colon,comma",
    "quote'single",
    'quote"double',
    "～全角～",
    "café",
    "tab-like　full-width space",
    "~tilde",
    "a" * 200 + ".txt",
]


class DriverContract:
    #: Seconds the backend's clock may be off from ours for `modified`.
    clock_skew = 24 * 3600
    #: False: the driver can't rename a folder (Bunny has no rename): it raises Unsupported, and nothing changes.
    renames_folders = True

    def make_driver(self) -> StorageDriver:  # pragma: no cover - abstract
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.d = self.make_driver()
        self.addCleanup(self.d.close)

    # -- helpers

    def put(self, path: str, data: bytes) -> None:
        self.d.write(path, io.BytesIO(data), len(data))

    def get(self, path: str, offset: int = 0, length: int | None = None) -> bytes:
        stream = self.d.open(path, offset, length)
        try:
            return b"".join(stream)
        finally:
            stream.close()

    def names(self, path: str) -> list[str]:
        return sorted(entry.name for entry in self.d.list(path))

    def assertFile(self, path: str, data: bytes) -> None:
        entry = self.d.stat(path)
        self.assertFalse(entry.is_dir, path)
        self.assertEqual(entry.size, len(data), path)
        self.assertEqual(self.get(path), data, path)

    def assertMissing(self, path: str) -> None:
        with self.assertRaises(NotFound, msg=path):
            self.d.stat(path)

    def skip_unless(self, flag: str) -> None:
        if not getattr(self.d, flag):
            self.skipTest(f"{type(self.d).__name__}.{flag} is False")

    #: The test server keeps its files on this machine's disk (FTP, SFTP, WebDAV): names it can't have are left out.
    server_on_this_disk = False

    def allowed(self, name: str) -> bool:
        return not any(ch in self.d.forbidden_chars for ch in name) and (not self.server_on_this_disk or on_this_disk(name))

    # -- the root and listing

    def test_root_is_an_empty_folder(self):
        root = self.d.stat("/")
        self.assertTrue(root.is_dir)
        self.assertEqual(root.path, "/")
        self.assertEqual(self.d.list("/"), [])
        self.d.check()

    def test_list_gives_entries(self):
        self.put("/d/one.txt", b"1")
        self.put("/d/sub/two.txt", b"22")
        entries = {entry.name: entry for entry in self.d.list("/d")}
        self.assertEqual(sorted(entries), ["one.txt", "sub"])
        one, sub = entries["one.txt"], entries["sub"]
        self.assertEqual((one.path, one.is_dir, one.size), ("/d/one.txt", False, 1))
        self.assertEqual((sub.path, sub.is_dir), ("/d/sub", True))
        for entry in entries.values():
            if entry.modified is not None:
                self.assertIsNotNone(entry.modified.tzinfo, entry)

    def test_missing_paths(self):
        self.put("/here.txt", b"x")
        for call in (
            lambda: self.d.stat("/nope"),
            lambda: self.d.stat("/nope/deeper"),
            lambda: self.d.list("/nope"),
            lambda: self.get("/nope.txt"),
            lambda: self.d.delete("/nope"),
            lambda: self.d.rename("/nope", "other"),
        ):
            with self.assertRaises(NotFound):
                call()

    def test_list_a_file(self):
        self.put("/f.txt", b"x")
        with self.assertRaises(NotADirectory):
            self.d.list("/f.txt")

    def test_open_a_folder(self):
        self.d.mkdir("/folder") if self.d.has_folders else self.put("/folder/x", b"x")
        with self.assertRaises(NotFound):
            self.get("/folder")

    # -- reading and writing

    def test_write_then_read(self):
        data = os.urandom(1000)
        self.put("/a.bin", data)
        self.assertFile("/a.bin", data)
        entry = self.d.stat("/a.bin")
        self.assertEqual((entry.name, entry.path), ("a.bin", "/a.bin"))
        if entry.modified is not None:
            self.assertIsNotNone(entry.modified.tzinfo)
            now = datetime.datetime.now(datetime.timezone.utc)
            self.assertLess(abs((now - entry.modified).total_seconds()), self.clock_skew)
        self.assertEqual(self.names("/"), ["a.bin"])

    def test_empty_file(self):
        self.put("/empty", b"")
        self.assertFile("/empty", b"")

    def test_large_file(self):
        data = os.urandom(3 * 1024 * 1024 + 17)
        self.put("/big.bin", data)
        self.assertFile("/big.bin", data)

    def test_range(self):
        self.skip_unless("can_range")
        data = os.urandom(10000)
        self.put("/r.bin", data)
        stream = self.d.open("/r.bin", 100, 50)
        try:
            self.assertEqual(b"".join(stream), data[100:150])
            self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, 10000))
        finally:
            stream.close()
        self.assertEqual(self.get("/r.bin", 9990), data[9990:])
        self.assertEqual(self.get("/r.bin", 0, 1), data[:1])

    def test_overwrite(self):
        self.put("/o.txt", b"first version")
        self.put("/o.txt", b"second")
        self.assertFile("/o.txt", b"second")
        self.assertEqual(self.names("/"), ["o.txt"])

    def test_write_creates_folders(self):
        self.put("/x/y/z.txt", b"deep")
        self.assertEqual(self.names("/"), ["x"])
        self.assertEqual(self.names("/x"), ["y"])
        self.assertTrue(self.d.stat("/x/y").is_dir)
        self.assertFile("/x/y/z.txt", b"deep")

    def test_write_onto_a_folder(self):
        self.put("/taken/inside.txt", b"x")
        with self.assertRaises(AlreadyExists):
            self.put("/taken", b"y")
        self.assertFile("/taken/inside.txt", b"x")

    def test_close_stream_early(self):
        data = os.urandom(2 * 1024 * 1024)
        self.put("/early.bin", data)
        stream = self.d.open("/early.bin")
        next(iter(stream))
        stream.close()  # the client went away; must not hang or raise
        self.assertFile("/early.bin", data)

    # -- folders

    def test_mkdir(self):
        self.skip_unless("has_folders")
        self.d.mkdir("/f")
        self.assertTrue(self.d.stat("/f").is_dir)
        self.assertEqual(self.names("/"), ["f"])
        self.assertEqual(self.d.list("/f"), [])

    def test_mkdir_creates_parents(self):
        self.skip_unless("has_folders")
        self.d.mkdir("/p/q/r")
        self.assertTrue(self.d.stat("/p/q/r").is_dir)
        self.assertEqual(self.names("/p"), ["q"])

    def test_mkdir_existing(self):
        self.skip_unless("has_folders")
        self.d.mkdir("/f")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/f")
        self.put("/g", b"file")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/g")
        self.assertFile("/g", b"file")

    # -- rename, move, copy, delete

    def test_rename_file(self):
        self.put("/a.txt", b"aaa")
        self.d.rename("/a.txt", "b.txt")
        self.assertEqual(self.names("/"), ["b.txt"])
        self.assertFile("/b.txt", b"aaa")

    def test_rename_folder(self):
        self.put("/d/x.txt", b"x")
        self.put("/d/s/y.txt", b"y")
        if not self.renames_folders:
            with self.assertRaises(Unsupported):
                self.d.rename("/d", "e")
            self.assertMissing("/e")
            self.assertFile("/d/x.txt", b"x")
            self.assertFile("/d/s/y.txt", b"y")
            return
        self.d.rename("/d", "e")
        self.assertMissing("/d")
        self.assertFile("/e/x.txt", b"x")
        self.assertFile("/e/s/y.txt", b"y")

    def test_rename_to_a_taken_name(self):
        self.put("/a", b"a")
        self.put("/b", b"b")
        with self.assertRaises(AlreadyExists):
            self.d.rename("/a", "b")
        self.assertFile("/a", b"a")
        self.assertFile("/b", b"b")

    def test_rename_changing_only_the_case(self):
        # Backends that ignore case take "Case.txt" for "case.txt" itself; those that don't have two names.
        self.put("/case.txt", b"c")
        self.put("/dir/x.txt", b"x")
        self.d.rename("/case.txt", "Case.txt")
        if self.renames_folders:
            self.d.rename("/dir", "DIR")
            self.assertEqual(self.names("/"), ["Case.txt", "DIR"])
            self.assertFile("/DIR/x.txt", b"x")
        else:
            with self.assertRaises(Unsupported):
                self.d.rename("/dir", "DIR")
            self.assertEqual(self.names("/"), ["Case.txt", "dir"])
        self.assertFile("/Case.txt", b"c")
        self.put("/case.txt", b"other")
        if len(self.names("/")) == 2:
            return  # the backend ignores case: that was the same file again
        with self.assertRaises(AlreadyExists):  # another entry, which a rename mustn't replace
            self.d.rename("/case.txt", "Case.txt")
        self.assertFile("/case.txt", b"other")
        self.assertFile("/Case.txt", b"c")

    def test_move_file(self):
        self.skip_unless("can_move")
        self.put("/a.txt", b"moved")
        self.put("/dst/keep.txt", b"k")
        self.d.move("/a.txt", "/dst")
        self.assertMissing("/a.txt")
        self.assertFile("/dst/a.txt", b"moved")

    def test_move_folder(self):
        self.skip_unless("can_move")
        self.put("/src/s/x.txt", b"x")
        self.put("/dst/keep.txt", b"k")
        self.d.move("/src/s", "/dst")
        self.assertMissing("/src/s")
        self.assertFile("/dst/s/x.txt", b"x")

    def test_move_errors(self):
        self.skip_unless("can_move")
        self.put("/a.txt", b"a")
        self.put("/dst/a.txt", b"other")
        with self.assertRaises(AlreadyExists):
            self.d.move("/a.txt", "/dst")
        with self.assertRaises(NotFound):
            self.d.move("/a.txt", "/no-such-folder")
        with self.assertRaises(NotFound):
            self.d.move("/no-such-file", "/dst")
        self.put("/m/n/x", b"x")
        with self.assertRaises(DriverError):
            self.d.move("/m", "/m/n")
        self.assertFile("/a.txt", b"a")
        self.assertFile("/dst/a.txt", b"other")
        self.assertFile("/m/n/x", b"x")

    def test_copy_file(self):
        self.skip_unless("can_copy")
        self.put("/a.txt", b"copied")
        self.put("/dst/keep.txt", b"k")
        self.d.copy("/a.txt", "/dst")
        self.assertFile("/a.txt", b"copied")
        self.assertFile("/dst/a.txt", b"copied")

    def test_copy_folder(self):
        self.skip_unless("can_copy")
        self.put("/src/s/x.txt", b"x")
        self.put("/src/s/t/y.txt", b"y")
        self.put("/dst/keep.txt", b"k")
        self.d.copy("/src/s", "/dst")
        self.assertFile("/src/s/x.txt", b"x")
        self.assertFile("/dst/s/x.txt", b"x")
        self.assertFile("/dst/s/t/y.txt", b"y")

    def test_copy_errors(self):
        self.skip_unless("can_copy")
        self.put("/a.txt", b"a")
        self.put("/dst/a.txt", b"other")
        with self.assertRaises(AlreadyExists):
            self.d.copy("/a.txt", "/dst")
        with self.assertRaises(NotFound):
            self.d.copy("/a.txt", "/no-such-folder")
        self.put("/m/n/x", b"x")
        with self.assertRaises(DriverError):
            self.d.copy("/m", "/m/n")
        self.assertFile("/dst/a.txt", b"other")

    def test_delete_file(self):
        self.put("/a.txt", b"a")
        self.put("/b.txt", b"b")
        self.d.delete("/a.txt")
        self.assertMissing("/a.txt")
        self.assertEqual(self.names("/"), ["b.txt"])

    def test_delete_folder_with_everything_in_it(self):
        self.put("/d/a", b"a")
        self.put("/d/s/b", b"b")
        self.put("/d2/c", b"c")  # a sibling whose name starts the same way
        self.d.delete("/d")
        self.assertMissing("/d")
        self.assertMissing("/d/s/b")
        self.assertEqual(self.names("/"), ["d2"])
        self.assertFile("/d2/c", b"c")

    def test_prefix_siblings_stay_apart(self):
        # Object stores keep "a/b" and "a b/c" as keys with a common prefix; listing "/a" must not
        # show the other one, and renaming "/a" (deleting it, where folders can't be renamed) must not take it along.
        self.put("/a/in.txt", b"1")
        self.put("/a b/out.txt", b"2")
        self.put("/ab.txt", b"3")
        self.assertEqual(self.names("/a"), ["in.txt"])
        if self.renames_folders:
            self.d.rename("/a", "z")
            self.assertEqual(self.names("/"), ["a b", "ab.txt", "z"])
        else:
            self.d.delete("/a")
            self.assertEqual(self.names("/"), ["a b", "ab.txt"])
        self.assertFile("/a b/out.txt", b"2")

    # -- names

    def test_unusual_names(self):
        for name in UNUSUAL_NAMES:
            if not self.allowed(name) or len(name.encode()) > self.d.max_name_bytes:
                continue
            with self.subTest(name=name):
                path = paths.join("/", name)
                data = name.encode("utf-8")
                self.put(path, data)
                self.assertIn(name, self.names("/"))
                self.assertFile(path, data)
                self.assertEqual(self.d.stat(path).name, name)
                self.put(paths.join("/", "dir " + name, "inner.txt"), b"in")
                self.assertEqual(self.names(paths.join("/", "dir " + name)), ["inner.txt"])
                self.d.delete(path)
                self.d.delete(paths.join("/", "dir " + name))
        self.assertEqual(self.d.list("/"), [])

    # -- direct links

    #: The User-Agent test_download_url says the browser has (115's links only work for it).
    browser_user_agent = "Mozilla/5.0 (HFile contract test)"

    def fetch_url(self, url: str, user_agent: str):
        """GET a direct link as the browser would. Override it when the test backend needs another
        route to the link's host (virtual-hosted buckets on a local server)."""
        import httpx

        return httpx.get(url, headers={"User-Agent": user_agent}, follow_redirects=True, timeout=30)

    def test_download_url(self):
        self.skip_unless("can_direct_download")
        data = os.urandom(5000)
        self.put("/dl/文件 1.bin", data)
        url = self.d.download_url("/dl/文件 1.bin", filename="文件 1.bin", expires=600,
                                  user_agent=self.browser_user_agent)
        self.assertIsInstance(url, str)
        response = self.fetch_url(url, self.browser_user_agent)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.content, data)


# The files ReadOnlyContract expects in the backend: path -> bytes (folders are the ones above them, and EMPTY_FOLDER).
READ_ONLY_TREE = {
    "/a.txt": b"hello",
    "/d/b.bin": bytes(range(256)) * 40,
    "/d/sub/c.txt": b"c",
    "/d/中文 名字.txt": "中文".encode(),
    "/d/a b/100%.txt": b"%",
}
EMPTY_FOLDER = "/empty"


class ReadOnlyContract:
    """The behaviour of a driver whose backend can only be read (can_write False: a share link, a release
    page), as tests. make_driver() returns a driver whose root holds READ_ONLY_TREE and the empty folder
    EMPTY_FOLDER (when the backend has folders), put there by the test backend itself."""

    clock_skew = DriverContract.clock_skew

    def make_driver(self) -> StorageDriver:  # pragma: no cover - abstract
        raise NotImplementedError

    def setUp(self):
        super().setUp()
        self.d = self.make_driver()
        self.addCleanup(self.d.close)

    get = DriverContract.get
    names = DriverContract.names
    assertFile = DriverContract.assertFile
    skip_unless = DriverContract.skip_unless

    def test_it_says_it_is_read_only(self):
        self.assertFalse(type(self.d).can_write)
        self.assertFalse(self.d.can_direct_upload)

    def test_root_and_listing(self):
        root = self.d.stat("/")
        self.assertTrue(root.is_dir)
        self.assertEqual(root.path, "/")
        self.d.check()
        expected = ["a.txt", "d"] + ([EMPTY_FOLDER[1:]] if self.d.has_folders else [])
        self.assertEqual(self.names("/"), sorted(expected))
        entries = {entry.name: entry for entry in self.d.list("/d")}
        self.assertEqual(sorted(entries), sorted(["a b", "b.bin", "sub", "中文 名字.txt"]))
        self.assertEqual((entries["b.bin"].path, entries["b.bin"].is_dir, entries["b.bin"].size),
                         ("/d/b.bin", False, len(READ_ONLY_TREE["/d/b.bin"])))
        self.assertEqual((entries["sub"].path, entries["sub"].is_dir), ("/d/sub", True))
        for entry in entries.values():
            if entry.modified is not None:
                self.assertIsNotNone(entry.modified.tzinfo, entry)
        if self.d.has_folders:
            self.assertEqual(self.d.list(EMPTY_FOLDER), [])

    def test_files(self):
        for path, data in READ_ONLY_TREE.items():
            with self.subTest(path=path):
                self.assertFile(path, data)
                entry = self.d.stat(path)
                self.assertEqual((entry.name, entry.path), (paths.name(path), path))

    def test_missing_paths(self):
        for call in (
            lambda: self.d.stat("/nope"),
            lambda: self.d.stat("/a.txt/deeper"),
            lambda: self.d.stat("/nope/deeper"),
            lambda: self.d.list("/nope"),
            lambda: self.get("/nope.txt"),
        ):
            with self.assertRaises(NotFound):
                call()

    def test_list_a_file(self):
        with self.assertRaises(NotADirectory):
            self.d.list("/a.txt")

    def test_open_a_folder(self):
        with self.assertRaises(NotFound):
            self.get("/d")

    def test_range(self):
        self.skip_unless("can_range")
        data = READ_ONLY_TREE["/d/b.bin"]
        stream = self.d.open("/d/b.bin", 100, 50)
        try:
            self.assertEqual(b"".join(stream), data[100:150])
            self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, len(data)))
        finally:
            stream.close()
        self.assertEqual(self.get("/d/b.bin", len(data) - 10), data[-10:])
        self.assertEqual(self.get("/d/b.bin", 0, 1), data[:1])

    def test_close_stream_early(self):
        stream = self.d.open("/d/b.bin")
        next(iter(stream))
        stream.close()
        self.assertFile("/a.txt", READ_ONLY_TREE["/a.txt"])

    browser_user_agent = DriverContract.browser_user_agent
    fetch_url = DriverContract.fetch_url

    def test_download_url(self):
        self.skip_unless("can_direct_download")
        data = READ_ONLY_TREE["/d/中文 名字.txt"]
        url = self.d.download_url("/d/中文 名字.txt", filename="中文 名字.txt", expires=600,
                                  user_agent=self.browser_user_agent)
        self.assertIsInstance(url, str)
        response = self.fetch_url(url, self.browser_user_agent)
        self.assertEqual(response.status_code, 200, response.text[:500])
        self.assertEqual(response.content, data)
