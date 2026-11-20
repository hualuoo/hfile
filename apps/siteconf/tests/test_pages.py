"""What the settings change on the pages: the icon, the logo, the footer, the announcement, custom CSS
and JS with their CSP (D33), and the guest landing page."""

import re

from django.test import TestCase
from rest_framework.test import APIClient

from apps.core.testing import TempMediaMixin

from ..models import SiteSettings
from .helpers import png, users


def csp(response):
    return dict(part.split(" ", 1) for part in response["Content-Security-Policy"].split("; "))


class TestBase(TestCase):
    client_class = APIClient  # multipart uploads with format=

    def setUp(self):
        self.boss, self.staff, self.alice = users()
        self.site = SiteSettings.load()

    def change(self, **fields):
        for name, value in fields.items():
            setattr(self.site, name, value)
        self.site.save()


class ShellTests(TempMediaMixin, TestBase):
    def test_hfiles_own_icon_and_logo_by_default(self):
        page = self.client.get("/").content.decode()
        self.assertRegex(page, r'<link rel="icon" href="/static/web/favicon\.svg\?v=[0-9a-f]{12}" type="image/svg\+xml">')
        self.assertRegex(page, r'<img class="brand-logo" src="/static/web/favicon\.svg\?v=[0-9a-f]{12}" alt="" width="32" height="32">')

    def test_an_uploaded_icon_and_logo(self):
        self.client.force_login(self.boss)
        with self.captureOnCommitCallbacks(execute=True):
            favicon = self.client.put("/api/v1/manage/settings/site/favicon/", {"file": png()}, format="multipart").json()["favicon"]
            logo = self.client.put("/api/v1/manage/settings/site/home_logo/", {"file": png(400, 100)}, format="multipart").json()["home_logo"]
        for path in ("/", "/manage/"):
            page = self.client.get(path).content.decode()
            self.assertIn(f'<link rel="icon" href="/media/{favicon}">', page)
            self.assertIn(f'<img class="brand-logo brand-logo-custom" src="/media/{logo}" alt="" height="32">', page)

    def test_the_csp_stays_ascii_whatever_the_links(self):
        # An IPv6 literal can't be a CSP source, and an old row may hold anything: left out of the policy.
        self.change(favicon="https://[2001:db8::1]/i.png", home_logo="https://例子.中国/l.png")
        header = self.client.get("/")["Content-Security-Policy"]
        self.assertTrue(header.isascii())
        self.assertTrue(header.startswith("default-src 'self'"))
        self.assertEqual(csp(self.client.get("/"))["img-src"].split(), ["'self'", "data:", "blob:"])

    def test_linked_images_are_let_in_by_the_csp(self):
        self.change(favicon="https://cdn.example.com/i.png", home_logo="https://img.example.org:8443/l.png")
        for path in ("/", "/manage/"):
            self.client.force_login(self.boss)
            images = csp(self.client.get(path))["img-src"].split()
            self.assertEqual(images, ["'self'", "data:", "blob:", "https://cdn.example.com", "https://img.example.org:8443"])

    def test_the_logo_link_on_public_pages(self):
        self.change(home_logo_link="https://blog.example.com/", home_logo_target="_blank")
        page = self.client.get("/").content.decode()
        self.assertIn('<a class="brand" href="https://blog.example.com/" target="_blank" rel="noopener">', page)
        self.client.force_login(self.boss)
        self.assertIn('<a class="brand" href="/">', self.client.get("/manage/").content.decode())  # /manage/: home
        self.change(home_logo_link="")
        self.client.logout()
        self.assertIn('<span class="brand">', self.client.get("/").content.decode())

    def test_names_are_escaped(self):
        self.change(site_name='<img src=x onerror=alert(1)>', icp="<b>备案</b>")
        page = self.client.get("/").content.decode()
        self.assertNotIn("<img src=x", page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;", page)
        self.assertIn("&lt;b&gt;备案&lt;/b&gt;", page)

    def test_the_footer_with_the_icp_number(self):
        page = self.client.get("/").content.decode()
        self.assertIn('Powered by <a href="https://github.com/hualuoo/hfile" target="_blank" rel="noopener noreferrer">HFile</a>', page)
        self.assertIn('<main class="main with-footer" id="main"', page)  # at the bottom of the page
        self.assertNotIn("beian.miit.gov.cn", page)
        self.change(icp="京ICP备12345678号")
        page = self.client.get("/").content.decode()
        self.assertIn('<a href="https://beian.miit.gov.cn/" target="_blank" rel="noopener noreferrer">京ICP备12345678号</a>', page)
        self.client.force_login(self.boss)
        page = self.client.get("/manage/").content.decode()
        self.assertNotIn('class="site-footer"', page)
        self.assertIn('<main class="main" id="main"', page)


class AnnouncementTests(TestBase):
    def test_on_public_pages_only(self):
        self.change(announcement="**重要**：周末维护。<script>alert(1)</script>")
        page = self.client.get("/").content.decode()
        self.assertIn('<aside class="announcement" id="announcement" aria-label="公告"', page)
        self.assertIn("<strong>重要</strong>：周末维护。", page)
        self.assertNotIn("alert(1)", page)
        self.assertRegex(page, r'<script src="/static/siteconf/js/public\.js\?v=[0-9a-f]{12}"></script>')
        self.client.force_login(self.boss)
        self.assertNotIn('id="announcement"', self.client.get("/manage/").content.decode())

    def test_switched_off_or_empty(self):
        self.change(announcement="公告", show_announcement=False)
        self.assertNotIn('id="announcement"', self.client.get("/").content.decode())
        self.change(announcement="   ", show_announcement=True)
        self.assertNotIn('id="announcement"', self.client.get("/").content.decode())

    def test_a_new_text_shows_again(self):
        self.change(announcement="第一条")
        first = re.search(r'data-version="([0-9a-f]{12})"', self.client.get("/").content.decode()).group(1)
        self.change(announcement="第二条")
        self.assertNotIn(first, self.client.get("/").content.decode())


class CustomCodeTests(TestBase):
    def test_linked_from_public_pages_only(self):
        self.change(custom_css="body { color: red; }", custom_js="console.log('hi')")
        page = self.client.get("/").content.decode()
        css = re.search(r'<link rel="stylesheet" href="(/site/custom\.css\?v=[0-9a-f]{12})">', page)
        js = re.search(r'<script src="(/site/custom\.js\?v=[0-9a-f]{12})"></script>', page)
        self.assertTrue(css and js)
        self.assertLess(page.index("web/css/app.css"), page.index("/site/custom.css"))  # after the site's own: it may override them
        self.assertGreater(page.index("/site/custom.js"), page.index("web/js/shell.js"))
        # Not where an admin logs in or manages the site: a broken script can't lock him out.
        for path, user in (("/?auth=login", None), ("/manage/", self.boss)):
            with self.subTest(path):
                if user:
                    self.client.force_login(user)
                page = self.client.get(path).content.decode()
                self.assertNotIn("/site/custom.", page)

    def test_no_password_is_typed_beside_custom_js(self):
        # D33: the login and password dialogs aren't on a page that runs the admin's JS (which may load
        # scripts from anywhere): 登录 and 修改密码 lead to the same page without it.
        page = self.client.get("/").content.decode()
        self.assertIn('id="login-dialog"', page)
        self.assertIn("data-login>", page)
        self.change(custom_js="console.log(1)")
        page = self.client.get("/").content.decode()
        self.assertNotIn('id="login-dialog"', page)
        self.assertNotIn("{#", page)
        self.assertIn('<a class="btn btn-filled" href="/login/?next=/">', page)  # accounts' login page
        login = self.client.get("/login/?next=/").content.decode()
        self.assertIn('name="password"', login)
        self.assertNotIn("/site/custom.js", login)
        login = self.client.get("/?auth=login").content.decode()  # LOGIN_URL: the dialog, without the custom JS
        self.assertIn('id="login-dialog"', login)
        self.assertNotIn("/site/custom.js", login)
        self.client.force_login(self.alice)
        page = self.client.get("/").content.decode()
        self.assertNotIn('id="password-dialog"', page)
        self.assertIn('<a class="menu-item" role="menuitem" href="?auth=password">', page)
        password = self.client.get("/?auth=password").content.decode()
        self.assertIn('id="password-dialog"', password)
        self.assertNotIn("/site/custom.js", password)
        self.assertEqual(csp(self.client.get("/?auth=password"))["script-src"], "'self'")

    def test_no_two_step_code_is_typed_beside_custom_js(self):
        # The 两步验证 dialog asks for the password and shows the new secret (accounts/js/totp.js).
        self.client.force_login(self.boss)
        page = self.client.get("/").content.decode()
        self.assertIn('id="totp-open"', page)
        self.change(custom_js="console.log(1)")
        page = self.client.get("/").content.decode()
        self.assertNotIn('id="totp-open"', page)
        self.assertIn('<a class="menu-item" role="menuitem" href="?auth=totp">', page)
        totp = self.client.get("/?auth=totp").content.decode()
        self.assertIn('id="totp-open"', totp)
        self.assertNotIn("/site/custom.js", totp)

    def test_the_csp_of_public_pages_with_custom_js(self):
        strict = csp(self.client.get("/"))
        self.assertEqual(strict["script-src"], "'self'")
        self.assertNotIn("connect-src", strict)
        self.change(custom_css="p{}")
        self.assertEqual(csp(self.client.get("/"))["script-src"], "'self'")  # CSS alone needs nothing
        self.change(custom_js="console.log(1)")
        relaxed = csp(self.client.get("/"))
        self.assertEqual((relaxed["script-src"], relaxed["connect-src"]), ("'self' https:", "'self' https:"))
        self.assertEqual(relaxed["img-src"].split(), ["'self'", "data:", "blob:", "https:"])
        self.assertEqual(relaxed["style-src"], "'self'")  # no inline anything
        self.assertEqual(csp(self.client.get("/?auth=login"))["script-src"], "'self'")
        self.client.force_login(self.boss)
        self.assertEqual(csp(self.client.get("/manage/"))["script-src"], "'self'")
        self.assertEqual(csp(self.client.get("/api/v1/site/"))["script-src"], "'self'")

    def test_the_css_file(self):
        self.assertEqual(self.client.get("/site/custom.css").status_code, 404)
        self.change(custom_css="<style>\nbody { color: red; }\n</STYLE>")
        version = self.site.custom_css_version
        r = self.client.get(f"/site/custom.css?v={version}")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "text/css; charset=utf-8"))
        self.assertEqual(r.content.decode().strip(), "body { color: red; }")
        self.assertEqual(r["Cache-Control"], "public, max-age=31536000, immutable")
        self.assertIn("no-cache", self.client.get("/site/custom.css?v=old").get("Cache-Control"))
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")

    def test_the_js_file(self):
        self.change(custom_js="window.counter = 1;")
        r = self.client.get("/site/custom.js")
        self.assertEqual((r["Content-Type"], r.content), ("text/javascript; charset=utf-8", b"window.counter = 1;"))
        self.change(custom_js='<script async src="https://stats.example/s.js" data-site="7"></script>\n<script>var _q = _q || [];</script>')
        loader = self.client.get("/site/custom.js").content.decode()
        self.assertIn('"src": "https://stats.example/s.js"', loader)
        self.assertIn('"data-site"', loader)
        self.assertIn(f'"src": "/site/custom.js?part=1&v={self.site.custom_js_version}"', loader)
        self.assertEqual(self.client.get("/site/custom.js?part=1").content.decode(), "var _q = _q || [];")
        for part in ("2", "x", "0", "01", "²", "①", "1" * 5000, "-1", " 1"):
            with self.subTest(part=part):
                self.assertEqual(self.client.get("/site/custom.js", {"part": part}).status_code, 404)


class GuestLandingTests(TestBase):
    def test_normal(self):
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertIn("还没有可以浏览的存储源", self.client.get("/").content.decode())

    def test_to_the_login(self):
        # accounts' login page, which comes back to the address asked for.
        self.change(guest_home="login")
        self.assertRedirects(self.client.get("/"), "/login/?next=%2F", fetch_redirect_response=False)
        self.assertRedirects(self.client.get("/?x=1"), "/login/?next=%2F%3Fx%3D1", fetch_redirect_response=False)
        # Pages that need a login send visitors to /?auth=login&next=... (settings.LOGIN_URL): on to the login page.
        self.assertRedirects(self.client.get("/?auth=login&next=/manage/"), "/login/?next=%2Fmanage%2F", fetch_redirect_response=False)
        for odd in ("//evil.example/", "https://evil.example/", "/\\evil.example", "/\t/evil.example", "/\n/evil.example", "/ /x"):
            with self.subTest(odd):
                self.assertRedirects(self.client.get("/", {"auth": "login", "next": odd}), "/login/?next=%2F", fetch_redirect_response=False)
        self.client.force_login(self.alice)
        self.assertIn("还没有可以浏览的存储源", self.client.get("/").content.decode())

    def test_to_the_login_with_a_secure_entry(self):
        # The only login page is /login/<entry>/, which visitors must not learn: the 401 page, linking nowhere.
        self.change(guest_home="login", secure_login_entry="hidden-door")
        for path in ("/", "/?auth=login&next=/manage/"):
            with self.subTest(path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 401)
                page = r.content.decode()
                self.assertIn("要登录才能使用这个网站", page)
                self.assertNotIn("hidden-door", page)
                self.assertNotIn("/login/", page)
                self.assertNotIn("auth=login", page)

    def test_a_custom_page(self):
        self.change(guest_home="custom", guest_home_html='<h1 class="type-display-small">欢迎</h1><a class="btn btn-filled" href="/login/">登录</a>',
                    custom_css="h1{}", site_name="我的网盘")
        r = self.client.get("/")
        page = r.content.decode()
        self.assertIn('<h1 class="type-display-small">欢迎</h1>', page)
        self.assertIn("<title>我的网盘</title>", page)
        self.assertRegex(page, r'<link rel="stylesheet" href="/site/custom\.css\?v=[0-9a-f]{12}">')
        self.assertIn("web/css/app.css", page)
        self.assertEqual(csp(r)["script-src"], "'self' https:")  # its own page, under the relaxed policy
        self.assertIn("Cookie", r["Vary"])
        # Asking to log in still leads to the login page, not the custom one.
        self.assertRedirects(self.client.get("/?auth=login&next=/manage/"), "/login/?next=%2Fmanage%2F", fetch_redirect_response=False)
        self.client.force_login(self.alice)
        self.assertIn("还没有可以浏览的存储源", self.client.get("/").content.decode())

    def test_a_whole_custom_document_is_sent_as_it_is(self):
        html = "<!DOCTYPE html><html><head><title>自己的</title></head><body>页面</body></html>"
        self.change(guest_home="custom", guest_home_html=html)
        self.assertEqual(self.client.get("/").content.decode(), html)
