"""Cloudreve v4 (https://cloudreve.org, https://github.com/cloudreve/cloudreve), a self-hosted cloud drive: the files of
one Cloudreve account, through Cloudreve's API. Documentation: https://cloudrevev4.apifox.cn (each endpoint as
markdown, listed in its llms.txt) and https://docs.cloudreve.org/en/api/ (overview, auth, file-uri, upload). Where they
are silent, the facts are from Cloudreve's source in that repository, its v4 code on master (as of release 4.19.1;
the paths below are in it). Each fact is marked [docs] or [source].

Paths
    Cloudreve addresses files by URI: cloudreve://my/<path> is <path> in the account's own files [docs: api/file-uri].
    The storage root is cloudreve://my/<base_path>. A URI is parsed as a URL, so each name is percent-encoded as
    Cloudreve's own PathEscape does it: everything but A-Z a-z 0-9 - _ . ! ~ * ' ( ) [docs; source:
    pkg/filemanager/fs/uri.go]. Names are unique in a folder (a database constraint) [source: inventory/file.go], so
    paths need no ids or caches here.

    Listings: GET /file with uri, page and page_size [docs]; Cloudreve pages by cursor by default (next_token until
    there is none), or by offset (page and total_items), and caps page_size at its max_page_size (2000) [docs; source:
    inventory/file_utils.go, inventory/setting.go]. One file or folder: GET /file/info [docs]. A missing path answers
    40016, and so does listing a file [source: pkg/filemanager/fs/dbfs/navigator.go]. GET /file/info refuses the
    account's root with 403 "Not supported action" [source: service/explorer/file.go GetFileInfoService,
    dbfs.WithNotRoot; dbfs/dbfs.go Get]: with the base path "/", a listing of one item tells that the root is there.

    An upload makes its file at once, with the metadata sys:upload_session_id until it ends [source: dbfs/upload.go
    PrepareUpload, CompleteUpload]. A new file is then a placeholder of the upload's size whose only entity is the one
    being uploaded [source: inventory/file.go CreateFile]: it is left out of listings and not found until the upload
    ends (one GET /file/info with extended=true tells it apart, for such files only). A file being replaced keeps its
    last version until then [source: inventory/file.go UpgradePlaceholder], and stays.

Login
    POST /session/token with the email and password gives an access token and a refresh token, with when each expires
    [docs]; calls send "Authorization: Bearer <access token>" [docs: api/auth]. Both tokens are kept in memory only.
    When they expire is Cloudreve's time, measured against its answer's Date header: a clock that is off doesn't
    matter. Before the access token expires (an hour by default, access_token_ttl [source: inventory/setting.go]) POST
    /session/token/refresh gives a new pair [docs]; when that is refused (40020) or the refresh token has expired too
    (two weeks by default), HFile logs in again. Cloudreve takes an unknown or expired access token for no login at all
    and answers 401 where the account's files are needed, in a batch call inside aggregated_error [source:
    pkg/auth/jwt.go, pkg/filemanager/fs/dbfs/my_navigator.go]: then the token is renewed and the call sent again, once.
    The login works only while Cloudreve's "Captcha for logins" (Dashboard → Settings → Captcha) is off: it answers
    40026 otherwise [source: middleware/captcha.go, routers/router.go]. An account with two-factor authentication
    answers 203 with a session for its one-time code [docs]; the API has no other way for it (its OAuth sign-in needs a
    browser [docs: api/auth]), so such accounts can't be used. Cloudreve's session cookie isn't kept.

Requests
    Answers: {"code": 0, "data": ..., "msg": ""} with HTTP 200; another code is an error [docs: api/overview, which
    lists them]. A batch call (delete, move, copy, download links) whose one item fails answers 40081 with the item's
    own error in aggregated_error [source: pkg/serializer/error.go]. A failure that isn't one of Cloudreve's own errors
    (a database's, a storage's) answers code -1 without a message [source: pkg/serializer/error.go Err]: the error says
    that Cloudreve gave no reason, with the answer's correlation_id, under which its log has it.

Versions
    Cloudreve v4 only: its API is under /api/v4 [docs: api/overview]. A Cloudreve v3 site has /api/v3 only and answers
    every /api/v4 path with HTTP 404 and gin's text "404 page not found" [source: 3.8.3's routers/router.go,
    middleware/frontend.go]; that answer gives an error saying that this type needs Cloudreve v4.

Changes
    mkdir: POST /file/create, type folder, with err_on_conflict [docs; source: service/explorer/file.go], which makes
    the folders above too [docs]. rename: POST /file/rename [docs]. move and copy: POST /file/move with copy [docs];
    Cloudreve answers 40004 for a name taken in the target [source: pkg/filemanager/fs/dbfs/manage.go]. delete: DELETE
    /file without skip_soft_delete, which puts things into the account's trash bin (回收站); Cloudreve keeps them there
    as long as its user group's setting says [docs; source: manage.go SoftDelete, manager/operation.go]. Something
    Cloudreve has locked (open in an online editor, being uploaded, locked by WebDAV) answers 40073 [docs]; it isn't
    unlocked here.

Downloads
    POST /file/url gives a temporary URL that needs no token [docs], signed by Cloudreve: its own
    /api/v4/file/content/... (with Range), or the storage policy's storage (a presigned S3 URL, ...) [source:
    pkg/filemanager/manager/entitysource/entitysource.go]. It lasts about Cloudreve's "File Blob URL TTL" (Dashboard →
    File System → Settings; an hour by default; a storage may cap it), and Cloudreve gives the same one again until 10
    minutes before it expires; the API takes no expiry, so download_url() can't honour `expires` [source:
    service/explorer/file.go, manager/entity.go, inventory/setting.go]. With download=true the file comes as an
    attachment [docs], with Content-Disposition [source]. download_url() is that URL; open() fetches it with the
    Range, following redirects. Links are reused for at most `link_ttl` seconds per file and version
    (primary_entity), while they have LINK_MARGIN seconds left; the site hands them out again no longer
    (timed_download_url()). Cloudreve's own content route answers an error as JSON with HTTP 200 and no
    Content-Disposition [source: middleware/auth.go SignRequired, routers/controllers/file.go]: that is an error
    here. The links use Cloudreve's Site URL (Dashboard → Settings → Basic), which browsers must be able to open.
    This server may not reach it (an internal address in docker): open() fetches a link of Cloudreve's own content
    route at the configured address instead, whose signature covers the path only [source:
    pkg/cluster/routes/routes.go MasterFileContentUrl, pkg/auth/auth.go getUrlSignContent].

Uploads
    PUT /file/upload makes an upload session [docs] with entity_type "version", which replaces a file of that name
    (Cloudreve refuses the name otherwise) [docs], keeping old versions only where the account's settings say
    [source: dbfs/upload.go]; it makes the folders above, and a placeholder file until the upload ends [source:
    dbfs/upload.go, manage.go Create]. Its answer names the storage policy. The local policy, and others with
    "Upload relay" on, take the file in chunks of chunk_size bytes (0, as with relay: all of it at once) POSTed in
    order to /file/upload/<session>/<index> with their exact length; the last one ends the upload [docs; source:
    service/explorer/upload.go, manager/upload.go]. The other policies (S3, OSS, COS, OBS, KS3, OneDrive, Qiniu, Upyun,
    a slave node) have the client send the chunks to their storage, each its own way [docs: api/upload]. For them the
    session is deleted again and the file goes to PUT /file/content [docs], which Cloudreve stores itself whatever the
    policy, up to its "Max size of editable document files" (maxEditSize, 50 MB by default) [source:
    service/explorer/file.go, manager/upload.go Update]; larger files are refused with a message asking for Upload
    relay. A failed upload's session is deleted [docs], with the placeholder.

Names
    Cloudreve refuses \\ / : * ? " < > | in names, and names of 256 bytes or more [source:
    pkg/filemanager/fs/dbfs/validator.go].

Limits
    Not tried against a real Cloudreve server: the tests use a fake built from the documentation and the source above.
"""

from __future__ import annotations

import datetime
import email.utils
import json
import logging
import time
import urllib.parse
from collections.abc import Callable, Mapping
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

SERVICE = gettext_lazy("Cloudreve")
API = "/api/v4"
CONTENT = API + "/file/content/"  # Cloudreve's own route of the files' data, which its links may be
SAFE = "-_.!~*'()"  # what Cloudreve's PathEscape leaves as it is, besides letters and digits
LINK_MARGIN = 60.0  # seconds a reused download link has left at least
ANSWER_LIMIT = 1024 * 1024  # bytes of a JSON answer read instead of a download
UPLOADING = "sys:upload_session_id"  # the metadata of a file while an upload to it is going on

# Answer codes (pkg/serializer/error.go). 403 is also "Not supported action" (below a file, the root), so it gets
# Cloudreve's own message.
OK, TWO_FACTOR, SIGNED_OUT = 0, 203, 401
EXISTS, TOO_LARGE, NO_SPACE, BAD_NAME, LOCKED, BATCH = 40004, 40049, 40051, 40052, 40073, 40081
GONE = (404, 40016, 40044, 40077, 40078)  # not found, no such path, file, blob; in the trash bin
NOT_ALLOWED = (40007, 40082)  # the user group can't; owner only
CAPTCHA = (40026, 40027)
WRONG_LOGIN = (40069, 40080)
BLOCKED = (40017, 40018)  # banned, not activated
SERVER = 50000  # codes from here on are Cloudreve's own failures

_log = logging.getLogger("hfile.storage.cloudreve")


def _address(value: Any) -> str:
    """The Cloudreve site's address without a query, a fragment or the "/" at the end."""
    return str(value or "").strip().split("#", 1)[0].split("?", 1)[0].rstrip("/")


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _code(answer: Mapping[str, Any]) -> int:
    code = answer.get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else -1


def _message(answer: Mapping[str, Any]) -> str:
    return short(answer.get("msg") or "")


def _unexplained(answer: Mapping[str, Any]) -> bool:
    """Cloudreve's answer to a failure it has no code for: serializer.Err of an error that isn't one of its own (a
    database's, a storage's) is code -1 without a message; its log has the reason under the correlation_id."""
    code = answer.get("code")
    return code == -1 and isinstance(code, int) and not isinstance(code, bool)


def _unexplained_failure(answer: Mapping[str, Any]) -> BackendError:
    """The error of an _unexplained() answer."""
    message, correlation = _message(answer), short(answer.get("correlation_id") or "", 64)
    if message:
        return BackendError(gettext("Cloudreve failed: %(message)s (%(code)s). Please try again later.")
                            % {"message": message, "code": -1})
    if correlation:
        return BackendError(gettext("Cloudreve failed without giving a reason (its log has it under the correlation ID "
                                    "%(id)s). Please try again later.") % {"id": correlation})
    return BackendError(gettext("Cloudreve failed without giving a reason (its log has it). Please try again later."))


def _item_error(answer: Mapping[str, Any]) -> dict[str, Any] | None:
    """The error of a batch call's (one) failed item, from aggregated_error."""
    errors = answer.get("aggregated_error")
    if _code(answer) != BATCH or not isinstance(errors, dict):
        return None
    return next((value for value in errors.values()
                 if isinstance(value, dict) and (_code(value) != -1 or _unexplained(value))), None)


def _signed_out(answer: Mapping[str, Any]) -> bool:
    """Cloudreve's answer that the call isn't logged in: the token is unknown or expired."""
    inner = _item_error(answer)
    return _code(answer) == SIGNED_OUT or (inner is not None and _code(inner) == SIGNED_OUT)


def _kind(item: Mapping[str, Any]) -> str | None:
    """"dir" or "file" for a FileResponse's type (1, 0); None for anything else."""
    value = item.get("type")
    if isinstance(value, bool) or value not in (0, 1):
        return None
    return "dir" if value == 1 else "file"


def _left(moment: datetime.datetime | None, now: datetime.datetime | None = None) -> float | None:
    """Seconds from `now` (by default our clock's) until `moment`; None when it is unknown."""
    if moment is None:
        return None
    return (moment - (now or datetime.datetime.now(datetime.timezone.utc))).total_seconds()


def _server_time(response: httpx.Response) -> datetime.datetime | None:
    """When Cloudreve sent its answer, by its own clock (the Date header); None when it doesn't say."""
    try:
        moment = email.utils.parsedate_to_datetime(response.headers.get("date", ""))
    except (TypeError, ValueError, IndexError):
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=datetime.timezone.utc)


def _content_refusal(response: httpx.Response) -> dict[str, Any] | None:
    """Cloudreve's JSON answer instead of a file from its own content route (a refused link), or None when the
    response is the file: Cloudreve sends a download=true file with Content-Disposition, and its errors as JSON with
    HTTP 200 and without."""
    headers = response.headers
    if (response.status_code != 200 or "json" not in headers.get("content-type", "").lower()
            or "content-disposition" in headers or API not in response.url.path):
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
    if not isinstance(answer, dict) or (_code(answer) in (-1, OK) and not _unexplained(answer)):
        raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
    return answer


class CloudreveDriver(StorageDriver):
    type = "cloudreve"
    label = gettext_lazy("Cloudreve")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://cloud.example.com",
              help=gettext_lazy("The address of the Cloudreve site, as it is opened in a browser. Cloudreve v4 only: for "
                                "Cloudreve v3, choose the type Cloudreve v3.")),
        Param("email", gettext_lazy("Email"),
              help=gettext_lazy('The email address of a Cloudreve account. HFile logs in as Cloudreve\'s login page does, '
                                'which works only while its "Captcha for logins" is off (Dashboard → Settings → '
                                'Captcha), and not for accounts with two-factor authentication.')),
        Param("password", gettext_lazy("Password"), kind="password", secret=True),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the account's own space that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # Cloudreve's signed links, see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    forbidden_chars = '\\:*?"<>|'
    max_name_bytes = 255

    # Tuning; tests make them small.
    page_size: ClassVar[int] = 1000
    max_pages: ClassVar[int] = 10_000
    token_lifetime: ClassVar[float] = 3600.0  # when Cloudreve doesn't say: its default access_token_ttl
    link_ttl: ClassVar[float] = 300.0  # seconds a download link is reused at most

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root = _address(self.config.get("url"))
        self._base = paths.clean(self.config.get("base_path") or "/")
        # No cookie is kept: Cloudreve's session cookie isn't needed, the token is the only credential.
        self._http = http.client(cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])))
        self._tokens = TokenCache(self._renew)
        self._refresh: tuple[str, float] | None = None  # (refresh token, time.monotonic() it expires); TokenCache's lock
        self._links = TTLCache(1024, self.link_ttl)  # (path, primary entity) -> (url, time.monotonic() it is good until)

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

    def _renew(self) -> tuple[str, float]:
        """TokenCache's refresh: a new access token from the refresh token while it lasts, else from a new login."""
        if self._refresh is not None and time.monotonic() < self._refresh[1]:
            answer, sent = self._post("/session/token/refresh", {"refresh_token": self._refresh[0]})
            if _code(answer) == OK:
                return self._keep(_dict(answer.get("data")), sent)
            if _code(answer) >= SERVER:
                raise self._failure(answer)
            _log.debug("refresh token refused (%s); logging in again", _code(answer))  # password changed, signed out...
            self._refresh = None
        return self._log_in()

    def _log_in(self) -> tuple[str, float]:
        email, password = str(self.config.get("email") or "").strip(), str(self.config.get("password") or "")
        answer, sent = self._post("/session/token", {"email": email, "password": password})
        code, message = _code(answer), _message(answer)
        if code == OK:
            _log.debug("logged in")
            return self._keep(_dict(_dict(answer.get("data")).get("token")), sent)
        if code == TWO_FACTOR:
            raise AuthError(gettext("This Cloudreve account uses two-factor authentication, which HFile can't log in "
                                    "with (Cloudreve asks for a one-time code). Please use an account without it."))
        if code in CAPTCHA:
            raise AuthError(gettext('Cloudreve asks for a captcha to log in. Its administrator must turn off "Captcha '
                                    'for logins" in Dashboard → Settings → Captcha for HFile to log in.'))
        if code in WRONG_LOGIN:
            raise AuthError(gettext("Cloudreve refused the email or password."))
        if code in BLOCKED:
            raise AuthError(gettext("Cloudreve says the account is blocked or not activated yet: %(message)s") % {"message": message})
        if code >= SERVER:
            raise self._failure(answer)
        raise AuthError(gettext("Cloudreve refused the login: %(message)s (%(code)s)") % {"message": message, "code": code})

    def _keep(self, token: Mapping[str, Any], sent: datetime.datetime | None) -> tuple[str, float]:
        """The access token of a login's or refresh's token pair and its lifetime; the refresh token is kept. Their ends
        are Cloudreve's times: measured from when it `sent` the answer by its own clock (else by ours)."""
        access, refresh = token.get("access_token"), token.get("refresh_token")
        if not isinstance(access, str) or not access:
            raise BackendError(gettext("%(service)s gave no token for the login.") % {"service": SERVICE})
        refresh_left = _left(parse_time(token.get("refresh_expires")), sent)
        if isinstance(refresh, str) and refresh and refresh_left is not None and refresh_left > 0:
            self._refresh = (refresh, time.monotonic() + refresh_left)
        else:
            self._refresh = None
        lifetime = _left(parse_time(token.get("access_expires")), sent)
        return access, lifetime if lifetime is not None and lifetime > 0 else self.token_lifetime

    # -- requests

    def _url(self, route: str) -> str:
        return self._root + API + route

    def _uri(self, path: str) -> str:
        """The Cloudreve URI of a storage path."""
        names = paths.segments(self._base) + paths.segments(path)
        return "cloudreve://my" + "".join("/" + urllib.parse.quote(name, safe=SAFE) for name in names)

    def _send(self, request: httpx.Request, *, stream: bool = False, follow_redirects: bool = False) -> httpx.Response:
        try:
            return self._http.send(request, stream=stream, follow_redirects=follow_redirects)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _answer(self, response: httpx.Response) -> dict[str, Any]:
        """The JSON answer of a call; a DriverError when there is none."""
        status = response.status_code
        if 300 <= status < 400:
            raise redirected(response, SERVICE)
        answer = json_or_none(response)
        if isinstance(answer, dict) and _unexplained(answer):
            raise _unexplained_failure(answer)
        if isinstance(answer, dict) and _code(answer) != -1:
            return answer
        if status == 404:
            raise DriverError(gettext("The address has no Cloudreve v4 API (it answered HTTP 404). A Cloudreve v3 site "
                                      "answers so: for one, choose the type Cloudreve v3. Please check the address."))
        if status == 413:
            raise DriverError(gettext("Cloudreve's web server refused an upload request as too large (HTTP 413). Its "
                                      "administrator must raise the web server's limit on request size above the "
                                      "storage policy's chunk size, or above the largest file when the policy relays "
                                      "uploads."))
        if status >= 500:
            raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                               % {"service": SERVICE, "status_code": status})
        raise BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of a "
                                   "Cloudreve v4 site?") % {"service": SERVICE})

    def _post(self, route: str, body: Mapping[str, Any]) -> tuple[dict[str, Any], datetime.datetime | None]:
        """A call without the token (the login, the refresh): its whole answer, and when Cloudreve sent it by its
        clock."""
        response = self._send(self._http.build_request("POST", self._url(route), json=dict(body)))
        return self._answer(response), _server_time(response)

    def _call(self, method: str, route: str, *, params: Mapping[str, Any] | None = None, body: Any = None,
              content: Any = None, headers: Mapping[str, str] | None = None,
              failure: Callable[[Mapping[str, Any]], DriverError] | None = None) -> Any:
        """One call of the API, logged in: its data when its code is 0; else `failure` (_failure) of its answer.
        A call Cloudreve answers as not logged in is sent again once with a new token, unless its body is a stream."""
        for attempt in range(2):
            token = self._tokens.get()
            request = self._http.build_request(method, self._url(route), params=params, json=body, content=content,
                                               headers={"Authorization": f"Bearer {token}", **(headers or {})})
            answer = self._answer(self._send(request))
            if _code(answer) == OK:
                return answer.get("data")
            if _signed_out(answer) and not attempt and content is None:
                self._tokens.invalidate(token)  # expired early, or Cloudreve's secret changed
                continue
            raise (failure or self._failure)(answer)
        raise AssertionError("unreachable")

    @staticmethod
    def _failure(answer: Mapping[str, Any]) -> DriverError:
        inner = _item_error(answer)
        if inner is not None:
            return CloudreveDriver._failure(inner)
        if _unexplained(answer):
            return _unexplained_failure(answer)
        code, message = _code(answer), _message(answer)
        if code in GONE:
            return NotFound()
        if code == EXISTS:
            return AlreadyExists()
        if code == SIGNED_OUT:
            return AuthError(gettext("Cloudreve still says HFile isn't logged in, right after its login (%(message)s). "
                                     "Please check the storage source's settings.") % {"message": message})
        if code == BAD_NAME:
            return InvalidPath(gettext("Cloudreve doesn't accept this name: %(message)s") % {"message": message})
        if code == NO_SPACE:
            return DriverError(gettext("%(service)s is out of space.") % {"service": SERVICE})
        if code == TOO_LARGE:
            return DriverError(gettext("The file is larger than Cloudreve's storage policy allows."))
        if code == LOCKED:
            return DriverError(gettext("Cloudreve has locked the file or folder: it is open in an online editor, being "
                                       "uploaded, or locked by a WebDAV client. Please try again later."))
        if code in NOT_ALLOWED:
            return DriverError(gettext("Cloudreve doesn't let this account do this: %(message)s") % {"message": message})
        if code >= SERVER:
            return BackendError(gettext("Cloudreve failed: %(message)s (%(code)s). Please try again later.")
                                % {"message": message, "code": code})
        return DriverError(gettext("Cloudreve refused this: %(message)s (%(code)s)") % {"message": message, "code": code})

    # -- paths

    def _base_missing(self) -> NotFound:
        return NotFound(gettext("The base path %(base)s doesn't exist in %(service)s.") % {"base": self._base, "service": SERVICE})

    def _unfinished(self, path: str, item: Mapping[str, Any]) -> bool:
        """Whether `item`, the file at `path` as a listing or GET /file/info gave it, is a new file whose upload hasn't
        ended: while an upload is going on its file has the metadata sys:upload_session_id, and a new one has no entity
        but the one being uploaded; a file being replaced keeps its last version (see the module's docstring)."""
        if _kind(item) != "file" or UPLOADING not in _dict(item.get("metadata")):
            return False
        try:
            info = _dict(self._call("GET", "/file/info", params={"uri": self._uri(path), "extended": "true"}))
        except NotFound:
            return True  # given up meanwhile
        entities = _dict(info.get("extended_info")).get("entities")
        return isinstance(entities, list) and len(entities) <= 1

    def _info(self, path: str) -> dict[str, Any]:
        """What Cloudreve says of the file or folder at `path`. NotFound, a new file still being uploaded too."""
        try:
            if path == paths.ROOT and self._base == paths.ROOT:
                # The account's root, which GET /file/info refuses: a listing of one item says that it is there.
                self._call("GET", "/file", params={"uri": self._uri(path), "page_size": 1})
                return {"type": 1, "name": ""}
            info = self._call("GET", "/file/info", params={"uri": self._uri(path)})
        except NotFound:
            if path == paths.ROOT:
                raise self._base_missing() from None
            raise
        if not isinstance(info, dict) or _kind(info) is None:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        if path == paths.ROOT and _kind(info) != "dir":
            raise NotADirectory(gettext("The base path %(base)s is a file, not a folder.") % {"base": self._base})
        if path != paths.ROOT and self._unfinished(path, info):
            raise NotFound()
        return info

    def _file(self, path: str) -> dict[str, Any]:
        """The info of the file at `path`. NotFound, a folder too."""
        info = self._info(path) if path != paths.ROOT else None
        if info is None or _kind(info) != "file":
            raise NotFound()
        return info

    def _children(self, path: str) -> list[dict[str, Any]]:
        """Everything in the folder at `path`, all pages, but new files still being uploaded. NotFound also when it is
        a file."""
        found: dict[str, dict[str, Any]] = {}  # by name: a page may repeat what the one before had
        params: dict[str, Any] = {"uri": self._uri(path), "page": 0, "page_size": self.page_size}
        for _ in range(self.max_pages):
            data = _dict(self._call("GET", "/file", params=params))
            files = data.get("files")
            if not isinstance(files, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            for item in files:
                if isinstance(item, dict) and _kind(item) is not None and usable_name(item.get("name")):
                    found[item["name"]] = item
            pagination = _dict(data.get("pagination"))
            if pagination.get("is_cursor") is True:
                token = pagination.get("next_token")
                if not isinstance(token, str) or not token:
                    break
                params = {**params, "next_page_token": token}
            else:
                page = (as_int(pagination.get("page")) or 0) + 1
                size = as_int(pagination.get("page_size")) or len(files)
                if not files or page * size >= (as_int(pagination.get("total_items")) or 0):
                    break
                params = {**params, "page": page}
        else:
            raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})
        return [item for name, item in found.items() if not self._unfinished(paths.join(path, name), item)]

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        is_dir = _kind(item) == "dir"
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                     modified=parse_time(item.get("updated_at")))

    # -- files

    def list(self, path: str) -> list[Entry]:
        try:
            items = self._children(path)
        except NotFound:
            # Cloudreve answers so for a file too. _info raises NotFound itself when nothing is there (for the base
            # path, saying so), NotADirectory when the base path is a file.
            if _kind(self._info(path)) == "file":
                raise NotADirectory() from None
            raise
        return [self._entry(paths.join(path, item["name"]), item) for item in items]

    def stat(self, path: str) -> Entry:
        info = self._info(path)
        if path == paths.ROOT:
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, info)

    def _link(self, path: str, info: Mapping[str, Any]) -> tuple[str, tuple[str, str], float]:
        """A download link of the file at `path`, whose info is `info`, its key in the cache of links, and for how
        many more seconds it is reused (0: not): for the same version of the file while it has LINK_MARGIN seconds
        left, link_ttl at most."""
        key = (path, str(info.get("primary_entity") or ""))
        cached = self._links.get(key)
        now = time.monotonic()
        if cached is not None and now < cached[1]:
            return cached[0], key, cached[1] - now
        data = _dict(self._call("POST", "/file/url", body={"uris": [self._uri(path)], "download": True}))
        urls = data.get("urls")
        url = _dict(urls[0]).get("url") if isinstance(urls, list) and urls else None
        if not isinstance(url, str) or not url.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        left = _left(parse_time(data.get("expires")))
        reused = min(left - LINK_MARGIN, self.link_ttl) if left is not None and left > LINK_MARGIN else 0.0
        if reused:
            self._links.put(key, (url, time.monotonic() + reused))
        return url, key, reused

    def _here(self, url: str) -> str:
        """A link of Cloudreve's own content route at the configured address: Cloudreve makes it with its Site URL,
        which this server may not reach (see the module's docstring). Other links as they are."""
        try:
            link = httpx.URL(url)
        except httpx.InvalidURL:
            return url
        if not link.path.startswith(CONTENT) or url.startswith(self._root + CONTENT):
            return url
        return self._root + link.raw_path.decode("ascii")  # the path and query, as Cloudreve signed and wrote them

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        info = self._file(path)
        total, modified = as_int(info.get("size")), parse_time(info.get("updated_at"))
        if length == 0 or total == 0:  # a file made empty in Cloudreve has no blob to fetch
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=modified)
        url, key, _reused = self._link(path, info)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        # The link is its own permission: no token goes with it, nor where it redirects.
        response = self._send(self._http.build_request("GET", self._here(url), headers=headers), stream=True,
                              follow_redirects=True)
        refusal = _content_refusal(response)
        if refusal is not None or response.status_code not in (200, 206):
            self._links.pop(key)
            if refusal is not None:
                raise self._failure(refusal)
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=modified)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        return self.timed_download_url(path, filename=filename, expires=expires, user_agent=user_agent)[0]

    def timed_download_url(self, path: str, *, filename: str, expires: int = 3600,
                           user_agent: str | None = None) -> tuple[str | None, float | None]:
        info = self._file(path)
        if as_int(info.get("size")) == 0:
            return None, None  # nothing to fetch, and maybe no blob to link to: the site sends it
        url, _key, reused = self._link(path, info)
        return url, reused  # as long as this driver reuses it: Cloudreve's "File Blob URL TTL" decides

    def _chunks(self, session_id: str, data: BinaryIO, size: int, chunk: int) -> None:
        """The file to Cloudreve in chunks of `chunk` bytes (0: all of it in one); the last one ends the upload."""
        count = -(-size // chunk) if chunk > 0 and size > 0 else 1
        route = f"/file/upload/{urllib.parse.quote(session_id, safe='')}/"
        for index in range(count):
            length = min(chunk, size - index * chunk) if chunk > 0 else size
            self._call("POST", route + str(index), content=exact_body(data, length),
                       headers={"Content-Type": "application/octet-stream", "Content-Length": str(length)})

    def _cancel(self, session_id: str, uri: str) -> None:
        """Deletes an upload session, and with it the placeholder of a new file; a failure is only logged."""
        try:
            self._call("DELETE", "/file/upload", body={"id": session_id, "uri": uri})
        except DriverError as error:
            _log.warning("couldn't delete Cloudreve's upload session for %s: %s", uri, error.message)

    def _put_content(self, uri: str, data: BinaryIO, size: int, policy: Mapping[str, Any]) -> None:
        """The whole file in one request, which Cloudreve stores itself whatever the storage policy."""

        def failure(answer: Mapping[str, Any]) -> DriverError:
            if _code(answer) != TOO_LARGE:
                return self._failure(answer)
            # Not the policy's own limit, which the upload session passed: Cloudreve's limit on this way in.
            return DriverError(gettext('This file is too large for HFile to upload to Cloudreve: its storage policy '
                                       '(%(policy)s) has files sent straight to its own storage, which HFile doesn\'t do, '
                                       'and Cloudreve takes them in itself only up to its "Max size of editable document '
                                       'files" (Dashboard → File System → Settings). Its administrator can turn on '
                                       '"Upload relay" in the storage policy\'s settings.')
                               % {"policy": short(policy.get("type") or "?", 20)})

        self._call("PUT", "/file/content", params={"uri": uri}, content=exact_body(data, size), failure=failure,
                   headers={"Content-Type": "application/octet-stream", "Content-Length": str(size)})

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        uri = self._uri(path)
        session = _dict(self._call("PUT", "/file/upload", body={"uri": uri, "size": size, "entity_type": "version"}))
        session_id = session.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError(gettext("%(service)s gave no upload session.") % {"service": SERVICE})
        policy = _dict(session.get("storage_policy"))
        if policy.get("type") != "local" and policy.get("relay") is not True:
            # The chunks would go to the policy's own storage, each kind its own way: Cloudreve stores the file instead.
            self._cancel(session_id, uri)
            self._put_content(uri, data, size, policy)
            return
        try:
            self._chunks(session_id, data, size, as_int(session.get("chunk_size")) or 0)
        except BaseException:
            self._cancel(session_id, uri)
            raise

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._call("POST", "/file/create", body={"uri": self._uri(path), "type": "folder", "err_on_conflict": True})

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        # Into the account's trash bin, see the module's docstring.
        self._call("DELETE", "/file", body={"uris": [self._uri(path)], "unlink": False, "skip_soft_delete": False})

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        if new_name == paths.name(path):
            self._info(path)  # NotFound when nothing is there
            return
        self._call("POST", "/file/rename", body={"uri": self._uri(path), "new_name": new_name})

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        if not copy and paths.parent(path) == folder:
            self._info(path)  # NotFound when nothing is there
            return  # already there
        self._call("POST", "/file/move", body={"uris": [self._uri(path)], "dst": self._uri(folder), "copy": copy})

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)
