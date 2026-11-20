"""The login log (apps/accounts/loginlog.py, models.LoginLog) and /manage/#login-log."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.core.testing import admin_url
from apps.siteconf.models import SiteSettings

from . import login as steps
from .models import LoginLog

User = get_user_model()
SESSION = "/api/v1/session/"
URL = "/api/v1/manage/login-logs/"
UA = "Mozilla/5.0 (X11; Linux x86_64) HFileTest/1.0"


def set_site(**fields):
    site = SiteSettings.load()
    for name, value in fields.items():
        setattr(site, name, value)
    site.save()


class RecordingTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def login(self, username, password, client=None, **extra):
        headers = {"HTTP_USER_AGENT": UA, "REMOTE_ADDR": "203.0.113.7", **extra}
        return (client or APIClient()).post(SESSION, {"username": username, "password": password}, format="json", **headers)

    def only_entry(self):
        self.assertEqual(LoginLog.objects.count(), 1)
        return LoginLog.objects.get()

    def test_a_login(self):
        self.assertEqual(self.login("ALICE", "pw-alice-2026").status_code, 201)
        entry = self.only_entry()
        self.assertEqual((entry.username, entry.user, entry.ip, entry.user_agent, entry.result, entry.reason, entry.source),
                         ("alice", self.alice, "203.0.113.7", UA, "success", "", "site"))
        self.assertLess(timezone.now() - entry.created_at, timedelta(minutes=1))

    def test_failures_and_why(self):
        guest = User.objects.guest()
        guest.set_password("guest-pass-2026")
        guest.save()
        bob = User.objects.create_user("bob", password="pw-bob-2026", is_active=False)
        for username, password, user, reason in (
            ("nobody", "x-secret-1", None, "unknown_user"),
            ("Alice", "x-secret-2", self.alice, "wrong_password"),
            ("bob", "pw-bob-2026", bob, "inactive"),
            ("guest", "guest-pass-2026", guest, "builtin"),
        ):
            with self.subTest(reason=reason):
                LoginLog.objects.all().delete()
                self.assertEqual(self.login(username, password).status_code, 400)
                entry = self.only_entry()
                self.assertEqual((entry.username, entry.user, entry.result, entry.reason), (username, user, "failure", reason))

    def test_never_a_password(self):
        self.login("alice", "x-wrong-secret-77")
        self.login("alice", "pw-alice-2026")
        for entry in LoginLog.objects.all():
            for value in (entry.username, entry.user_agent, entry.reason, entry.source):
                self.assertNotIn("secret", value)
                self.assertNotIn("pw-alice", value)
        self.assertNotIn("password", {field.name for field in LoginLog._meta.get_fields()})

    def test_forms_that_never_got_checked(self):
        APIClient().post(SESSION, {"username": "alice"}, format="json")  # no password
        APIClient().post(SESSION, {"username": "a" * 151, "password": "x"}, format="json")
        self.assertFalse(LoginLog.objects.exists())

    def test_turned_off(self):
        set_site(login_log=False)
        self.login("alice", "pw-alice-2026")
        self.login("alice", "wrong")
        self.assertFalse(LoginLog.objects.exists())

    def test_the_rate_limit_once_a_minute(self):
        client = APIClient()
        for _ in range(10):
            self.login("alice", "wrong", client)
        self.assertEqual(LoginLog.objects.filter(reason="wrong_password").count(), 10)
        for _ in range(5):
            self.assertEqual(self.login("alice", "pw-alice-2026", client).status_code, 429)
        throttled = LoginLog.objects.filter(reason="throttled")
        self.assertEqual(throttled.count(), 1)
        self.assertEqual((throttled.get().username, throttled.get().ip), ("alice", "203.0.113.7"))
        r = client.post(SESSION, "{not json", content_type="application/json", REMOTE_ADDR="203.0.113.7")
        self.assertEqual(r.status_code, 429)  # an unreadable body is still just a 429

    def test_long_values_are_cut(self):
        self.login("alice", "pw-alice-2026", HTTP_USER_AGENT="x" * 5000)
        self.assertEqual(len(self.only_entry().user_agent), 500)

    def test_a_deleted_user_keeps_the_entries(self):
        self.login("alice", "pw-alice-2026")
        self.alice.delete()
        entry = self.only_entry()
        self.assertEqual((entry.username, entry.user), ("alice", None))


class DjangoAdminLoginTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")

    def test_success_and_failure_there(self):
        browser = Client(HTTP_USER_AGENT=UA)
        browser.post(admin_url("login/"), {"username": "boss", "password": "wrong"})
        browser.post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        entries = list(LoginLog.objects.order_by("id").values_list("result", "reason", "source", "user_agent"))
        self.assertEqual(entries, [("failure", "wrong_password", "admin", UA), ("success", "", "admin", UA)])

    def test_an_account_with_a_step(self):
        steps.register(steps.Step("demo", "演示", needed=lambda request, user: True, check=lambda r, u, d: None))
        self.addCleanup(steps.unregister, "demo")
        Client().post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        entry = LoginLog.objects.get()
        self.assertEqual((entry.result, entry.reason, entry.user), ("failure", "needs_steps", self.boss))

    def test_the_rate_limit_there(self):
        browser = Client()
        for _ in range(12):
            browser.post(admin_url("login/"), {"username": "boss", "password": "wrong"})
        self.assertEqual(LoginLog.objects.filter(reason="throttled").count(), 1)


class LoginLogApiTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        now = timezone.now()
        self.old = LoginLog.objects.create(username="alice", user=self.alice, ip="192.168.1.20", result="success",
                                           source="site", created_at=now - timedelta(days=10))
        self.wrong = LoginLog.objects.create(username="Alice", user=self.alice, ip="192.168.1.21", result="failure",
                                             reason="wrong_password", source="site", user_agent=UA)
        self.ghost = LoginLog.objects.create(username="root", ip="2001:db8::1", result="failure", reason="unknown_user",
                                             source="admin")
        self.client.force_authenticate(self.boss)

    def ids(self, query=""):
        r = self.client.get(URL + query)
        self.assertEqual(r.status_code, 200, r.json())
        return [entry["id"] for entry in r.json()["results"]]

    def test_admins_only(self):
        self.assertEqual(APIClient().get(URL).status_code, 401)
        user = APIClient()
        user.force_authenticate(self.alice)
        self.assertEqual(user.get(URL).status_code, 403)
        self.assertEqual(user.delete(URL).status_code, 403)
        self.assertEqual(user.post(URL + "delete/", {"ids": [self.old.pk]}).status_code, 403)
        self.assertEqual(LoginLog.objects.count(), 3)

    def test_the_list(self):
        data = self.client.get(URL).json()
        self.assertEqual(data["count"], 3)
        newest = data["results"][0]
        self.assertEqual(newest["id"], self.ghost.pk)  # newest first
        self.assertEqual((newest["reason_label"], newest["source_label"], newest["user"], newest["result_label"]),
                         ("没有这个用户", "后台", None, "失败"))
        wrong = data["results"][1]
        self.assertEqual(wrong["user"], {"id": self.alice.pk, "username": "alice", "nickname": ""})
        self.assertEqual(wrong["user_agent"], UA)

    def test_filters(self):
        today = timezone.localdate()
        self.assertEqual(self.ids("?username=ALI"), [self.wrong.pk, self.old.pk])
        self.assertEqual(self.ids("?ip=192.168.1.2"), [self.wrong.pk, self.old.pk])
        self.assertEqual(self.ids("?ip=2001:DB8"), [self.ghost.pk])
        self.assertEqual(self.ids("?result=failure"), [self.ghost.pk, self.wrong.pk])
        self.assertEqual(self.ids(f"?since={today}"), [self.ghost.pk, self.wrong.pk])
        self.assertEqual(self.ids(f"?until={today - timedelta(days=1)}"), [self.old.pk])
        for query, field in (("?ip=192.168.1.0/24", "ip"), ("?result=maybe", "result"), ("?since=2026-13-01", "since"),
                             (f"?since={today}&until={today - timedelta(days=1)}", "until")):
            with self.subTest(query=query):
                r = self.client.get(URL + query)
                self.assertEqual(r.status_code, 400)
                self.assertIn(field, r.json())

    def test_delete_the_ticked(self):
        r = self.client.post(URL + "delete/", {"ids": [self.old.pk, self.ghost.pk, 999999]})
        self.assertEqual(r.json(), {"deleted": 2})
        self.assertEqual(list(LoginLog.objects.values_list("pk", flat=True)), [self.wrong.pk])
        for body in ({}, {"ids": []}, {"ids": "1"}, {"ids": [0]}, {"ids": list(range(1, 1002))}):
            self.assertEqual(self.client.post(URL + "delete/", body).status_code, 400, body)

    def test_delete_what_the_filters_find(self):
        r = self.client.delete(URL + "?result=failure")
        self.assertEqual(r.json(), {"deleted": 2})
        self.assertEqual(list(LoginLog.objects.values_list("pk", flat=True)), [self.old.pk])
        self.assertEqual(self.client.delete(URL + "?ip=bad/").status_code, 400)
        self.assertEqual(self.client.delete(URL).json(), {"deleted": 1})  # no filter: all of it
        self.assertFalse(LoginLog.objects.exists())

    def test_a_condition_that_isnt_read_deletes_nothing(self):
        """API-02: a parameter the filters don't know (a typo, since[0], Since, the list's page) or the
        conditions sent in a body would be dropped and the delete widened to all of it: 400 instead."""
        for query in ("?resut=failure", "?since[0]=2026-01-01", "?Since=2026-01-01", "?result=failure&page=1"):
            with self.subTest(query=query):
                r = self.client.delete(URL + query)
                self.assertEqual(r.status_code, 400, r.json())
                self.assertEqual(list(r.json()), [query[1:].split("=")[0] if "&" not in query else "page"])
        for body in ({"ids": [self.old.pk]}, {"result": "failure"}):
            with self.subTest(body=body):
                r = self.client.delete(URL, body, format="json")
                self.assertEqual((r.status_code, r.json()), (400, {"detail": "按条件删除时，条件要写在地址的查询参数里，不收请求体。"}))
        self.assertEqual(LoginLog.objects.count(), 3)
        self.assertEqual(self.client.get(URL + "?resut=failure&_=1").json()["count"], 3)  # GET still ignores them

    def test_delete_only_what_the_list_showed(self):
        """API-10: what was logged after the list was shown stays (?newest=, from the list's answer)."""
        listed = self.client.get(URL + "?result=failure").json()
        self.assertEqual(listed["newest"], self.ghost.pk)
        late = LoginLog.objects.create(username="eve", ip="10.0.0.1", result="failure", reason="unknown_user", source="site")
        self.assertEqual(self.client.delete(URL + f"?result=failure&newest={listed['newest']}").json(), {"deleted": 2})
        self.assertEqual(set(LoginLog.objects.values_list("pk", flat=True)), {self.old.pk, late.pk})
        self.assertEqual(self.client.delete(URL + "?newest=0").status_code, 400)
        self.assertEqual(self.client.get(URL + "?username=nobody").json()["newest"], None)


class PageTests(TestCase):
    def test_the_section(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        browser = Client()
        browser.force_login(boss)
        page = browser.get("/manage/").content.decode()
        self.assertIn('id="section-login-log"', page)
        self.assertRegex(page, r'<script src="/static/accounts/js/manage_login_log\.js\?v=[0-9a-f]{12}"></script>')
        self.assertNotIn('id="login-log-off"', page)
        set_site(login_log=False)
        self.assertIn('id="login-log-off"', browser.get("/manage/").content.decode())
