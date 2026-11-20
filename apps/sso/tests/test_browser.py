"""The single sign-on part of the manage page (static/sso/js/manage_sso.js) in a real browser. The browser tests'
base: apps/web/test_browser.py."""

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase, expect

from ..models import Provider


class ProviderDialogTests(BrowserTestCase):
    def test_clearing_the_saved_secret(self):
        # ACCT-05: the saved Client Secret can be cleared, the provider is a public client from then on. The box
        # for it is there while a secret is saved; ticked, there is nothing to type.
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        provider = Provider(code="gitlab", name="GitLab", client_id="cid", auth_url="https://gitlab.example.com/oauth/authorize",
                            token_url="https://gitlab.example.com/oauth/token", userinfo_url="https://gitlab.example.com/oauth/userinfo")
        provider.set_secret("s3cret-value")
        provider.save()
        page = self.open(self.new_context(user=admin), "/manage/#sso")
        dialog = page.get_by_role("dialog", name="编辑单点登录「GitLab」")
        clear = dialog.get_by_role("checkbox", name="清除已保存的 Client Secret（改成公开客户端）")
        secret = dialog.locator("#sso-client-secret")
        page.get_by_role("button", name="编辑").click()
        clear.check()
        expect(secret).to_be_disabled()
        dialog.get_by_role("button", name="保存").click()
        expect(page.locator("#snackbar .snackbar-text")).to_have_text("已保存 GitLab。")
        provider.refresh_from_db()
        self.assertEqual(provider.secret(), "")
        page.get_by_role("button", name="编辑").click()
        expect(secret).to_be_enabled()
        expect(clear).to_be_hidden()
        self.assertClean(page)
