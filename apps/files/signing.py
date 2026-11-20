"""Signed download links: /pd/<storage key>/<path>?... (contract W2, decision D15).

The only place that knows the signature's format. A link is an HMAC (django.core.signing, keyed by
[site] secret_key and SECRET_KEY_FALLBACKS) over what it grants:

    [storage id, storage path, expiry, disposition, redirect, ignore_hidden, filename]

with the storage's own salt (StorageSource.link_salt(), which holds its rotatable link_secret): 换一个
链接密钥 in the admin ends every link of that storage, the permanent ones included. The expiry is a
Unix time, or null for a permanent link (the storage's 永久下载链接 switch). The query:

    e      the expiry (absent: permanent)
    d      i (inline) or a (attachment); absent: the storage's 下载链接强制下载 decides when serving
    r      1: answer with a redirect to the backend's own URL (OneDrive, 115: links that cost a request
           to the backend each, made when the link is opened rather than for every file of a listing)
    h      1: the signer could see past the filter rules (忽略隐藏), so serving doesn't check them
    n      the file name for Content-Disposition, when it isn't the path's last segment
    sign   the signature (apps.core.masking keeps it out of the logs)

Every value is in the signature: dropping or changing one makes the link invalid. Other parameters
(a ?v= a client adds) are ignored.

    url = sign(storage, "/docs/a.pdf", lifetime=1800, inline=True)
    link = verify(storage, "/docs/a.pdf", request.GET)        # or verify_url(url)
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass

from django.core import signing as django_signing
from django.utils.translation import gettext as _

from apps.storages.drivers import paths
from apps.storages.models import StorageSource

PREFIX = "/pd/"
PARAMS = ("e", "d", "r", "h", "n", "sign")
NAME_MAX = 255 * 4  # a file name for Content-Disposition, in characters


class BadLink(Exception):
    """A link that doesn't hold: code "link_invalid" (forged, changed, another file's) or
    "link_expired"."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


INVALID = "link_invalid"
EXPIRED = "link_expired"


@dataclass(frozen=True)
class Link:
    """What a valid link grants."""

    storage: StorageSource
    path: str  # the storage path
    expires: int | None  # Unix time; None: permanent
    inline: bool | None  # None: the storage's force_download decides
    redirect: bool
    ignore_hidden: bool
    filename: str | None  # None: the path's last segment

    @property
    def name(self) -> str:
        return self.filename or paths.name(self.path)


def quote_path(path: str) -> str:
    """A storage path for a URL: each segment percent-encoded, "/" between them."""
    return "/".join(urllib.parse.quote(segment, safe="") for segment in path.split("/"))


def pd_path(storage: StorageSource, path: str) -> str:
    """/pd/<key>/<path> without the query."""
    return f"{PREFIX}{urllib.parse.quote(storage.key, safe='')}{quote_path(path)}"


def _expiry(lifetime: int, now: float | None = None) -> int:
    """now + lifetime, rounded up to a step (a sixth of the lifetime, at most 5 minutes): the same file
    gets the same link for a while, so browsers can keep what they fetched (a gallery's pictures). A
    link thus lasts at least `lifetime` seconds."""
    now = time.time() if now is None else now
    step = max(1, min(300, lifetime // 6))
    return int(math.ceil((now + lifetime) / step) * step)


def _payload(storage_id: int, path: str, expires: int | None, disposition: str, redirect: bool,
             ignore_hidden: bool, filename: str) -> str:
    return json.dumps([storage_id, path, expires, disposition, int(redirect), int(ignore_hidden), filename],
                      ensure_ascii=False, separators=(",", ":"))


def _signer(storage: StorageSource) -> django_signing.Signer:
    return django_signing.Signer(salt=storage.link_salt(), algorithm="sha256")


def sign(storage: StorageSource, path: str, *, lifetime: int | None, inline: bool | None = None,
         redirect: bool = False, ignore_hidden: bool = False, filename: str | None = None,
         not_after: int | None = None) -> str:
    """The signed /pd/ path (with its query) of a file: `lifetime` seconds (None: permanent).

    inline: True for inline, False for attachment, None for the storage's option at serving time.
    not_after: a Unix time the link must not outlive (a share's end): the expiry, rounded up to its
    step as usual, is capped at it, which rounds it down. None (the default) leaves the expiry alone.
    The caller has decided that the file may be handed out (apps.storages.access)."""
    if not paths.is_clean(path) or path == paths.ROOT:
        raise ValueError("a clean storage path of a file")
    if lifetime is not None and lifetime <= 0:
        raise ValueError("lifetime must be positive, or None for a permanent link")
    if filename is not None and (filename == paths.name(path) or not filename):
        filename = None
    if filename is not None and ("/" in filename or len(filename) > NAME_MAX):
        raise ValueError("a file name without /")
    expires = None if lifetime is None else _expiry(lifetime)
    if not_after is not None:
        expires = int(not_after) if expires is None else min(expires, int(not_after))
    disposition = "" if inline is None else ("i" if inline else "a")
    payload = _payload(storage.pk, path, expires, disposition, redirect, ignore_hidden, filename or "")
    signature = _signer(storage).signature(payload)
    query = []
    if expires is not None:
        query.append(("e", str(expires)))
    if disposition:
        query.append(("d", disposition))
    if redirect:
        query.append(("r", "1"))
    if ignore_hidden:
        query.append(("h", "1"))
    if filename:
        query.append(("n", filename))
    query.append(("sign", signature))
    return pd_path(storage, path) + "?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote)


def _one(query: Mapping, name: str) -> str | None:
    """A parameter sent once (a QueryDict, or a plain dict); BadLink when it is there twice."""
    getlist = getattr(query, "getlist", None)
    if getlist is not None:
        values = getlist(name)
        if len(values) > 1:
            raise BadLink(INVALID, _("Invalid download link."))
        return values[0] if values else None
    value = query.get(name)
    if isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise BadLink(INVALID, _("Invalid download link."))
        value = value[0]
    return value


def verify(storage: StorageSource, path: str, query: Mapping, *, now: float | None = None) -> Link:
    """The link of `storage` and the storage path `path` (as the URL names them) with these query
    parameters: a Link, or BadLink. It says nothing of whether the file may still be served: the
    caller asks apps.storages.access.check_served() for that."""
    signature = _one(query, "sign")
    if not signature or len(signature) > 200 or not paths.is_clean(path) or path == paths.ROOT:
        raise BadLink(INVALID, _("Invalid download link."))
    raw_expires = _one(query, "e")
    expires = None
    if raw_expires is not None:
        if not raw_expires.isascii() or not raw_expires.isdigit() or len(raw_expires) > 12:
            raise BadLink(INVALID, _("Invalid download link."))
        expires = int(raw_expires)
    disposition = _one(query, "d") or ""
    redirect = _one(query, "r") or ""
    hidden = _one(query, "h") or ""
    filename = _one(query, "n") or ""
    if disposition not in ("", "i", "a") or redirect not in ("", "1") or hidden not in ("", "1"):
        raise BadLink(INVALID, _("Invalid download link."))
    if len(filename) > NAME_MAX:
        raise BadLink(INVALID, _("Invalid download link."))
    payload = _payload(storage.pk, path, expires, disposition, redirect == "1", hidden == "1", filename)
    signer = _signer(storage)
    try:
        signer.unsign(f"{payload}{signer.sep}{signature}")  # constant time, and the fallback keys
    except django_signing.BadSignature:
        raise BadLink(INVALID, _("The download link is invalid or no longer works.")) from None
    if expires is not None and expires < (time.time() if now is None else now):
        raise BadLink(EXPIRED, _("The download link has expired. Go back to the file list and open it again."))
    return Link(storage=storage, path=path, expires=expires,
                inline=None if not disposition else disposition == "i",
                redirect=redirect == "1", ignore_hidden=hidden == "1", filename=filename or None)


def parse_url(url: str) -> tuple[str, str, dict[str, list[str]]]:
    """(storage key, storage path, query) of a /pd/ path, or of an absolute URL whose path is one (the
    caller checks that the URL is this site's). BadLink when it isn't a /pd/ link."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise BadLink(INVALID, _("Invalid download link.")) from None
    if not parts.path.startswith(PREFIX):
        raise BadLink(INVALID, _("Invalid download link."))
    rest = parts.path[len(PREFIX):]
    raw_key, slash, raw_path = rest.partition("/")
    if not slash or not raw_key:
        raise BadLink(INVALID, _("Invalid download link."))
    try:
        key = urllib.parse.unquote(raw_key, errors="strict")
        path = "/" + urllib.parse.unquote(raw_path, errors="strict")
    except UnicodeDecodeError:
        raise BadLink(INVALID, _("Invalid download link.")) from None
    query = urllib.parse.parse_qs(parts.query, keep_blank_values=True)
    return key, path, query


def verify_url(url: str, *, now: float | None = None) -> Link:
    """A whole /pd/ link (a path with its query, or an absolute URL of this site), verified: the Link,
    or BadLink. For the endpoints that are handed a link the browser got (the Markdown preview)."""
    key, path, query = parse_url(url)
    storage = StorageSource.by_key(key)
    if storage is None:
        raise BadLink(INVALID, _("Invalid download link."))
    return verify(storage, path, query, now=now)
