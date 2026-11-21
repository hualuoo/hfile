from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class UpgradesConfig(AppConfig):
    """Upgrading the database when a new version of the program starts, and backups."""

    name = "apps.upgrades"
    verbose_name = gettext_lazy("Upgrades")

    def ready(self):
        from apps.core import cleanup

        from .backup import remove_parts

        cleanup.register("backup_parts", remove_parts)
