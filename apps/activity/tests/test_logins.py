"""Logins in the activity log (apps/accounts/loginlog.py): what the login log used to keep."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts import login as steps
from apps.core.testing import admin_url

from ..models import ActivityLog
from ..testing import entries, logins, only_login

User = get_user_model()
SESSION = "/api/v1/session/"
UA = "Mozilla/5.0 (X11; Linux x86_64) HFileTest/1.0"


class RecordingTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")

    def login(self, username, password, client=None, **extra):
        headers = {"HTTP_USER_AGENT": UA, "REMOTE_ADDR": "203.0.113.7", **extra}
        return (client or APIClient()).post(SESSION, {"username": username, "password": password}, format="json", **headers)

    def test_a_login(self):
        self.assertEqual(self.login("ALICE", "pw-alice-2026").status_code, 201)
        entry = ActivityLog.objects.get()
        self.assertEqual((entry.kind, entry.actor, entry.actor_name, entry.target, entry.ip, entry.user_agent, entry.detail),
                         ("login", self.alice, "alice", None, "203.0.113.7", UA, {"source": "site"}))
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
                ActivityLog.objects.all().delete()
                self.assertEqual(self.login(username, password).status_code, 400)
                entry = only_login(self)
                self.assertEqual((entry.username, entry.user, entry.result, entry.reason), (username, user, "failure", reason))
                row = ActivityLog.objects.get()
                self.assertEqual((row.kind, row.actor, row.target), ("login_failed", None, user))
        # What was typed, kept beside the account's name when it differs.
        self.assertEqual(ActivityLog.objects.get().target_name, "guest")
        ActivityLog.objects.all().delete()
        self.login("Alice", "wrong")
        row = ActivityLog.objects.get()
        self.assertEqual((row.target_name, row.detail), ("alice", {"source": "site", "reason": "wrong_password", "username": "Alice"}))

    def test_never_a_password(self):
        self.login("alice", "x-wrong-secret-77")
        self.login("nobody-x-secret-78", "x-wrong-secret-79")
        self.login("alice", "pw-alice-2026")
        self.assertEqual(ActivityLog.objects.count(), 3)
        for row in ActivityLog.objects.values():
            text = repr(row)
            for secret in ("x-wrong-secret-77", "x-wrong-secret-79", "pw-alice"):
                self.assertNotIn(secret, text)

    def test_forms_that_never_got_checked(self):
        APIClient().post(SESSION, {"username": "alice"}, format="json")  # no password
        APIClient().post(SESSION, {"username": "a" * 151, "password": "x"}, format="json")
        self.assertFalse(ActivityLog.objects.exists())

    def test_the_rate_limit_once_a_minute(self):
        client = APIClient()
        for _ in range(10):
            self.login("alice", "wrong", client)
        self.assertEqual([entry.reason for entry in logins()], ["wrong_password"] * 10)
        for _ in range(5):
            self.assertEqual(self.login("alice", "pw-alice-2026", client).status_code, 429)
        throttled = [entry for entry in logins() if entry.reason == "throttled"]
        self.assertEqual([(entry.username, entry.ip, entry.user) for entry in throttled], [("alice", "203.0.113.7", None)])
        r = client.post(SESSION, "{not json", content_type="application/json", REMOTE_ADDR="203.0.113.7")
        self.assertEqual(r.status_code, 429)  # an unreadable body is still just a 429

    def test_long_values_are_cut(self):
        self.login("alice", "pw-alice-2026", HTTP_USER_AGENT="x" * 5000)
        self.login("b" * 150, "wrong")
        first, second = entries()
        self.assertEqual((len(first.user_agent), len(second.target_name)), (500, 150))

    def test_a_deleted_user_keeps_the_entries(self):
        self.login("alice", "pw-alice-2026")
        self.login("alice", "wrong")
        self.alice.delete()
        login, failed = entries()
        self.assertEqual((login.actor, login.actor_name, failed.target, failed.target_name),
                         (None, "alice", None, "alice"))

    def test_logging_out_isnt_logged(self):
        client = APIClient()
        self.login("alice", "pw-alice-2026", client)
        client.delete(SESSION)
        self.assertEqual([entry.kind for entry in entries()], ["login"])


class DjangoAdminLoginTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")

    def test_success_and_failure_there(self):
        browser = Client(HTTP_USER_AGENT=UA)
        browser.post(admin_url("login/"), {"username": "boss", "password": "wrong"})
        browser.post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        self.assertEqual([(entry.result, entry.reason, entry.source, entry.user_agent) for entry in logins()],
                         [("failure", "wrong_password", "admin", UA), ("success", "", "admin", UA)])

    def test_an_account_with_a_step(self):
        steps.register(steps.Step("demo", "演示", needed=lambda request, user: True, check=lambda r, u, d: None))
        self.addCleanup(steps.unregister, "demo")
        Client().post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        entry = only_login(self)
        self.assertEqual((entry.result, entry.reason, entry.user), ("failure", "needs_steps", self.boss))

    def test_the_rate_limit_there(self):
        browser = Client()
        for _ in range(12):
            browser.post(admin_url("login/"), {"username": "boss", "password": "wrong"})
        self.assertEqual(sum(entry.reason == "throttled" for entry in logins()), 1)
