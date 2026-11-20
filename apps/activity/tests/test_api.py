"""GET /api/v1/manage/activity/ and the /manage/#activity section: for admins, newest first, its filters, and
nothing that changes or deletes an entry."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.storages.models import StorageSource

from ..models import ActivityLog

User = get_user_model()
URL = "/api/v1/manage/activity/"


class ApiTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.alice = User.objects.create_user("alice", password="pw-alice-2026", nickname="爱丽丝")
        self.storage = StorageSource.objects.create(key="disk", name="网盘", type="local")
        now = timezone.now()
        log = ActivityLog.objects
        self.old = log.create(kind="login", actor=self.alice, actor_name="alice", ip="192.168.1.20", detail={"source": "site"},
                              created_at=now - timedelta(days=10))
        self.failed = log.create(kind="login_failed", target_name="root", ip="2001:db8::1",
                                 detail={"source": "admin", "reason": "unknown_user"}, user_agent="UA/1")
        self.deleted = log.create(kind="delete", actor=self.alice, actor_name="alice", storage=self.storage, storage_name="网盘",
                                  path="/docs/old", detail={"folder": True}, ip="192.168.1.21")
        self.shared = log.create(kind="create_share", actor=self.alice, actor_name="alice", storage=self.storage,
                                 storage_name="网盘", path="/docs", key="Ab3dE7fG", detail={"items": {"names": ["a.txt"], "count": 1}})
        self.changed = log.create(kind="update_user", actor=self.boss, actor_name="boss", target=self.alice, target_name="alice",
                                  detail={"password": True}, ip="192.168.1.21")
        self.settings = log.create(kind="update_settings", actor=self.boss, actor_name="boss",
                                   detail={"group": "link", "fields": ["record_download_log"]})
        self.client.force_authenticate(self.boss)

    def ids(self, query=""):
        r = self.client.get(URL + query)
        self.assertEqual(r.status_code, 200, r.json())
        return [entry["id"] for entry in r.json()["results"]]

    def test_admins_only(self):
        self.assertIn(APIClient().get(URL).status_code, (401, 403))
        user = APIClient()
        user.force_authenticate(self.alice)
        self.assertEqual(user.get(URL).status_code, 403)

    def test_nothing_changes_or_deletes_it(self):
        for method in ("post", "put", "patch", "delete"):
            with self.subTest(method=method):
                self.assertEqual(getattr(self.client, method)(URL).status_code, 405)
                self.assertEqual(getattr(self.client, method)(f"{URL}{self.old.pk}/").status_code, 404)
        self.assertEqual(ActivityLog.objects.count(), 6)

    def test_the_list(self):
        data = self.client.get(URL).json()
        self.assertEqual(data["count"], 6)
        self.assertEqual([entry["id"] for entry in data["results"]][-1], self.old.pk)  # newest first
        deleted = next(entry for entry in data["results"] if entry["id"] == self.deleted.pk)
        self.assertEqual(deleted["actor"], {"id": self.alice.pk, "username": "alice", "nickname": "爱丽丝", "kind": "normal"})
        self.assertEqual((deleted["kind_label"], deleted["category"], deleted["storage"], deleted["path"], deleted["detail_text"]),
                         ("删除", "files", {"id": self.storage.pk, "name": "网盘", "key": "disk"}, "/docs/old", "文件夹，连同里面的东西"))
        failed = next(entry for entry in data["results"] if entry["id"] == self.failed.pk)
        self.assertEqual((failed["actor"], failed["target"], failed["detail_text"], failed["user_agent"]),
                         (None, {"id": None, "username": "root", "nickname": "", "kind": ""}, "没有这个用户；后台", "UA/1"))
        settings = next(entry for entry in data["results"] if entry["id"] == self.settings.pk)
        self.assertEqual((settings["page"], settings["detail_text"]), ({"section": "links", "label": "直链和短链"}, "记录下载日志"))
        english = self.client.get(URL, HTTP_ACCEPT_LANGUAGE="en").json()["results"]
        self.assertEqual(next(entry for entry in english if entry["id"] == self.deleted.pk)["kind_label"], "Delete")

    def test_after_the_user_and_the_storage_are_gone(self):
        self.storage.delete()
        self.alice.delete()
        entry = next(entry for entry in self.client.get(URL).json()["results"] if entry["id"] == self.deleted.pk)
        self.assertEqual((entry["actor"], entry["storage"]),
                         ({"id": None, "username": "alice", "nickname": "", "kind": ""}, {"id": None, "name": "网盘", "key": ""}))

    def test_filters(self):
        today = timezone.localdate()
        self.assertEqual(self.ids("?kind=login_failed"), [self.failed.pk])
        self.assertEqual(self.ids("?category=account"), [self.failed.pk, self.old.pk])
        self.assertEqual(self.ids(f"?storage={self.storage.pk}"), [self.shared.pk, self.deleted.pk])
        self.assertEqual(self.ids(f"?user={self.alice.pk}"), [self.changed.pk, self.shared.pk, self.deleted.pk, self.old.pk])
        self.assertEqual(self.ids("?ip=192.168.1.21"), [self.changed.pk, self.deleted.pk])
        self.assertEqual(self.ids("?ip=2001:0DB8:0:0::1"), [self.failed.pk])  # written another way
        self.assertEqual(self.ids(f"?until={today - timedelta(days=1)}"), [self.old.pk])
        self.assertEqual(len(self.ids(f"?since={today}")), 5)
        for query, field in (("?kind=nope", "kind"), ("?category=nope", "category"), ("?user=x", "user"), ("?storage=0", "storage"),
                             ("?ip=192.168.1", "ip"), ("?since=2026-13-01", "since"),
                             (f"?since={today}&until={today - timedelta(days=1)}", "until"), ("?page_size=51", "page_size")):
            with self.subTest(query=query):
                r = self.client.get(URL + query)
                self.assertEqual(r.status_code, 400)
                self.assertIn(field, r.json())

    def test_search(self):
        for text, expected in (("ALI", [self.changed, self.shared, self.deleted, self.old]), ("爱丽", [self.changed, self.shared, self.deleted, self.old]),
                               ("@root", [self.failed]), ("网盘", [self.shared, self.deleted]), ("docs/old", [self.deleted]),
                               ("ab3de", [self.shared]), ("192.168.1.2", [self.changed, self.deleted, self.old]),
                               ("2001:DB8", [self.failed]), ("nothing-like-it", [])):
            with self.subTest(text=text):
                self.assertEqual(self.ids(f"?search={text}"), [entry.pk for entry in expected])

    def test_queries_for_a_page(self):
        # The site's settings (the access control), the count, the entries with their users and storage sources.
        with self.assertNumQueries(3):
            self.client.get(URL)
        for _ in range(10):
            ActivityLog.objects.create(kind="mkdir", actor=self.alice, actor_name="alice", storage=self.storage, path="/x")
        with self.assertNumQueries(3):
            self.client.get(URL)


class PageTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.browser = Client()
        self.browser.force_login(self.boss)

    def test_the_section(self):
        page = self.browser.get("/manage/").content.decode()
        self.assertIn('id="section-activity"', page)
        self.assertRegex(page, r'<script src="/static/activity/js/manage_activity_log\.js\?v=[0-9a-f]{12}"></script>')
        self.assertIn('<optgroup label="文件">', page)
        self.assertIn('<option value="category:files">全部文件操作</option>', page)
        self.assertIn('<option value="login_failed">登录失败</option>', page)
        self.assertNotIn("login-log", page)

    def test_no_switch_for_logins(self):
        page = self.browser.get("/manage/").content.decode()
        self.assertNotIn('name="login_log"', page)
        self.assertNotIn("login_log", self.browser.get("/api/v1/manage/settings/security/").json())
