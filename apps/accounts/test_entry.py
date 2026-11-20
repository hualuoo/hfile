"""The secure login entry (apps/accounts/entry.py, /login/<entry>/) and 显示登录入口 (show_login)."""

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from rest_framework.test import APIClient, APITestCase

from apps.core import masking
from apps.core.testing import admin_url
from apps.siteconf.models import SiteSettings

from . import entry
from . import totp
from .models import LoginLog
from .test_totp import code_for, turn_on

User = get_user_model()
SESSION = "/api/v1/session/"
ENTRY = "door-42"


def set_site(**fields):
    site = SiteSettings.load()
    for name, value in fields.items():
        setattr(site, name, value)
    site.save()


class WithoutAnEntryTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def test_the_login_page_and_the_dialog(self):
        r = self.client.get("/login/")
        self.assertEqual(r.status_code, 200)
        page = r.content.decode()
        self.assertIn('id="login-form"', page)
        self.assertNotIn("data-entry", page)
        self.assertNotIn('id="login-dialog"', page)  # the page has the form itself
        self.assertNotIn("data-login>", page)  # and no button for a dialog that isn't there
        self.assertEqual(page.count('id="login-username"'), 1)
        self.assertEqual(self.client.get("/login/door-42/").status_code, 404)
        home = self.client.get("/").content.decode()
        self.assertIn('id="login-dialog"', home)
        self.assertIn("data-login>", home)
        self.assertEqual(APIClient().post(SESSION, {"username": "alice", "password": "pw-alice-2026"}, format="json").status_code, 201)

    def test_show_login_only_hides_the_button(self):
        set_site(show_login=False)
        home = self.client.get("/").content.decode()
        self.assertNotIn("data-login>", home)
        self.assertIn('id="login-dialog"', home)  # ?auth=login and the login page still work
        self.assertEqual(self.client.get("/login/").status_code, 200)
        self.assertEqual(APIClient().post(SESSION, {"username": "alice", "password": "pw-alice-2026"}, format="json").status_code, 201)

    def test_logged_in_already(self):
        self.client.force_login(self.alice)
        self.assertRedirects(self.client.get("/login/?next=/manage/"), "/manage/", fetch_redirect_response=False)
        for target in ("https://evil.example/", "//evil.example/", "/\\evil.example"):
            r = self.client.get("/login/", {"next": target})
            self.assertEqual(r["Location"], "/", target)

    def test_without_an_entry_nothing_to_guess(self):
        for _ in range(12):
            self.assertEqual(self.client.get("/login/x/").status_code, 404)
        self.assertEqual(self.client.get("/login/").status_code, 200)

    def test_only_get(self):
        self.assertEqual(self.client.post("/login/", {"username": "alice", "password": "pw-alice-2026"}).status_code, 405)


class WithAnEntryTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        set_site(secure_login_entry=ENTRY)

    def login(self, client=None, **extra):
        return (client or APIClient()).post(SESSION, {"username": "alice", "password": "pw-alice-2026", **extra}, format="json")

    def test_only_the_entry_logs_in(self):
        for body in ({}, {"entry": ""}, {"entry": "door-41"}, {"entry": "DOOR-42"}, {"entry": ["door-42"]}):
            with self.subTest(body=body):
                r = self.login(**body)
                self.assertEqual((r.status_code, r.json()), (404, {"detail": "没有找到。"}))
        self.assertFalse(LoginLog.objects.exists())  # never got to the password
        self.assertEqual(self.login(entry=ENTRY).status_code, 201)
        self.assertEqual(APIClient().post(SESSION, ["x"], format="json").status_code, 404)
        self.assertEqual(APIClient().delete(SESSION).status_code, 204)  # logging out needs none

    def test_the_pages(self):
        browser = Client()
        for path in ("/login/", "/login/door-41/", "/login/DOOR-42/", "/login/door-42/x/", "/login", "/login/door-41"):
            with self.subTest(path=path):
                self.assertIn(browser.get(path).status_code, (301, 404))
                self.assertEqual(browser.get(path, follow=True).status_code, 404)
        self.assertRedirects(browser.get("/login/door-42"), f"/login/{ENTRY}/", status_code=301)
        r = browser.get(f"/login/{ENTRY}/")
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, f'data-entry="{ENTRY}"')
        home = browser.get("/").content.decode()
        self.assertNotIn("data-login>", home)  # no button
        self.assertNotIn('id="login-dialog"', home)  # and no dialog

    def test_guessing_the_entry_is_slow(self):
        # Wrong entries count in the login budget of the address (10 a minute; an IPv6 address by its /64).
        for number in range(10):
            guess = Client(REMOTE_ADDR=f"2001:db8::{number + 1}")
            self.assertEqual(guess.get(f"/login/guess-{number}/").status_code, 404)
        browser = Client(REMOTE_ADDR="2001:db8::77")
        r = browser.get(f"/login/{ENTRY}/")
        self.assertEqual(r.status_code, 429)  # the right one too, meanwhile: it looks like a wrong one
        self.assertIn("Retry-After", r)
        self.assertEqual(browser.get("/login/").status_code, 429)
        api = APIClient(REMOTE_ADDR="2001:db8::78")  # one budget with logging in
        self.assertEqual(self.login(api, entry=ENTRY).status_code, 429)
        # Other addresses aren't affected, and opening the right entry costs nothing.
        other = Client(REMOTE_ADDR="203.0.113.5")
        for _ in range(12):
            self.assertEqual(other.get(f"/login/{ENTRY}/").status_code, 200)
        self.assertEqual(self.login(APIClient(REMOTE_ADDR="203.0.113.5"), entry=ENTRY).status_code, 201)

    def test_with_two_step_verification(self):
        set_site(secure_login_entry="")
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        turn_on(boss)
        set_site(secure_login_entry=ENTRY)
        client = APIClient()
        r = client.post(SESSION, {"username": "boss", "password": "pw-boss-2026", "entry": ENTRY}, format="json")
        self.assertEqual(r.json()["step"], "totp")
        self.assertEqual(client.post(SESSION, {"step": "totp", "code": code_for(boss)}, format="json").status_code, 404)
        r = client.post(SESSION, {"step": "totp", "code": code_for(boss), "entry": ENTRY}, format="json")
        self.assertEqual(r.status_code, 201, r.json())

    def test_the_django_admin_refuses(self):
        User.objects.create_superuser("boss", password="pw-boss-2026")
        browser = Client()
        page = browser.get(admin_url("login/"))
        self.assertContains(page, "设了登录安全入口")
        r = browser.post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "请从安全入口登录网站")
        self.assertNotIn("_auth_user_id", browser.session)
        # Logged in at the entry, the admin opens.
        site = Client()
        site.post(SESSION, {"username": "boss", "password": "pw-boss-2026", "entry": ENTRY}, content_type="application/json")
        self.assertEqual(site.get(admin_url()).status_code, 200)

    def test_helpers(self):
        self.assertEqual((entry.current(), entry.login_path()), (ENTRY, f"/login/{ENTRY}/"))
        set_site(secure_login_entry="")
        self.assertEqual((entry.current(), entry.login_path()), ("", "/login/"))
        self.assertFalse(entry.matches(None, ENTRY))
        self.assertTrue(entry.matches(ENTRY, ENTRY))
        self.assertEqual(totp.DIGITS, 6)


class MaskingTests(SimpleTestCase):
    """The entry is a secret in its address: the access log and the console write /login/<entry>/."""

    def test_masked_wherever_it_is_an_address(self):
        for path, query, logged in (
            ("/login/door-42/", "", "/login/<entry>/"),
            ("/login/door-42", "", "/login/<entry>"),
            ("/login/door-42/", "next=%2Fmanage%2F", "/login/<entry>/?next=/manage/"),
            ("/", "next=%2Flogin%2Fdoor-42%2F", "/?next=/login/<entry>/"),
        ):
            self.assertEqual(masking.mask_address(path, query), logged)
        self.assertEqual(masking.mask_text('"GET /login/door-42/ HTTP/1.1" 404 179'), '"GET /login/<entry>/ HTTP/1.1" 404 179')
        self.assertEqual(masking.mask_text("登录地址：https://files.example.com/login/door-42/"), "登录地址：https://files.example.com/login/<entry>/")

    def test_in_the_access_log(self):
        from apps.core.access_log import AccessLog
        from apps.core.tests import no_start, request, site

        with self.assertLogs("access", "INFO") as logs:
            AccessLog(site("200 OK"))(request(path="/login/door-42/", query="next=%2Fmanage%2F"), no_start)
        line = logs.records[0].getMessage()
        self.assertIn('"/login/<entry>/?next=/manage/"', line)
        self.assertNotIn("door-42", line)

    def test_other_addresses_stay(self):
        for path, query in (("/login/", ""), ("/login/", "next=%2Fmanage%2F"), ("/sso/github/login/callback", ""),
                            ("/api/v1/manage/login-logs/", ""), ("/files/login/notes.txt", "")):
            self.assertEqual(masking.mask_address(path, query), path + ("?" + query.replace("%2F", "/") if query else ""))
