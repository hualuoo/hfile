"""Single sign-on in the pages: the buttons under the login form, the user menu's 账号绑定 (and D33's rule
beside custom JS), the /manage/ section, and the CSP for icons linked from elsewhere."""

import base64

from django.contrib.auth import get_user_model
from django.http import HttpResponse
from django.test import RequestFactory, TestCase, override_settings

from apps.siteconf.models import SiteSettings
from apps.sso import icons
from apps.sso.middleware import SsoIconMiddleware
from apps.sso.models import Identity, Provider
from apps.sso.presets import PRESETS

User = get_user_model()
SVG = "data:image/svg+xml;base64," + base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()


def provider(code="gh", **values):
    fields = {"name": "GitHub", "client_id": "c", "auth_url": "https://a.example/a", "token_url": "https://a.example/t",
              "userinfo_url": "https://a.example/u", "icon": SVG}
    fields.update(values)
    return Provider.objects.create(code=code, **fields)


def site(**values):
    settings = SiteSettings.load()
    for name, value in values.items():
        setattr(settings, name, value)
    settings.save()


class LoginButtonTests(TestCase):
    def test_under_the_login_dialog(self):
        page = self.client.get("/").content.decode()
        self.assertNotIn("sso-login", page)  # no provider, no buttons
        provider(name='Git<"Hub">')
        provider("off", name="Off", enabled=False)
        provider("plain", name="平台", icon="", order=-1)
        page = self.client.get("/").content.decode()
        self.assertIn('class="sso-login login-password-part"', page)
        self.assertIn('<a class="sso-button" href="/sso/gh/login" data-sso-login aria-label="用 Git&lt;&quot;Hub&quot;&gt; 登录"', page)
        self.assertIn(f'<img src="{SVG}" alt=""', page)
        self.assertIn('<span class="sso-letter" aria-hidden="true">平</span>', page)
        self.assertLess(page.index("/sso/plain/login"), page.index("/sso/gh/login"))  # in their order
        self.assertNotIn("/sso/off/login", page)
        self.assertIn("sso/js/login.js", page)

    def test_on_the_login_pages(self):
        provider()
        self.assertIn("/sso/gh/login", self.client.get("/login/").content.decode())
        site(secure_login_entry="door-42")
        self.assertNotIn("/sso/gh/login", self.client.get("/").content.decode())  # no dialog with an entry
        self.assertIn("/sso/gh/login", self.client.get("/login/door-42/").content.decode())


class LinksMenuTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.client.force_login(self.alice)

    def test_offered_while_there_is_something_to_bind(self):
        self.assertNotIn("账号绑定", self.client.get("/").content.decode())
        gh = provider(enabled=False)
        self.assertNotIn("账号绑定", self.client.get("/").content.decode())
        Identity.objects.create(provider=gh, external_id="1", user=self.alice)
        page = self.client.get("/").content.decode()
        self.assertIn('id="sso-links-open"', page)  # bound to a provider switched off: may still unbind
        self.assertIn('id="sso-links-dialog"', page)
        self.assertIn("sso/js/links.js", page)
        Identity.objects.all().delete()
        gh.enabled = True
        gh.save()
        self.assertIn('id="sso-links-open"', self.client.get("/").content.decode())

    def test_not_beside_custom_js(self):
        provider()
        site(custom_js="console.log('hi')")
        page = self.client.get("/").content.decode()
        self.assertIn('href="?auth=sso"', page)
        self.assertNotIn('id="sso-links-dialog"', page)
        self.assertNotIn("sso/js/links.js", page)
        page = self.client.get("/?auth=sso").content.decode()  # the same page without the custom JS
        self.assertIn('id="sso-links-dialog"', page)
        self.assertIn("sso/js/links.js", page)


class PasswordDialogTests(TestCase):
    def test_an_account_without_a_password_sets_one(self):
        user = User.objects.create_user("sso-made")
        self.client.force_login(user)
        page = self.client.get("/").content.decode()
        self.assertIn("设置密码", page)
        self.assertNotIn('name="old_password"', page)
        user.set_password("pw-alice-2026")
        user.save()
        self.client.force_login(user)
        page = self.client.get("/").content.decode()
        self.assertIn('name="old_password"', page)
        self.assertIn("修改密码", page)


class ManageSectionTests(TestCase):
    def setUp(self):
        self.client.force_login(User.objects.create_superuser("boss", password="pw-boss-2026"))

    def test_the_section(self):
        page = self.client.get("/manage/").content.decode()
        self.assertIn('href="#sso" data-section="sso"', page)
        self.assertIn('data-presets="[{&quot;key&quot;: &quot;github&quot;', page)
        for preset in PRESETS:
            self.assertIn(f'data-preset="{preset["key"]}"', page)
        self.assertIn("sso/js/manage_sso.js", page)
        self.assertIn('data-origin="http://testserver"', page)
        self.assertIn('id="sso-origin-warning"', page)  # no [site] url
        self.assertIn("允许单点登录自动注册新用户", page)
        self.assertIn('data-tab="sso"', page)  # the user dialog's tab
        with override_settings(SITE_URL="https://files.example.com"):
            page = self.client.get("/manage/", HTTP_HOST="testserver").content.decode()
        self.assertNotIn('id="sso-origin-warning"', page)
        self.assertIn('data-origin="https://files.example.com"', page)

    def test_read_only_for_staff_who_arent_superusers(self):
        helper = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        self.client.force_login(helper)
        page = self.client.get("/manage/").content.decode()
        self.assertIn('id="sso-read-only"', page)
        self.assertNotIn('id="sso-new-menu"', page)
        self.assertIn('data-can-edit="0"', page)

    def test_the_presets(self):
        for preset in PRESETS:
            values = preset["values"]
            with self.subTest(preset=preset["key"]):
                self.assertTrue(values["icon"].startswith("data:image/svg+xml;base64,"))
                self.assertIn(values["id_field"], ("id", "sub"))
                self.assertTrue(values["username_field"] and values["nickname_field"])
                from apps.sso.validation import clean_icon

                self.assertEqual(clean_icon(values["icon"]), values["icon"])


class IconPolicyTests(TestCase):
    def csp(self, path="/"):
        return self.client.get(path)["Content-Security-Policy"]

    def img_src(self, path="/"):
        return next(part for part in self.csp(path).split(";") if part.strip().startswith("img-src")).split()[1:]

    def test_linked_icons_are_let_in(self):
        self.assertEqual(self.img_src(), ["'self'", "data:", "blob:"])
        provider(icon="https://img.example.com/a.png")
        provider("b", icon="http://cdn.example.org:8080/b.svg", enabled=False)
        provider("c", icon="https://xn--fsqu00a.xn--fiqs8s/c.png")
        provider("d", icon="http://[2001:db8::1]/d.png")  # an IPv6 literal can't be named in a CSP
        self.assertEqual(self.img_src(), ["'self'", "data:", "blob:", "http://cdn.example.org:8080", "https://img.example.com",
                                          "https://xn--fsqu00a.xn--fiqs8s"])
        self.assertTrue(self.csp().isascii())
        self.assertIn("https://img.example.com", self.csp("/login/"))
        self.assertNotIn("img.example.com", self.client.get("/api/v1/sso/providers/")["Content-Security-Policy"])  # JSON

    def test_the_cache_is_cleared_on_save(self):
        gh = provider(icon="https://img.example.com/a.png")
        self.assertIn("https://img.example.com", self.img_src())
        gh.icon = SVG
        gh.save()
        self.assertNotIn("https://img.example.com", self.img_src())

    def test_with_the_relaxed_policy_of_custom_js(self):
        provider(icon="http://cdn.example.org/a.png")
        site(custom_js="console.log('hi')")
        sources = self.img_src()
        self.assertIn("https:", sources)  # siteconf's
        self.assertIn("http://cdn.example.org", sources)  # ours on top

    def test_a_view_s_own_policy_is_kept(self):
        def turned_off(request):
            response = HttpResponse("<p>x</p>")
            response._csp_config = {}  # a view that turned the policy off
            return response

        def its_own(request):
            response = HttpResponse("<p>x</p>")
            response._csp_config = {"img-src": ["'none'"], "default-src": ["'self'"]}
            return response

        def with_a_flag(request):
            response = HttpResponse("<p>x</p>")
            response._csp_config = {"img-src": ["'self'"], "upgrade-insecure-requests": True, "default-src": "'self'"}
            return response

        provider(icon="https://img.example.com/a.png")
        request = RequestFactory().get("/")
        self.assertEqual(SsoIconMiddleware(turned_off)(request)._csp_config, {})
        # A view that shut images out keeps it so ('none' beside a source would mean the source).
        self.assertEqual(SsoIconMiddleware(its_own)(request)._csp_config, {"img-src": ["'none'"], "default-src": ["'self'"]})
        self.assertEqual(SsoIconMiddleware(with_a_flag)(request)._csp_config,
                         {"img-src": ["'self'", "https://img.example.com"], "upgrade-insecure-requests": True, "default-src": "'self'"})

    def test_origins(self):
        for icon, origin in (("https://Img.Example.com/a.png", "https://img.example.com"), ("http://a.example:81/x", "http://a.example:81"),
                             (SVG, ""), ("https://例子.中国/a.png", ""), ("http://[::1]/a.png", ""), ("not a url", "")):
            self.assertEqual(icons.origin_of(icon), origin)
