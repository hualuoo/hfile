"""An in-memory fake of a KodBox server (apps.storages.drivers.kodbox), on FakeServer.

    fake = FakeKodBox().start()
    home = fake.add_user("alice", "pw-alice")    # the id of her own folder
    fake.url
    fake.close()

It answers as KodBox's documentation (https://doc.kodcloud.com/v2/) and source (github.com/kalcaddle/kodbox) say:

- The route is the first key of index.php's query; parameters come from the query and the body alike, the body's
  winning (parse_incoming). Answers are show_json's {"code", "timeUse", "timeNow", "data", "info"} with HTTP 200,
  and X-Request-Error when the code is false; each sets KodBox's session cookie.
- user/index/loginSubmit gives the token of a new session. An access token KodBox can't read, or whose session
  ended, is code "10001"; a disabled account "10002"; while `captcha`, the login answers info "10011". A session
  signed out by a login on more devices than the account may have (filter/userLoginState.class.php checkLimit,
  userLoginLimit) answers "10002" with "您账号已在其他设备登录,请稍后重试." (user/authRole.class.php): kick_sessions().
  user/index/logout ends the token's session.
- user/view/options: user.myhome, user.config (displayHideFile, recycleOpen, fileRepeat),
  system.settings.upload.chunkSize, and system.options.pathHidden while `options_list_hidden`.
- explorer/list/path pages (page, pageNum: at least 5, at most `max_page_num`) folders before files, then leaves
  out what the account doesn't show: names starting with . or ~ and those of `path_hidden`. The account's own
  folder lists the safe box ({block:safe}/) on its first page.
- mkdir and pathRename refuse / \\ : * ? " < > | and names over 256 bytes; unless `utf8mb4`, characters of 4 bytes
  become "-". mkdir keeps a folder of the name (skip), and answers the parent's path when a file has it.
- pathDelete puts things into the recycle bin while the account's recycleOpen is "1" and shiftDelete isn't 1.
- pathCuteTo and pathCopyTo with fileRepeat (default the account's: replace); skip fails for a file of a taken
  name and answers the folder of it for a folder.
- fileUpload: multipart with the file in "file"; \\ : * ? " < > | become "_" (pathAllowReplace); parts (chunk from
  0) go into the temp file Uploader keys by checkHashSimple (else the user, folder, name and size) and chunkSize
  until all have come; then the file's checkHashSimple is checked. `upload_max` refuses larger parts
  (UPLOAD_ERR_INI_SIZE); `body_max` larger requests (413, as nginx). `slow_finish`: seconds the request that ends an
  upload waits after storing the file before it answers (KodBox copying it to object storage).
- fileOut (GET or POST) sends the file with Range and, for download=1, Content-Disposition; while
  `redirect_downloads` it redirects to /storage/<key>, which needs no token.

Knobs: captcha, utf8mb4, path_hidden, options_list_hidden, chunk_size, max_page_num, upload_max, body_max,
redirect_downloads, space_full, denied (routes), fail_status, name_filter (what KodBox makes of an upload's name),
expire_on_chunk (the sessions end when that part arrives), slow_finish. Sessions: expire_sessions(), forget_tokens(),
kick_sessions(). Records: calls, logins, downloads, storage_requests.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Request, Response, file_response, json_response

SIGNED_OUT, DISABLED, CAPTCHA = "10001", "10002", "10011"
NOT_ALLOWED = '/\\:*?"<>|'  # pathAllowCheck
REPLACED = '\\:*?"<>|\r\n'  # pathAllowReplace
DEFAULT_HIDDEN = "Thumb.db,.DS_Store,.gitignore,.git,*.temp,*.tmp"
GONE = "该文档不存在！"
IN_TRASH = "该文件夹在回收站中,请还原后再试!"
UPLOAD = "explorer/upload/fileUpload"


def hash_simple(data: bytes) -> str:
    """checkHashSimple as explorer/checkHashSimple.md has it in PHP."""
    size = len(data)
    if size <= 200 * 50:
        return hashlib.md5(data).hexdigest() + str(size)
    step = size // 50
    sample = b"".join(data[step * n:step * n + 200] for n in range(50)) + data[size - 200:]
    return hashlib.md5(sample).hexdigest() + str(size)


def extension(name: str) -> str:
    """get_path_ext: after the last dot, the first run of letters, digits, _ and -, in lower case."""
    dot = name.rfind(".")
    match = re.search(r"[0-9a-zA-Z_\-]+", name[dot + 1:]) if dot >= 0 else None
    return match.group(0).lower() if match else ""


def answer(data: Any, code: Any = True, info: Any = None, more: Any = None) -> Response:
    body = {"code": code, "timeUse": "0.0010", "timeNow": f"{time.time():.4f}", "data": data}
    if info is not None:
        body["info"] = info
    if more is not None:
        body["infoMore"] = more
    headers = {"X-Powered-By": "kodbox.", "Set-Cookie": f"KOD_SESSION_ID={secrets.token_hex(8)}; path=/"}
    if code is False:
        headers["X-Request-Error"] = "1"
    return json_response(body, 200, headers)


def refuse(data: Any, code: Any = False, info: Any = None) -> HTTPError:
    return HTTPError(200, answer(data, code, info))


def multipart(body: bytes, content_type: str) -> tuple[dict[str, str], bytes | None]:
    """The fields of a multipart/form-data body, and the bytes of its file."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type)
    if not boundary:
        raise HTTPError(400, b"not multipart")
    fields: dict[str, str] = {}
    file = None
    for part in body.split(b"--" + boundary.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part[2:].partition(b"\r\n\r\n")
        value = value[:-2] if value.endswith(b"\r\n") else value
        disposition = head.decode("utf-8")
        name = re.search(r'name="([^"]*)"', disposition)
        if "filename=" in disposition:
            file = value
        elif name:
            fields[name.group(1)] = value.decode("utf-8")
    return fields, file


@dataclasses.dataclass
class User:
    name: str
    password: str
    home: int
    disabled: bool = False
    config: dict[str, str] = dataclasses.field(
        default_factory=lambda: {"displayHideFile": "0", "recycleOpen": "1", "fileRepeat": "replace"})


@dataclasses.dataclass
class Source:
    id: int
    parent: int  # 0: an account's own folder
    name: str
    folder: bool
    owner: str
    data: bytes = b""
    created: int = 0
    modified: int = 0
    deleted: bool = False  # in the recycle bin


class FakeKodBox:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeKodBox:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.users: dict[str, User] = {}
            self.sources: dict[int, Source] = {}
            self.sessions: dict[str, str] = {}  # session -> user name
            self.tokens: dict[str, str] = {}  # access token -> session
            self.temp: dict[str, dict[str, Any]] = {}  # Uploader's temp files by key
            self.links: dict[str, int] = {}  # /storage/<key> -> source id
            self.ids = itertools.count(7)
            self.captcha = False
            self.utf8mb4 = False
            self.path_hidden = DEFAULT_HIDDEN
            self.options_list_hidden = True
            self.chunk_size = 512 * 1024
            self.max_page_num = 500
            self.upload_max: int | None = None
            self.body_max: int | None = None
            self.redirect_downloads = False
            self.space_full = False
            self.denied: set[str] = set()
            self.fail_status = 0
            self.name_filter: Callable[[str], str] | None = None
            self.expire_on_chunk: int | None = None
            self.slow_finish = 0.0
            self.kicked: set[str] = set()  # sessions KodBox signed out (kodUserLogoutTrigger)
            self.calls: list[dict[str, Any]] = []
            self.logins = 0
            self.downloads: list[dict[str, Any]] = []
            self.storage_requests: list[dict[str, Any]] = []

    # -- for tests

    def add_user(self, name: str, password: str) -> int:
        with self.lock:
            home = self._add(name, 0, name, folder=True)
            self.users[name] = User(name, password, home.id)
            return home.id

    def expire_sessions(self) -> None:
        """Every session ends (4 hours unused, or KodBox's cache was cleared); the tokens stay readable."""
        with self.lock:
            self.sessions.clear()

    def forget_tokens(self) -> None:
        """No token can be read any more (KodBox's system password changed)."""
        with self.lock:
            self.tokens.clear()

    def kick_sessions(self, user: str) -> None:
        """The user's sessions so far are signed out, as when the account logs in on more devices than it may."""
        with self.lock:
            self.kicked |= {session for session, name in self.sessions.items() if name == user}

    def find(self, user: str, path: str) -> Source | None:
        with self.lock:
            source = self.sources[self.users[user].home]
            for name in [part for part in path.split("/") if part]:
                source = self._child(source.id, name)
                if source is None:
                    return None
            return source

    def mkdirs(self, user: str, path: str) -> Source:
        with self.lock:
            source = self.sources[self.users[user].home]
            for name in [part for part in path.split("/") if part]:
                source = self._child(source.id, name) or self._add(user, source.id, name, folder=True)
            return source

    def put(self, user: str, path: str, data: bytes) -> Source:
        with self.lock:
            folder, _, name = path.rpartition("/")
            return self._add(user, self.mkdirs(user, folder).id, name, folder=False, data=data)

    def names(self, user: str, path: str) -> list[str]:
        with self.lock:
            return sorted(source.name for source in self._children(self.find(user, path).id))

    def routes(self, route: str) -> list[dict[str, str]]:
        """The parameters of the calls of `route`."""
        with self.lock:
            return [call["params"] for call in self.calls if call["route"] == route]

    # -- the tree

    def _add(self, owner: str, parent: int, name: str, *, folder: bool, data: bytes = b"") -> Source:
        moment = int(time.time())
        source = Source(next(self.ids), parent, name, folder, owner, data, moment, moment)
        self.sources[source.id] = source
        return source

    def _children(self, folder_id: int) -> list[Source]:
        return [source for source in self.sources.values() if source.parent == folder_id and not source.deleted]

    def _child(self, folder_id: int, name: str) -> Source | None:
        return next((source for source in self._children(folder_id) if source.name == name), None)

    def _below(self, source: Source) -> list[Source]:
        found = [source]
        for child in [s for s in self.sources.values() if s.parent == source.id]:
            found += self._below(child)
        return found

    @staticmethod
    def _path(source: Source) -> str:
        return f"{{source:{source.id}}}/"

    def _at(self, user: User, path: Any) -> Source | None:
        match = re.fullmatch(r"\{source:(\d+)\}/?", path) if isinstance(path, str) else None
        source = self.sources.get(int(match.group(1))) if match else None
        return source if source is not None and source.owner == user.name else None

    def _view(self, source: Source) -> dict[str, Any]:
        view = {"name": source.name, "path": self._path(source), "type": "folder" if source.folder else "file",
                "size": str(len(source.data)), "createTime": str(source.created), "modifyTime": str(source.modified),
                "targetType": "user", "targetID": "1", "sourceID": str(source.id), "parentID": str(source.parent),
                "isFolder": "1" if source.folder else "0",
                "createUser": {"userID": "1", "nickName": source.owner, "name": source.owner}}
        if not source.folder:
            view["ext"] = extension(source.name)
        return view

    def _hidden(self, user: User, source: Source) -> bool:
        if user.config.get("displayHideFile") == "1":
            return False
        entries = self.path_hidden.split(",")
        return (source.name[:1] in (".", "~") or source.name in entries
                or (not source.folder and extension(source.name) in [entry[2:] for entry in entries if entry.startswith("*.")]))

    def _allowed(self, name: str) -> str:
        """pathAllowCheck: the name as KodBox keeps it, or its refusal."""
        if not self.utf8mb4:
            name = "".join("-" if len(ch.encode()) >= 4 else ch for ch in name)
        checked = name.strip(" \t\n\r\0\x0b")
        if any(ch in checked for ch in NOT_ALLOWED):
            raise refuse("不支持的特殊字符:" + ",".join(NOT_ALLOWED))
        if len(checked.encode()) > 256:
            raise refuse("长度超出最大限制! (max=256)")
        return name

    # -- requests

    def handle(self, req: Request) -> Response:
        if req.path.startswith("/storage/"):
            return self._storage(req)
        if not req.path.endswith("/index.php"):
            raise HTTPError(404, b"<html>404 Not Found</html>")
        if self.body_max is not None and len(req.body) > self.body_max:
            return Response(413, b"<html>413 Request Entity Too Large</html>", {"Content-Type": "text/html"})
        pairs = urllib.parse.parse_qsl(req.raw_query, keep_blank_values=True)
        route = pairs[0][0] if pairs else ""
        params = dict(pairs[1:])
        content_type = req.header("Content-Type") or ""
        file = None
        if content_type.startswith("multipart/form-data"):
            fields, file = multipart(req.body, content_type)
            params.update(fields)
        elif req.body:
            params.update(urllib.parse.parse_qsl(req.body.decode("utf-8"), keep_blank_values=True))
        with self.lock:
            self.calls.append({"route": route, "params": params, "query": req.raw_query, "method": req.method,
                               "cookie": req.header("Cookie")})
            if self.fail_status:
                return Response(self.fail_status, b"<html>Bad Gateway</html>", {"Content-Type": "text/html"})
            if route == "user/index/loginSubmit":
                return self._login(params)
            if route == "user/index/logout":
                return self._logout(params)
            if route == UPLOAD and self.expire_on_chunk is not None and params.get("chunk") == str(self.expire_on_chunk):
                self.expire_on_chunk = None
                self.sessions.clear()
            user = self._user(params)
            if route in self.denied:
                return answer("您没有此权限，请联系管理员!", False, 1004)
            handler = {
                "user/view/options": self._options,
                "explorer/list/path": self._list,
                "explorer/index/mkdir": self._mkdir,
                "explorer/index/pathRename": self._rename,
                "explorer/index/pathDelete": self._delete,
                "explorer/index/pathCuteTo": lambda u, p: self._relocate(u, p, copy=False),
                "explorer/index/pathCopyTo": lambda u, p: self._relocate(u, p, copy=True),
                "explorer/index/fileOut": lambda u, p: self._file_out(u, p, req),
                UPLOAD: lambda u, p: self._upload(u, p, file),
            }.get(route)
            if handler is None:
                raise HTTPError(404, b"<html>404 Not Found</html>")
            response = handler(user, params)
        if getattr(response, "stored", False) and self.slow_finish:
            time.sleep(self.slow_finish)  # outside the lock, as a slow storage doesn't hold KodBox's other requests
        return response

    def _login(self, params: dict[str, str]) -> Response:
        self.logins += 1
        if self.captcha:
            return answer("验证码错误", False, CAPTCHA)
        user = self.users.get(params.get("name", "").strip())
        if user is None or user.password != params.get("password", "").strip():  # KodBox trims both
            return answer("用户名或密码错误!", False)
        if user.disabled:
            return answer("账号被禁用或尚未启用！请联系管理员", DISABLED)
        session, token = secrets.token_hex(8), "kod" + secrets.token_hex(24)
        self.sessions[session] = user.name
        self.tokens[token] = session
        return answer("ok", True, token)

    def _logout(self, params: dict[str, str]) -> Response:
        """user/index/logout: the token's session ends; "ok" also when there is none."""
        session = self.tokens.get(params.get("accessToken") or "")
        if session is not None:
            self.sessions.pop(session, None)
            self.kicked.discard(session)
        return answer("ok", True)

    def _user(self, params: dict[str, str]) -> User:
        token = params.get("accessToken")
        if token is None:
            raise refuse("您尚未登录!请先登录", SIGNED_OUT)
        if token == "" or len(token) > 500:
            raise refuse("token error!", False, SIGNED_OUT)
        session = self.tokens.get(token)
        if session is None:
            raise refuse("登录已失效，请重新登录!", SIGNED_OUT)
        name = self.sessions.get(session)
        if name is None:
            raise refuse("您尚未登录!请先登录", SIGNED_OUT)
        if session in self.kicked:  # authRole autoCheck: Session::destory(), then the logout trigger's message
            self.kicked.discard(session)
            del self.sessions[session]
            raise refuse("您账号已在其他设备登录,请稍后重试.", DISABLED)
        user = self.users[name]
        if user.disabled:  # userDataInit's logoutError: Session::destory() too
            del self.sessions[session]
            raise refuse("账号被禁用或尚未启用！请联系管理员", DISABLED)
        return user

    def _options(self, user: User, params: dict[str, str]) -> Response:
        options = {"pathHidden": self.path_hidden} if self.options_list_hidden else {}
        return answer({
            "kod": {"version": "1.71.04"},
            "user": {"userID": "1", "myhome": f"{{source:{user.home}}}/", "desktop": "", "isRoot": 0,
                     "info": {"userID": "1", "name": user.name}, "config": dict(user.config)},
            "system": {"settings": {"upload": {"chunkSize": self.chunk_size, "threads": 10}}, "all": {}, "options": options},
            "io": [], "lang": "zh-CN"})

    def _list(self, user: User, params: dict[str, str]) -> Response:
        folder = self._at(user, params.get("path"))
        if folder is None:
            return answer(GONE, False)
        if folder.deleted:
            return answer(IN_TRASH, False)
        children = sorted(self._children(folder.id), key=lambda s: (not s.folder, s.name)) if folder.folder else []
        page_num = min(max(int(params.get("pageNum") or 500), 5), self.max_page_num)
        total = len(children)
        page_total = math.ceil(total / page_num)
        page = min(max(int(params.get("page") or 1), 1), max(page_total, 1))
        shown = [s for s in children[(page - 1) * page_num:page * page_num] if not self._hidden(user, s)]
        folders = [self._view(s) for s in shown if s.folder]
        if folder.parent == 0 and page == 1:
            folders.append({"name": "私密保险箱", "path": "{block:safe}/", "type": "folder", "pathReadOnly": True})
        return answer({"pageInfo": {"totalNum": total, "pageNum": page_num, "page": page, "pageTotal": page_total},
                       "folderList": folders, "fileList": [self._view(s) for s in shown if not s.folder],
                       "current": self._view(folder), "thisPath": self._path(folder)})

    def _mkdir(self, user: User, params: dict[str, str]) -> Response:
        match = re.fullmatch(r"(\{source:\d+\})/(.*)", params.get("path", ""), re.S)
        name = self._allowed(match.group(2) if match else "")
        parent = self._at(user, match.group(1)) if match else None
        if parent is None or not parent.folder:
            return answer(GONE, False)
        if parent.deleted:
            return answer(IN_TRASH, False)
        if self.space_full:
            return answer("剩余空间不足,请联系管理员!", False)
        existing = self._child(parent.id, name)
        if existing is not None:  # a folder is kept (skip); for a file KodBox answers the folder it is in
            return answer("操作成功！", True, self._path(existing if existing.folder else parent))
        return answer("操作成功！", True, self._path(self._add(user.name, parent.id, name, folder=True)))

    def _rename(self, user: User, params: dict[str, str]) -> Response:
        name = self._allowed(params.get("newName", ""))
        source = self._at(user, params.get("path"))
        if source is None or source.deleted:
            return answer(GONE, False)
        if any(other.name == name and other.id != source.id for other in self._children(source.parent)):
            return answer("该名称已存在！", False)
        source.name, source.modified = name, int(time.time())
        return answer("操作成功！", True, self._path(source))

    def _delete(self, user: User, params: dict[str, str]) -> Response:
        trash = user.config.get("recycleOpen") == "1" and params.get("shiftDelete") != "1"
        for item in json.loads(params.get("dataArr") or "[]"):
            source = self._at(user, item.get("path"))
            if source is None or source.deleted:
                return answer(GONE, False)
            if source.parent == 0:
                return answer("No permission to delete this path: " + item["path"], False)
            if trash:
                source.deleted = True
            else:
                for gone in self._below(source):
                    del self.sources[gone.id]
        return answer("删除成功", True)

    def _copy_tree(self, source: Source, parent: int) -> Source:
        made = self._add(source.owner, parent, source.name, folder=source.folder, data=source.data)
        for child in self._children(source.id):
            self._copy_tree(child, made.id)
        return made

    def _relocate(self, user: User, params: dict[str, str], *, copy: bool) -> Response:
        items = json.loads(params.get("dataArr") or "[]")
        target = self._at(user, params.get("path"))
        if target is None or not target.folder or target.deleted:
            return answer(GONE, False)
        repeat = params.get("fileRepeat") or user.config["fileRepeat"]
        results, listed = [], []
        for item in items:
            source = self._at(user, item.get("path"))
            if source is None or source.deleted or any(s.id == target.id for s in self._below(source)):
                continue  # this item fails
            existing = self._child(target.id, source.name)
            if existing is not None and existing.id != source.id:
                if repeat == "skip":
                    if existing.folder:
                        results.append(self._path(existing))  # a folder of the name is taken as the target
                        listed.append({"path": self._path(existing), "name": existing.name, "type": "folder"})
                    continue
                existing.deleted = True  # replace
            if copy:
                made = self._copy_tree(source, target.id)
            else:
                source.parent, made = target.id, source
            results.append(self._path(made))
            listed.append({"path": self._path(made), "name": made.name, "type": "folder" if made.folder else "file"})
        if not results:
            return answer("操作失败！", False)
        return answer("粘贴成功" if copy else "移动成功", True, results,
                      {"hasExistAll": False, "pathTo": params.get("path"), "listFrom": items, "listTo": listed})

    def _upload(self, user: User, params: dict[str, str], file: bytes | None) -> Response:
        if file is None:
            return answer("上传失败", False)
        if self.upload_max is not None and len(file) > self.upload_max:
            return answer("1:UPLOAD_ERR_INI_SIZE", False)
        folder = self._at(user, params.get("path"))
        if folder is None or not folder.folder or folder.deleted:
            return answer("目标目录不存在！", False)
        name = params.get("name") or "blob"
        name = "".join("_" if ch in REPLACED else ch for ch in name)
        if not self.utf8mb4:
            name = "".join("-" if len(ch.encode()) >= 4 else ch for ch in name)
        if self.name_filter is not None:
            name = self.name_filter(name)
        size, chunk = int(params.get("size") or 0), int(params.get("chunk") or 0)
        chunks, chunk_size = int(params.get("chunks") or 1), int(params.get("chunkSize") or 0)
        if chunks > 1 and chunk_size <= 0:
            return answer("chunkSize error!", False)
        if chunk_size > size:
            chunks = 1
        if chunks <= 1:
            data = file
        else:
            simple = params.get("checkHashSimple", "")
            named = simple if len(simple) >= 30 else hashlib.md5(f"{user.name}{params.get('path')}{name}{size}".encode()).hexdigest()
            key = hashlib.md5(f"{named}{chunk_size}".encode()).hexdigest()
            temp = self.temp.setdefault(key, {"data": bytearray(size), "parts": {}})
            offset = chunk * chunk_size
            temp["data"][offset:offset + len(file)] = file
            temp["parts"][chunk] = len(file)
            if len(temp["parts"]) != chunks:
                return answer(f"chunk_success_{chunk}", True)
            data = bytes(self.temp.pop(key)["data"])
            if simple and hash_simple(data) != simple:
                return answer("上传失败[chunk hash error!]", False)
        if size and size != len(data):
            return answer("上传失败[temp size error]", False)
        if self.space_full:
            return answer("剩余空间不足,请联系管理员!", False)
        existing = self._child(folder.id, name)
        if existing is not None and existing.folder:
            return answer("上传失败", False)
        repeat = params.get("fileRepeat") or user.config["fileRepeat"]
        if existing is not None and repeat == "replace":  # the same source, its new version
            existing.data, existing.modified = data, int(time.time())
            made = existing
        else:
            made = self._add(user.name, folder.id, name, folder=False, data=data)
        info: Any = self._path(made)
        if params.get("fileInfo") == "1":
            info = {"ext": extension(made.name), "name": made.name, "createTime": str(made.created),
                    "size": str(len(made.data)), "path": self._path(made), "pathDisplay": "/" + made.name,
                    "downloadPath": f"{self.url}/index.php?explorer/share/file&hash={secrets.token_hex(20)}"}
        response = answer("上传成功", True, info)
        response.stored = True  # type: ignore[attr-defined]  # see slow_finish
        return response

    def _file_out(self, user: User, params: dict[str, str], req: Request) -> Response:
        source = self._at(user, params.get("path"))
        self.downloads.append({"method": req.method, "query": req.raw_query, "range": req.header("Range"),
                               "download": params.get("download")})
        if source is None or source.deleted or source.folder:
            return answer(GONE, False)
        if self.redirect_downloads:
            key = secrets.token_hex(12)
            self.links[key] = source.id
            return Response(302, b"", {"Location": f"/storage/{key}?name={urllib.parse.quote(source.name)}"})
        headers = {"Content-Type": "application/json" if source.name.endswith(".json") else "application/octet-stream"}
        if params.get("download") == "1":
            headers["Content-Disposition"] = "attachment; filename*=UTF-8''" + urllib.parse.quote(source.name)
        return file_response(source.data, req.header("Range"), headers)

    def _storage(self, req: Request) -> Response:
        with self.lock:
            self.storage_requests.append({"method": req.method, "query": req.raw_query, "body": req.body,
                                          "cookie": req.header("Cookie"), "range": req.header("Range")})
            source = self.sources.get(self.links.get(req.path.split("/")[2], -1))
            if source is None:
                raise HTTPError(404, b"NoSuchKey")
            data = source.data
        return file_response(data, req.header("Range"))
