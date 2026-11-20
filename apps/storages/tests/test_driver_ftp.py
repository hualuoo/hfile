import contextlib
import datetime
import io
import ipaddress
import logging
import os
import socket
import ssl
import sys
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import asynchat
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pyftpdlib.authorizers import DummyAuthorizer
from pyftpdlib.filesystems import AbstractedFS
from pyftpdlib.handlers import FTPHandler, TLS_DTPHandler, TLS_FTPHandler
from pyftpdlib.servers import FTPServer

from apps.storages.drivers import driver_class, ftp, http
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.drivers.ftp import TEMP_PREFIX, FTPDriver, parse_list_line, parse_mlsd_line
from apps.storages.tests.contract import UNUSUAL_NAMES, DriverContract, linux_server, on_this_disk

# pyftpdlib sets up its own logging to stderr unless its logger has a handler.
logging.getLogger("pyftpdlib").addHandler(logging.NullHandler())
logging.getLogger("pyftpdlib").setLevel(logging.CRITICAL)
logging.getLogger("pyftpdlib").propagate = False

USER, PASSWORD = "hfile", "s3cret-pw"
UTC = datetime.timezone.utc


class FTPd(threading.Thread):
    """pyftpdlib serving `root` on 127.0.0.1 in a thread."""

    def __init__(self, root, *, handler=FTPHandler, encoding="utf8", perm="elradfmwMT", **attrs):
        super().__init__(daemon=True)
        authorizer = DummyAuthorizer()
        authorizer.add_user(USER, PASSWORD, root, perm=perm)
        attrs.update(authorizer=authorizer, encoding=encoding, timeout=60, banner="hfile test")
        self.server = FTPServer(("127.0.0.1", 0), type("Handler", (handler,), attrs))
        self.port = self.server.socket.getsockname()[1]
        self._stop_flag = threading.Event()
        self._stopped = threading.Event()

    def run(self):
        try:
            while not self._stop_flag.is_set():
                self.server.serve_forever(timeout=0.01, blocking=False, handle_exit=False)
        finally:
            self._stopped.set()

    def stop(self):
        self._stop_flag.set()
        self._stopped.wait(10)
        self.server.close_all()
        self.join(10)


class ListOnlyHandler(FTPHandler):
    """A server without MLSD and MLST, like vsftpd: the driver falls back to LIST."""

    proto_cmds = {k: v for k, v in FTPHandler.proto_cmds.items() if k not in ("MLSD", "MLST")}


class NoReplaceRenameHandler(FTPHandler):
    """Like IIS and FileZilla Server: RNTO onto an existing file is refused."""

    def ftp_RNTO(self, path):
        if self._rnfr and os.path.lexists(path):
            self._rnfr = None
            self.respond("553 File exists.")
            return
        return super().ftp_RNTO(path)


class UnreadableLineFS(AbstractedFS):
    """LIST shows a file called weird.bin in a line no parser reads, as some servers show odd entries."""

    def format_list(self, basedir, listing, ignore_err=True):
        for line in super().format_list(basedir, listing, ignore_err):
            yield b"?????????? 1 weird.bin\r\n" if line.rstrip().endswith(b" weird.bin") else line


class UnreadableLineHandler(ListOnlyHandler):
    abstracted_fs = UnreadableLineFS


class NoRestHandler(FTPHandler):
    """A server without REST."""

    proto_cmds = {k: v for k, v in FTPHandler.proto_cmds.items() if k != "REST"}


class ImplicitTLSHandler(TLS_FTPHandler):
    """Implicit FTPS: TLS starts before the greeting."""

    def handle(self):
        self.secure_connection(self.ssl_context)
        super().handle()


class ClosingTLSDataHandler(TLS_DTPHandler):
    """pyftpdlib's TLS data connection, closing its file before it ends TLS. On Windows that end never came
    when the client had dropped the connection (a download stopped early): pyftpdlib took the error for one
    to ignore and tried again forever (elsewhere a test write finds the connection gone first). The file
    stayed open, and Windows can't delete an open file: deleting it then got 550."""

    def close(self):
        if self.file_obj is not None and not self.file_obj.closed:
            self.file_obj.close()
        super().close()


class SilentCloseNotifyDataHandler(ClosingTLSDataHandler):
    """Like IIS and FileZilla Server: the client's close_notify at the end of an upload gets none back, and the
    upload ends when the client closes the connection. HFile waited for the answer until its timeout, 30
    seconds an upload."""

    waiting = False

    def close(self):
        if self.receive and self._ssl_established and not self._closed:
            if not self.waiting:  # the client's close_notify: wait for its FIN, saying nothing
                self.waiting = True
                return
            self._ssl_established = False  # no close_notify back, then either
        super().close()

    def recv(self, buffer_size):
        if self.waiting:  # readable again: the client's FIN
            self.close()
            return b""
        return super().recv(buffer_size)


class GBKRepliesHandler(ListOnlyHandler):
    """Replies with a line in GBK while names are UTF-8: a Chinese greeting, login message (ProFTPD's
    DisplayLogin), folder message (vsftpd's .message on CWD) and errors, as servers set up on Chinese Windows
    send them."""

    def push(self, data):
        if data[:4] in ("220 ", "230 ", "250 ", "550 "):
            asynchat.async_chat.push(self, f"{data[:3]}-欢迎光临，这一行是 GBK\r\n".encode("gbk"))
        super().push(data)


class StaleSessionHandler(FTPHandler):
    """Like ProFTPD and FileZilla Server once the TLS session of a control connection expired (their data
    connections have to resume it): from its third transfer on, every data connection of a control connection is
    refused with 425, after the 1xx reply. The client makes the data connection before it sends the command, but
    the server may read the command first (a busy machine): pyftpdlib answers 150 then, and sends the data when
    the connection is made (_on_dtp_connection); that one is refused too."""

    transfers = 0
    refuse_when_connected = False

    def push_dtp_data(self, data, isproducer=False, file=None, cmd=None):
        self.transfers += 1
        if self.transfers <= 2:
            super().push_dtp_data(data, isproducer, file, cmd)
            return
        if file is not None:
            file.close()
        if self.data_channel is None:
            self.respond("150 File status okay. About to open data connection.")
            self.refuse_when_connected = True
            return
        self.respond("125 Data connection already open. Transfer starting.")
        self.refuse()

    def _on_dtp_connection(self):
        super()._on_dtp_connection()
        if self.refuse_when_connected:
            self.refuse_when_connected = False
            self.refuse()

    def refuse(self):
        self.data_channel.close()
        self.respond("425 Unable to build data connection: TLS session of data connection not resumed.")


class DotfilesFS(AbstractedFS):
    show_all = False

    def listdir(self, path):
        names = super().listdir(path)
        return names if self.show_all else [name for name in names if not name.startswith(".")]


class DotfilesHandler(ListOnlyHandler):
    """Like vsftpd: LIST shows dotfiles with -a only; listing the folder "locked" is refused (550)."""

    abstracted_fs = DotfilesFS

    def pre_process_command(self, line, cmd, arg):
        if cmd == "LIST" and self.fs is not None:
            self.fs.show_all = arg.startswith("-") and "a" in arg
        return super().pre_process_command(line, cmd, arg)

    def ftp_LIST(self, path):
        if os.path.basename(path.rstrip("/")) == "locked":
            self.respond("550 Permission denied.")
            return None
        return super().ftp_LIST(path)


class NoListOptionsHandler(ListOnlyHandler):
    """A server that takes no options after LIST."""

    def pre_process_command(self, line, cmd, arg):
        if cmd == "LIST" and arg.startswith("-"):
            self.respond("501 Invalid argument.")
            return None
        return super().pre_process_command(line, cmd, arg)


class TruncatingFS(AbstractedFS):
    """Lists five names of a folder at most, as Pure-FTPd lists 10000 (its default)."""

    def format_list(self, basedir, listing, ignore_err=True):
        return super().format_list(basedir, sorted(listing)[:5], ignore_err)

    def format_mlsx(self, basedir, listing, perms, facts, ignore_err=True):
        return super().format_mlsx(basedir, sorted(listing)[:5], perms, facts, ignore_err)


class TruncatingHandler(FTPHandler):
    abstracted_fs = TruncatingFS


class NoCaseFS(AbstractedFS):
    """Finds names whatever their case, as IIS, FileZilla Server or Serv-U on NTFS do."""

    def ftp2fs(self, ftppath):
        real = super().ftp2fs(ftppath)
        found = self.root
        for part in os.path.relpath(real, self.root).split(os.sep):
            if part != ".":
                with contextlib.suppress(OSError):
                    part = next((name for name in os.listdir(found) if name.casefold() == part.casefold()), part)
                found = os.path.join(found, part)
        return found


class NoCaseHandler(FTPHandler):
    abstracted_fs = NoCaseFS


def make_certificate(folder: str) -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = datetime.datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                           critical=False)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    certfile, keyfile = os.path.join(folder, "cert.pem"), os.path.join(folder, "key.pem")
    with open(certfile, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(keyfile, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    return certfile, keyfile


class ServerMixin:
    """One server per test class; each test gets an empty folder of it as its base path."""

    server_on_this_disk = True

    handler = FTPHandler
    dtp_handler = ClosingTLSDataHandler  # with tls
    server_encoding = "utf8"
    server_perm = "elradfmwMT"
    tls = False
    driver_config: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server_root = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="hfile-ftp-"))
        attrs = {}
        if cls.tls:
            cls.certfile, keyfile = make_certificate(cls.enterClassContext(tempfile.TemporaryDirectory()))
            attrs = {"certfile": cls.certfile, "keyfile": keyfile, "ssl_context": None,
                     "tls_control_required": True, "tls_data_required": True, "dtp_handler": cls.dtp_handler}
        cls.ftpd = FTPd(cls.server_root, handler=cls.handler, encoding=cls.server_encoding, perm=cls.server_perm,
                        **attrs)
        cls.ftpd.start()
        cls.addClassCleanup(cls.ftpd.stop)

    def new_base(self) -> str:
        base = "/t-" + uuid.uuid4().hex[:12]
        os.mkdir(os.path.join(self.server_root, base[1:]))
        return base

    def config(self, **extra) -> dict:
        config = {"host": "127.0.0.1", "port": self.ftpd.port, "username": USER, "password": PASSWORD,
                  "base_path": self.new_base(), "encoding": "utf-8", "passive": True, "tls": "none",
                  "verify_tls": True, "rest": True, "max_connections": 4}
        config.update(self.driver_config)
        config.update(extra)
        return config

    def driver(self, **extra) -> FTPDriver:
        d = FTPDriver(self.config(**extra))
        self.addCleanup(d.close)
        return d

    def on_server(self, d: FTPDriver, *names: str) -> str:
        return os.path.join(self.server_root, d.config["base_path"].lstrip("/"), *names)


class FTPContractTests(ServerMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        return FTPDriver(self.config())


class FTPListOnlyContractTests(ServerMixin, DriverContract, unittest.TestCase):
    handler = ListOnlyHandler

    def make_driver(self):
        return FTPDriver(self.config())

    def test_uses_list(self):
        self.put("/a.txt", b"a")
        self.d.list("/")
        self.assertNotIn("MLST", self.d._features)


class FTPGBKContractTests(ServerMixin, DriverContract, unittest.TestCase):
    server_encoding = "gbk"
    driver_config = {"encoding": "gbk"}

    def make_driver(self):
        return FTPDriver(self.config())

    def test_unusual_names(self):
        # Overridden: GBK can't express every name of the contract (emoji); those are refused.
        for name in filter(self.allowed, UNUSUAL_NAMES):
            try:
                name.encode("gbk")
            except UnicodeEncodeError:
                with self.assertRaises(InvalidPath, msg=name):
                    self.put("/" + name, b"x")
                continue
            with self.subTest(name=name):
                self.put("/" + name, name.encode())
                self.assertIn(name, self.names("/"))
                self.assertFile("/" + name, name.encode())
                self.d.delete("/" + name)
        self.assertEqual(self.d.list("/"), [])

    def test_chinese_names_on_the_wire(self):
        self.put("/中文目录/文件.txt", b"zh")
        self.assertEqual(self.names("/"), ["中文目录"])
        self.assertTrue(os.path.isfile(self.on_server(self.d, "中文目录", "文件.txt")))


class FTPGBKRepliesContractTests(ServerMixin, DriverContract, unittest.TestCase):
    """A UTF-8 server whose replies have lines in GBK: they broke every connection (the greeting, the login),
    every folder (CWD's message) and every new file (SIZE's 550)."""

    handler = GBKRepliesHandler

    def make_driver(self):
        return FTPDriver(self.config())


class FTPActiveModeTests(ServerMixin, unittest.TestCase):
    def test_active_mode(self):
        d = self.driver(passive=False)
        d.write("/a/b.txt", io.BytesIO(b"active"), 6)
        self.assertEqual([e.name for e in d.list("/a")], ["b.txt"])
        self.assertEqual(d.open("/a/b.txt", 2, 3).read(), b"tiv")
        d.delete("/a")
        self.assertEqual(d.list("/"), [])


class Broken(io.RawIOBase):
    """Upload data whose client goes away after a while."""

    def __init__(self, good_reads=2):
        self.calls = 0
        self.good_reads = good_reads

    def read(self, n=-1):
        self.calls += 1
        if self.calls > self.good_reads:
            raise ConnectionResetError("client went away")
        return b"x" * 1000


class NoReplaceRenameTests(ServerMixin, unittest.TestCase):
    handler = NoReplaceRenameHandler

    def test_overwrite_through_delete_and_rename(self):
        d = self.driver()
        d.write("/f.txt", io.BytesIO(b"first"), 5)
        d.write("/f.txt", io.BytesIO(b"second"), 6)
        self.assertEqual(d.open("/f.txt").read(), b"second")
        self.assertEqual(os.listdir(self.on_server(d)), ["f.txt"])
        with self.assertRaises(ConnectionResetError):
            d.write("/f.txt", Broken(), 100000)
        self.assertEqual(d.open("/f.txt").read(), b"second")
        self.assertEqual(os.listdir(self.on_server(d)), ["f.txt"])


class NoRenameRightTests(ServerMixin, unittest.TestCase):
    server_perm = "elradmwMT"  # no "f": RNFR and RNTO are refused

    def test_overwrite_without_rename_right(self):
        d = self.driver()
        d.write("/f.txt", io.BytesIO(b"first"), 5)  # a new file needs no rename
        d.write("/f.txt", io.BytesIO(b"second"), 6)  # straight over the old file
        self.assertEqual(d.open("/f.txt").read(), b"second")
        self.assertEqual(os.listdir(self.on_server(d)), ["f.txt"])


class NoRestServerTests(ServerMixin, unittest.TestCase):
    handler = NoRestHandler

    def test_ranges_without_rest(self):
        d = self.driver()  # REST left on, as by default
        data = bytes(range(256)) * 1000
        d.write("/r.bin", io.BytesIO(data), len(data))
        with self.assertLogs("hfile.storage.ftp", "INFO"):
            self.assertEqual(d.open("/r.bin", 1000, 10).read(), data[1000:1010])
        self.assertEqual(d.open("/r.bin", 255990).read(), data[255990:])
        self.assertEqual(d.open("/r.bin").read(), data)


class StaleSessionTests(ServerMixin, unittest.TestCase):
    handler = StaleSessionHandler

    def test_a_connection_whose_transfers_fail_isnt_reused(self):
        d = self.driver(max_connections=1)
        for _ in range(2):
            self.assertEqual(d.list("/"), [])
        with self.assertRaises(BackendError):
            d.list("/")  # 425 after the 125: the connection went back to the pool, and every listing failed
        self.assertEqual(d.list("/"), [])  # a new connection


class DotfilesTests(ServerMixin, unittest.TestCase):
    handler = DotfilesHandler

    def test_a_refused_listing_keeps_the_dotfiles(self):
        d = self.driver()
        for name in (".env", "visible.txt"):
            d.write("/" + name, io.BytesIO(b"x"), 1)
        d.mkdir("/locked")
        self.assertEqual(sorted(e.name for e in d.list("/")), [".env", "locked", "visible.txt"])
        with self.assertRaises(DriverError):
            d.list("/locked")  # refused without -a too: the folder is the trouble, and -a stays
        self.assertEqual(sorted(e.name for e in d.list("/")), [".env", "locked", "visible.txt"])


class NoListOptionsTests(ServerMixin, unittest.TestCase):
    handler = NoListOptionsHandler

    def test_list_without_options(self):
        d = self.driver()
        d.write("/.env", io.BytesIO(b"x"), 1)
        self.assertEqual([e.name for e in d.list("/")], [".env"])
        self.assertEqual(d._list_args, "")


class TruncatedListingTests(ServerMixin, unittest.TestCase):
    handler = TruncatingHandler

    def test_deleting_a_folder_listed_in_part(self):
        d = self.driver()
        top = self.on_server(d, "big")
        os.makedirs(os.path.join(top, "sub"))
        for n in range(12):
            for folder in (top, os.path.join(top, "sub")):
                open(os.path.join(folder, f"f{n:02}"), "wb").close()
        self.assertEqual(len(d.list("/big")), 5)  # what the server shows
        d.delete("/big")  # deleted five at a time, until it's empty
        self.assertEqual(os.listdir(self.on_server(d)), [])


class NoCaseTests(ServerMixin, unittest.TestCase):
    handler = NoCaseHandler

    def test_a_rename_that_changes_only_the_case(self):
        d = self.driver()
        d.write("/readme.txt", io.BytesIO(b"text"), 4)
        d.mkdir("/docs")
        self.assertEqual(d.stat("/README.TXT").size, 4)  # the server finds it under any case
        d.rename("/readme.txt", "README.txt")
        d.rename("/docs", "Docs")
        self.assertEqual(sorted(os.listdir(self.on_server(d))), ["Docs", "README.txt"])
        self.assertEqual(d.open("/README.txt").read(), b"text")
        with self.assertRaises(AlreadyExists):  # another entry's name, in another case
            d.rename("/Docs", "readme.TXT")


class UnreadableLineTests(ServerMixin, unittest.TestCase):
    handler = UnreadableLineHandler

    def test_a_folder_isnt_deleted_in_part(self):
        """STOR-16: a LIST line that couldn't be read was left out, so deleting its folder deleted what came
        before it, then failed on the folder that wasn't empty. Now the whole tree is listed first, and nothing
        is deleted when a line of it can't be read."""
        d = self.driver()
        for path in ("/top/a.txt", "/top/inner/weird.bin", "/top/inner/z.txt"):
            d.write(path, io.BytesIO(b"x"), 1)
        with self.assertRaises(BackendError) as caught:
            d.delete("/top")
        self.assertIn("认不出", caught.exception.message)
        top = self.on_server(d, "top")
        self.assertEqual(sorted(os.listdir(top)), ["a.txt", "inner"])
        self.assertEqual(sorted(os.listdir(os.path.join(top, "inner"))), ["weird.bin", "z.txt"])
        d.delete("/top/a.txt")  # what can be read goes as before
        self.assertFalse(os.path.exists(os.path.join(top, "a.txt")))


class FTPSContractTests(ServerMixin, DriverContract, unittest.TestCase):
    handler = TLS_FTPHandler
    tls = True
    driver_config = {"tls": "explicit", "verify_tls": False}

    def make_driver(self):
        return FTPDriver(self.config())

    def test_certificate_is_checked(self):
        d = self.driver(verify_tls=True)
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("证书", str(caught.exception))
        trusted = ssl.create_default_context(cafile=self.certfile)
        with mock.patch.object(http, "ssl_context", lambda: trusted):
            d = self.driver(verify_tls=True)
            d.write("/v.txt", io.BytesIO(b"verified"), 8)
            self.assertEqual(d.open("/v.txt").read(), b"verified")

    def test_plain_client_is_refused(self):
        d = self.driver(tls="none")
        with self.assertRaises(DriverError):
            d.list("/")

    def test_old_connections_are_closed(self):
        """Many servers want FTPS data connections to resume their control connection's TLS session, which they
        forget after a few hours: an FTPS connection is used for TLS_SESSION_AGE at most."""
        d = self.driver()
        d.list("/")
        self.assertEqual(len(d._pool._idle), 1)
        with mock.patch.object(ftp, "TLS_SESSION_AGE", -1.0):
            old = self.driver()
            old.list("/")
        self.assertEqual(old._pool._idle, [])


class SilentCloseNotifyTests(ServerMixin, unittest.TestCase):
    handler = TLS_FTPHandler
    dtp_handler = SilentCloseNotifyDataHandler
    tls = True
    driver_config = {"tls": "explicit", "verify_tls": False}

    def test_uploads_dont_wait_for_the_servers_close_notify(self):
        d = self.driver()
        started = time.monotonic()
        d.write("/f.bin", io.BytesIO(b"x" * 300000), 300000)
        d.write("/f.bin", io.BytesIO(b"y" * 300000), 300000)  # through a temporary file
        self.assertLess(time.monotonic() - started, 10)  # it was TIMEOUT (30 s) an upload
        self.assertEqual(d.open("/f.bin").read(), b"y" * 300000)


class ImplicitFTPSTests(ServerMixin, unittest.TestCase):
    handler = ImplicitTLSHandler
    tls = True

    def test_implicit(self):
        d = self.driver(tls="implicit", verify_tls=False)
        d.write("/i/x.bin", io.BytesIO(b"x" * 300000), 300000)
        self.assertEqual(d.open("/i/x.bin", 299990).read(), b"x" * 10)
        self.assertEqual([e.name for e in d.list("/i")], ["x.bin"])
        stream = d.open("/i/x.bin")
        next(iter(stream))
        stream.close()
        d.delete("/i")
        self.assertEqual(d.list("/"), [])


class FTPBehaviourTests(ServerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.d = self.driver()

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def test_registered(self):
        self.assertIs(driver_class("ftp"), FTPDriver)

    def test_base_path_confines(self):
        self.put("/x.txt", b"x")
        self.assertTrue(os.path.isfile(self.on_server(self.d, "x.txt")))
        other = self.driver()
        self.assertEqual(other.list("/"), [])

    def test_missing_base_path(self):
        d = self.driver(base_path="/no-such-folder")
        with self.assertRaises(BackendError):
            d.list("/")
        with self.assertRaises(BackendError):
            d.check()

    def test_wrong_password(self):
        d = self.driver(password="wrong")
        with self.assertRaises(AuthError):
            d.list("/")

    def test_nothing_listening(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        d = self.driver(port=port)
        with self.assertRaises(BackendError):
            d.list("/")

    def test_newlines_are_refused(self):
        for bad in ["/a\r\nDELE x", "/a\nb", "/a\rb"]:
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                self.d.stat(bad)
        self.put("/ok", b"1")
        with self.assertRaises(InvalidPath):
            self.d.rename("/ok", "x\r\nRMD /")

    def test_names_that_could_be_misread(self):
        for name in filter(on_this_disk, [" lead space", "-a", "-l", "~home", "*?[x]", "a;b", "trailing."]):
            with self.subTest(name=name):
                self.put("/d/" + name, b"x")
                self.assertEqual([e.name for e in self.d.list("/d")], [name])
                self.assertEqual(self.d.open("/d/" + name).read(), b"x")
                self.d.delete("/d/" + name)

    def test_early_close_drops_connection_but_not_pool(self):
        d = self.driver(max_connections=1)
        d.write("/big", io.BytesIO(b"z" * 2000000), 2000000)
        for _ in range(5):
            stream = d.open("/big")
            next(iter(stream))
            stream.close()
            self.assertEqual(d.open("/big", 10, 5).read(), b"z" * 5)
        self.assertEqual([e.name for e in d.list("/")], ["big"])

    def test_busy_pool(self):
        d = self.driver(max_connections=1)
        d.write("/f", io.BytesIO(b"f" * 1000000), 1000000)
        stream = d.open("/f")
        d._pool._wait = 0.2
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("连接", str(caught.exception))
        stream.close()
        self.assertEqual([e.name for e in d.list("/")], ["f"])

    def test_dead_idle_connection_is_replaced(self):
        self.put("/f", b"1")
        conn = self.d._pool.acquire()
        conn.sock.close()  # the server or a NAT dropped it while idle
        self.d._pool.release(conn)
        self.d._pool._check_after = -1  # checked even in the clock's same tick (Windows: 15.6 ms)
        self.assertEqual(self.d.open("/f").read(), b"1")

    def test_keepalives(self):
        """A control connection is quiet while a transfer runs: NATs forgot it, and the transfer's reply never came."""
        self.d.list("/")
        conn = self.d._pool.acquire()
        self.addCleanup(self.d._pool.release, conn)
        self.assertTrue(conn.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        if sys.platform.startswith("linux"):
            self.assertEqual(conn.sock.getsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE), ftp.KEEPALIVE)

    def test_without_rest(self):
        d = self.driver(rest=False)
        self.assertFalse(d.can_range)
        self.assertTrue(FTPDriver.can_range)
        d.write("/r", io.BytesIO(b"0123456789"), 10)
        self.assertEqual(d.open("/r", 4, 3).read(), b"456")  # skipped, not REST

    def test_failed_upload_leaves_nothing(self):
        with self.assertRaises(ConnectionResetError):
            self.d.write("/up.bin", Broken(), 100000)
        with self.assertRaises(DriverError):
            self.d.write("/short.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual(self.d.list("/"), [])
        self.assertEqual(os.listdir(self.on_server(self.d)), [])

    def test_overwrite_writes_through_links(self):
        self.put("/real.txt", b"OLD")
        os.symlink("real.txt", self.on_server(self.d, "f.txt"))
        self.put("/f.txt", b"NEW")
        self.assertTrue(os.path.islink(self.on_server(self.d, "f.txt")))
        self.assertEqual(self.d.open("/real.txt").read(), b"NEW")
        self.assertEqual(sorted(os.listdir(self.on_server(self.d))), ["f.txt", "real.txt"])

    def test_failed_overwrite_keeps_the_old_file(self):
        self.put("/report.txt", b"the old report")
        with self.assertRaises(ConnectionResetError):
            self.d.write("/report.txt", Broken(), 100000)
        self.assertEqual(self.d.open("/report.txt").read(), b"the old report")
        self.assertEqual(os.listdir(self.on_server(self.d)), ["report.txt"])
        self.put("/report.txt", b"new")
        self.assertEqual(self.d.open("/report.txt").read(), b"new")

    def test_temporary_files(self):
        now = int(time.time())
        old = self.on_server(self.d, f"{TEMP_PREFIX}{now - 4 * 24 * 3600:x}-a")  # its name says when
        new = self.on_server(self.d, f"{TEMP_PREFIX}{now:x}-b")
        foreign = self.on_server(self.d, TEMP_PREFIX + "c" * 32)  # not one of ours: never deleted
        for file in (old, new, foreign):
            with open(file, "wb") as f:
                f.write(b"partial")
        long_ago = time.time() - 10 * 24 * 3600
        os.utime(new, (long_ago, long_ago))  # the server's clock is wrong: its times don't count
        self.assertEqual(self.d.list("/"), [])
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(foreign))
        with self.assertRaises(NotFound):
            self.d.open("/" + os.path.basename(new))
        with self.assertRaises(InvalidPath):
            self.put("/" + TEMP_PREFIX + "c", b"x")

    @linux_server
    def test_backslashes(self):
        for bad in ["/a\\..\\..\\etc", "/x\\y"]:
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                self.d.stat(bad)
        self.d.mkdir("/d")
        with open(self.on_server(self.d, "d", "back\\slash"), "wb") as f:
            f.write(b"x")
        with open(self.on_server(self.d, "d", TEMP_PREFIX + "x"), "wb") as f:
            f.write(b"x")
        self.assertEqual(self.d.list("/d"), [])
        self.d.delete("/d")  # also deletes what it doesn't show
        self.assertEqual(os.listdir(self.on_server(self.d)), [])

    @linux_server
    def test_names_of_dots_and_spaces_only(self):
        # Made outside HFile: left out (D46), the folder still lists, and deleting it deletes them too.
        self.d.mkdir("/d")
        for name in ("...", ".. "):
            with open(self.on_server(self.d, "d", name), "wb") as f:
                f.write(b"x")
        self.put("/d/ok.txt", b"ok")
        self.assertEqual([e.name for e in self.d.list("/d")], ["ok.txt"])
        self.d.delete("/d")
        self.assertEqual(os.listdir(self.on_server(self.d)), [])

    def test_base_path_left_out_of_server_answers(self):
        import ftplib

        d = self.driver(base_path="/srv/ftp/base")
        self.assertEqual(d._reply(ftplib.error_perm("550 /srv/ftp/base/x.txt: No such file")),
                         "550 /x.txt: No such file")
        self.assertEqual(d._reply(ftplib.error_perm('550 "/srv/ftp/base" is busy')), '550 "/" is busy')
        self.assertEqual(d._reply(ftplib.error_perm("550 /srv/ftp/basement")), "550 /srv/ftp/basement")

    @linux_server
    def test_delete_does_not_follow_links(self):
        self.put("/target/keep.txt", b"keep")
        self.d.mkdir("/victim")
        os.symlink("../target", self.on_server(self.d, "victim", "link"))
        os.symlink("target", self.on_server(self.d, "top-link"))
        names = {e.name: e for e in self.d.list("/victim")}
        self.assertTrue(names["link"].is_dir)  # shown as what it points to
        self.d.delete("/victim")
        self.d.delete("/top-link")
        self.assertEqual(sorted(e.name for e in self.d.list("/")), ["target"])
        self.assertEqual(self.d.open("/target/keep.txt").read(), b"keep")

    def test_list_a_file_and_missing(self):
        self.put("/f", b"1")
        with self.assertRaises(NotADirectory):
            self.d.list("/f")
        with self.assertRaises(NotFound):
            self.d.list("/missing")
        with self.assertRaises(NotADirectory):
            self.put("/f/x", b"2")

    def test_check_and_validation(self):
        self.d.check()
        self.assertEqual(FTPDriver.validate_config(self.config()), {})
        for key, value in [("host", ""), ("host", "ftp://h"), ("host", "h/p"), ("port", 0), ("port", 70000),
                           ("base_path", "/a/../b"), ("encoding", "ebcdic"), ("tls", "ssl3"),
                           ("password", "a\r\nb")]:
            with self.subTest(key=key, value=value):
                self.assertIn(key, FTPDriver.validate_config(self.config(**{key: value})))
        secret = [p.key for p in FTPDriver.params if p.secret]
        self.assertEqual(secret, ["password"])


class DataListenerTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform.startswith("linux"), "connects from 127.0.0.2")
    def test_active_mode_takes_the_servers_connection_only(self):
        listener = ftp._DataListener(socket.create_server(("127.0.0.1", 0)), "127.0.0.2")
        self.addCleanup(listener.close)
        listener.settimeout(10)
        port = listener.getsockname()[1]
        intruder = socket.create_connection(("127.0.0.1", port), timeout=10)  # first, from another address
        self.addCleanup(intruder.close)
        server = socket.create_connection(("127.0.0.1", port), timeout=10, source_address=("127.0.0.2", 0))
        self.addCleanup(server.close)
        with self.assertLogs("hfile.storage.ftp", "WARNING"):
            conn, address = listener.accept()
        self.addCleanup(conn.close)
        self.assertEqual(address[0], "127.0.0.2")
        self.assertEqual(intruder.recv(1), b"")  # hung up on


class ParserTests(unittest.TestCase):
    now = datetime.datetime(2026, 11, 8, 12, 0, tzinfo=UTC)

    def item(self, line):
        item = parse_list_line(line, self.now)
        return None if item is None else (item.name, item.kind, item.size, item.modified)

    def test_unix(self):
        cases = {
            "drwxr-xr-x   2 user     group        4096 Nov  7 22:40 folder":
                ("folder", "dir", None, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "-rw-r--r--   1 user     group     1234567 Nov  7  2025 file with spaces.txt":
                ("file with spaces.txt", "file", 1234567, datetime.datetime(2025, 11, 7, tzinfo=UTC)),
            "-rw-r--r--   1 user     group          10 Dec 31 23:59 last year":
                ("last year", "file", 10, datetime.datetime(2025, 12, 31, 23, 59, tzinfo=UTC)),
            "-rw-r--r--   1 user     group          10 Jan 01 00:00  leading space":
                (" leading space", "file", 10, datetime.datetime(2026, 1, 1, tzinfo=UTC)),
            "lrwxrwxrwx   1 user     group           7 Nov  7 22:40 link -> target":
                ("link", "link", None, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "-rw-r--r--   1 1000     1000            5 2026-11-07 22:40 iso.txt":
                ("iso.txt", "file", 5, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "-rw-r--r--+  1 user group 5 Nov 7 22:40 acl.txt":
                ("acl.txt", "file", 5, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "-rw-r--r-- 1 user group 5 11月  7 22:40 中文.txt":
                ("中文.txt", "file", 5, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "-rw-r--r-- 1 ftp ftp 5 Nov 07 2024 a -> b.txt":
                ("a -> b.txt", "file", 5, datetime.datetime(2024, 11, 7, tzinfo=UTC)),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.item(line), expected)
        for line in ["total 12", "crw-rw-rw-   1 root     root       1,   3 Nov  7 22:40 null",
                     "drwxr-xr-x   2 user group 4096 Nov  7 22:40 .", "garbage", "",
                     "-rw-r--r-- 1 u g 5 Nov 7 99999999999999999999:00 x", "-rw-r--r-- 1 u g 5 " + "x" * 5000]:
            with self.subTest(line=line[:60]):
                self.assertIsNone(self.item(line))

    def test_hostile_lines_are_cheap(self):
        lines = ["-rw-r--r-- 1 " + "a " * 2000 + "x", "-rw-r--r-- 1 " + "1 " * 2000 + "x",
                 "-rw-r--r-- " + "1 " * 1000 + "Nov " * 500]
        started = time.process_time()
        for _ in range(20):
            for line in lines:
                parse_list_line(line, self.now)
        self.assertLess(time.process_time() - started, 1.0)

    def test_windows(self):
        cases = {
            "11-07-26  10:40PM       <DIR>          folder":
                ("folder", "dir", None, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "11-07-26  09:05AM                 1234 file name.txt":
                ("file name.txt", "file", 1234, datetime.datetime(2026, 11, 7, 9, 5, tzinfo=UTC)),
            "11-07-2026  22:40       <DIR>          long year":
                ("long year", "dir", None, datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)),
            "01-02-99  12:00AM                    0 old":
                ("old", "file", 0, datetime.datetime(1999, 1, 2, 0, 0, tzinfo=UTC)),
        }
        for line, expected in cases.items():
            with self.subTest(line=line):
                self.assertEqual(self.item(line), expected)

    def test_mlsd(self):
        def parse(line):
            item = parse_mlsd_line(line)
            return None if item is None else (item.name, item.kind, item.size, item.modified)

        when = datetime.datetime(2026, 11, 7, 22, 40, tzinfo=UTC)
        self.assertEqual(parse("type=file;size=3;modify=20261107224000;perm=r; name with spaces"),
                         ("name with spaces", "file", 3, when))
        self.assertEqual(parse("type=dir;modify=20261107224000.123; sub"), ("sub", "dir", None, when))
        self.assertEqual(parse("Type=File;Size=5; caps"), ("caps", "file", 5, None))
        self.assertEqual(parse("type=file;size=1; a;b c"), ("a;b c", "file", 1, None))
        self.assertEqual(parse("type=OS.unix=slink:/x; link"), ("link", "link", None, None))
        for line in ["type=cdir; .", "type=pdir; ..", "type=OS.unix=chr-1/3; null", "no-space", "type=file;"]:
            with self.subTest(line=line):
                self.assertIsNone(parse(line))


if __name__ == "__main__":
    unittest.main()
