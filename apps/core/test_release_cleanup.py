"""Release check: the cleanup (apps.core.cleanup) with every registered job at once, on a database with live
and expired things of every kind and of two users. Each job removes only what has expired (and isn't in use), a
second run nothing more; expired shares and short links stay, with their download logs, for people to delete.

The release check found that the cleanup kept expired Django sessions; the sessions job fixes it.
"""

import hashlib
import os
import shutil
import time
import uuid
from datetime import timedelta
from unittest import mock

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts import signals
from apps.accounts.models import User
from apps.core import cleanup
from apps.links.models import DownloadLog, ShortLink
from apps.office.models import OfficeSession
from apps.shares.models import Share
from apps.storages import config as storage_config
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import StorageSource
from apps.uploads import spool
from apps.uploads.models import Kind, State, UploadSession
from apps.uploads.tests.base import test_folder


class EveryJobTests(TestCase):
    def setUp(self):
        self.tmp = test_folder()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / "root").mkdir()
        patch = override_settings(UPLOAD_SPOOL_DIR=str(self.tmp / "spool"), BACKUP_DIR=self.tmp / "backups")
        patch.enable()
        self.addCleanup(patch.disable)
        self.now = timezone.now()
        self.storage = StorageSource.objects.create(
            key="disk", name="disk", type="local",
            config=storage_config.encode(LocalDriver, {"root": str(self.tmp / "root"), "allow_external_symlinks": False}))
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.bob = User.objects.create_user("bob", password="pw-bob-2026x")
        self.past, self.future = self.now - timedelta(seconds=5), self.now + timedelta(hours=1)

    # -- what there is

    def upload(self, user, *, expires, held=False, state=State.RECEIVING, spool_file=True):
        session = UploadSession.objects.create(
            storage=self.storage, user=user, path=f"/{uuid.uuid4().hex}.bin", size=10, kind=Kind.PROXY, state=state,
            updated_at=self.now, expires_at=expires, locked_until=self.future if held else None)
        if spool_file:
            spool.create(session.id)
        return session

    def share(self, user, expires):
        return Share.objects.create(key=uuid.uuid4().hex[:12], storage=self.storage, folder="/", entries=["a.txt"],
                                    owner=user, expires_at=expires)

    def short_link(self, user, expires):
        link = ShortLink.objects.create(key=uuid.uuid4().hex[:8], storage=self.storage, path="/a.txt", created_by=user,
                                        expires_at=expires)
        DownloadLog.objects.create(kind=DownloadLog.Kind.SHORT, storage=self.storage, path="/a.txt", short_key=link.key)
        return link

    def office(self, user, expires, saving=False):
        path = f"/{uuid.uuid4().hex}.docx"
        return OfficeSession.objects.create(key=uuid.uuid4().hex[:24], storage=self.storage, path=path,
                                            file=hashlib.sha256(path.encode()).hexdigest(), user=user, expires_at=expires,
                                            saving_until=self.future if saving else None)

    def cleanup(self):
        with self.captureOnCommitCallbacks(execute=True), mock.patch.object(cleanup.logger, "info"):
            return dict(cleanup.run())

    def test_the_jobs(self):
        self.assertEqual(sorted(job.name for job in cleanup.jobs()), ["backup_parts", "office", "sessions", "uploads"])

    def test_each_job_removes_only_what_expired(self):
        uploads_gone = [self.upload(self.alice, expires=self.past), self.upload(self.bob, expires=self.past),
                        # finished an hour ago, its spool file left (a removal that failed): both go
                        self.upload(self.alice, expires=self.past, state=State.DONE)]
        uploads_kept = [self.upload(self.alice, expires=self.future), self.upload(self.bob, expires=self.future),
                        self.upload(self.bob, expires=self.past, held=True)]  # a request is writing it right now
        shares = [self.share(self.alice, self.past), self.share(self.bob, self.now), self.share(self.alice, self.future),
                  self.share(self.bob, None)]
        links = [self.short_link(self.alice, self.past), self.short_link(self.bob, self.future),
                 self.short_link(self.alice, None)]
        DownloadLog.objects.create(kind=DownloadLog.Kind.PATH, storage=self.storage, path="/a.txt")  # a path link's
        office_gone = [self.office(self.alice, self.past)]
        office_kept = [self.office(self.bob, self.future), self.office(self.alice, self.past, saving=True)]
        (self.tmp / "backups").mkdir()
        part_gone, part_kept = self.tmp / "backups" / "hfile-1.sql.gz.part", self.tmp / "backups" / "hfile-2.sql.gz.part"
        for part in (part_gone, part_kept):
            part.write_bytes(b"x")
        two_days_ago = time.time() - 2 * 24 * 3600
        os.utime(part_gone, (two_days_ago, two_days_ago))  # a backup cut short; the other is being written

        first = self.cleanup()
        self.assertEqual(first, {"sessions": 0, "uploads": 3, "office": 1, "backup_parts": 1})
        self.assertEqual(set(UploadSession.objects.values_list("id", flat=True)), {s.id for s in uploads_kept})
        self.assertFalse(any(spool.path_of(s.id).exists() for s in uploads_gone))
        self.assertTrue(all(spool.path_of(s.id).exists() for s in uploads_kept))
        self.assertEqual(set(OfficeSession.objects.values_list("id", flat=True)), {s.id for s in office_kept})
        self.assertFalse(any(OfficeSession.objects.filter(pk=s.pk).exists() for s in office_gone))
        self.assertEqual((part_gone.exists(), part_kept.exists()), (False, True))
        # Expired shares and short links stay, shown as expired, and so do the short links' download logs: only
        # their owners and the admins delete them.
        self.assertEqual(set(Share.objects.values_list("id", flat=True)), {s.id for s in shares})
        self.assertEqual(set(ShortLink.objects.values_list("id", flat=True)), {s.id for s in links})
        self.assertEqual(sorted(DownloadLog.objects.values_list("short_key", flat=True)),
                         sorted(["", *(link.key for link in links)]))

        second = self.cleanup()
        self.assertEqual(second, {"sessions": 0, "uploads": 0, "office": 0, "backup_parts": 0})

    # Django keeps ended sessions in django_session until something deletes them: the sessions job does, also
    # those of visitors who never log in (guest uploads, folder passwords, single sign-on starts all make sessions).
    def test_expired_django_sessions(self):
        store = SessionStore()
        store["hfile.uploads.owner"] = "x"
        store.create()
        Session.objects.filter(session_key=store.session_key).update(expire_date=self.past)
        live = SessionStore()
        live["hfile.uploads.owner"] = "y"
        live.create()
        self.assertEqual(self.cleanup()["sessions"], 1)
        self.assertEqual(list(Session.objects.values_list("session_key", flat=True)), [live.session_key])

    def test_a_great_many_sessions(self):
        """At most SESSIONS_PER_RUN in a run, the rest the next time: on SQLite its transaction holds the write lock."""
        Session.objects.bulk_create(Session(session_key=f"{n:040d}", session_data="", expire_date=self.past) for n in range(7))
        with mock.patch.object(signals, "SESSIONS_PER_RUN", 5), mock.patch.object(signals, "SESSIONS_BATCH", 2):
            self.assertEqual(self.cleanup()["sessions"], 5)
            self.assertEqual(self.cleanup()["sessions"], 2)
        self.assertFalse(Session.objects.exists())
