"""The storage sources' part of the manage page (static/storages/js/manage_storages.js) in a real browser. The
browser tests' base: apps/web/test_browser.py."""

import io
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image

from apps.accounts.models import User
from apps.activity.log import Kind
from apps.activity.models import ActivityLog
from apps.files.tests.helpers import EVERYTHING
from apps.web.test_browser import BrowserTestCase, expect

from .. import config, instances
from ..drivers import driver_class
from ..models import Grant, StorageSource
from .fake_cf_imgbed import FakeImgBed
from .fakes import FakeDriver, FakeTypeMixin


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
        wizard.get_by_role("radio", name="照片", exact=True).check()  # 其他设置: the icon and the sidebar too
        wizard.get_by_role("switch", name="显示在左侧栏").check()
        wizard.get_by_role("button", name="完成").click()
        expect(wizard.get_by_role("heading", name="「Photos」添加好了")).to_be_visible()
        expect(page.locator(".nav-storages").get_by_role("link", name="Photos")).to_be_visible()  # in the sidebar already
        storage = StorageSource.objects.get(key="photos")
        self.assertEqual((storage.name, storage.type, storage.enabled), ("Photos", "local", True))
        self.assertEqual((storage.icon, storage.show_in_nav), ("photo_library", True))
        self.assertClean(page)
        page.goto(f"{self.live_server_url}/photos/")
        expect(self.row(page, "hello.txt")).to_be_visible()  # a new storage is granted by default permissions (ADMIN_DEFAULTS)
        self.assertClean(page)


class IconAndSidebarTests(BrowserTestCase):
    def test_chosen_in_the_form_shown_in_the_sidebar(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"a.txt": b"a"}, users=[admin])
        storage = StorageSource.objects.get()
        context = self.new_context(user=admin)
        page = self.open(context, f"/manage/#storages/{storage.pk}")
        picker = page.locator("fieldset.icon-picker")
        expect(picker.get_by_role("radio", name="存储", exact=True)).to_be_checked()  # the default
        picker.get_by_role("radio", name="照片", exact=True).check()
        page.get_by_role("switch", name="显示在左侧栏").check()
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".storage-row", has_text="Disk").locator(".list-leading use")).to_have_attribute("href", re.compile(r"#i-photo_library$"))
        storage.refresh_from_db()
        self.assertEqual((storage.icon, storage.show_in_nav), ("photo_library", True))
        self.assertClean(page)
        # In the sidebar on every page; in the storage it is the current destination, its icon filled.
        link = page.locator(".nav-storages").get_by_role("link", name="Disk")
        page.goto(f"{self.live_server_url}/shares/")
        expect(link).to_be_visible()
        link.click()
        expect(self.row(page, "a.txt")).to_be_visible()
        expect(link).to_have_attribute("aria-current", "page")
        expect(link.locator("svg.on use")).to_have_attribute("href", re.compile(r"#i-photo_library-fill$"))
        expect(page.locator(".nav > .nav-link[aria-current]")).to_have_count(0)  # not 首页
        expect(page.locator("#files-storage-button use").first).to_have_attribute("href", re.compile(r"#i-photo_library$"))
        self.assertClean(page)

    def test_the_picker(self):
        # The chosen icon is filled; the arrow keys move and choose, the icon's name in the tooltip. A stored icon
        # this program doesn't offer (a later version's) shows as storage, and saving leaves it as it is.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"a.txt": b"a"}, users=[admin])
        storage = StorageSource.objects.get()
        page = self.open(self.new_context(user=admin), f"/manage/#storages/{storage.pk}")
        picker = page.locator("fieldset.icon-picker")
        default = picker.locator(".icon-choice").first
        expect(default.get_by_role("radio", name="存储", exact=True)).to_be_checked()
        expect(default.locator(".icon.on")).to_be_visible()
        expect(default.locator(".icon.off")).to_be_hidden()
        default.get_by_role("radio").press("ArrowRight")
        cloud = picker.get_by_role("radio", name="云", exact=True)
        expect(cloud).to_be_checked()
        expect(cloud).to_be_focused()
        expect(page.locator("#tooltip")).to_be_visible()
        expect(page.locator("#tooltip")).to_have_text("云")
        expect(default.locator(".icon.off")).to_be_visible()
        self.assertClean(page)
        StorageSource.objects.filter(pk=storage.pk).update(icon="gone")
        page.reload()
        expect(default.get_by_role("radio")).to_be_checked()
        page.get_by_role("textbox", name="备注").fill("改了备注")
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".storage-row", has_text="Disk")).to_be_visible()
        storage.refresh_from_db()
        self.assertEqual((storage.icon, storage.remark), ("gone", "改了备注"))
        self.assertEqual(ActivityLog.objects.filter(kind=Kind.UPDATE_STORAGE).latest("id").detail["fields"], ["remark"])
        self.assertClean(page)

    def test_the_manage_page_keeps_it_up_to_date(self):
        # /manage/ doesn't load again after a change there: its sidebar follows the storages all the same, and
        # one's own permissions on them.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"a.txt": b"a"}, users=[admin])
        self.local_storage({"b.txt": b"b"}, users=[admin], key="photos")
        page = self.open(self.new_context(user=admin), "/manage/#storages")
        nav = page.locator("nav.nav")
        expect(nav.locator(".nav-storages")).to_have_count(0)
        page.locator(".storage-row", has_text="Photos").locator("a.storage-main").click()
        page.get_by_role("switch", name="显示在左侧栏").check()
        page.get_by_role("button", name="保存").click()
        expect(nav.get_by_role("link", name="Photos")).to_be_visible()
        page.get_by_role("button", name="更多操作：Photos").click()
        page.get_by_role("menuitem", name="停用").click()
        page.get_by_role("alertdialog").get_by_role("button", name="停用").click()
        expect(nav.locator(".nav-storages")).to_have_count(0)
        self.assertClean(page)
        StorageSource.objects.update(enabled=True)
        page.goto(f"{self.live_server_url}/manage/#users")
        page.reload()  # only the hash changed: load the page again, its sidebar with Photos
        expect(nav.get_by_role("link", name="Photos")).to_be_visible()
        page.locator("tr", has_text="boss").get_by_role("button", name="编辑").click()
        dialog = page.locator("#user-dialog")
        dialog.get_by_role("tab", name="存储源权限").click()
        dialog.get_by_role("switch", name="允许使用「Photos」").uncheck()
        dialog.get_by_role("button", name="保存").click()
        expect(page.locator("#snackbar .snackbar-text")).to_have_text("存储源权限已保存")
        expect(nav.locator(".nav-storages")).to_have_count(0)
        self.assertClean(page)

    def test_more_than_the_rail_holds(self):
        # Taller than the window, the rail scrolls, and shows it does: its scrollbar isn't hidden.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        for n in range(8):
            self.local_storage({"a.txt": b"a"}, users=[admin], key=f"disk{n}")
        StorageSource.objects.update(show_in_nav=True)
        page = self.open(self.new_context(user=admin, height=600), "/shares/")
        nav = page.locator("nav.nav")
        self.assertGreater(nav.evaluate("(el) => el.scrollHeight - el.clientHeight"), 0)
        self.assertEqual(nav.evaluate("(el) => getComputedStyle(el).scrollbarWidth"), "thin")
        last = page.locator(".nav-storages").get_by_role("link", name="Disk7")
        last.scroll_into_view_if_needed()
        expect(last).to_be_in_viewport()
        self.assertClean(page)

    def test_not_on_phones(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"a.txt": b"a"}, users=[admin])
        StorageSource.objects.update(show_in_nav=True)
        page = self.open(self.new_context(user=admin, width=390, height=800), "/disk/")
        expect(self.row(page, "a.txt")).to_be_visible()
        expect(page.locator(".nav > .nav-link").first).to_be_visible()  # the bar at the bottom, without the storage
        expect(page.locator(".nav-storages")).to_be_hidden()
        home = page.locator('.nav > .nav-link[href="/"]')  # in it, 首页 is current, as in any storage
        expect(home.locator(".icon.on")).to_be_visible()
        expect(home.locator(".icon.off")).to_be_hidden()
        expect(home).to_have_attribute("aria-current", "page")  # for screen readers too
        page.set_viewport_size({"width": 1280, "height": 800})  # wider: the storage is, 首页 isn't
        expect(page.locator(".nav-storages").get_by_role("link", name="Disk")).to_have_attribute("aria-current", "page")
        expect(home).not_to_have_attribute("aria-current", "page")
        expect(home.locator(".icon.off")).to_be_visible()
        self.assertClean(page)
        # A visitor with only 首页 has no bar, a storage of the sidebar or not.
        Grant.objects.update_or_create(user=User.objects.guest(), storage=StorageSource.objects.get(),
                                       defaults={"permissions": ["download"], "root_path": "/", "enabled": True})
        page = self.open(self.new_context(width=390, height=800), "/disk/")
        expect(self.row(page, "a.txt")).to_be_visible()
        expect(page.locator(".nav > .nav-link")).to_be_hidden()
        expect(page.locator(".nav-storages")).to_be_hidden()
        self.assertClean(page)


class AuthorizationTests(FakeTypeMixin, BrowserTestCase):
    def test_back_in_the_same_tab(self):
        # A blocked popup: 授权 takes this page to the provider and back (the callback page's link). What the form
        # had and wasn't saved yet comes back with the token: the icon, the sidebar switch, the remark.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        storage = StorageSource.objects.create(key="od", name="OD", type="fake", config=config.encode(
            FakeDriver, {"endpoint": "https://fake.example", "port": 21, "passive": True, "mode": "a"}))
        Grant.objects.create(user=admin, storage=storage, permissions=EVERYTHING, root_path="/", enabled=True)
        context = self.new_context(user=admin)
        context.add_init_script("window.open = () => null;")

        def provider(route):  # the consent page, agreed to at once
            state = parse_qs(urlparse(route.request.url).query)["state"][0]
            route.fulfill(status=302, headers={"Location": f"{self.live_server_url}/storages/oauth/callback/?code=good-code&state={state}"})

        context.route("https://login.fake.example/**", provider)
        page = self.open(context, f"/manage/#storages/{storage.pk}")
        page.locator("fieldset.icon-picker").get_by_role("radio", name="照片", exact=True).check()
        page.get_by_role("switch", name="显示在左侧栏").check()
        page.get_by_role("textbox", name="备注").fill("授权前写的")
        page.get_by_role("button", name="授权").click()
        page.get_by_role("link", name="回到存储源设置").click()
        expect(page.locator(".notice", has_text="授权的结果已经填进表单")).to_be_visible()
        expect(page.locator("fieldset.icon-picker").get_by_role("radio", name="照片", exact=True)).to_be_checked()
        expect(page.get_by_role("switch", name="显示在左侧栏")).to_be_checked()
        expect(page.get_by_role("textbox", name="备注")).to_have_value("授权前写的")
        page.get_by_role("button", name="保存").click()
        expect(page.locator(".storage-row", has_text="OD")).to_be_visible()
        storage.refresh_from_db()
        self.assertEqual((storage.icon, storage.show_in_nav, storage.remark), ("photo_library", True, "授权前写的"))
        self.assertEqual(config.decode(FakeDriver, storage.config)[0]["refresh_token"], "oauth-refresh-for-https://fake.example")
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
