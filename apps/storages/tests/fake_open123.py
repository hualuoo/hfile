"""An in-memory fake of the 123 Cloud Drive open platform (https://open-api.123pan.com) for the driver tests (no Django).

One FakeServer plays the API (/api/..., /upload/v1|v2/...) and the download CDN (/cdn/...); a second one plays the
upload server that create names in "servers" (/upload/v2/file/slice). It checks what 123's documentation says the
real service checks: the Platform header, JSON bodies with numbers where it says number, bearer tokens (30 days;
at most 3 of a client ID, so a 4th logs the oldest out), limit at most 100, names (fewer than 255 characters, none
of "\\/:*?|><, not all spaces), the 10 GB limit, the slices (numbers, sizes and MD5s) and the whole file's MD5 on
completion. Download links work without the token; a bearer token sent there is noted in `leaks`.

Answers are as documented: HTTP 200 and {"code", "message", "data", "x-traceID"}, code 0 for success. Codes the docs
don't give (a name taken, a bad parameter) are made up; the driver goes by the documented ones and by the words of
the messages. Where the docs say nothing, these are assumptions: listing an id that isn't a live folder answers
5066, as download_info does for a missing file; mkdir, rename, move and copy refuse a name taken in the target
folder; copy takes files only ("此接口仅支持单个文件复制"); 123 knows the empty file, so its upload is 秒传;
duplicate 2 puts the old file in the recycle bin and makes a new one.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import hashlib
import itertools
import math
import re
import secrets
import threading
import time
import urllib.parse
import uuid
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

CLIENT_ID = "1cc3f37c84994218b0489226b988128b"
CLIENT_SECRET = "fake-client-secret"
CHINA = datetime.timezone(datetime.timedelta(hours=8))
FORBIDDEN = '"\\/:*?|><'
MAX_SIZE = 10 * 1024 ** 3
TOKEN_LIFETIME = 30 * 24 * 3600

# Codes the docs give, and made-up ones for what they don't say.
NOT_FOUND = 5066
NO_TRAFFIC = 5113
PROCESSING = 20103  # what OpenList's driver saw from upload_complete
BAD_REQUEST = 40000  # made up
NAME_TAKEN = 40001  # made up


@dataclasses.dataclass
class Item:
    id: int
    name: str
    parent: int
    is_dir: bool
    data: bytes = b""
    trashed: bool = False
    created: int = 0  # Unix seconds
    updated: int = 0

    @property
    def md5(self) -> str:
        return "" if self.is_dir else hashlib.md5(self.data).hexdigest()


class Fail(Exception):
    """An error answer of the API (HTTP 200, a code other than 0)."""

    def __init__(self, code: int, message: str):
        super().__init__(code, message)
        self.code = code
        self.message = message


def china_time(seconds: int) -> str:
    """A time as 123's listings give it: China's, without the zone."""
    return datetime.datetime.fromtimestamp(seconds, CHINA).strftime("%Y-%m-%d %H:%M:%S")


def parse_multipart(request: Request) -> dict[str, tuple[str | None, bytes]]:
    """The parts of a multipart/form-data body: field name -> (file name, content)."""
    content_type = request.header("Content-Type") or ""
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not content_type.startswith("multipart/form-data") or match is None:
        raise Fail(BAD_REQUEST, "Content-Type 应该是 multipart/form-data")
    pieces = request.body.split(b"--" + match[1].encode("ascii"))
    if len(pieces) < 3 or pieces[0] or not pieces[-1].startswith(b"--"):
        raise Fail(BAD_REQUEST, "multipart 格式错误")
    parts: dict[str, tuple[str | None, bytes]] = {}
    for piece in pieces[1:-1]:
        head, blank, content = piece.partition(b"\r\n\r\n")
        if not head.startswith(b"\r\n") or not blank or not content.endswith(b"\r\n"):
            raise Fail(BAD_REQUEST, "multipart 格式错误")
        disposition = next((line for line in head.decode("utf-8").split("\r\n")
                            if line.lower().startswith("content-disposition:")), "")
        name, filename = re.search(r'\bname="([^"]*)"', disposition), re.search(r'\bfilename="([^"]*)"', disposition)
        if name is None:
            raise Fail(BAD_REQUEST, "multipart 格式错误")
        parts[name[1]] = (filename[1] if filename else None, content[:-2])
    return parts


class Fake123:
    def __init__(self):
        self.lock = threading.RLock()
        self.server = FakeServer(self.handle)
        self.uploads = FakeServer(self.handle_upload)
        self.reset()

    # -- lifecycle and test helpers

    def start(self) -> Fake123:
        self.server.start()
        self.uploads.start()
        return self

    def close(self) -> None:
        self.server.close()
        self.uploads.close()

    def reset(self) -> None:
        with self.lock:
            self.items: dict[int, Item] = {0: Item(0, "", -1, True)}
            self.ids = itertools.count(14663228)
            self.clock = int(time.time()) - 3600  # a logical clock: every change is one second later
            self.tokens: collections.OrderedDict[str, float] = collections.OrderedDict()  # token -> expiry
            self.token_lifetime = TOKEN_LIFETIME
            self.links: dict[str, tuple[int, float]] = {}  # link token -> (file id, expiry)
            self.link_lifetime = 3600.0
            self.pending: dict[str, dict[str, Any]] = {}  # preuploadID -> what create was told
            self.known: dict[str, bytes] = {hashlib.md5(b"").hexdigest(): b""}  # MD5 -> bytes 123 has (秒传)
            self.page_limit = 100
            self.slice_size = 16 * 1024 * 1024
            self.servers: list[str] | None = None  # the upload servers create names; None: self.uploads
            self.counts: collections.Counter[str] = collections.Counter()  # calls per endpoint
            self.fail_next: dict[str, Any] = {}  # endpoint -> Fail, an HTTP status (int) or a body (bytes)
            self.throttle: collections.Counter[str] = collections.Counter()  # endpoint -> 429 answers to give
            self.qps: dict[str, int] = {}  # endpoint -> calls a second it takes, the rest get 429
            self.recent: dict[str, collections.deque] = collections.defaultdict(collections.deque)
            self.throttled: collections.Counter[str] = collections.Counter()  # 429 answers given
            self.complete_busy = 0  # upload_complete answers 20103 this many times,
            self.complete_pending = 0  # then completed false this many times
            self.slice_fail: collections.Counter[int] = collections.Counter()  # slice number -> HTTP 500s to give
            self.slices: list[tuple[str, int]] = []  # (upload server, slice number) of every slice taken
            self.traffic_used_up = False  # download_info answers 5113
            self.replace_keeps_id = False  # duplicate 2 puts the new bytes in the old file (undocumented either way)
            self.leaks: list[str] = []  # bearer tokens seen where 123's token must never go

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    def issue_token(self) -> tuple[str, int]:
        """A new access token, as /api/v1/access_token gives one (a 4th logs the oldest out): (token, expiry)."""
        with self.lock:
            token = "eyJ" + secrets.token_urlsafe(24)
            expiry = time.time() + self.token_lifetime
            self.tokens[token] = expiry
            while len(self.tokens) > 3:
                self.tokens.popitem(last=False)
            return token, int(expiry)

    def expire_tokens(self) -> None:
        with self.lock:
            for token in self.tokens:
                self.tokens[token] = 0.0

    def expire_links(self) -> None:
        with self.lock:
            self.links = {token: (file_id, 0.0) for token, (file_id, _) in self.links.items()}

    def add(self, parent: int, name: str, is_dir: bool, data: bytes = b"") -> Item:
        with self.lock:
            moment = self.tick()
            item = Item(next(self.ids), name, parent, is_dir, data, False, moment, moment)
            self.items[item.id] = item
            if not is_dir:
                self.known[item.md5] = data
            return item

    def children(self, folder_id: int, *, trashed: bool = False) -> list[Item]:
        """What is in a folder, by id; with `trashed`, what is in the recycle bin from there too."""
        return sorted((item for item in self.items.values()
                       if item.parent == folder_id and item.id != 0 and (trashed or not item.trashed)),
                      key=lambda item: item.id)

    def alive(self, item_id: int | None) -> Item | None:
        """The item, unless it or a folder above it is in the recycle bin."""
        item = self.items.get(item_id) if item_id is not None else None
        step = item
        while step is not None and step.id != 0:
            if step.trashed:
                return None
            step = self.items.get(step.parent)
        return item if step is not None else None

    def find(self, path: str) -> Item | None:
        with self.lock:
            item: Item | None = self.items[0]
            for name in [part for part in path.split("/") if part]:
                item = next((child for child in self.children(item.id) if child.name == name), None)
                if item is None:
                    return None
            return item

    def mkdirs(self, path: str) -> int:
        with self.lock:
            folder = 0
            for name in [part for part in path.split("/") if part]:
                match = next((c for c in self.children(folder) if c.name == name and c.is_dir), None)
                folder = match.id if match is not None else self.add(folder, name, True).id
            return folder

    def put_file(self, path: str, data: bytes) -> Item:
        parent, _, name = path.rpartition("/")
        return self.add(self.mkdirs(parent), name, False, data)

    # -- the servers

    def handle(self, request: Request) -> Response:
        with self.lock:
            if request.path.startswith("/cdn/"):
                return self._cdn(request, request.path[len("/cdn/"):])
            return self._api(request, self.API)

    def handle_upload(self, request: Request) -> Response:
        with self.lock:
            return self._api(request, self.UPLOAD)

    def _api(self, request: Request, handlers: dict) -> Response:
        endpoint = request.path
        self.counts[endpoint] += 1
        injected = self.fail_next.pop(endpoint, None)
        if injected == 429:  # the gateway's own answer
            return Response(429, b'{"message":"API rate limit exceeded"}',
                            {"Content-Type": "application/json", "Retry-After": "0"})
        if isinstance(injected, int):
            return Response(injected, b"<html>error</html>", {"Content-Type": "text/html"})
        if isinstance(injected, bytes):
            return Response(200, injected, {"Content-Type": "text/plain"})
        handler = handlers.get((request.method, endpoint))
        if handler is None:
            raise HTTPError(404, b"no such endpoint")
        try:
            if request.header("Platform") != "open_platform":
                raise Fail(BAD_REQUEST, "缺少 Platform 请求头")
            if self._throttled(endpoint):
                raise Fail(429, "请求太频繁")
            if endpoint != "/api/v1/access_token":
                token = request.bearer()
                if token is None or time.time() >= self.tokens.get(token, 0.0):
                    raise Fail(401, "access_token无效")
            if isinstance(injected, Fail):
                raise injected
            data = handler(self, request)
        except Fail as error:
            return self._reply(error.code, error.message, None)
        return self._reply(0, "ok", data)

    @staticmethod
    def _reply(code: int, message: str, data: Any) -> Response:
        return json_response({"code": code, "message": message, "data": data, "x-traceID": str(uuid.uuid4())})

    def _throttled(self, endpoint: str) -> bool:
        if self.throttle[endpoint] > 0:
            self.throttle[endpoint] -= 1
            self.throttled[endpoint] += 1
            return True
        limit = self.qps.get(endpoint)
        if limit:
            moment, recent = time.monotonic(), self.recent[endpoint]
            while recent and moment - recent[0] >= 1.0:
                recent.popleft()
            if len(recent) >= limit:
                self.throttled[endpoint] += 1
                return True
            recent.append(moment)
        return False

    # -- reading requests

    @staticmethod
    def _body(request: Request) -> dict[str, Any]:
        if "application/json" not in (request.header("Content-Type") or ""):
            raise Fail(BAD_REQUEST, "请求体应该是 JSON（Content-Type: application/json）")
        try:
            body = request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise Fail(BAD_REQUEST, "参数错误")
        return body

    @staticmethod
    def _number(body: dict[str, Any], key: str, *, required: bool = True) -> int | None:
        value = body.get(key)
        if value is None and not required:
            return None
        if type(value) is not int:
            raise Fail(BAD_REQUEST, f"{key} 应该是数字")
        return value

    @staticmethod
    def _query(request: Request, key: str, *, required: bool = True) -> int | None:
        value = request.arg(key)
        if value is None or value == "":
            if required:
                raise Fail(BAD_REQUEST, f"缺少参数 {key}")
            return None
        if not re.fullmatch(r"-?[0-9]+", value):
            raise Fail(BAD_REQUEST, f"{key} 应该是数字")
        return int(value)

    def _ids(self, body: dict[str, Any], key: str) -> list[Item]:
        ids = body.get(key)
        if not isinstance(ids, list) or not ids or len(ids) > 100 or any(type(i) is not int for i in ids):
            raise Fail(BAD_REQUEST, f"{key} 应该是 1 到 100 个文件 ID")
        return [self._item(i) for i in ids]

    def _item(self, item_id: int | None) -> Item:
        item = self.alive(item_id)
        if item is None or item.id == 0:
            raise Fail(NOT_FOUND, "文件不存在")
        return item

    def _folder(self, folder_id: int | None) -> Item:
        item = self.alive(folder_id)
        if item is None or not item.is_dir:
            raise Fail(NOT_FOUND, "文件不存在")
        return item

    @staticmethod
    def _check_name(name: Any) -> None:
        if not isinstance(name, str) or not name.strip() or len(name) >= 255 or any(ch in FORBIDDEN for ch in name):
            raise Fail(BAD_REQUEST, '文件名要小于255个字符且不能包含以下任何字符："\\/:*?|><')

    def _taken(self, folder_id: int, name: str, but: int | None = None) -> bool:
        return any(child.name == name and child.id != but for child in self.children(folder_id))

    def _inside(self, folder: Item, item: Item) -> bool:
        """Whether `folder` is `item` or below it."""
        step: Item | None = folder
        while step is not None and step.id != 0:
            if step.id == item.id:
                return True
            step = self.items.get(step.parent)
        return False

    # -- the API

    def _token(self, request: Request) -> dict[str, Any]:
        body = self._body(request)
        if body.get("clientID") != CLIENT_ID or body.get("clientSecret") != CLIENT_SECRET:
            raise Fail(401, "clientID 或 clientSecret 错误")
        token, expiry = self.issue_token()
        return {"accessToken": token,
                "expiredAt": datetime.datetime.fromtimestamp(expiry, CHINA).isoformat(timespec="seconds")}

    def _entry(self, item: Item) -> dict[str, Any]:
        return {"fileId": item.id, "filename": item.name, "parentFileId": item.parent, "type": 1 if item.is_dir else 0,
                "etag": item.md5, "size": 0 if item.is_dir else len(item.data), "category": 0, "status": 0,
                "punishFlag": 0, "s3KeyFlag": "x-0", "storageNode": "m0", "trashed": 1 if item.trashed else 0,
                "createAt": china_time(item.created), "updateAt": china_time(item.updated)}

    def _list(self, request: Request) -> dict[str, Any]:
        folder = self._folder(self._query(request, "parentFileId"))
        limit = self._query(request, "limit")
        if not 1 <= limit <= 100:
            raise Fail(BAD_REQUEST, "limit 最大不超过100")
        last = self._query(request, "lastFileId", required=False)
        rest = [child for child in self.children(folder.id, trashed=True) if not last or child.id >= last]
        page = rest[:min(limit, self.page_limit)]
        following = rest[len(page)].id if len(rest) > len(page) else -1  # where the next page starts
        return {"lastFileId": following, "fileList": [self._entry(child) for child in page]}

    def _mkdir(self, request: Request) -> dict[str, Any]:
        body = self._body(request)
        parent_id = body.get("parentID")
        if isinstance(parent_id, str) and parent_id.isdigit():  # OpenList's driver sends a string, and 123 takes it
            parent_id = int(parent_id)
        parent = self._folder(self._number({"parentID": parent_id}, "parentID"))
        name = body.get("name")
        self._check_name(name)
        if self._taken(parent.id, name):
            raise Fail(NAME_TAKEN, "该目录下已经有同名文件夹,无法进行创建")
        return {"dirID": self.add(parent.id, name, True).id}

    def _rename(self, request: Request) -> None:
        body = self._body(request)
        item = self._item(self._number(body, "fileId"))
        name = body.get("fileName")
        self._check_name(name)
        if self._taken(item.parent, name, but=item.id):
            raise Fail(NAME_TAKEN, "当前目录有重名文件")
        item.name = name
        item.updated = self.tick()

    def _move(self, request: Request) -> None:
        body = self._body(request)
        items = self._ids(body, "fileIDs")
        target = self._folder(self._number(body, "toParentFileID"))
        for item in items:
            if item.is_dir and self._inside(target, item):
                raise Fail(BAD_REQUEST, "不能移动到自身或子目录")
            if self._taken(target.id, item.name):
                raise Fail(NAME_TAKEN, "目标目录有同名文件")
        for item in items:
            item.parent = target.id

    def _trash(self, request: Request) -> None:
        for item in self._ids(self._body(request), "fileIDs"):
            item.trashed = True

    def _copy(self, request: Request) -> dict[str, Any]:
        body = self._body(request)
        item = self._item(self._number(body, "fileId"))
        target = self._folder(self._number(body, "targetDirId"))
        if item.is_dir:
            raise Fail(BAD_REQUEST, "此接口仅支持单个文件复制")
        if self._taken(target.id, item.name):
            raise Fail(NAME_TAKEN, "目标目录有同名文件")
        return {"sourceFileId": item.id, "targetFileId": self.add(target.id, item.name, False, item.data).id}

    def _download_info(self, request: Request) -> dict[str, Any]:
        item = self._item(self._query(request, "fileId"))
        if item.is_dir:
            raise Fail(NOT_FOUND, "文件不存在，检查传入fileId是否正确")
        if self.traffic_used_up:
            raise Fail(NO_TRAFFIC, "您今日自用下载流量已超出1GB上限，升级VIP会员可无限流量下载")
        token = secrets.token_urlsafe(12)
        self.links[token] = (item.id, time.time() + self.link_lifetime)
        return {"downloadUrl": f"{self.server.url}/cdn/{token}/{urllib.parse.quote(item.name)}"}

    def _check_place(self, folder_id: int, name: str, duplicate: int | None) -> None:
        same = next((child for child in self.children(folder_id) if child.name == name), None)
        if same is not None and (same.is_dir or duplicate not in (1, 2)):
            raise Fail(NAME_TAKEN, "当前目录有重名文件")

    def _place(self, folder_id: int, name: str, data: bytes, duplicate: int | None) -> Item:
        """A new file in the folder, as duplicate says when the name is taken: 1 keeps both, 2 replaces."""
        self._check_place(folder_id, name, duplicate)
        same = next((child for child in self.children(folder_id) if child.name == name), None)
        if same is not None:
            if duplicate == 2 and self.replace_keeps_id:
                same.data, same.updated = data, self.tick()
                self.known[same.md5] = data
                return same
            if duplicate == 2:
                same.trashed = True  # the old one goes to the recycle bin
            else:
                stem, dot, ext = name.rpartition(".")
                name = f"{stem}(1).{ext}" if dot and stem else f"{name}(1)"
        return self.add(folder_id, name, False, data)

    def _create(self, request: Request) -> dict[str, Any]:
        body = self._body(request)
        parent = self._folder(self._number(body, "parentFileID"))
        name, etag = body.get("filename"), body.get("etag")
        self._check_name(name)
        size = self._number(body, "size")
        duplicate = self._number(body, "duplicate", required=False)
        if not isinstance(etag, str) or not re.fullmatch(r"[0-9a-f]{32}", etag):
            raise Fail(BAD_REQUEST, "etag 应该是文件的 md5")
        if body.get("containDir") or duplicate not in (None, 1, 2) or size < 0:
            raise Fail(BAD_REQUEST, "参数错误")
        if size > MAX_SIZE:
            raise Fail(BAD_REQUEST, "开发者上传单文件大小限制10GB")
        self._check_place(parent.id, name, duplicate)
        content = self.known.get(etag)
        if content is not None and len(content) == size:  # 秒传
            return {"fileID": self._place(parent.id, name, content, duplicate).id, "reuse": True}
        preupload_id = secrets.token_urlsafe(32)
        self.pending[preupload_id] = {"parent": parent.id, "name": name, "size": size, "md5": etag,
                                      "duplicate": duplicate, "slice_size": self.slice_size, "slices": {},
                                      "file_id": None}
        return {"fileID": 0, "reuse": False, "preuploadID": preupload_id, "sliceSize": self.slice_size,
                "servers": list(self.servers) if self.servers is not None else [self.uploads.url]}

    def _complete(self, request: Request) -> dict[str, Any]:
        upload = self.pending.get(self._body(request).get("preuploadID") or "")
        if upload is None:
            raise Fail(BAD_REQUEST, "preuploadID 无效")
        if upload["file_id"] is not None:
            return {"completed": True, "fileID": upload["file_id"]}
        if self.complete_busy > 0:
            self.complete_busy -= 1
            raise Fail(PROCESSING, "文件校验中，请稍后再试")
        count = math.ceil(upload["size"] / upload["slice_size"])
        if sorted(upload["slices"]) != list(range(1, count + 1)):
            raise Fail(BAD_REQUEST, "分片没有全部上传")
        data = b"".join(upload["slices"][number] for number in range(1, count + 1))
        if len(data) != upload["size"] or hashlib.md5(data).hexdigest() != upload["md5"]:
            raise Fail(BAD_REQUEST, "文件 md5 校验失败")
        if self.complete_pending > 0:
            self.complete_pending -= 1
            return {"completed": False, "fileID": 0}
        upload["file_id"] = self._place(upload["parent"], upload["name"], data, upload["duplicate"]).id
        return {"completed": True, "fileID": upload["file_id"]}

    API = {
        ("POST", "/api/v1/access_token"): _token,
        ("GET", "/api/v2/file/list"): _list,
        ("POST", "/upload/v1/file/mkdir"): _mkdir,
        ("PUT", "/api/v1/file/name"): _rename,
        ("POST", "/api/v1/file/move"): _move,
        ("POST", "/api/v1/file/trash"): _trash,
        ("POST", "/api/v1/file/copy"): _copy,
        ("GET", "/api/v1/file/download_info"): _download_info,
        ("POST", "/upload/v2/file/create"): _create,
        ("POST", "/upload/v2/file/upload_complete"): _complete,
    }

    # -- the upload server

    def _slice(self, request: Request) -> None:
        parts = parse_multipart(request)

        def field(name: str) -> str:
            if name not in parts or parts[name][0] is not None:
                raise Fail(BAD_REQUEST, f"缺少参数 {name}")
            return parts[name][1].decode("utf-8")

        upload = self.pending.get(field("preuploadID"))
        if upload is None:
            raise Fail(BAD_REQUEST, "preuploadID 无效")
        number = field("sliceNo")
        if not number.isdigit() or "slice" not in parts or parts["slice"][0] is None:
            raise Fail(BAD_REQUEST, "参数错误")
        number = int(number)
        content = parts["slice"][1]
        count = math.ceil(upload["size"] / upload["slice_size"])
        if not 1 <= number <= count:
            raise Fail(BAD_REQUEST, "sliceNo 超出范围")
        if len(content) != (upload["slice_size"] if number < count else upload["size"] - (count - 1) * upload["slice_size"]):
            raise Fail(BAD_REQUEST, "分片大小应该是 sliceSize")
        if hashlib.md5(content).hexdigest() != field("sliceMD5"):
            raise Fail(BAD_REQUEST, "分片 md5 校验失败")
        if self.slice_fail[number] > 0:
            self.slice_fail[number] -= 1
            raise HTTPError(500, b"upstream error")
        upload["slices"][number] = content
        self.slices.append((request.server_url, number))

    UPLOAD = {("POST", "/upload/v2/file/slice"): _slice}

    # -- the CDN

    def _cdn(self, request: Request, rest: str) -> Response:
        self.counts["cdn"] += 1
        if request.header("Authorization"):
            self.leaks.append("cdn")
        link = self.links.get(rest.partition("/")[0])
        if link is None:
            raise HTTPError(404, b"no such link")
        file_id, expiry = link
        if time.time() >= expiry:
            raise HTTPError(403, b"link expired")
        item = self.alive(file_id)
        if item is None:
            raise HTTPError(404, b"gone")
        return file_response(item.data, request.header("Range"))
