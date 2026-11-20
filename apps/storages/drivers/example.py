"""Example Drive: a made-up service, to show how a driver is written (docs/development.md, 添加存储源). It isn't in
drivers.DRIVERS, so no storage source can be of this type. The fake server of its tests
(apps/storages/tests/fake_example.py) defines its HTTP API: read that as the service's documentation, which a real
driver cites here (its URLs), with the versions of the service it was written for.

Paths
    The storage root ("/") is the server's folder `base_path`: "/a/b" is base_path + "/a/b". The API takes paths as
    they are, in the query or in the JSON body. A folder is listed `page_size` items a page, following "next".

Login
    An API token, made on the server's settings page, sent as "Authorization: Bearer <token>" with every call. It
    doesn't expire: one the server refuses (401) is an AuthError, for the admin to replace.

Requests
    An error is {"error": {"code", "message"}} with its HTTP status (_error). A call the server throttles (429) is
    made again after the Retry-After it gives, `attempts` times in all; an upload isn't, as its data can't be sent
    twice.

Changes
    mkdir: POST /folders, which makes the missing folders above. rename and move: POST /move; copy: POST /copy;
    neither replaces anything (a taken name is 409 exists). delete: DELETE /items, a folder with everything in it.

Downloads
    open(): GET /content with the Range. download_url(): POST /links gives a link that needs no token and saves the
    file under the name asked for.

Uploads
    write(): PUT /content with the file as the body, streamed; the server makes the missing folders above it and
    replaces a file of that name.

Names
    The server takes any name of up to 255 bytes.

Limits
    No search, and no uploads from the browser: the API has neither.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    as_int,
    exact_body,
    json_or_none,
    origin,
    parse_time,
    range_header,
    redirected,
    response_stream,
    retry_after,
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

SERVICE = gettext_lazy("Example Drive")  # the service's name in messages: "Connecting to %(service)s failed ..."
API = "/api/v1"  # below the server's address


class ExampleDriver(StorageDriver):
    type = "example"
    label = gettext_lazy("Example Drive")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://cloud.example.com",
              help=gettext_lazy("The address of the Example Drive server, as it is opened in a browser.")),
        Param("token", gettext_lazy("API token"), kind="password", secret=True,
              help=gettext_lazy("Made on the server's settings page (Settings → API tokens), allowed to read and write.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the server that is this storage source's root, like / or /folder1.")),
    )

    # What the backend can do (StorageDriver says what each one means).
    can_range = True
    can_direct_download = True  # POST /links
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    setup_params = ("url", "token")

    # Tuning; the tests make them small.
    page_size: ClassVar[int] = 1000
    attempts: ClassVar[int] = 3

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = str(self.config.get("url") or "").strip().rstrip("/") + API
        self._token = str(self.config.get("token") or "").strip()
        self._base = paths.clean(self.config.get("base_path") or "/")
        # One client for all the threads: httpx's is thread-safe, and keeps its connections for the next calls.
        self._http = http.client()

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not origin(str(config.get("url") or "").strip())[0]:
            errors["url"] = gettext("The address should start with http:// or https://, like https://cloud.example.com.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        return errors

    # -- requests

    def _remote(self, path: str) -> str:
        """The server's path of a storage path."""
        return paths.rebase(path, self._base)

    def _call(self, method: str, api: str, *, params: Mapping[str, Any] | None = None, body: Any = None,
              content: Iterator[bytes] | None = None, headers: Mapping[str, str] | None = None, ok: int = 200) -> Any:
        """The JSON answer of an API call (None when it has no body); the DriverError of an error answer."""
        for attempt in range(1, self.attempts + 1):
            try:
                response = self._http.request(method, self._root + api, params=params, json=body, content=content,
                                              headers={"Authorization": f"Bearer {self._token}", **(headers or {})})
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code == 429 and content is None and attempt < self.attempts:
                wait = retry_after(response)
                if wait is not None:  # None: longer than a request of the site should wait
                    time.sleep(wait)
                    continue
            if response.status_code != ok:
                raise self._error(response)
            return json_or_none(response)
        raise AssertionError("unreachable")

    def _error(self, response: httpx.Response) -> DriverError:
        """The DriverError of an error answer: the subclass the site has for it (base.py), else a message."""
        answer = json_or_none(response)
        error = answer.get("error") if isinstance(answer, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        status = response.status_code
        if status == 404:
            return NotFound()
        if code == "exists":
            return AlreadyExists()
        if code == "not_a_folder":
            return NotADirectory()
        if status == 401:
            return AuthError()
        if status == 429:
            return BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": SERVICE})
        if 300 <= status < 400:
            return redirected(response, SERVICE)
        if status >= 500 or not isinstance(code, str):
            return BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.") % {"service": SERVICE, "status_code": status})
        return DriverError(gettext("%(service)s refused this (%(code)s): %(message)s")
                           % {"service": SERVICE, "code": short(code, 40), "message": short(error.get("message"))})

    def _item(self, path: str) -> dict[str, Any]:
        """The server's item at a storage path; NotFound when there is none."""
        item = self._call("GET", "/items", params={"path": self._remote(path)})
        if not isinstance(item, dict) or item.get("type") not in ("file", "folder"):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return item

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        folder = item["type"] == "folder"
        return Entry(name=paths.name(path), path=path, is_dir=folder, size=None if folder else as_int(item.get("size")),
                     modified=parse_time(item.get("modified")))

    # -- files

    def check(self) -> None:
        try:
            self.list(paths.ROOT)
        except NotFound:
            raise NotFound(gettext("The base path %(base)s doesn't exist in %(service)s.") % {"base": self._base, "service": SERVICE}) from None

    def list(self, path: str) -> list[Entry]:
        entries: list[Entry] = []
        cursor = None
        for _ in range(100_000):
            params = {"path": self._remote(path), "limit": self.page_size, **({"cursor": cursor} if cursor else {})}
            page = self._call("GET", "/children", params=params)
            items = page.get("items") if isinstance(page, dict) else None
            if not isinstance(items, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            for item in items:
                # A name that can't be one part of a path ("..", one with "/") is left out.
                if isinstance(item, dict) and item.get("type") in ("file", "folder") and usable_name(item.get("name")):
                    entries.append(self._entry(paths.join(path, item["name"]), item))
            cursor = page.get("next")
            if not isinstance(cursor, str) or not cursor:
                return entries
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def stat(self, path: str) -> Entry:
        item = self._item(path)
        if path == paths.ROOT and item["type"] != "folder":
            raise NotFound()  # a base path that is a file
        return self._entry(path, item)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        # The bytes as they are stored (identity), which response_stream passes on; a Range for a part.
        headers = {"Authorization": f"Bearer {self._token}", "Accept-Encoding": "identity"}
        span = range_header(offset, length, None)
        if span:
            headers["Range"] = span
        request = self._http.build_request("GET", self._root + "/content", params={"path": self._remote(path)}, headers=headers)
        try:
            response = self._http.send(request, stream=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code not in (200, 206):
            raise self._error(http.read_error(response))  # the error's JSON: a streamed answer isn't read yet
        return response_stream(response, offset=offset, length=length, total=None, service=SERVICE)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        body = {"path": self._remote(path), "expires": min(max(expires, 60), 86400), "filename": filename}  # what it takes
        answer = self._call("POST", "/links", body=body)
        url = answer.get("url") if isinstance(answer, dict) else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        return url

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        # Streamed from `data`, never read into memory; exact_body fails the upload when `data` ends early.
        self._call("PUT", "/content", params={"path": self._remote(path)}, content=exact_body(data, size),
                   headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"}, ok=201)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._call("POST", "/folders", body={"path": self._remote(path)}, ok=201)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._call("DELETE", "/items", params={"path": self._remote(path)}, ok=204)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        if new_name == paths.name(path):
            self._item(path)  # nothing to do, if it is there
            return
        self._call("POST", "/move", body={"from": self._remote(path), "to": self._remote(paths.join(paths.parent(path), new_name))})

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        """move() and copy(): the server's call, after the checks that have the site's own messages."""
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        if paths.parent(path) == folder and not copy:
            self._item(path)
            return  # already there
        self._call("POST", "/copy" if copy else "/move", body={"from": self._remote(path), "to": self._remote(paths.join(folder, paths.name(path)))})

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)
