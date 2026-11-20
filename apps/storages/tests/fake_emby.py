"""An in-memory fake of an Emby (or Jellyfin) server's API for browsing and downloading (tests only).

    fake = FakeEmby({"/Lib/a.txt": b"..."}, folders=("/Lib/empty",)).start()

The top folders are the user's libraries; files have a Path on the server's disk and a Name of their own
(a title). It answers where the driver depends on it, in one of two modes:

- "jellyfin" (the default), as Jellyfin 12 with its default EnableLegacyAuthorization=false
  (Jellyfin.Server.Implementations/Security/AuthorizationContext.cs): only "Authorization: MediaBrowser ..." is
  read, for the client's fields and the token (Token="..."); a login without Client or DeviceId is a 400, as
  SessionManager's ArgumentException;
- "emby", as Emby: only X-Emby-Authorization for the client's fields and X-Emby-Token for the token.

AuthenticateByName gives an AccessToken and the user's Id, and ends the user's sessions of the same DeviceId
("Logout any existing sessions for the user on this device", SessionManager.GetAuthorizationToken); the API key
works as a token; /Users/<id>/Views and /Users/<id>/Items?ParentId= paged by StartIndex and Limit (another user's id
is a 404); /Items/<id>/Download (refused without the download permission) and /Videos/<id>/stream?static=true send
the file, with Range. Knobs: mode, can_download, expire_tokens(), api_key. Records: logins.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

USER_ID = "f3a1c0de0000000000000000000000aa"


def fields(header: str | None) -> tuple[str, dict[str, str]]:
    """The scheme and the key="value" pairs of an authorization header."""
    scheme, _, rest = (header or "").partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', rest))


class FakeEmby:
    def __init__(self, files: dict[str, bytes], *, folders: tuple[str, ...] = (), mode: str = "jellyfin"):
        self.lock = threading.Lock()
        self.mode = mode
        self.users = {"alice": "pw-alice"}
        self.api_key = "emby-api-key"
        self.tokens: dict[str, tuple[str, str]] = {}  # token -> (DeviceId, user)
        self.logins = 0
        self.can_download = True
        self.items: dict[str, dict[str, Any]] = {}  # id -> {"parent", "name", "dir", "data", "path"}
        self.by_path: dict[str, str] = {"/": "root"}
        self.items["root"] = {"parent": None, "name": "", "dir": True}
        for path in sorted(folders):
            self._folder(path)
        for path, data in files.items():
            parent = self._folder(path.rsplit("/", 1)[0] or "/")
            item_id = secrets.token_hex(8)
            name = path.rsplit("/", 1)[1]
            self.items[item_id] = {"parent": parent, "name": "Title of " + name, "dir": False, "data": data,
                                   "path": "/srv/media" + path}
        self.server = FakeServer(self.handle)

    def _folder(self, path: str) -> str:
        if path in self.by_path:
            return self.by_path[path]
        parent = self._folder(path.rsplit("/", 1)[0] or "/")
        item_id = secrets.token_hex(8)
        self.items[item_id] = {"parent": parent, "name": path.rsplit("/", 1)[1], "dir": True}
        self.by_path[path] = item_id
        return item_id

    def start(self) -> FakeEmby:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def url(self) -> str:
        return self.server.url

    def expire_tokens(self) -> None:
        with self.lock:
            self.tokens.clear()

    def _json(self, item_id: str) -> dict[str, Any]:
        item = self.items[item_id]
        answer = {"Id": item_id, "Name": item["name"], "IsFolder": item["dir"],
                  "Type": "Folder" if item["dir"] else "Video", "DateCreated": "2026-11-17T12:00:00.0000000Z"}
        if not item["dir"]:
            answer.update({"Path": item["path"], "MediaSources": [{"Size": len(item["data"]), "Container": "mkv"}]})
        return answer

    def _client(self, req) -> dict[str, str]:
        """The client's fields (Client, DeviceId, Token, ...) from the header the server reads."""
        if self.mode == "jellyfin":
            scheme, found = fields(req.header("Authorization"))
            return found if scheme == "mediabrowser" else {}
        scheme, found = fields(req.header("X-Emby-Authorization"))
        return found if scheme in ("mediabrowser", "emby") else {}

    def _token(self, req) -> str | None:
        return self._client(req).get("Token") if self.mode == "jellyfin" else req.header("X-Emby-Token")

    def handle(self, req) -> Response:
        path = urllib.parse.unquote(req.path)
        with self.lock:
            if path == "/Users/AuthenticateByName" and req.method == "POST":
                client = self._client(req)
                if not client.get("Client") or not client.get("DeviceId"):
                    raise HTTPError(400, b"Value cannot be null or empty. (Parameter 'request.App')")
                body = json.loads(req.body or b"{}")
                if self.users.get(body.get("Username")) != body.get("Pw"):
                    raise HTTPError(401, b"Invalid username or password entered.")
                for token, (device, user) in list(self.tokens.items()):  # the user's other session of the device
                    if device == client["DeviceId"] and user == body["Username"]:
                        del self.tokens[token]
                token = secrets.token_hex(16)
                self.tokens[token] = (client["DeviceId"], body["Username"])
                self.logins += 1
                return json_response({"AccessToken": token, "User": {"Id": USER_ID, "Name": body["Username"]}})
            token = self._token(req)
            if token != self.api_key and token not in self.tokens:
                raise HTTPError(401, b"Access token is invalid or expired.")
            if path == f"/Users/{USER_ID}/Views":
                return json_response({"Items": [self._json(i) for i, it in self.items.items() if it["parent"] == "root"]})
            if path == f"/Users/{USER_ID}/Items":
                parent = req.arg("ParentId")
                if parent not in self.items or not self.items[parent]["dir"]:
                    raise HTTPError(404, b"Item not found")
                children = sorted((i for i, it in self.items.items() if it["parent"] == parent), key=lambda i: self.items[i]["name"])
                start, limit = int(req.arg("StartIndex", "0")), int(req.arg("Limit", "100000"))
                return json_response({"Items": [self._json(i) for i in children[start:start + limit]],
                                      "TotalRecordCount": len(children)})
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] in ("Items", "Videos") and parts[2] in ("Download", "stream"):
                item = self.items.get(parts[1])
                if item is None or item["dir"]:
                    raise HTTPError(404, b"Item not found")
                if parts[2] == "Download" and not self.can_download:
                    raise HTTPError(403, b"Forbidden")
                if parts[2] == "stream" and req.arg("static") != "true":
                    raise HTTPError(400, b"transcoding isn't faked")
                data = item["data"]
            else:
                raise HTTPError(404, b"Not found")
        return file_response(data, req.header("Range"))
