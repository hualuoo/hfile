"""An in-memory fake of Microsoft Graph's drive API and of the identity platform's token endpoint.

It follows Graph's documented behaviour where the driver depends on it: bearer tokens that
expire, client secrets, single-use authorisation codes, refresh tokens rotated on every use,
path addressing (".../root:/a/b:/children", segments percent-encoded), case-insensitive names,
conflictBehavior, @odata.nextLink paging, pre-authenticated download URLs (in the item, and as
the 302 of GET .../content), upload sessions (Content-Range, 320 KiB multiples, no Authorization
header allowed), asynchronous copy with a monitor URL that takes no token, search over names and
contents, SharePoint sites and drives, and the error format {"error": {"code", "message"}}.

Knobs for tests: page_size, throttle (next N API calls answer 429), fail_chunks (next N chunk
PUTs answer chunk_failure: 503, or 429 with a Retry-After), copy_polls and monitor_style, search_without_path (OneDrive for Business
leaves parentReference.path out of search results), auto_parents, patch_default,
select_download_url (False: $select answers leave @microsoft.graph.downloadUrl out, as Graph
does for some accounts), content_style, content_location and fail_content (GET .../content; the
Range headers it got are in content_ranges), size_delta (items say their files are that much bigger
than their content, as SharePoint's and OneDrive's sometimes do) and session_conflicts (next N
createUploadSession answer 409, as one does while an earlier session holds the name).

GET /<tenant>/oauth2/v2.0/authorize is a consent page that consents at once: it sends the browser
back to the redirect URI with a code (or, with deny_consent, with the error of a declined consent),
so a site using the fake can be authorised in a browser.
"""

from __future__ import annotations

import datetime
import itertools
import re
import secrets
import threading
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

CHUNK_UNIT = 320 * 1024
FORBIDDEN = set('"*:<>?/\\|')
SITE_ID = "contoso.sharepoint.com,11111111-2222-3333-4444-555555555555,66666666-7777-8888-9999-000000000000"
OTHER_SITE_ID = "contoso.sharepoint.com,aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee,ffffffff-0000-1111-2222-333333333333"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _graph_error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> HTTPError:
    return HTTPError(status, {"error": {"code": code, "message": message, "innerError": {"request-id": "x"}}}, headers)


class Item:
    _ids = itertools.count(1)

    def __init__(self, name: str, parent: Item | None, folder: bool, data: bytes = b""):
        self.id = f"ITEM{next(self._ids):06d}"
        self.name = name
        self.parent = parent
        self.folder = folder
        self.data = data
        self.children: dict[str, Item] = {}
        self.modified = _now()
        self.version = 1
        self.remote = False  # an item shared from another drive ("Add to my OneDrive")

    def path(self) -> str:
        parts = []
        item = self
        while item.parent is not None:
            parts.append(item.name)
            item = item.parent
        return "/" + "/".join(reversed(parts))

    def walk(self):
        yield self
        for child in list(self.children.values()):
            yield from child.walk()


class Drive:
    def __init__(self, drive_id: str, name: str):
        self.id = drive_id
        self.name = name
        self.root = Item("root", None, True)
        self.items: dict[str, Item] = {self.root.id: self.root}

    def add(self, parent: Item, name: str, folder: bool, data: bytes = b"") -> Item:
        item = Item(name, parent, folder, data)
        parent.children[name.casefold()] = item
        self.items[item.id] = item
        return item

    def remove(self, item: Item) -> None:
        if item.parent is not None:
            item.parent.children.pop(item.name.casefold(), None)
        for gone in item.walk():
            self.items.pop(gone.id, None)

    def find(self, path: str) -> Item | None:
        item = self.root
        for part in [p for p in path.split("/") if p]:
            if not item.folder:
                return None
            item = item.children.get(part.casefold())
            if item is None:
                return None
        return item


class FakeGraph:
    def __init__(self, *, client_id: str = "app-id", client_secret: str = "app-secret", tenant: str = "common",
                 page_size: int = 1000):
        self.lock = threading.RLock()
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenants = {"common", "organizations", tenant}
        self.page_size = page_size
        self.token_lifetime = 3600
        self.access_tokens: dict[str, datetime.datetime] = {}
        self.refresh_tokens: set[str] = set()
        self.codes: dict[str, str] = {}  # code -> redirect_uri
        self.drives = {
            "me": Drive("b!me-drive-0001", "OneDrive"),
            "docs": Drive("b!site-docs-0001", "Documents"),
            "archive": Drive("b!site-archive-0001", "Archive"),
        }
        self.sites = {
            SITE_ID: {"displayName": "Team", "path": "/sites/team", "drives": ["docs", "archive"]},
            OTHER_SITE_ID: {"displayName": "Root", "path": "/", "drives": []},
        }
        self.sessions: dict[str, dict[str, Any]] = {}
        self.jobs: dict[str, dict[str, Any]] = {}
        self.downloads: dict[str, tuple[Drive, str, datetime.datetime]] = {}
        # knobs
        self.throttle = 0
        self.throttle_after = "0"
        self.fail_chunks = 0
        self.chunk_failure: tuple[int, str | None] = (503, None)  # (status, Retry-After) of those
        self.size_delta = 0
        self.session_conflicts = 0
        self.content_ranges: list[str | None] = []
        self.copy_polls = 1
        self.monitor_style = "303"  # or "200" (status JSON) or "item" (the new item itself)
        self.search_without_path = False
        self.search_without_parent = False  # results without parentReference at all
        self.lose_chunk_answers = 0  # next N chunk PUTs are stored, then the connection drops
        self.lose_final_answers = 0  # the same for the last chunk: the file is complete, no answer
        self.drop_sessions = 0  # next N chunk PUTs find their session gone (404), as SharePoint's sometimes are
        self.throttle_downloads = 0  # next N GETs of download links answer 429 (Retry-After: throttle_after)
        self.discard_final = False  # ... and with this, the session ends without storing the file
        self.auto_parents = True
        self.patch_default = "replace"  # what PATCH does on a name conflict without conflictBehavior
        self.fail_next: list[tuple[int, str, str]] = []  # errors for the next API calls
        self.copy_error: tuple[str, str] | None = None  # (code, message) a copy job ends with
        self.next_link_base: str | None = None  # another origin for @odata.nextLink
        self.throttle_tokens = 0  # next N token requests answer 429
        self.users = {"alice@contoso.onmicrosoft.com"}  # /users/<user>/drive is the "me" drive
        self.app_tokens = 0  # client-credentials tokens given
        self.select_download_url = True  # False: never in $select answers (some real accounts)
        self.content_style = "302"  # GET .../content: "302" to the item's link, "200" the bytes themselves
        self.content_location: str | None = None  # a Location of its own for that 302
        self.fail_content: list[tuple[int, str, str]] = []  # errors for the next GET .../content
        self.deny_consent = False  # the consent page answers access_denied instead of a code
        self.requests: list[tuple[str, str, bool]] = []
        self.server = FakeServer(self.handle).start()
        self.url = self.server.url

    def close(self) -> None:
        self.server.close()

    # -- test helpers

    def issue_refresh_token(self) -> str:
        with self.lock:
            token = "rt-" + secrets.token_urlsafe(12)
            self.refresh_tokens.add(token)
            return token

    def grant_code(self, redirect_uri: str) -> str:
        """What the provider sends to the callback after the user consented."""
        with self.lock:
            code = "code-" + secrets.token_urlsafe(12)
            self.codes[code] = redirect_uri
            return code

    def expire_access_tokens(self) -> None:
        with self.lock:
            self.access_tokens.clear()

    def drive(self, key: str = "me") -> Drive:
        return self.drives[key]

    # -- dispatch

    def handle(self, req: Request) -> Response:
        with self.lock:
            self.requests.append((req.method, req.path, req.header("Authorization") is not None))
        path = req.path
        if path.endswith("/oauth2/v2.0/token"):
            return self._token(req)
        if path.endswith("/oauth2/v2.0/authorize") and req.method == "GET":
            return self._authorize(req)
        if path.startswith("/download/"):
            return self._download(req)
        if path.startswith("/upload/"):
            return self._upload(req)
        if path.startswith("/monitor/"):
            return self._monitor(req)
        if not path.startswith("/v1.0/"):
            raise HTTPError(404, b"no such endpoint")
        with self.lock:
            self._authenticate(req)
            if self.fail_next:
                status, code, message = self.fail_next.pop(0)
                raise _graph_error(status, code, message)
            if self.throttle > 0:
                self.throttle -= 1
                raise _graph_error(429, "activityLimitReached", "The request has been throttled",
                                   {"Retry-After": self.throttle_after})
            return self._api(req, path[len("/v1.0"):])

    # -- identity platform

    def _authorize(self, req: Request) -> Response:
        """The consent page, consenting at once: a 302 to the redirect URI with a code and the state.
        Like the identity platform, a request it can't send back (an unknown tenant or app, no
        authorization code flow, no web redirect URI) gets an error page instead."""
        tenant = urllib.parse.unquote(req.path.split("/")[1])
        redirect = req.arg("redirect_uri") or ""
        if (tenant not in self.tenants or req.arg("client_id") != self.client_id or req.arg("response_type") != "code"
                or not redirect.startswith(("http://", "https://"))
                or "offline_access" not in (req.arg("scope") or "").split()):
            raise HTTPError(400, b"AADSTS900144: the authorize request is not valid.")
        if self.deny_consent:
            answer = {"error": "access_denied", "error_description": "AADSTS65004: User declined to consent to access the app."}
        else:
            answer = {"code": self.grant_code(redirect)}
        answer["state"] = req.arg("state") or ""
        location = redirect + ("&" if "?" in redirect else "?") + urllib.parse.urlencode(answer)
        return Response(302, b"", {"Location": location, "Cache-Control": "no-store"})

    def _token(self, req: Request) -> Response:
        tenant = urllib.parse.unquote(req.path.split("/")[1])
        form = req.form()
        with self.lock:
            if self.throttle_tokens > 0:
                self.throttle_tokens -= 1
                raise HTTPError(429, {"error": "temporarily_unavailable", "error_description": "AADSTS50196"},
                                {"Retry-After": "1"})
            if tenant not in self.tenants:
                raise HTTPError(400, {"error": "invalid_request",
                                      "error_description": f"AADSTS90002: Tenant '{tenant}' not found.\r\nTrace ID: t"})
            if form.get("client_id") != self.client_id:
                raise HTTPError(400, {"error": "unauthorized_client", "error_description":
                                      f"AADSTS700016: Application '{form.get('client_id')}' was not found."})
            if form.get("client_secret") != self.client_secret:
                raise HTTPError(401, {"error": "invalid_client", "error_description":
                                      "AADSTS7000215: Invalid client secret provided.\r\nTrace ID: t\r\n"
                                      "Correlation ID: c"})
            grant = form.get("grant_type")
            if grant == "client_credentials":  # the app's own access: one scope, the API's .default
                if not (form.get("scope") or "").endswith("/.default") or tenant in ("common", "organizations"):
                    raise HTTPError(400, {"error": "invalid_scope", "error_description": "AADSTS1002012: scope"})
                access = "at-" + secrets.token_urlsafe(16)
                self.access_tokens[access] = _now() + datetime.timedelta(seconds=self.token_lifetime)
                self.app_tokens += 1
                return json_response({"token_type": "Bearer", "expires_in": self.token_lifetime, "access_token": access})
            if "offline_access" not in (form.get("scope") or "").split():
                raise HTTPError(400, {"error": "invalid_scope", "error_description": "AADSTS70011: scope"})
            if grant == "authorization_code":
                redirect = self.codes.pop(form.get("code") or "", None)
                if redirect is None or redirect != form.get("redirect_uri"):
                    raise HTTPError(400, {"error": "invalid_grant", "error_description":
                                          "AADSTS70000: The provided value for the 'code' parameter is not valid."})
            elif grant == "refresh_token":
                if form.get("refresh_token") not in self.refresh_tokens:
                    raise HTTPError(400, {"error": "invalid_grant", "error_description":
                                          "AADSTS70000: The refresh token has expired or is invalid.\r\nTrace ID: t"})
            else:
                raise HTTPError(400, {"error": "unsupported_grant_type"})
            access = "at-" + secrets.token_urlsafe(16)
            self.access_tokens[access] = _now() + datetime.timedelta(seconds=self.token_lifetime)
            refresh = self.issue_refresh_token()  # Microsoft hands out a new one each time
            return json_response({"token_type": "Bearer", "scope": form.get("scope"), "expires_in": self.token_lifetime,
                                  "access_token": access, "refresh_token": refresh})

    def _authenticate(self, req: Request) -> None:
        token = req.bearer()
        expires = self.access_tokens.get(token or "")
        if expires is None or expires < _now():
            raise _graph_error(401, "InvalidAuthenticationToken", "Access token has expired or is not yet valid.")

    # -- Graph

    def _api(self, req: Request, path: str) -> Response:
        if path == "/sites" or path.startswith("/sites/"):
            site_answer = self._sites(req, path)
            if site_answer is not None:
                return site_answer
        drive, rest, prefix = self._drive_of(path)
        return self._drive_api(req, drive, rest, prefix)

    def _drive_of(self, path: str) -> tuple[Drive, str, str]:
        if path.startswith("/me/drive"):
            return self.drives["me"], path[len("/me/drive"):], "/me/drive"
        match = re.match(r"/users/([^/]+)/drive(?=/|$)", path)
        if match:  # with the app's own access: the drive of a user of the tenant
            if urllib.parse.unquote(match.group(1)) not in self.users:
                raise _graph_error(404, "itemNotFound", "The user could not be found.")
            return self.drives["me"], path[match.end():], match.group(0)
        match = re.match(r"/drives/([^/]+)", path)
        if match:
            drive_id = urllib.parse.unquote(match.group(1))
            for drive in self.drives.values():
                if drive.id == drive_id:
                    return drive, path[match.end():], match.group(0)
            raise _graph_error(404, "itemNotFound", "The drive could not be found.")
        match = re.match(r"/sites/([^/:]+)/drive(?=/|$)", path)
        if match:
            site = self.sites.get(urllib.parse.unquote(match.group(1)))
            if site is None or not site["drives"]:
                raise _graph_error(404, "itemNotFound", "Requested site could not be found")
            return self.drives[site["drives"][0]], path[match.end():], match.group(0)
        raise _graph_error(400, "BadRequest", f"Unsupported path {path}")

    def _site_json(self, site_id: str) -> dict[str, Any]:
        site = self.sites[site_id]
        return {"id": site_id, "displayName": site["displayName"],
                "webUrl": "https://contoso.sharepoint.com" + ("" if site["path"] == "/" else site["path"])}

    def _sites(self, req: Request, path: str) -> Response | None:
        if path == "/sites" and req.method == "GET":
            if req.arg("search") is None:
                raise _graph_error(400, "invalidRequest", "search is required")
            return json_response({"value": [self._site_json(site) for site in self.sites]})
        if path == "/sites/root":
            return json_response(self._site_json(OTHER_SITE_ID))
        match = re.fullmatch(r"/sites/([A-Za-z0-9.-]+):(/.*)?", path)
        if match:
            relative = urllib.parse.unquote(match.group(2) or "/")
            for site_id, site in self.sites.items():
                if site["path"] == relative:
                    return json_response(self._site_json(site_id))
            raise _graph_error(404, "itemNotFound", "Requested site could not be found")
        match = re.fullmatch(r"/sites/([^/:]+)(/drives)?", path)
        if match:
            site_id = urllib.parse.unquote(match.group(1))
            if site_id not in self.sites:
                raise _graph_error(404, "itemNotFound", "Requested site could not be found")
            if match.group(2):
                return json_response({"value": [{"id": self.drives[key].id, "name": self.drives[key].name,
                                                 "driveType": "documentLibrary"}
                                                for key in self.sites[site_id]["drives"]]})
            return json_response(self._site_json(site_id))
        return None

    def _item_json(self, drive: Drive, item: Item, req: Request | None = None) -> dict[str, Any]:
        select = (req.arg("$select") if req is not None else None) or ""
        answer: dict[str, Any] = {
            "id": item.id,
            "name": item.name,
            "size": sum(len(i.data) for i in item.walk()) if item.folder else len(item.data) + self.size_delta,
            "lastModifiedDateTime": item.modified.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "eTag": f'"{{{item.id}}},{item.version}"',
        }
        if item.parent is None:
            answer["root"] = {}
            answer["parentReference"] = {"driveId": drive.id, "driveType": "business"}
        else:
            reference = {"driveId": drive.id, "id": item.parent.id}
            parent_path = item.parent.path()
            reference["path"] = "/drive/root:" + ("" if parent_path == "/" else urllib.parse.quote(parent_path))
            answer["parentReference"] = reference
        if item.remote:
            answer["remoteItem"] = {"id": "REMOTE" + item.id, "parentReference": {"driveId": "b!other-drive"},
                                    "folder": {"childCount": 1}}
        if item.folder:
            answer["folder"] = {"childCount": len(item.children)}
        else:
            answer["file"] = {"mimeType": "application/octet-stream"}
            if not select or ("@microsoft.graph.downloadUrl" in select.split(",") and self.select_download_url):
                answer["@microsoft.graph.downloadUrl"] = self._download_link(drive, item)
        if select:  # like Graph: only what was asked for
            wanted = set(select.split(","))
            answer = {key: value for key, value in answer.items() if key in wanted}
        return answer

    def _resolve(self, drive: Drive, rest: str) -> tuple[Item | None, str, str, str]:
        """(item or None, suffix such as "/children", drive path, item id or "") of the rest of a URL."""
        if rest.startswith("/items/"):
            item_id, _, suffix = rest[len("/items/"):].partition("/")
            item_id = urllib.parse.unquote(item_id)
            item = drive.items.get(item_id)
            return item, ("/" + suffix if suffix else ""), item.path() if item else "", item_id
        if rest == "/root" or rest.startswith("/root/"):
            return drive.root, rest[len("/root"):], "/", drive.root.id
        if rest.startswith("/root:"):
            raw, _, suffix = rest[len("/root:"):].partition(":")
            if not raw.startswith("/"):
                raise _graph_error(400, "invalidRequest", "Invalid path")
            parts = [urllib.parse.unquote(part, errors="strict") for part in raw.split("/") if part]
            for part in parts:
                if part in (".", "..") or any(ch in FORBIDDEN for ch in part):
                    raise _graph_error(400, "invalidRequest", "The path is malformed")
            path = "/" + "/".join(parts)
            return drive.find(path), suffix, path, ""
        raise _graph_error(400, "BadRequest", f"Unsupported drive path {rest}")

    def _check_name(self, name: Any) -> str:
        if not isinstance(name, str) or not name or name != name.strip(" ") or any(ch in FORBIDDEN for ch in name):
            raise _graph_error(400, "invalidRequest", "The name contains invalid characters or spaces.")
        if name.casefold() in ("con", "prn", "aux", "nul", "desktop.ini", ".lock") or name.startswith("~$"):
            raise _graph_error(400, "invalidRequest", "The name is reserved.")
        return name

    def _parent_for(self, drive: Drive, path: str) -> Item:
        """The folder a new item at `path` goes into; created on the way like Graph does."""
        parent = drive.root
        for part in [p for p in path.split("/") if p][:-1]:
            child = parent.children.get(part.casefold())
            if child is None:
                if not self.auto_parents:
                    raise _graph_error(404, "itemNotFound", "The parent folder doesn't exist.")
                child = drive.add(parent, self._check_name(part), True)
            if not child.folder:
                raise _graph_error(409, "nameAlreadyExists", "A file is in the way.")
            parent = child
        return parent

    def _drive_api(self, req: Request, drive: Drive, rest: str, prefix: str) -> Response:
        search = re.fullmatch(r"/root/search\(q='(.*)'\)", urllib.parse.unquote(rest))
        if search and req.method == "GET":
            return self._search(req, drive, prefix, search.group(1).replace("''", "'"))
        item, suffix, path, _ = self._resolve(drive, rest)
        method = req.method
        if suffix == "" and method == "GET":
            if item is None:
                raise _graph_error(404, "itemNotFound", "The resource could not be found.")
            return json_response(self._item_json(drive, item, req))
        if suffix == "/children" and method == "GET":
            if item is None:
                raise _graph_error(404, "itemNotFound", "The resource could not be found.")
            children = sorted(item.children.values(), key=lambda i: i.name) if item.folder else []
            top = min(int(req.arg("$top") or 200), self.page_size)
            start = int(req.arg("$skiptoken") or 0)
            page = children[start:start + top]
            answer: dict[str, Any] = {"value": [self._item_json(drive, child, req) for child in page]}
            if start + top < len(children):
                select = req.arg("$select") or ""
                answer["@odata.nextLink"] = (f"{self.next_link_base or self.url}/v1.0{prefix}/items/{item.id}/children?"
                                             f"$select={select}&$top={top}&$skiptoken={start + top}")
            return json_response(answer)
        if suffix == "/children" and method == "POST":
            if item is None:
                raise _graph_error(404, "itemNotFound", "The parent folder doesn't exist.")
            if not item.folder:
                raise _graph_error(400, "invalidRequest", "The parent is not a folder.")
            body = req.json() or {}
            name = self._check_name(body.get("name"))
            if "folder" not in body:
                raise _graph_error(400, "invalidRequest", "folder facet missing")
            behavior = body.get("@microsoft.graph.conflictBehavior", "fail")
            existing = item.children.get(name.casefold())
            if existing is not None:
                if behavior == "fail":
                    raise _graph_error(409, "nameAlreadyExists", "Name already exists")
                drive.remove(existing)
            created = drive.add(item, name, True)
            return json_response(self._item_json(drive, created), 201)
        if suffix == "/content" and method == "PUT":
            return self._put_content(req, drive, item, path)
        if suffix == "/content" and method == "GET":
            self.content_ranges.append(req.header("Range"))
            if self.fail_content:
                status, code, message = self.fail_content.pop(0)
                raise _graph_error(status, code, message)
            if item is None:
                raise _graph_error(404, "itemNotFound", "The resource could not be found.")
            if item.folder:
                raise _graph_error(400, "invalidRequest", "A folder has no content to download.")
            if self.content_style == "200":
                return file_response(item.data, req.header("Range"))
            return Response(302, b"", {"Location": self.content_location or self._download_link(drive, item)})
        if suffix == "/createUploadSession" and method == "POST":
            if self.session_conflicts > 0:
                self.session_conflicts -= 1
                raise _graph_error(409, "nameAlreadyExists", "A file with the same name is currently being uploaded.")
            return self._create_session(req, drive, item, path)
        if suffix == "" and method == "PATCH":
            return self._patch(req, drive, item)
        if suffix == "" and method == "DELETE":
            if item is None:
                raise _graph_error(404, "itemNotFound", "The resource could not be found.")
            if item.parent is None:
                raise _graph_error(403, "accessDenied", "The root can't be deleted.")
            drive.remove(item)
            return Response(204)
        if suffix == "/copy" and method == "POST":
            return self._copy(req, drive, item)
        raise _graph_error(400, "BadRequest", f"Unsupported {method} {rest}")

    def _put_content(self, req: Request, drive: Drive, item: Item | None, path: str) -> Response:
        if req.header("Content-Length") is None:
            raise _graph_error(411, "lengthRequired", "Content-Length is required")
        behavior = req.arg("@microsoft.graph.conflictBehavior") or "replace"
        if item is not None:
            if item.folder:
                raise _graph_error(409, "nameAlreadyExists", "A folder with this name exists")
            if behavior == "fail":
                raise _graph_error(409, "nameAlreadyExists", "Name already exists")
            item.data = req.body
            item.modified = _now()
            item.version += 1
            return json_response(self._item_json(drive, item), 200)
        name = self._check_name(path.rsplit("/", 1)[-1])
        parent = self._parent_for(drive, path)
        created = drive.add(parent, name, False, req.body)
        return json_response(self._item_json(drive, created), 201)

    def _create_session(self, req: Request, drive: Drive, item: Item | None, path: str) -> Response:
        body = req.json() or {}
        behavior = (body.get("item") or {}).get("@microsoft.graph.conflictBehavior", "replace")
        if item is not None and item.folder:
            raise _graph_error(409, "nameAlreadyExists", "A folder with this name exists")
        if item is not None and behavior == "fail":
            raise _graph_error(409, "nameAlreadyExists", "Name already exists")
        self._check_name(path.rsplit("/", 1)[-1])
        self._parent_for(drive, path)
        session_id = secrets.token_urlsafe(12)
        self.sessions[session_id] = {"drive": drive, "path": path, "behavior": behavior, "data": bytearray(),
                                     "total": None}
        return json_response({"uploadUrl": f"{self.url}/upload/{session_id}?tempauth=xyz",
                              "expirationDateTime": (_now() + datetime.timedelta(days=1)).isoformat(),
                              "nextExpectedRanges": ["0-"]})

    def _upload(self, req: Request) -> Response:
        session_id = req.path[len("/upload/"):]
        with self.lock:
            if req.header("Authorization") is not None:
                # Graph: "Including the Authorization header when issuing the PUT call may result in a 401".
                raise _graph_error(401, "unauthenticated", "Don't send credentials to the upload URL.")
            session = self.sessions.get(session_id)
            if session is None:
                raise _graph_error(404, "itemNotFound", "The upload session was not found.")
            if req.method == "DELETE":
                del self.sessions[session_id]
                return Response(204)
            if req.method == "GET":
                return json_response({"expirationDateTime": "2099-01-01T00:00:00Z",
                                      "nextExpectedRanges": [f"{len(session['data'])}-"]})
            if req.method != "PUT":
                raise _graph_error(405, "invalidRequest", "PUT the fragments")
            if self.drop_sessions > 0:
                self.drop_sessions -= 1
                del self.sessions[session_id]
                raise _graph_error(404, "itemNotFound", "The upload session was not found.")
            if self.fail_chunks > 0:
                self.fail_chunks -= 1
                status, retry_after = self.chunk_failure
                raise _graph_error(status, "serviceNotAvailable" if status == 503 else "activityLimitReached",
                                   "Try again later", {"Retry-After": retry_after} if retry_after else None)
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", req.header("Content-Range") or "")
            if not match:
                raise _graph_error(400, "invalidRequest", "Content-Range is required")
            start, end, total = map(int, match.groups())
            if session["total"] is None:
                session["total"] = total
            if total != session["total"] or start != len(session["data"]) or end < start or end >= total:
                raise _graph_error(416, "invalidRange", "The fragment doesn't fit the expected ranges.")
            if len(req.body) != end - start + 1 or len(req.body) > 60 * 1024 * 1024:
                raise _graph_error(400, "invalidRequest", "Fragment length mismatch")
            if end + 1 < total and len(req.body) % CHUNK_UNIT:
                raise _graph_error(400, "invalidRange", "Fragments must be multiples of 320 KiB.")
            session["data"] += req.body
            if end + 1 < total:
                if self.lose_chunk_answers > 0:
                    self.lose_chunk_answers -= 1
                    return Response(0)
                return json_response({"expirationDateTime": "2099-01-01T00:00:00Z",
                                      "nextExpectedRanges": [f"{end + 1}-"]}, 202)
            del self.sessions[session_id]
            if self.lose_final_answers > 0 and self.discard_final:
                self.lose_final_answers -= 1
                return Response(0)
            drive: Drive = session["drive"]
            item = drive.find(session["path"])
            if item is not None:
                if item.folder or session["behavior"] == "fail":
                    raise _graph_error(409, "nameAlreadyExists", "Name already exists")
                item.data = bytes(session["data"])
                item.modified = _now()
                item.version += 1
                answer = json_response(self._item_json(drive, item), 200)
            else:
                parent = self._parent_for(drive, session["path"])
                created = drive.add(parent, session["path"].rsplit("/", 1)[-1], False, bytes(session["data"]))
                answer = json_response(self._item_json(drive, created), 201)
            if self.lose_final_answers > 0:
                self.lose_final_answers -= 1
                return Response(0)
            return answer

    def _patch(self, req: Request, drive: Drive, item: Item | None) -> Response:
        if item is None:
            raise _graph_error(404, "itemNotFound", "The resource could not be found.")
        if item.parent is None:
            raise _graph_error(400, "invalidRequest", "The root can't be changed.")
        body = req.json() or {}
        behavior = req.arg("@microsoft.graph.conflictBehavior") or self.patch_default
        name = self._check_name(body["name"]) if "name" in body else item.name
        parent = item.parent
        reference = body.get("parentReference")
        if reference:
            parent = drive.items.get(reference.get("id") or "")
            if parent is None or not parent.folder:
                raise _graph_error(404, "itemNotFound", "The target folder doesn't exist.")
            ancestor = parent
            while ancestor is not None:
                if ancestor is item:
                    raise _graph_error(400, "invalidRequest", "Can't move a folder into itself.")
                ancestor = ancestor.parent
        existing = parent.children.get(name.casefold())
        if existing is not None and existing is not item:
            if behavior == "fail":
                raise _graph_error(409, "nameAlreadyExists", "Name already exists")
            drive.remove(existing)
        item.parent.children.pop(item.name.casefold(), None)
        item.name = name
        item.parent = parent
        parent.children[name.casefold()] = item
        item.modified = _now()
        return json_response(self._item_json(drive, item))

    def _copy(self, req: Request, drive: Drive, item: Item | None) -> Response:
        if item is None:
            raise _graph_error(404, "itemNotFound", "The resource could not be found.")
        body = req.json() or {}
        reference = body.get("parentReference") or {}
        target = drive.items.get(reference.get("id") or "")
        if target is None or not target.folder:
            raise _graph_error(400, "invalidRequest", "parentReference must point to a folder")
        if reference.get("driveId") not in (None, drive.id):
            raise _graph_error(400, "invalidRequest", "other drives aren't supported here")
        job = secrets.token_urlsafe(10)
        self.jobs[job] = {"drive": drive, "item": item, "target": target, "name": body.get("name") or item.name,
                          "behavior": req.arg("@microsoft.graph.conflictBehavior") or "fail",
                          "polls": self.copy_polls, "result": None}
        return Response(202, b"", {"Location": f"{self.url}/monitor/{job}"})

    def _monitor(self, req: Request) -> Response:
        with self.lock:
            if req.header("Authorization") is not None:
                raise HTTPError(400, b"the monitor URL takes no credentials")
            job = self.jobs.get(req.path[len("/monitor/"):])
            if job is None:
                raise _graph_error(404, "itemNotFound", "no such job")
            if job["polls"] > 0:
                job["polls"] -= 1
                return json_response({"status": "inProgress", "percentageComplete": 50.0}, 202)
            if job["result"] is None:
                drive, target, name = job["drive"], job["target"], job["name"]
                if self.copy_error is not None:
                    code, message = self.copy_error
                    job["result"] = {"status": "failed", "error": {"code": code, "message": message}}
                elif target.children.get(name.casefold()) is not None and job["behavior"] == "fail":
                    job["result"] = {"status": "failed",
                                     "error": {"code": "nameAlreadyExists", "message": "Name already exists"}}
                else:
                    copied = self._deep_copy(drive, job["item"], target, name)
                    job["result"] = {"status": "completed", "resourceId": copied.id}
            result = job["result"]
            if result["status"] == "completed" and self.monitor_style == "303":
                location = f"{self.url}/v1.0/drives/{job['drive'].id}/items/{result['resourceId']}"
                return Response(303, b"", {"Location": location})
            if result["status"] == "completed" and self.monitor_style == "item":
                drive = job["drive"]
                return json_response(self._item_json(drive, drive.items[result["resourceId"]]), 200)
            return json_response(result, 200)

    def _deep_copy(self, drive: Drive, item: Item, target: Item, name: str) -> Item:
        copy = drive.add(target, name, item.folder, item.data)
        for child in list(item.children.values()):
            self._deep_copy(drive, child, copy, child.name)
        return copy

    def _search(self, req: Request, drive: Drive, prefix: str, text: str) -> Response:
        needle = text.casefold()
        matches = [item for item in drive.root.walk() if item is not drive.root
                   and (needle in item.name.casefold() or needle.encode() in item.data.lower())]
        matches.sort(key=lambda i: i.path())
        top = min(int(req.arg("$top") or 200), self.page_size)
        start = int(req.arg("$skiptoken") or 0)
        values = []
        for item in matches[start:start + top]:
            answer = self._item_json(drive, item, req)
            if self.search_without_path:
                answer["parentReference"].pop("path", None)
            if self.search_without_parent:
                del answer["parentReference"]
            values.append(answer)
        result: dict[str, Any] = {"value": values}
        if start + top < len(matches):
            quoted = urllib.parse.quote(text.replace("'", "''"), safe="")
            result["@odata.nextLink"] = (f"{self.url}/v1.0{prefix}/root/search(q='{quoted}')?"
                                         f"$top={top}&$skiptoken={start + top}")
        return json_response(result)

    def _download_link(self, drive: Drive, item: Item) -> str:
        """A new pre-authenticated link to the item's bytes, good for an hour (with the lock held)."""
        token = secrets.token_urlsafe(16)
        self.downloads[token] = (drive, item.id, _now() + datetime.timedelta(hours=1))
        return f"{self.url}/download/{token}?tempauth=1"

    def _download(self, req: Request) -> Response:
        token = req.path[len("/download/"):]
        with self.lock:
            if req.header("Authorization") is not None:
                raise HTTPError(400, b"pre-authenticated URL: no Authorization header expected")
            entry = self.downloads.get(token)
            if entry is None or entry[2] < _now() or req.arg("tempauth") is None:
                raise _graph_error(401, "unauthenticated", "The download URL expired")
            if self.throttle_downloads > 0:
                self.throttle_downloads -= 1
                raise _graph_error(429, "activityLimitReached", "The request has been throttled",
                                   {"Retry-After": self.throttle_after})
            drive, item_id, _ = entry
            item = drive.items.get(item_id)
            if item is None:
                raise _graph_error(404, "itemNotFound", "gone")
            data = item.data
        return file_response(data, req.header("Range"),
                             {"Content-Disposition": f"attachment; filename*=UTF-8''{urllib.parse.quote(item.name)}"})
