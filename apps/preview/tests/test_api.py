"""POST /api/v1/preview/markdown/ and kkfileview/: the signed URL as the only proof, the access policy
when serving (check_served), the size cap, apps.core.markdown's sanitising, the kkFileView address."""

import base64
import re
import time
from unittest import mock
from urllib.parse import parse_qs, unquote, urlsplit

from django.test import override_settings

from apps.accounts.models import User
from apps.accounts.permissions import Permission
from apps.siteconf.models import SiteSettings
from apps.storages.drivers.base import Entry
from apps.storages.models import FilterMode, FilterRule, StorageSource
from apps.storages.tests.fakes import FakeDriver

from apps.files import signing

from .. import content
from .helpers import PreviewTestCase

XSS = b"""# Hi

<script>alert(1)</script>
<img src="x" onerror="alert(2)">
<a href="javascript:alert(3)">j</a>

[md link](javascript:alert(4)) and [spaced](  JaVaScRiPt:alert(8)) and ![x](data:text/html;base64,PHNjcmlwdD4=)

<iframe src="https://evil.example/"></iframe>
<p style="color:red" id="main" class="dialog" onclick="alert(5)">styled</p>
<svg><script>alert(6)</script></svg>
<form action="https://evil.example/"><input name="q"></form>
<object data="x.swf"></object>
<a href="https://example.com/">ok</a>
"""


class MarkdownTests(PreviewTestCase):
    def test_renders_on_the_server(self):
        r = self.post("markdown", self.signed("/docs/README.md"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("<h1>标题</h1>", r.json()["html"])
        # relative links and pictures stay relative: the viewer maps them to files the visitor lists
        self.assertIn('src="pic.png"', r.json()["html"])
        self.assertIn('href="other.md"', r.json()["html"])
        self.assertEqual(r.json()["size"], len(FakeDriver.tree["/docs/README.md"]))
        self.assertIn("no-store", r["Cache-Control"])

    def test_untrusted_html_goes(self):
        FakeDriver.tree["/docs/x.md"] = XSS
        html = self.post("markdown", self.signed("/docs/x.md")).json()["html"]
        for bad in ("<script", "onerror", 'href="javascript', "<iframe", "style=", 'id="main"', "dialog", "onclick", "<form",
                    "<input name", "<object", 'src="data:text/html', "<svg"):
            self.assertNotIn(bad, html.lower())
        self.assertNotRegex(html, r'(?i)(href|src)="\s*javascript')
        self.assertIn('<a href="https://example.com/" rel="noopener noreferrer">ok</a>', html)
        self.assertIn("styled", html)

    def test_encodings(self):
        FakeDriver.tree["/docs/gbk.md"] = "# 中文标题\n".encode("gbk")
        FakeDriver.tree["/docs/bom.md"] = b"\xef\xbb\xbf# BOM\n"
        FakeDriver.tree["/docs/utf16.markdown"] = "# 十六\n".encode("utf-16")
        self.assertIn("<h1>中文标题</h1>", self.post("markdown", self.signed("/docs/gbk.md")).json()["html"])
        self.assertIn("<h1>BOM</h1>", self.post("markdown", self.signed("/docs/bom.md")).json()["html"])
        self.assertIn("<h1>十六</h1>", self.post("markdown", self.signed("/docs/utf16.markdown")).json()["html"])

    def test_only_markdown_files(self):
        r = self.post("markdown", self.signed("/docs/notes.txt"))
        self.assertEqual((r.status_code, r.json()["code"]), (400, "not_markdown"))
        FakeDriver.tree["/docs/a.md.txt"] = b"# no"
        self.assertEqual(self.post("markdown", self.signed("/docs/a.md.txt")).json()["code"], "not_markdown")

    def test_size_cap(self):
        site = SiteSettings.load()
        site.text_preview_max_kb = 1
        site.save()
        FakeDriver.tree["/docs/big.md"] = b"x" * 1025
        r = self.post("markdown", self.signed("/docs/big.md"))
        self.assertEqual((r.status_code, r.json()["code"], r.json()["size"], r.json()["limit"]), (400, "too_large", 1025, 1024))
        self.assertIn("1 KB", r.json()["detail"])
        FakeDriver.tree["/docs/fits.md"] = b"x" * 1024
        self.assertEqual(self.post("markdown", self.signed("/docs/fits.md")).status_code, 200)

    def test_cap_holds_without_a_known_size(self):
        """A backend that doesn't say the size (an exported document): reading stops past the limit."""
        site = SiteSettings.load()
        site.text_preview_max_kb = 1
        site.save()
        FakeDriver.tree["/docs/big.md"] = b"x" * 5000
        with mock.patch.object(FakeDriver, "stat", lambda self, path: Entry(path.rsplit("/", 1)[1], path, False, None)):
            r = self.post("markdown", self.signed("/docs/big.md"))
        self.assertEqual((r.status_code, r.json()["code"]), (400, "too_large"))

    def test_markdown_is_capped_below_the_text_limit(self):
        site = SiteSettings.load()
        site.text_preview_max_kb = 10 * 1024
        site.save()
        self.assertEqual(content.markdown_limit(site), content.MARKDOWN_MAX)

    def test_missing_file_and_folders(self):
        r = self.post("markdown", self.signed("/docs/gone.md"))
        self.assertEqual((r.status_code, r.json()["code"]), (404, "not_found"))
        FakeDriver.tree["/docs/folder.md"] = None
        self.assertEqual(self.post("markdown", self.signed("/docs/folder.md")).json()["code"], "not_a_file")

    def test_what_the_storage_said_is_only_for_admins(self):
        """SEC-01: anybody with the signed URL may ask, logged in or not. A storage's failure is told in
        plain words; what the backend said (its address) goes to the log, and to an admin."""
        from apps.storages.drivers.base import BackendError

        said = "connect to 10.0.0.5:443 failed"
        with mock.patch.object(FakeDriver, "open", side_effect=BackendError(said)):
            with self.assertLogs("hfile.preview", "WARNING") as logged:
                r = self.post("markdown", self.signed("/docs/README.md"))
            self.assertEqual((r.status_code, r.json()["code"], r.json()["detail"]), (502, "storage_error", "连接存储失败，请稍后再试。"))
            self.assertIn(said, "\n".join(logged.output))
            self.client.force_login(User.objects.create_user("boss", password="pw-boss-2026", is_staff=True))
            with self.assertLogs("hfile.preview", "WARNING"):
                r = self.post("markdown", self.signed("/docs/README.md"))
            self.assertEqual((r.status_code, r.json()["detail"]), (502, said))


class RefusalTests(PreviewTestCase):
    def assertRefused(self, url, code, status=403, endpoint="markdown"):
        r = self.post(endpoint, url)
        self.assertEqual((r.status_code, r.json().get("code")), (status, code), r.json())

    def test_another_files_signature(self):
        url = self.signed("/docs/other.md")
        self.assertRefused(url.replace("other.md", "README.md"), signing.INVALID)
        self.assertRefused(url.replace("&sign=", "&sign=x"), signing.INVALID)
        self.assertRefused(url.replace("d=i", "d=a"), signing.INVALID)  # every value is signed

    def test_another_storages_signature(self):
        other = StorageSource.objects.create(key="other", name="别的", type="fake", config=self.storage.config)
        url = self.signed("/docs/README.md", storage=other)
        self.assertRefused(url.replace("/other/", "/files/"), signing.INVALID)

    def test_expired(self):
        url = self.signed("/docs/README.md")
        with mock.patch("apps.files.signing.time.time", return_value=time.time() + self.storage.link_lifetime + 5):
            self.assertRefused(url, signing.EXPIRED)

    def test_rotated_secret_revokes(self):
        url = self.signed("/docs/README.md")
        self.storage.link_secret = "rotated"
        self.storage.save()
        self.assertRefused(url, signing.INVALID)

    def test_disabled_or_deleted_storage(self):
        url = self.signed("/docs/README.md")
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        self.assertRefused(url, "storage_disabled")
        StorageSource.objects.filter(pk=self.storage.pk).delete()
        self.assertRefused(url, signing.INVALID)  # and nothing tells whether a key exists

    def test_filter_rules_are_checked_when_serving(self):
        url = self.signed("/docs/README.md")
        rule = FilterRule.objects.create(storage=self.storage, expression="/docs/README.md", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertRefused(url, "download_disabled")
        rule.mode = FilterMode.INACCESSIBLE
        rule.save()
        self.assertRefused(url, "inaccessible")
        rule.expression = "/docs"  # a folder above it
        rule.save()
        self.assertRefused(url, "inaccessible")

    def test_not_a_signed_url(self):
        good = self.signed("/docs/README.md")
        for url in ("", "/docs/README.md", "https://testserver" + good, "//testserver" + good, "/pd/files/docs/README.md",
                    "/pd/files/docs/../docs/README.md?e=9999999999&sign=x", "x" * 20_000):
            r = self.post("markdown", url)
            self.assertIn(r.status_code, (400, 403), url[:80])
        self.assertEqual(self.post("markdown", good + "&v=1&fullfilename=x.md").status_code, 200)  # others ignored
        self.assertEqual(self.client.post("/api/v1/preview/markdown/", {}, content_type="application/json").status_code, 400)
        self.assertEqual(self.client.post("/api/v1/preview/markdown/", [1], content_type="application/json").status_code, 400)

    def test_a_signed_url_is_the_whole_permission(self):
        """The signature stands for the access decision (D10): the grant isn't asked again, as for /pd/."""
        url = self.signed("/docs/README.md")
        self.grant(self.guest, [])
        self.assertEqual(self.post("markdown", url).status_code, 200)


class KkFileViewTests(PreviewTestCase):
    def setUp(self):
        super().setUp()
        site = SiteSettings.load()
        site.kkfileview_url = "http://kk.example:8012"
        site.save()

    def decode(self, address):
        parts = urlsplit(address)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", "http://kk.example:8012/onlinePreview")
        raw = parts.query.split("url=", 1)[1]
        self.assertNotRegex(raw, r"[+/=&]")  # base64 quoted as encodeURIComponent quotes it
        return base64.b64decode(parse_qs(parts.query)["url"][0]).decode("utf-8")

    def test_the_address(self):
        presented = self.signed("/docs/drawing.dwg")
        r = self.post("kkfileview", presented)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["mode"], "iframe")
        file_url = self.decode(r.json()["url"])
        # The URL the viewer had, absolute, so it ends when that one ends (no fresh signature: the endpoint
        # can't be used to keep a link alive); kkFileView picks the converter from fullfilename's suffix.
        self.assertTrue(presented.startswith("/pd/files/docs/drawing.dwg?"), presented)
        self.assertRegex(file_url, r"^http://testserver" + re.escape(presented) + r"&fullfilename=[0-9a-f]{32}\.dwg$")
        self.assertNotIn("drawing", parse_qs(urlsplit(file_url).query)["fullfilename"][0])
        link = signing.verify_url(file_url[len("http://testserver"):])  # the extra parameter doesn't matter
        self.assertEqual((link.storage.pk, link.path), (self.storage.pk, "/docs/drawing.dwg"))

    def test_no_new_signature(self):
        presented = self.signed("/docs/notes.txt")
        expires = parse_qs(urlsplit(presented).query)["e"][0]
        with mock.patch("apps.files.signing.time.time", return_value=time.time() + 590):
            file_url = self.decode(self.post("kkfileview", presented).json()["url"])
        self.assertEqual(parse_qs(urlsplit(file_url).query)["e"], [expires])

    def test_not_on_the_sites_own_origin(self):
        """kkFileView shows the files it converts as pages of its own origin: never the site's."""
        for kk, site_url in (("http://testserver", ""), ("http://testserver:80/kk", ""), ("https://files.example.com/kk", "https://files.example.com")):
            site = SiteSettings.load()
            site.kkfileview_url = kk
            site.save()
            with override_settings(SITE_URL=site_url):
                r = self.post("kkfileview", self.signed("/docs/notes.txt"))
            self.assertEqual((r.status_code, r.json()["code"]), (400, "kkfileview_same_origin"), kk)

    @override_settings(SITE_URL="https://files.example.com")
    def test_the_sites_own_address(self):
        file_url = self.decode(self.post("kkfileview", self.signed("/docs/notes.txt")).json()["url"])
        self.assertTrue(file_url.startswith("https://files.example.com/pd/files/docs/notes.txt?"))

    def test_random_name_and_odd_suffixes(self):
        names = set()
        for _ in range(3):
            names.add(parse_qs(urlsplit(self.decode(self.post("kkfileview", self.signed("/docs/notes.txt")).json()["url"])).query)["fullfilename"][0])
        self.assertEqual(len(names), 3)
        FakeDriver.tree["/docs/odd.p&q=1#x"] = b"x"
        FakeDriver.tree["/docs/noext"] = b"x"
        for path in ("/docs/odd.p&q=1#x", "/docs/noext"):
            file_url = self.decode(self.post("kkfileview", self.signed(path)).json()["url"])
            self.assertRegex(unquote(file_url), r"&fullfilename=[0-9a-f]{32}$")

    def test_new_tab_mode(self):
        site = SiteSettings.load()
        site.kkfileview_open_mode = "new_tab"
        site.save()
        self.assertEqual(self.post("kkfileview", self.signed("/docs/notes.txt")).json()["mode"], "new_tab")

    def test_off_without_an_address(self):
        site = SiteSettings.load()
        site.kkfileview_url = ""
        site.save()
        r = self.post("kkfileview", self.signed("/docs/notes.txt"))
        self.assertEqual((r.status_code, r.json()["code"]), (404, "kkfileview_off"))

    def test_refusals(self):
        url = self.signed("/docs/drawing.dwg")
        FilterRule.objects.create(storage=self.storage, expression="/docs/*.dwg", mode=FilterMode.DISABLE_DOWNLOAD)
        r = self.post("kkfileview", url)
        self.assertEqual((r.status_code, r.json()["code"]), (403, "download_disabled"))
        r = self.post("kkfileview", url.replace("drawing.dwg", "notes.txt"))
        self.assertEqual((r.status_code, r.json()["code"]), (403, signing.INVALID))


class ThrottleTests(PreviewTestCase):
    def test_per_address(self):
        url = self.signed("/docs/README.md")
        with mock.patch("apps.core.throttles.ScopedRateThrottle.THROTTLE_RATES", {"preview": "3/minute"}):
            codes = [self.post("markdown", url).status_code for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])


class UserTests(PreviewTestCase):
    def test_a_preview_only_user(self):
        """Preview alone is enough to read a file in the viewers (D37); the URL decides, not the user."""
        viewer = User.objects.create_user("viewer", password="pw-viewer-2026")
        self.grant(viewer, [Permission.PREVIEW])
        self.client.force_login(viewer)
        self.assertEqual(self.post("markdown", self.signed("/docs/README.md")).status_code, 200)
