"""Google Drive (Drive API v3) as a storage source.

Paths
    The storage root is the folder `base_path` of My Drive, or of the shared drive `drive_id`. Drive
    has ids, not paths: a storage path is resolved name by name from the root with files.list
    ("'<parent>' in parents and name = '<name>' and trashed = false"). Folder ids are cached for
    five minutes; HFile's own renames, moves and deletes drop what they touch, and a listing forgets
    the cached folders below it that are gone. A folder moved or renamed in Google Drive itself can
    still be read through its old path until then; changes (uploads, mkdir, rename, move, copy,
    delete) first check the cached folders on their path (name, parent, not trashed; a check counts
    for `verify_ttl` seconds), so they never land in a folder that has moved away.

Shortcuts
    Shortcuts are followed for listing, reading and writing; delete, rename and move act on the
    shortcut itself, and copying one makes a new shortcut to the same target. A shortcut can point
    anywhere in Drive, so by default only those whose target is inside the base folder are shown,
    like symlinks of local storage; `outside_shortcuts` shows the others too. Shortcuts that would
    loop (their target is the folder they are in, or above it) are never shown, nor broken ones:
    their target is gone, or in the trash (Drive still gives a trashed file by its id, also when its
    folder was trashed). What is known of a target is cached like the folders: one trashed meanwhile
    can still be listed until then, but isn't opened, and changes look again. A folder reached
    through a shortcut can be in another drive (a shared drive, My Drive, "Shared with me"): its
    children are asked for in all the account's drives (corpora=allDrives), not in the storage's
    own, which doesn't have them.

Login
    - client_id, client_secret: an OAuth client of type "Web application" in the admin's own
      Google Cloud project, with the Drive API enabled and the site's callback as an authorised
      redirect URI;
    - refresh_token: filled in by the authorisation (scope https://www.googleapis.com/auth/drive,
      offline access, consent asked every time so Google always gives one).

Params
    - drive_id: a shared drive, "" for My Drive (the helper "drives" lists them);
    - base_path: the folder shown as the root;
    - outside_shortcuts: also follow shortcuts to things outside the base folder (e.g. folders
      "shared with me").

Requests
    Google's API quotas apply: a throttled request (and a read that failed) is repeated with
    Google's exponential backoff and jitter: twice within about 3 seconds for what a page shows, up
    to seven times (1, 2, 4 ... 32 seconds apart) in uploads and copies, then fails with a message.

Changes
    delete moves to the trash (Google empties it after 30 days).

Uploads
    Uploads are resumable, in chunks of 8 MiB, a new file or the new content of the existing
    one (same id); one whose session Google lost (404) starts over once, when the data can be
    read again.

Names
    Drive accepts any name, also "/" and the same name twice in one folder. Names here are the names
    HFile lists (Google Docs-type files get an extension, below):
    - names that can't be one path segment ("/" inside, "." or "..") are left out of listings;
    - HFile never makes a duplicate: mkdir, uploads, rename, move and copy raise AlreadyExists when
      the name is taken, also by something HFile doesn't show (a shortcut out of the base folder, or
      a broken one), and a lock per (folder, name) keeps two requests of the site from racing;
    - duplicates made elsewhere are listed once: a file or folder of exactly that name before a
      Google Docs-type file, a folder before files, else the most recently modified one (then the
      larger id), of those HFile may show. Path lookups pick the same item, so what is listed is
      what is opened; rename the others in Google Drive to reach them.

Google Docs-type files
    Google Docs, Sheets, Slides, Drawings and Apps Script projects have no bytes of their own:
    open() exports them as docx, xlsx, pptx, pdf and json (Google exports at most 10 MB). They (and
    shortcuts to them) are listed with their Drive name plus that extension ("Plan" as "Plan.docx"),
    and no size, so that a download has a name and a type the visitor's programs open. A path looks
    for exactly its name first: a real "Plan.docx" next to the Doc "Plan" is the one listed and
    opened. Changes go by the listed name too: an upload never replaces or converts such a file
    (refused; "Plan" without the extension is another name, so a new file); a rename keeps the
    extension ("Plan.docx" to "Q3.docx" renames the Doc to "Q3"; a new name without ".docx" is
    refused); delete, move and copy act on the Doc itself. Forms, Sites, Maps and the other types
    that can't be exported are left out of listings.

Limits
    - no direct download or upload links (both need the token): the site proxies transfers;
    - search uses Drive's "name contains", which matches the starts of words, not any part of a name
      ("World" doesn't find "HelloWorld"), in Drive's names (not the extension of a Google Docs-type
      file); results are then filtered by name and folder.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import random
import re
import threading
import time
import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import filetypes, http, paths
from ._cloud import (
    KeyedLocks,
    TokenCache,
    TTLCache,
    as_int,
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
    Unsupported,
)

SERVICE = "Google Drive"
SCOPE = "https://www.googleapis.com/auth/drive"

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE_APPS = "application/vnd.google-apps."

#: Export formats of the Google Workspace types that have one; the others are left out.
EXPORTS = {
    "application/vnd.google-apps.document":
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.google-apps.spreadsheet":
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.google-apps.presentation":
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.google-apps.drawing": "application/pdf",
    "application/vnd.google-apps.script": "application/vnd.google-apps.script+json",
}
#: The extensions of those exports, which such files are listed with.
EXTENSIONS = {
    "application/vnd.google-apps.document": ".docx",
    "application/vnd.google-apps.spreadsheet": ".xlsx",
    "application/vnd.google-apps.presentation": ".pptx",
    "application/vnd.google-apps.drawing": ".pdf",
    "application/vnd.google-apps.script": ".json",
}

#: The fields of a file we read (files.get / files.list ask for them explicitly: Drive v3 gives
#: only id, name and mimeType otherwise).
ITEM_FIELDS = ("id,name,mimeType,size,modifiedTime,parents,driveId,"
               "shortcutDetails(targetId,targetMimeType)")
#: The fields of a shortcut's target and of the folders above it (_node).
NODE_FIELDS = "id,name,parents,trashed,driveId"

RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
PERMISSION_REASONS = {"insufficientPermissions", "insufficientFilePermissions", "forbidden",
                      "appNotAuthorizedToFile", "domainPolicy"}
#: Where a Cloud project enables the Drive API, when Google's message doesn't name the project's page.
API_LIBRARY = "https://console.cloud.google.com/apis/library/drive.googleapis.com"
_CONSOLE = re.compile(r"https://console\.(?:cloud|developers)\.google\.com/[^\s\"'<>]+")

_EPOCH = datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc)
#: Folder levels a copy goes down at most (Drive itself allows 100).
_COPY_DEPTH = 100
#: Tries of a request that is throttled (or a read that failed): few for what a page shows, more
#: in uploads and copies, which are waited for anyway (_patiently).
_TRIES = 3
_PATIENT_TRIES = 8
#: Google's "maximum_backoff" ("typically 32 or 64 seconds").
_LONGEST_WAIT = 32.0


@dataclass(frozen=True)
class _Item:
    """A Drive file as the driver sees it; for a shortcut, `mime` and `target` are its target's."""

    id: str  # the item itself (the shortcut for shortcuts)
    name: str
    mime: str
    target: str
    size: int | None
    modified: datetime.datetime | None
    parents: tuple[str, ...]
    shortcut: bool = False
    drive: str = ""  # the shared drive it is in, "" for My Drive (a shortcut's own, not its target's)

    @property
    def is_dir(self) -> bool:
        return self.mime == FOLDER

    @property
    def workspace(self) -> bool:
        """A Google Docs-type file: no bytes, exported or invisible."""
        return self.mime.startswith(GOOGLE_APPS) and self.mime not in (FOLDER, SHORTCUT)

    @property
    def listed(self) -> str:
        """The name HFile lists it under: a Workspace file's with the extension of its export."""
        return self.name + EXTENSIONS[self.mime] if self.mime in EXTENSIONS else self.name


def _item_from(raw: Any) -> _Item | None:
    if not isinstance(raw, dict):
        return None
    item_id, name, mime = raw.get("id"), raw.get("name"), raw.get("mimeType")
    if not (isinstance(item_id, str) and item_id and isinstance(name, str) and isinstance(mime, str)):
        return None
    target, effective, shortcut = item_id, mime, False
    if mime == SHORTCUT:
        details = raw.get("shortcutDetails")
        details = details if isinstance(details, dict) else {}
        target_id, target_mime = details.get("targetId"), details.get("targetMimeType")
        if not (isinstance(target_id, str) and target_id and isinstance(target_mime, str)):
            return None  # a broken shortcut: nothing to show
        target, effective, shortcut = target_id, target_mime, True
    # Workspace files report their storage size, not the size of their export: unknown.
    size = None if shortcut or effective.startswith(GOOGLE_APPS) else as_int(raw.get("size"))
    parents = raw.get("parents")
    parents = tuple(p for p in parents if isinstance(p, str)) if isinstance(parents, list) else ()
    drive = raw.get("driveId")
    return _Item(item_id, name, effective, target, size, parse_time(raw.get("modifiedTime")),
                 parents, shortcut, drive if isinstance(drive, str) else "")


def _visible(item: _Item) -> bool:
    """Shown by HFile: everything but the Workspace types that can't be exported."""
    return not item.workspace or item.mime in EXPORTS


def _rank(item: _Item) -> tuple:
    """The order of items listed under one name: a file or folder of exactly that name before a
    Workspace file listed with its extension, a folder before files, then the most recently
    modified, then the larger id. Listings and lookups both use it, so a listed entry is the one
    its path opens."""
    return (not item.workspace, item.is_dir, item.modified or _EPOCH, item.id)


def _preferred(items: list[_Item]) -> _Item:
    return max(items, key=_rank)


def _stem(name: str) -> str | None:
    """The Drive name of a Workspace file listed as `name`: `name` without its export's extension;
    None when it has none of them."""
    for extension in EXTENSIONS.values():
        if name.endswith(extension):
            return name[: -len(extension)]
    return None


def _backoff(attempt: int, *, patient: bool) -> float:
    """Seconds to wait after the `attempt`-th try of a request failed. Patient (uploads, copies):
    Google's truncated exponential backoff, 2^n seconds up to _LONGEST_WAIT plus up to a second at
    random. Else (what a page shows) 0.5 s, 1 s, plus up to half as much again at random. The
    jitter keeps throttled requests from all coming back at the same moment."""
    if patient:
        return min(2.0 ** (attempt - 1), _LONGEST_WAIT) + random.random()
    return 0.5 * attempt * (1 + random.random())


def _literal(value: str) -> str:
    """A string for a single-quoted literal of Drive's query language."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _segment(value: str) -> str:
    return urllib.parse.quote(value, safe="")


class _SessionGone(BackendError):
    """Google no longer knows the upload session (404, 410): write() starts the upload over."""


def _error_from(response: httpx.Response) -> DriverError:
    """The DriverError for an error answer of Drive (or of the OAuth token endpoint)."""
    status = response.status_code
    body = json_or_none(response)
    error = body.get("error") if isinstance(body, dict) else None
    message, reason = "", ""
    if isinstance(error, dict):
        message = str(error.get("message") or "")
        errors = error.get("errors")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            reason = str(errors[0].get("reason") or "")
        reason = reason or str(error.get("status") or "")
    elif isinstance(error, str):  # OAuth style: {"error": "invalid_grant", "error_description": ...}
        reason = error
        message = str(body.get("error_description") or "")
    text = short(message)
    if status == 404:
        return NotFound()
    if status == 401:
        return AuthError(gettext("Google refused the access token. Please authorize again in the storage source's settings."))
    if status == 429 or reason in RATE_REASONS:
        return BackendError(gettext("Too many requests: Google Drive is rate-limiting them. Please try again later."))
    if reason == "dailyLimitExceeded":
        return BackendError(gettext("The Google Drive API quota is used up. Please try again later, or raise the quota in the Google Cloud console."))
    if reason in ("storageQuotaExceeded", "quotaExceeded"):  # quotaExceeded: Drive's older word for it
        return DriverError(gettext("Google Drive is out of space."))
    if reason == "accessNotConfigured":
        # Google's message names the page that enables the API in the client's project: cut to 200
        # characters like the other messages, that link lost its end.
        found = _CONSOLE.search(message)
        url = found.group().rstrip(".,;)") if found else API_LIBRARY
        return AuthError(gettext("The Google Drive API isn't enabled in the Google Cloud project of this client ID. Enable it at %(url)s, wait a few minutes, then try again.") % {"url": url})
    if reason == "exportSizeLimitExceeded":
        return DriverError(gettext("This Google document is too large to export: Google only exports documents up to 10 MB."))
    if reason == "cannotDownloadAbusiveFile":
        return DriverError(gettext("Google considers this file possibly harmful and doesn't allow downloading it."))
    if reason in PERMISSION_REASONS:
        if text:
            return DriverError(gettext("Permission denied: Google Drive refused this (%(reason)s). %(detail)s") % {"reason": reason, "detail": text})
        return DriverError(gettext("Permission denied: Google Drive refused this (%(reason)s).") % {"reason": reason})
    if status >= 500:
        return BackendError(gettext("Google Drive failed (HTTP %(status)s). Please try again later.") % {"status": status})
    code = reason or f"HTTP {status}"
    if text:
        return DriverError(gettext("Google Drive refused the request (%(code)s): %(detail)s") % {"code": code, "detail": text})
    return DriverError(gettext("Google Drive refused the request (%(code)s).") % {"code": code})


class GoogleDriveDriver(StorageDriver):
    type = "google-drive"
    label = "Google Drive"
    params = (
        Param("client_id", gettext_lazy("Client ID"),
              help=gettext_lazy("In the Google Cloud console (console.cloud.google.com): enable the Google "
                   'Drive API, create an "OAuth client ID" of type "Web application" in '
                   "\"APIs & Services → Credentials\", add this site's authorization callback "
                   'URL to its "Authorized redirect URIs", then enter its client ID here.')),
        Param("client_secret", gettext_lazy("Client secret"), kind="password", secret=True,
              help=gettext_lazy("The client secret of the same OAuth client.")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy('With the client ID and secret filled in, click "Authorize", log in '
                   "to the Google account and agree: it's filled in for you. For apps "
                   "whose OAuth consent screen is still \"Testing\", Google's refresh "
                   'tokens expire after 7 days, so publish the app to "In production".')),
        Param("drive_id", gettext_lazy("Shared drive"), required=False, default="",
              help=gettext_lazy('Leave empty for "My Drive". For a shared drive, authorize, then click "List shared drives" and choose one.')),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in the drive that is this storage source's root, like / or /folder1.")),
        Param("outside_shortcuts", gettext_lazy("Show shortcuts pointing outside the base path"), kind="bool", required=False, default=False,
              advanced=True,
              help=gettext_lazy("Google Drive shortcuts can point anywhere. By default only shortcuts into the "
                   "base path are shown, like symbolic links in local storage; when on, those "
                   'pointing elsewhere (folders in "Shared with me", say) are shown too, and '
                   "through them files outside the base path can be seen, uploaded and deleted.")),
    )

    can_range = True
    can_direct_download = False  # downloads need the token
    can_direct_upload = False  # a resumable session needs the site's Origin for CORS
    can_move = True
    can_copy = True
    can_search = True
    has_folders = True
    forbidden_chars = ""  # Drive takes any name; "/" and "\" are refused by the site already

    oauth = True
    helpers: ClassVar[Mapping[str, str]] = {"drives": gettext_lazy("List shared drives")}
    auth_params = ("refresh_token",)

    # Endpoints (tests point them at a fake).
    api_root: ClassVar[str] = "https://www.googleapis.com"
    auth_endpoint: ClassVar[str] = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint: ClassVar[str] = "https://oauth2.googleapis.com/token"

    #: Bytes per PUT of a resumable upload; a multiple of 256 KiB (Drive's rule for all but the last).
    chunk_size: ClassVar[int] = 8 * 1024 * 1024
    page_size: ClassVar[int] = 1000
    folder_cache_ttl: ClassVar[float] = 300.0
    #: Seconds a cached folder checked before a change counts as checked (bulk uploads into one
    #: folder don't check it for every file).
    verify_ttl: ClassVar[float] = 10.0
    #: Pages of search results to read at most.
    search_pages: ClassVar[int] = 10
    #: Scales the waits before repeating a request (tests make it small).
    retry_delay: ClassVar[float] = 1.0

    def __init__(self, config: Mapping[str, Any], *, on_update=None):
        super().__init__(config, on_update=on_update)
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._drive_id = str(self.config.get("drive_id") or "").strip()
        self._http = http.client()
        self._tokens = TokenCache(self._refresh)
        self._folders = TTLCache(4096, self.folder_cache_ttl)  # storage path -> _Item
        self._outside_ok = bool(self.config.get("outside_shortcuts"))
        self._nodes = TTLCache(4096, self.folder_cache_ttl)  # id -> NODE_FIELDS, {} when not found
        self._ancestors = TTLCache(4096, self.folder_cache_ttl)  # folder id -> it and the ids above
        self._verified = TTLCache(4096, self.verify_ttl)  # path -> cached folder checked a moment ago
        self._root_lock = threading.Lock()
        self._root_item: _Item | None = None
        self._root_at = 0.0
        self._names = KeyedLocks()  # (folder id, name) of what is being created
        self._patience = threading.local()  # .on in _patiently()
        self.log = logging.getLogger(f"hfile.storage.{self.type}")

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        client_id = str(config.get("client_id") or "")
        if client_id and (client_id != client_id.strip() or any(ch.isspace() for ch in client_id)):
            errors["client_id"] = gettext("The client ID can't contain spaces.")
        drive_id = str(config.get("drive_id") or "").strip()
        if drive_id and (len(drive_id) > 128 or not all(ch.isascii() and (ch.isalnum() or ch in "-_")
                                                         for ch in drive_id)):
            errors["drive_id"] = gettext('A shared drive ID can only have letters, digits, - and _. Please choose one with "List shared drives".')
        return errors

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        client_id = str(config.get("client_id") or "").strip()
        if not client_id:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        query = {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # a refresh token
            "prompt": "consent",  # ... every time, not only at the first consent
            "state": state,
        }
        return cls.auth_endpoint + "?" + urllib.parse.urlencode(query)

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        answer = cls._token_request(config, {"grant_type": "authorization_code", "code": code,
                                             "redirect_uri": redirect_uri})
        refresh = answer.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AuthError(gettext("Google gave no refresh token: remove this app in the Google "
                                    "account's \"Third-party apps & services\", then authorize again."))
        return {"refresh_token": refresh}

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if name != "drives":
            raise Unsupported()
        refresh = str(config.get("refresh_token") or "")
        if not refresh:
            raise AuthError(gettext('Please click "Authorize" and log in to the Google account before listing shared drives.'))
        token = cls._token_request(config, {"grant_type": "refresh_token", "refresh_token": refresh})["access_token"]
        choices = [{"value": "", "label": gettext("My Drive")}]
        page: str | None = None
        with http.client() as client:
            for _ in range(20):
                params = {"pageSize": "100", "fields": "nextPageToken,drives(id,name)"}
                if page:
                    params["pageToken"] = page
                try:
                    response = client.get(cls.api_root + "/drive/v3/drives", params=params,
                                          headers={"Authorization": f"Bearer {token}"})
                except httpx.HTTPError as error:
                    raise transport_error(error, SERVICE) from error
                if response.status_code >= 400:
                    raise _error_from(response)
                if response.status_code >= 300:
                    raise redirected(response, SERVICE)
                body = json_or_none(response)
                drives = body.get("drives") if isinstance(body, dict) else None
                if not isinstance(drives, list):
                    raise BackendError(gettext("The shared drive list from Google Drive is wrong."))
                for drive in drives:
                    if isinstance(drive, dict) and isinstance(drive.get("id"), str):
                        choices.append({"value": drive["id"], "label": str(drive.get("name") or drive["id"])})
                page = body.get("nextPageToken")
                if not isinstance(page, str) or not page:
                    break
        message = None if len(choices) > 1 else gettext("This account has no shared drives.")
        answer: dict[str, Any] = {"param": "drive_id", "choices": choices}
        if message:
            answer["message"] = message
        return answer

    # -- login

    @classmethod
    def _token_request(cls, config: Mapping[str, Any], fields: dict[str, str],
                       client: httpx.Client | None = None) -> dict[str, Any]:
        """POST to Google's token endpoint with the app's id and secret; the JSON answer."""
        client_id = str(config.get("client_id") or "").strip()
        secret = str(config.get("client_secret") or "")
        if not client_id or not secret:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        data = {"client_id": client_id, "client_secret": secret, **fields}
        try:
            if client is not None:
                response = client.post(cls.token_endpoint, data=data)
            else:
                with http.client() as own:
                    response = own.post(cls.token_endpoint, data=data)
        except httpx.HTTPError as error:
            raise transport_error(error, "Google") from error
        body = json_or_none(response)
        if response.status_code >= 500 or response.status_code == 429:
            raise BackendError(gettext("Google's authorization service is unavailable for "
                                       "now (HTTP %(status_code)s). Please try again later.") % {"status_code": response.status_code})
        if response.status_code != 200 or not isinstance(body, dict):
            reason = body.get("error") if isinstance(body, dict) else None
            detail = short(body.get("error_description")) if isinstance(body, dict) else ""
            if reason == "invalid_client":
                raise AuthError(gettext("Google doesn't recognize this client ID or client secret "
                                        "(invalid_client). Please check the storage source's settings."))
            if reason == "invalid_grant":
                raise AuthError(gettext("Google's authorization has expired or been revoked (invalid_grant). "
                                        "Please authorize again in the storage source's settings."))
            code = reason or response.status_code
            if detail:
                raise AuthError(gettext("Google refused the authorization (%(code)s): %(detail)s") % {"code": code, "detail": detail})
            raise AuthError(gettext("Google refused the authorization (%(code)s).") % {"code": code})
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise BackendError(gettext("Google's authorization service returned no access token."))
        return body

    def _refresh(self) -> tuple[str, float]:
        refresh = str(self.config.get("refresh_token") or "")
        if not refresh:
            raise AuthError(gettext("Not authorized yet: click \"Authorize\" in the storage source's settings and log in to the Google account."))
        answer = self._token_request(self.config, {"grant_type": "refresh_token", "refresh_token": refresh},
                                     client=self._http)
        new = answer.get("refresh_token")
        if isinstance(new, str) and new and new != refresh:
            self.update_config({"refresh_token": new})
        return answer["access_token"], float(as_int(answer.get("expires_in")) or 3600)

    # -- requests

    def _call(self, method: str, url: str, *, params: Mapping[str, str] | None = None, json: Any = None,
              content: bytes | None = None, headers: Mapping[str, str] | None = None,
              stream: bool = False, accept: tuple[int, ...] = (), repeatable: bool = False) -> httpx.Response:
        """One API request with the bearer token. A 401 refreshes the token and repeats once. A
        throttled request, and a GET that failed (5xx, no connection), is tried up to _TRIES times,
        _PATIENT_TRIES in _patiently(), with a wait before each repeat (_pause). `repeatable`: a POST
        or PATCH that makes nothing by itself (an upload session's start) is repeated like a GET."""
        if not url.startswith(("http://", "https://")):
            url = self.api_root + url
        patient = getattr(self._patience, "on", False)
        tries = _PATIENT_TRIES if patient else _TRIES
        safe = method == "GET" or repeatable
        attempt = 0
        while True:
            attempt += 1
            token = self._tokens.get()
            all_headers = dict(headers or {})
            all_headers["Authorization"] = f"Bearer {token}"
            try:
                request = self._http.build_request(method, url, params=params, json=json, content=content,
                                                   headers=all_headers)
                response = self._http.send(request, stream=stream)
            except httpx.HTTPError as error:
                if safe and attempt < tries and self._pause(attempt, None, patient=patient):
                    continue
                raise transport_error(error, SERVICE) from error
            status = response.status_code
            if status < 300 or status in accept:
                return response
            if status < 400:  # the API makes no redirects (STOR-15)
                raise redirected(response, SERVICE)
            if stream:
                response = http.read_error(response)
            if status == 401 and attempt == 1:
                self._tokens.invalidate(token)
                continue
            if attempt < tries and (self._throttled(response) or (status >= 500 and safe)) \
                    and self._pause(attempt, response, patient=patient):
                continue
            raise _error_from(response)

    def _pause(self, attempt: int, response: httpx.Response | None, *, patient: bool) -> bool:
        """Wait before repeating a request after its `attempt`-th try failed: as long as its answer
        asks (Retry-After), else _backoff. False, without waiting, when the answer asks for longer
        than is worth it."""
        wait: float | None = _backoff(attempt, patient=patient)
        if response is not None:
            wait = retry_after(response, default=wait, longest=2 * _LONGEST_WAIT if patient else 5.0)
            if wait is None:
                return False
        time.sleep(wait * self.retry_delay)
        return True

    @contextlib.contextmanager
    def _patiently(self) -> Iterator[None]:
        """Requests made inside (by this thread) wait out throttling and failures with Google's full
        exponential backoff: uploads and copies, which take their time anyway. Google's quota is per
        user and minute, and a folder copy left half-done can't simply be started again."""
        before = getattr(self._patience, "on", False)
        self._patience.on = True
        try:
            yield
        finally:
            self._patience.on = before

    def _throttled(self, response: httpx.Response) -> bool:
        """429, or 403 rateLimitExceeded / userRateLimitExceeded: Google says to repeat it after a wait."""
        status = response.status_code
        return status == 429 or (status == 403 and self._reason(response) in RATE_REASONS)

    @staticmethod
    def _reason(response: httpx.Response) -> str:
        body = json_or_none(response)
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict):
            errors = error.get("errors")
            if isinstance(errors, list) and errors and isinstance(errors[0], dict):
                return str(errors[0].get("reason") or "")
        return ""

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        body = json_or_none(response)
        if not isinstance(body, dict):
            raise BackendError(gettext("The data from Google Drive is wrong."))
        return body

    def _drive_params(self) -> dict[str, str]:
        params = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        if self._drive_id:
            params.update(corpora="drive", driveId=self._drive_id)
        else:
            params["corpora"] = "user"
        return params

    def _folder_params(self, folder: _Item) -> dict[str, str]:
        """_drive_params for a query of a folder's children. A folder reached through a shortcut can
        be in another drive than the storage (a shared drive, My Drive, "Shared with me"), whose
        corpus doesn't have its children: then they are asked for in all of the account's drives."""
        if folder.shortcut:
            target = self._node(folder.target)
            drive = target.get("driveId") if target else None
            drive = drive if isinstance(drive, str) else ""
        else:
            drive = folder.drive
        if drive == self._drive_id:
            return self._drive_params()
        return {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"}

    def _query(self, q: str, *, folder: _Item | None = None, page_size: int | None = None,
               max_pages: int | None = None) -> Iterator[dict]:
        """The files matching a Drive query, page after page: in the storage's drive, or, for a
        query of `folder`'s children, in the drive that folder is in (_folder_params)."""
        params = self._drive_params() if folder is None else self._folder_params(folder)
        params.update(q=q, fields=f"nextPageToken,files({ITEM_FIELDS})",
                      pageSize=str(min(page_size or self.page_size, 1000)))
        pages = 0
        seen: set[str] = set()
        while True:
            pages += 1
            body = self._json(self._call("GET", "/drive/v3/files", params=params))
            files = body.get("files")
            if not isinstance(files, list):
                raise BackendError(gettext("The file list from Google Drive is wrong."))
            yield from (raw for raw in files if isinstance(raw, dict))
            token = body.get("nextPageToken")
            if not isinstance(token, str) or not token or (max_pages and pages >= max_pages):
                return
            if token in seen:
                raise BackendError(gettext("Google Drive repeated a page token. Please try again later."))
            seen.add(token)
            params["pageToken"] = token

    def _get(self, file_id: str, fields: str = ITEM_FIELDS) -> dict[str, Any]:
        return self._json(self._call("GET", f"/drive/v3/files/{_segment(file_id)}",
                                     params={"supportsAllDrives": "true", "fields": fields}))

    # -- paths

    def _root(self) -> _Item:
        """The base folder; looked up again after folder_cache_ttl, like the folders below it."""
        with self._root_lock:
            if self._root_item is None or time.monotonic() - self._root_at > self.folder_cache_ttl:
                self._root_item = self._resolve_base()
                self._root_at = time.monotonic()
            return self._root_item

    def _resolve_base(self) -> _Item:
        top_id = self._drive_id or "root"
        try:
            raw = self._get(top_id)
        except NotFound:
            if self._drive_id:
                raise NotFound(gettext("The shared drive wasn't found, or this account has no permission on it.")) from None
            raise
        item = _item_from(raw)
        if item is None or not item.is_dir:
            raise BackendError(gettext("The root folder from Google Drive is wrong."))
        item = replace(item, drive=self._drive_id)  # the storage's drive, whatever Google says of its root
        for segment in paths.segments(self._base):
            matches = self._named(item, segment)
            if not matches:
                raise NotFound(gettext("The base path %(base)s doesn't exist in Google Drive.") % {"base": self._base})
            child = _preferred(matches)
            if not child.is_dir:
                raise NotADirectory(gettext("The base path %(base)s isn't a folder.") % {"base": self._base})
            item = child
        return item

    def _named(self, folder: _Item, name: str) -> list[_Item]:
        """The items listed as `name` in a folder: those called that, and the Workspace files called
        that without their export's extension (one query for both)."""
        test = f"name = '{_literal(name)}'"
        stem = _stem(name)
        if stem is not None:
            test = f"({test} or name = '{_literal(stem)}')"
        q = f"'{_literal(folder.target)}' in parents and {test} and trashed = false"
        matches = []
        for raw in self._query(q, folder=folder, page_size=100):
            item = _item_from(raw)
            if item is not None and _visible(item) and item.listed == name:
                matches.append(item)
        return matches

    def _shown(self, items: list[_Item]) -> _Item | None:
        """Of items listed under one name, the one HFile shows and opens: the first by _rank that
        it may follow, so that a shortcut it doesn't show hides no file; None when there is none."""
        for item in sorted(items, key=_rank, reverse=True):
            if self._may_follow(item):
                return item
        return None

    def _lookup(self, path: str, *, verify: bool = False) -> _Item:
        """The item at `path`. With `verify` (before a change), cached folders on the way are
        checked against Drive first, so that a folder moved or renamed in Google Drive since it
        was cached isn't written into, moved or deleted through its old path."""
        if path == paths.ROOT:
            return self._root()
        cached = self._folders.get(path)
        if cached is not None:
            if not verify or self._still_there(path, cached):
                return cached
            self._forget(path)
        parent_path, name = paths.split(path)
        parent = self._lookup(parent_path, verify=verify)
        if not parent.is_dir:
            raise NotFound()
        named = self._named(parent, name)
        if verify:  # before a change: a shortcut's target may have gone to the trash since it was cached
            for item in named:
                if item.shortcut:
                    self._node(item.target, fresh=True)
        child = self._shown(named)
        if child is None:
            raise NotFound()
        if child.is_dir:
            self._folders.put(path, child)
        return child

    def _still_there(self, path: str, item: _Item) -> bool:
        """Whether a cached folder still has its name and place, and a shortcut's target isn't in the
        trash now (checked at most every `verify_ttl` seconds per path)."""
        if self._verified.get(path) is item:
            return True
        parent = self._lookup(paths.parent(path), verify=True)
        try:
            raw = self._get(item.id, "id,name,parents,trashed")
        except NotFound:
            return False
        parents = raw.get("parents")
        if raw.get("trashed") or raw.get("name") != paths.name(path) or not isinstance(parents, list) \
                or parent.target not in parents:
            return False
        if item.shortcut:
            target = self._node(item.target, fresh=True)
            if target is None or target.get("trashed"):
                return False
        self._verified.put(path, item)
        return True

    def _may_follow(self, item: _Item) -> bool:
        """Whether a shortcut is shown: its target there and not in the trash, inside the base
        folder (or anywhere with outside_shortcuts), and not the folder the shortcut is in or one
        above it (a loop)."""
        if not item.shortcut:
            return True
        target = self._node(item.target)
        if target is None or target.get("trashed"):
            # Broken. Drive still gives a trashed file by its id; "trashed" is also true when a folder
            # above it went to the trash.
            return False
        if not self._outside_ok and self._place(item.target) is None:
            return False
        return bool(item.parents) and item.target not in self._ancestry(item.parents[0])

    def _node(self, file_id: str, *, fresh: bool = False) -> dict[str, Any] | None:
        """A shortcut's target, or a folder above one: its NODE_FIELDS (cached like the folders, so
        that the targets in one folder share the folders above them); None when Drive doesn't have it
        for us."""
        known = None if fresh else self._nodes.get(file_id)
        if known is None:
            try:
                known = self._get(file_id, NODE_FIELDS)
            except NotFound:
                known = {}
            self._nodes.put(file_id, known)
        return known or None

    def _place(self, file_id: str) -> str | None:
        """The storage path of a file or folder by id, walking its parents up to the base
        folder; None when it isn't below the base folder, or is in the trash."""
        root_id = self._root().target
        names: list[str] = []
        current = file_id
        for _ in range(64):
            if current == root_id:
                return "/" + "/".join(reversed(names))
            node = self._node(current)
            if node is None or node.get("trashed"):
                return None
            name, parents = node.get("name"), node.get("parents")
            if not isinstance(name, str) or not usable_name(name) or not isinstance(parents, list) \
                    or not parents or not isinstance(parents[0], str):
                return None
            names.append(name)
            current = parents[0]
        return None

    def _ancestry(self, folder_id: str) -> frozenset[str]:
        """The folder and all the folders above it, by id (a shortcut to one of them loops)."""
        cached = self._ancestors.get(folder_id)
        if cached is not None:
            return cached
        ids = [folder_id]
        current = folder_id
        for _ in range(64):
            known = self._ancestors.get(current) if current != folder_id else None
            if known is not None:
                ids.extend(known)
                break
            try:
                raw = self._get(current, "id,parents")
            except NotFound:
                break
            up = raw.get("parents")
            if not isinstance(up, list) or not up or not isinstance(up[0], str):
                break
            current = up[0]
            ids.append(current)
        result = frozenset(ids)
        self._ancestors.put(folder_id, result)
        return result

    def _stale_folder(self, path: str) -> str | None:
        """After NotFound for `path`: the nearest cached folder at or above it, when Drive no
        longer has it (deleted or trashed since it was cached); None when it is fine, i.e. the
        miss is real. One request, so that misses (a site probing for readme files) stay cheap."""
        while path != paths.ROOT:
            cached = self._folders.get(path)
            if cached is not None:
                try:
                    raw = self._get(cached.target, "id,trashed")
                except NotFound:
                    return path
                return path if raw.get("trashed") else None
            path = paths.parent(path)
        return None

    def _fresh(self, path: str, work):
        """work(); when it says NotFound because a cached folder id went stale (the folder was
        deleted or trashed in Google Drive), forget that folder and try once more."""
        try:
            return work()
        except NotFound:
            stale = self._stale_folder(path)
            if stale is None:
                raise
            self._forget(stale)
            return work()

    def _forget(self, path: str) -> None:
        self._folders.discard_if(lambda key: paths.is_inside(key, path))

    def _children(self, folder: _Item) -> list[_Item]:
        q = f"'{_literal(folder.target)}' in parents and trashed = false"
        return [item for item in map(_item_from, self._query(q, folder=folder)) if item is not None]

    def _ensure_folder(self, path: str) -> _Item:
        """The folder at `path` (checked, see _lookup), created with the missing ones above it."""
        try:
            item = self._lookup(path, verify=True)
        except NotFound:
            parent_path, name = paths.split(path)
            item = self._create_folder(self._ensure_folder(parent_path), name, path, exist_ok=True)
        if not item.is_dir:
            raise NotADirectory(gettext("%(path)s isn't a folder.") % {"path": path})
        return item

    def _create_folder(self, parent: _Item, name: str, path: str, *, exist_ok: bool) -> _Item:
        with self._names.hold((parent.target, name)):
            named = self._named(parent, name)
            if named:
                shown = self._shown(named) if exist_ok else None
                if shown is not None and shown.is_dir:
                    return shown
                raise AlreadyExists()
            raw = self._json(self._call("POST", "/drive/v3/files",
                                        params={"supportsAllDrives": "true", "fields": ITEM_FIELDS},
                                        json={"name": name, "mimeType": FOLDER, "parents": [parent.target]}))
        item = _item_from(raw)
        if item is None:
            raise BackendError(gettext("The data Google Drive returned for the new folder is wrong."))
        self._folders.put(path, item)
        return item

    def _entry(self, item: _Item, path: str) -> Entry:
        return Entry(name=paths.name(path), path=path, is_dir=item.is_dir,
                     size=None if item.is_dir else item.size, modified=item.modified)

    def _with_target(self, item: _Item) -> _Item:
        """A file shortcut with its target's size and time (a shortcut has neither); NotFound when
        the target went to the trash since the shortcut was looked at."""
        if not item.shortcut or item.is_dir:
            return item
        raw = self._get(item.target, ITEM_FIELDS + ",trashed")
        target = _item_from(raw)
        if target is None:
            raise BackendError(gettext("The shortcut target from Google Drive is wrong."))
        if raw.get("trashed"):
            self._nodes.pop(item.target)  # and listings leave the shortcut out from now on
            raise NotFound()
        return replace(item, size=target.size, modified=target.modified or item.modified)

    # -- reading

    def _list(self, path: str) -> list[Entry]:
        folder = self._lookup(path)
        if not folder.is_dir:
            raise NotADirectory()
        groups: dict[str, list[_Item]] = {}
        for child in self._children(folder):
            if not usable_name(child.listed):
                self.log.debug("left out a name that can't be a path segment: %r", child.listed)
                continue
            if _visible(child):
                groups.setdefault(child.listed, []).append(child)
        chosen_by_name: dict[str, _Item] = {}
        for name, group in groups.items():
            shown = self._shown(group)
            if shown is None:
                self.log.debug("left out a shortcut that points outside the base folder, loops or is broken: %r", name)
                continue
            chosen_by_name[name] = shown
        folders = {name: item for name, item in chosen_by_name.items() if item.is_dir}
        depth = len(paths.segments(path))
        # Cached folders below this one that aren't there now (moved, renamed or deleted in Drive).
        self._folders.discard_if(lambda key: key != path and paths.is_inside(key, path)
                                 and paths.segments(key)[depth] not in folders)
        entries = []
        for name, chosen in chosen_by_name.items():
            if len(groups[name]) > 1:
                self.log.info("%d items called %r in one folder; HFile shows one of them", len(groups[name]), name)
            child_path = paths.join(path, name)
            if chosen.is_dir:
                cached = self._folders.get(child_path)
                if cached is not None and cached.id != chosen.id:  # another folder has the name now
                    self._forget(child_path)
                self._folders.put(child_path, chosen)
            entries.append(self._entry(chosen, child_path))
        return entries

    def list(self, path: str) -> list[Entry]:
        return self._fresh(path, lambda: self._list(path))

    def _stat(self, path: str) -> Entry:
        item = self._lookup(path)
        if path == paths.ROOT:
            return Entry(name="", path=paths.ROOT, is_dir=True, size=None, modified=item.modified)
        return self._entry(self._with_target(item), path)

    def stat(self, path: str) -> Entry:
        return self._fresh(path, lambda: self._stat(path))

    def _open(self, path: str, offset: int, length: int | None) -> ReadStream:
        item = self._lookup(path)
        if item.is_dir:
            raise NotFound()
        item = self._with_target(item)
        if item.workspace:
            # Exports have no Range: response_stream skips and cuts what the site asked for.
            response = self._call("GET", f"/drive/v3/files/{_segment(item.target)}/export",
                                  params={"mimeType": EXPORTS[item.mime]},
                                  headers={"Accept-Encoding": "identity"}, stream=True)
            return response_stream(response, offset=offset, length=length, total=None,
                                   service=SERVICE, modified=item.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, item.size)
        if span and item.size:
            headers["Range"] = span
        response = self._call("GET", f"/drive/v3/files/{_segment(item.target)}",
                              params={"alt": "media", "supportsAllDrives": "true"}, headers=headers, stream=True)
        return response_stream(response, offset=offset, length=length, total=item.size,
                               service=SERVICE, modified=item.modified)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        return self._fresh(path, lambda: self._open(path, offset, length))

    def _path_of(self, item: _Item, root_id: str, known: dict) -> str | None:
        """The storage path of a search result, from its parents up to the base folder; None
        when it isn't below it."""
        names = [item.listed]
        parents = item.parents
        for _ in range(64):
            if not parents:
                return None
            parent_id = parents[0]
            if parent_id == root_id:
                return "/" + "/".join(reversed(names))
            if parent_id not in known:
                try:
                    raw = self._get(parent_id, "id,name,parents")
                except NotFound:  # not ours to see: not below the base folder either
                    known[parent_id] = None
                else:
                    name, up = raw.get("name"), raw.get("parents")
                    known[parent_id] = (name, tuple(p for p in up if isinstance(p, str))) \
                        if isinstance(name, str) and isinstance(up, list) else None
            info = known[parent_id]
            if info is None or not usable_name(info[0]):
                return None
            names.append(info[0])
            parents = info[1]
        return None

    def search(self, folder: str, keyword: str, limit: int) -> list[Entry]:
        if limit <= 0 or not keyword:
            return []
        start = self._lookup(folder)
        if not start.is_dir:
            raise NotADirectory()
        root = self._root()
        wanted = keyword.casefold()
        known: dict[str, tuple[str, tuple[str, ...]] | None] = {}  # id -> (name, parents) for this call
        found: list[Entry] = []
        q = f"name contains '{_literal(keyword)}' and trashed = false"
        for raw in self._query(q, page_size=200, max_pages=self.search_pages):
            item = _item_from(raw)
            if item is None or not _visible(item) or not usable_name(item.listed):
                continue
            if wanted not in item.listed.casefold() or not self._may_follow(item):
                continue
            path = self._path_of(item, root.target, known)
            if path is None or path == folder or not paths.is_inside(path, folder):
                continue
            # The path is made of names: with duplicates (of the item, or of a folder above it) it
            # can open another item, which is then the one listed there, or nothing.
            try:
                if self._lookup(path).id != item.id:
                    continue
            except NotFound:
                continue
            found.append(self._entry(item, path))
            if len(found) >= limit:
                break
        return found

    # -- writing

    def _session_put(self, session: str, body: bytes, content_range: str) -> httpx.Response | None:
        """One PUT to the upload session (no token: the session URI is the credential); None when
        the connection failed."""
        try:
            return self._http.put(session, content=body, headers={"Content-Range": content_range})
        except httpx.HTTPError as error:
            self.log.info("upload chunk failed: %s", type(error).__name__)
            return None

    def _answered(self, session: str, total: int, response: httpx.Response | None,
                  failures: int) -> tuple[httpx.Response, int]:
        """A PUT's answer, after waiting out failures: for a lost connection, a 5xx or throttling
        Google says to wait (exponential backoff) and ask the session what it has, then go on.
        Returns the first other answer and the failures in a row so far (`failures` before);
        _SessionGone when the session is gone, BackendError when the failures don't stop."""
        while response is None or response.status_code >= 500 or self._throttled(response):
            failures += 1
            if failures >= _PATIENT_TRIES or not self._pause(failures, response, patient=True):
                raise BackendError(gettext("Uploading to Google Drive failed. Please try again later."))
            response = self._session_put(session, b"", f"bytes */{total}")
        if response.status_code in (404, 410):
            raise _SessionGone(gettext("The Google Drive upload session has expired. Please upload again."))
        return response, failures

    @staticmethod
    def _persisted(response: httpx.Response) -> int:
        """Bytes Google has, from a 308's "Range: bytes=0-N" (none without the header)."""
        value = response.headers.get("range", "")
        if not value:
            return 0
        last = value.rpartition("-")[2]
        if not value.startswith("bytes=0-") or not (last.isascii() and last.isdigit()):  # not "²" (STOR-17)
            raise BackendError(gettext("The upload progress from Google Drive is wrong."))
        return int(last) + 1

    def _session_status(self, session: str, total: int) -> int | dict[str, Any]:
        """What Google has of the upload: the bytes persisted, or the file once complete."""
        response, _ = self._answered(session, total, self._session_put(session, b"", f"bytes */{total}"), 0)
        if response.status_code in (200, 201):
            return self._json(response)
        if response.status_code == 308:
            return self._persisted(response)
        raise _error_from(response)

    def _send_chunk(self, session: str, chunk: bytes, start: int, total: int) -> dict[str, Any] | None:
        """PUT bytes start .. start+len(chunk) of the file until Google has them; the file's
        metadata when that finished the upload, else None."""
        sent = 0  # bytes of the chunk Google has
        failures = 0  # failed requests in a row (_answered)
        stalls = 0  # answers that took nothing
        while sent < len(chunk):
            first, last = start + sent, start + len(chunk) - 1
            before = failures
            response, failures = self._answered(
                session, total, self._session_put(session, chunk[sent:], f"bytes {first}-{last}/{total}"), failures)
            if response.status_code in (200, 201):
                return self._json(response)
            if response.status_code != 308:
                raise _error_from(response)
            have = self._persisted(response)
            if not start <= have <= start + len(chunk):
                raise BackendError(gettext("The Google Drive upload progress doesn't match. Please upload again."))
            if have - start > sent:
                failures = 0
            elif failures == before:  # Google answered and took nothing: don't loop for ever
                stalls += 1
                if stalls > 3:
                    raise BackendError(gettext("Google Drive isn't taking the uploaded data. Please try again later."))
            sent = have - start
        return None

    def _upload(self, method: str, url: str, metadata: dict[str, Any], data: BinaryIO, size: int,
                content_type: str) -> dict[str, Any]:
        """A resumable upload of `size` bytes of `data`; the file's metadata."""
        # Nothing is made before the data is all there, so a failed start can be repeated.
        response = self._call(method, url, params={"uploadType": "resumable", "supportsAllDrives": "true",
                                                   "fields": ITEM_FIELDS},
                              json=metadata, headers={"X-Upload-Content-Type": content_type,
                                                      "X-Upload-Content-Length": str(size)}, repeatable=True)
        session = response.headers.get("location")
        if not session or origin(session) != origin(self.api_root):
            raise BackendError(gettext("Google Drive gave no upload address."))
        if size == 0:
            done = self._session_status(session, 0)
            if isinstance(done, dict):
                return done
            raise BackendError(gettext("Google Drive didn't confirm the upload finished."))
        offset = 0
        while offset < size:
            want = min(self.chunk_size, size - offset)
            chunk = read_exact(data, want)
            if len(chunk) < want:
                raise DriverError(gettext("The uploaded data is incomplete: less than the size given."))
            done = self._send_chunk(session, chunk, offset, size)
            offset += len(chunk)
            if done is not None:
                if offset < size:
                    raise BackendError(gettext("Google Drive ended the upload early."))
                return done
        status = self._session_status(session, size)
        if isinstance(status, dict):
            return status
        raise BackendError(gettext("Google Drive didn't confirm the upload finished."))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent_path, name = paths.split(path)
        with self._patiently():
            folder = self._ensure_folder(parent_path)
            with self._names.hold((folder.target, name)):
                named = self._named(folder, name)
                existing = self._shown(named)
                content_type = filetypes.guess_type(name)[0] or "application/octet-stream"
                if existing is None:
                    if named:  # only something HFile doesn't show has the name
                        raise AlreadyExists()
                    method, url = "POST", "/upload/drive/v3/files"
                    metadata: dict[str, Any] = {"name": name, "parents": [folder.target]}
                elif existing.is_dir:
                    raise AlreadyExists()
                elif existing.workspace:
                    raise DriverError(gettext("This is a Google Docs, Sheets or Slides file and can't be overwritten by an upload."))
                elif existing.shortcut:
                    raise DriverError(gettext("This is a Google Drive shortcut and can't be overwritten by an upload."))
                else:  # new content for the same file: its id, sharing and history stay
                    method, url, metadata = "PATCH", f"/upload/drive/v3/files/{_segment(existing.id)}", {}
                begin = position(data)
                for attempt in range(2):
                    try:
                        self._upload(method, url, metadata, data, size, content_type)
                        return
                    except _SessionGone:
                        # Google: then "the upload must be restarted from the beginning". Once, when the
                        # data can be read again; and not when a new file's name is taken now (its upload
                        # may have finished after all), which would make a duplicate.
                        if attempt or begin is None or (existing is None and self._named(folder, name)):
                            raise
                        self.log.info("Google Drive lost an upload session; starting the upload over")
                        data.seek(begin)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent_path, name = paths.split(path)
        self._create_folder(self._ensure_folder(parent_path), name, path, exist_ok=False)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        item = self._lookup(path, verify=True)
        # To the trash, not gone: Drive keeps it 30 days.
        self._call("PATCH", f"/drive/v3/files/{_segment(item.id)}",
                   params={"supportsAllDrives": "true", "fields": "id"}, json={"trashed": True})
        self._forget(path)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        item = self._lookup(path, verify=True)
        if new_name == paths.name(path):
            return
        drive_name = new_name
        if item.mime in EXTENSIONS:  # listed as "Plan.docx": renamed to "Q3.docx", it is "Q3" in Drive
            extension = EXTENSIONS[item.mime]
            if not new_name.endswith(extension):
                raise DriverError(gettext("This is a Google Docs, Sheets or Slides file, listed with the extension of the format it downloads in: the new name has to end in %(extension)s too.") % {"extension": extension})
            drive_name = new_name[: -len(extension)]
        parent = self._lookup(paths.parent(path), verify=True)
        with self._names.hold((parent.target, new_name)):
            if any(other.id != item.id for other in self._named(parent, new_name)):
                raise AlreadyExists()
            self._call("PATCH", f"/drive/v3/files/{_segment(item.id)}",
                       params={"supportsAllDrives": "true", "fields": "id"}, json={"name": drive_name})
        self._forget(path)

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved into itself."))
        item = self._lookup(path, verify=True)
        source = self._lookup(paths.parent(path), verify=True)
        target = self._lookup(folder, verify=True)
        if not target.is_dir:
            raise NotADirectory()
        name = paths.name(path)
        if target.target == source.target:
            return  # already there
        with self._names.hold((target.target, name)):
            if self._named(target, name):
                raise AlreadyExists()
            self._call("PATCH", f"/drive/v3/files/{_segment(item.id)}",
                       params={"supportsAllDrives": "true", "fields": "id", "addParents": target.target,
                               "removeParents": source.target}, json={})
        self._forget(path)

    def _copy_one(self, item: _Item, folder_id: str, name: str) -> str:
        """A copy of one item in a folder (a new empty folder for a folder); its id."""
        params = {"supportsAllDrives": "true", "fields": "id"}
        if item.shortcut:
            body = {"name": name, "mimeType": SHORTCUT, "parents": [folder_id],
                    "shortcutDetails": {"targetId": item.target}}
            raw = self._json(self._call("POST", "/drive/v3/files", params=params, json=body))
        elif item.is_dir:
            raw = self._json(self._call("POST", "/drive/v3/files", params=params,
                                        json={"name": name, "mimeType": FOLDER, "parents": [folder_id]}))
        else:
            raw = self._json(self._call("POST", f"/drive/v3/files/{_segment(item.id)}/copy", params=params,
                                        json={"name": name, "parents": [folder_id]}))
        new_id = raw.get("id")
        if not isinstance(new_id, str) or not new_id:
            raise BackendError(gettext("The data Google Drive returned for the copy is wrong."))
        return new_id

    def _copy_children(self, source: _Item, target_id: str, made: set[str], depth: int) -> None:
        """Copy everything in a folder, duplicates and names HFile doesn't show included; only the
        Workspace types without an export (forms, sites) are skipped.

        `made` are the copies made so far: when the target is inside the source (through a
        shortcut), they show up among the children and must not be copied again, for ever.
        """
        if depth > _COPY_DEPTH:
            raise DriverError(gettext("The folders are nested too deep to copy."))
        for child in self._children(source):
            if child.id in made or not _visible(child):
                continue
            new_id = self._copy_one(child, target_id, child.name)
            made.add(new_id)
            if child.is_dir and not child.shortcut:
                self._copy_children(child, new_id, made, depth + 1)

    def copy(self, path: str, folder: str) -> None:
        if path == paths.ROOT or paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself."))
        with self._patiently():
            item = self._lookup(path, verify=True)
            target = self._lookup(folder, verify=True)
            if not target.is_dir:
                raise NotADirectory()
            name = paths.name(path)
            with self._names.hold((target.target, name)):
                if self._named(target, name):
                    raise AlreadyExists()
                created = self._copy_one(item, target.target, item.name)  # a Doc's Drive name has no extension
            if item.is_dir and not item.shortcut:
                self._copy_children(item, created, {created}, 1)
