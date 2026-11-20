"""The CSP the viewers need (workers everywhere; blob: media and kkFileView's frame only on public pages)
and the adapter on files' signed links (W2)."""

from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.core import masking
from apps.files import signing
from apps.siteconf.middleware import policy
from apps.siteconf.models import SiteSettings
from apps.storages.access import AccessDenied
from apps.storages.models import StorageSource

from .. import csp, sources


def header(response):
    return response.get("Content-Security-Policy", "")


class CspTests(TestCase):
    def kk(self, url, mode="iframe"):
        site = SiteSettings.load()
        site.kkfileview_url = url
        site.kkfileview_open_mode = mode
        site.save()
        return site

    def test_workers_everywhere_blob_media_on_public_pages(self):
        for path in ("/", "/manage/", "/login/"):
            value = header(self.client.get(path))
            self.assertIn("worker-src 'self'", value, path)
            self.assertNotIn("frame-src", value, path)
        self.assertIn("media-src 'self' blob:", header(self.client.get("/")))
        self.assertNotIn("media-src", header(self.client.get("/login/")))
        self.assertEqual(policy(SiteSettings.load(), "page")["media-src"], ["'self'", "blob:"])

    def test_kkfileview_framed_only_on_public_pages(self):
        site = self.kk("http://kk.example:8012")
        self.assertEqual(policy(site, "page")["frame-src"], ["http://kk.example:8012"])
        self.assertEqual(policy(site, "login")["frame-src"], ["http://kk.example:8012"])
        self.assertNotIn("frame-src", policy(site, None))
        self.assertNotIn("frame-src", policy(site, "custom"))
        self.assertIn("frame-src http://kk.example:8012", header(self.client.get("/")))
        admin = User.objects.create_superuser("admin", password="pw-admin-2026")
        self.client.force_login(admin)
        self.assertNotIn("frame-src", header(self.client.get("/manage/")))

    def test_not_framed_when_it_opens_in_a_tab_or_is_off(self):
        self.assertNotIn("frame-src", policy(self.kk("http://kk.example:8012", "new_tab"), "page"))
        self.assertNotIn("frame-src", policy(self.kk(""), "page"))

    @override_settings(SITE_URL="https://files.example.com")
    def test_never_the_sites_own_origin(self):
        for url in ("https://files.example.com/kkfileview", "https://FILES.example.com:443"):
            self.assertEqual(csp.kkfileview_origin(self.kk(url)), "", url)
        self.assertEqual(csp.kkfileview_origin(self.kk("https://files.example.com:8443")), "https://files.example.com:8443")

    def test_same_origin(self):
        self.assertTrue(csp.same_origin("http://a.example/x", "http://A.example:80"))
        self.assertFalse(csp.same_origin("http://a.example", "https://a.example"))
        self.assertFalse(csp.same_origin("http://a.example:81", "http://a.example"))
        self.assertFalse(csp.same_origin("", ""))
        self.assertFalse(csp.same_origin("http://a.example:99999", "http://a.example:99999"))

    def test_only_what_a_csp_source_can_name(self):
        for url, expected in (("https://kk.example.com/sub/path", "https://kk.example.com"),
                              ("http://[::1]:8012", ""), ("http://xn--fsqu00a.xn--fiqs8s", "http://xn--fsqu00a.xn--fiqs8s")):
            self.assertEqual(csp.kkfileview_origin(self.kk(url)), expected, url)

    def test_custom_js_widens_scripts_but_not_workers(self):
        site = self.kk("")
        site.custom_js = "console.log(1)"
        site.save()
        config_ = policy(site, "page")
        self.assertEqual(config_["script-src"], ["'self'", "https:"])
        self.assertEqual(config_["worker-src"], ["'self'"])


class SourcesTests(TestCase):
    """apps/preview/sources.py on apps.files.signing: only a /pd/ path of this site, other parameters ignored."""

    def test_verify(self):
        storage = StorageSource.objects.create(key="s", name="s", type="local", config={})
        url = signing.sign(storage, "/a b/c&d#e.txt", lifetime=600, inline=True)
        self.assertTrue(url.startswith("/pd/s/a%20b/c%26d%23e.txt?"), url)
        self.assertEqual(sources.verify(url + "&fullfilename=0123.txt").path, "/a b/c&d#e.txt")
        self.assertIn("sign=<sign>", masking.mask_text(url))  # kept out of the access log
        for bad in ("http://testserver" + url, "//testserver" + url, url.replace("/pd/", "/x/"), 42, "/pd/" + "x" * 10_001):
            with self.assertRaises(AccessDenied) as caught:
                sources.verify(bad)
            self.assertEqual(caught.exception.code, signing.INVALID)
