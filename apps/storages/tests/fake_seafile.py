"""An in-memory fake of a Seafile server's Web API and file server (tests only, no Django).

    fake = FakeSeafile().start()
    library_id = fake.add_library("文档", encrypted_with=None)
    fake.url
    fake.close()

It answers as Seafile does where the driver depends on it (seahub and seafile-server v14.0.9; the files are named in
the driver's docstring):
- /api2/auth-token/ gives a token for "Authorization: Token <token>"; an unknown token is a 401.
- Errors are {"error_msg": ...}; with `throttle`, the API answers 429 {"detail": "Request was throttled. ..."}. With
  `redirect_posts`, its POSTs answer 302 to a login page (a proxy in front of it).
- GET /api2/repos/ lists the account's libraries, and those shared with it (not those added with listed=False);
  GET /api2/repos/<id>/ tells of one of them.
- A folder listing is GET /api2/repos/<id>/dir/?p= (a file's path is a 404).
- Seafile renames a new folder, or a moved or copied item, whose name is taken: "a (1)".
- An encrypted library lists without its password: names aren't encrypted. POST /api2/repos/<id>/ sets its password
  (440 without one, 400 for a wrong one), and the file server refuses its files' data, downloads and uploads,
  with 400 "Repo is encrypted. Please provide password to view it." unless the password was set in the last
  `password_ttl` seconds (passwd-mgr.c's hour). opened: library id -> when.
- Download and upload links of the file server don't take the token. Its errors are Go's http.Error() of their
  message, text/plain; the upload's are {"error": "..."} made without escaping, so a message's "\\n" stays raw.
- An upload is multipart/form-data with parent_dir (it must exist), replace and file; a name of 256 bytes or more is
  refused, the name in the message; the name is kept in NFC. It answers ret-json's [{"name", "id", "size"}]: 442
  above `max_upload_size`; with `redirect_uploads` a 301 to https; with `upload_answer`, that page with HTTP 200
  instead (nothing stored); `upload_delay` seconds late.

Knobs: throttle, redirect_posts, password_ttl, max_upload_size, redirect_uploads, upload_answer, upload_delay;
revoke_tokens(). Records: calls (method and path of the API requests), file_requests (the headers the file server got).
"""

from __future__ import annotations

import datetime
import json
import re
import secrets
import threading
import time
import unicodedata
import urllib.parse
import uuid
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

ENCRYPTED = "Repo is encrypted. Please provide password to view it."


def error(status: int, message: str) -> HTTPError:
    return HTTPError(status, {"error_msg": message})


def file_server_error(status: int, message: str, *, upload: bool = False) -> HTTPError:
    """An error of the file server: http.Error() of the message, which the upload handlers make {"error": ...} first,
    with a plain Sprintf (formatJSONError)."""
    text = '{"error": "%s"}' % message if upload else message
    return HTTPError(status, Response(status, (text + "\n").encode(), {"Content-Type": "text/plain; charset=utf-8"}))


def multipart(body: bytes, content_type: str) -> dict[str, tuple[str | None, bytes]]:
    """{field: (filename, value)} of a multipart/form-data body."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type or "")
    if not boundary:
        raise error(400, "not multipart")
    fields: dict[str, tuple[str | None, bytes]] = {}
    for part in body.split(b"--" + boundary.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part.lstrip(b"\r\n").partition(b"\r\n\r\n")
        disposition = head.decode("utf-8")
        name = re.search(r'name="([^"]*)"', disposition)
        filename = re.search(r'filename="((?:[^"\\]|\\.)*)"', disposition)
        if name:
            fields[name.group(1)] = (re.sub(r"\\(.)", r"\1", filename.group(1)) if filename else None,
                                     value[:-2] if value.endswith(b"\r\n") else value)
    return fields


class FakeSeafile:
    def __init__(self):
        self.lock = threading.Lock()
        self.users = {"alice@example.com": "pw-alice"}
        self.tokens: dict[str, str] = {}
        self.libraries: dict[str, dict[str, Any]] = {}
        self.links: dict[str, tuple[str, str, str]] = {}  # token -> ("download" | "upload", library id, path)
        self.calls: list[tuple[str, str]] = []
        self.file_requests: list[dict[str, str | None]] = []
        self.opened: dict[str, float] = {}  # encrypted library -> when its password was set
        self._knobs()
        self.server = FakeServer(self.handle)

    def _knobs(self) -> None:
        self.password_ttl = 3600.0
        self.throttle = False
        self.redirect_posts = False
        self.max_upload_size: int | None = None
        self.redirect_uploads = False
        self.upload_answer: bytes | None = None
        self.upload_delay = 0.0

    def start(self) -> FakeSeafile:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    def reset(self) -> None:
        with self.lock:
            self.tokens.clear()
            self.libraries.clear()
            self.links.clear()
            self.calls.clear()
            self.file_requests.clear()
            self.opened.clear()
            self._knobs()

    def revoke_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def token_for(self, user: str = "alice@example.com") -> str:
        with self.lock:
            token = secrets.token_hex(20)
            self.tokens[token] = user
            return token

    def add_library(self, name: str, *, encrypted_with: str | None = None, owner: str = "alice@example.com",
                    shared: bool = False, listed: bool = True) -> str:
        with self.lock:
            library_id = str(uuid.uuid4())
            self.libraries[library_id] = {"name": name, "owner": owner, "password": encrypted_with, "shared": shared,
                                          "listed": listed, "tree": {"/": self._node(True)}}
            return library_id

    def add_file(self, library_id: str, path: str, data: bytes) -> None:
        with self.lock:
            tree = self.libraries[library_id]["tree"]
            self._make_dirs(tree, path.rsplit("/", 1)[0] or "/")
            tree[path] = self._node(False, data)

    @staticmethod
    def _node(is_dir: bool, data: bytes = b"") -> dict[str, Any]:
        return {"dir": is_dir, "data": data, "mtime": int(datetime.datetime.now(datetime.timezone.utc).timestamp())}

    # -- trees

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    @staticmethod
    def _join(folder: str, name: str) -> str:
        return folder.rstrip("/") + "/" + name

    def _children(self, tree, path):
        prefix = "/" if path == "/" else path + "/"
        return sorted(p for p in tree if p != path and p.startswith(prefix) and "/" not in p[len(prefix):])

    def _below(self, tree, path):
        return [p for p in tree if p == path or p.startswith(path.rstrip("/") + "/")]

    def _make_dirs(self, tree, path):
        if path in tree:
            if not tree[path]["dir"]:
                raise error(400, "Folder name invalid.")
            return
        self._make_dirs(tree, self._parent(path))
        tree[path] = self._node(True)

    def _free_name(self, tree, folder: str, name: str) -> str:
        """Seafile's way with a taken name: "a (1).txt", "a (2).txt"..."""
        if self._join(folder, name) not in tree:
            return name
        stem, dot, ext = name.rpartition(".")
        if not dot or not stem:
            stem, ext = name, ""
        for n in range(1, 1000):
            candidate = f"{stem} ({n})" + (f".{ext}" if ext else "")
            if self._join(folder, candidate) not in tree:
                return candidate
        raise error(500, "no free name")

    def _relocate(self, source_tree, path, target_tree, target, *, keep):
        for old in sorted(self._below(source_tree, path)):
            node = source_tree[old] if keep else source_tree.pop(old)
            target_tree[target + old[len(path):]] = dict(node)

    # -- requests

    def _user(self, req) -> str:
        header = req.header("Authorization") or ""
        user = self.tokens.get(header[len("Token "):]) if header.startswith("Token ") else None
        if user is None:
            raise HTTPError(401, {"detail": "Invalid token"})
        return user

    def _library(self, library_id: str, user: str) -> dict[str, Any]:
        """The library, if the user may use it: an encrypted one too, as its names aren't encrypted."""
        library = self.libraries.get(library_id)
        if library is None or (library["owner"] != user and not library["shared"]):
            raise error(404, "Library not found.")
        return library

    def _view(self, library_id: str, library: dict[str, Any], user: str) -> dict[str, Any]:
        return {"id": library_id, "name": library["name"], "type": "repo" if library["owner"] == user else "srepo",
                "encrypted": library["password"] is not None, "mtime": 1767225600, "permission": "rw", "size": 0}

    def _unlocked(self, library_id: str) -> bool:
        """Whether the file server may read and write the library's files: it isn't encrypted, or its password was
        set in the last password_ttl seconds."""
        if self.libraries[library_id]["password"] is None:
            return True
        return time.time() - self.opened.get(library_id, -1e12) < self.password_ttl

    def handle(self, req) -> Response:
        path = urllib.parse.unquote(req.path)
        if path.startswith("/seafhttp/"):
            return self._file_server(req, path)
        with self.lock:
            self.calls.append((req.method, path))
            if self.throttle:
                raise HTTPError(429, {"detail": "Request was throttled. Expected available in 5 seconds."})
            if self.redirect_posts and req.method == "POST" and path != "/api2/auth-token/":
                raise HTTPError(302, Response(302, b"", {"Location": "/accounts/login/"}))
            if path == "/api2/auth-token/" and req.method == "POST":
                form = urllib.parse.parse_qs(req.body.decode())
                user, password = form.get("username", [""])[0], form.get("password", [""])[0]
                if self.users.get(user) != password:
                    raise HTTPError(400, {"non_field_errors": ["Unable to login with provided credentials."]})
                token = secrets.token_hex(20)
                self.tokens[token] = user
                return json_response({"token": token})
            user = self._user(req)
            if path == "/api2/repos/" and req.method == "GET":
                return json_response([self._view(library_id, lib, user) for library_id, lib in self.libraries.items()
                                      if (lib["owner"] == user or lib["shared"]) and lib["listed"]])
            match = re.fullmatch(r"/api2/repos/([0-9a-f-]{36})/", path)
            if match and req.method == "GET":
                return json_response(self._view(match.group(1), self._library(match.group(1), user), user))
            if match and req.method == "POST":  # check_set_repo_password
                library = self._library(match.group(1), user)
                password = urllib.parse.parse_qs(req.body.decode()).get("password", [None])[0]
                if not password:
                    raise error(440, "Library password is needed.")
                if library["password"] != password:
                    raise error(400, "Wrong password")
                self.opened[match.group(1)] = time.time()
                return json_response("success")
            if path in ("/api/v2.1/repos/batch-delete-item/", "/api/v2.1/repos/sync-batch-move-item/",
                        "/api/v2.1/repos/sync-batch-copy-item/"):
                return self._batch(req, path, user, json.loads(req.body or b"{}"))
            match = re.fullmatch(r"/(api2|api/v2\.1)/repos/([0-9a-f-]{36})/(dir|file|upload-link)/", path)
            if not match:
                raise error(404, "Not found.")
            library_id, kind = match.group(2), match.group(3)
            library = self._library(library_id, user)
            return getattr(self, f"_{kind.replace('-', '_')}")(req, library_id, library, req.arg("p", "/"))

    def _dir(self, req, library_id, library, p) -> Response:
        tree = library["tree"]
        if req.method == "GET":
            node = tree.get(p)
            if node is None or not node["dir"]:
                raise error(404, f"Folder {p} not found.")
            return json_response([
                {"type": "dir" if tree[c]["dir"] else "file", "name": c.rsplit("/", 1)[1], "id": "0" * 40,
                 "mtime": tree[c]["mtime"], "permission": "rw", **({} if tree[c]["dir"] else {"size": len(tree[c]["data"])})}
                for c in self._children(tree, p)])
        if req.method != "POST" or p == "/":
            raise error(400, "p invalid.")
        form = {key: values[0] for key, values in urllib.parse.parse_qs(req.body.decode()).items()}
        if form.get("operation") == "mkdir":
            parent = self._parent(p)
            if parent not in tree:
                if form.get("create_parents") != "true":
                    raise error(400, "Parent dir doesn't exist.")
                self._make_dirs(tree, parent)
            if not tree[parent]["dir"]:
                raise error(400, "Folder name invalid.")
            tree[self._join(parent, self._free_name(tree, parent, p.rsplit("/", 1)[1]))] = self._node(True)
            return json_response("success", 201)
        if form.get("operation") == "rename":
            node = tree.get(p)
            if node is None or not node["dir"]:
                raise error(404, f"Folder {p} not found.")
            target = self._join(self._parent(p), self._free_name(tree, self._parent(p), form["newname"]))
            self._relocate(tree, p, tree, target, keep=False)
            return json_response("success")
        raise error(400, "operation invalid.")

    def _file(self, req, library_id, library, p) -> Response:
        tree = library["tree"]
        node = tree.get(p)
        if node is None or node["dir"]:
            raise error(404, f"File {p} not found.")
        if req.method == "GET":  # the download link
            token = secrets.token_hex(12)
            self.links[token] = ("download", library_id, p)
            return json_response(f"{self.url}/seafhttp/files/{token}/{urllib.parse.quote(p.rsplit('/', 1)[1])}")
        form = {key: values[0] for key, values in urllib.parse.parse_qs(req.body.decode()).items()}
        if req.method == "POST" and form.get("operation") == "rename":
            target = self._join(self._parent(p), self._free_name(tree, self._parent(p), form["newname"]))
            tree[target] = tree.pop(p)
            return json_response("success")
        raise error(400, "operation invalid.")

    def _upload_link(self, req, library_id, library, p) -> Response:
        node = library["tree"].get(p)
        if node is None or not node["dir"]:
            raise error(404, f"Folder {p} not found.")
        token = secrets.token_hex(12)
        self.links[token] = ("upload", library_id, p)
        return json_response(f"{self.url}/seafhttp/upload-api/{token}")

    def _batch(self, req, path, user, body) -> Response:
        if path.endswith("batch-delete-item/"):
            if req.method != "DELETE":
                raise error(405, "Method not allowed.")
            tree = self._library(body["repo_id"], user)["tree"]
            for name in body["dirents"]:
                for p in self._below(tree, self._join(body["parent_dir"], name)):
                    tree.pop(p, None)
            return json_response({"success": True})
        source = self._library(body["src_repo_id"], user)["tree"]
        target = self._library(body["dst_repo_id"], user)["tree"]
        folder = body["dst_parent_dir"]
        if folder not in target or not target[folder]["dir"]:
            raise error(404, f"Folder {folder} not found.")
        for name in body["src_dirents"]:
            p = self._join(body["src_parent_dir"], name)
            if p not in source:
                raise error(404, f"File or folder {p} not found.")
            destination = self._join(folder, self._free_name(target, folder, name))
            self._relocate(source, p, target, destination, keep=path.endswith("copy-item/"))
        return json_response({"success": True})

    # -- the file server

    def _file_server(self, req, path) -> Response:
        with self.lock:
            self.file_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            match = re.fullmatch(r"/seafhttp/(files|upload-api)/([0-9a-f]+)(?:/.*)?", path)
            link = self.links.get(match.group(2)) if match else None
            if link is None:
                raise HTTPError(403, b"Access denied")
            kind, library_id, p = link
            tree = self.libraries[library_id]["tree"]
            if kind == "download":
                if not self._unlocked(library_id):  # accessCB's parseCryptKey
                    raise file_server_error(400, ENCRYPTED)
                node = tree.get(p)
                if node is None:
                    raise HTTPError(404, b"File not found")
                data = node["data"]
            else:
                stored = self._upload(req, library_id, tree)
        if kind == "upload":
            if stored.status == 200 and self.upload_delay:
                time.sleep(self.upload_delay)  # indexing the blocks and the commit, after the last byte
            return stored
        return file_response(data, req.header("Range"))

    def _upload(self, req, library_id: str, tree: dict[str, Any]) -> Response:
        """upload-api's answer."""
        if self.redirect_uploads:
            return Response(301, b"<html>301 Moved Permanently</html>",
                            {"Location": "https://seafile.example.invalid" + req.target, "Content-Type": "text/html"})
        if self.max_upload_size is not None and len(req.body) > self.max_upload_size:
            raise file_server_error(442, "File size is too large.\n", upload=True)
        if self.upload_answer is not None:
            return Response(200, self.upload_answer, {"Content-Type": "text/html"})
        fields = multipart(req.body, req.header("Content-Type"))
        parent = fields.get("parent_dir", (None, b""))[1].decode()
        filename, content = fields.get("file", (None, b""))
        if not filename or parent not in tree or not tree[parent]["dir"]:
            raise HTTPError(400, {"error": "Invalid URL."})
        if len(filename.encode()) >= 256:  # postMultiFiles' shouldIgnoreFile, before the password is looked at
            raise file_server_error(400, f"invalid fileName: {filename}.\n", upload=True)
        if not self._unlocked(library_id):  # postMultiFiles' parseCryptKey
            raise file_server_error(400, ENCRYPTED, upload=True)
        filename = unicodedata.normalize("NFC", filename)  # normalizeUTF8Path
        name = filename if fields.get("replace", (None, b"0"))[1] == b"1" else self._free_name(tree, parent, filename)
        target = self._join(parent, name)
        if target in tree and tree[target]["dir"]:
            raise HTTPError(400, {"error": "A folder has the name."})
        tree[target] = self._node(False, content)
        return json_response([{"name": name, "id": "0" * 40, "size": len(content)}])
