"""SMB (Windows file sharing, and the shares of NAS boxes and Samba), through smbprotocol's smbclient
(https://github.com/jborean93/smbprotocol, SMB 2 and 3; written against smbprotocol 1.17.0).

Paths
    The storage root ("/") is the folder `base_path` of the share `share` on `host`: "/a/b" is
    \\\\host\\share\\base\\a\\b. `host` is a name or an IP address; an IPv6 address may be written in brackets,
    which are left out when connecting. A listing is what the server listed (smbclient's smb_info), each entry not
    asked about again; a link is followed as any path is, and one smbclient can't follow (to a local path of the
    server, or to another share) is left out.

Login
    With `username` (DOMAIN\\user, or user@domain, for a domain account) and `password`, by NTLM or Kerberos
    (whatever the server offers; smbprotocol's "negotiate"); messages are signed. Without a username, as the
    server's guest account, unsigned, as guests can't sign (Windows 10 and later refuse guests by default).
    `encrypt` asks for SMB 3's encryption of everything sent; guests can't have it.

Connections
    One connection per storage source (smbclient's connection cache of this instance), opened when it is first
    needed and used by every thread at once (smbprotocol sends their requests over it); close() ends it. When the
    server has closed it (a restart, an idle reset), the session is registered again as at first: smbclient would
    connect again by itself with its own defaults (signing required, no encryption, a guest's empty password taken
    for none), and every call gets the same settings for that. Connecting has connect_timeout seconds; a failed
    connection is remembered for failure_ttl seconds, during which requests fail at once instead of each waiting for
    the server. Once connected, smbprotocol waits for answers until its keep-alives fail (about 20 minutes).

Changes
    Copies of files are made by the server itself (SMB's server-side copy, when the server has it). A folder is
    copied by walking it, making its folders and copying its files so; not with smbclient's copytree, which asks
    for each folder's times without the storage's settings. Times aren't kept, as with a single file, and links to
    folders are left out, as they may lead back up. Renames and moves are SMB's rename, through rename_information()
    below. Shares ignore case, so a folder can't be moved or copied into itself in any spelling.

Downloads
    open() lets others read, write and delete the file meanwhile, as a local disk does: a long download doesn't
    lock it, and a document open in Word can be downloaded.

Uploads
    A file is written to a hidden temporary file in its folder, then put in place (SMB's rename that replaces),
    so a failed upload doesn't leave half a file. The writer sends its last 64 KiB when closed. A file open
    elsewhere can't be replaced (MS-FSCC: "the target file was open and ReplaceIfExists is nonzero").

Names
    Names can't have \\ : * ? " < > | in them (Windows' rules, which most shares follow; Samba refuses those
    characters too unless told otherwise).
"""

from __future__ import annotations

import errno
import ipaddress
import logging
import re
import secrets
import stat as stat_module
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import as_int, parse_time, usable_name
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
)

CHUNK = 1024 * 1024
# NT statuses of something that isn't there: STATUS_OBJECT_NAME_NOT_FOUND, STATUS_OBJECT_PATH_NOT_FOUND,
# STATUS_NO_SUCH_FILE, STATUS_NOT_FOUND, STATUS_DELETE_PENDING (deleted, until the last handle closes).
_MISSING = frozenset({0xC0000034, 0xC000003A, 0xC000000F, 0xC0000225, 0xC0000056})
FILE_ATTRIBUTE_DIRECTORY = 0x10
# Logons a server refuses for the state of the account (NT statuses, MS-ERREF), and why.
_LOGON_REFUSED = {
    0xC0000071: gettext_lazy("its password has expired"),  # STATUS_PASSWORD_EXPIRED
    0xC0000224: gettext_lazy("its password must be changed first"),  # STATUS_PASSWORD_MUST_CHANGE
    0xC0000072: gettext_lazy("it is disabled"),  # STATUS_ACCOUNT_DISABLED
    0xC0000193: gettext_lazy("it has expired"),  # STATUS_ACCOUNT_EXPIRED
    0xC0000234: gettext_lazy("it is locked out after too many wrong passwords"),  # STATUS_ACCOUNT_LOCKED_OUT
    0xC000006E: gettext_lazy("a restriction of the account forbids this logon"),  # STATUS_ACCOUNT_RESTRICTION
    0xC000006F: gettext_lazy("it may not log on at this time"),  # STATUS_INVALID_LOGON_HOURS
    0xC0000070: gettext_lazy("it may not log on from this computer"),  # STATUS_INVALID_WORKSTATION
    0xC000015B: gettext_lazy("it may not log on over the network"),  # STATUS_LOGON_TYPE_NOT_GRANTED
}
_NAME = re.compile(r"[A-Za-z0-9._-]+")  # a server's name, or its IPv4 address


def _client() -> Any:
    import smbclient  # noqa: PLC0415 - only for storages of this type (drivers/__init__.py)
    import smbclient.shutil  # noqa: F401, PLC0415

    return smbclient


def _status_text(status: int) -> str:
    """An NT status for a message: "STATUS_INVALID_PARAMETER, 0xC000000D", or the number alone."""
    from smbprotocol.header import NtStatus  # noqa: PLC0415 - imported with smbclient

    name = next((key for key, value in vars(NtStatus).items() if key.startswith("STATUS_") and value == status), None)
    return f"{name}, 0x{status:08X}" if name else f"0x{status:08X}"


def _server(value: str) -> str | None:
    """The server as smbclient takes it: its name or IPv4 address, or an IPv6 address without brackets (smbprotocol
    connects to the name as it is); None when it isn't one of these."""
    inner = value[1:-1] if value.startswith("[") and value.endswith("]") else value
    try:
        ipaddress.IPv6Address(inner)
    except ValueError:
        return value if _NAME.fullmatch(value) else None
    return inner


def rename_information(source: str, target: str, *, replace: bool, **kwargs: Any) -> None:
    """Rename `source` to `target` (UNC paths of one share), replacing a file there when `replace`: smbclient's
    rename() and replace() (its _rename_information, smbprotocol 1.17.0) with one change. They first open the target, to
    follow a DFS referral to it and learn its path in the share, asking for FILE_EXECUTE; Samba refuses that on a file
    the account may not execute (acl allow execute always = no, its default; OpenMediaVault makes every file 0664), so
    a file couldn't be replaced or have the case of its name changed. Here the target is opened for
    FILE_READ_ATTRIBUTES, which Samba never checks; the rest is as smbclient does it."""
    import ntpath  # noqa: PLC0415 - smbclient's, imported with it

    from smbclient._io import SMBFileTransaction, SMBRawIO, set_info  # noqa: PLC0415
    from smbprotocol.exceptions import SMBOSError  # noqa: PLC0415
    from smbprotocol.file_info import FileRenameInformation  # noqa: PLC0415
    from smbprotocol.open import CreateOptions, FilePipePrinterAccessMask  # noqa: PLC0415

    args = {**kwargs, "mode": "r", "share_access": "rwd", "create_options": CreateOptions.FILE_OPEN_REPARSE_POINT}
    target_raw = SMBRawIO(target, desired_access=FilePipePrinterAccessMask.FILE_READ_ATTRIBUTES, **args)
    try:
        SMBFileTransaction(target_raw).commit()
    except SMBOSError as error:
        if error.errno != errno.ENOENT:
            raise
    with SMBRawIO(source, desired_access=FilePipePrinterAccessMask.DELETE, **args) as source_raw:
        shares = {ntpath.normpath(raw.fd.tree_connect.share_name).split("\\")[-1].lower()
                  for raw in (source_raw, target_raw)}
        if source_raw.fd.connection.server_guid != target_raw.fd.connection.server_guid or len(shares) > 1:
            raise ValueError("Cannot rename a file to a different root than the source.")
        tree, name = target_raw.fd.tree_connect, target_raw.fd.file_name
        if tree.is_dfs_share and name.startswith(tree.share_name[2:]):  # the path in the share, not the DFS name
            name = name[len(tree.share_name) - 1:]
        with SMBFileTransaction(source_raw) as transaction:
            information = FileRenameInformation()
            information["replace_if_exists"] = replace
            information["file_name"] = name
            set_info(transaction, information)


class SMBDriver(StorageDriver):
    type = "smb"
    label = gettext_lazy("SMB (Windows share)")
    params = (
        Param("host", gettext_lazy("Server"), placeholder="nas.local",
              help=gettext_lazy("The server's name or IP address.")),
        Param("share", gettext_lazy("Shared folder"), placeholder="public",
              help=gettext_lazy("The name of the shared folder on that server.")),
        Param("username", gettext_lazy("Username"), required=False,
              help=gettext_lazy("DOMAIN\\user or user@domain for a domain account. Empty: the server's guest account.")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True),
        Param("port", gettext_lazy("Port"), kind="number", required=False, default=445, minimum=1, maximum=65535, advanced=True),
        Param("encrypt", gettext_lazy("Encrypt"), kind="bool", required=False, default=False, advanced=True,
              help=gettext_lazy("Ask for SMB 3's encryption of everything sent (the server must have it).")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the share that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = False
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    forbidden_chars = '\\:*?"<>|'

    setup_params = ("host", "share", "username", "password")

    # smbclient, or what the tests put in its place.
    client: ClassVar[Any] = None
    # Seconds: connecting (also when smbclient connects again by itself), and how long a failed connection is
    # remembered, so that the requests waiting for it fail at once instead of each trying again.
    connect_timeout: ClassVar[float] = http.CONNECT_TIMEOUT
    failure_ttl: ClassVar[float] = 10.0

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._smb = self.client or _client()
        # rename_information() above, or the tests' stand-in for smbclient's own.
        self._rename_information = getattr(self._smb, "rename_information", None) or rename_information
        host = str(self.config.get("host") or "").strip()
        self._host = _server(host) or host
        self._share = str(self.config.get("share") or "").strip().strip("\\/")
        self._base = paths.clean(self.config.get("base_path") or "/")
        username = str(self.config.get("username") or "").strip()
        self._connection_cache: dict[str, Any] = {}
        self._session_lock = threading.Lock()
        self._failure: DriverError | None = None  # the last failed connection, until _failed_until
        self._failed_until = 0.0
        self._login = {
            "username": username or "Guest",
            "password": str(self.config.get("password") or "") if username else "",
            "port": as_int(self.config.get("port")) or 445,
        }
        # What register_session and every call get: a connection smbclient opens again by itself (the server closed
        # the old one) asks for the same signing and encryption.
        self._session = {**self._login, "encrypt": True if self.config.get("encrypt") else None,
                         "require_signing": bool(username)}

    def close(self) -> None:
        try:
            self._smb.reset_connection_cache(fail_on_error=False, connection_cache=self._connection_cache)
        except Exception:  # noqa: BLE001 - closing a connection that is gone already
            pass

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if _server(str(config.get("host") or "").strip()) is None:
            errors["host"] = gettext("Give the server's name or IP address, without \\\\ or smb://.")
        share = str(config.get("share") or "").strip().strip("\\/")
        if not share or any(ch in share for ch in '\\/:*?"<>|'):
            errors["share"] = gettext("Give the name of the shared folder only, like public.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        if config.get("encrypt") and not str(config.get("username") or "").strip():
            errors["encrypt"] = gettext("The server's guest account can't encrypt: please fill in a username, or "
                                        "turn this off.")
        return errors

    # -- the share

    def _unc(self, path: str) -> str:
        inner = paths.rebase(path, self._base)
        tail = "\\".join(paths.segments(inner))
        return f"\\\\{self._host}\\{self._share}" + (f"\\{tail}" if tail else "")

    def _kwargs(self) -> dict[str, Any]:
        """What each smbclient call gets: this instance's connection cache and settings. The session is registered
        again whenever the connection is gone (the server closed it): smbclient would open a new one by itself, with
        its own defaults (signing required, no encryption, a guest's empty password taken for none)."""
        if not self._logged_on():
            with self._session_lock:
                if not self._logged_on():
                    if time.monotonic() < self._failed_until and self._failure is not None:
                        raise type(self._failure)(self._failure.message)
                    try:
                        self._call(self._smb.register_session, self._host, connection_cache=self._connection_cache,
                                   connection_timeout=self.connect_timeout, **self._session)
                    except DriverError as error:
                        self._failure, self._failed_until = error, time.monotonic() + self.failure_ttl
                        raise
        return {"connection_cache": self._connection_cache, "connection_timeout": self.connect_timeout, **self._session}

    def _logged_on(self) -> bool:
        """Is this instance's connection open, with a session of its account (smbclient's cache)?"""
        connection = self._connection_cache.get(f"{self._host.lower()}:{self._login['port']}")
        return (connection is not None and connection.transport.connected
                and any(session.username == self._login["username"] for session in connection.session_table.values()))

    def _call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return function(*args, **kwargs)
        except DriverError:
            raise
        except Exception as error:  # noqa: BLE001 - mapped to the storage's errors, or BackendError
            raise self._error(error) from None

    def _error(self, error: Exception) -> DriverError:
        from smbprotocol import exceptions as smb_errors  # noqa: PLC0415

        if isinstance(error, (smb_errors.SMBAuthenticationError, smb_errors.LogonFailure)):
            return AuthError(gettext("The SMB server refused the username or password."))
        if isinstance(error, smb_errors.BadNetworkName):
            return NotFound(gettext("The server %(host)s has no share named %(share)s.") % {"host": self._host, "share": self._share})
        if isinstance(error, smb_errors.AccessDenied):
            return DriverError(gettext("The SMB server refused this: no permission."))
        if isinstance(error, smb_errors.SMBLinkRedirectionError):  # to a local path of the server, or another share
            return DriverError(gettext("A link on the SMB server leads to %(target)s, which can't be followed from "
                                       "here.") % {"target": error.target})
        # The server's NT status first: smbclient gives some of them no errno (STATUS_NO_SUCH_FILE, which Samba and
        # others answer for a missing file, has errno 0).
        status = getattr(error, "ntstatus", None) or getattr(error, "status", None)
        if status in _LOGON_REFUSED:
            return AuthError(gettext("The SMB server refused the account %(username)s: %(reason)s.")
                             % {"username": self._login["username"], "reason": _LOGON_REFUSED[status]})
        if status in _MISSING:
            return NotFound()
        if status == 0xC0000035:  # STATUS_OBJECT_NAME_COLLISION
            return AlreadyExists()
        if status == 0xC0000103:  # STATUS_NOT_A_DIRECTORY
            return NotADirectory()
        if status == 0xC00000BA:  # STATUS_FILE_IS_A_DIRECTORY
            return AlreadyExists()
        if status == 0xC00000CC:  # STATUS_BAD_NETWORK_NAME
            return NotFound(gettext("The server %(host)s has no share named %(share)s.") % {"host": self._host, "share": self._share})
        if status in (0xC0000022, 0xC0000061):  # STATUS_ACCESS_DENIED, STATUS_PRIVILEGE_NOT_HELD
            return DriverError(gettext("The SMB server refused this: no permission."))
        if status in (0xC0000043, 0xC0000054, 0xC0000055):  # SHARING_VIOLATION, FILE_LOCK_CONFLICT, LOCK_NOT_GRANTED
            return DriverError(gettext("The file is in use on the SMB server. Please try again later."))
        if status == 0xC0000101:  # STATUS_DIRECTORY_NOT_EMPTY
            return DriverError(gettext("The folder isn't empty."))
        if status == 0xC000007F:  # STATUS_DISK_FULL
            return DriverError(gettext("The SMB share is out of space."))
        if status == 0xC0000044:  # STATUS_QUOTA_EXCEEDED
            return DriverError(gettext("This account's quota on the SMB share is used up."))
        if status == 0xC0000033:  # STATUS_OBJECT_NAME_INVALID
            return DriverError(gettext("The SMB server doesn't accept this name."))
        code = getattr(error, "errno", None)
        if code == errno.ENOENT:
            return NotFound()
        if code == errno.EEXIST:
            return AlreadyExists()
        if code == errno.ENOTDIR:
            return NotADirectory()
        if code == errno.EISDIR:
            return AlreadyExists()
        if code in (errno.EACCES, errno.EPERM):
            return DriverError(gettext("The SMB server refused this: no permission."))
        if code == errno.ENOTEMPTY:
            return DriverError(gettext("The folder isn't empty."))
        if code == errno.ENOSPC:
            return DriverError(gettext("The SMB share is out of space."))
        if status:  # the server answered, with an error the cases above don't know
            return BackendError(gettext("The SMB server %(host)s answered with an error (%(status)s).")
                                % {"host": self._host, "status": _status_text(status)})
        if isinstance(error, (OSError, ValueError, smb_errors.SMBException)):
            return BackendError(gettext("Couldn't reach the SMB server %(host)s: %(message)s") % {"host": self._host, "message": str(error)[:200]})
        raise error

    def _stat(self, path: str) -> Any:
        return self._call(self._smb.stat, self._unc(path), **self._kwargs())

    def _exists(self, path: str) -> Any | None:
        try:
            return self._stat(path)
        except NotFound:
            return None

    @staticmethod
    def _entry(path: str, info: Any) -> Entry:
        is_dir = stat_module.S_ISDIR(info.st_mode)
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else int(info.st_size),
                     modified=parse_time(int(info.st_mtime)) if info.st_mtime else None)

    @staticmethod
    def _listed(path: str, info: Any) -> Entry:
        """An entry from what a listing said of it (smbclient's SMBDirEntry.smb_info), without asking again."""
        is_dir = bool(info.file_attributes & FILE_ATTRIBUTE_DIRECTORY)
        # UTC; 1601 (FILETIME 0) when the server keeps none, and smbprotocol's naive datetime.max past the year 9999.
        moment = info.last_write_time
        known = moment is not None and moment.tzinfo is not None and moment.timestamp() > 0
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else int(info.end_of_file),
                     modified=moment if known else None)

    # -- files

    def list(self, path: str) -> list[Entry]:
        kwargs = self._kwargs()
        try:
            # scandir() asks the server as it is iterated: errors come then.
            found = self._call(lambda: list(self._smb.scandir(self._unc(path), **kwargs)))
        except NotFound:
            info = self._exists(path)
            if info is not None and not stat_module.S_ISDIR(info.st_mode):
                raise NotADirectory() from None
            raise
        entries = []
        for item in found:
            if item.name in (".", "..") or not usable_name(item.name):
                continue
            child = paths.join(path, item.name)
            if not self._call(item.is_symlink):
                # What the listing said: SMBDirEntry.stat() would ask the server again, with the connection cache
                # only (port 445, no username or password).
                entries.append(self._listed(child, item.smb_info))
                continue
            try:  # a link is followed as any path is
                entries.append(self._entry(child, self._stat(child)))
            except NotFound:
                continue  # a link to nothing
            except DriverError as error:  # one smbclient can't follow (to a local path, another share): left out
                self.log.info("SMB %s: the link %s is left out of its folder: %s", self._host, child, error.message)
        return entries

    def stat(self, path: str) -> Entry:
        info = self._stat(path)
        if path == paths.ROOT:
            if not stat_module.S_ISDIR(info.st_mode):
                raise NotFound()
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, info)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        info = self._stat(path)
        if stat_module.S_ISDIR(info.st_mode):
            raise NotFound()
        # Others may write, rename and delete it meanwhile, as on a local disk: a download (a video streamed for hours)
        # doesn't lock the file, and a document open in Word can be downloaded.
        handle = self._call(self._smb.open_file, self._unc(path), mode="rb", share_access="rwd", **self._kwargs())
        total = int(info.st_size)

        def chunks() -> Iterator[bytes]:
            left = total - offset if length is None else length
            self._call(handle.seek, offset)
            while left > 0:
                piece = self._call(handle.read, min(CHUNK, left))
                if not piece:
                    raise BackendError(gettext("The file read from %(service)s is incomplete. Please try again later.") % {"service": "SMB"})
                left -= len(piece)
                yield piece

        size = total - offset if length is None else length
        return ReadStream(chunks(), size=size, total=total, offset=offset,
                          modified=parse_time(int(info.st_mtime)) if info.st_mtime else None, close=handle.close)

    def _make_folders(self, path: str) -> None:
        """The folder at `path` and the missing ones above it; NotADirectory when a file is in the way."""
        info = self._exists(path)
        if info is not None:
            if not stat_module.S_ISDIR(info.st_mode):
                raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": path})
            return
        self._make_folders(paths.parent(path))
        try:
            self._call(self._smb.mkdir, self._unc(path), **self._kwargs())
        except AlreadyExists:  # made meanwhile
            pass

    def _replace(self, temporary: str, path: str, kwargs: dict[str, Any]) -> None:
        """The uploaded `temporary` file put in place of `path`."""
        try:
            self._rename_information(self._unc(temporary), self._unc(path), replace=True, **kwargs)
        except DriverError:
            raise
        except Exception as error:  # noqa: BLE001 - mapped below
            if getattr(error, "ntstatus", None) == 0xC0000022:  # STATUS_ACCESS_DENIED: also a target open elsewhere
                raise DriverError(gettext("%(name)s couldn't be replaced: the file may be open in another program, or "
                                          "this account may not change it.") % {"name": paths.name(path)}) from None
            raise self._error(error) from None

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        existing = self._exists(path)
        if existing is not None and stat_module.S_ISDIR(existing.st_mode):
            raise AlreadyExists()
        parent = paths.parent(path)
        self._make_folders(parent)
        temporary = paths.join(parent, f".hfile-upload-{secrets.token_hex(8)}")
        kwargs = self._kwargs()
        handle = self._call(self._smb.open_file, self._unc(temporary), mode="wb", **kwargs)
        try:
            try:
                left = size
                while left > 0:
                    piece = data.read(min(CHUNK, left))
                    if not piece:
                        raise DriverError(gettext("The uploaded data is incomplete: less than the size given."))
                    self._call(handle.write, piece[:left])
                    left -= min(len(piece), left)
            except BaseException:
                try:
                    handle.close()  # sends what the writer still holds, in vain: the first error is the one to tell
                except Exception:  # noqa: BLE001
                    pass
                raise
            self._call(handle.close)  # the writer (64 KiB) sends what it still holds: the end of the file
            self._replace(temporary, path, kwargs)
        except BaseException:
            try:
                self._smb.remove(self._unc(temporary), **kwargs)
            except Exception:  # noqa: BLE001 - it may not be there
                pass
            raise

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT or self._exists(path) is not None:
            raise AlreadyExists()
        self._make_folders(path)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        info = self._stat(path)
        kwargs = self._kwargs()
        if stat_module.S_ISDIR(info.st_mode):
            self._call(self._smb.shutil.rmtree, self._unc(path), **kwargs)
        else:
            self._call(self._smb.remove, self._unc(path), **kwargs)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        self._stat(path)
        if new_name == paths.name(path):
            return
        target = paths.join(paths.parent(path), new_name)
        # A share that ignores case finds the file itself under its new spelling: that rename is allowed.
        if self._exists(target) is not None and new_name.casefold() != paths.name(path).casefold():
            raise AlreadyExists()
        self._call(self._rename_information, self._unc(path), self._unc(target), replace=False, **self._kwargs())

    def _target(self, path: str, folder: str, *, copy: bool) -> str:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        # Shares ignore case (Windows, and Samba for Windows clients): "/A/b" is below "/a".
        if paths.is_inside(folder.casefold(), path.casefold()):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        self._stat(path)
        info = self._stat(folder)
        if not stat_module.S_ISDIR(info.st_mode):
            raise NotADirectory()
        target = paths.join(folder, paths.name(path))
        if self._exists(target) is not None:
            raise AlreadyExists()
        return target

    def move(self, path: str, folder: str) -> None:
        if path != paths.ROOT and paths.parent(path) == folder:
            self._stat(path)
            return  # already there
        target = self._target(path, folder, copy=False)
        self._call(self._rename_information, self._unc(path), self._unc(target), replace=False, **self._kwargs())

    def _copy_tree(self, source: str, target: str, kwargs: dict[str, Any]) -> None:
        """The folder `source` copied to `target` (UNC paths), each file by the server's own copy. Not smbclient's
        copytree, which asks for each folder's times without the storage's credentials (copystat). Times aren't kept,
        as with a single file; a link to a folder is left out, as it may lead back up."""
        self._call(self._smb.mkdir, target, **kwargs)
        for item in self._call(lambda: list(self._smb.scandir(source, **kwargs))):
            if item.name in (".", ".."):
                continue
            inner_source, inner_target = f"{source}\\{item.name}", f"{target}\\{item.name}"
            if not item.smb_info.file_attributes & FILE_ATTRIBUTE_DIRECTORY:
                self._call(self._smb.shutil.copyfile, inner_source, inner_target, **kwargs)
            elif self._call(item.is_symlink):
                self.log.info("SMB %s: the link %s isn't copied", self._host, inner_source)
            else:
                self._copy_tree(inner_source, inner_target, kwargs)

    def copy(self, path: str, folder: str) -> None:
        target = self._target(path, folder, copy=True)
        kwargs = self._kwargs()
        if stat_module.S_ISDIR(self._stat(path).st_mode):
            self._copy_tree(self._unc(path), self._unc(target), kwargs)
        else:
            self._call(self._smb.shutil.copyfile, self._unc(path), self._unc(target), **kwargs)
