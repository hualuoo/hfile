"""Direct and short links (static/links/js/links.js) in a real browser: made in the file browser, then followed.
The browser tests' base: apps/web/test_browser.py."""

import re

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase, expect


class LinkTests(BrowserTestCase):
    def test_both_links_of_a_file(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"docs/a.txt": b"hello\n"}, users=[admin])
        page = self.open(self.new_context(user=admin), "/disk/docs/")
        self.file_action(page, "a.txt", "同时获取")
        page.get_by_role("dialog", name="生成短链").get_by_role("button", name="确定").click()
        result = page.get_by_role("dialog", name="直链")
        direct = result.get_by_role("button", name="复制直链").inner_text()
        short = result.get_by_role("button", name="复制短链").inner_text()
        self.assertRegex(direct, rf"^{re.escape(self.live_server_url)}/.+/a\.txt$")
        self.assertRegex(short, rf"^{re.escape(self.live_server_url)}/s/[A-Za-z0-9]+$")
        expect(result.get_by_role("img", name="文件二维码")).to_be_visible()
        self.assertClean(page)
        # The direct link as the user who made it (by default it takes the download permission: path_link_anonymous
        # is off), the short link as anyone.
        answer = page.context.request.get(direct)
        self.assertEqual((answer.status, answer.body()), (200, b"hello\n"))
        answer = self.new_context().request.get(short)
        self.assertEqual((answer.status, answer.body()), (200, b"hello\n"))
