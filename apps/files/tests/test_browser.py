"""The file browser (static/files/js/files.js) in a real browser: what a user does with the folders and files of a
local storage, from its menus and dialogs. The browser tests' base: apps/web/test_browser.py."""

import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase, expect


def tree(root: Path) -> list[str]:
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


class FileBrowserTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        picture = io.BytesIO()
        Image.new("RGB", (40, 30), "red").save(picture, "PNG")
        self.root = self.local_storage({"docs/README.md": b"# Docs\n\nSome *text*.\n", "docs/a.txt": b"hello\n",
                                        "red.png": picture.getvalue(), "top.txt": b"top\n"}, users=[self.admin])
        self.page = self.open(self.new_context(user=self.admin), "/disk/")
        self.toast = self.page.locator("#snackbar .snackbar-text")

    def choose_folder(self, title: str, name: str):
        """In the folder picker of 移动 or 复制 (`title`), opens the folder `name` and chooses it."""
        picker = self.page.get_by_role("dialog", name=title)
        picker.get_by_role("option", name=name, exact=True).click()
        expect(picker.locator(".files-picker-crumbs [aria-current=page]")).to_have_text(name)
        picker.get_by_role("button", name="确认").click()

    def test_folders_and_files(self):
        page, root = self.page, self.root
        page.click("#files-upload-button")
        page.click("#files-upload-menu [data-files-action=new_folder]")
        dialog = page.get_by_role("dialog", name="新建文件夹")
        dialog.get_by_label("文件夹名称").fill("new")
        dialog.get_by_role("button", name="创建").click()
        expect(self.row(page, "new")).to_be_visible()
        self.file_action(page, "top.txt", "重命名")
        dialog = page.get_by_role("dialog", name="重命名")
        name = dialog.get_by_label("新名称")
        # It selects "top" a frame after opening; typed before that, "renamed.txt" would take only "top"'s place.
        expect(name).to_have_js_property("selectionEnd", 3)
        name.fill("renamed.txt")
        dialog.get_by_role("button", name="重命名").click()
        expect(self.row(page, "renamed.txt")).to_be_visible()
        self.file_action(page, "renamed.txt", "移动")
        self.choose_folder("移动到", "new")
        expect(self.row(page, "renamed.txt")).to_have_count(0)
        self.file_action(page, "docs", "复制")
        self.choose_folder("复制到", "new")
        expect(self.toast).to_have_text("复制成功")
        self.assertEqual(tree(root), ["docs", "docs/README.md", "docs/a.txt", "new", "new/docs", "new/docs/README.md",
                                      "new/docs/a.txt", "new/renamed.txt", "red.png"])
        self.assertEqual((root / "new/renamed.txt").read_bytes(), b"top\n")
        self.file_action(page, "new", "删除 (1)")
        page.get_by_role("alertdialog", name="删除").get_by_role("button", name="删除").click()
        expect(self.row(page, "new")).to_have_count(0)
        self.assertEqual(tree(root), ["docs", "docs/README.md", "docs/a.txt", "red.png"])
        self.assertClean(page)

    def test_uploading_files(self):
        chosen = Path(tempfile.mkdtemp(prefix="hfile-chosen-"))
        self.addCleanup(shutil.rmtree, chosen, ignore_errors=True)
        (chosen / "one.txt").write_bytes(b"one\n")
        (chosen / "two.txt").write_bytes(b"two\n")
        page = self.page
        page.click("#files-upload-button")
        with page.expect_file_chooser() as chooser:
            page.click("#files-upload-menu [data-files-action=upload_file]")
        chooser.value.set_files([chosen / "one.txt", chosen / "two.txt"])
        expect(self.toast).to_have_text("上传成功：2 个文件")
        expect(self.row(page, "one.txt")).to_be_visible()  # the list shows what landed in it
        expect(self.row(page, "two.txt")).to_be_visible()
        self.assertEqual(((self.root / "one.txt").read_bytes(), (self.root / "two.txt").read_bytes()), (b"one\n", b"two\n"))
        self.assertClean(page)

    def test_previews_and_a_download(self):
        page = self.page
        self.row(page, "red.png").dblclick()
        picture = page.get_by_role("dialog", name="red.png").locator("img.piv-img")
        expect(picture).to_be_visible()
        page.wait_for_function("image => image.complete && image.naturalWidth === 40", arg=picture.element_handle())
        page.keyboard.press("Escape")
        self.row(page, "docs").dblclick()
        self.row(page, "README.md").dblclick()
        expect(page.get_by_role("dialog", name="README.md").get_by_role("heading", name="Docs")).to_be_visible()
        page.keyboard.press("Escape")
        self.file_action(page, "a.txt", "下载")
        with page.expect_download() as download:
            page.get_by_role("alertdialog", name="下载").get_by_role("button", name="下载").click()
        self.assertEqual(download.value.suggested_filename, "a.txt")
        self.assertEqual(Path(download.value.path()).read_bytes(), b"hello\n")
        self.assertClean(page)
