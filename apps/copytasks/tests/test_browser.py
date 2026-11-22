"""/manage/#copy-tasks in a real browser (apps/web/test_browser.py's BrowserTestCase): making a task in the dialog
with the folder picker, following it, its details, stopping and resuming. The runs happen in place, inside the
request that starts them (the live server answers one request at a time, on its one connection to the test
database)."""

import shutil
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from apps.accounts.models import User
from apps.storages import config, drivers
from apps.storages.models import StorageSource
from apps.web.test_browser import BrowserTestCase, expect

from .. import runner, service
from ..models import CopyFailure, CopyTask
from .test_runner import options


class CopyTaskPageTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(runner, "start_thread", runner.execute), mock.patch.object(runner, "BACKOFF_MAX", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.src_root = self.local_storage({"photos/a.jpg": b"a" * 1000, "photos/2024/b.jpg": b"b" * 2000, "notes.txt": b"n"}, key="src")
        self.source = StorageSource.objects.get(key="src")
        # a second local storage on a folder of its own (local_storage() closes the instances first: once is enough)
        self.dst_root = self.src_root.parent / f"{self.src_root.name}-dst"
        self.dst_root.mkdir()
        self.addCleanup(shutil.rmtree, self.dst_root, ignore_errors=True)
        self.target = StorageSource.objects.create(key="dst", name="Dst", type="local",
                                                   config=config.encode(drivers.driver_class("local"), {"root": str(self.dst_root)}))

    def page(self, language="zh-hans"):
        return self.open(self.new_context(user=self.admin, language=language), "/manage/#copy-tasks")

    def test_a_new_task(self):
        page = self.page()
        expect(page.locator(".copy-empty")).to_have_text("还没有复制任务。点「新建复制任务」新建一个。")
        page.get_by_role("button", name="新建复制任务").click()
        dialog = page.get_by_role("dialog", name="新建复制任务")
        dialog.locator("#copy-source").select_option(str(self.source.pk))
        dialog.locator("[data-field=source_path]").get_by_role("button", name="选择文件夹").click()
        picker = page.get_by_role("dialog", name="从哪个文件夹复制")
        expect(picker.get_by_role("option")).to_have_count(1)
        picker.get_by_role("option", name="photos").click()
        expect(picker.get_by_role("option", name="2024")).to_be_visible()
        picker.get_by_role("button", name="选择这个文件夹").click()
        expect(dialog.locator("#copy-source-path")).to_have_value("/photos")
        dialog.locator("#copy-target").select_option(str(self.target.pk))
        dialog.locator("#copy-target-path").fill("/backup")
        dialog.locator("#copy-threads").fill("2")
        dialog.get_by_role("button", name="开始复制").click()
        expect(dialog).to_be_hidden()
        card = page.locator(".copy-task")
        expect(card.locator(".badge")).to_have_text("完成")
        expect(card.locator(".copy-task-route")).to_contain_text("Src")
        expect(card.locator(".copy-task-route")).to_contain_text("/photos")
        expect(card.locator(".copy-task-route")).to_contain_text("/backup/photos")
        expect(card.locator(".copy-task-line")).to_contain_text("2 / 2 个文件")
        facts = card.locator(".copy-facts")  # opened by itself
        expect(facts).to_contain_text("线程数2")
        expect(facts).to_contain_text("已复制2 个文件，2.93 KB")
        self.assertEqual((self.dst_root / "backup" / "photos" / "2024" / "b.jpg").read_bytes(), b"b" * 2000)
        self.assertEqual((self.dst_root / "backup" / "photos" / "a.jpg").read_bytes(), b"a" * 1000)
        self.assertEqual(CopyTask.objects.get().threads, 2)
        self.assertClean(page)

    def test_the_dialog_says_whats_wrong(self):
        page = self.page()
        page.get_by_role("button", name="新建复制任务").click()
        dialog = page.get_by_role("dialog", name="新建复制任务")
        dialog.locator("#copy-source").select_option(str(self.source.pk))
        dialog.locator("#copy-source-path").fill("/nope")
        dialog.locator("#copy-retries").fill("11")
        dialog.get_by_role("button", name="开始复制").click()
        expect(dialog.locator("[data-field=retries] .field-error")).to_have_text("0 到 10 的整数。")
        dialog.locator("#copy-retries").fill("3")
        dialog.locator("#copy-speed").fill("")
        dialog.locator("#copy-speed").press_sequentially("1e")  # not a number: not "no limit" either
        dialog.get_by_role("button", name="开始复制").click()
        expect(dialog.locator("[data-field=speed_limit] .field-error")).to_have_text("0（不限）到 10240 的数。")
        expect(dialog.locator("[data-field=retries] .field-error")).to_have_count(0)
        dialog.locator("#copy-speed").fill("0")
        dialog.get_by_role("button", name="开始复制").click()
        expect(dialog.locator("[data-field=source_path] .field-error")).to_have_text("这个文件夹不存在。")
        self.assertFalse(CopyTask.objects.exists())
        self.assertClean(page, allow=[r"^400 POST .*/api/v1/manage/copy-tasks/$", r"^console: Failed to load resource: .* 400"])

    def test_failures_and_resuming(self):
        (self.dst_root / "notes.txt").mkdir()  # a folder where the file goes
        task = service.create({**options(), "source": self.source.pk, "target": self.target.pk}, self.admin)
        page = self.page()
        card = page.locator(".copy-task")
        expect(card.locator(".badge")).to_have_text("失败")
        expect(card.locator(".copy-task-message")).to_have_text("1 个文件没有复制成功。")
        card.get_by_role("button", name="详情").click()
        failure = card.locator(".copy-failure")
        expect(failure).to_have_count(1)
        expect(failure).to_contain_text("/notes.txt")
        expect(failure).to_contain_text("目标文件夹里有同名的文件夹。")
        (self.dst_root / "notes.txt").rmdir()
        card.get_by_role("button", name="继续复制").click()
        page.get_by_role("alertdialog").get_by_role("button", name="继续复制").click()
        expect(card.locator(".badge")).to_have_text("完成")
        expect(card.locator(".copy-failure")).to_have_count(0)
        expect(card.locator(".copy-facts")).to_contain_text("第 2 次运行")
        expect(card.locator(".copy-facts")).to_contain_text("跳过2 个文件")
        self.assertEqual((self.dst_root / "notes.txt").read_bytes(), b"n")
        task.refresh_from_db()
        self.assertEqual((task.status, task.run, task.files_copied, task.files_skipped), (CopyTask.Status.DONE, 2, 1, 2))
        self.assertClean(page)

    def test_a_running_task(self):
        """What the page shows of a run under way (written here as a run would), and stopping it."""
        with mock.patch.object(runner, "start_thread"):
            task = service.create({**options(), "source": self.source.pk, "target": self.target.pk}, self.admin)
        CopyTask.objects.filter(pk=task.pk).update(
            updated_at=timezone.now() + timedelta(minutes=5), files_total=3, files_copied=1, bytes_total=3001,
            bytes_copied=1000, bytes_done=1500, speed=2048, current=[
                {"path": "/photos/2024/b.jpg", "size": 2000, "done": 500, "speed": 1024, "attempt": 2, "error": "连接断了"}])
        page = self.page()
        card = page.locator(".copy-task")
        expect(card.locator(".badge")).to_have_text("复制中")
        expect(card.locator(".copy-task-line")).to_contain_text("还在列出文件")
        expect(card.locator(".copy-task-line")).to_contain_text("2.00 KB/秒")
        card.get_by_role("button", name="详情").click()
        file = card.locator(".copy-file")
        expect(file).to_contain_text("/photos/2024/b.jpg")
        expect(file).to_contain_text("500 B / 1.95 KB")
        expect(file).to_contain_text("第 2 次尝试")
        expect(file).to_contain_text("上一次失败了：连接断了")
        card.get_by_role("button", name="停止").click()
        page.get_by_role("alertdialog").get_by_role("button", name="停止").click()
        expect(card.locator(".badge")).to_have_text("正在停止")
        expect(card.get_by_role("button", name="停止")).to_be_hidden()
        self.assertEqual(CopyTask.objects.get(pk=task.pk).status, CopyTask.Status.STOPPING)
        self.assertClean(page)

    def test_details_when_it_ends(self):
        """Open details follow the run to its end: the files that failed last show too."""
        with mock.patch.object(runner, "start_thread"):
            task = service.create({**options(), "source": self.source.pk, "target": self.target.pk}, self.admin)
        CopyTask.objects.filter(pk=task.pk).update(updated_at=timezone.now() + timedelta(minutes=5))
        page = self.page()
        card = page.locator(".copy-task")
        card.get_by_role("button", name="详情").click()
        expect(card.locator(".copy-files")).to_be_visible()
        expect(card.locator(".copy-failure")).to_have_count(0)
        CopyFailure.objects.create(task=task, path="/notes.txt", size=1, message="写入目标失败：连接断了", attempts=4)
        CopyTask.objects.filter(pk=task.pk).update(status=CopyTask.Status.FAILED, finished_at=timezone.now(),
                                                   files_total=1, files_failed=1, scanning=False,
                                                   message="1 个文件没有复制成功。")
        expect(card.locator(".badge")).to_have_text("失败")
        expect(card.locator(".copy-failure")).to_contain_text("写入目标失败：连接断了")
        expect(card.locator(".copy-failure")).to_contain_text("试了 4 次")
        expect(card.locator(".copy-files")).to_be_hidden()
        self.assertClean(page)

    def test_in_english(self):
        service.create({**options(), "source": self.source.pk, "target": self.target.pk}, self.admin)
        page = self.page(language="en")
        card = page.locator(".copy-task")
        expect(card.locator(".badge")).to_have_text("Done")
        expect(card.locator(".copy-task-line")).to_contain_text("3 of 3 files")
        self.assertClean(page)
