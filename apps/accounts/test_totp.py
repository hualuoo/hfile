"""Two-step verification (apps/accounts/totp.py): the codes, setting it up and turning it off
(/api/v1/users/me/totp/...), and the login step "totp"."""

import base64
import json
import time

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.test import Client, SimpleTestCase, TestCase, override_settings
from rest_framework.test import APIClient, APITestCase

from apps.core import secrets
from apps.core.testing import TO_THE_SECOND, admin_url
from apps.siteconf.models import SiteSettings

from . import login as steps
from . import totp
from apps.activity.models import ActivityLog
from apps.activity.testing import logins

User = get_user_model()
SESSION = "/api/v1/session/"
STATE = "/api/v1/users/me/totp/"
SETUP = "/api/v1/users/me/totp/setup/"
DISABLE = "/api/v1/users/me/totp/disable/"


class CodeTests(SimpleTestCase):
    # RFC 6238, appendix B (SHA-1): the secret "12345678901234567890", 8 digits there, 6 here.
    SECRET = base64.b32encode(b"12345678901234567890").decode()

    def test_the_rfc_vectors(self):
        for seconds, code in ((59, "287082"), (1111111109, "081804"), (1234567890, "005924"), (2000000000, "279037")):
            self.assertEqual(totp.code_at(self.SECRET, seconds // 30), code)

    def test_the_window(self):
        now = 1234567890
        step = now // 30
        for offset, found in ((-1, step - 1), (0, step), (1, step + 1)):
            self.assertEqual(totp.matching_step(self.SECRET, totp.code_at(self.SECRET, step + offset), now=now), found)
        for offset in (-2, 2):
            self.assertIsNone(totp.matching_step(self.SECRET, totp.code_at(self.SECRET, step + offset), now=now))

    def test_secrets_and_addresses(self):
        secret = totp.new_secret()
        self.assertRegex(secret, r"^[A-Z2-7]{32}$")
        self.assertNotEqual(secret, totp.new_secret())
        address = totp.uri("JBSWY3DPEHPK3PXP", "老板 a", "我的 网盘")
        self.assertEqual(address, "otpauth://totp/%E6%88%91%E7%9A%84%20%E7%BD%91%E7%9B%98:%E8%80%81%E6%9D%BF%20a"
                                  "?secret=JBSWY3DPEHPK3PXP&issuer=%E6%88%91%E7%9A%84%20%E7%BD%91%E7%9B%98&algorithm=SHA1&digits=6&period=30")
        self.assertTrue(totp.qr_data_uri(address).startswith("data:image/svg+xml"))

    def test_what_was_typed(self):
        self.assertEqual(totp.clean_code(" 123 456 "), "123456")
        self.assertEqual(totp.clean_code("１２３４５６"), "123456")  # full-width, from an input method
        for typed in ("12345", "1234567", "12345a", "", None, 123456, "١٢٣٤٥٦"):
            self.assertIsNone(totp.clean_code(typed), typed)


def code_for(user, offset=0):
    """A code of `user`'s device right now (`offset` steps later)."""
    user.refresh_from_db()
    return totp.code_at(secrets.decrypt(user.totp_secret), totp.current_step() + offset)


def turn_on(user):
    """Two-step verification on for `user`, without the setup dialog."""
    user.totp_secret = secrets.encrypt(totp.new_secret())
    user.save(update_fields=["totp_secret"])


class SetupTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client.force_login(self.boss)

    def test_names_too_long_for_a_qr_code(self):
        # 30 and 150 characters beyond U+FFFF, 12 bytes each in the address, don't fit in a QR code: the secret
        # alone, to type in, not a 500 (ACCT-03).
        site = SiteSettings.load()
        site.site_name = chr(0x20000) * 30
        site.save()
        self.boss.username = chr(0x20000) * 150
        self.boss.save(update_fields=["username"])
        response = self.client.post(SETUP)
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(response.json()["qr"], "")
        self.assertRegex(response.json()["secret"], r"^[A-Z2-7]+$")

    def test_from_setup_to_on(self):
        self.assertEqual(self.client.get(STATE).json(), {"enabled": False, "enabled_at": None, "available": True})
        data = self.client.post(SETUP).json()
        self.assertEqual(set(data), {"secret", "uri", "qr", "expires_in"})
        self.assertIn(f"secret={data['secret']}", data["uri"])
        self.assertIn("otpauth://totp/HFile:boss?", data["uri"])
        # Waiting in the session, encrypted.
        pending = self.client.session[totp.SETUP_KEY]
        self.assertNotIn(data["secret"], json.dumps(pending))
        self.assertEqual(secrets.decrypt(pending["secret"]), data["secret"])
        code = totp.code_at(data["secret"], totp.current_step())
        r = self.client.post(STATE, {"code": code, "password": "wrong"})
        self.assertEqual((r.status_code, r.json()), (400, {"password": ["密码不对。"]}))
        r = self.client.post(STATE, {"code": "000000" if code != "000000" else "111111", "password": "pw-boss-2026"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("code", r.json())
        self.boss.refresh_from_db()
        self.assertFalse(self.boss.totp_enabled)
        r = self.client.post(STATE, {"code": code, "password": "pw-boss-2026"})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r.json()["enabled"], True)
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.totp_secret.startswith("enc:"))  # encrypted, never the secret itself
        self.assertNotIn(data["secret"], self.boss.totp_secret)
        self.assertEqual(secrets.decrypt(self.boss.totp_secret), data["secret"])
        self.assertIsNotNone(self.boss.totp_enabled_at)
        self.assertRegex(self.client.get(STATE).json()["enabled_at"], TO_THE_SECOND)  # TEXT-01: it was UTC
        self.assertNotIn(totp.SETUP_KEY, self.client.session)
        self.assertNotIn(data["secret"], self.client.get(STATE).content.decode())
        # On already: no second setup.
        self.assertEqual(self.client.post(SETUP).status_code, 409)
        self.assertEqual(self.client.post(STATE, {"code": code, "password": "pw-boss-2026"}).status_code, 409)

    def test_the_code_that_confirmed_cant_log_in(self):
        data = self.client.post(SETUP).json()
        code = totp.code_at(data["secret"], totp.current_step())
        self.client.post(STATE, {"code": code, "password": "pw-boss-2026"})
        browser = APIClient()
        browser.post(SESSION, {"username": "boss", "password": "pw-boss-2026"})
        r = browser.post(SESSION, {"step": "totp", "code": code})
        self.assertEqual(r.status_code, 400)

    def test_an_expired_or_missing_setup(self):
        self.assertEqual(self.client.post(STATE, {"code": "123456", "password": "pw-boss-2026"}).status_code, 409)
        self.client.post(SETUP)
        session = self.client.session
        session[totp.SETUP_KEY] = {**session[totp.SETUP_KEY], "until": time.time() - 1}
        session.save()
        r = self.client.post(STATE, {"code": "123456", "password": "pw-boss-2026"})
        self.assertEqual((r.status_code, r.json()["detail"]), (409, "设置已经过期了，请重新开始。"))

    def test_superusers_only(self):
        alice = User.objects.create_user("alice", password="pw-alice-2026", is_staff=True)
        client = APIClient()
        client.force_login(alice)
        self.assertEqual(client.get(STATE).json()["available"], False)
        self.assertEqual(client.post(SETUP).status_code, 403)
        self.assertEqual(client.post(STATE, {"code": "123456", "password": "pw-alice-2026"}).status_code, 403)
        self.assertEqual(APIClient().get(STATE).status_code, 401)
        self.assertEqual(APIClient().post(SETUP).status_code, 401)

    def test_turning_it_off(self):
        turn_on(self.boss)
        self.assertEqual(self.client.post(DISABLE, {"password": "wrong"}).json(), {"password": ["密码不对。"]})
        r = self.client.post(DISABLE, {"password": "pw-boss-2026"})
        self.assertEqual(r.json(), {"enabled": False, "enabled_at": None, "available": True})
        self.boss.refresh_from_db()
        self.assertEqual((self.boss.totp_secret, self.boss.totp_last_step), ("", None))

    @override_settings(PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher",
                                         "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher"])
    def test_a_rehashed_password_keeps_the_session(self):
        # A hash of an older kind: checking the password replaces it (Django's check_password), which
        # must not log out the session that checked it.
        self.boss.password = make_password("pw-boss-2026", hasher="pbkdf2_sha1")
        self.boss.save(update_fields=["password"])
        self.client.force_login(self.boss)
        turn_on(self.boss)
        self.assertEqual(self.client.post(DISABLE, {"password": "pw-boss-2026"}).status_code, 200)
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.password.startswith("md5$"))
        self.assertTrue(self.client.get(SESSION).json()["authenticated"])
        # Turning it on checks the password too.
        self.boss.password = make_password("pw-boss-2026", hasher="pbkdf2_sha1")
        self.boss.save(update_fields=["password"])
        self.client.force_login(self.boss)
        data = self.client.post(SETUP).json()
        code = totp.code_at(data["secret"], totp.current_step())
        self.assertEqual(self.client.post(STATE, {"code": code, "password": "pw-boss-2026"}).status_code, 200)
        self.assertTrue(self.client.get(SESSION).json()["authenticated"])

    def test_limited(self):
        for _ in range(20):
            self.client.post(DISABLE, {"password": "wrong"})
        self.assertEqual(self.client.post(DISABLE, {"password": "pw-boss-2026"}).status_code, 429)
        self.assertEqual(self.client.get(STATE).status_code, 200)  # reading isn't limited


class LoginTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        turn_on(self.boss)

    def password(self, client=None):
        return (client or self.client).post(SESSION, {"username": "boss", "password": "pw-boss-2026"})

    def test_the_step_after_the_password(self):
        r = self.password()
        self.assertEqual(r.json(), {"authenticated": False, "step": "totp", "before_password": False, "detail": "还要两步验证才能登录。"})
        self.assertEqual(self.client.get(SESSION).json()["step"], "totp")
        r = self.client.post(SESSION, {"step": "totp", "code": "12345"})
        self.assertEqual(r.json(), {"code": ["请输入手机上显示的 6 位数字。"]})
        wrong = "000000" if code_for(self.boss) != "000000" else "111111"
        r = self.client.post(SESSION, {"step": "totp", "code": wrong})
        self.assertEqual(r.json(), {"code": ["两步验证码不对，或者已经用过了，请输入新的。"]})
        self.assertEqual([(entry.reason, entry.user) for entry in logins()], [("totp", self.boss)])
        r = self.client.post(SESSION, {"step": "totp", "code": code_for(self.boss)})
        self.assertEqual(r.status_code, 201, r.json())
        self.assertTrue(self.client.get(SESSION).json()["authenticated"])
        self.assertEqual(ActivityLog.objects.filter(kind="login").count(), 1)

    def test_a_code_works_once(self):
        code = code_for(self.boss)
        self.password()
        self.assertEqual(self.client.post(SESSION, {"step": "totp", "code": code}).status_code, 201)
        other = APIClient()
        self.password(other)
        self.assertEqual(other.post(SESSION, {"step": "totp", "code": code}).status_code, 400)  # replayed
        self.assertEqual(other.post(SESSION, {"step": "totp", "code": code_for(self.boss, 1)}).status_code, 201)

    def test_five_wrong_answers_end_the_login(self):
        self.password()
        for _ in range(4):
            self.assertIn("code", self.client.post(SESSION, {"step": "totp", "code": "xx"}).json())
        r = self.client.post(SESSION, {"step": "totp", "code": "xx"})
        self.assertEqual(r.json(), {"step": ["错了太多次，请重新输入用户名和密码。"]})
        self.assertIsNone(self.client.get(SESSION).json()["step"])
        self.assertEqual(self.client.post(SESSION, {"step": "totp", "code": code_for(self.boss)}).status_code, 400)

    def test_ten_wrong_codes_lock_the_codes_a_while(self):
        wrong = "000000" if code_for(self.boss) != "000000" else "111111"
        for attempt in range(totp.FAILURES_MAX):
            client = APIClient(REMOTE_ADDR=f"203.0.113.{attempt}")  # from many addresses
            self.password(client)
            client.post(SESSION, {"step": "totp", "code": wrong})
        client = APIClient(REMOTE_ADDR="198.51.100.1")
        self.password(client)
        r = client.post(SESSION, {"step": "totp", "code": code_for(self.boss)})
        self.assertEqual(r.json(), {"code": ["两步验证码错了太多次，请过 15 分钟再试。"]})

    def test_an_unreadable_secret(self):
        # Stored under another secret_key (config.toml's was changed since).
        with override_settings(SECRET_KEY="another-secret-key-" + "x" * 40):
            self.boss.totp_secret = secrets.encrypt(totp.new_secret())
        self.boss.save(update_fields=["totp_secret"])
        self.password()
        r = self.client.post(SESSION, {"step": "totp", "code": "123456"})
        self.assertIn("hfile resetadmin", r.json()["code"][0])

    def test_off_again_no_step(self):
        self.boss.totp_secret = ""
        self.boss.save()
        self.assertEqual(self.password().status_code, 201)

    def test_not_in_the_django_admin(self):
        r = Client().post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        self.assertContains(r, "这个账号要在网站的登录框里登录")

    def test_registered_once(self):
        self.assertEqual([step.name for step in steps.STEPS], ["captcha", "totp"])


class PageTests(TestCase):
    def test_the_menu_item_for_superusers(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        browser = Client()
        browser.force_login(boss)
        page = browser.get("/").content.decode()
        self.assertIn('id="totp-open"', page)
        self.assertRegex(page, r'<script src="/static/accounts/js/totp\.js\?v=[0-9a-f]{12}"></script>')
        browser.force_login(alice)
        page = browser.get("/").content.decode()
        self.assertNotIn('id="totp-open"', page)
        turn_on(alice)  # had it on as a superuser once: may still turn it off
        self.assertIn('id="totp-open"', browser.get("/").content.decode())

    def test_the_list_shows_it(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        turn_on(boss)
        client = APIClient()
        client.force_authenticate(boss)
        rows = {row["username"]: row for row in client.get("/api/v1/manage/users/").json()["results"]}
        self.assertEqual((rows["boss"]["totp_enabled"], rows["guest"]["totp_enabled"]), (True, False))
        self.assertNotIn("totp_secret", rows["boss"])
