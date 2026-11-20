"""An in-memory fake of the HTTP API of OpenList and AList (tests only, no Django).

    fake = FakeOpenList().start()
    fake.url, fake.users = {"admin": "pw"}, fake.admin_token
    fake.close()

It answers as those servers do (OpenList's server/handles and server/middlewares):

- HTTP 200 with {"code", "message", "data"} whatever happened; tokens from /api/auth/login (and the fixed token of
  the settings) in a raw "Authorization" header; a guest account that may be switched off; a wrong password is
  code 400 "username or password is incorrect", as AList v3 answers it.
- A user may have a base path of its own (`bases`): the API's paths are below it (user.JoinPath), /d/<path> takes
  the server's full path, and GET /api/me gives it as "base_path".
- Folder passwords ("meta" passwords) for the paths below `locked`, which the admin doesn't need.
- /api/fs/list gives each file's "sign", and so does /api/fs/get, which also asks the file's storage for a download
  link (FsGet's fs.Link): `no_links` are full paths whose storage gives none, a code 500.
- /d/<path> checks the sign first (sign_all, the default), then looks for the file; Range.
- /api/fs/mkdir and PUT /api/fs/put make the folders above; rename, move and copy refuse a taken name with 403
  "file [x] exists" (no overwrite).

Shares (OpenList 4): shares[id] = (folder or file, password or None); /api/fs/list of /@s/<id>/<path> lists it for
anyone with the password ("the share does not exist" for an unknown id, as dealError says; a share of one file lists
as that file, "not a folder"), and /sd/<id>/<path>?pwd= sends its files.

Knobs: guest (the guest account can read), expire_tokens(), locked and locked_password, bases, sign_all, no_links,
ignore_case (lookups of names ignore case, as the Local storage on Windows: a rename's check of the new name finds
the file itself), login_refusal ((code, message) for every login), task_moves (moves and copies run as tasks,
which are over after task_polls asks of /api/task/<kind>/info; task_error makes them fail), put_delay (seconds before an
upload's answer, as a storage OpenList uploads to after buffering), put_status (a proxy's answer to uploads, e.g.
413), fail_status (a proxy's answer to every API call). Records: calls (the API paths asked), download_requests
(the headers /d and /sd got), put_requests (the headers of the uploads).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

KEY = b"fake-openlist-sign-key"
SUCCEEDED, FAILED = 2, 7  # tache.State: StateSucceeded, StateFailed


def ok(data: Any = None) -> Response:
    return json_response({"code": 200, "message": "success", "data": data})


def fail(code: int, message: str) -> HTTPError:
    return HTTPError(200, {"code": code, "message": message, "data": None})


def sign(path: str) -> str:
    return hmac.new(KEY, path.encode(), hashlib.sha256).hexdigest()[:24] + ":0"


class FakeOpenList:
    def __init__(self):
        self.lock = threading.Lock()
        self.users = {"admin": "pw-admin", "alice": "pw-alice"}  # alice isn't an admin
        self.admin_token = "openlist-" + secrets.token_hex(16)
        self.tokens: dict[str, str] = {}  # token -> user
        self.guest = True
        self.locked = "/locked"
        self.locked_password = "s3same"
        # path -> {"dir": bool, "data": bytes, "modified": datetime}; "/" is the root folder
        self.tree: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.shares: dict[str, tuple[str, str | None]] = {}
        self.download_requests: list[dict[str, str | None]] = []
        self.put_requests: list[dict[str, str | None]] = []
        self.tasks: dict[str, dict[str, Any]] = {}
        self.reset()
        self.server = FakeServer(self.handle)

    def start(self) -> FakeOpenList:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    def reset(self) -> None:
        with self.lock:
            self.tree = {"/": self._node(True)}
            self.tokens.clear()
            self.guest = True
            self.calls.clear()
            self.download_requests.clear()
            self.put_requests.clear()
            self.shares.clear()
            self.tasks.clear()
            self.bases: dict[str, str] = {}
            self.sign_all = True
            self.no_links: set[str] = set()
            self.ignore_case = False
            self.login_refusal: tuple[int, str] | None = None
            self.task_moves = False
            self.task_polls = 2
            self.task_error: str | None = None
            self.put_delay = 0.0
            self.put_status = 0
            self.fail_status = 0

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    @staticmethod
    def _node(is_dir: bool, data: bytes = b"") -> dict[str, Any]:
        return {"dir": is_dir, "data": data, "modified": datetime.datetime.now(datetime.timezone.utc)}

    # -- the tree

    @staticmethod
    def _clean(path: str) -> str:
        parts = [p for p in path.replace("\\", "/").split("/") if p and p != "."]
        return "/" + "/".join(parts)

    def _parent(self, path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    def _children(self, path: str) -> list[str]:
        prefix = "/" if path == "/" else path + "/"
        return [p for p in self.tree if p != path and p.startswith(prefix) and "/" not in p[len(prefix):]]

    def _below(self, path: str) -> list[str]:
        return [p for p in self.tree if p == path or p.startswith(path.rstrip("/") + "/")]

    def _taken(self, path: str) -> bool:
        """Whether fs.Get finds something at `path` (ignoring case on a disk that does)."""
        if self.ignore_case:
            return any(p.lower() == path.lower() for p in self.tree)
        return path in self.tree

    def _make_dirs(self, path: str) -> None:
        if path in self.tree:
            if not self.tree[path]["dir"]:
                raise fail(500, "not a folder")
            return
        self._make_dirs(self._parent(path))
        self.tree[path] = self._node(True)

    def _item(self, path: str) -> dict[str, Any]:
        node = self.tree[path]
        stamp = node["modified"].strftime("%Y-%m-%dT%H:%M:%S.%f000+00:00")
        return {"name": path.rsplit("/", 1)[1] if path != "/" else "root", "size": 0 if node["dir"] else len(node["data"]),
                "is_dir": node["dir"], "modified": stamp, "created": "0001-01-01T00:00:00Z",
                "sign": "" if node["dir"] else sign(path), "thumb": "", "type": 1 if node["dir"] else 0,
                "hashinfo": "null"}

    # -- requests

    def _user(self, req) -> str:
        token = req.header("Authorization")
        if not token:
            if not self.guest:
                raise fail(401, "Guest user is disabled, login please")
            return "guest"
        if token == self.admin_token:
            return "admin"
        if token in self.tokens:
            return self.tokens[token]
        raise fail(401, "token is expired")

    def _joined(self, user: str, path: str) -> str:
        """user.JoinPath: the server's full path of a path of the user's."""
        return self._clean(self.bases.get(user, "/") + "/" + path)

    def _check_password(self, path: str, given: str | None) -> None:
        if self.user == "admin":
            return
        if (path == self.locked or path.startswith(self.locked + "/")) and given != self.locked_password:
            raise fail(403, "password is incorrect or you have no permission")

    def handle(self, req) -> Response:
        path = req.path
        if path.startswith("/d/"):
            return self._download(req)
        if path.startswith("/sd/"):
            return self._share_download(req)
        if self.fail_status:  # a proxy in front of the server
            return Response(self.fail_status, b"<html>a proxy's page</html>", {"Content-Type": "text/html"})
        if path == "/api/fs/put" and self.put_delay:
            time.sleep(self.put_delay)  # the body is in: OpenList uploads it to its storage before it answers
        with self.lock:
            self.calls.append(path)
            if path == "/api/auth/login" and req.method == "POST":
                body = json.loads(req.body or b"{}")
                if self.login_refusal is not None:
                    raise fail(*self.login_refusal)
                if self.users.get(body.get("username")) != body.get("password"):
                    raise fail(400, "username or password is incorrect")
                token = secrets.token_hex(16)
                self.tokens[token] = body["username"]
                return ok({"token": token})
            if path == "/api/me" and req.method == "GET":
                user = self._user(req)
                return ok({"id": 1, "username": user, "base_path": self.bases.get(user, "/"), "role": 0,
                           "disabled": False, "permission": 0, "otp": False})
            if path == "/api/fs/put" and req.method == "PUT":
                return self._put(req)
            if path.startswith("/api/task/") and req.method == "POST":
                return self._task(req)
            if req.method != "POST" or not path.startswith("/api/fs/"):
                raise HTTPError(404, b"404 page not found")
            body = json.loads(req.body or b"{}")
            op = path[len("/api/fs/"):]
            if str(body.get("path", "")).startswith("/@s/"):
                if op != "list":
                    raise fail(403, "permission denied")
                return self._share_list(body)
            user = self.user = self._user(req)
            writes = op in ("mkdir", "rename", "move", "copy", "remove")
            if writes and user == "guest":
                raise fail(403, "permission denied")
            for key in ("path", "src_dir", "dst_dir", "dir"):
                if isinstance(body.get(key), str):
                    body[key] = self._joined(user, body[key])
            return getattr(self, "_" + op)(body)

    def _list(self, body) -> Response:
        path = self._clean(body.get("path", "/"))
        self._check_password(path, body.get("password"))
        node = self.tree.get(path)
        if node is None:
            raise fail(500, "failed get objs: failed get dir: object not found")
        if not node["dir"]:
            raise fail(500, "failed get objs: not a folder")
        content = [self._item(child) for child in sorted(self._children(path))]
        return ok({"content": content or None, "total": len(content), "readme": "", "write": True, "provider": "Local"})

    def _get(self, body) -> Response:
        path = self._clean(body.get("path", "/"))
        self._check_password(path, body.get("password"))
        if path not in self.tree:
            raise fail(500, "failed get obj: object not found")
        if path in self.no_links:  # FsGet asks the storage for the file's link, which it refuses
            raise fail(500, "failed link: failed get link: the storage gives no link for this file")
        item = self._item(path)
        item.update({"raw_url": "" if self.tree[path]["dir"] else f"{self.url}/d{urllib.parse.quote(path)}",
                     "readme": "", "provider": "Local", "related": None})
        return ok(item)

    def _mkdir(self, body) -> Response:
        self._make_dirs(self._clean(body["path"]))
        return ok()

    def _rename(self, body) -> Response:
        path = self._clean(body["path"])
        if path not in self.tree or path == "/":
            raise fail(500, "failed get obj: object not found")
        target = self._parent(path).rstrip("/") + "/" + body["name"]
        if target != path and self._taken(target):
            raise fail(403, f"file [{body['name']}] exists")
        self._relocate(path, target, keep=False)
        return ok()

    def _relocate(self, path: str, target: str, *, keep: bool) -> None:
        for old in sorted(self._below(path)):
            node = self.tree[old] if keep else self.tree.pop(old)
            self.tree[target + old[len(path):]] = dict(node)

    def _move(self, body, keep: bool = False) -> Response:
        src, dst = self._clean(body["src_dir"]), self._clean(body["dst_dir"])
        if dst not in self.tree or not self.tree[dst]["dir"]:
            raise fail(500, "failed get dst dir: object not found")
        made = []
        for name in body.get("names") or []:
            path = src.rstrip("/") + "/" + name
            target = dst.rstrip("/") + "/" + name
            if self._taken(target):
                raise fail(403, f"file [{name}] exists")
            if path not in self.tree:
                raise fail(500, "failed get src object: object not found")
            if self.task_moves:  # between two storages: a task does it, later
                task = {"id": secrets.token_hex(8), "kind": "copy" if keep else "move", "state": 0, "polls": 0,
                        "work": (path, target, keep), "creator": self.user}
                self.tasks[task["id"]] = task
                made.append(self._task_info(task))
            else:
                self._relocate(path, target, keep=keep)
        if made:
            return ok({"message": f"Successfully created {len(made)} {'copy' if keep else 'move'} task(s)", "tasks": made})
        return ok({"message": "Move operations completed immediately"})

    def _copy(self, body) -> Response:
        return self._move(body, keep=True)

    def _remove(self, body) -> Response:
        folder = self._clean(body["dir"])
        for name in body.get("names") or []:
            for path in self._below(folder.rstrip("/") + "/" + name):
                self.tree.pop(path, None)
        return ok()

    def _put(self, req) -> Response:
        self.put_requests.append({"content_type": req.header("Content-Type"), "content_length": req.header("Content-Length")})
        if self.put_status:
            return Response(self.put_status, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
        user = self.user = self._user(req)
        if user == "guest":
            raise fail(403, "permission denied")
        raw = req.header("File-Path")
        if not raw:
            raise fail(400, "file path is empty")
        path = self._joined(user, urllib.parse.unquote(raw))
        length = req.header("Content-Length")
        if length is None or int(length) != len(req.body):
            raise fail(400, "content length mismatch")
        existing = self.tree.get(path)
        if existing is not None and existing["dir"]:
            raise fail(500, "object already exists")
        if existing is not None and req.header("Overwrite") == "false":
            raise fail(403, "file exists")
        self._make_dirs(self._parent(path))
        self.tree[path] = self._node(False, req.body)
        return ok({"task": None})

    # -- tasks (server/handles/task.go)

    @staticmethod
    def _task_info(task: dict[str, Any]) -> dict[str, Any]:
        return {"id": task["id"], "name": f"{task['kind']} [{task['work'][0]}]", "creator": task["creator"],
                "state": task["state"], "status": "", "progress": 100.0 if task["state"] == SUCCEEDED else 0.0,
                "error": task.get("error", "")}

    def _task(self, req) -> Response:
        if self._user(req) == "guest":
            raise fail(403, "You are a guest")  # middlewares.AuthNotGuest
        kind, _, action = req.path[len("/api/task/"):].partition("/")
        task = self.tasks.get(req.arg("tid") or "")
        if action != "info" or task is None or task["kind"] != kind:
            raise fail(404, "task not found")
        if task["state"] not in (SUCCEEDED, FAILED):
            task["polls"] += 1
            if task["polls"] >= self.task_polls:
                if self.task_error is not None:
                    task["state"], task["error"] = FAILED, self.task_error
                else:
                    path, target, keep = task["work"]
                    self._relocate(path, target, keep=keep)
                    task["state"] = SUCCEEDED
            else:
                task["state"] = 1  # running
        return ok(self._task_info(task))

    # -- shares

    def _shared(self, share_path: str, password: str | None) -> str:
        """The folder or file of the server that a /@s/<id>/<path> of a share is."""
        sid, _, rest = share_path[len("/@s/"):].partition("/")
        if sid not in self.shares:
            raise fail(500, "the share does not exist")
        folder, wanted = self.shares[sid]
        if wanted is not None and password != wanted:
            raise fail(403, "wrong share code")
        return self._clean(folder + "/" + rest)

    def _share_list(self, body) -> Response:
        path = self._shared(body["path"], body.get("password"))
        node = self.tree.get(path)
        if node is None:
            raise fail(500, "failed list sharing: failed get dir: object not found")
        if not node["dir"]:  # a share of one file lists that file itself (internal/sharing/list.go)
            raise fail(500, "failed list sharing: not a folder")
        content = [{k: v for k, v in self._item(child).items() if k != "sign"} for child in sorted(self._children(path))]
        return ok({"content": content or None, "total": len(content), "readme": "", "write": False, "provider": "unknown"})

    def _share_download(self, req) -> Response:
        rest = urllib.parse.unquote(req.path[len("/sd/"):])
        with self.lock:
            self.download_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            path = self._shared("/@s/" + rest, req.arg("pwd"))
            node = self.tree.get(path)
            if node is None or node["dir"]:
                raise HTTPError(404, {"code": 404, "message": "object not found"})
            data = node["data"]
        return file_response(data, req.header("Range"))

    def _download(self, req) -> Response:
        path = self._clean(urllib.parse.unquote(req.path[len("/d"):]))
        with self.lock:
            self.download_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            if self.sign_all and req.arg("sign") != sign(path):  # middlewares.Down runs before handles.Down
                raise HTTPError(401, {"code": 401, "message": "sign mismatch"})
            node = self.tree.get(path)
            if node is None or node["dir"]:
                raise HTTPError(404, {"code": 404, "message": "object not found"})
            data = node["data"]
        return file_response(data, req.header("Range"))
