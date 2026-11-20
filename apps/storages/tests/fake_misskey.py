"""An in-memory fake of a Misskey server's drive API and file server (apps.storages.drivers.misskey), on FakeServer.

    fake = FakeMisskey().start()
    user, token = fake.add_user()                    # an account, and an access token with read:drive and write:drive
    token = fake.add_token(user, ("read:drive",))    # another one of the account, which may only read
    fake.url
    fake.close()

It answers as Misskey 2026.11.0's source (github.com/misskey-dev/misskey, packages/backend/src) and its API definition
(/api.json) say:

- The API is POST /api/<endpoint>, JSON in and out (another type of body is 415); GET is 405; a path without an
  endpoint is 404 (fastify's own answer; UNKNOWN_API_ENDPOINT for a GET). The token is "Authorization: Bearer <token>"
  or "i" in the body: an unknown one is 401 AUTHENTICATION_FAILED (403 with `old_auth`, as misskey.io's /api.json
  says), none 401 CREDENTIAL_REQUIRED, one without the endpoint's permission 403 PERMISSION_DENIED, a suspended
  account's 403 YOUR_ACCOUNT_SUSPENDED. Errors are {"error": {"message", "code", "id", "kind"}}; parameters that don't
  fit the endpoint's definition are 400 INVALID_PARAM (ids are letters and digits).
- drive/folders and drive/files list the folderId (none or null: the root) of the token's account, `limit` (1 to 100,
  default 10) at a time, by id from the newest, below untilId (above sinceId); they don't check the folder.
  drive/folders/show gives one folder (NO_SUCH_FOLDER).
- drive/folders/create (parentId: NO_SUCH_FOLDER; a name of more than 128 characters fails in the database: 500),
  drive/folders/update (name, parentId: NO_SUCH_PARENT_FOLDER, RECURSIVE_NESTING), drive/folders/delete (empty ones
  only: HAS_CHILD_FILES_OR_FOLDERS), drive/files/update (name: INVALID_FILE_NAME; folderId: NO_SUCH_FOLDER),
  drive/files/delete (for good). NO_SUCH_FILE and NO_SUCH_FOLDER for ids the account hasn't. Names needn't be unique.
- drive/files/create takes multipart/form-data: the first file part, and the fields before it; booleans are read as
  JSON. A file over `max_file_size` (the server's maxFileSize) is 413 without a body. The name (or the file part's
  filename) is trimmed, "" and "blob" are none ("untitled"), others are checked (INVALID_FILE_NAME); then it gets the
  extension of the type found in the content (a few magic numbers; a .txt, .csv or .json name for what has none) when
  it has none or another one (".unknown" for no type). Without force=true the answer is a file of the account with the
  same MD5. The account's role (moderators have none): `uploadableFileTypes` (UNALLOWED_FILE_TYPE), `maxFileSizeMb`
  (MAX_FILE_SIZE_EXCEEDED, 413), `driveCapacityMb` (NO_FREE_SPACE). A folderId the account hasn't is a 500, as in
  Misskey.
- GET /files/<key> sends a file the server keeps itself to anyone (Content-Disposition inline with its name, Range:
  206); one kept in object storage is 204 there, an unknown key 404 with a picture, a query string a 301 to the path
  without it.

Accounts get a role that takes every type and files up to 1 GiB, and no rate limits, unless add_user(policies=...)
says otherwise; MISSKEY_DEFAULTS are Misskey's own.

Knobs: rate_limits ({endpoint: calls allowed}, then 429 RATE_LIMIT_EXCEEDED with Retry-After), max_file_size,
object_storage (new files' urls are /bucket/<key>, served with the name they were uploaded with, as an object
storage), redirect_files (/files/<key> redirects to /bucket/<key>), fail_status (every API call answers it, as a
proxy), old_auth. Records: calls, each with the status and the error code of its answer.
"""

from __future__ import annotations

import dataclasses
import datetime
import hashlib
import itertools
import json
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

MIB = 1024 * 1024
TIME2000 = 946684800000  # ms: ids count from 2000-01-01
DEFAULT_MAX_FILE_SIZE = 262144000  # the server's maxFileSize
MISSKEY_DEFAULTS = {"driveCapacityMb": 100, "maxFileSizeMb": 30,
                    "uploadableFileTypes": ["text/*", "application/json", "image/*", "video/*", "audio/*"]}
GENEROUS = {"driveCapacityMb": 1024 * 1024, "maxFileSizeMb": 1024, "uploadableFileTypes": ["*/*"]}
_ID = re.compile(r"[a-zA-Z0-9]+")
_EXTENSION = re.compile(r"\.[0-9a-zA-Z]+\Z")
COMPRESSED = (".gz", ".tar", ".tgz", ".bz2", ".xz", ".zip", ".7z")  # extensions Misskey leaves alone
MAGIC = ((b"\x89PNG\r\n\x1a\n", "image/png", "png"), (b"\xff\xd8\xff", "image/jpeg", "jpg"), (b"GIF8", "image/gif", "gif"),
         (b"%PDF-", "application/pdf", "pdf"), (b"PK\x03\x04", "application/zip", "zip"))
BY_NAME = {"txt": ("text/plain", "txt"), "csv": ("text/csv", "csv"), "json": ("application/json", "json")}
BROWSER_SAFE = ("image/png", "image/gif", "image/jpeg", "text/plain", "text/csv", "application/json")
PLACEHOLDER = b"\x89PNG\r\n\x1a\nplaceholder"

ENDPOINTS = {  # endpoint: (handler, the permission it needs)
    "drive/folders": ("_list_folders", "read:drive"),
    "drive/files": ("_list_files", "read:drive"),
    "drive/folders/show": ("_show_folder", "read:drive"),
    "drive/folders/create": ("_create_folder", "write:drive"),
    "drive/folders/update": ("_update_folder", "write:drive"),
    "drive/folders/delete": ("_delete_folder", "write:drive"),
    "drive/files/create": ("_create_file", "write:drive"),
    "drive/files/update": ("_update_file", "write:drive"),
    "drive/files/delete": ("_delete_file", "write:drive"),
}

MESSAGES = {
    "INVALID_PARAM": "Invalid param.",
    "CREDENTIAL_REQUIRED": "Credential required.",
    "AUTHENTICATION_FAILED": "Authentication failed. Please ensure your token is correct.",
    "PERMISSION_DENIED": "Your app does not have the necessary permissions to use this endpoint.",
    "YOUR_ACCOUNT_SUSPENDED": "Your account has been suspended.",
    "YOUR_ACCOUNT_MOVED": "You have moved your account.",
    "RATE_LIMIT_EXCEEDED": "Rate limit exceeded. Please try again later.",
    "INTERNAL_ERROR": "Internal error occurred. Please contact us if the error persists.",
    "NO_SUCH_FILE": "No such file.",
    "NO_SUCH_FOLDER": "No such folder.",
    "NO_SUCH_PARENT_FOLDER": "No such parent folder.",
    "ACCESS_DENIED": "Access denied.",
    "INVALID_FILE_NAME": "Invalid file name.",
    "RECURSIVE_NESTING": "It can not be structured like nesting folders recursively.",
    "HAS_CHILD_FILES_OR_FOLDERS": "This folder has child files or folders.",
    "NO_FREE_SPACE": "Cannot upload the file because you have no free space of drive.",
    "MAX_FILE_SIZE_EXCEEDED": "Cannot upload the file because it exceeds the maximum file size.",
    "UNALLOWED_FILE_TYPE": "Cannot upload the file because it is an unallowed file type.",
}


def error(status: int, code: str, kind: str = "client", headers: dict[str, str] | None = None) -> HTTPError:
    """An error answer of the API (the id is made up: each endpoint has its own)."""
    body = {"error": {"message": MESSAGES[code], "code": code, "id": hashlib.md5(code.encode()).hexdigest(), "kind": kind}}
    failure = HTTPError(status, body, headers)
    failure.code = code
    return failure


def invalid() -> HTTPError:
    return error(400, "INVALID_PARAM")


def valid_file_name(name: str) -> bool:
    """Misskey's rule for file names: not blank, at most 200 UTF-16 code units, no \\, / or ".."."""
    return bool(name.strip()) and len(name.encode("utf-16-le")) // 2 <= 200 and not ("\\" in name or "/" in name or ".." in name)


def correct_filename(name: str, ext: str | None) -> str:
    """The name Misskey stores: with the extension of the type it found, unless the name has it already (or one of
    the same type, or the type is unknown, or compressed)."""
    wanted = "." + ext if ext else ".unknown"
    match = _EXTENSION.search(name)
    if match is None:
        return name + wanted
    have = match.group(0).lower()
    same = have == wanted or (wanted, have) in ((".jpg", ".jpeg"), (".tif", ".tiff"), (".exe", ".dll"))
    return name if ext is None or same or wanted in COMPRESSED else name + wanted


def detect(data: bytes, name: str | None) -> tuple[str, str | None]:
    """(MIME type, extension) of the content, as Misskey finds them."""
    if data:
        for magic, mime, ext in MAGIC:
            if data.startswith(magic):
                return mime, ext
    if data and name is not None and name.split(".")[-1] in BY_NAME:
        return BY_NAME[name.split(".")[-1]]
    return "application/octet-stream", None


def allowed(mime: str, patterns: list[str]) -> bool:
    return any(p in ("*", "*/*") or (p.endswith("/*") and mime.startswith(p[:-1])) or mime == p for p in patterns)


def multipart(body: bytes, content_type: str) -> tuple[dict[str, str], tuple[str, bytes] | None, list[str]] | None:
    """The fields before the first file part, that file (filename, bytes), and the names of all parts in order; None
    when the body isn't multipart/form-data."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type)
    if not content_type.lower().startswith("multipart/form-data") or boundary is None:
        return None
    pieces = body.split(b"--" + boundary.group(1).encode())
    if len(pieces) < 3 or not pieces[-1].startswith(b"--"):
        return None
    fields: dict[str, str] = {}
    file: tuple[str, bytes] | None = None
    names: list[str] = []
    for piece in pieces[1:-1]:
        head, found, value = piece[2:].partition(b"\r\n\r\n")
        if not found or not value.endswith(b"\r\n"):
            return None
        value = value[:-2]
        disposition = head.decode("utf-8")
        name = re.search(r'\bname="([^"]*)"', disposition)
        filename = re.search(r'\bfilename="([^"]*)"', disposition)
        names.append(name.group(1) if name else "")
        if filename is not None:
            file = file or (filename.group(1), value)
        elif name is not None and file is None:
            fields[name.group(1)] = value.decode("utf-8")
    return fields, file, names


@dataclasses.dataclass
class User:
    id: str
    policies: dict[str, Any]
    moderator: bool = False
    suspended: bool = False
    moved: bool = False


@dataclasses.dataclass
class Folder:
    id: str
    user: str
    name: str
    parent: str | None


@dataclasses.dataclass
class File:
    id: str
    user: str
    folder: str | None
    name: str
    data: bytes
    type: str
    key: str  # the access key: /files/<key>, or /bucket/<key> in object storage
    url: str
    stored_name: str  # the name it was uploaded with, which object storage keeps

    @property
    def md5(self) -> str:
        return hashlib.md5(self.data).hexdigest()


class FakeMisskey:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeMisskey:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.users: dict[str, User] = {}
            self.tokens: dict[str, tuple[str, tuple[str, ...]]] = {}  # token -> (user id, permissions)
            self.folders: dict[str, Folder] = {}
            self.files: dict[str, File] = {}
            self.counter = itertools.count(1)
            self.last_ms = 0
            self.rate_limits: dict[str, int] = {}
            self.counts: dict[tuple[str, str], int] = {}
            self.max_file_size = DEFAULT_MAX_FILE_SIZE
            self.object_storage = False
            self.redirect_files = False
            self.fail_status = 0
            self.old_auth = False
            self.calls: list[dict[str, Any]] = []

    # -- for tests

    def add_user(self, *, policies: dict[str, Any] | None = None, moderator: bool = False) -> tuple[str, str]:
        """An account, and an access token of it with read:drive and write:drive (Settings → Service integration)."""
        with self.lock:
            user = User(self._new_id(), dict(policies or GENEROUS), moderator)
            self.users[user.id] = user
            return user.id, self.add_token(user.id, ("read:drive", "write:drive"))

    def add_token(self, user: str, permissions: tuple[str, ...]) -> str:
        with self.lock:
            token = secrets.token_urlsafe(24).replace("-", "a").replace("_", "b")[:32]
            self.tokens[token] = (user, tuple(permissions))
            return token

    def revoke(self, token: str) -> None:
        """The token deleted under Settings → Manage access tokens."""
        with self.lock:
            self.tokens.pop(token, None)

    def find(self, user: str, path: str) -> Folder | File | None:
        """The folder or file at `path` of the account's drive (the first of a name; folders first); None for "/"."""
        with self.lock:
            parent: str | None = None
            found: Folder | File | None = None
            for name in [part for part in path.split("/") if part]:
                if found is not None and isinstance(found, File):
                    return None
                found = self._child_folder(user, parent, name) or next(
                    (f for f in self.files.values() if f.user == user and f.folder == parent and f.name == name), None)
                if found is None:
                    return None
                parent = found.id
            return found

    def mkdirs(self, user: str, path: str) -> str | None:
        """The id of the folder at `path` (None: the root), made with those above it, as Misskey's own UI would."""
        with self.lock:
            parent: str | None = None
            for name in [part for part in path.split("/") if part]:
                folder = self._child_folder(user, parent, name) or self._add_folder(user, parent, name)
                parent = folder.id
            return parent

    def put(self, user: str, path: str, data: bytes, *, name: str | None = None) -> File:
        """A file at `path`, or named `name` (any name) in the folder `path`, as it is; another of the name may be there."""
        with self.lock:
            folder, _, last = path.rpartition("/") if name is None else (path, "", name)
            return self._add_file(user, self.mkdirs(user, folder), last, data, last)

    def names(self, user: str, path: str) -> list[str]:
        with self.lock:
            folder = self.find(user, path) if path != "/" else None
            parent = folder.id if folder is not None else None
            return sorted([f.name for f in self.folders.values() if f.user == user and f.parent == parent]
                          + [f.name for f in self.files.values() if f.user == user and f.folder == parent])

    def requests(self, endpoint: str) -> list[dict[str, Any]]:
        """The calls of the API's `endpoint` (a regular expression)."""
        with self.lock:
            return [call for call in self.calls if re.fullmatch("/api/" + endpoint, call["path"])]

    # -- the drive

    def _new_id(self) -> str:
        """An id as Misskey's default (aidx) makes: the time in base 36, a node, a counter; later ones sort after."""
        self.last_ms = max(self.last_ms, int(time.time() * 1000))
        return _base36(self.last_ms - TIME2000, 8) + "fake" + _base36(next(self.counter), 4)

    def _add_folder(self, user: str, parent: str | None, name: str) -> Folder:
        folder = Folder(self._new_id(), user, name, parent)
        self.folders[folder.id] = folder
        return folder

    def _add_file(self, user: str, folder: str | None, name: str, data: bytes, stored_name: str, mime: str | None = None) -> File:
        key = secrets.token_hex(16)
        url = f"{self.url}/bucket/{key}" if self.object_storage else f"{self.url}/files/{key}"
        file = File(self._new_id(), user, folder, name, data, mime or detect(data, name)[0], key, url, stored_name)
        self.files[file.id] = file
        return file

    def _child_folder(self, user: str, parent: str | None, name: str) -> Folder | None:
        return next((f for f in self.folders.values() if f.user == user and f.parent == parent and f.name == name), None)

    def _inside(self, folder: Folder, root: Folder) -> bool:
        """Whether `folder` is `root` or below it."""
        current: Folder | None = folder
        while current is not None:
            if current.id == root.id:
                return True
            current = self.folders.get(current.parent) if current.parent else None
        return False

    @staticmethod
    def _created(item_id: str) -> str:
        moment = datetime.datetime.fromtimestamp((int(item_id[:8], 36) + TIME2000) / 1000, datetime.timezone.utc)
        return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03}Z"

    def _pack_folder(self, folder: Folder, *, detail: bool = False) -> dict[str, Any]:
        packed: dict[str, Any] = {"id": folder.id, "createdAt": self._created(folder.id), "name": folder.name,
                                  "parentId": folder.parent}
        if detail:
            packed["foldersCount"] = sum(1 for f in self.folders.values() if f.parent == folder.id)
            packed["filesCount"] = sum(1 for f in self.files.values() if f.folder == folder.id)
        return packed

    def _pack_file(self, file: File) -> dict[str, Any]:
        """A DriveFile as the account's own calls give it (self: the original's url)."""
        return {"id": file.id, "createdAt": self._created(file.id), "name": file.name, "type": file.type,
                "md5": file.md5, "size": len(file.data), "isSensitive": False, "blurhash": None, "properties": {},
                "url": file.url, "thumbnailUrl": None, "comment": None, "folderId": file.folder, "folder": None,
                "userId": None, "user": None}

    # -- requests

    def handle(self, req: Request) -> Response:
        with self.lock:
            fields = multipart(req.body, req.header("Content-Type") or "")
            call = {
                "method": req.method, "path": urllib.parse.unquote(req.path), "target": req.target,
                "authorization": req.header("Authorization"), "cookie": req.header("Cookie"),
                "content_type": req.header("Content-Type"), "content_length": req.header("Content-Length"),
                "transfer_encoding": req.header("Transfer-Encoding"), "range": req.header("Range"),
                "accept_encoding": req.header("Accept-Encoding"), "size": len(req.body), "json": self._json_of(req),
                "fields": fields[0] if fields else None, "parts": fields[2] if fields else None}
            self.calls.append(call)
            try:
                response = self._answer(req, fields)
            except HTTPError as failure:
                call["status"], call["answer"] = failure.response.status, getattr(failure, "code", None)
                raise
            call["status"], call["answer"] = response.status, None
            return response

    def _answer(self, req: Request, fields) -> Response:
        if req.path.startswith("/files/") or req.path.startswith("/bucket/"):
            return self._serve(req)
        if not req.path.startswith("/api/"):  # the web client, for every path it routes
            return Response(200, b"<!DOCTYPE html><title>Misskey</title>", {"Content-Type": "text/html; charset=utf-8"})
        if self.fail_status:
            return Response(self.fail_status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
        endpoint = req.path[len("/api/"):]
        if endpoint not in ENDPOINTS:
            if req.method == "GET":
                return json_response({"error": {"message": "Unknown API endpoint.", "code": "UNKNOWN_API_ENDPOINT",
                                                "id": "2ca3b769-540a-4f08-9dd5-b5a825b6d0f1", "kind": "client"}}, 404)
            return json_response({"message": f"Route {req.method}:{req.path} not found", "error": "Not Found",
                                  "statusCode": 404}, 404)
        if req.method != "POST":
            return Response(405)
        handler, permission = ENDPOINTS[endpoint]
        if endpoint == "drive/files/create":
            return self._create_file(req, endpoint, permission, fields)
        body = self._body(req)
        user = self._user(req.bearer() if (req.header("Authorization") or "").startswith("Bearer ") else body.get("i"),
                          endpoint, permission)
        return getattr(self, handler)(user, body)

    @staticmethod
    def _json_of(req: Request) -> Any:
        if not (req.header("Content-Type") or "").startswith("application/json"):
            return None
        try:
            return json.loads(req.body)
        except ValueError:
            return None

    @staticmethod
    def _body(req: Request) -> dict[str, Any]:
        """fastify's parsing of a JSON body."""
        if not req.body:
            return {}
        if (req.header("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
            raise HTTPError(415, {"statusCode": 415, "code": "FST_ERR_CTP_INVALID_MEDIA_TYPE", "error": "Unsupported Media Type",
                                  "message": "Unsupported Media Type"})
        try:
            body = json.loads(req.body)
        except ValueError:
            raise HTTPError(400, {"statusCode": 400, "code": "FST_ERR_CTP_INVALID_JSON_BODY", "error": "Bad Request",
                                  "message": "Body is not valid JSON"}) from None
        if not isinstance(body, dict):
            raise invalid()
        return body

    def _user(self, token: Any, endpoint: str, permission: str) -> User:
        """Who calls (AuthenticateService), then ApiCallService's checks: rate limit, credential, account, permission."""
        if token is not None and not isinstance(token, str):
            raise HTTPError(400, b"")
        user, permissions = None, ()
        if token is not None:
            found = self.tokens.get(token)
            if found is None:
                raise error(403 if self.old_auth else 401, "AUTHENTICATION_FAILED", "permission" if self.old_auth else "client",
                            {"WWW-Authenticate": 'Bearer realm="Misskey", error="invalid_token"'})
            user, permissions = self.users[found[0]], found[1]
        limit = self.rate_limits.get(endpoint)
        if limit is not None and user is not None:
            used = self.counts.get((user.id, endpoint), 0)
            if used >= limit:
                raise error(429, "RATE_LIMIT_EXCEEDED", headers={"Retry-After": "1800"})
            self.counts[(user.id, endpoint)] = used + 1
        if user is None:
            raise error(401, "CREDENTIAL_REQUIRED")
        if user.suspended:
            raise error(403, "YOUR_ACCOUNT_SUSPENDED", "permission")
        if user.moved and endpoint == "drive/files/create":  # prohibitMoved
            raise error(403, "YOUR_ACCOUNT_MOVED", "permission")
        if permission not in permissions:
            raise error(403, "PERMISSION_DENIED", "permission")
        return user

    @staticmethod
    def _id(body: dict[str, Any], key: str, *, nullable: bool = False, required: bool = False) -> str | None:
        """A parameter of format misskey:id."""
        if key not in body:
            if required:
                raise invalid()
            return None
        value = body[key]
        if value is None and nullable:
            return None
        if not isinstance(value, str) or not _ID.fullmatch(value):
            raise invalid()
        return value

    @staticmethod
    def _name(body: dict[str, Any], key: str = "name", *, default: str | None = None) -> str | None:
        """A string of at most 200 characters (as Ajv counts them: code points)."""
        value = body.get(key, default)
        if value is not None and (not isinstance(value, str) or len(value) > 200):
            raise invalid()
        return value

    def _folder_of(self, user: User, folder_id: str | None, code: str = "NO_SUCH_FOLDER") -> Folder | None:
        if folder_id is None:
            return None
        folder = self.folders.get(folder_id)
        if folder is None or folder.user != user.id:
            raise error(400, code)
        return folder

    def _file_of(self, user: User, body: dict[str, Any]) -> File:
        file = self.files.get(self._id(body, "fileId", required=True))
        if file is None:
            raise error(400, "NO_SUCH_FILE")
        if file.user != user.id and not user.moderator:
            raise error(400, "ACCESS_DENIED")
        return file

    @staticmethod
    def _page(items: list[Any], body: dict[str, Any]) -> list[Any]:
        """makePaginationQuery: by id, from the newest, below untilId; from the oldest above sinceId."""
        limit = body.get("limit", 10)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise invalid()
        since = FakeMisskey._id(body, "sinceId")
        until = FakeMisskey._id(body, "untilId")
        items = sorted(items, key=lambda item: item.id, reverse=since is None or until is not None)
        if since is not None:
            items = [item for item in items if item.id > since]
        if until is not None:
            items = [item for item in items if item.id < until]
        return items[:limit]

    def _list_folders(self, user: User, body: dict[str, Any]) -> Response:
        parent = self._id(body, "folderId", nullable=True)
        folders = [f for f in self.folders.values() if f.user == user.id and f.parent == parent]
        return json_response([self._pack_folder(f) for f in self._page(folders, body)])

    def _list_files(self, user: User, body: dict[str, Any]) -> Response:
        folder = self._id(body, "folderId", nullable=True)
        files = [f for f in self.files.values() if f.user == user.id and f.folder == folder]
        return json_response([self._pack_file(f) for f in self._page(files, body)])

    def _show_folder(self, user: User, body: dict[str, Any]) -> Response:
        folder = self._folder_of(user, self._id(body, "folderId", required=True))
        return json_response(self._pack_folder(folder, detail=True))

    def _create_folder(self, user: User, body: dict[str, Any]) -> Response:
        name = self._name(body, default="Untitled")
        parent = self._folder_of(user, self._id(body, "parentId", nullable=True))
        if len(name) > 128:  # the database's column
            raise error(500, "INTERNAL_ERROR", "server")
        return json_response(self._pack_folder(self._add_folder(user.id, parent.id if parent else None, name)))

    def _update_folder(self, user: User, body: dict[str, Any]) -> Response:
        name = self._name(body)
        folder = self._folder_of(user, self._id(body, "folderId", required=True))
        parent_id = folder.parent
        if "parentId" in body:
            target = self._id(body, "parentId", nullable=True)
            if target == folder.id:
                raise error(400, "RECURSIVE_NESTING")
            parent = self._folder_of(user, target, "NO_SUCH_PARENT_FOLDER")
            if parent is not None and self._inside(parent, folder):
                raise error(400, "RECURSIVE_NESTING")
            parent_id = parent.id if parent else None
        if name and len(name) > 128:
            raise error(500, "INTERNAL_ERROR", "server")
        if name:
            folder.name = name
        folder.parent = parent_id
        return json_response(self._pack_folder(folder))

    def _delete_folder(self, user: User, body: dict[str, Any]) -> Response:
        folder = self._folder_of(user, self._id(body, "folderId", required=True))
        if any(f.parent == folder.id for f in self.folders.values()) or any(f.folder == folder.id for f in self.files.values()):
            raise error(400, "HAS_CHILD_FILES_OR_FOLDERS")
        del self.folders[folder.id]
        return Response(204)

    def _update_file(self, user: User, body: dict[str, Any]) -> Response:
        name = body.get("name")
        if name is not None and not isinstance(name, str):
            raise invalid()
        folder_id = self._id(body, "folderId", nullable=True)
        file = self._file_of(user, body)
        if name is not None and not valid_file_name(name):
            raise error(400, "INVALID_FILE_NAME")
        if folder_id is not None:
            self._folder_of(user, folder_id)
        if name is not None:
            file.name = name
        if "folderId" in body:
            file.folder = folder_id
        return json_response(self._pack_file(file))

    def _delete_file(self, user: User, body: dict[str, Any]) -> Response:
        file = self._file_of(user, body)
        del self.files[file.id]
        return Response(204)

    def _create_file(self, req: Request, endpoint: str, permission: str, parsed) -> Response:
        """handleMultipartRequest, then the endpoint and DriveService.addFile."""
        if parsed is None or parsed[1] is None:
            return Response(400)
        fields, (filename, data), _ = parsed
        if len(data) > self.max_file_size:
            return Response(413)  # truncated by @fastify/multipart
        bearer = req.bearer() if (req.header("Authorization") or "").startswith("Bearer ") else None
        user = self._user(bearer if bearer is not None else fields.get("i"), endpoint, permission)
        params: dict[str, Any] = dict(fields)
        for key in ("isSensitive", "force"):  # read as JSON
            if key in params:
                try:
                    params[key] = json.loads(params[key])
                except ValueError:
                    raise invalid() from None
                if not isinstance(params[key], bool):
                    raise invalid()
        folder_id = self._id(params, "folderId", nullable=True)
        name = params.get("name") if params.get("name") is not None else filename
        if name is not None:
            name = name.strip()
            if name in ("", "blob"):
                name = None
            elif not valid_file_name(name):
                raise error(400, "INVALID_FILE_NAME")
        mime, ext = detect(data, name)
        stored = correct_filename(name if name is not None else "untitled", ext)
        if not params.get("force", False):
            same = next((f for f in self.files.values() if f.user == user.id and f.md5 == hashlib.md5(data).hexdigest()), None)
            if same is not None:
                return json_response(self._pack_file(same))
        if not user.moderator:
            policies = user.policies
            if not allowed(mime, policies["uploadableFileTypes"]):
                raise error(400, "UNALLOWED_FILE_TYPE")
            if len(data) > policies["maxFileSizeMb"] * MIB:
                raise error(413, "MAX_FILE_SIZE_EXCEEDED")
            used = sum(len(f.data) for f in self.files.values() if f.user == user.id)
            if used + len(data) > policies["driveCapacityMb"] * MIB:
                raise error(400, "NO_FREE_SPACE")
        if folder_id is not None:
            folder = self.folders.get(folder_id)
            if folder is None or folder.user != user.id:
                raise error(500, "INTERNAL_ERROR", "server")  # Error('folder-not-found'), which nothing maps
        return json_response(self._pack_file(self._add_file(user.id, folder_id, stored, data, stored, mime)))

    # -- the files

    def _serve(self, req: Request) -> Response:
        """The file server (/files/<key>, the files the server keeps itself) and object storage (/bucket/<key>, any
        file, as the target of redirect_files too): no login."""
        if req.raw_query:
            return Response(301, b"", {"Location": req.path})
        bucket, key = req.path.startswith("/bucket/"), req.path.rpartition("/")[2]
        file = next((f for f in self.files.values() if f.key == key), None)
        if file is None:
            return Response(404, PLACEHOLDER, {"Content-Type": "image/png", "Cache-Control": "max-age=86400"})
        if not bucket and file.url.startswith(f"{self.url}/bucket/"):
            return Response(204, b"", {"Cache-Control": "max-age=86400"})  # not stored here: "unavailable"
        if not bucket and self.redirect_files:
            return Response(302, b"", {"Location": f"/bucket/{key}"})
        name = file.stored_name if bucket else file.name
        headers = {"Content-Type": file.type if file.type in BROWSER_SAFE else "application/octet-stream",
                   "Cache-Control": "max-age=31536000, immutable",
                   "Content-Disposition": f"inline; filename*=UTF-8''{urllib.parse.quote(name, safe='')}"}
        if not file.data:  # no Range for an empty file
            return Response(200, b"", {**headers, "Accept-Ranges": "bytes"})
        return file_response(file.data, req.header("Range"), headers)


def _base36(number: int, width: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    text = ""
    while number:
        number, rest = divmod(number, 36)
        text = digits[rest] + text
    return text.rjust(width, "0")[-width:]
