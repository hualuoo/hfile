"""An in-memory fake of a Cloudreve v3 server (apps.storages.drivers.cloudreve_v3), on FakeServer.

    fake = FakeCloudreveV3().start()
    fake.add_user("alice@example.com", "pw-alice")
    fake.url
    fake.close()

It answers as Cloudreve 3.8.3's source says (github.com/cloudreve/cloudreve; the files are named in the driver's
docstring):

- Every API answer is {"code", "data", "msg"} with HTTP 200, whatever the code. Other /api paths are gin's 404 "404
  page not found", GET /api/v3/directory gin's 301 to /api/v3/directory/, a path with a bad percent escape Go's 400;
  the rest is the frontend's index.html. Routes match the decoded path.
- POST /api/v3/user/session takes {userName, Password}: 40026 while `login_captcha` (checked first); 40001 for an email
  that isn't one or a password not of 4 to 64 characters; 40020 for a wrong email or password; 40017 / 40018 for a
  banned or inactive account; 203, and a cookie of no login, for an account with two_factor; else the user and
  Set-Cookie: cloudreve-session=... The other routes but site/ping need that cookie, of an active account: 401 "Login
  required" otherwise. expire_sessions() forgets them all, as a restart without the cache file does.
- GET /api/v3/directory/<path> lists a folder: its folders, then its files but placeholders, and the policy; 40016 for
  a missing path or a file. Folders and files are apart: a folder and a file may share a name.
- PUT /api/v3/directory makes the folders of a path, drops the spaces at the end of their names, keeps one already
  there without an error, and answers 40004 when a file has the name.
- PATCH /api/v3/object moves what is in src_dir (the rest silently stays): 40004 for a name its kind has in dst, or a
  folder moved into itself; below itself it goes, making a cycle, as in Cloudreve. POST /api/v3/object/copy copies one
  thing, 40016 for a name its kind has in dst; copies share the stored data. POST /api/v3/object/rename checks the name
  (a file's extension too, against the policy's file_type) and its kind's names (40004). DELETE /api/v3/object deletes
  for good; with `delete_fails` stored data can't be deleted: 203, and those files stay.
- PUT /api/v3/file/upload (not before version 3.5) checks the policy id (40006), size (40049), name (40052), extension
  (40050) and space (40051), then makes the folders above and a placeholder: 40004 when a file has the name, 40054 when
  it is a placeholder. POST /api/v3/file/upload/<session>/<index> takes the local policy's chunks only (40006), in
  order (40012), with their exact Content-Length (40013); the last makes the file. DELETE /api/v3/file/upload/<session>
  deletes the placeholder. POST /api/v3/file/create makes an empty file (no space check); PUT /api/v3/file/update/<id>
  replaces a file's data, a copy sharing it keeping the old data.
- PUT /api/v3/file/download/<id> gives, for the local policy, /api/v3/file/download/<session>?sign=... without the
  address (after the policy's base_url when it has one), valid `download_ttl` seconds; for the other policies (the one
  a file was stored with) <url>/storage/<key>?expires=...&sig=..., like a presigned URL. The download route takes no
  cookie and checks the sign (40020 as JSON with HTTP 200); a session gone is {"code": 404}; with `one_time` a session
  works once. Files come with Range and Content-Disposition.
- With `body_max`, larger requests get a 413 (as from nginx). FakeCloudreveV3("4.19.1") is a Cloudreve v4 site.

Knobs: login_captcha, capacity, policy (type, max_size, file_type, chunk_size, base_url), download_ttl, one_time,
refuse_signs, delete_fails, fail_status, fail_chunk, fail_update, body_max. Users: add_user(), ban(),
expire_sessions(). Records: calls (paths decoded), logins, downloads (requests of download links).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import email.utils
import hashlib
import hmac
import itertools
import json
import os
import re
import secrets
import string
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, Request, Response, file_response, json_response

RESERVED = '\\?*<":>/|'
OFFSET = datetime.timezone(datetime.timedelta(hours=8))  # Cloudreve's times carry the server's offset
INDEX_HTML = b"<!doctype html><html><head><title>Cloudreve</title></head><body></body></html>"
BAD_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
EMAIL = re.compile(r"[^@\s]+@[^@\s]+")
COOKIE = "cloudreve-session"


class Fail(Exception):
    """An error answer."""

    def __init__(self, code: int, msg: str = ""):
        super().__init__(code)
        self.code, self.msg = code, msg

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "msg": self.msg}


def answer(data: Any = None, code: int = 0, msg: str = "") -> Response:
    body: dict[str, Any] = {"code": code, "msg": msg}
    if data is not None:
        body["data"] = data
    return json_response(body)


def iso(moment: float) -> str:
    """A time as Go writes it (RFC 3339 with nanoseconds)."""
    stamp = datetime.datetime.fromtimestamp(moment, OFFSET)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{stamp.microsecond:06d}123+08:00"


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclasses.dataclass
class User:
    id: int
    email: str
    password: str
    root: int
    status: str = "active"  # active | banned | inactive
    two_factor: bool = False


@dataclasses.dataclass
class Folder:
    id: int
    name: str
    parent: int | None
    owner: int
    created: float
    updated: float


@dataclasses.dataclass
class File:
    id: int
    name: str
    folder: int
    owner: int
    size: int
    source: str  # where its data is stored; copies share it
    policy: str  # the type of the policy it was stored with
    created: float
    updated: float
    upload: str | None = None  # the upload session while it is a placeholder


class FakeCloudreveV3:
    def __init__(self, version: str = "3.8.3"):
        self.version = version
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.secret = secrets.token_bytes(16)
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeCloudreveV3:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.users: dict[int, User] = {}
            self.folders: dict[int, Folder] = {}
            self.files: dict[int, File] = {}
            self.blobs: dict[str, bytes] = {}
            self.sessions: dict[str, int] = {}  # cookie -> user id
            self.uploads: dict[str, dict[str, Any]] = {}  # upload session -> what it is for
            self.download_sessions: dict[str, dict[str, Any]] = {}
            self.hashes: dict[str, tuple[str, int]] = {}
            self.hash_of: dict[tuple[str, int], str] = {}
            self.ids = itertools.count(10)
            self.login_captcha = False
            self.capacity: int | None = None
            self.policy: dict[str, Any] = {"type": "local", "max_size": 0, "file_type": [],
                                           "chunk_size": 25 * 1024 * 1024, "base_url": ""}
            self.download_ttl = 600
            self.one_time = False
            self.refuse_signs = False
            self.delete_fails = False
            self.fail_status = 0
            self.fail_chunk: int | None = None
            self.fail_update = False
            self.body_max: int | None = None
            self.calls: list[dict[str, Any]] = []
            self.logins = 0
            self.downloads: list[dict[str, Any]] = []

    # -- for tests

    def add_user(self, email: str, password: str, *, status: str = "active", two_factor: bool = False) -> User:
        with self.lock:
            number = next(self.ids)
            root = self._new_folder("/", None, number)
            user = self.users[number] = User(number, email, password, root.id, status, two_factor)
            return user

    def user(self, email: str) -> User:
        with self.lock:
            return next(user for user in self.users.values() if user.email == email)

    def ban(self, email: str) -> None:
        with self.lock:
            self.user(email).status = "banned"

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def folder(self, email: str, path: str) -> Folder | None:
        with self.lock:
            return self._folder_at(self.user(email), path)

    def file(self, email: str, path: str) -> File | None:
        """The file at `path`, a placeholder too."""
        with self.lock:
            folder, _, name = path.rpartition("/")
            parent = self._folder_at(self.user(email), folder or "/")
            return self._child_file(parent.id, name) if parent is not None else None

    def mkdirs(self, email: str, path: str) -> Folder:
        with self.lock:
            return self._make_folders(self.user(email), path)

    def put(self, email: str, path: str, data: bytes, *, policy: str | None = None) -> File:
        """A file made as Cloudreve's own pages would, with the folders above."""
        with self.lock:
            user = self.user(email)
            folder, _, name = path.rpartition("/")
            return self._new_file(user, self._make_folders(user, folder or "/"), name, data, policy or self.policy["type"])

    def data(self, email: str, path: str) -> bytes | None:
        with self.lock:
            node = self.file(email, path)
            return None if node is None else self.blobs.get(node.source)

    def names(self, email: str, path: str) -> list[str]:
        """Everything in the folder, placeholders too; a name a folder and a file share comes twice."""
        with self.lock:
            folder = self.folder(email, path)
            return sorted([child.name for child in self._subfolders(folder.id)] + [child.name for child in self._files_in(folder.id)])

    def routes(self, method: str, path: str, *, prefix: bool = False) -> list[dict[str, Any]]:
        """The calls of a route of /api/v3 (decoded), or of the routes starting with `path`."""
        with self.lock:
            full = "/api/v3" + path
            return [call for call in self.calls if call["method"] == method
                    and (call["path"].startswith(full) if prefix else call["path"] == full)]

    # -- the tree

    def _hash(self, kind: str, number: int) -> str:
        """A hashid: opaque, and of one kind (folder, file, policy, user)."""
        key = (kind, number)
        value = self.hash_of.get(key)
        if value is None:
            value = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(6))
            self.hash_of[key], self.hashes[value] = value, key
        return value

    def _unhash(self, value: Any, kind: str) -> int | None:
        entry = self.hashes.get(value) if isinstance(value, str) else None
        return entry[1] if entry is not None and entry[0] == kind else None

    def _new_folder(self, name: str, parent: int | None, owner: int) -> Folder:
        moment = time.time()
        folder = Folder(next(self.ids), name, parent, owner, moment, moment)
        self.folders[folder.id] = folder
        return folder

    def _new_file(self, user: User, folder: Folder, name: str, data: bytes, policy: str, upload: str | None = None) -> File:
        moment = time.time()
        node = File(next(self.ids), name, folder.id, user.id, len(data), f"uploads/{user.id}/{secrets.token_hex(6)}_{name}",
                    policy, moment, moment, upload)
        self.files[node.id] = node
        self.blobs[node.source] = data
        return node

    def _subfolders(self, folder_id: int) -> list[Folder]:
        return sorted((folder for folder in self.folders.values() if folder.parent == folder_id), key=lambda folder: folder.id)

    def _files_in(self, folder_id: int) -> list[File]:
        return sorted((node for node in self.files.values() if node.folder == folder_id), key=lambda node: node.id)

    def _child_folder(self, folder_id: int, name: str) -> Folder | None:
        return next((folder for folder in self._subfolders(folder_id) if folder.name == name), None)

    def _child_file(self, folder_id: int, name: str) -> File | None:
        """GetChildFile: placeholders too."""
        return next((node for node in self._files_in(folder_id) if node.name == name), None)

    def _folder_at(self, user: User, path: str) -> Folder | None:
        """IsPathExist: folders only, "/" first; a trailing or double "/" finds nothing."""
        if not path.startswith("/"):
            return None
        folder: Folder | None = self.folders[user.root]
        if path == "/":
            return folder
        for name in path.split("/")[1:]:
            folder = self._child_folder(folder.id, name) if folder is not None else None
        return folder

    def _position(self, folder: Folder) -> str:
        names = []
        while folder.parent is not None:
            names.append(folder.name)
            folder = self.folders[folder.parent]
        return "/" + "/".join(reversed(names))

    def _below(self, folder: Folder) -> list[Folder]:
        """GetRecursiveChildFolder: the folder and those below it."""
        found = [folder]
        for child in self._subfolders(folder.id):
            found += self._below(child)
        return found

    @staticmethod
    def _legal(name: str) -> bool:
        """ValidateLegalName."""
        return bool(name) and not any(ch in RESERVED for ch in name) and len(name.encode()) < 256 and not name.endswith(" ")

    def _extension_ok(self, name: str) -> bool:
        """ValidateExtension: Go's filepath.Ext, from the last dot."""
        if not self.policy["file_type"]:
            return True
        extension = name[name.rfind("."):] if "." in name else ""
        return len(extension) > 1 and extension[1:].lower() in self.policy["file_type"]

    def _used(self, user: User) -> int:
        return sum(node.size for node in self.files.values() if node.owner == user.id)

    def _check_space(self, user: User, size: int) -> None:
        """HookValidateCapacity."""
        if self.capacity is not None and self.capacity - self._used(user) < size:
            raise Fail(40051, "Insufficient capacity")

    def _check_file(self, user: User, name: str, size: int) -> None:
        """HookValidateFile."""
        if self.policy["max_size"] and size > self.policy["max_size"]:
            raise Fail(40049, "File is too large")
        if not self._legal(name):
            raise Fail(40052, "Invalid object name")
        if not self._extension_ok(name):
            raise Fail(40050, "File type not allowed")

    def _make_folders(self, user: User, path: str) -> Folder:
        """CreateDirectory: the folders of `path`, made where missing; spaces at the end of names are dropped."""
        if path in (".", ""):
            raise Fail(40053, "Root protected")
        if not path.startswith("/"):
            raise Fail(40016, "Path not exist")
        if path == "/":
            return self.folders[user.root]
        path = "/" + "/".join(part for part in path.split("/") if part not in ("", "."))  # path.Clean
        base, _, name = path.rpartition("/")
        name = name.rstrip(" ")
        if not self._legal(name):
            raise Fail(40052, "Invalid object name")
        parent = self._folder_at(user, base or "/") or self._make_folders(user, base or "/")
        if self._child_file(parent.id, name) is not None:
            raise Fail(40004, "Object existed")
        return self._child_folder(parent.id, name) or self._new_folder(name, parent.id, user.id)

    def _forget(self, node: File) -> None:
        """A file record goes, and its data unless another file shares it."""
        del self.files[node.id]
        if not any(other.source == node.source for other in self.files.values()):
            self.blobs.pop(node.source, None)

    def _policy_view(self) -> dict[str, Any]:
        return {"id": self._hash("policy", 1), "name": "Default storage policy", "type": self.policy["type"],
                "max_size": self.policy["max_size"], "file_type": self.policy["file_type"]}

    # -- requests

    def handle(self, req: Request) -> Response:
        if self.version.startswith("4."):
            return self._v4(req)
        if req.path.startswith("/storage/"):
            return self._storage(req)
        if not req.path.startswith("/api"):
            return Response(200, INDEX_HTML, {"Content-Type": "text/html"})
        if BAD_ESCAPE.search(req.path):
            return Response(400, b"400 Bad Request", {"Content-Type": "text/plain"})
        path = urllib.parse.unquote(req.path)  # Go's URL.Path, which gin matches
        if req.method == "GET" and path.startswith("/api/v3/file/download/"):
            return self._download(req, path)
        if self.body_max is not None and len(req.body) > self.body_max:
            return Response(413, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
        with self.lock:
            self.calls.append({"method": req.method, "path": path, "query": req.raw_query, "cookie": req.header("Cookie"),
                               "length": req.header("Content-Length"), "json": self._body(req)})
            if self.fail_status:
                return Response(self.fail_status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
            if req.method == "GET" and path == "/api/v3/directory":
                return Response(301, b"", {"Location": "/api/v3/directory/"})
            handler = self._route(req.method, path)
            if handler is None:
                return Response(404, b"404 page not found", {"Content-Type": "text/plain"})
            try:
                return handler(req)
            except Fail as fail:
                return json_response(fail.body())

    def _route(self, method: str, path: str) -> Callable[[Request], Response] | None:
        uploads = tuple(int(part) for part in self.version.split(".")[:2]) >= (3, 5)
        exact: dict[tuple[str, str], Callable[..., Response]] = {
            ("PUT", "/api/v3/directory"): self._mkdir,
            ("DELETE", "/api/v3/object"): self._delete,
            ("PATCH", "/api/v3/object"): self._move,
            ("POST", "/api/v3/object/copy"): self._copy,
            ("POST", "/api/v3/object/rename"): self._rename,
            ("POST", "/api/v3/file/create"): self._create_file,
        }
        if uploads:
            exact[("PUT", "/api/v3/file/upload")] = self._create_session
        if (method, path) == ("GET", "/api/v3/site/ping"):
            return lambda req: answer(self.version)
        if (method, path) == ("POST", "/api/v3/user/session"):
            return self._login
        handler, args = exact.get((method, path)), ()
        patterns = [(r"/api/v3/directory(/.*)", "GET", self._list),
                    (r"/api/v3/file/upload/([^/]+)", "DELETE", self._cancel),
                    (r"/api/v3/file/update/([^/]+)", "PUT", self._put_content),
                    (r"/api/v3/file/download/([^/]+)", "PUT", self._download_session)]
        if uploads:
            patterns.append((r"/api/v3/file/upload/([^/]+)/(\d+)", "POST", self._chunk))
        for pattern, verb, candidate in patterns:
            match = re.fullmatch(pattern, path)
            if handler is None and match is not None and method == verb:
                handler, args = candidate, match.groups()
        if handler is None:
            return None

        def authed(req: Request) -> Response:  # AuthRequired
            user = self._user(req)
            if user is None:
                raise Fail(401, "Login required")
            return handler(req, user, *args)

        return authed

    def _v4(self, req: Request) -> Response:
        """Cloudreve 4: /api/v4 only; gin's 404 for any other /api path; the frontend for the rest."""
        if req.path == "/api/v4/site/ping":
            return json_response({"code": 0, "data": self.version, "msg": ""})
        if req.path.startswith("/api"):
            return Response(404, b"404 page not found", {"Content-Type": "text/plain"})
        return Response(200, INDEX_HTML, {"Content-Type": "text/html"})

    @staticmethod
    def _body(req: Request) -> Any:
        try:
            return json.loads(req.body.decode("utf-8")) if req.body else None
        except ValueError:
            return None

    def _json(self, req: Request) -> dict[str, Any]:
        """ShouldBindJSON: the body as JSON, whatever its Content-Type."""
        body = self._body(req)
        if not isinstance(body, dict):
            raise Fail(40001, "Parameter error")
        return body

    def _user(self, req: Request) -> User | None:
        """CurrentUser: the active account of the session cookie, or None."""
        for part in (req.header("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                user = self.users.get(self.sessions.get(value, -1))
                return user if user is not None and user.status == "active" else None
        return None

    @staticmethod
    def _ids(src: dict[str, Any], key: str) -> list[Any]:
        values = src.get(key)
        return values if isinstance(values, list) else []

    # -- login

    def _login(self, req: Request) -> Response:
        with self.lock:
            self.logins += 1
            if self.login_captcha:  # CaptchaRequired runs before the parameters are read
                raise Fail(40026, "CAPTCHA not match.")
            body = self._json(req)
            email, password = body.get("userName"), body.get("Password")
            if not isinstance(email, str) or not email:
                raise Fail(40001, "Email cannot be empty")
            if not EMAIL.fullmatch(email):
                raise Fail(40001, "Email format error")
            if not isinstance(password, str) or not password:
                raise Fail(40001, "Password cannot be empty")
            if not 4 <= len(password) <= 64:
                raise Fail(40001, "Password too short" if len(password) < 4 else "Password too long")
            user = next((user for user in self.users.values() if user.email == email), None)
            if user is None or user.password != password:
                raise Fail(40020, "Wrong password or email address")
            if user.status == "banned":
                raise Fail(40017, "This account has been blocked")
            if user.status == "inactive":
                raise Fail(40018, "This account is not activated")
            cookie = base64.urlsafe_b64encode(os.urandom(41)).decode()  # securecookie's text: base64url with "="
            if user.two_factor:
                response = answer(code=203)  # the cookie's session holds 2fa_user_id, not a login
            else:
                self.sessions[cookie] = user.id
                response = answer({"id": self._hash("user", user.id), "user_name": user.email,
                                   "nickname": user.email.split("@")[0], "status": 0, "avatar": "", "anonymous": False,
                                   "created_at": iso(time.time()), "preferred_theme": "",
                                   "group": {"id": 2, "name": "User", "webdav": True}, "tags": []})
            response.headers.append(("Set-Cookie", f"{COOKIE}={cookie}; Path=/; Max-Age=5184000; HttpOnly"))
            return response

    # -- folders and changes

    def _list(self, req: Request, user: User, path: str) -> Response:
        folder = self._folder_at(user, path)
        if folder is None:
            raise Fail(40016, "Path not exist")
        position = self._position(folder)
        objects = [{"id": self._hash("folder", child.id), "name": child.name, "path": position, "thumb": False,
                    "size": 0, "type": "dir", "date": iso(child.updated), "create_date": iso(child.created),
                    "source_enabled": False} for child in self._subfolders(folder.id)]
        objects += [{"id": self._hash("file", node.id), "name": node.name, "path": position, "thumb": False,
                     "size": node.size, "type": "file", "date": iso(node.updated), "create_date": iso(node.created),
                     "source_enabled": False} for node in self._files_in(folder.id) if node.upload is None]
        return answer({"parent": self._hash("folder", folder.id), "objects": objects, "policy": self._policy_view()})

    def _mkdir(self, req: Request, user: User) -> Response:
        path = self._json(req).get("path")
        if not isinstance(path, str) or not path:
            raise Fail(40001, "Path cannot be empty")
        self._make_folders(user, path)
        return answer()

    def _delete(self, req: Request, user: User) -> Response:
        body = self._json(req)
        folders = [self.folders[number] for number in (self._unhash(value, "folder") for value in self._ids(body, "dirs"))
                   if number in self.folders and self.folders[number].owner == user.id]
        doomed = [folder for top in folders for folder in self._below(top) if folder.parent is not None]
        files = [node for folder in doomed for node in self._files_in(folder.id)]
        files += [self.files[number] for number in (self._unhash(value, "file") for value in self._ids(body, "items"))
                  if number in self.files and self.files[number].owner == user.id and self.files[number] not in files]
        failed = 0
        for node in files:
            shared = any(other.source == node.source and other.id != node.id for other in self.files.values())
            if self.delete_fails and not shared:  # the storage couldn't delete its data: the record stays
                failed += 1
            else:
                self._forget(node)
        if failed:
            raise Fail(203, f"Failed to delete {failed} file(s).")
        for folder in doomed:
            self.folders.pop(folder.id, None)
        return answer()

    def _places(self, user: User, body: dict[str, Any]) -> tuple[Folder, Folder]:
        """The source and target folders of a move or copy."""
        source, target = body.get("src_dir"), body.get("dst")
        if not isinstance(source, str) or not source or not isinstance(target, str) or not target:
            raise Fail(40001, "Path cannot be empty")
        found = self._folder_at(user, source), self._folder_at(user, target)
        if found[0] is None or found[1] is None:
            raise Fail(40016, "Path not exist")
        return found  # type: ignore[return-value]

    def _move(self, req: Request, user: User) -> Response:
        body = self._json(req)
        source, target = self._places(user, body)
        src = _dict(body.get("src"))
        folder_ids = [self._unhash(value, "folder") for value in self._ids(src, "dirs")]
        file_ids = [self._unhash(value, "file") for value in self._ids(src, "items")]
        if target.id in folder_ids:
            raise Fail(40004, "Object existed")  # "cannot move a folder into itself"
        folders = [folder for folder in self.folders.values()
                   if folder.id in folder_ids and folder.owner == user.id and folder.parent == source.id]
        files = [node for node in self.files.values() if node.id in file_ids and node.owner == user.id and node.folder == source.id]
        for folder in folders:  # the unique index on (name, parent)
            other = self._child_folder(target.id, folder.name)
            if other is not None and other.id != folder.id:
                raise Fail(40004, "Object existed")
        for node in files:  # on (name, user, folder)
            other = self._child_file(target.id, node.name)
            if other is not None and other.id != node.id:
                raise Fail(40004, "Object existed")
        for folder in folders:
            folder.parent = target.id
        for node in files:
            node.folder = target.id
        return answer()

    def _copy(self, req: Request, user: User) -> Response:
        body = self._json(req)
        src = _dict(body.get("src"))
        if len(self._ids(src, "dirs")) + len(self._ids(src, "items")) > 1:
            raise Fail(40001, "You can only copy one object at the same time")
        source, target = self._places(user, body)
        folder_ids = [self._unhash(value, "folder") for value in self._ids(src, "dirs")]
        file_ids = [self._unhash(value, "file") for value in self._ids(src, "items")]
        top = self.folders.get(folder_ids[0]) if folder_ids else None
        if top is not None and top.owner == user.id:
            if self._child_folder(target.id, top.name) is not None:
                raise Fail(40016, "Object not exist")  # the unique index refused the first insert
            made: dict[int, int] = {}
            for folder in self._below(top):  # listed before anything is copied
                parent = target.id if folder.id == top.id else made[folder.parent]
                made[folder.id] = self._new_folder(folder.name, parent, user.id).id
            for folder_id, copy_id in list(made.items()):
                for node in self._files_in(folder_id):
                    if node.upload is None:
                        self._copy_file(node, copy_id)
        for node in [node for node in self.files.values() if node.id in file_ids and node.owner == user.id
                     and node.folder == source.id and node.upload is None]:
            if self._child_file(target.id, node.name) is not None:
                raise Fail(40016, "Object not exist")
            self._copy_file(node, target.id)
        return answer()

    def _copy_file(self, node: File, folder_id: int) -> None:
        moment = time.time()
        copy = dataclasses.replace(node, id=next(self.ids), folder=folder_id, created=moment, updated=moment)
        self.files[copy.id] = copy  # the same data

    def _rename(self, req: Request, user: User) -> Response:
        body = self._json(req)
        new, src = body.get("new_name"), _dict(body.get("src"))
        if not isinstance(new, str) or not 1 <= len(new) <= 255:
            raise Fail(40001, "new_name too long" if isinstance(new, str) and new else "new_name cannot be empty")
        if len(self._ids(src, "dirs")) + len(self._ids(src, "items")) > 1:
            raise Fail(40001, "You can only copy one object at the same time")
        file_ids = [number for number in (self._unhash(value, "file") for value in self._ids(src, "items")) if number]
        folder_ids = [number for number in (self._unhash(value, "folder") for value in self._ids(src, "dirs")) if number]
        if not self._legal(new) or (file_ids and not self._extension_ok(new)):
            raise Fail(40052, "Invalid object name")
        if file_ids:
            node = self.files.get(file_ids[0])
            if node is None or node.owner != user.id:
                raise Fail(40016, "Path not exist")
            other = self._child_file(node.folder, new)
            if other is not None and other.id != node.id:
                raise Fail(40004, "Object existed")
            node.name, node.updated = new, time.time()
            return answer()
        folder = self.folders.get(folder_ids[0]) if folder_ids else None
        if folder is None or folder.owner != user.id:
            raise Fail(40016, "Path not exist")
        other = self._child_folder(folder.parent, new)
        if other is not None and other.id != folder.id:
            raise Fail(40004, "Object existed")
        folder.name = new
        return answer()

    # -- uploads

    def _create_session(self, req: Request, user: User) -> Response:
        body = self._json(req)
        path, name, policy, size = body.get("path"), body.get("name"), body.get("policy_id"), body.get("size", 0)
        if not all(isinstance(value, str) and value for value in (path, name, policy)):
            raise Fail(40001, "Parameter error")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Fail(40001, "JSON marshall error")
        if self._unhash(policy, "policy") != 1:
            raise Fail(40035, "")
        self._check_file(user, name, size)
        self._check_space(user, size)
        folder = self._make_folders(user, path)
        existing = self._child_file(folder.id, name)
        if existing is not None:
            raise Fail(40054 if existing.upload else 40004, "Upload session existed" if existing.upload else "Object existed")
        session = str(uuid.uuid4())
        kind = self.policy["type"]
        chunk = self.policy["chunk_size"]
        self._new_file(user, folder, name, b"", kind, upload=session)
        self.uploads[session] = {"user": user.id, "size": size, "chunk": chunk, "policy": kind}
        credential: dict[str, Any] = {"sessionID": session, "chunkSize": chunk, "expires": int(time.time()) + 86400}
        if kind != "local":  # the client would send the chunks to the policy's storage
            credential["uploadURLs"] = [f"https://storage.example.com/{session}?partNumber=1"]
        return answer(credential)

    def _placeholder(self, user: User, session: str) -> File:
        node = next((node for node in self.files.values() if node.upload == session and node.owner == user.id), None)
        if node is None:
            raise Fail(40011, "")
        return node

    def _chunk(self, req: Request, user: User, session: str, index: str) -> Response:
        upload = self.uploads.get(session)
        if upload is None or upload["user"] != user.id:
            raise Fail(40011, "")
        node = self._placeholder(user, session)
        if upload["policy"] != "local":
            raise Fail(40006, "")
        number, chunk, size = int(index), upload["chunk"], upload["size"]
        if chunk == 0 and number > 0:
            raise Fail(40012, "Chunk index cannot be greater than 0")
        start = number * chunk
        if node.size < start:
            raise Fail(40012, "Chunk must be uploaded in order")
        last = chunk == 0 or (number + 1) * chunk >= size
        expected = size - start if last else chunk
        if req.header("Content-Length") != str(expected) or len(req.body) != expected:
            raise Fail(40013, f"Invalid Content-Length (expected: {expected})")
        if self.fail_chunk == number:
            raise Fail(40002, "failed to write the chunk")
        self._check_space(user, expected)
        self.blobs[node.source] = self.blobs.get(node.source, b"")[:start] + req.body
        node.size = start + expected
        if last:
            node.upload, node.updated = None, time.time()
            del self.uploads[session]
        return answer()

    def _cancel(self, req: Request, user: User, session: str) -> Response:
        self._forget(self._placeholder(user, session))
        self.uploads.pop(session, None)
        return answer()

    def _create_file(self, req: Request, user: User) -> Response:
        path = self._json(req).get("path")
        if not isinstance(path, str) or not path:
            raise Fail(40001, "Path cannot be empty")
        folder_path, _, name = path.rpartition("/")
        self._check_file(user, name, 0)
        folder = self._make_folders(user, folder_path or "/")
        existing = self._child_file(folder.id, name)
        if existing is not None:
            raise Fail(40054 if existing.upload else 40004, "Upload session existed" if existing.upload else "Object existed")
        self._new_file(user, folder, name, b"", self.policy["type"])
        return answer()

    def _put_content(self, req: Request, user: User, hashid: str) -> Response:
        number = self._unhash(hashid, "file")
        if number is None:
            raise Fail(40001, "Failed to parse object ID")
        length = req.header("Content-Length") or ""
        if not length.isdigit():
            raise Fail(40001, "Invalid content-length value")
        size = int(length)
        node = self.files.get(number)
        if node is None or node.owner != user.id:
            raise Fail(40044, "")
        self._check_file(user, node.name, size)
        if size > node.size:
            self._check_space(user, size)
        if self.fail_update:
            raise Fail(40002, "failed to upload the file to the storage")
        if any(other.source == node.source and other.id != node.id for other in self.files.values()):
            node.source = f"uploads/{user.id}/{secrets.token_hex(6)}_{node.name}"  # a copy keeps the old data
        self.blobs[node.source] = req.body[:size]
        node.size, node.updated = size, time.time()
        return answer()

    # -- downloads

    def _sign(self, path: str, expires: int) -> str:
        mac = hmac.new(self.secret, f"{path}:{expires}".encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode() + f":{expires}"

    def _download_session(self, req: Request, user: User, hashid: str) -> Response:
        number = self._unhash(hashid, "file")
        if number is None:
            raise Fail(40001, "Failed to parse object ID")
        node = self.files.get(number)
        if node is None or node.owner != user.id:
            raise Fail(40016, "Object not exist")
        expires = int(time.time()) + self.download_ttl
        if node.policy != "local":
            route = "/storage/" + urllib.parse.quote(node.source, safe="")
            return answer(f"{self.url}{route}?" + urllib.parse.urlencode({"expires": expires, "sig": self._sign(route, expires)}))
        session = "".join(secrets.choice(string.ascii_letters) for _ in range(16))
        self.download_sessions[session] = {"source": node.source, "name": node.name, "updated": node.updated,
                                           "expires": expires}
        route = f"/api/v3/file/download/{session}"
        url = route + "?" + urllib.parse.urlencode({"sign": self._sign(route, expires)})
        return answer(self.policy["base_url"].rstrip("/") + url if self.policy["base_url"] else url)

    def _record(self, req: Request) -> None:
        self.downloads.append({"path": req.path, "query": req.raw_query, "cookie": req.header("Cookie"),
                               "range": req.header("Range")})

    def _download(self, req: Request, path: str) -> Response:
        with self.lock:
            self._record(req)
            sign = req.arg("sign") or ""
            stamp = sign.rpartition(":")[2]
            if not stamp.isdigit():
                return json_response({"code": 40020, "msg": "expires timestamp missing"})
            if int(stamp) < time.time():
                return json_response({"code": 40020, "msg": "signature expired"})
            if self.refuse_signs or not hmac.compare_digest(sign, self._sign(path, int(stamp))):
                return json_response({"code": 40020, "msg": "invalid sign"})
            session = self.download_sessions.get(path.rsplit("/", 1)[1])
            if session is None or session["expires"] < time.time():
                return json_response({"code": 404, "msg": "Download session not exist"})
            if self.one_time:
                del self.download_sessions[path.rsplit("/", 1)[1]]
            data = self.blobs.get(session["source"])
            if data is None:
                return json_response({"code": -1, "msg": "open uploads/x: no such file or directory"})
        name = session["name"]
        headers = {"Content-Type": "application/json" if name.endswith(".json") else "application/octet-stream",
                   "Content-Disposition": 'attachment; filename="' + urllib.parse.quote(name, safe="$&+,:;=@") + '"',
                   "Last-Modified": email.utils.formatdate(session["updated"], usegmt=True)}
        return file_response(data, req.header("Range"), headers)

    def _storage(self, req: Request) -> Response:
        """A policy's storage, as a presigned URL of it answers."""
        with self.lock:
            self._record(req)
            stamp = req.arg("expires") or ""
            if (self.refuse_signs or not stamp.isdigit() or int(stamp) < time.time()
                    or not hmac.compare_digest(req.arg("sig") or "", self._sign(req.path, int(stamp)))):
                return Response(403, b"<Error><Code>AccessDenied</Code></Error>", {"Content-Type": "application/xml"})
            data = self.blobs.get(urllib.parse.unquote(req.path[len("/storage/"):]))
            if data is None:
                return Response(404, b"<Error><Code>NoSuchKey</Code></Error>", {"Content-Type": "application/xml"})
        return file_response(data, req.header("Range"), {"Content-Type": "application/octet-stream"})
