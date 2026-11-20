"""Release check of 0.1.0: `hfile cleanup` with every registered job at once, on a database with live and
expired things of every kind and of two users. Each job removes only what has expired (and isn't in use),
a dry run removes nothing, a second run nothing more.

The release check found that `hfile cleanup` kept expired Django sessions; the sessions job fixes it.
"""

import hashlib
import io
import os
import shutil
import time
import uuid
from datetime import timedelta

from django.contrib.sessions.backends.db import SessionStore
from django.contrib.sessions.models import Session
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts.models import User
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

    def cleanup(self, *args):
        out = io.StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("cleanup", *args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_each_job_removes_only_what_expired(self):
        uploads_gone = [self.upload(self.alice, expires=self.past), self.upload(self.bob, expires=self.past),
                        # finished an hour ago, its spool file left (a removal that failed): both go
                        self.upload(self.alice, expires=self.past, state=State.DONE)]
        uploads_kept = [self.upload(self.alice, expires=self.future), self.upload(self.bob, expires=self.future),
                        self.upload(self.bob, expires=self.past, held=True)]  # a request is writing it right now
        shares_gone = [self.share(self.alice, self.past), self.share(self.bob, self.now)]
        shares_kept = [self.share(self.alice, self.future), self.share(self.bob, None)]
        links_gone = [self.short_link(self.alice, self.past)]
        links_kept = [self.short_link(self.bob, self.future), self.short_link(self.alice, None)]
        DownloadLog.objects.create(kind=DownloadLog.Kind.PATH, storage=self.storage, path="/a.txt")  # a path link's
        office_gone = [self.office(self.alice, self.past)]
        office_kept = [self.office(self.bob, self.future), self.office(self.alice, self.past, saving=True)]
        (self.tmp / "backups").mkdir()
        part_gone, part_kept = self.tmp / "backups" / "hfile-1.sql.gz.part", self.tmp / "backups" / "hfile-2.sql.gz.part"
        for part in (part_gone, part_kept):
            part.write_bytes(b"x")
        two_days_ago = time.time() - 2 * 24 * 3600
        os.utime(part_gone, (two_days_ago, two_days_ago))  # a backup cut short; the other is being written

        dry = self.cleanup("--dry-run")
        self.assertIn("过期的上传会话：可以删除 3 个", dry)
        self.assertEqual(UploadSession.objects.count(), 6)
        self.assertEqual(sum(spool.path_of(s.id).exists() for s in [*uploads_gone, *uploads_kept]), 6)

        first = self.cleanup()
        for line in ("过期的上传会话：删除了 3 个", "过期的分享：删除了 2 个", "过期的短链（连同它们的下载日志）：删除了 1 个",
                     "过期的 OnlyOffice 编辑会话：删除了 1 个", "备份被打断时留下的 .part 文件：删除了 1 个"):
            self.assertIn(line, first)
        self.assertEqual(set(UploadSession.objects.values_list("id", flat=True)), {s.id for s in uploads_kept})
        self.assertFalse(any(spool.path_of(s.id).exists() for s in uploads_gone))
        self.assertTrue(all(spool.path_of(s.id).exists() for s in uploads_kept))
        self.assertEqual(set(Share.objects.values_list("id", flat=True)), {s.id for s in shares_kept})
        self.assertEqual(set(ShortLink.objects.values_list("id", flat=True)), {s.id for s in links_kept})
        self.assertEqual(sorted(DownloadLog.objects.values_list("short_key", flat=True)),
                         sorted(["", *(link.key for link in links_kept)]))
        self.assertEqual(set(OfficeSession.objects.values_list("id", flat=True)), {s.id for s in office_kept})
        self.assertFalse(any(UploadSession.objects.filter(pk=s.pk).exists() for s in uploads_gone))
        self.assertFalse(any(OfficeSession.objects.filter(pk=s.pk).exists() for s in office_gone))
        self.assertFalse(any(Share.objects.filter(pk=s.pk).exists() for s in shares_gone))
        self.assertFalse(any(ShortLink.objects.filter(pk=s.pk).exists() for s in links_gone))
        self.assertEqual((part_gone.exists(), part_kept.exists()), (False, True))

        second = self.cleanup()
        self.assertEqual(second.count("删除了 0 个"), 6, second)  # with the sessions

    # Django keeps ended sessions in django_session until something deletes them. Only a login does
    # (apps/accounts/signals.py clear_expired_sessions, at most once an hour); `hfile cleanup` has no job for
    # them, so a site used without logging in (guest uploads, folder passwords, single sign-on starts all
    # make sessions) keeps every one of them.
    def test_expired_django_sessions(self):
        store = SessionStore()
        store["hfile.uploads.owner"] = "x"
        store.create()
        Session.objects.filter(session_key=store.session_key).update(expire_date=self.past)
        self.cleanup()
        self.assertFalse(Session.objects.filter(session_key=store.session_key).exists())
