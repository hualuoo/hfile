"""An in-memory fake of 115's open platform for the driver tests (no Django).

One FakeServer serves proapi (/proapi/...), passportapi (/passport/...), qrcodeapi (/qrcode/...)
and the download CDN (/cdn/...); a second one plays Aliyun OSS (path-style /<bucket>/<key>, as
Aliyun's SDK addresses an IP endpoint). It checks what the real services check, as far as 115's
documentation and the SDKs tell: bearer tokens and their expiry, single-use refresh tokens,
PKCE, download links bound to the User-Agent that asked for them, the OSS V1 signature with
the STS key, its security token and expiry, and the upload callback (sizes and SHA-1 checked,
the file created in the init's target folder; duplicate names are allowed, like 115 does).

Answers follow the docs: proapi says "state": true/false, passport and qrcode say 1/0, errors
come with HTTP 200 and a "code". Listings are sorted by "o" and "asc" when custom_order is 1 or
2, else by the order 115 remembers for the account from its own apps (remembered_order).
"""

from __future__ import annotations

import base64
import collections
import dataclasses
import datetime
import email.utils
import hashlib
import hmac
import itertools
import json
import re
import secrets
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ElementTree
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

APP_ID = "100195125"
BUCKET = "fhnfile"
FORBIDDEN = '\\/:*?"<>|'
OSS_SUBRESOURCES = {"uploads", "uploadId", "partNumber", "acl", "append", "position", "tagging", "symlink",
                    "sequential"}
# What /open/ufile/files sorts by ("o"), of the fields its docs list.
ORDERS = {"file_name": lambda item: item.name, "file_size": lambda item: len(item.data),
          "user_utime": lambda item: item.mtime}


@dataclasses.dataclass
class Item:
    id: str
    name: str
    parent: str
    is_dir: bool
    data: bytes = b""
    pick_code: str = ""
    mtime: int = 0
    trashed: bool = False

    @property
    def sha1(self) -> str:
        return "" if self.is_dir else hashlib.sha1(self.data).hexdigest().upper()


class Fail(Exception):
    """An error answer of proapi or passport (HTTP 200, "state" false/0)."""

    def __init__(self, code: int, message: str):
        super().__init__(code, message)
        self.code = code
        self.message = message


def fake_v1_signature(secret: str, method: str, headers: Any, resource: str) -> str:
    """OSS V1 signature as the server recomputes it from the request it got."""
    oss = sorted((name.lower(), value.strip()) for name, value in headers.items() if name.lower().startswith("x-oss-"))
    text = "\n".join([method, headers.get("Content-MD5", "") or "", headers.get("Content-Type", "") or "",
                      headers.get("Date", "") or ""]) + "\n"
    text += "".join(f"{name}:{value}\n" for name, value in oss) + resource
    return base64.b64encode(hmac.new(secret.encode(), text.encode(), hashlib.sha1).digest()).decode()


def oss_error(status: int, code: str, message: str = "") -> HTTPError:
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>{code}</Code><Message>{message or code}</Message>'
            f"<RequestId>5C3D9175B6FC201293AD****</RequestId></Error>").encode()
    return HTTPError(status, Response(status, body, {"Content-Type": "application/xml"}))


class Fake115:
    def __init__(self):
        self.lock = threading.RLock()
        self.server = FakeServer(self.handle)
        self.oss = FakeServer(self.handle_oss)
        self.reset()

    # -- lifecycle and test helpers

    def start(self) -> Fake115:
        self.server.start()
        self.oss.start()
        return self

    def close(self) -> None:
        self.server.close()
        self.oss.close()

    def reset(self) -> None:
        with self.lock:
            self.items: dict[str, Item] = {"0": Item("0", "根目录", "", True)}
            self.ids = itertools.count(2593093001609739968)
            self.clock = int(time.time()) - 3600  # a logical clock: every change is one second later
            self.access: dict[str, float] = {}  # access token -> expiry (time.time())
            self.refresh: dict[str, bool] = {}  # refresh token -> still valid
            self.links: dict[str, tuple[str, str, float]] = {}  # link token -> (file id, UA, expiry)
            self.link_lifetime = 7200.0
            self.pending: dict[str, dict[str, Any]] = {}  # upload token -> what init promised
            self.sts: dict[str, dict[str, Any]] = {}  # STS key id -> secret, token, expiry
            self.objects: dict[str, bytes] = {}  # OSS key -> bytes
            self.multipart: dict[str, dict[str, Any]] = {}
            self.qr: dict[str, dict[str, Any]] = {}
            self.signs: dict[str, tuple[str, int, int]] = {}  # sign_key -> (sha1, first, last)
            self.known: dict[str, bytes] = {}  # SHA-1 -> content 115 "has" (秒传)
            self.page_limit = 1150
            self.require_sign = False
            self.fail_callback = False
            self.token_as_list = False
            self.qr_delay = 0.0
            self.fail_next: dict[str, Any] = {}  # proapi path -> Fail, int (HTTP status) or bytes (body)
            self.fuzzy_search: list[str] = []  # extra names search returns although they don't match
            self.search_everywhere = False  # search ignores cid (results outside the base folder)
            self.counts: collections.Counter[str] = collections.Counter()
            self.oss_requests: list[tuple[str, str]] = []
            self.oss_fail_parts: set[int] = set()  # part numbers OSS keeps answering 500 for
            self.leaks: list[str] = []  # bearer tokens seen where 115's token must never go
            self.rename_on_conflict = False  # an upload whose name is taken gets "name(1).ext"
            self.omit_file_id = False  # the callback answer doesn't say which file it made
            self.callback_makes_nothing = False  # ... and with this, it made none (an odd 115 day)
            self.refresh_too_often = False  # refreshToken answers 40140117
            self.refuse_all: Fail | None = None  # every proapi call with a good token gets this answer
            # The order the account's owner chose in 115's apps, ("user_utime", False) for the newest
            # first; (None, True): by name. Listings follow it unless custom_order says otherwise.
            self.remembered_order: tuple[str | None, bool] = (None, True)

    def tick(self) -> int:
        self.clock += 1
        return self.clock

    def issue_tokens(self, lifetime: int = 7200) -> dict[str, Any]:
        """A fresh token pair, as after a QR login; returns the driver params."""
        with self.lock:
            access, refresh = "at_" + secrets.token_hex(12), "rt_" + secrets.token_hex(12)
            self.access[access] = time.time() + lifetime
            self.refresh[refresh] = True
            return {"access_token": access, "refresh_token": refresh,
                    "token_expires_at": int(time.time()) + lifetime}

    def expire_access_tokens(self) -> None:
        with self.lock:
            for token in self.access:
                self.access[token] = 0.0

    def expire_sts(self) -> None:
        with self.lock:
            for sts in self.sts.values():
                sts["expiry"] = 0.0

    def expire_links(self) -> None:
        with self.lock:
            self.links = {token: (file_id, ua, 0.0) for token, (file_id, ua, _) in self.links.items()}

    def new_id(self) -> str:
        return str(next(self.ids))

    def add(self, parent: str, name: str, is_dir: bool, data: bytes = b"") -> Item:
        with self.lock:
            item = Item(self.new_id(), name, parent, is_dir, data,
                        "" if is_dir else secrets.token_hex(9)[:17], self.tick())
            self.items[item.id] = item
            if not is_dir:
                self.known[item.sha1] = data
            return item

    def find(self, path: str) -> Item | None:
        with self.lock:
            item = self.items["0"]
            for name in [part for part in path.split("/") if part]:
                matches = [child for child in self.children(item.id) if child.name == name]
                if not matches:
                    return None
                item = max(matches, key=lambda child: (child.is_dir, child.mtime, int(child.id)))
            return item

    def mkdirs(self, path: str) -> str:
        with self.lock:
            folder = "0"
            for name in [part for part in path.split("/") if part]:
                match = next((c for c in self.children(folder) if c.name == name and c.is_dir), None)
                folder = match.id if match is not None else self.add(folder, name, True).id
            return folder

    def put_file(self, path: str, data: bytes) -> Item:
        parent, _, name = path.rpartition("/")
        return self.add(self.mkdirs(parent), name, False, data)

    def children(self, folder_id: str) -> list[Item]:
        return sorted((item for item in self.items.values() if item.parent == folder_id and not item.trashed),
                      key=lambda item: (not item.is_dir, item.name, int(item.id)))

    def ancestors(self, item: Item) -> list[Item]:
        chain = []
        parent = self.items.get(item.parent)
        while parent is not None:
            chain.append(parent)
            parent = self.items.get(parent.parent)
        return list(reversed(chain))

    def alive(self, item_id: str) -> Item | None:
        item = self.items.get(item_id)
        if item is None or item.trashed:
            return None
        for ancestor in self.ancestors(item):
            if ancestor.trashed:
                return None
        return item

    def qr_set(self, uid: str, status: str) -> None:
        with self.lock:
            self.qr[uid]["status"] = status

    def last_qr(self) -> str:
        with self.lock:
            return list(self.qr)[-1]

    # -- the main server

    def handle(self, request: Request) -> Response:
        path = request.path
        if path.startswith("/qrcode/"):
            if self.qr_delay:
                time.sleep(self.qr_delay)  # a long poll that doesn't answer in time
            with self.lock:
                return self._qrcode(request, path[len("/qrcode"):])
        with self.lock:
            if path.startswith("/proapi/"):
                return self._proapi(request, path[len("/proapi"):])
            if path.startswith("/passport/"):
                return self._passport(request, path[len("/passport"):])
            if path.startswith("/cdn/"):
                return self._cdn(request, path[len("/cdn/"):])
        raise HTTPError(404, b"no such endpoint")

    @staticmethod
    def _params(request: Request) -> dict[str, str]:
        if request.method == "GET":
            return {name: values[0] for name, values in request.query.items()}
        content_type = request.header("Content-Type") or ""
        if request.body and "application/x-www-form-urlencoded" not in content_type:
            raise Fail(40100000, "参数缺失")
        return request.form()

    def _check_name(self, name: str) -> None:
        if not name or name.strip() == "" or len(name) > 255 or any(ch in FORBIDDEN for ch in name):
            raise Fail(990002, "文件名不能为空、超过255个字符或包含特殊字符 \\ / : * ? \" < > |")

    def _proapi(self, request: Request, endpoint: str) -> Response:
        self.counts[endpoint] += 1
        token = request.bearer()
        try:
            if token is None:
                raise Fail(40140123, "access_token 格式错误（防篡改）")
            expiry = self.access.get(token)
            if expiry is None:
                raise Fail(40140124, "access_token 签名校验失败（防篡改）")
            if time.time() >= expiry:
                raise Fail(40140125, "access_token 无效（已过期或者已解除授权）")
            if self.refuse_all is not None:
                raise self.refuse_all
            injected = self.fail_next.pop(endpoint, None)
            if isinstance(injected, Fail):
                raise injected
            if isinstance(injected, int):
                return Response(injected, b"<html>error</html>", {"Content-Type": "text/html"})
            if isinstance(injected, bytes):
                return Response(200, injected, {"Content-Type": "text/plain"})
            handler = self.PROAPI.get((request.method, endpoint))
            if handler is None:
                raise Fail(40100000, f"不支持 {request.method} {endpoint}")
            answer = handler(self, request, self._params(request))
        except Fail as error:
            return json_response({"state": False, "code": error.code, "message": error.message, "data": []})
        answer.setdefault("state", True)
        answer.setdefault("code", 0)
        answer.setdefault("message", "")
        return json_response(answer)

    def _folder(self, folder_id: str) -> Item:
        item = self.alive(folder_id)
        if item is None or not item.is_dir:
            raise Fail(70004, "目录不存在")
        return item

    def _list_item(self, item: Item) -> dict[str, Any]:
        return {
            "fid": item.id, "aid": "1", "pid": item.parent, "fc": "0" if item.is_dir else "1", "fn": item.name,
            "fco": "", "ism": "0", "isp": 0, "pc": item.pick_code, "upt": item.mtime, "uet": item.mtime,
            "uppt": item.mtime, "cm": 0, "fdesc": "", "ispl": 0, "fl": [], "sha1": item.sha1,
            "fs": 0 if item.is_dir else len(item.data), "fta": "1",
            "ico": "" if item.is_dir else item.name.rpartition(".")[2], "thumb": "", "uo": "",
        }

    def _files(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        folder = self.alive(form.get("cid") or "0")
        if folder is None or not folder.is_dir:
            folder = self.items["0"]  # what 115 does with an unknown cid: the root
        limit = min(int(form.get("limit") or 20), 1150, self.page_limit)
        offset = int(form.get("offset") or 0)
        children = self.children(folder.id)
        custom = form.get("custom_order")  # 1: the order asked for, folders first; 2: folders among files
        if custom in ("1", "2") and form.get("o") in ORDERS:
            order, ascending = form["o"], form.get("asc") != "0"
        else:  # 0, or none: the remembered order, folders first
            order, ascending = self.remembered_order
        if order is not None:  # else children()'s, by name
            children.sort(key=lambda item: (ORDERS[order](item), int(item.id)), reverse=not ascending)
            if custom != "2":
                children.sort(key=lambda item: not item.is_dir)
        if form.get("show_dir") != "1":
            children = [child for child in children if not child.is_dir]
        page = children[offset:offset + limit]
        return {"data": [self._list_item(child) for child in page], "count": len(children),
                "cid": int(folder.id), "offset": offset, "limit": limit, "sys_count": 0,
                "path": [{"name": a.name, "cid": int(a.id)} for a in self.ancestors(folder) + [folder]]}

    def _get_info(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        item = self.alive(form.get("file_id") or "")
        if item is None:
            return {"data": []}
        return {"data": {
            "count": "0", "size": f"{len(item.data)}B", "size_byte": len(item.data), "folder_count": "0",
            "play_long": 0, "show_play_long": 0, "ptime": str(item.mtime), "utime": str(item.mtime),
            "file_name": item.name, "pick_code": item.pick_code, "sha1": item.sha1, "file_id": item.id,
            "is_mark": "0", "open_time": 0, "file_category": "0" if item.is_dir else "1",
            "paths": [{"file_id": int(a.id), "file_name": a.name} for a in self.ancestors(item)],
        }}

    def _folder_add(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        parent = self._folder(form.get("pid") or "")
        name = form.get("file_name") or ""
        self._check_name(name)
        if any(child.name == name for child in self.children(parent.id)):
            raise Fail(20004, "该目录名称已存在。")
        item = self.add(parent.id, name, True)
        return {"data": {"file_name": name, "file_id": item.id}}

    def _items(self, ids: str) -> list[Item]:
        found = []
        for item_id in ids.split(","):
            item = self.alive(item_id.strip())
            if item is None or item.id == "0":
                raise Fail(990009, "文件不存在或已删除")
            found.append(item)
        return found

    def _delete(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        items = self._items(form.get("file_ids") or "")
        parent = form.get("parent_id")
        for item in items:
            if parent and item.parent != parent:
                raise Fail(990009, "文件不存在或已删除")
            item.trashed = True  # to the recycle bin
        return {"data": []}

    def _update(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        (item,) = self._items(form.get("file_id") or "")
        name = form.get("file_name")
        if name is not None:
            self._check_name(name)
            item.name = name  # duplicates allowed
            item.mtime = self.tick()
        return {"data": {"file_name": item.name, "star": "0"}}

    def _move(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        target = self._folder(form.get("to_cid") or "")
        for item in self._items(form.get("file_ids") or ""):
            if item.id == target.id or item in self.ancestors(target):
                raise Fail(990010, "不能移动到自身或子目录")
            item.parent = target.id
        return {"data": []}

    def _copy_tree(self, item: Item, parent: str) -> None:
        copy = self.add(parent, item.name, item.is_dir, item.data)
        for child in self.children(item.id):
            self._copy_tree(child, copy.id)

    def _copy(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        target = self._folder(form.get("pid") or "")
        for item in self._items(form.get("file_id") or ""):
            if item.is_dir and (item.id == target.id or item in self.ancestors(target)):
                raise Fail(990010, "不能复制到自身或子目录")
            if form.get("nodupli") == "1" and any(c.name == item.name for c in self.children(target.id)):
                raise Fail(990011, "目标目录已存在同名文件")
            self._copy_tree(item, target.id)
        return {"data": []}

    def _search(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        for name in ("search_value", "limit", "offset"):
            if not form.get(name):
                raise Fail(40100000, "参数缺失")
        limit, offset = int(form["limit"]), int(form["offset"])
        if offset + limit > 10000:
            raise Fail(990012, "offset+limit 不能超过 10000")
        root = self.items["0"] if self.search_everywhere else self._folder(form.get("cid") or "0")
        keyword = form["search_value"].lower()
        hits: list[Item] = []
        stack = [root]
        while stack:
            for child in self.children(stack.pop().id):
                if keyword in child.name.lower() or child.name in self.fuzzy_search:
                    hits.append(child)
                if child.is_dir:
                    stack.append(child)
        hits.sort(key=lambda item: (item.name, int(item.id)))
        page = hits[offset:offset + limit]
        return {"count": len(hits), "limit": limit, "offset": offset, "data": [{
            "file_id": item.id, "user_id": "1", "sha1": item.sha1, "file_name": item.name,
            "file_size": str(len(item.data)), "user_ptime": str(item.mtime), "user_utime": str(item.mtime),
            "pick_code": item.pick_code, "parent_id": item.parent, "area_id": "1", "is_private": 0,
            "file_category": "0" if item.is_dir else "1", "ico": item.name.rpartition(".")[2],
        } for item in page]}

    def _downurl(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        pick_code = form.get("pick_code") or ""
        item = next((i for i in self.items.values() if i.pick_code == pick_code and self.alive(i.id)), None)
        if item is None or item.is_dir:
            raise Fail(50003, "文件不存在")
        token = secrets.token_urlsafe(12)
        self.links[token] = (item.id, request.header("User-Agent") or "", time.time() + self.link_lifetime)
        url = f"{self.server.url}/cdn/{token}/{urllib.parse.quote(item.name)}"
        return {"data": {item.id: {"file_name": item.name, "file_size": len(item.data), "pick_code": pick_code,
                                   "sha1": item.sha1, "url": {"url": url}}}}

    def _upload_init(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        for name in ("file_name", "file_size", "target", "fileid"):
            if name not in form:
                raise Fail(40100000, "参数缺失")
        match = re.fullmatch(r"U_1_(\d+)", form["target"])
        if match is None:
            raise Fail(990013, "target 参数错误")
        folder = self._folder(match[1])
        name, size, sha1 = form["file_name"], int(form["file_size"]), form["fileid"]
        self._check_name(name)
        if not re.fullmatch(r"[0-9A-F]{40}", sha1) or not re.fullmatch(r"[0-9A-F]{40}", form.get("preid") or "0" * 40):
            raise Fail(990014, "fileid 应该是大写的 SHA-1")
        content = self.known.get(sha1)
        if content is not None and len(content) == size:
            preid = form.get("preid")
            if preid and hashlib.sha1(content[:128 * 1024]).hexdigest().upper() != preid:
                raise Fail(990015, "preid 不对")
            if self.require_sign and size:
                sign = self.signs.get(form.get("sign_key", ""))
                if sign is None or sign[0] != sha1:
                    first = secrets.randbelow(size)
                    last = min(size - 1, first + secrets.randbelow(4096))
                    key = secrets.token_hex(8)
                    self.signs[key] = (sha1, first, last)
                    return {"data": {"status": 7, "code": 701, "sign_key": key, "sign_check": f"{first}-{last}",
                                     "pick_code": "", "target": form["target"], "file_id": ""}}
                _, first, last = sign
                if form.get("sign_val") != hashlib.sha1(content[first:last + 1]).hexdigest().upper():
                    return {"data": {"status": 8, "code": 702, "sign_key": form["sign_key"],
                                     "sign_check": f"{first}-{last}", "target": form["target"]}}
            item = self.add(folder.id, name, False, content)  # 秒传
            return {"data": {"status": 2, "code": 0, "file_id": item.id, "pick_code": item.pick_code,
                             "target": form["target"], "sign_key": "", "sign_check": ""}}
        token = secrets.token_hex(8)
        key = f"{datetime.date.today():%Y%m%d}/{secrets.token_hex(10)}"
        self.pending[token] = {"folder": folder.id, "name": name, "size": size, "sha1": sha1, "key": key}
        callback = json.dumps({"callbackUrl": "https://uplb.115.com/3.0/ossCallback.php",
                               "callbackBody": "bucket=${bucket}&object=${object}&size=${size}&x:token=${x:token}"})
        variables = json.dumps({"x:token": token})
        return {"data": {"status": 1, "code": 0, "pick_code": secrets.token_hex(9)[:17], "target": form["target"],
                         "bucket": BUCKET, "object": key, "sign_key": "", "sign_check": "", "file_id": "",
                         "callback": {"callback": callback, "callback_var": variables}}}

    def _get_token(self, request: Request, form: dict[str, str]) -> dict[str, Any]:
        key_id = "STS." + secrets.token_hex(8)
        sts = {"secret": secrets.token_urlsafe(18), "token": "CAIS" + secrets.token_urlsafe(30),
               "expiry": time.time() + 3600}
        self.sts[key_id] = sts
        expiration = datetime.datetime.fromtimestamp(sts["expiry"], datetime.timezone.utc)
        data = {"endpoint": self.oss.url, "AccessKeySecret": sts["secret"], "SecurityToken": sts["token"],
                "Expiration": expiration.strftime("%Y-%m-%dT%H:%M:%SZ"), "AccessKeyId": key_id}
        return {"data": [data] if self.token_as_list else data}

    PROAPI = {
        ("GET", "/open/ufile/files"): _files,
        ("GET", "/open/folder/get_info"): _get_info,
        ("POST", "/open/folder/add"): _folder_add,
        ("POST", "/open/ufile/delete"): _delete,
        ("POST", "/open/ufile/update"): _update,
        ("POST", "/open/ufile/move"): _move,
        ("POST", "/open/ufile/copy"): _copy,
        ("GET", "/open/ufile/search"): _search,
        ("POST", "/open/ufile/downurl"): _downurl,
        ("POST", "/open/upload/init"): _upload_init,
        ("GET", "/open/upload/get_token"): _get_token,
    }

    # -- passport and QR codes

    def _passport(self, request: Request, endpoint: str) -> Response:
        self.counts["passport" + endpoint] += 1
        try:
            if request.method != "POST":
                raise Fail(40100000, "参数缺失")
            form = self._params(request)
            if endpoint == "/open/refreshToken":
                data = self._refresh(form)
            elif endpoint == "/open/authDeviceCode":
                data = self._device_code(form)
            elif endpoint == "/open/deviceCodeToToken":
                data = self._code_to_token(form)
            else:
                raise HTTPError(404, b"no such endpoint")
        except Fail as error:
            return json_response({"state": 0, "code": error.code, "message": error.message, "data": {},
                                  "error": error.message, "errno": error.code})
        return json_response({"state": 1, "code": 0, "message": "", "data": data, "error": "", "errno": 0})

    def _tokens(self) -> dict[str, Any]:
        tokens = self.issue_tokens()
        return {"access_token": tokens["access_token"], "refresh_token": tokens["refresh_token"], "expires_in": 7200}

    def _refresh(self, form: dict[str, str]) -> dict[str, Any]:
        token = form.get("refresh_token")
        if not token:
            raise Fail(40100000, "参数缺失")
        if self.refresh_too_often:
            raise Fail(40140117, "access_token 刷新太频繁")
        state = self.refresh.get(token)
        if state is None:
            raise Fail(40140116, "refresh_token 无效（已解除授权）")
        if state is False:  # used once already: 115 made a new one
            raise Fail(40140120, "refresh_token 检验失败（防篡改）")
        self.refresh[token] = False
        return self._tokens()

    def _device_code(self, form: dict[str, str]) -> dict[str, Any]:
        if form.get("client_id") != APP_ID:
            raise Fail(40140106, "APP ID 无效")
        if not form.get("code_challenge"):
            raise Fail(40140101, "code_challenge 必填")
        if form.get("code_challenge_method") not in ("md5", "sha1", "sha256"):
            raise Fail(40140102, "code_challenge_method 必须是 sha256、sha1、md5 之一")
        uid, now = secrets.token_hex(10), int(time.time())
        sign = hashlib.sha1(f"{uid}{now}".encode()).hexdigest()
        self.qr[uid] = {"time": str(now), "sign": sign, "challenge": form["code_challenge"],
                        "method": form["code_challenge_method"], "status": "waiting"}
        return {"uid": uid, "time": now, "qrcode": f"https://115.com/scan/dg-{uid}", "sign": sign}

    def _code_to_token(self, form: dict[str, str]) -> dict[str, Any]:
        qr = self.qr.get(form.get("uid") or "")
        verifier = form.get("code_verifier") or ""
        if qr is None or qr["status"] != "confirmed":
            raise Fail(40140134, "授权码 code 验证失败")
        if not 43 <= len(verifier) <= 128:
            raise Fail(40140112, "code_verifier 长度要求43~128位")
        digest = getattr(hashlib, qr["method"])(verifier.encode()).digest()
        accepted = {base64.b64encode(digest).decode(), base64.urlsafe_b64encode(digest).decode()}
        accepted |= {value.rstrip("=") for value in accepted}
        if qr["challenge"] not in accepted:
            raise Fail(40140113, "code_verifier 验证失败")
        qr["status"] = "used"
        return self._tokens()

    def _qrcode(self, request: Request, endpoint: str) -> Response:
        self.counts["qrcode" + endpoint] += 1
        if endpoint != "/get/status/" or request.method != "GET":
            raise HTTPError(404, b"no such endpoint")
        qr = self.qr.get(request.arg("uid") or "")
        if qr is None or request.arg("sign") != qr["sign"] or request.arg("time") != qr["time"]:
            return json_response({"state": 0, "code": 0, "message": "参数错误", "data": {}})
        status = qr["status"]
        if status == "expired":
            return json_response({"state": 0, "code": 0, "message": "二维码已过期", "data": {}})
        data: dict[str, Any] = {
            "waiting": {},
            "scanned": {"msg": "扫码成功，等待确认", "status": 1, "version": "1.0"},
            "confirmed": {"msg": "已确认", "status": 2, "version": "1.0"},
            "canceled": {"msg": "已取消", "status": -2, "version": "1.0"},
        }.get(status, {})
        return json_response({"state": 1, "code": 0, "message": "", "data": data})

    # -- the CDN

    def _cdn(self, request: Request, rest: str) -> Response:
        self.counts["cdn"] += 1
        if (request.header("Authorization") or "").startswith("Bearer"):
            self.leaks.append("cdn")
        token = rest.partition("/")[0]
        link = self.links.get(token)
        if link is None:
            raise HTTPError(404, b"no such link")
        file_id, user_agent, expiry = link
        if time.time() >= expiry:
            raise HTTPError(403, b"link expired")
        if request.header("User-Agent") != user_agent:
            raise HTTPError(403, b"user agent mismatch")
        item = self.alive(file_id)
        if item is None:
            raise HTTPError(404, b"gone")
        return file_response(item.data, request.header("Range"))

    # -- OSS

    def handle_oss(self, request: Request) -> Response:
        with self.lock:
            return self._oss(request)

    def _oss(self, request: Request) -> Response:
        bucket, _, key = request.path.lstrip("/").partition("/")
        bucket, key = urllib.parse.unquote(bucket), urllib.parse.unquote(key)
        query = []
        for part in request.raw_query.split("&") if request.raw_query else []:
            name, eq, value = part.partition("=")
            query.append((urllib.parse.unquote(name), urllib.parse.unquote(value) if eq else None))
        sub = sorted((name, value) for name, value in query if name in OSS_SUBRESOURCES)
        resource = f"/{bucket}/{key}" + ("?" + "&".join(n if v is None else f"{n}={v}" for n, v in sub) if sub else "")
        self.oss_requests.append((request.method, request.raw_query))
        if request.header("Authorization", "").startswith("Bearer"):
            self.leaks.append("oss")
        self._oss_auth(request, resource)
        if bucket != BUCKET:
            raise oss_error(404, "NoSuchBucket")
        names = {name for name, _ in query}
        if request.method == "PUT" and not names:
            return self._finish(request, key, request.body)
        if request.method == "POST" and "uploads" in names and names <= {"uploads", "sequential"}:
            upload_id = secrets.token_hex(16).upper()
            self.multipart[upload_id] = {"key": key, "parts": {}}
            body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<InitiateMultipartUploadResult '
                    f'xmlns="http://doc.oss-cn-hangzhou.aliyuncs.com"><Bucket>{BUCKET}</Bucket><Key>{key}</Key>'
                    f"<UploadId>{upload_id}</UploadId></InitiateMultipartUploadResult>").encode()
            return Response(200, body, {"Content-Type": "application/xml"})
        upload = self.multipart.get(dict(query).get("uploadId") or "")
        if names & {"uploadId"} and (upload is None or upload["key"] != key):
            raise oss_error(404, "NoSuchUpload")
        if request.method == "PUT" and names == {"partNumber", "uploadId"}:
            number = int(dict(query)["partNumber"])
            if not 1 <= number <= 10000:
                raise oss_error(400, "InvalidArgument")
            if number in self.oss_fail_parts:
                raise oss_error(500, "InternalError")
            etag = '"' + hashlib.md5(request.body).hexdigest().upper() + '"'
            upload["parts"][number] = (etag, request.body)
            return Response(200, b"", {"ETag": etag})
        if request.method == "POST" and names == {"uploadId"}:
            root = ElementTree.fromstring(request.body)
            listed = [(int(part.findtext("PartNumber")), part.findtext("ETag")) for part in root.iter("Part")]
            numbers = [number for number, _ in listed]
            if not listed or numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
                raise oss_error(400, "InvalidPartOrder")
            chunks = []
            for index, (number, etag) in enumerate(listed):
                stored = upload["parts"].get(number)
                if stored is None or stored[0] != etag:
                    raise oss_error(400, "InvalidPart")
                if index < len(listed) - 1 and len(stored[1]) < 100 * 1024:
                    raise oss_error(400, "EntityTooSmall")
                chunks.append(stored[1])
            del self.multipart[dict(query)["uploadId"]]
            return self._finish(request, key, b"".join(chunks))
        if request.method == "DELETE" and names == {"uploadId"}:
            del self.multipart[dict(query)["uploadId"]]
            return Response(204, b"")
        raise oss_error(400, "InvalidRequest")

    def _oss_auth(self, request: Request, resource: str) -> None:
        match = re.fullmatch(r"OSS ([^:\s]+):(\S+)", request.header("Authorization") or "")
        if match is None:
            raise oss_error(403, "AccessDenied", "Authorization header is invalid.")
        sts = self.sts.get(match[1])
        if sts is None:
            raise oss_error(403, "InvalidAccessKeyId")
        if request.header("x-oss-security-token") != sts["token"]:
            raise oss_error(403, "InvalidSecurityToken")
        if time.time() >= sts["expiry"]:
            raise oss_error(403, "SecurityTokenExpired", "The security token you provided has expired.")
        try:
            date = email.utils.parsedate_to_datetime(request.header("Date") or "")
        except (TypeError, ValueError):
            raise oss_error(403, "AccessDenied", "Date is missing.") from None
        if abs(date.timestamp() - time.time()) > 900:
            raise oss_error(403, "RequestTimeTooSkewed")
        if match[2] != fake_v1_signature(sts["secret"], request.method, request.headers, resource):
            raise oss_error(403, "SignatureDoesNotMatch")

    def _finish(self, request: Request, key: str, data: bytes) -> Response:
        """The object is complete: run 115's callback and answer with its result."""
        self.objects[key] = data
        header = request.header("x-oss-callback")
        if not header:
            return Response(200, b"", {"ETag": '"' + hashlib.md5(data).hexdigest().upper() + '"'})
        callback = json.loads(base64.b64decode(header))
        variables = json.loads(base64.b64decode(request.header("x-oss-callback-var") or "e30="))
        if not callback.get("callbackUrl") or not callback.get("callbackBody"):
            raise oss_error(400, "InvalidArgument", "callback is invalid")
        pending = self.pending.get(variables.get("x:token", ""))
        sha1 = hashlib.sha1(data).hexdigest().upper()
        if (self.fail_callback or pending is None or pending["key"] != key or pending["size"] != len(data)
                or pending["sha1"] != sha1):
            body = b'<?xml version="1.0" encoding="UTF-8"?>\n<Error><Code>CallbackFailed</Code>' \
                   b"<Message>Error status : 400.</Message></Error>"
            return Response(203, body, {"Content-Type": "application/xml"})
        del self.pending[variables["x:token"]]
        name = pending["name"]
        if self.rename_on_conflict and any(child.name == name for child in self.children(pending["folder"])):
            stem, dot, ext = name.rpartition(".")
            name = f"{stem}(1).{ext}" if dot else f"{name}(1)"
        if self.callback_makes_nothing:
            return json_response({"state": True, "code": 0, "message": "", "data": {"file_name": name}})
        item = self.add(pending["folder"], name, False, data)
        answer = {"pick_code": item.pick_code, "file_name": item.name, "file_size": len(data), "file_id": item.id,
                  "thumb_url": "", "sha1": sha1, "aid": 1, "cid": pending["folder"]}
        if self.omit_file_id:
            del answer["file_id"]
        return json_response({"state": True, "code": 0, "message": "", "data": answer})
