"""Cloudreve v3 (https://cloudreve.org, https://github.com/cloudreve/cloudreve, its 3.x tags), the older major version
of Cloudreve, a self-hosted cloud drive: the files of one Cloudreve account, through the API its own pages use.
Cloudreve v3 documents no API: its documentation (https://docsv3.cloudreve.org: use/policy.md, use/policy/compare.md,
use/webdav.md, getting-started/install.md) is about setting it up. The API facts are from Cloudreve's source at tag
3.8.3, the last 3.x release (the paths below are in it), and from the source of its pages
(github.com/cloudreve/frontend at the commit 3.8.3 pins: src/component/Uploader/core/api/index.ts, uploader/base.ts,
public/locales). OpenList's driver (drivers/cloudreve) pointed at the routes; each fact was checked in the source.
Each fact is marked [docs] or [source].

Paths
    Cloudreve v3 lists folders by path and changes things by id. GET /directory/<path>, each name percent-encoded (gin
    matches the decoded path), lists a folder whole: its folders first, then its files, each with its id (a hashid),
    name, type ("dir" or "file"), size and date (when it changed); and the account's storage policy [source:
    routers/router.go, service/explorer/directory.go, pkg/filesystem/manage.go List, pkg/serializer/explorer.go]. A
    missing folder, or a file, answers 40016 [source: pkg/filesystem/path.go]. Files still being uploaded aren't listed
    [source: listObjects]. A file or folder is found in its folder's listing; nothing is cached, so every call asks
    Cloudreve. Folders are unique by name in a folder, and so are files, but a folder and a file can share a name
    [source: models/folder.go, models/file.go: one table and unique index each]; a path finds the folder then.

Login
    POST /user/session with {"userName": email, "Password": password} [source: service/user/login.go] logs in and sets
    Cloudreve's session cookie, cloudreve-session [source: middleware/session.go]. HFile keeps its value in memory only
    and sends it as a Cookie header with API calls to the address, never with download links or where they redirect.
    Cloudreve keeps the session in its cache [source: pkg/sessionstore] and refuses the cookie 30 days after it set it
    (gorilla/securecookie's default maxAge, which pkg/sessionstore keeps): HFile logs in again before then. A call
    Cloudreve answers with 401 "Login required" (the session is gone: a restart without its cache file, a banned
    account [source: middleware/auth.go CurrentUser, AuthRequired]) logs in again and is sent once more, unless its
    body is a stream. The login works only while "Captcha for logins" (Dashboard → Settings → Public Access) is off:
    it answers 40026 or 40027 otherwise [source: middleware/captcha.go, routers/router.go; frontend:
    public/locales/*/dashboard.json]. An account with two-factor authentication answers 203 and wants its code at
    /user/2fa: such accounts can't be used. 40020 is a wrong email or password, 40017 a banned account, 40018 one not
    activated [source: login.go].

Requests
    Answers: {"code": 0, "data": ..., "msg": ""} with HTTP 200; another code is an error [source:
    pkg/serializer/response.go, error.go]: 401 not logged in, 40016 (or 404) not found, 40004 a name taken, 40052 a name
    refused, 40049 a file larger than the storage policy takes, 40050 a file type it doesn't take, 40051 the account out
    of space, 40054 a name being uploaded already, 203 a delete Cloudreve did only in part [source:
    pkg/filesystem/errors.go, manage.go].

Versions
    The routes are under /api/v3; those used here are the same from 3.5.0 to 3.8.3 [source: routers/router.go of each].
    Before 3.5 uploads had another API, and the upload session route answers 404 (an error here naming 3.5) [source:
    3.4.2's routers/router.go]. The answer codes above are those of 3.6.0 to 3.8.3 [source: pkg/serializer/error.go,
    pkg/filesystem/errors.go, the same in both]; 3.5 has older ones for some refusals (404 for a missing path, 40001
    for a wrong password, -1 with only a message), which come out here as Cloudreve's own message. A Cloudreve v4
    site has /api/v4 only and answers every /api/v3 path with gin's 404 [source: v4's routers/router.go and
    middleware/frontend.go at 4.19.1]: that error points to the type Cloudreve.

Changes
    Cloudreve checks few names: it lets a file and a folder take the same name, mkdir keeps a folder already there
    without an error, and a copy onto a taken name answers 40016 [source: models/folder.go Create, Rename,
    MoveFolderTo, MoveOrCopyFileTo; manage.go Copy]. So mkdir, write, rename, move and copy look at the target folder
    first, under a lock per name, and refuse a name taken by anything.
    mkdir: PUT /directory {path}, which makes the folders above too [source: service/explorer/directory.go,
    manage.go CreateDirectory]. rename: POST /object/rename {src, new_name}; move: PATCH /object {src_dir, src, dst};
    copy: POST /object/copy, one thing at a time; src is {dirs, items}, the ids of folders and of files [source:
    routers/router.go, service/explorer/objects.go]. Cloudreve refuses a folder moved into itself but not below itself
    [source: models/folder.go MoveFolderTo]: that is checked here. delete: DELETE /object {dirs, items} deletes for
    good, as Cloudreve v3 has no trash bin; a file its storage couldn't delete stays, with 203 [source: manage.go
    Delete].

Downloads
    PUT /file/download/<id> gives a temporary URL that needs no login [source: service/explorer/file.go
    CreateDownloadSession, routers/router.go: the sign group]: for the local storage policy, Cloudreve's own
    /api/v3/file/download/<session>?sign=..., without the address unless the policy has a CDN address (the address is
    put before it here); for the others, their storage's own URL [source: pkg/filesystem/driver/*/handler.go Source].
    It lasts Cloudreve's download_timeout (600 seconds by default); the API takes no expiry, so download_url() can't
    honour `expires` [source: pkg/filesystem/file.go GetDownloadURL, models/defaults.go]. A download session may work
    once only (the "one-time download" option of the downloader's user group: the anonymous one for HFile, which sends
    no cookie with it) [source: file.go Download], so links aren't reused: open() asks for one each time, and so does
    the site where it would hand one out again (reusable_links). The file
    comes with Range and as an attachment (Content-Disposition, with Cloudreve's name); errors come as JSON with HTTP
    200 and without it, an error here [source: file.go Download, explorer/slave.go ServeFile].

Uploads
    Only the local storage policy takes files through Cloudreve's API [source: models/policy.go IsTransitUpload]; for
    the others Cloudreve's pages send them straight to the storage [docs: use/policy/compare.md]. Local: PUT
    /file/upload {path, size, name, policy_id} makes an upload session and a placeholder, with the folders above
    [source: service/explorer/upload.go, pkg/filesystem/upload.go CreateUploadSession, hooks.go GenericAfterUpload;
    frontend: uploader/base.ts]; the file goes in chunks of its chunkSize (25 MB in the default policy; 0: all of it
    at once) POSTed in order to /file/upload/<session>/<index> with their exact length, and the last one makes the file
    [source: upload.go LocalUpload, processChunkUpload; models/migration.go]. A failed upload's session is deleted
    (DELETE /file/upload/<session>), and with it the placeholder [source: upload.go Delete].
    The others (a slave node, OneDrive, OSS, COS, S3, Qiniu, Upyun) go through Cloudreve itself, as its text editor's
    saves do [docs: use/policy/compare.md]: POST /file/create {path} makes an empty file, and PUT /file/update/<id>
    with the whole file stores it through the policy [source: service/explorer/file.go Create, PutContent]. The new
    file is listed empty until then.
    Cloudreve refuses an upload onto a file's name (40004) [source: hooks.go GenericAfterUpload]. A file is replaced
    by uploading the new one next to it under a hidden spare name, with its extension (a storage policy may take only
    some [source: pkg/filesystem/validator.go]), then deleting the old one and renaming the new one: a failed upload
    leaves the old file as it was. The replaced file's Cloudreve shares go with it. The policy's size limit, in the
    listing, is checked first. A web server in front of Cloudreve must take requests as large as a chunk (local
    policy) or the whole file (the others) [docs: getting-started/install.md, client_max_body_size]: else it answers
    413, an error here.

Names
    Cloudreve refuses \\ / : * ? " < > | in names, names of 256 bytes or more, and names ending in a space (it drops
    the spaces from new folders' names) [source: pkg/filesystem/validator.go, manage.go CreateDirectory]: those are
    refused here first.

Limits
    Not tried against a real Cloudreve server: the tests use a fake built from the source above.
"""

from __future__ import annotations

import json
import logging
import secrets
import urllib.parse
from collections.abc import Callable, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    KeyedLocks,
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
)
# As v4's. Not its _code: a code of -1 (CodeNotSet) comes with a message from v3, an empty one from v4.
from .cloudreve import _address, _dict

SERVICE = gettext_lazy("Cloudreve")
API = "/api/v3"
COOKIE = "cloudreve-session"
SPARE = ".hfile-upload-"  # the start of the name of an upload that is to replace a file
ANSWER_LIMIT = 1024 * 1024  # bytes of a JSON answer read instead of a download

# Answer codes (pkg/serializer/error.go). 203 is a two-factor login, and a delete done in part.
OK, PARTLY, SIGNED_OUT = 0, 203, 401
EXISTS, TOO_LARGE, WRONG_TYPE, NO_SPACE, BAD_NAME, UPLOADING = 40004, 40049, 40050, 40051, 40052, 40054
GONE = (404, 40016, 40044)  # not found; no such path or object; no such file
NOT_ALLOWED = (40006, 40007)  # the storage policy, the user group can't
WRONG_LOGIN = 40020
CAPTCHA = (40026, 40027)
BLOCKED = (40017, 40018)  # banned, not activated
SERVER = 50000  # codes from here on are Cloudreve's own failures

_log = logging.getLogger("hfile.storage.cloudreve-v3")


def _code(answer: Mapping[str, Any]) -> int | None:
    code = answer.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _message(answer: Mapping[str, Any]) -> str:
    return short(answer.get("msg") or "")


def _kind(item: Mapping[str, Any]) -> str | None:
    """"dir" or "file" for an object of a listing; None for anything else."""
    kind = item.get("type")
    return kind if kind in ("dir", "file") else None


def _src(item: Mapping[str, Any]) -> dict[str, list[str]]:
    """The {"dirs", "items"} naming one object in a call: Cloudreve takes folders' and files' ids apart."""
    folder = _kind(item) == "dir"
    return {"dirs": [item["id"]] if folder else [], "items": [] if folder else [item["id"]]}


def _session_cookie(response: httpx.Response) -> str | None:
    """The value of the session cookie an answer sets, or None."""
    for header in response.headers.get_list("set-cookie"):
        name, _, value = header.split(";", 1)[0].partition("=")
        if name.strip() == COOKIE and value.strip():
            return value.strip()
    return None


def _check_new(path: str) -> None:
    """InvalidPath for a new name ending in a space: Cloudreve refuses it for files and drops the spaces for folders."""
    if any(name.endswith(" ") for name in paths.segments(path)):
        raise InvalidPath(gettext("Names in %(service)s can't end with a space.") % {"service": SERVICE})


def _spare(name: str) -> str:
    """A hidden name for an upload that is to replace the file `name`, ending in its extension as Cloudreve sees it
    (from the last dot, Go's filepath.Ext), which a storage policy may limit."""
    spare = SPARE + secrets.token_hex(6)
    extension = name[name.rfind("."):] if "." in name else ""
    return spare + extension if len((spare + extension).encode("utf-8")) <= 255 else spare


def _content_refusal(response: httpx.Response) -> dict[str, Any] | None:
    """Cloudreve's JSON answer instead of a file from its own download route (a refused link), or None when the response
    is the file: Cloudreve sends a file with Content-Disposition, and its errors as JSON with HTTP 200 and without."""
    headers = response.headers
    if (response.status_code != 200 or "json" not in headers.get("content-type", "").lower()
            or "content-disposition" in headers or API + "/" not in response.url.path):
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
    if not isinstance(answer, dict) or _code(answer) in (None, OK):
        raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
    return answer


class CloudreveV3Driver(StorageDriver):
    type = "cloudreve-v3"
    label = gettext_lazy("Cloudreve v3")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://cloud.example.com",
              help=gettext_lazy("The address of the Cloudreve site, as it is opened in a browser. Cloudreve 3.5 or a "
                                "later 3.x version; for Cloudreve v4 choose the type Cloudreve.")),
        Param("email", gettext_lazy("Email"),
              help=gettext_lazy('The email address of a Cloudreve account. HFile logs in as Cloudreve\'s login page does, '
                                'which works only while its "Captcha for logins" is off (Dashboard → Settings → Public '
                                'Access), and not for accounts with two-factor authentication.')),
        Param("password", gettext_lazy("Password"), kind="password", secret=True),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the account's own space that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # Cloudreve's temporary links, see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    reusable_links = False  # one may work once only
    forbidden_chars = '\\:*?"<>|'
    max_name_bytes = 255

    # Tuning; tests make it small.
    session_lifetime: ClassVar[float] = 30 * 86400.0  # seconds Cloudreve takes its session cookie after the login

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = _address(self.config.get("url"))
        self._base = paths.clean(self.config.get("base_path") or "/")
        # No cookie jar: the session cookie is kept by TokenCache and sent to the API only.
        self._http = http.client(cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])))
        self._session = TokenCache(self._log_in)
        self._names = KeyedLocks()  # Cloudreve's paths of names being given to something

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
            errors["url"] = gettext("The address should start with http:// or https://, like https://cloud.example.com.")
        email = str(config.get("email") or "").strip()
        if email and "@" not in email:
            errors["email"] = gettext("Cloudreve logs in with the account's email address.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        return errors

    # -- login

    def _log_in(self) -> tuple[str, float]:
        """TokenCache's refresh: the session cookie of a new login with the email and password."""
        email, password = str(self.config.get("email") or "").strip(), str(self.config.get("password") or "")
        response = self._send(self._http.build_request("POST", self._url("/user/session"),
                                                       json={"userName": email, "Password": password}))
        answer = self._answer(response)
        code, message = _code(answer), _message(answer)
        if code == OK:
            cookie = _session_cookie(response)
            if cookie is None:
                raise BackendError(gettext("Cloudreve answered the login without a session cookie."))
            _log.debug("logged in")
            return cookie, self.session_lifetime
        if code == PARTLY:
            raise AuthError(gettext("This Cloudreve account uses two-factor authentication, which HFile can't log in "
                                    "with (Cloudreve asks for a one-time code). Please use an account without it."))
        if code in CAPTCHA:
            raise AuthError(gettext('Cloudreve asks for a captcha to log in. Its administrator must turn off "Captcha '
                                    'for logins" in Dashboard → Settings → Public Access for HFile to log in.'))
        if code == WRONG_LOGIN:
            raise AuthError(gettext("Cloudreve refused the email or password."))
        if code in BLOCKED:
            raise AuthError(gettext("Cloudreve says the account is blocked or not activated yet: %(message)s") % {"message": message})
        if code is not None and code >= SERVER:
            raise self._failure(answer)
        raise AuthError(gettext("Cloudreve refused the login: %(message)s (%(code)s)") % {"message": message, "code": code})

    # -- requests

    def _url(self, route: str) -> str:
        return self._root + API + route

    def _names_of(self, path: str) -> list[str]:
        """The names of Cloudreve's path of a storage path."""
        return paths.segments(self._base) + paths.segments(path)

    def _dir(self, path: str) -> str:
        """Cloudreve's path of a storage path, as request bodies have it."""
        return "/" + "/".join(self._names_of(path))

    def _send(self, request: httpx.Request, *, stream: bool = False, follow_redirects: bool = False) -> httpx.Response:
        try:
            return self._http.send(request, stream=stream, follow_redirects=follow_redirects)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _answer(self, response: httpx.Response, missing: str | None = None) -> dict[str, Any]:
        """The JSON answer of a call; a DriverError when there is none. `missing`: the message for an HTTP 404."""
        status = response.status_code
        if 300 <= status < 400:
            raise redirected(response, SERVICE)
        answer = json_or_none(response)
        if isinstance(answer, dict) and _code(answer) is not None:
            return answer
        if status == 404:
            raise DriverError(missing or gettext("The address has no Cloudreve v3 API (it answered HTTP 404). Cloudreve v4 "
                                                 "sites answer so: for them, choose the type Cloudreve. Otherwise please "
                                                 "check the address."))
        if status == 413:
            raise DriverError(gettext("Cloudreve's web server refused an upload request as too large (HTTP 413). Its "
                                      "administrator must raise the web server's limit on request size "
                                      "(client_max_body_size in nginx) above the chunk size of the local storage policy, "
                                      "or above the largest file for the other storage policies."))
        if status >= 500:
            raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                               % {"service": SERVICE, "status_code": status})
        raise BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of a "
                                   "Cloudreve v3 site?") % {"service": SERVICE})

    def _call(self, method: str, route: str, *, body: Any = None, content: Any = None,
              headers: Mapping[str, str] | None = None, missing: str | None = None) -> Any:
        """One call of the API, logged in: its data when its code is 0, else a DriverError (_failure). A call Cloudreve
        answers as not logged in is sent again once after a new login, unless its body is a stream."""
        for attempt in range(2):
            cookie = self._session.get()
            request = self._http.build_request(method, self._url(route), json=body, content=content,
                                               headers={"Cookie": f"{COOKIE}={cookie}", **(headers or {})})
            answer = self._answer(self._send(request), missing)
            if _code(answer) == OK:
                return answer.get("data")
            if _code(answer) == SIGNED_OUT:
                self._session.invalidate(cookie)  # Cloudreve forgot the session: the next call logs in again
                if not attempt and content is None:
                    continue
            raise self._failure(answer)
        raise AssertionError("unreachable")

    @staticmethod
    def _failure(answer: Mapping[str, Any]) -> DriverError:
        code, message = _code(answer), _message(answer)
        if code in GONE:
            return NotFound()
        if code == EXISTS:
            return AlreadyExists()
        if code == SIGNED_OUT:
            return AuthError(gettext("Cloudreve says HFile isn't logged in (%(message)s). Please try again; if it keeps "
                                     "happening, check the storage source's settings.") % {"message": message})
        if code == BAD_NAME:
            return InvalidPath(gettext("Cloudreve doesn't accept this name: %(message)s") % {"message": message})
        if code == NO_SPACE:
            return DriverError(gettext("%(service)s is out of space.") % {"service": SERVICE})
        if code == TOO_LARGE:
            return DriverError(gettext("The file is larger than Cloudreve's storage policy allows."))
        if code == WRONG_TYPE:
            return DriverError(gettext("Cloudreve's storage policy doesn't take files of this type."))
        if code == UPLOADING:
            return DriverError(gettext("Cloudreve is already receiving an upload of a file with this name. Please try "
                                       "again later."))
        if code in NOT_ALLOWED:
            return DriverError(gettext("Cloudreve doesn't let this account do this: %(message)s") % {"message": message})
        if code == PARTLY:
            return DriverError(gettext("Cloudreve did only part of this: %(message)s") % {"message": message})
        if code is not None and code >= SERVER:
            return BackendError(gettext("Cloudreve failed: %(message)s (%(code)s). Please try again later.")
                                % {"message": message, "code": code})
        return DriverError(gettext("Cloudreve refused this: %(message)s (%(code)s)") % {"message": message, "code": code})

    # -- paths

    def _listing(self, names: list[str]) -> dict[str, Any]:
        """Cloudreve's listing of the folder at its path of `names`. NotFound when no folder is there."""
        return _dict(self._call("GET", "/directory/" + "/".join(urllib.parse.quote(name, safe="") for name in names)))

    def _folder(self, path: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """What is in the folder at `path`, by name, and the account's storage policy. NotFound when no folder is there
        (Cloudreve answers so for a file too); for the root, an error about the base path."""
        try:
            data = self._listing(self._names_of(path))
        except NotFound:
            if path == paths.ROOT:
                raise self._base_error() from None
            raise
        objects = data.get("objects")
        if not isinstance(objects, list):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        children: dict[str, dict[str, Any]] = {}
        for item in objects:
            if (isinstance(item, dict) and _kind(item) is not None and usable_name(item.get("name"))
                    and isinstance(item.get("id"), str) and item["id"]):
                children.setdefault(item["name"], item)  # a folder and a file of one name: the folder, listed first
        return children, _dict(data.get("policy"))

    def _base_error(self) -> DriverError:
        """Why Cloudreve has no folder at the base path: a file is there, or nothing is."""
        names = paths.segments(self._base)
        if names:
            try:
                objects = self._listing(names[:-1]).get("objects")
            except NotFound:
                objects = None
            if isinstance(objects, list) and any(isinstance(item, dict) and item.get("name") == names[-1]
                                                 and _kind(item) == "file" for item in objects):
                return NotADirectory(gettext("The base path %(base)s is a file, not a folder.") % {"base": self._base})
        return NotFound(gettext("The base path %(base)s doesn't exist in %(service)s.") % {"base": self._base, "service": SERVICE})

    def _lookup(self, path: str) -> dict[str, Any]:
        """The object at `path` (not the root), from its folder's listing. NotFound."""
        folder, name = paths.split(path)
        item = self._folder(folder)[0].get(name)
        if item is None:
            raise NotFound()
        return item

    def _file(self, path: str) -> dict[str, Any]:
        """The object of the file at `path`. NotFound, a folder too."""
        item = self._lookup(path) if path != paths.ROOT else None
        if item is None or _kind(item) != "file":
            raise NotFound()
        return item

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        is_dir = _kind(item) == "dir"
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                     modified=parse_time(item.get("date")))

    # -- files

    def list(self, path: str) -> list[Entry]:
        try:
            children, _ = self._folder(path)
        except NotFound:
            # Cloudreve answers so for a file too; _lookup raises NotFound itself when nothing is there.
            if path != paths.ROOT and _kind(self._lookup(path)) == "file":
                raise NotADirectory() from None
            raise
        return [self._entry(paths.join(path, name), item) for name, item in children.items()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._folder(paths.ROOT)
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._lookup(path))

    def _link(self, item: Mapping[str, Any]) -> str:
        """A new download link of the file `item`; the local policy's come without the address."""
        url = self._call("PUT", "/file/download/" + urllib.parse.quote(item["id"], safe=""))
        if isinstance(url, str) and url.startswith("/"):
            url = self._root + url
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        return url

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(path)
        total, modified = as_int(item.get("size")), parse_time(item.get("date"))
        if length == 0 or total == 0:
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        # The link is its own permission: no cookie goes with it, nor where it redirects.
        response = self._send(self._http.build_request("GET", self._link(item), headers=headers), stream=True,
                              follow_redirects=True)
        refusal = _content_refusal(response)
        if refusal is not None:
            raise DriverError(gettext("Cloudreve refused this: %(message)s (%(code)s)")
                              % {"message": _message(refusal), "code": _code(refusal)})
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=modified)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        return self._link(self._file(path))

    def _prepare(self, folder: str) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        """_folder() of the folder an upload goes into, made first (with the ones above) when it is missing."""
        try:
            return self._folder(folder)
        except NotFound:
            pass
        try:
            self._call("PUT", "/directory", body={"path": self._dir(folder)})
        except AlreadyExists:  # what Cloudreve answers when a file is where a folder should be
            raise NotADirectory() from None
        return self._folder(folder)

    def _upload_chunks(self, folder: str, name: str, data: BinaryIO, size: int, policy: Mapping[str, Any]) -> None:
        """The file to the local storage policy, through an upload session: in chunks of its size (0: all of it in one),
        in order; the last one makes the file."""
        policy_id = policy.get("id")
        if not isinstance(policy_id, str) or not policy_id:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        session = _dict(self._call(
            "PUT", "/file/upload", body={"path": self._dir(folder), "size": size, "name": name, "policy_id": policy_id},
            missing=gettext("This Cloudreve site has no upload sessions (it answered HTTP 404): they came with Cloudreve "
                            "3.5, and HFile uploads to Cloudreve 3.5 or later only.")))
        session_id = session.get("sessionID")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError(gettext("%(service)s gave no upload session.") % {"service": SERVICE})
        chunk = as_int(session.get("chunkSize")) or 0
        count = -(-size // chunk) if chunk > 0 and size > 0 else 1
        route = f"/file/upload/{urllib.parse.quote(session_id, safe='')}/"
        try:
            for index in range(count):
                length = min(chunk, size - index * chunk) if chunk > 0 else size
                self._call("POST", route + str(index), content=exact_body(data, length),
                           headers={"Content-Type": "application/octet-stream", "Content-Length": str(length)})
        except BaseException:
            try:  # the session goes, and with it the placeholder
                self._call("DELETE", f"/file/upload/{urllib.parse.quote(session_id, safe='')}")
            except DriverError as error:
                _log.warning("couldn't delete Cloudreve's upload session for %s: %s", name, error.message)
            raise

    def _remove(self, item: Mapping[str, Any]) -> None:
        """Deletes a file or folder for good: Cloudreve v3 has no trash bin."""
        self._call("DELETE", "/object", body={**_src(item), "force": False, "unlink": False})

    def _discard(self, item: Mapping[str, Any]) -> None:
        """Deletes what a failed upload made; a failure is only logged."""
        try:
            self._remove(item)
        except DriverError as error:
            _log.warning("couldn't delete %s after a failed upload: %s", item.get("name"), error.message)

    def _upload_through(self, folder: str, name: str, data: BinaryIO, size: int) -> None:
        """The file through Cloudreve itself, for a storage policy whose uploads would go straight to its storage: an
        empty file, then its content in one request, which Cloudreve stores through the policy."""
        self._call("POST", "/file/create", body={"path": self._dir(paths.join(folder, name))})
        if size == 0:
            return
        made = self._folder(folder)[0].get(name)
        if made is None or _kind(made) != "file":
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the "
                                       "file is there.") % {"service": SERVICE})
        try:
            self._call("PUT", "/file/update/" + urllib.parse.quote(made["id"], safe=""), content=exact_body(data, size),
                       headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})
        except BaseException:
            self._discard(made)
            raise

    def _swap(self, folder: str, name: str, spare: str, old: Mapping[str, Any]) -> None:
        """The file just uploaded as `spare` takes the place of `old`, the file `name`: the old one goes, the new one is
        renamed. A failure before leaves the old one; the new one goes again."""
        new = self._folder(folder)[0].get(spare)
        if new is None or _kind(new) != "file":
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the "
                                       "file is there.") % {"service": SERVICE})
        try:
            self._remove(old)
        except BaseException:
            self._discard(new)
            raise
        try:
            self._call("POST", "/object/rename", body={"src": _src(new), "new_name": name})
        except DriverError as error:
            _log.warning("uploaded %s as %s but couldn't rename it: %s", name, spare, error.message)
            raise DriverError(gettext('The upload stopped halfway: the old file was deleted, and the new one is called '
                                      '"%(name)s" now. Please rename it.') % {"name": spare}) from None

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        _check_new(path)
        folder, name = paths.split(path)
        with self._names.hold(self._dir(path)):
            children, policy = self._prepare(folder)
            old = children.get(name)
            if old is not None and _kind(old) == "dir":
                raise AlreadyExists()
            limit = as_int(policy.get("max_size")) or 0
            if limit and size > limit:
                raise DriverError(gettext("The file is larger than Cloudreve's storage policy allows."))
            target = name if old is None else _spare(name)
            if policy.get("type") == "local":
                self._upload_chunks(folder, target, data, size, policy)
            else:
                self._upload_through(folder, target, data, size)
            if old is not None:
                self._swap(folder, name, target, old)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        _check_new(path)
        folder, name = paths.split(path)
        with self._names.hold(self._dir(path)):
            try:
                taken = name in self._folder(folder)[0]
            except NotFound:  # Cloudreve makes the folders above too
                taken = False
            if taken:  # Cloudreve would keep a folder there without an error
                raise AlreadyExists()
            self._call("PUT", "/directory", body={"path": self._dir(path)})

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._remove(self._lookup(path))

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        folder, name = paths.split(path)
        if new_name == name:
            self._lookup(path)  # NotFound when nothing is there
            return
        _check_new(new_name)
        with self._names.hold(self._dir(paths.join(folder, new_name))):
            children, _ = self._folder(folder)
            item = children.get(name)
            if item is None:
                raise NotFound()
            if new_name in children:
                raise AlreadyExists()
            self._call("POST", "/object/rename", body={"src": _src(item), "new_name": new_name})

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        source, name = paths.split(path)
        if not copy and source == folder:
            self._lookup(path)  # NotFound when nothing is there
            return  # already there
        item = self._lookup(path)
        with self._names.hold(self._dir(paths.join(folder, name))):
            if name in self._folder(folder)[0]:
                raise AlreadyExists()
            body = {"src_dir": self._dir(source), "src": _src(item), "dst": self._dir(folder)}
            if copy:
                self._call("POST", "/object/copy", body=body)
            else:
                self._call("PATCH", "/object", body=body)

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)
