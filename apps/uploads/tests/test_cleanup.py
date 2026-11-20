"""Expired upload sessions and their spool files go with `hfile cleanup` (uploads' job in apps.core.cleanup);
the spool is never served."""

import io
import os
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.checks import run_checks
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from apps.core import cleanup
from apps.core.testing import assert_private
from apps.storages import config as storage_config
from apps.storages.drivers.local import LocalDriver
from apps.uploads import service, spool
from apps.uploads.models import State, UploadSession
from apps.uploads.service import _light_cleanup as real_light_cleanup  # before the base test case replaces it

from .base import UploadTestCase


class CleanupTests(UploadTestCase):
    def session(self, name, size=100, **fields):
        made = self.create(name, size)
        self.assertEqual(made.status_code, 201, made.content)
        UploadSession.objects.filter(pk=made.json()["id"]).update(**fields)
        return UploadSession.objects.get(pk=made.json()["id"])

    def test_expired_sessions_and_their_files(self):
        past = timezone.now() - timedelta(seconds=1)
        old = self.session("old.txt", expires_at=past)
        done = self.session("done.txt", state=State.DONE, expires_at=past)
        fresh = self.session("fresh.txt")
        finishing = self.session("busy.txt", state=State.FINISHING, expires_at=past,
                                 locked_until=timezone.now() + timedelta(minutes=1))
        writing = self.session("chunk.txt", expires_at=past, locked_until=timezone.now() + timedelta(minutes=1))
        with self.captureOnCommitCallbacks(execute=True):
            removed = service.remove_expired(timezone.now())
        self.assertEqual(removed, 2)
        self.assertEqual(set(UploadSession.objects.values_list("id", flat=True)), {fresh.id, finishing.id, writing.id})
        self.assertTrue(spool.path_of(writing.id).exists())
        self.assertFalse(spool.path_of(old.id).exists())
        self.assertFalse(spool.path_of(done.id).exists())
        self.assertTrue(spool.path_of(fresh.id).exists())
        self.assertTrue(spool.path_of(finishing.id).exists())

    def test_spool_files_without_a_session(self):
        orphan = UploadSession(id="12345678-1234-4234-8234-123456789abc").id
        spool.create(orphan)
        recent = UploadSession(id="22345678-1234-4234-8234-123456789abc").id
        spool.create(recent)
        hour_ago = time.time() - service.ORPHAN_AGE - 5
        os.utime(spool.path_of(orphan), (hour_ago, hour_ago))
        (self.spool / "notes.txt").write_text("not ours")
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(service.remove_expired(timezone.now()), 1)
        self.assertFalse(spool.path_of(orphan).exists())
        self.assertTrue(spool.path_of(recent).exists())  # perhaps a session being made right now
        self.assertTrue((self.spool / "notes.txt").exists())

    def test_the_command(self):
        past = timezone.now() - timedelta(seconds=1)
        old = self.session("old.txt", expires_at=past)
        out, err = io.StringIO(), io.StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("cleanup", "--dry-run", stdout=out, stderr=err)
        self.assertIn("过期的上传会话：可以删除 1 个", out.getvalue())
        self.assertIn("试运行", out.getvalue())
        self.assertTrue(UploadSession.objects.filter(pk=old.id).exists())
        self.assertTrue(spool.path_of(old.id).exists())
        out = io.StringIO()
        with self.captureOnCommitCallbacks(execute=True):
            call_command("cleanup", "--only", "uploads", stdout=out, stderr=err)
        self.assertIn("过期的上传会话：删除了 1 个", out.getvalue())
        self.assertFalse(UploadSession.objects.filter(pk=old.id).exists())
        self.assertFalse(spool.path_of(old.id).exists())
        self.assertEqual(err.getvalue(), "")

    def test_the_job_is_registered(self):
        self.assertIn(("uploads", "过期的上传会话"), [(job.name, job.label) for job in cleanup.jobs()])

    def test_the_light_cleanup_while_sessions_are_made(self):
        """The base test case turns it off; the real one runs once per CLEANUP_EVERY seconds per process."""
        past = timezone.now() - timedelta(seconds=1)
        old = self.session("old.txt", expires_at=past)
        with mock.patch.object(service, "_cleaned_at", None):
            real_light_cleanup()
            self.assertFalse(UploadSession.objects.filter(pk=old.id).exists())
            again = self.session("again.txt", expires_at=past)
            real_light_cleanup()
            self.assertTrue(UploadSession.objects.filter(pk=again.id).exists())  # not again so soon


class SpoolTests(UploadTestCase):
    def test_not_under_the_served_uploads(self):
        media = Path(settings.MEDIA_ROOT).resolve()
        default = Path(settings.CONFIG_FILE).resolve().parent / "upload-spool"
        self.assertFalse(default.is_relative_to(media))
        session = self.create("a.txt", 10).json()
        name = spool.path_of(session["id"]).name
        with override_settings(MEDIA_ROOT=str(self.tmp / "media")):
            self.assertNotIn("uploads.W001", [check.id for check in run_checks()])
        # [paths] uploads set to a folder around it: a warning; /media/ still serves only names an app gave.
        with override_settings(MEDIA_ROOT=str(self.tmp)):
            self.assertIn("uploads.W001", [check.id for check in run_checks()])
            self.assertEqual(self.client.get(f"/media/spool/{name}").status_code, 404)
        self.assertEqual(self.client.get(f"/media/../spool/{name}").status_code, 404)

    def test_a_local_storage_cannot_serve_the_spool(self):
        self.create("a.txt", 10)
        problems = storage_config.site_problems("local", {"root": str(self.spool)})
        self.assertIn("root", problems)
        self.assertIn("上传到一半的文件", problems["root"])
        self.assertEqual(storage_config.site_problems("local", {"root": str(self.root)}), {})

    def test_modes(self):
        session = self.create("a.txt", 10).json()
        assert_private(self, self.spool, 0o700)
        assert_private(self, spool.path_of(session["id"]))

    def test_a_spool_that_cannot_be_made(self):
        blocker = self.tmp / "blocker"
        blocker.write_text("a file where the folder should be")
        with override_settings(UPLOAD_SPOOL_DIR=str(blocker / "spool")), self.assertLogs("hfile.uploads", "WARNING") as logged:
            body = self.assertCode(self.create("a.txt", 10), 500, "spool")
        self.assertFalse(UploadSession.objects.exists())
        # SEC-02: where the spool is, and what the system said, only in the log.
        self.assertEqual(body["detail"], "上传的暂存文件夹建不起来，请管理员看看网站的日志。")
        self.assertIn(str(blocker / "spool"), "\n".join(logged.output))

    def test_local_storage_settings_are_untouched(self):
        # The encoded config keeps working with the test's own root (a sanity check of the base).
        self.assertEqual(storage_config.decode(LocalDriver, self.storage.config)[0]["root"], str(self.root))
