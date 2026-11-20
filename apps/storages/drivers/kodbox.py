"""KodBox (可道云, https://kodcloud.com, https://github.com/kalcaddle/kodbox), a self-hosted PHP cloud drive: the
files of one KodBox account, through the API its own pages use. Documentation: https://doc.kodcloud.com/v2/ (the
markdown is under https://doc.kodcloud.com/v2/doc/: user/user.md, explorer/file.md, explorer/checkHashSimple.md,
help/options.md). Where it is silent, the facts are from KodBox's source in that repository (the paths below are
in it). Each fact is marked [docs] or [source]; KodBox's core (app/core, app/driver, app/model: its storage and
database layers) isn't in the repository, so what only the core decides is marked [unverified].

Paths
    KodBox knows files and folders as {source:ID}/ [docs]. Paths are found by listing from the account's own
    folder (its 个人空间), user.myhome of user/view/options [source: app/controller/user/view.class.php], down to
    `base_path` (IdDriver). Listings are explorer/list/path, `page_size` a page while pageInfo.pageTotal says
    there are more [docs]; entries that aren't {source:ID}/ (the safe box, {block:safe}/, which the account's
    folder lists [source: app/controller/explorer/listSafe.class.php]) are left out.

Login
    user/index/loginSubmit with name and password gives the access token [docs], only while KodBox's login
    captcha is off [docs; source: app/controller/user/index.class.php]. Every call sends it as accessToken in a
    POST body, never in a URL: KodBox takes every parameter from the query or the body alike, and the token from
    $_REQUEST [source: parse_incoming in app/function/web.function.php, user/index.class.php]. It is kept in
    memory; it lasts 30 days, but its session ends after 4 hours unused (sessionTime) [source:
    user/index.class.php, config/setting.php]. Code "10001" (ERROR_CODE_LOGOUT: not logged in, the token expired
    or unknown) logs in again, once; so does "10002" (ERROR_CODE_USER_INVALID) from a call: KodBox ended the
    session, because the account logged in on more devices than it may (userLoginLimit, 5 by default, for accounts
    that aren't the admin's) or was disabled [source: filter/userLoginState.class.php checkLimit, user/authRole.class.php
    autoCheck, user/index.class.php userDataInit]. "10002" from the login itself is a disabled account, and info
    "10011" a captcha it asks for [source: config/const.php, user/setting.class.php]. close() logs out
    (user/index/logout), so that an instance no longer used doesn't hold one of the account's places for the 4 hours
    its session would last; best effort, briefly [source: user/index.class.php logout]. KodBox's cookies aren't
    kept: the token is the only credential.

Requests
    Answers: {"code": true, "data": ..., "info": ...}, or code false with the message in data [docs], with HTTP
    200 either way [source: show_json, app/function/common.function.php]. The messages are in the language the
    call asks for [source: app/kod/I18n.class.php]: every call asks for zh-CN, whose texts tell NotFound,
    AlreadyExists and refused names apart [source: config/i18n/zh-CN/index.php].

Changes
    mkdir: explorer/index/mkdir with {source:PARENT}/name answers the new folder's path [docs]; KodBox keeps a
    folder already there (skip), and answers a file's parent folder when a file has the name, which is
    AlreadyExists here [source: app/controller/explorer/index.class.php]. rename: pathRename [docs]. move and
    copy: pathCuteTo and pathCopyTo [docs] with fileRepeat skip, so that a name taken meanwhile isn't replaced
    (the account's default is replace) [source: pathPast, config/setting.php]; a move keeps the id [unverified].
    delete: pathDelete without shiftDelete [docs], which puts it into the account's recycle bin (回收站) while the
    account's "Trash" (开启回收站) setting is on, as it is by default; with it off KodBox deletes for good [source:
    explorer/index.class.php, explorer/recycleDriver.class.php, config/setting.php].

Downloads
    Through this site only (can_direct_download False): explorer/index/fileOut needs the token, and the
    downloadPath of pathInfo is a link whose hash never expires [source: link() in
    app/controller/explorer/share.class.php, app/kod/Mcrypt.class.php]. open() POSTs fileOut with path and
    download=1 [docs: a GET] and the Range header. The core sends the file [unverified], but KodBox expects
    ranged fileOut requests (updateLastOpen reads their Range) and its WebDAV server, which sends files with the
    same IO::fileOut, says Accept-Ranges: bytes [source: explorer/index.class.php, plugins/webdav/php/], so
    can_range; a 200 to a ranged request is cut down here anyway. A redirect (to where a KodBox storage on object
    storage has the file [source: plugins/webdav/php/kodWebDav.class.php]) is followed without the token. JSON
    instead of the file (show_json, without the attachment's Content-Disposition) is an error, or an ended login.

Uploads
    explorer/upload/fileUpload [docs], as multipart/form-data: the file in "file", the folder, name and size
    beside it with the token (the raw-body upload of KodBox's pages would need them in the URL) [docs; source:
    app/kod/Uploader.class.php]; fileRepeat replace, so it replaces a file of that name (upload_conflict
    "replace") [source: app/controller/explorer/upload.class.php]; fileInfo 1, so the answer names the file as
    stored. A file larger than KodBox's upload chunk size (system.settings.upload.chunkSize of user/view/options,
    which its admin sets in 系统设置 → 基础设置 → 上传/下载, smaller than PHP's upload_max_filesize and
    post_max_size [docs: help/options.md; source: app/controller/admin/setting.class.php]) goes in parts with
    chunkSize, chunks and chunk, counted from 0; the answer with info ends it [docs; source: Uploader]. When
    `data` can seek, every part carries the file's checkHashSimple [docs: explorer/checkHashSimple.md]: KodBox
    then keeps its parts apart from an unfinished upload of the same name and size into that folder, which it
    would otherwise finish with, and checks the whole file [source: Uploader]. The request that ends an upload is
    answered once KodBox has stored the whole file, which takes long on object storage [source:
    explorer/upload.class.php fileUpload, IO::upload]: its answer is waited for a second per MiB longer than the
    others (http.TIMEOUT).

Names
    KodBox refuses / \\ : * ? " < > | in new names (forbidden_chars) and names over 256 bytes, and its database
    keeps 250 characters (max_name_bytes 250) [source: pathAllowCheck in explorer/index.class.php,
    app/controller/install/data/mysql.sql]. Characters outside the BMP (emoji) are refused here: unless its
    database is utf8mb4 (the default is utf8), KodBox stores them as "-" [source: pathAllowCheck,
    config/setting.php]. Listings leave out names starting with . or ~ and those of the system option
    pathHidden (default Thumb.db,.DS_Store,.gitignore,.git,*.temp,*.tmp) unless the account shows hidden files
    [source: parseDataHidden in app/controller/explorer/list.class.php]: such names are refused here then, as
    they would vanish. An upload KodBox stores under another name is an error.

Limits
    Not tried against a real KodBox server: the tests use a fake built from the documentation and the
    source above.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import re
import secrets
import threading
from collections.abc import Callable, Iterator, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
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
    read_exact,
    redirected,
    response_stream,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, StaleFolder, now
from .base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotFound, Param, ReadStream

SERVICE = gettext_lazy("KodBox")
LANGUAGE = "zh-CN"  # of KodBox's messages, which are matched below
SIGNED_OUT = "10001"  # ERROR_CODE_LOGOUT
DISABLED = "10002"  # ERROR_CODE_USER_INVALID
CAPTCHA = "10011"  # ERROR_IMG_CODE
DENIED = ("1004", "1007")  # the info of "no permission" answers
DEFAULT_HIDDEN = "Thumb.db,.DS_Store,.gitignore,.git,*.temp,*.tmp"  # pathHidden, when KodBox doesn't say
SAMPLE, SAMPLES = 200, 50  # checkHashSimple: 50 samples of 200 bytes, and the last 200
UPLOAD = "explorer/upload/fileUpload"
ANSWER_LIMIT = 1024 * 1024  # bytes of a JSON answer read instead of a download
_SOURCE = re.compile(r"\{source:([0-9]+)\}/?")
_EXT = re.compile(r"[0-9a-zA-Z_\-]+")  # get_path_ext's extension

# Words of KodBox's zh-CN messages (config/i18n/zh-CN/index.php) that tell its refusals apart.
_GONE, _NO_FOLDER, _IN_TRASH, _TAKEN, _BAD_CHARS, _TOO_LONG, _FULL, _NOT_ALLOWED = (  # i18n: data
    "该文档不存在", "目标目录不存在", "回收站中", "该名称已存在", "不支持的特殊字符", "长度超出最大限制", "剩余空间不足", "没有此权限")


def _address(value: Any) -> str:
    """The KodBox site's address without a query, a fragment, "index.php" or the "/" at the end."""
    address = str(value or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")
    return address[: -len("/index.php")] if address.endswith("/index.php") else address


def _path(node_id: str) -> str:
    return "{source:" + node_id + "}/"


def _source_id(path: Any) -> str | None:
    """The id of a {source:ID}/ path; None for anything else."""
    match = _SOURCE.fullmatch(path) if isinstance(path, str) else None
    return match.group(1) if match else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _node(item: Any) -> Node | None:
    """A Node of a listing's entry; None for what isn't a {source:ID}/ with a usable name."""
    item = _dict(item)
    node_id, name = _source_id(item.get("path")), item.get("name")
    if node_id is None or not usable_name(name):
        return None
    is_dir = item.get("type") == "folder"
    return Node(id=node_id, name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                modified=parse_time(item.get("modifyTime")))


def _items(node: Node) -> str:
    """dataArr: the JSON list of what a call acts on."""
    return json.dumps([{"path": _path(node.id), "name": node.name, "type": "folder" if node.is_dir else "file"}])


def _message(answer: Mapping[str, Any]) -> str:
    data = answer.get("data")
    return short(data) if isinstance(data, str) else ""


def _signed_out(answer: Mapping[str, Any]) -> bool:
    """KodBox's answer that the call isn't logged in: an expired or unknown token, or its session ended."""
    code = answer.get("code")
    return (code is not True and str(code) == SIGNED_OUT) or (code is False and str(answer.get("info")) == SIGNED_OUT)


def _ended(answer: Mapping[str, Any]) -> bool:
    """KodBox's answer that it has ended the call's session (code 10002): the account logged in on more devices than it
    may, or was disabled. A login then says which."""
    code = answer.get("code")
    return code is not True and str(code) == DISABLED


def hash_simple(data: BinaryIO, start: int, size: int) -> str:
    """KodBox's checkHashSimple of `size` bytes of `data` from `start`: the MD5 of 50 samples of 200 bytes spread
    over the file and of its last 200 bytes (of all of it, up to 10,000 bytes), then the size in decimal."""
    digest = hashlib.md5()
    if size <= SAMPLE * SAMPLES:
        data.seek(start)
        for chunk in exact_body(data, size):
            digest.update(chunk)
    else:
        step = size // SAMPLES
        for offset in [step * n for n in range(SAMPLES)] + [size - SAMPLE]:
            data.seek(start + offset)
            digest.update(read_exact(data, SAMPLE))
    return digest.hexdigest() + str(size)


@dataclasses.dataclass(frozen=True)
class _Account:
    """What user/view/options says of the account."""

    home: str  # the id of its own folder
    chunk: int  # KodBox's upload chunk size, in bytes
    show_hidden: bool  # its "Show hidden files" (displayHideFile)
    hidden: tuple[str, ...]  # pathHidden: names, and *.ext

    def hides(self, name: str, is_dir: bool) -> bool:
        """Whether KodBox's listings leave the name out (parseDataHidden)."""
        if self.show_hidden:
            return False
        if name[:1] in (".", "~") or name in self.hidden:
            return True
        dot = name.rfind(".")
        ext = _EXT.search(name[dot + 1:]) if dot >= 0 and not is_dir else None
        return ext is not None and "*." + ext.group(0).lower() in self.hidden


class _Part:
    """The bytes one upload request sends: `length` bytes of `data` from `at` (None: from where the stream is, which
    can't be sent again)."""

    def __init__(self, data: BinaryIO, at: int | None, length: int):
        self.data, self.at, self.length = data, at, length

    def body(self, form: Mapping[str, str]) -> tuple[Iterator[bytes], dict[str, str]]:
        """multipart/form-data: the form's fields, then the bytes as "file" (its name is in the form)."""
        if self.at is not None:
            self.data.seek(self.at)
        boundary = "hfile-" + secrets.token_hex(16)
        fields = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                         for key, value in form.items())
        head = (fields + f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="blob"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(head) + self.length + len(tail))}
        return itertools.chain([head], exact_body(self.data, self.length), [tail]), headers


def _api_answer(response: httpx.Response) -> dict[str, Any] | None:
    """The answer KodBox gave instead of a file (an error, an ended login), or None when it is the file: show_json's
    JSON comes without a download's Content-Disposition, and with X-Request-Error when it refuses."""
    headers = response.headers
    json_answer = "json" in headers.get("content-type", "").lower() and "content-disposition" not in headers
    if response.status_code != 200 or not (json_answer or "x-request-error" in headers):
        return None
    body = b""
    try:
        for chunk in response.iter_raw():
            body += chunk
            if len(body) > ANSWER_LIMIT:
                break
    except httpx.HTTPError as error:
        raise transport_error(error, SERVICE) from None
    finally:
        response.close()
    try:
        answer = json.loads(body.decode("utf-8"))
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or "code" not in answer:
        raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
    return answer


class KodBoxDriver(IdDriver):
    type = "kodbox"
    label = gettext_lazy("KodBox")
    service = SERVICE
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://kod.example.com",
              help=gettext_lazy("The address of the KodBox site, as it is opened in a browser.")),
        Param("username", gettext_lazy("Username"),
              help=gettext_lazy('An account of that site. HFile logs in as its login page does, which works only '
                                'while KodBox\'s login captcha is off: its administrator turns off "Need '
                                'verification" in System settings → Security.')),
        Param("password", gettext_lazy("Password"), kind="password", secret=True),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the account's own space that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = False  # see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    upload_conflict = "replace"
    forbidden_chars = '\\:*?"<>|'
    max_name_bytes = 250

    # Tuning; tests make them small.
    page_size: ClassVar[int] = 500
    max_pages: ClassVar[int] = 10_000
    account_ttl: ClassVar[float] = 600.0  # seconds what user/view/options said is kept
    default_chunk: ClassVar[int] = 512 * 1024  # KodBox's own default, when it doesn't say
    token_lifetime: ClassVar[float] = 29 * 86400.0  # under the token's 30 days; its session may end sooner
    logout_timeout: ClassVar[float] = 3.0  # seconds close() gives KodBox to end the session

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = _address(self.config.get("url"))
        # No cookie is kept: KodBox's session cookie would be a second credential beside the token.
        self._http = http.client(cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])))
        self._tokens = TokenCache(self._log_in)
        self._session: str | None = None  # the token of the last login, which close() ends; TokenCache's lock
        self._account_lock = threading.Lock()
        self._accounts = TTLCache(1, self.account_ttl)

    def close(self) -> None:
        token, self._session = self._session, None
        if token is not None:
            try:  # best effort: else the session ends by itself when it has been unused for 4 hours
                self._http.post(self._url("user/index/logout"), data={"accessToken": token, "language": LANGUAGE},
                                timeout=self.logout_timeout)
            except Exception as error:  # noqa: BLE001 - nothing is to be done about it now
                self.log.debug("couldn't log out: %s", type(error).__name__)
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = cls.base_path_errors(config)
        raw = str(config.get("url") or "").strip()
        try:
            url = httpx.URL(raw) if raw else None
        except httpx.InvalidURL:
            url = None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            errors["url"] = gettext("The address should start with http:// or https://, like https://kod.example.com.")
        return errors

    # -- login

    def _log_in(self) -> tuple[str, float]:
        form = {"name": str(self.config.get("username") or "").strip(), "password": str(self.config.get("password") or ""),
                "language": LANGUAGE}
        answer = self._answer(self._send(self._http.build_request("POST", self._url("user/index/loginSubmit"), data=form)))
        if answer.get("code") is True:
            token = answer.get("info")
            if not isinstance(token, str) or not token:
                raise BackendError(gettext("%(service)s gave no token for the login.") % {"service": SERVICE})
            self.log.debug("logged in")
            self._session = token
            return token, self.token_lifetime
        message = _message(answer)
        if str(answer.get("info")) == CAPTCHA:
            raise AuthError(gettext("KodBox asks for a captcha to log in. Its administrator must turn off "
                                    '"Need verification" in System settings → Security for HFile to log in.'))
        if str(answer.get("code")) == DISABLED:
            raise AuthError(gettext("KodBox says the account is disabled or not enabled yet: %(message)s") % {"message": message})
        raise AuthError(gettext("KodBox refused the login: %(message)s") % {"message": message or answer.get("code")})

    # -- requests

    def _url(self, route: str) -> str:
        return f"{self._root}/index.php?{route}"

    def _send(self, request: httpx.Request, *, stream: bool = False, follow_redirects: bool = False) -> httpx.Response:
        try:
            return self._http.send(request, stream=stream, follow_redirects=follow_redirects)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _answer(self, response: httpx.Response) -> dict[str, Any]:
        """The JSON answer of a call; BackendError when there is none."""
        status = response.status_code
        if 300 <= status < 400:
            raise redirected(response, SERVICE)
        if status == 413:
            raise DriverError(gettext("KodBox's web server refused an upload request as too large (HTTP 413). Its "
                                      "administrator must make the web server's limit larger than KodBox's upload "
                                      "chunk size (System settings → Basic → Upload Download)."))
        answer = json_or_none(response)
        if isinstance(answer, dict) and "code" in answer:
            return answer
        if status >= 500:
            raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                               % {"service": SERVICE, "status_code": status})
        raise BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of a KodBox site?")
                           % {"service": SERVICE})

    def _post(self, route: str, fields: Mapping[str, Any], *, part: _Part | None = None,
              timeout: httpx.Timeout | None = None) -> dict[str, Any]:
        """One call: its answer, when its code is true; else a DriverError. Logs in again once when KodBox says the
        call isn't logged in, or that it ended the session; an upload part is then sent again only when its data can
        seek. `timeout`: the request's own, for the part that ends an upload."""
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        for attempt in range(2):
            token = self._tokens.get()
            form = {"accessToken": token, "language": LANGUAGE, **{key: str(value) for key, value in fields.items()}}
            if part is None:
                request = self._http.build_request("POST", self._url(route), data=form, **extra)
            else:
                content, headers = part.body(form)
                request = self._http.build_request("POST", self._url(route), content=content, headers=headers, **extra)
            answer = self._answer(self._send(request))
            if (_signed_out(answer) or _ended(answer)) and not attempt:
                # Its session ended: 4 hours unused, KodBox's were cleared, or KodBox signed it out (see the docstring).
                self._tokens.invalidate(token)
                if part is None or part.at is not None:
                    continue
                raise BackendError(gettext("The KodBox login expired during the upload. Please try again."))
            if answer.get("code") is True:
                return answer
            raise self._failure(answer)
        raise AssertionError("unreachable")

    @staticmethod
    def _failure(answer: Mapping[str, Any]) -> DriverError:
        message = _message(answer)
        if _signed_out(answer):
            return AuthError(gettext("KodBox still says HFile isn't logged in, right after its login (%(message)s). "
                                     "Please check the storage source's settings.") % {"message": message})
        if str(answer.get("code")) == DISABLED:
            return AuthError(gettext("KodBox signed the account out: it is disabled, or logged in elsewhere (%(message)s).")
                             % {"message": message})
        if _GONE in message or _NO_FOLDER in message or _IN_TRASH in message:
            return NotFound()
        if _TAKEN in message:
            return AlreadyExists()
        if _BAD_CHARS in message or _TOO_LONG in message:
            return InvalidPath(gettext("KodBox doesn't accept this name: %(message)s") % {"message": message})
        if _FULL in message:
            return DriverError(gettext("%(service)s is out of space.") % {"service": SERVICE})
        if _NOT_ALLOWED in message or str(answer.get("info")) in DENIED:
            return DriverError(gettext("KodBox doesn't let this account do this: %(message)s") % {"message": message})
        if "UPLOAD_ERR_INI_SIZE" in message:
            return DriverError(gettext("KodBox's PHP refused a part of the upload as too large. Its administrator must "
                                       "make PHP's upload_max_filesize larger than KodBox's upload chunk size "
                                       "(System settings → Basic → Upload Download)."))
        return DriverError(gettext("KodBox refused this: %(message)s") % {"message": message or answer.get("code")})

    # -- the account

    def _account(self) -> _Account:
        with self._account_lock:
            account = self._accounts.get("account")
            if account is None:
                account = self._load_account()
                self._accounts.put("account", account)
            return account

    def _load_account(self) -> _Account:
        data = _dict(self._post("user/view/options", {}).get("data"))
        user, system = _dict(data.get("user")), _dict(data.get("system"))
        home = _source_id(user.get("myhome"))
        if home is None:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        chunk = as_int(_dict(_dict(system.get("settings")).get("upload")).get("chunkSize"))
        hidden = _dict(system.get("options")).get("pathHidden")
        return _Account(home=home, chunk=chunk if chunk is not None and chunk > 0 else self.default_chunk,
                        show_hidden=str(_dict(user.get("config")).get("displayHideFile")) == "1",
                        hidden=tuple((hidden if isinstance(hidden, str) else DEFAULT_HIDDEN).split(",")))

    # -- files
    # A new name is checked before the folders above it are made.

    def _check_name(self, name: str, *, is_dir: bool) -> None:
        """InvalidPath for a new name KodBox would change, or leave out of its listings. The folders write() and
        mkdir() make above a path are checked as they are made (_create_node)."""
        if any(ord(ch) > 0xFFFF for ch in name):
            raise InvalidPath(gettext('KodBox can\'t keep characters such as emoji in names: unless its database uses '
                                      'utf8mb4, it stores them as "-". Please use another name.'))
        if self._account().hides(name, is_dir):
            raise InvalidPath(gettext('KodBox leaves "%(name)s" out of its listings (names starting with . or ~, and '
                                      'the hidden names its administrator set), so it would vanish here. To use such '
                                      'names, turn on "Show hidden files" in the KodBox account\'s settings.') % {"name": name})

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path != paths.ROOT:
            self._check_name(paths.name(path), is_dir=False)
        super().write(path, data, size)

    def mkdir(self, path: str) -> None:
        if path != paths.ROOT:
            self._check_name(paths.name(path), is_dir=True)
        super().mkdir(path)

    # -- IdDriver's hooks

    def _account_root(self) -> str:
        return self._account().home

    def _fetch(self, folder_id: str) -> list[Node]:
        nodes: dict[str, Node] = {}  # by id: a page may repeat what the one before had
        for page in range(1, self.max_pages + 1):
            try:
                answer = self._post("explorer/list/path", {"path": _path(folder_id), "page": page, "pageNum": self.page_size})
            except NotFound:
                raise StaleFolder(folder_id) from None
            data = answer.get("data")
            if not isinstance(data, dict):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            current = _dict(data.get("current"))
            if _source_id(current.get("path")) not in (None, folder_id) or current.get("type", "folder") != "folder":
                raise StaleFolder(folder_id)
            for key in ("folderList", "fileList"):
                for item in data.get(key) or []:
                    node = _node(item)
                    if node is not None:
                        nodes[node.id] = node
            if page >= (as_int(_dict(data.get("pageInfo")).get("pageTotal")) or 1):
                return list(nodes.values())
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _create_node(self, parent_id: str, name: str) -> Node:
        self._check_name(name, is_dir=True)
        made = _source_id(self._post("explorer/index/mkdir", {"path": _path(parent_id) + name}).get("info"))
        if made is None:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        if made == parent_id:
            raise AlreadyExists()  # a file has the name: KodBox answers with the folder it is in
        return Node(id=made, name=name, is_dir=True, size=None, modified=now())

    def _delete_node(self, node: Node, parent_id: str) -> None:
        self._post("explorer/index/pathDelete", {"dataArr": _items(node)})  # into the recycle bin, see the docstring

    def _rename_node(self, node: Node, name: str) -> None:
        self._check_name(name, is_dir=node.is_dir)
        self._post("explorer/index/pathRename", {"path": _path(node.id), "newName": name})

    def _relocate(self, route: str, node: Node, target_id: str) -> tuple[list[str], dict[str, Any]]:
        """pathCuteTo or pathCopyTo: the ids of what it made or moved, and the first of its infoMore.listTo."""
        answer = self._post(route, {"dataArr": _items(node), "path": _path(target_id), "fileRepeat": "skip"})
        results = answer.get("info") if isinstance(answer.get("info"), list) else []
        listed = _dict(answer.get("infoMore")).get("listTo")
        return [made for made in map(_source_id, results) if made is not None], _dict(listed[0] if isinstance(listed, list) and listed else None)

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        moved, _ = self._relocate("explorer/index/pathCuteTo", node, target_id)
        if moved and moved[0] != node.id:
            raise AlreadyExists()  # KodBox answered with another item of the name: it was taken meanwhile

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        made, listed = self._relocate("explorer/index/pathCopyTo", node, target_id)
        if not made:
            return None
        name = listed.get("name") if usable_name(listed.get("name")) else node.name
        return dataclasses.replace(node, id=made[0], name=name, modified=now())

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        chunk = self._account().chunk
        start = position(data)
        fields: dict[str, Any] = {"path": _path(folder_id), "name": name, "size": size, "fileRepeat": "replace", "fileInfo": 1}
        # The request that ends the upload is answered once KodBox has stored the whole file (see the docstring).
        last = httpx.Timeout(http.TIMEOUT, connect=http.CONNECT_TIMEOUT, read=http.TIMEOUT + size / (1024 * 1024))
        if size <= chunk:
            answer = self._post(UPLOAD, fields, part=_Part(data, start, size), timeout=last)
        else:
            if start is not None:
                fields["checkHashSimple"] = hash_simple(data, start, size)
            count = -(-size // chunk)
            fields.update(chunkSize=chunk, chunks=count)
            for index in range(count):
                offset = index * chunk
                answer = self._post(UPLOAD, {**fields, "chunk": index},
                                    part=_Part(data, None if start is None else start + offset, min(chunk, size - offset)),
                                    timeout=last if index == count - 1 else None)
                if answer.get("info"):
                    break  # done: at the last part, or sooner when KodBox had the rest from an unfinished upload of it
        info = answer.get("info")
        described = _dict(info)  # fileInfo 1: name, size, path...; else the path alone
        made = _source_id(described.get("path") if described else info)
        if made is None:
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": SERVICE})
        stored = described.get("name") if isinstance(described.get("name"), str) else name
        if stored != name:
            raise DriverError(gettext('KodBox stored the file as "%(stored)s", not as "%(name)s".') % {"stored": stored, "name": name})
        stored_size = as_int(described.get("size"))
        return Node(id=made, name=name, is_dir=False, size=size if stored_size is None else stored_size, modified=now())

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        total = node.size
        if length == 0 or total == 0:
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=node.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        for attempt in range(2):
            token = self._tokens.get()
            form = {"accessToken": token, "language": LANGUAGE, "path": _path(node.id), "download": "1"}
            response = self._send(self._http.build_request("POST", self._url("explorer/index/fileOut"), data=form, headers=headers),
                                  stream=True)
            location = response.headers.get("location")
            if response.is_redirect and location:
                http.drain(response)
                # Where KodBox's storage has the file: fetched without the token, which was only in the POST body.
                response = self._send(self._http.build_request("GET", response.url.join(location), headers=headers),
                                      stream=True, follow_redirects=True)
            else:
                answer = _api_answer(response)
                if answer is not None:
                    if (_signed_out(answer) or _ended(answer)) and not attempt:
                        self._tokens.invalidate(token)
                        continue
                    raise self._failure(answer)
            if response.status_code == 404:
                http.drain(response)
                raise NotFound()
            return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=node.modified)
        raise AssertionError("unreachable")
