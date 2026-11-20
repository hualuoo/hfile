"""A recording stand-in for smbprotocol's network layer, under smbclient's real code (tests only): what the SMB driver
makes smbclient send, request by request, without an SMB server. FakeSMB (fake_smb.py) replaces smbclient's functions
whole, so it can't show what happens inside them; this keeps them.

Written against smbprotocol 1.17.0, the version requirements.lock pins. It replaces these internals, and only these:
- smbclient._pool's Connection, Session and TreeConnect (connecting, logging on, connecting the share): recorders.
  Session.connect() refuses a session without a password, which pyspnego would have nothing to log on with, and a
  guest ("Guest") on a connection that requires signing or in a session that requires encryption, as
  smbprotocol/session.py does when the server answers that the session is a guest's;
- smbclient._io.SMBFileTransaction.commit, where a compound request goes out. Each one is recorded (`requests`) and
  carried out on `share`, an in-memory share that ignores case as Windows and Samba do: opens by the mode (r, w, x),
  the queries of smbclient.stat(), renames, deletes and attributes;
- SMBRawIO.readinto and write, SMBDirectoryIO.query_directory, and smbprotocol.open.Open.close and flush, which send
  requests of their own.
There is no server-side copy (FSCTL_SRV_REQUEST_RESUME_KEY is STATUS_NOT_SUPPORTED), so smbclient.shutil.copyfile
copies through reads and writes, its own fallback.

    wire = Wire(self)                        # installed for this test, undone at its end
    wire.share.put("docs\\a.txt", b"data")   # paths in the share, separated by \\
    wire.drop()                              # the server closed every connection

Knobs: users ({username: password}), guest (whether the guest account may log on), refuse_execute (opens of an
existing file asking only for FILE_EXECUTE are refused, as Samba refuses them on a file without execute permission).
"""

from __future__ import annotations

import dataclasses
import itertools
import time
from collections import OrderedDict
from typing import Any
from unittest import mock

import smbclient._io as smb_io
import smbclient._pool as smb_pool
from smbprotocol.connection import Dialects
from smbprotocol.exceptions import SMBAuthenticationError, SMBException, SMBOSError
from smbprotocol.file_info import (
    FileAttributeTagInformation,
    FileBasicInformation,
    FileDispositionInformation,
    FileFsVolumeInformation,
    FileIdFullDirectoryInformation,
    FileInformationClass,
    FileInternalInformation,
    FileRenameInformation,
    FileStandardInformation,
)
from smbprotocol.header import NtStatus
from smbprotocol.ioctl import SMB2IOCTLRequest
from smbprotocol.open import (
    FilePipePrinterAccessMask,
    Open,
    SMB2CloseRequest,
    SMB2CreateRequest,
    SMB2QueryInfoRequest,
    SMB2SetInfoRequest,
)
from smbprotocol.structure import DateTimeField

DIRECTORY, ARCHIVE = 0x10, 0x20  # FILE_ATTRIBUTE_*


def filetime() -> int:
    return int(time.time() * 10_000_000) + DateTimeField.EPOCH_FILETIME


@dataclasses.dataclass(frozen=True)
class Request:
    """One compound request that reached the server."""
    server: str
    port: int
    username: str | None
    path: str  # the UNC path smbclient opened
    access: int  # its desired access
    actions: tuple[str, ...]  # the requests after the open: SMB2QueryInfoRequest, SMB2SetInfoRequest, ...


@dataclasses.dataclass(frozen=True)
class Logon:
    """One Session.connect()."""
    server: str
    port: int
    username: str | None
    password: str | None
    require_signing: bool
    require_encryption: bool


@dataclasses.dataclass
class Node:
    path: str  # in the share, as written ("docs\\a.txt"; "" is the share's root)
    is_dir: bool
    data: bytearray
    modified: int  # FILETIME
    index: int


class Share:
    """The share's files and folders, found whatever the case of their names."""

    def __init__(self):
        self._index = itertools.count(1)
        self.nodes: dict[str, Node] = {}
        self._add("", True)

    @staticmethod
    def key(path: str) -> str:
        return path.strip("\\").casefold()

    def get(self, path: str) -> Node | None:
        return self.nodes.get(self.key(path))

    def _add(self, path: str, is_dir: bool, data: bytes = b"") -> Node:
        node = Node(path.strip("\\"), is_dir, bytearray(data), filetime(), next(self._index))
        self.nodes[self.key(path)] = node
        return node

    def put(self, path: str, data: bytes | None = None) -> None:
        """A file (data) or a folder (None), and the folders above it, as another program would make them."""
        parts = path.strip("\\").split("\\")
        for count in range(1, len(parts)):
            if self.get("\\".join(parts[:count])) is None:
                self._add("\\".join(parts[:count]), True)
        self._add(path, data is None, data or b"")

    def children(self, folder: Node) -> list[Node]:
        prefix = self.key(folder.path) + "\\" if folder.path else ""
        return [node for key, node in self.nodes.items()
                if key and key.startswith(prefix) and "\\" not in key[len(prefix):]]

    def _parent(self, path: str) -> Node | None:
        return self.get(path.strip("\\").rpartition("\\")[0])

    def open(self, raw: Any, *, refuse_execute: bool) -> Node:
        """What SMB2 CREATE does with smbclient's mode (r: open, w: overwrite or create, x: create)."""
        path, mode, kind = raw.fd.file_name, raw.mode, raw.FILE_TYPE
        node = self.get(path)
        if node is None:
            parent = self._parent(path)
            if parent is None or not parent.is_dir:
                raise SMBOSError(NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, raw.name)
            if not any(letter in mode for letter in "wxa"):
                raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_NOT_FOUND, raw.name)
            return self._add(path, kind == "dir")
        if refuse_execute and raw._desired_access == FilePipePrinterAccessMask.FILE_EXECUTE:
            raise SMBOSError(NtStatus.STATUS_ACCESS_DENIED, raw.name)
        if "x" in mode:
            raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_COLLISION, raw.name)
        if kind == "dir" and not node.is_dir:
            raise SMBOSError(NtStatus.STATUS_NOT_A_DIRECTORY, raw.name)
        if kind == "file" and node.is_dir:
            raise SMBOSError(NtStatus.STATUS_FILE_IS_A_DIRECTORY, raw.name)
        if "w" in mode:
            node.data, node.modified = bytearray(), filetime()
        return node

    def query(self, node: Node, message: SMB2QueryInfoRequest) -> Any:
        wanted = (message["info_type"].get_value(), message["file_info_class"].get_value())
        attributes = DIRECTORY if node.is_dir else ARCHIVE
        if wanted == (FileBasicInformation.INFO_TYPE, FileBasicInformation.INFO_CLASS):
            info = FileBasicInformation()
            for field in ("creation_time", "last_access_time", "last_write_time", "change_time"):
                info[field] = node.modified
            info["file_attributes"] = attributes
        elif wanted == (FileStandardInformation.INFO_TYPE, FileStandardInformation.INFO_CLASS):
            info = FileStandardInformation()
            info["end_of_file"] = info["allocation_size"] = len(node.data)
            info["number_of_links"] = 1
            info["directory"] = node.is_dir
        elif wanted == (FileInternalInformation.INFO_TYPE, FileInternalInformation.INFO_CLASS):
            info = FileInternalInformation()
            info["index_number"] = node.index
        elif wanted == (FileFsVolumeInformation.INFO_TYPE, FileFsVolumeInformation.INFO_CLASS):
            info = FileFsVolumeInformation()
            info["volume_serial_number"] = 1
        elif wanted == (FileAttributeTagInformation.INFO_TYPE, FileAttributeTagInformation.INFO_CLASS):
            info = FileAttributeTagInformation()
            info["file_attributes"] = attributes
        else:
            raise AssertionError(f"smb_wire doesn't answer the query {wanted}")
        return info

    def rename(self, raw: Any, node: Node, target: str, replace: bool) -> None:
        target = target.strip("\\")
        old_key = self.key(node.path)
        if self.key(target) != old_key:
            existing = self.get(target)
            if existing is not None and (not replace or existing.is_dir):
                raise SMBOSError(NtStatus.STATUS_OBJECT_NAME_COLLISION if not replace else NtStatus.STATUS_ACCESS_DENIED,
                                 raw.name)
            parent = self._parent(target)
            if parent is None or not parent.is_dir:
                raise SMBOSError(NtStatus.STATUS_OBJECT_PATH_NOT_FOUND, raw.name)
        moved = {key: other for key, other in self.nodes.items() if key == old_key or key.startswith(old_key + "\\")}
        old_path = node.path
        for key, other in moved.items():
            del self.nodes[key]
            other.path = target + other.path[len(old_path):]
        for other in moved.values():
            self.nodes[self.key(other.path)] = other

    def delete(self, raw: Any, node: Node) -> None:
        if node.is_dir and self.children(node):
            raise SMBOSError(NtStatus.STATUS_DIRECTORY_NOT_EMPTY, raw.name)
        del self.nodes[self.key(node.path)]


class Wire:
    def __init__(self, test: Any, *, users: dict[str, str] | None = None):
        self.users = users if users is not None else {"alice": "pw"}
        self.guest = True
        self.refuse_execute = False
        self.share = Share()
        self.requests: list[Request] = []
        self.logons: list[Logon] = []
        self.connections: list[Any] = []
        self.global_cache: dict[str, Any] = {}  # smbclient's own cache, for calls made without the driver's
        wire = self

        class Transport:
            connected = True

        class Capabilities:
            def has_flag(self, flag):
                return False

        class Connection:
            def __init__(self, guid, server, port, require_signing=True):
                self.server_name, self.port, self.require_signing = server, port, require_signing
                self.server_guid = f"guid-{server.lower()}"
                self.dialect = Dialects.SMB_3_1_1
                self.transport = Transport()
                self.session_table: OrderedDict[int, Any] = OrderedDict()
                self.server_capabilities = Capabilities()
                wire.connections.append(self)

            def connect(self, timeout=60):
                pass

            def disconnect(self, close=True, timeout=None):
                self.transport.connected = False

        class Session:
            _ids = itertools.count(1)

            def __init__(self, connection, username=None, password=None, require_encryption=True,
                         auth_protocol="negotiate"):
                self.connection, self.username, self.password = connection, username, password
                self.require_encryption = require_encryption
                self.encrypt_data = False
                self.session_id = next(self._ids)
                self.tree_connect_table: dict[int, Any] = {}
                self.open_table: dict[Any, Any] = {}

            def connect(self):
                connection = self.connection
                wire.logons.append(Logon(connection.server_name, connection.port, self.username, self.password,
                                         connection.require_signing, bool(self.require_encryption)))
                if self.password is None:
                    raise SMBAuthenticationError("Failed to authenticate with server: no password to log on with")
                if self.username == "Guest":
                    if not wire.guest:
                        raise SMBAuthenticationError("Failed to authenticate with server: no guest access")
                    if connection.require_signing or self.require_encryption:
                        raise SMBException("SMB encryption or signing was required but session was authenticated "
                                           "as a guest which does not support encryption or signing")
                elif wire.users.get(self.username) != self.password:
                    raise SMBAuthenticationError("Failed to authenticate with server: STATUS_LOGON_FAILURE")
                self.encrypt_data = bool(self.require_encryption)
                connection.session_table[self.session_id] = self

            def disconnect(self, close=True, timeout=None):
                pass

        class TreeConnect:
            _ids = itertools.count(1)

            def __init__(self, session, share_name):
                self.session, self.share_name, self.is_dfs_share = session, share_name, False
                self.tree_connect_id = next(self._ids)

            def connect(self, require_secure_negotiate=True):
                self.session.tree_connect_table[self.tree_connect_id] = self

        def commit(transaction):
            raw = transaction.raw
            fd = raw.fd
            messages = [message for message, _receive in transaction._actions]
            opening = bool(messages) and isinstance(messages[0], SMB2CreateRequest)
            if opening:
                messages = messages[1:]
            wire.requests.append(Request(fd.connection.server_name, fd.connection.port, fd.tree_connect.session.username,
                                         raw.name, raw._desired_access, tuple(type(m).__name__ for m in messages)))
            if opening or raw.closed:
                node = wire.share.open(raw, refuse_execute=wire.refuse_execute)
            else:
                node = fd._wire_node
            results = [wire._carry_out(raw, node, message) for message in messages]
            if opening:
                fd._connected, fd.end_of_file, fd._wire_node = True, len(node.data), node
            transaction.results = tuple(results)

        def readinto(raw, buffer):
            node = raw.fd._wire_node
            data = bytes(node.data[raw._offset:raw._offset + len(buffer)])
            buffer[:len(data)] = data
            raw._offset += len(data)
            return len(data)

        def write(raw, buffer):
            node, data = raw.fd._wire_node, bytes(buffer)
            node.data[raw._offset:raw._offset + len(data)] = data
            raw._offset += len(data)
            raw.fd.end_of_file = max(raw.fd.end_of_file or 0, raw._offset)
            node.modified = filetime()
            return len(data)

        def query_directory(raw, pattern, info_class):
            for child in wire.share.children(raw.fd._wire_node):
                entry = FileIdFullDirectoryInformation()
                entry["file_name"] = child.path.rpartition("\\")[2].encode("utf-16-le")
                entry["end_of_file"] = entry["allocation_size"] = len(child.data)
                entry["file_attributes"] = DIRECTORY if child.is_dir else ARCHIVE
                for field in ("creation_time", "last_access_time", "last_write_time", "change_time"):
                    entry[field] = child.modified
                entry["file_id"] = child.index
                yield entry

        def close(fd, get_attributes=False, send=True):
            if not send:
                return SMB2CloseRequest(), lambda request: None
            fd._connected = False
            return None

        for target, name, value in (
            (smb_pool, "Connection", Connection), (smb_pool, "Session", Session), (smb_pool, "TreeConnect", TreeConnect),
            (smb_pool, "_SMB_CONNECTIONS", self.global_cache), (smb_io.SMBFileTransaction, "commit", commit),
            (smb_io.SMBRawIO, "readinto", readinto), (smb_io.SMBRawIO, "write", write),
            (smb_io.SMBDirectoryIO, "query_directory", query_directory), (Open, "close", close),
            (Open, "flush", lambda fd, send=True: None),
        ):
            test.enterContext(mock.patch.object(target, name, value))

    def _carry_out(self, raw: Any, node: Node, message: Any) -> Any:
        if isinstance(message, SMB2QueryInfoRequest):
            return self.share.query(node, message)
        if isinstance(message, SMB2SetInfoRequest):
            kind, data = message["file_info_class"].get_value(), message["buffer"].get_value()
            if kind == FileInformationClass.FILE_RENAME_INFORMATION:
                info = FileRenameInformation()
                info.unpack(data)
                self.share.rename(raw, node, info["file_name"].get_value(), bool(info["replace_if_exists"].get_value()))
            elif kind == FileInformationClass.FILE_DISPOSITION_INFORMATION:
                info = FileDispositionInformation()
                info.unpack(data)
                if info["delete_pending"].get_value():
                    self.share.delete(raw, node)
            elif kind == FileInformationClass.FILE_BASIC_INFORMATION:
                info = FileBasicInformation()
                info.unpack(data)
                if info["last_write_time"].get_value():
                    node.modified = info["last_write_time"].get_value()
            else:
                raise AssertionError(f"smb_wire doesn't set the information class {kind}")
            return None
        if isinstance(message, SMB2IOCTLRequest):
            raise SMBOSError(NtStatus.STATUS_NOT_SUPPORTED, raw.name)  # no server-side copy here
        raise AssertionError(f"smb_wire doesn't send {type(message).__name__}")

    def drop(self) -> None:
        """The server closed every connection (a restart, an idle reset): smbclient finds them closed."""
        for connection in self.connections:
            connection.transport.connected = False
