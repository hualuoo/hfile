"""An in-memory fake of the HTTP API of Example Drive, the made-up service of the example driver
(apps/storages/drivers/example.py; docs/development.md, 添加存储源). Tests only, no Django.

    fake = FakeExampleDrive().start()
    fake.url, fake.token
    fake.close()

Example Drive doesn't exist, so this is the documentation of its API as well. The fake of a real service models
what the service's documentation says, and what the service is seen to do; it refuses what the service refuses (a
wrong token, a taken name), so that the tests catch a driver's mistakes.

The API is under <address>/api/v1 and speaks JSON:
- Every call carries "Authorization: Bearer <token>", with the token made on the server's settings page.
- An error is {"error": {"code", "message"}} with its HTTP status: 400 invalid, 401 unauthorized, 404 not_found,
  409 exists or not_a_folder, 429 too_many_requests (with Retry-After, in seconds).
- An item is {"name", "type": "file" or "folder", "size" (a file's), "modified" (RFC 3339)}.
- Paths start with "/" and have no empty, "." or ".." parts; a name has at most 255 bytes.

    GET /items?path=            the item; the root "/" is a folder named "".
    GET /children?path=&limit=&cursor=
                                the items in a folder, `limit` (1 to 1000, 100 by default) a page: {"items": [...],
                                "next": the cursor of the next page, or null}. A file is 409 not_a_folder.
    GET /content?path=          the bytes of a file (a folder is 404 not_found); with a Range, 206 and those bytes.
    PUT /content?path=          stores the body as the file, and makes the missing folders above it; replaces a
                                file of that name, but a folder of that name is 409 exists. 201.
    POST /links {"path", "expires", "filename"}
                                {"url"}: a link that sends the file without the token for `expires` seconds (60 to
                                86400), as an attachment named `filename`.
    POST /folders {"path"}      makes the folder and the missing ones above it; 409 exists when the path is taken.
                                201.
    POST /move {"from", "to"}, POST /copy {"from", "to"}
                                moves or copies a file or folder to the path `to`, never replacing anything: 409
                                exists when `to` is taken; a folder into itself is 400 invalid; 404 when `from` or
                                the folder of `to` is missing.
    DELETE /items?path=         deletes a file, or a folder with everything in it; the root is 400 invalid. 204.

Knobs: throttle (the next N calls answer 429), calls ((method, path) of each API call), link_requests (the headers
each link got).
"""

from __future__ import annotations

import datetime
import secrets
import threading
import time
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

API = "/api/v1"


def error(status: int, code: str, message: str) -> HTTPError:
    return HTTPError(status, {"error": {"code": code, "message": message}})


class FakeExampleDrive:
    def __init__(self):
        self.lock = threading.Lock()
        self.token = "example-" + secrets.token_hex(16)
        self.tree: dict[str, dict[str, Any]] = {}  # path -> {"folder": bool, "data": bytes, "modified": datetime}
        self.links: dict[str, tuple[str, float, str]] = {}  # link id -> (path, valid until, filename)
        self.throttle = 0
        self.calls: list[tuple[str, str]] = []
        self.link_requests: list[dict[str, str | None]] = []
        self.reset()
        self.server = FakeServer(self.handle)

    def start(self) -> FakeExampleDrive:
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
            self.links.clear()
            self.throttle = 0
            self.calls.clear()
            self.link_requests.clear()

    def make_folder(self, path: str) -> None:
        """A folder made on the server itself, with the ones above it (a test's base path)."""
        with self.lock:
            self._make_folders(path)

    # -- the tree

    @staticmethod
    def _node(folder: bool, data: bytes = b"") -> dict[str, Any]:
        return {"folder": folder, "data": data, "modified": datetime.datetime.now(datetime.timezone.utc)}

    @staticmethod
    def _path(value: Any) -> str:
        if not isinstance(value, str) or not value.startswith("/"):
            raise error(400, "invalid", "A path starts with /.")
        if value != "/" and any(part in ("", ".", "..") or len(part.encode()) > 255 for part in value[1:].split("/")):
            raise error(400, "invalid", f"Not a path: {value}")
        return value

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    def _below(self, path: str) -> list[str]:
        """The path and every path below it."""
        prefix = "/" if path == "/" else path + "/"
        return [other for other in self.tree if other == path or other.startswith(prefix)]

    def _children(self, path: str) -> list[str]:
        prefix = "/" if path == "/" else path + "/"
        return sorted(other for other in self.tree if other != path and other.startswith(prefix) and "/" not in other[len(prefix):])

    def _existing(self, path: str) -> dict[str, Any]:
        node = self.tree.get(path)
        if node is None:
            raise error(404, "not_found", f"{path} doesn't exist.")
        return node

    def _make_folders(self, path: str) -> None:
        node = self.tree.get(path)
        if node is not None:
            if not node["folder"]:
                raise error(409, "not_a_folder", f"{path} is a file.")
            return
        self._make_folders(self._parent(path))
        self.tree[path] = self._node(True)

    def _item(self, path: str) -> dict[str, Any]:
        node = self.tree[path]
        item = {"name": path.rsplit("/", 1)[1], "type": "folder" if node["folder"] else "file",
                "modified": node["modified"].isoformat().replace("+00:00", "Z")}
        if not node["folder"]:
            item["size"] = len(node["data"])
        return item

    # -- requests

    def handle(self, req) -> Response:
        if req.path.startswith("/d/"):
            return self._download(req)
        if not req.path.startswith(API + "/"):
            raise HTTPError(404, b"Not found")
        calls = {
            ("GET", "/items"): self._get_item,
            ("GET", "/children"): self._get_children,
            ("GET", "/content"): self._get_content,
            ("PUT", "/content"): self._put_content,
            ("POST", "/links"): self._post_link,
            ("POST", "/folders"): self._post_folder,
            ("POST", "/move"): lambda req: self._relocate(req, copy=False),
            ("POST", "/copy"): lambda req: self._relocate(req, copy=True),
            ("DELETE", "/items"): self._delete_item,
        }
        with self.lock:
            self.calls.append((req.method, req.path))
            if req.header("Authorization") != f"Bearer {self.token}":
                raise error(401, "unauthorized", "The token is wrong or was revoked.")
            if self.throttle:
                self.throttle -= 1
                raise HTTPError(429, {"error": {"code": "too_many_requests", "message": "Slow down."}}, {"Retry-After": "0"})
            call = calls.get((req.method, req.path[len(API):]))
            if call is None:
                raise error(404, "not_found", "No such call.")
            return call(req)

    def _get_item(self, req) -> Response:
        path = self._path(req.arg("path"))
        self._existing(path)
        return json_response(self._item(path))

    def _get_children(self, req) -> Response:
        path = self._path(req.arg("path"))
        if not self._existing(path)["folder"]:
            raise error(409, "not_a_folder", f"{path} is a file.")
        limit, start = req.arg("limit") or "100", req.arg("cursor") or "0"
        if not (limit.isdigit() and 1 <= int(limit) <= 1000 and start.isdigit()):
            raise error(400, "invalid", "The limit is 1 to 1000, the cursor one of a page.")
        children = self._children(path)
        page = children[int(start):int(start) + int(limit)]
        more = int(start) + int(limit) < len(children)
        return json_response({"items": [self._item(child) for child in page], "next": str(int(start) + int(limit)) if more else None})

    def _get_content(self, req) -> Response:
        path = self._path(req.arg("path"))
        node = self._existing(path)
        if node["folder"]:
            raise error(404, "not_found", f"{path} is a folder.")
        return file_response(node["data"], req.header("Range"))

    def _put_content(self, req) -> Response:
        path = self._path(req.arg("path"))
        if path in self.tree and self.tree[path]["folder"]:
            raise error(409, "exists", f"A folder is at {path}.")
        self._make_folders(self._parent(path))
        self.tree[path] = self._node(False, req.body)
        return json_response(self._item(path), 201)

    def _post_link(self, req) -> Response:
        body = req.json() or {}
        path = self._path(body.get("path"))
        if self._existing(path)["folder"]:
            raise error(404, "not_found", f"{path} is a folder.")
        expires, filename = body.get("expires"), body.get("filename")
        if not isinstance(expires, int) or not 60 <= expires <= 86400 or not isinstance(filename, str) or not filename:
            raise error(400, "invalid", "A link needs expires (60 to 86400 seconds) and a filename.")
        link = secrets.token_urlsafe(16)
        self.links[link] = (path, time.time() + expires, filename)
        return json_response({"url": f"{self.url}/d/{link}"})

    def _download(self, req) -> Response:
        with self.lock:
            self.link_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            path, until, filename = self.links.get(req.path[len("/d/"):], ("", 0.0, ""))
            node = self.tree.get(path) if time.time() < until else None
            if node is None or node["folder"]:
                raise HTTPError(404, b"This link doesn't work any more.")
            disposition = "attachment; filename*=UTF-8''" + urllib.parse.quote(filename, safe="")
            return file_response(node["data"], req.header("Range"), {"Content-Disposition": disposition})

    def _post_folder(self, req) -> Response:
        path = self._path((req.json() or {}).get("path"))
        if path in self.tree:
            raise error(409, "exists", f"{path} is taken.")
        self._make_folders(path)
        return json_response(self._item(path), 201)

    def _relocate(self, req, *, copy: bool) -> Response:
        body = req.json() or {}
        source, target = self._path(body.get("from")), self._path(body.get("to"))
        self._existing(source)
        if source == "/" or target.startswith(source + "/"):
            raise error(400, "invalid", "The root can't be moved or copied, nor a folder into itself.")
        if not self._existing(self._parent(target))["folder"]:
            raise error(409, "not_a_folder", f"{self._parent(target)} is a file.")
        if target in self.tree:
            raise error(409, "exists", f"{target} is taken.")
        moved = {target + path[len(source):]: dict(self.tree[path]) for path in self._below(source)}
        if not copy:
            for path in self._below(source):
                del self.tree[path]
        self.tree.update(moved)
        return json_response(self._item(target))

    def _delete_item(self, req) -> Response:
        path = self._path(req.arg("path"))
        if path == "/":
            raise error(400, "invalid", "The root can't be deleted.")
        self._existing(path)
        for below in self._below(path):
            del self.tree[below]
        return Response(204)
