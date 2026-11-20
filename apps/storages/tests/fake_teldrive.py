"""An in-memory fake of a Teldrive 2 server's HTTP API (apps.storages.drivers.teldrive), on FakeServer.

    fake = FakeTeldrive().start()
    user, key = fake.add_user()        # an account and an API key of it
    fake.url
    fake.close()

It answers as Teldrive 2.0.12's API definition (openapi/teldrive.openapi.yaml, typespec/*.tsp) and its source
(github.com/tgdrive/teldrive: internal/ and db/) say:

- The API is under /api/v1; another path under /api is Go's plain "404 page not found", and any other path the
  web UI's page (200, HTML). A call needs a valid API key in X-Api-Key (else 401 unauthorized), then the
  Idempotency-Key header (a UUID) if it is one that has it, then a body that decodes: names of 1 to 255
  characters, a modTime for an upload, preferredPartSize of at least 1 MiB (else 400 invalid_request).
- Errors are {"error": {"code", "message"}} with the codes and messages of internal/api/support.go.
- Files and folders have UUIDs; the drive's root has none (parentId left out). Names are unique among the active
  entries of a folder as they are spelled. What isn't active (trashed, deletion_pending) is not_found.
- GET /files lists a folder (parentId; a file lists nothing), `limit` (1 to 500) a page by name and id, with a
  nextCursor while a page is full.
- POST /folders, PATCH /files/{id}, POST /files/{id}/move and /copy refuse a taken name (409 conflict); move
  refuses a folder into itself (409 conflict); a target that isn't a folder is 422; copy copies the tree with
  new ids. Only conflictPolicy fail is modelled for them.
- DELETE /files/{id} puts it and everything in it into the trash (status trashed).
- POST /uploads gives a session whose partSize is preferredPartSize, else 512 MiB, unless `chosen_part_size`.
  PUT /uploads/{id}/parts/{n} needs application/octet-stream and a Content-Length (a chunked body has none
  once Go has read it), n from 1, every part but the last exactly partSize (else 422). complete makes the file:
  replace marks a file of the name deletion_pending (not into the trash), fail or a folder of the name is 409
  upload_name_conflict, missing parts are a 500 as in Teldrive. DELETE /uploads/{id} aborts a session.
- GET /files/{id}/content/{name} sends the file, with Range (206; 416).

Knobs: revoke(key), chosen_part_size, fail_status (every call under /api), fail_part (that part number fails,
500), body_limit (a proxy's 413 for larger requests), version1 (no /api/v1, as Teldrive 1.x), drop_complete (that
many completions are made, but their answer is lost: the connection closes), complete_status (a proxy's answer to
the next completion, which isn't made). Records: calls.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import email.utils
import itertools
import json
import re
import secrets
import threading
import urllib.parse
import uuid
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

MIB = 1024 * 1024
DEFAULT_PART_SIZE = 512 * MIB
_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?(Z|[+-][0-9]{2}:[0-9]{2})")
SEGMENT = "([^/]+)"

# The messages internal/api/support.go gives its codes.
MESSAGES = {
    "unauthorized": "authentication is required",
    "not_found": "resource was not found",
    "forbidden": "operation is not permitted",
    "conflict": "operation conflicts with current state",
    "upload_name_conflict": "destination name already exists",
    "upload_invalid_state": "upload session is no longer open",
    "upload_part_conflict": "upload part size or checksum differs from the stored part",
    "internal_error": "request failed",
}

ROUTES = (  # method, route below /api/v1, handler, needs an Idempotency-Key
    ("GET", "/files", "_list", False),
    ("POST", "/folders", "_mkdir", True),
    ("PATCH", f"/files/{SEGMENT}", "_update", False),
    ("DELETE", f"/files/{SEGMENT}", "_trash", False),
    ("POST", f"/files/{SEGMENT}/move", "_move", True),
    ("POST", f"/files/{SEGMENT}/copy", "_copy", True),
    ("GET", f"/files/{SEGMENT}/content/{SEGMENT}", "_content", False),
    ("POST", "/uploads", "_create_upload", True),
    ("GET", f"/uploads/{SEGMENT}", "_get_upload", False),
    ("DELETE", f"/uploads/{SEGMENT}", "_abort", False),
    ("PUT", f"/uploads/{SEGMENT}/parts/{SEGMENT}", "_put_part", False),
    ("POST", f"/uploads/{SEGMENT}/complete", "_complete", True),
)


def error(status: int, code: str, message: str | None = None) -> HTTPError:
    return HTTPError(status, {"error": {"code": code, "message": message or MESSAGES[code]}})


def undecodable() -> HTTPError:
    """ogen's answer to parameters or a body it can't decode or that fail the definition's rules."""
    return error(400, "invalid_request", "request could not be decoded")


def invalid() -> HTTPError:
    """A service's ErrInvalidInput, ErrInvalidParent, ...: 422."""
    return error(422, "invalid_request", "request is invalid")


def now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def go_time(moment: datetime.datetime) -> str:
    """A time of PostgreSQL as Go's encoding/json writes it: RFC 3339 in UTC, microseconds without trailing zeros."""
    return moment.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".") + "Z"


def raw_base64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclasses.dataclass
class Item:
    id: str
    user: int
    parent: str | None  # None: the drive's root
    name: str
    folder: bool
    data: bytes = b""
    mime: str | None = None
    mod_time: datetime.datetime = dataclasses.field(default_factory=now)
    created: datetime.datetime = dataclasses.field(default_factory=now)
    updated: datetime.datetime = dataclasses.field(default_factory=now)
    status: str = "active"  # active | trashed | deletion_pending
    generation: int = 1


@dataclasses.dataclass
class Upload:
    id: str
    user: int
    parent: str | None
    name: str
    size: int
    mod_time: datetime.datetime
    mime: str | None
    policy: str
    part_size: int
    state: str = "open"  # open | completed | aborted
    created: datetime.datetime = dataclasses.field(default_factory=now)
    file_id: str | None = None
    parts: dict[int, bytes] = dataclasses.field(default_factory=dict)


class FakeTeldrive:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeTeldrive:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.keys: dict[str, int] = {}  # API key -> user id
            self.items: dict[str, Item] = {}
            self.uploads: dict[str, Upload] = {}
            self.user_ids = itertools.count(7_000_000_001)
            self.chosen_part_size: int | None = None
            self.fail_status = 0
            self.fail_part: int | None = None
            self.body_limit: int | None = None
            self.version1 = False
            self.drop_complete = 0
            self.complete_status = 0
            self.calls: list[dict[str, Any]] = []

    # -- for tests

    def add_user(self) -> tuple[int, str]:
        """A Telegram account that logged in to Teldrive, and an API key it made (Settings → API keys)."""
        with self.lock:
            user = next(self.user_ids)
            key = "tdk_" + raw_base64(secrets.token_bytes(32))
            self.keys[key] = user
            return user, key

    def revoke(self, key: str) -> None:
        with self.lock:
            self.keys.pop(key, None)

    def find(self, user: int, path: str) -> Item | None:
        """The active entry at `path` of the user's drive; None when there is none (and for "/")."""
        with self.lock:
            item, parent = None, None
            for name in [part for part in path.split("/") if part]:
                item = self._child(user, parent, name)
                if item is None:
                    return None
                parent = item.id
            return item

    def mkdirs(self, user: int, path: str) -> str | None:
        """The id of the folder at `path` (None: the root), made with those above it, as Teldrive's UI would."""
        with self.lock:
            parent = None
            for name in [part for part in path.split("/") if part]:
                item = self._child(user, parent, name) or self._add(user, parent, name, folder=True)
                parent = item.id
            return parent

    def put(self, user: int, path: str, data: bytes, *, name: str | None = None) -> Item:
        """A file at `path`, or named `name` (any name, "/" too) in the folder `path`, as Teldrive's UI would."""
        with self.lock:
            folder, _, last = path.rpartition("/") if name is None else (path, "", name)
            return self._add(user, self.mkdirs(user, folder), last, folder=False, data=data)

    def trash(self, user: int, path: str) -> None:
        """Delete it in Teldrive's own UI: into the trash, with everything in it."""
        with self.lock:
            for item in self._below(self.find(user, path)):
                item.status = "trashed"

    def names(self, user: int, path: str) -> list[str]:
        with self.lock:
            folder = self.find(user, path) if path != "/" else None
            return sorted(item.name for item in self._children(user, folder.id if folder else None))

    def requests(self, method: str, route: str) -> list[dict[str, Any]]:
        """The calls of `method` whose path below /api/v1 matches the regular expression `route`."""
        with self.lock:
            return [call for call in self.calls
                    if call["method"] == method and re.fullmatch("/api/v1" + route, call["path"])]

    # -- the tree

    def _add(self, user: int, parent: str | None, name: str, *, folder: bool, data: bytes = b"",
             mime: str | None = None, mod_time: datetime.datetime | None = None) -> Item:
        item = Item(str(uuid.uuid4()), user, parent, name, folder, data, mime)
        if mod_time is not None:
            item.mod_time = mod_time
        self.items[item.id] = item
        return item

    def _children(self, user: int, parent: str | None) -> list[Item]:
        return [item for item in self.items.values() if item.user == user and item.parent == parent and item.status == "active"]

    def _child(self, user: int, parent: str | None, name: str) -> Item | None:
        return next((item for item in self._children(user, parent) if item.name == name), None)

    def _below(self, item: Item) -> list[Item]:
        found = [item]
        for child in [other for other in self.items.values() if other.parent == item.id]:
            found += self._below(child)
        return found

    def _inside(self, folder: Item, root: Item) -> bool:
        """Whether `folder` is `root` or below it."""
        current: Item | None = folder
        while current is not None:
            if current.id == root.id:
                return True
            current = self.items.get(current.parent) if current.parent else None
        return False

    def _entry(self, item: Item) -> dict[str, Any]:
        entry: dict[str, Any] = {"id": item.id, "name": item.name, "kind": "folder" if item.folder else "file",
                                 "encryption": False, "status": item.status, "modTime": go_time(item.mod_time),
                                 "generation": item.generation, "createdAt": go_time(item.created),
                                 "updatedAt": go_time(item.updated)}
        if item.parent is not None:
            entry["parentId"] = item.parent
        if not item.folder:
            entry["size"] = len(item.data)
        if item.mime:
            entry["mimeType"] = item.mime
        return entry

    def _created(self, item: Item) -> Response:
        return json_response(self._entry(item), 201, {"Etag": f'"{item.generation}"', "Location": f"/files/{item.id}"})

    def _changed(self, item: Item) -> Response:
        item.generation += 1
        item.updated = now()
        return json_response(self._entry(item), 200, {"Etag": f'"{item.generation}"'})

    # -- requests

    def handle(self, req: Request) -> Response:
        path = urllib.parse.unquote(req.path)
        with self.lock:
            self.calls.append({
                "method": req.method, "path": path, "target": req.target, "query": req.query,
                "api_key": req.header("X-Api-Key"), "authorization": req.header("Authorization"),
                "cookie": req.header("Cookie"), "idempotency": req.header("Idempotency-Key"),
                "content_type": req.header("Content-Type"), "content_length": req.header("Content-Length"),
                "transfer_encoding": req.header("Transfer-Encoding"), "range": req.header("Range"),
                "accept_encoding": req.header("Accept-Encoding"), "size": len(req.body),
                "json": self._json_of(req)})
            if not req.path.startswith("/api/"):  # the web UI's page, for every path it routes
                return Response(200, b"<!doctype html><title>Teldrive</title>", {"Content-Type": "text/html; charset=utf-8"})
            if self.fail_status:  # a proxy in front of it
                return Response(self.fail_status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
            if self.body_limit is not None and len(req.body) > self.body_limit:
                return Response(413, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
            for method, route, handler, idempotent in ROUTES:
                match = None if self.version1 else re.fullmatch("/api/v1" + route, req.path)
                if match is not None and method == req.method:
                    break
            else:
                return Response(404, b"404 page not found\n", {"Content-Type": "text/plain; charset=utf-8"})
            user = self._user(req)
            if idempotent and not _UUID.fullmatch(req.header("Idempotency-Key") or ""):
                raise undecodable()
            return getattr(self, handler)(req, user, *[urllib.parse.unquote(group) for group in match.groups()])

    @staticmethod
    def _json_of(req: Request) -> Any:
        if not (req.header("Content-Type") or "").startswith("application/json"):
            return None
        try:
            return json.loads(req.body)
        except ValueError:
            return None

    def _user(self, req: Request) -> int:
        key = (req.header("X-Api-Key") or "").strip()
        user = self.keys.get(key) if key else None
        if user is None:
            raise error(401, "unauthorized")
        return user

    @staticmethod
    def _body(req: Request) -> dict[str, Any]:
        if not (req.header("Content-Type") or "").startswith("application/json"):
            raise undecodable()
        try:
            body = json.loads(req.body)
        except ValueError:
            raise undecodable() from None
        if not isinstance(body, dict):
            raise undecodable()
        return body

    @staticmethod
    def _uuid(value: Any) -> str:
        if not isinstance(value, str) or not _UUID.fullmatch(value):
            raise undecodable()
        return value

    @staticmethod
    def _name(value: Any) -> str:
        """minLength 1, maxLength 255, counted in characters as ogen counts them."""
        if not isinstance(value, str) or not 1 <= len(value) <= 255:
            raise undecodable()
        return value

    @staticmethod
    def _policy(body: dict[str, Any]) -> str:
        policy = body.get("conflictPolicy", "fail")
        if policy not in ("fail", "replace", "rename"):
            raise undecodable()
        return policy

    def _access(self, user: int, item_id: str) -> Item:
        """ResolveAccess: the item must be active (404), and the user's (403: no grants are modelled)."""
        item = self.items.get(item_id)
        if item is None or item.status != "active":
            raise error(404, "not_found")
        if item.user != user:
            raise error(403, "forbidden")
        return item

    def _target(self, user: int, body: dict[str, Any]) -> Item | None:
        """The folder of parentId (None: the root); 404 if it isn't active, 422 when it is a file."""
        if body.get("parentId") is None:
            return None
        target = self._access(user, self._uuid(body["parentId"]))
        if not target.folder:
            raise invalid()  # ErrInvalidParent
        return target

    def _list(self, req: Request, user: int) -> Response:
        limit = req.arg("limit", "100")
        if not (limit.isascii() and limit.isdigit()) or not 1 <= int(limit) <= 500:
            raise undecodable()
        parent = req.arg("parentId")
        if parent is not None:
            self._access(user, self._uuid(parent))
        children = sorted(self._children(user, parent), key=lambda item: (item.name, item.id))
        cursor = req.arg("cursor")
        if cursor is not None:
            try:
                after = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
                last = (after["name"], after["id"])
            except (ValueError, KeyError, TypeError):
                raise invalid() from None
            children = [item for item in children if (item.name, item.id) > last]
        page = children[: int(limit)]
        body: dict[str, Any] = {"items": [self._entry(item) for item in page]}
        if page and len(page) == int(limit):
            body["nextCursor"] = raw_base64(json.dumps({"name": page[-1].name, "id": page[-1].id, "sort": "name"}).encode())
        return json_response(body)

    def _mkdir(self, req: Request, user: int) -> Response:
        body = self._body(req)
        name = self._name(body.get("name"))
        if self._policy(body) != "fail":
            raise invalid()  # ErrUnsupportedConflictPolicy
        target = self._target(user, body)
        parent = target.id if target else None
        if not name.strip(" "):
            raise error(500, "internal_error")  # files_name_not_blank, a CHECK violation nothing maps
        if self._child(user, parent, name) is not None:
            raise error(409, "conflict")
        return self._created(self._add(user, parent, name, folder=True))

    def _update(self, req: Request, user: int, item_id: str) -> Response:
        self._uuid(item_id)
        body = self._body(req)
        name = self._name(body["name"]) if "name" in body else None
        item = self._access(user, item_id)
        if name is None and "modTime" not in body:
            raise invalid()  # ErrInvalidName
        if name is not None:
            if not name.strip(" "):
                raise error(500, "internal_error")
            other = self._child(user, item.parent, name)
            if other is not None and other.id != item.id:
                raise error(409, "conflict")
            item.name = name
        return self._changed(item)

    def _trash(self, req: Request, user: int, item_id: str) -> Response:
        item = self._access(user, self._uuid(item_id))
        for below in self._below(item):
            if below.status == "active":
                below.status, below.updated = "trashed", now()
        return Response(204)

    def _move(self, req: Request, user: int, item_id: str) -> Response:
        self._uuid(item_id)
        body = self._body(req)
        policy = self._policy(body)
        source = self._access(user, item_id)
        target = self._target(user, body)
        if target is not None and self._inside(target, source):
            raise error(409, "conflict")  # ErrCycle
        if policy != "fail":
            raise error(500, "internal_error", "conflict policies other than fail aren't modelled by the fake")
        parent = target.id if target else None
        other = self._child(user, parent, source.name)
        if other is not None and other.id != source.id:
            raise error(409, "conflict")
        source.parent = parent
        return self._changed(source)

    def _copy(self, req: Request, user: int, item_id: str) -> Response:
        self._uuid(item_id)
        body = self._body(req)
        policy = self._policy(body)
        name = self._name(body["name"]) if "name" in body else None
        source = self._access(user, item_id)
        target = self._target(user, body)
        if policy != "fail":
            raise error(500, "internal_error", "conflict policies other than fail aren't modelled by the fake")
        parent = target.id if target else None
        if self._child(user, parent, name or source.name) is not None:
            raise error(409, "conflict")
        # The tree as it was before anything is added, as Teldrive loads it first (loadTree).
        tree = {item.id: self._children(user, item.id) for item in self._below(source) if item.status == "active"}

        def clone(item: Item, parent: str | None, new_name: str) -> Item:
            made = self._add(user, parent, new_name, folder=item.folder, data=item.data, mime=item.mime, mod_time=item.mod_time)
            for child in tree.get(item.id, []):
                clone(child, made.id, child.name)
            return made

        return self._created(clone(source, parent, name or source.name))

    def _content(self, req: Request, user: int, item_id: str, name: str) -> Response:
        item = self._access(user, self._uuid(item_id))
        if item.folder:
            raise invalid()  # ErrInvalidDownload
        disposition = "attachment" if req.arg("download") == "1" else "inline"
        headers = {"Content-Type": item.mime or "application/octet-stream", "ETag": f'"{item.generation}"',
                   "Content-Disposition": f"{disposition}; filename*=utf-8''{urllib.parse.quote(item.name)}",
                   "Last-Modified": email.utils.format_datetime(item.mod_time, usegmt=True), "X-Accel-Buffering": "no"}
        return file_response(item.data, req.header("Range"), headers)

    # -- uploads

    def _session(self, upload: Upload) -> dict[str, Any]:
        session: dict[str, Any] = {"id": upload.id, "name": upload.name, "expectedSize": upload.size,
                                   "modTime": go_time(upload.mod_time), "encryption": False,
                                   "conflictPolicy": upload.policy, "partSize": upload.part_size, "state": upload.state,
                                   "expiresAt": go_time(upload.created + datetime.timedelta(days=7)),
                                   "createdAt": go_time(upload.created)}
        if upload.parent is not None:
            session["parentId"] = upload.parent
        if upload.mime:
            session["mimeType"] = upload.mime
        if upload.file_id:
            session["fileId"] = upload.file_id
        return session

    def _upload(self, user: int, upload_id: str) -> Upload:
        upload = self.uploads.get(self._uuid(upload_id))
        if upload is None or upload.user != user:
            raise error(404, "not_found")
        return upload

    def _create_upload(self, req: Request, user: int) -> Response:
        body = self._body(req)
        name = self._name(body.get("name"))
        size, preferred, mime = body.get("size"), body.get("preferredPartSize"), body.get("mimeType")
        if not isinstance(size, int) or isinstance(size, bool) or size < -1:
            raise undecodable()
        if not isinstance(body.get("modTime"), str) or not _TIME.fullmatch(body["modTime"]):
            raise undecodable()  # required
        if preferred is not None and (not isinstance(preferred, int) or isinstance(preferred, bool) or preferred < MIB):
            raise undecodable()  # minValue 1048576
        if mime is not None and not isinstance(mime, str):
            raise undecodable()
        policy = self._policy(body)
        if "hash" in body or body.get("encryption"):
            raise error(500, "internal_error", "hashes and encryption aren't modelled by the fake")
        target = self._target(user, body)
        upload = Upload(str(uuid.uuid4()), user, target.id if target else None, name, size,
                        datetime.datetime.fromisoformat(body["modTime"]), mime, policy,
                        self.chosen_part_size or preferred or DEFAULT_PART_SIZE)
        self.uploads[upload.id] = upload
        return json_response(self._session(upload), 201)

    def _get_upload(self, req: Request, user: int, upload_id: str) -> Response:
        return json_response(self._session(self._upload(user, upload_id)))

    def _part(self, upload: Upload, number: int) -> dict[str, Any]:
        moment = go_time(now())
        size = len(upload.parts[number])
        return {"uploadId": upload.id, "partNo": number, "state": "stored", "plainSize": size, "storedSize": size,
                "createdAt": moment, "updatedAt": moment}

    def _put_part(self, req: Request, user: int, upload_id: str, number: str) -> Response:
        self._uuid(upload_id)
        if not (number.isascii() and number.isdigit()) or int(number) < 1:
            raise undecodable()  # minValue 1
        if (req.header("Content-Type") or "").split(";")[0].strip() != "application/octet-stream":
            raise undecodable()
        length = req.header("Content-Length")
        if length is None or "chunked" in (req.header("Transfer-Encoding") or "").lower():
            raise undecodable()  # the header is required, and Go drops it from a chunked request
        part, size = int(number), int(length)
        upload = self._upload(user, upload_id)
        if size <= 0:
            raise invalid()  # ErrInvalidUpload
        if part in upload.parts:  # an exact retry is answered with the part stored
            if len(upload.parts[part]) != size:
                raise error(409, "upload_part_conflict")
            return json_response(self._part(upload, part), 200)
        if upload.state != "open":
            raise error(409, "upload_invalid_state")
        offset = (part - 1) * upload.part_size
        if upload.size == 0 or offset >= upload.size or size != min(upload.part_size, upload.size - offset):
            raise invalid()  # validatePartShape
        if self.fail_part == part:
            raise error(500, "internal_error")  # Telegram refused it
        if len(req.body) != size:
            raise error(500, "internal_error")  # body_size_mismatch
        upload.parts[part] = req.body
        return json_response(self._part(upload, part), 201)

    def _complete(self, req: Request, user: int, upload_id: str) -> Response:
        upload = self._upload(user, upload_id)
        if self.complete_status:  # a proxy's answer, before Teldrive got the call
            status, self.complete_status = self.complete_status, 0
            return Response(status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
        response = self._completed(user, upload)
        if self.drop_complete:  # made, but the answer is lost
            self.drop_complete -= 1
            return Response(0)
        return response

    def _completed(self, user: int, upload: Upload) -> Response:
        if upload.state == "completed":
            return self._created(self.items[upload.file_id])
        if upload.state != "open":
            raise error(409, "upload_invalid_state")
        count = -(-upload.size // upload.part_size) if upload.size else 0
        if sorted(upload.parts) != list(range(1, count + 1)) or sum(map(len, upload.parts.values())) != upload.size:
            raise error(500, "internal_error")  # ErrIncomplete, which support.go doesn't map
        existing = self._child(user, upload.parent, upload.name)
        if existing is not None:
            if upload.policy == "fail" or existing.folder:
                raise error(409, "upload_name_conflict")
            if upload.policy != "replace":
                raise error(500, "internal_error", "the rename policy isn't modelled by the fake")
            existing.status = "deletion_pending"  # deleted for good by Teldrive's purge, not trashed
        data = b"".join(upload.parts[part] for part in sorted(upload.parts))
        item = self._add(user, upload.parent, upload.name, folder=False, data=data, mime=upload.mime, mod_time=upload.mod_time)
        upload.state, upload.file_id = "completed", item.id
        upload.parts.clear()
        return self._created(item)

    def _abort(self, req: Request, user: int, upload_id: str) -> Response:
        upload = self._upload(user, upload_id)
        if upload.state == "completed":
            raise error(409, "upload_invalid_state")
        upload.state = "aborted"
        upload.parts.clear()  # Teldrive's upload cleanup job deletes them from Telegram
        return Response(204)
