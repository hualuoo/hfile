"""An in-memory fake of FebBox's open platform API (apps.storages.drivers.febbox), on FakeServer.

    fake = FakeFebBox().start()
    user = fake.add_user()                          # a FebBox account
    client_id, client_secret = fake.add_key(user)   # a key it generated on the Secret Key page
    fake.url
    fake.close()

It answers as FebBox's documentation (https://www.febbox.com/open/docs) says; where that is silent, as OpenList's FebBox
driver shows the real service behaves ([OpenList]), or as assumed here ([assumed]):

- POST /oauth/token with an application/x-www-form-urlencoded body: grant_type client_credentials, client_id and client_secret give
  {"code": 1, "msg": "success", "data": {access_token, expires_in, token_type "Bearer", scope null, refresh_token}},
  a token for the account that generated the key. Other grants, or a wrong key: {"code": -1, "msg": ...} [assumed].
- POST /oauth with "Authorization: Bearer <token>" and a multipart/form-data body whose `module` names the call
  (another body has no fields here, so no module). Answers are {"code", "msg", "data"}, code 1 for success. A missing, unknown or expired token is
  {"code": -10001, "msg": "invalid token"} with an error status [OpenList] (401 [assumed]). With `per_minute` set,
  an account's calls beyond that many a minute are {"code": -429, "msg": "too many requests, max 120 per minute"}
  (HTTP 200 [assumed]). A module it doesn't know is code -1 [assumed].
- file_list: parent_id (0: the root), page from 1, pagelimit 1 to 500 and one of the documented orders (else code -1
  [assumed]); entries fid, file_name, is_dir, file_size, ext, parent_id, update_time, add_time, is_share; total, page,
  pagelimit, has_more. The root also lists the folders the account subscribed to (is_share 1, from_uid, share_fid,
  fid_org). A parent_id that isn't one of the account's folders lists nothing [assumed].
- create_dir (the module's name [OpenList]), file_rename, file_move (`to` [OpenList]), file_copy (a folder with
  everything in it, under new fids) and file_delete answer {"code": 1, "msg": "success"} [assumed: undocumented]. A
  folder may hold two of a name [assumed]. A fid that isn't one of the account's, an empty name, a target that isn't
  a folder, or a folder moved or copied into itself: code -1 [assumed].
- file_get_play_url_user: one file's original.download_url, a link of this server that needs no token, takes Range
  (206) and expires after `link_ttl` seconds (FebBox's: 14400), then answers 403 [assumed]. What the links send is
  counted against the account's traffic (set_traffic; none set: no limit); once that is used up the module answers
  {"code": -1, "msg": "Traffic exceeded"}.

Knobs: expire_tokens(), revoke(client_id), set_traffic(user, bytes), token_ttl, per_minute, throttle (the next that
many calls answer -429), html_status (calls answer a Cloudflare page with that status: every call, or the next
html_calls of them), bad_token_status (the HTTP status of -10001 answers), no_has_more (listings without has_more and total), link_ttl. Records: calls (of /oauth and
/oauth/token), downloads (of the links).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, Request, Response, file_response, json_response

ORDERS = ("size_asc", "size_desc", "name_asc", "name_desc", "update_asc", "update_desc", "ext_asc", "ext_desc")
CLOUDFLARE = b"<!DOCTYPE html><html><head><title>Just a moment...</title></head><body>Checking your browser</body></html>"


@dataclasses.dataclass
class Item:
    fid: int
    user: int
    parent: int  # 0: the account's root
    name: str
    folder: bool
    data: bytes = b""
    added: int = dataclasses.field(default_factory=lambda: int(time.time()))
    updated: int = dataclasses.field(default_factory=lambda: int(time.time()))
    deleted: bool = False
    share_owner: int | None = None  # a folder subscribed from another account's share: its owner's uid
    share_fid: int | None = None  # and the share's fid on the owner's disk


@dataclasses.dataclass
class Link:
    fid: int
    user: int
    expires: float


def multipart(body: bytes, content_type: str) -> dict[str, list[str]]:
    """{name: [values]} of a multipart/form-data body of text fields; {} without a boundary."""
    boundary = re.search(r'boundary="?([^";]+)"?', content_type)
    if not boundary:
        return {}
    form: dict[str, list[str]] = {}
    for part in body.split(b"--" + boundary.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, value = part.removeprefix(b"\r\n").partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]*)"', head)
        if name:
            form.setdefault(name.group(1).decode(), []).append(value.removesuffix(b"\r\n").decode("utf-8"))
    return form


def failure(message: str, code: int = -1) -> Response:
    return json_response({"code": code, "msg": message})


def success(data: Any = None) -> Response:
    body: dict[str, Any] = {"code": 1, "msg": "success"}
    if data is not None:
        body["data"] = data
    return json_response(body)


class FakeFebBox:
    def __init__(self):
        self.server = FakeServer(self.handle)
        self.lock = threading.RLock()
        self.reset()

    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> FakeFebBox:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def reset(self) -> None:
        with self.lock:
            self.keys: dict[str, tuple[str, int]] = {}  # client_id -> (client_secret, user)
            self.tokens: dict[str, tuple[str, int, float]] = {}  # access token -> (client_id, user, expiry)
            self.items: dict[int, Item] = {}
            self.links: dict[str, Link] = {}
            self.fids = itertools.count(1001)
            self.user_ids = itertools.count(50001)
            self.token_ttl = 3600
            self.per_minute: int | None = None
            self.recent: dict[int, list[float]] = {}  # user -> times of the calls in the last minute
            self.throttle = 0
            self.html_status = 0
            self.html_calls: int | None = None
            self.bad_token_status = 401
            self.no_has_more = False
            self.link_ttl = 14400.0
            self.traffic: dict[int, int] = {}  # user -> bytes of traffic left (none: no limit)
            self.calls: list[dict[str, Any]] = []
            self.downloads: list[dict[str, Any]] = []

    # -- for tests

    def add_user(self) -> int:
        with self.lock:
            return next(self.user_ids)

    def add_key(self, user: int) -> tuple[str, str]:
        """A key the account generated on the Secret Key page: (client_id, client_secret)."""
        with self.lock:
            client_id, secret = secrets.token_hex(16), secrets.token_hex(16)
            self.keys[client_id] = (secret, user)
            return client_id, secret

    def revoke(self, client_id: str) -> None:
        """The key is deleted on the Secret Key page: its tokens stop working too."""
        with self.lock:
            self.keys.pop(client_id)
            self.tokens = {token: owner for token, owner in self.tokens.items() if owner[0] != client_id}

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def set_traffic(self, user: int, left: int | None) -> None:
        with self.lock:
            if left is None:
                self.traffic.pop(user, None)
            else:
                self.traffic[user] = left

    def find(self, user: int, path: str) -> Item | None:
        """The entry at `path` of the account's own files; None when there is none (and for "/")."""
        with self.lock:
            item, parent = None, 0
            for name in [part for part in path.split("/") if part]:
                item = self._child(user, parent, name)
                if item is None:
                    return None
                parent = item.fid
            return item

    def mkdirs(self, user: int, path: str) -> int:
        """The fid of the folder at `path` (0: the root), made with those above it, as FebBox's apps would."""
        with self.lock:
            parent = 0
            for name in [part for part in path.split("/") if part]:
                item = self._child(user, parent, name)
                if item is None or not item.folder:
                    item = self._add(user, parent, name, folder=True)
                parent = item.fid
            return parent

    def put(self, user: int, path: str, data: bytes, *, name: str | None = None) -> Item:
        """A file at `path`, or named `name` (any name, "/" too) in the folder `path`, as FebBox's apps would."""
        with self.lock:
            folder, _, last = path.rpartition("/") if name is None else (path, "", name)
            return self._add(user, self.mkdirs(user, folder), last, folder=False, data=data)

    def upload(self, user: int, parent: int, name: str, data: bytes) -> Item:
        """A new file in the folder `parent`, next to any of the same name, as FebBox's apps would make it."""
        with self.lock:
            return self._add(user, parent, name, folder=False, data=data)

    def add_share(self, user: int, name: str) -> Item:
        """A folder of another account's share that `user` subscribed to: it is listed in the root (is_share 1)."""
        with self.lock:
            owner = next(self.user_ids)
            shared = self._add(owner, 0, name, folder=True)
            self._add(owner, shared.fid, "inside.mkv", folder=False, data=b"m")
            shortcut = self._add(user, 0, name, folder=True)
            shortcut.share_owner, shortcut.share_fid = owner, shared.fid
            return shortcut

    def names(self, user: int, path: str) -> list[str]:
        with self.lock:
            folder = self.find(user, path) if path != "/" else None
            return sorted(item.name for item in self._children(user, folder.fid if folder else 0))

    def requests(self, module: str) -> list[dict[str, Any]]:
        """The calls of /oauth with this module, as forms (name -> list of values)."""
        with self.lock:
            return [call["form"] for call in self.calls
                    if call["path"] == "/oauth" and call["form"].get("module") == [module]]

    # -- the tree

    def _add(self, user: int, parent: int, name: str, *, folder: bool, data: bytes = b"") -> Item:
        item = Item(next(self.fids), user, parent, name, folder, data)
        self.items[item.fid] = item
        return item

    def _children(self, user: int, parent: int) -> list[Item]:
        return [item for item in self.items.values() if item.user == user and item.parent == parent and not item.deleted]

    def _child(self, user: int, parent: int, name: str) -> Item | None:
        """The newest of the name, as a path finds it."""
        found = [item for item in self._children(user, parent) if item.name == name and item.share_owner is None]
        return max(found, key=lambda item: (item.folder, item.updated, item.fid)) if found else None

    def _below(self, item: Item) -> list[Item]:
        found = [item]
        if item.share_owner is None:
            for child in self._children(item.user, item.fid):
                found += self._below(child)
        return found

    def _own(self, user: int, value: Any, *, folder: bool | None = None) -> Item | None:
        """The account's own entry whose fid `value` (a form's text) is; None when it isn't one (or not a folder, or
        not a file, when `folder` says)."""
        fid = int(value) if isinstance(value, str) and value.isascii() and value.isdigit() else None
        item = self.items.get(fid) if fid is not None else None
        if item is None or item.user != user or item.deleted or item.share_owner is not None:
            return None
        if folder is not None and item.folder != folder:
            return None
        return item

    def _folder(self, user: int, value: Any) -> int | None:
        """The fid of a target folder (0: the root), or None when `value` isn't one of the account's folders."""
        if value == "0":
            return 0
        item = self._own(user, value, folder=True)
        return item.fid if item is not None else None

    def _inside(self, folder: int, root: Item) -> bool:
        """Whether the folder `folder` is `root` or below it."""
        current = self.items.get(folder)
        while current is not None:
            if current.fid == root.fid:
                return True
            current = self.items.get(current.parent)
        return False

    def _entry(self, item: Item) -> dict[str, Any]:
        entry = {"fid": item.fid, "file_name": item.name, "is_dir": 1 if item.folder else 0,
                 "file_size": 0 if item.folder else len(item.data), "ext": item.name.rpartition(".")[2] if "." in item.name else "",
                 "parent_id": item.parent, "update_time": item.updated, "add_time": item.added, "is_share": 0}
        if item.share_owner is not None:
            entry.update({"is_share": 1, "from_uid": item.share_owner, "share_fid": item.share_fid,
                          "fid_org": item.share_fid})
        return entry

    # -- requests

    def handle(self, req: Request) -> Response:
        path = urllib.parse.unquote(req.path)
        with self.lock:
            if path.startswith("/dl/"):
                return self._download(req, path[4:])
            content_type = req.header("Content-Type") or ""
            if path == "/oauth/token":
                form = urllib.parse.parse_qs(req.body.decode("utf-8"), keep_blank_values=True) \
                    if content_type.startswith("application/x-www-form-urlencoded") else {}
            else:
                form = multipart(req.body, content_type) if content_type.startswith("multipart/form-data") else {}
            self.calls.append({"method": req.method, "path": path, "target": req.target, "form": form,
                               "authorization": req.header("Authorization"), "content_type": req.header("Content-Type")})
            if self.html_status and self.html_calls != 0:  # Cloudflare in front of it
                if self.html_calls is not None:
                    self.html_calls -= 1
                return Response(self.html_status, CLOUDFLARE, {"Content-Type": "text/html; charset=UTF-8"})
            if req.method != "POST" or path not in ("/oauth", "/oauth/token"):
                return Response(404, b"<html>404 Not Found</html>", {"Content-Type": "text/html"})
            fields = {name: values[0] for name, values in form.items()}
            if path == "/oauth/token":
                return self._token(fields)
            user = self._user(req)
            if user is None:
                return json_response({"code": -10001, "msg": "invalid token", "server_runtime": 0.001,
                                      "server_name": "fake"}, self.bad_token_status)
            if self.throttle:
                self.throttle -= 1
                return failure("too many requests, max 120 per minute", -429)
            if self.per_minute is not None:
                now = time.monotonic()
                recent = [moment for moment in self.recent.get(user, []) if now - moment < 60]
                if len(recent) >= self.per_minute:
                    return failure("too many requests, max 120 per minute", -429)
                self.recent[user] = recent + [now]
            handler = getattr(self, "_module_" + fields.get("module", ""), None)
            if handler is None:
                return failure("module not found")
            return handler(user, fields, form, req)

    def _token(self, fields: dict[str, str]) -> Response:
        if fields.get("grant_type") != "client_credentials":
            return failure("unsupported grant type")
        client_id = fields.get("client_id", "")
        key = self.keys.get(client_id)
        if key is None or key[0] != fields.get("client_secret"):
            return failure("invalid client")
        token = secrets.token_hex(64)
        self.tokens[token] = (client_id, key[1], time.monotonic() + self.token_ttl)
        return success({"access_token": token, "expires_in": self.token_ttl, "token_type": "Bearer", "scope": None,
                        "refresh_token": secrets.token_hex(64)})

    def _user(self, req: Request) -> int | None:
        token = req.bearer()
        owner = self.tokens.get(token) if token else None
        if owner is None or time.monotonic() >= owner[2]:
            return None
        return owner[1]

    # -- modules

    def _module_file_list(self, user: int, fields: dict[str, str], form, req) -> Response:
        page, limit, order = fields.get("page", ""), fields.get("pagelimit", ""), fields.get("order")
        if not (page.isascii() and page.isdigit() and int(page) >= 1 and limit.isascii() and limit.isdigit()
                and 1 <= int(limit) <= 500 and order in ORDERS):
            return failure("parameter error")
        parent = self._folder(user, fields.get("parent_id"))
        children = self._children(user, parent) if parent is not None else []
        field, _, direction = order.rpartition("_")
        keys = {"size": lambda item: (len(item.data), item.fid), "name": lambda item: (item.name, item.fid),
                "update": lambda item: (item.updated, item.fid),
                "ext": lambda item: (item.name.rpartition(".")[2], item.fid)}
        children.sort(key=keys[field], reverse=direction == "desc")
        start = (int(page) - 1) * int(limit)
        data: dict[str, Any] = {"file_list": [self._entry(item) for item in children[start:start + int(limit)]],
                                "show_type": "list"}
        if not self.no_has_more:
            data.update({"total": len(children), "page": int(page), "pagelimit": int(limit),
                         "has_more": 1 if start + int(limit) < len(children) else 0})
        return success(data)

    def _module_create_dir(self, user: int, fields: dict[str, str], form, req) -> Response:
        parent = self._folder(user, fields.get("parent_id"))
        name = fields.get("name", "")
        if parent is None or not name:
            return failure("parameter error")
        self._add(user, parent, name, folder=True)
        return success()

    def _module_file_rename(self, user: int, fields: dict[str, str], form, req) -> Response:
        item = self._own(user, fields.get("fid"))
        name = fields.get("name", "")
        if item is None or not name:
            return failure("file not exist")
        item.name, item.updated = name, int(time.time())
        return success()

    def _targets(self, user: int, form: dict[str, list[str]]) -> list[Item] | None:
        items = [self._own(user, value) for value in form.get("fids[]", [])]
        return items if items and all(item is not None for item in items) else None

    def _module_file_move(self, user: int, fields: dict[str, str], form, req) -> Response:
        items, target = self._targets(user, form), self._folder(user, fields.get("to"))
        if items is None or target is None:
            return failure("file not exist")
        if any(item.folder and self._inside(target, item) for item in items):
            return failure("cannot move a folder into itself")
        for item in items:
            item.parent = target
        return success()

    def _module_file_copy(self, user: int, fields: dict[str, str], form, req) -> Response:
        items, target = self._targets(user, form), self._folder(user, fields.get("to"))
        if items is None or target is None:
            return failure("file not exist")
        if any(item.folder and self._inside(target, item) for item in items):
            return failure("cannot copy a folder into itself")
        # The trees as they were before anything is added.
        trees = {item.fid: self._children(user, item.fid) for top in items for item in self._below(top) if item.folder}

        def clone(item: Item, parent: int) -> None:
            made = self._add(user, parent, item.name, folder=item.folder, data=item.data)
            for child in trees.get(item.fid, []):
                clone(child, made.fid)

        for item in items:
            clone(item, target)
        return success()

    def _module_file_delete(self, user: int, fields: dict[str, str], form, req) -> Response:
        items = self._targets(user, form)
        if items is None:
            return failure("file not exist")
        for item in items:
            for below in self._below(item):
                below.deleted = True
        return success()

    def _module_file_get_play_url_user(self, user: int, fields: dict[str, str], form, req) -> Response:
        item = self._own(user, fields.get("fid"), folder=False)
        if item is None:
            return failure("file not exist")
        if user in self.traffic and self.traffic[user] <= 0:
            return failure("Traffic exceeded")
        token = secrets.token_urlsafe(24)
        self.links[token] = Link(item.fid, user, time.monotonic() + self.link_ttl)
        original = {"download_url": f"{req.server_url}/dl/{token}", "accept_ranges": 1}
        return success({"fid": item.fid, "traffic_bill": "user", "expires_in": int(self.link_ttl), "original": original,
                        "hls_quality_list": []})

    # -- the links

    def _download(self, req: Request, token: str) -> Response:
        self.downloads.append({"method": req.method, "token": token, "range": req.header("Range"),
                               "authorization": req.header("Authorization"), "cookie": req.header("Cookie"),
                               "accept_encoding": req.header("Accept-Encoding")})
        link = self.links.get(token)
        if req.method != "GET" or link is None or time.monotonic() >= link.expires:
            return Response(403, b"<html>403 Forbidden</html>", {"Content-Type": "text/html"})
        item = self.items.get(link.fid)
        if item is None or item.deleted:
            return Response(404, b"<html>404 Not Found</html>", {"Content-Type": "text/html"})
        response = file_response(item.data, req.header("Range"))
        if link.user in self.traffic:
            self.traffic[link.user] -= response.length or 0
        return response
