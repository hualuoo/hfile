"""An in-memory fake of GitHub's REST API for releases, of github.com's download links and of the file host
they redirect to (tests only, no Django).

    fake = FakeGitHub().start()
    fake.add_repo("owner", "repo", private=False)
    fake.add_release("owner", "repo", "v1", {"a.txt": b"..."}, prerelease=False, draft=False)
    fake.api_url, fake.web_url
    fake.close()

It follows GitHub where the driver depends on it: a token is "Authorization: Bearer <token>" (401 "Bad
credentials" for another one); a private repository is a 404 without it; /releases/latest is the newest
release that is neither a draft nor a prerelease (404 when there is none); /releases is paged by per_page
(at most per_page_max) and page, with a Link header to the next page; drafts are only shown to the token.
An asset asked for with "Accept: application/octet-stream" redirects to the file host, as a
browser_download_url of a public repository does; the file host refuses an Authorization header (it
isn't GitHub's API) and answers Range requests.

Knobs: rate_limited (the next N API calls answer 403 with x-ratelimit-remaining 0), per_page_max; calls (the API
paths asked, in order) and web_requests (the github.com paths asked, decoded). A download link writes a tag with "/" as
%2F, as GitHub's does.
"""

from __future__ import annotations

import datetime
import itertools
import re
import threading
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

RESET_AT = 1893456000  # 2030-01-01 00:00 UTC, the x-ratelimit-reset of a refused call


class FakeGitHub:
    def __init__(self):
        self.lock = threading.Lock()
        self.ids = itertools.count(100)
        self.clock = itertools.count(1)
        self.repos: dict[str, dict[str, Any]] = {}  # "owner/repo" -> {"private", "releases": [newest first]}
        self.assets: dict[int, tuple[str, bytes]] = {}  # id -> (owner/repo, data)
        self.token = "gh-t0ken"
        self.rate_limited = 0
        self.per_page_max = 100
        self.calls: list[str] = []
        self.web_requests: list[str] = []
        self.file_requests: list[dict[str, str | None]] = []
        self.api = FakeServer(self.handle_api)
        self.files = FakeServer(self.handle_files)

    def start(self) -> FakeGitHub:
        self.api.start()
        self.files.start()
        return self

    def close(self) -> None:
        self.api.close()
        self.files.close()

    @property
    def api_url(self) -> str:
        return self.api.url + "/api"

    @property
    def web_url(self) -> str:
        return self.api.url + "/web"

    def reset(self) -> None:
        with self.lock:
            self.repos.clear()
            self.assets.clear()
            self.rate_limited = 0
            self.per_page_max = 100
            self.calls.clear()
            self.web_requests.clear()
            self.file_requests.clear()

    # -- setting up

    def add_repo(self, owner: str, repo: str, *, private: bool = False) -> None:
        with self.lock:
            self.repos[f"{owner}/{repo}"] = {"private": private, "releases": []}

    def add_release(self, owner: str, repo: str, tag: str, files: dict[str, bytes], *,
                    prerelease: bool = False, draft: bool = False) -> None:
        with self.lock:
            moment = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(hours=next(self.clock))
            stamp = moment.strftime("%Y-%m-%dT%H:%M:%SZ")
            assets = []
            for name, data in files.items():
                asset_id = next(self.ids)
                self.assets[asset_id] = (f"{owner}/{repo}", data)
                quoted = "/".join(urllib.parse.quote(part, safe="") for part in (tag, name))
                assets.append({"id": asset_id, "name": name, "size": len(data), "content_type": "application/octet-stream",
                               "created_at": stamp, "updated_at": stamp,
                               "browser_download_url": f"{self.web_url}/{owner}/{repo}/releases/download/{quoted}"})
            self.repos[f"{owner}/{repo}"]["releases"].insert(0, {
                "id": next(self.ids), "tag_name": tag, "name": tag, "draft": draft, "prerelease": prerelease,
                "created_at": stamp, "published_at": None if draft else stamp, "assets": assets})

    # -- the API

    def _authorized(self, req) -> bool:
        header = req.header("Authorization")
        if header is None:
            return False
        if header != f"Bearer {self.token}":
            raise HTTPError(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest"})
        return True

    def _repo(self, name: str, authorized: bool) -> dict[str, Any]:
        repo = self.repos.get(name)
        if repo is None or (repo["private"] and not authorized):
            raise HTTPError(404, {"message": "Not Found"})
        return repo

    def handle_api(self, req) -> Response:
        if req.path.startswith("/web/"):
            return self._web(req, req.path[len("/web"):])
        path = urllib.parse.unquote(req.path)
        if not path.startswith("/api/"):
            raise HTTPError(404, b"no such endpoint")
        path = path[len("/api"):]
        with self.lock:
            self.calls.append(path)
            if req.header("Accept") not in ("application/vnd.github+json", "application/octet-stream"):
                raise HTTPError(415, {"message": "Unsupported Accept header"})
            if self.rate_limited > 0:
                self.rate_limited -= 1
                raise HTTPError(403, {"message": "API rate limit exceeded for 203.0.113.5."},
                                {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(RESET_AT)})
            authorized = self._authorized(req)
            match = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
            if not match:
                raise HTTPError(404, {"message": "Not Found"})
            name, rest = f"{match.group(1)}/{match.group(2)}", match.group(3) or ""
            repo = self._repo(name, authorized)
            releases = [r for r in repo["releases"] if authorized or not r["draft"]]
            if rest == "":
                return json_response({"full_name": name, "private": repo["private"]})
            if rest == "/releases/latest":
                latest = next((r for r in releases if not r["draft"] and not r["prerelease"]), None)
                if latest is None:
                    raise HTTPError(404, {"message": "Not Found"})
                return json_response(latest)
            if rest == "/releases":
                per_page = min(int(req.arg("per_page", "30")), self.per_page_max)
                page = int(req.arg("page", "1"))
                chunk = releases[(page - 1) * per_page:page * per_page]
                headers = {}
                if page * per_page < len(releases):
                    headers["Link"] = f'<{self.api_url}/repos/{name}/releases?per_page={per_page}&page={page + 1}>; rel="next"'
                return json_response(chunk, headers=headers)
            asset = re.fullmatch(r"/releases/assets/(\d+)", rest)
            if asset:
                asset_id = int(asset.group(1))
                if self.assets.get(asset_id, ("",))[0] != name:
                    raise HTTPError(404, {"message": "Not Found"})
                if req.header("Accept") != "application/octet-stream":
                    raise HTTPError(400, {"message": "ask for the bytes"})
                return Response(302, b"", {"Location": f"{self.files.url}/files/{asset_id}?sig=s1gned"})
            raise HTTPError(404, {"message": "Not Found"})

    def _web(self, req, raw: str) -> Response:
        """github.com/owner/repo/releases/download/tag/name: public repositories' files, for browsers. `raw` is still
        percent-encoded: a tag's "/" is %2F."""
        parts = [urllib.parse.unquote(part) for part in raw.split("/")[1:]]
        with self.lock:
            self.web_requests.append("/" + "/".join(parts))
            matched = len(parts) == 6 and parts[2:4] == ["releases", "download"]
            repo = self.repos.get(f"{parts[0]}/{parts[1]}") if matched else None
            if repo is None or repo["private"]:
                raise HTTPError(404, b"Not Found")
            for release in repo["releases"]:
                if release["tag_name"] == parts[4] and not release["draft"]:
                    for asset in release["assets"]:
                        if asset["name"] == parts[5]:
                            return Response(302, b"", {"Location": f"{self.files.url}/files/{asset['id']}?sig=s1gned"})
        raise HTTPError(404, b"Not Found")

    # -- the file host

    def handle_files(self, req) -> Response:
        with self.lock:
            self.file_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            match = re.fullmatch(r"/files/(\d+)", req.path)
            found = self.assets.get(int(match.group(1))) if match else None
        if req.header("Authorization") is not None:
            raise HTTPError(400, b"<Error><Code>InvalidArgument</Code><Message>Only one auth mechanism allowed</Message></Error>")
        if found is None or req.arg("sig") != "s1gned":
            raise HTTPError(404, b"Not Found")
        return file_response(found[1], req.header("Range"))
