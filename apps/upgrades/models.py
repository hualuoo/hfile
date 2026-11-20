from django.db import models
from django.utils.translation import gettext_lazy


class UpgradeLog(models.Model):
    """A version of the program starting on this database for the first time (apps/upgrades/service.py).

    Written once its migrations, if it had any, have gone through, so a failed upgrade leaves no
    row (the database is restored to before it).
    """

    version = models.CharField(gettext_lazy("Version"), max_length=40)
    previous = models.CharField(gettext_lazy("Previous version"), max_length=40, blank=True)  # "": no earlier record
    migrations = models.JSONField(gettext_lazy("Migrations applied"), default=list, blank=True)  # ["accounts.0002_x", ...]
    backup = models.TextField(gettext_lazy("Backup before upgrade"), blank=True)  # the file's path; "" when nothing changed
    started_at = models.DateTimeField(gettext_lazy("Started at"))
    finished_at = models.DateTimeField(gettext_lazy("Finished at"))

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Upgrade history")
        ordering = ["-id"]

    def __str__(self):
        return f"{self.previous or '?'} → {self.version}"
