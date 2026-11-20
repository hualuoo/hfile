"""123 Cloud Drive (123云盘, https://www.123pan.com) through its open platform, the API at https://open-api.123pan.com.

Documentation: https://123yunpan.yuque.com/org-wiki-123yunpan-muaork/cr6ced, and its pages (append them to that
URL): rules, codes and QPS: txgcvbfgh0gtuad5; access token: gn1nai4x0v0ry9ki; file list: zrip9b0ye81zimv4; mkdir:
ouyvcxqg3185zzk4; uploads (v2): xogi45g7okqk7svr, txow0iqviqsgotfl, scs8yg89yz8immus, fzzc5o8gok517720; rename:
ha6mfe9tteht5skc; move: rsyfsn1gnpgo4m4f; recycle bin: en07662k2kki4bo6; copy: thpz0w9er500pob9; download:
fnf60phsushn8ip2; the mount apps' codes and per-account QPS: kf05anzt1r0qnudd; FAQ: ghfd4h0l6c6y6oi8. Also
https://www.123pan.com/developer.

Paths
    IdDriver (apps/storages/drivers/_ids.py): the account's root is folder id 0, and a path is found by listing
    each folder from there. Listings are /api/v2/file/list, `page_size` (at most 100) a call while lastFileId
    isn't -1; they include what is in the recycle bin (trashed 1), which is left out. Their times have no zone:
    they are China's (UTC+8; the old list API says +0800 CST). An id 123 doesn't know (5066) is looked up again
    from its parent.

Login
    The admin's own developer credentials: once a developer package (开发者权益包) is bought, 123 sends the
    client_id and client_secret to the account's messages (站内信). POST /api/v1/access_token gives an access token
    for 30 days (expiredAt), sent as "Authorization: Bearer <token>" with "Platform: open_platform". One client ID
    may have at most 3 tokens in use, and getting more logs the older ones out; so the token is saved
    (update_config, as "<client ID>:<token>": a client ID filled in later, maybe another account's, doesn't use
    it) and reused until `renew_before` seconds before it expires, or until 123 refuses it (code 401; but 401 "tokens
    number has exceeded the limit" is an AuthError: its FAQ says the account is logged in in too many places, and a
    token counts as one). Then another one is got, once and under a lock. One refused within `token_gap` seconds of
    getting it isn't replaced again: something else uses the client ID, and replacing it would log that out in turn
    (AuthError). Refused credentials are remembered for `refusal_ttl` seconds. 123's OAuth (yun.123pan.com/auth) is
    only for mount apps its business team approves (companies), so it isn't offered.

Requests
    Each API is paced at about two thirds of the QPS 123 documents for it (`rates`: 开发须知's table, and the mount
    apps' per-account one where it is lower; 2 a second for the APIs neither lists), and a call waits its turn for
    at most `queue_wait` seconds (then BackendError, so that a burst doesn't hold all of the site's threads). A
    throttled call (code 429, or HTTP 429) is tried again after 1, 2, 4... seconds, or what Retry-After says, for
    at most `throttle_wait` seconds.

Changes
    mkdir: /upload/v1/file/mkdir (123 refuses a taken name). rename: PUT /api/v1/file/name. move:
    /api/v1/file/move. delete: /api/v1/file/trash, into 123's recycle bin. copy: /api/v1/file/copy copies one file;
    a folder is copied as a new folder, with what is in it copied a file and a folder at a time (but names HFile
    can't show, which listings leave out).

Downloads
    Through this site only (can_direct_download False). /api/v1/file/download_info gives a CDN link, and what is
    downloaded with it counts as the account's own download traffic (自用下载流量: 1 GB a day without VIP, then
    code 5113); the open platform doesn't say such links may be handed to other people, nor whether a browser can
    use one as it is. 123's way to hand links out is 直链 (a folder made a direct-link space in 123's app, with
    its own traffic), which isn't used here: it needs that setting, and its URL signing is documented in 123's help
    center, not the open platform's documentation (the type 123-link, pan123_link.py, signs such links). open() fetches
    the link with the Range and without the token; links are reused for `link_ttl`
    seconds while the file's id, size and time stay the same (123 doesn't say whether a file it replaces keeps its
    id), and asked for again when the CDN refuses one (403, 404, 410).

Uploads
    123 wants the file's MD5 first, so data that can't seek is spooled to a temporary file. /upload/v2/file/create
    (duplicate 2: it replaces a file of that name) answers reuse true when 123 has these bytes already (秒传:
    done), or gives the slice size and upload servers: each slice goes to one of them (/upload/v2/file/slice,
    multipart, with its MD5; on failure again on the next, `slice_tries` times), then
    /upload/v2/file/upload_complete is asked every `poll_interval` seconds while it isn't done (completed false, or
    code 20103, which OpenList's driver waits out too), for at most `complete_wait` seconds. The slices carry the
    access token, so an upload server given as http:// is used over https://. At most 10 GB a file (123's limit
    for developers).

Names
    Without "\\:*?|>< and at most 254 bytes (123 says fewer than 255 characters, or 256).

Limits
    No search: 123's searches the whole account. Not tried against the real service: the tests use a fake built
    from the documentation above.
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
import re
import secrets
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http
from ._cloud import (
    RateLimiter,
    TTLCache,
    as_int,
    exact_body,
    json_or_none,
    range_header,
    redirected,
    response_stream,
    retry_after,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, StaleFolder, now
from .base import AlreadyExists, AuthError, BackendError, DriverError, NotFound, Param, ReadStream

SERVICE = gettext_lazy("123 Cloud Drive")
CHINA = datetime.timezone(datetime.timedelta(hours=8))  # the zone of 123's times, which don't give it

# The APIs: paths below api_root (SLICE: below an upload server).
TOKEN = "/api/v1/access_token"
LIST = "/api/v2/file/list"
MKDIR = "/upload/v1/file/mkdir"
RENAME = "/api/v1/file/name"
MOVE = "/api/v1/file/move"
TRASH = "/api/v1/file/trash"
COPY = "/api/v1/file/copy"
DOWNLOAD = "/api/v1/file/download_info"
CREATE = "/upload/v2/file/create"
SLICE = "/upload/v2/file/slice"
COMPLETE = "/upload/v2/file/upload_complete"

INTERNAL = 1  # "内部错误"
NOT_FOUND = 5066  # "文件不存在"
NO_TRAFFIC = 5113  # the day's download traffic is used up
PROCESSING = 20103  # upload_complete while 123 still checks the file (not in the docs; OpenList's driver waits)
# Words of 123's messages, matched in its answers.
_GONE = "不存在"  # i18n: data
_TAKEN = ("同名", "重名", "已存在", "已经存在")  # i18n: data


# --- answers and errors -----------------------------------------------------------------------------

def _data(answer: Mapping[str, Any]) -> dict[str, Any]:
    data = answer.get("data")
    return data if isinstance(data, dict) else {}


def _answer(response: httpx.Response) -> tuple[int, dict[str, Any]]:
    """The code of an answer (0: done) and the answer; a failed exchange is a BackendError. HTTP 401 and 429
    without a code of their own (the gateway's answers) count as those codes."""
    status = response.status_code
    if 300 <= status < 400:
        raise redirected(response, SERVICE)
    answer = json_or_none(response)
    answer = answer if isinstance(answer, dict) else {}
    code = as_int(answer.get("code"))
    if status in (401, 429) and not code:
        return status, answer
    if status >= 500 or (status != 200 and not code):
        raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                           % {"service": SERVICE, "status_code": status})
    if code is None:
        raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                           % {"service": SERVICE})
    return code, answer


def _detail(code: int, message: str) -> str:
    return (gettext(" (%(code)s): %(message)s") % {"code": code, "message": message} if message
            else gettext(" (%(code)s)") % {"code": code})


def _too_many_logins(message: str) -> bool:
    """123's "tokens number has exceeded the limit" (its FAQ): the account is logged in in too many places."""
    lowered = message.lower()
    return "token" in lowered and "exceeded" in lowered


def _error(code: int, answer: Mapping[str, Any]) -> DriverError:
    """The DriverError for an answer whose code isn't 0. A refused token first, whatever the message says; then
    the words of the message, which also tell what an undocumented code (or 1) means."""
    message = short(answer.get("message") or "")
    if _too_many_logins(message):
        return AuthError(gettext("%(service)s says the account is logged in in too many places%(detail)s. Please log "
                                 "out of %(service)s where it isn't needed, then try again.")
                         % {"service": SERVICE, "detail": _detail(code, message)})
    if code == 401:
        return AuthError(gettext("%(service)s refused the access token%(detail)s. Please check the client ID and "
                                 "secret, and that the account's developer package hasn't expired.")
                         % {"service": SERVICE, "detail": _detail(code, message)})
    if code == NOT_FOUND or _GONE in message:
        return NotFound()
    if any(word in message for word in _TAKEN):
        return AlreadyExists()
    if code == 429:
        return BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.")
                            % {"service": SERVICE})
    if code == NO_TRAFFIC:
        return DriverError(gettext("This account's download traffic on %(service)s is used up for today%(detail)s.")
                           % {"service": SERVICE, "detail": _detail(code, message)})
    if code == INTERNAL:
        return BackendError(gettext("%(service)s failed%(detail)s. Please try again later.")
                            % {"service": SERVICE, "detail": _detail(code, message)})
    return DriverError(gettext("%(service)s refused this (%(code)s): %(message)s")
                       % {"service": SERVICE, "code": code, "message": message})


# --- listings, uploads ----------------------------------------------------------------------------------

def _time(value: Any) -> datetime.datetime | None:
    """A time of 123's: "2025-02-24 17:56:45", China's time (the answers don't give the zone), or ISO 8601 with
    its zone (expiredAt); None when missing or unreadable."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=CHINA)


def _list_node(item: Any) -> Node | None:
    """A node from an entry of /api/v2/file/list; None for one in the recycle bin, or one HFile can't show."""
    if not isinstance(item, dict) or as_int(item.get("trashed")) == 1:
        return None
    file_id, name = as_int(item.get("fileId")), item.get("filename")
    if not file_id or not usable_name(name):
        return None
    is_dir = as_int(item.get("type")) == 1
    return Node(id=str(file_id), name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                modified=_time(item.get("updateAt")) or _time(item.get("createAt")))


def _upload_server(value: Any, *, secure: bool) -> str | None:
    """scheme://host[:port] of an upload server create names; None when it isn't one. With `secure`, plain http
    becomes https: the slices carry the access token."""
    if not isinstance(value, str):
        return None
    try:
        url = httpx.URL(value.strip())
    except httpx.InvalidURL:
        return None
    if url.scheme not in ("http", "https") or not url.host:
        return None
    host = f"[{url.host}]" if ":" in url.host else url.host
    upgrade = secure and url.scheme == "http"
    port = url.port if url.port and not (upgrade and url.port == 80) else None
    return f"{'https' if upgrade else url.scheme}://{host}" + (f":{port}" if port else "")


def _tell(data: BinaryIO) -> int | None:
    """Where a seekable file object is, or None when it can't seek."""
    try:
        if data.seekable():
            return data.tell()
    except (AttributeError, OSError, ValueError):
        pass
    return None


class _Source:
    """The bytes of one upload: seekable (spooled to a temporary file when `data` can't seek), with the MD5 that 123
    wants before them."""

    def __init__(self, data: BinaryIO, size: int):
        self.size = size
        start = _tell(data)
        self._own = start is None
        self._file: BinaryIO = tempfile.TemporaryFile(prefix="hfile-123-") if self._own else data
        self._start = start or 0
        digest = hashlib.md5(usedforsecurity=False)
        try:
            for chunk in exact_body(data, size):
                digest.update(chunk)
                if self._own:
                    self._file.write(chunk)
        except BaseException:
            self.close()
            raise
        self.md5 = digest.hexdigest()

    def chunks(self, offset: int, length: int) -> Iterator[bytes]:
        """Bytes offset .. offset + length, as chunks for a request body."""
        self._file.seek(self._start + offset)
        return exact_body(self._file, length)

    def md5_of(self, offset: int, length: int) -> str:
        digest = hashlib.md5(usedforsecurity=False)
        for chunk in self.chunks(offset, length):
            digest.update(chunk)
        return digest.hexdigest()

    def close(self) -> None:
        if self._own:
            self._file.close()


# --- the driver -------------------------------------------------------------------------------------

class Open123Driver(IdDriver):
    type = "open123"
    label = gettext_lazy("123 Cloud Drive (open platform)")
    service = SERVICE
    root_id = "0"
    params = (
        Param("client_id", gettext_lazy("Client ID"),
              help=gettext_lazy("Once you buy a developer package on the 123 Cloud Drive open platform "
                                "(https://www.123pan.com/developer), 123 Cloud Drive sends the client_id and "
                                "client_secret to the account's messages.")),
        Param("client_secret", gettext_lazy("Client secret"), kind="password", secret=True,
              help=gettext_lazy("The client_secret from the same message.")),
        Param("access_token", gettext_lazy("Access token"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("HFile gets it with the client ID and secret, and saves it: one client ID can have at "
                                "most 3 tokens in use (each storage source using it holds one), so each is kept for "
                                "its 30 days. No need to fill it in.")),
        # Secret only so that the form keeps the saved value: saving the form after HFile got a new token
        # must not put the old expiry next to it.
        Param("token_expires_at", gettext_lazy("Access token expiry"), kind="number", required=False, advanced=True,
              minimum=0, secret=True,
              help=gettext_lazy("Unix time (seconds), filled in when HFile gets the access token; no need to change it.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in 123 Cloud Drive that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = False  # see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    upload_conflict = "replace"  # create's duplicate 2
    forbidden_chars = '"\\:*?|><'
    max_name_bytes = 254

    setup_params = ("client_id", "client_secret")

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://open-api.123pan.com"
    page_size: ClassVar[int] = 100  # entries per listing call (123's maximum)
    max_pages: ClassVar[int] = 10_000
    # Calls a second per API: about two thirds of the QPS 123 documents (开发须知; in brackets the mount apps'
    # per-account table, where it is lower), 2 for the APIs neither lists. Slices aren't paced.
    rates: ClassVar[Mapping[str, float]] = {
        TOKEN: 1.0,  # 10 (8); a token lasts 30 days
        LIST: 10.0,  # 15
        MKDIR: 3.0,  # 20 (5)
        MOVE: 6.0,  # 20 (10)
        TRASH: 3.0,  # (5)
        DOWNLOAD: 3.0,  # (5)
        CREATE: 3.0,  # v1's create: (5)
        COMPLETE: 10.0,  # v1's: (20)
        RENAME: 2.0,
        COPY: 2.0,
    }
    queue_wait: ClassVar[float] = 10.0  # seconds a call may wait for its turn, else BackendError
    throttle_delay: ClassVar[float] = 1.0  # the first wait after a 429; it doubles
    throttle_wait: ClassVar[float] = 20.0  # seconds a throttled call is tried again for
    renew_before: ClassVar[float] = 3600.0  # seconds before its expiry a token is replaced
    token_gap: ClassVar[float] = 60.0  # seconds after getting a token in which a refusal of it isn't answered with another
    refusal_ttl: ClassVar[float] = 30.0  # seconds refused credentials are remembered
    link_ttl: ClassVar[float] = 300.0  # seconds a download link is reused
    max_file_size: ClassVar[int] = 10 * 1024 ** 3
    slice_tries: ClassVar[int] = 3
    retry_delay: ClassVar[float] = 1.0
    poll_interval: ClassVar[float] = 1.0  # upload_complete asked again after this, while the upload isn't done
    complete_wait: ClassVar[float] = 60.0

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)  # listing_ttl, guard_ttl, folder_ttl: IdDriver's
        self._http = http.client()
        self._limiters = {api: RateLimiter(rate, longest=self.queue_wait, service=SERVICE)
                          for api, rate in self.rates.items()}
        self._refused: tuple[float, str] | None = None  # (until, message) of refused credentials
        self._issued: float | None = None  # time.monotonic() when this instance last got a token
        self._links = TTLCache(1024, self.link_ttl)  # (file id, size, modified) -> download link

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = cls.base_path_errors(config)
        client_id = config.get("client_id")
        if not isinstance(client_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", client_id.strip()):
            errors["client_id"] = gettext("The client ID should be the letters and digits 123 Cloud Drive sent.")
        expires = config.get("token_expires_at")
        if expires is not None and (type(expires) is not int or expires < 0):
            errors["token_expires_at"] = gettext("The access token expiry should be an integer of at least 0 (Unix seconds).")
        return errors

    # -- login

    def _saved_token(self) -> str | None:
        """The saved access token: one this client ID got (saved as "<client ID>:<token>"), or one the admin typed in;
        None for one another client ID got (the admin filled in another since, maybe another account's)."""
        saved = str(self.config.get("access_token") or "").strip()
        owner, colon, token = saved.partition(":")
        if not colon:
            return saved or None
        return token if token and owner == str(self.config.get("client_id") or "").strip() else None

    def _access_token(self) -> str:
        with self.token_lock:
            token = self._saved_token()
            expires = as_int(self.config.get("token_expires_at")) or 0
            if token and (expires == 0 or time.time() < expires - self.renew_before):  # unknown expiry: try it
                return token
            return self._new_token_locked()

    def _renew_after_refusal(self, token: str) -> None:
        """123 refused `token` (401): get another one, unless another thread already did."""
        with self.token_lock:
            if self._saved_token() != token:
                return
            if self._issued is not None and time.monotonic() - self._issued < self.token_gap:
                raise AuthError(gettext("%(service)s refused the access token it gave a moment ago. One client ID can "
                                        "have at most 3 tokens in use, and getting another logs the oldest out: other "
                                        "storage sources or programs may be using this client ID (please stop them, or "
                                        "give them other developer accounts), or the account's developer package has "
                                        "expired.") % {"service": SERVICE})
            self._new_token_locked()

    def _new_token_locked(self) -> str:
        if self._refused is not None and time.monotonic() < self._refused[0]:
            raise AuthError(self._refused[1])
        client_id = str(self.config.get("client_id") or "").strip()
        secret = str(self.config.get("client_secret") or "").strip()
        if not client_id or not secret:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        try:
            token, expires = self._request_token(client_id, secret)
        except AuthError as error:
            self._refused = (time.monotonic() + self.refusal_ttl, error.message)
            raise
        self._refused = None
        self._issued = time.monotonic()
        self.log.info("got a new access token")
        self.update_config({"access_token": f"{client_id}:{token}", "token_expires_at": expires})
        return token

    def _request_token(self, client_id: str, secret: str) -> tuple[str, int]:
        """A new access token, and when it expires (Unix seconds; 0: not known)."""
        code, answer = self._exchange("POST", self.api_root + TOKEN, pace=TOKEN, token=None,
                                      json={"clientID": client_id, "clientSecret": secret})
        if code:
            message = short(answer.get("message") or "")
            if code == 429 or _too_many_logins(message):
                raise _error(code, answer)
            if code == INTERNAL:  # not the credentials' fault
                raise BackendError(gettext("%(service)s failed%(detail)s. Please try again later.")
                                   % {"service": SERVICE, "detail": _detail(code, message)})
            raise AuthError(gettext("%(service)s refused the client ID or secret%(detail)s. Please check them, and "
                                    "that the account's developer package hasn't expired.")
                            % {"service": SERVICE, "detail": _detail(code, message)})
        data = _data(answer)
        token = data.get("accessToken")
        if not isinstance(token, str) or not token.strip() or not token.strip().isprintable():
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                               % {"service": SERVICE})
        expires = _time(data.get("expiredAt"))
        return token.strip(), int(expires.timestamp()) if expires is not None else 0

    # -- requests

    def _exchange(self, method: str, url: str, *, pace: str | None, token: str | None,
                  params: Mapping[str, Any] | None = None, json: Any = None,
                  body: Callable[[], Iterator[bytes]] | None = None,
                  headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """One request, tried again while 123 says it's throttling (429) for at most throttle_wait seconds: the code
        and the answer. `pace`: the API whose limiter it waits for; `body` makes the request body afresh each try."""
        deadline = time.monotonic() + self.throttle_wait
        delay = self.throttle_delay
        while True:
            limiter = self._limiters.get(pace) if pace is not None else None
            if limiter is not None:
                limiter.wait()
            all_headers = {"Platform": "open_platform", "Content-Type": "application/json"}
            if token is not None:
                all_headers["Authorization"] = f"Bearer {token}"
            all_headers.update(headers or {})
            try:
                response = self._http.request(method, url, params=params, json=json,
                                              content=body() if body is not None else None, headers=all_headers)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            code, answer = _answer(response)
            if code != 429:
                return code, answer
            wait = retry_after(response, default=delay, longest=deadline - time.monotonic())
            if wait is None:
                return code, answer
            self.log.debug("%s is throttled; trying again in %.1f s", pace or "an upload", wait)
            time.sleep(wait)
            delay *= 2

    def _request(self, method: str, url: str, *, pace: str | None, accept: tuple[int, ...] = (),
                 **kwargs: Any) -> dict[str, Any]:
        """A call with the access token: its answer (code 0, or one of `accept`), else DriverError. When 123 refuses
        the token (401), another one is got and the call made once more; not when it says the account is logged in in
        too many places (its FAQ: log out elsewhere), which another token, one more login, would make worse."""
        token = self._access_token()
        code, answer = self._exchange(method, url, pace=pace, token=token, **kwargs)
        if code == 401 and not _too_many_logins(short(answer.get("message") or "")):
            self.log.info("the access token was refused; getting another")
            self._renew_after_refusal(token)
            code, answer = self._exchange(method, url, pace=pace, token=self._access_token(), **kwargs)
        if code == 0 or code in accept:
            return answer
        self.log.debug("%s %s: code %s (x-traceID %s)", method, pace or "an upload", code, answer.get("x-traceID"))
        raise _error(code, answer)

    def _call(self, method: str, api: str, *, accept: tuple[int, ...] = (), **kwargs: Any) -> dict[str, Any]:
        return self._request(method, self.api_root + api, pace=api, accept=accept, **kwargs)

    # -- downloads and uploads

    def _link(self, node: Node, *, fresh: bool = False) -> str:
        # Not by the id alone: 123 doesn't say whether a file it replaces (duplicate 2) keeps its id.
        key = (node.id, node.size, node.modified)
        if not fresh:
            cached = self._links.get(key)
            if cached is not None:
                return cached
        link = _data(self._call("GET", DOWNLOAD, params={"fileId": node.id})).get("downloadUrl")
        if not isinstance(link, str) or not link.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        self._links.put(key, link)
        return link

    def _upload(self, source: _Source, folder_id: str, name: str) -> Node | None:
        """Upload `source` as `name` into the folder: the new file, or None when 123 doesn't say which it is."""
        data = _data(self._call("POST", CREATE, json={"parentFileID": int(folder_id), "filename": name,
                                                      "etag": source.md5, "size": source.size, "duplicate": 2}))
        if data.get("reuse") is True:  # 秒传: 123 has these bytes already
            return self._uploaded(as_int(data.get("fileID")), name, source.size)
        preupload_id, slice_size = data.get("preuploadID"), as_int(data.get("sliceSize"))
        secure = self.api_root.startswith("https://")
        servers = [server for server in (_upload_server(value, secure=secure) for value in data.get("servers") or ())
                   if server is not None]
        if not isinstance(preupload_id, str) or not preupload_id or not preupload_id.isprintable():
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                               % {"service": SERVICE})
        if source.size:  # an empty file has no slices
            if slice_size is None or slice_size <= 0 or not servers:
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                                   % {"service": SERVICE})
            for number, offset in enumerate(range(0, source.size, slice_size), start=1):
                self._send_slice(servers, preupload_id, number, source, offset, min(slice_size, source.size - offset))
        return self._uploaded(self._complete(preupload_id), name, source.size)

    @staticmethod
    def _uploaded(file_id: int | None, name: str, size: int) -> Node | None:
        return Node(id=str(file_id), name=name, is_dir=False, size=size, modified=now()) if file_id else None

    def _send_slice(self, servers: list[str], preupload_id: str, number: int, source: _Source,
                    offset: int, length: int) -> None:
        """POST one slice (multipart/form-data, with its MD5) to the first of the upload servers; when it fails, that
        server goes to the end of the list, and the slice to the next one."""
        boundary = secrets.token_hex(16)
        fields = (("preuploadID", preupload_id), ("sliceNo", str(number)), ("sliceMD5", source.md5_of(offset, length)))
        head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                       for key, value in fields)
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="slice"; filename="slice{number}"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        start, end = head.encode("utf-8"), f"\r\n--{boundary}--\r\n".encode("ascii")
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(start) + length + len(end))}
        for attempt in range(self.slice_tries):
            try:
                self._request("POST", servers[0] + SLICE, pace=None, headers=headers,
                              body=lambda: itertools.chain((start,), source.chunks(offset, length), (end,)))
                return
            except BackendError as error:  # a broken connection, a failing server: again, on the next one
                if attempt + 1 >= self.slice_tries:
                    raise
                self.log.info("slice %s failed (%s); trying again", number, error.message)
                servers.append(servers.pop(0))
                time.sleep(self.retry_delay)

    def _complete(self, preupload_id: str) -> int:
        """upload_complete, asked again while the upload isn't done: the new file's id."""
        deadline = time.monotonic() + self.complete_wait
        while True:
            answer = self._call("POST", COMPLETE, json={"preuploadID": preupload_id}, accept=(PROCESSING,))
            data = _data(answer)
            file_id = as_int(data.get("fileID"))
            if as_int(answer.get("code")) == 0 and data.get("completed") is True and file_id:
                return file_id
            if time.monotonic() + self.poll_interval > deadline:
                raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see "
                                           "whether the file is there.") % {"service": SERVICE})
            time.sleep(self.poll_interval)

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[Node]:
        """Everything in the folder with id `folder_id` (all pages), but what is in the recycle bin."""
        nodes: list[Node] = []
        query: dict[str, Any] = {"parentFileId": folder_id, "limit": self.page_size}
        for _ in range(self.max_pages):
            try:
                data = _data(self._call("GET", LIST, params=query))
            except NotFound:
                raise StaleFolder(folder_id) from None
            items = data.get("fileList")
            if items is None:  # an empty folder may say null
                items = []
            if not isinstance(items, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                                   % {"service": SERVICE})
            nodes += [node for node in map(_list_node, items) if node is not None]
            last = as_int(data.get("lastFileId"))
            if last is None or last == -1:
                return nodes
            query["lastFileId"] = last
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _create_node(self, parent_id: str, name: str) -> Node:
        folder_id = as_int(_data(self._call("POST", MKDIR, json={"name": name, "parentID": int(parent_id)})).get("dirID"))
        if not folder_id:  # made, but we can't tell which one it is (the base forgets the path)
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                               % {"service": SERVICE})
        return Node(id=str(folder_id), name=name, is_dir=True, size=None, modified=now())

    def _delete_node(self, node: Node, parent_id: str) -> None:
        self._call("POST", TRASH, json={"fileIDs": [int(node.id)]})

    def _rename_node(self, node: Node, name: str) -> None:
        self._call("PUT", RENAME, json={"fileId": int(node.id), "fileName": name})

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        self._call("POST", MOVE, json={"fileIDs": [int(node.id)], "toParentFileID": int(target_id)})

    def _copy_file(self, node: Node, target_id: str) -> Node | None:
        answer = self._call("POST", COPY, json={"fileId": int(node.id), "targetDirId": int(target_id)})
        copy_id = as_int(_data(answer).get("targetFileId"))
        return Node(id=str(copy_id), name=node.name, is_dir=False, size=node.size, modified=now()) if copy_id else None

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        """A file: 123's copy. A folder: a new one, and what is in it copied a file and a folder at a time."""
        if not node.is_dir:
            return self._copy_file(node, target_id)
        top = self._create_node(target_id, node.name)
        pending = [(node.id, top.id)]
        while pending:
            source_id, copy_id = pending.pop()
            try:
                children = self._fetch(source_id)
            except StaleFolder:  # gone meanwhile
                raise NotFound() from None
            for child in children:
                if child.is_dir:
                    pending.append((child.id, self._create_node(copy_id, child.name).id))
                else:
                    self._copy_file(child, copy_id)
        return top

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        if size > self.max_file_size:
            raise DriverError(gettext("%(service)s takes files of at most 10 GB from developer apps.") % {"service": SERVICE})
        source = _Source(data, size)
        try:
            return self._upload(source, folder_id, name)
        finally:
            source.close()

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        if node.size == 0:  # nothing to fetch
            return ReadStream(iter(()), size=0, total=0, modified=node.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, node.size)
        if span is not None:
            headers["Range"] = span
        for attempt in range(2):
            request = self._http.build_request("GET", self._link(node, fresh=attempt > 0), headers=headers)
            try:
                response = self._http.send(request, stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code in (403, 404, 410) and not attempt:
                http.drain(response)  # an expired link: ask for another
                continue
            return response_stream(response, offset=offset, length=length, total=node.size, service=SERVICE,
                                   modified=node.modified)
        raise AssertionError("unreachable")
