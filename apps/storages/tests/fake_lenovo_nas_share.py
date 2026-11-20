"""An in-memory fake of the share API of Lenovo's storage devices, as Lenovo's share server
(siot-share.lenovo.com.cn) answers it at /oneproxy/api/share/v1, and of its downloads (tests only, no Django).

    fake = FakeLenovoShare().start()
    fake.add_share("Ab.cDeF1234", "e5eu", {"/a.txt": b"...", "/d/b.bin": b"..."}, folders=("/empty",))
    fake.url
    fake.close()

It answers as the share page's JavaScript expects: HTTP 200 with {"result": true, "data": ...} or {"result": false,
"error": {"code", "msg"}}. access gives a share token for the share's extraction code: -40001 for another code,
-40004 for no such share, -40006, -40007 and -45005 for a share that has expired, was cancelled or whose device is
offline. files and file/link need the token (-40001 without it, or once it has expired) and refuse a path outside
the share (-40003) or one that isn't there (-40004). A share is a folder of the device's disk, below DISK, whose
files and folders are its root; items' paths are their paths on that disk. files lists the share's root without a
path, needs num and page (from 0) and says has_more; file/link gives a path to download from, with param {code,
dtoken}, and the download answers Range.

Knobs: expires_in (seconds, of the next tokens), expire_tokens(), a share's "state" (ok, expired, cancelled,
offline), ignore_page (files always answers the first page), link_base (file/link gives an absolute address on
it), link_with_token (param carries the share token too), ranges (False: downloads ignore Range), download_error
((code, msg): downloads answer the API's error, HTTP 200 and JSON), download_type (the Content-Type of downloads);
calls (the API calls and their queries), token_refusals (calls refused for their token) and downloads (their Range
headers).
"""

from __future__ import annotations

import secrets
import threading
import time
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

API = "/oneproxy/api/share/v1/"
DOWNLOAD = API + "file/download"
DISK = "/mnt/sda1/user 1/c++ & more"  # where the shared folders are on the device: names that need escaping
TIME = 1700000000  # every item's time and chtime (Unix seconds)
_KINDS = {"txt": "txt", "jpg": "pic", "png": "pic", "mp4": "video", "mp3": "audio", "pdf": "pdf", "zip": "zip"}


def ok(data: Any) -> Response:
    return json_response({"result": True, "data": data})


def refusal(code: int, msg: str) -> HTTPError:
    return HTTPError(200, {"result": False, "error": {"code": code, "msg": msg}})


class FakeLenovoShare:
    def __init__(self):
        self.lock = threading.Lock()
        self.shares: dict[str, dict[str, Any]] = {}  # share code -> {"password", "state", "root": its folder on disk}
        self.disk: dict[str, bytes | None] = {}  # path on the device -> the file's bytes, None for a folder
        self.tokens: dict[str, tuple[str, float]] = {}  # stoken -> (share code, expiry as time.monotonic())
        self.dtokens: dict[str, tuple[str, str]] = {}  # dtoken -> (share code, path on disk)
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.downloads: list[str | None] = []
        self.reset()
        self.server = FakeServer(self.handle)

    def start(self) -> FakeLenovoShare:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    def reset(self) -> None:
        with self.lock:
            self.shares.clear()
            self.disk.clear()
            self.tokens.clear()
            self.dtokens.clear()
            self.calls.clear()
            self.downloads.clear()
            self.expires_in = 7200
            self.ignore_page = False
            self.link_base = ""
            self.link_with_token = False
            self.ranges = True
            self.download_error: tuple[int, str] | None = None
            self.download_type: str | None = None
            self.token_refusals = 0

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def add_share(self, code: str, password: str, files: dict[str, bytes], folders: tuple[str, ...] = ()) -> None:
        """A share of the folder DISK/<code>, holding `files` (path in the share -> bytes) and `folders`."""
        root = f"{DISK}/{code}"
        with self.lock:
            self.shares[code] = {"password": password, "state": "ok", "root": root}
            self.disk[root] = None
            for folder in folders:
                self._folders(root + folder)
            for path, data in files.items():
                self._folders(root + path.rsplit("/", 1)[0])
                self.disk[root + path] = data

    def _folders(self, path: str) -> None:
        if path and path not in self.disk:
            self._folders(path.rsplit("/", 1)[0])
            self.disk[path] = None

    # -- requests

    def handle(self, req) -> Response:
        if req.path.endswith(DOWNLOAD):  # also below link_base
            return self._download(req)
        if req.method != "GET" or not req.path.startswith(API):
            raise HTTPError(404, b"404 page not found")
        call = req.path[len(API):]
        query = {name: values[0] for name, values in req.query.items()}
        with self.lock:
            self.calls.append((call, query))
            share = self.shares.get(query.get("code", ""))
            if call == "access":
                return self._access(share, query)
            if call == "files":
                return self._files(share, query)
            if call == "file/link":
                return self._link(share, query)
            raise HTTPError(404, b"404 page not found")

    @staticmethod
    def _state(share: dict[str, Any] | None) -> dict[str, Any]:
        if share is None:
            raise refusal(-40004, "share not exist")
        if share["state"] == "expired":
            raise refusal(-40006, "share is expired")
        if share["state"] == "cancelled":
            raise refusal(-40007, "share is canceled")
        if share["state"] == "offline":
            raise refusal(-45005, "device is offline")
        return share

    def _access(self, share: dict[str, Any] | None, query: dict[str, str]) -> Response:
        share = self._state(share)
        if query.get("password") != share["password"]:
            raise refusal(-40001, "password error")
        stoken = secrets.token_hex(16)
        self.tokens[stoken] = (query["code"], time.monotonic() + self.expires_in)
        return ok({"stoken": stoken, "expires_in": self.expires_in})

    def _checked(self, share: dict[str, Any] | None, query: dict[str, str]) -> dict[str, Any]:
        """The share of a call that needs its token."""
        share = self._state(share)
        found = self.tokens.get(query.get("stoken", ""))
        if found is None or found[0] != query["code"] or time.monotonic() >= found[1]:
            self.token_refusals += 1
            raise refusal(-40001, "stoken is invalid")
        return share

    def _target(self, share: dict[str, Any], query: dict[str, str]) -> str:
        """The path on disk that the query's path names, in the share."""
        path = query.get("path", "").rstrip("/")
        if path != share["root"] and not path.startswith(share["root"] + "/"):
            raise refusal(-40003, "permission denied")
        if path not in self.disk:
            raise refusal(-40004, "file not exist")
        return path

    def _item(self, path: str) -> dict[str, Any]:
        data = self.disk[path]
        folder, name = path.rsplit("/", 1)
        kind = "dir" if data is None else _KINDS.get(name.rpartition(".")[2].lower(), "other")
        return {"name": name, "type": kind, "size": 0 if data is None else len(data), "time": TIME, "chtime": TIME,
                "path": path, "dir": folder + "/"}

    def _files(self, share: dict[str, Any] | None, query: dict[str, str]) -> Response:
        share = self._checked(share, query)
        num, page = query.get("num", ""), query.get("page", "")
        if not (num.isdigit() and page.isdigit()) or int(num) < 1:
            raise refusal(-40002, "invalid params")
        folder = self._target(share, query) if "path" in query else share["root"]
        if self.disk[folder] is not None:
            raise refusal(-40002, "not a directory")
        children = sorted(path for path in self.disk if path != folder and path.rsplit("/", 1)[0] == folder)
        start = 0 if self.ignore_page else int(page) * int(num)
        return ok({"list": [self._item(path) for path in children[start:start + int(num)]],
                   "has_more": start + int(num) < len(children)})

    def _link(self, share: dict[str, Any] | None, query: dict[str, str]) -> Response:
        share = self._checked(share, query)
        path = self._target(share, query)
        if self.disk[path] is None:
            raise refusal(-40002, "not a file")
        dtoken = secrets.token_hex(16)
        self.dtokens[dtoken] = (query["code"], path)
        param = {"code": query["code"], "dtoken": dtoken}
        if self.link_with_token:
            param["stoken"] = query["stoken"]
        return ok({"url": self.link_base + DOWNLOAD, "param": param})

    def _download(self, req) -> Response:
        with self.lock:
            self.downloads.append(req.header("Range"))
            if self.download_error is not None:
                code, msg = self.download_error
                return ok(None) if code == 0 else json_response({"result": False, "error": {"code": code, "msg": msg}})
            found = self.dtokens.get(req.arg("dtoken") or "")
            if found is None or found[0] != req.arg("code"):
                raise HTTPError(403, {"result": False, "error": {"code": -40001, "msg": "dtoken is invalid"}})
            data = self.disk.get(found[1])
            if data is None:
                raise HTTPError(404, {"result": False, "error": {"code": -40004, "msg": "file not exist"}})
            ranges, kind = self.ranges, self.download_type
        return file_response(data, req.header("Range") if ranges else None, {"Content-Type": kind} if kind else None)
