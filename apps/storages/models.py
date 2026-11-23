"""Storage sources, who may use them (grants) and their rules (filters, folder passwords, readmes).

A storage source is one configured backend (a folder of the server, a bucket, a drive); its driver
(apps/storages/drivers/) does the talking. `config` holds the driver's params (apps/storages/config.py): plain
JSON, with the secret ones encrypted by apps.core.secrets. The site's own options of a storage (how links
are made, proxying, the gallery switch) are columns.

Every user, the guest and the template included, has at most one Grant per storage: a root folder, an
on/off switch and a list of permission codes (apps/accounts/permissions.py). The access policy
(apps/storages/access.py) decides with these rows and the storage's rules what a request may do.
"""

from __future__ import annotations

import re
import secrets

from django.conf import settings
from django.db import models
from django.utils.translation import gettext as _, gettext_lazy

from apps.accounts.permissions import validate_permissions

KEY_MAX = 32
KEY_RE = re.compile(r"[a-z0-9_-]+")
NAME_MAX = 64
REMARK_MAX = 500
DOMAIN_MAX = 255
ROOT_MAX = 1024
EXPRESSION_MAX = 512
DESCRIPTION_MAX = 255
FOLDER_PASSWORD_MAX = 128
README_MAX = 100_000
LINK_LIFETIME_MIN = 60
LINK_LIFETIME_MAX = 7 * 24 * 3600  # S3 presigned URLs last a week at most
LINK_LIFETIME_DEFAULT = 1800
PROXY_CONNECTIONS_MIN = 1
PROXY_CONNECTIONS_MAX = 1000
PROXY_CONNECTIONS_DEFAULT = 16
ICON_MAX = 40


def new_link_secret() -> str:
    return secrets.token_urlsafe(24)


class WhenFull(models.TextChoices):
    """What a download through the site gets when its storage serves proxy_connections already."""

    LINK = "link", gettext_lazy("Send to the storage's own link")
    REFUSE = "refuse", gettext_lazy("Answer 429 (Too Many Requests)")


class StorageSource(models.Model):
    key = models.CharField(
        gettext_lazy("Alias"), max_length=KEY_MAX, unique=True,
        help_text=gettext_lazy("The name in addresses: /<alias>/folder/file. Lowercase letters, digits, - and _ only."),
    )
    name = models.CharField(gettext_lazy("Name"), max_length=NAME_MAX)
    # apps.storages.drivers.DRIVERS; fixed once the storage exists (the params and paths of another type
    # mean nothing).
    type = models.CharField(gettext_lazy("Type"), max_length=32)
    enabled = models.BooleanField(gettext_lazy("Enabled"), default=True)
    position = models.PositiveIntegerField(gettext_lazy("Order"), default=0)
    remark = models.TextField(gettext_lazy("Remark"), blank=True, help_text=gettext_lazy("Only admins can see it."))
    # The driver's params (apps/storages/config.py): secrets are "enc:..." (apps.core.secrets) or "".
    config = models.JSONField(gettext_lazy("Settings"), default=dict, blank=True)
    # Raised whenever an admin saves the params: the live driver instance is rebuilt for a new
    # revision. Changes the driver makes itself (refreshed tokens) keep it (apps/storages/instances.py).
    config_revision = models.PositiveIntegerField(default=1, editable=False)
    # When the driver last saved changes of its own (a refreshed token).
    tokens_saved_at = models.DateTimeField(null=True, blank=True, editable=False)

    # --- the site's options for downloads and uploads
    # local, FTP, SFTP, WebDAV: downloads go to this address + the storage path instead of /pd/.
    download_domain = models.CharField(gettext_lazy("Download domain"), max_length=DOMAIN_MAX, blank=True)
    # Storages that can give their own download URLs (buckets, OneDrive, ...): send through the site.
    proxy_download = models.BooleanField(gettext_lazy("Download through the site"), default=False)
    # Downloads through the site (/pd/) of this storage at once, every connection counted: one more is sent
    # to the storage's own link (proxy_when_full, for the storages that have one) or refused with 429
    # (apps/files/limits.py). The site has a thread for each, all storages added up (apps/files/pool.py).
    proxy_connections = models.PositiveIntegerField(gettext_lazy("Connections at once"), default=PROXY_CONNECTIONS_DEFAULT)
    proxy_when_full = models.CharField(gettext_lazy("When all are in use"), max_length=10, choices=WhenFull.choices,
                                       default=WhenFull.REFUSE)
    # Storages that can take uploads from the browser: upload through the site instead.
    proxy_upload = models.BooleanField(gettext_lazy("Upload through the site"), default=False)
    force_download = models.BooleanField(gettext_lazy("Download links force downloading"), default=True)
    # Seconds a signed download link (/pd/, a presigned URL) stays valid.
    link_lifetime = models.PositiveIntegerField(gettext_lazy("Download link lifetime (seconds)"), default=LINK_LIFETIME_DEFAULT)
    # Signed links that don't expire (bound to the file). Rotating link_secret revokes them all.
    permanent_links = models.BooleanField(gettext_lazy("Permanent download links"), default=False)
    # Mixed with [site] secret_key into the key that signs this storage's links: not a secret alone.
    link_secret = models.CharField(max_length=64, default=new_link_secret, editable=False)
    gallery_default = models.BooleanField(gettext_lazy("Open in gallery view"), default=False)
    # Readme rules give way to a README.md in the folder itself.
    readme_from_file = models.BooleanField(gettext_lazy("Read README.md in folders"), default=False)
    # A name of apps/storages/icons.py, and whether the storage is a destination of the navigation rail
    # (wider screens; phones keep it in the file browser's storage menu).
    icon = models.CharField(gettext_lazy("Icon"), max_length=ICON_MAX, default="storage")
    show_in_nav = models.BooleanField(gettext_lazy("Show in the sidebar"), default=False)

    created_at = models.DateTimeField(gettext_lazy("Created at"), auto_now_add=True)
    updated_at = models.DateTimeField(gettext_lazy("Modified"), auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Storage source")
        ordering = ["position", "id"]

    def __str__(self):
        return _("%(name)s (%(key)s)") % {"name": self.name, "key": self.key}

    @property
    def type_label(self) -> str:
        from .drivers import DRIVERS, driver_class

        if self.type not in DRIVERS:
            return self.type
        try:
            return str(driver_class(self.type).label)  # made now, in the request's language
        except Exception:  # noqa: BLE001 - a driver whose library is missing still has a name
            return self.type

    @property
    def shown_icon(self) -> str:
        """The icon to show: the default for a name that isn't offered (any more)."""
        from .icons import shown

        return shown(self.icon)

    def link_salt(self) -> str:
        """The salt of this storage's signed links (django.core.signing, keyed by secret_key)."""
        return f"hfile.storages.link.{self.pk}.{self.link_secret}"

    @classmethod
    def by_key(cls, key: str) -> StorageSource | None:
        """The storage of a key from an address, its letters in either case; None when there is none. Only
        what can be a key is looked up: MySQL's collation would also find "docs" for "ｄｏｃｓ", "dócs" and
        "do\\x00cs", and lower() makes the Kelvin sign a k (API-16)."""
        if not key.isascii() or not KEY_RE.fullmatch(key.lower()):
            return None
        return cls.objects.filter(key=key.lower()).first()


class Grant(models.Model):
    """What one user may do on one storage source."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="storage_grants")
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="grants")
    enabled = models.BooleanField(gettext_lazy("Allowed"), default=True)
    # The folder of the storage the user sees as "/" (a clean storage path, apps/storages/drivers/paths.py).
    root_path = models.CharField(gettext_lazy("Root folder"), max_length=ROOT_MAX, default="/")
    permissions = models.JSONField(gettext_lazy("Permissions"), default=list, blank=True, validators=[validate_permissions])

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Storage source permissions")
        constraints = [models.UniqueConstraint(fields=["user", "storage"], name="storages_grant_user_storage_unique")]

    def __str__(self):
        return f"{self.user_id} @ {self.storage_id}"


class FilterMode(models.TextChoices):
    HIDDEN = "hidden", gettext_lazy("Hide only")
    INACCESSIBLE = "inaccessible", gettext_lazy("Hide and block access")
    DISABLE_DOWNLOAD = "disable_download", gettext_lazy("Hide and block downloads")


class ReadmePathMode(models.TextChoices):
    RELATIVE = "relative", gettext_lazy("Relative path")
    ABSOLUTE = "absolute", gettext_lazy("Absolute path")


class ReadmeDisplay(models.TextChoices):
    TOP = "top", gettext_lazy("At the top")
    BOTTOM = "bottom", gettext_lazy("At the bottom")
    DIALOG = "dialog", gettext_lazy("In a dialog")


class Rule(models.Model):
    """What the three kinds of rules share: an ordered glob expression (apps/storages/rules.py) on one storage."""

    position = models.PositiveIntegerField(gettext_lazy("Order"), default=0)
    expression = models.CharField(gettext_lazy("Expression"), max_length=EXPRESSION_MAX)
    description = models.CharField(gettext_lazy("Description"), max_length=DESCRIPTION_MAX, blank=True)

    class Meta:
        abstract = True
        ordering = ["position", "id"]


class FilterRule(Rule):
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="filter_rules")
    mode = models.CharField(gettext_lazy("Mode"), max_length=20, choices=FilterMode.choices, default=FilterMode.HIDDEN)

    class Meta(Rule.Meta):
        verbose_name = verbose_name_plural = gettext_lazy("Filter rule")


class FolderPassword(Rule):
    """A folder password. The first rule (in order) that covers a path decides (apps/storages/access.py).

    The password is kept as typed: the admin hands it out and reads it back (decision D44); it is
    compared in constant time and never logged. `revision` changes with the expression or the
    password, which revokes what visitors unlocked with the old one."""

    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="folder_passwords")
    password = models.CharField(gettext_lazy("Password"), max_length=FOLDER_PASSWORD_MAX)
    revision = models.PositiveIntegerField(default=1, editable=False)

    class Meta(Rule.Meta):
        verbose_name = verbose_name_plural = gettext_lazy("Folder password")


class ReadmeRule(Rule):
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="readme_rules")
    text = models.TextField(gettext_lazy("Readme (Markdown)"), blank=True)
    path_mode = models.CharField(gettext_lazy("Path mode"), max_length=10, choices=ReadmePathMode.choices, default=ReadmePathMode.RELATIVE)
    display = models.CharField(gettext_lazy("Shown"), max_length=10, choices=ReadmeDisplay.choices, default=ReadmeDisplay.TOP)

    class Meta(Rule.Meta):
        verbose_name = verbose_name_plural = gettext_lazy("Folder readme")
