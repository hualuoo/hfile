"""SFTP storage (SSH File Transfer Protocol), with paramiko.

Paths
    The storage path "/a/b" is <base path>/a/b on the server, sent as absolute UTF-8 bytes, so
    nothing depends on the login folder. The base path is no boundary against symlinks on the
    server: they're followed like the server follows them (confine the account with the server's own
    chroot if needed); a link that leads nowhere is left out.

Login
    With a password, a private key (OpenSSH, PEM or PKCS#8; Ed25519, ECDSA or RSA; with or without a
    passphrase), or both: the key first, then the password, which also covers servers that want
    both. The password also answers keyboard-interactive prompts.

Host key
    With the fingerprint left empty, the first connection trusts the server's key and saves its
    SHA256 fingerprint (trust on first use, like ssh); afterwards a different key is refused before
    any password or key is sent. The admin can fill it in instead: what ssh-keygen -lf shows on the
    server for any of its keys.

Params
    host, port, username, password, private_key, passphrase, host_key, base_path, max_connections.

Connections
    Connections come from a pool of at most `max_connections` SSH connections. Reads and writes are
    pipelined (16 requests of 32 KiB in flight), so a long round trip doesn't slow a transfer down
    to one packet per round trip.

Changes
    Deleting a folder deletes the links in it, never what they point to: each folder is checked with
    lstat before going into it.

Downloads
    A download closed early drains its requests in flight and its connection goes back to the pool.

Uploads
    Uploads go to a hidden temporary file ".hfile-upload-<time>-<random>" next to the target and are
    then renamed over it (posix-rename@openssh.com, atomic; else delete, then rename), made with the
    old file's permission bits; a failed upload deletes it, temporary files never appear in
    listings, and ones left behind are deleted when their folder is listed three days later, by the
    time in their name (our clock, not the server's). An account that may not rename (SFTPGo without
    the rename permission, sftp-server -P rename; tried once with an empty temporary file) has its
    files written in place instead: a failed upload of a new file deletes it, of an existing one
    leaves it cut short. Writing to a symlink writes the file it points to.

Names
    Backslashes are refused (Windows servers take them for separators). Listed names that aren't
    UTF-8 or have a backslash are left out.

Limits
    No copy (SFTP v3 has none; can_copy is False); names must be UTF-8. The largest file is what the
    server accepts.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import errno
import hashlib
import hmac
import io
import logging
import re
import secrets
import socket
import stat as st
import threading
import time
from collections.abc import Iterator
from typing import BinaryIO

import paramiko
from paramiko.sftp import (
    CMD_CLOSE,
    CMD_DATA,
    CMD_HANDLE,
    CMD_NAME,
    CMD_OPEN,
    CMD_OPENDIR,
    CMD_READ,
    CMD_READDIR,
    CMD_STATUS,
    CMD_WRITE,
    SFTP_EOF,
    SFTP_FLAG_CREATE,
    SFTP_FLAG_EXCL,
    SFTP_FLAG_READ,
    SFTP_FLAG_TRUNC,
    SFTP_FLAG_WRITE,
    SFTP_NO_SUCH_FILE,
    SFTP_OK,
    SFTP_PERMISSION_DENIED,
    SFTPError,
    int64,
)
from paramiko.sftp_attr import SFTPAttributes

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import paths
from .base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
    only_case_differs,
    rename_through,
)
from .pool import Pool

log = logging.getLogger("hfile.storage.sftp")

TIMEOUT = 30.0  # seconds: connecting, the SSH handshake, each reply
KEEPALIVE = 30  # seconds between SSH keepalives on idle connections
BLOCK = 32 * 1024  # bytes per read or write request (what every server accepts)
CHUNK = 256 * 1024  # bytes per chunk of a download
WINDOW = 16  # requests in flight per transfer
WRITE_CHUNK = 256 * 1024
TEMP_PREFIX = ".hfile-upload-"
STALE_TEMP_SECONDS = 3 * 24 * 3600  # judged by the time in the name: our clock, not the server's

_FINGERPRINT = re.compile(r"(SHA256):([A-Za-z0-9+/]{43})=?|(MD5):((?:[0-9a-fA-F]{2}:){15}[0-9a-fA-F]{2})")


# --- keys and fingerprints -----------------------------------------------------------------------

def fingerprint(key: paramiko.PKey) -> str:
    """The key's SHA256 fingerprint as ssh-keygen -l shows it."""
    digest = hashlib.sha256(key.asbytes()).digest()
    return "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")


def _parse_fingerprint(text: str) -> tuple[str, str] | None:
    """('SHA256', base64) or ('MD5', hex) from what the admin pasted, e.g. ssh-keygen's
    '256 SHA256:abc... root@host (ED25519)'; None if there isn't one."""
    match = _FINGERPRINT.search(text or "")
    if match is None:
        return None
    if match.group(1):
        return "SHA256", match.group(2)
    return "MD5", match.group(4).lower()


def _matches(key: paramiko.PKey, expected: tuple[str, str]) -> bool:
    kind, value = expected
    if kind == "SHA256":
        return hmac.compare_digest(fingerprint(key), "SHA256:" + value)
    md5 = hashlib.md5(key.asbytes(), usedforsecurity=False).hexdigest()
    return hmac.compare_digest(":".join(md5[i:i + 2] for i in range(0, 32, 2)), value)


class PrivateKeyError(DriverError):
    """A private key that can't be used; `field` is the param to blame."""

    def __init__(self, message: str, field: str = "private_key"):
        super().__init__(message)
        self.field = field


def load_private_key(text: str, passphrase: str | None = None) -> paramiko.PKey:
    """A paramiko key from the text of an OpenSSH, PEM (PKCS#1, SEC1) or PKCS#8 private key."""
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

    if "PuTTY-User-Key-File" in text:
        raise PrivateKeyError(gettext("PuTTY .ppk private keys aren't supported: convert it to OpenSSH "
                        "format with PuTTYgen (Conversions → Export OpenSSH key) first."))
    data = text.strip().encode("utf-8") + b"\n"
    password = passphrase.encode("utf-8") if passphrase else None

    def load(password):
        if b"BEGIN OPENSSH PRIVATE KEY" in data:
            return serialization.load_ssh_private_key(data, password=password)
        return serialization.load_pem_private_key(data, password=password)

    try:
        try:
            key = load(password)
        except TypeError as e:
            if password is None:
                raise PrivateKeyError(gettext("This private key is password-protected. Please fill in its passphrase."), "passphrase") from e
            key = load(None)  # a passphrase for a key that has none: ignore it
    except PrivateKeyError:
        raise
    except UnsupportedAlgorithm as e:
        raise PrivateKeyError(gettext("This kind of private key isn't supported.")) from e
    except ValueError as e:
        if password is not None:  # an unencrypted key with a passphrase is TypeError, above
            raise PrivateKeyError(gettext("The passphrase is wrong, or the private key is malformed."), "passphrase") from e
        raise PrivateKeyError(gettext("The private key is malformed.")) from e
    if isinstance(key, rsa.RSAPrivateKey):
        return paramiko.RSAKey(key=key)
    if isinstance(key, ec.EllipticCurvePrivateKey):
        if key.curve.name not in ("secp256r1", "secp384r1", "secp521r1"):  # the curves SSH has (paramiko failed on others)
            raise PrivateKeyError(gettext("This kind of private key isn't supported."))
        return paramiko.ECDSAKey(vals=(key, key.public_key()))
    if isinstance(key, ed25519.Ed25519PrivateKey):
        openssh = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                                    serialization.NoEncryption()).decode("ascii")
        return paramiko.Ed25519Key(file_obj=io.StringIO(openssh))
    raise PrivateKeyError(gettext("This kind of private key isn't supported: please use an Ed25519, ECDSA or RSA key."))


# --- connections and transfers -------------------------------------------------------------------

class _Connection:
    def __init__(self, transport: paramiko.Transport, sftp: paramiko.SFTPClient):
        self.transport = transport
        self.sftp = sftp
        self.broken = False

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self.sftp.close()
        with contextlib.suppress(Exception):
            self.transport.close()


def _status(msg: paramiko.Message) -> tuple[int, str]:
    """The code and text of an SSH_FXP_STATUS reply, after the request number."""
    code = msg.get_int()
    return code, msg.get_string().decode("utf-8", "replace")


def _status_error(code: int, text: str) -> OSError:
    """The error of an SSH_FXP_STATUS reply, like paramiko's own."""
    if code == SFTP_NO_SUCH_FILE:
        return OSError(errno.ENOENT, text)
    if code == SFTP_PERMISSION_DENIED:
        return OSError(errno.EACCES, text)
    return OSError(text)


def _open_handle(sftp: paramiko.SFTPClient, remote: bytes, flags: int, mode: int | None = None) -> bytes:
    attrs = SFTPAttributes()
    if mode is not None:
        attrs.st_mode = mode  # what a new file is made with (less the server's umask)
    t, msg = sftp._request(CMD_OPEN, remote, flags, attrs)
    if t != CMD_HANDLE:
        raise SFTPError("Expected handle")
    return msg.get_binary()


class _Download:
    """Pipelined reads of [offset, offset + size) of an open handle; owns the pooled connection."""

    def __init__(self, driver: SFTPDriver, conn: _Connection, handle: bytes, offset: int, size: int):
        self._driver = driver
        self._conn = conn
        self._sftp = conn.sftp
        self._handle = handle
        self._next = offset  # where the next request starts
        self._pos = offset  # where the next chunk given out starts
        self._end = offset + size
        self._asked: dict[int, int] = {}  # offset -> length asked for
        self._pending: dict[int, int] = {}  # request number -> offset
        self._data: dict[int, bytes] = {}  # offset -> bytes received
        self._error: BaseException | None = None
        self._done = False

    # Called by paramiko's _read_response() for our requests.
    def _async_response(self, t, msg, num):
        offset = self._pending.pop(num, None)
        if offset is None or self._done:
            return
        if t == CMD_DATA:
            self._data[offset] = msg.get_string()
        elif t == CMD_STATUS:
            code, text = _status(msg)
            if code == SFTP_EOF:
                self._data[offset] = b""
            else:
                self._error = _status_error(code, text)
        else:
            self._error = SFTPError("Expected data")

    def _ask(self, offset: int, length: int) -> None:
        num = self._sftp._async_request(self, CMD_READ, self._handle, int64(offset), int(length))
        self._pending[num] = offset
        self._asked[offset] = length

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        if self._pos >= self._end:
            self._finish(broken=False)
            raise StopIteration
        try:
            pieces, length = [], 0
            while length < CHUNK and self._pos < self._end:
                piece = self._block()
                pieces.append(piece)
                length += len(piece)
            return b"".join(pieces)
        except BaseException as e:
            error, broken = self._driver._error(e)
            self._finish(broken=broken)
            if error is e:
                raise
            raise error from e

    def _block(self) -> bytes:
        """The bytes at the current position: what one request brought."""
        while len(self._pending) < WINDOW and self._next < self._end:
            length = min(BLOCK, self._end - self._next)
            self._ask(self._next, length)
            self._next += length
        while self._pos not in self._data:
            if self._error is not None:
                raise self._error
            self._sftp._read_response()
        data = self._data.pop(self._pos)
        asked = self._asked.pop(self._pos)
        if not data:
            raise BackendError(gettext("The file got shorter while being read. Please download it again."))
        data = data[:asked]
        if len(data) < asked:  # a short read: ask for the rest of that block
            self._ask(self._pos + len(data), asked - len(data))
        self._pos += len(data)
        return data

    def _finish(self, *, broken: bool) -> None:
        if self._done:
            return
        self._done = True
        if not broken:
            try:
                # Closing the handle waits for the replies still on the way, so the connection
                # is clean for the next borrower.
                self._sftp._request(CMD_CLOSE, self._handle)
            except Exception:  # noqa: BLE001
                broken = True
        self._driver._pool.release(self._conn, broken=broken or self._conn.broken)

    def close(self) -> None:
        self._finish(broken=False)


class _Upload:
    """Pipelined writes to an open handle; raises the first error the server reports."""

    def __init__(self, sftp: paramiko.SFTPClient, handle: bytes):
        self._sftp = sftp
        self._handle = handle
        self._pending: set[int] = set()
        self._error: BaseException | None = None

    def _async_response(self, t, msg, num):
        self._pending.discard(num)
        if t != CMD_STATUS:
            self._error = self._error or SFTPError("Expected status")
            return
        code, text = _status(msg)
        if code != SFTP_OK:
            self._error = self._error or _status_error(code, text)

    def write(self, offset: int, data: bytes) -> None:
        while len(self._pending) >= WINDOW:
            self._sftp._read_response()
        if self._error is not None:
            raise self._error
        self._pending.add(self._sftp._async_request(self, CMD_WRITE, self._handle, int64(offset), data))

    def flush(self) -> None:
        while self._pending:
            self._sftp._read_response()
        if self._error is not None:
            raise self._error


def _temp_name() -> str:
    """A hidden temporary name that carries when it was made, by our clock."""
    return f"{TEMP_PREFIX}{int(time.time()):x}-{secrets.token_hex(12)}"


def _is_stale_temp(name: str) -> bool:
    stamp, sep, _rest = name[len(TEMP_PREFIX):].partition("-")
    try:
        return bool(sep) and time.time() - int(stamp, 16) > STALE_TEMP_SECONDS
    except ValueError:
        return False


def _why(e: BaseException) -> str:
    if isinstance(e, TimeoutError):
        return gettext("timed out waiting for the server's answer")
    return str(e) or type(e).__name__


class _ReadFailed(Exception):
    """Reading the data to upload failed: the caller's error, passed on as it is."""

    def __init__(self, error: BaseException):
        super().__init__(error)
        self.error = error


def _entry(path: str, attrs: SFTPAttributes) -> Entry:
    is_dir = attrs.st_mode is not None and st.S_ISDIR(attrs.st_mode)
    modified = None
    if attrs.st_mtime is not None:
        modified = datetime.datetime.fromtimestamp(attrs.st_mtime, tz=datetime.timezone.utc)
    return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else attrs.st_size,
                 modified=modified)


def _kind(attrs: SFTPAttributes) -> int:
    return st.S_IFMT(attrs.st_mode) if attrs.st_mode is not None else st.S_IFREG


# --- the driver ----------------------------------------------------------------------------------

class SFTPDriver(StorageDriver):
    type = "sftp"
    label = "SFTP"
    params = (
        Param("host", gettext_lazy("Server address"), placeholder="sftp.example.com", help=gettext_lazy("The SSH server's domain or IP.")),
        Param("port", gettext_lazy("Port"), kind="number", default=22, minimum=1, maximum=65535),
        Param("username", gettext_lazy("Username")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True,
              help=gettext_lazy("Fill in the password, the private key or both; both when the server asks for both.")),
        Param("private_key", gettext_lazy("Private key"), kind="textarea", required=False, secret=True,
              placeholder="-----BEGIN OPENSSH PRIVATE KEY-----",
              help=gettext_lazy("The whole private key in OpenSSH or PEM format (Ed25519, ECDSA or "
                                "RSA). Convert PuTTY .ppk keys to OpenSSH format with PuTTYgen first.")),
        Param("passphrase", gettext_lazy("Private key passphrase"), kind="password", required=False, secret=True,
              help=gettext_lazy("Fill in when the private key is password-protected.")),
        Param("host_key", gettext_lazy("Host key fingerprint"), required=False, placeholder="ssh-ed25519 SHA256:...",
              help=gettext_lazy("Confirms that the server connected to is the real one. When empty, the first "
                   "connection notes the server's fingerprint and later connections are refused if "
                   "it changes. You can also run ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub "
                   "on the server and enter the fingerprint it shows.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/", placeholder="/home/user/files",
              help=gettext_lazy("The folder on the server that is the storage's root, as an absolute path like /home/user/files.")),
        Param("max_connections", gettext_lazy("Max connections"), kind="number", required=False, default=8, minimum=1, maximum=64,
              advanced=True,
              help=gettext_lazy("The most SSH connections to the server at once; keep it below what the server "
                                "allows (OpenSSH's MaxStartups). Each download in progress takes one connection.")),
    )

    can_range = True
    can_direct_download = False
    can_direct_upload = False
    can_move = True
    can_copy = False
    can_search = False
    has_folders = True

    setup_params = ("host", "port", "username", "password", "private_key", "passphrase")

    def __init__(self, config, *, on_update=None):
        super().__init__(config, on_update=on_update)
        c = self.config
        self._host = str(c.get("host") or "").strip()
        self._port = int(c.get("port") or 22)
        self._user = c.get("username") or ""
        self._password = c.get("password") or ""
        try:
            self._base = paths.clean(c.get("base_path") or "/")
        except InvalidPath:
            self._base = None
        self._key: paramiko.PKey | None = None
        self._key_lock = threading.Lock()
        self._host_key_lock = threading.Lock()
        self._renames: bool | None = None  # whether the account may rename files; tried with the first upload
        self._pool: Pool[_Connection] = Pool(
            self._connect, lambda conn: conn.close(), size=int(c.get("max_connections") or 8),
            check=self._alive,
            busy=gettext("All connections to the SFTP server are in use (downloads hold theirs "
                         'until done). Please try again later, or raise "Max connections".'))
        self.max_transfers = self._pool.size

    def close(self) -> None:
        self._pool.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config):
        errors = {}
        host = str(config.get("host") or "").strip()
        if not host:
            errors["host"] = gettext("Please fill in the server address.")
        elif "://" in host or any(ch in host for ch in "/@ \t\r\n\x00") or len(host) > 253:
            errors["host"] = gettext("Only the domain or IP, like sftp.example.com.")
        port = config.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            errors["port"] = gettext("The port is a number from 1 to 65535.")
        if not str(config.get("username") or "").strip():
            errors["username"] = gettext("Please fill in the username.")
        private_key = config.get("private_key") or ""
        if not config.get("password") and not private_key.strip():
            errors["password"] = gettext("Fill in at least the password or the private key.")
        if private_key.strip():
            try:
                load_private_key(private_key, config.get("passphrase") or None)
            except PrivateKeyError as e:
                errors[e.field] = str(e)
        host_key = config.get("host_key") or ""
        if host_key.strip() and _parse_fingerprint(host_key) is None:
            errors["host_key"] = gettext("The fingerprint is malformed: it should look like ssh-ed25519 SHA256:xxxx (as ssh-keygen -lf shows it).")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as e:
            errors["base_path"] = str(e)
        return errors

    # -- connections

    def _private_key(self) -> paramiko.PKey | None:
        text = self.config.get("private_key") or ""
        if not text.strip():
            return None
        with self._key_lock:
            if self._key is None:
                self._key = load_private_key(text, self.config.get("passphrase") or None)
            return self._key

    def _connect(self) -> _Connection:
        if not self._host:
            raise BackendError(gettext("The SFTP storage has no server address set."))
        if self._base is None:
            raise BackendError(gettext("The SFTP storage's base path is set wrong. Please ask the administrator to check the storage source's settings."))
        key = self._private_key()
        try:
            sock = socket.create_connection((self._host, self._port), timeout=TIMEOUT)
        except OSError as e:
            raise BackendError(gettext("Couldn't connect to the SFTP server %(host)s:%(port)s: %(error)s") % {"host": self._host, "port": self._port, "error": e}) from e
        transport = paramiko.Transport(sock)
        try:
            transport.banner_timeout = transport.handshake_timeout = transport.auth_timeout = TIMEOUT
            self._prefer_saved_key_type(transport)
            try:
                transport.start_client(timeout=TIMEOUT)
            except (EOFError, paramiko.SSHException):
                # The transport's thread times the handshake too (handshake_timeout): when its timer runs out first,
                # start_client() raises that thread's EOFError(), which says nothing. A timeout all the same.
                if not transport.packetizer.handshake_timed_out():
                    raise
            if not transport.initial_kex_done:
                # start_client() gives up quietly when time is up; the next call said "No existing session" (STOR-13).
                raise BackendError(ngettext("The SFTP server %(host)s:%(port)s didn't finish the handshake within %(seconds)s second (timed out). Please check the address and port, or try again later.", "The SFTP server %(host)s:%(port)s didn't finish the handshake within %(seconds)s seconds (timed out). Please check the address and port, or try again later.", TIMEOUT) % {"host": self._host, "port": self._port, "seconds": format(TIMEOUT, "g")})
            self._check_host_key(transport.get_remote_server_key())
            self._login(transport, key)
            try:
                sftp = paramiko.SFTPClient.from_transport(transport)
            except (SFTPError, EOFError, OSError, paramiko.SSHException) as e:
                # A login shell that prints text spoils SFTP's first packet ("Garbage packet received"), or has
                # ended the channel already ("Socket is closed").
                raise BackendError(gettext("The SFTP server %(host)s:%(port)s accepted the login, but SFTP didn't start (%(error)s). The account may not be allowed SFTP, or its login shell prints text (nologin, or an echo in .bashrc) where SFTP's data should be; OpenSSH's \"Subsystem sftp internal-sftp\" doesn't go through the shell.") % {"host": self._host, "port": self._port, "error": _why(e)}) from e
            sftp.get_channel().settimeout(TIMEOUT)
            transport.set_keepalive(KEEPALIVE)
            log.debug("connected to sftp %s:%s", self._host, self._port)
            return _Connection(transport, sftp)
        except BaseException as e:
            transport.close()
            if isinstance(e, DriverError):
                raise
            if isinstance(e, paramiko.IncompatiblePeer):
                raise BackendError(gettext("The SFTP server %(host)s:%(port)s and this program have no encryption algorithm in common (%(error)s). Servers that offer only old ones, such as ssh-rsa and diffie-hellman-group14-sha1 with SHA-1 (OpenSSH before 7.2), need updating.") % {"host": self._host, "port": self._port, "error": e}) from e
            if isinstance(e, (paramiko.SSHException, EOFError, OSError)):
                raise BackendError(gettext("Setting up the connection to the SFTP server %(host)s:%(port)s failed: %(error)s") % {"host": self._host, "port": self._port, "error": e}) from e
            raise

    def _alive(self, conn: _Connection) -> bool:
        """A round trip on an idle connection: dead peers and NATs that forgot it show up here."""
        return conn.transport.is_active() and conn.sftp.stat(self._base.encode("utf-8")) is not None

    def _prefer_saved_key_type(self, transport: paramiko.Transport) -> None:
        """Ask first for the kind of host key whose fingerprint was saved, so that a server with
        several keys shows the one we know: named first ("ssh-rsa SHA256:..."), or last as ssh-keygen -l
        shows it ("256 SHA256:... root@host (ECDSA)", the size first)."""
        saved = (self.config.get("host_key") or "").strip()
        words = saved.split()
        if not words or _parse_fingerprint(words[0]) is not None:
            return
        keygen = re.fullmatch(r"(\d+) .* \((ED25519|ECDSA|RSA)\)", saved)
        if keygen:
            bits, kind = keygen.groups()
            wanted = {"ED25519": ("ssh-ed25519",), "ECDSA": (f"ecdsa-sha2-nistp{bits}",),
                      "RSA": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")}[kind]
        else:
            wanted = {"ssh-rsa": ("rsa-sha2-512", "rsa-sha2-256", "ssh-rsa")}.get(words[0], (words[0],))
        options = transport.get_security_options()
        current = list(options.key_types)
        first = [name for name in wanted if name in current]
        if first:
            options.key_types = tuple(first + [name for name in current if name not in first])

    def _check_host_key(self, key: paramiko.PKey) -> None:
        shown = f"{key.get_name()} {fingerprint(key)}"
        with self._host_key_lock:
            saved = (self.config.get("host_key") or "").strip()
            if not saved:
                self.update_config({"host_key": shown})
                log.info("saved the host key of sftp %s:%s: %s", self._host, self._port, shown)
                return
        expected = _parse_fingerprint(saved)
        if expected is None:
            raise AuthError(gettext("The host key fingerprint in the storage source's settings is malformed. Please ask the administrator to check it."))
        if not _matches(key, expected):
            log.warning("sftp %s:%s has another host key: %s", self._host, self._port, shown)
            raise AuthError(gettext("The SFTP server's host key doesn't match the saved fingerprint; the server's is "
                                    "now %(fingerprint)s. Someone may be posing as the server; if the server really "
                                    "changed its key, the administrator should check it and put the new fingerprint in "
                                    "the storage source's settings (or clear it to note it on the next connection).") % {"fingerprint": shown})

    def _login(self, transport: paramiko.Transport, key: paramiko.PKey | None) -> None:
        if key is not None:
            try:
                transport.auth_publickey(self._user, key)
            except paramiko.BadAuthenticationType as e:  # the server doesn't take keys: the password may do
                if not self._password:
                    raise AuthError(gettext("SFTP login failed: the server doesn't accept private keys (it accepts: %(methods)s). Please fill in the password.") % {"methods": ", ".join(e.allowed_types)}) from e
            except paramiko.AuthenticationException as e:
                if not self._password:
                    raise AuthError(gettext("SFTP login failed: the server doesn't accept this private key.")) from e
            if transport.is_authenticated():
                return
        if self._password:
            try:
                transport.auth_password(self._user, self._password)
            except paramiko.BadAuthenticationType as e:
                raise AuthError(gettext("SFTP login failed: the server doesn't accept passwords (it accepts: %(methods)s). Please fill in a private key.") % {"methods": ", ".join(e.allowed_types)}) from e
            except paramiko.AuthenticationException as e:
                raise AuthError(gettext("SFTP login failed: wrong username or password.")) from e
            if transport.is_authenticated():
                return
        raise AuthError(gettext("SFTP login failed: wrong username, password or private key."))

    def _error(self, e: BaseException) -> tuple[BaseException, bool]:
        """(the error to raise, whether the connection is broken) for an exception of paramiko."""
        if isinstance(e, DriverError):
            return e, False
        if isinstance(e, (socket.timeout, TimeoutError, ConnectionError, EOFError, paramiko.SSHException)):
            return BackendError(gettext("The connection to the SFTP server failed: %(reason)s") % {"reason": _why(e)}), True
        if isinstance(e, OSError):
            if e.errno == errno.ENOENT:
                return NotFound(), False
            if e.errno == errno.EACCES:
                return DriverError(gettext("The file permissions on the SFTP server don't allow this.")), False
            if e.errno is None:  # a failure the server reported
                text = " ".join(str(e).split())[:200]
                return DriverError(gettext("The SFTP server refused this: %(detail)s") % {"detail": text or gettext("Failed")}), False
            return BackendError(gettext("The connection to the SFTP server failed: %(reason)s") % {"reason": _why(e)}), True
        if isinstance(e, SFTPError):
            return BackendError(gettext("The SFTP server's answer is wrong: %(error)s") % {"error": e}), True
        if isinstance(e, UnicodeError):
            return BackendError(gettext("The SFTP server sent a path that isn't UTF-8.")), False
        return e, True

    @contextlib.contextmanager
    def _session(self) -> Iterator[paramiko.SFTPClient]:
        conn = self._pool.acquire()
        broken = False
        try:
            yield conn.sftp
        except BaseException as e:
            error, broken = self._error(e)
            if error is e:
                raise
            raise error from e
        finally:
            self._pool.release(conn, broken=broken or conn.broken)

    # -- paths

    def _remote(self, path: str) -> bytes:
        if not isinstance(path, str) or not paths.is_clean(path):
            raise InvalidPath(gettext("Invalid path."))
        if self._base is None:
            raise BackendError(gettext("The SFTP storage's base path is set wrong. Please ask the administrator to check the storage source's settings."))
        for part in paths.segments(path):
            if "\\" in part:  # a separator on Windows servers: "a\\..\\.." could leave the base path
                raise InvalidPath(gettext("Names in SFTP storage can't contain \\."))
            if part.startswith(TEMP_PREFIX):
                raise NotFound()  # hidden from listings, so hidden from everything
        try:
            return paths.rebase(path, self._base).encode("utf-8")
        except UnicodeEncodeError:
            raise InvalidPath(gettext("The name has characters that can't be used.")) from None

    @staticmethod
    def _stat_or_none(sftp, remote: bytes, *, follow: bool = True) -> SFTPAttributes | None:
        try:
            return sftp.stat(remote) if follow else sftp.lstat(remote)
        except OSError as e:
            if e.errno == errno.ENOENT:
                return None
            raise

    @staticmethod
    def _listdir(sftp, remote: bytes) -> list[tuple[bytes, str | None, SFTPAttributes]]:
        """(raw name, name or None when it isn't UTF-8, lstat attributes) of a folder's entries."""
        t, msg = sftp._request(CMD_OPENDIR, remote)
        if t != CMD_HANDLE:
            raise SFTPError("Expected handle")
        handle = msg.get_binary()
        items = []
        try:
            while True:
                try:
                    t, msg = sftp._request(CMD_READDIR, handle)
                except EOFError:
                    break
                if t != CMD_NAME:
                    raise SFTPError("Expected name response")
                for _ in range(msg.get_int()):
                    raw = msg.get_string()
                    msg.get_string()  # the "longname" (ls -l text)
                    attrs = SFTPAttributes._from_msg(msg)
                    if raw in (b".", b"..") or b"/" in raw or b"\x00" in raw:
                        continue
                    try:
                        name = raw.decode("utf-8")
                    except UnicodeDecodeError:
                        name = None
                    items.append((raw, name, attrs))
        finally:
            sftp._request(CMD_CLOSE, handle)
        return items

    # -- reading

    @staticmethod
    def _drop_stale_temp(sftp, remote: bytes, name: str, attrs: SFTPAttributes) -> None:
        """A temporary file left by an upload that never finished: delete it a few days on."""
        if _kind(attrs) == st.S_IFREG and _is_stale_temp(name):
            with contextlib.suppress(OSError):
                sftp.remove(remote)
                log.info("deleted a temporary file left by an interrupted upload: %r", remote)

    def list(self, path: str) -> list[Entry]:
        remote = self._remote(path)
        with self._session() as sftp:
            try:
                items = self._listdir(sftp, remote)
            except OSError:
                attrs = self._stat_or_none(sftp, remote)
                if attrs is None:
                    if path == paths.ROOT:
                        raise BackendError(gettext("The base path set doesn't exist on the SFTP server. Please ask the administrator to check the storage source's settings.")) from None
                    raise NotFound() from None
                if _kind(attrs) != st.S_IFDIR:
                    raise NotADirectory() from None
                raise
            entries = []
            for raw, name, attrs in items:
                child = remote.rstrip(b"/") + b"/" + raw
                if name is None or "\\" in name or paths.dots_only(name):  # D46 for the last
                    log.debug("left out a name that isn't UTF-8, has a backslash or is only dots and spaces in %s: %r",
                              path, raw)
                    continue
                if name.startswith(TEMP_PREFIX):
                    self._drop_stale_temp(sftp, child, name, attrs)
                    continue
                if _kind(attrs) == st.S_IFLNK:
                    try:
                        attrs = sftp.stat(child)
                    except OSError as e:
                        if isinstance(e, (TimeoutError, ConnectionError)) or e.errno not in (None, errno.ENOENT,
                                                                                             errno.EACCES):
                            raise  # the connection, not the link
                        log.debug("left out a link that leads nowhere in %s: %s (%s)", path, name, e)
                        continue
                if _kind(attrs) in (st.S_IFDIR, st.S_IFREG):
                    entries.append(_entry(paths.join(path, name), attrs))
            return entries

    def stat(self, path: str) -> Entry:
        remote = self._remote(path)
        with self._session() as sftp:
            attrs = self._stat_or_none(sftp, remote)
            if attrs is None:
                if path == paths.ROOT:
                    raise BackendError(gettext("The base path set doesn't exist on the SFTP server. Please ask the administrator to check the storage source's settings."))
                raise NotFound()
        if _kind(attrs) not in (st.S_IFDIR, st.S_IFREG):
            raise NotFound()
        return _entry(path, attrs)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        remote = self._remote(path)
        conn = self._pool.acquire()
        try:
            attrs = conn.sftp.stat(remote)
            if _kind(attrs) != st.S_IFREG:
                raise NotFound()
            total = attrs.st_size or 0
            offset = min(max(offset, 0), total)
            size = total - offset if length is None else max(0, min(length, total - offset))
            modified = _entry(path, attrs).modified
            if size == 0:
                self._pool.release(conn)
                return ReadStream([], size=0, total=total, offset=offset, modified=modified)
            handle = _open_handle(conn.sftp, remote, SFTP_FLAG_READ)
        except BaseException as e:
            error, broken = self._error(e)
            self._pool.release(conn, broken=broken)
            if error is e:
                raise
            raise error from e
        download = _Download(self, conn, handle, offset, size)
        return ReadStream(download, size=size, total=total, offset=offset, modified=modified, close=download.close)

    # -- writing

    def _replace(self, sftp, temp: bytes, target: bytes) -> None:
        try:
            sftp.posix_rename(temp, target)  # OpenSSH's extension: an atomic replace
            return
        except OSError as e:
            if e.errno is not None:
                raise
        # No posix-rename: plain rename doesn't replace, so delete the old file first.
        try:
            sftp.rename(temp, target)
            return
        except OSError:
            if self._stat_or_none(sftp, target, follow=False) is None:
                raise
        sftp.remove(target)
        sftp.rename(temp, target)

    def _may_rename(self, sftp, folder: bytes) -> bool:
        """Whether this account may rename files, tried once with an empty temporary file in `folder`. SFTPGo's
        users without the rename permission and OpenSSH's sftp-server with -P rename may not, and then nothing
        could be uploaded: their files are written in place instead."""
        if self._renames is None:
            first, second = (folder + b"/" + _temp_name().encode() for _ in range(2))
            sftp._request(CMD_CLOSE, _open_handle(sftp, first, SFTP_FLAG_WRITE | SFTP_FLAG_CREATE | SFTP_FLAG_EXCL))
            try:
                self._replace(sftp, first, second)
                left, self._renames = second, True
            except OSError as e:
                if isinstance(e, (TimeoutError, ConnectionError)) or e.errno not in (None, errno.EACCES):
                    raise
                log.info("sftp %s:%s doesn't let this account rename files (%s); they're written in place",
                         self._host, self._port, e)
                left, self._renames = first, False
            with contextlib.suppress(OSError):
                sftp.remove(left)
        return self._renames

    @staticmethod
    def _upload(sftp, remote: bytes, data: BinaryIO, size: int, flag: int, mode: int | None = None) -> None:
        handle = _open_handle(sftp, remote, SFTP_FLAG_WRITE | SFTP_FLAG_CREATE | flag, mode)
        upload = _Upload(sftp, handle)
        try:
            offset, left = 0, size
            while left > 0:
                try:
                    chunk = data.read(min(WRITE_CHUNK, left))
                except Exception as e:
                    raise _ReadFailed(e) from e
                if not chunk:
                    raise DriverError(ngettext("The uploaded data is incomplete: %(missing)s byte missing.", "The uploaded data is incomplete: %(missing)s bytes missing.", left) % {"missing": left})
                chunk = chunk[:left]
                for start in range(0, len(chunk), BLOCK):
                    piece = chunk[start:start + BLOCK]
                    upload.write(offset, piece)
                    offset += len(piece)
                left -= len(chunk)
            upload.flush()
        except BaseException:
            with contextlib.suppress(Exception):
                sftp._request(CMD_CLOSE, handle)
            raise
        sftp._request(CMD_CLOSE, handle)

    def _remove_quietly(self, remote: bytes) -> None:
        try:
            with self._session() as sftp:
                sftp.remove(remote)
        except Exception:  # noqa: BLE001
            log.debug("couldn't delete a temporary upload file", exc_info=True)

    def _makedirs(self, sftp, folder: str) -> None:
        missing = []
        while folder != paths.ROOT:
            attrs = self._stat_or_none(sftp, self._remote(folder))
            if attrs is not None:
                if _kind(attrs) != st.S_IFDIR:
                    raise NotADirectory(gettext('"%(name)s" is a file, not a folder.') % {"name": paths.name(folder)})
                break
            missing.append(folder)
            folder = paths.parent(folder)
        for path in reversed(missing):
            remote = self._remote(path)
            try:
                sftp.mkdir(remote)
            except OSError:
                attrs = self._stat_or_none(sftp, remote)
                if attrs is None or _kind(attrs) != st.S_IFDIR:
                    raise

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists(gettext("This is the storage's root folder."))
        name = paths.name(path)
        if name.startswith(TEMP_PREFIX):
            raise InvalidPath(gettext("Names starting with %(prefix)s are for the program's own use. Please choose another name.") % {"prefix": TEMP_PREFIX})
        remote = self._remote(path)
        cleanup = None  # what a failed upload leaves behind, to delete
        try:
            with self._session() as sftp:
                attrs = self._stat_or_none(sftp, remote)
                if attrs is not None and _kind(attrs) == st.S_IFDIR:
                    raise AlreadyExists(gettext("There is already a folder with that name there."))
                target = remote
                if attrs is None:
                    self._makedirs(sftp, paths.parent(path))
                elif _kind(sftp.lstat(remote)) == st.S_IFLNK:
                    target = sftp.normalize(remote).encode("utf-8")  # write what the link points to
                folder = target.rsplit(b"/", 1)[0]
                if self._may_rename(sftp, folder):
                    mode = None  # a new file's is the server's choice
                    if attrs is not None and _kind(attrs) == st.S_IFREG and attrs.st_mode is not None:
                        mode = st.S_IMODE(attrs.st_mode) & 0o777  # the old file's, from the start: it may be private
                    cleanup = folder + b"/" + _temp_name().encode()
                    self._upload(sftp, cleanup, data, size, SFTP_FLAG_EXCL, mode)
                    if mode is not None:  # the server's umask may have taken bits off at the open
                        with contextlib.suppress(OSError):
                            sftp.chmod(cleanup, mode)
                    self._replace(sftp, cleanup, target)
                else:  # in place, as plain SFTP clients write: a failed upload leaves an old file cut short
                    cleanup = target if attrs is None else None
                    self._upload(sftp, target, data, size, SFTP_FLAG_TRUNC)
                cleanup = None
        except BaseException as e:
            if cleanup is not None:
                self._remove_quietly(cleanup)
            if isinstance(e, _ReadFailed):
                raise e.error from None
            raise

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        if paths.name(path).startswith(TEMP_PREFIX):
            raise InvalidPath(gettext("Names starting with %(prefix)s are for the program's own use. Please choose another name.") % {"prefix": TEMP_PREFIX})
        remote = self._remote(path)
        with self._session() as sftp:
            if self._stat_or_none(sftp, remote, follow=False) is not None:
                raise AlreadyExists()
            self._makedirs(sftp, paths.parent(path))
            try:
                sftp.mkdir(remote)
            except OSError:
                if self._stat_or_none(sftp, remote, follow=False) is not None:
                    raise AlreadyExists() from None
                raise

    def _remove_tree(self, sftp, remote: bytes) -> None:
        """Delete a folder and everything in it, never going into a symlinked folder: each folder
        is checked with lstat before going in, whatever READDIR said (servers needn't report
        links as links there)."""
        folders, todo = [], [remote]
        while todo:
            folder = todo.pop()
            folders.append(folder)
            for raw, _name, attrs in self._listdir(sftp, folder):
                child = folder.rstrip(b"/") + b"/" + raw
                if _kind(attrs) == st.S_IFDIR and _kind(sftp.lstat(child)) == st.S_IFDIR:
                    todo.append(child)
                else:
                    sftp.remove(child)
        for folder in reversed(folders):
            sftp.rmdir(folder)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be deleted."))
        remote = self._remote(path)
        with self._session() as sftp:
            attrs = sftp.lstat(remote)
            if _kind(attrs) == st.S_IFDIR:
                self._remove_tree(sftp, remote)
            else:
                sftp.remove(remote)  # a file, or a link: its target stays

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)  # InvalidPath for "/" and ".."
        if new_name.startswith(TEMP_PREFIX):
            raise InvalidPath(gettext("Names starting with %(prefix)s are for the program's own use. Please choose another name.") % {"prefix": TEMP_PREFIX})
        remote, remote_target = self._remote(path), self._remote(target)
        with self._session() as sftp:
            sftp.lstat(remote)
            if new_name == paths.name(path):
                return
            if self._stat_or_none(sftp, remote_target, follow=False) is not None:
                # A server on a file system that ignores case (Windows, macOS) finds the entry itself under its new
                # spelling; its folder's listing has the names as they are.
                if (not only_case_differs(paths.name(path), new_name)
                        or any(name == new_name for _raw, name, _attrs in
                               self._listdir(sftp, self._remote(paths.parent(path))))):
                    raise AlreadyExists()
                rename_through(lambda old, new: sftp.rename(self._remote(old), self._remote(new)), path, new_name)
                return
            sftp.rename(remote, remote_target)

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved into itself or a folder inside it."))
        target = paths.join(folder, paths.name(path))
        remote, remote_target = self._remote(path), self._remote(target)
        with self._session() as sftp:
            sftp.lstat(remote)
            if paths.parent(path) == folder:
                return
            dest = sftp.stat(self._remote(folder))
            if _kind(dest) != st.S_IFDIR:
                raise NotADirectory()
            if self._stat_or_none(sftp, remote_target, follow=False) is not None:
                raise AlreadyExists()
            sftp.rename(remote, remote_target)
