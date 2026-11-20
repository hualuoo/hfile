import contextlib
import io
import logging
import os
import posixpath
import socket
import stat
import tempfile
import threading
import time
import unittest
import uuid
from unittest import mock

import paramiko
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from paramiko.sftp import SFTP_FAILURE, SFTP_OK, SFTP_OP_UNSUPPORTED, SFTP_PERMISSION_DENIED

from apps.storages.drivers import driver_class, sftp as sftp_module
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotADirectory, NotFound
from apps.storages.drivers.sftp import TEMP_PREFIX, SFTPDriver, fingerprint, load_private_key
from apps.storages.tests.contract import DriverContract, linux_server, on_this_disk

logging.getLogger("paramiko").addHandler(logging.NullHandler())
logging.getLogger("paramiko").setLevel(logging.CRITICAL)
logging.getLogger("paramiko").propagate = False

USER, PASSWORD = "hfile", "s3cret-pw"


def _openssh(key, password=None):
    encryption = serialization.BestAvailableEncryption(password) if password else serialization.NoEncryption()
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH, encryption).decode()


def make_keys():
    ed = ed25519.Ed25519PrivateKey.generate()
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ecdsa = ec.generate_private_key(ec.SECP256R1())
    return {
        "ed25519-openssh-encrypted": (_openssh(ed, b"pass phrase"), "pass phrase"),
        "rsa-pkcs1": (rsa_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
                                            serialization.NoEncryption()).decode(), None),
        "ecdsa-pkcs8-encrypted": (ecdsa.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                      serialization.BestAvailableEncryption(b"pw")).decode(), "pw"),
    }


KEYS = make_keys()
HOST_ED25519 = load_private_key(_openssh(ed25519.Ed25519PrivateKey.generate()))
HOST_RSA = load_private_key(KEYS["rsa-pkcs1"][0])
HOST_ECDSA = load_private_key(_openssh(ec.generate_private_key(ec.SECP256R1())))


class Broken(io.RawIOBase):
    """Upload data whose client goes away after a while."""

    def __init__(self):
        self.calls = 0

    def read(self, n=-1):
        self.calls += 1
        if self.calls > 2:
            raise ConnectionResetError("client went away")
        return b"x" * 100000


def _get_text_bytes_safe(self):
    # A real SFTP server deals in bytes; paramiko's test server decodes paths as UTF-8 strictly.
    # Decoding with surrogateescape lets it act on names that aren't UTF-8, like OpenSSH would.
    return self.get_string().decode("utf-8", "surrogateescape")


class StubServer(paramiko.ServerInterface):
    def __init__(self, sftpd):
        self.sftpd = sftpd
        self.key_done = False

    def get_allowed_auths(self, username):
        if self.sftpd.require_both and self.key_done:
            return "password"
        return self.sftpd.auths

    def check_auth_password(self, username, password):
        self.sftpd.attempts.append(("password", username))
        if "password" not in self.sftpd.auths or (self.sftpd.require_both and not self.key_done):
            return paramiko.AUTH_FAILED
        if username == USER and password == self.sftpd.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        self.sftpd.attempts.append(("publickey", username))
        if "publickey" not in self.sftpd.auths:
            return paramiko.AUTH_FAILED
        if username == USER and any(key.asbytes() == k.asbytes() for k in self.sftpd.authorized):
            if self.sftpd.require_both:
                self.key_done = True
                return paramiko.AUTH_PARTIALLY_SUCCESSFUL
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def check_channel_request(self, kind, chanid):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED


class StubHandle(paramiko.SFTPHandle):
    short_reads = False

    def read(self, offset, length):
        if self.short_reads:
            length = min(length, 1000)  # servers may answer reads with fewer bytes
        return super().read(offset, length)

    def stat(self):
        return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))


class StubSFTP(paramiko.SFTPServerInterface):
    """An SFTP server for a folder, acting on bytes paths like OpenSSH."""

    def __init__(self, server, root, sftpd, *args, **kwargs):
        super().__init__(server, *args, **kwargs)
        self.root = os.fsencode(os.path.realpath(root))
        self.sftpd = sftpd

    def _real(self, path):
        """The file at SFTP path `path` (names between "/"), in this machine's form; with `nocase`, the names of
        the entries there whatever the case, as on Windows or macOS."""
        path = os.fsencode(path) if isinstance(path, str) else path
        parts = posixpath.normpath(b"/" + path).lstrip(b"/").split(b"/")
        if not self.sftpd.nocase:
            return os.path.join(self.root, *parts)
        found = self.root
        for part in filter(None, parts):
            with contextlib.suppress(OSError):
                part = next((name for name in os.listdir(found) if name.lower() == part.lower()), part)
            found = os.path.join(found, part)
        return found

    def _call(self, func, *args):
        try:
            result = func(*args)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return SFTP_OK if result is None else result

    def list_folder(self, path):
        def listing():
            real = self._real(path)
            out = []
            for name in os.listdir(real):
                full = os.path.join(real, name)
                info = os.lstat(full)
                if self.sftpd.readdir_follows and os.path.exists(full):
                    info = os.stat(full)  # some servers describe what a link points to
                attr = paramiko.SFTPAttributes.from_stat(info)
                attr.filename = name  # bytes, as the server has it
                out.append(attr)
            return out
        return self._call(listing)

    def stat(self, path):
        if "hang" in os.fsdecode(path):
            time.sleep(2)  # a server that doesn't answer in time
        return self._call(lambda: paramiko.SFTPAttributes.from_stat(os.stat(self._real(path))))

    def lstat(self, path):
        return self._call(lambda: paramiko.SFTPAttributes.from_stat(os.lstat(self._real(path))))

    def open(self, path, flags, attr):
        real = self._real(path)
        mode = 0o666 if attr.st_mode is None else stat.S_IMODE(attr.st_mode)  # a new file's, as OpenSSH does
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0), mode)  # Windows: no \n made \r\n
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        mode = "r+b" if flags & os.O_RDWR else "wb" if flags & os.O_WRONLY else "rb"
        handle = StubHandle(flags)
        handle.short_reads = self.sftpd.short_reads
        handle.filename = real
        handle.readfile = handle.writefile = os.fdopen(fd, mode)
        return handle

    def remove(self, path):
        return self._call(os.remove, self._real(path))

    def rename(self, oldpath, newpath):
        if not self.sftpd.renames:
            return SFTP_PERMISSION_DENIED
        if os.path.lexists(self._real(newpath)):
            return SFTP_FAILURE  # SFTP v3 rename never replaces
        return self._call(os.rename, self._real(oldpath), self._real(newpath))

    def posix_rename(self, oldpath, newpath):
        if not self.sftpd.renames:
            return SFTP_PERMISSION_DENIED
        if not self.sftpd.posix_rename:
            return SFTP_OP_UNSUPPORTED
        return self._call(os.replace, self._real(oldpath), self._real(newpath))

    def mkdir(self, path, attr):
        return self._call(os.mkdir, self._real(path))

    def rmdir(self, path):
        return self._call(os.rmdir, self._real(path))

    def chattr(self, path, attr):
        return self._call(paramiko.SFTPServer.set_file_attr, self._real(path), attr)

    def canonicalize(self, path):
        real = os.path.realpath(self._real(path))
        if real == self.root or real.startswith(os.path.join(self.root, b"")):
            return os.fsdecode(b"/" + os.path.relpath(real, self.root).replace(os.sep.encode(), b"/").lstrip(b"."))
        return os.fsdecode(path) if isinstance(path, bytes) else path


class NologinSubsystem(paramiko.SubsystemHandler):
    """An account whose login shell is nologin, on a server that runs sftp-server through the shell: text comes
    where SFTP's first packet should."""

    def start_subsystem(self, name, transport, channel):
        channel.recv(4)  # the client's first packet: the text comes as the answer to it
        channel.sendall(b"This account is currently not available.\n")
        channel.close()


class SFTPd(threading.Thread):
    """A paramiko SFTP server for `root` on 127.0.0.1 in a thread."""

    def __init__(self, root, *, host_keys=(HOST_ED25519,), password=PASSWORD, authorized=(),
                 auths="password,publickey", require_both=False, posix_rename=True, renames=True, nologin=False,
                 short_reads=False, readdir_follows=False, nocase=False):
        super().__init__(daemon=True)
        self.root = root
        self.host_keys = host_keys
        self.password = password
        self.authorized = list(authorized)
        self.auths = auths
        self.require_both = require_both
        self.posix_rename = posix_rename
        self.renames = renames
        self.nologin = nologin
        self.short_reads = short_reads
        self.readdir_follows = readdir_follows
        self.nocase = nocase
        self.attempts = []
        self.transports = []
        self.listener = socket.create_server(("127.0.0.1", 0))
        self.listener.settimeout(0.1)
        self.port = self.listener.getsockname()[1]
        self._stop_flag = threading.Event()

    def run(self):
        while not self._stop_flag.is_set():
            try:
                client, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            transport = paramiko.Transport(client)
            for key in self.host_keys:
                transport.add_server_key(key)
            if self.nologin:
                transport.set_subsystem_handler("sftp", NologinSubsystem)
            else:
                transport.set_subsystem_handler("sftp", paramiko.SFTPServer, StubSFTP, self.root, self)
            transport.start_server(event=threading.Event(), server=StubServer(self))
            self.transports.append(transport)

    def stop(self):
        self._stop_flag.set()
        self.listener.close()
        self.join(5)
        for transport in self.transports:
            transport.close()


def setUpModule():
    patcher = mock.patch.object(paramiko.Message, "get_text", _get_text_bytes_safe)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class ServerMixin:
    server_options: dict = {}
    server_on_this_disk = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server_root = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="hfile-sftp-"))
        cls.sftpd = SFTPd(cls.server_root, **cls.server_options)
        cls.sftpd.start()
        cls.addClassCleanup(cls.sftpd.stop)

    def new_base(self):
        base = "/t-" + uuid.uuid4().hex[:12]
        os.mkdir(os.path.join(self.server_root, base[1:]))
        return base

    def config(self, **extra):
        config = {"host": "127.0.0.1", "port": self.sftpd.port, "username": USER, "password": PASSWORD,
                  "private_key": "", "passphrase": "", "host_key": "", "base_path": self.new_base(),
                  "max_connections": 4}
        config.update(extra)
        return config

    def driver(self, **extra):
        changes = []
        d = SFTPDriver(self.config(**extra), on_update=changes.append)
        d.changes = changes
        self.addCleanup(d.close)
        return d

    def on_server(self, d, *names):
        return os.path.join(self.server_root, d.config["base_path"].lstrip("/"), *names)


class SFTPContractTests(ServerMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        return SFTPDriver(self.config())


class SFTPShortReadContractTests(ServerMixin, DriverContract, unittest.TestCase):
    """A server that answers reads with at most 1000 bytes: the pipelined reader asks again."""

    server_options = {"short_reads": True}

    def make_driver(self):
        return SFTPDriver(self.config())


class HostKeyTests(ServerMixin, unittest.TestCase):
    def test_trust_on_first_use(self):
        d = self.driver()
        d.list("/")
        expected = f"ssh-ed25519 {fingerprint(HOST_ED25519)}"
        self.assertEqual(d.changes, [{"host_key": expected}])
        self.assertEqual(d.config["host_key"], expected)
        d.list("/")  # more connections compare with the saved one
        self.assertEqual(len(d.changes), 1)
        again = self.driver(host_key=expected)
        again.list("/")
        self.assertEqual(again.changes, [])

    def test_fingerprint_forms(self):
        md5 = HOST_ED25519.get_fingerprint().hex()
        for saved in [fingerprint(HOST_ED25519),
                      f"256 {fingerprint(HOST_ED25519)} root@host (ED25519)",
                      fingerprint(HOST_ED25519) + "=",
                      "MD5:" + ":".join(md5[i:i + 2] for i in range(0, 32, 2))]:
            with self.subTest(saved=saved):
                self.driver(host_key=saved).list("/")

    def test_other_key_is_refused_before_login(self):
        other = load_private_key(_openssh(ed25519.Ed25519PrivateKey.generate()))
        self.sftpd.attempts.clear()
        d = self.driver(host_key=f"ssh-ed25519 {fingerprint(other)}")
        with self.assertRaises(AuthError) as caught, self.assertLogs("hfile.storage.sftp", "WARNING"):
            d.list("/")
        self.assertIn(fingerprint(HOST_ED25519), str(caught.exception))
        self.assertEqual(self.sftpd.attempts, [])  # no password went to the impostor
        self.assertEqual(d.changes, [])


class SeveralHostKeysTests(ServerMixin, unittest.TestCase):
    server_options = {"host_keys": (HOST_ED25519, HOST_RSA, HOST_ECDSA)}

    def test_saved_key_type_is_asked_for(self):
        self.driver(host_key=f"ssh-rsa {fingerprint(HOST_RSA)}").list("/")
        self.driver(host_key=f"ssh-ed25519 {fingerprint(HOST_ED25519)}").list("/")
        with self.assertRaises(AuthError), self.assertLogs("hfile.storage.sftp", "WARNING"):
            self.driver(host_key=fingerprint(HOST_RSA)).list("/")  # no type: the server shows ed25519

    def test_fingerprints_as_ssh_keygen_shows_them(self):
        """The help text says to paste what ssh-keygen -lf shows; for an ECDSA or RSA key that said someone may be
        posing as the server (paramiko asked for the ed25519 key)."""
        for key, shown in ((HOST_ED25519, "256 {} root@host (ED25519)"), (HOST_ECDSA, "256 {} root@host (ECDSA)"),
                           (HOST_RSA, "2048 {} no comment (RSA)")):
            with self.subTest(shown=shown):
                self.driver(host_key=shown.format(fingerprint(key))).list("/")


class KeyLoginTests(ServerMixin, unittest.TestCase):
    server_options = {"authorized": [load_private_key(text, pw) for text, pw in KEYS.values()]}

    def test_each_key_kind(self):
        for name, (text, passphrase) in KEYS.items():
            with self.subTest(key=name):
                d = self.driver(password="", private_key=text, passphrase=passphrase or "")
                d.write("/k.txt", io.BytesIO(name.encode()), len(name))
                self.assertEqual(d.open("/k.txt").read(), name.encode())

    def test_unknown_key(self):
        stranger = _openssh(ed25519.Ed25519PrivateKey.generate())
        with self.assertRaises(AuthError):
            self.driver(password="", private_key=stranger).list("/")
        # A key the server refuses, then the password.
        self.driver(private_key=stranger).list("/")

    def test_wrong_password(self):
        with self.assertRaises(AuthError):
            self.driver(password="wrong").list("/")


class BothRequiredTests(ServerMixin, unittest.TestCase):
    server_options = {"authorized": [load_private_key(*KEYS["rsa-pkcs1"])], "require_both": True}

    def test_key_then_password(self):
        text, _ = KEYS["rsa-pkcs1"]
        self.driver(private_key=text).list("/")
        with self.assertRaises(AuthError):
            self.driver(password="", private_key=text).list("/")


class ValidationTests(unittest.TestCase):
    def base(self, **extra):
        config = {"host": "h.example", "port": 22, "username": "u", "password": "p", "private_key": "",
                  "passphrase": "", "host_key": "", "base_path": "/", "max_connections": 8}
        config.update(extra)
        return config

    def test_valid(self):
        self.assertEqual(SFTPDriver.validate_config(self.base()), {})
        text, passphrase = KEYS["ed25519-openssh-encrypted"]
        self.assertEqual(SFTPDriver.validate_config(self.base(password="", private_key=text, passphrase=passphrase)), {})
        self.assertEqual(SFTPDriver.validate_config(self.base(host_key="ssh-rsa SHA256:" + "A" * 43)), {})

    def test_invalid(self):
        text, _ = KEYS["ed25519-openssh-encrypted"]
        cases = [
            ({"host": ""}, "host"), ({"host": "sftp://h"}, "host"), ({"port": 0}, "port"), ({"username": ""}, "username"),
            ({"password": ""}, "password"), ({"private_key": "not a key"}, "private_key"),
            ({"private_key": text}, "passphrase"), ({"private_key": text, "passphrase": "wrong"}, "passphrase"),
            ({"private_key": "PuTTY-User-Key-File-3: ssh-ed25519\n..."}, "private_key"),
            ({"host_key": "nonsense"}, "host_key"), ({"base_path": "/a/.."}, "base_path"),
            ({"private_key": ec.generate_private_key(ec.SECP256K1()).private_bytes(  # not a curve of SSH's
                serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()},
             "private_key"),
        ]
        for extra, key in cases:
            with self.subTest(extra=extra):
                self.assertIn(key, SFTPDriver.validate_config(self.base(**extra)))

    def test_params(self):
        self.assertIs(driver_class("sftp"), SFTPDriver)
        secret = sorted(p.key for p in SFTPDriver.params if p.secret)
        self.assertEqual(secret, ["passphrase", "password", "private_key"])


class SFTPBehaviourTests(ServerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.d = self.driver()

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def names(self, path):
        return sorted(e.name for e in self.d.list(path))

    def test_base_path_confines(self):
        self.put("/x.txt", b"x")
        self.assertTrue(os.path.isfile(self.on_server(self.d, "x.txt")))
        self.assertEqual(self.driver().list("/"), [])

    def test_missing_base_path(self):
        with self.assertRaises(BackendError):
            self.driver(base_path="/no-such").list("/")
        with self.assertRaises(BackendError):
            self.driver(base_path="/no-such").stat("/")

    def test_nothing_listening(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(BackendError):
            self.driver(port=port).list("/")

    def test_not_ssh(self):
        server = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)

        def answer():
            conn, _ = server.accept()
            conn.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            conn.close()

        threading.Thread(target=answer, daemon=True).start()
        with self.assertRaises(BackendError):
            self.driver(port=server.getsockname()[1]).list("/")

    def stalled_server(self) -> int:
        """The port of a server that sends an SSH banner, then nothing: the key exchange never ends."""
        server = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(server.close)
        accepted = []

        def answer():
            conn, _ = server.accept()
            accepted.append(conn)  # kept open, saying nothing
            conn.sendall(b"SSH-2.0-OpenSSH_9.9\r\n")  # a banner, then no key exchange

        threading.Thread(target=answer, daemon=True).start()
        self.addCleanup(lambda: [conn.close() for conn in accepted])
        return server.getsockname()[1]

    def test_a_handshake_that_times_out_says_so(self):
        """STOR-13: paramiko's start_client() gives up quietly when the handshake times out; the next call said
        「No existing session」, which tells nobody that the server didn't answer in time."""
        with mock.patch.object(sftp_module, "TIMEOUT", 0.5), self.assertRaises(BackendError) as caught:
            self.driver(port=self.stalled_server()).list("/")
        self.assertIn("超时", caught.exception.message)
        self.assertNotIn("No existing session", caught.exception.message)

    def test_a_handshake_that_times_out_in_paramikos_thread_says_so(self):
        """STOR-13 too: paramiko's thread times the handshake as well (handshake_timeout, the same TIMEOUT). When
        its timer runs out first, start_client() raises the thread's EOFError(), which says nothing: the message
        was 「建立连接失败：」 (one time in ten in the test above). Here start_client() always waits longer."""
        start_client = paramiko.Transport.start_client

        def waiting_longer(transport, event=None, timeout=None):
            return start_client(transport, event, timeout * 10)

        with mock.patch.object(sftp_module, "TIMEOUT", 0.5), mock.patch.object(paramiko.Transport, "start_client", waiting_longer), \
                self.assertRaises(BackendError) as caught:
            self.driver(port=self.stalled_server()).list("/")
        self.assertIn("超时", caught.exception.message)

    @linux_server
    def test_delete_does_not_follow_links(self):
        self.put("/target/keep.txt", b"keep")
        self.d.mkdir("/victim")
        os.symlink("../target", self.on_server(self.d, "victim", "link"))
        os.symlink("target", self.on_server(self.d, "top-link"))
        self.assertTrue({e.name: e for e in self.d.list("/victim")}["link"].is_dir)
        self.d.delete("/victim")
        self.d.delete("/top-link")
        self.assertEqual(self.names("/"), ["target"])
        self.assertEqual(self.d.open("/target/keep.txt").read(), b"keep")

    @linux_server
    def test_names_that_are_not_utf8(self):
        self.d.mkdir("/d")
        folder = os.fsencode(self.on_server(self.d, "d"))
        open(folder + b"/bad-\xff.txt", "wb").close()
        self.put("/d/ok.txt", b"ok")
        self.assertEqual(self.names("/d"), ["ok.txt"])
        self.d.delete("/d")  # deletes what it can't show, too
        self.assertEqual(self.names("/"), [])

    @linux_server
    def test_names_of_dots_and_spaces_only(self):
        # Made outside HFile: left out (D46), the folder still lists, and deleting it deletes them too.
        self.put("/d/ok.txt", b"ok")
        for name in ("...", ".. "):
            open(self.on_server(self.d, "d", name), "wb").close()
        os.mkdir(self.on_server(self.d, "d", "...."))
        self.assertEqual(self.names("/d"), ["ok.txt"])
        self.d.delete("/d")
        self.assertEqual(self.names("/"), [])

    @linux_server
    def test_bad_links_are_left_out(self):
        self.put("/d/ok.txt", b"ok")
        os.symlink("loop2", self.on_server(self.d, "d", "loop1"))
        os.symlink("loop1", self.on_server(self.d, "d", "loop2"))
        os.symlink("missing", self.on_server(self.d, "d", "dangling"))
        if os.geteuid() != 0:
            os.mkdir(self.on_server(self.d, "locked"))
            open(self.on_server(self.d, "locked", "x"), "wb").close()
            os.chmod(self.on_server(self.d, "locked"), 0)
            self.addCleanup(os.chmod, self.on_server(self.d, "locked"), 0o755)
            os.symlink("../locked/x", self.on_server(self.d, "d", "no-permission"))
        self.assertEqual(self.names("/d"), ["ok.txt"])

    def test_timeouts_are_not_dangling_links(self):
        with mock.patch.object(sftp_module, "TIMEOUT", 0.5):
            d = self.driver()
            d.mkdir("/d")
            os.symlink("target", self.on_server(d, "d", "link-that-hangs"))  # stat of it doesn't answer
            with self.assertRaises(BackendError) as caught:
                d.list("/d")
        self.assertIn("超时", str(caught.exception))
        self.assertEqual(d._pool._idle, [])  # that connection isn't reused

    @linux_server
    def test_backslashes(self):
        for bad in ["/a\\..\\..\\etc", "/x\\y"]:
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                self.d.stat(bad)
        self.d.mkdir("/d")
        open(self.on_server(self.d, "d", "back\\slash"), "wb").close()
        self.assertEqual(self.names("/d"), [])
        self.d.delete("/d")
        self.assertEqual(self.names("/"), [])

    def test_temp_files_hidden_and_refused(self):
        name = TEMP_PREFIX + "0" * 32  # not one of ours: never deleted
        with open(self.on_server(self.d, name), "wb") as f:
            f.write(b"partial")
        now = int(time.time())
        stale = self.on_server(self.d, f"{TEMP_PREFIX}{now - 4 * 24 * 3600:x}-abc")
        fresh = self.on_server(self.d, f"{TEMP_PREFIX}{now:x}-def")
        for file in (stale, fresh):
            with open(file, "wb") as f:
                f.write(b"upload")
        long_ago = time.time() - 10 * 24 * 3600
        os.utime(fresh, (long_ago, long_ago))  # the server's clock is wrong: its times don't count
        self.assertEqual(self.names("/"), [])
        self.assertFalse(os.path.exists(stale))
        self.assertTrue(os.path.exists(fresh))
        self.assertTrue(os.path.exists(self.on_server(self.d, name)))
        with self.assertRaises(NotFound):
            self.d.open("/" + name)
        with self.assertRaises(InvalidPath):
            self.put("/" + name, b"x")

    def test_failed_upload_leaves_nothing(self):
        class Broken(io.RawIOBase):
            def __init__(self):
                self.calls = 0

            def read(self, n=-1):
                self.calls += 1
                if self.calls > 2:
                    raise ConnectionResetError("client went away")
                return b"x" * 100000

        self.put("/keep.bin", b"old")
        with self.assertRaises(ConnectionResetError):
            self.d.write("/keep.bin", Broken(), 1000000)
        with self.assertRaises(DriverError):
            self.d.write("/short.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual(sorted(os.listdir(self.on_server(self.d))), ["keep.bin"])
        self.assertEqual(self.d.open("/keep.bin").read(), b"old")

    @linux_server
    def test_a_private_file_stays_private_while_replaced(self):
        self.put("/secret", b"old")
        os.chmod(self.on_server(self.d, "secret"), 0o600)
        folder, seen = self.on_server(self.d), []

        class Watching(io.BytesIO):
            def read(self, n=-1):  # while the upload's temporary file exists
                seen.extend(stat.S_IMODE(os.stat(os.path.join(folder, name)).st_mode)
                            for name in os.listdir(folder) if name.startswith(TEMP_PREFIX))
                return super().read(n)

        self.d.write("/secret", Watching(b"new"), 3)
        self.assertEqual(seen, [0o600])  # it was 0644 until the upload was complete
        self.assertEqual(stat.S_IMODE(os.stat(self.on_server(self.d, "secret")).st_mode), 0o600)

    def test_no_algorithm_in_common(self):
        def only_rsa(driver, transport):  # the server has an ed25519 key only
            transport.get_security_options().key_types = ("rsa-sha2-512", "rsa-sha2-256")

        with mock.patch.object(SFTPDriver, "_prefer_saved_key_type", only_rsa), self.assertRaises(BackendError) as caught:
            self.driver().list("/")
        self.assertIn("7.2", caught.exception.message)

    @linux_server
    def test_overwrite_keeps_mode_and_writes_link_targets(self):
        self.put("/f", b"1")
        os.chmod(self.on_server(self.d, "f"), 0o640)
        self.put("/f", b"2")
        self.assertEqual(stat.S_IMODE(os.stat(self.on_server(self.d, "f")).st_mode), 0o640)
        os.symlink("f", self.on_server(self.d, "link"))
        self.put("/link", b"through the link")
        self.assertTrue(os.path.islink(self.on_server(self.d, "link")))
        self.assertEqual(self.d.open("/f").read(), b"through the link")

    def test_early_close_keeps_connection_usable(self):
        d = self.driver(max_connections=1)
        d.write("/big", io.BytesIO(b"z" * 3000000), 3000000)
        conns = set()
        for _ in range(5):
            stream = d.open("/big")
            next(iter(stream))
            stream.close()
            self.assertEqual(d.open("/big", 100, 5).read(), b"z" * 5)
            conn = d._pool.acquire()
            conns.add(id(conn))
            d._pool.release(conn)
        self.assertEqual(len(conns), 1)  # the same connection all along
        self.assertEqual([e.name for e in d.list("/")], ["big"])

    def test_busy_pool(self):
        d = self.driver(max_connections=1)
        d.write("/f", io.BytesIO(b"f" * 1000000), 1000000)
        stream = d.open("/f")
        d._pool._wait = 0.2
        with self.assertRaises(BackendError):
            d.list("/")
        stream.close()
        self.assertEqual([e.name for e in d.list("/")], ["f"])

    def test_dead_idle_connection_is_replaced(self):
        self.put("/f", b"1")
        conn = self.d._pool.acquire()
        conn.transport.close()
        self.d._pool.release(conn)
        self.d._pool._check_after = -1  # checked even in the clock's same tick (Windows: 15.6 ms)
        self.assertEqual(self.d.open("/f").read(), b"1")

    def test_list_a_file_and_errors(self):
        self.put("/f", b"1")
        with self.assertRaises(NotADirectory):
            self.d.list("/f")
        with self.assertRaises(NotFound):
            self.d.list("/missing")
        with self.assertRaises(NotADirectory):
            self.put("/f/x", b"2")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/f")

    def test_odd_names(self):
        for name in filter(on_this_disk, [" lead", "-a", "~home", "*?[x]", "trailing ", "%2F"]):
            with self.subTest(name=name):
                self.put("/" + name, b"x")
                self.assertIn(name, self.names("/"))
                self.assertEqual(self.d.open("/" + name).read(), b"x")
                self.d.delete("/" + name)


class FollowingReaddirTests(ServerMixin, unittest.TestCase):
    """A server whose READDIR describes what links point to: deleting must still not go in."""

    server_options = {"readdir_follows": True}

    def test_delete_does_not_follow_links(self):
        d = self.driver()
        d.write("/keep/k.txt", io.BytesIO(b"k"), 1)
        d.mkdir("/trash")
        os.symlink("../keep", self.on_server(d, "trash", "link"))
        d.delete("/trash")
        self.assertEqual(sorted(os.listdir(self.on_server(d))), ["keep"])
        self.assertEqual(os.listdir(self.on_server(d, "keep")), ["k.txt"])


class NoRenameTests(ServerMixin, unittest.TestCase):
    """An account that may not rename, like SFTPGo's without the rename permission: nothing could be uploaded."""

    server_options = {"renames": False}

    def test_files_are_written_in_place(self):
        d = self.driver()
        d.write("/f.txt", io.BytesIO(b"first"), 5)
        d.write("/f.txt", io.BytesIO(b"second"), 6)
        self.assertEqual(d.open("/f.txt").read(), b"second")
        self.assertIs(d._renames, False)
        with self.assertRaises(ConnectionResetError):
            d.write("/new.bin", Broken(), 1000000)  # a new file that failed: deleted
        self.assertEqual(os.listdir(self.on_server(d)), ["f.txt"])  # nor is the try's temporary file left


class NoCaseTests(ServerMixin, unittest.TestCase):
    """A server on a disk that ignores case (Windows, macOS): its rename, like OpenSSH's link(), refuses a name the
    disk takes for the entry itself."""

    server_options = {"nocase": True}

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


class PublicKeyOnlyTests(ServerMixin, unittest.TestCase):
    server_options = {"auths": "publickey", "authorized": [load_private_key(*KEYS["rsa-pkcs1"])]}

    def test_a_password_says_what_the_server_wants(self):
        with self.assertRaises(AuthError) as caught:
            self.driver().list("/")  # it said: wrong username or password
        self.assertIn("publickey", caught.exception.message)
        self.driver(password="", private_key=KEYS["rsa-pkcs1"][0]).list("/")


class PasswordOnlyTests(ServerMixin, unittest.TestCase):
    server_options = {"auths": "password", "authorized": [load_private_key(*KEYS["rsa-pkcs1"])]}

    def test_a_key_says_what_the_server_wants(self):
        with self.assertRaises(AuthError) as caught:
            self.driver(password="", private_key=KEYS["rsa-pkcs1"][0]).list("/")
        self.assertIn("password", caught.exception.message)
        self.driver(private_key=KEYS["rsa-pkcs1"][0]).list("/")  # with the password as well, that one goes


class NologinTests(ServerMixin, unittest.TestCase):
    server_options = {"nologin": True}

    def test_a_login_shell_that_prints_text(self):
        with self.assertRaises(BackendError) as caught:  # it was paramiko's SFTPError, an HTTP 500
            self.driver().list("/")
        self.assertIn("internal-sftp", caught.exception.message)


class NoPosixRenameTests(ServerMixin, unittest.TestCase):
    server_options = {"posix_rename": False}

    def test_overwrite_without_posix_rename(self):
        d = self.driver()
        d.write("/f", io.BytesIO(b"first"), 5)
        d.write("/f", io.BytesIO(b"second"), 6)
        self.assertEqual(d.open("/f").read(), b"second")
        self.assertEqual(os.listdir(self.on_server(d)), ["f"])


if __name__ == "__main__":
    unittest.main()
