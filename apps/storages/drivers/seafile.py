"""Seafile (https://www.seafile.com), through its Web API (https://seafile-api.readme.io): one library of a
Seafile server, or every library the account can use, as the folders of the root. Where the documentation is silent
or out of date, the facts are from the source of Seahub and seafile-server (github.com/haiwen, v14.0.9-server).

Paths
    With `library` (its name or id), the storage root is that library's folder `base_path`. Without one, the
    root lists the account's libraries as folders, and "/lib/a" is "/a" of the library named "lib" (of two of
    one name, the account's own); encrypted libraries aren't shown there, as their files need the library's
    password. Nothing can be made, moved or deleted at that root itself: libraries are made in Seafile.

    A folder: GET /api2/repos/<id>/dir/?p=<path>; a file or folder is found in its folder's listing.

Login
    POST /api2/auth-token/ with the username and password gives the account's token, sent as
    "Authorization: Token <token>"; it doesn't expire, and is asked for again when Seafile refuses it. A refused
    login is remembered for a while (TokenCache) rather than tried again on every call: Seahub counts each failure
    against the username, and after 5 it asks its web login for a captcha, or freezes the account when its
    FREEZE_USER_ON_LOGIN_FAILED is on (seahub/api2/serializers.py, settings.py LOGIN_ATTEMPT_LIMIT). Or the token
    itself (`token`), which an account with two-factor authentication needs.

    Seafile doesn't encrypt names, so an encrypted library lists without its password (the manual's "Security
    features": "File metadata is NOT encrypted"). Its file server refuses the files' data, downloads and uploads,
    with 400 "Repo is encrypted. Please provide password to view it." until the password has been set (POST
    /api2/repos/<id>/ with `library_password`), and forgets it an hour after it was set (seafile-server
    fileserver/fileop.go parseCryptKey, server/passwd-mgr.c). So the password is set before the first download or
    upload, again after `unlock_ttl` seconds, and when the file server refuses: then the request is sent once more,
    an upload only when its data can seek. Whether a library is encrypted comes from GET /api2/repos/, or from GET
    /api2/repos/<id>/ for a library chosen by an id that the list leaves out.

Requests
    Seahub's errors are {"error_msg": ...} or Django REST framework's {"detail": ...} (seahub/api2/utils.py
    api_error); the file server's are its message, as plain text, or for uploads {"error": ...} made without escaping,
    so that a raw newline may be in it (fileop.go formatJSONError, fileserver.go). 429 is Seahub's throttling,
    3000 calls a minute per user by default (settings.py); 520 an operation it refused; 442 a file above the
    server's max_upload_size, 443 an account out of space. Neither Seahub nor the file server redirects: a redirect
    (a proxy's: an http address that must be https, a login page) is an error, as what was asked wasn't done.

Changes
    Seafile renames a new folder, or what is moved or copied, whose name is taken ("a (1)"), so the target is
    looked at first. Folders: POST /api2/repos/<id>/dir/?p= (operation mkdir, with create_parents; or
    rename); files: POST /api/v2.1/repos/<id>/file/?p= (operation rename); moving and copying, within a
    library or between two: POST /api/v2.1/repos/sync-batch-move-item/ and sync-batch-copy-item/; deleting:
    DELETE /api/v2.1/repos/batch-delete-item/ (into the library's trash).

Downloads
    GET /api2/repos/<id>/file/?p=<path>&reuse=1 gives a link of Seafile's file server, valid for about an
    hour and usable more than once: download_url() is that link, open() fetches it with the Range.

Uploads
    GET /api2/repos/<id>/upload-link/?p=<folder> gives an upload address; the file goes there as
    multipart/form-data (parent_dir, replace=1, file), streamed, with its exact length. Missing folders above
    it are made first. The server's upload limit (max_upload_size) applies. The file server answers once it has
    stored the file's blocks and made the commit (fileop.go doUpload): its answer is waited for a second per MiB
    longer than http.TIMEOUT. It must be ret-json's list with the file, its name in NFC as the file server keeps
    it (normalizeUTF8Path), and its size; anything else is an error saying Seafile didn't confirm the upload.

Limits
    Moves and copies between libraries are done by Seafile before it answers (sync-batch-move-item and
    sync-batch-copy-item copy the files' blocks; seahub/api2/endpoints/repos_batch.py), and HFile waits for that
    answer only as long as its read timeout (http.TIMEOUT): a large folder may be reported as timed out while Seafile
    goes on and finishes it.
"""

from __future__ import annotations

import dataclasses
import datetime
import itertools
import json
import re
import secrets
import threading
import time
import unicodedata
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    TokenCache,
    TTLCache,
    as_int,
    exact_body,
    json_or_none,
    parse_time,
    position,
    range_header,
    redirected,
    response_stream,
    short,
    transport_error,
    usable_name,
)
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

SERVICE = gettext_lazy("Seafile")
_LIBRARY_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _address(value: Any) -> str:
    """The Seafile server's address without a query, a fragment or the "/" at the end."""
    return str(value or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _error_text(response: httpx.Response) -> str:
    """Seafile's message in an error answer that has been read: Seahub's {"error_msg"} or {"detail"}, the file
    server's {"error"} (with a raw newline, maybe) or its plain text."""
    try:
        answer = json.loads(response.text, strict=False)
    except ValueError:
        answer = None
    if isinstance(answer, dict):
        return short(answer.get("error_msg") or answer.get("detail") or answer.get("error") or "")
    if response.headers.get("content-type", "").startswith("text/plain"):
        return short(response.text)
    return ""


def _locked(response: httpx.Response) -> bool:
    """The file server's refusal of an encrypted library's data, its password not set (any more): its own words, as
    other refusals may quote the file's name."""
    return response.status_code == 400 and "repo is encrypted" in response.text.lower()


@dataclasses.dataclass(frozen=True)
class _Library:
    id: str
    name: str
    encrypted: bool
    modified: datetime.datetime | None


class SeafileDriver(StorageDriver):
    type = "seafile"
    label = gettext_lazy("Seafile")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://seafile.example.com",
              help=gettext_lazy("The address of the Seafile server, as it is opened in a browser.")),
        Param("username", gettext_lazy("Username"), required=False,
              help=gettext_lazy("The account's email address (or username).")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True),
        Param("token", gettext_lazy("Token"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("Instead of the username and password: the account's API token. Accounts with "
                                "two-factor authentication need it.")),
        Param("library", gettext_lazy("Library"), required=False,
              help=gettext_lazy("The name or ID of the library to show. Empty: every library of the account, as the folders of the root.")),
        Param("library_password", gettext_lazy("Library password"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("For an encrypted library.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("With a library: its folder that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    setup_params = ("url", "username", "password", "token", "library")

    # Tuning; tests make them small.
    token_lifetime: ClassVar[float] = 365 * 86400.0  # Seafile's tokens don't expire: asked again when refused
    unlock_ttl: ClassVar[float] = 3000.0  # seconds a library's password is trusted to be set: Seafile keeps it an hour

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = _address(self.config.get("url"))
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._http = http.client()
        self._fixed = str(self.config.get("token") or "").strip() or None  # the token given, else a login's
        self._tokens = TokenCache(self._log_in)
        self._libraries = TTLCache(4, 60.0)  # "all" -> {name: _Library}, "one" -> _Library
        self._unlock_lock = threading.Lock()
        self._unlocked: dict[str, float] = {}  # library id -> time.monotonic() its password was set

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        raw = str(config.get("url") or "").strip()
        try:
            url = httpx.URL(raw) if raw else None
        except httpx.InvalidURL:
            url = None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            errors["url"] = gettext("The address should start with http:// or https://, like https://seafile.example.com.")
        if not str(config.get("token") or "").strip():
            if not str(config.get("username") or "").strip() or not config.get("password"):
                errors["username"] = gettext("Give the username and password, or the token.")
        try:
            base = paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        else:
            if base != paths.ROOT and not str(config.get("library") or "").strip():
                errors["base_path"] = gettext("A base path needs a library.")
        return errors

    # -- login

    def _log_in(self) -> tuple[str, float]:
        """TokenCache's refresh: the token of a login with the username and password."""
        try:
            response = self._http.post(self._root + "/api2/auth-token/", data={
                "username": str(self.config.get("username") or "").strip(), "password": str(self.config.get("password") or "")})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        answer = json_or_none(response)
        token = answer.get("token") if isinstance(answer, dict) else None
        if response.status_code == 200 and isinstance(token, str) and token:
            return token, self.token_lifetime
        if response.status_code == 400 and isinstance(answer, dict):
            if "X-SEAFILE-OTP" in response.headers or "two" in str(answer).lower():
                raise AuthError(gettext("This Seafile account uses two-factor authentication: give its API token instead of the password."))
            raise AuthError(gettext("Seafile refused the username or password."))
        raise self._failure(response)

    def _current_token(self) -> str:
        return self._fixed if self._fixed is not None else self._tokens.get()

    def _open_library(self, library: _Library) -> None:
        password = str(self.config.get("library_password") or "")
        if not password:
            raise DriverError(gettext("The library %(library)s is encrypted: give its password in the storage source's settings.") % {"library": library.name})
        try:
            self._call("POST", f"{self._root}/api2/repos/{library.id}/", data={"password": password})
        except DriverError as error:
            if isinstance(error, (AuthError, BackendError)):
                raise
            raise DriverError(gettext("Seafile refused the library password.")) from None

    def _unlock(self, library: _Library, *, again: bool = False) -> None:
        """Sets the password of an encrypted library before its files' data is read or written: once, after
        unlock_ttl, or `again` when the file server has forgotten it (see the module's docstring)."""
        if not library.encrypted:
            return
        with self._unlock_lock:
            set_at = self._unlocked.get(library.id)
            if not again and set_at is not None and time.monotonic() - set_at < self.unlock_ttl:
                return
            self._open_library(library)
            self._unlocked[library.id] = time.monotonic()

    # -- requests

    def _call(self, method: str, url: str, *, library: _Library | None = None, **kwargs: Any) -> httpx.Response:
        """A request of the API, logged in (again once, when the token is refused) and with the library opened
        (again once, when Seafile asks for its password). DriverError for an error answer."""
        opened = False
        extra = kwargs.pop("headers", {})
        for attempt in range(3):
            token = self._current_token()
            headers = {"Authorization": f"Token {token}", "Accept": "application/json", **extra}
            try:
                response = self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if 300 <= response.status_code < 400:
                raise redirected(response, SERVICE)
            if response.status_code == 401 or (response.status_code == 403 and "token" in response.text.lower()):
                if self._fixed is not None or attempt:
                    raise AuthError(gettext("Seafile refused the token. Please check the storage source's settings."))
                self._tokens.invalidate(token)
                continue
            if response.status_code == 440 and library is not None and not opened:
                self._open_library(library)
                opened = True
                continue
            if response.status_code >= 400:
                raise self._failure(response, library)
            return response
        raise AuthError(gettext("Seafile refused the token. Please check the storage source's settings."))

    def _failure(self, response: httpx.Response, library: _Library | None = None) -> DriverError:
        message = _error_text(response)
        status = response.status_code
        if status == 404:
            return NotFound()
        if status == 440:
            return DriverError(gettext("The library %(library)s is encrypted: give its password in the storage source's settings.")
                               % {"library": library.name if library else ""})
        if status == 442:
            return DriverError(gettext("The file is larger than the Seafile server takes (its max_upload_size)."))
        if status == 443:
            return DriverError(gettext("The Seafile account is out of space."))
        if status == 429:
            return BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": SERVICE})
        if status == 403:
            return DriverError(gettext("Seafile refused this: %(message)s") % {"message": message or "403"})
        if status == 520:  # HTTP_520_OPERATION_FAILED: an operation Seafile refused
            return DriverError(gettext("Seafile refused this (HTTP %(status)s): %(message)s") % {"status": status, "message": message})
        if status >= 500:
            return BackendError(gettext("Seafile failed (HTTP %(status)s). Please try again later.") % {"status": status})
        return DriverError(gettext("Seafile refused this (HTTP %(status)s): %(message)s") % {"status": status, "message": message})

    def _json(self, method: str, url: str, **kwargs: Any) -> Any:
        answer = json_or_none(self._call(method, url, **kwargs))
        if answer is None:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return answer

    # -- libraries and paths

    @staticmethod
    def _library_of(item: Any) -> _Library | None:
        """The library a listing of libraries, or GET /api2/repos/<id>/, describes."""
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return None
        return _Library(id=item["id"], name=str(item.get("name") or ""), encrypted=bool(item.get("encrypted")),
                        modified=parse_time(item.get("mtime")))

    def _all_libraries(self) -> dict[str, _Library]:
        cached = self._libraries.get("all")
        if cached is not None:
            return cached
        answer = self._json("GET", self._root + "/api2/repos/")
        found: dict[str, _Library] = {}
        for item in answer if isinstance(answer, list) else []:
            library = self._library_of(item)
            if library is None:
                continue
            mine = item.get("type") == "repo"
            if usable_name(library.name) and (library.name not in found or mine):
                found[library.name] = library
        self._libraries.put("all", found)
        return found

    def _chosen(self) -> _Library:
        cached = self._libraries.get("one")
        if cached is not None:
            return cached
        wanted = str(self.config.get("library") or "").strip()
        libraries = self._all_libraries()
        library = next((lib for lib in libraries.values() if lib.id == wanted), None) or libraries.get(wanted)
        if library is None and _LIBRARY_ID.fullmatch(wanted):  # one the listing leaves out (shared to a group...)
            try:
                library = self._library_of(self._json("GET", f"{self._root}/api2/repos/{wanted}/"))
            except NotFound:
                library = None
        if library is None:
            raise NotFound(gettext("Seafile has no library %(library)s for this account.") % {"library": wanted})
        self._libraries.put("one", library)
        return library

    def _where(self, path: str) -> tuple[_Library | None, str]:
        """The library and the path in it of a storage path; (None, "/") for the root of the libraries."""
        if str(self.config.get("library") or "").strip():
            return self._chosen(), paths.rebase(path, self._base)
        if path == paths.ROOT:
            return None, paths.ROOT
        name, *rest = paths.segments(path)
        library = self._all_libraries().get(name)
        if library is None or library.encrypted:
            raise NotFound()
        return library, "/" + "/".join(rest)

    def _changeable(self, path: str) -> tuple[_Library, str]:
        """_where() for what changes: not the root, nor a library itself."""
        library, inner = self._where(path)
        if library is None or inner == paths.ROOT:
            if path == paths.ROOT:
                raise DriverError(gettext("The storage source's root folder can't be changed."))
            raise DriverError(gettext("Libraries are made, renamed and deleted in Seafile itself."))
        return library, inner

    def _dirents(self, library: _Library, inner: str) -> list[dict[str, Any]]:
        answer = self._json("GET", f"{self._root}/api2/repos/{library.id}/dir/", params={"p": inner}, library=library)
        if not isinstance(answer, list):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return [item for item in answer if isinstance(item, dict) and usable_name(item.get("name"))]

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        is_dir = item.get("type") == "dir"
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                     modified=parse_time(item.get("mtime")))

    def _find(self, path: str) -> dict[str, Any] | None:
        """The dirent at `path` (not a library's root), from its folder's listing; None when it isn't there."""
        library, inner = self._where(path)
        parent, name = paths.split(inner)
        try:
            items = self._dirents(library, parent)
        except NotFound:
            return None
        return next((item for item in items if item.get("name") == name), None)

    def _is_folder(self, path: str) -> bool | None:
        """True for a folder (and the roots), False for a file, None when nothing is there."""
        library, inner = self._where(path)
        if library is None or inner == paths.ROOT:
            return True
        item = self._find(path)
        return None if item is None else item.get("type") == "dir"

    # -- files

    def check(self) -> None:
        self.list(paths.ROOT)

    def list(self, path: str) -> list[Entry]:
        library, inner = self._where(path)
        if library is None:
            return [Entry(name=name, path=paths.join(path, name), is_dir=True, modified=lib.modified)
                    for name, lib in self._all_libraries().items() if not lib.encrypted]
        try:
            items = self._dirents(library, inner)
        except NotFound:
            if self._is_folder(path) is False:
                raise NotADirectory() from None
            raise
        return [self._entry(paths.join(path, item["name"]), item) for item in items]

    def stat(self, path: str) -> Entry:
        library, inner = self._where(path)
        if library is None or inner == paths.ROOT:
            if library is not None and path == paths.ROOT:
                self._dirents(library, inner)  # the base path must be there
            return Entry(name=paths.name(path) if path != paths.ROOT else "", path=path, is_dir=True,
                         modified=library.modified if library is not None and path != paths.ROOT else None)
        item = self._find(path)
        if item is None:
            raise NotFound()
        return self._entry(path, item)

    def _file(self, path: str) -> tuple[_Library, str, dict[str, Any]]:
        """The library, path in it and dirent of the file at `path`. NotFound, a folder too."""
        library, inner = self._where(path)
        item = self._find(path) if library is not None and inner != paths.ROOT else None
        if item is None or item.get("type") == "dir":
            raise NotFound()
        return library, inner, item

    def _link(self, library: _Library, inner: str) -> str:
        url = self._json("GET", f"{self._root}/api2/repos/{library.id}/file/", params={"p": inner, "reuse": 1}, library=library)
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        return url

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        library, inner, item = self._file(path)
        total = as_int(item.get("size"))
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        self._unlock(library)
        url = self._link(library, inner)
        for attempt in range(2):
            try:  # the file server's link is its own permission: no token there
                response = self._http.send(self._http.build_request("GET", url, headers=headers), stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code == 400 and library.encrypted and not attempt:
                # Its "Repo is encrypted": Seafile has forgotten the library's password, after which the link works.
                http.drain(response)
                self._unlock(library, again=True)
                continue
            break
        if response.status_code == 404:
            http.drain(response)
            raise NotFound()
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE,
                               modified=parse_time(item.get("mtime")))

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        library, inner, _ = self._file(path)
        self._unlock(library)  # the browser's download needs it set as much as open()'s
        return self._link(library, inner)

    def _make_folders(self, path: str) -> None:
        """The folder at `path` and the missing ones above it (Seafile's create_parents)."""
        library, inner = self._where(path)
        self._call("POST", f"{self._root}/api2/repos/{library.id}/dir/", params={"p": inner},
                   data={"operation": "mkdir", "create_parents": "true"}, library=library)

    def _send_file(self, link: str, folder: str, name: str, data: BinaryIO, size: int) -> httpx.Response:
        """The upload's request: the file as multipart/form-data to the upload address, which is its own permission,
        as a download link is. Its answer is waited for longer (see the module's docstring)."""
        boundary = "hfile-" + secrets.token_hex(16)
        # A quoted string (RFC 7578, 2045): \ and " escaped, as Seafile's file server reads them.
        quoted = name.replace("\\", "\\\\").replace('"', '\\"')
        head = (f'--{boundary}\r\nContent-Disposition: form-data; name="parent_dir"\r\n\r\n{folder}\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="replace"\r\n\r\n1\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{quoted}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "Content-Length": str(len(head) + size + len(tail))}
        timeout = httpx.Timeout(http.TIMEOUT, connect=http.CONNECT_TIMEOUT, read=http.TIMEOUT + size / (1024 * 1024))
        try:
            return self._http.post(link, params={"ret-json": 1}, headers=headers, timeout=timeout,
                                   content=itertools.chain([head], exact_body(data, size), [tail]))
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _check_upload(self, response: httpx.Response, name: str, size: int, library: _Library) -> None:
        """An upload's answer must be ret-json's list with the file of `name` (NFC, as the file server keeps names)
        and `size`."""
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        if response.status_code >= 400:
            raise self._failure(response, library)
        answer = json_or_none(response)
        wanted = unicodedata.normalize("NFC", name)
        stored = [item for item in answer if isinstance(item, dict)] if isinstance(answer, list) else []
        if not any(isinstance(item.get("name"), str) and unicodedata.normalize("NFC", item["name"]) == wanted
                   and as_int(item.get("size")) == size for item in stored):
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": SERVICE})

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        library, inner = self._changeable(path)
        parent = paths.parent(path)
        state = self._is_folder(parent)
        if state is False:
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": parent})
        if state is None:
            self._make_folders(parent)
        elif self._is_folder(path) is True:
            raise AlreadyExists()
        link = self._json("GET", f"{self._root}/api2/repos/{library.id}/upload-link/", params={"p": paths.parent(inner)}, library=library)
        if not isinstance(link, str) or not link.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no upload address.") % {"service": SERVICE})
        self._unlock(library)
        start = position(data)
        response = self._send_file(link, paths.parent(inner), paths.name(inner), data, size)
        if _locked(response):  # Seafile has forgotten the library's password: set again, then sent again
            self._unlock(library, again=True)
            if start is None:
                raise BackendError(gettext("Seafile asked for the library's password again during the upload. Please "
                                           "upload the file again."))
            data.seek(start)
            response = self._send_file(link, paths.parent(inner), paths.name(inner), data, size)
        self._check_upload(response, paths.name(inner), size, library)

    def mkdir(self, path: str) -> None:
        self._changeable(path)
        if self._is_folder(path) is not None:  # Seafile would make "name (1)"
            raise AlreadyExists()
        parent = paths.parent(path)
        if self._is_folder(parent) is False:
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": parent})
        self._make_folders(path)

    def delete(self, path: str) -> None:
        library, inner = self._changeable(path)
        if self._find(path) is None:
            raise NotFound()
        parent, name = paths.split(inner)
        self._call("DELETE", f"{self._root}/api/v2.1/repos/batch-delete-item/", library=library,
                   json={"repo_id": library.id, "parent_dir": parent, "dirents": [name]})

    def rename(self, path: str, new_name: str) -> None:
        library, inner = self._changeable(path)
        item = self._find(path)
        if item is None:
            raise NotFound()
        if new_name == item.get("name"):
            return
        if self._find(paths.join(paths.parent(path), new_name)) is not None:
            raise AlreadyExists()
        if item.get("type") == "dir":
            url = f"{self._root}/api2/repos/{library.id}/dir/"
        else:
            url = f"{self._root}/api/v2.1/repos/{library.id}/file/"
        self._call("POST", url, params={"p": inner}, data={"operation": "rename", "newname": new_name}, library=library)

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        library, inner = self._changeable(path)
        if self._find(path) is None:
            raise NotFound()
        target_library, target = self._where(folder)
        if target_library is None:
            raise DriverError(gettext("Libraries are made, renamed and deleted in Seafile itself."))
        state = self._is_folder(folder)
        if state is None:
            raise NotFound()
        if state is False:
            raise NotADirectory()
        parent, name = paths.split(inner)
        if paths.parent(path) == folder and not copy:
            return  # already there
        if self._find(paths.join(folder, name)) is not None:  # Seafile would name it "name (1)"
            raise AlreadyExists()
        action = "sync-batch-copy-item" if copy else "sync-batch-move-item"
        self._call("POST", f"{self._root}/api/v2.1/repos/{action}/", library=library, json={
            "src_repo_id": library.id, "src_parent_dir": parent, "src_dirents": [name],
            "dst_repo_id": target_library.id, "dst_parent_dir": target})

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)
