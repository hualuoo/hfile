"""GitHub Releases (https://docs.github.com/en/rest/releases): the releases of GitHub repositories, as a
read-only storage.

Paths
    `repositories` names them, one per line: "owner/repo" shows the repository at the root, "a/b:owner/repo"
    in the folder /a/b. The folders above a repository's folder are made up: they hold what is below them. In
    a repository's folder are the files (assets) of its latest release, whatever its tag (kustomize's are like
    "kustomize/v5.8.0"); with `all_releases`, a folder per release instead, named after its tag, with that
    release's files, and a release whose tag can't be a folder's name (a tag with "/" in it) isn't shown.
    Drafts aren't shown, nor a file whose name can't be a path segment. Where a file has the name of a folder
    made up for another repository, the folder is shown.

Requests
    The REST API at `api_root`, with GitHub's JSON media type and API version; with a `token`, as
    "Authorization: Bearer". Releases come from /repos/{owner}/{repo}/releases/latest, or with all_releases
    from /releases, 100 a page following the Link header, at most `max_pages` pages (the newest releases).
    Answers are cached for `cache_ttl` seconds: without a token GitHub allows 60 requests an hour per IP
    address, with one 5,000. When GitHub says a rate limit is reached (403 or 429 with x-ratelimit-remaining
    0, a Retry-After, or a secondary limit's message), the storage asks the API nothing more until it may
    again (the reset time, the Retry-After, or a minute: github_repo.py's rule, as GitHub asks; it may ban a
    client that goes on), a BackendError saying when. A refused token (401) is an AuthError; a repository
    GitHub doesn't show (404: it doesn't exist, or it is private and there is no token that can see it) a
    NotFound naming it.

Downloads
    download_url(): the file's browser_download_url (https://github.com/owner/repo/releases/download/...),
    which sends the browser on to GitHub's file host; through `download_mirror` when set (a GitHub mirror
    like https://ghproxy.net/https://github.com, for places where github.com is slow). Only for public
    repositories: a private one's needs the token, so it gives None and the site sends the file. open() of a
    public repository's file GETs its browser_download_url (github.com itself, not through download_mirror),
    which isn't a request of the REST API; redirects are followed, without the token. A private one's:
    /releases/assets/{id} with "Accept: application/octet-stream", which redirects to the file host; that
    host is asked without the token. Both with the Range.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import TTLCache, as_int, json_or_none, parse_time, range_header, response_stream, short, transport_error, usable_name
from .base import (
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
from .github_repo import _limit_wait

SERVICE = gettext_lazy("GitHub")

_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?")  # GitHub's user and organisation names
_REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


@dataclasses.dataclass(frozen=True)
class _Mount:
    path: str  # storage path of the repository's folder
    owner: str
    repo: str

    @property
    def name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclasses.dataclass(frozen=True)
class _Asset:
    id: int | str
    name: str
    size: int | None
    modified: datetime.datetime | None
    url: str  # browser_download_url
    tag: str = ""  # of its release


@dataclasses.dataclass(frozen=True)
class _Release:
    tag: str
    published: datetime.datetime | None
    assets: Mapping[str, _Asset]


def parse_repositories(text: Any, *, nested: bool = False) -> tuple[list[_Mount], list[str]]:
    """The mounts of the `repositories` param, and what is wrong with it (messages). `nested`: the owner may be
    a path of groups ("org/group" in "org/group/repo", as CNB has)."""
    mounts: list[_Mount] = []
    errors: list[str] = []
    seen: set[str] = set()
    lines = text.splitlines() if isinstance(text, str) else []
    for number, line in enumerate(lines, start=1):
        line = line.strip()
        if not line:
            continue
        folder, _, repository = line.rpartition(":")
        owner, slash, repo = repository.strip().rpartition("/") if nested else repository.strip().partition("/")
        good_owner = (all(_REPO.fullmatch(part) and part not in (".", "..") for part in owner.split("/")) if nested
                      else _OWNER.fullmatch(owner))
        if not slash or not good_owner or not _REPO.fullmatch(repo) or repo in (".", ".."):
            errors.append(gettext("Line %(line)s: write owner/repo, or folder/path:owner/repo.") % {"line": number})
            continue
        try:
            path = paths.clean("/" + folder.strip().strip("/"))
        except InvalidPath as error:
            errors.append(gettext("Line %(line)s: the folder is wrong: %(message)s") % {"line": number, "message": error.message})
            continue
        if path in seen:
            errors.append(gettext("Line %(line)s: %(path)s already has a repository.") % {"line": number, "path": path})
            continue
        seen.add(path)
        mounts.append(_Mount(path, owner, repo))
    if not mounts and not errors:
        errors.append(gettext("Give at least one repository."))
    return mounts, errors


def _release(item: Any) -> _Release | None:
    """A release of an API answer, or None for a draft or one without a tag. Its tag may not be a folder's name (it
    has "/"): only all_releases needs one."""
    if not isinstance(item, dict) or item.get("draft"):
        return None
    tag = item.get("tag_name")
    if not isinstance(tag, str) or not tag:
        return None
    assets: dict[str, _Asset] = {}
    for raw in item.get("assets") or []:
        if not isinstance(raw, dict):
            continue
        name, asset_id = raw.get("name"), as_int(raw.get("id"))
        if asset_id is None and isinstance(raw.get("id"), str) and raw["id"]:
            asset_id = raw["id"]  # CNB's ids are strings
        url = raw.get("browser_download_url")
        if not usable_name(name) or asset_id is None or not isinstance(url, str):
            continue
        assets[name] = _Asset(id=asset_id, name=name, size=as_int(raw.get("size")),
                              modified=parse_time(raw.get("updated_at") or raw.get("created_at")), url=url, tag=tag)
    return _Release(tag=tag, published=parse_time(item.get("published_at") or item.get("created_at")), assets=assets)


class GitHubReleasesDriver(StorageDriver):
    type = "github-releases"
    label = gettext_lazy("GitHub Releases")
    service: ClassVar[str] = SERVICE
    params = (
        Param("repositories", gettext_lazy("Repositories"), kind="textarea",
              placeholder="OpenListTeam/OpenList\ntools/rclone:rclone/rclone",
              help=gettext_lazy("One per line: owner/repo shows the repository at the root, folder/path:owner/repo in that folder.")),
        Param("token", gettext_lazy("Token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Optional: a GitHub personal access token (https://github.com/settings/tokens), for private "
                                "repositories, and 5,000 requests an hour instead of 60. Read access to the repositories' "
                                "contents is enough.")),
        Param("all_releases", gettext_lazy("All releases"), kind="bool", required=False, default=False,
              help=gettext_lazy("Show every release as a folder named after its tag. Off: only the files of each repository's latest release.")),
        Param("download_mirror", gettext_lazy("Download mirror"), required=False, advanced=True,
              placeholder="https://ghproxy.net/https://github.com",
              help=gettext_lazy("Optional, for files of public repositories: a GitHub mirror that download links go "
                                "through instead of https://github.com, for places where GitHub is slow.")),
    )

    can_range = True
    can_direct_download = True  # public repositories only, see download_url()
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    setup_params = ("repositories", "token")
    nested_owners: ClassVar[bool] = False  # see parse_repositories()

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://api.github.com"
    web_root: ClassVar[str] = "https://github.com"  # where browser_download_url points, for download_mirror
    cache_ttl: ClassVar[float] = 300.0
    max_pages: ClassVar[int] = 10  # of 100 releases

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._mounts, _ = parse_repositories(self.config.get("repositories"), nested=self.nested_owners)
        self._token = str(self.config.get("token") or "").strip()
        self._all = bool(self.config.get("all_releases"))
        self._http = http.client()
        self._cache = TTLCache(256, self.cache_ttl)  # ("releases" | "private", owner/repo) -> answer
        self._lock = threading.Lock()  # the rate limit's hold
        self._held: tuple[float, str] | None = None  # (time.monotonic() until, message): GitHub's rate limit

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        _, problems = parse_repositories(config.get("repositories"), nested=cls.nested_owners)
        if problems:
            errors["repositories"] = " ".join(problems)
        mirror = str(config.get("download_mirror") or "").strip()
        if mirror and not re.fullmatch(r"https?://[^\s/]+(/\S*)?", mirror):
            errors["download_mirror"] = gettext("The download mirror should be an http:// or https:// address.")
        return errors

    # -- requests

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _not_held(self) -> None:
        """BackendError while a rate limit GitHub named holds: it asks clients to send it nothing meanwhile."""
        with self._lock:
            held = self._held
        if held is not None and time.monotonic() < held[0]:
            raise BackendError(held[1])

    def _limited(self, response: httpx.Response, message: str) -> BackendError | None:
        """The BackendError of a rate limit GitHub says is reached (github_repo.py's rule), or None; until it resets
        the API isn't asked (_not_held())."""
        found = _limit_wait(response, message)
        if found is None:
            return None
        seconds, reset = found
        if reset is not None:
            moment = datetime.datetime.fromtimestamp(reset, datetime.timezone.utc).strftime("%H:%M")
            text = (gettext("GitHub's rate limit for this token is used up until %(time)s (UTC). Please try again then.")
                    if self._token else
                    gettext("GitHub's rate limit is reached until %(time)s (UTC). A token raises it from 60 to 5,000 requests an hour."))
            error = BackendError(text % {"time": moment})
        else:
            error = BackendError(gettext("GitHub is holding back the requests of this storage for now. Please try again "
                                         "in %(seconds)s seconds.") % {"seconds": int(seconds)})
        with self._lock:
            self._held = (time.monotonic() + min(seconds, 3600.0), error.message)
        return error

    def _error(self, response: httpx.Response, mount: _Mount | None, message: str | None = None) -> DriverError:
        """The DriverError of an answer that isn't the one asked for. `message`: GitHub's, when the body was read
        already ("" when it isn't wanted)."""
        status = response.status_code
        if message is None:
            answer = json_or_none(response)
            message = short(answer.get("message")) if isinstance(answer, dict) else ""
        if status == 401:
            return AuthError(gettext("GitHub refused the token. Please check it in the storage source's settings."))
        limited = self._limited(response, message)
        if limited is not None:
            return limited
        if status == 404 and mount is not None:
            if not self._token:
                return NotFound(gettext("The repository %(repo)s doesn't exist, or it is private: that needs a token that "
                                        "can see it.") % {"repo": mount.name})
            return NotFound(gettext("The repository %(repo)s doesn't exist, or the token can't see it.") % {"repo": mount.name})
        if status == 404:
            return NotFound()
        detail = gettext(" (%(code)s): %(message)s") % {"code": f"HTTP {status}", "message": message} if message else gettext(" (HTTP %(status)s)") % {"status": status}
        return BackendError(gettext("GitHub failed%(detail)s. Please try again later.") % {"detail": detail})

    def _get(self, url: str, mount: _Mount | None, params: Mapping[str, Any] | None = None) -> httpx.Response:
        """A GET of the API that answered 200 (the answer read); DriverError otherwise, and BackendError without asking
        while GitHub's rate limit holds."""
        self._not_held()
        try:
            response = self._http.get(url, params=params, headers=self._headers())
        except httpx.HTTPError as error:
            raise transport_error(error, self.service) from None
        if response.status_code != 200:
            raise self._error(response, mount)
        return response

    def _releases(self, mount: _Mount) -> list[_Release]:
        key = ("releases", mount.name, self._all)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        base = f"{self.api_root}/repos/{mount.owner}/{mount.repo}"
        if not self._all:
            try:
                answers = [self._get(base + "/releases/latest", None).json()]
            except NotFound:  # no release yet (or no such repository: told apart below)
                self._get(base, mount)
                answers = []
        else:
            answers, url, params = [], base + "/releases", {"per_page": 100}
            for _ in range(self.max_pages):
                response = self._get(url, mount, params)
                page = json_or_none(response)
                if not isinstance(page, list):
                    raise BackendError(gettext("The answer from GitHub couldn't be understood. Please try again later."))
                answers += page
                found = _NEXT.search(response.headers.get("link", ""))
                if found is None:
                    break
                url, params = found.group(1), None
        releases = [release for release in map(_release, answers) if release is not None]
        self._cache.put(key, releases)
        return releases

    def _private(self, mount: _Mount) -> bool:
        """Whether GitHub keeps the repository's files from people without access. Without a token only public
        repositories can be seen, so it isn't asked."""
        if not self._token:
            return False
        key = ("private", mount.name)
        cached = self._cache.get(key)
        if cached is None:
            answer = json_or_none(self._get(f"{self.api_root}/repos/{mount.owner}/{mount.repo}", mount))
            cached = not isinstance(answer, dict) or answer.get("private") is not False
            self._cache.put(key, cached)
        return cached

    # -- the tree

    def _made_up(self, path: str) -> list[str]:
        """The names of the folders made up below `path` (repositories' folders, and the folders above them)."""
        depth = len(paths.segments(path))
        return sorted({paths.segments(mount.path)[depth] for mount in self._mounts
                       if mount.path != path and paths.is_inside(mount.path, path)})

    def _is_made_up(self, path: str) -> bool:
        return any(paths.is_inside(mount.path, path) for mount in self._mounts)

    def _mount_of(self, path: str) -> _Mount | None:
        """The repository whose folder `path` is in (the deepest one)."""
        inside = [mount for mount in self._mounts if paths.is_inside(path, mount.path)]
        return max(inside, key=lambda mount: len(mount.path), default=None)

    def _contents(self, mount: _Mount, rest: list[str]) -> tuple[bool, dict[str, Entry | _Asset]]:
        """What is at `rest` (segments below the repository's folder): whether it is a folder, and what is in it
        (names -> Entry of a release folder, or _Asset); for a file, the one _Asset under its name. NotFound."""
        releases = self._releases(mount)
        if not self._all:
            assets = releases[0].assets if releases else {}
            if not rest:
                return True, dict(assets)
            if len(rest) == 1 and rest[0] in assets:
                return False, {rest[0]: assets[rest[0]]}
            raise NotFound()
        by_tag = {release.tag: release for release in releases if usable_name(release.tag)}
        if not rest:
            return True, {tag: Entry(name=tag, path=paths.join(mount.path, tag), is_dir=True, modified=release.published)
                          for tag, release in by_tag.items()}
        release = by_tag.get(rest[0])
        if release is None:
            raise NotFound()
        if len(rest) == 1:
            return True, dict(release.assets)
        if len(rest) == 2 and rest[1] in release.assets:
            return False, {rest[1]: release.assets[rest[1]]}
        raise NotFound()

    def _asset(self, path: str) -> tuple[_Asset, _Mount]:
        """The file at `path`. NotFound, a folder too."""
        mount = self._mount_of(path)
        if mount is None or self._is_made_up(path):
            raise NotFound()
        is_dir, found = self._contents(mount, paths.segments(paths.relative(path, mount.path)))
        if is_dir:
            raise NotFound()
        (asset,) = found.values()
        return asset, mount

    @staticmethod
    def _entry(folder: str, name: str, found: Entry | _Asset) -> Entry:
        if isinstance(found, Entry):
            return found
        return Entry(name=name, path=paths.join(folder, name), is_dir=False, size=found.size, modified=found.modified)

    # -- files

    def check(self) -> None:
        for mount in self._mounts:  # every repository, so that a wrong line is found now
            self._releases(mount)

    def list(self, path: str) -> list[Entry]:
        made_up = self._is_made_up(path)
        entries = {name: Entry(name=name, path=paths.join(path, name), is_dir=True) for name in self._made_up(path)}
        mount = self._mount_of(path)
        found: dict[str, Entry | _Asset] = {}
        if mount is not None:
            try:
                is_dir, found = self._contents(mount, paths.segments(paths.relative(path, mount.path)))
            except NotFound:
                # A repository that isn't there shows as an error, unless its folder holds others; a made-up
                # folder inside a repository's (a release tag it doesn't have) is just a folder.
                if not (entries or (made_up and path != mount.path)):
                    raise
                is_dir, found = True, {}
            if not is_dir:
                if not made_up:
                    raise NotADirectory()
                found = {}  # a file of the repository with the made-up folder's name: the folder wins
        elif not made_up:
            raise NotFound()
        for name, item in found.items():
            entries.setdefault(name, self._entry(path, name, item))
        return list(entries.values())

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT or self._is_made_up(path):
            return Entry(name=paths.name(path) if path != paths.ROOT else "", path=path, is_dir=True)
        mount = self._mount_of(path)
        if mount is None:
            raise NotFound()
        parent, name = paths.split(path)  # the parent is the repository's folder or below it: path isn't made up
        is_dir, found = self._contents(mount, paths.segments(paths.relative(parent, mount.path)))
        if not is_dir or name not in found:
            raise NotFound()
        return self._entry(parent, name, found[name])

    def _download_request(self, mount: _Mount, asset: _Asset) -> tuple[str, dict[str, str] | None]:
        """Where a file's bytes are asked for, and the API's headers for it; None for a link asked without them, whose
        redirects are followed. A public repository's file: its browser_download_url, which isn't the REST API (none
        of its 60 requests an hour), and not through download_mirror, which is for browsers. Else the API's
        /releases/assets/{id}, which gives the bytes or redirects to them."""
        if not self._private(mount) and asset.url.startswith(("https://", "http://")):
            return asset.url, None
        return (f"{self.api_root}/repos/{mount.owner}/{mount.repo}/releases/assets/{asset.id}",
                {**self._headers(), "Accept": "application/octet-stream"})

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        asset, mount = self._asset(path)
        span = range_header(offset, length, asset.size)
        plain = {"Accept-Encoding": "identity", **({"Range": span} if span else {})}
        url, headers = self._download_request(mount, asset)
        api = headers is not None
        if api:
            self._not_held()
        try:
            if api:
                response = self._http.send(self._http.build_request("GET", url, headers={**headers, **plain}), stream=True)
                if response.has_redirect_location:
                    # To the file host: a signed link, asked without the token (it isn't the API).
                    url, api = response.url.join(response.headers["location"]), False
                    http.drain(response)
            if not api:
                response = self._http.send(self._http.build_request("GET", url, headers=plain), stream=True,
                                           follow_redirects=True)
        except httpx.HTTPError as error:
            raise transport_error(error, self.service) from None
        if response.status_code not in (200, 206):
            if not api:  # the file host's, or github.com's: its words aren't wanted
                http.drain(response)
                raise self._error(response, mount, "")
            raise self._error(http.read_error(response), mount)
        return response_stream(response, offset=offset, length=length, total=asset.size, service=self.service,
                               modified=asset.modified)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        asset, mount = self._asset(path)
        if self._private(mount) or not asset.url.startswith(("https://", "http://")):
            return None
        mirror = str(self.config.get("download_mirror") or "").strip().rstrip("/")
        if mirror and asset.url.startswith(self.web_root + "/"):
            return mirror + asset.url[len(self.web_root):]
        return asset.url
