"""IPFS (https://ipfs.tech): the files of an IPFS node's Mutable File System (MFS), through the Kubo RPC API
(https://docs.ipfs.tech/reference/kubo/rpc/).

Paths
    The storage root ("/") is the MFS folder `base_path`; "/a/b" is base_path + "/a/b".

Login
    The RPC API gives full control of the node, so it should only be reachable by this site (Kubo binds it to
    localhost); a node that asks for it gets `token` as "Authorization: Bearer <token>" (Kubo's
    API.Authorizations).

Requests
    Every call is a POST to `api_url` + /api/v0/files/..., the path in its "arg" query parameter. files/ls
    (long), files/stat, files/read (offset and count: the Range), files/write (create, truncate, parents; the
    file as multipart/form-data, streamed), files/mkdir (parents), files/mv, files/cp (cheap: it links the same
    blocks), files/rm (recursive). MFS's mv and cp put the item inside a folder that has the target's name, so
    the target is looked at first. Errors are a 500 with {"Message": ...}. MFS keeps no modification times by
    default: none are shown.

Downloads
    open() reads through the RPC API, asking for the bytes as they are (Accept-Encoding: identity): a compressed
    answer, from a proxy, is refused. With `gateway` (the node's HTTP gateway, or a public one), download_url()
    is the gateway's link to the file's content by its CID, which anyone who knows the CID can open, and
    which saves it under its name; without, the site sends the file (can_direct_download is False).

Uploads
    Kubo's files/write truncates the file first and keeps what arrived of a body that broke off (a deferred
    Close and FlushPath in core/commands/files.go): written straight to its name, an upload that fails would
    leave part of the new file in place of the old one. So it goes to a hidden ".hfile-upload-<random>" in the
    same folder, then files/mv puts it onto the file's own name (mfs.Mv replaces a file there; a folder of the
    name is refused first). When it fails, the hidden file is deleted; listings never show such names.
"""

from __future__ import annotations

import itertools
import secrets
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import as_int, exact_body, json_or_none, short, transport_error, usable_name
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

SERVICE = gettext_lazy("IPFS")
CHUNK = 256 * 1024
TEMP_PREFIX = ".hfile-upload-"  # an upload goes to such a hidden name first, see Uploads in the module's docstring


class IPFSDriver(StorageDriver):
    type = "ipfs"
    label = gettext_lazy("IPFS (MFS)")
    params = (
        Param("api_url", gettext_lazy("RPC API address"), default="http://127.0.0.1:5001",
              help=gettext_lazy("The Kubo node's RPC API (port 5001 by default). It controls the whole node: don't make it reachable from the internet.")),
        Param("token", gettext_lazy("Token"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("For a node whose RPC API asks for one (API.Authorizations): sent as Authorization: Bearer.")),
        Param("gateway", gettext_lazy("Gateway"), required=False, placeholder="http://127.0.0.1:8080",
              help=gettext_lazy("Optional: an IPFS HTTP gateway that browsers can reach. Downloads then go there, by the file's CID, which anyone who knows it can open.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The MFS folder that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # with a gateway, see download_url()
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    setup_params = ("api_url",)

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._api = str(self.config.get("api_url") or "").strip().rstrip("/") + "/api/v0"
        self._gateway = str(self.config.get("gateway") or "").strip().rstrip("/")
        self.can_direct_download = bool(self._gateway)
        self._base = paths.clean(self.config.get("base_path") or "/")
        token = str(self.config.get("token") or "").strip()
        self._http = http.client(headers={"Authorization": f"Bearer {token}"} if token else None)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        for key in ("api_url", "gateway"):
            raw = str(config.get(key) or "").strip()
            if not raw and key == "gateway":
                continue
            url = httpx.URL(raw) if raw else None
            if url is None or url.scheme not in ("http", "https") or not url.host:
                errors[key] = gettext("The address should start with http:// or https://, like http://127.0.0.1:5001.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        return errors

    # -- requests

    def _mfs(self, path: str) -> str:
        return paths.rebase(path, self._base)

    def _request(self, command: str, params: list[tuple[str, Any]], **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.post(f"{self._api}/files/{command}", params=params, **kwargs)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code != 200:
            raise self._error(response)
        return response

    def _error(self, response: httpx.Response) -> DriverError:
        answer = json_or_none(response)
        message = short(answer.get("Message")) if isinstance(answer, dict) else short(response.text)
        lowered = message.lower()
        if response.status_code in (401, 403):
            return AuthError(gettext("The IPFS node refused the request (HTTP %(status)s): check the token, or the node's API.Authorizations.") % {"status": response.status_code})
        if "does not exist" in lowered or "no link named" in lowered or "not found" in lowered:
            return NotFound()
        if "already exists" in lowered or "already has entry" in lowered:
            return AlreadyExists()
        if "not a directory" in lowered:
            return NotADirectory()
        if response.status_code == 404:
            return BackendError(gettext("The address isn't an IPFS node's RPC API (HTTP 404)."))
        if response.status_code >= 500 and not message:
            return BackendError(gettext("The IPFS node failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})
        return DriverError(gettext("The IPFS node refused this: %(message)s") % {"message": message or response.status_code})

    def _stat(self, path: str) -> dict[str, Any]:
        answer = json_or_none(self._request("stat", [("arg", self._mfs(path))]))
        if not isinstance(answer, dict):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return answer

    def _existing(self, path: str) -> dict[str, Any] | None:
        try:
            return self._stat(path)
        except NotFound:
            return None

    @staticmethod
    def _entry(path: str, is_dir: bool, size: Any) -> Entry:
        return Entry(name=paths.name(path) if path != paths.ROOT else "", path=path, is_dir=is_dir,
                     size=None if is_dir else as_int(size))

    # -- files

    def list(self, path: str) -> list[Entry]:
        info = self._stat(path)
        if info.get("Type") != "directory":
            raise NotADirectory()
        answer = json_or_none(self._request("ls", [("arg", self._mfs(path)), ("long", "true")]))
        items = answer.get("Entries") if isinstance(answer, dict) else None
        entries = []
        for item in items or []:  # null for an empty folder
            if isinstance(item, dict) and usable_name(item.get("Name")) and not item["Name"].startswith(TEMP_PREFIX):
                entries.append(self._entry(paths.join(path, item["Name"]), item.get("Type") == 1, item.get("Size")))
        return entries

    def stat(self, path: str) -> Entry:
        info = self._stat(path)
        if path == paths.ROOT and info.get("Type") != "directory":
            raise NotFound()
        return self._entry(path, info.get("Type") == "directory", info.get("Size"))

    def _file(self, path: str) -> dict[str, Any]:
        info = self._stat(path)
        if info.get("Type") != "file":
            raise NotFound()
        return info

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        total = as_int(self._file(path).get("Size")) or 0
        size = total - offset if length is None else length
        params = [("arg", self._mfs(path)), ("offset", offset)] + ([("count", length)] if length is not None else [])
        request = self._http.build_request("POST", f"{self._api}/files/read", params=params, headers={"Accept-Encoding": "identity"})
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code != 200:
            raise self._error(http.read_error(response))
        encoding = response.headers.get("content-encoding", "").strip().lower()
        if encoding not in ("", "identity"):  # the bytes are passed on as they come
            http.drain(response)
            raise BackendError(gettext("%(service)s returned a compressed file (%(encoding)s), which can't be passed on as it is.") % {"service": SERVICE, "encoding": short(encoding, 20)})

        def chunks() -> Iterator[bytes]:
            left = size
            try:
                for piece in response.iter_raw(CHUNK):
                    piece = piece[:left]
                    left -= len(piece)
                    if piece:
                        yield piece
                    if left <= 0:
                        break
            except httpx.HTTPError as error:
                raise BackendError(gettext("The connection dropped while reading the file from %(service)s. Please try again later.") % {"service": SERVICE}) from error
            finally:
                response.close()
            if left > 0:
                raise BackendError(gettext("The file read from %(service)s is incomplete. Please try again later.") % {"service": SERVICE})

        return ReadStream(chunks(), size=size, total=total, offset=offset, close=response.close)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        if not self._gateway:
            return None
        cid = self._file(path).get("Hash")
        if not isinstance(cid, str) or not cid.isalnum():
            return None
        return f"{self._gateway}/ipfs/{cid}?" + urllib.parse.urlencode({"filename": filename, "download": "true"})

    def _discard(self, temporary: str) -> None:
        """Delete the hidden file of an upload that failed. Kubo may still be keeping what arrived of it when the
        first try comes, so it is tried a few times."""
        for attempt in range(3):
            try:
                self._request("rm", [("arg", self._mfs(temporary))])
                return
            except NotFound:
                if attempt < 2:
                    time.sleep(0.1)
            except DriverError:
                return

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        existing = self._existing(path)
        if existing is not None and existing.get("Type") == "directory":
            raise AlreadyExists()
        parent = paths.parent(path)
        found = self._existing(parent) if parent != paths.ROOT else {"Type": "directory"}
        if found is not None and found.get("Type") != "directory":
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": parent})
        boundary = "hfile-" + secrets.token_hex(16)
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="file"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        temporary = paths.join(parent, TEMP_PREFIX + secrets.token_hex(8))  # see Uploads in the module's docstring
        try:
            self._request("write", [("arg", self._mfs(temporary)), ("create", "true"), ("truncate", "true"), ("parents", "true")],
                          content=itertools.chain([head], exact_body(data, size), [tail]),
                          headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                   "Content-Length": str(len(head) + size + len(tail))})
            self._request("mv", [("arg", self._mfs(temporary)), ("arg", self._mfs(path))])
        except BaseException:
            self._discard(temporary)
            raise

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT or self._existing(path) is not None:  # with parents, MFS says nothing of either
            raise AlreadyExists()
        self._request("mkdir", [("arg", self._mfs(path)), ("parents", "true")])

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._stat(path)
        self._request("rm", [("arg", self._mfs(path)), ("recursive", "true")])

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        self._stat(path)
        if new_name == paths.name(path):
            return
        target = paths.join(paths.parent(path), new_name)
        if self._existing(target) is not None:
            raise AlreadyExists()
        self._request("mv", [("arg", self._mfs(path)), ("arg", self._mfs(target))])

    def _target(self, path: str, folder: str, *, copy: bool) -> str:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        self._stat(path)
        if self._stat(folder).get("Type") != "directory":
            raise NotADirectory()
        target = paths.join(folder, paths.name(path))
        if self._existing(target) is not None:  # MFS would put it inside a folder of that name
            raise AlreadyExists()
        return target

    def move(self, path: str, folder: str) -> None:
        if path != paths.ROOT and paths.parent(path) == folder:
            self._stat(path)
            return  # already there
        self._request("mv", [("arg", self._mfs(path)), ("arg", self._mfs(self._target(path, folder, copy=False)))])

    def copy(self, path: str, folder: str) -> None:
        self._request("cp", [("arg", self._mfs(path)), ("arg", self._mfs(self._target(path, folder, copy=True)))])
