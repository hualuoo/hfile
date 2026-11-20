"""CNB Releases (https://cnb.cool): the releases of CNB repositories, as a read-only storage, through CNB's OpenAPI
(https://api.cnb.cool, described by https://api.cnb.cool/swagger.json).

Paths
    As GitHub Releases' (github_releases.py, which this extends): `repositories` names them, one per line,
    "org/repo" at the root or "folder/path:org/repo" in that folder; CNB's organizations have groups, so
    "org/group/repo" too. A repository's folder holds the files of its latest release; with `all_releases`, a
    folder per release, named after its tag.

Login
    CNB's API answers nobody without a token (401, errcode 16): `token` is an access token with the
    repo-release:r permission, sent as "Authorization: Bearer". That is enough. A token reads a public repository
    whatever it was granted, a private one only with what it was granted
    (https://docs.cnb.cool/zh/guide/access-token.html); so on a private one /{repo}, which needs
    repo-basic-info:r (the OpenAPI description), may be refused (AuthError, or NotFound though its releases
    were read). That is taken as a repository that isn't public (its files go through the site) and, when it
    has no latest release, as one without releases.

Requests
    Releases come from /{repo}/-/releases/latest (404 when there is none), or with all_releases from
    /{repo}/-/releases, `page_size` a page, newest first, at most `max_pages` pages. /{repo} gives the
    repository's visibility_level (Public, Private or Secret). Errors are {"errcode", "errmsg"}. Answers are
    cached for `cache_ttl` seconds.

Downloads
    download_url(): a public repository's file's browser_download_url
    (https://cnb.cool/{repo}/-/releases/download/{tag}/{name}), which redirects the browser to a link that
    expires; a private or secret repository's files need the token, so the site sends them. open():
    /{repo}/-/releases/download/{tag}/{name} with the token, whose redirect to a link that expires is
    followed without it, with the Range. A tag with "/" (shown without all_releases) goes in that path as %2F,
    as GitHub writes it: not tried on CNB.

Limits
    Not tried against CNB with a token: the tests use a fake built from the OpenAPI description.
"""

from __future__ import annotations

import urllib.parse
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from ._cloud import json_or_none, short
from .base import AuthError, BackendError, DriverError, NotFound, Param
from .github_releases import GitHubReleasesDriver, _Asset, _Mount, _release

SERVICE = gettext_lazy("CNB")


class CNBReleasesDriver(GitHubReleasesDriver):
    type = "cnb-releases"
    label = gettext_lazy("CNB Releases")
    service = SERVICE
    params = (
        Param("repositories", gettext_lazy("Repositories"), kind="textarea",
              placeholder="cnb/feedback\ntools:org/group/repo",
              help=gettext_lazy("One per line: org/repo (or org/group/repo) shows the repository at the root, "
                                "folder/path:org/repo in that folder.")),
        Param("token", gettext_lazy("Token"), kind="password", secret=True,
              help=gettext_lazy("A CNB access token (https://cnb.cool/profile/token) with the repo-release:r "
                                "permission: CNB's API needs one, for public repositories too.")),
        Param("all_releases", gettext_lazy("All releases"), kind="bool", required=False, default=False,
              help=gettext_lazy("Show every release as a folder named after its tag. Off: only the files of each repository's latest release.")),
    )

    nested_owners = True
    api_root = "https://api.cnb.cool"
    web_root = "https://cnb.cool"
    page_size: ClassVar[int] = 100

    # -- requests

    @staticmethod
    def _repo_path(mount: _Mount) -> str:
        return "/".join(urllib.parse.quote(part, safe="") for part in mount.name.split("/"))

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", **({"Authorization": f"Bearer {self._token}"} if self._token else {})}

    def _error(self, response: httpx.Response, mount: _Mount | None, message: str | None = None) -> DriverError:
        status = response.status_code
        if message is None:
            answer = json_or_none(response)
            message = short(answer.get("errmsg")) if isinstance(answer, dict) else ""
        if status in (401, 403):
            return AuthError(gettext("CNB refused the token (%(message)s). It needs the repo-release:r permission: "
                                     "please check it in the storage source's settings.") % {"message": message or f"HTTP {status}"})
        if status == 404 and mount is not None:
            return NotFound(gettext("The repository %(repo)s doesn't exist, or the token can't see it.") % {"repo": mount.name})
        if status == 404:
            return NotFound()
        detail = gettext(" (%(code)s): %(message)s") % {"code": f"HTTP {status}", "message": message} if message else gettext(" (HTTP %(status)s)") % {"status": status}
        return BackendError(gettext("CNB failed%(detail)s. Please try again later.") % {"detail": detail})

    def _releases(self, mount: _Mount) -> list[Any]:
        key = ("releases", mount.name, self._all)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        base = f"{self.api_root}/{self._repo_path(mount)}"
        if not self._all:
            try:
                answers = [json_or_none(self._get(base + "/-/releases/latest", None))]
            except NotFound:  # no release yet (or no such repository: told apart here)
                try:
                    self._get(base, mount)
                except AuthError:  # /{repo} needs repo-basic-info:r; its releases answered: there is none yet
                    pass
                answers = []
        else:
            answers = []
            for page in range(1, self.max_pages + 1):
                items = json_or_none(self._get(base + "/-/releases", mount, {"page": page, "page_size": self.page_size}))
                if not isinstance(items, list):
                    raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
                answers += items
                if len(items) < self.page_size:
                    break
        releases = [release for release in map(_release, answers) if release is not None]
        self._cache.put(key, releases)
        return releases

    def _private(self, mount: _Mount) -> bool:
        key = ("private", mount.name)
        cached = self._cache.get(key)
        if cached is None:
            try:
                answer = json_or_none(self._get(f"{self.api_root}/{self._repo_path(mount)}", mount))
            except (AuthError, NotFound):  # the token may read its releases, not it (repo-basic-info:r): not public
                answer = None
            cached = not isinstance(answer, dict) or answer.get("visibility_level") != "Public"
            self._cache.put(key, cached)
        return cached

    # -- files

    def _download_request(self, mount: _Mount, asset: _Asset) -> tuple[str, dict[str, str]]:
        names = "/".join(urllib.parse.quote(part, safe="") for part in (asset.tag, asset.name))
        return f"{self.api_root}/{self._repo_path(mount)}/-/releases/download/{names}", self._headers()
