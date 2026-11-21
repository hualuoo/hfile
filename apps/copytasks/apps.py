from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class CopyTasksConfig(AppConfig):
    """复制任务 (/manage/#copy-tasks): admins copy a folder of one storage source into a folder of another one, in
    the background, without the files going to the server's disk (apps/copytasks/runner.py)."""

    name = "apps.copytasks"
    verbose_name = gettext_lazy("Copy tasks")
