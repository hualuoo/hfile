"""Dropbox (API v2) as a storage source. Official docs:
https://www.dropbox.com/developers/documentation/http/documentation (served now from
https://docs.dropboxapi.com/dropbox-api/) and the OAuth guide https://developers.dropbox.com/oauth-guide. Pages
used: docs/technical-reference/request-response-formats, json-encoding and path-root-header-modes,
docs/error-handling, docs/team-files, docs/performance, docs/get-started/authorization, and the references of the
routes below (api-reference/user-endpoints/files/...).

Paths
    Dropbox addresses files by path, and so does this driver: the storage path /a/b is the Dropbox path
    <base_path>/a/b, and the account's root is "" (the API's name for it). Paths ignore case, as Dropbox does;
    names keep the case they were given. Dropbox makes the missing folders above a new file or folder itself;
    moves and copies into a folder that doesn't exist are refused here first, since Dropbox would make that
    folder too.

    Team spaces: an account whose root namespace isn't its home namespace (a member of a team with a team
    space; users/get_current_account says so, asked once) sends Dropbox-API-Path-Root
    {".tag": "root", "root": <root namespace>} with every call, so that its paths are those of the Dropbox
    website: the team space at the top, the member's own folder as /<their name> in it. Other accounts send no
    such header. When Dropbox answers that the root changed (422 invalid_root: the account joined or left a
    team), the account is asked again and the call repeated.

Login
    Access tokens last four hours: one comes from the refresh token when needed (TokenCache), and again when
    Dropbox answers 401 expired_access_token. Dropbox keeps the refresh token (a refresh answers with an access
    token only), so nothing is saved after a refresh.

Params
    app_key, app_secret   the admin's own app (Dropbox App Console), with this site's OAuth callback as a
                          redirect URI and the permissions files.metadata.read, files.content.read and
                          files.content.write (and account_info.read, which every app has);
    refresh_token         filled in by "授权" (token_access_type=offline);
    base_path             the folder shown as the root.

Requests
    RPC routes (api.dropboxapi.com) take their argument as JSON in the body; content routes
    (content.dropboxapi.com: downloads and uploads) take it in the Dropbox-API-Arg header, which must be ASCII:
    everything else, 0x7F too, is escaped as \\uXXXX (json.dumps does that). A throttled call (429, or 409
    too_many_write_operations) is sent again after a Retry-After of at most `throttle_wait` seconds where the
    request can be sent again (not a streamed upload from data that can't seek back), twice at most; then
    BackendError.

    Other calls: list_folder and list_folder/continue (cursor paging), get_metadata, create_folder_v2, delete_v2
    (to Dropbox's deleted files, which it keeps for a while), move_v2 and copy_v2 (autorename false: a taken name
    is AlreadyExists), search_v2 and search/continue_v2.

Changes
    A rename that only changes the case of letters goes through a third name: move_v2 doesn't do it.

Downloads
    open(): /files/download with a Range header. download_url(): /files/get_temporary_link, a link that needs
    no token and lasts four hours, whatever `expires` asks. The API can't name the download (get_temporary_link
    takes the path only), so `filename` isn't used. An account without a verified email address gets no links:
    then None, and the site sends the file.

Uploads
    write(): files up to `single_upload_limit` (150 MiB, /files/upload's limit) in one /files/upload, streamed
    from `data`; larger ones through an upload session: start, append_v2 in pieces of `upload_chunk` (8 MiB;
    Dropbox advises multiples of 4 MiB, at most 150 MiB a request), finish. A piece stays in memory until
    Dropbox has it: one whose answer was lost is sent again, and an incorrect_offset answer then tells whether
    the first one got there. Both replace a file at the path (mode overwrite; a folder there is a conflict) and
    never rename (autorename false). A /files/upload whose answer is lost (a broken connection, a 5xx) is sent
    once more when `data` can seek: for identical contents Dropbox writes nothing (WriteMode). A session takes
    350 GB at most (too_large).
    upload_target(): /files/get_temporary_upload_link, a one-time link the browser POSTs the file to (Content-Type
    application/octet-stream), so up to 150 MiB. The link carries no token, and content.dropboxapi.com allows any
    origin (its preflight answers Access-Control-Allow-Origin: *, seen 2026-11-17), so no CORS setup is needed.

Names
    Dropbox refuses / and \\ only, which the site refuses already; < > : " | ? * are allowed (they sync badly to
    Windows). A path can't end with whitespace.

Limits
    Files that can't be downloaded as they are (Google Docs and the like, which Dropbox only exports) are left out
    of listings and search. Search is Dropbox's search of names, which matches words rather than any part of a
    name and lags a little behind changes; its results are kept to names containing the keyword below the folder,
    from `search_pages` pages of `search_page` results at most.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext as _, gettext_lazy

from . import http, paths
from ._cloud import (
    TokenCache,
    as_int,
    exact_body,
    json_or_none,
    origin,
    parse_time,
    position,
    range_header,
    read_exact,
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
    UploadTarget,
    rename_through,
)

SERVICE = "Dropbox"

# Tags at the top of an upload session's errors (UploadSessionLookupError; finish wraps it in lookup_failed).
_SESSION_ERRORS = frozenset({"lookup_failed", "not_found", "incorrect_offset", "closed", "not_closed", "too_large"})


def _api_arg(arg: Any) -> str:
    """JSON for the Dropbox-API-Arg (or Dropbox-API-Path-Root) header: ASCII only, everything else escaped as
    \\uXXXX, 0x7F included, as Dropbox asks (json.dumps' ensure_ascii does exactly that)."""
    return json.dumps(arg, ensure_ascii=True, separators=(",", ":"))


def _tags(response: httpx.Response) -> list[str]:
    """The union tags of an error answer, from its error_summary: "path/conflict/folder/.." gives
    ["path", "conflict", "folder"]. Dropbox appends a random number of dots, and asks to match by prefix."""
    answer = json_or_none(response)
    summary = answer.get("error_summary") if isinstance(answer, dict) else None
    if not isinstance(summary, str):
        return []
    return [part for part in summary.split("/") if part.strip(".")]


def _throttled(status: int, tags: list[str]) -> bool:
    return status == 429 or (status == 409 and "too_many_write_operations" in tags)


def _error_value(answer: Mapping[str, Any], key: str) -> Any:
    """A field of the error's innermost union value, e.g. incorrect_offset's correct_offset."""
    node = answer.get("error")
    while isinstance(node, dict):
        if key in node:
            return node[key]
        tag = node.get(".tag")
        node = node.get(tag) if isinstance(tag, str) else None
    return None


def _user_message(answer: Mapping[str, Any]) -> str:
    message = answer.get("user_message")
    return short(message.get("text")) if isinstance(message, dict) else ""


def _usable(metadata: Any) -> bool:
    """A file or folder HFile shows: a name that can be a path segment, and for a file one Dropbox can download as
    it is (not a Google Docs-type file, which only /files/export gives)."""
    if not isinstance(metadata, dict) or metadata.get(".tag") not in ("file", "folder"):
        return False
    if not usable_name(metadata.get("name")):
        return False
    return metadata[".tag"] == "folder" or metadata.get("is_downloadable", True) is not False


def _below(display: Any, top: list[str]) -> list[str] | None:
    """The names of a Dropbox path (path_display) below the folder whose names are `top`, comparing them as
    Dropbox does, without regard to case; None when it isn't below that folder."""
    if not isinstance(display, str):
        return None
    parts = paths.segments(display)
    if len(parts) <= len(top) or any(a.lower() != b.lower() for a, b in zip(parts, top)):
        return None
    return parts[len(top):]


class _Unanswered(BackendError):
    """No usable answer: the connection failed, or Dropbox failed (5xx). The request may have been done."""


class _WrongOffset(BackendError):
    """An upload session holds another number of bytes than the request said (incorrect_offset)."""

    def __init__(self, correct_offset: Any):
        super().__init__(_("Dropbox's upload session is at another offset than expected. Please upload again."))
        self.correct_offset = as_int(correct_offset)


class _EmailNotVerified(DriverError):
    """get_temporary_link refused: the account's email address isn't verified."""


class DropboxDriver(StorageDriver):
    type = "dropbox"
    label = gettext_lazy("Dropbox")
    params = (
        Param("app_key", gettext_lazy("App key"),
              help=gettext_lazy("Create an app in the Dropbox App Console (www.dropbox.com/developers/apps) with "
                                "\"Full Dropbox\" access (\"App folder\" limits the storage to the app's own "
                                "folder). On its \"Permissions\" tab turn on files.metadata.read, "
                                "files.content.read and files.content.write and submit; on its \"Settings\" tab "
                                "add this site's authorization callback URL to \"Redirect URIs\" (Dropbox takes "
                                "https:// addresses only, or localhost). Enter the \"App key\" from the "
                                "\"Settings\" tab here.")),
        Param("app_secret", gettext_lazy("App secret"), kind="password", secret=True,
              help=gettext_lazy("The \"App secret\" from the same \"Settings\" tab (click \"Show\").")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy("After saving the settings above, click \"Authorize\", log in to the Dropbox "
                                "account and allow access: it's filled in for you. Disconnecting the app in the "
                                "Dropbox account's settings ends it; just authorize again.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in Dropbox that is this storage source's root, like / or /folder1. It "
                                "must already exist. On a team with a team space, paths start at the team space, "
                                "as on the Dropbox website: your own folder is /your name.")),
    )

    can_range = True
    can_direct_download = True
    can_direct_upload = True
    can_move = True
    can_copy = True
    can_search = True
    has_folders = True
    forbidden_chars = ""  # Dropbox refuses only / and \, which the site refuses in every name already

    oauth = True
    setup_params = ("app_key", "app_secret")
    auth_params = ("refresh_token",)

    # Endpoints (tests point them at a fake).
    api_root: ClassVar[str] = "https://api.dropboxapi.com"
    content_root: ClassVar[str] = "https://content.dropboxapi.com"
    authorize_endpoint: ClassVar[str] = "https://www.dropbox.com/oauth2/authorize"

    #: Entries per list_folder page (Dropbox takes 1 to 2000).
    page_size: ClassVar[int] = 2000
    #: Bytes up to which write() uses one /files/upload (its limit: 150 MiB); above it an upload session.
    single_upload_limit: ClassVar[int] = 150 * 1024 * 1024
    #: Bytes per upload session request: a multiple of 4 MiB, at most 150 MiB.
    upload_chunk: ClassVar[int] = 8 * 1024 * 1024
    #: Seconds a temporary upload link given to the browser stays valid (Dropbox: 60 to 14400).
    upload_link_ttl: ClassVar[int] = 3600
    #: Results per search page (at most 1000), and pages read at most.
    search_page: ClassVar[int] = 200
    search_pages: ClassVar[int] = 10
    #: Longest Retry-After waited for before sending a throttled request again.
    throttle_wait: ClassVar[float] = 10.0
    #: Seconds before sending an upload piece again, doubling; and the wait when Dropbox gives no Retry-After.
    retry_delay: ClassVar[float] = 0.5

    def __init__(self, config: Mapping[str, Any], *, on_update=None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._http = http.client()
        self._tokens = TokenCache(self._refresh)
        self._root_lock = threading.Lock()
        self._root_known = False
        self._root_header: str | None = None

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = error.message
        if any(ch.isspace() for ch in str(config.get("app_key") or "")):
            errors["app_key"] = _("The app key can't contain spaces.")
        return errors

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        app_key = str(config.get("app_key") or "").strip()
        if not app_key:
            raise DriverError(_("Fill in the app key first."))
        query = urllib.parse.urlencode({
            "client_id": app_key,
            "response_type": "code",
            "token_access_type": "offline",  # a refresh token
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return f"{cls.authorize_endpoint}?{query}"

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        with http.client() as client:
            answer = cls._token_request(client, config, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            })
        refresh_token = answer.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AuthError(_("Dropbox gave no refresh token. Please authorize again."))
        return {"refresh_token": refresh_token}

    # -- login

    @classmethod
    def _token_request(cls, client: httpx.Client, config: Mapping[str, Any], fields: dict[str, str]) -> dict[str, Any]:
        """POST to Dropbox's token endpoint with the app's key and secret; the answer has an access token."""
        app_key = str(config.get("app_key") or "").strip()
        app_secret = str(config.get("app_secret") or "")
        if not app_key or not app_secret:
            raise AuthError(_("Fill in the app key and app secret first."))
        try:
            response = client.post(f"{cls.api_root}/oauth2/token",
                                   data={"client_id": app_key, "client_secret": app_secret, **fields})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from error
        answer = json_or_none(response)
        if response.status_code == 429 or response.status_code >= 500:
            raise BackendError(_("Dropbox's authorization service is unavailable for now (HTTP %(status_code)s). "
                                 "Please try again later.") % {"status_code": response.status_code})
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        if response.status_code != 200 or not isinstance(answer, dict):
            # {"error": "invalid_grant", "error_description": "refresh token is invalid or revoked"}, or the
            # description after a colon: {"error": "invalid_client: Invalid client_id or client_secret"}.
            error = str(answer.get("error") or "") if isinstance(answer, dict) else ""
            code, _colon, text = error.partition(":")
            code = code.strip() or f"HTTP {response.status_code}"
            if code == "invalid_client":
                raise AuthError(_("Dropbox doesn't accept this app key or app secret (invalid_client). Please check "
                                  "them in the storage source's settings."))
            if code == "invalid_grant":
                raise AuthError(_("Dropbox's authorization has expired or been revoked (invalid_grant). Please "
                                  "authorize again in the storage source's settings."))
            description = answer.get("error_description") if isinstance(answer, dict) else None
            detail = short(description or text, 160)
            if detail:
                raise AuthError(_("Dropbox refused the authorization (%(code)s): %(detail)s") % {"code": code, "detail": detail})
            raise AuthError(_("Dropbox refused the authorization (%(code)s).") % {"code": code})
        token = answer.get("access_token")
        if not isinstance(token, str) or not token:
            raise BackendError(_("Dropbox's authorization service returned no access token."))
        return answer

    def _refresh(self) -> tuple[str, float]:
        refresh_token = str(self.config.get("refresh_token") or "").strip()
        if not refresh_token:
            raise AuthError(_("Not authorized yet: click \"Authorize\" in the storage source's settings and log in "
                              "to the Dropbox account."))
        self.log.debug("refreshing the access token")
        answer = self._token_request(self._http, self.config,
                                     {"grant_type": "refresh_token", "refresh_token": refresh_token})
        return answer["access_token"], float(as_int(answer.get("expires_in")) or 14400)

    # -- requests

    def _path_root(self) -> str | None:
        """The Dropbox-API-Path-Root header this account needs: None, unless its root namespace isn't its home
        namespace (a team space), whose root it then names."""
        with self._root_lock:
            if not self._root_known:
                info = self._rpc("/2/users/get_current_account", rooted=False).get("root_info")
                root = info.get("root_namespace_id") if isinstance(info, dict) else None
                if not isinstance(root, str) or not root:
                    raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": SERVICE})
                self._root_header = None if root == info.get("home_namespace_id") else _api_arg({".tag": "root", "root": root})
                self._root_known = True
            return self._root_header

    def _send(self, url: str, *, payload: Any = None, arg: Any = None,
              content: bytes | Callable[[], Iterable[bytes]] | None = None, length: int | None = None,
              headers: Mapping[str, str] | None = None, stream: bool = False, rooted: bool = True,
              repeatable: bool = True) -> httpx.Response:
        """One call with the access token (and the path root, when `rooted`). RPC routes get `payload` as JSON body;
        content routes get `arg` in the Dropbox-API-Arg header and `content` as the body: bytes, or a function
        giving the chunks of a body of `length` bytes, called for each try. Sent again, when `repeatable`, after
        an expired token, a changed root or a short throttle. An answer below 300 is returned (stream=True: its
        body unread); any other raises."""
        attempt = 0
        while True:
            attempt += 1
            token = self._tokens.get()
            all_headers = {"Authorization": f"Bearer {token}"}
            root = self._path_root() if rooted else None
            if root is not None:
                all_headers["Dropbox-API-Path-Root"] = root
            if arg is not None:
                all_headers["Dropbox-API-Arg"] = _api_arg(arg)
            body = content() if callable(content) else content
            if body is not None:
                all_headers["Content-Type"] = "application/octet-stream"
                if length is not None:
                    all_headers["Content-Length"] = str(length)
            all_headers.update(headers or {})
            try:
                request = self._http.build_request("POST", url, json=payload, content=body, headers=all_headers)
                response = self._http.send(request, stream=stream)
            except httpx.HTTPError as error:
                raise _Unanswered(transport_error(error, SERVICE).message) from error
            status = response.status_code
            if status < 300:
                return response
            if stream:
                response = http.read_error(response)  # the error's message
            if status < 400:  # the API makes no redirects (STOR-15)
                raise redirected(response, SERVICE)
            tags = _tags(response)
            if status == 401 and tags[:1] == ["expired_access_token"]:
                self._tokens.invalidate(token)
                if repeatable and attempt == 1:
                    continue
            elif status == 422 and tags[:1] == ["invalid_root"]:
                with self._root_lock:
                    self._root_known = False
                if repeatable and attempt < 3:
                    continue
            elif _throttled(status, tags) and repeatable and attempt < 3:
                wait = retry_after(response, default=self.retry_delay, longest=self.throttle_wait)
                if wait is not None:
                    self.log.info("Dropbox throttled a call, waiting %.1f s", wait)
                    time.sleep(wait)
                    continue
            raise self._error(response, tags)

    def _rpc(self, route: str, arg: Mapping[str, Any] | None = None, *, rooted: bool = True) -> dict[str, Any]:
        """An RPC call (api.dropboxapi.com); its JSON answer."""
        return self._answer(self._send(self.api_root + route, payload=arg, rooted=rooted))

    def _upload(self, route: str, arg: Mapping[str, Any], body: bytes) -> dict[str, Any]:
        """A content-upload call (content.dropboxapi.com) with a body in memory; its JSON answer."""
        return self._answer(self._send(self.content_root + route, arg=arg, content=body))

    @staticmethod
    def _answer(response: httpx.Response) -> dict[str, Any]:
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": SERVICE})
        return answer

    @staticmethod
    def _error(response: httpx.Response, tags: list[str]) -> DriverError:
        """The DriverError for an error answer, by its status and the tags of its error_summary."""
        status = response.status_code
        answer = json_or_none(response)
        answer = answer if isinstance(answer, dict) else {}
        code = "/".join(tags) or f"HTTP {status}"
        detail = _(" (%(code)s)") % {"code": code}
        if status == 401:
            if tags[:1] == ["expired_access_token"]:
                return BackendError(_("Dropbox's access token expired during the request. Please try again."))
            if tags[:1] == ["missing_scope"]:
                scope = short(_error_value(answer, "required_scope"), 60) or code
                return AuthError(_("The Dropbox app lacks the permission %(scope)s: turn it on in the \"Permissions\" "
                                   "tab of the app in the App Console, then authorize again.") % {"scope": scope})
            return AuthError(_("%(service)s doesn't accept the current authorization%(detail)s. Please authorize "
                               "again in the storage source's settings.") % {"service": SERVICE, "detail": detail})
        if _throttled(status, tags):
            if "too_many_write_operations" in tags:
                return BackendError(_("Dropbox is busy with too many changes at once. Please try again later."))
            return BackendError(_("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": SERVICE})
        if status >= 500:
            return _Unanswered(_("%(service)s failed for now%(detail)s. Please try again later.") % {
                "service": SERVICE, "detail": _(" (HTTP %(status)s)") % {"status": status}})
        if tags and tags[0] in _SESSION_ERRORS:
            if "incorrect_offset" in tags:
                return _WrongOffset(_error_value(answer, "correct_offset"))
            if "too_large" in tags:
                return DriverError(_("The file is too large: Dropbox takes files of at most 350 GB."))
            if "not_found" in tags:
                return BackendError(_("The %(service)s upload session has expired. Please upload again.") % {"service": SERVICE})
            return BackendError(_("Dropbox's upload session failed (%(code)s). Please upload again.") % {"code": code})
        if tags[:1] == ["invalid_root"]:
            return BackendError(_("The Dropbox account's root folder has changed (it joined or left a team). Please "
                                  "try again."))
        if "not_found" in tags or "not_file" in tags:  # not_file: a folder where a file was asked for
            return NotFound()
        if "not_folder" in tags:
            return NotADirectory()
        if "file_ancestor" in tags:
            return NotADirectory(_("Dropbox has a file where a folder of this path should be."))
        if "conflict" in tags:
            return AlreadyExists()
        if "malformed_path" in tags or "disallowed_name" in tags:
            return InvalidPath(_("Dropbox doesn't accept this name or path (%(code)s).") % {"code": code})
        if "insufficient_space" in tags or "insufficient_quota" in tags:
            return DriverError(_("%(service)s is out of space.") % {"service": SERVICE})
        if "no_write_permission" in tags:
            return DriverError(_("The Dropbox account has no permission to change this (%(code)s).") % {"code": code})
        if "restricted_content" in tags:
            return DriverError(_("Dropbox doesn't allow transferring this file: its content is restricted (after a "
                                 "copyright claim, say)."))
        if "unsupported_file" in tags or "unsupported_content_type" in tags:
            return DriverError(_("This file can't be downloaded from Dropbox as it is (a Google Docs file, say)."))
        if "email_not_verified" in tags:
            return _EmailNotVerified(_("Dropbox gives download links only to accounts with a verified email address."))
        if "locked" in tags:
            return DriverError(_("The file is locked in Dropbox. Please try again later."))
        if "too_many_files" in tags:  # a move or copy of more than 10,000 items; a delete of too many
            return DriverError(_("Dropbox can't do this with so many files and folders at once (a move or copy "
                                 "takes at most 10,000)."))
        if "internal_error" in tags:  # a move, copy or search that failed on Dropbox's side
            return BackendError(_("%(service)s failed for now%(detail)s. Please try again later.") % {
                "service": SERVICE, "detail": detail})
        message = _user_message(answer) or ("" if tags else short(response.text))
        if message:
            return DriverError(_("%(service)s refused the request%(detail)s: %(message)s") % {
                "service": SERVICE, "detail": detail, "message": message})
        return DriverError(_("%(service)s refused the request%(detail)s.") % {"service": SERVICE, "detail": detail})

    # -- paths and entries

    def _full(self, path: str) -> str:
        """The Dropbox path of a storage path; "" for the account's root."""
        full = paths.rebase(path, self._base)
        return "" if full == paths.ROOT else full

    @staticmethod
    def _entry(metadata: Mapping[str, Any], path: str) -> Entry:
        is_dir = metadata.get(".tag") == "folder"
        return Entry(name=paths.name(path), path=path, is_dir=is_dir,
                     size=None if is_dir else as_int(metadata.get("size")),
                     modified=None if is_dir else parse_time(metadata.get("server_modified")))

    def _check_new_name(self, name: str) -> None:
        paths.check_name(name, forbidden=self.forbidden_chars, max_bytes=self.max_name_bytes)
        if name != name.rstrip():
            raise InvalidPath(_("Names in Dropbox can't end with a space."))

    @staticmethod
    def _commit(full: str) -> dict[str, Any]:
        """Where an upload goes: replacing a file there, never under another name."""
        return {"path": full, "mode": "overwrite", "autorename": False}

    # -- reading

    def check(self) -> None:
        try:
            root = self.stat(paths.ROOT)
        except NotFound:
            raise NotFound(_("The base path %(base)s doesn't exist in %(service)s.") % {"base": self._base, "service": SERVICE}) from None
        if not root.is_dir:
            raise NotADirectory(_("The base path %(base)s is a file, not a folder.") % {"base": self._base})
        self.list(paths.ROOT)

    def list(self, path: str) -> list[Entry]:
        answer = self._rpc("/2/files/list_folder", {"path": self._full(path), "limit": self.page_size})
        entries: list[Entry] = []
        cursors: set[str] = set()
        while True:
            for metadata in answer.get("entries") or []:
                if _usable(metadata):
                    entries.append(self._entry(metadata, paths.join(path, metadata["name"])))
            if answer.get("has_more") is not True:
                return entries
            cursor = answer.get("cursor")
            if not isinstance(cursor, str) or not cursor:
                raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": SERVICE})
            if cursor in cursors:
                raise BackendError(_("Dropbox repeated a listing cursor. Please try again later."))
            cursors.add(cursor)
            answer = self._rpc("/2/files/list_folder/continue", {"cursor": cursor})

    def stat(self, path: str) -> Entry:
        full = self._full(path)
        if full == "":  # get_metadata doesn't take the account's root
            return Entry(name="", path=paths.ROOT, is_dir=True)
        metadata = self._rpc("/2/files/get_metadata", {"path": full})
        if not _usable(metadata):
            raise NotFound()
        return self._entry(metadata, path)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        full = self._full(path)
        if full == "":
            raise NotFound()
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, None)
        if span:
            headers["Range"] = span
        response = self._send(self.content_root + "/2/files/download", arg={"path": full}, headers=headers,
                              stream=True)
        try:  # the file's metadata, as ASCII JSON
            result = json.loads(response.headers.get("dropbox-api-result") or "null")
        except ValueError:
            result = None
        result = result if isinstance(result, dict) else {}
        rev = result.get("rev")
        return response_stream(response, offset=offset, length=length, total=as_int(result.get("size")),
                               service=SERVICE, etag=rev if isinstance(rev, str) else None,
                               modified=parse_time(result.get("server_modified")))

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        full = self._full(path)
        if full == "":
            raise NotFound()
        try:
            answer = self._rpc("/2/files/get_temporary_link", {"path": full})
        except _EmailNotVerified:
            return None
        link = answer.get("link")
        if not isinstance(link, str) or origin(link) == ("", "", 0):
            raise BackendError(_("The download address from %(service)s is wrong.") % {"service": SERVICE})
        return link

    def search(self, folder: str, keyword: str, limit: int) -> list[Entry]:
        if not keyword or limit <= 0:
            return []
        scope = self._full(folder)
        options: dict[str, Any] = {"max_results": self.search_page, "filename_only": True}
        if scope:
            options["path"] = scope
        answer = self._rpc("/2/files/search_v2", {"query": keyword, "options": options})
        needle = keyword.casefold()
        top = paths.segments(scope)
        found: dict[str, Entry] = {}  # by path: Dropbox may give a result on two pages
        for _page in range(self.search_pages):
            for match in answer.get("matches") or []:
                metadata = match.get("metadata") if isinstance(match, dict) else None
                metadata = metadata.get("metadata") if isinstance(metadata, dict) else None  # MetadataV2
                if not _usable(metadata) or needle not in metadata["name"].casefold():
                    continue
                rest = _below(metadata.get("path_display"), top)
                if rest is None or not all(usable_name(part) for part in rest):
                    continue
                rest[-1] = metadata["name"]  # path_display may have the case of the last name only right
                path = paths.join(folder, *rest)
                found.setdefault(path, self._entry(metadata, path))
                if len(found) >= limit:
                    return list(found.values())
            cursor = answer.get("cursor")
            if answer.get("has_more") is not True or not isinstance(cursor, str) or not cursor:
                break
            answer = self._rpc("/2/files/search/continue_v2", {"cursor": cursor})
        return list(found.values())

    # -- writing

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        if size > self.single_upload_limit:
            return None  # more than one request takes: the site sends it through write()
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        answer = self._rpc("/2/files/get_temporary_upload_link",
                           {"commit_info": self._commit(self._full(path)), "duration": self.upload_link_ttl})
        link = answer.get("link")
        if not isinstance(link, str) or origin(link) == ("", "", 0):
            raise BackendError(_("%(service)s gave no upload address.") % {"service": SERVICE})
        return UploadTarget(kind="single", url=link, method="POST",
                            headers={"Content-Type": "application/octet-stream"}, expires_in=self.upload_link_ttl)

    def _append(self, session_id: str, offset: int, piece: bytes) -> None:
        """Append one piece at `offset` (Dropbox answers null), sending it again while no answer comes."""
        arg = {"cursor": {"session_id": session_id, "offset": offset}, "close": False}
        for attempt in range(3):
            try:
                self._send(self.content_root + "/2/files/upload_session/append_v2", arg=arg, content=piece)
                return
            except _WrongOffset as error:
                if attempt and error.correct_offset == offset + len(piece):
                    return  # the try before got there; its answer was lost
                raise
            except _Unanswered:
                if attempt == 2:
                    raise
                self.log.info("an upload piece got no answer; sending it again")
                time.sleep(self.retry_delay * 2 ** attempt)

    def _upload_session(self, full: str, data: BinaryIO, size: int) -> None:
        """An upload session: start and finish carry no data, appends the pieces, one in memory at a time."""
        session_id = self._upload("/2/files/upload_session/start", {"close": False}, b"").get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": SERVICE})
        offset = 0
        while offset < size:
            want = min(self.upload_chunk, size - offset)
            piece = read_exact(data, want)
            if len(piece) != want:
                raise DriverError(_("The uploaded data is incomplete: less than the size given."))
            self._append(session_id, offset, piece)
            offset += want
        self._upload("/2/files/upload_session/finish",
                     {"cursor": {"session_id": session_id, "offset": size}, "commit": self._commit(full)}, b"")

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        full = self._full(path)
        if size > self.single_upload_limit:
            self._upload_session(full, data, size)
            return
        start = position(data)

        def body() -> Iterator[bytes]:
            if start is not None:
                data.seek(start)
            return exact_body(data, size)

        # Streamed, not kept: it can be sent again only when `data` can seek back. Also when its answer is lost, once:
        # the file may be stored already, and Dropbox writes nothing for identical contents (WriteMode).
        for attempt in range(2):
            try:
                self._send(self.content_root + "/2/files/upload", arg=self._commit(full), content=body, length=size,
                           repeatable=start is not None)
                return
            except _Unanswered:
                if start is None or attempt:
                    raise
                self.log.info("an upload got no answer; sending it again")
                time.sleep(self.retry_delay)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        self._rpc("/2/files/create_folder_v2", {"path": self._full(path), "autorename": False})

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be deleted."))
        self._rpc("/2/files/delete_v2", {"path": self._full(path)})

    def _relocate(self, route: str, path: str, target: str) -> None:
        self._rpc(route, {"from_path": self._full(path), "to_path": self._full(target), "autorename": False})

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be renamed."))
        self._check_new_name(new_name)
        name = paths.name(path)
        if new_name == name:
            self.stat(path)
            return
        if new_name.lower() == name.lower():
            # Dropbox doesn't rename by the case alone ("case-only renaming is currently not supported"): two moves.
            rename_through(lambda old, new: self._relocate("/2/files/move_v2", old, new), path, new_name)
            return
        self._relocate("/2/files/move_v2", path, paths.join(paths.parent(path), new_name))

    def _check_folder(self, path: str, folder: str, *, copy: bool) -> None:
        """What a move or copy of `path` into `folder` needs before Dropbox is asked."""
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be copied.") if copy
                              else _("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(_("A folder can't be copied into itself.") if copy
                              else _("A folder can't be moved into itself."))
        if not self.stat(folder).is_dir:  # missing: NotFound here, as Dropbox would make it
            raise NotADirectory()

    def move(self, path: str, folder: str) -> None:
        self._check_folder(path, folder, copy=False)
        if paths.parent(path) == folder:
            self.stat(path)
            return
        self._relocate("/2/files/move_v2", path, paths.join(folder, paths.name(path)))

    def copy(self, path: str, folder: str) -> None:
        self._check_folder(path, folder, copy=True)
        if paths.parent(path) == folder:  # its own name is taken there, by itself
            self.stat(path)
            raise AlreadyExists()
        self._relocate("/2/files/copy_v2", path, paths.join(folder, paths.name(path)))
