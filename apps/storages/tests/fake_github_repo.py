"""An in-memory fake of the parts of GitHub's REST API a GitHub repository storage uses, and of
raw.githubusercontent.com (tests only, no Django).

    fake = FakeGitHubRepo().start()
    fake.add_repo("o", "r", {"a.txt": b"a"}, private=False)   # its default branch "main", with one commit
    fake.api_url, fake.ghes_url, fake.raw_url
    fake.files("o", "r")                                     # {path: bytes} of the branch's newest commit
    fake.close()

Git objects are kept as git keeps them: blobs, trees and commits named by the SHA-1 of their content, so equal
content has an equal name. It answers as GitHub's documentation says (https://docs.github.com/en/rest/git,
/en/rest/repos/repos, /en/rest/using-the-rest-api/rate-limits-for-the-rest-api, .../troubleshooting-the-rest-api)
where the driver depends on it:

- "Authorization: Bearer <token>": `tokens` maps the known ones to "write" or "read", another is a 401 "Bad
  credentials". A change with a "read" token is a 403 "Resource not accessible by personal access token" with
  X-Accepted-GitHub-Permissions. A private repository without a token, or none of that name, is a 404.
- A User-Agent is needed (GitHub refuses requests without one), and a supported X-GitHub-Api-Version; Accept must be
  GitHub's JSON media type, or its raw one for a blob's bytes (stricter than GitHub, to keep the driver to them).
- Every answer has the x-ratelimit-* headers. `rate_limited` makes the next calls a 403 with
  x-ratelimit-remaining 0; `secondary_limited` a 403 of a secondary rate limit, with retry-after;
  `server_errors` a 502.
- A repository without a commit answers its git calls with 409 "Git Repository is empty."; one in `moved` (renamed)
  answers 301.
- GET git/ref/heads/<branch>, git/commits/<sha>, git/trees/<sha> (one level, cut past `tree_limit` entries:
  "truncated"), git/blobs/<sha> (JSON with base64, or the bytes with the raw media type; a Range is ignored).
- POST git/blobs (base64 or utf-8, at most `max_blob` bytes), git/trees (base_tree, nested paths, sha null deletes
  and deleting what isn't there is a 422; a folder left empty is dropped, as git drops it; ".git" can't be a name),
  git/commits (the tree and the parents must exist).
- PATCH git/refs/heads/<branch> moves a branch forward only (force false), else 422 "Update is not a fast forward";
  a protected branch (protect()) refuses with 422. `foreign_commits`: before each of the next ref updates someone
  else pushes a commit (`foreign_change`, or a new foreign-<n>.txt), so the update isn't a fast forward.
  `lose_ref_answers`: the next ref updates are made, and answered 502 all the same (a proxy in front of GitHub that
  lost the answer); `ref_errors`: the next ones are answered 502 and not made.
- raw.githubusercontent.com: /<owner>/<repo>/<commit or branch>/<path> of public repositories, with Range.

Knobs as above, git_modes (a folder's mode given as 40000), and calls ((method, path) of each API request), blob_requests (the Accept, Range and
Accept-Encoding of each blob asked for), raw_requests (the Authorization and Range the raw host got).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import itertools
import re
import threading
import urllib.parse
from typing import Any

from .fake_cloud_http import FakeServer, HTTPError, Response, file_response, json_response

RESET_AT = 1893456000  # 2030-01-01 00:00 UTC, the x-ratelimit-reset of every answer
API_VERSIONS = ("2022-11-28", "2026-03-10")
JSON_TYPE = "application/vnd.github+json"
RAW_TYPE = "application/vnd.github.raw+json"
MODES = {"100644": "blob", "100755": "blob", "120000": "blob", "040000": "tree", "160000": "commit"}
NOT_FOUND = b"404: Not Found"


def git_sha(kind: str, body: bytes) -> str:
    return hashlib.sha1(b"%s %d\x00" % (kind.encode(), len(body)) + body).hexdigest()


def error(status: int, message: str, headers: dict[str, str] | None = None) -> HTTPError:
    return HTTPError(status, {"message": message, "documentation_url": "https://docs.github.com/rest"}, headers)


def _order(entries: dict[str, tuple[str, str]]) -> list[str]:
    """Git's order of a tree's names: by their bytes, a folder's as if it ended in "/"."""
    return sorted(entries, key=lambda name: name.encode() + (b"/" if entries[name][0] == "040000" else b""))


class FakeGitHubRepo:
    def __init__(self):
        self.lock = threading.RLock()
        self.clock = itertools.count(1)
        self.repos: dict[str, dict[str, Any]] = {}  # "owner/repo" in lower case -> the repository
        self.tokens = {"gh-write": "write", "gh-read": "read"}
        self.moved: set[str] = set()
        self._knobs()
        self.api = FakeServer(self.handle_api)
        self.raw = FakeServer(self.handle_raw)

    def _knobs(self) -> None:
        self.rate_limited = 0
        self.secondary_limited = 0
        self.secondary_retry_after = "30"
        self.server_errors = 0
        self.foreign_commits = 0
        self.lose_ref_answers = 0
        self.ref_errors = 0
        self.foreign_change: dict[str, bytes | None] | None = None
        self.foreign = 0
        self.tree_limit = 100_000
        self.git_modes = False  # trees give a folder's mode as git writes it, 40000
        self.max_blob = 100 * 1024 * 1024
        self.used = 0
        self.calls: list[tuple[str, str]] = []
        self.blob_requests: list[dict[str, str | None]] = []
        self.raw_requests: list[dict[str, str | None]] = []

    def start(self) -> FakeGitHubRepo:
        self.api.start()
        self.raw.start()
        return self

    def close(self) -> None:
        self.api.close()
        self.raw.close()

    @property
    def api_url(self) -> str:
        return self.api.url + "/api"

    @property
    def ghes_url(self) -> str:
        """The same API, as GitHub Enterprise Server's https://host/api/v3."""
        return self.api.url + "/ghes/api/v3"

    @property
    def raw_url(self) -> str:
        return self.raw.url

    def reset(self) -> None:
        with self.lock:
            self.repos.clear()
            self.moved.clear()
            self.tokens = {"gh-write": "write", "gh-read": "read"}
            self._knobs()

    # -- setting up and looking

    def add_repo(self, owner: str, repo: str, files: dict[str, Any] | None = None, *, private: bool = False,
                 branch: str = "main", empty: bool = False) -> None:
        """A repository whose branch `branch` (its default) has one commit with `files`: {path: bytes, or (mode,
        bytes) for an executable or a symbolic link, or ("160000", commit sha) for a submodule}; without files, the
        root's .gitkeep alone. `empty`: no commit at all."""
        with self.lock:
            self.repos[f"{owner}/{repo}".lower()] = {"name": f"{owner}/{repo}", "private": private, "default": branch,
                                                     "refs": {}, "objects": {}, "protected": set()}
            if not empty:
                self.add_branch(owner, repo, branch, files)

    def add_branch(self, owner: str, repo: str, branch: str, files: dict[str, Any] | None = None) -> None:
        with self.lock:
            entry = self._repo(owner, repo)
            tree = self._tree_of(entry, {".gitkeep": b""} if files is None else files)
            entry["refs"][branch] = self._put_commit(entry, tree, [], "Initial commit")

    def protect(self, owner: str, repo: str, branch: str) -> None:
        with self.lock:
            self._repo(owner, repo)["protected"].add(branch)

    def push(self, owner: str, repo: str, changes: dict[str, bytes | None], branch: str | None = None) -> None:
        """A commit of someone else on the branch: files added or replaced (bytes) or deleted (None)."""
        with self.lock:
            entry = self._repo(owner, repo)
            self._push(entry, branch or entry["default"], changes)

    def head(self, owner: str, repo: str, branch: str | None = None) -> str:
        with self.lock:
            entry = self._repo(owner, repo)
            return entry["refs"][branch or entry["default"]]

    def commits(self, owner: str, repo: str, branch: str | None = None) -> list[str]:
        """The messages of the branch's commits, newest first (following first parents)."""
        with self.lock:
            entry = self._repo(owner, repo)
            sha, messages = entry["refs"][branch or entry["default"]], []
            while sha:
                commit = entry["objects"][sha][1]
                messages.append(commit["message"])
                sha = commit["parents"][0] if commit["parents"] else None
            return messages

    def files(self, owner: str, repo: str, branch: str | None = None) -> dict[str, bytes]:
        return {path: data for path, (mode, data) in self.entries(owner, repo, branch).items() if mode != "160000"}

    def entries(self, owner: str, repo: str, branch: str | None = None) -> dict[str, tuple[str, Any]]:
        """{path: (mode, bytes)} of the files of the branch's newest commit ((mode, sha) for a submodule)."""
        with self.lock:
            entry = self._repo(owner, repo)
            found: dict[str, tuple[str, Any]] = {}

            def walk(sha: str, prefix: str) -> None:
                for name, (mode, child) in entry["objects"][sha][1].items():
                    if mode == "040000":
                        walk(child, prefix + name + "/")
                    else:
                        found[prefix + name] = (mode, child if mode == "160000" else entry["objects"][child][1])

            commit = entry["objects"][entry["refs"][branch or entry["default"]]][1]
            walk(commit["tree"], "")
            return found

    # -- git objects

    def _repo(self, owner: str, repo: str) -> dict[str, Any]:
        return self.repos[f"{owner}/{repo}".lower()]

    def _put_blob(self, repo: dict[str, Any], data: bytes) -> str:
        sha = git_sha("blob", data)
        repo["objects"][sha] = ("blob", data)
        return sha

    def _put_tree(self, repo: dict[str, Any], entries: dict[str, tuple[str, str]]) -> str:
        body = b"".join(b"%s %s\x00%s" % (entries[name][0].lstrip("0").encode(), name.encode(), bytes.fromhex(entries[name][1]))
                        for name in _order(entries))
        sha = git_sha("tree", body)
        repo["objects"][sha] = ("tree", dict(entries))
        return sha

    def _put_commit(self, repo: dict[str, Any], tree: str, parents: list[str], message: str) -> str:
        moment = 1767225600 + next(self.clock)  # 2026-01-01 and a second more for each commit
        who = f"Fake <fake@example.com> {moment} +0000"
        lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents), f"author {who}", f"committer {who}", "", message]
        sha = git_sha("commit", ("\n".join(lines) + "\n").encode())
        repo["objects"][sha] = ("commit", {"tree": tree, "parents": list(parents), "message": message, "time": moment})
        return sha

    def _tree_of(self, repo: dict[str, Any], files: dict[str, Any]) -> str:
        root: dict[str, Any] = {}
        for path, value in files.items():
            *folders, name = path.strip("/").split("/")
            node = root
            for folder in folders:
                node = node.setdefault(folder, {})
            node[name] = value

        def build(node: dict[str, Any]) -> str:
            entries: dict[str, tuple[str, str]] = {}
            for name, value in node.items():
                if isinstance(value, dict):
                    entries[name] = ("040000", build(value))
                elif isinstance(value, tuple) and value[0] == "160000":
                    entries[name] = value
                elif isinstance(value, tuple):
                    entries[name] = (value[0], self._put_blob(repo, value[1]))
                else:
                    entries[name] = ("100644", self._put_blob(repo, value))
            return self._put_tree(repo, entries)

        return build(root)

    def _change(self, repo: dict[str, Any], sha: str | None, parts: list[str], value: tuple[str, str] | None) -> str | None:
        """The tree `sha` (None: an empty one) with the entry at `parts` set to `value` (mode, sha) or deleted (None):
        the new tree's SHA, or None when it ends up empty (git keeps no empty folder)."""
        entries = dict(repo["objects"][sha][1]) if sha else {}
        name, current = parts[0], entries.get(parts[0])
        if len(parts) == 1:
            if value is None and current is None:
                raise error(422, f"GitHub can't delete {name}: there is no such file or folder")
            if value is None:
                del entries[name]
            else:
                entries[name] = value
        else:
            if current is not None and current[0] != "040000":
                raise error(422, f"tree.path {name} is not a folder")
            if current is None and value is None:
                raise error(422, f"GitHub can't delete {'/'.join(parts)}: there is no such file or folder")
            child = self._change(repo, current[1] if current else None, parts[1:], value)
            if child is None:
                entries.pop(name, None)
            else:
                entries[name] = ("040000", child)
        return self._put_tree(repo, entries) if entries else None

    def _push(self, repo: dict[str, Any], branch: str, changes: dict[str, bytes | None]) -> None:
        head = repo["refs"][branch]
        tree = repo["objects"][head][1]["tree"]
        for path, data in changes.items():
            value = None if data is None else ("100644", self._put_blob(repo, data))
            tree = self._change(repo, tree, path.split("/"), value) or self._put_tree(repo, {})
        repo["refs"][branch] = self._put_commit(repo, tree, [head], "Someone else's change")

    def _descends(self, repo: dict[str, Any], commit: str, ancestor: str) -> bool:
        seen, todo = set(), [commit]
        while todo:
            sha = todo.pop()
            if sha == ancestor:
                return True
            if sha not in seen:
                seen.add(sha)
                todo.extend(repo["objects"][sha][1]["parents"])
        return False

    # -- the API

    def handle_api(self, req) -> Response:
        path = urllib.parse.unquote(req.path)
        for prefix in ("/api/", "/ghes/api/v3/"):
            if path.startswith(prefix):
                path = path[len(prefix) - 1:]
                break
        else:
            raise HTTPError(404, b"no such endpoint")
        with self.lock:
            self.calls.append((req.method, path))
            limited = self.rate_limited > 0
            self.rate_limited -= limited
            self.used += 1
            limits = {"x-ratelimit-limit": "5000", "x-ratelimit-remaining": "0" if limited else str(max(5000 - self.used, 1)),
                      "x-ratelimit-used": str(self.used), "x-ratelimit-reset": str(RESET_AT), "x-ratelimit-resource": "core"}
            try:
                if limited:
                    raise error(403, "API rate limit exceeded for user ID 1.")
                if self.secondary_limited > 0:
                    self.secondary_limited -= 1
                    raise error(403, "You have exceeded a secondary rate limit. Please wait a few minutes before you try "
                                     "again.", {"retry-after": self.secondary_retry_after})
                if self.server_errors > 0:
                    self.server_errors -= 1
                    raise error(502, "Server Error")
                response = self._api(req, path)
            except HTTPError as failure:
                response = failure.response
            response.headers += list(limits.items())
            return response

    def _api(self, req, path: str) -> Response:
        if not req.header("User-Agent"):
            raise error(403, "Request forbidden by administrative rules. Please make sure your request has a User-Agent header.")
        version = req.header("X-GitHub-Api-Version")
        if version is not None and version not in API_VERSIONS:
            raise error(400, f"API version {version} is not supported.")
        if req.header("Accept") not in (JSON_TYPE, RAW_TYPE):
            raise error(415, "Unsupported 'Accept' header")
        access = None
        header = req.header("Authorization")
        if header is not None:
            access = self.tokens.get(header[len("Bearer "):]) if header.startswith("Bearer ") else None
            if access is None:
                raise error(401, "Bad credentials")
        match = re.fullmatch(r"/repos/([^/]+)/([^/]+)(/.*)?", path)
        if not match:
            raise error(404, "Not Found")
        name, rest = f"{match.group(1)}/{match.group(2)}", match.group(3) or ""
        if name.lower() in self.moved:
            raise error(301, "Moved Permanently", {"Location": f"{self.api_url}/repositories/1{rest}"})
        repo = self.repos.get(name.lower())
        if repo is None or (repo["private"] and access is None):
            raise error(404, "Not Found")
        if rest == "" and req.method == "GET":
            return json_response({"id": 1, "name": match.group(2), "full_name": repo["name"], "private": repo["private"],
                                  "visibility": "private" if repo["private"] else "public", "archived": False,
                                  "default_branch": repo["default"], "permissions": {"admin": False, "push": True, "pull": True}})
        if not rest.startswith("/git/"):
            raise error(404, "Not Found")
        if not repo["refs"]:
            raise error(409, "Git Repository is empty.")
        if req.method != "GET":
            if access is None:
                raise error(401, "Requires authentication")
            if access != "write":
                raise error(403, "Resource not accessible by personal access token",
                            {"X-Accepted-GitHub-Permissions": "contents=write"})
        return self._git(req, repo, rest[len("/git"):])

    def _git(self, req, repo: dict[str, Any], rest: str) -> Response:
        objects = repo["objects"]
        if req.method == "GET":
            match = re.fullmatch(r"/ref/heads/(.+)", rest)
            if match:
                sha = repo["refs"].get(match.group(1))
                if sha is None:
                    raise error(404, "Not Found")
                return json_response(self._ref(match.group(1), sha))
            match = re.fullmatch(r"/(commits|trees|blobs)/([0-9a-f]{40})", rest)
            kind, sha = (match.group(1)[:-1], match.group(2)) if match else (None, None)
            if kind is None or objects.get(sha, ("",))[0] != kind:
                raise error(404, "Not Found")
            if kind == "commit":
                return json_response(self._commit(sha, objects[sha][1]))
            if kind == "tree":
                items = self._tree_items(repo, sha)
                return json_response({"sha": sha, "url": f"{self.api_url}/git/trees/{sha}",
                                      "tree": items[:self.tree_limit], "truncated": len(items) > self.tree_limit})
            return self._blob(req, sha, objects[sha][1])
        body = req.json() if req.body else None
        if not isinstance(body, dict):
            raise error(400, "Problems parsing JSON")
        if req.method == "POST" and rest == "/blobs":
            return self._new_blob(repo, body)
        if req.method == "POST" and rest == "/trees":
            return self._new_tree(repo, body)
        if req.method == "POST" and rest == "/commits":
            tree, parents, message = body.get("tree"), body.get("parents", []), body.get("message")
            if not isinstance(message, str) or objects.get(tree, ("",))[0] != "tree":
                raise error(422, "Tree SHA does not exist")
            if not isinstance(parents, list) or any(objects.get(p, ("",))[0] != "commit" for p in parents):
                raise error(422, "Parent SHA does not exist or is not a commit object")
            sha = self._put_commit(repo, tree, parents, message)
            return json_response(self._commit(sha, objects[sha][1]), 201)
        match = re.fullmatch(r"/refs/heads/(.+)", rest)
        if req.method == "PATCH" and match:
            return self._update_ref(repo, match.group(1), body)
        raise error(404, "Not Found")

    def _ref(self, branch: str, sha: str) -> dict[str, Any]:
        return {"ref": f"refs/heads/{branch}", "node_id": "REF_1", "url": f"{self.api_url}/git/refs/heads/{branch}",
                "object": {"type": "commit", "sha": sha, "url": f"{self.api_url}/git/commits/{sha}"}}

    def _commit(self, sha: str, commit: dict[str, Any]) -> dict[str, Any]:
        who = {"name": "Fake", "email": "fake@example.com", "date": "2026-01-01T00:00:00Z"}
        return {"sha": sha, "node_id": "C_1", "url": f"{self.api_url}/git/commits/{sha}", "html_url": "https://github.com/",
                "author": who, "committer": who, "message": commit["message"],
                "tree": {"sha": commit["tree"], "url": f"{self.api_url}/git/trees/{commit['tree']}"},
                "parents": [{"sha": p, "url": f"{self.api_url}/git/commits/{p}", "html_url": "https://github.com/"}
                            for p in commit["parents"]],
                "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None, "verified_at": None}}

    def _tree_items(self, repo: dict[str, Any], sha: str) -> list[dict[str, Any]]:
        entries, items = repo["objects"][sha][1], []
        for name in _order(entries):
            mode, child = entries[name]
            item = {"path": name, "mode": "40000" if self.git_modes and mode == "040000" else mode, "type": MODES[mode], "sha": child}
            if MODES[mode] == "blob":
                item.update(size=len(repo["objects"][child][1]), url=f"{self.api_url}/git/blobs/{child}")
            items.append(item)
        return items

    def _blob(self, req, sha: str, data: bytes) -> Response:
        self.blob_requests.append({"accept": req.header("Accept"), "range": req.header("Range"),
                                   "encoding": req.header("Accept-Encoding")})
        if req.header("Accept") != RAW_TYPE:
            return json_response({"sha": sha, "node_id": "B_1", "size": len(data), "url": f"{self.api_url}/git/blobs/{sha}",
                                  "content": base64.encodebytes(data).decode(), "encoding": "base64"})

        def pieces():  # the whole blob, in pieces: no Range (the API is said to ignore it)
            for start in range(0, len(data), 64 * 1024):
                yield data[start:start + 64 * 1024]

        return Response(200, pieces(), {"Content-Type": "application/octet-stream"}, length=len(data))

    def _new_blob(self, repo: dict[str, Any], body: dict[str, Any]) -> Response:
        content, encoding = body.get("content"), body.get("encoding", "utf-8")
        if not isinstance(content, str) or encoding not in ("utf-8", "base64"):
            raise error(422, "Invalid request.")
        try:
            data = base64.b64decode(content, validate=True) if encoding == "base64" else content.encode()
        except binascii.Error:
            raise error(422, "Invalid request: the content isn't base64.") from None
        if len(data) > self.max_blob:
            raise error(422, "The blob is too large.")
        sha = self._put_blob(repo, data)
        return json_response({"sha": sha, "url": f"{self.api_url}/git/blobs/{sha}"}, 201)

    def _new_tree(self, repo: dict[str, Any], body: dict[str, Any]) -> Response:
        objects, base, entries = repo["objects"], body.get("base_tree"), body.get("tree")
        if not isinstance(entries, list):
            raise error(422, "Invalid request.")
        if base is not None and objects.get(base, ("",))[0] != "tree":
            raise error(422, "base_tree is not a valid tree")
        root = base
        for entry in entries:
            if not isinstance(entry, dict):
                raise error(422, "Invalid request.")
            path, mode = entry.get("path"), entry.get("mode")
            parts = path.split("/") if isinstance(path, str) else [""]
            if any(part in ("", ".", "..") or part.lower() == ".git" for part in parts):
                raise error(422, f"tree.path {path!r} contains a malformed path component")
            if MODES.get(mode) is None or entry.get("type") != MODES[mode]:
                raise error(422, "tree.mode and tree.type don't agree")
            if "sha" in entry and "content" in entry:
                raise error(422, "Use either tree.sha or content, not both")
            if "content" in entry:
                if mode == "040000" or not isinstance(entry["content"], str):
                    raise error(422, "tree.content must be a string, for a blob")
                value = (mode, self._put_blob(repo, entry["content"].encode()))
            elif "sha" in entry:
                sha = entry["sha"]
                if sha is not None and MODES[mode] != "commit" and objects.get(sha, ("",))[0] != MODES[mode]:
                    raise error(422, f"tree.sha {sha} is not a valid {MODES[mode]}")
                value = None if sha is None else (mode, sha)
            else:
                raise error(422, "Must supply tree.sha or tree.content")
            root = self._change(repo, root, parts, value) or self._put_tree(repo, {})
        if root is None:
            root = self._put_tree(repo, {})
        return json_response({"sha": root, "url": f"{self.api_url}/git/trees/{root}",
                              "tree": self._tree_items(repo, root), "truncated": False}, 201)

    def _update_ref(self, repo: dict[str, Any], branch: str, body: dict[str, Any]) -> Response:
        if self.ref_errors > 0:
            self.ref_errors -= 1
            raise error(502, "Server Error")
        sha = body.get("sha")
        if branch not in repo["refs"]:
            raise error(422, "Reference does not exist")
        if repo["objects"].get(sha, ("",))[0] != "commit":
            raise error(422, "Object does not exist")
        if self.foreign_commits > 0:
            self.foreign_commits -= 1
            self.foreign += 1
            self._push(repo, branch, self.foreign_change or {f"foreign-{self.foreign}.txt": b"pushed by someone else"})
        if branch in repo["protected"]:
            raise error(422, f"Protected branch update failed for refs/heads/{branch}.")
        if not body.get("force") and not self._descends(repo, sha, repo["refs"][branch]):
            raise error(422, "Update is not a fast forward")
        repo["refs"][branch] = sha
        if self.lose_ref_answers > 0:
            self.lose_ref_answers -= 1
            raise error(502, "Server Error")
        return json_response(self._ref(branch, sha))

    # -- raw.githubusercontent.com

    def handle_raw(self, req) -> Response:
        owner, name, ref, *rest = [urllib.parse.unquote(part) for part in req.path.split("/")[1:]] + ["", "", ""]
        rest = [part for part in rest if part]
        with self.lock:
            self.raw_requests.append({"authorization": req.header("Authorization"), "range": req.header("Range")})
            repo = self.repos.get(f"{owner}/{name}".lower())
            if repo is None or repo["private"] or not rest:
                raise HTTPError(404, NOT_FOUND)
            commit = repo["objects"].get(repo["refs"].get(ref, ref))
            if commit is None or commit[0] != "commit":
                raise HTTPError(404, NOT_FOUND)
            kind, value = "tree", commit[1]["tree"]
            for part in rest:
                entries = repo["objects"][value][1] if kind == "tree" else {}
                if part not in entries or entries[part][0] == "160000":
                    raise HTTPError(404, NOT_FOUND)
                kind, value = MODES[entries[part][0]], entries[part][1]
            if kind != "blob":
                raise HTTPError(404, NOT_FOUND)
            data = repo["objects"][value][1]
        return file_response(data, req.header("Range"), {"Content-Type": "text/plain; charset=utf-8",
                                                         "Cache-Control": "max-age=300"})
