from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class ActivityConfig(AppConfig):
    """The 操作日志 (/manage/#activity): logins, changes to files, links and shares, and every change admins
    make. Downloads are the download log's (apps/links)."""

    name = "apps.activity"
    verbose_name = gettext_lazy("Activity log")
