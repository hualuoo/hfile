"""A stand-in for smbprotocol's smbclient over a local folder (tests only): the SMB driver's logic without an
SMB server, which this machine doesn't have.

    fake = FakeSMB(root_folder, host="nas", share="public", users={"alice": "pw"})
    Driver = type("D", (SMBDriver,), {"client": fake})

It has the functions of smbclient the driver uses, with UNC paths (\\\\host\\share\\a\\b) of that one share,
and errors as smbclient gives them: OSError with SMB's errno (ENOENT, EEXIST, ENOTDIR, ...), LogonFailure-like
SMBAuthenticationError for wrong credentials, BadNetworkName-like errors for another share, and SMB's rename,
which (unlike Linux's) refuses a name that is taken. register_session keeps a connection in the cache it is given,
as smbclient does. scandir() gives what smbclient's gives: the listing's own data in smb_info, and a stat() that
fails here, as smbclient's would ask the server again without the storage's port and credentials.
Files are written through a BufferedWriter of 64 KiB, as smbclient's open_file() gives: small writes are sent by
close(). Knobs: offline (every call raises ConnectionRefusedError), sessions (what register_session got), links
({name in the share: the error following it raises}: listed as links), disk_full (writes are refused with
STATUS_DISK_FULL), refuse_replace (a rename onto a file is refused with STATUS_ACCESS_DENIED, as when that file is
open elsewhere) and opens (what open_file got).
"""

from __future__ import annotations

import datetime
import errno
import io
import os
import shutil as local_shutil
from types import SimpleNamespace
from typing import Any

from smbclient._os import SMBDirEntryInformation
from smbprotocol.exceptions import SMBAuthenticationError, SMBOSError
from smbprotocol.header import NtStatus

DIRECTORY, ARCHIVE, REPARSE_POINT = 0x10, 0x20, 0x400  # FILE_ATTRIBUTE_*
SYMLINK_TAG = 0xA000000C  # IO_REPARSE_TAG_SYMLINK, in ea_size as a listing gives it


class _Written(io.RawIOBase):
    """A file written on the share, as smbclient's SMBFileIO: each write() is a request, refused when it's full."""

    def __init__(self, local: str, fake: FakeSMB, unc: str):
        self._file, self._fake, self._unc = open(local, "wb"), fake, unc

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        if self._fake.disk_full:
            raise SMBOSError(NtStatus.STATUS_DISK_FULL, self._unc)
        return self._file.write(data)

    def close(self) -> None:
        self._file.close()
        super().close()


class _Entry:
    """An entry of scandir(), as smbclient's SMBDirEntry."""

    def __init__(self, name: str, path: str, info: SMBDirEntryInformation, link: bool):
        self.name, self.path, self.smb_info, self._link = name, path, info, link

    def is_symlink(self) -> bool:
        return self._link

    def stat(self, follow_symlinks=True):
        raise AssertionError("SMBDirEntry.stat() asks the server again with the connection cache only: port 445, "
                             "no username or password")


class FakeSMB:
    def __init__(self, root: str, *, host: str = "nas", share: str = "public", users: dict[str, str] | None = None):
        self.root = root
        self.host = host
        self.share = share
        self.users = users if users is not None else {"alice": "pw-alice"}
        self.guest = True
        self.offline = False
        self.links: dict[str, Exception] = {}
        self.disk_full = False
        self.refuse_replace = False
        self.opens: list[tuple[str, str, str | None]] = []  # (path in the share, mode, share_access) of open_file
        self.sessions: list[dict[str, Any]] = []
        self.resets = 0
        self.shutil = SimpleNamespace(rmtree=self._rmtree, copyfile=self._copyfile)

    # -- paths and sessions

    def _local(self, unc: str, kwargs: dict[str, Any]) -> str:
        if self.offline:
            raise ConnectionRefusedError(errno.ECONNREFUSED, "Connection refused")
        if kwargs.get("connection_cache") is None:
            raise AssertionError("the driver must use its own connection cache")
        self._check_user(kwargs.get("username"), kwargs.get("password"))
        prefix = f"\\\\{self.host}\\{self.share}"
        if not (unc == prefix or unc.startswith(prefix + "\\")):
            raise OSError(errno.ENOENT, "STATUS_BAD_NETWORK_NAME", unc)
        rest = unc[len(prefix):].lstrip("\\")
        if "/" in rest:
            raise OSError(errno.EINVAL, "a / in an SMB path", unc)
        return os.path.join(self.root, *rest.split("\\")) if rest else self.root

    def _check_user(self, username, password) -> None:
        if username == "Guest" and password == "" and self.guest:
            return
        if self.users.get(username) != password:
            raise SMBAuthenticationError("STATUS_LOGON_FAILURE")

    def register_session(self, server, username=None, password=None, port=445, encrypt=None, connection_timeout=60,
                         connection_cache=None, auth_protocol="negotiate", require_signing=True):
        if self.offline:
            raise ValueError(f"Failed to connect to '{server}:{port}': [Errno 111] Connection refused")
        if server != self.host:
            raise ValueError(f"Failed to connect to '{server}:{port}'")
        self._check_user(username, password)
        self.sessions.append({"username": username, "port": port, "encrypt": encrypt, "require_signing": require_signing,
                              "own_cache": connection_cache is not None})
        if connection_cache is not None:  # as smbclient keeps it: the connection, with the session of that account
            connection_cache[f"{server.lower()}:{port}"] = SimpleNamespace(
                transport=SimpleNamespace(connected=True), session_table={1: SimpleNamespace(username=username)})

    def reset_connection_cache(self, fail_on_error=True, connection_cache=None):
        self.resets += 1
        if connection_cache is not None:
            connection_cache.clear()

    # -- the functions

    def scandir(self, path, search_pattern="*", **kwargs):
        local = self._local(path, kwargs)
        if not os.path.isdir(local):
            raise OSError(errno.ENOENT if not os.path.exists(local) else errno.ENOTDIR, "STATUS_OBJECT_NAME_NOT_FOUND", path)
        entries = []
        for item in os.scandir(local):
            info = item.stat(follow_symlinks=False)
            link = item.is_symlink() or self._share_path(f"{path}\\{item.name}") in self.links
            folder = item.is_dir()  # a link to a folder has FILE_ATTRIBUTE_DIRECTORY too (Windows)
            moment = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)
            entries.append(_Entry(item.name, f"{path}\\{item.name}", SMBDirEntryInformation(
                creation_time=moment, last_access_time=moment, last_write_time=moment, change_time=moment,
                end_of_file=0 if folder else info.st_size, allocation_size=info.st_size,
                file_attributes=(DIRECTORY if folder else ARCHIVE) | (REPARSE_POINT if link else 0),
                ea_size=SYMLINK_TAG if link else 0, file_id=info.st_ino, file_name=item.name), link))
        return entries

    def _share_path(self, unc: str) -> str:
        return unc[len(f"\\\\{self.host}\\{self.share}"):].strip("\\")

    def stat(self, path, follow_symlinks=True, **kwargs):
        local = self._local(path, kwargs)
        if follow_symlinks and self._share_path(path) in self.links:
            raise self.links[self._share_path(path)]
        return os.stat(local)

    def open_file(self, path, mode="r", buffering=-1, share_access=None, **kwargs):
        local = self._local(path, kwargs)
        self.opens.append((self._share_path(path), mode, share_access))
        if os.path.isdir(local):
            raise OSError(errno.EISDIR, "STATUS_FILE_IS_A_DIRECTORY", path)
        if "r" in mode:
            return open(local, mode)
        return io.BufferedWriter(_Written(local, self, path), buffer_size=65536)  # smbclient's: MAX_PAYLOAD_SIZE

    def mkdir(self, path, **kwargs):
        local = self._local(path, kwargs)
        if os.path.exists(local):
            raise OSError(errno.EEXIST, "STATUS_OBJECT_NAME_COLLISION", path)
        if not os.path.isdir(os.path.dirname(local)):
            raise OSError(errno.ENOENT, "STATUS_OBJECT_PATH_NOT_FOUND", path)
        os.mkdir(local)

    def makedirs(self, path, exist_ok=False, **kwargs):
        os.makedirs(self._local(path, kwargs), exist_ok=exist_ok)

    def remove(self, path, **kwargs):
        local = self._local(path, kwargs)
        if os.path.isdir(local):
            raise OSError(errno.EISDIR, "STATUS_FILE_IS_A_DIRECTORY", path)
        os.remove(local)

    def rmdir(self, path, **kwargs):
        os.rmdir(self._local(path, kwargs))

    def rename_information(self, src, dst, *, replace, **kwargs):
        """What smb.rename_information() does on a server: SMB's rename refuses a name another entry has unless
        `replace` (Linux's would replace it), not the entry's own name in other capitals, which is what a share
        that ignores case sees (and Windows here)."""
        local_src, local_dst = self._local(src, kwargs), self._local(dst, kwargs)
        if os.path.exists(local_dst) and not os.path.samefile(local_src, local_dst):
            if not replace:
                raise OSError(errno.EEXIST, "STATUS_OBJECT_NAME_COLLISION", dst)
            if self.refuse_replace:  # MS-FSCC: "the target file was open and ReplaceIfExists is nonzero"
                raise SMBOSError(NtStatus.STATUS_ACCESS_DENIED, src, dst)
            os.replace(local_src, local_dst)
            return
        os.rename(local_src, local_dst)

    def _rmtree(self, path, ignore_errors=False, onerror=None, **kwargs):
        local_shutil.rmtree(self._local(path, kwargs))

    def _copyfile(self, src, dst, follow_symlinks=True, **kwargs):
        local_shutil.copyfile(self._local(src, kwargs), self._local(dst, kwargs))
