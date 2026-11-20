"""Shares: creating them, what a visitor gets (the listing, one item, the URL of a download or a
preview), the owners' and admins' lists, deleting, and the cleanup job (inventory 10.2-10.8).

Every visitor request goes through apps/shares/access.py first (the share, the visit, the owner re-checked,
the path within the entries); here the storage is asked and the answers are shaped as files' (the share
page mounts files' browser on them, contract W4):

- listing items carry the share's own URLs, never a storage's: "url" (what <img>, <video>, <audio> and
  the gallery load) is /share/<key>/preview?path=... with the owner's preview, else the download one;
  "download_url" is /share/<key>/download?path=... with the owner's download. Both check the share
  again when opened and redirect to the file's URL (apps.files.transfer, W1), counting a download;
- the item endpoint's "inline_url" is a files-signed /pd/ link (same origin, inline): what the viewers
  read with JS and what the Markdown and kkFileView endpoints accept;
- access_count +1 for each listing of the share's root, download_count +1 for each download or preview
  URL handed out (inventory 10.6), by F() updates.
"""

from __future__ import annotations

import datetime
import logging
import urllib.parse
from types import SimpleNamespace
from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import F, Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.translation import gettext

from apps.accounts.permissions import Permission
from apps.activity import log as activity
from apps.core import origin
from apps.core.times import iso
from apps.files import transfer
from apps.files.kinds import Kinds
from apps.files.service import FileError, capabilities, driver_problem, item_data, sort_entries, storage_data
from apps.storages import access as policy
from apps.storages import instances
from apps.storages.access import LIST, PREVIEW, SHARE, STAT, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError, NotADirectory, NotFound

from . import access as shares
from .access import ANY, ShareError
from .models import FILE, FOLDER, Share, new_key

logger = logging.getLogger("hfile.shares")

SORT_FIELDS = ("name", "size", "time")
LINK_MAX = 3600  # seconds a /pd/ link handed out in a share lasts at most (also on a 永久下载链接 storage)
ORDERS = ("asc", "desc")
KEY_TRIES = 10


# --- URLs ---------------------------------------------------------------------------------------------

def page_path(share: Share) -> str:
    return f"/share/{share.key}"


def absolute_url(share: Share, request=None) -> str:
    """The link people pass on, absolute (apps.core.origin: [site] url, else the request's address)."""
    return origin.absolute(page_path(share), request)


def file_url(share: Share, client_path: str, what: str) -> str:
    """/share/<key>/download or /preview with the file's path in the share."""
    return f"{page_path(share)}/{what}?path={urllib.parse.quote(client_path, safe='/')}"


# --- creating ------------------------------------------------------------------------------------------

def _driver_error(access, error: DriverError, what: str) -> FileError:
    """The answer for a driver's error (plain words: visitors are nobody's staff); the backend's own
    words go to the log."""
    code, message, status = driver_problem(error, staff=False)
    if code in ("storage_auth", "storage_error", "failed"):
        logger.warning("Storage %s: %s failed: %s", access.storage.key, what, error.message)
    return FileError(code, message, status_code=status)


def create(request, user, body: dict) -> Share:
    """创建分享 (inventory 10.2): `body` as serializers.CreateBody checked it. The cheap checks come
    first: the user's grant and permission (create_share_link; custom_share_key for a key of one's own)
    for the folder and every entry, filters and folder passwords included; then one listing of the
    folder, which every entry must be in (its type is the storage's). Raises AccessDenied, FileError."""
    access = policy.open_storage(request, body["storage"], actor=policy.Actor(user))
    folder = access.path(body["path"])
    access.check(SHARE, folder)
    custom_key = body.get("key") or ""
    if custom_key and not access.has(Permission.CUSTOM_SHARE_KEY):
        raise AccessDenied(policy.PERMISSION_DENIED, gettext("You don't have permission to choose the share link's key."), permission=Permission.CUSTOM_SHARE_KEY)
    wanted = body["entries"]  # [(name, type)]
    names = list(dict.fromkeys(name for name, _kind in wanted))
    if len(names) < len(wanted):
        raise FileError("invalid_name", gettext("A file and a folder with the same name can't be shared together. Please share them separately."))
    inside = access.relative(folder).rstrip("/")
    for name in names:
        entry_path = access.path(f"{inside}/{name}")  # one segment (the serializer), the policy's path checks
        if paths.parent(entry_path) != folder:
            raise FileError("invalid_name", gettext("Invalid name."))
        access.check(SHARE, entry_path)
    try:
        # By name and kind: on S3 an object "a" and a prefix "a/" are both listed.
        listed = {(entry.name, FOLDER if entry.is_dir else FILE): entry for entry in access.driver.list(folder)}
    except NotFound:
        raise FileError("not_found", gettext("The folder doesn't exist."), status_code=404) from None
    except NotADirectory:
        raise FileError("not_a_folder", gettext("This isn't a folder.")) from None
    except DriverError as error:
        raise _driver_error(access, error, f"listing {folder}") from None
    entries = []
    for name, kind in wanted:
        entry = listed.get((name, kind))
        if entry is None:
            if kind == FOLDER:
                gone = gettext("The folder \"%(name)s\" doesn't exist; it may have just been moved or deleted. Please refresh and try again.")
            else:
                gone = gettext("The file \"%(name)s\" doesn't exist; it may have just been moved or deleted. Please refresh and try again.")
            raise FileError("not_found", gone % {"name": name}, status_code=404)
        if policy.filter_state(access.rules, entry.path)["hidden"]:
            # Visitors never see what a filter rule hides (D32): such a share would look empty.
            raise FileError("hidden", gettext("\"%(name)s\" is hidden by a filter rule, so people opening the share couldn't see it; it can't be shared.") % {"name": name})
        entries.append({"name": name, "type": FOLDER if entry.is_dir else FILE})
    share = Share(storage=access.storage, folder=folder, entries=entries, names="\n".join(names), owner=user,
                  expires_at=body.get("expires_at"))
    share.set_password(body.get("password") or "")
    return _save(share, custom_key)


def _save(share: Share, custom_key: str) -> Share:
    """With the custom key, or a new random one (another one when it is taken); logged in the activity log."""
    for _ in range(1 if custom_key else KEY_TRIES):
        share.key = custom_key or new_key()
        try:
            with transaction.atomic():
                share.save()
                activity.record(activity.Kind.CREATE_SHARE, actor=share.owner, storage=share.storage, path=share.folder,
                                key=share.key, detail=log_detail(share, password=share.has_password,
                                                                 expires_at=iso(share.expires_at)))
            return share
        except IntegrityError:
            if Share.objects.filter(key=share.key).exists():
                if custom_key:
                    raise ShareError("key_taken", gettext("That share key is taken. Please choose another."), status=409) from None
                continue
            raise
    raise ShareError("key_taken", gettext("Couldn't generate a new share key. Please try again."), status=409)


# --- what visitors see --------------------------------------------------------------------------------

def public_info(request, share: Share, visit: shares.Visit, access=None) -> dict:
    """GET /api/v1/shares/<key>/ and the share page's config. Before the password (10.Z.4): whether one
    is needed, the expiry, the number of entries and the owner's nickname. After it, while the owner
    may still share (open_share(), or the `access` the caller has from it), the storage and the entries
    as the visitor may see them now (filter rules, a locked folder); else nothing more."""
    data: dict[str, Any] = {
        "key": share.key,
        "need_password": share.has_password,
        "verified": visit.verified,
        "expired": False,
        "expires_at": iso(share.expires_at),
        "entry_count": len(share.entries or ()),
        "owner": share.owner.nickname or None,
    }
    if not visit.verified:
        return data
    if access is None:
        try:
            access = shares.open_share(request, share, visit)
        except AccessDenied:
            return data
    data.update({
        "entries": visible_entries(access, share),
        "storage": storage_data(share.storage),
        "created_at": iso(share.created_at),
    })
    return data


def visible_entries(access, share: Share) -> list[dict]:
    """The shared entries a visitor sees now: those a filter rule hides, or inside a folder whose
    password this visit hasn't given, are left out (the listing of "/" asks the storage itself)."""
    stand_ins = [SimpleNamespace(name=item["name"], path=paths.join(access.root, item["name"]), type=item["type"])
                 for item in share.entries or ()]
    return [{"name": entry.name, "type": entry.type} for entry in access.visible(stand_ins)]


def visitor_permissions(access) -> dict[str, bool]:
    """What the share page offers (files' permission map): the owner's preview and download (the server
    checks them again, D32), 复制下载链接 with both copy_download_link and download, and no operation."""
    granted = dict.fromkeys(Permission.values, False)
    granted[Permission.PREVIEW] = access.may(PREVIEW)  # with download, which it needs too
    granted[Permission.DOWNLOAD] = access.has(Permission.DOWNLOAD)
    granted[Permission.COPY_DOWNLOAD_LINK] = access.has(Permission.COPY_DOWNLOAD_LINK) and access.has(Permission.DOWNLOAD)
    return granted


def _download_disabled(access, storage_path: str) -> bool:
    return bool(access.rules.no_download) and policy.filter_state(access.rules, storage_path)["download_disabled"]


def _item(access, share: Share, entry, kinds: Kinds) -> dict:
    """files' item (name, path in the share, type, size, modified, kind, locked), with the share's URLs."""
    data = item_data(access, entry, kinds, None)
    data["download_url"] = None
    if data["type"] == "file" and not data.get("locked") and not _download_disabled(access, entry.path):
        if access.has(Permission.DOWNLOAD):
            data["download_url"] = file_url(share, data["path"], "download")
        if access.may(PREVIEW):
            data["url"] = file_url(share, data["path"], "preview")
        else:
            data["url"] = data["download_url"]
    return data


def _counted(share: Share, field: str) -> None:
    Share.objects.filter(pk=share.pk).update(**{field: F(field) + 1})


def _list(access, folder: str):
    try:
        return access.driver.list(folder)
    except NotFound:
        raise FileError("not_found", gettext("The folder doesn't exist."), status_code=404) from None
    except NotADirectory:
        raise FileError("not_a_folder", gettext("This isn't a folder.")) from None
    except DriverError as error:
        raise _driver_error(access, error, f"listing {folder}") from None


def listing(request, share: Share, visit: shares.Visit, path: Any, *, sort: str | None = None,
            order: str | None = None) -> dict:
    """A folder of the share as files' listing (W4): at "/" only the shared entries (by name and type),
    below them everything the filter rules let visitors see; no readme, no root."""
    from apps.siteconf.models import SiteSettings

    site = SiteSettings.load()
    sort = sort if sort in SORT_FIELDS else site.default_sort_field
    order = order if order in ORDERS else site.default_sort_order
    access = shares.open_share(request, share, visit)
    folder = shares.resolve(access, share, path, want=FOLDER)
    access.check(LIST, folder)
    entries = _list(access, folder)
    at_root = folder == access.root
    if at_root:
        wanted = {(item["name"], item["type"]) for item in share.entries or ()}
        entries = [entry for entry in entries if (entry.name, FOLDER if entry.is_dir else FILE) in wanted]
    shown = sort_entries(access.visible(entries, keys=access.opened), sort, order)
    kinds = Kinds(site)
    items = [_item(access, share, entry, kinds) for entry in shown]
    if at_root:
        _counted(share, "access_count")
    return {
        "storage": storage_data(share.storage),
        "path": access.relative(folder),
        "root": None,
        "items": items,
        "readme": None,
        "permissions": visitor_permissions(access),
        "capabilities": capabilities(access),
        "gallery_default": share.storage.gallery_default,
        "sort": {"field": sort, "order": order},
    }


def item(request, share: Share, visit: shares.Visit, path: Any) -> dict:
    """One file or folder of the share; a file the owner may preview gets "inline_url" (a files-signed
    same-origin /pd/ link, for the viewers), which counts as a download."""
    from apps.siteconf.models import SiteSettings

    access = shares.open_share(request, share, visit)
    storage_path = shares.resolve(access, share, path, want=ANY)
    access.check(STAT, storage_path)
    if _download_disabled(access, storage_path):
        raise AccessDenied(policy.DOWNLOAD_DISABLED, gettext("This file can't be downloaded."))
    try:
        entry = access.driver.stat(storage_path)
    except DriverError as error:
        raise _driver_error(access, error, f"stat {storage_path}") from None
    relative = access.relative(storage_path)
    first, _, rest = relative[1:].partition("/")
    shared = share.entry(first) if relative != "/" else None
    if shared is not None and not rest and (shared["type"] == FOLDER) != entry.is_dir:
        # The shared file is a folder now, or the shared folder a file (S3: an object beside a prefix).
        raise ShareError(shares.NOT_FOUND, gettext("The file or folder doesn't exist."), status=404)
    data = _item(access, share, entry, Kinds(SiteSettings.load()))
    if storage_path == access.root:
        data["name"] = ""
    data["inline_url"] = None
    if not entry.is_dir and access.may(PREVIEW):
        access.check(PREVIEW, storage_path, is_dir=False)
        data["inline_url"] = share_link(request, share, storage_path, inline=True, same_origin=True)
        _counted(share, "download_count")
    return data


def _link_lifetime(share: Share) -> int:
    """Seconds a link the share hands out may last: the storage's link lifetime, at most LINK_MAX, and no
    longer than the share has left (share_link() also caps the expiry at the share's end itself, since
    signing rounds expiries up)."""
    lifetime = min(share.storage.link_lifetime or LINK_MAX, LINK_MAX)
    if share.expires_at is not None:
        lifetime = min(lifetime, int((share.expires_at - timezone.now()).total_seconds()))
    return max(1, lifetime)


def share_link(request, share: Share, storage_path: str, *, inline: bool | None = None,
               same_origin: bool = False) -> str:
    """The URL of a file of the share (W1, apps.files.transfer.download_url), made safe to hand to a visitor
    with W1's lifetime and not_after:

    - it lasts at most LINK_MAX and ends at the share's end at the latest: this site's /pd/ links (never
      permanent, also on a storage with 永久下载链接) and a bucket's presigned links. Else a deleted,
      expired or password share would be outlived by them;
    - OneDrive, SharePoint and 115 (transfer.DEFERRED): a /pd/ link that redirects to the backend's link
      when opened (r=1, as files' listings do), where it is cached and limited per address, rather than
      a request to the backend for every hit of /share/<key>/download or /preview.

    A download domain's URL is the storage's, unsigned (W1 can't limit it)."""
    ends = int(share.expires_at.timestamp()) if share.expires_at is not None else None
    return transfer.download_url(request, share.storage, storage_path, inline=inline, same_origin=same_origin,
                                 lifetime=_link_lifetime(share), not_after=ends)


def file_target(request, share: Share, visit: shares.Visit, path: Any, operation: str, *,
                same_origin: bool = False) -> str:
    """Where /share/<key>/download (operation DOWNLOAD) or /preview (PREVIEW) sends the browser: the
    file's URL (W1; inline for a preview; this site's signed /pd/ link with same_origin), after the
    checks; counts a download. The storage isn't asked whether the file is there (a gallery would ask
    once per picture): the URL fails if it isn't."""
    access = shares.open_share(request, share, visit)
    storage_path = shares.resolve(access, share, path, want=FILE)
    access.check(operation, storage_path, is_dir=False)
    try:
        url = share_link(request, share, storage_path, inline=True if operation == PREVIEW else None,
                         same_origin=same_origin)
    except instances.StorageUnavailable as error:
        raise AccessDenied(policy.STORAGE_DISABLED, error.message) from None
    except DriverError as error:
        raise _driver_error(access, error, f"download link of {storage_path}") from None
    _counted(share, "download_count")
    return url


# --- the lists of 我的分享 and 分享列表 -----------------------------------------------------------------

def _start_of(day: datetime.date) -> datetime.datetime:
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def filtered(shares_qs, query: dict, *, now=None):
    """The lists' filters: keyword (the key or a name), storage (id), status (all, active, expired),
    created since/until (days, in the site's time zone), user (the admin's: username or nickname)."""
    now = now or timezone.now()
    keyword = query.get("keyword") or ""
    if keyword:
        # Keys compare exactly (a binary collation on MySQL): a part typed in any case is looked for in their
        # lower-case form, the same on both databases (API-17; as apps/links/manage_views.py's _key_contains).
        shares_qs = shares_qs.annotate(_key_lower=Lower("key")).filter(
            Q(_key_lower__contains=keyword.lower()) | Q(names__icontains=keyword))
    if query.get("storage"):
        shares_qs = shares_qs.filter(storage_id=query["storage"])
    status = query.get("status") or "all"
    if status == "active":
        shares_qs = shares_qs.filter(Q(expires_at__isnull=True) | Q(expires_at__gt=now))
    elif status == "expired":
        shares_qs = shares_qs.filter(expires_at__lte=now)
    if query.get("since"):
        shares_qs = shares_qs.filter(created_at__gte=_start_of(query["since"]))
    if query.get("until"):
        shares_qs = shares_qs.filter(created_at__lt=_start_of(query["until"] + datetime.timedelta(days=1)))
    user = query.get("user") or ""
    if user:
        shares_qs = shares_qs.filter(Q(owner__username__icontains=user) | Q(owner__nickname__icontains=user))
    return shares_qs.order_by("-created_at", "-id")


def log_detail(share: Share, **more) -> dict:
    """What the activity log keeps of a share: the names of what it holds, and `more`."""
    return {"items": activity.some(entry.get("name") for entry in share.entries or [] if isinstance(entry, dict)), **more}


def may_delete(user, share: Share) -> bool:
    """The owner, or any superuser."""
    return bool(user and user.is_authenticated and (share.owner_id == user.pk or user.is_superuser))


def clear_expired(*, owner=None, now=None) -> int:
    """清理所有已过期分享: the owner's, or everybody's (owner None)."""
    expired = Share.objects.filter(expires_at__lte=now or timezone.now())
    if owner is not None:
        expired = expired.filter(owner=owner)
    deleted, _ = expired.delete()
    return deleted


def remove_expired(now) -> int:
    """hfile cleanup (D29): every share expired at `now`."""
    return clear_expired(now=now)


