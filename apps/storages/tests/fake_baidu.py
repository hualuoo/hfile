"""An in-memory fake of the Baidu Netdisk open platform for the driver tests (no Django).

One FakeServer plays all of Baidu's hosts: the OAuth service (/oauth/2.0/token), the xpan API (/rest/2.0/xpan/...),
the PCS hosts that locate uploads and take the slices (/rest/2.0/pcs/file, /rest/2.0/pcs/superfile2), the
download links (/file/...) and the CDN they redirect to (/cdn/...). A second server is a host that isn't Baidu's:
locateupload names it first, and nothing must ever reach it.

It models the API as Baidu's docs (https://pan.baidu.com/union/doc/) describe it, and as OpenList's driver shows
the service behaves where they say nothing:
- tokens go in the query string; access tokens expire (20016), a refresh replaces the access token (the old one
  answers -6) and the refresh token, which works once; a refused refresh kills the refresh token too, as the docs
  say; a withdrawn authorisation answers 20017; the token endpoint takes GET, as the docs show it;
- every path must be in the app's folder /apps/<APP_NAME> (20020; precreate 31064), as for apps made after
  3 June 2026;
- list pages with start/limit, at most page_limit entries a page; -9 for a missing folder, an empty list for a file;
- precreate (no empty files; a rapid upload, return_type 2, when the whole file's MD5 and its first 256 KiB's
  match content it has; else an uploadid and the slices it doesn't have yet), locateupload (appid 250528,
  upload_version 2.0), superfile2 (multipart, the field "file"; slices at most the membership's size, the first
  of several at least base_slice), create (the slices put together by their MD5s, the membership's file size;
  rtype 0 fails on a taken path, 3 replaces a file);
- filemanager copy, move, rename and delete, with each item's errno in "info"; deletes go to the recycle bin;
- filemetas by fsids gives dlinks (with an encoded "=" in their sign, which must come back as it was), which need
  the access token and User-Agent pan.baidu.com, answer their errors as JSON with download_error_status (HTTP 200,
  as the docs' table says), and redirect to the CDN, which needs that User-Agent and takes Range.
Assumptions where neither says: errno 12 for a filemanager call with failed items, -8 for a taken target, -9 for a
missing parent folder (Baidu may make it), -7 for names with \\ : * ? " < > | or of more than 255 characters,
and -6 for unknown access tokens (110 and 111 on the PCS hosts).
"""

from __future__ import annotations

import collections
import dataclasses
import hashlib
import itertools
import json
import re
import secrets
import threading
import time
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

APP_KEY = "EVaI5x0U6lEmP125G0Su55ROEXZtItdD"
SECRET_KEY = "VPgfmrt8UBM5kgkeUemwRVmr5AjhFuEV"
APP_NAME = "HFile"
APP_FOLDER = "/apps/" + APP_NAME
USER_AGENT = "pan.baidu.com"
FORBIDDEN = '\\:*?"<>|'
KiB, MiB, GiB = 1024, 1024 ** 2, 1024 ** 3
TOKEN_LIFETIME = 30 * 24 * 3600
_MD5 = re.compile(r"[0-9a-f]{32}")


class Fail(Exception):
    """An error answer: errno (xpan, superfile2) or error_code (locateupload, the OpenAPI token codes)."""

    def __init__(self, code: int, message: str = ""):
        super().__init__(code, message)
        self.code = code
        self.message = message


@dataclasses.dataclass
class Node:
    path: str
    fs_id: int
    is_dir: bool
    data: bytes = b""
    ctime: int = 0
    mtime: int = 0

    @property
    def name(self) -> str:
        return self.path.rpartition("/")[2]


@dataclasses.dataclass
class Grant:
    """One authorisation of the app by the user, and its tokens now."""

    scope: str
    access: str = ""
    refresh: str = ""
    revoked: bool = False


def parent_of(path: str) -> str:
    return path.rpartition("/")[0] or "/"


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def oauth_error(status: int, error: str, description: str) -> Response:
    return json_response({"error": error, "error_description": description}, status)


def multipart_file(request: Request) -> bytes | None:
    """The field "file" of a multipart/form-data body, or None."""
    match = re.match(r'multipart/form-data;\s*boundary="?([^";]+)"?', request.header("Content-Type") or "")
    if match is None:
        return None
    parts = request.body.split(b"--" + match[1].encode("ascii"))
    if len(parts) < 3 or parts[0] != b"" or not parts[-1].startswith(b"--"):
        return None
    for part in parts[1:-1]:
        head, separator, content = part.partition(b"\r\n\r\n")
        if separator and content.endswith(b"\r\n") and re.search(rb'name="file"', head):
            return content[:-2]
    return None


class FakeBaidu:
    def __init__(self):
        self.lock = threading.RLock()
        self.server = FakeServer(self.handle)
        self.other = FakeServer(self.handle_other)
        self.reset()

    # -- lifecycle and test helpers

    def start(self) -> FakeBaidu:
        self.server.start()
        self.other.start()
        return self

    def close(self) -> None:
        self.server.close()
        self.other.close()

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def other_url(self) -> str:
        return self.other.url.replace("127.0.0.1", "localhost")

    def reset(self, *, app_folder: bool = True) -> None:
        with self.lock:
            self.clock = int(time.time()) - 3600  # a logical clock: every change is one second later
            self.ids = itertools.count(560000000000001)
            self.nodes: dict[str, Node] = {"/": Node("/", 1, True)}
            self.trash: list[Node] = []
            self.grants: dict[str, Grant] = {}
            self.access: dict[str, tuple[str, float]] = {}  # access token -> (grant, expiry)
            self.refresh: dict[str, str] = {}  # refresh token -> grant
            self.spent: set[str] = set()  # refresh tokens used up
            self.codes: dict[str, dict[str, Any]] = {}  # authorization code -> what it grants
            self.uploads: dict[str, dict[str, Any]] = {}  # uploadid -> what precreate was told
            self.blocks: dict[str, bytes] = {}  # MD5 -> a slice Baidu has
            self.contents: dict[str, bytes] = {}  # MD5 -> a file's bytes Baidu has (rapid uploads)
            self.links: dict[str, tuple[int, str, float]] = {}  # dlink token -> (fs_id, sign, expiry)
            self.cdn: dict[str, int] = {}  # CDN token -> fs_id
            self.vip_type = 0
            self.base_slice = 4 * MiB
            self.max_blocks = 1024
            self.limits = {0: (4 * GiB, 4 * MiB), 1: (10 * GiB, 16 * MiB), 2: (20 * GiB, 32 * MiB)}
            self.page_limit = 1000
            self.link_lifetime = 8 * 3600.0
            self.redirect_downloads = True
            self.download_error_status = 200
            self.fail_next: dict[str, Any] = {}  # method -> errno (int) or ("http", status), once
            self.broken_slices = 0  # superfile2 answers HTTP 500 this many times
            self.corrupt_slices = 0  # superfile2 keeps this many slices with a byte changed
            self.upload_servers: list[str] | None = None  # locateupload's servers; None: the other host, then this one
            self.counts: collections.Counter[str] = collections.Counter()
            self.slices: list[tuple[int, int]] = []  # (partseq, bytes) of each slice superfile2 took
            self.leaks: list[str] = []  # where the access token went that it must not
            self.user_agents: set[str] = set()
            self.other_requests: list[str] = []
            self.mkdirs("/apps")
            if app_folder:
                self.mkdirs(APP_FOLDER)

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    def issue_tokens(self, lifetime: int = TOKEN_LIFETIME, scope: str = "basic netdisk") -> dict[str, Any]:
        """A fresh authorisation, as 授权 gives it; returns the driver's params."""
        with self.lock:
            grant = secrets.token_hex(6)
            self.grants[grant] = Grant(scope)
            tokens = self._new_tokens(grant, lifetime)
            return {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"],
                    "token_expires_at": int(time.time()) + lifetime}

    def issue_code(self, redirect_uri: str, scope: str = "basic netdisk") -> str:
        """The code Baidu sends back to the redirect URI once the user agreed."""
        with self.lock:
            code = secrets.token_hex(16)
            self.codes[code] = {"redirect_uri": redirect_uri, "scope": scope, "expiry": time.time() + 600, "used": False}
            return code

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access = {token: (grant, 0.0) for token, (grant, _) in self.access.items()}

    def revoke(self) -> None:
        """The user withdrew the authorisation in Baidu Netdisk's settings."""
        with self.lock:
            for grant in self.grants.values():
                grant.revoked = True

    def expire_links(self) -> None:
        with self.lock:
            self.links = {token: (fs_id, sign, 0.0) for token, (fs_id, sign, _) in self.links.items()}

    def find(self, path: str) -> Node | None:
        with self.lock:
            return self.nodes.get(path)

    def mkdirs(self, path: str) -> Node:
        with self.lock:
            current = ""
            for name in [part for part in path.split("/") if part]:
                current += "/" + name
                node = self.nodes.get(current)
                if node is None:
                    node = self._add(current, True)
                elif not node.is_dir:
                    raise ValueError(f"{current} is a file")
            return self.nodes[path] if path in self.nodes else self.nodes["/"]

    def put_file(self, path: str, data: bytes) -> Node:
        with self.lock:
            self.mkdirs(parent_of(path))
            old = self.nodes.get(path)
            if old is not None:
                del self.nodes[path]
            return self._add(path, False, data)

    def children(self, folder: str) -> list[Node]:
        with self.lock:
            return sorted((node for path, node in self.nodes.items() if path != "/" and parent_of(path) == folder),
                          key=lambda node: node.name)

    def _add(self, path: str, is_dir: bool, data: bytes = b"") -> Node:
        now = self.tick()
        node = Node(path, next(self.ids), is_dir, data, now, now)
        self.nodes[path] = node
        if not is_dir:
            self.contents[md5_hex(data)] = data
        return node

    def _by_id(self, fs_id: int) -> Node | None:
        return next((node for node in self.nodes.values() if node.fs_id == fs_id), None)

    def _new_tokens(self, grant_id: str, lifetime: int = TOKEN_LIFETIME) -> dict[str, Any]:
        grant = self.grants[grant_id]
        if grant.refresh:
            self.refresh.pop(grant.refresh, None)
        grant.access = f"121.{secrets.token_hex(16)}.{secrets.token_urlsafe(24)}.LPOBOA"
        grant.refresh = f"122.{secrets.token_hex(16)}.{secrets.token_urlsafe(24)}.DbEWAW"
        self.access[grant.access] = (grant_id, time.time() + lifetime)
        self.refresh[grant.refresh] = grant_id
        return {"expires_in": lifetime, "refresh_token": grant.refresh, "access_token": grant.access,
                "session_secret": "", "session_key": "", "scope": grant.scope}

    # -- dispatch

    def handle(self, request: Request) -> Response:
        path = request.path
        with self.lock:
            self.user_agents.add(request.header("User-Agent") or "")
            if path == "/oauth/2.0/token":
                return self._token(request)
            if path in ("/rest/2.0/xpan/file", "/rest/2.0/xpan/multimedia", "/rest/2.0/xpan/nas"):
                return self._xpan(request)
            if path in ("/rest/2.0/pcs/file", "/rest/2.0/pcs/superfile2"):
                return self._pcs(request)
            if path.startswith("/file/"):
                return self._dlink(request, path[len("/file/"):])
            if path.startswith("/cdn/"):
                return self._cdn(request, path[len("/cdn/"):])
        raise HTTPError(404, b"no such endpoint")

    def handle_other(self, request: Request) -> Response:
        with self.lock:
            self.other_requests.append(request.target)
        raise HTTPError(404, b"not Baidu")

    @staticmethod
    def _request_id() -> int:
        return secrets.randbits(62)

    # -- OAuth

    def _token(self, request: Request) -> Response:
        self.counts["token"] += 1
        if request.method != "GET":  # as Baidu's docs call it
            return oauth_error(400, "invalid_request", "use GET")
        injected = self.fail_next.pop("token", None)
        if isinstance(injected, tuple):
            return Response(injected[1], b"<html>busy</html>", {"Content-Type": "text/html"})
        grant_type = request.arg("grant_type")
        app_ok = request.arg("client_id") == APP_KEY and request.arg("client_secret") == SECRET_KEY
        if grant_type == "refresh_token":
            refresh = request.arg("refresh_token") or ""
            grant_id = self.refresh.pop(refresh, None)  # a refused refresh kills it too (Baidu's docs)
            used = refresh in self.spent
            self.spent.add(refresh)
            if not app_ok:
                return oauth_error(401, "invalid_client", "unknown client id")
            if grant_id is None:
                if used:
                    return oauth_error(400, "expired_token", "refresh token has been used")
                return oauth_error(400, "invalid_grant", "invalid refresh token")
            if self.grants[grant_id].revoked:
                return oauth_error(400, "invalid_request", "invalid refresh")
            return json_response(self._new_tokens(grant_id))
        if grant_type == "authorization_code":
            if not app_ok:
                return oauth_error(401, "invalid_client", "unknown client id")
            code = self.codes.get(request.arg("code") or "")
            if code is None or code["used"] or time.time() > code["expiry"]:
                return oauth_error(400, "invalid_grant", "Invalid authorization code")
            code["used"] = True
            if request.arg("redirect_uri") != code["redirect_uri"]:
                return oauth_error(400, "invalid_grant", "redirect uri mismatch")
            grant_id = secrets.token_hex(6)
            self.grants[grant_id] = Grant(code["scope"])
            return json_response(self._new_tokens(grant_id))
        return oauth_error(400, "unsupported_grant_type", "The authorization grant type is not supported")

    def _auth(self, request: Request, *, pcs: bool = False) -> None:
        """The access token of the query (Baidu takes it there only)."""
        token = request.arg("access_token") or ""
        entry = self.access.get(token)
        problem: Fail | None = None
        if entry is None:
            problem = Fail(110, "Access token invalid or no longer valid") if pcs else Fail(-6, "Invalid Bduss")
        else:
            grant = self.grants[entry[0]]
            if grant.revoked:
                problem = Fail(20017, "access_token invalid")
            elif grant.access != token or "netdisk" not in grant.scope.split():
                problem = Fail(110, "Access token invalid or no longer valid") if pcs else Fail(-6, "Invalid Bduss")
            elif time.time() >= entry[1]:
                problem = Fail(111, "Access token expired") if pcs else Fail(20016, "access_token expired")
        if problem is not None:
            self.counts["refused"] += 1
            raise problem

    def _token_ok(self, token: str | None) -> bool:
        entry = self.access.get(token or "")
        if entry is None:
            return False
        grant = self.grants[entry[0]]
        return not grant.revoked and grant.access == token and time.time() < entry[1]

    # -- the xpan API

    def _xpan(self, request: Request) -> Response:
        method = request.arg("method") or ""
        self.counts[method] += 1
        try:
            self._auth(request)
            injected = self.fail_next.pop(method, None)
            if isinstance(injected, int):
                raise Fail(injected, "injected")
            if isinstance(injected, tuple):
                return Response(injected[1], b"<html>error</html>", {"Content-Type": "text/html"})
            handler = self.XPAN.get((request.method, request.path, method))
            if handler is None:
                raise Fail(2, "参数错误")
            if request.method == "POST":
                if "application/x-www-form-urlencoded" not in (request.header("Content-Type") or ""):
                    raise Fail(2, "参数错误")
                form = request.form()
            else:
                form = {}
            answer = handler(self, request, form)
        except Fail as error:
            return json_response({"errno": error.code, "errmsg": error.message, "request_id": self._request_id()})
        answer.setdefault("errno", 0)
        answer.setdefault("request_id", self._request_id())
        return json_response(answer)

    def _path(self, value: Any, *, outside: int = 20020) -> str:
        if (not isinstance(value, str) or not value.startswith("/") or "//" in value
                or (value != "/" and value.endswith("/"))):
            raise Fail(-7, "文件名非法")
        if value != APP_FOLDER and not value.startswith(APP_FOLDER + "/"):
            raise Fail(outside, "路径不在允许的访问范围内")
        return value

    @staticmethod
    def _check_name(name: Any) -> None:
        if (not isinstance(name, str) or not name or name in (".", "..") or "/" in name or len(name) > 255
                or any(ch in FORBIDDEN for ch in name)):
            raise Fail(-7, "文件名非法")

    def _parent_dir(self, path: str) -> None:
        parent = self.nodes.get(parent_of(path))
        if parent is None or not parent.is_dir:
            raise Fail(-9, "文件或目录不存在")

    @staticmethod
    def _int(value: Any) -> int:
        if not isinstance(value, str) or not value.isascii() or not value.isdigit():
            raise Fail(2, "参数错误")
        return int(value)

    @staticmethod
    def _block_list(form: dict[str, str]) -> list[str]:
        try:
            blocks = json.loads(form.get("block_list") or "")
        except ValueError:
            raise Fail(2, "参数错误") from None
        if not isinstance(blocks, list) or not blocks or not all(isinstance(b, str) and _MD5.fullmatch(b) for b in blocks):
            raise Fail(2, "参数错误")
        return blocks

    def _limits(self) -> tuple[int, int]:
        return self.limits.get(self.vip_type, self.limits[0])

    @staticmethod
    def _category(node: Node) -> int:
        if node.is_dir:
            return 6
        return {"mp4": 1, "mp3": 2, "jpg": 3, "png": 3, "txt": 4, "pdf": 4}.get(node.name.rpartition(".")[2].lower(), 6)

    def _list_entry(self, node: Node) -> dict[str, Any]:
        entry = {
            "server_filename": node.name, "privacy": 0, "category": self._category(node), "unlist": 0,
            "fs_id": node.fs_id, "server_atime": 0, "isdir": 1 if node.is_dir else 0, "path": node.path,
            "size": 0 if node.is_dir else len(node.data), "server_ctime": node.ctime, "server_mtime": node.mtime,
            "local_ctime": node.ctime, "local_mtime": node.mtime, "share": 0, "oper_id": 1234567890,
        }
        if node.is_dir:
            entry.update(empty=0, dir_empty=0 if any(c.is_dir for c in self.children(node.path)) else 1)
        else:
            entry["md5"] = md5_hex(b"cloud" + node.data)  # "not the file's real MD5"
        return entry

    def _created(self, node: Node) -> dict[str, Any]:
        """create's (and a rapid upload's) description of a new file."""
        return {"fs_id": node.fs_id, "md5": md5_hex(b"cloud" + node.data), "server_filename": node.name,
                "category": self._category(node), "path": node.path, "size": len(node.data), "ctime": node.ctime,
                "mtime": node.mtime, "isdir": 0}

    def _list(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        folder = self._path(request.arg("dir") or "/")
        node = self.nodes.get(folder)
        if node is None:
            raise Fail(-9, "文件或目录不存在")
        if not node.is_dir:
            return {"guid_info": "", "list": [], "guid": 0}
        start, limit = self._int(request.arg("start") or "0"), self._int(request.arg("limit") or "1000")
        if not limit:
            raise Fail(2, "参数错误")
        page = self.children(folder)[start:start + min(limit, self.page_limit)]
        return {"guid_info": "", "list": [self._list_entry(child) for child in page], "guid": 0}

    def _place(self, path: str, data: bytes, rtype: str) -> Node:
        """A new file at `path`, with precreate's and create's rtype: 0 fails on a taken path, 1 and 2 rename,
        3 replaces a file."""
        old = self.nodes.get(path)
        if old is not None:
            if old.is_dir or rtype == "0":
                raise Fail(-8, "文件或目录已存在")
            if rtype == "3":
                del self.nodes[path]
            else:
                path = self._free_name(path)
        return self._add(path, False, data)

    def _free_name(self, path: str) -> str:
        stem, dot, ext = path.rpartition(".") if "." in path.rpartition("/")[2] else (path, "", "")
        number = 1
        while f"{stem}({number}){dot}{ext}" in self.nodes:
            number += 1
        return f"{stem}({number}){dot}{ext}"

    def _create(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        isdir = form.get("isdir")
        if isdir == "1":
            path = self._path(form.get("path"))
            rtype = form.get("rtype", "0")
            if rtype not in ("0", "1") or {"size", "block_list", "uploadid"} & set(form):
                raise Fail(2, "参数错误")
            self._check_name(path.rpartition("/")[2])
            self._parent_dir(path)
            if path in self.nodes:
                if rtype == "0":
                    raise Fail(-8, "文件或目录已存在")
                path = self._free_name(path)
            node = self._add(path, True)
            return {"ctime": node.ctime, "fs_id": node.fs_id, "isdir": 1, "mtime": node.mtime, "path": node.path,
                    "status": 0, "category": 6}
        if isdir != "0":
            raise Fail(2, "参数错误")
        path = self._path(form.get("path"), outside=31064)
        size, blocks = self._int(form.get("size")), self._block_list(form)
        session = self.uploads.get(form.get("uploadid") or "")
        if (session is None or session["path"] != path or session["size"] != size or session["blocks"] != blocks
                or session["rtype"] != form.get("rtype", "0")):
            raise Fail(31355, "参数异常")
        if any(block not in self.blocks for block in blocks):
            raise Fail(31363, "分片缺失")
        data = b"".join(self.blocks[block] for block in blocks)
        if len(data) != size:
            raise Fail(31190, "文件不存在")
        if size > self._limits()[0]:
            raise Fail(31365, "文件总大小超限")
        del self.uploads[form["uploadid"]]
        return self._created(self._place(path, data, session["rtype"]))

    def _precreate(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        path = self._path(form.get("path"), outside=31064)
        if any(key not in form for key in ("size", "isdir", "block_list", "autoinit")):
            raise Fail(2, "参数错误")
        if form["isdir"] != "0" or form["autoinit"] != "1":
            raise Fail(2, "参数错误")
        size, blocks = self._int(form["size"]), self._block_list(form)
        rtype = form.get("rtype", "0")
        if not size or rtype not in ("0", "1", "2", "3") or len(blocks) > self.max_blocks:
            raise Fail(2, "参数错误")  # no empty files (Baidu's FAQ)
        self._check_name(path.rpartition("/")[2])
        self._parent_dir(path)
        old = self.nodes.get(path)
        if old is not None and old.is_dir:
            raise Fail(-8, "文件或目录已存在")
        known = self.contents.get(form.get("content-md5") or "")
        if known is not None and len(known) == size and md5_hex(known[:256 * KiB]) == form.get("slice-md5"):
            node = self._place(path, known, rtype)  # 秒传
            return {"path": node.path, "return_type": 2, "info": self._created(node)}
        upload_id = f"N1-{secrets.token_hex(16)}"
        self.uploads[upload_id] = {"path": path, "size": size, "blocks": blocks, "rtype": rtype}
        needed = [index for index, block in enumerate(blocks) if block not in self.blocks]
        return {"path": path, "uploadid": upload_id, "return_type": 1, "block_list": needed}

    def _tree(self, path: str) -> list[Node]:
        return [node for key, node in self.nodes.items() if key == path or key.startswith(path + "/")]

    def _operate(self, opera: str, entry: Any, ondup: str) -> None:
        if opera == "delete":
            path = self._path(entry)
            if path not in self.nodes:
                raise Fail(-9, "文件不存在")
            for node in self._tree(path):
                del self.nodes[node.path]
                self.trash.append(node)  # to the recycle bin
            return
        if not isinstance(entry, dict):
            raise Fail(2, "参数错误")
        source = self._path(entry.get("path"))
        node = self.nodes.get(source)
        if node is None:
            raise Fail(-9, "文件不存在")
        if opera == "rename":
            name = entry.get("newname")
            self._check_name(name)
            target = parent_of(source).rstrip("/") + "/" + name
        else:
            dest = self._path(entry.get("dest"))
            folder = self.nodes.get(dest)
            if folder is None or not folder.is_dir:
                raise Fail(-9, "文件不存在")
            name = entry.get("newname") or node.name
            self._check_name(name)
            target = dest + "/" + name
            if node.is_dir and (dest == source or dest.startswith(source + "/")):
                raise Fail(2, "不能移动到自身或子目录")
        self._path(target)
        if target == source:
            return
        if target in self.nodes:
            if ondup == "skip":
                return
            if ondup == "newcopy":
                target = self._free_name(target)
            elif ondup == "overwrite":
                for old in self._tree(target):
                    del self.nodes[old.path]
            else:
                raise Fail(-8, "文件或目录已存在")
        moved = self._tree(source)
        for old in moved:
            new_path = target + old.path[len(source):]
            if opera == "copy":
                self._add(new_path, old.is_dir, old.data)
            else:
                del self.nodes[old.path]
                self.nodes[new_path] = dataclasses.replace(old, path=new_path)
        if opera != "copy":
            self.nodes[target].mtime = self.tick()

    def _filemanager(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        opera = request.arg("opera")
        if opera not in ("copy", "move", "rename", "delete") or form.get("async") not in ("0", "1", "2"):
            raise Fail(2, "参数错误")
        try:
            filelist = json.loads(form.get("filelist") or "")
        except ValueError:
            raise Fail(2, "参数错误") from None
        if not isinstance(filelist, list) or not filelist:
            raise Fail(2, "参数错误")
        info, failed = [], False
        for entry in filelist:
            source = entry if opera == "delete" else entry.get("path") if isinstance(entry, dict) else None
            try:
                self._operate(opera, entry, form.get("ondup", "fail"))
            except Fail as error:
                if error.code == 20020:
                    raise  # the whole call is refused
                info.append({"errno": error.code, "path": source})
                failed = True
            else:
                info.append({"errno": 0, "path": source})
        return {"errno": 12 if failed else 0, "info": info}

    def _new_link(self, node: Node) -> str:
        token = secrets.token_hex(10)
        sign = f"FDTAXUGERLQlBHSKfW-DCb740ccc5511e5e8fedcff06b081203-{secrets.token_hex(6)}%3D"
        self.links[token] = (node.fs_id, sign, time.time() + self.link_lifetime)
        return (f"{self.url}/file/{token}?fid=1-250528-{node.fs_id}&rt=pr&sign={sign}&expires=8h&chkbd=0&chkv=0"
                f"&dp-logid={secrets.randbits(60)}&dp-callid=0&r={secrets.randbelow(10 ** 9)}")

    def _filemetas(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        try:
            fsids = json.loads(request.arg("fsids") or "")
        except ValueError:
            raise Fail(2, "参数错误") from None
        if not isinstance(fsids, list) or not fsids or len(fsids) > 100 or not all(type(i) is int for i in fsids):
            raise Fail(2, "参数错误")
        found = []
        for fs_id in fsids:
            node = self._by_id(fs_id)
            if node is None or not node.path.startswith(APP_FOLDER):
                continue
            entry = {"category": self._category(node), "filename": node.name, "fs_id": node.fs_id,
                     "isdir": 1 if node.is_dir else 0, "md5": md5_hex(b"cloud" + node.data), "oper_id": 1234567890,
                     "path": node.path, "server_ctime": node.ctime, "server_mtime": node.mtime,
                     "size": 0 if node.is_dir else len(node.data), "thumbs": {}}
            if request.arg("dlink") == "1" and not node.is_dir:
                entry["dlink"] = self._new_link(node)
            found.append(entry)
        return {"errmsg": "succ", "list": found, "names": {}}

    def _uinfo(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        return {"avatar_url": "https://example.com/avatar.jpg", "baidu_name": "百度用户A001", "errmsg": "succ",
                "netdisk_name": "netdiskuser", "uk": 208281036, "vip_type": self.vip_type}

    XPAN = {
        ("GET", "/rest/2.0/xpan/file", "list"): _list,
        ("POST", "/rest/2.0/xpan/file", "create"): _create,
        ("POST", "/rest/2.0/xpan/file", "precreate"): _precreate,
        ("POST", "/rest/2.0/xpan/file", "filemanager"): _filemanager,
        ("GET", "/rest/2.0/xpan/multimedia", "filemetas"): _filemetas,
        ("GET", "/rest/2.0/xpan/nas", "uinfo"): _uinfo,
    }

    # -- the PCS hosts: locating uploads and taking slices

    def _pcs(self, request: Request) -> Response:
        endpoint = "locateupload" if request.path == "/rest/2.0/pcs/file" else "upload"
        self.counts[endpoint] += 1
        try:
            self._auth(request, pcs=True)
            injected = self.fail_next.pop(endpoint, None)
            if isinstance(injected, int):
                raise Fail(injected, "injected")
            if isinstance(injected, tuple):
                return Response(injected[1], b"<html>error</html>", {"Content-Type": "text/html"})
            answer = self._locate(request) if endpoint == "locateupload" else self._superfile2(request)
        except Fail as error:
            field = "error_code" if endpoint == "locateupload" or error.code in (110, 111) else "errno"
            return json_response({field: error.code, "error_msg": error.message, "request_id": self._request_id()})
        answer.setdefault("request_id", self._request_id())
        return json_response(answer)

    def _locate(self, request: Request) -> dict[str, Any]:
        if (request.method != "GET" or request.arg("method") != "locateupload" or request.arg("appid") != "250528"
                or request.arg("upload_version") != "2.0"):
            raise Fail(31023, "参数错误")  # an error_code: 2 would be OpenAPI's "service temporarily unavailable"
        path = self._path(request.arg("path"))
        session = self.uploads.get(request.arg("uploadid") or "")
        if session is None or session["path"] != path:
            raise Fail(31355, "参数异常")
        servers = self.upload_servers if self.upload_servers is not None else [self.other_url, self.url]
        return {"bak_server": [], "bak_servers": [{"server": self.url}], "client_ip": "127.0.0.1", "error_code": 0,
                "error_msg": "", "expire": 60, "host": self.url.partition("://")[2], "newno": "", "quic_server": [],
                "quic_servers": [], "server": [], "server_time": int(time.time()),
                "servers": [{"server": server} for server in servers], "sl": 0}

    def _superfile2(self, request: Request) -> dict[str, Any]:
        if request.method != "POST" or request.arg("method") != "upload" or request.arg("type") != "tmpfile":
            raise Fail(2, "参数错误")
        session = self.uploads.get(request.arg("uploadid") or "")
        if session is None or session["path"] != request.arg("path"):
            raise Fail(31355, "参数异常")
        index = self._int(request.arg("partseq"))
        if index >= len(session["blocks"]):
            raise Fail(2, "参数错误")
        data = multipart_file(request)
        if not data:
            raise Fail(2, "参数错误")
        if self.broken_slices:
            self.broken_slices -= 1
            raise HTTPError(500, b"<html>500 Internal Server Error</html>", {"Content-Type": "text/html"})
        if len(data) > self._limits()[1]:
            raise Fail(31364, "超出分片大小限制")
        if len(session["blocks"]) > 1 and index == 0 and len(data) < self.base_slice:
            raise Fail(31299, "第一个分片的大小小于4MB")
        if self.corrupt_slices:  # what arrived isn't what was sent
            self.corrupt_slices -= 1
            data = bytes([data[0] ^ 1]) + data[1:]
        self.slices.append((index, len(data)))
        self.blocks[md5_hex(data)] = data
        return {"md5": md5_hex(data)}

    # -- downloads

    def _refused_download(self, code: int, message: str) -> Response:
        body = json.dumps({"errno": code, "show_msg": message, "request_id": self._request_id()}).encode()
        return Response(self.download_error_status, body, {"Content-Type": "application/json"})

    def _dlink(self, request: Request, token: str) -> Response:
        self.counts["download"] += 1
        link = self.links.get(token)
        if link is None or f"sign={link[1]}" not in request.raw_query.split("&"):
            return self._refused_download(31362, "sign error")
        fs_id, _sign, expiry = link
        if time.time() >= expiry:
            return self._refused_download(31360, "download link expired")
        if not self._token_ok(request.arg("access_token")):
            self.counts["refused"] += 1
            return self._refused_download(31045, "access token verify failed")
        if request.header("User-Agent") != USER_AGENT:
            return self._refused_download(31326, "hit anti-leech")
        injected = self.fail_next.pop("download", None)
        if isinstance(injected, int):
            return self._refused_download(injected, "injected")
        if isinstance(injected, tuple):
            return Response(injected[1], b"<html>error</html>", {"Content-Type": "text/html"})
        node = self._by_id(fs_id)
        if node is None:
            return self._refused_download(31066, "file does not exist")
        if self.redirect_downloads:
            cdn = secrets.token_hex(12)
            self.cdn[cdn] = fs_id
            return Response(302, b"", {"Location": f"{self.url}/cdn/{cdn}?xcode={secrets.token_hex(8)}"})
        return file_response(node.data, request.header("Range"))

    def _cdn(self, request: Request, token: str) -> Response:
        self.counts["cdn"] += 1
        if "access_token" in request.query:
            self.leaks.append("cdn")
        fs_id = self.cdn.get(token)
        node = self._by_id(fs_id) if fs_id is not None else None
        if node is None:
            raise HTTPError(404, b"no such file")
        if request.header("User-Agent") != USER_AGENT:
            raise HTTPError(403, b"forbidden")
        return file_response(node.data, request.header("Range"))
