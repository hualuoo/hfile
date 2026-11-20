"""Upload sessions: one per file being uploaded (decision D16).

A proxied upload (local, FTP, SFTP, WebDAV, Google Drive, 115, and the others with 代理上传) sends its
bytes to the site in chunks below the request limit; they wait in a spool file (apps/uploads/spool.py) until
the last one has come, then the site streams the file into the driver. A direct upload (S3 family,
OneDrive and SharePoint, 又拍云) sends them to the backend; the session then only remembers what was
checked when it was made, until the browser says it is done and the site looks at the file.

The session belongs to the user who made it, and for the guest also to the browser's Django session
(`owner`), so one visitor can't write into another's upload. Every request on it checks the owner and
the permissions again (apps/uploads/service.py).
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy

from apps.storages.models import StorageSource


class Kind(models.TextChoices):
    PROXY = "proxy", gettext_lazy("Through the site")
    DIRECT = "direct", gettext_lazy("Direct from the browser")


class State(models.TextChoices):
    RECEIVING = "receiving", gettext_lazy("Receiving")  # proxied: chunks arriving
    WAITING = "waiting", gettext_lazy("Waiting for the direct upload")  # direct: the browser sends the bytes to the backend
    FINISHING = "finishing", gettext_lazy("Writing to storage")  # the site writes the spool into the driver, or checks the file
    FAILED = "failed", gettext_lazy("Failed")  # the last attempt to finish failed; it can be tried again until it expires
    DONE = "done", gettext_lazy("Done")


class UploadSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    storage = models.ForeignKey(StorageSource, on_delete=models.CASCADE, related_name="upload_sessions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_sessions")
    # For the guest: the SHA-256 of a random token kept in the browser's Django session ("" for users).
    owner = models.CharField(max_length=64, blank=True)
    # The guest's address as the rate limits count it (an IPv6 /64): a visitor who drops his cookies
    # gets a new owner, not a new address ("" for users).
    address = models.CharField(max_length=64, blank=True)
    # The file's storage path, and the folders that didn't exist when the session was made (the upload
    # creates them): the permissions are checked again with them on every request.
    path = models.TextField()
    created_folders = models.JSONField(default=list, blank=True)
    # (rule id, revision) of the folder passwords the maker had unlocked, or sent, for this upload.
    keys = models.JSONField(default=list, blank=True)
    size = models.PositiveBigIntegerField()
    received = models.PositiveBigIntegerField(default=0)
    kind = models.CharField(max_length=10, choices=Kind.choices)
    state = models.CharField(max_length=12, choices=State.choices)
    overwrite = models.BooleanField(default=False)
    # Taken by the request writing a chunk or finishing, until this moment (several processes, no long
    # transactions): another one waits or answers "busy".
    locked_until = models.DateTimeField(null=True, blank=True)
    error_code = models.CharField(max_length=40, blank=True)
    error = models.CharField(max_length=500, blank=True)
    item = models.JSONField(null=True, blank=True)  # the stored file, once done: name, size, modified
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField()  # the last request on it (not auto_now: updates are conditional)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Upload session")
        indexes = [models.Index(fields=["user", "updated_at"], name="uploads_session_user_recent")]  # the limits

    def __str__(self):
        return f"{self.id} {self.path} ({self.state})"
