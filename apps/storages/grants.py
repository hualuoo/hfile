"""Grants: which user may use which storage source, from which root folder, with which permissions.

- A new storage gives every user (the guest and the template included) a grant from that user's
  default permissions: root "/", enabled only when the list isn't empty (contract 3).
- Copying a storage copies its grants; copying a user (apps.accounts.signals.user_copied) copies his.
- The admin edits a user's grants as one list with a row per storage (GET/PUT
  /api/v1/manage/users/<id>/grants/, App.grantsEditor in apps/storages/static/storages/js/grants.js).
"""

from __future__ import annotations

import unicodedata

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _, ngettext

from apps.accounts.permissions import clean_permissions, usable

from .drivers import paths
from .drivers.base import InvalidPath
from .models import ROOT_MAX, Grant, StorageSource


def clean_root(value) -> str:
    """A grant's root folder as stored: a clean storage path ("" is "/"). Raises ValidationError.

    Names are kept as typed (a driver uses them exactly): no NFC rewrite, which could miss a folder
    that the backend stores decomposed, and no spaces cut off at the ends: "/team " is the folder
    "team ", not "/team" (API-12). Stray spaces make a segment of spaces only, which clean() refuses."""
    if value is None:
        return "/"
    if not isinstance(value, str):
        raise ValidationError(_("The root folder should be text."))
    if len(value) > 4 * ROOT_MAX:
        raise ValidationError(ngettext("The root folder is at most %(limit)s character.", "The root folder is at most %(limit)s characters.", ROOT_MAX) % {"limit": ROOT_MAX})
    if any(unicodedata.category(ch) == "Cc" for ch in value):
        raise ValidationError(_("The root folder can't contain control characters such as line breaks or tabs."))
    if any(part and not part.strip() for part in value.split("/")):
        raise ValidationError(_("The root folder has only spaces at its start, end or between two /: please remove them (spaces within names are kept)."))
    try:
        root = paths.clean(value)
    except InvalidPath as error:
        raise ValidationError(_("Invalid root folder: %(message)s") % {"message": error.message}) from None
    if len(root) > ROOT_MAX:  # as stored: clean() may put a "/" in front (API-11)
        raise ValidationError(ngettext("The root folder is at most %(limit)s character (counting the leading /).", "The root folder is at most %(limit)s characters (counting the leading /).", ROOT_MAX) % {"limit": ROOT_MAX})
    return root


def _defaults(user) -> list[str]:
    """The user's default permissions, without those missing what they need first (saved before D55)."""
    return usable(clean_permissions(user.default_permissions or []))


def default_grant(user, storage: StorageSource) -> Grant:
    permissions = _defaults(user)
    return Grant(user=user, storage=storage, root_path="/", enabled=bool(permissions), permissions=permissions)


def grant_new_storage(storage: StorageSource) -> dict[str, int]:
    """Contract 3: every user gets a grant from his default permissions. In the creating transaction."""
    User = get_user_model()
    User.objects.guest()  # the built-in users exist (a flushed test database makes them again)
    User.objects.template()
    users = list(User.objects.exclude(storage_grants__storage=storage))
    Grant.objects.bulk_create([default_grant(user, storage) for user in users])
    enabled = sum(1 for user in users if _defaults(user))
    return {"users": len(users), "enabled": enabled}


def copy_storage_grants(source: StorageSource, target: StorageSource) -> None:
    Grant.objects.bulk_create([
        Grant(user_id=grant.user_id, storage=target, root_path=grant.root_path, enabled=grant.enabled,
              permissions=usable(grant.permissions or []))
        for grant in Grant.objects.filter(storage=source)
    ])


def copy_user_grants(sender, source, target, **kwargs) -> None:
    """apps.accounts.signals.user_copied (contract 2): the copy gets the source's grants, inside the
    transaction that made it."""
    existing = {grant.storage_id: grant for grant in Grant.objects.filter(user=target)}
    for grant in Grant.objects.filter(user=source):
        copy = existing.get(grant.storage_id) or Grant(user=target, storage_id=grant.storage_id)
        copy.root_path = grant.root_path
        copy.enabled = grant.enabled
        copy.permissions = usable(grant.permissions or [])  # without what misses a prerequisite (D55)
        copy.save()


def rows_for(user) -> list[dict]:
    """The grants editor's list: one row per storage, in display order, granted or not. A permission saved
    before D55 without what it needs first is left out (it did nothing)."""
    grants = {grant.storage_id: grant for grant in Grant.objects.filter(user=user)}
    rows = []
    for storage in StorageSource.objects.order_by("position", "id"):
        grant = grants.get(storage.pk)
        rows.append({
            "storage": {"id": storage.pk, "key": storage.key, "name": storage.name, "type": storage.type,
                        "enabled": storage.enabled},
            "granted": grant is not None,
            "enabled": bool(grant and grant.enabled),
            "root_path": grant.root_path if grant else "/",
            "permissions": usable(grant.permissions or []) if grant else [],
        })
    return rows
