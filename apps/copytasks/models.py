"""Copy tasks (/manage/#copy-tasks): an admin copies a folder of one storage source into a folder of another
one, in the background. apps/copytasks/runner.py does the copying and writes its progress into the task's
row; the files it couldn't copy are CopyFailure rows.

A task keeps the names of its storage sources and of who made it, as the activity log does: it still reads
right after they are deleted. A run is one go at the copying: the first when the task is made, another for
each 继续 (restart_task: the files already there with the same name and size are skipped). `run_id` says
which run may write the row; a run that finds another id there stops without writing.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy, pgettext_lazy

from apps.accounts.validators import USERNAME_MAX
from apps.storages.models import NAME_MAX

MESSAGE_MAX = 500
THREADS_MAX = 16
RETRIES_MAX = 10
# Bytes a second: 10 GB/s is above what one server sends.
SPEED_LIMIT_MAX = 10 * 1024 ** 3


class CopyTask(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", gettext_lazy("Copying")
        STOPPING = "stopping", gettext_lazy("Stopping")
        DONE = "done", pgettext_lazy("copy task", "Done")
        FAILED = "failed", pgettext_lazy("copy task", "Failed")
        STOPPED = "stopped", pgettext_lazy("copy task", "Stopped")

    class Policy(models.TextChoices):
        SKIP = "skip", gettext_lazy("Skip")
        OVERWRITE = "overwrite", gettext_lazy("Overwrite")

    created_at = models.DateTimeField(gettext_lazy("Created at"), default=timezone.now)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
                                   related_name="+", verbose_name=gettext_lazy("Created by"))
    created_by_name = models.CharField(gettext_lazy("Username"), max_length=USERNAME_MAX, blank=True)

    source = models.ForeignKey("storages.StorageSource", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name=gettext_lazy("Source storage source"))
    source_name = models.CharField(gettext_lazy("Source storage source name"), max_length=NAME_MAX)
    source_path = models.TextField(gettext_lazy("Source folder"))
    target = models.ForeignKey("storages.StorageSource", null=True, blank=True, on_delete=models.SET_NULL,
                               related_name="+", verbose_name=gettext_lazy("Target storage source"))
    target_name = models.CharField(gettext_lazy("Target storage source name"), max_length=NAME_MAX)
    target_path = models.TextField(gettext_lazy("Target folder"))

    # How it copies.
    threads = models.PositiveSmallIntegerField(gettext_lazy("Threads"), default=4)  # files at once
    speed_limit = models.PositiveBigIntegerField(gettext_lazy("Speed limit"), default=0)  # bytes a second, all
    # threads together; 0: none
    # A file of the same name in the target folder: with the same size too (same_file), or another one (same_name).
    same_file = models.CharField(gettext_lazy("Same file"), max_length=10, choices=Policy.choices, default=Policy.SKIP)
    same_name = models.CharField(gettext_lazy("Same name"), max_length=10, choices=Policy.choices, default=Policy.OVERWRITE)
    retries = models.PositiveSmallIntegerField(gettext_lazy("Retries"), default=3)  # of a file that failed

    # The run: its state, and what it has done so far.
    status = models.CharField(gettext_lazy("Status"), max_length=10, choices=Status.choices, default=Status.RUNNING)
    # Why it failed or stopped, in the language of the admin who started the run.
    message = models.CharField(gettext_lazy("Result"), max_length=MESSAGE_MAX, blank=True)
    run = models.PositiveIntegerField(default=1)
    run_id = models.CharField(max_length=32)
    started_at = models.DateTimeField(gettext_lazy("Started at"), default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)  # the run's last write: a run that stops writing has died
    finished_at = models.DateTimeField(gettext_lazy("Finished at"), null=True, blank=True)
    scanning = models.BooleanField(default=True)  # still listing the source folder: more files may come
    files_total = models.PositiveBigIntegerField(default=0)  # found so far
    files_copied = models.PositiveBigIntegerField(default=0)
    files_skipped = models.PositiveBigIntegerField(default=0)
    files_failed = models.PositiveBigIntegerField(default=0)
    folders_failed = models.PositiveBigIntegerField(default=0)  # couldn't be listed or made: what's in them is missing
    bytes_total = models.PositiveBigIntegerField(default=0)
    bytes_copied = models.PositiveBigIntegerField(default=0)
    bytes_skipped = models.PositiveBigIntegerField(default=0)
    bytes_done = models.PositiveBigIntegerField(default=0)  # of the files done (copied, skipped, failed) and under way
    transferred = models.PositiveBigIntegerField(default=0)  # read from the source, a file read twice twice
    speed = models.PositiveBigIntegerField(default=0)  # bytes a second over the last seconds
    # The files being copied now: [{"path", "size", "done", "speed", "attempt", "error"}].
    current = models.JSONField(default=list, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Copy tasks")
        ordering = ["-id"]

    def __str__(self):
        return f"{self.source_name}:{self.source_path} → {self.target_name}:{self.target_path}"


class CopyFailure(models.Model):
    """A file the task couldn't copy, or a folder it couldn't list or make (nothing in it was copied)."""

    task = models.ForeignKey(CopyTask, on_delete=models.CASCADE, related_name="failures")
    path = models.TextField(gettext_lazy("Path"))  # in the source folder: "/photos/a.jpg"
    is_dir = models.BooleanField(default=False)
    size = models.PositiveBigIntegerField(null=True, blank=True)
    message = models.CharField(gettext_lazy("Error"), max_length=MESSAGE_MAX)
    attempts = models.PositiveSmallIntegerField(default=1)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Copy failures")
        ordering = ["id"]
