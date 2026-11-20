"""Users, logging in and out (GET/POST/DELETE /api/v1/session/, with its steps), changing one's password."""

import time
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import Client, TestCase, override_settings
from rest_framework import serializers
from rest_framework.test import APIClient, APITestCase

from apps.core.testing import admin_url

from . import login as steps
from .admin import HFileUserCreationForm

User = get_user_model()
PASSWORD = "pw-alice-2026"


class UserModelTests(TestCase):
    def test_the_fields(self):
        names = {field.name for field in User._meta.get_fields()}
        self.assertTrue({"username", "nickname", "email", "last_login_ip", "is_staff", "is_superuser"} <= names)
        self.assertFalse({"first_name", "last_name"} & names)
        user = User.objects.create_user("alice", password=PASSWORD)  # no email needed
        self.assertEqual((user.email, user.nickname, user.display_name, user.get_full_name()), ("", "", "alice", "alice"))
        user.nickname = "小艾"
        self.assertEqual((user.display_name, user.get_short_name()), ("小艾", "小艾"))

    def test_usernames_are_unique_whatever_their_case(self):
        User.objects.create_user("alice", password=PASSWORD)
        for name in ("Alice", "ALICE"):
            with self.subTest(name=name), self.assertRaises(IntegrityError), transaction.atomic():
                User.objects.create_user(name, password=PASSWORD)  # the database refuses it, SQLite and MySQL alike

    def test_the_django_admin_form_says_so(self):
        User.objects.create_user("alice", password=PASSWORD)
        form = HFileUserCreationForm(data={"username": "ALICE", "password1": "x-Strong-pw-2026", "password2": "x-Strong-pw-2026"})
        self.assertFalse(form.is_valid())
        self.assertIn("username", form.errors)
        form = HFileUserCreationForm(data={"username": "ｂｏｂ", "password1": "x-Strong-pw-2026", "password2": "x-Strong-pw-2026"})
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.save().username, "bob")  # full-width letters as ASCII ones


class SessionTests(APITestCase):
    url = "/api/v1/session/"

    def setUp(self):
        self.user = User.objects.create_user("alice", password=PASSWORD, nickname="小艾")

    def test_logging_in_and_out(self):
        state = self.client.get(self.url).json()
        self.assertEqual((state["authenticated"], state["user"], state["step"]), (False, None, None))
        self.assertTrue(state["csrf_token"])
        r = self.client.post(self.url, {"username": "ALICE", "password": PASSWORD})  # any case
        self.assertEqual(r.status_code, 201, r.json())
        self.assertEqual(r.json()["authenticated"], True)
        self.assertEqual({key: r.json()["user"][key] for key in ("username", "nickname", "is_staff")},
                         {"username": "alice", "nickname": "小艾", "is_staff": False})
        self.assertNotIn("password", r.json()["user"])
        self.assertEqual(self.client.get(self.url).json()["user"]["username"], "alice")
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertFalse(self.client.get(self.url).json()["authenticated"])
        self.assertEqual(self.client.delete(self.url).status_code, 204)  # for anybody

    def test_full_width_letters_are_the_ascii_ones(self):
        self.assertEqual(self.client.post(self.url, {"username": "ａｌｉｃｅ", "password": PASSWORD}).status_code, 201)

    def test_wrong_and_missing(self):
        for body in ({"username": "alice", "password": "nope"}, {"username": "nobody", "password": PASSWORD}):
            r = self.client.post(self.url, body)
            self.assertEqual((r.status_code, r.json()), (400, {"non_field_errors": ["用户名或密码不对。"]}))
        r = self.client.post(self.url, {})
        self.assertEqual(r.json(), {"username": ["要填用户名。"], "password": ["要填密码。"]})
        self.user.is_active = False  # 停用
        self.user.save()
        self.assertEqual(self.client.post(self.url, {"username": "alice", "password": PASSWORD}).status_code, 400)

    def test_logging_in_checks_csrf(self):
        csrf = APIClient(enforce_csrf_checks=True)
        r = csrf.post(self.url, {"username": "alice", "password": PASSWORD}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "csrf_failed"))
        token = csrf.get(self.url).json()["csrf_token"]
        r = csrf.post(self.url, {"username": "alice", "password": PASSWORD}, format="json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 201)

    def test_the_login_ip(self):
        self.client.post(self.url, {"username": "alice", "password": PASSWORD}, REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, "10.1.2.3")  # proxies = 0: the header is ignored
        self.assertIsNotNone(self.user.last_login)
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}):  # [server] proxies = 1
            self.client.post(self.url, {"username": "alice", "password": PASSWORD}, REMOTE_ADDR="10.1.2.3", HTTP_X_FORWARDED_FOR="203.0.113.9")
        self.user.refresh_from_db()
        self.assertEqual(self.user.last_login_ip, "203.0.113.9")

    def test_at_most_ten_a_minute_per_address(self):
        for _ in range(10):
            self.client.post(self.url, {"username": "alice", "password": "wrong"}, REMOTE_ADDR="2001:db8::1")
        r = self.client.post(self.url, {"username": "alice", "password": PASSWORD}, REMOTE_ADDR="2001:db8::2")  # the same /64
        self.assertEqual(r.status_code, 429)
        self.assertRegex(r.json()["detail"], r"^太频繁了，请 \d+ 秒后再试。$")
        self.assertIn("Retry-After", r)
        self.client.force_login(self.user)  # logged in already: still per address
        self.assertEqual(self.client.post(self.url, {"username": "alice", "password": PASSWORD}, REMOTE_ADDR="2001:db8::3").status_code, 429)
        self.assertEqual(self.client.post(self.url, {"username": "alice", "password": PASSWORD}, REMOTE_ADDR="203.0.113.5").status_code, 201)


class LoginStepTests(APITestCase):
    """apps/accounts/login.py: a login can ask for more than the password; here with made-up steps (the real
    ones, captcha and totp, are apps/accounts/login_steps.py's and aren't needed by these users)."""

    url = "/api/v1/session/"

    def setUp(self):
        self.alice = User.objects.create_user("alice", password=PASSWORD)
        self.bob = User.objects.create_user("bob", password="pw-bob-2026")

        def check_code(request, user, data):
            if "code" not in data:
                raise serializers.ValidationError({"code": ["要填验证码。"]})
            if data["code"] != f"{user.username}-123":
                raise serializers.ValidationError({"code": ["验证码不对。"]})

        def check_picture(request, user, data):
            if "picture" not in data:
                raise steps.StepRequired
            if data["picture"] != "ABCD":
                raise serializers.ValidationError({"picture": ["图片里的字不对。"]})

        steps.register(steps.Step("demo_code", "演示验证", needed=lambda request, user: user.username == "alice", check=check_code))
        self.addCleanup(steps.unregister, "demo_code")
        self.picture = False
        steps.register(steps.Step("demo_picture", "演示图片", needed=lambda request, user: self.picture,
                                  check=check_picture, before_password=True))
        self.addCleanup(steps.unregister, "demo_picture")

    def test_a_step_after_the_password(self):
        r = self.client.post(self.url, {"username": "alice", "password": PASSWORD})
        self.assertEqual((r.status_code, r.json()), (200, {
            "authenticated": False, "step": "demo_code", "before_password": False, "detail": "还要演示验证才能登录。"}))
        state = self.client.get(self.url).json()
        self.assertEqual((state["authenticated"], state["step"]), (False, "demo_code"))  # a page loaded again knows
        r = self.client.post(self.url, {"step": "demo_code", "code": "000"})
        self.assertEqual((r.status_code, r.json()), (400, {"code": ["验证码不对。"]}))
        self.assertFalse(self.client.get(self.url).json()["authenticated"])
        r = self.client.post(self.url, {"step": "demo_code", "code": "alice-123"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["user"]["username"], "alice")
        self.assertEqual(self.client.get(self.url).json()["step"], None)
        # Bob doesn't need it.
        self.client.delete(self.url)
        self.assertEqual(self.client.post(self.url, {"username": "bob", "password": "pw-bob-2026"}).status_code, 201)

    def test_a_step_without_its_password(self):
        r = self.client.post(self.url, {"step": "demo_code", "code": "alice-123"})  # nothing begun in this session
        self.assertEqual((r.status_code, r.json()), (400, {"step": ["登录已经过期了，请重新输入用户名和密码。"]}))
        self.client.post(self.url, {"username": "alice", "password": PASSWORD})
        r = self.client.post(self.url, {"step": "other", "code": "x"})
        self.assertEqual(r.json(), {"step": ["现在要的是「演示验证」这一步。"]})
        # Too late, or the password changed meanwhile: from the start again.
        with mock.patch.object(steps.time, "time", return_value=time.time() + steps.PENDING_SECONDS + 1):
            self.assertEqual(self.client.post(self.url, {"step": "demo_code", "code": "alice-123"}).status_code, 400)
        self.client.post(self.url, {"username": "alice", "password": PASSWORD})
        self.alice.set_password("pw-alice-2027")
        self.alice.save()
        r = self.client.post(self.url, {"step": "demo_code", "code": "alice-123"})
        self.assertEqual((r.status_code, r.json()), (400, {"step": ["登录已经过期了，请重新输入用户名和密码。"]}))
        self.assertFalse(self.client.get(self.url).json()["authenticated"])

    def test_a_step_before_the_password(self):
        self.picture = True
        r = self.client.post(self.url, {"username": "bob", "password": "pw-bob-2026"})
        self.assertEqual((r.status_code, r.json()["step"], r.json()["before_password"]), (200, "demo_picture", True))
        r = self.client.post(self.url, {"username": "bob", "password": "pw-bob-2026", "picture": "XXXX"})
        self.assertEqual((r.status_code, r.json()), (400, {"picture": ["图片里的字不对。"]}))
        # Checked before the password: a wrong answer says nothing about the password.
        r = self.client.post(self.url, {"username": "bob", "password": "wrong", "picture": "XXXX"})
        self.assertEqual(r.json(), {"picture": ["图片里的字不对。"]})
        r = self.client.post(self.url, {"username": "bob", "password": "pw-bob-2026", "picture": "ABCD"})
        self.assertEqual(r.status_code, 201)

    def test_the_django_admin_doesnt_go_round_them(self):
        self.alice.is_staff = True
        self.alice.save()
        r = Client().post(admin_url("login/"), {"username": "alice", "password": PASSWORD})
        self.assertEqual(r.status_code, 200)  # the form again, not a redirect: not logged in
        self.assertContains(r, "这个账号要在网站的登录框里登录")
        steps.unregister("demo_code")
        r = Client().post(admin_url("login/"), {"username": "alice", "password": PASSWORD})
        self.assertEqual(r.status_code, 302)

    def test_names_are_unique(self):
        with self.assertRaises(ValueError):
            steps.register(steps.Step("demo_code", "又一个", needed=lambda r, u: False, check=lambda r, u, d: None))


class DjangoAdminLoginTests(TestCase):
    def test_it_counts_in_the_same_budget(self):
        User.objects.create_user("boss", password="pw-boss-2026", is_staff=True)
        browser = Client()
        for _ in range(10):
            browser.post(admin_url("login/"), {"username": "boss", "password": "wrong"})
        r = browser.post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        self.assertContains(r, "登录太频繁了")
        api = APIClient()
        self.assertEqual(api.post("/api/v1/session/", {"username": "boss", "password": "pw-boss-2026"}, format="json").status_code, 429)


class PasswordTests(APITestCase):
    url = "/api/v1/users/me/password/"

    def setUp(self):
        self.user = User.objects.create_user("alice", password=PASSWORD, nickname="小艾")

    def login(self, client):
        response = client.post("/api/v1/session/", {"username": "alice", "password": PASSWORD}, format="json")
        self.assertEqual(response.status_code, 201)

    def test_a_login_lasts_30_days(self):
        """ACCT-06: Django's default is 14 days."""
        response = self.client.post("/api/v1/session/", {"username": "alice", "password": PASSWORD}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.cookies["hfile_session"]["max-age"], 30 * 24 * 3600)
        self.assertEqual(self.client.session.get_expiry_age(), 30 * 24 * 3600)

    def test_this_session_stays_and_the_others_end(self):
        other = APIClient()
        self.login(other)
        self.login(self.client)
        r = self.client.put(self.url, {"old_password": PASSWORD, "new_password": "a-New-pass-2027"})
        self.assertEqual(r.status_code, 204)
        self.assertTrue(self.client.get("/api/v1/session/").json()["authenticated"])
        self.assertFalse(other.get("/api/v1/session/").json()["authenticated"])
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("a-New-pass-2027"))

    def test_mistakes(self):
        self.login(self.client)
        for body, errors in (
            ({"old_password": "wrong", "new_password": "a-New-pass-2027"}, {"old_password": ["原密码不对。"]}),
            ({"old_password": PASSWORD, "new_password": PASSWORD}, {"new_password": ["新密码不能和原密码一样。"]}),
            ({"old_password": PASSWORD, "new_password": "x" * 129}, {"new_password": ["密码最多 128 个字符。"]}),
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.put(self.url, body).json(), errors)
        weak = self.client.put(self.url, {"old_password": PASSWORD, "new_password": "12345678"}).json()["new_password"]
        self.assertTrue(weak and all("密码" in message for message in weak), weak)  # Django's own, in Chinese

    def test_logged_in_only_and_at_most_ten_an_hour(self):
        self.assertEqual(self.client.put(self.url, {"old_password": PASSWORD, "new_password": "a-New-pass-2027"}).status_code, 401)
        self.client.force_authenticate(self.user)
        for _ in range(10):
            self.client.put(self.url, {"old_password": "wrong", "new_password": "a-New-pass-2027"})
        self.assertEqual(self.client.put(self.url, {"old_password": PASSWORD, "new_password": "a-New-pass-2027"}).status_code, 429)
