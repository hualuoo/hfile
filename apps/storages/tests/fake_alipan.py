"""An in-memory fake of Alipan's open platform API (apps.storages.drivers.alipan_open), on FakeServer.

It refuses what the documentation (https://www.yuque.com/aliyundrive/zpfszx) says Alipan refuses:

- /oauth/access_token takes JSON only; a code and a refresh token are good for one use, a refresh gives a new
  pair; a wrong App ID or secret is AppNotExists / InvalidClientSecret.
- The API: JSON bodies, "Authorization: Bearer"; 401 AccessTokenInvalid / AccessTokenExpired, 403
  PermissionDenied when the authorization is withdrawn, 403 UserNotAllowedAccessDrive for a drive not allowed.
- list: at most 100 a page, an opaque next_marker.
- create: a folder with refuse gives the one there with "exist"; a file has parts (at most 10,000); pre_hash
  of a file it has is 409 PreHashMatched; content_hash with a wrong proof_code is refused.
- Upload URLs expire and are refused with a Content-Type (SignatureDoesNotMatch); complete wants every part,
  all but the last at least 100 KB.
- Download links expire, are refused with a Referer, and with 403 ExceedMaxConcurrency while `busy` > 0. A frozen
  file (punish_flag 2), or one with punish_flag 103 in the resource library, gives a notice instead (FAQ 38).
- A Live Photo (.livp) has no download url, only streamsUrl with its picture and video ([OpenList]:
  drivers/aliyundrive_open/driver.go, Link).
- A folder copy's task can't be asked about: async_task/get answers 400 (the copy doc: "暂时未开放查询task状态的接口"
  [what it answers is assumed]).
- consent(url): the account allows the app on the authorize page; the drives the URL asks for replace the ones
  allowed before (FAQ 47: "用户选择某个盘符授权，最后一次选择替换之前的选择").

Knobs: throttle (429s to give), busy, async_folders, url_ttl, link_ttl, allowed (drives), revoked, known
(SHA-1 -> bytes Alipan already has: rapid uploads), expire_tokens(), expire_links().
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import itertools
import secrets
import threading
import time
import urllib.parse

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

CLIENT_ID = "app-1"
CLIENT_SECRET = "secret-1"
USER_ID = "user-1"
MIN_PART = 100 * 1024
NOTICE = b"This file was frozen."


def _iso(moment: float) -> str:
    return datetime.datetime.fromtimestamp(moment, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _sha1(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest().upper()


def proof(token: str, data: bytes) -> str:
    if not data:
        return ""
    first = int(hashlib.md5(token.encode()).hexdigest()[:16], 16) % len(data)
    return base64.b64encode(data[first:min(first + 8, len(data))]).decode()


def _oss_error(code: str, message: str) -> Response:
    body = (f'<?xml version="1.0" encoding="UTF-8"?>\n<Error>\n  <Code>{code}</Code>\n  <Message>{message}</Message>\n'
            "  <RequestId>6655C0A3822EB93231B6B974</RequestId>\n</Error>").encode()
    return Response(403, body, {"Content-Type": "application/xml"})


@dataclasses.dataclass
class Item:
    id: str
    drive: str
    parent: str
    name: str
    folder: bool
    data: bytes = b""
    created: float = 0.0
    updated: float = 0.0
    punish: int = 0
    trashed: bool = False


class FakeAlipan:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.items: dict[str, Item] = {}
        self.drives = {"resource": "drive-r", "backup": "drive-b"}
        self.allowed = {"resource", "backup"}
        self.codes: set[str] = set()
        self.refresh_tokens: set[str] = set()
        self.access_tokens: dict[str, float] = {}  # token -> expiry (time.time())
        self.revoked = False
        self.uploads: dict[str, dict] = {}
        self.tasks: dict[str, int] = {}  # task id -> polls before it has succeeded
        self.copy_tasks: set[str] = set()  # tasks of folder copies, which can't be asked about
        self.links: dict[str, tuple[str, float]] = {}  # link key -> (file id, expiry)
        self.known: dict[str, bytes] = {}
        self.throttle = 0
        self.retry_after_ms = 30
        self.busy = 0
        self.async_folders = False
        self.url_ttl = 3600.0
        self.link_ttl = 900.0
        self.refreshes = 0
        self.calls: list[tuple[str, dict]] = []
        self.puts: list[dict] = []
        self.downloads: list[dict] = []
        self._ids = itertools.count(1)

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeAlipan:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    # -- for tests

    def _new_id(self) -> str:
        return f"{next(self._ids):04x}" + secrets.token_hex(18)

    def issue_code(self) -> str:
        code = secrets.token_hex(8)
        with self.lock:
            self.codes.add(code)
        return code

    def consent(self, url: str) -> str:
        """The account allows the app on the authorize page at `url`: the drives it asks for replace the ones allowed
        before (FAQ 47). Gives the code the page sends to the callback."""
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if query.get("client_id") != [CLIENT_ID]:
            raise ValueError("not this app")
        with self.lock:
            self.allowed = set(query.get("drive", ["resource,backup"])[0].split(",")) & set(self.drives)
        return self.issue_code()

    def _issue(self) -> dict:
        access, refresh = "at-" + secrets.token_hex(12), "rt-" + secrets.token_hex(12)
        self.access_tokens[access] = time.time() + 7200
        self.refresh_tokens.add(refresh)
        return {"token_type": "Bearer", "access_token": access, "refresh_token": refresh, "expires_in": 7200}

    def issue_tokens(self) -> dict:
        """Settings of an authorized storage: the tokens and their expiry."""
        with self.lock:
            answer = self._issue()
        return {"access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
                "token_expires_at": int(time.time()) + 7200}

    def expire_tokens(self) -> None:
        with self.lock:
            for token in self.access_tokens:
                self.access_tokens[token] = 0.0

    def expire_links(self) -> None:
        with self.lock:
            for key, (file_id, _) in list(self.links.items()):
                self.links[key] = (file_id, 0.0)

    def find(self, path: str, drive: str = "resource") -> Item | None:
        parent, item = "root", None
        for name in [part for part in path.split("/") if part]:
            found = [i for i in self._children(self.drives[drive], parent) if i.name == name]
            if not found:
                return None
            item = found[0]
            parent = item.id
        return item

    def mkdirs(self, path: str, drive: str = "resource") -> str:
        with self.lock:
            parent = "root"
            for name in [part for part in path.split("/") if part]:
                found = [i for i in self._children(self.drives[drive], parent) if i.name == name and i.folder]
                parent = found[0].id if found else self._add(self.drives[drive], parent, name, folder=True).id
            return parent

    def put(self, path: str, data: bytes, drive: str = "resource", **fields) -> Item:
        with self.lock:
            folder, _, name = path.rpartition("/")
            item = self._add(self.drives[drive], self.mkdirs(folder, drive), name, folder=False, data=data)
            for key, value in fields.items():
                setattr(item, key, value)
            self.known[_sha1(data)] = data
            return item

    def names(self, path: str, drive: str = "resource") -> list[str]:
        folder = self.find(path, drive) if path.strip("/") else None
        parent = folder.id if folder is not None else "root"
        return sorted(i.name for i in self._children(self.drives[drive], parent))

    def api_calls(self, api: str) -> list[dict]:
        return [body for path, body in self.calls if path.endswith(api)]

    # -- the tree

    def _add(self, drive: str, parent: str, name: str, *, folder: bool, data: bytes = b"", item_id: str | None = None) -> Item:
        moment = time.time()
        item = Item(id=item_id or self._new_id(), drive=drive, parent=parent, name=name, folder=folder, data=data,
                    created=moment, updated=moment)
        self.items[item.id] = item
        return item

    def _children(self, drive: str, parent: str) -> list[Item]:
        return sorted((i for i in self.items.values() if i.drive == drive and i.parent == parent and not i.trashed),
                      key=lambda i: (i.name, i.id))

    def _in_trash(self, item: Item) -> bool:
        while True:
            if item.trashed:
                return True
            if item.parent == "root":
                return False
            item = self.items[item.parent]

    def _item(self, drive: str, file_id) -> Item:
        item = self.items.get(file_id) if isinstance(file_id, str) else None
        if item is None or item.drive != drive:
            raise HTTPError(404, {"code": "NotFound.FileId", "message": "file not found"})
        if self._in_trash(item):
            raise HTTPError(403, {"code": "ForbiddenFileInTheRecycleBin", "message": "file is in the recycle bin"})
        return item

    def _folder(self, drive: str, file_id) -> str:
        if file_id == "root":
            return "root"
        item = self._item(drive, file_id)
        if not item.folder:
            raise HTTPError(400, {"code": "InvalidParameter.ParentFileId", "message": "not a folder"})
        return item.id

    def _view(self, item: Item) -> dict:
        view = {"drive_id": item.drive, "file_id": item.id, "parent_file_id": item.parent, "name": item.name,
                "type": "folder" if item.folder else "file", "created_at": _iso(item.created),
                "updated_at": _iso(item.updated), "punish_flag": item.punish}
        if not item.folder:
            view.update({"size": len(item.data), "content_hash": _sha1(item.data),
                         "file_extension": item.name.rpartition(".")[2] if "." in item.name else "", "category": "others"})
        return view

    @staticmethod
    def _check_name(name) -> str:
        if not isinstance(name, str) or not name or name.endswith("/") or "/" in name or len(name.encode()) > 1024:
            raise HTTPError(400, {"code": "InvalidParameter.Name", "message": "invalid name"})
        return name

    def _task(self) -> str:
        task = "task-" + secrets.token_hex(6)
        self.tasks[task] = 2
        return task

    # -- requests

    def handle(self, request: Request) -> Response:
        if request.path.startswith("/oss/"):
            return self._oss(request)
        if request.path.startswith("/dl/"):
            return self._download(request)
        if request.method != "POST":
            raise HTTPError(405, b"POST only")
        if "application/json" not in (request.header("Content-Type") or ""):
            raise HTTPError(400, {"code": "InvalidParameter", "message": "the body should be JSON"})
        body = request.json() or {}
        with self.lock:
            if request.path == "/oauth/access_token":
                return json_response(self._token(body))
            if self.throttle > 0:
                self.throttle -= 1
                return json_response({"code": "TooManyRequests", "message": "too fast"}, 429,
                                     {"x-retry-after": str(self.retry_after_ms)})
            token = self._auth(request)
            self.calls.append((request.path, body))
            handler = {
                "/adrive/v1.0/user/getDriveInfo": self._drive_info,
                "/adrive/v1.0/openFile/list": self._list,
                "/adrive/v1.0/openFile/create": lambda b: self._create(b, token),
                "/adrive/v1.0/openFile/getUploadUrl": self._upload_url,
                "/adrive/v1.0/openFile/complete": self._complete,
                "/adrive/v1.0/openFile/update": self._update,
                "/adrive/v1.0/openFile/move": self._move,
                "/adrive/v1.0/openFile/copy": self._copy,
                "/adrive/v1.0/openFile/recyclebin/trash": self._trash,
                "/adrive/v1.0/openFile/async_task/get": self._task_state,
                "/adrive/v1.0/openFile/getDownloadUrl": self._download_url,
            }.get(request.path)
            if handler is None:
                raise HTTPError(404, {"code": "NotFound", "message": "no such API"})
            return json_response(handler(body))

    def _token(self, body: dict) -> dict:
        if body.get("client_id") != CLIENT_ID:
            raise HTTPError(400, {"code": "AppNotExists", "message": "app not exists"})
        if body.get("client_secret") != CLIENT_SECRET:
            raise HTTPError(400, {"code": "InvalidClientSecret", "message": "invalid client secret"})
        grant = body.get("grant_type")
        if grant == "authorization_code":
            if body.get("code") not in self.codes:
                raise HTTPError(400, {"code": "InvalidCode", "message": "code is empty or expired"})
            self.codes.discard(body["code"])
        elif grant == "refresh_token":
            if body.get("refresh_token") not in self.refresh_tokens:
                raise HTTPError(401, {"code": "RefreshTokenInvalid", "message": "invalid refresh token"})
            self.refresh_tokens.discard(body["refresh_token"])
            self.refreshes += 1
        else:
            raise HTTPError(400, {"code": "InvalidGrantType", "message": "invalid grant type"})
        return self._issue()

    def _auth(self, request: Request) -> str:
        token = request.bearer()
        if self.revoked:
            raise HTTPError(403, {"code": "PermissionDenied", "message": "permission not exists"})
        expiry = self.access_tokens.get(token or "")
        if expiry is None:
            raise HTTPError(401, {"code": "AccessTokenInvalid", "message": "invalid access token"})
        if time.time() >= expiry:
            raise HTTPError(401, {"code": "AccessTokenExpired", "message": "JWT is expired"})
        return token

    def _drive(self, body: dict) -> str:
        drive = body.get("drive_id")
        kinds = [kind for kind, value in self.drives.items() if value == drive]
        if not kinds or kinds[0] not in self.allowed:
            raise HTTPError(403, {"code": "UserNotAllowedAccessDrive", "message": "drive not allowed"})
        return drive

    def _drive_info(self, body: dict) -> dict:
        answer = {"user_id": USER_ID, "name": "alice", "avatar": "", "default_drive_id": self.drives["backup"]}
        for kind in sorted(self.allowed):
            answer[f"{kind}_drive_id"] = self.drives[kind]
        return answer

    def _list(self, body: dict) -> dict:
        drive = self._drive(body)
        parent = self._folder(drive, body.get("parent_file_id"))
        limit = min(int(body.get("limit") or 50), 100)
        marker = body.get("marker") or ""
        start = int(marker[1:]) if marker.startswith("m") else 0
        children = self._children(drive, parent)
        page = children[start:start + limit]
        more = start + limit < len(children)
        return {"items": [self._view(i) for i in page], "next_marker": f"m{start + limit}" if more else ""}

    def _part_urls(self, upload_id: str, numbers) -> list[dict]:
        expiry = time.time() + self.url_ttl
        return [{"part_number": n, "upload_url": f"{self.url}/oss/{upload_id}/{n}?expires={expiry}&sig={_sha1(f'{upload_id}{n}{expiry}'.encode())}",
                 "part_size": None, "etag": None, "content_type": ""} for n in numbers]

    def _create(self, body: dict, token: str) -> dict:
        drive = self._drive(body)
        parent = self._folder(drive, body.get("parent_file_id"))
        name = self._check_name(body.get("name"))
        mode = body.get("check_name_mode")
        if mode not in ("auto_rename", "refuse", "ignore"):
            raise HTTPError(400, {"code": "InvalidParameter.CheckNameMode", "message": "check_name_mode is required"})
        same = [i for i in self._children(drive, parent) if i.name == name]
        base = {"drive_id": drive, "parent_file_id": parent, "file_name": name}
        if same and mode == "refuse":
            return {**base, "file_id": same[0].id, "type": "folder" if same[0].folder else "file", "exist": True,
                    "upload_id": "", "rapid_upload": False, "part_info_list": []}
        if mode == "auto_rename" and same:
            raise HTTPError(400, {"code": "NotImplemented", "message": "the fake doesn't rename"})
        if body.get("type") == "folder":
            item = self._add(drive, parent, name, folder=True)
            return {**base, "file_id": item.id, "type": "folder", "exist": False, "upload_id": "", "rapid_upload": False}
        if body.get("type") != "file":
            raise HTTPError(400, {"code": "InvalidParameter.Type", "message": "type"})
        parts = body.get("part_info_list") or []
        numbers = [part.get("part_number") for part in parts if isinstance(part, dict)]
        if numbers != list(range(1, len(parts) + 1)) or len(parts) > 10_000:
            raise HTTPError(400, {"code": "InvalidParameter.PartInfoList", "message": "part numbers"})
        size = body.get("size")
        if "pre_hash" in body:
            if any(len(data) == size and _sha1(data[:1024]) == body["pre_hash"] for data in self.known.values()):
                raise HTTPError(409, {"code": "PreHashMatched", "message": "pre hash matched"})
        file_id, upload_id = self._new_id(), "up-" + secrets.token_hex(8)
        upload = {"file_id": file_id, "drive": drive, "parent": parent, "name": name, "count": len(parts),
                  "parts": {}, "done": False, "rapid": False}
        if "content_hash" in body:
            if body.get("content_hash_name") != "sha1" or body.get("proof_version") != "v1":
                raise HTTPError(400, {"code": "InvalidParameter.ContentHash", "message": "sha1 and v1"})
            data = self.known.get(str(body["content_hash"]).upper())
            if data is not None and len(data) == size:
                if body.get("proof_code") != proof(token, data):
                    raise HTTPError(400, {"code": "InvalidParameter.ProofCode", "message": "proof code mismatch"})
                self._add(drive, parent, name, folder=False, data=data, item_id=file_id)
                upload.update(done=True, rapid=True)
                self.uploads[upload_id] = upload
                return {**base, "file_id": file_id, "upload_id": upload_id, "rapid_upload": True, "exist": bool(same),
                        "part_info_list": []}
        self.uploads[upload_id] = upload
        return {**base, "file_id": file_id, "upload_id": upload_id, "rapid_upload": False, "exist": bool(same),
                "part_info_list": self._part_urls(upload_id, numbers)}

    def _upload(self, body: dict) -> dict:
        upload = self.uploads.get(body.get("upload_id"))
        if upload is None or upload["file_id"] != body.get("file_id") or upload["drive"] != self._drive(body):
            raise HTTPError(404, {"code": "NotFound.UploadId", "message": "upload not found"})
        return upload

    def _upload_url(self, body: dict) -> dict:
        upload = self._upload(body)
        numbers = [part.get("part_number") for part in body.get("part_info_list") or []]
        return {"drive_id": upload["drive"], "file_id": upload["file_id"], "upload_id": body["upload_id"],
                "created_at": _iso(time.time()), "part_info_list": self._part_urls(body["upload_id"], numbers)}

    def _complete(self, body: dict) -> dict:
        upload = self._upload(body)
        if upload["rapid"]:
            return self._view(self.items[upload["file_id"]])
        if upload["done"]:
            raise HTTPError(400, {"code": "UploadAlreadyCompleted", "message": "completed"})
        parts = upload["parts"]
        if sorted(parts) != list(range(1, upload["count"] + 1)):
            raise HTTPError(400, {"code": "PartNotComplete", "message": "parts missing"})
        if any(len(parts[n]) < MIN_PART for n in range(1, upload["count"])):
            raise HTTPError(400, {"code": "PartTooSmall", "message": "part smaller than 100KB"})
        data = b"".join(parts[n] for n in range(1, upload["count"] + 1))
        item = self._add(upload["drive"], upload["parent"], upload["name"], folder=False, data=data, item_id=upload["file_id"])
        upload["done"] = True
        self.known[_sha1(data)] = data
        return self._view(item)

    def _oss(self, request: Request) -> Response:
        if request.method != "PUT":
            raise HTTPError(405, b"PUT only")
        _, _, upload_id, number = request.path.split("/")
        with self.lock:
            self.puts.append({"upload": upload_id, "part": int(number), "content_type": request.header("Content-Type"),
                              "length": len(request.body)})
            if request.header("Content-Type"):
                return _oss_error("SignatureDoesNotMatch", "The request signature we calculated does not match.")
            expiry = request.arg("expires") or "0"
            if request.arg("sig") != _sha1(f"{upload_id}{number}{expiry}".encode()):
                return _oss_error("SignatureDoesNotMatch", "The request signature we calculated does not match.")
            if time.time() > float(expiry):
                return _oss_error("AccessDenied", "Request has expired.")
            upload = self.uploads.get(upload_id)
            if upload is None or upload["done"] or not 1 <= int(number) <= upload["count"]:
                raise HTTPError(404, b"NoSuchUpload")
            if int(number) in upload["parts"]:
                raise HTTPError(409, b"PartAlreadyExist")
            upload["parts"][int(number)] = request.body
        return Response(200, b"", {"ETag": '"' + hashlib.md5(request.body).hexdigest() + '"'})

    def _update(self, body: dict) -> dict:
        drive = self._drive(body)
        item = self._item(drive, body.get("file_id"))
        name = self._check_name(body.get("name"))
        mode = body.get("check_name_mode") or "ignore"
        if mode == "refuse" and any(i.name == name and i.id != item.id for i in self._children(drive, item.parent)):
            raise HTTPError(409, {"code": "AlreadyExist.File", "message": "a file with that name exists"})
        item.name = name
        item.updated = time.time()
        return self._view(item)

    def _target(self, drive: str, item: Item, folder) -> str:
        target = self._folder(drive, folder)
        walk = target
        while walk != "root":
            if walk == item.id:
                raise HTTPError(400, {"code": "InvalidParameter.ToParentFileId", "message": "into itself"})
            walk = self.items[walk].parent
        return target

    def _move(self, body: dict) -> dict:
        drive = self._drive(body)
        item = self._item(drive, body.get("file_id"))
        target = self._target(drive, item, body.get("to_parent_file_id"))
        mode = body.get("check_name_mode") or "refuse"
        if mode == "refuse" and any(i.name == item.name and i.id != item.id for i in self._children(drive, target)):
            return {"drive_id": drive, "file_id": item.id, "async_task_id": "", "exist": True}
        item.parent = target
        task = self._task() if item.folder and self.async_folders else ""
        return {"drive_id": drive, "file_id": item.id, "async_task_id": task, "exist": False}

    def _copy_tree(self, item: Item, parent: str) -> Item:
        copy = self._add(item.drive, parent, item.name, folder=item.folder, data=item.data)
        for child in self._children(item.drive, item.id):
            self._copy_tree(child, copy.id)
        return copy

    def _copy(self, body: dict) -> dict:
        drive = self._drive(body)
        item = self._item(drive, body.get("file_id"))
        target = self._target(drive, item, body.get("to_parent_file_id"))
        if body.get("auto_rename"):
            raise HTTPError(400, {"code": "NotImplemented", "message": "the fake doesn't rename"})
        copy = self._copy_tree(item, target)
        answer = {"drive_id": drive, "file_id": copy.id}
        if item.folder:
            answer["async_task_id"] = self._task() if self.async_folders else ""
            if answer["async_task_id"]:
                self.copy_tasks.add(answer["async_task_id"])
        return answer

    def _trash(self, body: dict) -> dict:
        drive = self._drive(body)
        item = self._item(drive, body.get("file_id"))
        item.trashed = True
        return {"drive_id": drive, "file_id": item.id,
                "async_task_id": self._task() if item.folder and self.async_folders else ""}

    def _task_state(self, body: dict) -> dict:
        task = body.get("async_task_id")
        if task in self.copy_tasks:
            raise HTTPError(400, {"code": "InvalidParameter.AsyncTaskId", "message": "the task can't be queried"})
        if task not in self.tasks:
            raise HTTPError(404, {"code": "NotFound.AsyncTask", "message": "no such task"})
        self.tasks[task] -= 1
        return {"async_task_id": task, "state": "Succeed" if self.tasks[task] <= 0 else "Running"}

    def _download_url(self, body: dict) -> dict:
        drive = self._drive(body)
        item = self._item(drive, body.get("file_id"))
        if item.folder:
            raise HTTPError(400, {"code": "InvalidParameter.FileId", "message": "a folder"})
        key = secrets.token_hex(12)
        expiry = time.time() + min(self.link_ttl, float(body.get("expire_sec") or 900))
        self.links[key] = (item.id, expiry)
        url = f"{self.url}/dl/{key}?" + urllib.parse.urlencode({"x-oss-expires": int(expiry)})
        if item.name.lower().endswith(".livp"):  # its parts only
            return {"url": "", "streamsUrl": {"jpeg": url, "mov": url}, "expiration": _iso(expiry), "method": "GET",
                    "description": ""}
        return {"url": url, "expiration": _iso(expiry), "method": "GET", "description": ""}

    def _download(self, request: Request) -> Response:
        key = request.path.split("/")[2]
        with self.lock:
            self.downloads.append({"range": request.header("Range"), "referer": request.header("Referer")})
            if request.header("Referer"):
                return _oss_error("AccessDenied", "Referer not allowed.")
            file_id, expiry = self.links.get(key, ("", 0.0))
            if time.time() > expiry:
                return _oss_error("AccessDenied", "Request has expired.")
            if self.busy > 0:
                self.busy -= 1
                return _oss_error("RequestDeniedByCallback", "Callback deny this request reason: ExceedMaxConcurrency")
            item = self.items[file_id]
            notice = item.punish == 2 or (item.punish == 103 and item.drive == self.drives["resource"])
            data = NOTICE if notice else item.data
        return file_response(data, request.header("Range"))
