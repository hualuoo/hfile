"""The 操作日志: what people did on the site, when and from which IP. Rows are only ever added.

Written by apps.activity.log (record() and record_many()). Names are copied in (the user's, the storage
source's), so an entry still reads right after the user or the storage source is deleted; only the link
to it goes. `detail` holds what the entry's kind needs, as codes and plain values; apps/activity/describe.py
turns it into text in the reader's language.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy, pgettext_lazy

from apps.accounts.validators import USERNAME_MAX
from apps.ipgeo.sources import LOCATION_MAX
from apps.storages.models import NAME_MAX


class ActivityLog(models.Model):
    """One thing somebody did. Which fields an entry has depends on its kind (apps/activity/log.py):

    - actor: who did it: a user (the built-in guest for visitors who weren't logged in); None for a failed
      login, and for what a command on the server did (`hfile resetadmin`, the installer has the admin);
    - target: the user it was about (a user an admin changed; the account a failed login was for, or
      what was typed when no account has that name);
    - storage and path: a storage source, and a file or folder in it (its full path there); to_path
      where it went (renamed, moved, copied);
    - key: a share's or a short link's key.
    """

    class Kind(models.TextChoices):
        # 账号
        LOGIN = "login", gettext_lazy("Login")
        LOGIN_FAILED = "login_failed", gettext_lazy("Failed login")
        CHANGE_PASSWORD = "change_password", gettext_lazy("Password change")
        TOTP_ON = "totp_on", gettext_lazy("Two-step verification on")
        TOTP_OFF = "totp_off", gettext_lazy("Two-step verification off")
        LINK_SSO = "link_sso", gettext_lazy("Single sign-on linked")
        UNLINK_SSO = "unlink_sso", gettext_lazy("Single sign-on unlinked")
        SSO_REGISTER = "sso_register", gettext_lazy("Registered by single sign-on")
        # 文件
        MKDIR = "mkdir", gettext_lazy("New folder")
        RENAME = "rename", gettext_lazy("Rename")
        MOVE = "move", gettext_lazy("Move")
        COPY = "copy", gettext_lazy("Copy")
        DELETE = "delete", gettext_lazy("Delete")
        UPLOAD = "upload", gettext_lazy("Upload")
        EDIT = "edit", gettext_lazy("Online edit")
        # 链接和分享
        SHORT_LINK = "short_link", gettext_lazy("Short link made")
        CREATE_SHARE = "create_share", gettext_lazy("Share created")
        DELETE_SHARE = "delete_share", pgettext_lazy("activity log", "Share deleted")  # not the toast's 分享已删除
        CLEAR_SHARES = "clear_shares", gettext_lazy("Expired shares cleared")
        # 用户管理
        CREATE_USER = "create_user", gettext_lazy("User created")
        UPDATE_USER = "update_user", gettext_lazy("User changed")
        COPY_USER = "copy_user", gettext_lazy("User copied")
        DELETE_USER = "delete_user", gettext_lazy("User deleted")
        SET_GRANTS = "set_grants", gettext_lazy("Storage source permissions changed")
        LINK_IDENTITY = "link_identity", gettext_lazy("Single sign-on linked for a user")
        UNLINK_IDENTITY = "unlink_identity", gettext_lazy("Single sign-on unlinked for a user")
        # 存储源管理
        CREATE_STORAGE = "create_storage", gettext_lazy("Storage source created")
        UPDATE_STORAGE = "update_storage", gettext_lazy("Storage source changed")
        DELETE_STORAGE = "delete_storage", gettext_lazy("Storage source deleted")
        ENABLE_STORAGE = "enable_storage", gettext_lazy("Storage source enabled")
        DISABLE_STORAGE = "disable_storage", gettext_lazy("Storage source disabled")
        COPY_STORAGE = "copy_storage", gettext_lazy("Storage source copied")
        SORT_STORAGES = "sort_storages", gettext_lazy("Storage sources reordered")
        ROTATE_LINK_SECRET = "rotate_link_secret", gettext_lazy("Link secret replaced")
        UPDATE_FILTERS = "update_filters", gettext_lazy("Filter rules changed")
        UPDATE_PASSWORDS = "update_passwords", gettext_lazy("Folder passwords changed")
        UPDATE_READMES = "update_readmes", gettext_lazy("Folder readmes changed")
        # 网站管理
        UPDATE_SETTINGS = "update_settings", gettext_lazy("Settings changed")
        CREATE_PROVIDER = "create_provider", gettext_lazy("Single sign-on added")
        UPDATE_PROVIDER = "update_provider", gettext_lazy("Single sign-on changed")
        DELETE_PROVIDER = "delete_provider", gettext_lazy("Single sign-on deleted")
        SORT_PROVIDERS = "sort_providers", gettext_lazy("Single sign-on reordered")
        DELETE_SHORT_LINKS = "delete_short_links", gettext_lazy("Short links deleted")
        DELETE_DOWNLOAD_LOGS = "delete_download_logs", gettext_lazy("Download log entries deleted")
        UPDATE_IP_GEO = "update_ip_geo", gettext_lazy("IP location turned on or off")
        DOWNLOAD_IP_GEO = "download_ip_geo", gettext_lazy("IP location database downloaded")
        FILL_IP_GEO = "fill_ip_geo", gettext_lazy("IP locations filled in")
        INSTALL = "install", gettext_lazy("Installed")
        RESET_ADMIN = "reset_admin", gettext_lazy("Superuser reset (resetadmin)")

    created_at = models.DateTimeField(gettext_lazy("Time"), default=timezone.now, db_index=True)
    kind = models.CharField(gettext_lazy("Action"), max_length=32, choices=Kind.choices)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                              related_name="+", verbose_name=gettext_lazy("User"))
    actor_name = models.CharField(gettext_lazy("Username"), max_length=USERNAME_MAX, blank=True)
    target = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name=gettext_lazy("User it was about"))
    target_name = models.CharField(gettext_lazy("Username it was about"), max_length=USERNAME_MAX, blank=True)
    storage = models.ForeignKey("storages.StorageSource", null=True, blank=True, on_delete=models.SET_NULL,
                                related_name="+", verbose_name=gettext_lazy("Storage source"))
    storage_name = models.CharField(gettext_lazy("Storage source name"), max_length=NAME_MAX, blank=True)
    path = models.TextField(gettext_lazy("Path"), blank=True)
    to_path = models.TextField(gettext_lazy("New path"), blank=True)
    key = models.CharField("Key", max_length=32, blank=True)
    detail = models.JSONField(gettext_lazy("Details"), default=dict, blank=True)
    # None when it didn't come from a browser's request: a command on the server, OnlyOffice saving.
    ip = models.GenericIPAddressField("IP", null=True, blank=True, unpack_ipv4=True)
    # Its IP 归属地 (ipgeo app): looked up when the entry is written while that is turned on, or filled in
    # later (补全 on /manage/#ip-geo). The only thing about an entry that ever changes.
    ip_location = models.CharField(gettext_lazy("IP location"), max_length=LOCATION_MAX, blank=True)
    user_agent = models.CharField(gettext_lazy("Browser"), max_length=500, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Activity log")
        indexes = [
            models.Index(fields=["kind", "created_at"], name="activity_kind_time"),
            models.Index(fields=["ip"], name="activity_ip"),
        ]

    def __str__(self):
        return f"{self.actor_name or '-'} {self.get_kind_display()}"
