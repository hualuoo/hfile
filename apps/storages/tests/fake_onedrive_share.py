"""An in-memory fake of what a browser without an account meets when it opens a SharePoint share link ("Anyone with
the link") to a folder (tests only, no Django), for apps.storages.drivers.onedrive_share.

    fake = FakeOneDriveShare().start()            # a folder of OneDrive for work or school (/personal/<user>)
    fake = FakeOneDriveShare(site=True).start()   # a folder of a SharePoint site's library (/sites/Team)
    fake.share_url; fake.add("/a/b.txt", b"..."); fake.close()

The link (as OpenList's driver shows SharePoint behaves): its GET answers 302 with a FedAuth cookie to the folder's
page, <web>/_layouts/15/onedrive.aspx?id=<folder> (OneDrive) or <web>/<library>/Forms/AllItems.aspx?id=<folder>
(a site). With `password`, it answers an ASP.NET form instead; posted back with its __VIEWSTATE, __EVENTVALIDATION
and the right txtPassword, the form answers that 302 and cookie, else it is shown again. `sign_in` sends the link to
a sign-in page on another host (.invalid: never asked), `removed` answers 404, `hops` puts redirects on the link's
host before the folder's page (they want the cookie).

The REST API, with the cookie (documented): POST <web>/_api/contextinfo gives a form digest; POST
<web>/_api/web/GetListUsingPath(DecodedUrl=@a1)/RenderListDataAsStream?@a1='<library>' wants it in X-RequestDigest
(403 without, as without the cookie), lists FolderServerRelativeUrl (403 outside the shared folder, 404 for none)
RowLimit rows a page (from the ViewXml), with a NextHref while there are more; rows as OpenList shows them, with
.spItemUrl unless `item_urls` is off (on another host with `item_url_root`), the data in "ListData" with `wrap`.
`fail` is the status of the next listing.

Downloads: <web>/_layouts/15/download.aspx?UniqueId=<guid> sends the file to the cookie, with the Range and
Content-Disposition (`redirect_downloads`: a 302 to its tempauth link first; `block_downloads`: 403); without the
cookie it sends the browser to sign in; with a valid tempauth it needs none. A row's .spItemUrl
(<host>/_api/v2.0/drives/.../items/<id>) answers the item with its @content.downloadUrl, the tempauth link, to the
cookie (401 without; 403 with `v2_refuses`).

`throttled` ({endpoint: Retry-After}, endpoints "link", "contextinfo", "render", "download") makes each named one
answer 429 once, as SharePoint throttles: https://learn.microsoft.com/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online

To look at: calls (method, path), download_requests ({"cookie", "range", "tempauth"}); expire_sessions() makes
every cookie and digest invalid.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import html
import re
import secrets
import threading
import urllib.parse
import uuid
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

KEY = b"fake-sharepoint-tempauth-key"
SIGN_IN = "https://login.invalid/common/oauth2/authorize"
ACCESS_DENIED = ("-2147024891, System.UnauthorizedAccessException",
                 "Access denied. You do not have permission to perform this action or access this resource.")
NOT_FOUND = ("-2147024894, System.IO.FileNotFoundException", "File Not Found.")
BAD_DIGEST = ("-2130575251, Microsoft.SharePoint.SPException", "The security validation for this page is invalid and "
              "might be corrupted. Please use your web browser's Back button to try your operation again.")


def odata_error(status: int, error: tuple[str, str]) -> HTTPError:
    code, message = error
    return HTTPError(status, {"odata.error": {"code": code, "message": {"lang": "en-US", "value": message}}})


def html_page(status: int, text: str) -> Response:
    return Response(status, f"<!DOCTYPE html><html><body>{text}</body></html>".encode(),
                    {"Content-Type": "text/html; charset=utf-8"})


class FakeOneDriveShare:
    def __init__(self, *, site: bool = False):
        self.site = site
        if site:
            self.web_path = "/sites/Team"
            self.library = "/sites/Team/Shared Documents"
            self.link_path = "/:f:/s/Team/EaFolderShareToken"
        else:
            self.web_path = "/personal/user_contoso_com"
            self.library = "/personal/user_contoso_com/Documents"
            self.link_path = "/:f:/g/personal/user_contoso_com/EaFolderShareToken"
        self.shared = self.library + "/Shared Folder"
        self.lock = threading.Lock()
        self.server = FakeServer(self.handle)
        self.reset()

    def start(self) -> FakeOneDriveShare:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    @property
    def share_url(self) -> str:
        return self.url + self.link_path + "?e=AbCdEf"

    def reset(self) -> None:
        with self.lock:
            self.nodes: dict[str, dict[str, Any]] = {}  # server-relative path -> node
            self.next_id = 1
            self._folder(self.shared)
            self._put(self.library + "/Other/not shared.txt", b"not shared")
            self.password: str | None = None
            self.sign_in = False
            self.removed = False
            self.hops = 0
            self.redirect_downloads = False
            self.block_downloads = False
            self.v2_refuses = False
            self.item_urls = True
            self.item_url_root: str | None = None
            self.wrap = False
            self.fail: int | None = None
            self.throttled: dict[str, str] = {}
            self.sessions: set[str] = set()
            self.digests: set[str] = set()
            self.viewstate = secrets.token_urlsafe(24)
            self.validation = secrets.token_urlsafe(16)
            self.calls: list[tuple[str, str]] = []
            self.download_requests: list[dict[str, Any]] = []

    def expire_sessions(self) -> None:
        with self.lock:
            self.sessions.clear()
            self.digests.clear()

    # -- the files

    def add(self, path: str, data: bytes | None = None) -> None:
        """A file (or with data None a folder) at `path` in the shared folder, with the folders above it."""
        with self.lock:
            if data is None:
                self._folder(self.shared + path)
            else:
                self._put(self.shared + path, data)

    def load(self, tree: dict[str, bytes], empty: str | None = None) -> None:
        for path, data in tree.items():
            self.add(path, data)
        if empty:
            self.add(empty)

    def _node(self, is_dir: bool, data: bytes = b"") -> dict[str, Any]:
        node = {"dir": is_dir, "data": data, "id": self.next_id, "guid": str(uuid.uuid4()),
                "modified": datetime.datetime(2026, 11, 17, 8, 30, self.next_id % 60, tzinfo=datetime.timezone.utc)}
        self.next_id += 1
        return node

    def _folder(self, path: str) -> None:
        if path in self.nodes:
            return
        if path != self.library:
            self._folder(path.rsplit("/", 1)[0])
        self.nodes[path] = self._node(True)

    def _put(self, path: str, data: bytes) -> None:
        self._folder(path.rsplit("/", 1)[0])
        self.nodes[path] = self._node(False, data)

    def _inside(self, path: str) -> bool:
        return path.lower() == self.shared.lower() or path.lower().startswith(self.shared.lower() + "/")

    def _by_guid(self, guid: str) -> str | None:
        return next((path for path, node in self.nodes.items() if node["guid"] == guid.lower()), None)

    @staticmethod
    def _tempauth(guid: str) -> str:
        return hmac.new(KEY, guid.encode(), hashlib.sha256).hexdigest()

    def _tempauth_url(self, path: str) -> str:
        guid = self.nodes[path]["guid"]
        return (f"{self.url}{self.web_path}/_layouts/15/download.aspx?UniqueId={guid}&Translate=false"
                f"&tempauth={self._tempauth(guid)}&ApiVersion=2.0")

    # -- requests

    def _throttle(self, endpoint: str) -> None:
        wait = self.throttled.pop(endpoint, None)
        if wait is not None:
            raise HTTPError(429, {"odata.error": {"code": "-2147024860, Microsoft.SharePoint.SPQueryThrottledException",
                                                  "message": {"value": "The request has been throttled."}}},
                            {"Retry-After": wait})

    def handle(self, req) -> Response:
        path = urllib.parse.unquote(req.path)
        with self.lock:
            self.calls.append((req.method, path))
            if path == self.link_path and req.method == "GET":
                self._throttle("link")
                return self._link()
            if path == self.web_path + "/_layouts/15/hop.aspx" and req.method == "GET":
                return self._hop(req)
            if path == self.web_path + "/_layouts/15/guestaccess.aspx" and req.method == "POST":
                return self._password(req)
            if path == self.web_path + "/_api/contextinfo" and req.method == "POST":
                self._throttle("contextinfo")
                return self._contextinfo(req)
            if path == self.web_path + "/_api/web/GetListUsingPath(DecodedUrl=@a1)/RenderListDataAsStream" and req.method == "POST":
                self._throttle("render")
                return self._render(req)
            if path == self.web_path + "/_layouts/15/download.aspx" and req.method == "GET":
                self._throttle("download")
                return self._download(req)
            if path.startswith("/_api/v2.0/drives/") and req.method == "GET":
                return self._drive_item(req, path)
        raise HTTPError(404, b"404 NOT FOUND")

    def _session(self, req) -> bool:
        cookies = dict(part.strip().partition("=")[::2] for part in (req.header("Cookie") or "").split(";") if "=" in part)
        return cookies.get("FedAuth") in self.sessions

    def _folder_page(self) -> str:
        folder = urllib.parse.quote(self.shared, safe="")
        if self.site:
            return f"{self.url}{urllib.parse.quote(self.library)}/Forms/AllItems.aspx?id={folder}&p=true&ga=1"
        return f"{self.url}{self.web_path}/_layouts/15/onedrive.aspx?id={folder}&ga=1"

    def _to_folder(self) -> Response:
        """The 302 that opens the folder (through `hops` first), with a new session's cookie."""
        token = secrets.token_hex(16)
        self.sessions.add(token)
        location = f"{self.web_path}/_layouts/15/hop.aspx?n={self.hops}" if self.hops else self._folder_page()
        return Response(302, headers=[("Location", location), ("Set-Cookie", f"FedAuth={token}; path=/; HttpOnly")])

    def _link(self) -> Response:
        if self.removed:
            return html_page(404, "Sorry, this item doesn't exist.")
        if self.sign_in:
            return Response(302, headers={"Location": SIGN_IN})
        if self.password is not None:
            return self._password_form()
        return self._to_folder()

    def _hop(self, req) -> Response:
        if not self._session(req):
            return html_page(403, "Access denied.")
        left = int(req.arg("n") or 0) - 1
        location = f"{self.web_path}/_layouts/15/hop.aspx?n={left}" if left > 0 else self._folder_page()
        return Response(302, headers={"Location": location})

    def _password_form(self, error: str = "") -> Response:
        action = html.escape(f"{self.web_path}/_layouts/15/guestaccess.aspx?share=EaFolderShareToken&e=AbCdEf")
        return html_page(200, f"""
<form id="search" action="/_layouts/15/search.aspx" method="get"><input type="text" name="k" value=""></form>
<form name="aspnetForm" method="post" action="{action}" id="inputForm">
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="{self.viewstate}" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="__VIEWSTATEGENERATOR" value="BAB98CB3" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="{self.validation}" />
<input type="hidden" name="__VIEWSTATEENCRYPTED" id="__VIEWSTATEENCRYPTED" value="" />
<p>{html.escape(error)}</p>
<input name="txtPassword" type="password" id="txtPassword" autocomplete="off" />
<input type="submit" name="btnSubmitPassword" value="Verify" id="btnSubmitPassword" />
<input type="button" name="btnCancel" value="Cancel" />
</form>""")

    def _password(self, req) -> Response:
        form = req.form()
        if form.get("__VIEWSTATE") != self.viewstate or form.get("__EVENTVALIDATION") != self.validation:
            return html_page(500, "Validation of viewstate MAC failed.")
        if "btnCancel" in form or "k" in form:
            return html_page(500, "A field of another button or form was sent.")
        if self.password is None or form.get("txtPassword") != self.password:
            return self._password_form("That password didn't work. Please try again.")
        return self._to_folder()

    def _contextinfo(self, req) -> Response:
        if not self._session(req):
            raise odata_error(403, ACCESS_DENIED)
        digest = "0x" + secrets.token_hex(32).upper() + ",17 Nov 2026 08:30:00 -0000"
        self.digests.add(digest)
        return json_response({"FormDigestTimeoutSeconds": 1800, "FormDigestValue": digest, "LibraryVersion": "16.0.0.0",
                              "SiteFullUrl": self.url + self.web_path, "WebFullUrl": self.url + self.web_path})

    def _render(self, req) -> Response:
        if self.fail is not None:
            status, self.fail = self.fail, None
            raise HTTPError(status, {"odata.error": {"code": str(status), "message": {"value": "Failed."}}},
                            {"Retry-After": "120"} if status in (429, 503) else None)
        if not self._session(req):
            raise odata_error(403, ACCESS_DENIED)
        if req.header("X-RequestDigest") not in self.digests:
            raise odata_error(403, BAD_DIGEST)
        library = req.arg("@a1") or ""
        if len(library) < 2 or library[0] != "'" or library[-1] != "'" or library[1:-1].replace("''", "'").lower() != self.library.lower():
            raise odata_error(404, ("-1, System.ArgumentException", "List does not exist."))
        parameters = (req.json() or {}).get("parameters") or {}
        folder = str(parameters.get("FolderServerRelativeUrl") or "").rstrip("/")
        if not self._inside(folder):
            raise odata_error(403, ACCESS_DENIED)
        node = next((node for path, node in self.nodes.items() if path.lower() == folder.lower()), None)
        if node is None or not node["dir"]:
            raise odata_error(404, NOT_FOUND)
        found = re.search(r"<RowLimit[^>]*>(\d+)</RowLimit>", str(parameters.get("ViewXml") or ""))
        limit = int(found.group(1)) if found else 30
        children = sorted((path for path in self.nodes if path.rsplit("/", 1)[0].lower() == folder.lower()),
                          key=lambda path: self.nodes[path]["id"])
        first = int(req.arg("PageFirstRow") or 1)
        page = children[first - 1:first - 1 + limit]
        data: dict[str, Any] = {"Row": [self._row(path) for path in page], "FirstRow": first,
                                "LastRow": first + len(page) - 1, "RowLimit": limit, "FolderPermissions": "0x1000030041"}
        if first - 1 + limit < len(children):
            data["NextHref"] = (f"?Paged=TRUE&p_ID={self.nodes[page[-1]]['id']}&PageFirstRow={first + limit}"
                                "&View=00000000-0000-0000-0000-000000000000")
        return json_response({"ListData": data} if self.wrap else data)

    def _row(self, path: str) -> dict[str, Any]:
        node = self.nodes[path]
        row = {
            "ID": str(node["id"]),
            "FileRef": path,
            "FileLeafRef": path.rsplit("/", 1)[1],
            "FSObjType": "1" if node["dir"] else "0",
            "File_x0020_Size": "" if node["dir"] else str(len(node["data"])),
            "Modified": node["modified"].strftime("%m/%d/%Y %I:%M %p"),
            "Modified.": node["modified"].strftime("%Y-%m-%dT%H:%M:%SZ"),
            "UniqueId": "{" + node["guid"] + "}",
        }
        if self.item_urls:
            row[".spItemUrl"] = (f"{self.item_url_root or self.url}/_api/v2.0/drives/b!FakeDriveId/items/"
                                 f"01FAKE{node['id']:06d}?version=Published")
        return row

    def _download(self, req) -> Response:
        guid = (req.arg("UniqueId") or "").strip("{}")
        tempauth = req.arg("tempauth")
        self.download_requests.append({"cookie": req.header("Cookie"), "range": req.header("Range"),
                                       "tempauth": tempauth is not None})
        path = self._by_guid(guid)
        if tempauth is not None:
            if path is None or not hmac.compare_digest(tempauth, self._tempauth(self.nodes[path]["guid"])):
                return html_page(401, "Unauthorized.")
        elif not self._session(req):
            return Response(302, headers={"Location": SIGN_IN})
        elif path is None:
            return html_page(404, "File Not Found.")
        elif not self._inside(path) or self.block_downloads:
            return html_page(403, "Access denied.")
        elif self.redirect_downloads:
            return Response(302, headers={"Location": self._tempauth_url(path)})
        node = self.nodes[path]
        if node["dir"]:
            return html_page(404, "File Not Found.")
        name = urllib.parse.quote(path.rsplit("/", 1)[1])
        return file_response(node["data"], req.header("Range"), {"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})

    def _drive_item(self, req, path: str) -> Response:
        if not self._session(req):
            raise HTTPError(401, {"error": {"code": "unauthenticated", "message": "Unauthenticated"}})
        if self.v2_refuses:
            raise HTTPError(403, {"error": {"code": "accessDenied", "message": "Access denied"}})
        found = re.fullmatch(r"/_api/v2\.0/drives/b!FakeDriveId/items/01FAKE(\d+)", path)
        item = next((item for item, node in self.nodes.items() if found and node["id"] == int(found.group(1))), None)
        if item is None or not self._inside(item):
            raise HTTPError(404, {"error": {"code": "itemNotFound", "message": "Item not found"}})
        node = self.nodes[item]
        return json_response({"id": f"01FAKE{node['id']:06d}", "name": item.rsplit("/", 1)[1],
                              "size": len(node["data"]), "file": {"mimeType": "application/octet-stream"},
                              "@content.downloadUrl": self._tempauth_url(item)})
