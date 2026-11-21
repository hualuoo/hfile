"""OnlyOffice editor sessions (decision D24): a file's document key, who saves it, and until when.

The Document Server knows a document by its key: everybody who opens a file with the same key shares
one editing session there (co-editing), and a key it has seen may be served from its cache. So there
is one current editing session per physical file (storage, path), shared by its editors, and one
current viewing session, shared by those who may only view it; a file that changed gets a new key:
the receiver of apps.files.signals.changed (W5) drops the sessions at and below the changed paths, and a
file found changed outside the site (its size or time) gets a new one when it is opened again.

The row says who saves when the Document Server calls back (apps/office/callback.py): the last authorised
editor that opened it (`user`, `can_edit`), with the folder passwords that user had unlocked for the
file (`keys`, as a short link keeps them). Sessions live in the database, so open editors survive a
restart of the site; the cleanup (apps.core.cleanup) removes expired ones.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy

from apps.storages.models import StorageSource


class OfficeSession(models.Model):
    key = models.CharField(gettext_lazy("Document key"), max_length=32, unique=True)
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="office_sessions",
                                verbose_name=gettext_lazy("Storage source"))
    # The storage's config_revision when the key was made: once an admin saves new params (another root,
    # bucket or base path), the same path is another file, and the session ends (apps/office/sessions.py).
    storage_revision = models.PositiveIntegerField(default=0, editable=False)
    path = models.TextField(gettext_lazy("Path"))  # the storage path of the file
    # SHA-256 of the path, in hex: what the site compares paths by. MySQL's collation compares text
    # without case, and two files that differ only in case are two documents.
    file = models.CharField(max_length=64, editable=False)
    # The file's current editing session holds its `file` here, its viewing session another hash of the path
    # (apps.office.sessions.slot()); NULL once it is being closed (the Document Server's final save, status 2):
    # a new open of the file then gets a new key. Unique per storage.
    current = models.CharField(max_length=64, null=True, blank=True, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="office_sessions",
                             verbose_name=gettext_lazy("User for saving"))
    can_edit = models.BooleanField(gettext_lazy("Can edit"), default=False)  # False: the viewing session
    # (rule id, revision) of the folder passwords `user` had unlocked for the file, checked again at saving.
    keys = models.JSONField(default=list, blank=True)
    # The file as it was when the key was made (or last saved through it): bytes, and its time in whole
    # seconds; None where the storage didn't say.
    file_size = models.BigIntegerField(null=True, blank=True)
    file_time = models.BigIntegerField(null=True, blank=True)
    # Somebody has it open for editing at the Document Server (its callbacks: 1 opens, 4 closes).
    in_use = models.BooleanField(default=False)
    created_at = models.DateTimeField(gettext_lazy("Created at"), default=timezone.now)
    expires_at = models.DateTimeField(gettext_lazy("Expires at"), db_index=True)
    # A save is under way until then (apps/office/callback.py): one save of a file at a time.
    saving_until = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("OnlyOffice editing session")
        constraints = [
            models.UniqueConstraint(fields=["storage", "current"], name="office_session_one_per_file"),
        ]
        indexes = [models.Index(fields=["storage", "file"], name="office_session_file")]

    def __str__(self):
        return f"{self.key} {self.path}"
