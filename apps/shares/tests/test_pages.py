"""The pages: /share/<key> (public, the password form or the browser, the CSP), /shares/ (我的分享), the
admin section, the create action on the browser's pages, the reserved names and the access log's mask."""

from __future__ import annotations

import html
import json
import re

from django.test import Client

from apps.accounts.models import User
from apps.core import masking, reserved
from apps.files import transfer
from apps.siteconf.models import SiteSettings
from apps.storages import config
from apps.storages.drivers.s3 import S3Driver
from apps.storages.models import StorageSource

from .helpers import EVERYTHING, FOLDER, SharesTestCase


def page_config(response):
    match = re.search(r'data-config="([^"]*)"', response.content.decode())
    return json.loads(html.unescape(match.group(1)))


def csp(response):
    return dict(part.split(" ", 1) for part in response["Content-Security-Policy"].split("; "))


class SharePageTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.browser = Client()

    def strict(self, response):
        page = response.content.decode()
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")  # no inline scripts
        self.assertNotRegex(page, r"\sstyle=")
        self.assertNotRegex(page, r"\son[a-z]+=")
        self.assertEqual(csp(response)["script-src"], "'self'")
        return page

    def test_before_the_password(self):
        share = self.files_share(password="abcd")
        response = self.browser.get(f"/share/{share.key}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("no-cache", response["Cache-Control"])
        page = self.strict(response)
        self.assertIn('id="share-password-form"', page)
        self.assertIn("此分享受密码保护，请输入访问密码", page)
        self.assertNotIn("files/js/files.js", page)  # no browser yet
        self.assertIn("Powered by HFile", page)  # a public page: the footer
        data = page_config(response)
        self.assertEqual((data["storage"], data["path"], data["info"]["verified"]), (None, "/", False))
        for secret in ("docs", "top.txt", '"disk"', "abcd"):
            self.assertNotIn(secret, page)

    def test_after_the_password_the_browser(self):
        share = self.files_share(password="abcd")
        self.verify(share, "abcd", client=None)
        self.browser.cookies = self.visitor.cookies
        response = self.browser.get(f"/share/{share.key}/", {"path": "/docs"})
        page = self.strict(response)
        self.assertIn("files/js/files.js", page)
        self.assertIn("shares/js/share.js", page)
        self.assertLess(page.index("files/js/files.js"), page.index("shares/js/share.js"))
        self.assertIn("shares/js/create.js", page)  # files/_assets.html: what other apps add
        data = page_config(response)
        self.assertEqual((data["storage"]["key"], data["path"], data["page"]), ("disk", "/docs", f"/share/{share.key}"))
        self.assertEqual(data["info"]["entries"][0]["name"], "docs")

    def test_without_a_password(self):
        share = self.files_share()
        page = self.strict(self.browser.get(f"/share/{share.key}"))
        self.assertIn('id="share-files"', page)
        self.assertNotIn('id="share-password-form"', page)

    def test_unknown_expired_and_bad_paths(self):
        response = self.browser.get("/share/nothing1")
        self.assertEqual(response.status_code, 404)
        self.assertIn("分享链接不可用", response.content.decode())
        self.assertEqual(self.browser.get("/share/a.b").status_code, 404)
        share = self.files_share()
        self.assertEqual(page_config(self.browser.get(f"/share/{share.key}", {"path": "/../x"}))["path"], "/")

    def test_the_csp_lets_in_where_a_bucket_serves_its_files(self):
        # apps.files.transfer keeps each storage's sources by its id and config revision, which a rolled-back
        # test hands to the next one's storage: forget them around this one.
        transfer._sources.clear()
        self.addCleanup(transfer._sources.clear)
        bucket = StorageSource.objects.create(
            key="bucket", name="桶", type="s3",
            config=config.encode(S3Driver, {"bucket": "media", "region": "us-east-1", "endpoint": "https://s3.example.com",
                                            "access_key": "AKIDEXAMPLE", "secret_key": "secret", "path_style": "path"}))
        self.grant(self.alice, EVERYTHING, storage=bucket)
        share = self.share((("pictures", FOLDER),), storage=bucket, password="abcd")
        locked = csp(self.browser.get(f"/share/{share.key}"))
        self.assertNotIn("s3.example.com", locked.get("img-src", ""))  # nothing of the storage before the password
        self.verify(share, "abcd")
        self.browser.cookies = self.visitor.cookies
        policy = csp(self.browser.get(f"/share/{share.key}"))
        self.assertIn("https://s3.example.com", policy["img-src"])
        self.assertIn("https://s3.example.com", policy["media-src"])

    def test_custom_code_and_announcement(self):
        site = SiteSettings.load()
        site.announcement = "公告的内容"
        site.custom_css = "body{}"
        site.save()
        share = self.files_share()
        page = self.browser.get(f"/share/{share.key}").content.decode()
        self.assertIn("公告的内容", page)
        self.assertIn("/site/custom.css", page)


class MySharesPageTests(SharesTestCase):
    def test_needs_a_login(self):
        response = Client().get("/shares/")
        self.assertRedirects(response, "/?auth=login&next=/shares/", fetch_redirect_response=False)

    def test_the_page(self):
        other = self.make_storage("other", "local", self.root)
        self.share()
        self.share(storage=other)
        client = Client()
        client.force_login(self.alice)
        response = client.get("/shares/")
        self.assertEqual(response.status_code, 200)
        page = response.content.decode()
        self.assertIn('data-share-list data-mode="mine" data-api="/api/v1/shares/"', page)
        self.assertIn("shares/js/share_list.js", page)
        self.assertIn(f'<option value="{other.pk}">存储 other</option>', page)
        self.assertIn('aria-current="page"><span class="nav-indicator">', page)  # 我的分享 is the current destination
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotIn("/site/custom.js", page)  # not a public page

    def test_in_the_navigation(self):
        client = Client()
        self.assertNotIn('href="/shares/"', client.get("/").content.decode())
        client.force_login(self.alice)
        self.assertIn('href="/shares/"', client.get("/").content.decode())


class ManageSectionTests(SharesTestCase):
    def test_the_section(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        client = Client()
        client.force_login(admin)
        page = client.get("/manage/").content.decode()
        self.assertIn('data-share-list data-mode="admin" data-api="/api/v1/manage/shares/"', page)
        self.assertIn('href="#shares" data-section="shares"', page)
        self.assertIn('<option value="%d">存储 disk</option>' % self.storage.pk, page)
        self.assertIn('<p class="manage-group">链接</p>', page)


class BrowserPageTests(SharesTestCase):
    def test_the_create_action_is_on_the_browsers_pages(self):
        client = Client()
        client.force_login(self.alice)
        page = client.get("/disk/").content.decode()
        self.assertIn("shares/js/create.js", page)
        self.assertIn("shares/css/shares.css", page)
        self.assertLess(page.index("files/js/files.js"), page.index("shares/js/create.js"))


class RegistryTests(SharesTestCase):
    def test_reserved_names(self):
        self.assertIn("share", reserved.static_names())
        self.assertIn("shares", reserved.static_names())
        self.assertTrue(reserved.problem("shares"))
        self.assertTrue(reserved.problem("Share"))

    def test_the_access_log_hides_keys(self):
        self.assertEqual(masking.mask_address("/share/AbC123", "pwd=secret"), "/share/<key>?pwd=<pwd>")
        self.assertEqual(masking.mask_address("/share/AbC123/download", "path=/a.txt"), "/share/<key>/download?path=/a.txt")
        self.assertEqual(masking.mask_address("/api/v1/shares/AbC123/list/"), "/api/v1/shares/<key>/list/")
        self.assertEqual(masking.mask_address("/shares/"), "/shares/")
        self.assertEqual(masking.mask_address("/api/v1/manage/shares/AbC123/"), "/api/v1/manage/shares/<key>/")
        self.assertEqual(masking.mask_address("/api/v1/shares/clear-expired/"), "/api/v1/shares/clear-expired/")
