"""Tests of the SMB driver (apps.storages.drivers.smb) over FakeSMB, a stand-in for smbclient on a local folder, and
over smb_wire, which keeps smbclient's own code and replaces only smbprotocol's network layer (what happens inside
smbclient: reconnecting, its own requests).

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_smb
"""

from __future__ import annotations

import io
import os
import socket
import stat as stat_module
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from smbprotocol.exceptions import SMBLinkRedirectionError, SMBOSError, SMBResponseException
from smbprotocol.header import NtStatus, SMB2HeaderResponse

from apps.storages.drivers.base import AuthError, BackendError, DriverError, NotFound
from apps.storages.drivers.smb import SMBDriver
from apps.storages.tests.contract import DriverContract, linux_server
from apps.storages.tests.fake_smb import FakeSMB
from apps.storages.tests.smb_wire import Wire


def driver_for(fake: FakeSMB, **config) -> SMBDriver:
    cls = type("FakeSMBDriver", (SMBDriver,), {"client": fake})
    settings = {"host": fake.host, "share": fake.share, "username": "alice", "password": "pw-alice", **config}
    return cls({key: value for key, value in settings.items() if value is not None})


class SMBContractTests(DriverContract, unittest.TestCase):
    server_on_this_disk = True  # FakeSMB keeps the files in a local folder

    def make_driver(self):
        self.fake = FakeSMB(self.enterContext(tempfile.TemporaryDirectory()))
        return driver_for(self.fake)


class SMBBasePathContractTests(DriverContract, unittest.TestCase):
    server_on_this_disk = True

    def make_driver(self):
        root = self.enterContext(tempfile.TemporaryDirectory())
        os.makedirs(os.path.join(root, "a", "base"))
        with open(os.path.join(root, "a", "outside.txt"), "wb") as f:
            f.write(b"not below the base")
        self.fake = FakeSMB(root)
        return driver_for(self.fake, base_path="/a/base")


class SMBTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeSMB(self.enterContext(tempfile.TemporaryDirectory()))

    def new_driver(self, **config):
        driver = driver_for(self.fake, **config)
        self.addCleanup(driver.close)
        return driver

    def test_one_session_for_the_instance(self):
        d = self.new_driver(port=4445, encrypt=True)
        d.mkdir("/x")
        d.list("/")
        self.assertEqual(self.fake.sessions, [{"username": "alice", "port": 4445, "encrypt": True,
                                               "require_signing": True, "own_cache": True}])
        d.close()
        self.assertEqual(self.fake.resets, 1)

    def test_the_guest_account(self):
        guest = self.new_driver(username=None, password=None)
        guest.list("/")
        self.assertEqual(self.fake.sessions[-1]["username"], "Guest")
        self.assertFalse(self.fake.sessions[-1]["require_signing"])  # guests can't sign
        self.fake.guest = False
        with self.assertRaises(AuthError):
            self.new_driver(username=None, password=None).list("/")

    def test_wrong_credentials_and_an_unreachable_server(self):
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong").list("/")
        self.fake.offline = True
        with self.assertRaises(BackendError):
            self.new_driver().list("/")

    def test_another_share(self):
        with self.assertRaises(NotFound):
            self.new_driver(share="other").list("/")

    def test_a_failed_upload_leaves_nothing(self):
        d = self.new_driver()
        with self.assertRaises(Exception):
            d.write("/cut.bin", io.BytesIO(b"short"), 100)
        self.assertEqual(os.listdir(self.fake.root), [])

    def test_an_upload_to_a_full_share(self):
        self.fake.disk_full = True
        d = self.new_driver()
        for size in (10, 3 * 1024 * 1024):  # the end of an upload goes out with close(); a large one's start before
            with self.subTest(size=size), self.assertRaises(DriverError):
                d.write("/upload.bin", io.BytesIO(bytes(size)), size)
            self.assertEqual(os.listdir(self.fake.root), [])

    def test_downloads_let_others_at_the_file(self):
        d = self.new_driver()
        d.write("/a.txt", io.BytesIO(b"abc"), 3)
        with d.open("/a.txt", 1, 1) as stream:
            self.assertEqual(b"".join(stream), b"b")
        self.assertEqual([access for _path, mode, access in self.fake.opens if mode == "rb"], ["rwd"])

    def test_replacing_a_file_the_server_refuses(self):
        d = self.new_driver()
        d.write("/report.docx", io.BytesIO(b"old"), 3)
        self.fake.refuse_replace = True
        with self.assertRaises(DriverError) as caught:
            d.write("/report.docx", io.BytesIO(b"new"), 3)
        self.assertIn("report.docx", caught.exception.message)
        self.assertEqual(os.listdir(self.fake.root), ["report.docx"])

    def test_settings(self):
        check = SMBDriver.validate_config
        self.assertEqual(check({"host": "nas.local", "share": "public"}), {})
        self.assertEqual(check({"host": "192.168.1.10", "share": "public"}), {})
        self.assertIn("host", check({"host": "\\\\nas", "share": "public"}))
        self.assertIn("host", check({"host": "smb://nas", "share": "public"}))
        self.assertIn("share", check({"host": "nas", "share": "a/b"}))
        self.assertIn("base_path", check({"host": "nas", "share": "s", "base_path": "/a/../b"}))

    def test_a_listing_is_what_the_server_listed(self):
        d = self.new_driver()
        d.write("/docs/a.txt", io.BytesIO(b"12345"), 5)
        d.mkdir("/docs/sub")
        entries = {entry.name: entry for entry in d.list("/docs")}  # scandir's entries' stat() raises (FakeSMB)
        local = os.stat(os.path.join(self.fake.root, "docs", "a.txt"))
        self.assertEqual((entries["a.txt"].is_dir, entries["a.txt"].size), (False, 5))
        self.assertEqual(int(entries["a.txt"].modified.timestamp()), int(local.st_mtime))
        self.assertEqual((entries["sub"].is_dir, entries["sub"].size), (True, None))

    def test_a_link_that_can_not_be_followed_is_left_out(self):
        d = self.new_driver()
        for name in ("report.docx", "photo.jpg"):
            d.write(f"/{name}", io.BytesIO(b"x"), 1)
        d.mkdir("/Projects")
        # mklink /D Projects D:\Projects on a Windows server: smbprotocol can't follow a link to a local path.
        self.fake.links["Projects"] = SMBLinkRedirectionError(
            "Cannot resolve link targets that point to a local path", r"\\nas\public\Projects", r"D:\Projects")
        self.assertEqual(sorted(entry.name for entry in d.list("/")), ["photo.jpg", "report.docx"])

    def test_an_ipv6_address(self):
        check = SMBDriver.validate_config
        for host in ("fd00::5", "[fd00::5]", "fe80::1%eth0"):
            self.assertEqual(check({"host": host, "share": "public"}), {}, host)
        for host in ("[fd00::5", "fd00::zz", "[nas]"):
            self.assertIn("host", check({"host": host, "share": "public"}), host)
        fake = FakeSMB(self.enterContext(tempfile.TemporaryDirectory()), host="fd00::5")
        d = driver_for(fake, host="[fd00::5]")
        self.addCleanup(d.close)
        self.assertEqual(d.list("/"), [])  # FakeSMB only answers fd00::5, without the brackets

    @linux_server
    def test_a_case_only_rename(self):
        d = self.new_driver()
        d.write("/a.txt", io.BytesIO(b"a"), 1)
        d.rename("/a.txt", "A.txt")
        self.assertEqual([e.name for e in d.list("/")], ["A.txt"])


class WireTests(unittest.TestCase):
    """The driver over smbclient's real code (smb_wire): a server named nas with the share "public"."""

    def setUp(self):
        self.wire = Wire(self)

    def new_driver(self, **config):
        driver = SMBDriver({"host": "nas", "share": "public", "username": "alice", "password": "pw", **config})
        self.addCleanup(driver.close)
        return driver

    def test_a_guest_after_the_server_closed_the_connection(self):
        d = self.new_driver(username="", password="")
        d.stat("/")
        self.wire.drop()  # a NAS restart: smbclient finds its connection closed and opens a new one
        d.stat("/")
        last = self.wire.logons[-1]
        self.assertEqual((last.username, last.password, last.require_signing), ("Guest", "", False))

    def test_encryption_after_the_server_closed_the_connection(self):
        d = self.new_driver(encrypt=True)
        d.stat("/")
        self.wire.drop()
        d.stat("/")
        self.assertEqual([logon.require_encryption for logon in self.wire.logons], [True, True])

    def test_copying_a_folder(self):
        self.wire.share.put("src\\sub\\x.txt", b"x")
        self.wire.share.put("src\\y.txt", b"yy")
        self.wire.share.put("dst")
        self.new_driver().copy("/src", "/dst")
        self.assertEqual(bytes(self.wire.share.get("dst\\src\\sub\\x.txt").data), b"x")
        self.assertEqual(bytes(self.wire.share.get("dst\\src\\y.txt").data), b"yy")
        self.assertEqual({logon.username for logon in self.wire.logons}, {"alice"})
        self.assertEqual({request.username for request in self.wire.requests}, {"alice"})
        self.assertEqual(self.wire.global_cache, {})  # no connection outside the driver's own cache

    def test_replacing_a_file_samba_will_not_open_for_execute(self):
        # Samba refuses an open for FILE_EXECUTE of a file without execute permission (acl allow execute always =
        # no); OpenMediaVault makes every file 0664. smbclient's replace() and rename() open the target so first.
        self.wire.refuse_execute = True
        self.wire.share.put("report.docx", b"old")
        d = self.new_driver()
        d.write("/report.docx", io.BytesIO(b"new"), 3)
        self.assertEqual(bytes(self.wire.share.get("report.docx").data), b"new")
        d.rename("/report.docx", "Report.docx")  # the target is the file itself
        self.assertEqual(self.wire.share.get("report.docx").path, "Report.docx")

    def test_a_listing_on_another_port(self):
        for name in ("a.txt", "b.txt", "c.txt"):
            self.wire.share.put(f"docs\\{name}", name.encode())
        d = self.new_driver(port=4445)
        self.assertEqual(sorted(entry.name for entry in d.list("/docs")), ["a.txt", "b.txt", "c.txt"])
        self.assertEqual({(c.server_name, c.port) for c in self.wire.connections}, {("nas", 4445)})
        self.assertEqual([request.path for request in self.wire.requests], [r"\\nas\public\docs"])  # the listing alone


class ErrorTests(unittest.TestCase):
    """What the driver makes of what servers answer (no server needed: smbprotocol's own exceptions)."""

    def setUp(self):
        self.d = SMBDriver({"host": "nas", "share": "public", "username": "alice", "password": "pw"})

    @staticmethod
    def answer(status: int) -> SMBResponseException:
        header = SMB2HeaderResponse()
        header["status"] = status
        header["data"] = b"\x09" + b"\x00" * 8  # an SMB2 ERROR response without details
        return SMBResponseException(header)

    def test_kinds(self):
        # A session the server refused for the account's state: password expired, account disabled, locked out, ...
        for status in (0xC0000071, 0xC0000072, 0xC0000234, 0xC000006E, 0xC000006F, 0xC0000070, 0xC0000193,
                       0xC0000224, 0xC000015B):
            with self.subTest(status=hex(status)):
                mapped = self.d._error(self.answer(status))
                self.assertIsInstance(mapped, AuthError)
                self.assertIn("alice", mapped.message)
        # What trying again later won't change: over quota, a lock, a link smbprotocol can't follow.
        for error in (SMBOSError(0xC0000044, r"\\nas\public\a"), SMBOSError(0xC0000054, r"\\nas\public\a"),
                      SMBOSError(0xC0000055, r"\\nas\public\a"),
                      SMBLinkRedirectionError("Cannot resolve link targets that point to a local path",
                                              r"\\nas\public\Projects", r"D:\Projects")):
            with self.subTest(error=type(error).__name__):
                self.assertIs(type(self.d._error(error)), DriverError)
        self.assertIn("0xC000000D", self.d._error(self.answer(0xC000000D)).message)  # other answers of the server

    def test_guests_can_not_encrypt(self):
        check = SMBDriver.validate_config
        self.assertIn("encrypt", check({"host": "nas", "share": "public", "encrypt": True}))
        self.assertEqual(check({"host": "nas", "share": "public", "username": "alice", "encrypt": True}), {})


class CaseInsensitiveShare:
    """In place of smbclient: a share that ignores case, as Windows and Samba do for Windows clients, with the
    folders given; what copy() and move() ask first, and a record of what they change."""

    def __init__(self, *folders: str):
        self.folders = {folder.casefold() for folder in (r"\\nas\public", *folders)}
        self.changes: list[tuple[str, str]] = []
        self.shutil = SimpleNamespace(copyfile=lambda source, target, **kwargs: self.changes.append(("copyfile", target)))

    def register_session(self, server, **kwargs):
        pass

    def reset_connection_cache(self, **kwargs):
        pass

    def stat(self, path, follow_symlinks=True, **kwargs):
        if path.casefold() in self.folders:
            return SimpleNamespace(st_mode=stat_module.S_IFDIR | 0o755, st_size=0, st_mtime=0)
        raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, path)

    def mkdir(self, path, **kwargs):
        self.changes.append(("mkdir", path))

    def scandir(self, path, **kwargs):
        return []

    def rename_information(self, source, target, *, replace, **kwargs):
        self.changes.append(("rename", target))


class CaseTests(unittest.TestCase):
    def test_into_itself_written_in_other_case(self):
        share = CaseInsensitiveShare(r"\\nas\public\a", r"\\nas\public\a\b")
        d = type("OnTheShare", (SMBDriver,), {"client": share})({"host": "nas", "share": "public", "username": "alice",
                                                                  "password": "pw"})
        for call in (lambda: d.copy("/a", "/A/b"), lambda: d.move("/a", "/A/b"), lambda: d.copy("/A", "/a/B")):
            with self.assertRaises(DriverError):
                call()
        self.assertEqual(share.changes, [])


class StalledServerTests(unittest.TestCase):
    """The real smbclient against a server that accepts connections and never answers (a hung NAS)."""

    def test_requests_made_at_once_fail_together(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        self.addCleanup(listener.close)
        accepted, stop = [], threading.Event()

        def accept():
            listener.settimeout(0.2)
            while not stop.is_set():
                try:
                    accepted.append(listener.accept()[0])  # kept open, never answered
                except OSError:
                    pass

        acceptor = threading.Thread(target=accept, daemon=True)
        acceptor.start()

        def stop_accepting():
            stop.set()
            acceptor.join()
            for connection in accepted:
                connection.setblocking(False)
                try:
                    connection.recv(65536)  # the request it never answered: closed unread, it would be a reset
                except OSError:
                    pass
                connection.close()

        self.addCleanup(stop_accepting)
        timeout = 1.0
        quick = type("Quick", (SMBDriver,), {"connect_timeout": timeout})
        d = quick({"host": "127.0.0.1", "share": "public", "username": "alice", "password": "pw",
                   "port": listener.getsockname()[1]})
        self.addCleanup(d.close)
        ended, start = {}, time.monotonic()

        def call(number):
            with self.assertRaises(BackendError):
                d.list("/")
            ended[number] = time.monotonic() - start

        callers = [threading.Thread(target=call, args=(number,), daemon=True) for number in range(3)]
        for caller in callers:
            caller.start()
        deadline = start + 6 * timeout
        for caller in callers:
            caller.join(max(0.0, deadline - time.monotonic()))
        self.assertEqual(len(ended), 3, "requests still waiting for the server")
        self.assertLess(max(ended.values()), 2 * timeout + 0.5)


if __name__ == "__main__":
    unittest.main()
