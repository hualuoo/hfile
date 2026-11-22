import errno
import io
import os
import shutil
import socket
import stat
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from django.utils.translation import gettext

try:  # the limit on open files, which Windows doesn't have
    import resource
except ImportError:
    resource = None

from apps.core.testing import deny, symlink
from apps.storages.drivers import driver_class, local
from apps.storages.drivers.base import AlreadyExists, DriverError, InvalidPath, NotADirectory, NotFound
from apps.storages.drivers.local import TEMP_PREFIX, LocalDriver
from apps.storages.tests.contract import DriverContract


def make_root(test: unittest.TestCase) -> str:
    """An empty folder inside a temporary folder, so that tests can put things next to the root. Its real
    path: on Windows the temporary folder may be named with 8.3 short names (C:\\Users\\RUNNER~1\\…), and
    the targets of links made from it then too, which the driver finds nothing by (_nt.py)."""
    outer = os.path.realpath(test.enterContext(tempfile.TemporaryDirectory(prefix="hfile-local-")))
    root = os.path.join(outer, "root")
    os.mkdir(root)
    return root


class LocalContractTests(DriverContract, unittest.TestCase):
    def make_driver(self):
        return LocalDriver({"root": make_root(self)})


class LocalTestCase(unittest.TestCase):
    allow_external = False

    def setUp(self):
        self.root = make_root(self)
        self.outer = os.path.dirname(self.root)
        self.d = LocalDriver({"root": self.root, "allow_external_symlinks": self.allow_external})
        self.addCleanup(self.d.close)

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def get(self, path, offset=0, length=None):
        return self.d.open(path, offset, length).read()

    def names(self, path):
        return sorted(entry.name for entry in self.d.list(path))

    def real(self, *names):
        return os.path.join(self.root, *names)


class RegistryTests(unittest.TestCase):
    def test_registered(self):
        self.assertIs(driver_class("local"), LocalDriver)


class ParamTests(unittest.TestCase):
    def test_validate(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(LocalDriver.validate_config({"root": folder}), {})
            self.assertEqual(LocalDriver.validate_config({"root": folder + "/"}), {})
            file = os.path.join(folder, "f")
            open(file, "w").close()
            for bad in ["", None, "relative/path", "/", "//", "/a/\x00b", file, folder + "/missing"]:
                with self.subTest(root=bad):
                    self.assertIn("root", LocalDriver.validate_config({"root": bad}))

    def test_check(self):
        with tempfile.TemporaryDirectory() as folder:
            LocalDriver({"root": folder}).check()
            with self.assertRaises(DriverError):
                LocalDriver({"root": folder + "/missing"}).check()
            with self.assertRaises(DriverError):
                LocalDriver({"root": "relative"}).check()

    def test_root_gone(self):
        with tempfile.TemporaryDirectory() as folder:
            root = os.path.join(folder, "r")
            os.mkdir(root)
            d = LocalDriver({"root": root})
            os.rmdir(root)
            with self.assertRaises(DriverError) as caught:
                d.list("/")
            self.assertNotIsInstance(caught.exception, NotFound)
            self.assertNotIn(folder, str(caught.exception))  # no server paths in messages

    def test_secrets_and_params(self):
        keys = [p.key for p in LocalDriver.params]
        self.assertEqual(keys, ["root", "allow_external_symlinks"])
        self.assertFalse(any(p.secret for p in LocalDriver.params))
        self.assertFalse(LocalDriver.defaults()["allow_external_symlinks"])


class ConfinementTests(LocalTestCase):
    def test_bad_paths(self):
        for bad in ["..", "/..", "/a/../..", "/./a", "a", "/a/", "//a", "/a\x00"]:
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                self.d.stat(bad)
        with self.assertRaises(InvalidPath):
            self.d.rename("/x", "a/b")
        with self.assertRaises(InvalidPath):
            self.d.rename("/x", "..")

    def test_writes_stay_inside(self):
        self.put("/a/b.txt", b"x")
        self.assertTrue(os.path.isfile(self.real("a", "b.txt")))
        self.assertEqual(sorted(os.listdir(self.outer)), ["root"])

    def test_outside_links_are_hidden_and_refused(self):
        secret_dir = os.path.join(self.outer, "secret")
        os.mkdir(secret_dir)
        with open(os.path.join(secret_dir, "s.txt"), "wb") as f:
            f.write(b"secret")
        symlink(secret_dir, self.real("abs-dir"))
        symlink("../secret/s.txt", self.real("rel-file"))
        symlink("/etc/passwd", self.real("passwd"))
        os.mkdir(self.real("sub"))
        symlink("../../secret", self.real("sub", "up-dir"))
        symlink(self.root + "/../secret", self.real("tricky"))  # lexically under the root, then ".."
        self.put("/visible.txt", b"v")
        self.assertEqual(self.names("/"), ["sub", "visible.txt"])
        self.assertEqual(self.names("/sub"), [])
        for path in ["/abs-dir", "/abs-dir/s.txt", "/rel-file", "/passwd", "/sub/up-dir", "/sub/up-dir/s.txt", "/tricky"]:
            with self.subTest(path=path):
                with self.assertRaises(NotFound):
                    self.d.stat(path)
                with self.assertRaises(NotFound):
                    self.d.open(path)
                with self.assertRaises(NotFound):
                    self.d.list(path)
                with self.assertRaises(NotFound):
                    self.d.delete(path)
                with self.assertRaises(NotFound):
                    self.d.rename(path, "renamed")
                with self.assertRaises(NotFound):
                    self.d.copy(path, "/")
                with self.assertRaises(NotFound):
                    self.d.move(path, "/sub")
        with self.assertRaises(NotFound):
            self.put("/abs-dir/new.txt", b"x")
        with self.assertRaises(NotFound):
            self.d.mkdir("/abs-dir/new")
        with self.assertRaises(NotFound):
            self.put("/rel-file", b"overwrite")
        with self.assertRaises(NotFound):
            self.d.move("/visible.txt", "/abs-dir")
        self.assertEqual(sorted(os.listdir(secret_dir)), ["s.txt"])
        with open(os.path.join(secret_dir, "s.txt"), "rb") as f:
            self.assertEqual(f.read(), b"secret")
        self.assertTrue(os.path.islink(self.real("abs-dir")))  # still there, untouched

    def test_inside_links_are_followed(self):
        self.put("/real/file.txt", b"content")
        self.put("/real/deeper/x.txt", b"x")
        symlink("real", self.real("rel-dir"))
        symlink(self.real("real"), self.real("abs-dir"))
        symlink("real/file.txt", self.real("rel-file"))
        symlink(os.path.realpath(self.real("real", "file.txt")), self.real("realpath-file"))
        symlink("../../real/deeper/../file.txt", self.real("real", "deeper", "up-file"))
        entries = {entry.name: entry for entry in self.d.list("/")}
        self.assertTrue(entries["rel-dir"].is_dir)
        self.assertTrue(entries["abs-dir"].is_dir)
        self.assertEqual((entries["rel-file"].is_dir, entries["rel-file"].size), (False, 7))
        self.assertEqual(self.names("/rel-dir"), ["deeper", "file.txt"])
        self.assertEqual(self.names("/abs-dir/deeper"), ["up-file", "x.txt"])
        for path in ["/rel-file", "/realpath-file", "/rel-dir/file.txt", "/abs-dir/deeper/up-file"]:
            with self.subTest(path=path):
                self.assertEqual(self.get(path), b"content")
                self.assertEqual(self.d.stat(path).path, path)
        # Writing through a link writes its target; the link stays.
        self.put("/rel-file", b"new content")
        self.assertTrue(os.path.islink(self.real("rel-file")))
        self.assertEqual(self.get("/real/file.txt"), b"new content")
        self.put("/rel-dir/made.txt", b"m")
        self.assertTrue(os.path.isfile(self.real("real", "made.txt")))
        # Deleting a link deletes the link only.
        self.d.delete("/rel-dir")
        self.assertFalse(os.path.lexists(self.real("rel-dir")))
        self.assertEqual(self.get("/real/deeper/x.txt"), b"x")
        # Renaming a link renames the link.
        self.d.rename("/rel-file", "renamed-link")
        self.assertTrue(os.path.islink(self.real("renamed-link")))

    def _racing_stat(self, name_to_swap, replacement_target):
        """The driver's stat (os.stat; _nt.stat on Windows) that swaps `name_to_swap` (in the root) for a
        symlink right after looking at it: a realpath() check followed by open() would follow the new link."""
        real_stat = local._os.stat
        swapped = []

        def racing(path, *args, dir_fd=None, follow_symlinks=True):
            result = real_stat(path, *args, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            if path == name_to_swap and dir_fd is not None and not swapped:
                swapped.append(path)
                os.rename(self.real(name_to_swap), self.real(name_to_swap + "-old"))
                symlink(replacement_target, self.real(name_to_swap))
            return result

        return mock.patch.object(local._os, "stat", racing), swapped

    def test_folder_swapped_for_a_link_during_a_walk(self):
        secret_dir = os.path.join(self.outer, "secret")
        os.mkdir(secret_dir)
        with open(os.path.join(secret_dir, "s.txt"), "wb") as f:
            f.write(b"secret")
        self.put("/a/s.txt", b"inside")
        patch, swapped = self._racing_stat("a", secret_dir)
        with patch, self.assertRaises(NotFound):
            self.d.open("/a/s.txt")
        self.assertEqual(swapped, ["a"])

    def test_file_swapped_for_a_link_before_open(self):
        secret = os.path.join(self.outer, "secret.txt")
        with open(secret, "wb") as f:
            f.write(b"secret")
        self.put("/f.txt", b"inside")
        patch, swapped = self._racing_stat("f.txt", secret)
        with patch, self.assertRaises(NotFound):
            self.d.open("/f.txt")
        self.assertEqual(swapped, ["f.txt"])

    def test_root_is_a_link(self):
        real = os.path.join(self.outer, "real-root")
        os.mkdir(real)
        symlink(real, os.path.join(self.outer, "linked-root"))
        d = LocalDriver({"root": os.path.join(self.outer, "linked-root")})
        d.write("/a.txt", io.BytesIO(b"a"), 1)
        symlink(os.path.join(real, "a.txt"), os.path.join(real, "by-real-path"))
        symlink(os.path.join(self.outer, "linked-root", "a.txt"), os.path.join(real, "by-link-path"))
        self.assertEqual(sorted(e.name for e in d.list("/")), ["a.txt", "by-link-path", "by-real-path"])
        self.assertEqual(d.open("/by-real-path").read(), b"a")

    def test_dangling_and_looping_links(self):
        symlink("nowhere", self.real("dangling"))
        symlink("loop2", self.real("loop1"))
        symlink("loop1", self.real("loop2"))
        symlink(".", self.real("self"))
        self.assertEqual(self.names("/"), ["self"])
        self.assertEqual(self.names("/self/self/self"), ["self"])
        with self.assertRaises(NotFound):
            self.d.stat("/dangling")
        with self.assertRaises(DriverError):
            self.d.stat("/loop1")
        deep = "/" + "/".join(["self"] * 41)
        with self.assertRaises(DriverError):
            self.d.list(deep)

    def test_copy_follows_inside_links_only(self):
        secret = os.path.join(self.outer, "secret.txt")
        with open(secret, "wb") as f:
            f.write(b"secret")
        self.put("/src/a.txt", b"a")
        self.put("/target/t.txt", b"t")
        symlink("../target", self.real("src", "inside-dir"))
        symlink("a.txt", self.real("src", "inside-file"))
        symlink(secret, self.real("src", "outside-file"))
        symlink("..", self.real("src", "loop"))
        self.d.mkdir("/dst")
        self.d.copy("/src", "/dst")
        copied = self.real("dst", "src")
        self.assertEqual(sorted(os.listdir(copied)), ["a.txt", "inside-dir", "inside-file", "loop"])
        self.assertFalse(os.path.islink(os.path.join(copied, "inside-file")))
        self.assertEqual(self.get("/dst/src/inside-file"), b"a")
        self.assertEqual(self.get("/dst/src/inside-dir/t.txt"), b"t")
        # The link to the root was followed once; the copy below it stops at the folders being copied.
        self.assertTrue(os.path.isdir(os.path.join(copied, "loop", "target")))
        self.assertFalse(os.path.exists(os.path.join(copied, "loop", "src")))
        self.assertNotIn(TEMP_PREFIX, " ".join(os.listdir(self.real("dst"))))

    def test_temp_names_are_refused(self):
        name = TEMP_PREFIX + "0" * 32
        for call in (lambda: self.put("/" + name, b"x"), lambda: self.d.mkdir("/" + name)):
            with self.assertRaises(InvalidPath):
                call()
        self.put("/a.txt", b"a")
        with self.assertRaises(InvalidPath):
            self.d.rename("/a.txt", name)
        with open(self.real(name), "wb") as f:
            f.write(b"in progress")
        with self.assertRaises(NotFound):
            self.d.stat("/" + name)
        with self.assertRaises(NotFound):
            self.d.open("/" + name)
        symlink(name, self.real("link-to-temp"))
        with self.assertRaises(NotFound):
            self.d.open("/link-to-temp")
        self.assertEqual(self.names("/"), ["a.txt"])

    def test_root_operations_refused(self):
        with self.assertRaises(DriverError):
            self.d.delete("/")
        with self.assertRaises(DriverError):
            self.d.rename("/", "x")
        self.d.mkdir("/d")
        with self.assertRaises(DriverError):
            self.d.move("/", "/d")
        with self.assertRaises(DriverError):
            self.d.copy("/", "/d")
        with self.assertRaises(AlreadyExists):
            self.put("/", b"x")
        self.assertTrue(os.path.isdir(self.root))


class ExternalLinksAllowedTests(LocalTestCase):
    allow_external = True

    def test_outside_links_work(self):
        secret_dir = os.path.join(self.outer, "shared")
        os.mkdir(secret_dir)
        with open(os.path.join(secret_dir, "s.txt"), "wb") as f:
            f.write(b"shared")
        symlink(secret_dir, self.real("abs-dir"))
        symlink("../shared/s.txt", self.real("rel-file"))
        self.assertEqual(self.names("/"), ["abs-dir", "rel-file"])
        self.assertEqual(self.get("/rel-file"), b"shared")
        self.assertEqual(self.names("/abs-dir"), ["s.txt"])
        # Copies never follow links out of the folder.
        with self.assertRaises(DriverError):
            self.d.copy("/abs-dir", "/")
        self.put("/dir/x", b"x")
        symlink(secret_dir, self.real("dir", "out"))
        self.d.mkdir("/dst")
        self.d.copy("/dir", "/dst")
        self.assertEqual(self.names("/dst/dir"), ["x"])
        # Deleting the link leaves what it points to alone.
        self.d.delete("/abs-dir")
        self.assertEqual(os.listdir(secret_dir), ["s.txt"])


class NameTests(LocalTestCase):
    def test_non_utf8_names_are_left_out(self):
        self.put("/ok.txt", b"ok")
        if os.name == "nt":  # names are UTF-16 there: what isn't text is a lone surrogate
            os.close(os.open(self.real("bad-\udcff.txt"), os.O_CREAT | os.O_WRONLY))
            os.mkdir(self.real("bad-\udcfe-dir"))
        else:
            try:
                fd = os.open(os.fsencode(self.root) + b"/bad-\xff.txt", os.O_CREAT | os.O_WRONLY)
            except OSError as error:
                if error.errno != errno.EILSEQ:
                    raise
                self.skipTest("the file system takes only UTF-8 names (APFS on macOS)")
            os.close(fd)
            os.mkdir(os.fsencode(self.root) + b"/bad-\xfe-dir")
        self.assertEqual(self.names("/"), ["ok.txt"])
        # The site never passes such a path (paths.clean refuses lone surrogates); the driver
        # refuses it too, as missing or as invalid.
        with self.assertRaises((NotFound, InvalidPath)):
            self.d.stat("/bad-\udcff.txt")
        with self.assertRaises((NotFound, InvalidPath)):
            self.d.open("/bad-\udcff.txt")

    def test_names_of_dots_and_spaces_only_are_left_out(self):
        # Made outside HFile: they can't be addressed (D46), and the folder must still list. Windows
        # drops dots and spaces at the end of a name, except after \\?\ (as other systems' programs make them).
        self.put("/ok.txt", b"ok")
        prefix = "\\\\?\\" if os.name == "nt" else ""
        for name in ("...", ".. ", " . "):
            path = prefix + os.path.join(self.root, name)
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY))
            self.addCleanup(os.unlink, path)
        folder = prefix + os.path.join(self.root, "....")
        os.mkdir(folder)
        self.addCleanup(os.rmdir, folder)
        self.assertEqual(self.names("/"), ["ok.txt"])

    @unittest.skipUnless(hasattr(os, "mkfifo"), "Windows has no FIFOs")
    def test_special_files_are_left_out(self):
        os.mkfifo(self.real("fifo"))
        symlink("fifo", self.real("link-to-fifo"))
        self.put("/f.txt", b"f")
        self.assertEqual(self.names("/"), ["f.txt"])
        for path in ("/fifo", "/link-to-fifo"):
            with self.subTest(path=path):
                with self.assertRaises(NotFound):
                    self.d.open(path)  # must not hang
                with self.assertRaises(NotFound):
                    self.d.stat(path)
                with self.assertRaises(NotFound):
                    self.d.delete(path)
                with self.assertRaises(NotFound):
                    self.d.rename(path, "other")
        self.assertTrue(os.path.exists(self.real("fifo")))

    def test_times_out_of_range(self):
        far = os.stat_result((stat.S_IFREG | 0o644, 1, 1, 1, 0, 0, 5, 0, 10**18, 0))
        self.assertIsNone(local._mtime(far))
        self.assertIsNone(local._entry("/f", far).modified)

    def test_backslash_and_odd_names_are_plain_names(self):
        names = ["a\\b", "..a", "a..", " lead", "trail ", "-dash", "*?[]"]
        if os.name == "nt":  # Windows refuses the others (WindowsTests)
            names = ["..a", " lead", "-dash"]
        for name in names:
            with self.subTest(name=name):
                path = "/" + name
                self.put(path, b"x")
                self.assertTrue(os.path.isfile(self.real(name)))
                self.assertEqual(self.get(path), b"x")
                self.d.delete(path)
        self.assertEqual(os.listdir(self.root), [])


class WriteTests(LocalTestCase):
    def test_failed_write_leaves_nothing(self):
        self.put("/keep.txt", b"old")

        class Broken(io.RawIOBase):
            def __init__(self):
                self.calls = 0

            def read(self, n=-1):
                self.calls += 1
                if self.calls > 1:
                    raise OSError("client went away")
                return b"x" * 10

        with self.assertRaises(OSError):
            self.d.write("/keep.txt", Broken(), 100)
        with self.assertRaises(DriverError):
            self.d.write("/new.txt", io.BytesIO(b"short"), 100)
        self.assertEqual(sorted(os.listdir(self.root)), ["keep.txt"])
        self.assertEqual(self.get("/keep.txt"), b"old")

    def test_reads_more_than_size_are_cut(self):
        self.d.write("/a", io.BytesIO(b"0123456789"), 4)
        self.assertEqual(self.get("/a"), b"0123")

    def test_overwrite_is_atomic_for_readers(self):
        self.put("/f.bin", b"A" * 300000)
        stream = self.d.open("/f.bin")
        first = next(iter(stream))
        self.put("/f.bin", b"B" * 10)
        rest = b"".join(stream)
        stream.close()
        self.assertEqual(first + rest, b"A" * 300000)
        self.assertEqual(self.get("/f.bin"), b"B" * 10)

    @unittest.skipIf(os.name == "nt", "Windows: no mode bits but read-only, and a new file takes its folder's access list")
    def test_overwrite_keeps_permission_bits(self):
        self.put("/f", b"1")
        os.chmod(self.real("f"), 0o640)
        self.put("/f", b"2")
        self.assertEqual(stat.S_IMODE(os.stat(self.real("f")).st_mode), 0o640)

    @unittest.skipIf(getattr(os, "geteuid", lambda: -1)() == 0, "root may write any file")
    def test_a_read_only_file_isnt_overwritten(self):
        """FILE-05: overwriting writes a new file and renames it over the old one, so a file whose mode said
        nobody may write it (0444) was replaced all the same, and a hard link to it kept the old bytes. A file
        is overwritten only when it could be written to."""
        self.put("/r.txt", b"old")
        os.chmod(self.real("r.txt"), 0o444)
        with self.assertRaises(DriverError) as caught:
            self.put("/r.txt", b"new")
        self.assertIn("只读", caught.exception.message)
        self.assertEqual(self.get("/r.txt"), b"old")
        os.chmod(self.real("r.txt"), 0o644)
        self.put("/r.txt", b"new")
        self.assertEqual(self.get("/r.txt"), b"new")
        # A folder that can't be written to: the new file can't be made next to the old one. Said as such.
        os.mkdir(self.real("ro"))
        self.put("/ro/f.txt", b"old")
        if os.name == "nt":  # a folder's read-only attribute keeps nothing out there
            deny(self, self.real("ro"), "WD,AD")
        else:
            os.chmod(self.real("ro"), 0o555)
            self.addCleanup(os.chmod, self.real("ro"), 0o755)
        with self.assertRaises(DriverError) as caught:
            self.put("/ro/f.txt", b"new")
        self.assertIn("文件夹", caught.exception.message)
        self.assertEqual(self.get("/ro/f.txt"), b"old")

    def test_temp_file_hidden_while_writing(self):
        seen = {}

        class Slow(io.RawIOBase):
            def __init__(inner):
                inner.sent = False

            def read(inner, n=-1):
                if not inner.sent:
                    inner.sent = True
                    return b"x"
                seen["names"] = self.names("/")
                seen["files"] = os.listdir(self.root)
                return b"y"

        self.d.write("/up.txt", Slow(), 2)
        self.assertEqual(seen["names"], [])
        self.assertEqual(len(seen["files"]), 1)
        self.assertTrue(seen["files"][0].startswith(TEMP_PREFIX))
        self.assertEqual(self.get("/up.txt"), b"xy")

    def test_stale_temp_files_are_cleaned(self):
        old = self.real(TEMP_PREFIX + "a" * 32)
        new = self.real(TEMP_PREFIX + "b" * 32)
        for path in (old, new):
            with open(path, "wb") as f:
                f.write(b"partial")
        long_ago = time.time() - 2 * 24 * 3600
        os.utime(old, (long_ago, long_ago))
        self.assertEqual(self.names("/"), [])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))

    def test_write_through_file_in_path(self):
        self.put("/f", b"file")
        with self.assertRaises(NotFound):
            self.put("/f/x.txt", b"x")
        with self.assertRaises(NotFound):
            self.d.mkdir("/f/sub")
        with self.assertRaises(NotADirectory):
            self.d.list("/f")

    def test_concurrent_writes(self):
        errors = []

        def work(i):
            try:
                for j in range(20):
                    self.put(f"/c/{i % 3}/f{j}.txt", f"{i}-{j}".encode())
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.names("/c"), ["0", "1", "2"])
        self.assertEqual(len(self.names("/c/0")), 20)


class RenameMoveTests(LocalTestCase):
    def test_rename_never_replaces(self):
        self.put("/a", b"a")
        self.d.mkdir("/empty")
        with self.assertRaises(AlreadyExists):
            self.d.rename("/a", "empty")
        self.d.mkdir("/d")
        with self.assertRaises(AlreadyExists):
            self.d.rename("/d", "empty")  # rename(2) would replace an empty folder
        self.assertTrue(os.path.isdir(self.real("d")))

    def test_rename_noreplace_fallback(self):
        self.put("/a", b"a")
        self.put("/b", b"b")
        self.d.mkdir("/d")
        inode = os.stat(self.real("a")).st_ino
        with mock.patch.object(local, "_renameat2", None):
            with self.assertRaises(AlreadyExists):
                self.d.rename("/a", "b")  # link() refuses to replace
            self.d.rename("/a", "c")
            with self.assertRaises(AlreadyExists):
                self.d.rename("/d", "c")
            self.d.rename("/d", "e")  # folders: checked, then renamed
        self.assertEqual(self.names("/"), ["b", "c", "e"])
        self.assertEqual(os.stat(self.real("c")).st_ino, inode)
        self.assertEqual(os.stat(self.real("c")).st_nlink, 1)

    def test_same_folder_move_is_nothing(self):
        self.put("/d/a", b"a")
        self.d.move("/d/a", "/d")
        self.assertEqual(self.get("/d/a"), b"a")
        with self.assertRaises(NotFound):
            self.d.move("/d/missing", "/d")

    def test_move_into_itself_through_a_link(self):
        self.put("/m/n/x", b"x")
        symlink("m/n", self.real("into-n"))
        with self.assertRaises(DriverError):
            self.d.move("/m", "/into-n")
        self.assertEqual(self.get("/m/n/x"), b"x")

    def test_copy_into_itself_through_a_link(self):
        """The target folder is told by what it is, not by its name: a link inside the root leads into the folder."""
        self.put("/m/n/x", b"x")
        symlink("m/n", self.real("into-n"))
        with self.assertRaises(DriverError) as caught:
            self.d.copy("/m", "/into-n")
        self.assertEqual(caught.exception.message, gettext("A folder can't be copied into itself or a folder inside it."))
        self.assertEqual(self.names("/m/n"), ["x"])

    def test_move_across_file_systems(self):
        self.put("/src/s/x.txt", b"x")
        self.put("/src/s/t/y.txt", b"y")
        symlink("x.txt", self.real("src", "s", "link"))
        self.d.mkdir("/dst")
        if os.name == "nt":  # links aren't made there (local._copy_link): the move is refused, nothing lost
            with self._exdev(), self.assertRaises(DriverError):
                self.d.move("/src/s", "/dst")
            self.assertEqual(self.names("/src/s"), ["link", "t", "x.txt"])
            self.assertEqual(os.listdir(self.real("dst")), [])
            return
        with self._exdev():
            self.d.move("/src/s", "/dst")
        self.assertEqual(self.names("/src"), [])
        self.assertEqual(self.get("/dst/s/t/y.txt"), b"y")
        self.assertTrue(os.path.islink(self.real("dst", "s", "link")))  # moved as a link
        self.assertEqual(self.get("/dst/s/link"), b"x")

    def _exdev(self):
        real_rename = local._rename_noreplace

        def rename(src_fd, src, dst_fd, dst):
            if not src.startswith(TEMP_PREFIX):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            return real_rename(src_fd, src, dst_fd, dst)

        return mock.patch.object(local, "_rename_noreplace", rename)

    @unittest.skipIf(os.name == "nt", "Windows: no permission bits, and a move to another volume doesn't keep times")
    def test_move_across_file_systems_keeps_permissions_and_times(self):
        """Copied and deleted, a move to another file system gave what it moved new permission bits (a 0700 folder
        became 0775, which every account on the server can read; a script lost its execute bit) and the time of the
        move as their modification time. They are kept now, as a move on one file system keeps them."""
        self.put("/src/s/run.sh", b"#!/bin/sh\n")
        self.put("/src/s/t/data.bin", b"x")
        self.put("/src/f.sh", b"#!/bin/sh\n")
        for names, mode in ((("s", "run.sh"), 0o750), (("s", "t"), 0o700), (("s",), 0o751), (("f.sh",), 0o700)):
            os.chmod(self.real("src", *names), mode)
        moved = [("s", "run.sh"), ("s", "t", "data.bin"), ("s", "t"), ("s",), ("f.sh",)]
        for i, names in enumerate(moved):  # what is in a folder first: making it changes the folder's time
            os.utime(self.real("src", *names), ns=(1577934245 * 10**9, 1577934245 * 10**9 + i * 1001))
        before = {names: os.lstat(self.real("src", *names)) for names in moved}
        self.d.mkdir("/dst")
        with self._exdev():
            self.d.move("/src/s", "/dst")
            self.d.move("/src/f.sh", "/dst")
        self.assertEqual(self.names("/src"), [])
        for names, info in before.items():
            with self.subTest(names=names):
                after = os.lstat(self.real("dst", *names))
                self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(info.st_mode))
                self.assertEqual(after.st_mtime_ns, info.st_mtime_ns)

    @unittest.skipIf(os.name == "nt", "Windows: no permission bits")
    def test_copies_keep_permission_bits(self):
        """Copies were made with 0666 and 0777 less the umask: a private 0700 folder became 0755, a script lost its
        execute bit. Now they have the original's bits less the umask, as cp gives them; a folder its owner may not
        write to (0555) is copied all the same."""
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        self.put("/src/private/key.txt", b"k")
        self.put("/src/run.sh", b"#!/bin/sh\n")
        self.put("/src/ro/r.txt", b"r")
        for names, mode in ((("private", "key.txt"), 0o600), (("private",), 0o700), (("run.sh",), 0o775), (("ro",), 0o555)):
            os.chmod(self.real("src", *names), mode)
        self.addCleanup(os.chmod, self.real("src", "ro"), 0o755)
        self.d.mkdir("/dst")
        self.d.copy("/src", "/dst")
        copied = os.path.join(self.root, "dst", "src")
        if os.path.isdir(os.path.join(copied, "ro")):
            self.addCleanup(os.chmod, os.path.join(copied, "ro"), 0o755)
        modes = {names: stat.S_IMODE(os.stat(os.path.join(copied, *names)).st_mode)
                 for names in (("private",), ("private", "key.txt"), ("run.sh",), ("ro",), ("ro", "r.txt"))}
        self.assertEqual(modes, {("private",): 0o700, ("private", "key.txt"): 0o600, ("run.sh",): 0o755,
                                 ("ro",): 0o555, ("ro", "r.txt"): 0o644})

    @unittest.skipUnless(sys.platform.startswith("linux"), "names that aren't UTF-8 (macOS's APFS refuses them), FIFOs "
                                                           "and Unix sockets: Linux's")
    def test_move_across_file_systems_keeps_everything(self):
        self.put("/src/s/ok.txt", b"ok")
        folder = os.fsencode(self.real("src", "s"))
        with open(folder + b"/\xb1\xa8.doc", "wb") as f:  # a GBK name: not shown, but moved
            f.write(b"gbk")
        os.mkfifo(self.real("src", "s", "pipe"))
        self.d.mkdir("/dst")
        with self._exdev():
            self.d.move("/src/s", "/dst")
        moved = os.fsencode(self.real("dst", "s"))
        self.assertEqual(sorted(os.listdir(moved)), [b"ok.txt", b"pipe", b"\xb1\xa8.doc"])
        self.assertTrue(stat.S_ISFIFO(os.lstat(moved + b"/pipe").st_mode))
        self.assertFalse(os.path.exists(self.real("src", "s")))
        # A socket can't be copied: the move is refused and nothing is lost or left behind.
        self.put("/src/t/x", b"x")
        server = socket.socket(socket.AF_UNIX)
        self.addCleanup(server.close)
        server.bind(self.real("src", "t", "sock"))
        with self._exdev(), self.assertRaises(DriverError):
            self.d.move("/src/t", "/dst")
        self.assertEqual(sorted(os.listdir(self.real("src", "t"))), ["sock", "x"])
        self.assertEqual(os.listdir(self.real("dst")), ["s"])

    @unittest.skipIf(resource is None, "Windows has no limit on open files to set")
    def test_deep_trees_use_few_descriptors(self):
        names = ["d"] * 200
        os.makedirs(self.real(*names))
        with open(self.real(*names, "f.txt"), "wb") as f:
            f.write(b"deep")
        symlink("../" * 199 + "d", self.real(*names, "up"))  # 199 levels of ".." in one link
        deep = "/" + "/".join(names)
        self.d.mkdir("/copy")
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        in_use = len(os.listdir("/dev/fd"))  # this process's open files: on Linux /proc/self/fd, on macOS its own
        resource.setrlimit(resource.RLIMIT_NOFILE, (in_use + 16, hard))
        try:
            self.assertEqual(self.names(deep), ["f.txt", "up"])
            self.assertEqual(self.get(deep + "/f.txt"), b"deep")
            self.assertTrue(self.d.stat(deep + "/up").is_dir)
            self.d.copy("/d", "/copy")
            self.d.delete("/d")
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertEqual(self.get("/copy" + deep + "/f.txt"), b"deep")
        self.assertEqual(self.names("/"), ["copy"])

    def test_too_deep(self):
        names = ["x"] * (local.MAX_DEPTH + 4)
        if os.name == "nt":  # longer than Windows' 260 characters: made and removed after \\?\ (the driver needs none)
            os.makedirs("\\\\?\\" + self.real(*names))
            self.addCleanup(shutil.rmtree, "\\\\?\\" + self.real("x"))
        else:
            os.makedirs(self.real(*names))
        with self.assertRaises(InvalidPath):
            self.d.list("/" + "/".join(names))
        with self.assertRaises(InvalidPath):
            self.d.mkdir("/" + "/".join(["y"] * (local.MAX_DEPTH + 1)))
        with self.assertRaises(DriverError) as caught:
            self.d.delete("/x")
        self.assertIn("一部分", str(caught.exception))
        with self.assertRaises(DriverError):
            self.d.copy("/x", "/")

    def test_parent_that_can_be_entered_but_not_read(self):
        if getattr(os, "geteuid", lambda: -1)() == 0:
            self.skipTest("root reads everything")
        if os.name != "nt" and not hasattr(os, "O_PATH"):
            self.skipTest("a folder that can't be read is entered with O_PATH, Linux's (_WALK): elsewhere it can't be")
        self.put("/p/child/x.txt", b"x")
        self.put("/p/other.txt", b"o")
        if os.name == "nt":
            readable_again = deny(self, self.real("p"), "RD")
        else:
            os.chmod(self.real("p"), 0o300)
            self.addCleanup(os.chmod, self.real("p"), 0o755)

            def readable_again():
                os.chmod(self.real("p"), 0o755)
        self.assertEqual(self.get("/p/child/x.txt"), b"x")
        self.d.delete("/p/child")
        self.d.delete("/p/other.txt")
        readable_again()
        self.assertEqual(os.listdir(self.real("p")), [])

    def test_failed_copy_leaves_nothing(self):
        self.put("/src/a", b"a" * 100)
        self.put("/src/b", b"b" * 100)
        self.d.mkdir("/dst")
        calls = []
        real_copy = local._copy_fd

        def failing(src, dst):
            calls.append(1)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
            real_copy(src, dst)

        with mock.patch.object(local, "_copy_fd", failing), self.assertRaises(DriverError) as caught:
            self.d.copy("/src", "/dst")
        self.assertIn("空间", str(caught.exception))
        self.assertEqual(os.listdir(self.real("dst")), [])


class ReadTests(LocalTestCase):
    def test_stream_details(self):
        self.put("/f", b"0123456789")
        stream = self.d.open("/f", 3, 4)
        self.assertEqual((stream.offset, stream.size, stream.total), (3, 4, 10))
        self.assertIsNotNone(stream.etag)
        self.assertIsNotNone(stream.modified.tzinfo)
        self.assertEqual(stream.read(), b"3456")
        self.assertEqual(self.get("/f", 9), b"9")
        self.assertEqual(self.get("/f", 0, 0), b"")

    def test_unopened_stream_closes_its_file(self):
        self.put("/f", b"x")
        before = len(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
        for _ in range(50):
            self.d.open("/f").close()
            self.d.list("/")
            self.d.stat("/f")
        if before is not None:
            self.assertLessEqual(len(os.listdir("/proc/self/fd")), before + 2)

    def test_unreadable_folder(self):
        if getattr(os, "geteuid", lambda: -1)() == 0:
            self.skipTest("root reads everything")
        self.put("/locked/x", b"x")
        if os.name == "nt":
            deny(self, self.real("locked"), "RD")
        else:
            os.chmod(self.real("locked"), 0)
            self.addCleanup(os.chmod, self.real("locked"), 0o755)
        with self.assertRaises(DriverError) as caught:
            self.d.list("/locked")
        self.assertIn("权限", str(caught.exception))


class ErrorTests(unittest.TestCase):
    def test_a_file_in_use(self):
        """On Windows a file another program has open without sharing it (Excel, Word) can't be deleted or renamed: NT's
        sharing violation, EBUSY (_nt.py), was told as "Device or resource busy", in English, as the server failing."""
        error = local._error(OSError(errno.EBUSY, os.strerror(errno.EBUSY)))
        self.assertIs(type(error), DriverError)
        self.assertNotIn(os.strerror(errno.EBUSY), error.message)


@unittest.skipUnless(os.name == "nt", "what is different on Windows (_nt.py)")
class WindowsTests(LocalTestCase):
    def test_names_match_whatever_their_case(self):
        self.put("/Doc.TXT", b"x")
        self.assertEqual(self.get("/doc.txt"), b"x")
        self.assertEqual(self.names("/"), ["Doc.TXT"])
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/DOC.txt")

    def test_a_folder_into_itself_under_other_capitals(self):
        """/A/b is inside /a: a copy landed in the folder it copied (with the folders it had then), and a move into a
        folder inside it said that the server's permissions don't allow it."""
        self.put("/a/b/x", b"x")
        for operation, message in (("copy", "A folder can't be copied into itself or a folder inside it."),
                                   ("move", "A folder can't be moved into itself or a folder inside it.")):
            for folder in ("/A", "/A/B"):
                with self.subTest(operation, folder=folder):
                    with self.assertRaises(DriverError) as caught:
                        getattr(self.d, operation)("/a", folder)
                    self.assertEqual(caught.exception.message, gettext(message))
        self.assertEqual(self.names("/a"), ["b"])
        self.assertEqual(self.names("/a/b"), ["x"])

    def test_an_alternate_data_stream_is_never_reached(self):
        self.put("/a.txt", b"main")
        with open(self.real("a.txt") + ":hidden", "wb") as f:  # Windows writes the stream itself
            f.write(b"stream")
        for path in ("/a.txt:hidden", "/a.txt::$DATA", "/a.txt:hidden:$DATA"):
            with self.subTest(path=path), self.assertRaises(NotFound):
                self.get(path)
        with self.assertRaises(InvalidPath):
            self.put("/a.txt:other", b"x")
        self.assertEqual(self.get("/a.txt"), b"main")

    def test_a_short_name_finds_nothing(self):
        """An 8.3 name ("SECRE~1") is another name for the same folder: rules for the long one wouldn't see it."""
        import ctypes

        self.d.mkdir("/Secret Folder")
        self.put("/Secret Folder/s.txt", b"s")
        buffer = ctypes.create_unicode_buffer(1024)
        ctypes.windll.kernel32.GetShortPathNameW(self.real("Secret Folder"), buffer, len(buffer))
        short = os.path.basename(buffer.value)
        if not short or short == "Secret Folder":
            self.skipTest("this volume makes no 8.3 names")
        for path in (f"/{short}/s.txt", f"/{short.lower()}/s.txt"):
            with self.subTest(path=path), self.assertRaises(NotFound):
                self.get(path)
        with self.assertRaises(NotFound):
            self.d.list(f"/{short}")
        self.assertEqual(self.get("/secret folder/s.txt"), b"s")

    def test_junctions_are_links(self):
        import _winapi

        secret = os.path.join(self.outer, "secret")
        os.mkdir(secret)
        with open(os.path.join(secret, "s.txt"), "wb") as f:
            f.write(b"secret")
        self.d.mkdir("/inside")
        _winapi.CreateJunction(secret, self.real("out"))
        _winapi.CreateJunction(self.real("inside"), self.real("in"))
        self.assertEqual(self.names("/"), ["in", "inside"])  # the one out of the root isn't shown
        for path in ("/out/s.txt", "/out"):
            with self.subTest(path=path), self.assertRaises(NotFound):
                self.d.stat(path)
        self.put("/in/f.txt", b"f")  # followed inside the root
        self.assertEqual(self.get("/inside/f.txt"), b"f")
        self.d.delete("/in")  # the junction goes, not the folder it leads to
        self.assertEqual(self.names("/"), ["inside"])
        self.assertEqual(self.names("/inside"), ["f.txt"])

    def test_names_windows_refuses(self):
        for name in ("a<b", "a>b", 'a"b', "a|b", "a?b", "a*b", "a\x01b", "dot.", "space ", "CON", "con.txt", "Nul",
                     "COM1", "lpt9.log", "COM\u00b9"):
            with self.subTest(name=name):
                with self.assertRaises(InvalidPath):
                    self.put("/" + name, b"x")
                with self.assertRaises(InvalidPath):
                    self.d.mkdir("/" + name)
        self.put("/console.txt", b"x")  # only the device names themselves
        self.assertEqual(self.names("/"), ["console.txt"])

    def test_a_file_being_read_is_replaced_and_deleted(self):
        """As on Linux: nothing the site has open keeps others from replacing or deleting the file."""
        self.put("/f.txt", b"old")
        stream = self.d.open("/f.txt")
        try:
            self.put("/f.txt", b"new")
            self.assertEqual(self.get("/f.txt"), b"new")
            self.d.delete("/f.txt")
            self.assertEqual(self.names("/"), [])
            self.assertEqual(stream.read(), b"old")
        finally:
            stream.close()

    def test_the_root_has_a_drive(self):
        for root in ("\\files", "files"):
            with self.subTest(root=root):
                self.assertIn("盘符", LocalDriver.validate_config({"root": root})["root"])
        drive = os.path.splitdrive(self.root)[0] + "\\"
        self.assertIn("root", LocalDriver.validate_config({"root": drive}))
        self.assertEqual(LocalDriver.validate_config({"root": self.root}), {})


if __name__ == "__main__":
    unittest.main()
