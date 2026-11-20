"""GitHub repository (https://docs.github.com/en/rest): one branch of one GitHub repository as a storage, read and
changed through GitHub's REST API with the admin's personal access token. Every change is one commit on the branch.

Docs: repository contents (https://docs.github.com/en/rest/repos/contents); the Git database
(https://docs.github.com/en/rest/guides/using-the-rest-api-to-interact-with-your-git-database): blobs
(https://docs.github.com/en/rest/git/blobs), trees (https://docs.github.com/en/rest/git/trees), commits
(https://docs.github.com/en/rest/git/commits), references (https://docs.github.com/en/rest/git/refs); repositories
(https://docs.github.com/en/rest/repos/repos#get-a-repository); rate limits
(https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api,
https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api); tokens
(https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api,
https://docs.github.com/en/rest/authentication/permissions-required-for-fine-grained-personal-access-tokens,
https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens);
file sizes (https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-large-files-on-github,
https://docs.github.com/en/repositories/working-with-files/managing-large-files/about-git-large-file-storage).

Paths
    The storage root is the folder `base_path` of the branch `branch` (empty: the repository's default branch); the
    folder must exist. Git's paths are case-sensitive: "a.txt" and "A.txt" are two files. Git keeps no empty
    folders, so a folder made here holds an empty ".gitkeep", a folder whose last entry goes gets one, and a folder
    that held only it loses it when something is put in. Listings hide every ".gitkeep" file, and it can't be a new
    name; nor can ".git", which git refuses. Submodules aren't shown; a symbolic link shows as the small file git
    keeps for it, holding the path it points to.

    Listings use the Git trees API, a folder at a time: GET git/ref/heads/<branch> gives the head commit,
    git/commits/<sha> its tree, git/trees/<sha> one folder (not recursive: a recursive tree is cut off past 100,000
    entries or 7 MB, and the contents API stops at 1,000 files a folder). Trees and commits are named by their content
    and never change, so they are cached by SHA, the trees while they hold `tree_cache_entries` entries in all (the
    least recently used go first; a bigger tree is kept alone): each change of a folder makes a new tree of it, and of
    the folders above it. Only the head is asked for again, `head_ttl` seconds after it was (a commit made here sets it
    at once; meanwhile changes pushed by others aren't seen). A folder GitHub gives only in part ("truncated") is an
    error, not a short listing. No modification times: git keeps them per commit, and finding each file's last commit
    would cost a request per file.

Login
    A fine-grained token needs the repository's Contents permission: read for the GETs, write for the POSTs and the
    PATCH (and Workflows for files in .github/workflows); a classic token, the repo scope.

Requests
    The REST API at `api_root`, or `api_url` (GitHub Enterprise Server's https://host/api/v3), with GitHub's JSON
    media type, API version 2022-11-28 and "Authorization: Bearer <token>". A token that may only read gets 403
    "Resource not accessible by personal access token" (or 404) on a change, which is told apart from a refused
    token (401).

Changes
    Each is one commit: for an upload a blob (POST git/blobs, base64 in a JSON body streamed with its exact length),
    then a tree made from the head's with the paths that change (POST git/trees with base_tree; nested paths, sha null
    deletes, a copied folder is one entry naming the same tree), a commit with the head as its parent, and PATCH
    git/refs/heads/<branch> without force. When the branch moved meanwhile (someone pushed: GitHub refuses the update as
    not a fast forward, and the head isn't ours any more), the change is checked and made again on the new head, up to
    `commit_attempts` times. When the update's answer is lost (a timeout, a 5xx), the branch is asked for: pointing at
    the new commit, the change is made. One change of a storage at a time, and its POST and PATCH requests at most
    `writes_per_second`, as GitHub asks to stay under its secondary limits (80 content-creating requests a minute, 500
    an hour). An empty repository (without a commit) answers 409: it needs a first commit, made on GitHub.

Downloads
    open(): GET git/blobs/<sha> with the raw media type, without a Range, which the API doesn't document (and is
    said to ignore): can_range is False. download_url(): for a public repository on github.com,
    https://raw.githubusercontent.com/<owner>/<repo>/<commit>/<path> with the head commit's SHA (the form of the
    contents API's download_url; the raw host itself isn't documented). That address never changes, so
    raw.githubusercontent.com's cache (minutes, for a branch's name) can't give an older version after a commit, and
    its Range works; a deleted file stays in the history, and its old links keep working. None for a private
    repository (its raw links need a token, which mustn't leave the server) and for GitHub Enterprise Server: the
    site sends the file then.

Limits
    GitHub blocks files over 100 MiB and the blob API serves up to 100 MB: larger uploads are refused before they
    are sent, and those over 50 MiB are logged, as git warns of them. Files kept in Git LFS show as their pointer
    files (version, oid, size), as the API gives them; nothing is written to LFS. A token has 5,000 requests an
    hour (each download through the site is one). When GitHub says a rate limit is reached (403 or 429 with
    x-ratelimit-remaining 0, a retry-after, or a secondary limit's message), the storage asks it nothing more until
    it may again (the reset time, the retry-after, or a minute): GitHub may ban a client that goes on. Listings
    keep the last head meanwhile.

    Not tried against GitHub itself: the tests use a fake built from the documentation above.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import logging
import re
import threading
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import RateLimiter, TTLCache, as_int, exact_body, json_or_none, response_stream, short, transport_error, usable_name
from .base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
)

SERVICE = gettext_lazy("GitHub")
API_VERSION = "2022-11-28"
JSON_TYPE = "application/vnd.github+json"
RAW_TYPE = "application/vnd.github.raw+json"
PLACEHOLDER = ".gitkeep"  # what keeps an empty folder in git; listings hide it

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")  # GitHub's user and organisation names
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # SHA-1, or SHA-256
_API_URL = re.compile(r"https?://[^\s/?#]+(?:/[^\s?#]*)?")
_KINDS = {"100644": "blob", "100755": "blob", "120000": "blob", "040000": "tree", "160000": "commit"}  # mode -> type
_BLOB_HEAD = b'{"encoding":"base64","content":"'
_BLOB_TAIL = b'"}'
_MIB = 1024 * 1024

_log = logging.getLogger("hfile.storage")


@dataclasses.dataclass(frozen=True)
class _Item:
    """An entry of a git tree: a file (blob), a folder (tree) or a submodule (commit)."""

    name: str
    mode: str
    type: str
    sha: str
    size: int | None


@dataclasses.dataclass(frozen=True)
class _Head:
    """The branch's newest commit, and the tree of the whole repository in it."""

    commit: str
    tree: str


def _item(raw: Any) -> _Item | None:
    """An entry of a tree in an answer of the API, or None when it isn't one."""
    if not isinstance(raw, dict):
        return None
    name, mode, sha = raw.get("path"), raw.get("mode"), raw.get("sha")
    mode = "040000" if mode == "40000" else mode  # GitHub's docs write a folder's mode so; git itself writes 40000
    kind = _KINDS.get(mode) if isinstance(mode, str) else None
    if (not isinstance(name, str) or not name or kind is None or raw.get("type") != kind
            or not isinstance(sha, str) or not _SHA.fullmatch(sha)):
        return None
    return _Item(name=name, mode=mode, type=kind, sha=sha, size=as_int(raw.get("size")) if kind == "blob" else None)


def _blob_body(data: BinaryIO, size: int) -> Iterator[bytes]:
    """The JSON body of POST git/blobs: `size` bytes of `data` in base64, encoded as they are read."""
    yield _BLOB_HEAD
    rest = b""
    for piece in exact_body(data, size):
        piece = rest + piece
        cut = len(piece) - len(piece) % 3
        rest = piece[cut:]
        if cut:
            yield base64.b64encode(piece[:cut])
    if rest:
        yield base64.b64encode(rest)
    yield _BLOB_TAIL


class _Trees:
    """The trees fetched (sha -> {name: _Item}), while they hold at most `limit` entries in all: the least recently
    used go first, and a tree bigger than that is kept alone. A git object never changes: nothing expires."""

    def __init__(self, limit: int):
        self._limit = limit
        self._lock = threading.Lock()
        self._data: OrderedDict[str, dict[str, _Item]] = OrderedDict()
        self._entries = 0

    def get(self, sha: str) -> dict[str, _Item] | None:
        with self._lock:
            items = self._data.get(sha)
            if items is not None:
                self._data.move_to_end(sha)
            return items

    def put(self, sha: str, items: dict[str, _Item]) -> None:
        with self._lock:
            old = self._data.pop(sha, None)
            if old is not None:
                self._entries -= max(len(old), 1)
            self._data[sha] = items
            self._entries += max(len(items), 1)
            while self._entries > self._limit and len(self._data) > 1:
                _, dropped = self._data.popitem(last=False)
                self._entries -= max(len(dropped), 1)


def _limit_wait(response: httpx.Response, message: str) -> tuple[float, int | None] | None:
    """When GitHub's answer says a rate limit is reached (403 or 429 with x-ratelimit-remaining 0, a retry-after, or a
    secondary limit's message `message`): the seconds not to ask GitHub anything, and the Unix time its primary limit
    resets when that one is used up (else None). None when no limit is reached. GitHub's rule
    (https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api): wait the retry-after, else until
    x-ratelimit-reset when none are left (a minute at least), else a minute."""
    status, headers = response.status_code, response.headers
    remaining = headers.get("x-ratelimit-remaining")
    if not (status == 429 or (status == 403 and (remaining == "0" or "retry-after" in headers
                                                 or "rate limit" in message.lower()))):
        return None
    wait, reset = as_int(headers.get("retry-after")), as_int(headers.get("x-ratelimit-reset"))
    if wait is None and remaining == "0" and reset:
        return max(reset - time.time(), 60.0), reset
    return (float(wait) if wait is not None and wait > 0 else 60.0), None


class GitHubRepoDriver(StorageDriver):
    type = "github-repo"
    label = gettext_lazy("GitHub repository")
    params = (
        Param("owner", gettext_lazy("Repository owner"), placeholder="octocat",
              help=gettext_lazy("The user or organization the repository belongs to: the first part of "
                                "github.com/owner/repository.")),
        Param("repo", gettext_lazy("Repository"), placeholder="files",
              help=gettext_lazy("The repository's name: the second part of github.com/owner/repository.")),
        Param("token", gettext_lazy("Token"), kind="password", secret=True,
              help=gettext_lazy("A personal access token of a GitHub account that may push to the repository. A "
                                "fine-grained one (https://github.com/settings/personal-access-tokens/new): give "
                                "it this repository, with the repository permission Contents set to Read and write; "
                                "with Read-only the storage can only be read, and changes are refused. A classic one "
                                "(https://github.com/settings/tokens): the repo scope.")),
        Param("branch", gettext_lazy("Branch"), required=False,
              help=gettext_lazy("The branch shown and changed; every change is a commit on it. Empty: the "
                                "repository's default branch.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the repository that is this storage source's root, like / or "
                                "/folder1. It must already exist.")),
        Param("api_url", gettext_lazy("API address"), required=False, advanced=True,
              placeholder="https://github.example.com/api/v3",
              help=gettext_lazy("Only for GitHub Enterprise Server: its REST API address, like "
                                "https://github.example.com/api/v3. Empty: GitHub.com. Files of GitHub Enterprise "
                                "Server are always downloaded through this site.")),
    )

    can_range = False  # see open()
    can_direct_download = True  # public repositories on github.com only, see download_url()
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    setup_params = ("owner", "repo", "token", "branch", "api_url")

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://api.github.com"
    raw_root: ClassVar[str] = "https://raw.githubusercontent.com"
    head_ttl: ClassVar[float] = 30.0  # seconds the branch's head is trusted
    repo_ttl: ClassVar[float] = 300.0  # the repository's visibility and default branch
    commit_attempts: ClassVar[int] = 3  # a change made again on a branch that moved meanwhile
    writes_per_second: ClassVar[float] = 1.0  # POST and PATCH requests, as GitHub asks
    wait_longest: ClassVar[float] = 30.0  # seconds a change may wait for its turn
    max_file_size: ClassVar[int] = 100 * _MIB  # GitHub blocks larger files
    warn_file_size: ClassVar[int] = 50 * _MIB  # git warns of larger ones
    tree_cache_entries: ClassVar[int] = 100_000  # of the trees kept, in all: about 50 MB

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._owner = str(self.config.get("owner") or "").strip()
        self._repo = str(self.config.get("repo") or "").strip()
        self._name = f"{self._owner}/{self._repo}"
        self._token = str(self.config.get("token") or "").strip()
        self._branch_name = str(self.config.get("branch") or "").strip()
        self._base = paths.clean(self.config.get("base_path") or "/")
        own_api = str(self.config.get("api_url") or "").strip().rstrip("/")
        self._on_github = not own_api or own_api == self.api_root.rstrip("/")  # raw.githubusercontent.com's links
        self._repo_url = f"{own_api or self.api_root.rstrip('/')}/repos/{self._owner}/{self._repo}"
        self._http = http.client()
        self._lock = threading.Lock()  # the head and the rate limit's hold
        self._commit_lock = threading.Lock()  # one change at a time
        self._head_cache: tuple[float, _Head] | None = None  # (time.monotonic(), head)
        self._head_version = 0  # one more for each commit made here
        self._held: tuple[float, str] | None = None  # (time.monotonic() until, message): GitHub's rate limit
        self._trees = _Trees(self.tree_cache_entries)
        self._commits = TTLCache(64)  # commit sha -> tree sha
        self._about = TTLCache(1, self.repo_ttl)  # "repo" -> (private, default branch)
        self._writes = RateLimiter(self.writes_per_second, longest=self.wait_longest, service=SERVICE)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if not _OWNER.fullmatch(str(config.get("owner") or "").strip()):
            errors["owner"] = gettext("The owner is a GitHub user or organization name, as in github.com/owner/repository.")
        repo = str(config.get("repo") or "").strip()
        if not _REPO.fullmatch(repo) or repo in (".", "..") or repo.lower().endswith(".git"):
            errors["repo"] = gettext("The repository is its name as in github.com/owner/repository, without .git.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        api = str(config.get("api_url") or "").strip()
        if api and not _API_URL.fullmatch(api):
            errors["api_url"] = gettext("The API address should start with http:// or https://, like https://github.example.com/api/v3.")
        return errors

    # -- requests

    def _send(self, method: str, url: str, *, accept: str = JSON_TYPE, json: Any = None, content: Any = None,
              extra: Mapping[str, str] | None = None, stream: bool = False) -> httpx.Response:
        """A request of the API with the token. BackendError while GitHub's rate limit holds."""
        with self._lock:
            held = self._held
        if held is not None and time.monotonic() < held[0]:
            raise BackendError(held[1])
        if method != "GET":
            self._writes.wait()
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION, "Authorization": f"Bearer {self._token}",
                   **(extra or {})}
        try:
            return self._http.send(self._http.build_request(method, url, headers=headers, json=json, content=content),
                                   stream=stream)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _json(self, method: str, url: str, *, ok: tuple[int, ...] = (200,), **kwargs: Any) -> dict[str, Any]:
        """The JSON object of an answer with a status in `ok`; DriverError otherwise."""
        response = self._send(method, url, **kwargs)
        if response.status_code not in ok:
            raise self._error(response, changing=method != "GET")
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            raise BackendError(gettext("The answer from GitHub couldn't be understood. Please try again later."))
        return answer

    @staticmethod
    def _sha(value: Any) -> str:
        if not isinstance(value, str) or not _SHA.fullmatch(value):
            raise BackendError(gettext("The answer from GitHub couldn't be understood. Please try again later."))
        return value

    def _error(self, response: httpx.Response, *, changing: bool = False) -> DriverError:
        """The DriverError for an answer that isn't the one asked for. `changing`: of a POST or PATCH."""
        status = response.status_code
        answer = json_or_none(response)
        message = short(answer.get("message")) if isinstance(answer, dict) else ""
        if status == 401:
            return AuthError(gettext("GitHub refused the token. Please check it in the storage source's settings."))
        limited = self._limited(response, message)
        if limited is not None:
            return limited
        if 300 <= status < 400:
            return DriverError(gettext("GitHub says the repository %(repo)s has moved (renamed or transferred): please "
                                       "put its new owner and name in the storage source's settings.") % {"repo": self._name})
        # "Resource not accessible by personal access token": a permission the token lacks. A change GitHub
        # answers with 404 is one the token may not make (it saw the repository to get this far).
        if (status == 403 and "not accessible" in message.lower()) or (changing and status == 404):
            return DriverError(gettext(
                "GitHub didn't let the token do this. A fine-grained token needs access to this repository with the "
                "Contents permission set to Read and write (Read-only can only read), and Workflows too for files in "
                ".github/workflows; a classic token needs the repo scope, and workflow for those files."))
        if status == 404:
            return NotFound(gettext("The repository %(repo)s doesn't exist, or the token can't see it.") % {"repo": self._name})
        if status == 409:
            return DriverError(gettext("The repository %(repo)s is empty, or GitHub is still making it: give it a first "
                                       "commit on GitHub (a README, say), then try again.") % {"repo": self._name})
        if status < 500 and message:
            return DriverError(gettext("GitHub refused this: %(message)s") % {"message": message})
        detail = (gettext(" (%(code)s): %(message)s") % {"code": f"HTTP {status}", "message": message} if message
                  else gettext(" (HTTP %(status)s)") % {"status": status})
        return BackendError(gettext("GitHub failed%(detail)s. Please try again later.") % {"detail": detail})

    def _limited(self, response: httpx.Response, message: str) -> BackendError | None:
        """The BackendError of a rate limit GitHub says is reached, or None. Until GitHub may be asked again it
        isn't (_send): it asks clients not to, and may ban one that goes on."""
        found = _limit_wait(response, message)
        if found is None:
            return None
        seconds, reset = found
        if reset is not None:
            moment = datetime.datetime.fromtimestamp(reset, datetime.timezone.utc).strftime("%H:%M")
            error = BackendError(gettext("GitHub's rate limit for this token is used up until %(time)s (UTC). Please "
                                         "try again then.") % {"time": moment})
        else:
            error = BackendError(gettext("GitHub is holding back the requests of this storage for now. Please try again "
                                         "in %(seconds)s seconds.") % {"seconds": int(seconds)})
        with self._lock:
            self._held = (time.monotonic() + min(seconds, 3600.0), error.message)
        return error

    def _held_back(self) -> bool:
        with self._lock:
            return self._held is not None and time.monotonic() < self._held[0]

    def _about_repo(self) -> tuple[bool, str]:
        """Whether the repository is private (or can't be told), and its default branch."""
        cached = self._about.get("repo")
        if cached is None:
            answer = self._json("GET", self._repo_url)
            default = answer.get("default_branch")
            cached = (answer.get("private") is not False, default if isinstance(default, str) else "")
            self._about.put("repo", cached)
        return cached

    def _branch(self) -> str:
        branch = self._branch_name or self._about_repo()[1]
        if not branch:
            raise BackendError(gettext("The answer from GitHub couldn't be understood. Please try again later."))
        return branch

    @staticmethod
    def _ref_path(branch: str) -> str:
        """heads/<branch> for the refs API, each part of the name percent-encoded."""
        return "heads/" + "/".join(urllib.parse.quote(part, safe="") for part in branch.split("/"))

    def _head(self, *, fresh: bool = False) -> _Head:
        """The branch's head: the one known when it is younger than head_ttl seconds (or GitHub's rate limit
        holds), else GitHub's."""
        with self._lock:
            cached, version = self._head_cache, self._head_version
        if cached is not None and not fresh and (time.monotonic() - cached[0] < self.head_ttl or self._held_back()):
            return cached[1]
        branch = self._branch()
        try:
            ref = self._json("GET", f"{self._repo_url}/git/ref/{self._ref_path(branch)}")
        except NotFound:
            self._about_repo()  # NotFound of its own when the repository isn't there
            raise NotFound(gettext("The repository %(repo)s has no branch %(branch)s.") % {"repo": self._name, "branch": branch}) from None
        target = ref.get("object")
        commit = self._sha(target.get("sha") if isinstance(target, dict) else None)
        tree = self._commits.get(commit)
        if tree is None:
            answer = self._json("GET", f"{self._repo_url}/git/commits/{commit}")
            tree = self._sha(answer["tree"].get("sha") if isinstance(answer.get("tree"), dict) else None)
            self._commits.put(commit, tree)
        head = _Head(commit, tree)
        with self._lock:
            if self._head_version == version:  # else a commit made here meanwhile is newer
                self._head_cache = (time.monotonic(), head)
        return head

    def _set_head(self, commit: str, tree: str) -> None:
        self._commits.put(commit, tree)
        with self._lock:
            self._head_cache = (time.monotonic(), _Head(commit, tree))
            self._head_version += 1

    # -- trees

    def _tree(self, sha: str, folder: str) -> dict[str, _Item]:
        """The entries of the tree `sha` by name (the folder `folder`, for messages); a git object never changes."""
        items = self._trees.get(sha)
        if items is None:
            answer = self._json("GET", f"{self._repo_url}/git/trees/{sha}")
            if answer.get("truncated"):
                raise BackendError(gettext("GitHub gives only a part of the folder %(path)s: it holds too many entries.") % {"path": folder})
            items = {}
            for raw in answer.get("tree") if isinstance(answer.get("tree"), list) else []:
                item = _item(raw)
                if item is not None:
                    items[item.name] = item
            self._trees.put(sha, items)
        return items

    def _base_tree(self, head: _Head) -> str:
        """The tree of the folder base_path in `head`. NotFound when the branch has no such folder."""
        sha = head.tree
        for name in paths.segments(self._base):
            item = self._tree(sha, paths.ROOT).get(name)
            if item is None or item.type != "tree":
                raise NotFound(gettext("The branch %(branch)s of %(repo)s has no folder %(path)s.")
                               % {"branch": self._branch(), "repo": self._name, "path": self._base})
            sha = item.sha
        return sha

    def _folder_tree(self, head: _Head, path: str) -> str | None:
        """The tree of the folder at `path`, or None when there's none (nothing there, or a file on the way)."""
        sha, folder = self._base_tree(head), paths.ROOT
        for name in paths.segments(path):
            item = self._tree(sha, folder).get(name)
            if item is None or item.type != "tree":
                return None
            sha, folder = item.sha, paths.join(folder, name)
        return sha

    def _find(self, head: _Head, path: str) -> _Item | None:
        """The entry at `path` as git has it, hidden ones too; the root is a folder."""
        if path == paths.ROOT:
            return _Item("", "040000", "tree", self._base_tree(head), None)
        parent, name = paths.split(path)
        sha = self._folder_tree(head, parent)
        return None if sha is None else self._tree(sha, parent).get(name)

    @staticmethod
    def _shown(item: _Item) -> bool:
        """Whether listings show the entry: not a submodule, a placeholder or a name that can't be a path segment."""
        if item.type == "commit" or not usable_name(item.name):
            return False
        return not (item.type == "blob" and item.name == PLACEHOLDER)

    def _source(self, head: _Head, path: str) -> _Item:
        """The shown entry at `path` (not the root) that a change starts from. NotFound."""
        item = self._find(head, path) if path != paths.ROOT else None
        if item is None or not self._shown(item):
            raise NotFound()
        return item

    def _file(self, head: _Head, path: str) -> _Item:
        item = self._source(head, path)
        if item.type != "blob":
            raise NotFound()
        return item

    @staticmethod
    def _entry(path: str, item: _Item) -> Entry:
        is_dir = item.type == "tree"
        return Entry(name=paths.name(path), path=path, is_dir=is_dir, size=None if is_dir else item.size)

    # -- reading

    def check(self) -> None:
        self._about_repo()
        self.list(paths.ROOT)

    def list(self, path: str) -> list[Entry]:
        head = self._head()
        sha = self._folder_tree(head, path)
        if sha is None:
            item = self._find(head, path)
            if item is not None and self._shown(item):
                raise NotADirectory()
            raise NotFound()
        return [self._entry(paths.join(path, item.name), item) for item in self._tree(sha, path).values()
                if self._shown(item)]

    def stat(self, path: str) -> Entry:
        head = self._head()
        if path == paths.ROOT:
            self._base_tree(head)
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._source(head, path))

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(self._head(), path)
        response = self._send("GET", f"{self._repo_url}/git/blobs/{item.sha}", accept=RAW_TYPE,
                              extra={"Accept-Encoding": "identity"}, stream=True)
        if response.status_code != 200:
            raise self._error(http.read_error(response))
        return response_stream(response, offset=offset, length=length, total=item.size, service=SERVICE)

    def _repo_path(self, path: str) -> str:
        """The path in the repository of a storage path, as git's trees name it: "" for the repository's root."""
        return paths.rebase(path, self._base).lstrip("/")

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        if not self._on_github or self._about_repo()[0]:
            return None
        head = self._head()
        self._file(head, path)
        return f"{self.raw_root}/{self._owner}/{self._repo}/{head.commit}/{urllib.parse.quote(self._repo_path(path), safe='/')}"

    # -- writing

    def _removal(self, path: str, item: _Item) -> dict[str, Any]:
        return {"path": self._repo_path(path), "mode": item.mode, "type": item.type, "sha": None}

    def _placed(self, path: str, item: _Item) -> dict[str, Any]:
        return {"path": self._repo_path(path), "mode": item.mode, "type": item.type, "sha": item.sha}

    def _placeholder(self, folder: str) -> dict[str, Any]:
        return {"path": self._repo_path(paths.join(folder, PLACEHOLDER)), "mode": "100644", "type": "blob", "content": ""}

    def _keep(self, head: _Head, folder: str, name: str) -> list[dict[str, Any]]:
        """A placeholder for `folder` when `name`, all it holds, goes: git keeps no empty folder."""
        sha = self._folder_tree(head, folder)
        return [self._placeholder(folder)] if sha is not None and list(self._tree(sha, folder)) == [name] else []

    def _unkeep(self, head: _Head, path: str) -> list[dict[str, Any]]:
        """The removal of the placeholder of the nearest folder above `path` that exists, when it holds nothing else:
        it gets something now."""
        sha, folder = self._base_tree(head), paths.ROOT
        for name in paths.segments(paths.parent(path)):
            item = self._tree(sha, folder).get(name)
            if item is None or item.type != "tree":
                break
            sha, folder = item.sha, paths.join(folder, name)
        items = self._tree(sha, folder)
        kept = items.get(PLACEHOLDER)
        if len(items) == 1 and kept is not None and kept.type == "blob":
            return [self._removal(paths.join(folder, PLACEHOLDER), kept)]
        return []

    def _writable(self, head: _Head, path: str) -> _Item | None:
        """The file at `path` or None, where something new goes. AlreadyExists for a folder (or a submodule) there,
        NotADirectory for a file above it."""
        if path == paths.ROOT:
            raise AlreadyExists()
        sha, folder = self._base_tree(head), paths.ROOT
        for name in paths.segments(paths.parent(path)):
            item = self._tree(sha, folder).get(name)
            folder = paths.join(folder, name)
            if item is None:
                return None  # made with it
            if item.type != "tree":
                raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": folder})
            sha = item.sha
        item = self._tree(sha, folder).get(paths.name(path))
        if item is not None and item.type != "blob":
            raise AlreadyExists()
        return item

    @staticmethod
    def _new_name(name: str) -> None:
        """Refuse a name this storage can't give something new."""
        if name == PLACEHOLDER:
            raise InvalidPath(gettext('"%(name)s" is kept for the empty folders of this storage; please choose another '
                                      'name.') % {"name": name})
        if name.casefold() == ".git":
            raise InvalidPath(gettext('Git doesn\'t allow a file or folder named "%(name)s".') % {"name": name})

    def _landed(self, commit: str, tree: str) -> bool:
        """Whether the branch points at `commit` now, the update to it sent without an answer that says so (a timeout,
        a 5xx); if it does, that is the head known."""
        try:
            head = self._head(fresh=True)
        except DriverError:
            return False
        if head.commit != commit:
            return False
        self._set_head(commit, tree)
        return True

    def _commit(self, plan: Callable[[_Head], list[dict[str, Any]]], message: str) -> None:
        """Make a change one commit on the branch. `plan(head)` checks the change against `head` and gives the entries
        of the new tree ([]: nothing to change); it runs again on the new head when the branch moved meanwhile."""
        if not self._commit_lock.acquire(timeout=self.wait_longest):
            raise BackendError(gettext("Too many requests to %(service)s; they're queued by its rate limit. Please try "
                                       "again later.") % {"service": SERVICE})
        try:
            head = self._head()
            for _ in range(self.commit_attempts):
                entries = plan(head)
                if not entries:
                    return
                tree = self._sha(self._json("POST", f"{self._repo_url}/git/trees", ok=(201,),
                                            json={"base_tree": head.tree, "tree": entries}).get("sha"))
                commit = self._sha(self._json("POST", f"{self._repo_url}/git/commits", ok=(201,),
                                              json={"message": message, "tree": tree, "parents": [head.commit]}).get("sha"))
                try:
                    response = self._send("PATCH", f"{self._repo_url}/git/refs/{self._ref_path(self._branch())}",
                                          json={"sha": commit, "force": False})
                except BackendError:  # its answer lost (or it wasn't sent): GitHub may have made the update
                    if self._landed(commit, tree):
                        return
                    raise
                if response.status_code >= 500 and self._landed(commit, tree):
                    return
                if response.status_code == 200:
                    self._set_head(commit, tree)
                    return
                if response.status_code not in (409, 422):  # not a fast forward: 422, or perhaps 409 (both documented)
                    raise self._error(response, changing=True)
                moved = self._head(fresh=True)
                if moved.commit == head.commit:  # refused for another reason: a protected branch, a rule
                    raise self._error(response, changing=True)
                head = moved
            raise BackendError(gettext("The branch kept changing while this change was being saved. Please try again."))
        finally:
            self._commit_lock.release()

    def _upload(self, data: BinaryIO, size: int) -> str:
        """The SHA of a new blob of `size` bytes from `data`."""
        length = len(_BLOB_HEAD) + (size + 2) // 3 * 4 + len(_BLOB_TAIL)
        answer = self._json("POST", f"{self._repo_url}/git/blobs", ok=(201,), content=_blob_body(data, size),
                            extra={"Content-Type": "application/json", "Content-Length": str(length)})
        return self._sha(answer.get("sha"))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        self._new_name(paths.name(path))
        if size > self.max_file_size:
            raise DriverError(gettext("GitHub takes no files over %(limit)s MB in a repository; this one is %(size)s MB.")
                              % {"limit": self.max_file_size // _MIB, "size": f"{size / _MIB:.1f}"})
        if size > self.warn_file_size:
            _log.warning("GitHub repository %s: %s has %s bytes; GitHub advises against files over 50 MiB",
                         self._name, self._repo_path(path), size)
        self._writable(self._head(), path)  # before the file is sent
        blob = self._upload(data, size) if size else None

        def plan(head: _Head) -> list[dict[str, Any]]:
            old = self._writable(head, path)
            entry = {"path": self._repo_path(path), "mode": "100755" if old is not None and old.mode == "100755" else "100644",
                     "type": "blob", **({"sha": blob} if blob else {"content": ""})}
            return [entry, *self._unkeep(head, path)]

        self._commit(plan, f"HFile: upload {self._repo_path(path)}")

    def mkdir(self, path: str) -> None:
        self._new_name(paths.name(path))

        def plan(head: _Head) -> list[dict[str, Any]]:
            if self._writable(head, path) is not None:
                raise AlreadyExists()
            return [self._placeholder(path), *self._unkeep(head, path)]

        self._commit(plan, f"HFile: create folder {self._repo_path(path)}")

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))

        def plan(head: _Head) -> list[dict[str, Any]]:
            item = self._source(head, path)
            return [self._removal(path, item), *self._keep(head, paths.parent(path), item.name)]

        self._commit(plan, f"HFile: delete {self._repo_path(path)}")

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        self._new_name(new_name)
        target = paths.join(paths.parent(path), new_name)

        def plan(head: _Head) -> list[dict[str, Any]]:
            item = self._source(head, path)
            if new_name == item.name:
                return []
            if self._find(head, target) is not None:
                raise AlreadyExists()
            return [self._removal(path, item), self._placed(target, item)]

        self._commit(plan, f"HFile: rename {self._repo_path(path)} to {new_name}")

    def _relocate(self, path: str, folder: str, *, copy: bool) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy
                              else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy
                              else gettext("A folder can't be moved into itself."))
        target = paths.join(folder, paths.name(path))

        def plan(head: _Head) -> list[dict[str, Any]]:
            item = self._source(head, path)
            if self._folder_tree(head, folder) is None:
                found = self._find(head, folder)
                raise NotADirectory() if found is not None and self._shown(found) else NotFound()
            if not copy and paths.parent(path) == folder:
                return []  # already there
            if self._find(head, target) is not None:
                raise AlreadyExists()
            entries = [self._placed(target, item), *self._unkeep(head, target)]
            if not copy:
                entries += [self._removal(path, item), *self._keep(head, paths.parent(path), item.name)]
            return entries

        what = "copy" if copy else "move"
        self._commit(plan, f"HFile: {what} {self._repo_path(path)} to {self._repo_path(folder) or '/'}")

    def move(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, folder, copy=True)
