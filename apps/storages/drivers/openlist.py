"""OpenList and AList (https://github.com/OpenListTeam/OpenList, https://github.com/AlistGo/alist): the
files of another OpenList or AList server, through its HTTP API (/api/fs/...). Both have the same API
(AList v3; OpenList is its fork), documented at https://alist.nn.ci/guide/api/ and https://doc.oplist.org.

Paths
    The storage root ("/") is the server's folder `base_path`; "/a/b" is base_path + "/a/b". The server takes
    them below the account's own base path (the root of its OpenList user: user.JoinPath in server/handles),
    and its /d links take the server's full path, so that base path is asked for once (GET /api/me). Paths
    are sent as they are, in JSON; the download links encode each segment. A file or folder is found in its
    folder's /api/fs/list, which OpenList answers from its cache of the folder: its /api/fs/get of a file asks
    the storage behind it for a download link (FsGet's fs.Link), and fails for a file that storage gives none
    (a file it blocked, a spent download quota), so that the file couldn't even be deleted. /api/fs/get is
    asked about the root only, a folder.

Login
    With `username` and `password`, POST /api/auth/login gives a token (valid for two days by default),
    sent as "Authorization: <token>"; it is asked again when the server refuses it (code 401). Any other
    answer to a login is a refusal: AList's code 400 for a wrong password, 402 for an account with two-step
    verification, 429 once the server locks logins from this site's address after 5 failures (for 5 minutes,
    which AList starts again at every attempt). A refusal is remembered for `login_refusal_ttl` seconds,
    longer than that lock, so that a wrong password doesn't keep it on. Or a fixed `token`, the one in the
    server's settings: it doesn't expire, and a refusal is an AuthError. Without either, the server's guest
    account is used. `folder_password` is sent with every request, for folders the server protects with a
    password (its "meta" passwords).

Requests
    Answers: {"code": 200, "message": "success", "data": ...}; another code is an error, whatever the
    HTTP status: 401 a refused token, 403 no permission (or a wrong folder password), 500 with a
    message such as "object not found" or "not a folder". An answer that isn't such JSON is a proxy's: a 413
    refuses an upload as too large, the others are reported with their HTTP status.

Changes
    Moving and copying within the server are its /api/fs/move and /api/fs/copy. Between two of its storages,
    and on a storage without a move or copy of its own, the server answers at once with tasks: they are
    asked about (/api/task/<move or copy>/info) until they are over, for at most `task_wait` seconds. A task
    that failed is a DriverError, one still running then a BackendError. The guest may not ask about tasks:
    for it the answer is taken as it is, and such a move or copy may finish after the call returns.

Downloads
    /api/fs/list gives each file's "sign"; GET /d/<the account's base path><path>?sign=<sign> sends the file,
    or redirects to where the server's storage has it. download_url() is that link (it works without the
    token, for as long as the server's signs last: for ever with its default link_expiration of 0); open()
    fetches it with the Range, without the token, following the redirects. A 401 or 404 there asks /api/me
    again, in case the account's base path changed.

Uploads
    PUT /api/fs/put with the file as the body, its path in the File-Path header (percent-encoded), "Overwrite:
    true" and the type guessed from the name (OpenList guesses one only when there is no Content-Type). The
    server answers once its own storage has the file; for a cloud drive it first buffers the whole upload
    (115, 123, Aliyun, Baidu, ...), so the answer may come long after the last byte: it is waited for
    `upload_wait` seconds, and `upload_wait_per_mib` more for each MiB.

Names
    "\\" is taken as "/" by the server. A rename that changes only the case of a name, which a storage that
    ignores case (OpenList's Local storage on Windows or macOS) refuses as taken, goes through a third name.
"""

from __future__ import annotations

import datetime
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import filetypes, http, paths
from ._cloud import (
    TokenCache,
    as_int,
    exact_body,
    json_or_none,
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
    only_case_differs,
    rename_through,
)

SERVICE = gettext_lazy("OpenList")
TOKEN_LIFETIME = 47 * 3600.0  # the server's default is 48 hours; asked again on a refusal anyway
MIB = 1024 * 1024
# The states of a task (tache.State) that is over: succeeded, canceled, failed.
TASK_SUCCEEDED = 2
TASK_OVER = (2, 4, 7)
_SHARE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_SHARE_LINK = re.compile(r"/(?:@s|sd)/([^/]+)")  # /@s/<id> (the share's page), /sd/<id> (a share of one file)


def _moment(value: Any) -> datetime.datetime | None:
    moment = parse_time(value)
    return moment if moment is not None and moment.year > 1970 else None  # Go's zero time: unknown


def _share_id(value: Any) -> str:
    """The share's ID, as it is or from the share's link (https://openlist.example.com/@s/<id>)."""
    text = str(value or "").strip()
    if "/" not in text:
        return text
    match = _SHARE_LINK.search(urllib.parse.urlsplit(text).path)
    return urllib.parse.unquote(match.group(1)) if match else text


class OpenListDriver(StorageDriver):
    type = "openlist"
    label = gettext_lazy("OpenList / AList")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://alist.example.com",
              help=gettext_lazy("The address of the OpenList or AList server, as it is opened in a browser.")),
        Param("username", gettext_lazy("Username"), required=False,
              help=gettext_lazy("An account of that server. Leave the username, password and token empty to use its guest account.")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True),
        Param("token", gettext_lazy("Token"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("Instead of a username and password: the token in the server's settings (Settings → Other). It doesn't expire.")),
        Param("folder_password", gettext_lazy("Folder password"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("For folders the server protects with a password.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the server that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    # The server's paths: "\\" is taken as "/" there.
    forbidden_chars = "\\"

    setup_params = ("url", "username", "password", "token")

    # The endpoint of the login, below the server's address.
    login_path: ClassVar[str] = "/api/auth/login"

    # Tuning, tests make them small: seconds a refused login is remembered (see Login in the module's docstring);
    # seconds to wait for an upload's answer, and more for each MiB; how long and how often to ask about tasks.
    login_refusal_ttl: ClassVar[float] = 600.0
    upload_wait: ClassVar[float] = http.TIMEOUT
    upload_wait_per_mib: ClassVar[float] = 1.0
    task_wait: ClassVar[float] = 60.0
    poll_interval: ClassVar[float] = 0.5

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = str(self.config.get("url") or "").strip().rstrip("/")
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._fixed = str(self.config.get("token") or "").strip()
        self._login = bool(str(self.config.get("username") or "").strip()) and not self._fixed
        self._http = http.client()
        self._tokens = TokenCache(self._log_in)
        self._tokens.failure_ttl = self.login_refusal_ttl
        self._account_base: str | None = None  # the account's base path on the server, from GET /api/me

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        url = httpx.URL(str(config.get("url") or "").strip()) if str(config.get("url") or "").strip() else None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            errors["url"] = gettext("The address should start with http:// or https://, like https://alist.example.com.")
        if str(config.get("username") or "").strip() and not config.get("password"):
            errors["password"] = gettext("Give the password of that account too.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        return errors

    # -- login

    def _log_in(self) -> tuple[str, float]:
        """A token of the account; AuthError for any answer but code 200 (see Login in the module's docstring)."""
        body = {"username": str(self.config.get("username") or "").strip(), "password": str(self.config.get("password") or "")}
        try:
            response = self._http.post(self._root + self.login_path, json=body)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        answer = self._answer(response)
        code = as_int(answer.get("code"))
        if code == 200:
            data = answer.get("data")
            token = data.get("token") if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise BackendError(gettext("%(service)s gave no token for the login.") % {"service": SERVICE})
            return token, TOKEN_LIFETIME
        if code == 402:
            raise AuthError(gettext("The %(service)s account uses two-step verification, which HFile can't answer. "
                                    "Please use the server's token instead, or an account without it.") % {"service": SERVICE})
        if code == 429:
            raise AuthError(gettext("%(service)s blocks logins from this server for a few minutes after failed "
                                    "attempts. Please check the username and password in the storage source's "
                                    "settings, then wait a few minutes.") % {"service": SERVICE})
        message = short(answer.get("message"))
        raise AuthError(gettext("%(service)s refused the account or token: %(message)s. Please check the storage source's settings.") % {"service": SERVICE, "message": message or code})

    # -- requests

    def _remote(self, path: str) -> str:
        return paths.rebase(path, self._base)

    def _error(self, code: int | None, message: str) -> DriverError:
        lowered = message.lower()
        if any(words in lowered for words in ("not found", "not exist", "no such", "doesn't exist")):
            return NotFound()
        if "not a folder" in lowered or "not a dir" in lowered:
            return NotADirectory()
        if "exist" in lowered:
            return AlreadyExists()
        if code == 401:
            return AuthError(gettext("%(service)s refused the account or token: %(message)s. Please check the storage source's settings.") % {"service": SERVICE, "message": message or code})
        if code == 403:
            return DriverError(gettext("%(service)s refused this: %(message)s. The account may lack the permission, or the folder password is wrong.") % {"service": SERVICE, "message": message or code})
        return DriverError(gettext("%(service)s refused this (%(code)s): %(message)s") % {"service": SERVICE, "code": code, "message": message})

    def _call(self, api: str, body: Mapping[str, Any] | None = None, *, method: str = "POST",
              params: Mapping[str, str] | None = None) -> Any:
        """The "data" of an API call with the account's token; DriverError for an answer whose code isn't 200."""
        for attempt in range(2):
            headers = {}
            token = None
            if self._fixed or self._login:
                token = self._fixed or self._tokens.get()
                headers["Authorization"] = token
            try:
                response = self._http.request(method, self._root + api, json=dict(body or {}) if method == "POST" else None,
                                              params=params, headers=headers)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            answer = self._answer(response)
            code = as_int(answer.get("code"))
            if code == 200:
                return answer.get("data")
            message = short(answer.get("message"))
            if code == 401 and self._login and token is not None and not attempt:
                self._tokens.invalidate(token)  # expired: log in again, once
                continue
            raise self._error(code, message)
        raise AssertionError("unreachable")

    def _answer(self, response: httpx.Response) -> dict[str, Any]:
        """The JSON answer of the API; an error for an answer that isn't one (a proxy's, or not the server's)."""
        answer = json_or_none(response)
        if isinstance(answer, dict) and "code" in answer:
            return answer
        status = response.status_code
        if 300 <= status < 400:
            raise redirected(response, SERVICE)
        if status == 413:
            raise DriverError(gettext("The web server of %(service)s, or a proxy in front of it, refused the upload as too "
                                      "large (HTTP 413). Please ask its administrator to allow larger requests (for nginx: "
                                      "client_max_body_size).") % {"service": SERVICE})
        if status >= 500:
            raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.") % {"service": SERVICE, "status_code": status})
        raise BackendError(gettext("The answer from %(service)s (HTTP %(status_code)s) couldn't be understood: is the "
                                   "address that of an OpenList or AList server?") % {"service": SERVICE, "status_code": status})

    def _password(self) -> str:
        return str(self.config.get("folder_password") or "")

    def _items(self, folder: str) -> list[dict[str, Any]]:
        """The entries of the folder at `folder`, from /api/fs/list (see Paths in the module's docstring)."""
        data = self._call("/api/fs/list", {"path": self._remote(folder), "password": self._password(),
                                           "page": 1, "per_page": 0, "refresh": False})
        if not isinstance(data, dict) or "content" not in data or not isinstance(data["content"], (list, type(None))):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return [item for item in data["content"] or [] if isinstance(item, dict)]  # null for an empty folder

    def _root_item(self) -> dict[str, Any]:
        """The base path, from /api/fs/get: a folder, so the server asks for no download link."""
        data = self._call("/api/fs/get", {"path": self._remote(paths.ROOT), "password": self._password()})
        if not isinstance(data, dict):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return data

    def _get(self, path: str) -> dict[str, Any]:
        """The file or folder at `path`, as its folder's listing gives it. NotFound."""
        if path == paths.ROOT:
            return self._root_item()
        parent, name = paths.split(path)
        try:
            items = self._items(parent)
        except NotADirectory:  # below a file
            raise NotFound() from None
        found = next((item for item in items if item.get("name") == name), None)
        if found is None:
            raise NotFound()
        return found

    def _existing(self, path: str) -> dict[str, Any] | None:
        try:
            return self._get(path)
        except NotFound:
            return None

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        is_dir = bool(item.get("is_dir"))
        return Entry(name=paths.name(path), path=path, is_dir=is_dir,
                     size=None if is_dir else as_int(item.get("size")), modified=_moment(item.get("modified")))

    def _user_base(self, *, again: bool = False) -> str:
        """The account's base path on the server (see Paths in the module's docstring)."""
        if self._account_base is None or again:
            data = self._call("/api/me", method="GET")
            base = data.get("base_path") if isinstance(data, dict) else None
            try:
                self._account_base = paths.clean(base) if isinstance(base, str) else paths.ROOT
            except InvalidPath:
                self._account_base = paths.ROOT
        return self._account_base

    def _link(self, path: str, item: Mapping[str, Any]) -> str:
        full = paths.rebase(self._remote(path), self._user_base())
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in paths.segments(full))
        sign = item.get("sign")
        return f"{self._root}/d/{quoted}" + (f"?sign={urllib.parse.quote(sign, safe='')}" if isinstance(sign, str) and sign else "")

    def _relinked(self) -> bool:
        """After a 401 or 404 of a download link: whether the account's base path is another now."""
        before = self._account_base
        return self._user_base(again=True) != before

    def _wait(self, kind: str, data: Any) -> None:
        """The tasks a move or copy answered with (`kind`: "move" or "copy"), until they are over (see Changes in the
        module's docstring)."""
        tasks = data.get("tasks") if isinstance(data, dict) else None
        ids = [task["id"] for task in tasks if isinstance(task, dict) and isinstance(task.get("id"), str)] if isinstance(tasks, list) else []
        if not ids or not (self._fixed or self._login):
            return
        deadline = time.monotonic() + self.task_wait
        for task_id in ids:
            while True:
                try:
                    info = self._call(f"/api/task/{kind}/info", params={"tid": task_id})
                except NotFound:  # cleared from the server's list meanwhile: nothing more to learn
                    break
                state = as_int(info.get("state")) if isinstance(info, dict) else None
                if state == TASK_SUCCEEDED:
                    break
                if state in TASK_OVER:
                    error = short(info.get("error")) or "-"
                    raise DriverError(gettext("%(service)s couldn't finish this: %(error)s") % {"service": SERVICE, "error": error})
                if time.monotonic() >= deadline:
                    raise BackendError(gettext("%(service)s is still working on this in the background. Please refresh in a "
                                               "while to see the result.") % {"service": SERVICE})
                time.sleep(self.poll_interval)

    # -- files

    def list(self, path: str) -> list[Entry]:
        entries = []
        for item in self._items(path):  # (a file's path: "not a folder", NotADirectory)
            name = item.get("name")
            if usable_name(name) and "\\" not in name:
                entries.append(self._entry(paths.join(path, name), item))
        return entries

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            if not self._root_item().get("is_dir"):
                raise NotFound()
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._get(path))

    def _file(self, path: str) -> dict[str, Any]:
        item = self._get(path)
        if item.get("is_dir"):
            raise NotFound()
        return item

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(path)
        total = as_int(item.get("size"))
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        for attempt in range(2):
            try:
                # No token: the sign is the permission, and the redirects may lead to another host.
                response = self._http.send(self._http.build_request("GET", self._link(path, item), headers=headers),
                                           stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code in (401, 404):
                http.drain(response)
                if not attempt and self._relinked():
                    continue
                if response.status_code == 404:
                    raise NotFound()
                raise BackendError(gettext("Downloading from %(service)s failed (HTTP %(status_code)s).") % {"service": SERVICE, "status_code": response.status_code})
            return response_stream(response, offset=offset, length=length, total=total, service=SERVICE,
                                   modified=_moment(item.get("modified")))
        raise AssertionError("unreachable")

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        if not self.can_direct_download:  # a share with a password, see OpenListShareDriver
            return None
        return self._link(path, self._file(path))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        existing = self._existing(path)
        if existing is not None and existing.get("is_dir"):
            raise AlreadyExists()
        headers = {"File-Path": urllib.parse.quote(self._remote(path), safe="/"), "Content-Length": str(size),
                   "Overwrite": "true", "As-Task": "false", "Password": self._password()}
        mime = filetypes.guess_type(paths.name(path), strict=False)[0]
        if mime:
            headers["Content-Type"] = mime
        # The answer comes once the server's storage has the file: see Uploads in the module's docstring.
        timeout = httpx.Timeout(http.TIMEOUT, connect=http.CONNECT_TIMEOUT,
                                read=self.upload_wait + size / MIB * self.upload_wait_per_mib)
        for attempt in range(2):
            token = None
            if self._fixed or self._login:
                token = self._fixed or self._tokens.get()
                headers["Authorization"] = token
            try:
                response = self._http.put(self._root + "/api/fs/put", content=exact_body(data, size), headers=headers,
                                          timeout=timeout)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            answer = self._answer(response)
            code = as_int(answer.get("code"))
            if code == 200:
                return
            if code == 401 and self._login and token is not None and not attempt and hasattr(data, "seek"):
                try:  # the token expired before the upload: again, from the start of the data
                    data.seek(0)
                except (OSError, ValueError):
                    pass
                else:
                    self._tokens.invalidate(token)
                    continue
            raise self._error(code, short(answer.get("message")))

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT or self._existing(path) is not None:
            raise AlreadyExists()
        parent = paths.parent(path)
        found = self._existing(parent) if parent != paths.ROOT else {"is_dir": True}
        if found is not None and not found.get("is_dir"):
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": parent})
        self._call("/api/fs/mkdir", {"path": self._remote(path)})  # with the missing folders above

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._get(path)  # NotFound: the server says nothing about a name that isn't there
        parent, name = paths.split(path)
        self._call("/api/fs/remove", {"dir": self._remote(parent), "names": [name]})

    def _rename_to(self, path: str, target: str) -> None:
        self._call("/api/fs/rename", {"path": self._remote(path), "name": paths.name(target)})

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        parent, name = paths.split(path)
        try:
            names = {item.get("name") for item in self._items(parent)}
        except NotADirectory:  # below a file
            raise NotFound() from None
        if name not in names:
            raise NotFound()
        if new_name == name:
            return
        if new_name in names:
            raise AlreadyExists()
        try:
            self._rename_to(path, paths.join(parent, new_name))
        except AlreadyExists:
            if not only_case_differs(name, new_name):
                raise
            # A storage that ignores case finds the file itself under the new name: see Names in the module's docstring.
            rename_through(self._rename_to, path, new_name)

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        self._get(path)
        target = self._get(folder)
        if not target.get("is_dir"):
            raise NotADirectory()
        parent, name = paths.split(path)
        if parent == folder:
            if copy:
                raise AlreadyExists()
            return  # already there
        if self._existing(paths.join(folder, name)) is not None:
            raise AlreadyExists()
        data = self._call("/api/fs/copy" if copy else "/api/fs/move",
                          {"src_dir": self._remote(parent), "dst_dir": self._remote(folder), "names": [name]})
        self._wait("copy" if copy else "move", data)

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)


class OpenListShareDriver(OpenListDriver):
    """A share of an OpenList server (OpenList 4's 分享, /@s/<id>), read-only, as its guests see it.

    Listings are /api/fs/list of /@s/<share id>/<path> with the share's password; a file or folder is found in
    its folder's listing; downloads are /sd/<share id>/<path>?pwd=<password>, which may redirect to where the
    server's storage has the file. With a password they go through this site (can_direct_download is False):
    the password would be in the link, and opens all of the share. A share of one file lists as that file, and
    is refused: the storage's root must be a folder."""

    type = "openlist-share"
    label = gettext_lazy("OpenList share")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://openlist.example.com",
              help=gettext_lazy("The address of the OpenList server, as it is opened in a browser.")),
        Param("share_id", gettext_lazy("Share ID"),
              help=gettext_lazy("The share's link, or the part after /@s/ in it: for https://openlist.example.com/@s/AbC123 it is AbC123.")),
        Param("share_password", gettext_lazy("Share password"), kind="password", required=False, secret=True,
              help=gettext_lazy("When the share has one.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in the share that is this storage source's root, like / or /folder1.")),
    )
    can_move = False
    can_copy = False
    can_write = False
    forbidden_chars = ""
    setup_params = ("url", "share_id")

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._share = _share_id(self.config.get("share_id"))
        self._login, self._fixed = False, ""  # as a guest
        if self._password():
            self.can_direct_download = False  # see the class's docstring

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = {key: value for key, value in super().validate_config(config).items() if key in ("url", "base_path")}
        if not _SHARE_ID.fullmatch(_share_id(config.get("share_id"))):
            errors["share_id"] = gettext("The share ID is the part after /@s/ in the share's link; the whole link can be pasted too.")
        return errors

    def _remote(self, path: str) -> str:
        inner = paths.rebase(path, self._base)
        return f"/@s/{self._share}" + ("" if inner == paths.ROOT else inner)

    def _password(self) -> str:
        return str(self.config.get("share_password") or "")

    def _items(self, folder: str) -> list[dict[str, Any]]:
        try:
            return super()._items(folder)
        except NotADirectory:
            if folder == paths.ROOT and self._base == paths.ROOT:  # a share of one file lists as that file
                raise DriverError(gettext("This OpenList share is a single file, which can't be a storage source. "
                                          "Please share a folder instead.")) from None
            raise

    def _root_item(self) -> dict[str, Any]:
        """From the share's listing: whether /api/fs/get takes a share's paths isn't documented."""
        self._items(paths.ROOT)  # the share must be there
        return {"is_dir": True}

    def _link(self, path: str, item: Mapping[str, Any]) -> str:
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in paths.segments(paths.rebase(path, self._base)))
        password = self._password()
        return (f"{self._root}/sd/{urllib.parse.quote(self._share, safe='')}" + (f"/{quoted}" if quoted else "")
                + (f"?pwd={urllib.parse.quote(password, safe='')}" if password else ""))

    def _relinked(self) -> bool:
        return False  # a share's links have no account's base path
