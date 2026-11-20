"""Shares (static/shares/js/create.js, share.js) in a real browser: a user makes one in the file browser, and a
visitor without an account opens it with its password. The browser tests' base: apps/web/test_browser.py."""

import re
from urllib.parse import urlsplit

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase, expect


class ShareTests(BrowserTestCase):
    def test_a_shared_folder_with_a_password(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"docs/a.txt": b"hello\n", "top.txt": b"top\n"}, users=[admin])
        page = self.open(self.new_context(user=admin), "/disk/")
        self.file_action(page, "docs", "创建分享")
        dialog = page.get_by_role("dialog", name="创建分享链接")
        dialog.get_by_label("访问密码").fill("pass1234")
        dialog.get_by_role("button", name="创建分享").click()
        url = dialog.locator("#share-result-url")
        expect(url).to_have_value(re.compile(rf"^{re.escape(self.live_server_url)}/share/[A-Za-z0-9_-]+$"))
        expect(dialog.locator("#share-result-password")).to_have_value("pass1234")
        self.assertClean(page)

        visitor = self.open(self.new_context(), urlsplit(url.input_value()).path)
        password = visitor.get_by_label("访问密码")
        password.fill("wrong")
        visitor.get_by_role("button", name="访问").click()
        expect(visitor.locator("#share-password-form [role=alert]")).to_have_text("密码错误，请重新输入")
        password.fill("pass1234")
        visitor.get_by_role("button", name="访问").click()
        expect(self.row(visitor, "docs")).to_be_visible()  # the page again, with the share's cookie: what it shares
        expect(self.row(visitor, "top.txt")).to_have_count(0)
        self.row(visitor, "docs").dblclick()
        self.row(visitor, "a.txt").dblclick()
        expect(visitor.get_by_role("dialog", name="a.txt").locator("code")).to_have_text("hello\n")
        self.assertClean(visitor, allow=[r"^403 POST .*/verify/$", r"^console: Failed to load resource: .* 403"])
