"""Misskey (https://misskey-hub.net/), a self-hosted server of the fediverse: the drive of one Misskey account, through
Misskey's HTTP API. Documentation: https://misskey-hub.net/en/docs/for-developers/api/ (access tokens:
https://misskey-hub.net/en/docs/for-developers/api/token/, permissions:
https://misskey-hub.net/en/docs/for-developers/api/permission/). The endpoints are described by the OpenAPI definition
every server serves at /api.json, with a reference at /api-doc (e.g. https://misskey.io/api-doc), and by the source,
https://github.com/misskey-dev/misskey/tree/2026.11.0/packages/backend/src (release 2026.11.0). Each fact below is
marked [docs] (that documentation or the API definition) or [source] (the source: the paths are below
packages/backend/src). OpenList's driver (drivers/misskey) gave the first leads to the endpoints, all confirmed there.

Paths
    Files and folders are known by ids of letters and digits [source: server/api/endpoint-base.ts]; the drive's root
    has none: calls about it leave folderId (parentId) out, or send null [docs]. Paths are found by listing from the
    root down to `base_path` (IdDriver). A folder is listed in two parts, drive/folders and drive/files, each
    `page_size` (at most 100) a page, newest first, the next page asked for with untilId, the id of the last one [docs;
    source: makePaginationQuery in core/QueryService.ts]. Neither checks that the folder exists [source:
    server/api/endpoints/drive/files.ts, folders.ts], so a folder that lists nothing is asked for with
    drive/folders/show, which answers NO_SUCH_FOLDER for one deleted elsewhere [docs]. Names aren't unique in a folder:
    Misskey keeps several files or folders of one name, and finds a name as it is spelled [source: models/DriveFile.ts,
    DriveFolder.ts, endpoints/drive/files/find.ts], so case counts.

Login
    An access token of the account, made in Misskey under Settings → Service integration → API → "Generate access
    token" [source: packages/frontend/src/pages/settings/connect.vue] (Settings → API in older versions [docs]),
    with the permissions read:drive ("Access your Drive files and folders") and, to change anything, write:drive
    ("Edit or delete your Drive files and folders") [docs]. It goes in "Authorization: Bearer" with every call
    [docs], never in a URL nor in a body. It has no expiry: it works until it is deleted under "Manage access
    tokens" [source: models/AccessToken.ts, packages/frontend/src/pages/settings/apps.vue]; then Misskey answers 401
    AUTHENTICATION_FAILED (403 by misskey.io's /api.json, of a fork of 2025.4 [docs]), and a call the token has no
    permission for 403 PERMISSION_DENIED [source: server/api/ApiCallService.ts]. HFile acts as the account.

Requests
    Every call is a POST to <address>/api/<endpoint> with a JSON body, answered with JSON, or with 204 and nothing
    [docs]. Errors are {"error": {"code", "message", "id"}} with the HTTP status [docs]: 400 for the client's
    (NO_SUCH_FILE, NO_SUCH_FOLDER, INVALID_FILE_NAME ...), 401, 403, 413, 429 RATE_LIMIT_EXCEEDED with Retry-After, 500
    INTERNAL_ERROR [docs; source: ApiCallService.ts]. A path without an endpoint answers 404 [source:
    server/api/ApiServerService.ts].

Changes
    mkdir: drive/folders/create; rename and move: drive/files/update (name, folderId), drive/folders/update (name,
    parentId) [docs]. Misskey takes names that are in use (IdDriver checks first). It has no copy:
    drive/files/upload-from-url only queues a download and answers 204 [docs], so can_copy is False and the site
    copies files itself. drive/files/delete deletes a file for good: Misskey has no trash, and the notes it was
    attached to lose it [source: deleteFile in core/DriveService.ts, packAttachedFiles in
    core/entities/NoteEntityService.ts]. drive/folders/delete deletes empty folders only (HAS_CHILD_FILES_OR_FOLDERS
    [docs]): what is in one is deleted first, a call per file and folder.

Downloads
    A file's url, which comes with its listing, is the address Misskey gives everyone for it (other servers and
    browsers fetch the files of notes from it): <address>/files/<key>, or in the object storage the server keeps
    files in [source: save in core/DriveService.ts]. It needs no login, and lasts as long as the file, through
    renames and moves. download_url() gives it; open() fetches it with Range, which Misskey answers with 206
    [source: server/file/FileServerUtils.ts], without the token, following redirects. Misskey names the file in its
    answer (Content-Disposition inline): by its name, in object storage by the one it had when it was uploaded
    [source: server/file/FileServerDriveHandler.ts, upload in core/DriveService.ts].

Uploads
    drive/files/create takes multipart/form-data [docs]: the fields come first, then the file, streamed with its
    length; Misskey takes the request's first file and the fields read with it [source: handleMultipartRequest in
    ApiCallService.ts]. force is true: else Misskey answers with a file of the account that has the same MD5,
    wherever it is, and makes none [docs: force; source: addFile in core/DriveService.ts]. Misskey trims the name,
    and adds an extension when it has none or one that doesn't fit the type Misskey finds in the content (".unknown"
    when it finds none) [source: misc/correct-filename.ts, core/FileInfoService.ts]: a file that came back named
    otherwise is renamed with drive/files/update, which keeps a name as it is [source: updateFile in
    core/DriveService.ts]. Over a file of the name, the new one is uploaded, then the old one deleted (IdDriver's
    "keep"). Misskey answers once it has stored the file, in object storage too [source: save]: that may take
    `upload_timeout` seconds.

Names
    File names have 1 to 200 characters as JavaScript counts them (UTF-16 code units, never more than UTF-8 bytes)
    and no \\, / or ".." [source: validateFileName in core/entities/DriveFileEntityService.ts]: max_name_bytes is
    200, and a name with ".." is refused here before anything is sent. Folder names may have 200 characters by the
    API definition [docs], but the database keeps 128 and fails on longer ones [source: models/DriveFolder.ts]: they
    are refused here. A name with "/", which a folder made elsewhere may have, is left out of listings (usable_name).

Limits
    An upload may be as large as the smaller of the server's maxFileSize (its configuration file; 250 MiB by default;
    over it Misskey answers 413 without a body) and "Upload-able max file size" of the account's roles (30 MB by
    default; MAX_FILE_SIZE_EXCEEDED); "Uploadable file types" (by default text/*, application/json, image/*, video/*
    and audio/*; UNALLOWED_FILE_TYPE) and "Drive capacity" (100 MB by default; NO_FREE_SPACE) apply too; moderators
    and administrators have only the server's limit [source: core/RoleService.ts, addFile, ApiServerService.ts]. The
    driver doesn't learn them: the server's maxFileSize is public (api/meta), but the account's own limits come with
    api/i, which needs the read:account permission [source: core/entities/MetaEntityService.ts, endpoints/i.ts];
    Misskey's refusal is reported instead. An account may make 10 folders and 120 uploads an hour, divided by its
    roles' rate limit factor [source: the endpoints' meta, server/api/RateLimiterService.ts].

    Not tried against a real Misskey server: the tests use a fake built from the API definition and the source above.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
import re
import secrets
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
from ._cloud import (
    as_int,
    exact_body,
    json_or_none,
    parse_time,
    range_header,
    redirected,
    response_stream,
    retry_after,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, StaleFolder
from .base import AuthError, BackendError, DriverError, InvalidPath, NotFound, Param, ReadStream, Unsupported

SERVICE = gettext_lazy("Misskey")
ROOT = ""  # the id of the drive's root, which has none in Misskey: calls about it leave folderId out
FOLDER_NAME_LIMIT = 128  # characters a folder's name keeps in Misskey's database
NOT_FOUND = ("NO_SUCH_FILE", "NO_SUCH_FOLDER", "NO_SUCH_PARENT_FOLDER")
_ID = re.compile(r"[A-Za-z0-9]+")  # the API's format misskey:id


@dataclasses.dataclass(frozen=True)
class _Node(Node):
    url: str | None = None  # a file's public address


def _to_node(item: Any, *, is_dir: bool) -> _Node | None:
    """A Node of a DriveFolder or DriveFile; None for one without an id or a usable name."""
    if not isinstance(item, dict):
        return None
    node_id, name, url = item.get("id"), item.get("name"), item.get("url")
    if not isinstance(node_id, str) or not _ID.fullmatch(node_id) or not usable_name(name):
        return None
    if is_dir or not isinstance(url, str) or not url.startswith(("https://", "http://")):
        url = None
    return _Node(id=node_id, name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                 modified=parse_time(item.get("createdAt")), url=url)


def _in(folder_id: str, key: str) -> dict[str, str]:
    """The folder of a call (`key` folderId or parentId): none for the root."""
    return {} if folder_id == ROOT else {key: folder_id}


def _permission(endpoint: str) -> str:
    """The permission a drive endpoint needs."""
    return "write:drive" if endpoint.rpartition("/")[2] in ("create", "update", "delete") else "read:drive"


def _bad_file_name() -> InvalidPath:
    return InvalidPath(gettext('Misskey doesn\'t take this file name: file names there have at most 200 characters and '
                               'no \\, / or "..".'))


def _check_file_name(name: str) -> None:
    """InvalidPath for a file name Misskey refuses that HFile's own checks let through."""
    if ".." in name:
        raise _bad_file_name()


def _check_folder_name(name: str) -> None:
    if len(name) > FOLDER_NAME_LIMIT:
        raise InvalidPath(gettext("Misskey takes folder names of at most 128 characters."))


def _unreadable() -> BackendError:
    return BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})


class MisskeyDriver(IdDriver):
    type = "misskey"
    label = gettext_lazy("Misskey")
    service = SERVICE
    root_id = ROOT
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://misskey.example",
              help=gettext_lazy("The address of the Misskey server, as it is opened in a browser.")),
        Param("access_token", gettext_lazy("Access token"), kind="password", secret=True,
              help=gettext_lazy('In Misskey, open Settings → Service integration (Settings → API in older versions), '
                                'press "Generate access token", turn on "Access your Drive files and folders" and, to '
                                'upload and change files, "Edit or delete your Drive files and folders", and copy the '
                                'token, which Misskey shows only once. HFile then acts as that account.')),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the drive that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # the files' own public addresses, see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = False  # Misskey has no copy: the site copies files itself
    can_search = False
    has_folders = True
    upload_conflict = "keep"
    max_name_bytes = 200  # see Names in the module's docstring

    # Tuning; tests make them small.
    page_size: ClassVar[int] = 100  # the most a listing takes
    max_pages: ClassVar[int] = 10_000
    upload_timeout: ClassVar[float] = 600.0  # seconds to wait for the answer to an upload

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._api = str(self.config.get("url") or "").strip().rstrip("/") + "/api"
        self._token = str(self.config.get("access_token") or "").strip()
        self._http = http.client()

    def close(self) -> None:
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
            errors["url"] = gettext("The address should start with http:// or https://, like https://misskey.example.")
        if not str(config.get("access_token") or "").isascii():  # it goes in a header, which takes ASCII only
            errors["access_token"] = gettext("A Misskey access token has only ASCII letters and digits. Please copy it again from Misskey.")
        return errors

    # -- requests

    def _call(self, endpoint: str, body: Mapping[str, Any] | None = None, *, timeout: float | None = None,
              **kwargs: Any) -> Any:
        """POST /api/<endpoint> with the token in its header: the JSON answer (None for 204), else _failure()'s
        DriverError. The body is `body` as JSON, unless `content` and its headers are given."""
        headers = dict(kwargs.pop("headers", {}))
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if "content" not in kwargs:
            kwargs["json"] = dict(body or {})
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=http.CONNECT_TIMEOUT)
        try:
            response = self._http.post(f"{self._api}/{endpoint}", headers=headers, **kwargs)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        if not response.is_success:
            raise self._failure(response, endpoint)
        if response.status_code == 204:
            return None
        answer = json_or_none(response)
        if answer is None:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of a "
                                       "Misskey server?") % {"service": SERVICE})
        return answer

    @staticmethod
    def _failure(response: httpx.Response, endpoint: str) -> DriverError:
        answer = json_or_none(response)
        error = answer.get("error") if isinstance(answer, dict) else None
        error = error if isinstance(error, dict) else {}
        code = short(error.get("code") or "", 60)
        message = short(error.get("message") or "")
        status = response.status_code
        if status == 401 or code == "AUTHENTICATION_FAILED":
            return AuthError(gettext("Misskey refused the access token: it may have been deleted. Please generate a new "
                                     "one in Misskey (Settings → Service integration) and save it in the storage "
                                     "source's settings."))
        if code == "PERMISSION_DENIED":
            return AuthError(gettext('The Misskey access token lacks the permission %(permission)s. Please generate one '
                                     'with "Access your Drive files and folders" and "Edit or delete your Drive files '
                                     'and folders" in Misskey, and save it in the storage source\'s settings.')
                             % {"permission": _permission(endpoint)})
        if code == "YOUR_ACCOUNT_SUSPENDED":
            return AuthError(gettext("The Misskey account is suspended."))
        if code in NOT_FOUND:
            return NotFound()
        if code == "INVALID_FILE_NAME":
            return _bad_file_name()
        if code == "NO_FREE_SPACE":
            return DriverError(gettext('The Misskey account\'s drive is full ("Drive capacity" of its role). Please '
                                       'delete files from it, or ask the server\'s administrator for more space.'))
        if code == "MAX_FILE_SIZE_EXCEEDED":
            return DriverError(gettext('The file is larger than Misskey lets this account upload ("Upload-able max '
                                       'file size" of its role, 30 MB by default).'))
        if code == "UNALLOWED_FILE_TYPE":
            return DriverError(gettext('Misskey doesn\'t let this account upload this type of file. By default it '
                                       'takes text, JSON, images, audio and video only; the server\'s administrator can '
                                       'allow more in "Uploadable file types" of the role.'))
        if code == "INAPPROPRIATE":
            return DriverError(gettext("Misskey refused the file: its detection of sensitive media took it for "
                                       "inappropriate content."))
        if code == "HAS_CHILD_FILES_OR_FOLDERS":
            return DriverError(gettext("The folder couldn't be deleted: something was put in it meanwhile. Please try "
                                       "again."))
        if code == "YOUR_ACCOUNT_MOVED":
            return DriverError(gettext("The Misskey account has moved to another account, and Misskey takes no uploads "
                                       "from it."))
        if status == 413:
            return DriverError(gettext("The file is larger than the Misskey server, or a proxy in front of it, takes "
                                       "(HTTP 413). Misskey's own limit is maxFileSize in its configuration file, 250 MiB "
                                       "by default."))
        if status == 429:
            if "retry-after" not in response.headers:
                return BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.")
                                    % {"service": SERVICE})
            minutes = max(1, math.ceil(retry_after(response, longest=math.inf) / 60))
            return BackendError(ngettext("Misskey limits the folders and files an account may make in an hour (by "
                                         "default 10 folders and 120 uploads). Please try again in %(minutes)s minute.",
                                         "Misskey limits the folders and files an account may make in an hour (by "
                                         "default 10 folders and 120 uploads). Please try again in %(minutes)s minutes.",
                                         minutes) % {"minutes": minutes})
        if status == 404:
            return BackendError(gettext("No Misskey API answered at this address (HTTP 404). Please check the address: "
                                        "it must be that of a Misskey server."))
        if status >= 500:
            return BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                                % {"service": SERVICE, "status_code": status})
        detail = f"{message} ({code})" if message and code else message or code or "-"
        return DriverError(gettext("Misskey refused this (HTTP %(status_code)s): %(message)s") % {"status_code": status, "message": detail})

    def _pages(self, endpoint: str, folder_id: str, *, is_dir: bool) -> list[_Node]:
        """What drive/folders or drive/files lists of the folder: all pages, newest first."""
        body: dict[str, Any] = {"limit": self.page_size, **_in(folder_id, "folderId")}
        nodes: dict[str, _Node] = {}  # by id
        for _ in range(self.max_pages):
            page = self._call(endpoint, body)
            if not isinstance(page, list):
                raise _unreadable()
            for item in page:
                node = _to_node(item, is_dir=is_dir)
                if node is not None:
                    nodes[node.id] = node
            if len(page) < self.page_size:
                return list(nodes.values())
            last = page[-1].get("id") if isinstance(page[-1], dict) else None
            if not isinstance(last, str) or not _ID.fullmatch(last) or last == body.get("untilId"):
                raise _unreadable()
            body = {**body, "untilId": last}
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _children(self, folder_id: str) -> list[_Node]:
        return self._pages("drive/folders", folder_id, is_dir=True) + self._pages("drive/files", folder_id, is_dir=False)

    # -- files

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        """The file's own address at Misskey: public, and valid as long as the file, not `expires`; Misskey names
        the file itself."""
        return self._file(path).url

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        _check_file_name(paths.name(path))  # before a folder is made for it
        super().write(path, data, size)

    def copy(self, path: str, folder: str) -> None:
        raise Unsupported()  # Misskey has no copy (can_copy False): the site copies files itself

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[Node]:
        nodes = self._children(folder_id)
        if not nodes and folder_id != ROOT:  # the listings don't tell an empty folder from a deleted one
            try:
                self._call("drive/folders/show", {"folderId": folder_id})
            except NotFound:
                raise StaleFolder(folder_id) from None
        return nodes

    def _create_node(self, parent_id: str, name: str) -> Node:
        _check_folder_name(name)
        made = _to_node(self._call("drive/folders/create", {"name": name, **_in(parent_id, "parentId")}), is_dir=True)
        if made is None:
            raise _unreadable()
        return made

    def _delete_node(self, node: Node, parent_id: str) -> None:
        if not node.is_dir:
            self._call("drive/files/delete", {"fileId": node.id})  # for good: Misskey has no trash
            return
        for child in self._children(node.id):  # Misskey deletes empty folders only
            try:
                self._delete_node(child, node.id)
            except NotFound:  # gone meanwhile
                pass
        self._call("drive/folders/delete", {"folderId": node.id})

    def _rename_node(self, node: Node, name: str) -> None:
        if node.is_dir:
            _check_folder_name(name)
            self._call("drive/folders/update", {"folderId": node.id, "name": name})
        else:
            _check_file_name(name)
            self._call("drive/files/update", {"fileId": node.id, "name": name})

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        target = target_id if target_id != ROOT else None  # null: the drive's root
        if node.is_dir:
            self._call("drive/folders/update", {"folderId": node.id, "parentId": target})
        else:
            self._call("drive/files/update", {"fileId": node.id, "folderId": target})

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        fields = {**_in(folder_id, "folderId"), "name": name, "force": "true"}
        boundary = "hfile-" + secrets.token_hex(16)
        head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'
                       for key, value in fields.items())
        head += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="blob"\r\n'
                 "Content-Type: application/octet-stream\r\n\r\n")
        start, end = head.encode(), f"\r\n--{boundary}--\r\n".encode()
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
                   "Content-Length": str(len(start) + size + len(end))}
        made = _to_node(self._call("drive/files/create", content=itertools.chain([start], exact_body(data, size), [end]),
                                   headers=headers, timeout=self.upload_timeout), is_dir=False)
        if made is None:
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": SERVICE})
        if made.name != name:  # trimmed, or given an extension (see Uploads in the module's docstring)
            made = _to_node(self._call("drive/files/update", {"fileId": made.id, "name": name}), is_dir=False)
            if made is None or made.name != name:
                raise _unreadable()
        return made

    def _read_node(self, node: _Node, offset: int, length: int | None) -> ReadStream:
        total = node.size
        if length == 0 or total == 0:  # nothing to fetch
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=node.modified)
        if node.url is None:
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        # The address is public: no token goes with it, nor where it redirects.
        try:
            response = self._http.send(self._http.build_request("GET", node.url, headers=headers), stream=True,
                                       follow_redirects=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code == 404:  # deleted meanwhile (Misskey answers with a placeholder picture)
            http.drain(response)
            raise NotFound()
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=node.modified)
