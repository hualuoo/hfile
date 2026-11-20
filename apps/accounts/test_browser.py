"""Accounts in a real browser: logging in and out (the login page, static/web/js/shell.js and
static/accounts/js/login_steps.js), the two-step verification dialog (static/accounts/js/totp.js), and making a
user on the manage page (static/accounts/js/manage_users.js, with the storages app's grants editor). The browser
tests' base: apps/web/test_browser.py."""

import re

from apps.core import secrets
from apps.siteconf.models import SiteSettings
from apps.storages.models import Grant
from apps.web.test_browser import BrowserTestCase, expect

from . import totp
from .models import User


class LoginTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def log_in(self, page, username, password):
        """Fills the login page's form on `page` and sends it."""
        page.get_by_label("用户名").fill(username)
        page.get_by_label("密码", exact=True).fill(password)
        page.get_by_role("button", name="登录", exact=True).click()

    def test_logging_in_and_out(self):
        page = self.open(self.new_context(), "/login/")
        self.log_in(page, "alice", "not-her-password")
        expect(page.locator("#login-form [role=alert]")).to_have_text("用户名或密码不对。")
        self.log_in(page, "alice", "pw-alice-2026")
        page.wait_for_url(f"{self.live_server_url}/")
        account = page.get_by_role("button", name="账号：alice")
        account.click()
        page.get_by_role("menuitem", name="退出").click()
        expect(account).to_have_count(0)
        expect(page.get_by_role("button", name="登录", exact=True)).to_be_visible()
        self.assertClean(page, allow=[r"^400 POST .*/api/v1/session/$", r"^console: Failed to load resource: .* 400"])

    def test_two_step_verification(self):
        secret = totp.new_secret()
        self.alice.totp_secret = secrets.encrypt(secret)
        self.alice.save(update_fields=["totp_secret"])
        page = self.open(self.new_context(), "/login/")
        self.log_in(page, "alice", "pw-alice-2026")
        page.get_by_label("两步验证码").fill(totp.code_at(secret, totp.current_step()))
        page.get_by_role("button", name="验证", exact=True).click()
        page.wait_for_url(f"{self.live_server_url}/")
        expect(page.get_by_role("button", name="账号：alice")).to_be_visible()
        self.assertClean(page)


class TotpSetupTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")

    def set_up(self):
        """The page with the dialog of the user menu's 两步验证 (?auth=totp opens it), after 开始设置: the page
        and the dialog, which shows the key."""
        page = self.open(self.new_context(user=self.admin), "/?auth=totp")
        dialog = page.get_by_role("dialog", name="两步验证")
        dialog.get_by_role("button", name="开始设置").click()
        expect(dialog.locator(".totp-secret code")).to_have_text(re.compile(r"^[A-Z2-7]{4}( [A-Z2-7]{1,4})+$"))
        return page, dialog

    def test_a_qr_code(self):
        page, dialog = self.set_up()
        expect(dialog.get_by_role("img", name="两步验证的二维码")).to_be_visible()
        expect(dialog).to_contain_text("1. 用验证器 App 扫这个二维码；")
        self.assertClean(page)

    def test_names_too_long_for_a_qr_code(self):
        # ACCT-03: 30 and 150 characters beyond U+FFFF, 12 bytes each in the address, don't fit in a QR code. The
        # dialog says to type the key in, without a picture; it said to scan one, and showed none.
        site = SiteSettings.load()
        site.site_name = chr(0x20000) * 30
        site.save()
        self.admin.username = chr(0x20000) * 150
        self.admin.save(update_fields=["username"])
        page, dialog = self.set_up()
        expect(dialog).to_contain_text("1. 站点名称和用户名太长，二维码装不下：在验证器 App 里手动输入下面的密钥")
        expect(dialog.locator("img")).to_have_count(0)
        self.assertClean(page)


class ManageUsersTests(BrowserTestCase):
    def test_a_new_user_with_a_storage(self):
        # Made with every default permission (what new storage sources give them), allowed on the storage source
        # with those, and logged in as.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"docs/a.txt": b"hello\n"})
        page = self.open(self.new_context(user=admin), "/manage/#users")
        page.get_by_role("button", name="新建用户").click()
        dialog = page.locator("#user-dialog")
        dialog.get_by_label("用户名").fill("alice")
        dialog.get_by_label("密码", exact=True).fill("pw-alice-2026")
        dialog.get_by_role("tab", name="默认权限").click()
        dialog.get_by_role("button", name="全选").click()
        dialog.get_by_role("button", name="保存").click()
        expect(dialog.get_by_role("heading", name="编辑用户「alice」")).to_be_visible()  # saved, and on to its storages
        dialog.get_by_role("switch", name="允许使用「Disk」").check()
        dialog.get_by_role("button", name="修改「Disk」的权限").click()
        permissions = page.get_by_role("dialog", name="「Disk」的权限")
        permissions.get_by_role("button", name="用这个用户的默认权限").click()
        permissions.get_by_role("button", name="确定").click()
        dialog.get_by_role("button", name="保存").click()
        expect(page.locator("#snackbar .snackbar-text")).to_have_text("存储源权限已保存")
        grant = Grant.objects.get(user__username="alice")
        self.assertEqual((grant.enabled, grant.root_path), (True, "/"))
        self.assertEqual(sorted(grant.permissions), sorted(User.objects.get(username="alice").default_permissions))
        self.assertIn("download", grant.permissions)
        self.assertClean(page)

        alice = self.open(self.new_context(), "/login/?next=/disk/docs/")
        alice.get_by_label("用户名").fill("alice")
        alice.get_by_label("密码", exact=True).fill("pw-alice-2026")
        alice.get_by_role("button", name="登录", exact=True).click()
        alice.wait_for_url(f"{self.live_server_url}/disk/docs/")
        expect(self.row(alice, "a.txt")).to_be_visible()
        self.assertClean(alice)
