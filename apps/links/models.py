"""Short links and the download log (inventory 1.10, 1.11 and section 9; decisions D6, D31, D40, D44).

- ShortLink: /s/<key> for one file of a storage, made by a user (created_by). Opening it checks that
  user again (D31): still active, still with the permissions, the file still inside his root and allowed
  by the current filter rules, and the folder passwords he had unlocked when he made it (`keys`).
  expires_at NULL is forever (D44). Deleting the storage or the user deletes the link.
- DownloadLog: one row for each download through a path link or a short link while 记录下载日志 is on,
  and for each download from the 下载 button of the site's pages that goes through the site (页面下载:
  /pd/ links, apps.files.download.download_clicked; D54). `username` is who was logged in, "" for a
  visitor (and for the rows older than migration links.0002). The short link is a plain key (D40):
  deleting one link keeps its log, cleaning up expired links removes theirs (apps.links.service.remove_expired).

Keys are compared exactly, on every database: MySQL's collation ignores case (and accents), so the key
columns use a binary collation there (KeyField). "aB3x9Z" and "Ab3X9z" are two links, also for the
unique index, and cleaning up one never removes the other's log.
"""

import string

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy

from apps.accounts.validators import USERNAME_MAX
from apps.storages.models import StorageSource

KEY_LENGTH = 8  # 62^8 keys: 6 characters can be guessed slowly from a large IPv6 range
KEY_MAX = 16
KEY_ALPHABET = string.ascii_letters + string.digits
TEXT_MAX = 2048  # characters of a User-Agent or Referer the log keeps


class KeyField(models.CharField):
    """Text compared case-sensitively everywhere: on MySQL the column gets the binary collation
    utf8mb4_bin (the site's own, utf8mb4_0900_ai_ci, ignores case); SQLite compares exactly anyway."""

    def db_parameters(self, connection):
        params = super().db_parameters(connection)
        if connection.vendor == "mysql":
            params["collation"] = "utf8mb4_bin"
        return params


class ShortLink(models.Model):
    key = KeyField(gettext_lazy("Short link key"), max_length=KEY_MAX, unique=True, editable=False)
    storage = models.ForeignKey(
        StorageSource, on_delete=models.CASCADE, related_name="short_links", verbose_name=gettext_lazy("Storage source"),
    )
    path = models.TextField(gettext_lazy("File path"), help_text=gettext_lazy("The file's full path in the storage source (including the creator's root folder)."))
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="short_links", verbose_name=gettext_lazy("Created by"),
    )
    # (rule id, revision) of the folder passwords the creator had unlocked for the file
    # (apps.storages.access unlocked_keys()), checked again when the link is opened (keys=).
    keys = models.JSONField(gettext_lazy("Folder passwords unlocked when it was created"), default=list, blank=True, editable=False)
    created_at = models.DateTimeField(gettext_lazy("Created at"), default=timezone.now, db_index=True)
    expires_at = models.DateTimeField(gettext_lazy("Expires at"), null=True, blank=True, db_index=True, help_text=gettext_lazy("Empty means it never expires."))

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Short link")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return self.key

    def expired(self, now=None) -> bool:
        return self.expires_at is not None and self.expires_at <= (now or timezone.now())


class DownloadLog(models.Model):
    class Kind(models.TextChoices):
        PATH = "path", gettext_lazy("Direct link")
        SHORT = "short", gettext_lazy("Short link")
        PAGE = "page", gettext_lazy("Page download")

    kind = models.CharField(gettext_lazy("Download type"), max_length=8, choices=Kind.choices)
    storage = models.ForeignKey(
        StorageSource, on_delete=models.CASCADE, related_name="download_logs", verbose_name=gettext_lazy("Storage source"),
    )
    path = models.TextField(gettext_lazy("File path"))
    short_key = KeyField(gettext_lazy("Short link key"), max_length=KEY_MAX, blank=True, db_index=True)  # "" for path links
    ip = models.GenericIPAddressField("IP", null=True, blank=True, unpack_ipv4=True)
    username = models.CharField(gettext_lazy("User"), max_length=USERNAME_MAX, blank=True)
    user_agent = models.CharField("User-Agent", max_length=TEXT_MAX, blank=True)
    referer = models.CharField("Referer", max_length=TEXT_MAX, blank=True)
    created_at = models.DateTimeField(gettext_lazy("Downloaded at"), default=timezone.now, db_index=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Download log")
        ordering = ["-created_at", "-id"]

    def __str__(self):
        return f"{self.get_kind_display()} {self.path}"
