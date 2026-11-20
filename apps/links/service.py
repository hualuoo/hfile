"""Path links and short links: making them, deciding who may open them, cleaning up (inventory section 9;
decisions D13, D14, D29, D31, D40, D40a, D44). Every decision is apps.storages.access's:

- making a path link: LINK for each path (获取直链 and 下载, the filter rules, folder passwords this
  browser unlocked or 忽略密码), as opening it will check the visitor, so no link is handed out that
  can't work. Nothing is stored and the file's existence isn't checked.
- opening a path link, /<prefix>/<storage key>/<storage path> (the path includes the maker's root):
  - 未登录的人也能用直链下载 off (the default): the visitor (the logged-in user, else the guest) through
    OPEN_LINK: 下载, the path inside his own root, the filter rules, and a folder password only when this
    browser has unlocked it (a link carries none);
  - on: anybody, as far as check_served() lets the file be served (storage enabled, filter rules).
- making a short link: SHORT_LINK for each path (生成短链 and 下载, rules, passwords), all before any is
  stored; the lifetime is one of the site's 短链有效期; a permanent one reuses the maker's own permanent
  link of the same file; at most SHORT_LINKS_PER_MINUTE a minute per maker. The folder-password rules he
  had unlocked are kept with the link.
- opening a short link: its creator is the actor again (D31): active, still with both permissions, the
  file inside his current root, the current filter rules, and the folder passwords through the keys
  kept (keys=), not through anybody's browser.

Links never skip the filter rules (apps.storages.access: 忽略隐藏 doesn't apply to them), and the download they
lead to is a plain apps.files.transfer.download_url() (never ignore_hidden).
"""

from __future__ import annotations

import base64
import datetime
import io
import logging
import secrets
import urllib.parse

import segno
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext, ngettext
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail

from apps.core import origin
from apps.core.times import iso
from apps.files import signing
from apps.siteconf.models import SiteSettings
from apps.siteconf.serializers import FOREVER_SECONDS
from apps.storages import access as policy
from apps.storages.access import AccessDenied
from apps.storages.drivers import paths
from apps.storages.models import StorageSource

from .models import KEY_ALPHABET, KEY_LENGTH, KEY_MAX, DownloadLog, ShortLink

logger = logging.getLogger("hfile.links")

BATCH_MAX = 1000  # files in one request for links
KEY_TRIES = 5
# What a link redirects to (a signed /pd/ link, a bucket's presigned URL) stays valid this long at most: the
# link's own checks (expiry, the switch, the creator's permissions) end any access a few minutes later.
TARGET_LIFETIME = 600
TARGET_LIFETIME_MIN = 60
# Short links one user may make in a minute (the guest's count for all visitors): a script can't fill the table.
SHORT_LINKS_PER_MINUTE = 1000

PATH_LINKS_OFF = "path_links_off"
SHORT_LINKS_OFF = "short_links_off"
INVALID_EXPIRY = "invalid_expiry"
NOT_A_FILE = "not_a_file"
TOO_MANY = "too_many_links"


class LinkError(APIException):
    """A refusal of the links API: {"detail", "code"} with a stable code (D38)."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message, code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = {"detail": ErrorDetail(message, code), "code": code}


# --- addresses ------------------------------------------------------------------------------------

def path_link_path(storage: StorageSource, storage_path: str, prefix: str | None = None) -> str:
    """/<prefix>/<storage key>/<storage path>, each segment percent-encoded."""
    prefix = prefix if prefix is not None else SiteSettings.load().direct_link_prefix
    return f"/{urllib.parse.quote(prefix, safe='')}/{urllib.parse.quote(storage.key, safe='')}{signing.quote_path(storage_path)}"


def short_link_path(key: str) -> str:
    return f"/s/{key}"


def target_lifetime(expires_at: datetime.datetime | None = None, now: datetime.datetime | None = None) -> int:
    """Seconds the URL a link redirects to may stay valid: TARGET_LIFETIME, or what is left of a short link's
    own lifetime (at least TARGET_LIFETIME_MIN, for a download to begin)."""
    if expires_at is None:
        return TARGET_LIFETIME
    left = int((expires_at - (now or timezone.now())).total_seconds())
    return max(TARGET_LIFETIME_MIN, min(TARGET_LIFETIME, left))


def new_key() -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def is_key(text: str) -> bool:
    return 0 < len(text) <= KEY_MAX and all(char in KEY_ALPHABET for char in text)


def qr_code(url: str) -> dict | None:
    """The QR code of a link as data: URLs, {"svg", "png"}; None when the link is too long for one."""
    try:
        code = segno.make_qr(url, error="m")
    except segno.DataOverflowError:
        return None
    return {
        "svg": code.svg_data_uri(scale=8, border=4, dark="#000", light="#fff"),
        "png": "data:image/png;base64," + base64.b64encode(_png(code)).decode("ascii"),
    }


def _png(code) -> bytes:
    buffer = io.BytesIO()
    code.save(buffer, kind="png", scale=8, border=4, dark="#000", light="#fff")
    return buffer.getvalue()


# --- making links ---------------------------------------------------------------------------------

def _file_paths(access, client_paths) -> list[str]:
    """The storage paths of the files the client named (relative to his root)."""
    found = []
    for client_path in client_paths:
        storage_path = access.path(client_path)
        if storage_path in (paths.ROOT, access.root):
            raise LinkError(NOT_A_FILE, gettext("Links can only be made for files, not for the root folder."))
        found.append(storage_path)
    return found


def require(site, kind: str) -> None:
    """The site's switch for making links of this kind ("path" or "short"): LinkError (403) when it is off."""
    if kind == "path" and not site.show_path_link:
        raise LinkError(PATH_LINKS_OFF, gettext("Direct links aren't allowed on this site."), status_code=403)
    if kind == "short" and not site.show_short_link:
        raise LinkError(SHORT_LINKS_OFF, gettext("Short links aren't allowed on this site."), status_code=403)


def make_path_links(request, site, access, client_paths, *, password: str | None = None) -> list[dict]:
    """[{"path", "name", "url"}] for the files (client paths), every one checked first (LINK)."""
    require(site, "path")
    storage_paths = _file_paths(access, client_paths)
    for storage_path in storage_paths:
        access.check(policy.LINK, storage_path, password=password)
    return [{
        "path": access.relative(storage_path),
        "name": paths.name(storage_path),
        "url": origin.absolute(path_link_path(access.storage, storage_path, site.direct_link_prefix), request),
    } for storage_path in storage_paths]


def lifetimes(site) -> dict[int, dict]:
    """The site's 短链有效期 by seconds (-1 is forever)."""
    found = {}
    for item in site.short_link_expiries or ():
        if isinstance(item, dict) and isinstance(item.get("seconds"), int):
            found.setdefault(item["seconds"], item)
    return found


def make_short_links(request, site, access, client_paths, seconds, *, password: str | None = None) -> list[dict]:
    """[{"path", "name", "key", "url", "expires_at"}] for the files: all of them or none."""
    require(site, "short")
    if seconds not in lifetimes(site):
        raise LinkError(INVALID_EXPIRY, gettext("Invalid expiry: please choose one of the short link lifetimes."))
    storage_paths = _file_paths(access, client_paths)
    recent = ShortLink.objects.filter(created_by=access.actor.user,
                                      created_at__gte=timezone.now() - datetime.timedelta(minutes=1)).count()
    if recent + len(storage_paths) > SHORT_LINKS_PER_MINUTE:
        raise LinkError(TOO_MANY, ngettext("Too many short links: at most %(limit)s a minute. Please try again later.", "Too many short links: at most %(limit)s a minute. Please try again later.", SHORT_LINKS_PER_MINUTE) % {"limit": SHORT_LINKS_PER_MINUTE},
                        status_code=429)
    unlocked = []
    for storage_path in storage_paths:
        access.check(policy.SHORT_LINK, storage_path, password=password)
        unlocked.append([list(pair) for pair in access.unlocked_keys(storage_path)])
    now = timezone.now()
    expires_at = None if seconds == FOREVER_SECONDS else now + datetime.timedelta(seconds=seconds)
    with transaction.atomic():
        links = _store(access.storage, access.actor.user, list(zip(storage_paths, unlocked)), now, expires_at)
    return [{
        "path": access.relative(link.path),
        "name": paths.name(link.path),
        "key": link.key,
        "url": origin.absolute(short_link_path(link.key), request),
        "expires_at": iso(link.expires_at),
    } for link in links]


def _store(storage, user, items, now, expires_at) -> list[ShortLink]:
    """The links for [(storage path, keys)], in that order. A permanent one reuses the user's own permanent
    link of the same file (the path compared exactly: MySQL's collation would take /a.md for /A.md), with
    the keys brought up to date; the others are new rows with new keys."""
    reusable: dict[str, ShortLink] = {}
    if expires_at is None:
        wanted = {path for path, _keys in items}
        candidates = (ShortLink.objects.select_for_update()
                      .filter(storage=storage, created_by=user, expires_at__isnull=True, path__in=wanted)
                      .order_by("id"))
        for link in candidates:
            if link.path in wanted:
                reusable.setdefault(link.path, link)
    links: list[ShortLink | None] = []
    new: list[ShortLink] = []
    for path, keys in items:
        link = reusable.get(path)
        if link is not None:
            if link.keys != keys:
                link.keys = keys
                link.save(update_fields=["keys"])
            links.append(link)
            continue
        link = ShortLink(storage=storage, path=path, created_by=user, keys=keys, created_at=now, expires_at=expires_at)
        new.append(link)
        links.append(link)
        if expires_at is None:
            reusable[path] = link  # the same file twice in one request: one link
    if new:
        _insert(new)
    return links


def _insert(links: list[ShortLink]) -> None:
    """Gives the links unique keys and stores them; a key taken meanwhile (another process) is drawn again."""
    for attempt in range(KEY_TRIES):
        keys: set[str] = set()
        while len(keys) < len(links):
            keys.add(new_key())
        taken = set(ShortLink.objects.filter(key__in=keys).values_list("key", flat=True))
        fresh = [key for key in keys if key not in taken]
        while len(fresh) < len(links):
            key = new_key()
            if key not in keys and not ShortLink.objects.filter(key=key).exists():
                keys.add(key)
                fresh.append(key)
        for link, key in zip(links, fresh):
            link.key = key
        try:
            with transaction.atomic():
                ShortLink.objects.bulk_create(links)
            return
        except IntegrityError:
            if attempt == KEY_TRIES - 1:
                raise
            logger.info("A new short link's key was taken meanwhile; drawing again")


# --- opening links --------------------------------------------------------------------------------

def open_path_link(request, site, storage_key: str, path: str) -> tuple[StorageSource, str]:
    """The storage and the storage path of /<prefix>/<storage key>/<path>, if this request may download
    it. Raises AccessDenied."""
    storage_path = policy.clean_path(path)
    if storage_path == paths.ROOT:
        raise AccessDenied(NOT_A_FILE, gettext("Wrong direct link address: the file path is missing."), status=404)
    if site.path_link_anonymous:
        storage = StorageSource.by_key(storage_key)
        if storage is None:
            raise AccessDenied(policy.STORAGE_NOT_FOUND, gettext("The storage source doesn't exist."), status=404)
        policy.check_served(storage, storage_path, operation=policy.OPEN_LINK)
        return storage, storage_path
    access = policy.open_storage(request, storage_key)
    access.check(policy.OPEN_LINK, storage_path)
    return access.storage, storage_path


def check_creator(link: ShortLink) -> None:
    """D31: may the short link's creator still make it? Raises AccessDenied. Nothing of the visitor's (his
    session, his unlocked folders) counts: the request is left out."""
    access = policy.open_storage(None, link.storage, actor=policy.Actor(link.created_by))
    access.check(policy.SHORT_LINK, link.path, keys=link.keys)


# --- cleaning up ----------------------------------------------------------------------------------

def delete_expired(now: datetime.datetime) -> tuple[int, int]:
    """Deletes the short links expired at `now` and the download logs of exactly those keys, in one
    transaction (删除过期短链, and `hfile cleanup`, D29): (links, log entries) deleted."""
    with transaction.atomic():
        expired = ShortLink.objects.filter(expires_at__isnull=False, expires_at__lte=now)
        logs, _ = DownloadLog.objects.filter(kind=DownloadLog.Kind.SHORT, short_key__in=expired.values("key")).delete()
        links, _ = expired.delete()
    return links, logs


def remove_expired(now: datetime.datetime) -> int:
    """The job of `hfile cleanup` (apps.core.cleanup): delete_expired(), the number of links."""
    return delete_expired(now)[0]
