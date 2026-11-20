"""百度网盘 (Baidu Netdisk) through its open platform, 百度网盘开放平台 (https://pan.baidu.com/union/).

It follows Baidu's documentation (read on 2026-11-17), these pages under https://pan.baidu.com/union/doc/:
    使用入门/创建应用, 使用入门/权限与配额, 使用入门/接入授权/授权码模式, .../授权回调地址, .../faq;
    平台简介/错误码; 基础网盘服务/获取用户信息/获取用户信息 (uinfo);
    基础网盘服务/获取文件信息/获取文件列表 (list), .../查询文件信息 (filemetas); 基础网盘服务/下载;
    基础网盘服务/管理文件 (filemanager); 基础网盘服务/创建文件夹;
    基础网盘服务/上传/能力说明, .../预上传 (precreate), .../获取上传域名 (locateupload),
    .../分片上传 (superfile2), .../创建文件 (create); faq/上传, faq/下载, faq/管理文件;
and Baidu's OAuth error codes (https://openauth.baidu.com/doc/appendix.html). Where they say nothing, the facts
are those OpenList's driver shows (precreate's return_type 2 for a rapid upload, the membership's slice sizes).
The test fake (tests/fake_baidu.py) models the same.

Paths
    Baidu addresses files by path. The storage root ("/") is the folder `base_path` of the account; "/a/b" is
    base_path + "/a/b". An app may only use its own folder, /apps/<app name> (Baidu Netdisk shows it as
    我的应用数据/<app name>): apps made after 3 June 2026 can't download, upload, move or manage anything
    elsewhere, and the open platform's rules forbid any app to list or touch the folders outside the ones the
    user authorised (权限与配额, 应用目录权限细则), so base_path must be that folder or one in it. A new
    app's folder only exists once something is made in it: check() makes it, and the folders in it down to
    base_path, when they're missing.

    A file is found in the listing of its folder (one list call per 1000 entries), cached for `listing_ttl`
    seconds; list() itself always asks Baidu, and our own changes drop the cached listings they touch (a listing
    fetched while a change of ours was under way isn't cached). Before putting something under a name (upload,
    mkdir, rename, move, copy), the target folder's listing is trusted only when Baidu gave it at most `guard_ttl`
    seconds ago. Missing folders above an upload or a new folder are made one by one.

Login
    Access tokens last 30 days. The saved one is used until 5 minutes before token_expires_at, or until Baidu
    refuses it (errno -6, 20016 or 31045; error_code 100, 110 or 111 on the PCS hosts); then it is refreshed
    (GET https://openapi.baidu.com/oauth/2.0/token, grant_type refresh_token), once and under a lock, the new
    tokens are saved through update_config() before they are used, and the call is made once more. Baidu's FAQ
    asks not to retry refused tokens (it limits apps that do): when the refresh is refused, when the new token is
    refused too, or when Baidu says the authorisation was withdrawn (20017), those tokens aren't sent again, and
    every call is AuthError without asking Baidu until the config has other ones (authorised again, or refreshed
    by another instance of the storage and adopted by this one).

Params
    app_key, secret_key   AppKey and SecretKey of the admin's own app on the open platform, with this site's
                          OAuth callback in its 授权回调页 (安全设置);
    access_token,         filled in by 授权 (authorization code flow, scope basic,netdisk) and saved again after
    refresh_token,        every refresh: Baidu replaces the refresh token on each refresh and the old one stops
    token_expires_at      working (it also stops when a refresh fails);
    base_path             the app's folder, /apps/<app name>, or a folder in it.

Requests
    Every call has the access token in its query string and the User-Agent pan.baidu.com (Baidu's examples send
    it; downloads need it). Calls of the API wait their turn under `calls_per_second` over all threads (Baidu
    doesn't publish its limits and limits "abnormal" use), at most `queue_wait` seconds, else BackendError; the
    slices of an upload and the downloads themselves aren't paced. Errors are Baidu's errno (xpan) or error_code
    (PCS, OpenAPI), mapped to the errors of base.py; a rate limit (20012, 31034, error_code 4, 17, 18, 19) is
    BackendError at once, never retried.

Changes
    list (start/limit, 1000 a page), create isdir=1 (mkdir; rtype 0, a taken name fails), filemanager (copy, move,
    rename, delete; async=0, ondup fail; deletes go to Baidu's recycle bin), uinfo (vip_type, for large uploads).

Downloads
    filemetas (fsids, dlink=1) gives a dlink, valid 8 hours and reused for `link_ttl` seconds; it is fetched with
    &access_token=... appended and User-Agent pan.baidu.com, following Baidu's redirect, with Range. A link Baidu
    refuses as expired (31360, 31362, or a bare 403/404/410) is asked for again once; Baidu may answer a refused
    link with HTTP 200 and an errno, which is told from the file by its JSON type and length. A browser can't send
    the token's query or that User-Agent (and Baidu doesn't let browsers download more than 50 MB), so there is
    no download_url(): the site sends the files.

Uploads
    precreate with the MD5 of each slice (block_list), of the whole file (content-md5) and of its first 256 KiB
    (slice-md5), rtype 3 (a file at the path is replaced). return_type 2 means Baidu had the bytes (秒传): done.
    Otherwise locateupload names the upload hosts (d.pcs.baidu.com when it names none), the slices precreate asks
    for go to superfile2 (multipart, field "file"; tried `slice_tries` times, on the next host each time, after a
    broken connection, a 5xx, an MD5 that isn't the slice's or -11015, a network failure that Baidu's upload FAQ
    says to try again), and create makes the file. The access token goes only to hosts of baidu.com and
    baidupcs.com, over https (an http address of them is made https). Slices are 4 MiB; a file that would need
    more than 1024 slices needs a membership (uinfo's vip_type: 普通会员 slices up to 16 MiB and files up to
    10 GB, 超级会员 32 MiB and 20 GB; others 4 GB), and gets the smallest multiple of 4 MiB that fits. Files too
    large for the account are refused before anything is read. The hashes need a first pass over the data, so
    data that can't seek is spooled to a temporary file. Baidu takes no empty files.

Names
    At most 255 bytes, without \\ : * ? " < > | (an assumption: Baidu's docs only say "special
    characters", 31062).

Limits
    No search: Baidu's allows 2000 calls a day per user, and the site walks folders instead. No direct browser
    uploads or downloads (no CORS, and they need the token). An app made for 公开发布 that hasn't passed Baidu's
    review only gets test quotas (the docs say 10 calls an hour) and 10 users; one made for 个人使用 works at once,
    for up to 10 users. Baidu restricts or bans apps and accounts that touch folders outside the app's, call too
    often, retry refused tokens, share one personal account and its membership among many people, use a personal
    account for a company's site, an image host or a migration tool, or upload from many different IP regions;
    uploads are limited to 100000 a day. Downloads of non-members are slow (Baidu's choice).

    Not tried against the real service: there was no account to try it with.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import json
import logging
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    RateLimiter,
    TTLCache,
    as_int,
    exact_body,
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
    Unsupported,
)

SERVICE = gettext_lazy("Baidu Netdisk")
USER_AGENT = "pan.baidu.com"  # Baidu's examples send it; dlinks refuse other User-Agents (31326)
SCOPE = "basic,netdisk"
LOCATE_APP_ID = "250528"  # locateupload's appid, a fixed value in Baidu's docs

KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3
PREHASH_BYTES = 256 * KiB  # slice-md5: the MD5 of the first 256 KB

_TOKEN_ERRNOS = frozenset({-6, 20016, 31045})  # refused or expired access token: refresh once
_TOKEN_ERROR_CODES = frozenset({100, 110, 111})  # the same in the OpenAPI codes of the PCS hosts
_WITHDRAWN = 20017  # the user withdrew the authorisation: refreshing can't help
_RELINK_CODES = frozenset({31360, 31362})  # a download link expired or refused: ask for a new one


# --- answers and errors -----------------------------------------------------------------------------

class _Transient(BackendError):
    """A broken connection, a 5xx or Baidu's -11015: the slice of an upload is sent again."""


def _url(base: str, params: Mapping[str, Any]) -> str:
    """base?query, spaces as %20: Baidu's examples send either, and %20 can't be read as a plus."""
    return base + "?" + urllib.parse.urlencode({key: str(value) for key, value in params.items()},
                                               quote_via=urllib.parse.quote)


def _code(answer: Mapping[str, Any]) -> tuple[str, int] | None:
    """("errno", n) of an xpan answer or ("error_code", n) of a PCS or OpenAPI one when it reports an error."""
    for field in ("errno", "error_code"):
        number = as_int(answer.get(field))
        if number:
            return field, number
    return None


def _token_refused(code: tuple[str, int]) -> bool:
    field, number = code
    return number in _TOKEN_ERRNOS or (field == "error_code" and number in _TOKEN_ERROR_CODES)


def _error(answer: Mapping[str, Any]) -> DriverError:
    """DriverError for an answer with an errno or error_code; a batch's own items (filemanager's info) say best."""
    code = _code(answer) or ("errno", 0)
    for key in ("info", "list"):
        items = answer.get(key)
        if isinstance(items, list):
            found = next((as_int(item.get("errno")) for item in items
                          if isinstance(item, dict) and as_int(item.get("errno"))), None)
            if found:
                code = ("errno", found)
                break
    field, number = code
    pcs = field == "error_code"
    message = short(answer.get("errmsg") or answer.get("error_msg") or answer.get("show_msg") or "")
    if number == _WITHDRAWN or _token_refused(code):
        return AuthError(gettext("Baidu Netdisk refused the authorization (%(code)s). Please authorize again in the "
                                 "storage source's settings and allow access to Baidu Netdisk.") % {"code": number})
    if number in (-3, -9, 31066, 42214):
        return NotFound()
    if number in (-8, 31061):
        return AlreadyExists()
    if number in (-7, 31062):
        return InvalidPath(gettext("Baidu Netdisk doesn't accept this name, or doesn't let the app use this path "
                                   "(%(code)s).") % {"code": number})
    if number == -10:
        return DriverError(gettext("Baidu Netdisk is out of space."))
    if number in (20020, 20021, 31064):
        return DriverError(gettext("Baidu Netdisk doesn't let the app use this path (%(code)s): the base path should be "
                                   "the app's folder, /apps/ followed by the app's name.") % {"code": number})
    if number in (6, 20011, 20013, 20015, 31024) or (pcs and number == 5):
        return AuthError(gettext("The Baidu Netdisk open platform doesn't let the app do this (%(code)s). Please check "
                                 "the app's state and permissions in the open platform's console.") % {"code": number})
    if number in (20012, 31034) or (pcs and number in (4, 17, 18, 19)):
        return BackendError(gettext("Baidu Netdisk is limiting the app's requests (%(code)s). An app that hasn't passed "
                                    "Baidu's review may only make a few. Please try again later.") % {"code": number})
    if number == 42905 or (pcs and number in (1, 2)) or (not pcs and number == 111):
        return BackendError(gettext("Baidu Netdisk is busy (%(code)s). Please try again in a moment.") % {"code": number})
    if number in (10, 31190, 31299, 31355, 31363, 31364):
        return BackendError(gettext("Baidu Netdisk refused the uploaded data (%(code)s). Please try again.") % {"code": number})
    if number == -11015:  # Baidu's upload FAQ: a network failure broke the upload off, and it may be tried again
        return _Transient(gettext("The upload to Baidu Netdisk broke off because of a network failure (%(code)s). "
                                  "Please try again.") % {"code": number})
    if number == 31365:
        return DriverError(gettext("The file is larger than Baidu Netdisk takes for this account's membership (31365)."))
    if number in (31326, 31360, 31362):
        return BackendError(gettext("Baidu Netdisk refused the download link (%(code)s). Please try again.") % {"code": number})
    if message:
        return DriverError(gettext("Baidu Netdisk refused this (%(code)s): %(message)s") % {"code": number, "message": message})
    return DriverError(gettext("Baidu Netdisk refused this (%(code)s).") % {"code": number})


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The JSON answer of a call, also one that reports an error; BackendError when there is none."""
    status = response.status_code
    if 300 <= status < 400:  # the API makes no redirects
        raise redirected(response, SERVICE)
    if status >= 500:
        raise _Transient(gettext("Baidu Netdisk failed (HTTP %(status)s). Please try again later.") % {"status": status})
    answer = json_or_none(response)
    if isinstance(answer, dict) and (status < 400 or _code(answer) is not None):
        return answer
    if status >= 400:
        raise BackendError(gettext("Baidu Netdisk failed (HTTP %(status)s). Please try again later.") % {"status": status})
    raise BackendError(gettext("The data from Baidu Netdisk couldn't be understood. Please try again later."))


def _file_answer(response: httpx.Response, size: int | None) -> bool:
    """Whether a download answer is the file (or its range): a 206, or a 200 unless it is JSON or HTML of another
    length than the file (Baidu's docs answer a refused link with HTTP 200 and an errno)."""
    if response.status_code == 206:
        return True
    if response.status_code != 200:
        return False
    kind = response.headers.get("content-type", "").lower()
    if "json" not in kind and "text/html" not in kind:
        return True
    length = response.headers.get("content-length", "").strip()
    return length.isascii() and length.isdigit() and size is not None and int(length) == size


def _error_body(response: httpx.Response) -> dict[str, Any]:
    """The JSON object of a refused download (at most 64 KiB of it is read); {} when it has none. Closes the response."""
    data = b""
    try:
        for chunk in response.iter_bytes():
            data += chunk
            if len(data) > 64 * KiB:
                break
    except httpx.HTTPError:
        pass
    finally:
        response.close()
    try:
        answer = json.loads(data.decode("utf-8"))
    except ValueError:  # UnicodeDecodeError is one too
        return {}
    return answer if isinstance(answer, dict) else {}


def _size_text(size: int) -> str:
    for unit, scale in (("GB", GiB), ("MB", MiB), ("KB", KiB)):
        if size >= scale:
            return f"{size / scale:g} {unit}"
    return f"{size} B"


# --- listings --------------------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Item:
    """A file or folder as Baidu's listing gave it."""

    name: str
    is_dir: bool
    size: int | None
    modified: datetime.datetime | None
    fs_id: int


def _list_item(raw: Any, folder: str) -> _Item | None:
    """An entry of the listing of the Baidu folder `folder`, or None for one HFile can't show."""
    if not isinstance(raw, dict):
        return None
    name, fs_id = raw.get("server_filename"), as_int(raw.get("fs_id"))
    if not usable_name(name) or fs_id is None:
        return None
    where = raw.get("path")
    if isinstance(where, str) and (where.rpartition("/")[0] or "/").casefold() != folder.casefold():
        return None  # not in this folder: an answer about another path
    is_dir = as_int(raw.get("isdir")) == 1
    return _Item(name=name, is_dir=is_dir, size=None if is_dir else as_int(raw.get("size")),
                 modified=parse_time(raw.get("server_mtime")), fs_id=fs_id)


# --- uploads --------------------------------------------------------------------------------------------

def _needed(value: Any, count: int) -> list[int]:
    """The slices precreate asks for (its block_list of indices); all of them when it names none: Baidu's docs say
    an empty list means [0], the one slice of a small file."""
    wanted: set[int] = set()
    if isinstance(value, list):
        wanted = {index for index in (as_int(item) for item in value) if index is not None and 0 <= index < count}
    return sorted(wanted) or list(range(count))


def _tell(data: BinaryIO) -> int | None:
    """Where a seekable file object is, or None when it can't seek."""
    try:
        if data.seekable():
            return data.tell()
    except (AttributeError, OSError, ValueError):
        pass
    return None


class _Source:
    """The bytes of one upload: seekable (spooled to a temporary file when needed), with the MD5 sums precreate
    asks for (lower case hex): of the whole file, of its first 256 KiB, and of each slice of `slice_size` bytes."""

    def __init__(self, data: BinaryIO, size: int, slice_size: int):
        self.size = size
        self.slice_size = slice_size
        start = _tell(data)
        self._own = start is None
        if self._own:
            self._file: BinaryIO = tempfile.TemporaryFile(prefix="hfile-baidu-")
            self._start = 0
        else:
            self._file = data
            self._start = start
        whole, head, block = (hashlib.md5(usedforsecurity=False) for _ in range(3))
        self.blocks: list[str] = []
        head_left, block_left = min(size, PREHASH_BYTES), slice_size
        try:
            for chunk in exact_body(data, size):
                whole.update(chunk)
                if head_left:
                    piece = chunk[:head_left]
                    head.update(piece)
                    head_left -= len(piece)
                view = memoryview(chunk)
                while view:
                    piece = view[:block_left]
                    block.update(piece)
                    block_left -= len(piece)
                    view = view[len(piece):]
                    if not block_left:
                        self.blocks.append(block.hexdigest())
                        block, block_left = hashlib.md5(usedforsecurity=False), slice_size
                if self._own:
                    self._file.write(chunk)
        except BaseException:
            self.close()
            raise
        if block_left != slice_size or not self.blocks:
            self.blocks.append(block.hexdigest())
        self.md5 = whole.hexdigest()
        self.head_md5 = head.hexdigest()

    def chunks(self, offset: int, length: int) -> Iterator[bytes]:
        """Bytes offset .. offset + length, as chunks for a request body."""
        self._file.seek(self._start + offset)
        return exact_body(self._file, length)

    def close(self) -> None:
        if self._own:
            self._file.close()


# --- the driver -------------------------------------------------------------------------------------------

class BaiduNetdiskDriver(StorageDriver):
    type = "baidu-netdisk"
    label = gettext_lazy("Baidu Netdisk")
    params = (
        Param("app_key", "AppKey",
              help=gettext_lazy("Create an app on the Baidu Netdisk open platform (pan.baidu.com/union): a software app "
                                "for \"personal use\", which works at once without Baidu's review, for up to 10 users. "
                                "In its \"Security settings\", add this site's authorization callback URL to the OAuth "
                                "callback addresses (Baidu takes public addresses only, and a change takes up to an hour "
                                "to work). Then enter the app's AppKey here.")),
        Param("secret_key", "SecretKey", kind="password", secret=True,
              help=gettext_lazy("The SecretKey of the same app.")),
        Param("access_token", gettext_lazy("Access token"), kind="password", required=False, secret=True,
              help=gettext_lazy('With the AppKey and SecretKey filled in, click "Authorize", log in to Baidu and allow '
                                "the app: it's filled in for you.")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Filled in when authorizing. Baidu gives a new one on every refresh and the old one "
                                "stops working; HFile saves the new one itself.")),
        # Secret only so that the form keeps the saved value: after a refresh in the background, saving the form
        # must not put the old expiry next to the new access token.
        Param("token_expires_at", gettext_lazy("Access token expiry"), kind="number", required=False, advanced=True,
              minimum=0, secret=True,
              help=gettext_lazy("Unix time (seconds), filled in when authorizing and refreshing the token; no need "
                                "to change it.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", placeholder="/apps/HFile",
              help=gettext_lazy("The app's own folder: /apps/ followed by the app's name (Baidu Netdisk shows it under "
                                "\"My app data\"), or a folder in it. Baidu only lets apps use their own folder and may "
                                "restrict apps that touch others. HFile makes the folder when it doesn't exist yet.")),
    )

    can_range = True
    can_direct_download = False  # dlinks need the access token and User-Agent pan.baidu.com
    can_direct_upload = False  # no CORS, and the calls need the token
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    forbidden_chars = '\\:*?"<>|'
    max_name_bytes = 255

    oauth = True
    setup_params = ("app_key", "secret_key", "base_path")
    auth_params = ("access_token", "refresh_token", "token_expires_at")

    # Endpoints and tuning; tests point them at a fake and make them small.
    oauth_root: ClassVar[str] = "https://openapi.baidu.com"
    api_root: ClassVar[str] = "https://pan.baidu.com"
    upload_root: ClassVar[str] = "https://d.pcs.baidu.com"  # locateupload, and the upload host when it names none
    # Hosts that may get the access token in their URLs (dlinks, upload hosts from Baidu's answers: Baidu's own
    # domains), and whether http URLs of them are made https.
    trusted_hosts: ClassVar[tuple[str, ...]] = ("baidu.com", "baidupcs.com")
    secure_transport: ClassVar[bool] = True
    page_size: ClassVar[int] = 1000  # entries per list call (Baidu's docs: at most 1000)
    max_pages: ClassVar[int] = 10000
    listing_ttl: ClassVar[float] = 30.0  # seconds a listing serves lookups
    guard_ttl: ClassVar[float] = 5.0  # seconds it can also tell that a name is free
    link_ttl: ClassVar[float] = 3600.0  # seconds a dlink is reused (Baidu: valid 8 hours)
    vip_ttl: ClassVar[float] = 3600.0  # seconds the membership is trusted
    renew_margin: ClassVar[float] = 300.0  # refresh the access token this long before it expires
    slice_size: ClassVar[int] = 4 * MiB  # every membership's slice size
    max_slices: ClassVar[int] = 1024
    # vip_type -> (largest file, largest slice); unknown types are taken as 0.
    memberships: ClassVar[Mapping[int, tuple[int, int]]] = {0: (4 * GiB, 4 * MiB), 1: (10 * GiB, 16 * MiB),
                                                            2: (20 * GiB, 32 * MiB)}
    slice_tries: ClassVar[int] = 3
    retry_delay: ClassVar[float] = 1.0
    calls_per_second: ClassVar[float] = 5.0
    queue_wait: ClassVar[float] = 10.0  # seconds a call may wait for its turn, else BackendError

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._http = http.client(headers={"User-Agent": USER_AGENT})
        self._limiter = RateLimiter(self.calls_per_second, longest=self.queue_wait, service=SERVICE)
        # (why, access token, refresh token): Baidu refused these tokens, so they aren't tried again
        self._dead: tuple[str, Any, Any] | None = None
        self._listings = TTLCache(256, self.listing_ttl)  # storage path -> (time.monotonic() asked, {name: _Item})
        self._links = TTLCache(1024, self.link_ttl)  # (fs_id, size, modified) -> dlink
        self._vip = TTLCache(1, self.vip_ttl)
        self._changes_lock = threading.Lock()
        self._changes = 0  # counts our changes, so a listing fetched meanwhile isn't cached

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        app_key = config.get("app_key")
        if not isinstance(app_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", app_key.strip()):
            errors["app_key"] = gettext("The AppKey should be the letters and digits that the Baidu Netdisk open "
                                        "platform shows for the app.")
        try:
            base = paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        else:
            parts = paths.segments(base)
            if len(parts) < 2 or parts[0] != "apps":
                errors["base_path"] = gettext("The base path should be the app's folder, /apps/ followed by the app's "
                                              "name, or a folder in it.")
        expires = config.get("token_expires_at")
        if expires is not None and (type(expires) is not int or expires < 0):
            errors["token_expires_at"] = gettext("The access token expiry should be an integer of at least 0 (Unix seconds).")
        return errors

    @classmethod
    def _app(cls, config: Mapping[str, Any]) -> tuple[str, str]:
        app_key = str(config.get("app_key") or "").strip()
        secret = str(config.get("secret_key") or "")
        if not app_key or not secret:
            raise DriverError(gettext("Please fill in the AppKey and SecretKey first."))
        return app_key, secret

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        app_key, _ = cls._app(config)
        query = {"response_type": "code", "client_id": app_key, "redirect_uri": redirect_uri, "scope": SCOPE,
                 "state": state}
        return cls.oauth_root + "/oauth/2.0/authorize?" + urllib.parse.urlencode(query)

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        with http.client(headers={"User-Agent": USER_AGENT}) as client:
            answer = cls._token_request(client, config, {"grant_type": "authorization_code", "code": code,
                                                         "redirect_uri": redirect_uri})
        return cls._tokens(answer)

    # -- login

    @staticmethod
    def _tokens(answer: Mapping[str, Any]) -> dict[str, Any]:
        lifetime = as_int(answer.get("expires_in")) or 30 * 24 * 3600
        return {"access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
                "token_expires_at": int(time.time()) + lifetime}

    @classmethod
    def _token_request(cls, client: httpx.Client, config: Mapping[str, Any], fields: Mapping[str, str]) -> dict[str, Any]:
        """GET Baidu's token endpoint (as its docs do) with the app's AppKey and SecretKey; the answer with the tokens."""
        app_key, secret = cls._app(config)
        try:
            response = client.get(_url(cls.oauth_root + "/oauth/2.0/token",
                                       {**fields, "client_id": app_key, "client_secret": secret}))
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        status = response.status_code
        if status == 429 or status >= 500:
            raise BackendError(gettext("Baidu's authorization service is unavailable for now (HTTP %(status)s). "
                                       "Please try again later.") % {"status": status})
        answer = json_or_none(response)
        answer = answer if isinstance(answer, dict) else {}
        access, refresh = answer.get("access_token"), answer.get("refresh_token")
        if status == 200 and isinstance(access, str) and access and isinstance(refresh, str) and refresh:
            scope = answer.get("scope")
            if isinstance(scope, str) and "netdisk" not in scope.replace(",", " ").split():
                raise AuthError(gettext("The authorization doesn't include Baidu Netdisk. Please authorize again and "
                                        "allow access to Baidu Netdisk."))
            return answer
        error = answer.get("error")
        if not isinstance(error, str) or not error:
            raise BackendError(gettext("Baidu's authorization service gave no token (HTTP %(status)s). Please try again "
                                       "later.") % {"status": status})
        if error in ("invalid_client", "unauthorized_client"):
            raise AuthError(gettext("Baidu doesn't accept the AppKey or SecretKey (%(code)s). Please check them.")
                            % {"code": short(error, 40)})
        if error in ("invalid_grant", "invalid_request", "expired_token"):
            raise AuthError(gettext("Baidu's authorization has expired or been withdrawn (%(code)s). Please authorize "
                                    "again in the storage source's settings.") % {"code": short(error, 40)})
        raise AuthError(gettext("Baidu refused the authorization (%(code)s): %(message)s")
                        % {"code": short(error, 40), "message": short(answer.get("error_description"))})

    def _refused_before(self) -> str | None:
        """Why Baidu refused the tokens the config has now, when it did (called under token_lock). New ones (an
        authorization again, or what another instance of the storage refreshed, adopted into config) are tried."""
        if self._dead is not None:
            message, access, refresh = self._dead
            if self.config.get("access_token") == access and self.config.get("refresh_token") == refresh:
                return message
            self._dead = None
        return None

    def _token(self) -> str:
        with self.token_lock:
            refused = self._refused_before()
            if refused is not None:
                raise AuthError(refused)
            token = self.config.get("access_token")
            expires = as_int(self.config.get("token_expires_at")) or 0
            if token and (expires == 0 or time.time() < expires - self.renew_margin):  # unknown expiry: try it
                return token
            return self._refresh_locked()

    def _refresh_after_refusal(self, token: str) -> None:
        """Baidu refused `token`: refresh, unless another thread already did."""
        with self.token_lock:
            refused = self._refused_before()
            if refused is not None:
                raise AuthError(refused)
            if self.config.get("access_token") == token:
                self._refresh_locked()

    def _refresh_locked(self) -> str:
        refresh = self.config.get("refresh_token")
        if not refresh:
            raise AuthError(gettext('Not authorized yet: click "Authorize" in the storage source\'s settings and log '
                                    "in to Baidu."))
        try:
            answer = self._token_request(self._http, self.config,
                                         {"grant_type": "refresh_token", "refresh_token": refresh})
        except AuthError as error:
            # Baidu's docs: a refused refresh kills the refresh token, and trying again only gets the app limited.
            self._dead = (error.message, self.config.get("access_token"), refresh)
            self.log.warning("Baidu refused to refresh the access token; no more calls until it's authorized again")
            raise
        tokens = self._tokens(answer)
        self.update_config(tokens)  # the old refresh token is dead now: save the new ones before using them
        self.log.info("refreshed the access token")
        return tokens["access_token"]

    def _refusal(self, token: str, answer: Mapping[str, Any]) -> DriverError:
        """The error of a refused call. When Baidu refused the token itself, these tokens aren't used again
        (retrying a refused token only gets the app limited), unless another thread has new ones meanwhile."""
        error = _error(answer)
        code = _code(answer)
        if code is not None and (code[1] == _WITHDRAWN or _token_refused(code)):
            with self.token_lock:
                if self._refused_before() is None and self.config.get("access_token") == token:
                    self._dead = (error.message, token, self.config.get("refresh_token"))
                    self.log.warning("Baidu Netdisk refused the authorization (%s); no more calls until it's "
                                     "authorized again", code[1])
        return error

    # -- requests

    def _call(self, method: str, url: str, params: Mapping[str, Any], *, data: Mapping[str, Any] | None = None,
              content: Callable[[], Iterator[bytes]] | None = None, headers: Mapping[str, str] | None = None,
              pace: bool = True) -> dict[str, Any]:
        """One call with the access token in the query; the answer when it reports no error, else DriverError. A
        refused token is refreshed once and the call made again (`content` makes the body afresh)."""
        refreshed = False
        while True:
            token = self._token()
            if pace:
                self._limiter.wait()
            try:
                response = self._http.request(method, _url(url, {**params, "access_token": token}), data=data,
                                              content=content() if content is not None else None, headers=headers)
            except httpx.HTTPError as error:
                raise _Transient(transport_error(error, SERVICE).message) from None
            answer = _answer(response)
            code = _code(answer)
            if code is None:
                return answer
            if code[1] != _WITHDRAWN and _token_refused(code) and not refreshed:
                refreshed = True
                self.log.info("Baidu Netdisk refused the access token (%s); refreshing it", code[1])
                self._refresh_after_refusal(token)
                continue
            raise self._refusal(token, answer)

    def _xpan(self, method: str, api: str, params: Mapping[str, Any],
              data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self._call(method, self.api_root + api, params, data=data)

    def _secure(self, url: Any) -> str | None:
        """`url` when it may get the access token: http(s) on a trusted host, http made https. None otherwise."""
        if not isinstance(url, str):
            return None
        url = url.strip()
        scheme, host, _port = origin(url)
        if not scheme or not any(host == trusted or host.endswith("." + trusted) for trusted in self.trusted_hosts):
            return None
        if scheme == "http" and self.secure_transport:
            url = "https://" + url[len("http://"):]
        return url

    # -- paths and listings

    def _abs(self, path: str) -> str:
        return paths.rebase(path, self._base)

    def _base_missing(self) -> NotFound:
        return NotFound(gettext("The base path %(base)s doesn't exist in %(service)s.")
                        % {"base": self._base, "service": SERVICE})

    def _change_count(self) -> int:
        with self._changes_lock:
            return self._changes

    def _changed(self, *changed: str) -> None:
        """Our call changed these storage paths, or may have when it failed: forget the listings at and below them
        and those of their folders."""
        with self._changes_lock:
            self._changes += 1
            for path in changed:
                self._listings.discard_if(lambda key, path=path: paths.is_inside(key, path))
                if path != paths.ROOT:
                    self._listings.pop(paths.parent(path))

    def _fetch(self, path: str) -> dict[str, _Item]:
        """Everything in the folder at `path`, from Baidu (all pages)."""
        folder = self._abs(path)
        items: dict[str, _Item] = {}
        start = 0
        for _ in range(self.max_pages):
            answer = self._xpan("GET", "/rest/2.0/xpan/file", {"method": "list", "dir": folder, "start": start,
                                                                "limit": self.page_size})
            entries = answer.get("list")
            if entries is None and isinstance(answer.get("data"), dict):  # some answers wrap it (the docs say)
                entries = answer["data"].get("list")
            if not isinstance(entries, list):
                raise BackendError(gettext("The file list from Baidu Netdisk couldn't be understood. Please try again later."))
            for raw in entries:
                item = _list_item(raw, folder)
                if item is not None and (item.name not in items or item.is_dir):
                    items[item.name] = item
            if len(entries) < self.page_size:
                return items
            start += len(entries)
        raise BackendError(gettext("The Baidu Netdisk file list never ends. Please try again later."))

    def _listing(self, path: str, *, within: float | None = None, fresh: bool = False) -> dict[str, _Item]:
        """What is in the folder at `path`: the cached listing when Baidu gave it at most `within` seconds ago
        (listing_ttl by default), else Baidu's. NotFound when Baidu has no such folder."""
        if not fresh:
            cached = self._listings.get(path)
            if cached is not None and time.monotonic() - cached[0] <= (self.listing_ttl if within is None else within):
                return cached[1]
        changes, asked = self._change_count(), time.monotonic()
        try:
            items = self._fetch(path)
        except NotFound:
            if path == paths.ROOT:
                raise self._base_missing() from None
            raise
        with self._changes_lock:
            if self._changes == changes:  # else a change of ours may be missing from it
                self._listings.put(path, (asked, items))
        return items

    def _lookup(self, path: str) -> _Item:
        """The file or folder at `path` (not the root), from the listing of its folder. NotFound."""
        parent, name = paths.split(path)
        item = self._listing(parent).get(name)
        if item is None:
            raise NotFound()
        return item

    def _folder_listing(self, folder: str) -> dict[str, _Item]:
        """The listing of the folder at `folder` that tells whether a name is free in it. NotFound, NotADirectory."""
        if folder != paths.ROOT and not self._lookup(folder).is_dir:
            raise NotADirectory()
        return self._listing(folder, within=self.guard_ttl)

    @staticmethod
    def _entry(path: str, item: _Item) -> Entry:
        return Entry(name=item.name, path=path, is_dir=item.is_dir, size=None if item.is_dir else item.size,
                     modified=item.modified)

    # -- reading

    def check(self) -> None:
        try:
            self._listing(paths.ROOT, fresh=True)
        except NotFound:
            # A new app's folder only exists once something was made in it. It's the app's own, so make it and the
            # folders in it down to the base path, one by one: create needs the folder above.
            parts = paths.segments(self._base)
            try:
                for depth in range(2, len(parts) + 1):
                    try:
                        self._xpan("POST", "/rest/2.0/xpan/file", {"method": "create"},
                                   {"path": "/" + "/".join(parts[:depth]), "isdir": "1", "rtype": "0"})
                    except AlreadyExists:
                        pass
            except NotFound:
                raise self._base_missing() from None
            finally:
                self._changed(paths.ROOT)
            self._listing(paths.ROOT, fresh=True)

    def list(self, path: str) -> list[Entry]:
        if path != paths.ROOT and not self._lookup(path).is_dir:
            raise NotADirectory()
        items = self._listing(path, fresh=True)
        return [self._entry(paths.join(path, item.name), item) for item in items.values()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._listing(paths.ROOT)
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._lookup(path))

    def _link(self, item: _Item, *, fresh: bool = False) -> str:
        """A dlink of the file (filemetas); it needs the access token and User-Agent pan.baidu.com."""
        key = (item.fs_id, item.size, item.modified)
        if not fresh:
            cached = self._links.get(key)
            if cached is not None:
                return cached
        answer = self._xpan("GET", "/rest/2.0/xpan/multimedia", {"method": "filemetas", "fsids": f"[{item.fs_id}]",
                                                                  "dlink": 1})
        entries = answer.get("list")
        info = next((entry for entry in entries if isinstance(entry, dict) and as_int(entry.get("fs_id")) == item.fs_id),
                    None) if isinstance(entries, list) else None
        if info is not None and as_int(info.get("errno")):
            raise _error(info)
        if info is None or not isinstance(info.get("dlink"), str) or not info["dlink"]:
            raise NotFound(gettext("Baidu Netdisk gave no download address for this file."))
        link = self._secure(info["dlink"])
        if link is None:
            raise BackendError(gettext("Baidu Netdisk gave an address that isn't Baidu's, so the access token wasn't "
                                       "sent there."))
        self._links.put(key, link)
        return link

    def _read(self, item: _Item, offset: int, length: int | None) -> ReadStream:
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, item.size)
        if span is not None:
            headers["Range"] = span
        relinked = refreshed = False
        while True:
            link = self._link(item, fresh=relinked)
            token = self._token()
            url = link + ("&" if "?" in link else "?") + urllib.parse.urlencode({"access_token": token},
                                                                                quote_via=urllib.parse.quote)
            try:
                response = self._http.send(self._http.build_request("GET", url, headers=headers), stream=True,
                                           follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if _file_answer(response, item.size):
                return response_stream(response, offset=offset, length=length, total=item.size, service=SERVICE,
                                       modified=item.modified)
            status = response.status_code
            answer = _error_body(response)
            code = _code(answer)
            if code is not None and code[1] != _WITHDRAWN and _token_refused(code) and not refreshed:
                refreshed = True
                self._refresh_after_refusal(token)
                continue
            if not relinked and (code[1] in _RELINK_CODES if code is not None else status in (403, 404, 410)):
                relinked = True  # an expired or refused link: ask for a new one
                continue
            if code is not None:
                raise self._refusal(token, answer)
            if status == 200:
                raise BackendError(gettext("The data from Baidu Netdisk couldn't be understood. Please try again later."))
            raise BackendError(gettext("Downloading from %(service)s failed (HTTP %(status_code)s).")
                               % {"service": SERVICE, "status_code": status})

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        if path == paths.ROOT:
            raise NotFound()
        item = self._lookup(path)
        if item.is_dir:
            raise NotFound()
        return self._read(item, offset, length)

    # -- writing

    def _make_folder(self, path: str) -> None:
        """create isdir=1 at `path`; its folder must exist. AlreadyExists when the name is taken."""
        try:
            self._xpan("POST", "/rest/2.0/xpan/file", {"method": "create"},
                       {"path": self._abs(path), "isdir": "1", "rtype": "0"})
        finally:
            self._changed(path)

    def _ensure_folder(self, path: str) -> None:
        """The folder at `path` exists afterwards, made with the missing ones above it. NotADirectory for a file."""
        if path == paths.ROOT:
            return
        try:
            item = self._lookup(path)
        except NotFound:
            parent, name = paths.split(path)
            self._ensure_folder(parent)
            try:
                self._make_folder(path)
            except AlreadyExists:  # made meanwhile, or a file has the name
                item = self._listing(parent, fresh=True).get(name)
                if item is None or not item.is_dir:
                    raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": path}) from None
            return
        if not item.is_dir:
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": path})

    def _slice_size(self, size: int) -> int:
        """The slice size for a file of `size` bytes: slice_size, or a larger multiple of it for a file that would
        need more than max_slices (members only). DriverError when the account's membership doesn't take the file."""
        base = self.slice_size
        if size <= min(largest for largest, _ in self.memberships.values()) and -(-size // base) <= self.max_slices:
            return base
        vip = self._vip.get("vip")
        if vip is None:
            answer = self._xpan("GET", "/rest/2.0/xpan/nas", {"method": "uinfo", "vip_version": "v2"})
            vip = as_int(answer.get("vip_type")) or 0
            self._vip.put("vip", vip)
        largest_file, largest_slice = self.memberships.get(vip, self.memberships[0])
        per_slice = -(-size // self.max_slices)  # bytes per slice to stay within max_slices, rounded up
        slice_size = max(base, -(-per_slice // base) * base)  # ... to a multiple of base
        if size > largest_file or slice_size > largest_slice:
            raise DriverError(gettext("The file is too large for Baidu Netdisk: this account takes files of at most "
                                      "%(limit)s.") % {"limit": _size_text(largest_file)})
        return slice_size

    def _upload_servers(self, target: str, upload_id: str) -> list[str]:
        """The upload hosts locateupload names (Baidu's own only), or upload_root when it names none."""
        answer = self._call("GET", self.upload_root + "/rest/2.0/pcs/file", {
            "method": "locateupload", "appid": LOCATE_APP_ID, "path": target, "uploadid": upload_id,
            "upload_version": "2.0"})
        found: list[str] = []
        for key in ("servers", "bak_servers"):
            entries = answer.get(key)
            for entry in entries if isinstance(entries, list) else []:
                url = self._secure(entry.get("server")) if isinstance(entry, dict) else None
                if url is not None and url.rstrip("/") not in found:
                    found.append(url.rstrip("/"))
        return found or [self.upload_root]

    def _put_slice(self, servers: list[str], target: str, upload_id: str, index: int, source: _Source) -> None:
        """superfile2: slice `index` of the file, tried slice_tries times (the next host each time) on a broken
        connection, a 5xx, Baidu's -11015 or an MD5 that isn't the slice's."""
        offset = index * source.slice_size
        length = min(source.slice_size, source.size - offset)
        failure: DriverError = BackendError()
        for attempt in range(max(1, self.slice_tries)):
            if attempt:
                time.sleep(self.retry_delay * attempt)
            boundary = secrets.token_hex(16)
            head = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="blob"\r\n'
                    "Content-Type: application/octet-stream\r\n\r\n").encode("ascii")
            tail = f"\r\n--{boundary}--\r\n".encode("ascii")

            def body(head: bytes = head, tail: bytes = tail) -> Iterator[bytes]:
                yield head
                yield from source.chunks(offset, length)
                yield tail

            try:
                answer = self._call("POST", servers[attempt % len(servers)] + "/rest/2.0/pcs/superfile2", {
                    "method": "upload", "type": "tmpfile", "path": target, "uploadid": upload_id, "partseq": index,
                }, content=body, pace=False, headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                                      "Content-Length": str(len(head) + length + len(tail))})
            except _Transient as error:
                failure = error
                continue
            md5 = answer.get("md5")
            if not isinstance(md5, str) or md5.lower() == source.blocks[index]:
                return
            failure = BackendError(gettext("Baidu Netdisk received an upload part that isn't what was sent. Please "
                                           "try again."))
        raise failure

    def _upload(self, target: str, source: _Source) -> None:
        blocks = json.dumps(source.blocks)
        answer = self._xpan("POST", "/rest/2.0/xpan/file", {"method": "precreate"}, {
            "path": target, "size": str(source.size), "isdir": "0", "block_list": blocks, "autoinit": "1", "rtype": "3",
            "content-md5": source.md5, "slice-md5": source.head_md5})
        if as_int(answer.get("return_type")) == 2:
            return  # 秒传: Baidu had these bytes and made the file from them
        upload_id = answer.get("uploadid")
        if not isinstance(upload_id, str) or not upload_id:
            raise BackendError(gettext("Baidu Netdisk's upload start gave no upload ID. Please try again later."))
        servers = self._upload_servers(target, upload_id)
        for index in _needed(answer.get("block_list"), len(source.blocks)):
            self._put_slice(servers, target, upload_id, index, source)
        self._xpan("POST", "/rest/2.0/xpan/file", {"method": "create"}, {
            "path": target, "size": str(source.size), "isdir": "0", "block_list": blocks, "uploadid": upload_id,
            "rtype": "3"})

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        if size <= 0:
            raise Unsupported(gettext("Baidu Netdisk doesn't accept empty files."))
        slice_size = self._slice_size(size)  # before anything is read or made: too large for the account
        parent, name = paths.split(path)
        self._ensure_folder(parent)
        old = self._listing(parent, within=self.guard_ttl).get(name)
        if old is not None and old.is_dir:
            raise AlreadyExists()
        source = _Source(data, size, slice_size)
        try:
            self._upload(self._abs(path), source)
        finally:
            source.close()
            self._changed(path)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent, name = paths.split(path)
        self._ensure_folder(parent)
        if name in self._listing(parent, within=self.guard_ttl):
            raise AlreadyExists()
        self._make_folder(path)

    def _manage(self, opera: str, filelist: list[Any], *changed: str) -> None:
        """One filemanager call (copy, move, rename, delete), synchronous, failing when the target is taken."""
        form = {"async": "0", "filelist": json.dumps(filelist, ensure_ascii=False)}
        if opera != "delete":
            form["ondup"] = "fail"
        try:
            answer = self._xpan("POST", "/rest/2.0/xpan/file", {"method": "filemanager", "opera": opera}, form)
        finally:
            self._changed(*changed)
        items = answer.get("info")
        if isinstance(items, list) and any(isinstance(item, dict) and as_int(item.get("errno")) for item in items):
            raise _error(answer)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._lookup(path)
        self._manage("delete", [self._abs(path)], path)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        item = self._lookup(path)
        if new_name == item.name:
            return
        parent = paths.parent(path)
        if new_name in self._listing(parent, within=self.guard_ttl):
            raise AlreadyExists()
        self._manage("rename", [{"path": self._abs(path), "newname": new_name}], path, paths.join(parent, new_name))

    def _target(self, path: str, folder: str, copy: bool) -> _Item:
        """Checks of move() and copy(): the item at `path`, when `folder` is a folder without its name."""
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy
                              else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy
                              else gettext("A folder can't be moved into itself."))
        item = self._lookup(path)
        if item.name in self._folder_listing(folder):
            raise AlreadyExists()
        return item

    def move(self, path: str, folder: str) -> None:
        if path != paths.ROOT and paths.parent(path) == folder:
            self._lookup(path)
            return  # already there
        item = self._target(path, folder, copy=False)
        self._manage("move", [{"path": self._abs(path), "dest": self._abs(folder), "newname": item.name}],
                     path, paths.join(folder, item.name))

    def copy(self, path: str, folder: str) -> None:
        item = self._target(path, folder, copy=True)
        self._manage("copy", [{"path": self._abs(path), "dest": self._abs(folder), "newname": item.name}],
                     paths.join(folder, item.name))
