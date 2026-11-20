"""Lenovo NAS share links: the files and folders the owner of a Lenovo storage device (Lenovo Home Storage, formerly
Lenovo Personal Cloud) shares with a link https://siot-share.lenovo.com.cn/s/#/<share code> and an extraction code,
as a read-only storage. Anyone with the link and the code can read them, without an account.

Lenovo documents no API for share links. This is what the share page itself does, read from its JavaScript
(https://siot-share.lenovo.com.cn/s/, the bundle /s/assets/index.241a289c.js in November 2026) [page]; OpenList's
driver (drivers/lenovonas_share) and its documentation (https://doc.oplist.org/guide/drivers/lenovo_nas_share) gave
leads [OpenList]. The API is reverse-engineered, and this driver hasn't been tried against the real service.

Paths
    files?code=&stoken=&num=&page=[&path=] lists a folder; without a path the share's root, the files and
    folders that were shared [page]. Each item has its name, type ("dir": a folder), size, time (Unix seconds,
    which the page shows as the file's time) and path, which lists the folder (path=) or names the file [page].
    The root's items may come from different folders of the device [page: it keeps each one's folder, dir]; of
    items with the same name the first is shown. A storage path is found by listing the folders above it from
    the share's root; listings are cached for cache_ttl seconds. A listing comes in pages of page_size items,
    page counting from 0: the page asks for page 0 of 5000 only [page]. The next pages are asked for while
    has_more is true [OpenList: the field], which the page never does, so that is untested: the paging stops at
    a page that brings no new item, with a warning in the log when has_more still said there was more.

Login
    access?code=<share code>&password=<extraction code> gives data.stoken, valid for data.expires_in seconds
    [page]; the other calls send it as stoken. It is asked for again shortly before it expires, and once more
    when a call is refused with -40001, which the page takes for an expired token (it asks for the extraction
    code again) [page]. A refusal of the extraction code is remembered for half a minute (TokenCache).

Requests
    GET <host>/oneproxy/api/share/v1/<call>?<query>, the values percent-encoded as encodeURIComponent() does it,
    without other headers [page]. The answer is {"result": true, "data": ...} or {"result": false, "error":
    {"code", "msg"}} [page]. The codes [page: its constants and messages]: -40001 a wrong extraction code (at
    access) or a share token that has expired (elsewhere), -40004 no such share (at access) or file, -40006 the
    share has expired, -40007 it was cancelled, -45005 the device that shares it is offline.

Host
    https://siot-share.lenovo.com.cn [page]. OpenList's documentation says that the device's own address on its
    local network (http://192.168.x.x) answers the same, and doesn't recommend it [OpenList]; untested.

Downloads
    file/link?code=&stoken=&path= gives data.url and data.param: the file is data.url (a path is on `host`) with
    param as its query string, which the page has the browser download [page]. param holds a download token,
    dtoken [OpenList], not the share token: download_url() gives that link, at the cost of a request. If param
    ever carries the share token (stoken), it gives None and the site sends the file. open() fetches the link
    with the Range; an answer of the whole file is cut down here. A JSON answer whose length isn't the file's is
    taken for the API's error (the API answers its errors with HTTP 200) and read, ERROR_BYTES at most. Untested:
    how long a link lasts, whether it checks the Referer, which name the browser saves the file under, and how
    the link answers when it fails.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    TokenCache,
    TTLCache,
    as_int,
    json_or_none,
    origin,
    parse_time,
    range_header,
    redirected,
    response_stream,
    short,
    transport_error,
    usable_name,
)
from .base import (
    AuthError,
    BackendError,
    DriverError,
    Entry,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
)

SERVICE = gettext_lazy("Lenovo NAS")
HOST = "https://siot-share.lenovo.com.cn"
ERROR_BYTES = 64 * 1024  # of a download that answered JSON instead of the file, read at most
_CODE = re.compile(r"[A-Za-z0-9._~-]{1,128}")
_UNESCAPED = "-_.!~*'()"  # what encodeURIComponent() leaves as it is, besides letters and digits
_log = logging.getLogger("hfile.storage")

# The share API's error codes [page].
UNAUTHORIZED = -40001
NOT_FOUND = -40004
EXPIRED = -40006
CANCELLED = -40007
OFFLINE = -45005


def share_code(text: Any) -> tuple[str, bool]:
    """The share code of the share_id param, a code or a link .../s/#/<code>, and whether the link has the
    extraction code in it too (.../s/#/<code>/<extraction code>), as the share page reads its address [page]."""
    text = str(text or "").strip()
    if "#/" not in text:
        return text, False
    code, *rest = text.split("#/", 1)[1].split("/")
    return code, any(rest)


def _query(values: Mapping[str, Any]) -> str:
    """A query string as the share page writes one: name=value, the values percent-encoded as
    encodeURIComponent() does it [page]; "" for none."""
    if not values:
        return ""
    return "?" + "&".join(f"{name}={urllib.parse.quote(str(value), safe=_UNESCAPED)}" for name, value in values.items())


class LenovoNasShareDriver(StorageDriver):
    type = "lenovo-nas-share"
    label = gettext_lazy("Lenovo NAS share link")
    params = (
        Param("share_id", gettext_lazy("Share link or ID"), placeholder=HOST + "/s/#/Ab.cDeF1234",
              help=gettext_lazy("The share's link, or the part after #/ in it.")),
        Param("share_password", gettext_lazy("Share password"), kind="password", secret=True,
              help=gettext_lazy("The extraction code that comes with the link.")),
        Param("host", gettext_lazy("Server address"), required=False, default=HOST, advanced=True,
              help=gettext_lazy("Lenovo's share server. A Lenovo device may serve the same at its address on the "
                                "local network, like http://192.168.1.10 (untested).")),
    )

    can_range = True
    can_direct_download = True  # unless a link carries the share token, see download_url()
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    setup_params = ("share_id", "share_password")

    # The API and tuning; tests make them small.
    api_path: ClassVar[str] = "/oneproxy/api/share/v1"
    page_size: ClassVar[int] = 5000  # items a listing asks for at a time, as the page does
    cache_ttl: ClassVar[float] = 30.0  # seconds a folder's listing is used
    token_lifetime: ClassVar[float] = 600.0  # seconds a share token is used when access doesn't say

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._code, _ = share_code(self.config.get("share_id"))
        self._host = str(self.config.get("host") or HOST).strip().rstrip("/")
        self._http = http.client()
        self._tokens = TokenCache(self._access)
        self._listings = TTLCache(256, self.cache_ttl)  # storage path -> {name: (Entry, its path in the API)}

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        code, with_password = share_code(config.get("share_id"))
        if with_password:  # it would be kept in the clear: the share password is a secret
            errors["share_id"] = gettext("This link has the extraction code in it. Please give the link without it, "
                                         "and the code as the share password.")
        elif not _CODE.fullmatch(code):
            errors["share_id"] = gettext("The share link or ID isn't right: give the share's link, or the part "
                                         "after #/ in it.")
        try:
            host = httpx.URL(str(config.get("host") or HOST).strip())
        except httpx.InvalidURL:
            host = None
        if host is None or host.scheme not in ("http", "https") or not host.host or host.path != "/" or host.query or host.fragment:
            errors["host"] = gettext("The server address should be like https://siot-share.lenovo.com.cn or "
                                     "http://192.168.1.10, without a path.")
        return errors

    # -- login

    def _access(self) -> tuple[str, float]:
        """A share token for the extraction code, and its lifetime (TokenCache's refresh)."""
        data = self._call("access", {"password": str(self.config.get("share_password") or "")}, token=False)
        stoken = data.get("stoken") if isinstance(data, dict) else None
        if not isinstance(stoken, str) or not stoken:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        lifetime = as_int(data.get("expires_in"))
        return stoken, float(lifetime) if lifetime is not None and lifetime > 0 else self.token_lifetime

    # -- requests

    def _call(self, call: str, query: Mapping[str, Any], *, token: bool = True) -> Any:
        """The data of a call of the share API; DriverError for a refusal. With `token` the share token goes
        along, and is asked for again once when the call says it has expired."""
        for attempt in range(2):
            stoken = self._tokens.get() if token else None
            values = {"code": self._code, **({"stoken": stoken} if stoken is not None else {}), **query}
            try:
                response = self._http.get(f"{self._host}{self.api_path}/{call}{_query(values)}")
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if 300 <= response.status_code < 400:  # the API makes no redirects
                raise redirected(response, SERVICE)
            answer = json_or_none(response)
            if not isinstance(answer, dict) or not isinstance(answer.get("result"), bool):
                if response.status_code >= 500:
                    raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.") % {"service": SERVICE, "status_code": response.status_code})
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            if answer["result"]:
                return answer.get("data")
            error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
            code = as_int(error.get("code"))
            if code == UNAUTHORIZED and stoken is not None and not attempt:
                self._tokens.invalidate(stoken)  # it expired before its time: another one, once
                continue
            raise self._refusal(code, short(error.get("msg")), access=not token)
        raise AssertionError("unreachable")

    @staticmethod
    def _refusal(code: int | None, message: str, *, access: bool) -> DriverError:
        """The error for an answer {"result": false}; `access`: of the call that asks for a share token."""
        if code == UNAUTHORIZED and access:
            return AuthError(gettext("%(service)s refused the extraction code. Please check the share password in the "
                                     "storage source's settings.") % {"service": SERVICE})
        if code == NOT_FOUND:
            if access:
                return AuthError(gettext("The share doesn't exist. Please check the share link in the storage "
                                         "source's settings."))
            return NotFound()
        if code == EXPIRED:
            return AuthError(gettext("The share link has expired."))
        if code == CANCELLED:
            return AuthError(gettext("The share link has been cancelled."))
        if code == OFFLINE:
            return BackendError(gettext("The device sharing this is offline. Please try again later."))
        return DriverError(gettext("%(service)s refused this (%(code)s): %(message)s") % {"service": SERVICE, "code": code, "message": message})

    def _items(self, path: str) -> dict[str, tuple[Entry, str]]:
        """What is in the folder at `path`: name -> (Entry, its path in the API). NotFound, NotADirectory."""
        cached = self._listings.get(path)
        if cached is not None:
            return cached
        query: dict[str, Any] = {}
        if path != paths.ROOT:  # the share's root is listed without a path [page]
            entry, query["path"] = self._find(path)
            if not entry.is_dir:
                raise NotADirectory()
        items: dict[str, tuple[Entry, str]] = {}
        seen: set[str] = set()
        page = 0
        while True:
            data = self._call("files", {"num": self.page_size, "page": page, **query})
            found = data.get("list") if isinstance(data, dict) else None
            if not isinstance(found, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            new = False
            for raw in found:
                where = raw.get("path") if isinstance(raw, dict) else None
                if not isinstance(where, str) or not where or where in seen:
                    continue
                seen.add(where)
                new = True
                name = raw.get("name")
                if usable_name(name) and name not in items:
                    is_dir = raw.get("type") == "dir"
                    items[name] = (Entry(name=name, path=paths.join(path, name), is_dir=is_dir,
                                         size=None if is_dir else as_int(raw.get("size")),
                                         modified=parse_time(raw.get("time"))), where)
            if not data.get("has_more") or not new:  # nothing new: the API doesn't page as asked
                if data.get("has_more"):
                    _log.warning("%s: page %s of the share's folder %s brought nothing new, though has_more said more "
                                 "would come; the folder may show only part of what is in it", SERVICE, page, path)
                break
            page += 1
        self._listings.put(path, items)
        return items

    def _find(self, path: str) -> tuple[Entry, str]:
        """The file or folder at `path` (not the root), from its folder's listing. NotFound."""
        parent, name = paths.split(path)
        try:
            found = self._items(parent).get(name)
        except NotADirectory:  # below a file
            raise NotFound() from None
        if found is None:
            raise NotFound()
        return found

    def _file(self, path: str) -> tuple[Entry, str]:
        """The file at `path`. NotFound, a folder too."""
        if path == paths.ROOT:
            raise NotFound()
        entry, where = self._find(path)
        if entry.is_dir:
            raise NotFound()
        return entry, where

    def _link(self, where: str) -> tuple[str, bool]:
        """The download address of the file at `where` (its path in the API), and whether it carries the share
        token."""
        data = self._call("file/link", {"path": where})
        url = data.get("url") if isinstance(data, dict) else None
        param = (data.get("param") or {}) if isinstance(data, dict) else None
        link = ""
        if isinstance(url, str) and url and isinstance(param, dict):
            try:
                link = str(httpx.URL(self._host + "/").join(url + _query(param)))
            except httpx.InvalidURL:
                pass
        if origin(link) == ("", "", 0):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        return link, "stoken" in param

    # -- files

    def list(self, path: str) -> list[Entry]:
        return [entry for entry, _ in self._items(path).values()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._items(path)  # the share must be there
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._find(path)[0]

    def _error_answer(self, response: httpx.Response, expected: int | None) -> DriverError | None:
        """The error of a download that answered JSON instead of the file: a JSON answer whose length isn't
        `expected` (the file's, or the range's), read (ERROR_BYTES at most) and closed. None for anything else."""
        kind = response.headers.get("content-type", "").lower()
        length = as_int(response.headers.get("content-length"))
        if "json" not in kind or expected is None or length is None or length == expected:
            return None
        pieces: list[bytes] = []
        size = 0
        try:
            for piece in response.iter_bytes():
                pieces.append(piece)
                size += len(piece)
                if size > ERROR_BYTES:
                    break
        except httpx.HTTPError:
            pass
        finally:
            response.close()
        try:
            answer = json.loads(b"".join(pieces)[:ERROR_BYTES])
        except ValueError:
            answer = None
        if isinstance(answer, dict) and answer.get("result") is False:
            error = answer.get("error") if isinstance(answer.get("error"), dict) else {}
            return self._refusal(as_int(error.get("code")), short(error.get("msg")), access=False)
        return BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        entry, where = self._file(path)
        url, _ = self._link(where)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, entry.size)
        if span:
            headers["Range"] = span
        try:
            response = self._http.send(self._http.build_request("GET", url, headers=headers), stream=True,
                                       follow_redirects=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code == 404:
            http.drain(response)
            raise NotFound()
        if response.status_code in (200, 206) and entry.size is not None:
            expected = entry.size if response.status_code == 200 else (length if length is not None else entry.size - offset)
            error = self._error_answer(response, expected)
            if error is not None:
                raise error
        return response_stream(response, offset=offset, length=length, total=entry.size, service=SERVICE,
                               modified=entry.modified)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        url, private = self._link(self._file(path)[1])
        return None if private else url
