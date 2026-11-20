"""An in-memory fake of Google Drive (API v3, resumable uploads, OAuth token endpoint) for the
driver tests; no Django.

It checks what Google checks, so that a passing test means something:
- bearer tokens on every API call (missing -> 401 "required", unknown or expired -> 401 "authError"),
  client id and secret, codes bound to their redirect URI, refresh tokens (revocable);
- field masks: without `fields`, files come back with id, name and mimeType only (Drive v3's
  default), and a list without "nextPageToken" in its mask has no next page;
- shared drives need supportsAllDrives (else their files are "not found"), corpora=drive needs
  driveId and includeItemsFromAllDrives; corpora=drive has only that drive's items;
- the subset of the query language the driver uses ("'x' in parents", name =/contains, mimeType,
  trashed, joined by "and" and "or", groups in parentheses), with \\ and \\' the only escapes;
  anything else is a 400;
- a folder's children count as trashed when it is (a file's "trashed" is true through a trashed
  folder too, "explicitlyTrashed" only for itself); trashed files stay readable by id;
- one parent per file; no downloads of folders or Docs (export them instead), no export of
  binary files, exports above `export_limit` refused;
- resumable sessions: offsets must follow what was persisted, chunks before the last must be
  multiples of 256 KiB, 308 with "Range: bytes=0-N"; sessions need no token.
Knobs for tests: max_page_size, token_lifetime, rotate_refresh, short_puts (persist only 256 KiB of
the next n chunks), fail_puts (503 for the next n chunk PUTs), inject() (a canned answer for the
next matching request).
"""

from __future__ import annotations

import datetime
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

FOLDER = "application/vnd.google-apps.folder"
SHORTCUT = "application/vnd.google-apps.shortcut"
GOOGLE_APPS = "application/vnd.google-apps."
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
SLIDES = "application/vnd.google-apps.presentation"
DRAWING = "application/vnd.google-apps.drawing"
SCRIPT = "application/vnd.google-apps.script"
FORM = "application/vnd.google-apps.form"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

EXPORT_FORMATS = {
    DOC: {DOCX, "application/pdf", "text/plain"},
    SHEET: {XLSX, "application/pdf", "text/csv"},
    SLIDES: {"application/vnd.openxmlformats-officedocument.presentationml.presentation", "application/pdf"},
    DRAWING: {"application/pdf", "image/png"},
    SCRIPT: {"application/vnd.google-apps.script+json"},
}

CLIENT_ID = "fake-client.apps.googleusercontent.com"
CLIENT_SECRET = "fake-client-secret"
SCOPE = "https://www.googleapis.com/auth/drive"
ALIGN = 256 * 1024


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _error(status: int, message: str, reason: str) -> HTTPError:
    return HTTPError(status, {"error": {"code": status, "message": message,
                                        "errors": [{"domain": "global", "reason": reason, "message": message}]}})


def _parse_mask(text: str) -> dict[str, Any]:
    """"a,b(c,d(e))" -> {"a": None, "b": {"c": None, "d": {"e": None}}}."""

    def parse(i: int) -> tuple[dict[str, Any], int]:
        out: dict[str, Any] = {}
        name = ""
        while i < len(text):
            ch = text[i]
            if ch == ",":
                if name.strip():
                    out[name.strip()] = None
                name = ""
                i += 1
            elif ch == "(":
                sub, i = parse(i + 1)
                out[name.strip()] = sub
                name = ""
            elif ch == ")":
                if name.strip():
                    out[name.strip()] = None
                return out, i + 1
            else:
                name += ch
                i += 1
        if name.strip():
            out[name.strip()] = None
        return out, i

    return parse(0)[0]


def _apply_mask(value: Any, mask: dict[str, Any] | None) -> Any:
    if mask is None or "*" in mask:
        return value
    if isinstance(value, list):
        return [_apply_mask(item, mask) for item in value]
    if isinstance(value, dict):
        return {key: _apply_mask(item, mask[key]) for key, item in value.items() if key in mask}
    return value


_LITERAL = r"'((?:[^'\\]|\\['\\])*)'"


def _unescape(text: str) -> str:
    return re.sub(r"\\(['\\])", r"\1", text)


def _split(q: str, word: str) -> list[str]:
    """Clauses joined by `word` (" and ", " or "), outside quoted literals and parentheses."""
    parts, current, i, quoted, depth = [], "", 0, False, 0
    while i < len(q):
        ch = q[i]
        if quoted:
            current += ch
            if ch == "\\" and i + 1 < len(q):
                current += q[i + 1]
                i += 2
                continue
            if ch == "'":
                quoted = False
            i += 1
            continue
        if ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                raise _error(400, "Invalid Value", "invalid")
        elif depth == 0 and q[i:i + len(word)].lower() == word:
            parts.append(current)
            current = ""
            i += len(word)
            continue
        current += ch
        i += 1
    if quoted or depth:
        raise _error(400, "Invalid Value", "invalid")
    parts.append(current)
    return [part.strip() for part in parts]


def _word_prefix(name: str, value: str) -> bool:
    """Drive's "name contains": the value starts the name or one of its words."""
    name, value = name.casefold(), value.casefold()
    if name.startswith(value):
        return True
    return any(word.startswith(value) for word in re.split(r"[\s._\-()\[\],;]+", name) if word)


class FakeDrive:
    def __init__(self):
        self.lock = threading.RLock()
        self.items: dict[str, dict[str, Any]] = {}
        self.root_id = self._new_id()
        self.items[self.root_id] = {"id": self.root_id, "name": "My Drive", "mimeType": FOLDER, "parents": [],
                                    "trashed": False, "modifiedTime": _now(), "driveId": None}
        self.drives: dict[str, str] = {}  # shared drive id -> name (its root folder has the same id)
        self.clients = {CLIENT_ID: CLIENT_SECRET}
        self.codes: dict[str, dict[str, str]] = {}
        self.refresh_tokens: dict[str, str] = {}  # token -> client id
        self.access_tokens: dict[str, float] = {}  # token -> expiry (time.time())
        self.sessions: dict[str, dict[str, Any]] = {}
        self.page_tokens: dict[str, tuple[str, int]] = {}
        self.max_page_size = 1000
        self.token_lifetime = 3600
        self.rotate_refresh = False
        self.export_limit = 10 * 1024 * 1024
        self.short_puts = 0
        self.fail_puts = 0
        self.token_requests = 0
        self.deleted: list[str] = []  # files.delete calls (the driver should trash instead)
        self.log: list[tuple[str, str, dict[str, list[str]], bool]] = []  # method, path, query, had a token
        self._injected: list[list[Any]] = []
        self.server = FakeServer(self.handle).start()
        self.url = self.server.url

    def close(self) -> None:
        self.server.close()

    # -- helpers for tests --------------------------------------------------------------------------

    @staticmethod
    def _new_id() -> str:
        return secrets.token_urlsafe(21)

    def issue_refresh_token(self, client_id: str = CLIENT_ID) -> str:
        with self.lock:
            token = "1//rt-" + secrets.token_urlsafe(16)
            self.refresh_tokens[token] = client_id
            return token

    def expire_access_tokens(self) -> None:
        with self.lock:
            for token in self.access_tokens:
                self.access_tokens[token] = 0.0

    def consent(self, authorize_url: str) -> str:
        """What Google's consent page does: check the request, then give a code for the callback."""
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(authorize_url).query))
        assert query.get("client_id") in self.clients, query
        assert query.get("response_type") == "code", query
        assert query.get("scope") == SCOPE, query
        assert query.get("access_type") == "offline", query
        assert query.get("redirect_uri"), query
        code = "4/code-" + secrets.token_urlsafe(12)
        with self.lock:
            self.codes[code] = {"client_id": query["client_id"], "redirect_uri": query["redirect_uri"]}
        return code

    def create_drive(self, name: str) -> str:
        with self.lock:
            drive_id = "0A" + secrets.token_urlsafe(12).replace("-", "x").replace("_", "y")
            self.drives[drive_id] = name
            self.items[drive_id] = {"id": drive_id, "name": name, "mimeType": FOLDER, "parents": [],
                                    "trashed": False, "modifiedTime": _now(), "driveId": drive_id}
            return drive_id

    def add(self, parent: str, name: str, mime: str = "application/octet-stream", data: bytes | None = None,
            *, modified: str | None = None, target: str | None = None, exports: dict[str, bytes] | None = None,
            size: int | None = None) -> str:
        with self.lock:
            parent_item = self.items[parent]
            item_id = self._new_id()
            item: dict[str, Any] = {"id": item_id, "name": name, "mimeType": mime, "parents": [parent],
                                    "trashed": False, "modifiedTime": modified or _now(),
                                    "driveId": parent_item["driveId"]}
            if mime == SHORTCUT:
                item["shortcutDetails"] = {"targetId": target, "targetMimeType": self.items[target]["mimeType"]}
            elif mime != FOLDER and not mime.startswith(GOOGLE_APPS):
                item["data"] = data or b""
            elif mime.startswith(GOOGLE_APPS) and mime != FOLDER:
                item["exports"] = dict(exports or {})
                item["size"] = size if size is not None else 1234  # Drive's storage size, not the export's
            self.items[item_id] = item
            return item_id

    def folder(self, path: str, top: str | None = None) -> str:
        """The id of a folder path below My Drive (or `top`), made as needed."""
        with self.lock:
            current = top or self.root_id
            for name in [part for part in path.split("/") if part]:
                found = [i for i in self.children(current) if i["name"] == name and i["mimeType"] == FOLDER]
                current = found[0]["id"] if found else self.add(current, name, FOLDER)
            return current

    def children(self, parent: str, *, trashed: bool = False) -> list[dict[str, Any]]:
        with self.lock:
            return [item for item in self.items.values()
                    if parent in item["parents"] and (trashed or not self._trashed(item))]

    def child(self, parent: str, name: str) -> dict[str, Any]:
        found = [item for item in self.children(parent) if item["name"] == name]
        assert len(found) == 1, (name, found)
        return found[0]

    def inject(self, method: str, path_pattern: str, response: Response, times: int = 1) -> None:
        """Answer the next `times` requests matching method and regex `path_pattern` with `response`."""
        with self.lock:
            self._injected.append([method, re.compile(path_pattern), response, times])

    # -- the model ----------------------------------------------------------------------------------

    def _trashed(self, item: dict[str, Any]) -> bool:
        seen = 0
        while item is not None and seen < 100:
            if item["trashed"]:
                return True
            parents = item["parents"]
            item = self.items.get(parents[0]) if parents else None
            seen += 1
        return False

    def _file(self, file_id: str, supports_all: bool) -> dict[str, Any]:
        if file_id == "root":
            file_id = self.root_id
        item = self.items.get(file_id)
        if item is None or (item["driveId"] and not supports_all):
            raise _error(404, f"File not found: {file_id}.", "notFound")
        return item

    def _resource(self, item: dict[str, Any]) -> dict[str, Any]:
        out = {"kind": "drive#file", "id": item["id"], "name": item["name"], "mimeType": item["mimeType"],
               "parents": list(item["parents"]), "trashed": self._trashed(item),
               "explicitlyTrashed": item["trashed"], "modifiedTime": item["modifiedTime"]}
        if item["driveId"]:
            out["driveId"] = item["driveId"]
        if "data" in item:
            out["size"] = str(len(item["data"]))
        elif "size" in item:
            out["size"] = str(item["size"])
        if "shortcutDetails" in item:
            out["shortcutDetails"] = dict(item["shortcutDetails"])
        return out

    @staticmethod
    def _masked(body: Any, fields: str | None, default: str) -> Response:
        """The answer cut down to the field mask, or to Drive v3's default without one."""
        return json_response(_apply_mask(body, _parse_mask(fields if fields is not None else default)))

    def _auth(self, request) -> None:
        token = request.bearer()
        if token is None:
            raise _error(401, "Request is missing required authentication credential.", "required")
        expiry = self.access_tokens.get(token)
        if expiry is None or expiry < time.time():
            raise _error(401, "Invalid Credentials", "authError")

    def _predicate(self, q: str):
        if not q.strip():
            return lambda item: True
        return self._expression(q)

    def _expression(self, q: str):
        """Clauses joined by "or" of clauses joined by "and" ("and" binds closer)."""
        alternatives = []
        for alternative in _split(q, " or "):
            tests = [self._clause(clause) for clause in _split(alternative, " and ")]
            alternatives.append(lambda item, tests=tests: all(test(item) for test in tests))
        return lambda item: any(test(item) for test in alternatives)

    def _clause(self, clause: str):
        if clause.startswith("(") and clause.endswith(")"):
            return self._expression(clause[1:-1])
        if m := re.fullmatch(_LITERAL + r"\s+in\s+parents", clause):
            parent = _unescape(m.group(1))
            return lambda item: parent in item["parents"]
        if m := re.fullmatch(r"name\s*(=|!=|contains)\s*" + _LITERAL, clause):
            op, value = m.group(1), _unescape(m.group(2))
            if op == "=":
                return lambda item: item["name"] == value
            if op == "!=":
                return lambda item: item["name"] != value
            return lambda item: _word_prefix(item["name"], value)
        if m := re.fullmatch(r"mimeType\s*(=|!=)\s*" + _LITERAL, clause):
            op, value = m.group(1), _unescape(m.group(2))
            return lambda item: (item["mimeType"] == value) == (op == "=")
        if m := re.fullmatch(r"trashed\s*=\s*(true|false)", clause):
            wanted = m.group(1) == "true"
            return lambda item: self._trashed(item) == wanted
        raise _error(400, "Invalid Value", "invalid")

    # -- HTTP ---------------------------------------------------------------------------------------

    def handle(self, request) -> Response:
        with self.lock:
            self.log.append((request.method, request.path, request.query, bool(request.header("Authorization"))))
            for entry in self._injected:
                method, pattern, response, times = entry
                if times > 0 and method == request.method and pattern.search(request.path):
                    entry[3] -= 1
                    if entry[3] == 0:
                        self._injected.remove(entry)
                    return response
        path = request.path
        if path == "/token" and request.method == "POST":
            return self._token(request)
        if path.startswith("/upload/drive/v3/files"):
            if request.method == "PUT":
                return self._session_put(request)
            return self._start_upload(request)
        if path == "/drive/v3/drives" and request.method == "GET":
            return self._drives(request)
        if path == "/drive/v3/files":
            if request.method == "GET":
                return self._list(request)
            if request.method == "POST":
                return self._create(request)
        m = re.fullmatch(r"/drive/v3/files/([^/]+)(/copy|/export)?", path)
        if m:
            file_id = urllib.parse.unquote(m.group(1))
            action = m.group(2)
            if action == "/copy" and request.method == "POST":
                return self._copy(request, file_id)
            if action == "/export" and request.method == "GET":
                return self._export(request, file_id)
            if action is None and request.method == "GET":
                return self._get(request, file_id)
            if action is None and request.method == "PATCH":
                return self._update(request, file_id)
            if action is None and request.method == "DELETE":
                self._auth(request)
                with self.lock:
                    self._file(file_id, request.arg("supportsAllDrives") == "true")
                    self.deleted.append(file_id)
                    self.items.pop(file_id, None)
                return Response(204)
        raise _error(404, "Not Found", "notFound")

    def _token(self, request) -> Response:
        with self.lock:
            self.token_requests += 1
            if not (request.header("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
                raise HTTPError(400, {"error": "invalid_request", "error_description": "Bad content type"})
            form = request.form()
            client_id, secret = form.get("client_id"), form.get("client_secret")
            if not secret or self.clients.get(client_id) != secret:
                raise HTTPError(401, {"error": "invalid_client",
                                      "error_description": "The OAuth client was not found."})
            grant = form.get("grant_type")
            answer: dict[str, Any] = {"token_type": "Bearer", "scope": SCOPE, "expires_in": self.token_lifetime}
            if grant == "authorization_code":
                entry = self.codes.pop(form.get("code", ""), None)
                if entry is None or entry["client_id"] != client_id:
                    raise HTTPError(400, {"error": "invalid_grant", "error_description": "Malformed auth code."})
                if entry["redirect_uri"] != form.get("redirect_uri"):
                    raise HTTPError(400, {"error": "redirect_uri_mismatch", "error_description": "Bad Request"})
                answer["refresh_token"] = self.issue_refresh_token(client_id)
            elif grant == "refresh_token":
                refresh = form.get("refresh_token", "")
                if self.refresh_tokens.get(refresh) != client_id:
                    raise HTTPError(400, {"error": "invalid_grant",
                                          "error_description": "Token has been expired or revoked."})
                if self.rotate_refresh:
                    del self.refresh_tokens[refresh]
                    answer["refresh_token"] = self.issue_refresh_token(client_id)
            else:
                raise HTTPError(400, {"error": "unsupported_grant_type", "error_description": "Invalid grant_type"})
            token = "ya29.at-" + secrets.token_urlsafe(16)
            self.access_tokens[token] = time.time() + self.token_lifetime
            answer["access_token"] = token
            return json_response(answer)

    def _list(self, request) -> Response:
        self._auth(request)
        with self.lock:
            try:
                page_size = int(request.arg("pageSize", "100"))
            except ValueError:
                raise _error(400, "Invalid value for pageSize", "invalid") from None
            if not 1 <= page_size <= 1000:
                raise _error(400, "Invalid value for pageSize", "invalid")
            page_size = min(page_size, self.max_page_size)
            supports = request.arg("supportsAllDrives") == "true"
            include = request.arg("includeItemsFromAllDrives") == "true"
            corpora = request.arg("corpora", "user")
            drive_id = request.arg("driveId")
            if include and not supports:
                raise _error(403, "includeItemsFromAllDrives can only be set when supportsAllDrives is true.",
                             "invalid")
            if (corpora == "drive") != bool(drive_id):
                raise _error(400, "The driveId parameter must be specified if and only if corpora is set to drive.",
                             "invalid")
            if corpora == "drive":
                if not include:
                    raise _error(403, "includeItemsFromAllDrives must be true with corpora drive.", "invalid")
                if drive_id not in self.drives:
                    raise _error(404, f"Shared drive not found: {drive_id}", "notFound")
            elif corpora not in ("user", "allDrives"):
                raise _error(400, "Invalid Value", "invalid")
            q = request.arg("q", "")
            match = self._predicate(q)

            def in_corpus(item):
                if item["id"] == self.root_id or item["id"] in self.drives:
                    return False
                if corpora == "drive":
                    return item["driveId"] == drive_id
                return item["driveId"] is None or include

            found = sorted((item for item in self.items.values() if in_corpus(item) and match(item)),
                           key=lambda item: (item["name"], item["id"]))
            offset = 0
            token = request.arg("pageToken")
            if token:
                if token not in self.page_tokens or self.page_tokens[token][0] != q:
                    raise _error(400, "Invalid Value", "invalid")
                offset = self.page_tokens[token][1]
            page = found[offset:offset + page_size]
            body: dict[str, Any] = {"kind": "drive#fileList", "incompleteSearch": False,
                                    "files": [self._resource(item) for item in page]}
            if offset + page_size < len(found):
                next_token = secrets.token_urlsafe(9)
                self.page_tokens[next_token] = (q, offset + page_size)
                body["nextPageToken"] = next_token
            return self._masked(body, request.arg("fields"),
                                "kind,nextPageToken,incompleteSearch,files(kind,id,name,mimeType)")

    def _check_parent(self, parents: Any, supports: bool) -> dict[str, Any]:
        if not isinstance(parents, list) or len(parents) != 1:
            raise _error(403, "Increasing the number of parents is not allowed.", "cannotAddParent")
        parent = self._file(parents[0], supports)
        if parent["mimeType"] != FOLDER:
            raise _error(400, "The specified parent is not a folder.", "invalid")
        return parent

    def _create(self, request) -> Response:
        self._auth(request)
        with self.lock:
            body = request.json() or {}
            supports = request.arg("supportsAllDrives") == "true"
            parent = self._check_parent(body.get("parents") or [self.root_id], supports)
            mime = body.get("mimeType") or "application/octet-stream"
            item_id = self._new_id()
            item: dict[str, Any] = {"id": item_id, "name": body.get("name") or "Untitled", "mimeType": mime,
                                    "parents": [parent["id"]], "trashed": False, "modifiedTime": _now(),
                                    "driveId": parent["driveId"]}
            if mime == SHORTCUT:
                target_id = (body.get("shortcutDetails") or {}).get("targetId")
                target = self.items.get(target_id or "")
                if target is None:
                    raise _error(400, "The shortcut target is missing.", "invalid")
                item["shortcutDetails"] = {"targetId": target_id, "targetMimeType": target["mimeType"]}
            elif mime != FOLDER:
                item["data"] = b""
            self.items[item_id] = item
            return self._masked(self._resource(item), request.arg("fields"), "kind,id,name,mimeType")

    def _get(self, request, file_id: str) -> Response:
        self._auth(request)
        with self.lock:
            item = self._file(file_id, request.arg("supportsAllDrives") == "true")
            if request.arg("alt") != "media":
                return self._masked(self._resource(item), request.arg("fields"), "kind,id,name,mimeType")
            if "data" not in item:
                raise _error(403, "Only files with binary content can be downloaded. "
                                  "Use Export with Docs Editors files.", "fileNotDownloadable")
            data = item["data"]
        return file_response(data, request.header("Range"))

    def _export(self, request, file_id: str) -> Response:
        self._auth(request)
        with self.lock:
            item = self._file(file_id, True)
            mime = request.arg("mimeType")
            if not mime:
                raise _error(400, "Required parameter: mimeType", "required")
            allowed = EXPORT_FORMATS.get(item["mimeType"])
            if allowed is None:
                raise _error(403, "Export only supports Docs Editors files.", "fileNotExportable")
            if mime not in allowed:
                raise _error(400, "The requested conversion is not supported.", "badRequest")
            data = item["exports"].get(mime, f"exported {item['id']} as {mime}".encode())
            if len(data) > self.export_limit:
                raise _error(403, "This file is too large to be exported.", "exportSizeLimitExceeded")
        return Response(200, data, {"Content-Type": mime})

    def _update(self, request, file_id: str) -> Response:
        self._auth(request)
        with self.lock:
            supports = request.arg("supportsAllDrives") == "true"
            item = self._file(file_id, supports)
            body = request.json() or {}
            if "parents" in body:
                raise _error(403, "The resource body includes fields which are not directly writable.",
                             "fieldNotWritable")
            parents = list(item["parents"])
            remove = [p for p in (request.arg("removeParents") or "").split(",") if p]
            add = [p for p in (request.arg("addParents") or "").split(",") if p]
            for parent_id in remove:
                if parent_id not in parents:
                    raise _error(400, "The parent to remove is not a parent of the file.", "invalid")
                parents.remove(parent_id)
            for parent_id in add:
                parent = self._file(parent_id, supports)
                if parent["mimeType"] != FOLDER:
                    raise _error(400, "The specified parent is not a folder.", "invalid")
                if parent["driveId"] != item["driveId"]:
                    raise _error(403, "Moving between drives is not supported here.", "invalid")
                walk: dict[str, Any] | None = parent
                while walk is not None:  # no cycles
                    if walk["id"] == item["id"]:
                        raise _error(400, "A folder cannot be moved into itself.", "invalid")
                    walk = self.items.get(walk["parents"][0]) if walk["parents"] else None
                parents.append(parent_id)
            if len(parents) != 1:
                raise _error(403, "Increasing the number of parents is not allowed.", "cannotAddParent")
            item["parents"] = parents
            if "name" in body:
                item["name"] = body["name"]
            if "trashed" in body:
                item["trashed"] = bool(body["trashed"])
            return self._masked(self._resource(item), request.arg("fields"), "kind,id,name,mimeType")

    def _copy(self, request, file_id: str) -> Response:
        self._auth(request)
        with self.lock:
            supports = request.arg("supportsAllDrives") == "true"
            item = self._file(file_id, supports)
            if item["mimeType"] in (FOLDER, SHORTCUT):
                raise _error(403, "This file cannot be copied by the user.", "cannotCopyFile")
            body = request.json() or {}
            parent = self._check_parent(body.get("parents") or item["parents"], supports)
            new = {key: value for key, value in item.items() if key not in ("exports", "data")}
            new.update(id=self._new_id(), name=body.get("name") or f"Copy of {item['name']}",
                       parents=[parent["id"]], trashed=False, modifiedTime=_now(), driveId=parent["driveId"])
            if "data" in item:
                new["data"] = item["data"]
            if "exports" in item:
                new["exports"] = dict(item["exports"])
            self.items[new["id"]] = new
            return self._masked(self._resource(new), request.arg("fields"), "kind,id,name,mimeType")

    def _drives(self, request) -> Response:
        self._auth(request)
        with self.lock:
            size = int(request.arg("pageSize", "10"))
            if not 1 <= size <= 100:
                raise _error(400, "Invalid value for pageSize", "invalid")
            drives = sorted(self.drives.items())
            token = request.arg("pageToken")
            offset = self.page_tokens[token][1] if token in self.page_tokens else 0
            body: dict[str, Any] = {"kind": "drive#driveList",
                                    "drives": [{"kind": "drive#drive", "id": drive_id, "name": name}
                                               for drive_id, name in drives[offset:offset + size]]}
            if offset + size < len(drives):
                next_token = secrets.token_urlsafe(9)
                self.page_tokens[next_token] = ("drives", offset + size)
                body["nextPageToken"] = next_token
            return self._masked(body, request.arg("fields"), "kind,nextPageToken,drives(kind,id,name)")

    # -- resumable uploads ------------------------------------------------------------------------------

    def _start_upload(self, request) -> Response:
        self._auth(request)
        with self.lock:
            if request.arg("uploadType") != "resumable":
                raise _error(400, "Invalid upload type.", "invalid")
            supports = request.arg("supportsAllDrives") == "true"
            metadata = request.json() or {}
            total = request.header("X-Upload-Content-Length")
            session: dict[str, Any] = {"data": bytearray(), "total": int(total) if total is not None else None,
                                       "fields": request.arg("fields"), "metadata": metadata, "done": None,
                                       "content_type": request.header("X-Upload-Content-Type")}
            m = re.fullmatch(r"/upload/drive/v3/files/([^/]+)", request.path)
            if request.method == "PATCH" and m:
                item = self._file(urllib.parse.unquote(m.group(1)), supports)
                if "data" not in item:
                    raise _error(403, "This file's content can't be replaced.", "invalid")
                session["file_id"] = item["id"]
            elif request.method == "POST" and request.path == "/upload/drive/v3/files":
                session["parent"] = self._check_parent(metadata.get("parents") or [self.root_id], supports)["id"]
            else:
                raise _error(404, "Not Found", "notFound")
            upload_id = secrets.token_urlsafe(18)
            self.sessions[upload_id] = session
            location = f"{self.url}{request.path}?uploadType=resumable&upload_id={upload_id}"
            return Response(200, b"", {"Location": location})

    def _finish(self, session: dict[str, Any]) -> dict[str, Any]:
        if "file_id" in session:
            item = self.items[session["file_id"]]
            item["data"] = bytes(session["data"])
            item["modifiedTime"] = _now()
        else:
            parent = self.items[session["parent"]]
            meta = session["metadata"]
            item = {"id": self._new_id(), "name": meta.get("name") or "Untitled",
                    "mimeType": meta.get("mimeType") or session["content_type"] or "application/octet-stream",
                    "parents": [parent["id"]], "trashed": False, "modifiedTime": _now(),
                    "driveId": parent["driveId"], "data": bytes(session["data"])}
            self.items[item["id"]] = item
        session["done"] = item
        return item

    def _session_put(self, request) -> Response:
        with self.lock:
            session = self.sessions.get(request.arg("upload_id") or "")
            if session is None:
                raise _error(404, "Upload session not found.", "notFound")
            if self.fail_puts > 0:
                self.fail_puts -= 1
                raise _error(503, "Backend Error", "backendError")
            content_range = request.header("Content-Range") or ""
            m = re.fullmatch(r"bytes (\*|(\d+)-(\d+))/(\d+)", content_range)
            if not m:
                raise _error(400, "Invalid Content-Range.", "invalid")
            total = int(m.group(4))
            if session["total"] is not None and session["total"] != total:
                raise _error(400, "The total size doesn't match X-Upload-Content-Length.", "invalid")
            session["total"] = total
            data: bytearray = session["data"]
            if session["done"] is not None:
                return self._masked(self._resource(session["done"]), session["fields"],
                                    "kind,id,name,mimeType")
            if m.group(1) == "*":
                if request.body:
                    raise _error(400, "A status query has no body.", "invalid")
                if len(data) == total:
                    item = self._finish(session)
                    return self._masked(self._resource(item), session["fields"], "kind,id,name,mimeType")
                return Response(308, b"", {"Range": f"bytes=0-{len(data) - 1}"} if data else {})
            first, last = int(m.group(2)), int(m.group(3))
            if first != len(data):
                raise _error(400, f"The upload offset is {first} byte(s), but {len(data)} were persisted.", "invalid")
            if last - first + 1 != len(request.body) or last >= total:
                raise _error(400, "The body doesn't match Content-Range.", "invalid")
            final = last + 1 == total
            if not final and len(request.body) % ALIGN:
                raise _error(400, "Chunks before the last must be multiples of 256 KiB.", "invalid")
            body = request.body
            if self.short_puts > 0 and len(body) > ALIGN:
                self.short_puts -= 1
                body = body[:ALIGN]
            data.extend(body)
            if len(data) == total:
                item = self._finish(session)
                return self._masked(self._resource(item), session["fields"], "kind,id,name,mimeType")
            return Response(308, b"", {"Range": f"bytes=0-{len(data) - 1}"})

