"""What a user may do on a storage source.

A user's access to one storage source is a grant (storages app): a root folder, an on/off switch
("允许使用") and a list of these permission codes. User.default_permissions is the list a user
gets on storage sources created later; storage sources that exist already aren't touched.

The codes' labels and help texts are shown on the user edit page.

Some codes are of no use without another (REQUIRES, decision D55): the pickers on the user pages offer
them only once one of their prerequisites is ticked, and the API refuses them alone. Lists saved before
are shown without them (usable()): alone they never did anything (storages.access refuses links without
下载, a file has no download address without 预览 or 下载, a share needs 创建分享链接 first).
"""

from __future__ import annotations

from collections.abc import Iterable

from django.core.exceptions import ValidationError
from django.db import models
from django.utils.translation import gettext as _, gettext_lazy, ngettext


class Permission(models.TextChoices):
    NEW_FOLDER = "new_folder", gettext_lazy("New folder")
    UPLOAD = "upload", gettext_lazy("Upload")
    PREVIEW = "preview", gettext_lazy("Preview")
    DOWNLOAD = "download", gettext_lazy("Download")
    COPY_DOWNLOAD_LINK = "copy_download_link", gettext_lazy("Copy download link")
    RENAME = "rename", gettext_lazy("Rename")
    COPY = "copy", gettext_lazy("Copy")
    MOVE = "move", gettext_lazy("Move")
    DELETE = "delete", gettext_lazy("Delete")
    GENERATE_LINK = "generate_link", gettext_lazy("Get direct links")
    GENERATE_SHORT_LINK = "generate_short_link", gettext_lazy("Create short links")
    CREATE_SHARE_LINK = "create_share_link", gettext_lazy("Create share links")
    CUSTOM_SHARE_KEY = "custom_share_key", gettext_lazy("Choose share link keys")
    IGNORE_PASSWORD = "ignore_password", gettext_lazy("Ignore passwords")
    IGNORE_HIDDEN = "ignore_hidden", gettext_lazy("Ignore hiding")


HELP = {
    Permission.NEW_FOLDER: gettext_lazy("Turning this off also turns off uploading folders."),
    Permission.UPLOAD: gettext_lazy('Upload files, and save after editing online. Overwriting an existing file also needs the "Delete" permission.'),
    Permission.PREVIEW: gettext_lazy("Preview images, video, audio, text, Office files and more in the browser."),
    Permission.DOWNLOAD: gettext_lazy("Allowing previews but not downloads only hides the download buttons; it can't fully stop a file from being downloaded."),
    Permission.COPY_DOWNLOAD_LINK: gettext_lazy("Adds \"Copy download link\" to the right-click menu: it copies the file's current download address, not a direct or short link."),
    Permission.CREATE_SHARE_LINK: gettext_lazy("Allows creating share links."),
    Permission.CUSTOM_SHARE_KEY: gettext_lazy("When creating a share link, the key in the link can be chosen instead of generated."),
    Permission.IGNORE_PASSWORD: gettext_lazy("Ignores every folder password of this storage source; no passwords needed."),
    Permission.IGNORE_HIDDEN: gettext_lazy("Ignores every filter rule of this storage source; every folder and file can be seen."),
}

# What a permission needs first: one of these. 复制下载链接 copies the listing's download address, which
# needs 预览 or 下载 (storages.access READ); a link hands out the file (D40a); a key of one's own is for
# creating shares.
REQUIRES: dict[str, tuple[str, ...]] = {
    Permission.COPY_DOWNLOAD_LINK: (Permission.PREVIEW, Permission.DOWNLOAD),
    Permission.GENERATE_LINK: (Permission.DOWNLOAD,),
    Permission.GENERATE_SHORT_LINK: (Permission.DOWNLOAD,),
    Permission.CUSTOM_SHARE_KEY: (Permission.CREATE_SHARE_LINK,),
}

ALL: tuple[str, ...] = tuple(Permission.values)

# The first admin's default permissions: everything but the two that bypass the rules. Everyone
# else starts with none.
ADMIN_DEFAULTS: tuple[str, ...] = tuple(code for code in ALL if not code.startswith("ignore_"))


def clean_permissions(value: Iterable[str] | None) -> list[str]:
    """A checked list of codes, without duplicates, in the order of Permission. Raises ValidationError."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple, set, frozenset)):
        raise ValidationError(_("Permissions should be a list."))
    unknown = [code for code in value if not isinstance(code, str) or code not in ALL]
    if unknown:
        # The first three, each cut at 40 characters: not whatever was sent, 5000 characters too (SEC-05).
        shown = [text if len(text) <= 40 else text[:40] + "…" for text in map(str, unknown[:3])]
        more = ngettext(" and others (%(count)s in all)", " and others (%(count)s in all)", len(unknown)) % {"count": len(unknown)} if len(unknown) > 3 else ""
        raise ValidationError(_("Unknown permissions: %(codes)s"), params={"codes": _(", ").join(shown) + more})
    chosen = set(value)
    return [code for code in ALL if code in chosen]


def _either(codes: Iterable[str]) -> str:
    return _(" or ").join(_('"%(label)s"') % {"label": Permission(code).label} for code in codes)


def unmet(codes: Iterable[str]) -> list[str]:
    """The codes among these whose prerequisites (REQUIRES) aren't among them, in their order."""
    codes = list(codes)
    chosen = set(codes)
    return [code for code in codes if code in REQUIRES and chosen.isdisjoint(REQUIRES[code])]


def usable(codes: Iterable[str]) -> list[str]:
    """The codes without those that miss their prerequisites: what a list saved before D55 amounts to."""
    kept = list(codes)
    while missing := set(unmet(kept)):
        kept = [code for code in kept if code not in missing]
    return kept


def check_requires(codes: Iterable[str]) -> None:
    """For what a client sends: ValidationError naming each code that misses its prerequisites."""
    missing = unmet(codes)
    if missing:
        raise ValidationError([_('"%(label)s" needs the %(needed)s permission first.') % {"label": Permission(code).label, "needed": _either(REQUIRES[code])} for code in missing])


def help_for(code: str) -> str:
    """The text under a permission on the user pages: what it needs first, then HELP."""
    needs = _("Check %(needed)s first. ") % {"needed": _either(REQUIRES[code])} if code in REQUIRES else ""
    return needs + HELP.get(code, "")


def validate_permissions(value) -> None:
    """For model fields: the stored list must already be clean."""
    if clean_permissions(value) != list(value or []):
        raise ValidationError(_("The permission list has repeated items or is in the wrong order."))
