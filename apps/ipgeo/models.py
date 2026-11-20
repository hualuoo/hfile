from django.db import models
from django.utils.translation import gettext_lazy, pgettext_lazy

from .sources import LOCATION_MAX


class IpGeoSettings(models.Model):
    """IP 归属地 (/manage/#ip-geo). A single row (pk=1); use IpGeoSettings.load().

    The database file itself is in [paths] geoip (settings.GEOIP_DIR). This row says which download is in
    use, and how the last job went: a download or a 补全, run in the background by apps/ipgeo/jobs.py,
    which writes its progress here while it runs.
    """

    class Job(models.TextChoices):
        DOWNLOAD = "download", gettext_lazy("Downloading the database")
        FILL = "fill", gettext_lazy("Filling in locations")

    class JobState(models.TextChoices):
        RUNNING = "running", gettext_lazy("Running")
        DONE = "done", gettext_lazy("Done")
        FAILED = "failed", gettext_lazy("Failed")

    enabled = models.BooleanField(gettext_lazy("Enabled"), default=False)
    # The database in use (apps.ipgeo.sources); the file name is "" until the first download.
    file_name = models.CharField(gettext_lazy("File name"), max_length=100, blank=True)
    sha256 = models.CharField("SHA-256", max_length=64, blank=True)
    size = models.PositiveBigIntegerField(gettext_lazy("File size"), null=True, blank=True)
    built_at = models.DateTimeField(gettext_lazy("Data date"), null=True, blank=True)
    downloaded_at = models.DateTimeField(gettext_lazy("Downloaded at"), null=True, blank=True)
    # The last job: done and total are bytes for a download, addresses for a 补全.
    job_id = models.CharField(max_length=32, blank=True)
    job = models.CharField(gettext_lazy("Job"), max_length=10, choices=Job.choices, blank=True)
    job_state = models.CharField(gettext_lazy("Job state"), max_length=10, choices=JobState.choices, blank=True)
    job_done = models.PositiveBigIntegerField(default=0)
    job_total = models.PositiveBigIntegerField(default=0)
    job_message = models.CharField(gettext_lazy("Job result"), max_length=300, blank=True)
    job_started_at = models.DateTimeField(null=True, blank=True)
    job_updated_at = models.DateTimeField(null=True, blank=True)
    job_finished_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("IP location settings")

    def __str__(self):
        return str(pgettext_lazy("IP location", "Turned on") if self.enabled else pgettext_lazy("IP location", "Turned off"))

    @classmethod
    def load(cls):
        return cls.objects.get_or_create(pk=1)[0]


class PlaceName(models.Model):
    """The names of a place of the stored locations, by its code there (apps/ipgeo/places.py): a country's
    ISO code, a province's or city's GeoNames ID. {"en": ..., "zh-CN": ...}, as the database's records had
    them when a lookup met the place (apps.ipgeo.lookup.remember); the pages show the one of their language.
    Kept when the database is updated: records looked up in the old one keep their codes."""

    code = models.CharField(gettext_lazy("Code"), max_length=LOCATION_MAX, primary_key=True)
    names = models.JSONField(gettext_lazy("Names"), default=dict)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Place names")

    def __str__(self):
        return self.code
