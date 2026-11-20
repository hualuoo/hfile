"""A folder's readme (目录文档) as the browser shows it (inventory 5.3, decisions D22 and D33).

The storage's readme rules (access.readme()), or with 读取文件夹里的 README.md the folder's own
README.md (any case), read through the driver up to the site's text preview size and only when the
visitor may read that file. Both are rendered with apps.core.markdown.render(), the untrusted mode: README
files are anyone's uploads, and readme rules can be typed by staff who aren't superusers.

Relative links and images point into the folder: they become the download URLs of those files (or the
browser's address of a folder for a link ending in "/"), and only for what the visitor may read (the
policy checks each); links that leave the visitor's root, or that he can't open, are dropped. So a
readme never grants more than its reader already has.

Every listing of the folder asks for it: the result is cached for up to a minute per text (a README.md's
path, size and time, or the rule) and visitor state (root, permissions, bypasses, unlocked passwords,
rules, the storage's link options), and at most LINKS_MAX links are rewritten.
"""

from __future__ import annotations

import hashlib
import logging
import urllib.parse

from django.core.cache import cache

from apps.core import markdown
from apps.storages.access import LIST, READ, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError

from . import signing, transfer

logger = logging.getLogger("hfile.files")

README_NAMES = ("readme.md",)
TEXT_MAX = 1024 * 1024  # bytes of a README.md, whatever text_preview_max_kb says
LINKS_MAX = 500  # relative links and images rewritten in one readme
CACHE_SECONDS = 60


def text_hash(text: str) -> str:
    """For 不再显示 of a readme shown in a dialog: a new text shows again."""
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16]


def browse_url(storage, client_path: str) -> str:
    """The browser's address of a folder: /<key>/<path> (/<key>/ for the root)."""
    key = urllib.parse.quote(storage.key, safe="")
    if client_path in ("", paths.ROOT):
        return f"/{key}/"
    return f"/{key}{signing.quote_path(client_path)}"


def _resolved(base: str, link: str) -> str | None:
    """A relative link's client path from the folder `base`; None when it leaves the root."""
    stack = [] if link.startswith("/") else paths.segments(base)
    for segment in link.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if not stack:
                return None
            stack.pop()
            continue
        stack.append(segment)
    return "/" + "/".join(stack)


class Links:
    """apps.core.markdown.render(relative=...) for one folder of one visitor. At most LINKS_MAX links are
    rewritten (each is a check of the policy and a signature); the rest are dropped."""

    def __init__(self, request, access, folder: str):
        self.request = request
        self.access = access
        self.base = access.relative(folder)
        self.urls = transfer.ListingUrls(request, access.storage) if access.may(READ) else None
        self.done = 0

    def __call__(self, url: str) -> str | None:
        if url.startswith("#"):
            return url  # a heading of the readme itself
        self.done += 1
        if self.done > LINKS_MAX:
            return None
        try:
            parts = urllib.parse.urlsplit(url)
            link = urllib.parse.unquote(parts.path, errors="strict")
        except (ValueError, UnicodeDecodeError):
            return None
        if parts.scheme or parts.netloc or not link or "\\" in link or "\x00" in link:
            return None
        target = _resolved(self.base, link)
        if target is None:
            return None
        try:
            storage_path = self.access.path(target)
        except AccessDenied:
            return None
        fragment = f"#{parts.fragment}" if parts.fragment else ""
        if link.endswith("/") or storage_path == self.access.root:
            try:
                self.access.check(LIST, storage_path)
            except AccessDenied:
                return None
            return browse_url(self.access.storage, target) + fragment
        if self.urls is None:
            return None
        try:
            self.access.check(READ, storage_path)
        except AccessDenied:
            return None
        from .service import needs_bypass

        try:
            return self.urls.url(storage_path, ignore_hidden=needs_bypass(self.access, storage_path))
        except (DriverError, ValueError):
            return None


def _read(access, entry, limit: int) -> str | None:
    """The README's text, or None when it is larger than `limit` or can't be read."""
    if entry.size is not None and entry.size > limit:
        return None
    driver = access.driver
    try:
        if entry.size == 0:
            return ""
        stream = driver.open(entry.path)
    except DriverError as error:
        logger.info("Storage %s: README %s not read: %s", access.storage.key, entry.path, error.message)
        return None
    chunks, total = [], 0
    try:
        for chunk in stream:
            total += len(chunk)
            if total > limit:
                return None
            chunks.append(chunk)
    except DriverError as error:
        logger.info("Storage %s: README %s not read: %s", access.storage.key, entry.path, error.message)
        return None
    finally:
        stream.close()
    # Decoded as the Markdown preview decodes it, UTF-16 too (FILE-06). Imported here: apps.preview.content
    # imports apps.files.service, which imports this module.
    from apps.preview.content import decode

    return decode(b"".join(chunks))


def _readme_file(entries):
    """The folder's README.md (whatever its case; README.md itself first)."""
    found = [entry for entry in entries if not entry.is_dir and entry.name.lower() in README_NAMES]
    found.sort(key=lambda entry: (entry.name != "README.md", entry.name))
    return found[0] if found else None


def _visitor_state(access) -> tuple:
    """What a readme's links depend on besides its text: the visitor's root, what he may read, his
    bypasses, the folder passwords he has unlocked, the rules and the storage's link options."""
    unlocked = ()
    if not access.bypass_passwords and access.rules.passwords:
        unlocked = tuple(
            (rule.pk, rule.revision) for rule, _glob in access.rules.passwords
            if (rule.pk, rule.revision) in access.opened
            or (access.keys is not None and access.keys.has(access.storage.pk, rule.pk, rule.revision)))
    storage = access.storage
    return (
        access.root, access.may(READ), access.may(LIST), access.bypass_filters, access.bypass_passwords, unlocked,
        tuple((rule.pk, rule.expression, rule.mode) for rule, _glob in access.rules.filters),
        tuple((rule.pk, rule.revision) for rule, _glob in access.rules.passwords),
        storage.config_revision, storage.download_domain, storage.proxy_download, storage.permanent_links,
        storage.link_lifetime, storage.link_secret,
    )


def _cache_key(access, folder: str, source: tuple) -> str:
    state = repr((access.storage.pk, folder, source, _visitor_state(access)))
    return "hfile:files:readme:" + hashlib.sha256(state.encode("utf-8", "surrogatepass")).hexdigest()


def _cache_seconds(storage) -> int:
    """Short enough that the links in it (signed, with an expiry) stay valid well after it is served."""
    return max(5, min(CACHE_SECONDS, storage.link_lifetime // 4))


def folder_readme(request, access, folder: str, entries, *, site) -> dict | None:
    """{"html", "display" (top, bottom, dialog), "hash"} for the folder (a storage path whose listing
    the visitor got, `entries` its visible entries), or None. Rendered at most once a minute per text
    and visitor state (every listing of the folder asks for it)."""
    if access.storage.readme_from_file:
        entry = _readme_file(entries)
        if entry is None:
            return None
        try:
            access.check(READ, entry.path)
        except AccessDenied:
            return None
        modified = entry.modified.timestamp() if entry.modified is not None else None
        source, what = ("file", entry.path, entry.size, modified), entry
    else:
        rule = access.readme(folder)
        if rule is None or not rule.text.strip():
            return None
        source, what = ("rule", rule.pk, text_hash(rule.text), rule.display), rule
    key = _cache_key(access, folder, source)
    found = cache.get(key)
    if found is not None:
        return found or None
    readme = _render(request, access, folder, what, site)
    cache.set(key, readme or {}, _cache_seconds(access.storage))
    return readme


def _render(request, access, folder: str, what, site) -> dict | None:
    if access.storage.readme_from_file:
        limit = min(TEXT_MAX, max(1, site.text_preview_max_kb) * 1024)
        text = _read(access, what, limit)
        if text is None:
            return None
        display = "bottom"
    else:
        text, display = what.text, what.display
    if not text.strip():
        return None
    html = markdown.render(text, relative=Links(request, access, folder))
    return {"html": html, "display": display, "hash": text_hash(text)}
