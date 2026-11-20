"""Teldrive (https://github.com/tgdrive/teldrive), a self-hosted drive that keeps its files in Telegram: the files of
one Teldrive account, through the HTTP API of Teldrive 2. Documentation: https://tgdrive.github.io/teldrive/ (its API
reference: https://tgdrive.github.io/teldrive/api/). The API is defined in the repository by
openapi/teldrive.openapi.yaml, made from typespec/*.tsp: https://github.com/tgdrive/teldrive/tree/2.0.12/openapi and
https://github.com/tgdrive/teldrive/tree/2.0.12/typespec (release 2.0.12). Each fact below is marked [docs] (the
documentation or that definition) or [source] (Teldrive's source in that repository: the paths are in it). Teldrive
1.x has another API (/api/files ...), which this driver doesn't speak.

Paths
    Files and folders are known by UUID; the drive's root has none (parent_id NULL [source:
    db/migrations/000001_init.sql]), and a call about it leaves the optional parentId out [docs]. Paths are found by
    listing from the root down to `base_path` (IdDriver). A listing is GET /files with parentId, `page_size` (at most
    500) a page, following nextCursor [docs], which Teldrive gives while a page is full [source: ListFiles in
    internal/api/handler_core.go]. A folder that was trashed or is gone answers 404 [source: ResolveAccessMany in
    internal/shares/service.go]. Names are unique in a folder as they are spelled: case counts [source:
    db/migrations/000007_remove_normalized_names.sql].

Login
    An API key of the account, made in Teldrive under Settings → API keys [docs: rclone/setup], sent as X-Api-Key
    with every call [docs], never in a URL. It lasts until it is revoked, or until the expiry it may have been made
    with [docs]; then Teldrive answers 401 [source: GetActiveAPIKeyByHash in db/queries/auth.sql]. The access token
    of a login session would last 15 minutes [docs: configuration/security]. HFile acts as the account that made
    the key [source: AuthenticateAPIKey in internal/authn/service.go].

Requests
    The API is under <address>/api/v1 [docs]. Errors are {"error": {"code", "message"}} with the HTTP status [docs]:
    unauthorized 401, not_found 404, conflict 409 (a name taken, or a folder moved into itself; for an upload part,
    its Telegram channel full or unhealthy), upload_name_conflict 409, invalid_request 400 or 422, internal_error
    500, service_unavailable 503 [source: internal/api/support.go]. The calls that make something need an
    Idempotency-Key header, a UUID of the caller's [docs] (a new one for each call here); without one Teldrive
    answers 400 [source: the generated internal/api/gen].

Changes
    mkdir: POST /folders; rename: PATCH /files/{id}; move: POST /files/{id}/move; copy: POST /files/{id}/copy. A
    name taken meanwhile is a 409, never replaced: conflictPolicy fail for those that take one [docs], the unique
    names for a rename [source: as above]. Teldrive copies the Telegram messages of every file before it answers a
    copy [source: Copy in internal/fileops/service.go]: that call may take `copy_timeout` seconds. delete: DELETE
    /files/{id} puts it, with everything in it, into the account's Trash, where it can be restored or purged
    [docs: using/files-and-folders]; a periodic job set up by default (teldrive-trash-cleanup, Settings → Periodic
    jobs) purges what has been there 30 days [source: internal/jobs/runtime.go, internal/jobs/trash_cleanup.go].

Downloads
    Through this site only (can_direct_download False): GET /files/{id}/content/{name} needs the key, and answers a
    Range with 206 [docs; source: streamFile in internal/api/raw.go]. Teldrive's share links work without the key,
    and a read-only share of one file serves only that file [docs: using/sharing; source: resolveFile in
    internal/shares/service.go], but each link would be a share of the account: on Teldrive's Shared page while it
    lasts, and in the file's list of shares for good [source: ListShared, ListFileShares in db/queries/shares.sql].
    And the address the site reaches Teldrive at need not be one browsers can.

Uploads
    POST /uploads makes an upload session: name, parentId, size, modTime, mimeType (by the name), conflictPolicy
    replace, and preferredPartSize, the storage source's `part_size`; the session's partSize is the one used, as
    Teldrive may choose another [docs]. Parts are numbered from 1 and all but the last are partSize long; an empty
    file has none [source: validatePartShape in internal/uploads/service.go]. Each is PUT /uploads/{id}/parts/{n},
    application/octet-stream with its Content-Length [docs], which Teldrive streams on to Telegram as it arrives
    [source: UploadPart in internal/transfer/upload.go]: the parts are streamed from `data`, nothing is spooled.
    No checksum is sent: X-Part-Checksum is optional, and BLAKE3, which Python's standard library hasn't [docs].
    POST /uploads/{id}/complete makes the file [docs]; replace deletes a file that had the name for good (not into
    the Trash), and a folder of the name is upload_name_conflict [source: prepareConflictPolicy in
    internal/uploads/service.go]. A completion that gets no answer, or a 5xx, is asked again once: it is idempotent,
    and gives the file of a session completed already [source: Complete in internal/uploads/service.go], so an
    upload Teldrive has made isn't reported as failed. When a part or the completion fails, DELETE /uploads/{id}
    aborts the session and Teldrive deletes its parts from Telegram [docs]. Each part is one file in Telegram
    [source: UploadPart], which takes files of at most 2000 MiB (without Telegram Premium); rclone's Teldrive
    backend and Teldrive's imports take parts of up to 2000 MiB [docs: rclone/mounting-and-performance, the
    imports' chunkSize], and so does `part_size`.

Names
    Teldrive takes names of 1 to 255 characters [docs] (characters, not bytes [source: ogen v1.24.0's
    validate.String, which the generated code calls]), not of spaces only [source: files_name_not_blank in
    db/migrations/000001_init.sql], and refuses none of the characters HFile allows; HFile's limit of 255 bytes is
    the stricter. It doesn't refuse "/" either: such a name, made elsewhere, is left out of listings (usable_name).

Limits
    Teldrive's own HTTP server ends a request after an hour by default (its http.read-timeout and
    http.write-timeout, 1h [source: internal/config/config.go, internal/app/app.go]): a download through this
    site that takes longer breaks off then, and so does the upload of a part that takes longer. Its admin can set
    them to 0 (no limit); a smaller `part_size` makes the parts quicker.

    Not tried against a real Teldrive server: the tests use a fake built from the API definition and the
    source above.
"""

from __future__ import annotations

import datetime
import re
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import filetypes, http
from ._cloud import as_int, exact_body, json_or_none, parse_time, range_header, redirected, response_stream, short, transport_error, usable_name
from ._ids import IdDriver, Node, StaleFolder
from .base import AlreadyExists, AuthError, BackendError, DriverError, NotFound, Param, ReadStream

SERVICE = gettext_lazy("Teldrive")
ROOT = ""  # the id of the drive's root, which has none in Teldrive: calls about it leave parentId out
MIB = 1024 * 1024
DEFAULT_PART_SIZE = 512  # MiB, Teldrive's own default
TAKEN = ("conflict", "upload_name_conflict")  # the codes of a 409 for a name in use
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _to_node(item: Any) -> Node | None:
    """A Node of a FileEntry; None for one without a UUID (it couldn't go into a URL), a usable name or a kind."""
    if not isinstance(item, dict):
        return None
    node_id, name, kind = item.get("id"), item.get("name"), item.get("kind")
    if not isinstance(node_id, str) or not _UUID.fullmatch(node_id) or not usable_name(name) or kind not in ("file", "folder"):
        return None
    is_dir = kind == "folder"
    return Node(id=node_id, name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                modified=parse_time(item.get("modTime")))


def _parent(folder_id: str) -> dict[str, str]:
    """The parentId of a call about the folder: none for the root."""
    return {} if folder_id == ROOT else {"parentId": folder_id}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _unreadable() -> BackendError:
    return BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})


class TeldriveDriver(IdDriver):
    type = "teldrive"
    label = gettext_lazy("Teldrive")
    service = SERVICE
    root_id = ROOT
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://drive.example.com",
              help=gettext_lazy("The address of the Teldrive server, as it is opened in a browser. It must be "
                                "Teldrive 2.0 or later.")),
        Param("api_key", gettext_lazy("API key"), kind="password", secret=True,
              help=gettext_lazy("In Teldrive, open Settings → API keys, create a key and copy its secret, which "
                                "Teldrive shows only once. HFile then acts as the account that made the key.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in Teldrive that is this storage source's root, like / or /folder1.")),
        Param("part_size", gettext_lazy("Upload part size (MiB)"), kind="number", required=False,
              default=DEFAULT_PART_SIZE, minimum=16, maximum=2000, advanced=True,
              help=gettext_lazy("Teldrive keeps a file in Telegram in parts of this size, each uploaded in one "
                                "request; Telegram takes at most 2000 MiB. Make it smaller than the largest "
                                "request a proxy in front of Teldrive lets through, and small enough to upload "
                                "within an hour, after which Teldrive ends a request by default.")),
    )

    can_range = True
    can_direct_download = False  # see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    upload_conflict = "replace"

    # Tuning; tests make them small.
    page_size: ClassVar[int] = 500  # the most a listing takes
    max_pages: ClassVar[int] = 10_000
    copy_timeout: ClassVar[float] = 600.0  # seconds to wait for a copy's answer

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._api = str(self.config.get("url") or "").strip().rstrip("/") + "/api/v1"
        self._key = str(self.config.get("api_key") or "").strip()
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
            errors["url"] = gettext("The address should start with http:// or https://, like https://drive.example.com.")
        if not str(config.get("api_key") or "").isascii():  # it goes in a header, which takes ASCII only
            errors["api_key"] = gettext("A Teldrive API key has only ASCII letters, digits and symbols. Please copy it again from Teldrive.")
        return errors

    # -- requests

    def _send(self, method: str, path: str, *, idempotent: bool = False, stream: bool = False,
              timeout: float | None = None, taken: bool = True, **kwargs: Any) -> httpx.Response:
        """A call of the API (`path` below /api/v1) with the key in its header: the answer when it succeeded, else
        _failure()'s DriverError. `idempotent`: the call needs an Idempotency-Key, a new UUID; `taken`: whether a
        409 conflict means a name is taken."""
        headers = {"X-Api-Key": self._key, **kwargs.pop("headers", {})}
        if idempotent:
            headers["Idempotency-Key"] = str(uuid.uuid4())
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=http.CONNECT_TIMEOUT)
        try:
            response = self._http.send(self._http.build_request(method, self._api + path, headers=headers, **kwargs), stream=stream)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.is_success:
            return response
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        if stream:
            response = http.read_error(response)
        raise self._failure(response, taken=taken)

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        answer = json_or_none(self._send(method, path, **kwargs))
        if not isinstance(answer, dict):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of a Teldrive server?")
                               % {"service": SERVICE})
        return answer

    @staticmethod
    def _failure(response: httpx.Response, *, taken: bool = True) -> DriverError:
        answer = json_or_none(response)
        error = answer.get("error") if isinstance(answer, dict) else None
        error = error if isinstance(error, dict) else {}
        code, message = short(error.get("code") or "", 60), short(error.get("message") or "")
        status = response.status_code
        if status == 401:
            return AuthError(gettext("Teldrive refused the API key: it may have been revoked, or have expired. Please make "
                                     "a new one in Teldrive (Settings → API keys) and save it in the storage source's settings."))
        if status == 404:
            return NotFound()
        if status == 409 and taken and code in TAKEN:
            return AlreadyExists()
        if status == 413:
            return DriverError(gettext("Teldrive's web server, or a proxy in front of it, refused an upload part as too large "
                                       "(HTTP 413). Make the storage source's upload part size smaller, or that server's limit "
                                       "larger (Teldrive's documentation sets nginx's client_max_body_size to 0)."))
        if status == 429 or status >= 500:
            return BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                                % {"service": SERVICE, "status_code": status})
        detail = f"{message} ({code})" if message and code else message or code or "-"
        return DriverError(gettext("Teldrive refused this (HTTP %(status_code)s): %(message)s") % {"status_code": status, "message": detail})

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[Node]:
        query: dict[str, Any] = {"limit": self.page_size, **_parent(folder_id)}
        nodes: dict[str, Node] = {}  # by id: an entry renamed between two pages comes twice
        for _ in range(self.max_pages):
            try:
                answer = self._json("GET", "/files", params=query)
            except NotFound:
                if folder_id == ROOT:  # the root is always there: the API isn't
                    raise BackendError(gettext("No Teldrive API answered at this address (HTTP 404). Please check the "
                                               "address: it must be that of a Teldrive server, version 2.0 or later.")) from None
                raise StaleFolder(folder_id) from None
            items = answer.get("items")
            if not isinstance(items, list):
                raise _unreadable()
            for item in items:
                node = _to_node(item)
                if node is not None:
                    nodes[node.id] = node
            cursor = answer.get("nextCursor")
            if not isinstance(cursor, str) or not cursor:
                return list(nodes.values())
            query = {**query, "cursor": cursor}
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _create_node(self, parent_id: str, name: str) -> Node:
        made = _to_node(self._json("POST", "/folders", json={"name": name, "conflictPolicy": "fail", **_parent(parent_id)},
                                   idempotent=True))
        if made is None or not made.is_dir:
            raise _unreadable()
        return made

    def _delete_node(self, node: Node, parent_id: str) -> None:
        self._send("DELETE", f"/files/{node.id}")  # into the Trash, see the module's docstring

    def _rename_node(self, node: Node, name: str) -> None:
        self._send("PATCH", f"/files/{node.id}", json={"name": name})

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        self._send("POST", f"/files/{node.id}/move", json={"conflictPolicy": "fail", **_parent(target_id)}, idempotent=True)

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        return _to_node(self._json("POST", f"/files/{node.id}/copy", json={"conflictPolicy": "fail", **_parent(target_id)},
                                   idempotent=True, timeout=self.copy_timeout))

    def _abort(self, upload_id: str) -> None:
        """Abort an upload session that failed: Teldrive deletes the parts it has from Telegram."""
        try:
            self._send("DELETE", f"/uploads/{upload_id}")
        except DriverError as error:
            self.log.warning("couldn't abort the upload %s: %s", upload_id, error.message)

    def _complete(self, upload_id: str) -> Node | None:
        """POST /uploads/{id}/complete, asked once more when no answer came or a 5xx (see Uploads in the module's
        docstring)."""
        for attempt in range(2):
            try:
                return _to_node(self._json("POST", f"/uploads/{upload_id}/complete", idempotent=True))
            except BackendError:
                if attempt:
                    raise
        raise AssertionError("unreachable")

    def _part_size(self) -> int:
        """The part size to ask for, in bytes."""
        mib = as_int(self.config.get("part_size"))
        return (mib if mib is not None and mib > 0 else DEFAULT_PART_SIZE) * MIB

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        body: dict[str, Any] = {"name": name, "size": size, "modTime": _now(), "conflictPolicy": "replace",
                                "preferredPartSize": self._part_size(), **_parent(folder_id)}
        mime = filetypes.guess_type(name, strict=False)[0]
        if mime:
            body["mimeType"] = mime  # for Teldrive's own player and previews
        session = self._json("POST", "/uploads", json=body, idempotent=True)
        upload_id = session.get("id")
        if not isinstance(upload_id, str) or not _UUID.fullmatch(upload_id):
            raise _unreadable()
        try:
            part_size = as_int(session.get("partSize"))
            if part_size is None or part_size <= 0:
                raise _unreadable()
            for number, offset in enumerate(range(0, size, part_size), start=1):
                length = min(part_size, size - offset)
                # A 409 conflict here is the part's Telegram channel, not a name.
                self._send("PUT", f"/uploads/{upload_id}/parts/{number}", content=exact_body(data, length), taken=False,
                           headers={"Content-Type": "application/octet-stream", "Content-Length": str(length)})
            made = self._complete(upload_id)
        except BaseException:
            self._abort(upload_id)
            raise
        if made is None or made.is_dir:
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": SERVICE})
        return made

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        total = node.size
        if length == 0 or total == 0:  # nothing to fetch (Teldrive would open a Telegram session for it)
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=node.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        response = self._send("GET", f"/files/{node.id}/content/{urllib.parse.quote(node.name, safe='')}", headers=headers, stream=True)
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=node.modified)
