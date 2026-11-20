"""The viewers in a real browser: their self-test (/components/preview/selftest/, static/preview/js/selftest.js),
the parts of them only a browser can run, and the text editor in the file browser. The browser tests' base:
apps/web/test_browser.py."""

from django.test import override_settings

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase, expect


@override_settings(DEBUG=True)  # the page is there with DEBUG only (apps/preview/dev.py)
class SelfTestTests(BrowserTestCase):
    def test_the_viewers_self_test(self):
        page = self.open(self.new_context(), "/components/preview/selftest/")
        page.wait_for_function("document.getElementById('selftest-result').dataset.status !== 'running'")
        result = page.locator("#selftest-result")
        self.assertEqual(result.get_attribute("data-status"), "pass", result.text_content())
        self.assertClean(page)


class TextEditorTests(BrowserTestCase):
    def test_text_with_nul_characters(self):
        # FILE-03: UTF-16 without a byte-order mark reads as UTF-8 with NUL between the letters, which the server
        # doesn't save. Editing it asks first, and saving it anyway says where the first NUL is.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        data = "hello\n".encode("utf-16-le")
        root = self.local_storage({"utf16.txt": data}, users=[admin])
        page = self.open(self.new_context(user=admin), "/disk/")
        page.locator(".files-item", has_text="utf16.txt").dblclick()
        expect(page.locator(".pt-foot")).to_contain_text("UTF-8")  # read
        read_only = page.get_by_role("switch", name="只读")
        warning = page.get_by_role("alertdialog", name="有 NUL 字符")
        read_only.click()
        warning.get_by_role("button", name="取消").click()
        expect(read_only).to_be_checked()
        expect(page.locator(".pt-editor")).to_have_count(0)
        read_only.click()
        warning.get_by_role("button", name="仍然编辑").click()
        expect(page.locator(".pt-editor")).to_be_editable()
        page.get_by_role("button", name="保存").click()
        expect(page.locator("#snackbar .snackbar-text")).to_have_text("保存失败：第 1 行第 2 个字符是 NUL（U+0000）：在线编辑不能保存带 NUL 字符的文本，请先删掉它。")
        self.assertEqual((root / "utf16.txt").read_bytes(), data)
        self.assertClean(page, allow=[r"^400 PUT .*/api/v1/uploads/text/$", r"^console: Failed to load resource: .* 400"])
