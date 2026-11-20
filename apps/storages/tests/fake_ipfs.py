"""An in-memory fake of a Kubo node's RPC API (the files/* commands of MFS) and of its HTTP gateway (tests only).

    fake = FakeKubo().start()
    fake.api_url, fake.gateway_url
    fake.close()

It answers as Kubo does where the driver depends on it: POST only (405 otherwise); the path in "arg"; an error
is a 500 with {"Message", "Code": 0, "Type": "error"}; mv and cp put the item inside a folder that has the
target's name, and mv replaces a file of the target's name (boxo's mfs.Mv); mkdir with parents says nothing when
the folder is there; files/read takes offset and count; files/write takes the file as multipart/form-data,
truncates the file first and keeps what arrived when the body breaks off (core/commands/files.go: Truncate(0),
io.Copy, and a deferred Close and FlushPath). The gateway serves content by CID (/ipfs/<cid>), with Range.
Knobs: token (the RPC API then wants "Authorization: Bearer <token>"), read_encoding (a Content-Encoding that
files/read answers with, as a compressing proxy). Records: calls ((command, args)), read_headers (the
Accept-Encoding of each files/read).
"""

from __future__ import annotations

import hashlib
import re
import threading
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response
from .fake_seafile import multipart


def failure(message: str) -> HTTPError:
    return HTTPError(500, {"Message": message, "Code": 0, "Type": "error"})


def cid(data: bytes) -> str:
    return "bafkrei" + hashlib.sha256(data).hexdigest()[:52]


class FakeKubo:
    def __init__(self):
        self.lock = threading.Lock()
        self.token: str | None = None
        self.tree: dict[str, dict[str, Any]] = {"/": {"dir": True, "data": b""}}
        self.reset()
        self.server = FakeServer(self.handle)

    def start(self) -> FakeKubo:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def api_url(self) -> str:
        return self.server.url

    @property
    def gateway_url(self) -> str:
        return self.server.url + "/gw"

    def reset(self) -> None:
        with self.lock:
            self.tree = {"/": {"dir": True, "data": b""}}
            self.token = None
            self.read_encoding: str | None = None
            self.calls: list[tuple[str, list[str]]] = []
            self.read_headers: list[str | None] = []

    @staticmethod
    def _parent(path):
        return path.rsplit("/", 1)[0] or "/"

    @staticmethod
    def _join(folder, name):
        return folder.rstrip("/") + "/" + name

    def _children(self, path):
        prefix = "/" if path == "/" else path + "/"
        return sorted(p for p in self.tree if p != path and p.startswith(prefix) and "/" not in p[len(prefix):])

    def _below(self, path):
        return [p for p in self.tree if p == path or p.startswith(path.rstrip("/") + "/")]

    def _node(self, path):
        node = self.tree.get(path)
        if node is None:
            raise failure("file does not exist")
        return node

    def _make_dirs(self, path):
        if path in self.tree:
            if not self.tree[path]["dir"]:
                raise failure(f"{path} is not a directory")
            return
        self._make_dirs(self._parent(path))
        self.tree[path] = {"dir": True, "data": b""}

    def _destination(self, source, target, *, replace=False):
        """Kubo's mv and cp: into a folder that has the target's name; mv over a file replaces it."""
        if target in self.tree and self.tree[target]["dir"]:
            target = self._join(target, source.rsplit("/", 1)[1])
        if target in self.tree and replace and not self.tree[target]["dir"]:
            del self.tree[target]
        if target in self.tree:
            raise failure("directory already has entry by that name")
        if self.tree.get(self._parent(target), {}).get("dir") is not True:
            raise failure("file does not exist")
        return target

    def handle(self, req) -> Response:
        if req.path.startswith("/gw/ipfs/"):
            wanted = req.path[len("/gw/ipfs/"):]
            with self.lock:
                data = next((n["data"] for n in self.tree.values() if not n["dir"] and cid(n["data"]) == wanted), None)
            if data is None:
                raise HTTPError(404, b"not found")
            return file_response(data, req.header("Range"))
        if req.method != "POST":
            raise HTTPError(405, b"405 - Method Not Allowed")
        match = re.fullmatch(r"/api/v0/files/(\w+)", req.path)
        if not match:
            raise HTTPError(404, b"404 page not found")
        if self.token is not None and req.header("Authorization") != f"Bearer {self.token}":
            raise HTTPError(403, b"403 - Forbidden")
        args = req.query.get("arg", [])
        flag = lambda name: req.arg(name) == "true"  # noqa: E731
        with self.lock:
            command = match.group(1)
            self.calls.append((command, list(args)))
            if command == "stat":
                node = self._node(args[0])
                return json_response({"Hash": cid(node["data"]), "Size": 0 if node["dir"] else len(node["data"]),
                                      "CumulativeSize": len(node["data"]), "Type": "directory" if node["dir"] else "file"})
            if command == "ls":
                node = self._node(args[0])
                if not node["dir"]:
                    return json_response({"Entries": [{"Name": args[0].rsplit("/", 1)[1], "Type": 0, "Size": len(node["data"]), "Hash": ""}]})
                entries = [{"Name": c.rsplit("/", 1)[1], "Type": 1 if self.tree[c]["dir"] else 0,
                            "Size": 0 if self.tree[c]["dir"] else len(self.tree[c]["data"]), "Hash": cid(self.tree[c]["data"])}
                           for c in self._children(args[0])]
                return json_response({"Entries": entries or None})
            if command == "read":
                node = self._node(args[0])
                if node["dir"]:
                    raise failure(f"{args[0]} was not a file")
                offset = int(req.arg("offset", "0"))
                count = req.arg("count")
                data = node["data"][offset:] if count is None else node["data"][offset:offset + int(count)]
                self.read_headers.append(req.header("Accept-Encoding"))
                headers = {"Content-Type": "application/octet-stream", "Transfer-Encoding": "chunked"}
                if self.read_encoding:
                    headers["Content-Encoding"] = self.read_encoding
                return Response(200, iter([data[i:i + 65536] for i in range(0, len(data), 65536)]), headers)
            if command == "write":
                path = args[0]
                if path in self.tree and self.tree[path]["dir"]:
                    raise failure(f"{path} was not a file")
                if path not in self.tree and not flag("create"):
                    raise failure("file does not exist")
                if flag("parents"):
                    self._make_dirs(self._parent(path))
                elif self._parent(path) not in self.tree:
                    raise failure("file does not exist")
                _, data = multipart(req.body, req.header("Content-Type"))["file"]
                old = b"" if flag("truncate") else self.tree.get(path, {"data": b""})["data"]
                self.tree[path] = {"dir": False, "data": data if flag("truncate") else old[:0] + data}
                if len(req.body) < int(req.header("Content-Length") or 0):  # broke off: what arrived is kept
                    raise failure("unexpected EOF")
                return Response(200)
            if command == "mkdir":
                path = args[0]
                if path in self.tree and not flag("parents"):
                    raise failure("file already exists")
                if flag("parents"):
                    self._make_dirs(path)
                else:
                    if self._parent(path) not in self.tree:
                        raise failure("file does not exist")
                    self.tree[path] = {"dir": True, "data": b""}
                return Response(200)
            if command in ("mv", "cp"):
                source = args[0]
                self._node(source)
                target = self._destination(source, args[1], replace=command == "mv")
                for old in sorted(self._below(source)):
                    node = self.tree[old] if command == "cp" else self.tree.pop(old)
                    self.tree[target + old[len(source):]] = dict(node)
                return Response(200)
            if command == "rm":
                node = self._node(args[0])
                if node["dir"] and not flag("recursive"):
                    raise failure(f"{args[0]} is a directory, use -r to remove directories")
                for path in self._below(args[0]):
                    self.tree.pop(path)
                return Response(200)
        raise HTTPError(404, b"404 page not found")
