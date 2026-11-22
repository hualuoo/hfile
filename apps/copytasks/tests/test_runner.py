"""Copy tasks from end to end (apps/copytasks/runner.py and service.py) between local storages on folders of their
own, and the variants of drivers.py. Runs happen in place (start_thread is execute), with their threads real."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone, translation

from apps.accounts.models import User
from apps.activity.models import ActivityLog
from apps.storages import config, drivers, instances
from apps.storages.models import StorageSource

from .. import runner, service
from ..models import CopyFailure, CopyLog, CopyTask
from . import drivers as test_drivers

Status = CopyTask.Status


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def options(**changes):
    data = {"source_path": "/", "target_path": "/", "threads": 2, "speed_limit": 0, "same_file": "skip",
            "same_name": "overwrite", "retries": 2}
    data.update(changes)
    return data


class CopyTestCase(TestCase):
    """Storage "src" on a folder with a few files, "dst" on an empty one; an admin."""

    source_type = "local"
    target_type = "local"

    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.dict(drivers.DRIVERS, test_drivers.TYPES), mock.patch.object(instances, "GRACE", 0),
                        mock.patch.object(runner, "start_thread", runner.execute),
                        mock.patch.object(runner, "BACKOFF_MAX", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        test_drivers.reset()
        instances.close_all()
        self.addCleanup(instances.close_all)
        self.src = self.folder("src")
        self.dst = self.folder("dst")
        self.source = self.storage("src", self.source_type, self.src)
        self.target = self.storage("dst", self.target_type, self.dst)
        self.admin = User.objects.create_user("admin", password="pw-admin-2026", is_staff=True)
        self.files(self.src, {"a.txt": b"alpha", "docs/b.txt": b"bravo!", "docs/deep/c.bin": bytes(range(256)) * 300,
                              "empty/": None, "zero.txt": b""})

    def folder(self, name):
        path = Path(tempfile.mkdtemp(prefix=f"hfile-copy-{name}-"))
        self.addCleanup(shutil.rmtree, path, ignore_errors=True)
        return path

    def storage(self, key, type_, root, **fields):
        cls = drivers.driver_class(type_)
        return StorageSource.objects.create(key=key, name=f"存储 {key}", type=type_,
                                            config=config.encode(cls, {"root": str(root)}), **fields)

    @staticmethod
    def files(root, tree):
        for name, data in tree.items():
            path = Path(root) / name
            if data is None:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    @staticmethod
    def tree(root):
        """{relative path, with / on Windows too: the file's SHA-256, or None for a folder}. Not the bytes: when two
        such dicts differ, unittest's message is a difflib diff of them, which with c.bin's 76800 bytes in them takes
        hours on Python 3.12."""
        root = Path(root)
        return {("/" + path.relative_to(root).as_posix()): (None if path.is_dir() else sha256(path.read_bytes()))
                for path in sorted(root.rglob("*")) if not path.name.startswith(".")}

    def copy(self, **changes) -> CopyTask:
        data = options(**changes)
        data.setdefault("source", self.source.pk)
        data.setdefault("target", self.target.pk)
        task = service.create(data, self.admin)
        task.refresh_from_db()
        return task

    def failures(self, task):
        """In path order: of two files copied at once (two threads), either may fail first."""
        return sorted(CopyFailure.objects.filter(task=task).values_list("path", "message"))


class CopyingTests(CopyTestCase):
    def test_copies_everything(self):
        task = self.copy()
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(self.tree(self.dst), self.tree(self.src))
        self.assertEqual((task.files_total, task.files_copied, task.files_skipped, task.files_failed), (4, 4, 0, 0))
        size = 5 + 6 + 256 * 300
        self.assertEqual((task.bytes_total, task.bytes_copied, task.bytes_done, task.transferred), (size, size, size, size))
        self.assertFalse(task.scanning)
        self.assertEqual(task.current, [])
        self.assertEqual(task.speed, 0)
        self.assertIsNotNone(task.finished_at)
        self.assertEqual((task.created_by, task.created_by_name, task.source_name, task.target_name),
                         (self.admin, "admin", "存储 src", "存储 dst"))

    def test_into_a_folder_that_isnt_there_yet(self):
        """The source folder itself is copied: into a folder of its name in the target folder, made with the folders
        above it that aren't there yet."""
        task = self.copy(source_path="/docs", target_path="/backup/2026")
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(task.target_path, "/backup/2026/docs")
        self.assertEqual(self.tree(self.dst), {"/backup": None, "/backup/2026": None, "/backup/2026/docs": None,
                                               "/backup/2026/docs/b.txt": sha256(b"bravo!"), "/backup/2026/docs/deep": None,
                                               "/backup/2026/docs/deep/c.bin": sha256(bytes(range(256)) * 300)})

    def test_into_a_folder_of_its_name_already_there(self):
        """What is in it stays; a file of the same name goes as same_file and same_name say (overwrite: another size)."""
        self.files(self.dst, {"backup/docs/old.txt": b"old", "backup/docs/b.txt": b"BRAVO"})
        task = self.copy(source_path="/docs", target_path="/backup")
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(self.tree(self.dst / "backup" / "docs"), {
            "/b.txt": sha256(b"bravo!"), "/deep": None, "/deep/c.bin": sha256(bytes(range(256)) * 300),
            "/old.txt": sha256(b"old")})

    def test_from_the_root_folder(self):
        """The root folder has no name: what is in it goes straight into the target folder."""
        task = self.copy(target_path="/backup")
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(task.target_path, "/backup")
        self.assertEqual(self.tree(self.dst / "backup"), self.tree(self.src))

    def test_within_one_storage(self):
        task = self.copy(target=self.source.pk, source_path="/docs/deep", target_path="/copy")
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual((self.src / "copy" / "deep" / "c.bin").read_bytes(), bytes(range(256)) * 300)

    def test_same_file_and_same_name(self):
        """A file there with the same name and size: same_file; with the same name only: same_name."""
        cases = [("skip", "skip", b"ALPHA", b"bravo", 1, 3), ("overwrite", "skip", b"alpha", b"bravo", 3, 1),
                 ("skip", "overwrite", b"ALPHA", b"bravo!", 2, 2), ("overwrite", "overwrite", b"alpha", b"bravo!", 4, 0)]
        for same_file, same_name, a, b, copied, skipped in cases:
            with self.subTest(same_file=same_file, same_name=same_name):
                for path in self.dst.iterdir():
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
                # "ALPHA" has a.txt's size, "bravo" not b.txt's ("bravo!"); zero.txt is there too, empty
                self.files(self.dst, {"a.txt": b"ALPHA", "docs/b.txt": b"bravo", "zero.txt": b""})
                task = self.copy(source_path="/", same_file=same_file, same_name=same_name)
                self.assertEqual(task.status, Status.DONE, task.message)
                self.assertEqual((self.dst / "a.txt").read_bytes(), a)
                self.assertEqual((self.dst / "docs" / "b.txt").read_bytes(), b)
                # c.bin is new; zero.txt is the same file, as a.txt
                self.assertEqual((task.files_copied, task.files_skipped), (copied, skipped))

    def test_a_target_that_reads_twice(self):
        """A target that hashes the file before storing it gets a file it can seek in: nothing on the disk."""
        target = self.storage("hash", "test-copy-hashfirst", self.folder("hash"))
        task = self.copy(target=target.pk, source_path="/docs")
        self.assertEqual(task.status, Status.DONE, task.message)
        data = bytes(range(256)) * 300
        self.assertIn(hashlib.md5(data).hexdigest(), test_drivers.HashFirstDriver.digests)
        stored = self.tree(Path(config.decode(drivers.driver_class("test-copy-hashfirst"), target.config)[0]["root"]))
        self.assertEqual(stored["/docs/deep/c.bin"], sha256(data))
        self.assertEqual(task.transferred, 2 * (6 + len(data)))  # each file read twice

    def test_a_source_that_cant_start_halfway(self):
        source = self.storage("norange", "test-copy-norange", self.src)
        target = self.storage("hash", "test-copy-hashfirst", self.folder("hash"))
        task = self.copy(source=source.pk, target=target.pk, source_path="/docs/deep")
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(test_drivers.NoRangeDriver.opens, ["/docs/deep/c.bin", "/docs/deep/c.bin"])

    def test_tries_again(self):
        source = self.storage("flaky", "test-copy-flaky", self.src)
        task = self.copy(source=source.pk, source_path="/docs", retries=2)
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(test_drivers.FlakyDriver.tries, {"/docs/b.txt": 3, "/docs/deep/c.bin": 3})

    def test_gives_up_after_the_retries(self):
        source = self.storage("flaky", "test-copy-flaky", self.src)
        test_drivers.FlakyDriver.failures = 5
        task = self.copy(source=source.pk, source_path="/docs", retries=1)
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(task.message, "2 个文件没有复制成功。")
        self.assertEqual(task.files_failed, 2)
        self.assertEqual(self.failures(task), [("/b.txt", "读取源文件失败：连接断了"),
                                               ("/deep/c.bin", "读取源文件失败：连接断了")])
        self.assertEqual(list(task.failures.values_list("attempts", flat=True)), [2, 2])
        self.assertEqual(test_drivers.FlakyDriver.tries, {"/docs/b.txt": 2, "/docs/deep/c.bin": 2})
        self.assertEqual(self.tree(self.dst), {"/docs": None, "/docs/deep": None})

    def test_a_stream_that_breaks(self):
        source = self.storage("broken", "test-copy-broken", self.src)
        task = self.copy(source=source.pk, source_path="/docs", retries=0)
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(self.failures(task), [("/b.txt", "读取源文件失败：读到一半断开了"),
                                               ("/deep/c.bin", "读取源文件失败：读到一半断开了")])
        self.assertEqual(self.tree(self.dst), {"/docs": None, "/docs/deep": None})  # no half files (local writes a temporary one)

    def test_an_account_that_stops_working_stops_the_run(self):
        target = self.storage("expired", "test-copy-expired", self.folder("expired"))
        task = self.copy(target=target.pk, threads=1, retries=3)
        self.assertEqual(task.status, Status.FAILED)
        self.assertTrue(task.message.startswith("存储 expired："), task.message)
        self.assertEqual(test_drivers.ExpiredDriver.writes, 1)  # not tried again, nor the other files
        self.assertEqual(task.files_failed, 0)

    def test_names_the_target_refuses(self):
        self.files(self.src, {"x#y.txt": b"x", "f#g/h.txt": b"h"})
        target = self.storage("strict", "test-copy-strict", self.folder("strict"))
        task = self.copy(target=target.pk)
        self.assertEqual(task.status, Status.FAILED)
        message = "这种存储的名字里不能有这些字符：#"
        self.assertEqual(self.failures(task), [("/f#g", message), ("/x#y.txt", message)])
        self.assertEqual((task.files_failed, task.folders_failed, task.files_copied), (1, 1, 4))
        self.assertIn("1 个文件没有复制成功。", task.message)
        self.assertIn("1 个文件夹没能复制，里面的东西也都没有复制。", task.message)

    def test_a_folder_where_a_file_is(self):
        self.files(self.dst, {"docs": b"a file"})
        task = self.copy()
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(self.failures(task), [("/docs", "目标文件夹里有同名的文件。")])
        self.assertEqual((self.dst / "docs").read_bytes(), b"a file")
        self.assertEqual((self.dst / "a.txt").read_bytes(), b"alpha")

    def test_a_file_where_a_folder_is(self):
        self.files(self.dst, {"a.txt/": None})
        task = self.copy()
        self.assertEqual(self.failures(task), [("/a.txt", "目标文件夹里有同名的文件夹。")])

    def test_sizes_unknown(self):
        source = self.storage("nosize", "test-copy-nosize", self.src)
        task = self.copy(source=source.pk, source_path="/docs")
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(self.failures(task), [("/b.txt", "这个文件的大小不知道，不能复制。"),
                                               ("/deep/c.bin", "这个文件的大小不知道，不能复制。")])

    def test_the_source_folder_gone(self):
        """Gone after the task was made, before its run looked."""
        shutil.rmtree(self.src / "docs")
        with mock.patch.object(service, "_check_folders"):
            task = self.copy(source_path="/docs")
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(task.message, "源文件夹不存在。")

    def test_a_storage_disabled_before_it_starts(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        StorageSource.objects.filter(pk=self.target.pk).update(enabled=False)
        runner.execute(runner.Run(task, "zh-hans"))
        task.refresh_from_db()
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(task.message, "存储 dst：这个存储源已停用。")
        self.assertEqual(self.tree(self.dst), {})

    def test_a_link_back_up(self):
        """The local storage follows links inside its folder: a folder that lists as one above it is a link back up and
        isn't gone into (it would copy copies of copies; two such links, without end)."""
        os.symlink("..", self.src / "docs" / "deep" / "up")  # docs/deep/up is docs
        os.symlink(".", self.src / "docs" / "again")  # docs/again is docs too
        task = self.copy(source_path="/docs")
        self.assertEqual(task.status, Status.FAILED)
        message = "它和上面的一个文件夹里的文件和文件夹完全一样，多半是指回上层的链接，里面的东西没有复制。"
        self.assertEqual(self.failures(task), [("/again", message), ("/deep/up", message)])
        self.assertEqual(task.message, "2 个文件夹没能复制，里面的东西也都没有复制。")
        self.assertEqual(self.tree(self.dst), {"/docs": None, "/docs/b.txt": sha256(b"bravo!"), "/docs/deep": None,
                                               "/docs/deep/c.bin": sha256(bytes(range(256)) * 300)})

    def test_a_link_back_up_listed_as_ntfs_does(self):
        """NTFS lists a folder with an older time until it has been listed itself: docs listed before deep was,
        docs/deep/up (docs again) after. docs is listed again then, and lists as docs/deep/up does."""
        os.symlink("..", self.src / "docs" / "deep" / "up")
        os.symlink(".", self.src / "docs" / "again")
        source = self.storage("stale", "test-copy-stale", self.src)
        task = self.copy(source=source.pk, source_path="/docs")
        message = "它和上面的一个文件夹里的文件和文件夹完全一样，多半是指回上层的链接，里面的东西没有复制。"
        self.assertEqual(self.failures(task), [("/again", message), ("/deep/up", message)])
        # docs again after docs/deep/up; on Windows after docs/again too: the driver's realpath() opens deep, so NTFS
        # brings deep's time in docs up to date before that
        self.assertEqual(test_drivers.StaleTimesDriver.lists[-2:], ["/docs/deep/up", "/docs"])
        self.assertEqual(self.tree(self.dst), {"/docs": None, "/docs/b.txt": sha256(b"bravo!"), "/docs/deep": None,
                                               "/docs/deep/c.bin": sha256(bytes(range(256)) * 300)})

    def test_listings_are_tried_again(self):
        source = self.storage("flakylist", "test-copy-flakylist", self.src)
        task = self.copy(source=source.pk, source_path="/docs", retries=1)
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(test_drivers.FlakyListDriver.tries, {"/docs": 2, "/docs/deep": 2})
        test_drivers.reset()
        test_drivers.FlakyListDriver.failures = 2
        task = self.copy(source=source.pk, source_path="/docs", target_path="/again", retries=1)
        self.assertEqual(self.failures(task), [("/", "没能列出这个文件夹：列表超时了")])

    def test_workers_by_the_connections(self):
        """A storage with a pool of connections (FTP, SFTP) gets no more files at once than it has connections, less
        one for the scanner; half of those when it is both ends."""

        class Pooled:
            def __init__(self, size):
                self.max_transfers = size

        free = object()
        self.assertEqual(runner.workers(16, free, object()), 16)
        self.assertEqual(runner.workers(16, Pooled(8), free), 7)
        self.assertEqual(runner.workers(4, free, Pooled(8)), 4)
        self.assertEqual(runner.workers(16, Pooled(4), Pooled(8)), 3)
        self.assertEqual(runner.workers(16, Pooled(1), free), 1)
        same = Pooled(8)
        self.assertEqual(runner.workers(16, same, same), 3)
        same = Pooled(2)
        self.assertEqual(runner.workers(16, same, same), 1)
        for type_, extra in (("ftp", {"host": "ftp.example.test"}), ("sftp", {"host": "sftp.example.test", "username": "u"})):
            with self.subTest(type_):
                driver = drivers.driver_class(type_)({**drivers.driver_class(type_).defaults(), **extra, "max_connections": 5})
                self.assertEqual(driver.max_transfers, 5)
                driver.close()

    def test_messages_in_the_language_of_who_started_it(self):
        source = self.storage("flaky", "test-copy-flaky", self.src)
        test_drivers.FlakyDriver.failures = 5
        with translation.override("en"):
            task = self.copy(source=source.pk, source_path="/docs/deep", retries=0)
        self.assertEqual(task.message, "1 file couldn't be copied.")
        self.assertEqual(self.failures(task), [("/c.bin", "Couldn't read it from the source: 连接断了")])


class StoppingTests(CopyTestCase):
    def test_an_admin_stops_it(self):
        """Stopping it marks it; the run sees that in the database at its next tick."""
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        service.stop(task.pk)
        self.assertEqual(CopyTask.objects.get(pk=task.pk).status, Status.STOPPING)
        run.tick(timezone.now())
        self.assertTrue(run.stop.is_set())
        run.finish(timezone.now())
        task.refresh_from_db()
        self.assertEqual((task.status, task.message), (Status.STOPPED, ""))
        entry = ActivityLog.objects.filter(kind=ActivityLog.Kind.STOP_COPY_TASK).get()
        self.assertEqual((entry.storage, entry.path, entry.to_path, entry.detail["task"]), (self.source, "/", "/", task.pk))

    def test_stopping_halfway_keeps_what_was_copied(self):
        source = self.storage("waiting", "test-copy-waiting", self.src)
        test_drivers.WaitingDriver.paths = {"/docs/b.txt"}
        with mock.patch.object(runner, "start_thread"):
            task = self.copy(source=source.pk, threads=1)
        run = runner.Run(task, "zh-hans")

        def halt_when_reached():  # a.txt and zero.txt are copied; b.txt waits after its first byte
            test_drivers.WaitingDriver.reached.wait(10)
            run.halt(Status.STOPPED)
            test_drivers.WaitingDriver.go_on.set()

        helper = threading.Thread(target=halt_when_reached)
        helper.start()
        runner.execute(run)
        helper.join()
        task.refresh_from_db()
        self.assertEqual(task.status, Status.STOPPED)
        self.assertEqual((self.dst / "a.txt").read_bytes(), b"alpha")
        self.assertTrue((self.dst / "zero.txt").exists())
        self.assertFalse((self.dst / "docs" / "b.txt").exists())
        self.assertEqual(task.files_copied, 2)
        self.assertEqual(task.files_failed, 0)
        self.assertEqual(task.current, [])

    def test_stop_and_resume_only_when_they_make_sense(self):
        task = self.copy()
        self.assertEqual(task.status, Status.DONE)
        with self.assertRaisesMessage(Exception, "这个任务没有在运行。"):
            service.stop(task.pk)
        with self.assertRaisesMessage(Exception, "只有失败或停止的任务才能继续。"):
            service.resume(task.pk)

    def test_a_run_that_died(self):
        """Its last write long ago: shown as failed, and it can be resumed; not stopped."""
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        CopyTask.objects.filter(pk=task.pk).update(updated_at=timezone.now() - runner.STALE - timedelta(seconds=1))
        task.refresh_from_db()
        status, message = runner.status_of(task)
        self.assertEqual(status, Status.FAILED)
        self.assertIn("可能是中途重启了程序", message)
        self.assertTrue(service.summary(task, timezone.now())["can_resume"])
        self.assertFalse(service.summary(task, timezone.now())["can_stop"])
        with self.assertRaisesMessage(Exception, "这个任务没有在运行。"):
            service.stop(task.pk)
        task = service.resume(task.pk)
        task.refresh_from_db()
        self.assertEqual((task.status, task.run), (Status.DONE, 2))

    def test_a_run_taken_over_writes_nothing(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        CopyTask.objects.filter(pk=task.pk).update(run_id=runner.new_run_id())
        with self.assertRaises(runner.Superseded):
            run.tick(timezone.now())
        with self.assertRaises(runner.Superseded):
            run.finish(timezone.now())
        runner.execute(run)  # ends without writing
        self.assertEqual(CopyTask.objects.get(pk=task.pk).status, Status.RUNNING)

    def test_storage_settings_changed(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        runner._drivers(run)
        StorageSource.objects.filter(pk=self.target.pk).update(config_revision=5)
        run.tick(timezone.now())
        self.assertTrue(run.stop.is_set())
        self.assertIn("存储 dst", str(run.ending[1]))
        StorageSource.objects.filter(pk=self.target.pk).delete()
        run.finish(timezone.now())
        task.refresh_from_db()
        self.assertEqual(task.status, Status.FAILED)
        self.assertIn("的设置改了", task.message)

    def test_after_a_database_error(self):
        """The run's thread drops its connection after an error of the database (Django does that only between
        requests), so that its next write opens a new one; a test's, inside a transaction, stays."""
        source = self.storage("waiting", "test-copy-waiting", self.src)
        test_drivers.WaitingDriver.paths = {"/docs/b.txt"}
        with mock.patch.object(runner, "start_thread"):
            task = self.copy(source=source.pk, source_path="/docs", threads=1)
        run = runner.Run(task, "zh-hans")
        ticked = threading.Event()

        def tick(now):
            ticked.set()
            raise DatabaseError("MySQL server has gone away")

        def go_on():
            test_drivers.WaitingDriver.reached.wait(10)
            ticked.wait(10)
            test_drivers.WaitingDriver.go_on.set()

        helper = threading.Thread(target=go_on)
        helper.start()
        with mock.patch.object(run, "tick", tick), mock.patch.object(runner, "TICK", 0.01), \
                mock.patch.object(runner, "_drop_connections", wraps=runner._drop_connections) as dropped, \
                self.assertLogs("apps.copytasks.runner", "WARNING") as logged:
            runner.execute(run)
        helper.join()
        self.assertTrue(dropped.called)
        self.assertIn("couldn't write the progress", logged.output[0])
        task.refresh_from_db()  # the test's connection works
        self.assertEqual((task.status, task.files_copied), (Status.DONE, 2))

    def test_stopped_before_the_storages_close_at_exit(self):
        """stop_all() is registered with the first run, after the storages' close_all(): atexit runs it first."""
        with mock.patch.object(runner, "_exit_hooked", False), mock.patch.object(runner.atexit, "register") as register, \
                mock.patch.object(runner, "start_thread"):
            self.copy()
            self.copy(source_path="/docs")
        register.assert_called_once_with(runner.stop_all)

    def test_the_program_exits(self):
        """stop_all() at exit: the run ends as interrupted."""
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        with runner._active_lock:
            runner._active.add(run)
        try:
            with mock.patch.object(run.ended, "wait"):
                runner.stop_all(grace=0)
        finally:
            with runner._active_lock:
                runner._active.discard(run)
        self.assertTrue(run.stop.is_set())
        run.finish(timezone.now())
        task.refresh_from_db()
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(task.message, "复制到一半网站停止了，任务中断。可以继续复制。")


class ResumingTests(CopyTestCase):
    def test_goes_on_where_it_stopped(self):
        """继续: what is there with the same name and size is skipped, the rest copied; the failures go."""
        source = self.storage("flaky", "test-copy-flaky", self.src)
        test_drivers.FlakyDriver.failures = 1
        task = self.copy(source=source.pk, same_file="overwrite", retries=0)
        self.assertEqual(task.status, Status.FAILED)
        self.assertEqual(task.files_failed, 4)  # every file failed once; zero.txt too (it is opened all the same)
        self.assertEqual(CopyFailure.objects.filter(task=task).count(), 4)
        (self.dst / "a.txt").write_bytes(b"alpha")  # as if the first run had copied it
        task = service.resume(task.pk)
        task.refresh_from_db()
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual((task.run, task.files_copied, task.files_skipped, task.files_failed), (2, 3, 1, 0))
        self.assertEqual(CopyFailure.objects.filter(task=task).count(), 0)
        self.assertEqual(self.tree(self.dst), self.tree(self.src))
        entry = ActivityLog.objects.filter(kind=ActivityLog.Kind.RESUME_COPY_TASK).get()
        self.assertEqual(entry.detail, {"task": task.pk, "to_storage": "存储 dst", "to_storage_id": self.target.pk,
                                        "run": 2})

    def test_goes_on_in_the_same_folder(self):
        """继续 copies into the task's target_path as it is: the folder of the source folder's name made with the task, or
        for a task made when what was in the source folder went straight into the folder picked (1.2.0), that one."""
        with mock.patch.object(runner, "start_thread"):
            task = self.copy(source_path="/docs", target_path="/backup")
            older = self.copy(source_path="/docs", target_path="/older")
        CopyTask.objects.filter(pk=older.pk).update(target_path="/older")
        CopyTask.objects.filter(pk__in=[task.pk, older.pk]).update(status=Status.STOPPED)
        for pk in (task.pk, older.pk):
            service.resume(pk)
            self.assertEqual(CopyTask.objects.get(pk=pk).status, Status.DONE)
        data = sha256(bytes(range(256)) * 300)
        self.assertEqual(self.tree(self.dst), {
            "/backup": None, "/backup/docs": None, "/backup/docs/b.txt": sha256(b"bravo!"), "/backup/docs/deep": None,
            "/backup/docs/deep/c.bin": data,
            "/older": None, "/older/b.txt": sha256(b"bravo!"), "/older/deep": None, "/older/deep/c.bin": data})

    def test_needs_its_storages(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        CopyTask.objects.filter(pk=task.pk).update(status=Status.STOPPED)
        StorageSource.objects.filter(pk=self.source.pk).update(enabled=False)
        with self.assertRaisesMessage(Exception, "存储源 存储 src 已停用，请先启用它。"):
            service.resume(task.pk)
        self.source.delete()
        with self.assertRaisesMessage(Exception, "存储源 存储 src 已经删除了。"):
            service.resume(task.pk)


class LogTests(CopyTestCase):
    """The detailed log (CopyLog): a line for each file copied, skipped, tried again or failed, as it happens, between the
    lines of its run's beginning and end; the lines of every run are kept."""

    def lines(self, task, *fields):
        """Oldest first: (kind, path, *fields)."""
        return list(CopyLog.objects.filter(task=task).order_by("id").values_list("kind", "path", *fields))

    def test_a_run(self):
        task = self.copy()
        lines = self.lines(task, "run", "size", "attempts", "status", "message")
        self.assertEqual(lines[0], ("started", "", 1, None, 0, "", ""))
        self.assertEqual(lines[-1], ("ended", "", 1, None, 0, "done", ""))
        self.assertEqual(sorted(lines[1:-1]), [  # two threads: the files in either order
            ("copied", "/a.txt", 1, 5, 1, "", ""), ("copied", "/docs/b.txt", 1, 6, 1, "", ""),
            ("copied", "/docs/deep/c.bin", 1, 256 * 300, 1, "", ""), ("copied", "/zero.txt", 1, 0, 1, "", "")])
        self.assertTrue(all(seconds >= 0 for seconds in CopyLog.objects.filter(kind="copied").values_list("seconds", flat=True)))
        self.assertEqual(CopyLog.objects.filter(task=task).first().at, task.finished_at)  # the newest: the end

    def test_skipped_and_kept(self):
        """The same name and size: skipped (same_file); the same name and another size: kept as it is (same_name)."""
        self.files(self.dst, {"a.txt": b"ALPHA", "docs/b.txt": b"bravo", "zero.txt": b""})
        task = self.copy(same_name="skip")
        self.assertEqual(sorted(self.lines(task, "size")[1:-1]), [
            ("copied", "/docs/deep/c.bin", 256 * 300), ("kept", "/docs/b.txt", 6), ("skipped", "/a.txt", 5),
            ("skipped", "/zero.txt", 0)])

    def test_tries(self):
        """Each try that failed before another, with its error; the try that copied it, or how many failed."""
        source = self.storage("flaky", "test-copy-flaky", self.src)
        task = self.copy(source=source.pk, source_path="/docs", retries=2)  # each file fails twice
        lines = self.lines(task, "attempts", "seconds", "message")
        error = "读取源文件失败：连接断了"
        for path in ("/b.txt", "/deep/c.bin"):
            self.assertEqual([line[2:] if line[0] == "retry" else line[:3] for line in lines if line[1] == path],
                             [(1, 0, error), (2, 0, error), ("copied", path, 3)])  # BACKOFF_MAX is 0 here: no wait
        test_drivers.reset()
        test_drivers.FlakyDriver.failures = 5
        task = self.copy(source=source.pk, source_path="/docs", target_path="/again", retries=1)
        lines = self.lines(task, "attempts", "status", "message")
        self.assertEqual(sorted(lines[1:-1]), [
            ("failed", "/b.txt", 2, "", error), ("failed", "/deep/c.bin", 2, "", error),
            ("retry", "/b.txt", 1, "", error), ("retry", "/deep/c.bin", 1, "", error)])
        self.assertEqual(lines[-1], ("ended", "", 0, "failed", "2 个文件没有复制成功。"))

    def test_folders_that_failed(self):
        os.symlink("..", self.src / "docs" / "deep" / "up")
        task = self.copy(source_path="/docs")
        self.assertIn(("failed", "/deep/up", True), self.lines(task, "is_dir"))

    def test_every_run(self):
        """继续: the failures of the run before go, its lines stay; the new run's come after them."""
        source = self.storage("flaky", "test-copy-flaky", self.src)
        test_drivers.FlakyDriver.failures = 1
        task = self.copy(source=source.pk, same_file="overwrite", retries=0)
        service.resume(task.pk)
        self.assertEqual([(kind, run) for kind, _path, run in self.lines(task, "run")], [
            ("started", 1), *[("failed", 1)] * 4, ("ended", 1), ("started", 2), *[("copied", 2)] * 4, ("ended", 2)])
        self.assertFalse(CopyFailure.objects.filter(task=task).exists())

    def test_a_run_that_died(self):
        """It wrote no end: 继续 writes one for it first, as of its last write."""
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        last = timezone.now() - runner.STALE - timedelta(seconds=1)
        CopyTask.objects.filter(pk=task.pk).update(updated_at=last)
        service.resume(task.pk)
        lines = list(CopyLog.objects.filter(task=task).order_by("id"))
        self.assertEqual([(line.kind, line.run) for line in lines[:3]], [("started", 1), ("ended", 1), ("started", 2)])
        self.assertEqual((lines[1].status, lines[1].at), ("failed", last))
        self.assertIn("可能是中途重启了程序", lines[1].message)
        self.assertEqual((lines[-1].kind, lines[-1].status), ("ended", "done"))

    def test_stopped(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        run.halt(Status.STOPPED)
        run.finish(timezone.now())
        self.assertEqual(self.lines(task, "status")[-1], ("ended", "", "stopped"))

    def test_the_scanner_waits_for_the_lines_to_be_written(self):
        """More than LOG_BACKLOG lines not written yet (a 继续 skips many files at once): the scanner waits for the run's
        thread to write them."""
        self.files(self.dst, {"a.txt": b"alpha", "docs/b.txt": b"bravo!", "zero.txt": b""})
        with mock.patch.object(runner, "LOG_BACKLOG", 1), mock.patch.object(runner, "TICK", 0.01):
            task = self.copy()
        self.assertEqual(task.status, Status.DONE, task.message)
        self.assertEqual(sorted(kind for kind, _path in self.lines(task)[1:-1]), ["copied", "skipped", "skipped", "skipped"])

    def test_lines_stay_when_a_write_fails(self):
        with mock.patch.object(runner, "start_thread"):
            task = self.copy()
        run = runner.Run(task, "zh-hans")
        run._skipped("/a.txt", 5, CopyLog.Kind.SKIPPED)
        with mock.patch.object(CopyLog.objects, "bulk_create", side_effect=DatabaseError("gone")), \
                self.assertRaises(DatabaseError):
            run._write()
        run._write()
        self.assertEqual(self.lines(task), [("started", ""), ("skipped", "/a.txt")])
