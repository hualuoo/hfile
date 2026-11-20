"""An in-memory fake of the Yandex Disk REST API and of Yandex's OAuth token endpoint (tests only, no Django).

    fake = FakeYandex().start()
    fake.api_url, fake.token_url, fake.client_id, fake.client_secret
    fake.close()

It answers as Yandex does where the driver depends on it:
- "Authorization: OAuth <token>"; a refused token is a 401 UnauthorizedError.
- Paths are "disk:/a/b".
- Folders are paged by limit and offset in "_embedded".
- PUT /resources makes one folder: a 409 when it is there, or when its parent isn't.
- move and copy with overwrite=false refuse a taken name (409 DiskResourceAlreadyExistsError).
- Deleting, moving or copying a folder runs as an operation: a 202 with a link to it, then "in-progress" once,
  then "success". The link is .../operations/<id>, or .../operations?id=<id> as the docs' examples write it
  (operation_form "query").
- New names are at most 255 characters ("Максимальная длина имени загружаемого файла — 255 символов", the upload
  page), else a 400 [assumed: the docs give no error for it].
- A refresh gives a new refresh token ("Яндекс OAuth возвращает токен и новый refresh-токен", Yandex ID's refresh
  page); with retire_refresh_tokens the old one stops working [assumed: the docs don't say].
- Download and upload links refuse a token (they are their own permission).
- Errors are {"error", "description", "message"}.

Knobs: expire_tokens(), throttle (the next N API calls answer 429), refreshes (refresh-token grants), and
link_requests (the headers the links got); for operations: operation_form, operation_href (a link to give instead),
operation_polls ("in-progress" answers before the end), operation_result ("success" or "failed"),
apply_operations (False: a 202's operation changes nothing) and operation_requests (polls counted); for tokens:
retire_refresh_tokens and token_delay (seconds a refresh takes).
"""

from __future__ import annotations

import datetime
import secrets
import threading
import time
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response


MAX_NAME_CHARS = 255


def error(status: int, code: str) -> HTTPError:
    return HTTPError(status, {"error": code, "description": code, "message": code})


class FakeYandex:
    def __init__(self):
        self.lock = threading.Lock()
        self.client_id, self.client_secret = "yandex-app", "yandex-secret"
        self.codes: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.tokens: set[str] = set()
        self.tree: dict[str, dict[str, Any]] = {}
        self.links: dict[str, tuple[str, str]] = {}  # token -> ("download" | "upload", path)
        self.operations: dict[str, int] = {}  # id -> polls left before "success"
        self.throttle = 0
        self.refreshes = 0
        self.link_requests: list[dict[str, str | None]] = []
        self.retire_refresh_tokens = False
        self.token_delay = 0.0
        self.reset()
        self.server = FakeServer(self.handle)

    def start(self) -> FakeYandex:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def api_url(self) -> str:
        return self.server.url + "/v1/disk"

    @property
    def token_url(self) -> str:
        return self.server.url + "/token"

    def reset(self) -> None:
        with self.lock:
            self.tree = {"/": self._node(True)}
            self.links.clear()
            self.operations.clear()
            self.throttle = 0
            self.link_requests.clear()
            self.operation_form = "path"
            self.operation_href: str | None = None
            self.operation_polls = 1
            self.operation_result = "success"
            self.apply_operations = True
            self.operation_requests = 0

    def issue_code(self) -> str:
        with self.lock:
            code = secrets.token_hex(8)
            self.codes.add(code)
            return code

    def issue_refresh_token(self) -> str:
        with self.lock:
            token = secrets.token_hex(16)
            self.refresh_tokens.add(token)
            return token

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    @staticmethod
    def _node(is_dir: bool, data: bytes = b"") -> dict[str, Any]:
        return {"dir": is_dir, "data": data, "modified": datetime.datetime.now(datetime.timezone.utc)}

    # -- paths

    @staticmethod
    def _path(value: str | None) -> str:
        if not value or not value.startswith("disk:/"):
            raise error(400, "FieldValidationError")
        return "/" + "/".join(p for p in value[len("disk:"):].split("/") if p)

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit("/", 1)[0] or "/"

    @staticmethod
    def _new_name(path: str) -> None:
        """A 400 for a new name longer than Yandex takes."""
        if len(path.rsplit("/", 1)[1]) > MAX_NAME_CHARS:
            raise error(400, "FieldValidationError")

    def _children(self, path):
        prefix = "/" if path == "/" else path + "/"
        return sorted(p for p in self.tree if p != path and p.startswith(prefix) and "/" not in p[len(prefix):])

    def _below(self, path):
        return [p for p in self.tree if p == path or p.startswith(path.rstrip("/") + "/")]

    def _resource(self, path: str) -> dict[str, Any]:
        node = self.tree[path]
        item = {"name": path.rsplit("/", 1)[1] if path != "/" else "disk", "path": "disk:" + path,
                "type": "dir" if node["dir"] else "file", "modified": node["modified"].isoformat(timespec="seconds")}
        if not node["dir"]:
            item["size"] = len(node["data"])
        return item

    def _operation(self, response_status: int = 202) -> Response:
        operation = secrets.token_hex(6)
        self.operations[operation] = self.operation_polls
        if self.operation_href is not None:
            href = self.operation_href
        elif self.operation_form == "query":
            href = f"{self.api_url}/operations?id={operation}"
        else:
            href = f"{self.api_url}/operations/{operation}"
        return json_response({"href": href, "method": "GET", "templated": False}, response_status)

    # -- requests

    def handle(self, req) -> Response:
        path = req.path
        if path == "/token":
            return self._token(req)
        if path.startswith("/links/"):
            return self._link(req)
        if not path.startswith("/v1/disk/"):
            raise HTTPError(404, b"no such endpoint")
        with self.lock:
            header = req.header("Authorization") or ""
            if not header.startswith("OAuth ") or header[len("OAuth "):] not in self.tokens:
                raise error(401, "UnauthorizedError")
            if self.throttle > 0:
                self.throttle -= 1
                raise HTTPError(429, {"error": "TooManyRequestsError", "description": "slow down", "message": "x"},
                                {"Retry-After": "0"})
            return self._api(req, path[len("/v1/disk"):])

    def _token(self, req) -> Response:
        form = {k: v[0] for k, v in urllib.parse.parse_qs(req.body.decode()).items()}
        if self.token_delay:
            time.sleep(self.token_delay)  # refreshes overlap
        with self.lock:
            if form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
                raise HTTPError(400, {"error": "invalid_client", "error_description": "Client not found"})
            if form.get("grant_type") == "authorization_code" and form.get("code") in self.codes:
                self.codes.discard(form["code"])
                refresh = secrets.token_hex(16)
                self.refresh_tokens.add(refresh)
            elif form.get("grant_type") == "refresh_token" and form.get("refresh_token") in self.refresh_tokens:
                if self.retire_refresh_tokens:
                    self.refresh_tokens.discard(form["refresh_token"])
                refresh = secrets.token_hex(16)
                self.refresh_tokens.add(refresh)
                self.refreshes += 1
            else:
                raise HTTPError(400, {"error": "invalid_grant", "error_description": "Code has expired"})
            access = secrets.token_hex(16)
            self.tokens.add(access)
        return json_response({"token_type": "bearer", "access_token": access, "expires_in": 31536000, "refresh_token": refresh})

    def _api(self, req, api: str) -> Response:
        if api == "/operations" or api.startswith("/operations/"):
            operation = req.arg("id") if api == "/operations" else api[len("/operations/"):]
            self.operation_requests += 1
            left = self.operations.get(operation or "")
            if left is None:
                raise error(404, "DiskNotFoundError")
            if left > 0:
                self.operations[operation] = left - 1
                return json_response({"status": "in-progress"})
            return json_response({"status": self.operation_result})
        if api == "/resources" and req.method == "GET":
            path = self._path(req.arg("path"))
            if path not in self.tree:
                raise error(404, "DiskNotFoundError")
            item = self._resource(path)
            if self.tree[path]["dir"]:
                children = self._children(path)
                limit, offset = int(req.arg("limit", "20")), int(req.arg("offset", "0"))
                item["_embedded"] = {"items": [self._resource(c) for c in children[offset:offset + limit]],
                                     "limit": limit, "offset": offset, "total": len(children), "path": "disk:" + path}
            return json_response(item)
        if api == "/resources" and req.method == "PUT":
            path = self._path(req.arg("path"))
            self._new_name(path)
            if path in self.tree:
                raise error(409, "DiskPathPointsToExistentDirectoryError")
            if self.tree.get(self._parent(path), {}).get("dir") is not True:
                raise error(409, "DiskPathDoesntExistsError")
            self.tree[path] = self._node(True)
            return json_response({"href": f"{self.api_url}/resources?path=disk:{path}", "method": "GET"}, 201)
        if api == "/resources" and req.method == "DELETE":
            path = self._path(req.arg("path"))
            if path not in self.tree or path == "/":
                raise error(404, "DiskNotFoundError")
            was_dir = self.tree[path]["dir"]
            if was_dir and not self.apply_operations:
                return self._operation()
            for p in self._below(path):
                self.tree.pop(p)
            return self._operation() if was_dir else Response(204)
        if api in ("/resources/move", "/resources/copy") and req.method == "POST":
            source, target = self._path(req.arg("from")), self._path(req.arg("path"))
            if source not in self.tree:
                raise error(404, "DiskNotFoundError")
            if target in self.tree and req.arg("overwrite") != "true":
                raise error(409, "DiskResourceAlreadyExistsError")
            if self.tree.get(self._parent(target), {}).get("dir") is not True:
                raise error(409, "DiskPathDoesntExistsError")
            self._new_name(target)
            was_dir = self.tree[source]["dir"]
            if was_dir and not self.apply_operations:
                return self._operation()
            for old in sorted(self._below(source)):
                node = self.tree[old] if api.endswith("copy") else self.tree.pop(old)
                self.tree[target + old[len(source):]] = dict(node)
            if was_dir:
                return self._operation()
            return json_response({"href": f"{self.api_url}/resources?path=disk:{target}", "method": "GET"}, 201)
        if api == "/resources/download" and req.method == "GET":
            path = self._path(req.arg("path"))
            if path not in self.tree or self.tree[path]["dir"]:
                raise error(404, "DiskNotFoundError")
            token = secrets.token_hex(10)
            self.links[token] = ("download", path)
            return json_response({"href": f"{self.server.url}/links/{token}", "method": "GET", "templated": False})
        if api == "/resources/upload" and req.method == "GET":
            path = self._path(req.arg("path"))
            self._new_name(path)
            if path in self.tree and (self.tree[path]["dir"] or req.arg("overwrite") != "true"):
                raise error(409, "DiskResourceAlreadyExistsError")
            if self.tree.get(self._parent(path), {}).get("dir") is not True:
                raise error(409, "DiskPathDoesntExistsError")
            token = secrets.token_hex(10)
            self.links[token] = ("upload", path)
            return json_response({"operation_id": token, "href": f"{self.server.url}/links/{token}", "method": "PUT"})
        raise error(404, "DiskNotFoundError")

    def _link(self, req) -> Response:
        with self.lock:
            self.link_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            if req.header("Authorization"):
                raise HTTPError(400, b"no tokens on links")
            kind, path = self.links.get(req.path[len("/links/"):], (None, None))
            if kind == "download" and req.method == "GET":
                node = self.tree.get(path)
                if node is None:
                    raise HTTPError(404, b"gone")
                data = node["data"]
            elif kind == "upload" and req.method == "PUT":
                self.tree[path] = self._node(False, req.body)
                return Response(201)
            else:
                raise HTTPError(404, b"no such link")
        return file_response(data, req.header("Range"))
