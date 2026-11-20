"""FTP storage, also FTPS (explicit AUTH TLS, or implicit TLS).

Paths
    The storage path "/a/b" is <base path>/a/b on the server. Every command gets the absolute path,
    so a name that starts with spaces, "-" or "~" can't be misread; paths with CR, LF or NUL are
    refused before anything is sent, and so are backslashes, which Windows servers take for
    separators ("a\\..\\.." would leave the base path). Server answers in error messages have the
    base path taken out.

    Listings use MLSD when the server has it (FEAT lists MLST), else CWD and "LIST -a", parsed in
    the Unix and the Windows (IIS, DOS) formats. stat uses MLST, else SIZE, MDTM and CWD. A listed
    symlink shows as what it points to.

Params
    host, port, username, password, base_path, encoding, passive, tls, verify_tls, rest,
    max_connections.

Connections
    Connections come from a pool of at most `max_connections`; a download keeps its connection until
    it's closed. A connection on which a transfer failed isn't used again, nor an FTPS one older
    than an hour (its TLS session may be expiring, see FTPDriver._release); control connections send
    TCP keepalives while a transfer runs. Passive mode connects the data channel to the control
    connection's address, whatever address the server's PASV reply names; active mode takes the data
    connection from that address only.

Changes
    Deleting a folder lists it with LIST, which shows symlinks as links, and deletes a link without
    going into it.

Downloads
    Downloads are RETR, with REST for ranges when that's on (a server that refuses REST gets ranges
    read from the start). A download that stops before the end of the file (a range, or the client
    going away) closes its control connection rather than reusing it in an unknown state.

Uploads
    A new file is a STOR to its name, deleted when the upload fails. An existing file is replaced
    through a hidden ".hfile-upload-<time>-<random>" file renamed over it (RNFR, RNTO; on servers
    that don't rename over a file, the new file takes the visible name "<name>.hfile-new-..." while
    the old one is deleted), so a failed upload leaves the old file as it was. A symlink (to write
    what it points to) and a file on an account that may not rename (RNFR refused) are overwritten
    in place. Temporary files never appear in listings; ones left by a crash are deleted when their
    folder is listed three days later, by the time in their name (our clock, not the server's).

Names
    Names are sent and read in the configured encoding (UTF-8, GBK, ...): a name the encoding can't
    express is refused, and listed names it can't decode are left out. Replies hold no names and are
    read with what isn't in the encoding replaced (a GBK greeting on a UTF-8 server).

Limits
    No copy (FTP has no server-side copy; can_copy is False). LIST times have minute precision and
    are read as UTC, like MLSD and MDTM times; folders' times are unknown without MLST. The largest
    file is what the server accepts. On a server without MLST whose SIZE is refused (vsftpd with
    SIZE left out of cmds_allowed) existing files look missing, and an upload over one is written in
    place. Pure-FTPd with -x (ProhibitDotFilesWrite) refuses the hidden temporary names: existing
    files can't be replaced there.
"""

from __future__ import annotations

import contextlib
import datetime
import ftplib
import logging
import math
import re
import secrets
import select
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
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

log = logging.getLogger("hfile.storage.ftp")

TIMEOUT = 30.0  # seconds, for connecting and for each read or write
KEEPALIVE = 60  # seconds a control connection is quiet before TCP keepalives start (_keep_alive)
TLS_SESSION_AGE = 3600.0  # seconds an FTPS control connection is used for (FTPDriver._release)
CHUNK = 256 * 1024
WRITE_CHUNK = 256 * 1024
MAX_LISTING = 256 * 1024 * 1024  # bytes of one folder listing
TEMP_PREFIX = ".hfile-upload-"  # uploads that replace a file go to such a hidden name first
STALE_TEMP_SECONDS = 3 * 24 * 3600  # judged by the time in the name: our clock, not the server's

ENCODINGS = (
    ("utf-8", gettext_lazy("UTF-8 (recommended)")),
    ("gbk", gettext_lazy("GBK (Simplified Chinese)")),
    ("gb18030", gettext_lazy("GB18030 (Simplified Chinese)")),
    ("big5", gettext_lazy("Big5 (Traditional Chinese)")),
    ("shift_jis", gettext_lazy("Shift_JIS (Japanese)")),
    ("euc-kr", gettext_lazy("EUC-KR (Korean)")),
    ("latin-1", gettext_lazy("ISO-8859-1 (Western European)")),
)
TLS_MODES = (
    ("none", gettext_lazy("No encryption")),
    ("explicit", gettext_lazy("Explicit FTPS (AUTH TLS, usually port 21)")),
    ("implicit", gettext_lazy("Implicit FTPS (usually port 990)")),
)

_UTC = datetime.timezone.utc

MAX_LINE = 4096  # characters of a LIST line; longer ones are left out
# A Unix line: the type and permission bits, then (links, owner and group, skipped) the first
# "size date time " in the rest, then the name. No nested repeats: a hostile line can't make the
# search backtrack for long.
_UNIX_HEAD = re.compile(r"(?P<type>[bcdelfmpSs-])(?:[r-][w-][xsStTL-]){3}[+.@]?\s+(?P<rest>.*)",
                        re.ASCII | re.DOTALL)
_UNIX_TAIL = re.compile(
    r"(?:^|\s)(?P<size>\d{1,20})\s+"
    r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2}|[A-Za-z]{3}\s+\d{1,2}|\d{1,2}\s+[A-Za-z]{3}|\d{1,2}月\s+\d{1,2}日?)\s+"
    r"(?P<time>\d{1,2}:\d{2}|\d{4}年?)\s"
    r"(?P<name>.*)",
    re.ASCII | re.DOTALL,
)
_DOS_LINE = re.compile(
    r"(?P<date>\d{1,2}-\d{1,2}-\d{2,4})\s+"
    r"(?P<time>\d{1,2}:\d{2}\s*(?:[AaPp][Mm])?)\s+"
    r"(?:(?P<dir><DIR>)|(?P<size>\d{1,20}))\s+"
    r"(?P<name>\S.*)",
    re.ASCII | re.DOTALL,
)
_MONTHS = {name: i for i, name in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --- ftplib with what the driver needs -----------------------------------------------------------

class _DataListener(socket.socket):
    """A listening socket that takes connections from one address only. In active mode the server connects to
    us; anyone else who reaches the port first would be taken for it, and read what's uploaded or send what's
    downloaded ("port stealing", RFC 2577)."""

    def __init__(self, sock: socket.socket, peer: str):
        super().__init__(sock.family, sock.type, sock.proto, sock.detach())
        self.peer = peer

    def accept(self):
        while True:
            conn, address = super().accept()
            if address[0] == self.peer:
                return conn, address
            log.warning("refused a data connection from %s: the FTP server is %s", address[0], self.peer)
            conn.close()


class _ActivePortMixin:
    """Active mode: listen on the control connection's own address, not on every interface, for the server's
    address only."""

    def makeport(self):
        host = self.sock.getsockname()[0]
        sock = _DataListener(socket.create_server((host, 0), family=self.af, backlog=1), self.sock.getpeername()[0])
        port = sock.getsockname()[1]
        if self.af == socket.AF_INET:
            self.sendport(host, port)
        else:
            self.sendeprt(host, port)
        sock.settimeout(self.timeout)
        return sock


class _LenientRepliesMixin:
    """Replies are read with what isn't in the chosen encoding replaced: a greeting, a login or folder message
    (vsftpd's .message on CWD) or an error in another encoding, such as GBK from a server whose names are UTF-8,
    is only text to show. ftplib read them strictly, and such a line broke the connection. Names never come from
    replies: listings are read from the data connection, strictly."""

    _reply_file = None

    @property
    def file(self):
        return self._reply_file

    @file.setter
    def file(self, value):  # ftplib's connect() and auth() make it, before the greeting is read
        if value is not None:
            value.reconfigure(errors="replace")
        self._reply_file = value


class _PlainFTP(_LenientRepliesMixin, _ActivePortMixin, ftplib.FTP):
    hfile_broken = False
    hfile_retire_at = math.inf


class _TLSFTP(_LenientRepliesMixin, _ActivePortMixin, ftplib.FTP_TLS):
    hfile_broken = False
    hfile_retire_at = math.inf
    implicit = False

    def connect(self, host="", port=0, timeout=-999, source_address=None):
        if not self.implicit:
            return super().connect(host, port, timeout, source_address)
        # Implicit FTPS: TLS from the first byte.
        if host:
            self.host = host
        if port > 0:
            self.port = port
        if timeout != -999:
            self.timeout = timeout
        self.sock = socket.create_connection((self.host, self.port), self.timeout, source_address=source_address)
        self.af = self.sock.family
        self.sock = self.context.wrap_socket(self.sock, server_hostname=self.host)
        self.file = self.sock.makefile("r", encoding=self.encoding)
        self.welcome = self.getresp()
        return self.welcome

    def ntransfercmd(self, cmd, rest=None):
        # Data connections reuse the control connection's TLS session: many servers require it.
        conn, size = ftplib.FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            try:
                conn = self.context.wrap_socket(conn, server_hostname=self.host, session=self.sock.session)
            except BaseException:
                conn.close()
                raise
        return conn, size


def _close_data(conn: socket.socket) -> None:
    if isinstance(conn, ssl.SSLSocket):
        with contextlib.suppress(OSError, ValueError):
            conn = conn.unwrap()
    with contextlib.suppress(OSError):
        conn.close()


def _end_upload(conn: socket.socket) -> None:
    """The end of an upload's data: TLS's close_notify (vsftpd wants one: strict_ssl_read_eof), then the end of
    the TCP stream. The server's close_notify isn't waited for: IIS and FileZilla Server send theirs only once
    the connection is closed, and unwrap() waited TIMEOUT for it on every upload; leaving without it is allowed
    (RFC 8446, 6.1). The caller closes the connection after the server's reply: closing it earlier, with TLS
    1.3's session tickets unread, resets it, which can cut off what the server hasn't read yet. Errors are left
    to that reply, which tells why when the server gave up."""
    if isinstance(conn, ssl.SSLSocket):
        conn.settimeout(0)
        deadline = time.monotonic() + TIMEOUT
        with contextlib.suppress(OSError):
            while True:
                try:
                    conn.unwrap()
                    break  # the server's came first
                except ssl.SSLWantReadError:
                    break  # ours is out
                except ssl.SSLWantWriteError:  # the socket's buffer is still full
                    if not select.select([], [conn], [], max(0.0, deadline - time.monotonic()))[1]:
                        break
    with contextlib.suppress(OSError):
        conn.shutdown(socket.SHUT_WR)


def _keep_alive(sock: socket.socket) -> None:
    """TCP keepalives on a control connection, which is quiet while a long transfer runs on its data connection:
    NATs and firewalls forget connections quiet for a few minutes (Azure's after 4), and then the transfer's
    reply never came (an upload that had arrived whole was deleted as failed)."""
    with contextlib.suppress(OSError):
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option, value in (("TCP_KEEPIDLE", KEEPALIVE), ("TCP_KEEPINTVL", KEEPALIVE), ("TCP_KEEPCNT", 4)):
            if hasattr(socket, option):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, option), value)


# --- listings ------------------------------------------------------------------------------------

@dataclass
class _Item:
    name: str
    kind: str  # file | dir | link
    size: int | None = None
    modified: datetime.datetime | None = None


def _parse_mlsx_time(value: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value[:14], "%Y%m%d%H%M%S").replace(tzinfo=_UTC)
    except ValueError:
        return None


def _parse_facts(text: str) -> dict[str, str]:
    facts = {}
    for fact in text.split(";"):
        key, eq, value = fact.partition("=")
        if eq:
            facts[key.strip().lower()] = value
    return facts


def _item_from_facts(name: str, facts: dict[str, str]) -> _Item | None:
    kind = facts.get("type", "").lower()
    if kind in ("cdir", "pdir") or name in (".", ".."):
        return None
    if kind == "dir":
        kind = "dir"
    elif kind == "file":
        kind = "file"
    elif kind.startswith(("os.unix=slink", "os.unix=symlink")):
        kind = "link"
    else:
        return None  # devices, unknown types
    size = None
    if kind == "file":
        with contextlib.suppress(ValueError):
            size = int(facts.get("size", ""))
    return _Item(name, kind, size, _parse_mlsx_time(facts.get("modify", "")))


def parse_mlsd_line(line: str) -> _Item | None:
    """'type=file;size=3;modify=20261107224000; name with spaces' (RFC 3659)."""
    facts, sep, name = line.partition(" ")
    if not sep or not name:
        return None
    return _item_from_facts(name, _parse_facts(facts))


def _month(text: str) -> int | None:
    text = text.strip().lower()
    if text.endswith("月"):
        text = text[:-1]
    if text.isascii() and text.isdigit():  # not "²" (STOR-17)
        number = int(text)
        return number if 1 <= number <= 12 else None
    return _MONTHS.get(text[:3])


def _unix_time(date: str, clock: str, now: datetime.datetime) -> datetime.datetime | None:
    try:
        if re.fullmatch(r"\d+[-/]\d+[-/]\d+", date):  # ls --time-style=long-iso
            year, month, day = (int(part) for part in re.split(r"[-/]", date))
            hour, _, minute = clock.partition(":")
            return datetime.datetime(year, month, day, int(hour), int(minute or 0), tzinfo=_UTC)
        first, second = date.split()
        if first[0].isascii() and first[0].isdigit() and not first.endswith("月"):
            day, month = int(first), _month(second)
        else:
            month, day = _month(first), int(second.rstrip("日"))
        if month is None:
            return None
        if ":" in clock:
            hour, minute = (int(part) for part in clock.split(":"))
            # No year: this year, unless that's more than a day ahead, then last year.
            value = datetime.datetime(now.year, month, day, hour, minute, tzinfo=_UTC)
            if value > now + datetime.timedelta(days=1):
                value = value.replace(year=now.year - 1)
            return value
        return datetime.datetime(int(clock.rstrip("年")), month, day, tzinfo=_UTC)
    except (ValueError, OverflowError):
        return None


def parse_list_line(line: str, now: datetime.datetime | None = None) -> _Item | None:
    """One line of a LIST answer in the Unix (ls -l) or the Windows (IIS, DOS) format."""
    if len(line) > MAX_LINE:
        return None
    now = now or datetime.datetime.now(_UTC)
    head = _UNIX_HEAD.fullmatch(line)
    match = _UNIX_TAIL.search(head["rest"]) if head else None
    if match:
        kind = {"d": "dir", "l": "link", "-": "file", "f": "file"}.get(head["type"])
        if kind is None:
            return None  # devices, FIFOs, sockets
        name = match["name"]
        if kind == "link":
            name = name.split(" -> ", 1)[0]
        if name in (".", "..") or not name:
            return None
        digits = match["size"]
        size = int(digits) if kind == "file" and digits.isascii() and digits.isdigit() else None
        return _Item(name, kind, size, _unix_time(match["date"], match["time"], now))
    match = _DOS_LINE.fullmatch(line)
    if match:
        name = match["name"]
        if name in (".", ".."):
            return None
        modified = None
        with contextlib.suppress(ValueError, OverflowError):
            month, day, year = (int(part) for part in match["date"].split("-"))
            if year < 100:
                year += 2000 if year < 70 else 1900
            clock = match["time"].replace(" ", "").upper()
            hour, minute = int(clock.split(":")[0]), int(clock.split(":")[1][:2])
            if clock.endswith("PM") and hour < 12:
                hour += 12
            elif clock.endswith("AM") and hour == 12:
                hour = 0
            modified = datetime.datetime(year, month, day, hour, minute, tzinfo=_UTC)
        if match["dir"]:
            return _Item(name, "dir", None, modified)
        return _Item(name, "file", int(match["size"]), modified)
    return None


def _unreadable() -> BackendError:
    return BackendError(gettext("A line of the FTP server's file list couldn't be read, so to be safe nothing in this folder was deleted."))


def _reply(error: BaseException) -> str:
    text = " ".join(str(error).split())
    return text[:200]


def _temp_name() -> str:
    """A hidden temporary name that carries when it was made, by our clock."""
    return f"{TEMP_PREFIX}{int(time.time()):x}-{secrets.token_hex(12)}"


def _is_stale_temp(name: str) -> bool:
    stamp, sep, _rest = name[len(TEMP_PREFIX):].partition("-")
    try:
        return bool(sep) and time.time() - int(stamp, 16) > STALE_TEMP_SECONDS
    except ValueError:
        return False


class _ReadFailed(Exception):
    """Reading the data to upload failed: the caller's error, passed on as it is."""

    def __init__(self, error: BaseException):
        super().__init__(error)
        self.error = error


# --- downloads -----------------------------------------------------------------------------------

class _Download:
    """The bytes of a RETR; owns the pooled connection until it's done or closed."""

    def __init__(self, driver: FTPDriver, ftp: ftplib.FTP, conn: socket.socket, *, skip: int,
                 size: int | None, to_end: bool):
        self._driver = driver
        self._ftp = ftp
        self._conn = conn
        self._skip = skip  # bytes to drop first (a range without REST)
        self._left = size  # None: to the end
        self._to_end = to_end
        self._done = False

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._done:
            raise StopIteration
        try:
            while self._skip > 0:
                data = self._conn.recv(min(CHUNK, self._skip))
                if not data:
                    raise BackendError(gettext("The FTP server ended the transfer early."))
                self._skip -= len(data)
            if self._left is not None and self._left <= 0:
                self._finish()
                raise StopIteration
            data = self._conn.recv(CHUNK if self._left is None else min(CHUNK, self._left))
            if not data:
                if self._left is None:
                    self._finish()
                    raise StopIteration
                raise BackendError(gettext("The FTP server ended the transfer early; the file may be cut short."))
            if self._left is not None:
                self._left -= len(data)
            return data
        except StopIteration:
            raise
        except BaseException as e:
            self._end(broken=True)
            error, _broken = self._driver._error(e)
            if error is e:
                raise
            raise error from e

    def _finish(self) -> None:
        """Everything wanted was read: the connection can go back to the pool only if the server
        finished the transfer as well."""
        if not self._to_end:
            self._end(broken=True)
            return
        if self._left is not None and self._conn.recv(1):  # the file grew meanwhile
            self._end(broken=True)
            return
        _close_data(self._conn)
        try:
            self._ftp.voidresp()
        except BaseException:
            self._end(broken=True)
            raise
        self._end(broken=False)

    def _end(self, *, broken: bool) -> None:
        if self._done:
            return
        self._done = True
        if broken:
            with contextlib.suppress(OSError):
                self._conn.close()
        self._driver._release(self._ftp, broken=broken)

    def close(self) -> None:
        self._end(broken=True)


# --- the driver ----------------------------------------------------------------------------------

class FTPDriver(StorageDriver):
    type = "ftp"
    label = "FTP"
    params = (
        Param("host", gettext_lazy("Server address"), placeholder="ftp.example.com", help=gettext_lazy("The FTP server's domain or IP, without ftp://.")),
        Param("port", gettext_lazy("Port"), kind="number", default=21, minimum=1, maximum=65535,
              help=gettext_lazy("Usually 21; 990 for implicit FTPS.")),
        Param("username", gettext_lazy("Username"), required=False, help=gettext_lazy("Leave empty to log in anonymously (anonymous).")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/", placeholder="/",
              help=gettext_lazy("The folder on the FTP server that is the storage's root, like / or /pub/files.")),
        Param("encoding", gettext_lazy("File name encoding"), kind="select", default="utf-8", options=ENCODINGS,
              help=gettext_lazy("The encoding of file names on the server, not of the files' contents. Older Chinese Windows FTP servers usually use GBK.")),
        Param("passive", gettext_lazy("Passive mode"), kind="bool", required=False, default=True,
              help=gettext_lazy("Passive mode is the usual one. Active mode needs the FTP server to connect back to "
                                "the machine this program runs on, which usually doesn't work behind a firewall or NAT.")),
        Param("tls", gettext_lazy("Encryption (FTPS)"), kind="select", default="none", options=TLS_MODES,
              help=gettext_lazy("Turn it on when the server supports FTPS; otherwise passwords and files travel in plain text.")),
        Param("verify_tls", gettext_lazy("Verify the server certificate"), kind="bool", required=False, default=True, advanced=True,
              help=gettext_lazy("Verifies the server's certificate with FTPS. It can be turned off for "
                                "a self-signed certificate, but then someone could pose as the server.")),
        Param("rest", gettext_lazy("Resumable transfers (REST)"), kind="bool", required=False, default=True, advanced=True,
              help=gettext_lazy("When on, videos can be seeked and downloads resumed or split. Turn it off for the rare servers without REST.")),
        Param("max_connections", gettext_lazy("Max connections"), kind="number", required=False, default=8, minimum=1, maximum=64,
              advanced=True,
              help=gettext_lazy("The most connections to the server at once; keep it below what the server "
                                "allows per user (or per IP). Each download in progress takes one connection.")),
    )

    can_range = True
    can_direct_download = False
    can_direct_upload = False
    can_move = True
    can_copy = False
    can_search = False
    has_folders = True

    setup_params = ("host", "port", "username", "password", "encoding", "passive", "tls")

    def __init__(self, config, *, on_update=None):
        super().__init__(config, on_update=on_update)
        c = self.config
        self._host = str(c.get("host") or "").strip()
        self._port = int(c.get("port") or 21)
        self._user = c.get("username") or ""
        self._password = c.get("password") or ""
        self._encoding = c.get("encoding") or "utf-8"
        self._passive = c.get("passive", True) is not False
        self._tls = c.get("tls") or "none"
        self._verify = c.get("verify_tls", True) is not False
        try:
            self._base = paths.clean(c.get("base_path") or "/")
        except InvalidPath:
            self._base = None
        # Ranges need REST; without it the site sends whole files only.
        self.can_range = c.get("rest", True) is not False
        self._features: dict[str, str] | None = None
        self._features_lock = threading.Lock()
        self._mlsd = True  # MLSD worked (or wasn't tried yet)
        self._no_rest = False  # the server refused REST: ranges are read from the start
        self._list_args = "-a"
        self._context: ssl.SSLContext | None = None
        self._pool: Pool[ftplib.FTP] = Pool(
            self._connect, lambda ftp: ftp.close(), size=int(c.get("max_connections") or 8),
            check=self._alive, busy=gettext('All connections to the FTP server are in use (downloads hold theirs until done). Please try again later, or raise "Max connections".'))

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
            errors["host"] = gettext("Only the domain or IP, like ftp.example.com, without ftp:// or a path.")
        port = config.get("port")
        if not isinstance(port, int) or not 1 <= port <= 65535:
            errors["port"] = gettext("The port is a number from 1 to 65535.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as e:
            errors["base_path"] = str(e)
        if config.get("encoding", "utf-8") not in dict(ENCODINGS):
            errors["encoding"] = gettext("This encoding isn't supported.")
        if config.get("tls", "none") not in dict(TLS_MODES):
            errors["tls"] = gettext("Please choose an encryption mode.")
        for key in ("username", "password"):
            if any(ch in str(config.get(key) or "") for ch in "\r\n\x00"):
                errors[key] = gettext("It can't contain line breaks.")
        return errors

    # -- connections

    def _ssl_context(self) -> ssl.SSLContext:
        if self._verify:
            return http.ssl_context()
        if self._context is None:
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            self._context = context
        return self._context

    def _connect(self) -> ftplib.FTP:
        if not self._host:
            raise BackendError(gettext("The FTP storage has no server address set."))
        if self._base is None:
            raise BackendError(gettext("The FTP storage's base path is set wrong. Please ask the administrator to check the storage source's settings."))
        if self._tls == "none":
            ftp: ftplib.FTP = _PlainFTP(encoding=self._encoding, timeout=TIMEOUT)
        else:
            ftp = _TLSFTP(context=self._ssl_context(), encoding=self._encoding, timeout=TIMEOUT)
            ftp.implicit = self._tls == "implicit"
        try:
            ftp.connect(self._host, self._port)
            _keep_alive(ftp.sock)
            ftp.login(self._user, self._password)
            if self._tls != "none":
                ftp.prot_p()
                ftp.hfile_retire_at = time.monotonic() + TLS_SESSION_AGE
            ftp.set_pasv(self._passive)
            ftp.voidcmd("TYPE I")
            features = self._learn_features(ftp)
            if self._encoding == "utf-8" and "UTF8" in features:
                with contextlib.suppress(ftplib.Error):
                    ftp.sendcmd("OPTS UTF8 ON")
            log.debug("connected to ftp %s:%s", self._host, self._port)
            return ftp
        except BaseException as e:
            with contextlib.suppress(Exception):
                ftp.close()
            if isinstance(e, ftplib.error_perm) and _reply(e)[:3] in ("530", "331", "332"):
                raise AuthError(gettext("FTP login failed: wrong username or password. (The server answered: %(reply)s)") % {"reply": _reply(e)}) from e
            if isinstance(e, ssl.SSLCertVerificationError):
                raise BackendError(gettext("The FTP server's certificate didn't pass verification. If the server uses a self-signed certificate, you can turn off \"Verify the server certificate\" in the storage source's \"More settings\". (%(verify_message)s)") % {"verify_message": e.verify_message}) from e
            if isinstance(e, ftplib.Error):
                raise BackendError(gettext("The FTP server refused the connection: %(reply)s") % {"reply": _reply(e)}) from e
            if isinstance(e, UnicodeError):
                raise BackendError(gettext("The FTP server sent text that isn't in the chosen encoding. Please check the storage source's \"File name encoding\".")) from e
            if isinstance(e, (OSError, EOFError)):
                raise BackendError(gettext("Couldn't connect to the FTP server %(host)s:%(port)s: %(error)s") % {"host": self._host, "port": self._port, "error": e}) from e
            raise

    def _learn_features(self, ftp: ftplib.FTP) -> dict[str, str]:
        with self._features_lock:
            if self._features is None:
                features = {}
                try:
                    answer = ftp.sendcmd("FEAT")
                except ftplib.Error:
                    answer = ""
                for line in answer.splitlines()[1:]:
                    if line.startswith(" "):
                        name, _, rest = line.strip().partition(" ")
                        features[name.upper()] = rest
                self._features = features
            return self._features

    @staticmethod
    def _alive(ftp: ftplib.FTP) -> bool:
        ftp.voidcmd("NOOP")
        return True

    def _reply(self, error: BaseException) -> str:
        """The server's answer for messages, with the base path left out of the paths in it."""
        text = _reply(error)
        if self._base and self._base != paths.ROOT:
            base = re.escape(self._base)
            text = re.sub(base + r"(?=/)", "", text)
            text = re.sub(base + r"(?=$|[\s\"':])", "/", text)
        return text

    def _error(self, e: BaseException) -> tuple[BaseException, bool]:
        """(the error to raise, whether the connection is broken) for an exception of ftplib."""
        if isinstance(e, DriverError):
            return e, False
        if isinstance(e, ftplib.error_perm):
            text = self._reply(e)
            code = text[:3]
            if code == "530":
                return AuthError(gettext("The FTP server wants a new login: %(text)s") % {"text": text}), True
            if code in ("552", "452"):
                return DriverError(gettext("The FTP server is out of space: %(text)s") % {"text": text}), False
            if code == "553":
                return InvalidPath(gettext("The FTP server doesn't accept this name: %(text)s") % {"text": text}), False
            return DriverError(gettext("The FTP server refused this: %(text)s") % {"text": text}), False
        if isinstance(e, ftplib.error_temp):
            text = self._reply(e)
            if text[:3] == "452":
                return DriverError(gettext("The FTP server is out of space: %(text)s") % {"text": text}), False
            return BackendError(gettext("The FTP server can't do this for now: %(text)s") % {"text": text}), text[:3] == "421"
        if isinstance(e, ftplib.Error):
            return BackendError(gettext("The FTP server's answer is wrong: %(reply)s") % {"reply": self._reply(e)}), True
        if isinstance(e, UnicodeEncodeError):
            return InvalidPath(gettext("The name has characters the chosen encoding can't represent.")), False
        if isinstance(e, UnicodeDecodeError):
            return BackendError(gettext("The FTP server sent text that isn't in the chosen encoding. "
                                        "Please check the storage source's \"File name encoding\".")), True
        if isinstance(e, ssl.SSLError):
            return BackendError(gettext("The encrypted connection to the FTP server failed: %(error)s") % {"error": e}), True
        if isinstance(e, (OSError, EOFError)):
            return BackendError(gettext("The connection to the FTP server failed: %(error)s") % {"error": e}), True
        return e, True

    @contextlib.contextmanager
    def _session(self) -> Iterator[ftplib.FTP]:
        ftp = self._pool.acquire()
        broken = False
        try:
            yield ftp
        except BaseException as e:
            error, broken = self._error(e)
            if error is e:
                raise
            raise error from e
        finally:
            self._release(ftp, broken=broken)

    def _release(self, ftp: ftplib.FTP, *, broken: bool = False) -> None:
        """Give a connection back to the pool. It's closed instead when it's broken, a transfer on it failed (see
        _read_data), or it's an FTPS one older than TLS_SESSION_AGE: data connections have to resume the control
        connection's TLS session on many servers (ProFTPD, FileZilla Server), which they forget after a few hours
        (OpenSSL's default: 2), and then they refuse every data connection of it with 425."""
        self._pool.release(ftp, broken=broken or ftp.hfile_broken or time.monotonic() > ftp.hfile_retire_at)

    # -- paths and commands

    def _remote(self, path: str, *, listed: bool = False) -> str:
        """The server's path for a storage path. Backslashes are refused: Windows servers take
        them for separators, so "a\\..\\.." could leave the base path. Temporary names are
        hidden. listed: a path made of names the server listed, which are taken as they are."""
        if not isinstance(path, str) or not paths.is_clean(path):
            raise InvalidPath(gettext("Invalid path."))
        if self._base is None:
            raise BackendError(gettext("The FTP storage's base path is set wrong. Please ask the administrator to check the storage source's settings."))
        if not listed:
            for part in paths.segments(path):
                if "\\" in part:
                    raise InvalidPath(gettext("Names in FTP storage can't contain \\."))
                if part.startswith(TEMP_PREFIX):
                    raise NotFound()
        full = paths.rebase(path, self._base)
        if any(ch in full for ch in "\r\n\x00"):
            raise InvalidPath(gettext("FTP file names can't contain line breaks."))
        try:
            full.encode(self._encoding)
        except UnicodeEncodeError:
            raise InvalidPath(gettext("The name has characters %(encoding)s can't represent.") % {"encoding": self._encoding.upper()}) from None
        return full

    def _read_data(self, ftp: ftplib.FTP, command: str) -> list[bytes]:
        conn = ftp.transfercmd(command)
        chunks, total = [], 0
        try:
            while True:
                data = conn.recv(65536)
                if not data:
                    break
                total += len(data)
                if total > MAX_LISTING:
                    ftp.hfile_broken = True
                    raise BackendError(gettext("This folder's listing is too large."))
                chunks.append(data)
        except BaseException:
            ftp.hfile_broken = True
            raise
        finally:
            _close_data(conn)
        try:
            ftp.voidresp()
        except BaseException:
            ftp.hfile_broken = True  # the transfer failed after it began (425 or 426): see _release
            raise
        return b"".join(chunks).splitlines()

    def _decode_items(self, lines: list[bytes], parse, folder: str, *, strict: bool = False) -> list[_Item]:
        """The items of a listing. A line that can't be read is left out; `strict`: refused (BackendError), for a
        folder to delete, where what isn't read isn't deleted either (STOR-16)."""
        items = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                line = raw.decode(self._encoding)
            except UnicodeDecodeError:
                log.debug("left out a listing line that isn't %s in %s: %r", self._encoding, folder, raw)
                if strict:
                    raise _unreadable() from None
                continue
            item = parse(line)
            if item is None:
                if not line.startswith("total "):
                    log.debug("couldn't read a listing line in %s: %r", folder, line)
                    if strict:
                        raise _unreadable()
                continue
            if any(ch in item.name for ch in "/\x00"):
                if strict:
                    raise _unreadable()
                continue
            items.append(item)
        return items

    @staticmethod
    def _cwd(ftp: ftplib.FTP, remote: str) -> bool:
        try:
            ftp.sendcmd("CWD " + remote)
            return True
        except ftplib.error_perm:
            return False

    def _is_dir(self, ftp: ftplib.FTP, path: str) -> bool:
        return self._cwd(ftp, self._remote(path))

    def _list_items(self, ftp: ftplib.FTP, path: str, *, need_links: bool = False) -> list[_Item]:
        """The items of a folder; NotFound or NotADirectory."""
        items = self._list_remote(ftp, self._remote(path), need_links=need_links)
        if items is None:
            raise self._missing_folder(ftp, path)
        return items

    def _list_remote(self, ftp: ftplib.FTP, remote: str, *, need_links: bool = False,
                     strict: bool = False) -> list[_Item] | None:
        """The items of the folder at a server path, all of them; None when it isn't a folder.
        need_links: use LIST, which shows symlinks as links (MLSD on some servers shows what
        they point to)."""
        features = self._learn_features(ftp)
        if self._mlsd and "MLST" in features and not need_links:
            try:
                lines = self._read_data(ftp, "MLSD " + remote)
                return self._decode_items(lines, parse_mlsd_line, remote, strict=strict)
            except ftplib.error_perm as e:
                if _reply(e)[:3] not in ("500", "502"):
                    return None
                self._mlsd = False
        if not self._cwd(ftp, remote):
            return None
        command = f"LIST {self._list_args}".strip()
        try:
            lines = self._read_data(ftp, command)
        except ftplib.error_perm:
            if not self._list_args:
                raise
            lines = self._read_data(ftp, "LIST")  # refused too when the folder is the trouble, not "-a"
            self._list_args = ""  # the server doesn't take "-a"
        return self._decode_items(lines, parse_list_line, remote, strict=strict)

    def _missing_folder(self, ftp: ftplib.FTP, path: str) -> DriverError:
        if path == paths.ROOT:
            return BackendError(gettext("The base path set doesn't exist on the FTP server. Please ask "
                                        "the administrator to check the storage source's settings."))
        entry = self._stat_or_none(ftp, path)
        return NotFound() if entry is None else NotADirectory()

    def _resolve_link(self, ftp: ftplib.FTP, path: str, item: _Item) -> _Item | None:
        """What a listed link points to: a folder if CWD works, else a file with SIZE."""
        if self._is_dir(ftp, path):
            return _Item(item.name, "dir", None, item.modified)
        size = self._size(ftp, path)
        if size is None:
            return None  # dangling
        return _Item(item.name, "file", size, self._mdtm(ftp, path) or item.modified)

    def _size(self, ftp: ftplib.FTP, path: str) -> int | None:
        try:
            answer = ftp.sendcmd("SIZE " + self._remote(path))
        except ftplib.error_perm:
            return None
        try:
            return int(answer[3:].strip())
        except ValueError:
            return None

    def _mdtm(self, ftp: ftplib.FTP, path: str) -> datetime.datetime | None:
        if "MDTM" not in self._learn_features(ftp):
            return None
        try:
            answer = ftp.sendcmd("MDTM " + self._remote(path))
        except ftplib.error_perm:
            return None
        return _parse_mlsx_time(answer[3:].strip())

    def _stat(self, ftp: ftplib.FTP, path: str) -> Entry:
        entry = self._stat_or_none(ftp, path)
        if entry is None:
            raise NotFound()
        return entry

    def _stat_or_none(self, ftp: ftplib.FTP, path: str) -> Entry | None:
        remote = self._remote(path)
        if path == paths.ROOT:
            if not self._is_dir(ftp, path):
                raise BackendError(gettext("The base path set doesn't exist on the FTP server. Please ask the administrator to check the storage source's settings."))
            return Entry(name="", path=path, is_dir=True)
        name = paths.name(path)
        if "MLST" in self._learn_features(ftp):
            try:
                answer = ftp.sendcmd("MLST " + remote)
            except ftplib.error_perm as e:
                if _reply(e)[:3] not in ("500", "502"):
                    return None
            else:
                item = None
                for line in answer.splitlines():
                    if line.startswith(" "):
                        facts, _, _rest = line[1:].partition(" ")
                        item = _item_from_facts(name, _parse_facts(facts))
                        break
                if item is not None and item.kind == "link":
                    item = self._resolve_link(ftp, path, item)
                if item is None:
                    return None
                return Entry(name=name, path=path, is_dir=item.kind == "dir", size=item.size, modified=item.modified)
        size = self._size(ftp, path)
        if size is not None:
            return Entry(name=name, path=path, is_dir=False, size=size, modified=self._mdtm(ftp, path))
        if self._is_dir(ftp, path):
            return Entry(name=name, path=path, is_dir=True)
        return None

    # -- reading

    def _drop_stale_temp(self, ftp: ftplib.FTP, path: str, item: _Item) -> None:
        """A temporary file left by an upload that crashed: delete it a few days on."""
        if item.kind == "file" and _is_stale_temp(item.name):
            with contextlib.suppress(ftplib.Error):
                ftp.sendcmd(f"DELE {self._remote(path).rstrip('/')}/{item.name}")
                log.info("deleted a temporary file left by an interrupted upload: %s", item.name)

    def list(self, path: str) -> list[Entry]:
        with self._session() as ftp:
            entries = []
            for item in self._list_items(ftp, path):
                if item.name.startswith(TEMP_PREFIX):
                    self._drop_stale_temp(ftp, path, item)
                    continue
                if "\\" in item.name or paths.dots_only(item.name):  # D46 for the last
                    log.debug("left out a name with a backslash or of dots and spaces only in %s: %r", path, item.name)
                    continue
                child = paths.join(path, item.name)
                if item.kind == "link":
                    item = self._resolve_link(ftp, child, item)
                    if item is None:
                        continue
                is_dir = item.kind == "dir"
                entries.append(Entry(name=item.name, path=child, is_dir=is_dir,
                                     size=None if is_dir else item.size, modified=item.modified))
            return entries

    def stat(self, path: str) -> Entry:
        with self._session() as ftp:
            return self._stat(ftp, path)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        ftp = self._pool.acquire()
        try:
            entry = self._stat(ftp, path)
            if entry.is_dir:
                raise NotFound()
            total = entry.size
            if total is None:
                total = self._size(ftp, path)
            offset = max(offset, 0)
            if total is not None:
                offset = min(offset, total)
                size = total - offset if length is None else max(0, min(length, total - offset))
            else:
                size = length
            if size == 0:
                self._release(ftp)
                return ReadStream([], size=0, total=total, offset=offset, modified=entry.modified)
            use_rest = offset > 0 and self.can_range and not self._no_rest
            try:
                conn, _ = ftp.ntransfercmd("RETR " + self._remote(path), offset if use_rest else None)
            except ftplib.error_perm as e:
                # RETR always exists, so "not implemented" or a syntax error here is about REST:
                # read from the start and drop the bytes before the range instead.
                if not use_rest or _reply(e)[:3] not in ("500", "501", "502", "504"):
                    raise
                log.info("ftp %s:%s doesn't take REST (%s); ranges are read from the start", self._host,
                         self._port, _reply(e))
                self._no_rest = True
                use_rest = False
                conn, _ = ftp.ntransfercmd("RETR " + self._remote(path))
        except BaseException as e:
            error, broken = self._error(e)
            self._release(ftp, broken=broken)
            if error is e:
                raise
            raise error from e
        download = _Download(self, ftp, conn, skip=0 if use_rest else offset, size=size,
                             to_end=total is None or offset + size == total)
        return ReadStream(download, size=size, total=total, offset=offset, modified=entry.modified,
                          close=download.close)

    # -- writing

    def _listed_kind_or_none(self, ftp: ftplib.FTP, path: str) -> str | None:
        name = paths.name(path)
        for item in self._list_items(ftp, paths.parent(path), need_links=True):
            if item.name == name:
                return item.kind
        return None

    def _can_replace(self, ftp: ftplib.FTP, path: str, remote: str) -> bool:
        """Whether an existing file can be replaced through a temporary file: it's no link
        (renaming would replace the link, not what it points to), and renaming is allowed. RNFR
        tells; RNTO to the same name then ends the rename without changing anything."""
        if self._listed_kind_or_none(ftp, path) == "link":
            return False
        try:
            ftp.sendcmd("RNFR " + remote)
        except ftplib.error_perm:
            return False
        with contextlib.suppress(ftplib.Error):
            ftp.sendcmd("RNTO " + remote)
        return True

    def _rename_over(self, ftp: ftplib.FTP, temp: str, remote: str, spare: str) -> None:
        """Rename the complete upload `temp` over the file `remote`."""
        ftp.sendcmd("RNFR " + temp)
        try:
            ftp.sendcmd("RNTO " + remote)
            return
        except ftplib.error_perm:
            pass  # this server doesn't rename over a file (IIS, FileZilla Server): delete it first
        # The new file gets a visible name before the old one goes: should the last rename fail,
        # it's there to be recovered (a temporary name would be swept away).
        ftp.sendcmd("RNFR " + temp)
        ftp.sendcmd("RNTO " + spare)
        try:
            ftp.sendcmd("DELE " + remote)
        except BaseException:
            with contextlib.suppress(ftplib.Error, OSError):
                ftp.sendcmd("DELE " + spare)  # the old file stays; this copy isn't needed
            raise
        try:
            ftp.sendcmd("RNFR " + spare)
            ftp.sendcmd("RNTO " + remote)
        except BaseException as e:
            spare_name = spare.rsplit("/", 1)[-1]
            log.warning("ftp %s:%s: deleted the old file but couldn't rename the upload into place; it's kept as %s",
                        self._host, self._port, spare)
            raise DriverError(gettext("The old file was deleted, but the new one couldn't take "
                                      'the original name; it is now called "%(spare_name)s".') % {"spare_name": spare_name}) from e

    @staticmethod
    def _store(ftp: ftplib.FTP, remote: str, data: BinaryIO, size: int) -> None:
        conn, _ = ftp.ntransfercmd("STOR " + remote)
        try:
            left = size
            while left > 0:
                try:
                    chunk = data.read(min(WRITE_CHUNK, left))
                except Exception as e:
                    raise _ReadFailed(e) from e
                if not chunk:
                    raise DriverError(ngettext("The uploaded data is incomplete: %(missing)s byte missing.", "The uploaded data is incomplete: %(missing)s bytes missing.", left) % {"missing": left})
                chunk = chunk[:left]
                conn.sendall(chunk)
                left -= len(chunk)
            _end_upload(conn)
            ftp.voidresp()
        except BaseException:
            ftp.hfile_broken = True
            raise
        finally:
            with contextlib.suppress(OSError):
                conn.close()

    def _delete_quietly(self, remote: str) -> None:
        """Delete what a failed upload left; best effort."""
        try:
            with self._session() as ftp:
                ftp.sendcmd("DELE " + remote)
        except Exception:  # noqa: BLE001
            log.debug("couldn't delete a partial upload", exc_info=True)

    def _makedirs(self, ftp: ftplib.FTP, folder: str) -> None:
        missing = []
        while folder != paths.ROOT and not self._is_dir(ftp, folder):
            missing.append(folder)
            folder = paths.parent(folder)
        for path in reversed(missing):
            try:
                ftp.sendcmd("MKD " + self._remote(path))
            except ftplib.error_perm:
                if self._is_dir(ftp, path):
                    continue  # made meanwhile
                if self._stat_or_none(ftp, path) is not None:
                    raise NotADirectory(gettext('"%(name)s" is a file, not a folder.') % {"name": paths.name(path)}) from None
                raise

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        """A new file is stored under its name; a failed upload of one is deleted. An existing
        file is replaced through a hidden temporary file that is renamed over it once complete,
        so a failed upload leaves the old file as it was, except where that can't work: a link
        (written through, like the other storages do) and an account that may not rename."""
        if path == paths.ROOT:
            raise AlreadyExists(gettext("This is the storage's root folder."))
        name = paths.name(path)
        if name.startswith(TEMP_PREFIX):
            raise InvalidPath(gettext("Names starting with %(prefix)s are for the program's own use. Please choose another name.") % {"prefix": TEMP_PREFIX})
        remote = self._remote(path)
        folder = self._remote(paths.parent(path)).rstrip("/")
        cleanup = None  # what a failed upload leaves behind, to delete
        try:
            with self._session() as ftp:
                entry = self._stat_or_none(ftp, path)
                if entry is not None and entry.is_dir:
                    raise AlreadyExists(gettext("There is already a folder with that name there."))
                if entry is None:
                    self._makedirs(ftp, paths.parent(path))
                    target = cleanup = remote
                elif self._can_replace(ftp, path, remote):
                    target = cleanup = f"{folder}/{_temp_name()}"
                else:
                    target = remote  # straight over the old file, as plain FTP clients do
                self._store(ftp, target, data, size)
                if target != remote:
                    self._rename_over(ftp, target, remote, f"{folder}/{name}.hfile-new-{secrets.token_hex(4)}")
                cleanup = None
        except BaseException as e:
            if cleanup is not None:
                self._delete_quietly(cleanup)
            if isinstance(e, _ReadFailed):
                raise e.error from None
            raise

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        remote = self._remote(path)
        with self._session() as ftp:
            if self._stat_or_none(ftp, path) is not None:
                raise AlreadyExists()
            self._makedirs(ftp, paths.parent(path))
            try:
                ftp.sendcmd("MKD " + remote)
            except ftplib.error_perm:
                if self._stat_or_none(ftp, path) is not None:
                    raise AlreadyExists() from None
                raise

    def _listed_kind(self, ftp: ftplib.FTP, path: str) -> str:
        """How the parent's LIST shows `path`: dir, file or link. stat follows links, so this is
        what tells a folder from a link to one before deleting everything in it."""
        kind = self._listed_kind_or_none(ftp, path)
        if kind is None:
            raise BackendError(gettext("This item couldn't be recognized in the FTP server's file list, so to be safe it wasn't deleted."))
        return kind

    def _remove_tree(self, ftp: ftplib.FTP, path: str) -> None:
        """Delete a folder and everything in it, also what listings don't show (temporary files,
        names with a backslash), with server paths made of the names the server listed. The whole
        tree is listed before anything goes: a line that couldn't be read was left out, and the
        folder deleted in part until RMD failed on the one that wasn't empty (STOR-16). Servers may
        list part of a large folder only (Pure-FTPd: 10000 names): what's left is listed and deleted
        again, as long as each time deletes something."""
        top = self._remote(path)
        while True:
            folders, files, todo = [], [], [top]
            while todo:
                folder = todo.pop()
                folders.append(folder)
                items = self._list_remote(ftp, folder, need_links=True, strict=True)
                if items is None:
                    raise BackendError(gettext("The FTP server couldn't list the folder to delete."))
                for item in items:
                    (todo if item.kind == "dir" else files).append(f"{folder.rstrip('/')}/{item.name}")
            for child in files:
                ftp.sendcmd("DELE " + child)
            removed = 0
            try:
                for folder in reversed(folders):
                    ftp.sendcmd("RMD " + folder)
                    removed += 1
                return
            except ftplib.error_perm:
                if not files and not removed:
                    raise  # nothing was deleted this time: no listing cut short keeps the folder full

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be deleted."))
        remote = self._remote(path)
        with self._session() as ftp:
            entry = self._stat(ftp, path)
            if entry.is_dir and self._listed_kind(ftp, path) == "dir":
                self._remove_tree(ftp, path)
            else:
                ftp.sendcmd("DELE " + remote)  # a file, or a link (to a folder too)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)  # InvalidPath for "/" and ".."
        with self._session() as ftp:
            self._stat(ftp, path)
            if new_name == paths.name(path):
                return
            if self._stat_or_none(ftp, target) is not None:
                # A server that ignores case (IIS, FileZilla Server, Serv-U) finds the entry itself under its new
                # spelling; its folder's listing has the names as they are.
                if (not only_case_differs(paths.name(path), new_name)
                        or any(item.name == new_name for item in self._list_items(ftp, paths.parent(path)))):
                    raise AlreadyExists()
                rename_through(lambda old, new: ftp.rename(self._remote(old), self._remote(new)), path, new_name)
                return
            ftp.rename(self._remote(path), self._remote(target))

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved into itself or a folder inside it."))
        target = paths.join(folder, paths.name(path))
        with self._session() as ftp:
            self._stat(ftp, path)
            if paths.parent(path) == folder:
                return
            if not self._stat(ftp, folder).is_dir:
                raise NotADirectory()
            if self._stat_or_none(ftp, target) is not None:
                raise AlreadyExists()
            ftp.rename(self._remote(path), self._remote(target))
