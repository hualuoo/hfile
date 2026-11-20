"""An in-memory fake of the Dropbox API v2 and its OAuth endpoints, for the driver's tests (no Django).

Two servers, as Dropbox has two hosts: `api` (api.dropboxapi.com: OAuth, RPC routes with JSON bodies) and
`content` (content.dropboxapi.com: downloads and uploads, their argument in the Dropbox-API-Arg header; and the
temporary links). A route asked of the wrong host is a 404. It follows Dropbox's documented behaviour where the
driver depends on it:

- OAuth: GET /oauth2/authorize consents at once (a 302 back to the redirect URI with a code and the state; the
  code gives a refresh token only with token_access_type=offline); POST /oauth2/token takes form fields, codes
  work once and with their redirect URI, refresh answers carry no new refresh token; errors as Dropbox gives
  them ({"error": "invalid_client: ..."} as answered on 2026-11-17, invalid_grant with an error_description);
- bearer tokens that expire (401 expired_access_token) or are unknown (401 invalid_access_token);
- headers must be ASCII (0x20-0x7E): a request with any other character in a header is refused (400); the
  Dropbox-API-Arg must be a JSON object; RPC bodies JSON; upload bodies application/octet-stream;
- paths: "" for the root, other paths starting with "/", not ending with "/" or whitespace (malformed_path);
  names ignore case and keep it; writes make the missing folders above them (a file in the way:
  conflict/file_ancestor); get_metadata doesn't take the root;
- list_folder with cursors (pages of `page_size` at most), list_folder/continue (an unknown cursor: reset);
- upload sessions: start, append_v2 at the session's offset (else incorrect_offset with correct_offset),
  finish, sessions that expire (expire_sessions());
- create_folder_v2, delete_v2, move_v2 and copy_v2 (from_lookup/not_found, to/conflict/..., a folder into
  itself), with autorename false only;
- search_v2 and search/continue_v2: each word of the query must start a word of the name (Dropbox matches
  words, not any part of a name), below options.path, max_results a page;
- get_temporary_link: a link on the content host that needs no token (410 Gone after four hours), refused
  (email_not_verified) for an account whose email isn't verified; get_temporary_upload_link: a one-time link
  that takes a POST with Content-Type application/octet-stream only;
- team spaces (team_space=True): the account's root namespace is a team root holding the member's home folder
  as /Member; calls without Dropbox-API-Path-Root are rooted at the home folder, with {".tag": "root", ...} at
  the root it names, which must be the account's (else 422 invalid_root; change_root() changes it);
- errors as {"error_summary": "path/not_found/..", "error": {...}}, with a random number of dots.

Knobs: page_size; throttle (the next N calls answer 429 with Retry-After `throttle_after`, reason
`throttle_reason`); fail_next (errors for the next calls: (status, tags)); lose_appends and lose_uploads (the next N
appends, or /files/upload calls, are stored, then the connection drops without an answer); stuck_cursor
(list_folder pages give one cursor again); email_verified. `requests` records every request; token_requests() the token endpoint's.
"""

from __future__ import annotations

import datetime
import hashlib
import itertools
import json
import random
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from apps.storages.tests.fake_cloud_http import (
    FakeServer,
    HTTPError,
    Request,
    Response,
    file_response,
    json_response,
)

HOME_NS = "1001"
TEAM_NS = "2002"
LINK_LIFETIME = 4 * 3600  # seconds a temporary link works
BLOCK = 4 * 1024 * 1024  # content_hash blocks


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


def _content_hash(data: bytes) -> str:
    """Dropbox's content_hash: SHA-256 of the SHA-256 of each 4 MiB block."""
    digests = b"".join(hashlib.sha256(data[i:i + BLOCK]).digest() for i in range(0, len(data), BLOCK))
    return hashlib.sha256(digests).hexdigest()


def api_error(status: int, *tags: str, extra: dict[str, Any] | None = None, user_message: str | None = None) -> HTTPError:
    """An error answer as Dropbox gives it: the union tags nested in "error", their path in error_summary,
    followed by a random number of dots (Dropbox's way of keeping clients from matching it exactly)."""
    error: dict[str, Any] = {}
    node = error
    for i, tag in enumerate(tags):
        node[".tag"] = tag
        if i + 1 < len(tags):
            node[tag] = {}
            node = node[tag]
    node.update(extra or {})
    body: dict[str, Any] = {"error_summary": "/".join(tags) + "/" + "." * random.randint(0, 3), "error": error}
    if user_message:
        body["user_message"] = {"locale": "en", "text": user_message}
    return HTTPError(status, body)


def _bad_request(route: str, message: str) -> HTTPError:
    """Dropbox's answer to a malformed call: 400, plain text."""
    return HTTPError(400, f'Error in call to API function "{route}": {message}'.encode(), {"Content-Type": "text/plain; charset=utf-8"})


class Node:
    _ids = itertools.count(1)

    def __init__(self, name: str, folder: bool, data: bytes = b"", *, downloadable: bool = True):
        self.id = f"id:FAKE{next(self._ids):08d}"
        self.name = name
        self.folder = folder
        self.data = data
        self.downloadable = downloadable
        self.children: dict[str, Node] = {}  # by name.lower(): Dropbox ignores case
        self.parent: Node | None = None
        self.touch()

    def touch(self) -> None:
        self.modified = _now()
        self.rev = secrets.token_hex(8)

    def add(self, child: Node) -> Node:
        child.parent = self
        self.children[child.name.lower()] = child
        return child

    def detach(self) -> None:
        if self.parent is not None:
            self.parent.children.pop(self.name.lower(), None)
            self.parent = None

    def path_from(self, root: Node) -> str:
        names = []
        node = self
        while node is not root and node is not None:
            names.append(node.name)
            node = node.parent
        return "/" + "/".join(reversed(names))

    def walk(self):
        yield self
        for child in list(self.children.values()):
            yield from child.walk()

    def clone(self, name: str) -> Node:
        copy = Node(name, self.folder, self.data, downloadable=self.downloadable)
        for child in self.children.values():
            copy.add(child.clone(child.name))
        return copy


class FakeDropbox:
    def __init__(self, *, team_space: bool = False, app_key: str = "fake-app-key", app_secret: str = "fake-app-secret"):
        self.lock = threading.RLock()
        self.app_key = app_key
        self.app_secret = app_secret
        self.home = Node("", True)
        self.team: Node | None = None
        self.home_ns = HOME_NS
        self.root_ns = HOME_NS
        if team_space:
            self.team = Node("", True)
            self.home.name = "Member"
            self.team.add(self.home)
            self.team.add(Node("Team Docs", True))
            self.root_ns = TEAM_NS
        self.token_lifetime = 14400
        self.access_tokens: dict[str, float] = {}  # token -> time.time() it expires
        self.refresh_tokens: set[str] = set()
        self.codes: dict[str, tuple[str, bool]] = {}  # code -> (redirect_uri, offline)
        self.cursors: dict[str, tuple[Node, Node, int, int]] = {}  # list_folder: (root, folder, offset, page size)
        self.search_cursors: dict[str, tuple[list[dict[str, Any]], int, int]] = {}
        self.sessions: dict[str, bytearray] = {}
        self.links: dict[str, tuple[Node, float]] = {}
        self.upload_links: dict[str, dict[str, Any]] = {}
        # knobs
        self.page_size = 1000
        self.throttle = 0
        self.throttle_after = "0"
        self.throttle_reason = "too_many_requests"
        self.fail_next: list[tuple[Any, ...]] = []  # (status, tags[, extra[, route]]): errors for the next calls
        self.lose_appends = 0
        self.lose_uploads = 0
        self.stuck_cursor = False
        self.email_verified = True
        self.requests: list[dict[str, Any]] = []
        self.api = FakeServer(self._handle_api).start()
        self.content = FakeServer(self._handle_content).start()
        self.api_url = self.api.url
        self.content_url = self.content.url

    def close(self) -> None:
        self.api.close()
        self.content.close()

    # -- test helpers

    def root(self) -> Node:
        """The account's root folder: the team space's, or the home folder."""
        return self.team if self.team is not None else self.home

    def issue_refresh_token(self) -> str:
        with self.lock:
            token = "rt-" + secrets.token_urlsafe(12)
            self.refresh_tokens.add(token)
            return token

    def grant_code(self, redirect_uri: str, *, offline: bool = True) -> str:
        """What the authorize page sends to the callback after the user allowed access."""
        with self.lock:
            code = "code-" + secrets.token_urlsafe(12)
            self.codes[code] = (redirect_uri, offline)
            return code

    def expire_access_tokens(self) -> None:
        with self.lock:
            for token in self.access_tokens:
                self.access_tokens[token] = 0.0

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()

    def change_root(self) -> None:
        """The account's root namespace changes (it joined another team space)."""
        with self.lock:
            self.root_ns = str(int(self.root_ns) + 1000)

    def find(self, path: str, root: Node | None = None) -> Node | None:
        node = root or self.root()
        for part in [p for p in path.split("/") if p]:
            node = node.children.get(part.lower()) if node.folder else None
            if node is None:
                return None
        return node

    def put(self, path: str, data: bytes = b"", *, folder: bool = False, downloadable: bool = True,
            root: Node | None = None) -> Node:
        """A file or folder made outside the driver (and the folders above it)."""
        with self.lock:
            parent = root or self.root()
            *folders, name = [p for p in path.split("/") if p]
            for part in folders:
                parent = parent.children.get(part.lower()) or parent.add(Node(part, True))
            return parent.add(Node(name, folder, data, downloadable=downloadable))

    def token_requests(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == "/oauth2/token"]

    def calls(self, route: str) -> list[dict[str, Any]]:
        return [r for r in self.requests if r["path"] == route]

    # -- dispatch

    def _log(self, host: str, req: Request) -> None:
        try:
            rpc = json.loads(req.body) if host == "api" and req.path.startswith("/2/") and req.body else None
        except ValueError:
            rpc = None
        with self.lock:
            self.requests.append({
                "host": host, "method": req.method, "path": req.path, "auth": req.header("Authorization") is not None,
                "arg": req.header("Dropbox-API-Arg"), "rpc": rpc, "path_root": req.header("Dropbox-API-Path-Root"),
                "range": req.header("Range"), "content_type": req.header("Content-Type"), "size": len(req.body),
            })

    @staticmethod
    def _check_request(req: Request) -> None:
        """Headers are ASCII: Dropbox asks for \\uXXXX escapes in its JSON headers (JSON encoding page). And the
        body is complete: a client that stopped halfway (its data ended early) stores nothing."""
        for name, value in req.headers.items():
            if any(not (0x20 <= ord(ch) < 0x7F) and ch != "\t" for ch in value):
                raise HTTPError(400, f"The {name} header has characters that aren't HTTP header safe.".encode(),
                                {"Content-Type": "text/plain; charset=utf-8"})
        length = req.header("Content-Length")
        if length is not None and length.isdigit() and int(length) != len(req.body):
            raise HTTPError(400, b"The request body is incomplete.")

    def _handle_api(self, req: Request) -> Response:
        self._log("api", req)
        self._check_request(req)
        if req.path == "/oauth2/token" and req.method == "POST":
            return self._token(req)
        if req.path == "/oauth2/authorize" and req.method == "GET":
            return self._authorize(req)
        handler = self.RPC.get(req.path)
        if handler is None or req.method != "POST":
            raise HTTPError(404, b"Unknown API function")
        route = req.path[len("/2/"):]
        content_type = (req.header("Content-Type") or "").split(";")[0].strip()
        if req.body and content_type != "application/json":
            raise _bad_request(route, f'Bad HTTP "Content-Type" header: "{content_type}". Expecting "application/json".')
        try:
            arg = json.loads(req.body.decode("utf-8")) if req.body else None
        except ValueError:
            raise _bad_request(route, "request body: could not decode input as JSON") from None
        if req.path != "/2/users/get_current_account" and not isinstance(arg, dict):
            raise _bad_request(route, "request body: expected an object")
        with self.lock:
            self._call_checks(req)
            return handler(self, route, arg, self._rooted(req))

    def _handle_content(self, req: Request) -> Response:
        self._log("content", req)
        self._check_request(req)
        if req.path.startswith("/apitl/1/"):
            return self._temporary_download(req)
        if req.path.startswith("/apitul/1/"):
            return self._temporary_upload(req)
        handler = self.CONTENT.get(req.path)
        if handler is None or req.method != "POST":
            raise HTTPError(404, b"Unknown API function")
        route = req.path[len("/2/"):]
        content_type = (req.header("Content-Type") or "").split(";")[0].strip()
        download = req.path == "/2/files/download"
        if download and (req.body or content_type not in ("", "text/plain", "application/octet-stream")):
            raise _bad_request(route, f'Bad HTTP "Content-Type" header: "{content_type}". A download takes no body.')
        if not download and content_type != "application/octet-stream":
            raise _bad_request(route, f'Bad HTTP "Content-Type" header: "{content_type}". Expecting "application/octet-stream".')
        try:
            arg = json.loads(req.header("Dropbox-API-Arg") or "")
        except ValueError:
            raise _bad_request(route, "HTTP header \"Dropbox-API-Arg\": could not decode input as JSON") from None
        if not isinstance(arg, dict):
            raise _bad_request(route, "HTTP header \"Dropbox-API-Arg\": expected an object")
        with self.lock:
            self._call_checks(req)
            return handler(self, req, arg, self._rooted(req))

    def _call_checks(self, req: Request) -> None:
        """The bearer token, then what the tests asked for (throttling, errors)."""
        token = req.bearer()
        expires = self.access_tokens.get(token or "")
        if expires is None:
            raise api_error(401, "invalid_access_token")
        if expires < time.time():
            raise api_error(401, "expired_access_token")
        if self.throttle > 0:
            self.throttle -= 1
            raise HTTPError(429, {"error_summary": f"{self.throttle_reason}/..",
                                  "error": {"reason": {".tag": self.throttle_reason}, "retry_after": 1}},
                            {"Retry-After": self.throttle_after})
        if self.fail_next and self.fail_next[0][3:4] in ((), (None,), (req.path,)):
            status, tags, *rest = self.fail_next.pop(0)
            raise api_error(status, *tags, extra=rest[0] if rest else None)

    def _rooted(self, req: Request) -> Node:
        """The folder the call's paths start at: the home folder, or the root Dropbox-API-Path-Root names."""
        header = req.header("Dropbox-API-Path-Root")
        if header is None:
            return self.home
        try:
            value = json.loads(header)
        except ValueError:
            raise _bad_request(req.path[len("/2/"):], "HTTP header \"Dropbox-API-Path-Root\": could not decode input as JSON") from None
        tag = value.get(".tag") if isinstance(value, dict) else None
        if tag == "home":
            return self.home
        if tag == "root":
            if value.get("root") != self.root_ns:
                raise api_error(422, "invalid_root", extra={"invalid_root": self._root_info()})
            return self.root()
        raise api_error(422, "no_permission")

    def _root_info(self) -> dict[str, Any]:
        if self.team is None:
            return {".tag": "user", "root_namespace_id": self.root_ns, "home_namespace_id": self.home_ns}
        return {".tag": "team", "root_namespace_id": self.root_ns, "home_namespace_id": self.home_ns,
                "home_path": "/" + self.home.name}

    # -- OAuth

    def _authorize(self, req: Request) -> Response:
        redirect = req.arg("redirect_uri") or ""
        if req.arg("client_id") != self.app_key:
            raise HTTPError(400, b"Invalid client_id.")
        if req.arg("response_type") != "code" or not redirect.startswith(("http://", "https://")):
            raise HTTPError(400, b"Invalid authorization request.")
        code = self.grant_code(redirect, offline=req.arg("token_access_type") == "offline")
        answer = urllib.parse.urlencode({"code": code, "state": req.arg("state") or ""})
        return Response(302, b"", {"Location": redirect + ("&" if "?" in redirect else "?") + answer})

    def _token(self, req: Request) -> Response:
        form = req.form()
        with self.lock:
            if form.get("client_id") != self.app_key or form.get("client_secret") != self.app_secret:
                raise HTTPError(400, {"error": "invalid_client: Invalid client_id or client_secret"})
            grant = form.get("grant_type")
            answer: dict[str, Any] = {"token_type": "bearer", "expires_in": self.token_lifetime}
            if grant == "authorization_code":
                code = self.codes.pop(form.get("code") or "", None)
                if code is None:
                    raise HTTPError(400, {"error": "invalid_grant", "error_description": "code doesn't exist or has expired"})
                if code[0] != form.get("redirect_uri"):
                    raise HTTPError(400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"})
                answer.update(account_id="dbid:FAKE", uid="12345")
                if code[1]:
                    answer["refresh_token"] = self.issue_refresh_token()
            elif grant == "refresh_token":
                if form.get("refresh_token") not in self.refresh_tokens:
                    raise HTTPError(400, {"error": "invalid_grant", "error_description": "refresh token is invalid or revoked"})
            else:
                raise HTTPError(400, {"error": "unsupported_grant_type"})
            answer["access_token"] = "sl.fake-" + secrets.token_urlsafe(16)
            self.access_tokens[answer["access_token"]] = time.time() + self.token_lifetime
            return json_response(answer)

    # -- paths and metadata

    @staticmethod
    def _parts(route: str, path: Any, *, root_ok: bool = True) -> list[str]:
        """The names of a Dropbox path, checked as Dropbox checks them."""
        if not isinstance(path, str) or not (path == "" or path.startswith("/")):
            raise _bad_request(route, f"request body: path: {path!r} did not match pattern")
        if path == "":
            if not root_ok:
                raise _bad_request(route, "request body: path: The root folder is unsupported.")
            return []
        parts = path[1:].split("/")
        if path.endswith("/") or path[-1].isspace() or any(p in ("", ".", "..") or "\\" in p for p in parts):
            raise api_error(409, "path", "malformed_path")
        return parts

    @staticmethod
    def _lookup(root: Node, parts: list[str]) -> Node | None:
        node = root
        for part in parts:
            node = node.children.get(part.lower()) if node.folder else None
            if node is None:
                return None
        return node

    def _parent_for(self, root: Node, parts: list[str], tag: str) -> Node:
        """The folder a new item at `parts` goes into, made with the folders above it as Dropbox does."""
        parent = root
        for part in parts[:-1]:
            child = parent.children.get(part.lower())
            if child is None:
                child = parent.add(Node(part, True))
            elif not child.folder:
                raise api_error(409, tag, "conflict", "file_ancestor")
            parent = child
        return parent

    def _metadata(self, node: Node, root: Node) -> dict[str, Any]:
        display = node.path_from(root)
        answer: dict[str, Any] = {".tag": "folder" if node.folder else "file", "name": node.name, "id": node.id,
                                  "path_lower": display.lower(), "path_display": display}
        if not node.folder:
            stamp = node.modified.strftime("%Y-%m-%dT%H:%M:%SZ")
            answer.update(client_modified=stamp, server_modified=stamp, rev=node.rev, size=len(node.data),
                          is_downloadable=node.downloadable, content_hash=_content_hash(node.data))
        return answer

    def _commit(self, route: str, root: Node, commit: Any, data: bytes, tag: str = "path") -> Node:
        """Store an upload as CommitInfo says (modes add and overwrite, no autorename)."""
        if not isinstance(commit, dict):
            raise _bad_request(route, "commit info: expected an object")
        mode = commit.get("mode", "add")
        if mode not in ("add", "overwrite") or commit.get("autorename", False):
            raise _bad_request(route, "the fake knows the modes add and overwrite only, without autorename")
        parts = self._parts(route, commit.get("path"), root_ok=False)
        parent = self._parent_for(root, parts, tag)
        existing = parent.children.get(parts[-1].lower())
        if existing is not None:
            if existing.folder:
                raise api_error(409, tag, "conflict", "folder")
            if mode == "add" and existing.data != data:
                raise api_error(409, tag, "conflict", "file")
            existing.data = data
            existing.touch()
            return existing
        return parent.add(Node(parts[-1], False, data))

    # -- RPC routes

    def _current_account(self, route, arg, root):
        if arg is not None:
            raise _bad_request(route, "request body: this function takes no arguments")
        return json_response({"account_id": "dbid:FAKE", "name": {"display_name": "Member"}, "email": "member@example.com",
                              "email_verified": self.email_verified, "root_info": self._root_info()})

    def _list_folder(self, route, arg, root):
        limit = arg.get("limit", 2000)
        if not isinstance(limit, int) or not 1 <= limit <= 2000 or arg.get("recursive"):
            raise _bad_request(route, "request body: limit 1-2000, recursive false (the fake)")
        node = self._lookup(root, self._parts(route, arg.get("path")))
        if node is None:
            raise api_error(409, "path", "not_found")
        if not node.folder:
            raise api_error(409, "path", "not_folder")
        return self._page(root, node, 0, min(limit, self.page_size))

    def _list_continue(self, route, arg, root):
        state = self.cursors.get(arg.get("cursor") or "")
        if state is None:
            raise api_error(409, "reset")
        return self._page(*state)

    def _page(self, root: Node, node: Node, offset: int, size: int) -> Response:
        children = sorted(node.children.values(), key=lambda child: child.name.lower())
        page = children[offset:offset + size]
        cursor = "stuck-cursor" if self.stuck_cursor else "cursor-" + secrets.token_urlsafe(8)
        self.cursors[cursor] = (root, node, offset + len(page), size)
        return json_response({"entries": [self._metadata(child, root) for child in page], "cursor": cursor,
                              "has_more": offset + len(page) < len(children)})

    def _get_metadata(self, route, arg, root):
        node = self._lookup(root, self._parts(route, arg.get("path"), root_ok=False))
        if node is None:
            raise api_error(409, "path", "not_found")
        return json_response(self._metadata(node, root))

    def _create_folder(self, route, arg, root):
        if arg.get("autorename", False):
            raise _bad_request(route, "the fake doesn't autorename")
        parts = self._parts(route, arg.get("path"), root_ok=False)
        parent = self._parent_for(root, parts, "path")
        existing = parent.children.get(parts[-1].lower())
        if existing is not None:
            raise api_error(409, "path", "conflict", "folder" if existing.folder else "file")
        return json_response({"metadata": self._metadata(parent.add(Node(parts[-1], True)), root)})

    def _delete(self, route, arg, root):
        parts = self._parts(route, arg.get("path"), root_ok=False)
        node = self._lookup(root, parts)
        if node is None:
            raise api_error(409, "path_lookup", "not_found")
        metadata = self._metadata(node, root)
        node.detach()
        return json_response({"metadata": metadata})

    def _relocate(self, route, arg, root):
        if arg.get("autorename", False):
            raise _bad_request(route, "the fake doesn't autorename")
        source = self._lookup(root, self._parts(route, arg.get("from_path"), root_ok=False))
        if source is None:
            raise api_error(409, "from_lookup", "not_found")
        target_parts = self._parts(route, arg.get("to_path"), root_ok=False)
        from_lower, to_lower = arg["from_path"].lower(), arg["to_path"].lower()
        if from_lower == to_lower:
            raise api_error(409, "duplicated_or_nested_paths")
        if to_lower.startswith(from_lower + "/"):
            raise api_error(409, "cant_move_folder_into_itself" if route == "files/move_v2" else "duplicated_or_nested_paths")
        parent = self._parent_for(root, target_parts, "to")
        existing = parent.children.get(target_parts[-1].lower())
        if existing is not None:
            raise api_error(409, "to", "conflict", "folder" if existing.folder else "file")
        if route == "files/move_v2":
            source.detach()
            source.name = target_parts[-1]
            moved = parent.add(source)
        else:
            moved = parent.add(source.clone(target_parts[-1]))
        return json_response({"metadata": self._metadata(moved, root)})

    @staticmethod
    def _words(text: str) -> list[str]:
        return re.findall(r"\w+", text.casefold())

    def _search(self, route, arg, root):
        query = arg.get("query")
        options = arg.get("options") or {}
        page = options.get("max_results", 100)
        if not isinstance(query, str) or not query or not isinstance(page, int) or not 1 <= page <= 1000:
            raise _bad_request(route, "request body: query, options.max_results 1-1000")
        scope = self._lookup(root, self._parts(route, options.get("path", "")))
        if scope is None or not scope.folder:
            raise api_error(409, "path", "not_found" if scope is None else "not_folder")
        wanted = self._words(query)
        matches = []
        for node in sorted(scope.walk(), key=lambda n: n.path_from(root).lower()):
            words = self._words(node.name)
            by_name = node is not scope and all(any(word.startswith(w) for word in words) for w in wanted)
            by_content = not options.get("filename_only") and query.casefold().encode() in node.data.lower()
            if by_name or (node is not scope and by_content):
                matches.append({"metadata": {".tag": "metadata", "metadata": self._metadata(node, root)},
                                "match_type": {".tag": "filename" if by_name else "file_content"}})
        return self._search_page(matches, 0, page)

    def _search_continue(self, route, arg, root):
        state = self.search_cursors.get(arg.get("cursor") or "")
        if state is None:
            raise api_error(409, "invalid_argument")
        matches, offset, page = state
        return self._search_page(matches, offset, page)

    def _search_page(self, matches: list[dict[str, Any]], offset: int, page: int) -> Response:
        answer: dict[str, Any] = {"matches": matches[offset:offset + page], "has_more": offset + page < len(matches)}
        if answer["has_more"]:
            cursor = "search-" + secrets.token_urlsafe(8)
            self.search_cursors[cursor] = (matches, offset + page, page)
            answer["cursor"] = cursor
        return json_response(answer)

    def _temporary_link(self, route, arg, root):
        node = self._lookup(root, self._parts(route, arg.get("path"), root_ok=False))
        if node is None:
            raise api_error(409, "path", "not_found")
        if node.folder:
            raise api_error(409, "path", "not_file")
        if not self.email_verified:
            raise api_error(409, "email_not_verified")
        if not node.downloadable:
            raise api_error(409, "unsupported_file")
        token = secrets.token_urlsafe(16)
        self.links[token] = (node, time.time() + LINK_LIFETIME)
        return json_response({"metadata": self._metadata(node, root), "link": f"{self.content_url}/apitl/1/{token}"})

    def _temporary_upload_link(self, route, arg, root):
        duration = arg.get("duration", 14400)
        if not isinstance(duration, (int, float)) or not 60 <= duration <= 14400:
            raise _bad_request(route, "request body: duration: 60 to 14400")
        commit = arg.get("commit_info")
        self._parts(route, commit.get("path") if isinstance(commit, dict) else None, root_ok=False)
        token = secrets.token_urlsafe(16)
        self.upload_links[token] = {"root": root, "commit": commit, "expires": time.time() + duration, "used": False}
        return json_response({"link": f"{self.content_url}/apitul/1/{token}"})

    RPC = {
        "/2/users/get_current_account": _current_account,
        "/2/files/list_folder": _list_folder,
        "/2/files/list_folder/continue": _list_continue,
        "/2/files/get_metadata": _get_metadata,
        "/2/files/create_folder_v2": _create_folder,
        "/2/files/delete_v2": _delete,
        "/2/files/move_v2": _relocate,
        "/2/files/copy_v2": _relocate,
        "/2/files/search_v2": _search,
        "/2/files/search/continue_v2": _search_continue,
        "/2/files/get_temporary_link": _temporary_link,
        "/2/files/get_temporary_upload_link": _temporary_upload_link,
    }

    # -- content routes

    def _download(self, req, arg, root):
        route = "files/download"
        node = self._lookup(root, self._parts(route, arg.get("path"), root_ok=False))
        if node is None:
            raise api_error(409, "path", "not_found")
        if node.folder:
            raise api_error(409, "path", "not_file")
        if not node.downloadable:
            raise api_error(409, "unsupported_file")
        result = json.dumps(self._metadata(node, root))  # ASCII, as a header must be
        return file_response(node.data, req.header("Range"), {"Dropbox-API-Result": result})

    def _upload(self, req, arg, root):
        route = "files/upload"
        try:
            node = self._commit(route, root, arg, req.body)
        except HTTPError as error:
            if error.response.status != 409:
                raise
            body = json.loads(error.response.body)
            if body["error"].get(".tag") == "path":  # UploadError.path is UploadWriteFailed: the WriteError as its reason
                reason = body["error"].pop("path")
                body["error"]["path"] = {"reason": reason, "upload_session_id": "unused"}
                raise HTTPError(error.response.status, body) from None
            raise
        if self.lose_uploads > 0:
            self.lose_uploads -= 1
            return Response(0)  # stored; the answer is lost
        return json_response(self._metadata(node, root))

    def _session_start(self, req, arg, root):
        if arg.get("session_type") not in (None, "sequential"):
            raise _bad_request("files/upload_session/start", "the fake knows sequential sessions only")
        session_id = "session-" + secrets.token_urlsafe(8)
        self.sessions[session_id] = bytearray(req.body)
        return json_response({"session_id": session_id})

    def _session_data(self, arg: dict[str, Any], *wrap: str) -> bytearray:
        """The session the cursor names, at the cursor's offset (UploadSessionLookupError otherwise)."""
        cursor = arg.get("cursor") if isinstance(arg.get("cursor"), dict) else {}
        data = self.sessions.get(cursor.get("session_id") or "")
        if data is None:
            raise api_error(409, *wrap, "not_found")
        if cursor.get("offset") != len(data):
            raise api_error(409, *wrap, "incorrect_offset", extra={"correct_offset": len(data)})
        return data

    def _session_append(self, req, arg, root):
        data = self._session_data(arg)
        data += req.body
        if self.lose_appends > 0:
            self.lose_appends -= 1
            return Response(0)  # stored; the answer is lost
        return json_response(None)

    def _session_finish(self, req, arg, root):
        route = "files/upload_session/finish"
        data = self._session_data(arg, "lookup_failed")
        data += req.body
        node = self._commit(route, root, arg.get("commit"), bytes(data))
        del self.sessions[arg["cursor"]["session_id"]]
        return json_response(self._metadata(node, root))

    CONTENT = {
        "/2/files/download": _download,
        "/2/files/upload": _upload,
        "/2/files/upload_session/start": _session_start,
        "/2/files/upload_session/append_v2": _session_append,
        "/2/files/upload_session/finish": _session_finish,
    }

    # -- temporary links (no token)

    def _temporary_download(self, req: Request) -> Response:
        with self.lock:
            link = self.links.get(req.path[len("/apitl/1/"):])
            if link is None:
                raise HTTPError(404, b"Not found")
            node, expires = link
            if expires < time.time():
                raise HTTPError(410, b"Gone")
            data = node.data
        return file_response(data, req.header("Range"))

    def _temporary_upload(self, req: Request) -> Response:
        if req.method != "POST":
            raise HTTPError(405, b"Method not allowed")
        if req.header("Content-Type") != "application/octet-stream":
            raise HTTPError(400, b'Invalid or missing Content-Type. Please specify the Content-Type. The only allowed '
                                 b'Content-Type is "application/octet-stream".')
        with self.lock:
            link = self.upload_links.get(req.path[len("/apitul/1/"):])
            if link is None:
                raise HTTPError(409, b"Temporary upload link is invalid.")
            if link["used"] or link["expires"] < time.time():
                raise HTTPError(410, b"Temporary upload link has been recently consumed.")
            link["used"] = True
            try:
                self._commit("apitul", link["root"], link["commit"], req.body)
            except HTTPError:
                raise HTTPError(409, b"The upload failed.") from None  # "generic HTTP 409 Conflict responses"
        return json_response({"content-hash": _content_hash(req.body)})
