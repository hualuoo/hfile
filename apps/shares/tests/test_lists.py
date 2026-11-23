"""我的分享 and 分享列表: the lists and their filters, passwords for the owner and admins only, deleting,
clearing the expired ones (and nothing else does), and the cascades (inventory 10.7, 10.8; D6, D29)."""

from __future__ import annotations

import datetime

from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.core import cleanup
from apps.shares.models import FILE, Share
from apps.storages.models import StorageSource

from .helpers import API, EVERYTHING, SharesTestCase

MANAGE = "/api/v1/manage/shares/"


class ListTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.bob = User.objects.create_user("bob", password="pw-bob-2026", nickname="鲍勃")
        self.grant(self.bob, EVERYTHING)
        now = timezone.now()
        self.mine = self.share((("docs", "folder"),), password="abcd", key="Mine0001")
        self.old = self.share((("top.txt", FILE),), key="Mine0002", expires_at=now - datetime.timedelta(days=1))
        self.later = self.share((("docs", "folder"), ("top.txt", FILE)), key="Mine0003",
                                expires_at=now + datetime.timedelta(days=1))
        self.bobs = self.share((("top.txt", FILE),), owner=self.bob, key="Bobs0001",
                               expires_at=now - datetime.timedelta(days=2))
        self.login()

    def keys(self, response):
        self.assertEqual(response.status_code, 200, response.content[:300])
        return [row["key"] for row in response.json()["results"]]

    def test_mine_only_with_the_password(self):
        response = self.client.get(API)
        self.assertEqual(self.keys(response), ["Mine0003", "Mine0002", "Mine0001"])  # newest first
        row = next(r for r in response.json()["results"] if r["key"] == "Mine0001")
        self.assertEqual((row["password"], row["has_password"], row["label"], row["entry_count"]), ("abcd", True, "docs", 1))
        self.assertEqual(row["url"], "http://testserver/share/Mine0001")
        self.assertEqual(row["storage"], {"id": self.storage.pk, "key": "disk", "name": "存储 disk", "type": "local",
                                          "type_label": row["storage"]["type_label"], "icon": "storage"})
        self.assertTrue(row["can_delete"])
        self.assertNotIn("owner", row)
        self.assertEqual(next(r for r in response.json()["results"] if r["key"] == "Mine0002")["expired"], True)

    def test_filters(self):
        self.assertEqual(self.keys(self.client.get(API, {"status": "expired"})), ["Mine0002"])
        self.assertEqual(self.keys(self.client.get(API, {"status": "active"})), ["Mine0003", "Mine0001"])
        self.assertEqual(self.keys(self.client.get(API, {"keyword": "top"})), ["Mine0003", "Mine0002"])
        self.assertEqual(self.keys(self.client.get(API, {"keyword": "0001"})), ["Mine0001"])
        other = self.make_storage("other", "local", self.root)
        self.share((("docs", "folder"),), key="Mine0004", storage=other)
        self.assertEqual(self.keys(self.client.get(API, {"storage": other.pk})), ["Mine0004"])
        today = timezone.localdate()
        Share.objects.filter(key="Mine0001").update(created_at=timezone.now() - datetime.timedelta(days=10))
        since = (today - datetime.timedelta(days=1)).isoformat()
        self.assertNotIn("Mine0001", self.keys(self.client.get(API, {"since": since})))
        until = (today - datetime.timedelta(days=5)).isoformat()
        self.assertEqual(self.keys(self.client.get(API, {"until": until})), ["Mine0001"])
        self.assertEqual(self.client.get(API, {"status": "soon"}).status_code, 400)
        self.assertEqual(self.client.get(API, {"since": "2026-13-01"}).status_code, 400)

    def test_the_keyword_finds_keys_in_any_case(self):
        # The key column compares exactly (utf8mb4_bin on MySQL); the keyword doesn't, on both databases (API-17).
        self.share(key="mine0001")
        self.assertEqual(self.keys(self.client.get(API, {"keyword": "MINE0001"})), ["mine0001", "Mine0001"])
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        client = APIClient()
        client.force_login(admin)
        self.assertEqual(self.keys(client.get(MANAGE, {"keyword": "bobs"})), ["Bobs0001"])

    def test_who_may_list(self):
        self.assertEqual(self.visitor.get(API).status_code, 401)  # nor the guest (10.Z.5)
        self.assertEqual(self.client.get(MANAGE).status_code, 403)  # alice isn't an admin

    def test_the_admins_list(self):
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        client = APIClient()
        client.force_login(admin)
        response = client.get(MANAGE)
        self.assertEqual(self.keys(response), ["Bobs0001", "Mine0003", "Mine0002", "Mine0001"])  # newest first
        row = next(r for r in response.json()["results"] if r["key"] == "Bobs0001")
        self.assertEqual(row["owner"], {"id": self.bob.pk, "username": "bob", "nickname": "鲍勃"})
        mine = next(r for r in response.json()["results"] if r["key"] == "Mine0001")
        self.assertEqual(mine["password"], "abcd")  # admins see it (D32)
        self.assertEqual(self.keys(client.get(MANAGE, {"user": "鲍"})), ["Bobs0001"])
        self.assertEqual(self.keys(client.get(MANAGE, {"user": "ali", "status": "expired"})), ["Mine0002"])

    def test_deleting(self):
        bobs = APIClient(enforce_csrf_checks=True)
        bobs.force_login(self.bob)
        self.vcsrf(bobs)
        refused = bobs.delete(f"{API}Mine0001/")
        self.assertEqual((refused.status_code, refused.json()["code"]), (403, "not_owner"))
        self.assertEqual(self.visitor.delete(f"{API}Mine0001/").status_code, 401)
        self.csrf()
        self.assertEqual(self.client.delete(f"{API}Mine0001/").status_code, 204)
        self.assertFalse(Share.objects.filter(key="Mine0001").exists())
        self.assertEqual(self.client.delete(f"{API}Mine0001/").status_code, 404)
        # A staff admin may list everybody's, but delete only his own; a superuser any.
        staff = User.objects.create_user("staff", password="pw-staff-2026", is_staff=True)
        client = APIClient()
        client.force_login(staff)
        self.assertEqual(client.get(MANAGE).status_code, 200)
        self.assertFalse(next(r for r in client.get(MANAGE).json()["results"] if r["key"] == "Bobs0001")["can_delete"])
        self.assertEqual(client.delete(f"{MANAGE}Bobs0001/").status_code, 403)
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        client.force_login(admin)
        self.assertTrue(next(r for r in client.get(MANAGE).json()["results"] if r["key"] == "Bobs0001")["can_delete"])
        self.assertEqual(client.delete(f"{MANAGE}Bobs0001/").status_code, 204)
        self.assertEqual(client.delete(f"{API}Mine0002/").status_code, 204)  # the owners' address too

    def test_clearing_the_expired(self):
        self.csrf()
        response = self.client.post(f"{API}clear-expired/")
        self.assertEqual(response.json(), {"deleted": 1})  # alice's own
        self.assertEqual(sorted(Share.objects.values_list("key", flat=True)), ["Bobs0001", "Mine0001", "Mine0003"])
        staff = User.objects.create_user("staff", password="pw-staff-2026", is_staff=True)
        client = APIClient()
        client.force_login(staff)
        self.assertEqual(client.post(f"{MANAGE}clear-expired/").json(), {"deleted": 1})  # everybody's
        self.assertEqual(sorted(Share.objects.values_list("key", flat=True)), ["Mine0001", "Mine0003"])

    def test_the_cleanup_leaves_them(self):
        """The cleanup (apps.core.cleanup) has no job for shares: expired ones stay, shown as expired, until their
        owner or an admin deletes them."""
        self.assertNotIn("shares", [job.name for job in cleanup.jobs()])

    def test_cascades(self):
        self.bob.delete()
        self.assertFalse(Share.objects.filter(key="Bobs0001").exists())
        StorageSource.objects.filter(pk=self.storage.pk).delete()
        self.assertEqual(Share.objects.count(), 0)

    def test_an_unreadable_password(self):
        Share.objects.filter(key="Mine0001").update(password="enc:broken")
        row = next(r for r in self.client.get(API).json()["results"] if r["key"] == "Mine0001")
        self.assertEqual((row["password"], row["password_unreadable"], row["has_password"]), (None, True, True))
