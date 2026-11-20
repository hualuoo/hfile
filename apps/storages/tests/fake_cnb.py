"""An in-memory fake of CNB's OpenAPI for releases (https://api.cnb.cool/swagger.json), of cnb.cool's download
links and of the file host they redirect to (tests only, no Django).

    fake = FakeCNB().start()
    fake.add_repo("org/group/repo", visibility="Public")
    fake.add_release("org/group/repo", "v1", {"a.txt": b"..."}, prerelease=False, draft=False)
    fake.api_url, fake.web_url

As CNB documents it: every API call needs "Authorization: Bearer <token>" (401 errcode 16 without one, 401 for
another one); /{repo} has visibility_level; /{repo}/-/releases is paged by page and page_size (default 10),
newest first; /-/releases/latest is the newest release that is neither a draft nor a prerelease (404 when there
is none); /-/releases/download/{tag}/{name} redirects to a link of the file host that expires. cnb.cool's
browser links do the same for public repositories only. The file host refuses an Authorization header and
answers Range requests. Errors are {"errcode", "errmsg"}. A tag with "/" goes in a download path as %2F.

Knobs: basic_info False refuses /{repo} of a repository that isn't public (403), as CNB does for a token without
repo-basic-info:r (CNB gives a token read access to public repositories only, unless granted more:
https://docs.cnb.cool/zh/guide/access-token.html; the status and errcode are a guess).
"""

from __future__ import annotations

import datetime
import itertools
import re
import secrets
import threading
import time
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response


class FakeCNB:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(100)
        self.clock = itertools.count(1)
        self.repos: dict[str, dict[str, Any]] = {}  # "org/group/repo" -> {"visibility", "releases": [newest first]}
        self.files: dict[str, bytes] = {}  # asset id -> data
        self.links: dict[str, tuple[str, float]] = {}  # key -> (asset id, expiry)
        self.token = "cnb-t0ken"
        self.basic_info = True
        self.calls: list[str] = []
        self.file_requests: list[dict[str, str | None]] = []
        self.server = FakeServer(self.handle)

    def start(self) -> FakeCNB:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    @property
    def api_url(self) -> str:
        return self.server.url + "/api"

    @property
    def web_url(self) -> str:
        return self.server.url + "/web"

    def reset(self) -> None:
        with self.lock:
            self.repos.clear()
            self.files.clear()
            self.links.clear()
            self.basic_info = True
            self.calls.clear()
            self.file_requests.clear()

    def add_repo(self, name: str, *, visibility: str = "Public") -> None:
        with self.lock:
            self.repos[name] = {"visibility": visibility, "releases": []}

    def add_release(self, repo: str, tag: str, files: dict[str, bytes], *, prerelease: bool = False,
                    draft: bool = False) -> None:
        with self.lock:
            moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=next(self.clock))
            stamp = moment.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            assets = []
            for name, data in files.items():
                asset_id = f"a{next(self.ids)}x"
                self.files[asset_id] = data
                quoted = "/".join(urllib.parse.quote(part, safe="") for part in (tag, name))
                assets.append({"id": asset_id, "name": name, "path": f"/{repo}/-/releases/download/{quoted}",
                               "size": len(data), "content_type": "application/octet-stream", "created_at": stamp,
                               "updated_at": stamp, "hash_algo": "sha256", "hash_value": "",
                               "browser_download_url": f"{self.web_url}/{repo}/-/releases/download/{quoted}",
                               "url": f"{self.api_url}/{repo}/-/releases/download/{quoted}"})
            self.repos[repo]["releases"].insert(0, {
                "id": f"r{next(self.ids)}", "tag_name": tag, "name": tag, "draft": draft, "prerelease": prerelease,
                "is_latest": False, "created_at": stamp, "updated_at": stamp,
                "published_at": "" if draft else stamp, "assets": assets})

    # -- requests

    def _link(self, release: dict, name: str) -> Response:
        for asset in release["assets"]:
            if asset["name"] == name:
                key = secrets.token_hex(8)
                self.links[key] = (asset["id"], time.time() + 3600)
                return Response(302, b"", {"Location": f"{self.server.url}/files/{key}"})
        raise HTTPError(404, {"errcode": 404, "errmsg": "attachment not found"})

    def _release_of(self, repo: dict, tag: str) -> dict:
        for release in repo["releases"]:
            if release["tag_name"] == tag and not release["draft"]:
                return release
        raise HTTPError(404, {"errcode": 404, "errmsg": "release not found"})

    @staticmethod
    def _download(raw: str) -> tuple[str, str, str] | None:
        """(repo, tag, name) of a still percent-encoded path <repo>/-/releases/download/<tag>/<name>: a tag's "/" is
        %2F."""
        repo, marker, rest = raw.partition("/-/releases/download/")
        parts = rest.split("/")
        if not marker or len(parts) != 2:
            return None
        return urllib.parse.unquote(repo), urllib.parse.unquote(parts[0]), urllib.parse.unquote(parts[1])

    def handle(self, req) -> Response:
        path = urllib.parse.unquote(req.path)
        if path.startswith("/files/"):
            return self._file(req, path[len("/files/"):])
        with self.lock:
            if path.startswith("/web/"):
                found = self._download(req.path[len("/web/"):])
                repo = self.repos.get(found[0]) if found else None
                if repo is None or repo["visibility"] != "Public":
                    raise HTTPError(404, b"Not Found")
                return self._link(self._release_of(repo, found[1]), found[2])
            if not path.startswith("/api/"):
                raise HTTPError(404, b"no such endpoint")
            path = path[len("/api"):]
            self.calls.append(path)
            header = req.header("Authorization")
            if header is None:
                raise HTTPError(401, {"errcode": 16, "errmsg": "The task will not run because the user is not logged in."})
            if header != f"Bearer {self.token}":
                raise HTTPError(401, {"errcode": 10, "errmsg": "invalid token"})
            match = re.fullmatch(r"/(.+?)(/-/releases(?:/.*)?)?", path)
            name, rest = match.group(1), match.group(2) or ""
            repo = self.repos.get(name)
            if repo is None:
                raise HTTPError(404, {"errcode": 404, "errmsg": "repo not found"})
            releases = repo["releases"]
            if rest == "":
                if not self.basic_info and repo["visibility"] != "Public":
                    raise HTTPError(403, {"errcode": 7, "errmsg": "permission denied: repo-basic-info:r"})
                return json_response({"path": name, "name": name.rpartition("/")[2], "visibility_level": repo["visibility"]})
            if rest == "/-/releases/latest":
                latest = next((r for r in releases if not r["draft"] and not r["prerelease"]), None)
                if latest is None:
                    raise HTTPError(404, {"errcode": 404, "errmsg": "release not found"})
                return json_response({**latest, "is_latest": True})
            if rest == "/-/releases":
                size = int(req.arg("page_size", "10"))
                page = int(req.arg("page", "1"))
                return json_response(releases[(page - 1) * size:page * size])
            download = self._download(req.path[len("/api/"):])
            if download:
                return self._link(self._release_of(repo, download[1]), download[2])
            raise HTTPError(404, {"errcode": 404, "errmsg": "not found"})

    def _file(self, req, key: str) -> Response:
        with self.lock:
            self.file_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            asset_id, expiry = self.links.get(key, ("", 0.0))
            data = self.files.get(asset_id)
        if req.header("Authorization") is not None:
            raise HTTPError(400, b"<Error><Code>InvalidArgument</Code><Message>Only one auth mechanism allowed</Message></Error>")
        if data is None or time.time() > expiry:
            raise HTTPError(403, b"<Error><Code>AccessDenied</Code><Message>Request has expired</Message></Error>")
        return file_response(data, req.header("Range"))
