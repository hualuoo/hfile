"""An in-memory fake of a Cloudreve v4 server (apps.storages.drivers.cloudreve), on FakeServer.

    fake = FakeCloudreve().start()
    fake.add_user("alice@example.com", "pw-alice")
    fake.url
    fake.close()

It answers as Cloudreve's documentation (https://cloudrevev4.apifox.cn, https://docs.cloudreve.org/en/api/) and its v4
source (github.com/cloudreve/cloudreve) say:

- Every API answer is {"code", "data", "msg"} with HTTP 200. A batch call (DELETE /file, /file/move, /file/url) that
  fails for an item answers 40081 with the item's error in aggregated_error. Other /api paths are gin's 404 "404 page
  not found"; the rest is the frontend's index.html.
- POST /session/token takes {email, password}: 40026 while `login_captcha` (checked first), 40001 when the email isn't
  one or the password is shorter than 4, 40080 for a wrong email or password, 40017 / 40018 for a blocked or inactive
  account, 203 with a 2FA session id for an account with two_factor; else the user and a token pair with
  access_expires and refresh_expires (`access_ttl`, `refresh_ttl` seconds). POST /session/token/refresh gives a new
  pair, or 40020 (expired, unknown, or the password changed since).
- An Authorization header with an unknown or expired access token makes the call anonymous: 401 "Login required"
  where the account's files are needed (in aggregated_error for DELETE /file and /file/url), 40011 for a chunk.
- URIs are parsed like Go's url.Parse: the path is percent-decoded. The fake refuses (40001) a path segment with
  anything Cloudreve's PathEscape would have escaped, which catches a driver that doesn't encode names.
- GET /file lists folders before files, by cursor (next_token) or, with `cursor` off, by offset (page, total_items);
  page_size 0 is 100, more than `max_page_size` is cut down. 40016 for a missing path or a file. Its `parent` is the
  folder listed.
- GET /file/info refuses the account's root (cloudreve://my) with 403 "Not supported action", as Cloudreve's
  GetFileInfoService does (dbfs.WithNotRoot). With extended=true it adds extended_info.entities: the file's finished
  version and the one an upload is making.
- Names: 40052 for \\ / : * ? " < > |, ".", "..", or 256 bytes or more. A taken name is 40004, something below a file
  403, a path in `locked` (decoded, like "/a.txt") or with an upload going on 40073.
- DELETE /file puts things into the trash bin (Node.trashed) unless skip_soft_delete.
- POST /file/url gives /api/v4/file/content/e<entity>/0/<name>?download=true&sign=..., valid `url_ttl` seconds and
  given again until `url_margin` seconds before it expires (Cloudreve's cache); 40077 for a folder or a file without a
  blob. The content route takes no token, sends the blob with Range and, for download=true, Content-Disposition; a
  bad or expired sign (or any while `refuse_signs`) is 40020 as JSON with HTTP 200; a blob gone is a 404.
- PUT /file/upload uses the storage policy `policy` (type, relay, chunk_size, max_size): 40049 above max_size, 40051
  above `capacity`, 40004 for a folder of the name or a file of it without entity_type version. It makes the folders
  above and a placeholder file, and locks the path; the file carries the metadata sys:upload_session_id until the
  upload ends. A new file's placeholder has the upload's size and no finished version; a file being replaced keeps
  its last version until then (Cloudreve's UpgradePlaceholder). POST /file/upload/<session>/<index> takes local or
  relayed chunks (else 40006) with their exact Content-Length (40013); relay makes chunk_size 0, one chunk; the last
  finishes the file, whose old blob is dropped. DELETE /file/upload ends a session and drops a new file's placeholder.
  PUT /file/content stores up to `max_edit_size` bytes (40049) itself, whatever the policy.
- Every API answer sets Cloudreve's session cookie. With `body_max`, larger requests get a 413 (as from nginx).
- A failure that isn't one of Cloudreve's own errors (a database's, a storage's) answers {"code": -1, "msg": "",
  "correlation_id": ...} (serializer.Err): the API paths in `unexplained` answer so.
- `clock`: seconds the fake's clock is off, for the tokens' and links' times and the Date header.
- `site_url`: Cloudreve's Site URL, which its links are made with (default: the fake's own address).

Knobs: login_captcha, access_ttl, refresh_ttl, cursor, max_page_size, policy, max_edit_size, url_ttl, url_margin,
capacity, locked, refuse_signs, fail_status, fail_chunk, body_max, unexplained, clock, site_url;
FakeCloudreve(version=3) is a Cloudreve v3 site. Users: add_user(), change_password(). Tokens: expire_access_tokens(),
expire_refresh_tokens(). Other clients' uploads: begin_upload(). Records: calls, logins, refreshes, content_requests,
last_correlation.
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
import time
import urllib.parse
import uuid
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, Request, Response, file_response, json_response

SAFE = "-_.!~*'()"
SEGMENT = re.compile(r"(?:[A-Za-z0-9\-_.!~*'()]|%[0-9A-F]{2})+")  # a name as Cloudreve's PathEscape writes it
REFUSED = '\\/:*?"<>|'
OFFSET = datetime.timezone(datetime.timedelta(hours=8))  # Cloudreve's times carry the server's offset
INDEX_HTML = b"<!doctype html><html><head><title>Cloudreve</title></head><body></body></html>"


class Fail(Exception):
    """An error answer."""

    def __init__(self, code: int, msg: str = "", data: Any = None):
        super().__init__(code)
        self.code, self.msg, self.data = code, msg, data

    def body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "msg": self.msg}
        if self.data is not None:
            body["data"] = self.data
        return body


def answer(data: Any = None, code: int = 0, msg: str = "") -> Response:
    body: dict[str, Any] = {"code": code, "msg": msg}
    if data is not None:
        body["data"] = data
    return json_response(body)


def iso(moment: float) -> str:
    """A time as Go writes it (RFC 3339 with nanoseconds)."""
    stamp = datetime.datetime.fromtimestamp(moment, OFFSET)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S") + f".{stamp.microsecond:06d}123+08:00"


def quote(name: str) -> str:
    return urllib.parse.quote(name, safe=SAFE)


@dataclasses.dataclass
class User:
    email: str
    password: str
    root: int
    status: str = "active"  # active | inactive | manual_banned
    two_factor: bool = False
    generation: int = 0  # changes with the password: refresh tokens issued before are refused


@dataclasses.dataclass
class Node:
    id: int
    parent: int | None
    name: str
    folder: bool
    owner: str
    entity: int | None = None  # the blob of its current version
    created: float = 0.0
    updated: float = 0.0
    trashed: bool = False
    upload: str | None = None  # the upload session going on (metadata sys:upload_session_id)
    pending: int | None = None  # a new file's placeholder: the size its upload announced


class FakeCloudreve:
    def __init__(self, version: int = 4):
        self.version = version
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.reset()
        fake = self

        def date_time_string(handler, timestamp=None):  # the Date header, by the fake's clock
            return email.utils.formatdate((time.time() if timestamp is None else timestamp) + fake.clock, usegmt=True)

        # The handler class is this server's own (FakeServer makes one per server).
        self.server._httpd.RequestHandlerClass.date_time_string = date_time_string

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeCloudreve:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.users: dict[str, User] = {}
            self.nodes: dict[int, Node] = {}
            self.blobs: dict[int, bytes] = {}
            self.access: dict[str, tuple[str, float]] = {}  # token -> (email, expires)
            self.refresh: dict[str, tuple[str, float, int]] = {}  # token -> (email, expires, generation)
            self.sessions: dict[str, dict[str, Any]] = {}
            self.uploading: dict[str, str] = {}  # locked path -> session
            self.signs: dict[str, tuple[int, float]] = {}  # sign -> (entity, expires)
            self.url_cache: dict[tuple[int, bool], tuple[str, float]] = {}
            self.ids = itertools.count(10)
            self.login_captcha = False
            self.access_ttl = 3600
            self.refresh_ttl = 1209600
            self.cursor = True
            self.max_page_size = 2000
            self.policy: dict[str, Any] = {"id": "J7uV", "name": "Default storage policy", "type": "local",
                                           "relay": False, "chunk_size": 25 * 1024 * 1024, "max_size": 0}
            self.max_edit_size = 52428800
            self.url_ttl = 3600
            self.url_margin = 600
            self.capacity: int | None = None
            self.locked: set[str] = set()
            self.refuse_signs = False
            self.fail_status = 0
            self.fail_chunk: int | None = None
            self.body_max: int | None = None
            self.unexplained: set[str] = set()  # API paths that answer code -1
            self.clock = 0.0
            self.site_url: str | None = None
            self.calls: list[dict[str, Any]] = []
            self.logins = 0
            self.refreshes = 0
            self.content_requests: list[dict[str, Any]] = []
            self.last_correlation = ""

    def _now(self) -> float:
        return time.time() + self.clock

    # -- for tests

    def add_user(self, email: str, password: str, *, status: str = "active", two_factor: bool = False) -> User:
        with self.lock:
            root = self._add(email, None, "", folder=True)
            user = self.users[email] = User(email, password, root.id, status, two_factor)
            return user

    def change_password(self, email: str, password: str) -> None:
        """Refresh tokens issued before are refused from now on; access tokens last until they expire."""
        with self.lock:
            user = self.users[email]
            user.password, user.generation = password, user.generation + 1

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access = {token: (email, 0.0) for token, (email, _) in self.access.items()}

    def expire_refresh_tokens(self) -> None:
        with self.lock:
            self.refresh = {token: (email, 0.0, generation) for token, (email, _, generation) in self.refresh.items()}

    def find(self, email: str, path: str) -> Node | None:
        with self.lock:
            node, rest = self._walk(self.users[email], [name for name in path.split("/") if name])
            return None if rest else node

    def mkdirs(self, email: str, path: str) -> Node:
        with self.lock:
            node = self.nodes[self.users[email].root]
            for name in [name for name in path.split("/") if name]:
                node = self._child(node.id, name) or self._add(email, node.id, name, folder=True)
            return node

    def put(self, email: str, path: str, data: bytes | None) -> Node:
        """A file, with a blob (None: none, as a file made empty in Cloudreve's own pages)."""
        with self.lock:
            folder, _, name = path.rpartition("/")
            node = self._add(email, self.mkdirs(email, folder).id, name, folder=False)
            if data is not None:
                node.entity = next(self.ids)
                self.blobs[node.entity] = data
            return node

    def data(self, email: str, path: str) -> bytes | None:
        with self.lock:
            node = self.find(email, path)
            return None if node is None or node.entity is None else self.blobs.get(node.entity)

    def names(self, email: str, path: str) -> list[str]:
        with self.lock:
            return sorted(node.name for node in self._children(self.find(email, path).id))

    def trashed(self, email: str) -> list[str]:
        with self.lock:
            return sorted(node.name for node in self.nodes.values() if node.owner == email and node.trashed)

    def routes(self, method: str, path: str) -> list[dict[str, Any]]:
        """The calls of one route, as recorded."""
        with self.lock:
            return [call for call in self.calls if call["method"] == method and call["path"] == "/api/v4" + path]

    def begin_upload(self, email: str, path: str, size: int) -> str:
        """Another client starts uploading `size` bytes to `path` (PUT /file/upload with entity_type version): its
        session id. The file stays locked, with the upload's metadata, until the session ends."""
        with self.lock:
            names = [name for name in path.split("/") if name]
            return self._open_session(self.users[email], names, size, "version")["session_id"]

    # -- the tree

    def _add(self, owner: str, parent: int | None, name: str, *, folder: bool) -> Node:
        moment = time.time()
        node = Node(next(self.ids), parent, name, folder, owner, created=moment, updated=moment)
        self.nodes[node.id] = node
        return node

    def _children(self, folder_id: int) -> list[Node]:
        return [node for node in self.nodes.values() if node.parent == folder_id and not node.trashed]

    def _child(self, folder_id: int, name: str) -> Node | None:
        return next((node for node in self._children(folder_id) if node.name == name), None)

    def _below(self, node: Node) -> list[Node]:
        found = [node]
        for child in [other for other in self.nodes.values() if other.parent == node.id]:
            found += self._below(child)
        return found

    def _walk(self, user: User, names: list[str]) -> tuple[Node, list[str]]:
        """The last node on the path that exists, and the names below it."""
        node = self.nodes[user.root]
        for index, name in enumerate(names):
            child = self._child(node.id, name) if node.folder else None
            if child is None:
                return node, names[index:]
            node = child
        return node, []

    def _find(self, user: User, names: list[str]) -> Node:
        node, rest = self._walk(user, names)
        if rest:
            raise Fail(40016, "Path not exist")
        return node

    def _inside(self, node: Node, folder: Node) -> bool:
        """node is folder or below it."""
        current: Node | None = node
        while current is not None:
            if current.id == folder.id:
                return True
            current = self.nodes.get(current.parent) if current.parent is not None else None
        return False

    def _shared(self, entity: int) -> bool:
        return any(node.entity == entity for node in self.nodes.values())

    def _make_folders(self, user: User, node: Node, names: list[str]) -> Node:
        """Create's loop: each name below `node`, which must be a folder."""
        for name in names:
            if not node.folder:
                raise Fail(403, "Not supported action")
            self._check_name(name)
            node = self._add(user.email, node.id, name, folder=True)
        if not node.folder:
            raise Fail(403, "Not supported action")
        return node

    def _store(self, node: Node, data: bytes) -> None:
        """A new version of the file: the old version's blob goes, as Cloudreve keeps one version by default."""
        old, node.entity = node.entity, next(self.ids)
        self.blobs[node.entity] = data
        node.updated, node.upload, node.pending = time.time(), None, None
        if old is not None and not self._shared(old):
            self.blobs.pop(old, None)

    @staticmethod
    def _check_name(name: str) -> None:
        if not name or len(name.encode()) >= 256 or any(ch in REFUSED for ch in name) or name in (".", ".."):
            raise Fail(40052, "Invalid object name")

    def _check_lock(self, names: list[str]) -> None:
        path = "/" + "/".join(names)
        if path in self.locked or path in self.uploading:
            raise Fail(40073, "Lock conflict", [{"path": self._uri(names), "token": "cool-lock" + secrets.token_hex(4),
                                                 "owner": {"application": {"type": "viewer"}}, "type": 0}])

    def _check_capacity(self, user: User, size: int) -> None:
        used = sum(len(self.blobs.get(node.entity, b"")) for node in self.nodes.values()
                   if node.owner == user.email and node.entity is not None)
        if self.capacity is not None and used + size > self.capacity:
            raise Fail(40051, "Insufficient capacity")

    @staticmethod
    def _uri(names: list[str]) -> str:
        return "cloudreve://my" + "".join("/" + quote(name) for name in names)

    def _view(self, node: Node, names: list[str], *, extended: bool = False) -> dict[str, Any]:
        if node.folder:
            size = 0
        elif node.entity is None:
            size = node.pending or 0  # a new file's placeholder has the size its upload announced
        else:
            size = len(self.blobs.get(node.entity, b""))
        view = {"type": 1 if node.folder else 0, "id": f"f{node.id}", "name": node.name, "permission": None,
                "created_at": iso(node.created), "updated_at": iso(node.updated), "size": size,
                "metadata": {"sys:upload_session_id": node.upload} if node.upload else None,
                "path": self._uri(names), "capability": "39/9", "owned": True, "primary_entity": f"e{node.entity or 0}"}
        if extended:
            entities = []
            if node.entity is not None:
                entities.append({"id": f"e{node.entity}", "size": len(self.blobs.get(node.entity, b"")), "type": 0})
            if node.upload is not None:  # the entity an upload is making (its upload_session_id isn't shown)
                entities.append({"id": f"e-{node.upload[:8]}", "size": self.sessions[node.upload]["size"], "type": 0})
            view["extended_info"] = {"storage_used": sum(entity["size"] for entity in entities), "entities": entities}
        return view

    def _policy_view(self) -> dict[str, Any]:
        view = {"id": self.policy["id"], "name": self.policy["name"], "type": self.policy["type"],
                "max_size": self.policy["max_size"]}
        if self.policy.get("relay"):
            view["relay"] = True
        return view

    # -- requests

    def handle(self, req: Request) -> Response:
        if self.version == 3:
            return self._v3(req)
        if not req.path.startswith("/api/"):
            return Response(200, INDEX_HTML, {"Content-Type": "text/html"})
        if req.path.startswith("/api/v4/file/content/") and req.method in ("GET", "HEAD"):
            return self._content(req)
        if self.body_max is not None and len(req.body) > self.body_max:
            return Response(413, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
        with self.lock:
            self.calls.append({"method": req.method, "path": req.path, "query": req.raw_query,
                               "auth": req.header("Authorization"), "cookie": req.header("Cookie"),
                               "length": req.header("Content-Length"), "json": self._body(req)})
            if self.fail_status:
                return Response(self.fail_status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
            if req.path in self.unexplained:  # serializer.Err of an error that isn't an AppError
                self.last_correlation = str(uuid.uuid4())
                return json_response({"code": -1, "msg": "", "correlation_id": self.last_correlation})
            handler = {
                ("POST", "/api/v4/session/token"): self._login,
                ("POST", "/api/v4/session/token/refresh"): self._refresh_tokens,
                ("GET", "/api/v4/file"): self._list,
                ("DELETE", "/api/v4/file"): self._delete,
                ("GET", "/api/v4/file/info"): self._info,
                ("POST", "/api/v4/file/create"): self._create,
                ("POST", "/api/v4/file/rename"): self._rename,
                ("POST", "/api/v4/file/move"): self._move,
                ("POST", "/api/v4/file/url"): self._url,
                ("PUT", "/api/v4/file/content"): self._put_content,
                ("PUT", "/api/v4/file/upload"): self._create_session,
                ("DELETE", "/api/v4/file/upload"): self._cancel,
            }.get((req.method, req.path))
            match = re.fullmatch(r"/api/v4/file/upload/([^/]+)/(\d+)", req.path)
            if handler is None and match and req.method == "POST":
                handler = lambda r: self._chunk(r, urllib.parse.unquote(match.group(1)), int(match.group(2)))  # noqa: E731
            if handler is None:
                return Response(404, b"404 page not found", {"Content-Type": "text/plain"})
            try:
                response = handler(req)
            except Fail as fail:
                response = json_response(fail.body())
        # Cloudreve's session cookie (gin-contrib/sessions), which the API doesn't need.
        response.headers.append(("Set-Cookie", f"cloudreve-session={secrets.token_hex(8)}; Path=/; HttpOnly"))
        return response

    def _v3(self, req: Request) -> Response:
        """Cloudreve 3.8.3: /api/v3 only; gin's 404 for any other /api path; the frontend for the rest."""
        if req.path == "/api/v3/site/ping":
            return json_response({"code": 0, "data": "3.8.3", "msg": ""})
        if req.path.startswith("/api"):
            return Response(404, b"404 page not found", {"Content-Type": "text/plain"})
        return Response(200, INDEX_HTML, {"Content-Type": "text/html"})

    @staticmethod
    def _body(req: Request) -> Any:
        if "json" not in (req.header("Content-Type") or ""):
            return None
        try:
            return json.loads(req.body.decode("utf-8"))
        except ValueError:
            return None

    def _json(self, req: Request) -> dict[str, Any]:
        body = self._body(req)
        if not isinstance(body, dict):
            raise Fail(40001, "Invalid parameters.")
        return body

    def _user(self, req: Request) -> User | None:
        """The account of the access token; None (anonymous) without one, or for one unknown or expired."""
        header = req.header("Authorization") or ""
        entry = self.access.get(header[len("Bearer "):]) if header.startswith("Bearer ") else None
        if entry is None or entry[1] <= self._now():
            return None
        return self.users.get(entry[0])

    def _me(self, req: Request) -> User:
        user = self._user(req)
        if user is None:
            raise Fail(401, "Login required")
        return user

    @staticmethod
    def _names(uri: Any) -> list[str]:
        split = urllib.parse.urlsplit(uri) if isinstance(uri, str) else None
        if split is None or split.scheme != "cloudreve" or split.netloc != "my" or split.query or split.fragment:
            raise Fail(40001, "unknown uri")
        names = []
        for segment in [segment for segment in split.path.split("/") if segment]:
            try:
                name = urllib.parse.unquote(segment, errors="strict")
            except UnicodeDecodeError:
                raise Fail(40001, "unknown uri") from None
            if not SEGMENT.fullmatch(segment) or name in (".", ".."):
                raise Fail(40001, "unknown uri")
            names.append(name)
        return names

    def _batch(self, errors: dict[str, dict[str, Any]]) -> Response:
        if not errors:
            return answer()
        msg = next(iter(errors.values()))["msg"] if len(errors) == 1 else "One or more operation failed"
        return json_response({"code": 40081, "msg": msg, "aggregated_error": errors,
                              "correlation_id": str(uuid.uuid4())})

    # -- sessions

    def _issue(self, user: User) -> dict[str, Any]:
        now = self._now()
        access, refresh = "acc-" + secrets.token_hex(16), "ref-" + secrets.token_hex(16)
        self.access[access] = (user.email, now + self.access_ttl)
        self.refresh[refresh] = (user.email, now + self.refresh_ttl, user.generation)
        return {"access_token": access, "refresh_token": refresh,
                "access_expires": iso(now + self.access_ttl), "refresh_expires": iso(now + self.refresh_ttl)}

    def _login(self, req: Request) -> Response:
        self.logins += 1
        if self.login_captcha:  # CaptchaRequired runs before the parameters are read
            raise Fail(40026, "CAPTCHA not match.")
        body = self._json(req)
        email, password = body.get("email"), body.get("password")
        if not isinstance(email, str) or "@" not in email or not isinstance(password, str) or not 4 <= len(password) <= 128:
            raise Fail(40001, "Invalid parameters.")
        user = self.users.get(email)
        if user is None or user.password != password:
            raise Fail(40080, "Incorrect password or email address")
        if user.status == "manual_banned":
            raise Fail(40017, "This account has been blocked")
        if user.status == "inactive":
            raise Fail(40018, "This account is not activated")
        if user.two_factor:
            return answer(str(uuid.uuid4()), code=203)
        return answer({"user": {"id": f"u{user.root}", "email": user.email, "nickname": user.email.split("@")[0],
                                "status": user.status, "group": {"id": "g1", "name": "User"}},
                       "token": self._issue(user)})

    def _refresh_tokens(self, req: Request) -> Response:
        self.refreshes += 1
        token = self._json(req).get("refresh_token")
        entry = self.refresh.get(token) if isinstance(token, str) else None
        user = self.users.get(entry[0]) if entry is not None else None
        if entry is None or entry[1] <= self._now() or user is None or user.generation != entry[2] or user.status != "active":
            raise Fail(40020, "Failed to issue token pair")
        return answer(self._issue(user))

    # -- files

    def _list(self, req: Request) -> Response:
        user = self._me(req)
        names = self._names(req.arg("uri"))
        folder = self._find(user, names)
        if not folder.folder:
            raise Fail(40016, "Path not exist")
        children = sorted(self._children(folder.id), key=lambda node: (not node.folder, node.id))
        size = int(req.arg("page_size") or 0)
        size = 100 if size <= 0 else min(size, self.max_page_size)
        if self.cursor:
            token = req.arg("next_page_token") or ""
            start = json.loads(base64.b64decode(token.partition("|")[2]))["offset"] if token else 0
            page = children[start:start + size]
            pagination: dict[str, Any] = {"page": 0, "page_size": size, "is_cursor": True}
            if start + size < len(children):
                pagination["next_token"] = f"{start + size}|" + base64.b64encode(json.dumps({"offset": start + size}).encode()).decode()
        else:
            number = int(req.arg("page") or 0)
            page = children[number * size:(number + 1) * size]
            pagination = {"page": number, "page_size": size}
            if children:
                pagination["total_items"] = len(children)
        return answer({"files": [self._view(node, names + [node.name]) for node in page], "parent": self._view(folder, names),
                       "pagination": pagination, "context_hint": str(uuid.uuid4()), "mixed_type": False,
                       "props": {"capability": "39/9", "max_page_size": self.max_page_size,
                                 "order_by_options": ["name", "size", "updated_at", "created_at"],
                                 "order_direction_options": ["asc", "desc"]},
                       "storage_policy": self._policy_view()})

    def _info(self, req: Request) -> Response:
        user = self._me(req)
        names = self._names(req.arg("uri"))
        if not names:  # dbfs.WithNotRoot: IsRootFolder() -> fs.ErrNotSupportedAction
            raise Fail(403, "Not supported action")
        return answer(self._view(self._find(user, names), names, extended=req.arg("extended") in ("true", "1")))

    def _create(self, req: Request) -> Response:
        user = self._me(req)
        body = self._json(req)
        names = self._names(body.get("uri"))
        if body.get("type") not in ("file", "folder"):
            raise Fail(40001, "Invalid parameters.")
        folder = body["type"] == "folder"
        node, rest = self._walk(user, names)
        if not rest:
            if node.folder == folder and body.get("err_on_conflict") is not True:
                return answer(self._view(node, names))
            raise Fail(40004, "Object existed")
        self._check_lock(names)
        parent = self._make_folders(user, node, rest[:-1])
        self._check_name(rest[-1])
        made = self._add(user.email, parent.id, rest[-1], folder=folder)
        return answer(self._view(made, names))

    def _rename(self, req: Request) -> Response:
        user = self._me(req)
        body = self._json(req)
        names, new = self._names(body.get("uri")), body.get("new_name")
        if not isinstance(new, str) or not 1 <= len(new) <= 255:
            raise Fail(40001, "Invalid parameters.")
        node = self._find(user, names)
        if node.id == user.root:
            raise Fail(403, "Not supported action")
        self._check_name(new)
        self._check_lock(names)
        other = self._child(node.parent, new)
        if other is not None and other.id != node.id:
            raise Fail(40004, "Object existed")
        node.name, node.updated = new, time.time()
        return answer(self._view(node, names[:-1] + [new]))

    def _move(self, req: Request) -> Response:
        user = self._me(req)
        body = self._json(req)
        uris = body.get("uris")
        if not isinstance(uris, list) or not uris:
            raise Fail(40001, "Invalid parameters.")
        target_names = self._names(body.get("dst"))
        target = self._find(user, target_names)
        if not target.folder:
            raise Fail(403, "Not supported action")
        copy = body.get("copy") is True
        errors: dict[str, dict[str, Any]] = {}
        items: list[tuple[list[str], Node]] = []
        for uri in uris:
            try:
                names = self._names(uri)
                node = self._find(user, names)
                if node.id == user.root or (node.folder and self._inside(target, node)):
                    raise Fail(403, "Not supported action")
                items.append((names, node))
            except Fail as fail:
                errors[str(uri)] = fail.body()
        for names, node in items:
            self._check_lock(names)
            self._check_lock(target_names + [node.name])
        for names, node in items:
            other = self._child(target.id, node.name)
            if other is not None and (copy or other.id != node.id):  # the database's unique name per folder
                raise Fail(40004, "Object existed")
            if copy:
                self._check_capacity(user, sum(len(self.blobs.get(n.entity, b"")) for n in self._below(node) if n.entity))
                self._copy_tree(node, target.id)
            else:
                node.parent = target.id
        return self._batch(errors)

    def _copy_tree(self, node: Node, parent: int) -> None:
        made = self._add(node.owner, parent, node.name, folder=node.folder)
        made.entity = node.entity  # a copy shares the blob
        for child in self._children(node.id):
            self._copy_tree(child, made.id)

    def _delete(self, req: Request) -> Response:
        user = self._user(req)
        body = self._json(req)
        uris = body.get("uris")
        if not isinstance(uris, list) or not uris:
            raise Fail(40001, "Invalid parameters.")
        errors: dict[str, dict[str, Any]] = {}
        items: list[tuple[list[str], Node]] = []
        for uri in uris:
            try:
                if user is None:
                    raise Fail(401, "Login required")
                names = self._names(uri)
                node = self._find(user, names)
                if node.id == user.root:
                    raise Fail(403, "Not supported action")
                items.append((names, node))
            except Fail as fail:
                errors[str(uri)] = fail.body()
        for names, _ in items:
            self._check_lock(names)
        for _, node in items:
            if body.get("skip_soft_delete") is True:
                for gone in self._below(node):
                    del self.nodes[gone.id]
            else:
                node.trashed = True  # into the trash bin, under another name: the name is free again
        return self._batch(errors)

    def _url(self, req: Request) -> Response:
        user = self._user(req)
        body = self._json(req)
        uris = body.get("uris")
        if not isinstance(uris, list) or not uris:
            raise Fail(40001, "Invalid parameters.")
        errors: dict[str, dict[str, Any]] = {}
        urls, expires = [], None
        for uri in uris:
            try:
                if user is None:
                    raise Fail(401, "Login required")
                node = self._find(user, self._names(uri))
                if node.folder or node.entity is None or node.entity not in self.blobs:
                    raise Fail(40077, "Entity not exist")
                url, ends = self._signed(node, body.get("download") is True)
                urls.append({"url": url})
                expires = ends if expires is None else min(expires, ends)
            except Fail as fail:
                errors[str(uri)] = fail.body()
        if errors:  # without skip_error the whole request fails
            return self._batch(errors)
        return answer({"urls": urls, "expires": iso(expires)})

    def _signed(self, node: Node, download: bool) -> tuple[str, float]:
        """A signed link of the node's blob, on the Site URL; one made before while it has url_margin seconds left."""
        now = self._now()
        cached = self.url_cache.get((node.entity, download))
        if cached is not None and cached[1] - self.url_margin > now:
            return cached
        sign, ends = secrets.token_urlsafe(16), now + self.url_ttl
        self.signs[sign] = (node.entity, ends)
        query = urllib.parse.urlencode({**({"download": "true"} if download else {}), "sign": sign})
        url = f"{self.site_url or self.url}/api/v4/file/content/e{node.entity}/0/{quote(node.name)}?{query}"
        if self.url_ttl > self.url_margin:
            self.url_cache[(node.entity, download)] = (url, ends)
        return url, ends

    def _content(self, req: Request) -> Response:
        with self.lock:
            self.content_requests.append({"method": req.method, "path": req.path, "query": req.raw_query,
                                          "auth": req.header("Authorization"), "cookie": req.header("Cookie"),
                                          "range": req.header("Range")})
            entry = self.signs.get(req.arg("sign") or "")
            match = re.fullmatch(r"/api/v4/file/content/e(\d+)/\d+/([^/]*)", req.path)
            if (self.refuse_signs or entry is None or entry[1] <= self._now() or match is None
                    or int(match.group(1)) != entry[0]):
                return json_response({"code": 40020, "msg": "invalid sign", "correlation_id": str(uuid.uuid4())})
            data = self.blobs.get(entry[0])
            if data is None:
                return Response(404, b"Entity data does not exist.", {"Content-Type": "text/plain; charset=utf-8"})
            name = urllib.parse.unquote(match.group(2))
        headers = {"Content-Type": "application/json" if name.endswith(".json") else "application/octet-stream",
                   "Etag": f'"e{entry[0]}"'}
        if req.arg("download") == "true":
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + quote(name)
        return file_response(data, req.header("Range"), headers)

    # -- uploads

    def _create_session(self, req: Request) -> Response:
        user = self._me(req)
        body = self._json(req)
        names, size, kind = self._names(body.get("uri")), body.get("size"), body.get("entity_type") or ""
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or kind not in ("", "version", "live_photo"):
            raise Fail(40001, "Invalid parameters.")
        return answer(self._open_session(user, names, size, kind))

    def _open_session(self, user: User, names: list[str], size: int, kind: str) -> dict[str, Any]:
        """PrepareUpload and the upload credential: the session's answer."""
        node, rest = self._walk(user, names)
        if not rest and (node.folder or not kind):
            raise Fail(40004, "Object existed")
        if rest and kind not in ("", "version"):
            raise Fail(40016, "Path not exist")
        self._check_lock(names)
        self._check_name(names[-1])
        if self.policy["max_size"] and size > self.policy["max_size"]:
            raise Fail(40049, "File is too large")
        self._check_capacity(user, size)
        session = str(uuid.uuid4())
        if rest:  # a new file's placeholder: the upload's size, its only entity the one being uploaded
            target = self._add(user.email, self._make_folders(user, node, rest[:-1]).id, rest[-1], folder=False)
            target.pending = size
        else:
            target = node
        target.upload = session
        relay = self.policy.get("relay") is True
        through = self.policy["type"] == "local" or relay
        self.sessions[session] = {"user": user.email, "node": target.id, "new": bool(rest), "size": size,
                                  "chunk": 0 if relay else self.policy["chunk_size"], "received": set(),
                                  "data": bytearray(size), "path": "/" + "/".join(names)}
        self.uploading["/" + "/".join(names)] = session
        data: dict[str, Any] = {"session_id": session, "upload_id": "", "chunk_size": self.sessions[session]["chunk"],
                                "expires": int(time.time()) + 86400, "storage_policy": self._policy_view(),
                                "uri": self._uri(names), "callback_secret": secrets.token_urlsafe(24)}
        if not through:  # the client would send the chunks to the policy's storage
            data["upload_urls"] = [f"https://storage.example.com/upload/{session}?partNumber=1"]
            data["completeURL"] = f"https://storage.example.com/upload/{session}?uploadId=x"
        return data

    def _chunk(self, req: Request, session_id: str, index: int) -> Response:
        user = self._user(req)
        session = self.sessions.get(session_id)
        if session is None or user is None or session["user"] != user.email:
            raise Fail(40011, "")
        if self.policy["type"] != "local" and self.policy.get("relay") is not True:
            raise Fail(40006, "")
        chunk, size = session["chunk"], session["size"]
        if chunk == 0 and index > 0:
            raise Fail(40012, "Chunk index cannot be greater than 0")
        if index > 0 and index * chunk >= size:
            raise Fail(40012, "Chunk offset cannot be greater than file size")
        last = chunk == 0 or (index + 1) * chunk >= size
        expected = size - index * chunk if last else chunk
        if req.header("Content-Length") != str(expected) or len(req.body) != expected:
            raise Fail(40013, f"Invalid Content-Length (expected: {expected})")
        if self.fail_chunk == index:
            raise Fail(50004, "Failed to upload file")
        session["data"][index * chunk:index * chunk + expected] = req.body
        session["received"].add(index)
        if len(session["received"]) >= (1 if chunk <= 0 or size == 0 else -(-size // chunk)):
            del self.sessions[session_id]
            self.uploading.pop(session["path"], None)
            self._store(self.nodes[session["node"]], bytes(session["data"]))
        return answer()

    def _cancel(self, req: Request) -> Response:
        user = self._me(req)
        body = self._json(req)
        node = self._find(user, self._names(body.get("uri")))  # the placeholder
        session = self.sessions.get(body.get("id"))
        if session is not None and (session["user"] != user.email or session["node"] != node.id):
            raise Fail(404, "Upload session not found")
        node.upload = None
        if session is not None:
            del self.sessions[body["id"]]
            self.uploading.pop(session["path"], None)
            if session["new"]:
                del self.nodes[node.id]
        return answer()

    def _put_content(self, req: Request) -> Response:
        user = self._me(req)
        names = self._names(req.arg("uri"))
        size = int(req.header("Content-Length") or 0)
        if size > self.max_edit_size:
            raise Fail(40049, "File is too large")
        node, rest = self._walk(user, names)
        if not rest and node.folder:
            raise Fail(40004, "Object existed")
        self._check_lock(names)
        self._check_name(names[-1])
        if self.policy["max_size"] and size > self.policy["max_size"]:
            raise Fail(40049, "File is too large")
        self._check_capacity(user, size)
        if rest:
            node = self._add(user.email, self._make_folders(user, node, rest[:-1]).id, rest[-1], folder=False)
        self._store(node, req.body[:size])
        return answer(self._view(node, names))
