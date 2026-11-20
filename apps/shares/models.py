"""Share links (分享, inventory section 10; decisions D6, D32, D44).

A share publishes some files and folders of one folder of a storage under /share/<key>. Visitors act as
its owner (apps.storages.access.share_actor): the owner's grant, rooted at the share's folder, re-checked on
every request (apps/shares/access.py).

- key: what the link says, compared as it is spelled (KeyField): "Ab3" and "ab3" are two shares on
  both databases. A custom key is [A-Za-z0-9_-]{3,8} (custom_share_key), else 8 random characters of
  [A-Za-z0-9] from `secrets`.
- folder: the storage-absolute path of the folder holding the entries, inside the owner's root when
  the share was made. entries: [{"name", "type": "file" | "folder"}], each name one segment of it;
  names: the same names one per line, for the lists' keyword filter.
- password: "" for none, else encrypted with apps.core.secrets: the owner and admins can read it back
  ("复制带密码链接", D32), visitors' answers are compared in constant time.
- nonce and revision go into the salt of the visitors' cookies (apps/shares/access.py Visit): a cookie of
  another share, or of an older password, doesn't verify. Shares can't be edited, so
  revision stays 1 for now.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import string

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy, ngettext

from apps.core import secrets as stored_secrets
from apps.storages.models import StorageSource

KEY_ALPHABET = string.ascii_letters + string.digits
KEY_LENGTH = 8  # 62**8: about 2 * 10**14 keys
KEY_MAX = 16
CUSTOM_KEY_MIN, CUSTOM_KEY_MAX = 3, 8
PASSWORD_MIN, PASSWORD_MAX = 4, 20
ENTRIES_MAX = 1000
FILE, FOLDER = "file", "folder"


def new_key() -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def new_nonce() -> str:
    return secrets.token_urlsafe(12)


class KeyField(models.CharField):
    """A key compared exactly as it is spelled. SQLite compares text byte by byte already; MySQL's
    default collation (utf8mb4_0900_ai_ci) ignores case and accents, so the column is utf8mb4_bin there."""

    def db_parameters(self, connection):
        parameters = super().db_parameters(connection)
        if connection.vendor == "mysql":
            parameters["collation"] = "utf8mb4_bin"
        return parameters


class Share(models.Model):
    key = KeyField("Share Key", max_length=KEY_MAX, unique=True)
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="shares", verbose_name=gettext_lazy("Storage source"))
    folder = models.TextField(gettext_lazy("Folder"), help_text=gettext_lazy("The full path in the storage source."))
    entries = models.JSONField(gettext_lazy("Shared files and folders"), default=list)
    names = models.TextField(gettext_lazy("Name"), blank=True, editable=False)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="shares", verbose_name=gettext_lazy("Shared by"))
    password = models.TextField(gettext_lazy("Access password"), blank=True, editable=False)
    created_at = models.DateTimeField(gettext_lazy("Created at"), default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(gettext_lazy("Valid until"), null=True, blank=True, db_index=True, help_text=gettext_lazy("Empty means it never expires."))
    access_count = models.PositiveBigIntegerField(gettext_lazy("Visits"), default=0)
    download_count = models.PositiveBigIntegerField(gettext_lazy("Downloads"), default=0)
    revision = models.PositiveIntegerField(default=1, editable=False)
    nonce = models.CharField(max_length=32, default=new_nonce, editable=False)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Share")
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["owner", "created_at"], name="shares_owner_created")]

    def __str__(self):
        return self.key

    # -- the password

    @property
    def has_password(self) -> bool:
        return bool(self.password)

    def set_password(self, raw: str) -> None:
        self.password = stored_secrets.encrypt(raw) if raw else ""

    def plain_password(self) -> str:
        """For the owner and admins. Raises apps.core.secrets.UnreadableSecret after secret_key changed."""
        return stored_secrets.decrypt(self.password)

    def password_matches(self, given: str) -> bool:
        """In constant time, also as to the length (both sides hashed first); spaces at either end don't
        count (they come with copying)."""
        if not self.password or not isinstance(given, str):
            return False
        expected = hashlib.sha256(self.plain_password().encode("utf-8")).digest()
        return hmac.compare_digest(hashlib.sha256(given.strip().encode("utf-8")).digest(), expected)

    # -- the rest

    def is_expired(self, now=None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or timezone.now())

    def entry(self, name: str) -> dict | None:
        """The entry of that name (exactly), or None."""
        for item in self.entries or ():
            if isinstance(item, dict) and item.get("name") == name and item.get("type") in (FILE, FOLDER):
                return item
        return None

    @property
    def label(self) -> str:
        """What the lists show: the first name, "等 N 项" when there are more."""
        names = [item.get("name", "") for item in self.entries or ()]
        if not names:
            return ""
        return names[0] if len(names) == 1 else ngettext("%(first)s and others (%(count)s item)", "%(first)s and others (%(count)s items)", len(names)) % {"first": names[0], "count": len(names)}
