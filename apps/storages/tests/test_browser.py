"""The storage sources' part of the manage page (static/storages/js/manage_storages.js) in a real browser. The
browser tests' base: apps/web/test_browser.py."""

import io
import shutil
import tempfile
from pathlib import Path

from PIL import Image

from apps.accounts.models import User
from apps.files.tests.helpers import EVERYTHING
from apps.web.test_browser import BrowserTestCase, expect

from .. import config, instances
from ..drivers import driver_class
from ..models import Grant, StorageSource
from .fake_cf_imgbed import FakeImgBed


class StorageWizardTests(BrowserTestCase):
    def test_adding_a_local_storage(self):
        # The four steps, as an admin does them: name and alias, the folder, the connection test (it runs by
        # itself), then 完成 with the defaults, which turns it on. Browsed afterwards.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        folder = Path(tempfile.mkdtemp(prefix="hfile-browser-"))
        self.addCleanup(shutil.rmtree, folder, ignore_errors=True)
        (folder / "hello.txt").write_bytes(b"hello\n")
        instances.close_all()  # a storage of an earlier test may have had this one's id
        self.addCleanup(instances.close_all)
        page = self.open(self.new_context(user=admin), "/manage/#storages/new")
        wizard = page.locator("form.setup-form")
        wizard.get_by_role("textbox", name="名称", exact=True).fill("Photos")
        wizard.get_by_role("textbox", name="别名", exact=True).fill("photos")
        wizard.get_by_role("button", name="下一步").click()
        wizard.get_by_role("textbox", name="文件夹", exact=True).fill(str(folder))
        wizard.get_by_role("button", name="保存").click()
        expect(wizard.locator(".form-test-result")).to_have_text("连接正常。")
        wizard.get_by_role("button", name="下一步").click()
        wizard.get_by_role("button", name="完成").click()
        expect(wizard.get_by_role("heading", name="「Photos」添加好了")).to_be_visible()
        storage = StorageSource.objects.get(key="photos")
        self.assertEqual((storage.name, storage.type, storage.enabled), ("Photos", "local", True))
        self.assertClean(page)
        page.goto(f"{self.live_server_url}/photos/")
        expect(self.row(page, "hello.txt")).to_be_visible()  # a new storage is granted by default permissions (ADMIN_DEFAULTS)
        self.assertClean(page)


class RuleTesterTests(BrowserTestCase):
    def test_a_wrong_root(self):
        # API-15: the rule test's 用户的根目录 has errors of its own, and says they are its. A wrong root was
        # answered as the path's error, and read as if the path to test were wrong.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({})
        page = self.open(self.new_context(user=admin), f"/manage/#storages/{StorageSource.objects.get().pk}/readmes")
        page.get_by_label("测试路径", exact=True).fill("/music")
        page.get_by_label("用户的根目录", exact=True).fill("/a/../b")
        expect(page.locator(".tester-result [role=alert]")).to_have_text("用户的根目录：路径里不能有 . 或 .. 这样只有点和空格的一段。")
        self.assertClean(page, allow=[r"^400 POST .*/rules/test/$", r"^console: Failed to load resource: .* 400"])


class ImgBedPictureTests(BrowserTestCase):
    def test_a_picture_of_the_external_channel(self):
        """ImgBed redirects a file of its External channel to the link it keeps, on a host of its own (here another
        port, so another origin): the viewer shows it as it shows a file ImgBed holds."""
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        bed, elsewhere = FakeImgBed().start(), FakeImgBed().start()
        self.addCleanup(bed.close)
        self.addCleanup(elsewhere.close)
        picture = io.BytesIO()
        Image.new("RGB", (40, 30), "red").save(picture, "PNG")
        bed.put("held.png", picture.getvalue(), FileType="image/png")
        elsewhere.external["linked.png"] = picture.getvalue()
        bed.put("linked.png", b"", Channel="External", ChannelName="External", FileSizeBytes=0, FileSize="0.00",
                ExternalLink=f"{elsewhere.url}/external/linked.png")
        instances.close_all()  # a storage of an earlier test may have had this one's id
        self.addCleanup(instances.close_all)
        settings = {"url": bed.url, "token": bed.add_token("list", "upload", "delete", "manage"), "base_path": "/",
                    "channel": "telegram"}
        storage = StorageSource.objects.create(key="bed", name="Bed", type="cf-imgbed",
                                               config=config.encode(driver_class("cf-imgbed"), settings))
        Grant.objects.create(user=admin, storage=storage, permissions=EVERYTHING, root_path="/", enabled=True)
        page = self.open(self.new_context(user=admin), "/bed/")
        for name in ("held.png", "linked.png"):
            with self.subTest(name):
                self.row(page, name).dblclick()
                shown = page.get_by_role("dialog", name=name).locator("img.piv-img")
                expect(shown).to_be_visible()
                page.wait_for_function("image => image.complete && image.naturalWidth === 40", arg=shown.element_handle())
                page.keyboard.press("Escape")
        self.assertClean(page)
