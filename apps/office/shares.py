"""OnlyOffice inside a share (inventory 10.5): the one thing this app needs of the shares app.

Contract S1 (briefs/wave3-shares.md), which the shares app provides:

    apps.shares.access.share_file(request, key, path) -> (access, storage_path)

`key` is the share key and `path` the path inside the share as the share page sends it. It checks the
share as every share request does (it exists, isn't expired, this request's share cookie says its
password was entered, the path lies inside the shared entries, the owner still may share it) and
returns a apps.storages.access.Access whose actor is the owner rooted at the share's folder, with the
file's storage path. It raises apps.storages.access.AccessDenied, or a shares error with a `code`, as the
share API answers. The caller then runs access.check(<operation>, storage_path, is_dir=False).

Everything here goes through share_file() below, so that this app doesn't depend on the shares app
being installed: without it there are no shares to open (404).
"""

from __future__ import annotations

from django.apps import apps
from django.utils.translation import gettext as _

from apps.storages.access import AccessDenied

NOT_FOUND = "share_not_found"


def share_file(request, key: str, path: str):
    """(access, storage_path) of a file inside a share, by contract S1."""
    if not apps.is_installed("shares"):
        raise AccessDenied(NOT_FOUND, _("The share doesn't exist."), status=404)
    from apps.shares import access as share_access

    return share_access.share_file(request, key, path)
