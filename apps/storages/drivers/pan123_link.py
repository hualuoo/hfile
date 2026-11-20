"""123 Cloud Drive direct links (123云盘直链): files the admin made direct links of in their 123 Cloud Drive account,
arranged in folders, as a read-only storage.

Documentation: 123's help pages on direct links, https://www.123pan.com/faq/?problem=direct_link, and on their URL
authentication (直链鉴权开发指引), https://www.123pan.com/faq/?problem=dev_guide; 123's signing examples,
https://gitee.com/pan123-git/123pan-link (example/sign.go, sign.java, sign.js, sign.php); the open platform's page
on direct links, https://123yunpan.yuque.com/org-wiki-123yunpan-muaork/cr6ced/tdxfsmtemp4gu4o2. Not tried against
the real service: the tests use a fake built from these.

Paths
    As the URL tree's (url_tree.py, whose driver this one extends): one line per file or folder, two spaces of
    indent a folder deeper; a folder is "name:", a file [name:][size:]link. The links are the ones 123 gives for
    the files of a folder made a direct-link space (启用直链空间, then 获取直链地址 on a file), like
    https://vip.123pan.com/<uid>/<path> in 123's examples, or on the admin's own domain (直链自定义域名). Sizes
    not given are asked for with HEAD, as the URL tree does.

URL authentication (直链鉴权)
    Turned on in 123's direct link management (鉴权管理, with a key the admin sets there), a link needs
    auth_key=<timestamp>-<rand>-<uid>-<md5>: the timestamp is when it expires (Unix seconds), rand a random value
    without "-" (a UUID's hex, as 123 suggests), uid the account ID (UID, on 123's 个人中心 page), and md5 the
    lowercase hex MD5 of "<path>-<timestamp>-<rand>-<uid>-<key>", the path being the link's, percent-decoded (as
    123's examples do), without the query. With `private_key` and `uid` set, every link is signed here; the key
    itself is never sent anywhere. An auth_key a link of the tree has already is replaced, the rest of its query
    kept.

Downloads
    download_url(): the link, signed to last `expires` seconds (the storage's download link lifetime, which the
    site shortens for shares and links). open(), and the HEAD for a size: the link signed for `fetch_lifetime`
    seconds, with the Range. 123 doesn't document how its links answer: redirects are followed, and a refusal
    (401, 403) is an AuthError naming what to check: the key and UID, the server's clock, the account's direct
    link traffic (without it, 123's help says, links stop working).
"""

from __future__ import annotations

import hashlib
import time
import urllib.parse
import uuid
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from .base import AuthError, BackendError, DriverError, NotFound, Param
from .url_tree import URLTreeDriver, _File

SERVICE = gettext_lazy("123 Cloud Drive")


def sign(url: str, key: str, uid: int, expires_at: int, rand: str) -> str:
    """`url` with the auth_key of 123's URL authentication, valid until `expires_at` (Unix seconds)."""
    parts = urllib.parse.urlsplit(url)
    digest = hashlib.md5(f"{urllib.parse.unquote(parts.path)}-{expires_at}-{rand}-{uid}-{key}".encode()).hexdigest()
    query = [item for item in parts.query.split("&") if item and item.partition("=")[0] != "auth_key"]
    query.append(f"auth_key={expires_at}-{rand}-{uid}-{digest}")
    return urllib.parse.urlunsplit(parts._replace(query="&".join(query)))


class Pan123LinkDriver(URLTreeDriver):
    type = "123-link"
    label = gettext_lazy("123 Cloud Drive direct links")
    service = SERVICE
    params = (
        Param("tree", gettext_lazy("Files and folders"), kind="textarea",
              placeholder="Music:\n  https://vip.123pan.com/1800000000/Music/a.mp3\n"
                          "notes.txt:1024:https://vip.123pan.com/1800000000/notes.txt",
              help=gettext_lazy('Direct links copied in 123 Cloud Drive ("Get direct link" on a file in a direct-link '
                                "space), one per line, two spaces of indent a folder deeper. A folder: name: (with the "
                                '":"). A file: its link, or name:link, or name:size in bytes:link.')),
        Param("uid", gettext_lazy("Account ID (UID)"), kind="number", required=False, minimum=1,
              help=gettext_lazy("The account ID on 123 Cloud Drive's personal center page; needed with the URL "
                                "authentication key.")),
        Param("private_key", gettext_lazy("URL authentication key"), kind="password", secret=True, required=False,
              default="",
              help=gettext_lazy("When URL authentication is on in 123 Cloud Drive's direct link management "
                                '("Authentication management", bottom right), enter its key here: links then get a '
                                "time-limited auth_key signature, lasting as long as this storage's download links. "
                                "Leave empty when it is off.")),
        *(param for param in URLTreeDriver.params if param.key == "head_sizes"),
    )
    setup_params = ("tree", "uid", "private_key")

    fetch_lifetime: ClassVar[int] = 600  # seconds the links this site fetches itself are signed for

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._key = str(self.config.get("private_key") or "")
        self._uid = self.config.get("uid")

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = super().validate_config(config)
        if config.get("private_key") and not config.get("uid"):
            errors["uid"] = gettext("Give the account ID too: links are signed with it.")
        return errors

    # -- requests

    def _address(self, file: _File, expires: int | None = None) -> str:
        """The tree's link, signed to last `expires` seconds (`fetch_lifetime` for this site's own requests) when
        there is a key."""
        if not self._key:
            return file.url
        lifetime = self.fetch_lifetime if expires is None else expires
        return sign(file.url, self._key, self._uid, int(time.time()) + max(1, int(lifetime)), uuid.uuid4().hex)

    def _failure(self, response: httpx.Response) -> DriverError:
        if response.status_code in (404, 410):
            return NotFound()
        if response.status_code in (401, 403):
            return AuthError(gettext("%(service)s refused the direct link (HTTP %(status_code)s). Please check the account "
                                     "ID and the URL authentication key, the server's time, and that the account has "
                                     "direct link traffic left.") % {"service": SERVICE, "status_code": response.status_code})
        return BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                            % {"service": SERVICE, "status_code": response.status_code})
