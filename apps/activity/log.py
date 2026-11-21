"""Writing the 操作日志 (models.ActivityLog), and its kinds in groups for the page's filter.

Each change calls record() (several at once: record_many()) where it is made:
- a change to the database in the same transaction as the change: the entry is there exactly when the
  change is (a failure to write it undoes the change);
- a change made outside the database (a file deleted on a storage source, uploaded, renamed) after it
  is made, `quiet=True`: a failure to write the entry is logged and the answer goes out as it would.
Logins come from apps/accounts/loginlog.py. Downloads aren't here: they are the download log's.

The IP and the browser are those of the request being handled (apps.core.middleware.CurrentRequestMiddleware),
and so is the actor unless given; what a command on the server did has none. While IP 归属地 is on, the entry
also gets the IP's location (apps/ipgeo).
"""

import logging
from dataclasses import dataclass

from django.db import DatabaseError, transaction
from django.utils.translation import gettext_lazy

from apps.core.net import client_ip, current_request
from apps.ipgeo.lookup import locate

from .models import ActivityLog

logger = logging.getLogger(__name__)

Kind = ActivityLog.Kind

NAME_MAX = ActivityLog._meta.get_field("actor_name").max_length
STORAGE_NAME_MAX = ActivityLog._meta.get_field("storage_name").max_length
KEY_MAX = ActivityLog._meta.get_field("key").max_length
USER_AGENT_MAX = ActivityLog._meta.get_field("user_agent").max_length
LIST_MAX = 20  # names an entry keeps of a list (the items of a share); it keeps how many there were too


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    all_label: str  # the filter's option for every kind of it
    kinds: tuple

    @property
    def choices(self):
        return [(kind.value, kind.label) for kind in self.kinds]


# The groups of the filter on /manage/#activity, in Kind's order.
CATEGORIES = (
    Category("account", gettext_lazy("Account"), gettext_lazy("All account actions"), (
        Kind.LOGIN, Kind.LOGIN_FAILED, Kind.CHANGE_PASSWORD, Kind.TOTP_ON, Kind.TOTP_OFF, Kind.LINK_SSO,
        Kind.UNLINK_SSO, Kind.SSO_REGISTER)),
    Category("files", gettext_lazy("Files"), gettext_lazy("All file actions"), (
        Kind.MKDIR, Kind.RENAME, Kind.MOVE, Kind.COPY, Kind.DELETE, Kind.UPLOAD, Kind.EDIT)),
    Category("links", gettext_lazy("Links and shares"), gettext_lazy("All link and share actions"), (
        Kind.SHORT_LINK, Kind.CREATE_SHARE, Kind.DELETE_SHARE, Kind.CLEAR_SHARES)),
    Category("users", gettext_lazy("User management"), gettext_lazy("All user management"), (
        Kind.CREATE_USER, Kind.UPDATE_USER, Kind.COPY_USER, Kind.DELETE_USER, Kind.SET_GRANTS,
        Kind.LINK_IDENTITY, Kind.UNLINK_IDENTITY)),
    Category("storages", gettext_lazy("Storage source management"), gettext_lazy("All storage source management"), (
        Kind.CREATE_STORAGE, Kind.UPDATE_STORAGE, Kind.DELETE_STORAGE, Kind.ENABLE_STORAGE, Kind.DISABLE_STORAGE,
        Kind.COPY_STORAGE, Kind.SORT_STORAGES, Kind.ROTATE_LINK_SECRET, Kind.UPDATE_FILTERS,
        Kind.UPDATE_PASSWORDS, Kind.UPDATE_READMES, Kind.CREATE_COPY_TASK, Kind.STOP_COPY_TASK,
        Kind.RESUME_COPY_TASK)),
    Category("site", gettext_lazy("Site management"), gettext_lazy("All site management"), (
        Kind.UPDATE_SETTINGS, Kind.CREATE_PROVIDER, Kind.UPDATE_PROVIDER, Kind.DELETE_PROVIDER,
        Kind.SORT_PROVIDERS, Kind.DELETE_SHORT_LINKS, Kind.DELETE_DOWNLOAD_LOGS, Kind.UPDATE_IP_GEO,
        Kind.DOWNLOAD_IP_GEO, Kind.FILL_IP_GEO, Kind.INSTALL, Kind.RESET_ADMIN)),
)
CATEGORY_BY_KEY = {category.key: category for category in CATEGORIES}
CATEGORY_OF = {kind.value: category.key for category in CATEGORIES for kind in category.kinds}

FROM_REQUEST = object()  # the default of record()'s actor, ip and user_agent


def text(value, limit):
    return str(value or "").replace("\x00", "")[:limit]


def some(names):
    """{"names": the first LIST_MAX, "count": how many there are} for a list in `detail`."""
    names = [str(name) for name in names]
    return {"names": names[:LIST_MAX], "count": len(names)}


def _user(user):
    """A user an entry names: AnonymousUser is nobody."""
    return user if user is not None and getattr(user, "is_authenticated", False) else None


def entry(kind, *, actor=FROM_REQUEST, target=None, target_name=None, storage=None, path="", to_path="", key="",
          detail=None, ip=FROM_REQUEST, user_agent=FROM_REQUEST, location=None):
    """An entry, not saved yet. `actor`, `ip` and `user_agent` default to those of the request being handled;
    `target_name` to the target's username. `location`: the IP's, when it was looked up already."""
    request = current_request()
    if actor is FROM_REQUEST:
        actor = getattr(request, "user", None) if request is not None else None
    actor = _user(actor)
    target = _user(target)
    if ip is FROM_REQUEST:
        ip = client_ip(request) if request is not None else None
    if user_agent is FROM_REQUEST:
        user_agent = request.META.get("HTTP_USER_AGENT", "") if request is not None else ""
    return ActivityLog(
        kind=kind,
        actor=actor,
        actor_name=text(actor.get_username() if actor else "", NAME_MAX),
        target=target,
        target_name=text(target_name if target_name is not None else (target.get_username() if target else ""), NAME_MAX),
        storage=storage,
        storage_name=text(storage.name if storage is not None else "", STORAGE_NAME_MAX),
        path=text(path, 100_000),
        to_path=text(to_path, 100_000),
        key=text(key, KEY_MAX),
        detail=detail or {},
        ip=ip,
        ip_location=locate(ip) if location is None else location,
        user_agent=text(user_agent, USER_AGENT_MAX),
    )


def _quietly(write, kind):
    """write() in a savepoint; False (and a warning in the program's log) when the database refused it."""
    try:
        with transaction.atomic():
            write()
    except DatabaseError:
        logger.warning("Could not write the activity log (%s)", kind, exc_info=True)
        return False
    return True


def record(kind, *, quiet=False, **fields):
    """Adds one entry (see entry() for the fields) and returns it; None when `quiet` and it couldn't be written."""
    item = entry(kind, **fields)
    if not quiet:
        item.save()
    elif not _quietly(item.save, kind):
        return None
    return item


def record_many(kind, items, *, quiet=False, **common):
    """One entry for each of `items` (dicts of path, to_path, key and detail) with the `common` fields, in one
    INSERT; the IP's location is looked up once."""
    items = list(items)
    if not items:
        return []
    first = entry(kind, **common, **items[0])
    same = {"actor": first.actor, "ip": first.ip, "user_agent": first.user_agent, "location": first.ip_location}
    rows = [first] + [entry(kind, **{**common, **same}, **item) for item in items[1:]]
    if not quiet:
        return ActivityLog.objects.bulk_create(rows)
    return rows if _quietly(lambda: ActivityLog.objects.bulk_create(rows), kind) else []
