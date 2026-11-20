"""115 网盘 through the 115 open platform (https://open.115.com, docs on yuque.com/115yun/open).

Paths
    The storage root ("/") is the folder `base_path` of the 115 account; "/a/b" is
    base_path + "/a/b". 115 addresses files and folders by id only: the account's root is id
    "0", and a path is found by listing each folder from there. Folder ids are cached for
    `folder_ttl` seconds (five minutes), and a listing forgets the cached folders below it that
    are gone or are another folder now; a folder moved or renamed in 115's own apps can still be
    reached through its old path until one of these happens. Our own renames, moves and deletes
    drop the cached ids at and below the old path at once, and an id 115 no longer knows is
    dropped and looked up again. (115 answers a listing of an unknown folder id with the listing
    of the root, so every listing checks the folder id in the answer.) Files are found in the
    listing of their folder, which is cached for `listing_ttl` seconds from when 115 gave it;
    list() itself always asks 115. (This, the in-place updates and the guard against duplicate
    names below are IdDriver's, apps/storages/drivers/_ids.py, the base of the id-addressed drivers.)

    Our own changes update the cached listings in place: a delete takes the node out, a move
    takes it out of one listing and into the other, a rename renames it, and a new folder or an
    upload that 115 describes is added (with our clock's time until a listing gives 115's). A
    batch of changes in one big folder so lists it once, not once per item (a listing costs a
    call per 1150 entries, at about a call a second). An updated listing still expires
    `listing_ttl` seconds after 115 gave it, so changes made elsewhere show up as soon as
    before. When we don't know the new node (a copy, an upload 115 doesn't describe) or a call
    failed (115 may have done it or not), the listing is dropped and asked for again.

    115 allows several files or folders with the same name in one folder. HFile never makes
    such duplicates: write, mkdir, rename, move and copy look at the target folder first, under a
    lock per folder and name, so two requests can't both create a name. That look trusts a
    listing 115 gave at most `guard_ttl` seconds ago (a few), else it asks 115 again; a name made
    in 115's own apps during those seconds can be missed (as one made while a listing is being
    fetched always could), and 115 then holds two of that name. Of existing duplicates a listing
    shows one, the same one a path finds: folders before files, then the most recently
    modified. The others can be renamed in 115's own apps.

Login
    The saved access token is used until 5 minutes before token_expires_at, or until 115
    refuses it (code 99, 40101017 or 40140123..6); then it is refreshed, once and under a lock,
    and the new tokens are saved through update_config(). 115 asks not to refresh more often
    than needed (40140117), which is why the access token is a param and not kept in memory,
    and why a refresh that can't help isn't asked for: for `refusal_ttl` seconds after one 115
    refused, or one whose new token it refused too (an account it won't serve), and for two
    minutes after it said "too often". Meanwhile requests that need a refresh fail, except that
    during those two minutes a saved token that is due for one is still tried.

Params
    app_id            AppID of the admin's app on the 115 open platform;
    access_token,     filled in by the QR-code login (helper "qrcode": scan with the 115 app)
    refresh_token,    and saved again after every refresh, because 115 replaces the refresh
    token_expires_at  token on each refresh and the old one stops working (error 40140120);
    qps               calls per second over all threads of this storage (115 limits the call
                      rate of all its APIs without publishing the limits, so 1, the lowest, is
                      the safest);
    base_path         the folder of the account shown as the root.

Requests
    Every call waits its turn under `qps`, at most `queue_wait` seconds (then BackendError, so
    that a burst doesn't hold all of the site's threads).

Changes
    Deleting moves the file or folder to 115's recycle bin (回收站).

Downloads
    /open/ufile/downurl gives a CDN link that only works with the User-Agent that asked for it.
    open() asks with a fixed browser User-Agent and downloads with the same one; links are
    cached for `link_ttl` seconds per pick code and User-Agent. download_url() gives a link for
    the browser only when the site passes the browser's User-Agent (`user_agent=`, an extension
    of StorageDriver.download_url); without it, it returns None and the site sends the file.

Uploads
    /open/upload/init with the SHA-1 of the file and of its first 128 KiB. 115 may answer that
    it has the content already (秒传: done), ask first for the SHA-1 of a byte range
    (sign_check, a second check against forged hashes), or name an OSS bucket and object: then
    /open/upload/get_token gives temporary STS keys, and the file goes to Aliyun OSS, signed
    with OSS's V1 signature (HMAC-SHA1), in one PutObject up to `single_put_limit` or as a
    multipart upload above (a "sequential" one, parts in order, as other 115 clients ask), with
    115's callback headers. OSS calls 115's server when the object is complete; 115 creates the
    file and its answer comes back through OSS. OSS refuses requests whose Date is more than 15
    minutes from its clock: that error says to correct the server's time. The hashes
    need a first pass over the data, so data that can't seek is spooled to a temporary file.
    A file already at the path is replaced: the new one is uploaded first and found in the
    folder (by the id 115 gave, else as the new entry of that size); only then is the old one
    deleted, and the new one renamed back if 115 named it otherwise ("a(1).txt").

Names
    At most 255 bytes, without \\ : * ? " < > | (115 refuses these like Windows does; an
    assumption, its docs don't list them).

Limits
    No direct browser uploads (the STS keys would reach the browser). Search is 115's
    /open/ufile/search, at most 10000 results deep. A listing takes one call per 1150 entries,
    in name order (_fetch). While its pages are read, a rename, or an entry leaving the folder
    (which shifts the rest), can still make it miss one; with the order 115's apps remember, any
    change could. A missing name in a listing at most `listing_ttl` seconds old is believed
    without asking again.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import hmac
import ipaddress
import re
import secrets
import tempfile
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Mapping
from email.utils import formatdate
from typing import Any, BinaryIO, ClassVar
from xml.sax.saxutils import escape as xml_escape

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    RateLimiter,
    TTLCache,
    as_int,
    exact_body,
    json_or_none,
    parse_time,
    range_header,
    read_exact,
    response_stream,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, StaleFolder, now
from .base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    Entry,
    NotFound,
    Param,
    ReadStream,
    Unsupported,
)

SERVICE = gettext_lazy("115")
OSS_SERVICE = gettext_lazy("115's object storage")

# The User-Agent open() asks for and fetches download links with.
DOWNLOAD_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36")

PREHASH_BYTES = 128 * 1024  # "preid": SHA-1 of the first 128 KiB
_TOKEN_CODES = {99, 40101017, 40140123, 40140124, 40140125, 40140126}  # access token refused
_DEAD_GRANT_CODES = {40140114, 40140115, 40140116, 40140119, 40140120}  # log in again
_RETRY_CODES = {40110000, 40140121}  # "请求异常需要重试", "access_token 刷新失败"
_BUCKET = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_SIGN_RANGE = re.compile(r"(\d+)-(\d+)")


# --- answers and errors -----------------------------------------------------------------------------

def _ok(answer: Mapping[str, Any]) -> bool:
    """proapi says "state": true; passport and qrcode APIs say "state": 1."""
    state = answer.get("state")
    return state is True or (type(state) is int and state == 1)


def _data(answer: Mapping[str, Any]) -> dict[str, Any]:
    """The "data" object; 115 sometimes wraps it in a list."""
    data = answer.get("data")
    if isinstance(data, list):
        data = data[0] if data else {}
    return data if isinstance(data, dict) else {}


def _code(answer: Mapping[str, Any]) -> int | None:
    return as_int(answer.get("code")) or as_int(answer.get("errno"))


def _message(answer: Mapping[str, Any]) -> str:
    return short(answer.get("message") or answer.get("error") or "")


def _token_refused(answer: Mapping[str, Any]) -> bool:
    return _code(answer) in _TOKEN_CODES


def _error(answer: Mapping[str, Any]) -> DriverError:
    """DriverError for an answer with "state": false."""
    code, message = _code(answer), _message(answer)
    detail = gettext(" (%(code)s): %(message)s") % {"code": code, "message": message} if message else gettext(" (%(code)s)") % {"code": code}
    if _token_refused(answer):
        return AuthError(gettext("115 refused the access token%(detail)s. Please scan the QR code to log in again.") % {"detail": detail})
    if code == 430004 or "不存在" in message:
        return NotFound()
    if "已存在" in message or "同名" in message:
        return AlreadyExists()
    if "频繁" in message or "限流" in message:
        return BackendError(gettext("Too many requests to 115: it is rate-limiting them%(detail)s. Please try again later.") % {"detail": detail})
    if "空间不足" in message or "空间已满" in message:
        return DriverError(gettext("115 is out of space."))
    return DriverError(gettext("115 refused this%(detail)s.") % {"detail": detail})


def _answer(response: httpx.Response) -> dict[str, Any]:
    """The JSON answer of an API call; HTTP errors become BackendError, 401 a token refusal."""
    if response.status_code == 401:
        return {"state": False, "code": 99, "message": "HTTP 401"}
    if response.status_code == 429:
        raise BackendError(gettext("Too many requests to 115: it is rate-limiting them. Please try again later."))
    if response.status_code != 200:
        raise BackendError(gettext("The 115 API failed (HTTP %(status_code)s). Please try again later.") % {"status_code": response.status_code})
    answer = json_or_none(response)
    if not isinstance(answer, dict):
        raise BackendError(gettext("The data from 115 couldn't be understood. Please try again later."))
    return answer


class _TooOften(BackendError):
    """115 refused to refresh the access token: too soon after the last refresh (40140117)."""

    def __init__(self):
        super().__init__(gettext("The 115 access token was refreshed too often. Please try again in a few minutes."))


# --- nodes and listings -------------------------------------------------------------------------------

@dataclasses.dataclass(frozen=True)
class _Node(Node):
    pick_code: str | None = None  # what downloads need: a file 115 gave none for is listed again


def _list_node(item: Any) -> _Node | None:
    """A node from an entry of /open/ufile/files, or None for one HFile can't show."""
    if not isinstance(item, dict):
        return None
    name, file_id = item.get("fn"), item.get("fid")
    if not usable_name(name) or file_id in (None, ""):
        return None
    if str(item.get("aid", "1")) in ("7", "120"):  # in the recycle bin, or deleted
        return None
    is_dir = str(item.get("fc")) != "1"
    pick_code = item.get("pc")
    return _Node(
        id=str(file_id),
        name=name,
        is_dir=is_dir,
        size=None if is_dir else as_int(item.get("fs")),
        modified=parse_time(item.get("upt") or item.get("uet") or item.get("uppt")),
        pick_code=pick_code if isinstance(pick_code, str) and pick_code else None,
    )


def _made(data: Mapping[str, Any], name: str, size: int) -> _Node | None:
    """The file an upload made, from 115's answer (秒传, or the callback's through OSS), or None
    when the answer doesn't give its id. Its name is the one 115 gave, when the answer says it."""
    file_id = data.get("file_id")
    if file_id in (None, ""):
        return None
    file_name, pick_code = data.get("file_name"), data.get("pick_code")
    return _Node(
        id=str(file_id),
        name=file_name if isinstance(file_name, str) and file_name else name,
        is_dir=False,
        size=size,
        modified=now(),
        pick_code=pick_code if isinstance(pick_code, str) and pick_code else None,
    )


# --- uploads ----------------------------------------------------------------------------------------

def _tell(data: BinaryIO) -> int | None:
    """Where a seekable file object is, or None when it can't seek."""
    try:
        if data.seekable():
            return data.tell()
    except (AttributeError, OSError, ValueError):
        pass
    return None


class _Source:
    """The bytes of one upload: seekable (spooled to a temporary file when needed), with the
    SHA-1 sums 115 asks for (upper case hex, of the whole and of the first 128 KiB)."""

    def __init__(self, data: BinaryIO, size: int):
        self.size = size
        start = _tell(data)
        self._own = start is None
        if self._own:
            self._file: BinaryIO = tempfile.TemporaryFile(prefix="hfile-115-")
            self._start = 0
        else:
            self._file = data
            self._start = start
        whole, head = hashlib.sha1(), hashlib.sha1()
        head_left = min(size, PREHASH_BYTES)
        try:
            for chunk in exact_body(data, size):
                whole.update(chunk)
                if head_left:
                    piece = chunk[:head_left]
                    head.update(piece)
                    head_left -= len(piece)
                if self._own:
                    self._file.write(chunk)
        except BaseException:
            self.close()
            raise
        self.sha1 = whole.hexdigest().upper()
        self.head_sha1 = head.hexdigest().upper()

    def chunks(self, offset: int, length: int) -> Iterator[bytes]:
        """Bytes offset .. offset + length, as chunks for a request body."""
        self._file.seek(self._start + offset)
        return exact_body(self._file, length)

    def range_sha1(self, first: int, last: int) -> str:
        """Upper case SHA-1 of bytes first .. last, both included (115's sign_check)."""
        self._file.seek(self._start + first)
        digest = hashlib.sha1()
        left = last - first + 1
        while left > 0:
            piece = read_exact(self._file, min(left, 1024 * 1024))
            if not piece:
                raise DriverError(gettext("The uploaded data is incomplete: less than the size given."))
            digest.update(piece)
            left -= len(piece)
        return digest.hexdigest().upper()

    def close(self) -> None:
        if self._own:
            self._file.close()


@dataclasses.dataclass(frozen=True)
class _Sts:
    """Temporary OSS keys from /open/upload/get_token."""

    endpoint: str  # scheme://host[:port]
    key_id: str
    secret: str
    token: str
    renew_at: float  # time.monotonic()


def oss_signature(secret: str, method: str, headers: Mapping[str, str], resource: str) -> str:
    """OSS's V1 request signature: base64 of HMAC-SHA1 over VERB, Content-MD5, Content-Type,
    Date, the x-oss-* headers (lower case, sorted, "name:value" lines) and the resource
    ("/bucket/key", plus "?" and the sorted sub-resources such as "partNumber=1&uploadId=x")."""
    lower = {name.lower(): str(value).strip() for name, value in headers.items()}
    oss_headers = "".join(f"{name}:{lower[name]}\n" for name in sorted(n for n in lower if n.startswith("x-oss-")))
    text = (f"{method}\n{lower.get('content-md5', '')}\n{lower.get('content-type', '')}\n"
            f"{lower.get('date', '')}\n{oss_headers}{resource}")
    digest = hmac.new(secret.encode("utf-8"), text.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def _sub_resources(sub: list[tuple[str, str | None]]) -> str:
    return "&".join(name if value is None else f"{name}={value}" for name, value in sorted(sub))


def _path_style(host: str) -> bool:
    """Aliyun's SDK addresses buckets by path when the endpoint is an IP address (or localhost)."""
    if host == "localhost":
        return True
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _oss_endpoint(value: Any) -> str:
    """scheme://host[:port] of the endpoint 115 gives; plain http to *.aliyuncs.com is upgraded,
    so the STS token never travels in clear."""
    if not isinstance(value, str) or not value.strip():
        raise BackendError(gettext("115 gave no object storage address."))
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL:
        raise BackendError(gettext("The object storage address from 115 is wrong.")) from None
    if url.scheme not in ("http", "https") or not url.host:
        raise BackendError(gettext("The object storage address from 115 is wrong."))
    scheme = url.scheme
    if scheme == "http" and url.host.endswith(".aliyuncs.com"):
        scheme = "https"
    port = f":{url.port}" if url.port else ""
    return f"{scheme}://{url.host}{port}"


def _xml(content: bytes) -> ElementTree.Element | None:
    """An OSS XML answer, or None; documents with a DTD are refused (no entity expansion)."""
    if not content or b"<!doctype" in content.lower() or b"<!entity" in content.lower():
        return None
    try:
        return ElementTree.fromstring(content)
    except (ElementTree.ParseError, LookupError):  # LookupError: an encoding Python doesn't know (STOR-04)
        return None


def _xml_text(root: ElementTree.Element | None, tag: str) -> str | None:
    if root is None:
        return None
    for element in root.iter():
        if element.tag.rpartition("}")[2] == tag:
            return (element.text or "").strip()
    return None


def _oss_code(response: httpx.Response) -> str:
    return _xml_text(_xml(response.content), "Code") or ""


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


# --- the driver -------------------------------------------------------------------------------------

class Open115Driver(IdDriver):
    type = "open115"
    label = gettext_lazy("115")
    service = SERVICE
    root_id = "0"
    params = (
        Param("app_id", "AppID", help=gettext_lazy("The AppID you get after creating an app on the 115 open platform (https://open.115.com).")),
        Param("access_token", gettext_lazy("Access token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Click \"Scan to log in\", scan the code with the 115 phone app and confirm: it's filled in for you.")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Filled in after scanning to log in. 115 gives a new one on every "
                                "refresh and the old one stops working; HFile saves the new one itself.")),
        # Secret only so that the form keeps the saved value: after a refresh in the background,
        # saving the form must not put the old expiry next to the new access token.
        Param("token_expires_at", gettext_lazy("Access token expiry"), kind="number", required=False, advanced=True, minimum=0,
              secret=True, help=gettext_lazy("Unix time (seconds), filled in when scanning to log in and refreshing the token; no need to change it.")),
        Param("qps", gettext_lazy("Requests per second"), kind="number", default=1, minimum=1, maximum=10,
              help=gettext_lazy("How many requests a second HFile sends to 115 at most. 115 limits the call "
                   "rate of all its APIs and doesn't publish the limits, so keep it low: 1, the lowest, "
                   "is the safest. Its rules also strictly forbid sharing a developer account or its "
                   "membership among several people; see https://www.yuque.com/115yun/open/vq62qwp8ia2efoli")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in 115 that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # only when the site passes the browser's User-Agent, see download_url()
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = True
    has_folders = True
    forbidden_chars = '\\:*?"<>|'
    max_name_bytes = 255

    helpers = {"qrcode": gettext_lazy("Scan to log in")}
    setup_params = ("app_id",)
    auth_params = ("access_token", "refresh_token", "token_expires_at")

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://proapi.115.com"
    passport_root: ClassVar[str] = "https://passportapi.115.com"
    qrcode_root: ClassVar[str] = "https://qrcodeapi.115.com"
    page_size: ClassVar[int] = 1150  # entries per listing call (115's maximum)
    search_page_size: ClassVar[int] = 100
    link_ttl: ClassVar[float] = 600.0  # seconds a download link is reused
    single_put_limit: ClassVar[int] = 64 * 1024 * 1024  # larger files go up in parts
    part_size: ClassVar[int] = 20 * 1024 * 1024  # smallest part of a multipart upload
    oss_retries: ClassVar[int] = 3  # tries of one OSS request on 5xx or a broken connection
    retry_delay: ClassVar[float] = 0.5
    queue_wait: ClassVar[float] = 10.0  # seconds a call may wait for its turn under qps, else BackendError
    qr_poll_timeout: ClassVar[float] = 25.0  # the QR status call is a long poll
    #: Seconds a refused refresh is remembered, and one whose new token 115 refused too, so a dead
    #: authorisation doesn't send every request to the refresh endpoint (115 also limits refreshes);
    #: new settings, new driver.
    refusal_ttl: ClassVar[float] = 30.0

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)  # listing_ttl, guard_ttl, folder_ttl: IdDriver's
        self._limiter = RateLimiter(float(as_int(self.config.get("qps")) or 1), longest=self.queue_wait,
                                    service=SERVICE)
        self._too_often_until = 0.0  # 115 said "refreshing too often" until then
        self._http = http.client()
        self._refused: tuple[float, str] | None = None  # (until, message) of a refused or useless refresh
        self._sts_lock = threading.Lock()
        self._sts_cached: _Sts | None = None
        self._links = TTLCache(1024, self.link_ttl)  # (pick code, User-Agent) -> link

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        app_id = config.get("app_id")
        if not isinstance(app_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", app_id.strip()):
            errors["app_id"] = gettext("The AppID should be the string of digits or letters the 115 open platform gave.")
        errors.update(cls.base_path_errors(config))
        qps = config.get("qps")
        if qps is not None and (type(qps) is not int or not 1 <= qps <= 10):
            errors["qps"] = gettext("Requests per second should be an integer from 1 to 10.")
        expires = config.get("token_expires_at")
        if expires is not None and (type(expires) is not int or expires < 0):
            errors["token_expires_at"] = gettext("The access token expiry should be an integer of at least 0 (Unix seconds).")
        return errors

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        """"qrcode": without args, start a QR-code login (PKCE device code) and show the code;
        with the args of that answer, poll its state, until it gives the tokens."""
        if name != "qrcode":
            raise Unsupported()
        if not args or not args.get("uid"):
            return cls._qr_start(config)
        return cls._qr_poll(args)

    # -- login

    @classmethod
    def _qr_answer(cls, args: dict[str, str], message: str) -> dict[str, Any]:
        import segno  # only the admin form needs it

        image = segno.make(args["qrcode"], error="m").png_data_uri(scale=5, border=2)
        return {"image": image, "poll": "qrcode", "args": args, "message": message}

    @classmethod
    def _qr_start(cls, config: Mapping[str, Any]) -> dict[str, Any]:
        app_id = str(config.get("app_id") or "").strip()
        if not app_id:
            raise DriverError(gettext("Fill in the AppID before scanning to log in."))
        verifier = secrets.token_urlsafe(48)  # 64 characters; PKCE allows 43 to 128
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=")
        with http.client() as client:
            try:
                response = client.post(cls.passport_root + "/open/authDeviceCode", data={
                    "client_id": app_id,
                    "code_challenge": challenge.decode("ascii"),
                    "code_challenge_method": "sha256",
                })
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
        answer = _answer(response)
        if not _ok(answer):
            raise DriverError(gettext("115 gave no login QR code (%(code)s): %(message)s. Please check the AppID.") % {"code": _code(answer), "message": _message(answer)})
        data = _data(answer)
        args = {
            "uid": data.get("uid"),
            "time": str(data.get("time") if data.get("time") is not None else ""),
            "sign": data.get("sign"),
            "code_verifier": verifier,
            "qrcode": data.get("qrcode"),
        }
        if not all(isinstance(value, str) and value for value in args.values()):
            raise BackendError(gettext("The login QR code from 115 is incomplete. Please try again."))
        return cls._qr_answer(args, gettext("Scan the code with the 115 phone app and confirm on the phone."))

    @classmethod
    def _qr_poll(cls, args: Mapping[str, Any]) -> dict[str, Any]:
        def arg(key: str) -> str:
            value = args.get(key)
            if type(value) is int:
                value = str(value)
            if not isinstance(value, str) or not value or len(value) > 512 or not value.isprintable():
                raise DriverError(gettext("The scan login's details are wrong. Please click \"Scan to log in\" again."))
            return value

        state = {key: arg(key) for key in ("uid", "time", "sign", "code_verifier", "qrcode")}
        with http.client(timeout=cls.qr_poll_timeout) as client:
            try:
                response = client.get(cls.qrcode_root + "/get/status/",
                                      params={"uid": state["uid"], "time": state["time"], "sign": state["sign"]})
            except httpx.TimeoutException:  # a long poll that ran out: nothing happened yet
                return cls._qr_answer(state, gettext("Waiting for the scan…"))
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            answer = _answer(response)
            if not _ok(answer):
                return {"message": gettext('The QR code has expired. Please click "Scan to log in" again.')}
            data = answer.get("data") if isinstance(answer.get("data"), dict) else {}
            status = as_int(data.get("status"))
            if status is None or status == 0:
                return cls._qr_answer(state, gettext("Waiting for the scan…"))
            if status == 1:
                return cls._qr_answer(state, gettext("Scanned. Please confirm on the phone."))
            if status != 2:
                return {"message": gettext('The scan login was canceled. Please click "Scan to log in" again.')}
            try:
                response = client.post(cls.passport_root + "/open/deviceCodeToToken",
                                       data={"uid": state["uid"], "code_verifier": state["code_verifier"]})
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
        answer = _answer(response)
        if not _ok(answer):
            raise AuthError(gettext("115 gave no token (%(code)s): %(message)s. Please scan to log in again.") % {"code": _code(answer), "message": _message(answer)})
        data = _data(answer)
        access, refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(refresh, str) or not refresh:
            raise BackendError(gettext("115's answer has no token. Please scan to log in again."))
        lifetime = as_int(data.get("expires_in")) or 7200
        return {
            "changes": {"access_token": access, "refresh_token": refresh,
                        "token_expires_at": int(time.time()) + lifetime},
            "message": gettext("Authorized. Please save the storage source."),
        }

    def _access_token(self) -> str:
        with self.token_lock:
            token = self.config.get("access_token")
            expires = as_int(self.config.get("token_expires_at")) or 0
            if token and (expires == 0 or time.time() < expires - 300):  # unknown expiry: try it
                return token
            if token and time.monotonic() < self._too_often_until:
                return token  # 115 refused to refresh this often: the saved token may still work
            try:
                return self._refresh_locked()
            except _TooOften:
                if not token:
                    raise
                self.log.info("115 refuses to refresh this often; using the saved access token")
                return token

    def _refresh_after_refusal(self, token: str) -> None:
        """115 refused `token`: refresh, unless another thread already did."""
        with self.token_lock:
            if self.config.get("access_token") == token:
                self._refresh_locked()

    def _refresh_locked(self) -> str:
        if self._refused is not None and time.monotonic() < self._refused[0]:
            raise AuthError(self._refused[1])
        if time.monotonic() < self._too_often_until:  # 115 would say it again
            raise _TooOften()
        try:
            access = self._refresh_now()
        except AuthError as error:
            self._refused = (time.monotonic() + self.refusal_ttl, error.message)
            raise
        self._refused = None
        return access

    def _refresh_now(self) -> str:
        refresh = self.config.get("refresh_token")
        if not refresh:
            raise AuthError(gettext("Not authorized yet: click \"Scan to log in\" in the storage source's settings and scan with the 115 phone app."))
        self._limiter.wait()
        try:
            response = self._http.post(self.passport_root + "/open/refreshToken", data={"refresh_token": refresh})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        answer = _answer(response)
        if not _ok(answer):
            code = _code(answer)
            if code == 40140117:
                self._too_often_until = time.monotonic() + 120
                raise _TooOften()
            if code in _RETRY_CODES:
                raise BackendError(gettext("Refreshing the 115 access token failed (%(code)s). Please try again later.") % {"code": code})
            if code in _DEAD_GRANT_CODES:
                raise AuthError(gettext("The 115 authorization has expired (%(code)s). Please scan to log in again.") % {"code": code})
            raise AuthError(gettext("115 refused to refresh the access token (%(code)s): "
                                    "%(message)s. Please scan to log in again.") % {"code": code, "message": _message(answer)})
        data = _data(answer)
        access, new_refresh = data.get("access_token"), data.get("refresh_token")
        if not isinstance(access, str) or not access or not isinstance(new_refresh, str) or not new_refresh:
            raise BackendError(gettext("115's token refresh answer has no token. Please try again later."))
        lifetime = as_int(data.get("expires_in")) or 7200
        self.log.info("refreshed the access token")
        # The old refresh token is dead now: save the new one before anything else.
        self.update_config({"access_token": access, "refresh_token": new_refresh,
                            "token_expires_at": int(time.time()) + lifetime})
        return access

    # -- requests

    def _call(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
              data: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> dict[str, Any]:
        """One proapi call; returns the answer with "state": true, else raises."""
        for attempt in range(2):
            token = self._access_token()
            all_headers = {"Authorization": f"Bearer {token}"}
            all_headers.update(headers or {})
            self._limiter.wait()
            try:
                response = self._http.request(method, self.api_root + path, params=params, data=data,
                                              headers=all_headers)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            answer = _answer(response)
            if _ok(answer):
                return answer
            if attempt == 0 and _token_refused(answer):
                self.log.info("the access token was refused (code %s); refreshing", _code(answer))
                self._refresh_after_refusal(token)
                continue
            error = _error(answer)
            if _token_refused(answer):  # the token of a refresh a moment ago: another one won't help
                with self.token_lock:
                    self._refused = (time.monotonic() + self.refusal_ttl, error.message)
            raise error
        raise AssertionError("unreachable")

    # -- uploads

    def _upload(self, source: _Source, name: str, folder_id: str) -> _Node | None:
        """Upload `source` as `name` into the folder; returns the new file as 115 describes it,
        or None when 115 doesn't say which file it made."""
        form = {"file_name": name, "file_size": str(source.size), "target": f"U_1_{folder_id}",
                "fileid": source.sha1, "preid": source.head_sha1}
        for _ in range(4):
            info = _data(self._call("POST", "/open/upload/init", data=form))
            status = as_int(info.get("status"))
            if status == 2:  # 秒传: 115 has the content already
                return _made(info, name, source.size)
            if status in (6, 7, 8):  # prove we have the bytes: SHA-1 of the range it names
                match = _SIGN_RANGE.fullmatch(str(info.get("sign_check") or ""))
                sign_key = info.get("sign_key")
                if match is None or not sign_key:
                    raise BackendError(gettext("115 asked for a second check but gave no range to check."))
                first, last = int(match[1]), int(match[2])
                if not 0 <= first <= last < source.size:
                    raise BackendError(gettext("The range 115 asked to check is beyond the file's size."))
                form = dict(form, sign_key=str(sign_key), sign_val=source.range_sha1(first, last))
                continue
            if status == 1:
                return _made(self._oss_upload(source, info), name, source.size)
            raise BackendError(gettext("115's upload start gave an unknown status (%(status)s).") % {"status": status})
        raise BackendError(gettext("115 kept asking for a second check, so the upload couldn't start."))

    def _sts(self) -> _Sts:
        with self._sts_lock:
            sts = self._sts_cached
            if sts is not None and time.monotonic() < sts.renew_at:
                return sts
            data = _data(self._call("GET", "/open/upload/get_token"))
            key_id, token = data.get("AccessKeyId"), data.get("SecurityToken")
            secret = data.get("AccessKeySecret") or data.get("AccessKeySecrett")  # the docs' spelling
            if not all(isinstance(value, str) and value for value in (key_id, secret, token)):
                raise BackendError(gettext("The upload credentials from 115 are incomplete."))
            lifetime = 3000.0
            expiration = parse_time(data.get("Expiration") or data.get("expiration"))
            if expiration is not None:
                left = (expiration - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
                lifetime = min(max(left - 300, 0.0), 3300.0)
            sts = _Sts(_oss_endpoint(data.get("endpoint")), key_id, secret, token, time.monotonic() + lifetime)
            self._sts_cached = sts
            return sts

    def _drop_sts(self, sts: _Sts) -> None:
        with self._sts_lock:
            if self._sts_cached is sts:
                self._sts_cached = None

    def _oss_send(self, sts: _Sts, method: str, bucket: str, key: str, *, sub: list[tuple[str, str | None]],
                  headers: Mapping[str, str], content: bytes | Iterator[bytes] | None) -> httpx.Response:
        endpoint = httpx.URL(sts.endpoint)
        host = f"[{endpoint.host}]" if ":" in endpoint.host else endpoint.host
        netloc = host + (f":{endpoint.port}" if endpoint.port else "")
        quoted = urllib.parse.quote(key, safe="/~")
        if _path_style(endpoint.host):
            url = f"{endpoint.scheme}://{netloc}/{bucket}/{quoted}"
        else:
            url = f"{endpoint.scheme}://{bucket}.{netloc}/{quoted}"
        if sub:
            url += "?" + "&".join(name if value is None else f"{name}={urllib.parse.quote(value, safe='')}"
                                  for name, value in sorted(sub))
        all_headers = {"Date": formatdate(usegmt=True), "x-oss-security-token": sts.token}
        all_headers.update(headers)
        resource = f"/{bucket}/{key}" + (f"?{_sub_resources(sub)}" if sub else "")
        all_headers["Authorization"] = f"OSS {sts.key_id}:{oss_signature(sts.secret, method, all_headers, resource)}"
        try:
            return self._http.request(method, url, headers=all_headers, content=content)
        except httpx.HTTPError as error:
            raise transport_error(error, OSS_SERVICE) from None

    def _oss(self, method: str, bucket: str, key: str, *, sub: list[tuple[str, str | None]] | None = None,
             headers: Mapping[str, str] | None = None,
             body: Callable[[], bytes | Iterator[bytes]] | None = None) -> httpx.Response:
        """One OSS request, repeated on 5xx and broken connections, and once with new STS keys
        when OSS says they expired; `body` makes the request body afresh for every try."""
        renewed = False
        attempt = 0
        while True:
            sts = self._sts()
            try:
                response = self._oss_send(sts, method, bucket, key, sub=sub or [], headers=headers or {},
                                          content=body() if body is not None else None)
            except BackendError:
                attempt += 1
                if attempt >= self.oss_retries:
                    raise
                time.sleep(self.retry_delay * attempt)
                continue
            if response.status_code == 403 and not renewed and _oss_code(response) == "SecurityTokenExpired":
                self.log.info("the OSS keys expired; asking 115 for new ones")
                self._drop_sts(sts)
                renewed = True
                continue
            if response.status_code >= 500:
                attempt += 1
                if attempt < self.oss_retries:
                    time.sleep(self.retry_delay * attempt)
                    continue
            return response

    @staticmethod
    def _oss_failure(response: httpx.Response) -> DriverError:
        code = _oss_code(response)
        if code == "RequestTimeTooSkewed":  # our Date is more than 15 minutes from OSS's: trying again won't help
            return AuthError(gettext("Uploading to 115's object storage failed: the server's clock is more than 15 "
                                     "minutes off from the storage's (RequestTimeTooSkewed). Please correct the server's time."))
        if code:
            return BackendError(gettext("Uploading to 115's object storage failed (HTTP %(status)s, %(code)s). Please try again later.")
                                % {"status": response.status_code, "code": short(code, 60)})
        return BackendError(gettext("Uploading to 115's object storage failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})

    def _oss_upload(self, source: _Source, info: Mapping[str, Any]) -> dict[str, Any]:
        bucket, key = info.get("bucket"), info.get("object")
        callback = info.get("callback")
        if isinstance(callback, list):
            callback = callback[0] if callback else None
        callback = callback if isinstance(callback, dict) else {}
        body, variables = callback.get("callback"), callback.get("callback_var")
        if (not isinstance(bucket, str) or not _BUCKET.fullmatch(bucket) or not isinstance(key, str) or not key
                or not key.isprintable() or not isinstance(body, str) or not body):
            raise BackendError(gettext("115's upload start is missing the object storage location or callback."))
        callback_headers = {"x-oss-callback": _b64(body)}
        if isinstance(variables, str) and variables:
            callback_headers["x-oss-callback-var"] = _b64(variables)
        if source.size <= self.single_put_limit:
            response = self._oss("PUT", bucket, key, headers={
                "Content-Type": "application/octet-stream", "Content-Length": str(source.size), **callback_headers,
            }, body=lambda: source.chunks(0, source.size))
        else:
            response = self._multipart(source, bucket, key, callback_headers)
        return self._uploaded(response)

    def _multipart(self, source: _Source, bucket: str, key: str, callback_headers: Mapping[str, str]) -> httpx.Response:
        # "sequential": the parts come in order (they do). OSS's docs leave it out, but OpenList's and
        # p115client's 115 uploads ask for it, and Aliyun's SDKs sign it as a sub-resource.
        response = self._oss("POST", bucket, key, sub=[("sequential", None), ("uploads", None)],
                             headers={"Content-Type": "application/octet-stream"})
        upload_id = _xml_text(_xml(response.content), "UploadId") if response.status_code == 200 else None
        if not upload_id:
            raise self._oss_failure(response)
        try:
            part_size = max(self.part_size, -(-source.size // 9000))  # stays below 10000 parts
            parts: list[tuple[int, str]] = []
            for number, offset in enumerate(range(0, source.size, part_size), start=1):
                length = min(part_size, source.size - offset)
                response = self._oss("PUT", bucket, key, sub=[("partNumber", str(number)), ("uploadId", upload_id)],
                                     headers={"Content-Length": str(length)},
                                     body=lambda offset=offset, length=length: source.chunks(offset, length))
                etag = response.headers.get("etag")
                if response.status_code != 200 or not etag:
                    raise self._oss_failure(response)
                parts.append((number, etag))
            document = "".join(f"<Part><PartNumber>{number}</PartNumber><ETag>{xml_escape(etag)}</ETag></Part>"
                               for number, etag in parts)
            payload = f"<CompleteMultipartUpload>{document}</CompleteMultipartUpload>".encode("utf-8")
            response = self._oss("POST", bucket, key, sub=[("uploadId", upload_id)],
                                 headers={"Content-Type": "application/xml", **callback_headers},
                                 body=lambda: payload)
            if response.status_code not in (200, 203):
                raise self._oss_failure(response)
            return response
        except BaseException:
            try:  # best effort: don't leave the parts behind
                self._oss("DELETE", bucket, key, sub=[("uploadId", upload_id)])
            except DriverError:
                pass
            raise

    def _uploaded(self, response: httpx.Response) -> dict[str, Any]:
        """The "data" of 115's callback answer (passed on by OSS): the new file's id and more."""
        if response.status_code == 203:
            raise BackendError(gettext("The file reached 115's object storage, but 115 didn't confirm it (the callback failed). Please try again later."))
        if response.status_code != 200:
            raise self._oss_failure(response)
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            return {}
        if "state" in answer and not _ok(answer):
            raise _error(answer)
        return _data(answer)

    # -- files

    def _file(self, path: str) -> _Node:
        node = super()._file(path)
        if not node.pick_code:
            raise BackendError(gettext("115 gave no pick code for this file, so it can't be downloaded."))
        return node

    def _link(self, node: _Node, user_agent: str, *, fresh: bool = False) -> str:
        """A download link of the file, only valid with `user_agent`."""
        key = (node.pick_code, user_agent)
        if not fresh:
            cached = self._links.get(key)
            if cached is not None:
                return cached
        answer = self._call("POST", "/open/ufile/downurl", data={"pick_code": node.pick_code},
                            headers={"User-Agent": user_agent})
        data = answer.get("data")
        info = None
        if isinstance(data, dict):
            info = data.get(node.id)
            if not isinstance(info, dict):
                info = next((value for value in data.values() if isinstance(value, dict)), None)
        url = info.get("url") if isinstance(info, dict) else None
        url = url.get("url") if isinstance(url, dict) else url
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise NotFound(gettext("115 gave no download address for this file."))
        self._links.put(key, url)
        return url

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        """A link the browser can download from, or None.

        115's links only work with the User-Agent that asked for them, so the site must pass the
        browser's `user_agent`; without one this returns None and the site sends the file. The
        link is valid for a few hours (115 decides), not `expires`; 115 names the file itself.
        """
        if not isinstance(user_agent, str):
            return None
        user_agent = user_agent.strip()
        if not user_agent or len(user_agent) > 512 or any(not " " <= ch <= "~" for ch in user_agent):
            return None
        return self._link(self._file(path), user_agent)

    def _search_folder(self, folder_id: str, folders: dict[str, str | None]) -> str | None:
        """The storage path of the folder with that id, or None when it isn't below the root."""
        if folder_id in folders:
            return folders[folder_id]
        root_id = self._root_id()
        path: str | None = None
        if folder_id == root_id:
            path = paths.ROOT
        elif folder_id:
            answer = self._call("GET", "/open/folder/get_info", params={"file_id": folder_id})
            info = _data(answer)
            chain = [(str(step.get("file_id")), step.get("file_name"))
                     for step in info.get("paths") or [] if isinstance(step, dict)]
            chain.append((folder_id, info.get("file_name")))
            ids = [step_id for step_id, _ in chain]
            if root_id in ids:
                names = [step_name for _, step_name in chain[ids.index(root_id) + 1:]]
                if all(usable_name(step_name) for step_name in names):
                    path = paths.ROOT + "/".join(names) if names else paths.ROOT
        folders[folder_id] = path
        return path

    def _search_entry(self, item: Any, keyword: str, folder: str, folders: dict[str, str | None]) -> Entry | None:
        if not isinstance(item, dict):
            return None
        name = item.get("file_name")
        if not usable_name(name) or keyword.lower() not in name.lower():
            return None
        if str(item.get("area_id", "1")) in ("7", "120"):
            return None
        parent = self._search_folder(str(item.get("parent_id", "")), folders)
        if parent is None:
            return None
        path = paths.join(parent, name)
        if path == folder or not paths.is_inside(path, folder):
            return None
        is_dir = str(item.get("file_category")) != "1"
        return Entry(name=name, path=path, is_dir=is_dir, size=None if is_dir else as_int(item.get("file_size")),
                     modified=parse_time(item.get("user_utime") or item.get("user_ptime")))

    def search(self, folder: str, keyword: str, limit: int) -> list[Entry]:
        keyword = keyword.strip()
        if not keyword or limit <= 0:
            return []
        folder_id = self._folder_id(folder)
        found: list[Entry] = []
        folders: dict[str, str | None] = {folder_id: folder}  # id -> storage path; None: not ours
        offset = 0
        page = max(1, min(self.search_page_size, 1000))
        while len(found) < limit and offset + page <= 10000:
            answer = self._call("GET", "/open/ufile/search", params={
                "search_value": keyword, "limit": page, "offset": offset, "cid": folder_id})
            items = answer.get("data")
            if not isinstance(items, list):
                items = []
            for item in items:
                entry = self._search_entry(item, keyword, folder, folders)
                if entry is not None:
                    found.append(entry)
                    if len(found) >= limit:
                        break
            offset += len(items)
            if not items or offset >= (as_int(answer.get("count")) or 0):
                break
        return found

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[_Node]:
        """Everything in the folder with id `folder_id` (all pages).

        The pages come in name order (o and asc, with custom_order 2, as p115client sends them). Else
        115 may use the order it remembers from its apps, which may be by modification time: then any
        change while the pages are read could move an entry to a page already read, and the listing
        would leave it out. A rename can still move one, by its new name. (The upload time, user_ptime,
        would be steadier, but here 115 sorts it like user_utime, p115client's notes say.)
        """
        nodes: list[_Node] = []
        offset = 0
        for _ in range(20000):  # 23 million entries: a folder can't be that big, 115 would be looping
            answer = self._call("GET", "/open/ufile/files", params={
                "cid": folder_id, "show_dir": 1, "limit": self.page_size, "offset": offset,
                "o": "file_name", "asc": 1, "custom_order": 2})
            if str(answer.get("cid")) != folder_id:
                raise StaleFolder(folder_id)
            items = answer.get("data")
            if not isinstance(items, list):
                raise BackendError(gettext("The file list from 115 couldn't be understood. Please try again later."))
            for item in items:
                node = _list_node(item)
                if node is not None:
                    nodes.append(node)
                elif isinstance(item, dict):
                    self.log.debug("left out an entry that can't be a path segment")
            offset += len(items)
            count = as_int(answer.get("count")) or 0
            if not items or offset >= count:
                return nodes
        raise BackendError(gettext("The 115 file list never ends. Please try again later."))

    def _create_node(self, parent_id: str, name: str) -> _Node:
        answer = self._call("POST", "/open/folder/add", data={"pid": parent_id, "file_name": name})
        folder_id = _data(answer).get("file_id")
        if folder_id in (None, ""):  # made, but we can't tell which one it is (the base forgets the path)
            raise BackendError(gettext("115 gave no ID for the new folder."))
        return _Node(id=str(folder_id), name=name, is_dir=True, size=None, modified=now())

    def _delete_node(self, node: _Node, parent_id: str) -> None:
        self._call("POST", "/open/ufile/delete", data={"file_ids": node.id, "parent_id": parent_id})

    def _rename_node(self, node: _Node, name: str) -> None:
        self._call("POST", "/open/ufile/update", data={"file_id": node.id, "file_name": name})

    def _move_node(self, node: _Node, source_id: str, target_id: str) -> None:
        self._call("POST", "/open/ufile/move", data={"file_ids": node.id, "to_cid": target_id})

    def _copy_node(self, node: _Node, target_id: str) -> None:
        self._call("POST", "/open/ufile/copy", data={"pid": target_id, "file_id": node.id, "nodupli": "1"})
        return None  # 115 doesn't say the copy's id: the folder is listed again

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> _Node | None:
        source = _Source(data, size)
        try:
            return self._upload(source, name, folder_id)
        finally:
            source.close()

    def _read_node(self, node: _Node, offset: int, length: int | None) -> ReadStream:
        headers = {"User-Agent": DOWNLOAD_USER_AGENT, "Accept-Encoding": "identity"}
        span = range_header(offset, length, node.size)
        if span is not None:
            headers["Range"] = span
        for attempt in range(2):
            url = self._link(node, DOWNLOAD_USER_AGENT, fresh=attempt > 0)
            request = self._http.build_request("GET", url, headers=headers)
            try:
                response = self._http.send(request, stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code in (403, 404, 410) and attempt == 0:
                http.drain(response)  # an expired link: ask for a new one
                self._links.pop((node.pick_code, DOWNLOAD_USER_AGENT))
                continue
            return response_stream(response, offset=offset, length=length, total=node.size,
                                   service=SERVICE, modified=node.modified)
        raise AssertionError("unreachable")

    def _cacheable(self, node: _Node) -> bool:
        return bool(node.pick_code)  # (downloads need the pick code)
