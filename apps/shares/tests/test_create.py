"""创建分享 (POST /api/v1/shares/): who may, keys, passwords, entries, expiry (inventory 10.2, D32, D44)."""

from __future__ import annotations

import datetime
import json

from django.db import connection
from django.utils import timezone

from apps.accounts.models import User
from apps.accounts.permissions import Permission
from apps.core import secrets
from apps.shares.models import ENTRIES_MAX, Share

from .helpers import API, EVERYTHING, SharesTestCase


class CreateTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_a_share_of_a_folder_and_a_file(self):
        response = self.create(self.body(entries=[{"name": "docs", "type": "folder"}, {"name": "top.txt", "type": "file"}]))
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        share = Share.objects.get(key=data["key"])
        self.assertRegex(share.key, r"^[A-Za-z0-9]{8}$")
        self.assertEqual(share.entries, [{"name": "docs", "type": "folder"}, {"name": "top.txt", "type": "file"}])
        self.assertEqual((share.folder, share.owner, share.storage, share.expires_at, share.password),
                         ("/", self.alice, self.storage, None, ""))
        self.assertEqual(data["url"], f"http://testserver/share/{share.key}")
        self.assertEqual((data["label"], data["entry_count"], data["has_password"], data["password"]),
                         ("docs 等 2 项", 2, False, None))

    def test_entries_are_found_by_name_and_type(self):
        """A file is never widened to a folder of the same name (S3 lists an object "a" and a prefix "a/")."""
        response = self.create(self.body(entries=[{"name": "docs", "type": "file"}]))
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        response = self.create(self.body(entries=[{"name": "docs", "type": "file"}, {"name": "docs", "type": "folder"}]))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_name"))
        response = self.create(self.body(entries=[{"name": "docs", "type": "folder"}, {"name": "docs", "type": "folder"}]))
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Share.objects.get().entries, [{"name": "docs", "type": "folder"}])  # once

    def test_in_a_folder_of_ones_root(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        response = self.create(self.body(path="/", entries=[{"name": "sub", "type": "folder"}, {"name": "a.txt", "type": "file"}]))
        self.assertEqual(response.status_code, 201, response.content)
        share = Share.objects.get()
        self.assertEqual(share.folder, "/docs")  # the storage's path
        self.assertEqual(self.create(self.body(path="/sub")).json()["code"], "not_found")  # docs isn't in /docs/sub

    def test_the_password(self):
        data = self.create(self.body(password="  abcd  ")).json()
        share = Share.objects.get(key=data["key"])
        self.assertTrue(share.password.startswith(secrets.PREFIX))  # encrypted at rest
        self.assertNotIn("abcd", share.password)
        self.assertEqual(share.plain_password(), "abcd")
        self.assertEqual((data["has_password"], data["password"]), (True, "abcd"))  # to the owner
        for bad in ("abc", "x" * 21, "ab\ncd"):
            with self.subTest(bad=bad):
                response = self.create(self.body(password=bad))
                self.assertEqual(response.status_code, 400)
                self.assertIn("password", response.json())

    def test_expiry(self):
        now = timezone.now()
        week = Share.objects.get(key=self.create(self.body(expiry="7d")).json()["key"])
        self.assertAlmostEqual((week.expires_at - now).total_seconds(), 7 * 86400, delta=60)
        month = Share.objects.get(key=self.create(self.body(expiry="30d")).json()["key"])
        self.assertAlmostEqual((month.expires_at - now).total_seconds(), 30 * 86400, delta=60)
        later = (now + datetime.timedelta(days=3)).isoformat()
        custom = Share.objects.get(key=self.create(self.body(expiry="custom", expires_at=later)).json()["key"])
        self.assertAlmostEqual((custom.expires_at - now).total_seconds(), 3 * 86400, delta=60)
        past = (now - datetime.timedelta(minutes=1)).isoformat()
        response = self.create(self.body(expiry="custom", expires_at=past))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["expires_at"], ["过期时间必须是未来的时间。"])
        self.assertEqual(self.create(self.body(expiry="custom")).status_code, 400)
        self.assertEqual(self.create(self.body(expiry="tomorrow")).status_code, 400)
        # expires_at alone is a custom expiry, and with another expiry a contradiction (API-04: it was ignored,
        # a share without expiry lasting forever).
        alone = Share.objects.get(key=self.create(self.body(expires_at=later)).json()["key"])
        self.assertAlmostEqual((alone.expires_at - now).total_seconds(), 3 * 86400, delta=60)
        self.assertEqual(self.create(self.body(expires_at=past)).json()["expires_at"], ["过期时间必须是未来的时间。"])
        for expiry in ("forever", "7d", "30d"):
            response = self.create(self.body(expiry=expiry, expires_at=later))
            self.assertEqual(response.status_code, 400)
            self.assertEqual(response.json()["expires_at"],
                             [f"有效期是 {expiry} 时不能再填过期时间；要自定义过期时间，有效期写 custom 或者不写。"])
        forever = Share.objects.get(key=self.create(self.body(expiry="forever", expires_at=None)).json()["key"])
        self.assertIsNone(forever.expires_at)  # null is no expiry

    def test_custom_keys(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != Permission.CUSTOM_SHARE_KEY])
        response = self.create(self.body(key="my-key"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "permission_denied")
        self.grant(self.alice, [*EVERYTHING, Permission.CUSTOM_SHARE_KEY])
        self.assertEqual(self.create(self.body(key="my-key")).json()["key"], "my-key")
        response = self.create(self.body(key="my-key"))
        self.assertEqual((response.status_code, response.json()["code"]), (409, "key_taken"))
        for bad in ("ab", "abcdefghi", "a b", "a/b", "键值ab", "a.b"):
            with self.subTest(bad=bad):
                response = self.create(self.body(key=bad))
                self.assertEqual(response.status_code, 400)
                self.assertIn("只能包含字母、数字、下划线和短横线", response.json()["key"][0])

    def test_keys_differ_by_case(self):
        """Compared as spelled, on both databases (utf8mb4_bin on MySQL)."""
        self.grant(self.alice, [*EVERYTHING, Permission.CUSTOM_SHARE_KEY])
        self.assertEqual(self.create(self.body(key="CaSe")).status_code, 201)
        self.assertEqual(self.create(self.body(key="case")).status_code, 201)
        self.assertEqual(sorted(Share.objects.values_list("key", flat=True)), ["CaSe", "case"])
        self.assertEqual(Share.objects.get(key="case").key, "case")
        self.assertFalse(Share.objects.filter(key="CASE").exists())
        if connection.vendor == "mysql":
            with connection.cursor() as cursor:
                cursor.execute("SHOW CREATE TABLE shares_share")
                self.assertIn("utf8mb4_bin", cursor.fetchone()[1])

    def test_random_keys_retry(self):
        from apps.shares import service

        self.share(key="AAAAAAAA")
        keys = iter(["AAAAAAAA", "BBBBBBBB"])
        original = service.new_key
        service.new_key = lambda: next(keys)
        try:
            self.assertEqual(self.create(self.body()).json()["key"], "BBBBBBBB")
        finally:
            service.new_key = original

    def test_who_may(self):
        anonymous = self.create(self.body(), client=self.visitor)
        self.assertEqual(anonymous.status_code, 401)  # not the guest, even with the permission (10.Z.5)
        self.grant(self.guest, EVERYTHING)
        self.assertEqual(self.create(self.body(), client=self.visitor).status_code, 401)
        self.grant(self.alice, [code for code in EVERYTHING if code != Permission.CREATE_SHARE_LINK])
        response = self.create(self.body())
        self.assertEqual((response.status_code, response.json()["code"]), (403, "permission_denied"))
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertEqual(self.create(self.body()).json()["code"], "no_grant")
        self.grant(self.alice, EVERYTHING)
        self.storage.enabled = False
        self.storage.save()
        self.assertEqual(self.create(self.body()).json()["code"], "storage_disabled")
        self.assertEqual(Share.objects.count(), 0)

    def test_csrf(self):
        from rest_framework.test import APIClient

        client = APIClient(enforce_csrf_checks=True)
        client.force_login(self.alice)
        response = client.post(API, self.body(), format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (403, "csrf_failed"))

    def test_the_entries(self):
        self.assertEqual(self.create(self.body(entries=[])).json()["entries"], ["请先选择要分享的文件或文件夹。"])
        for bad in ("", ".", "..", "a/b", "a\x00"):
            with self.subTest(bad=bad):
                self.assertEqual(self.create(self.body(entries=[{"name": bad, "type": "file"}])).status_code, 400)
        self.assertEqual(self.create(self.body(entries=[{"name": "x", "type": "link"}])).status_code, 400)
        self.assertEqual(self.create(self.body(entries=["docs"])).status_code, 400)
        self.assertEqual(self.create(self.body(entries=[{"name": "top.txt"}])).status_code, 400)  # the type is needed
        missing = self.create(self.body(entries=[{"name": "docs", "type": "folder"}, {"name": "gone.txt", "type": "file"}]))
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "not_found"))
        self.assertEqual(self.create(self.body(path="/nothing")).json()["code"], "not_found")
        self.assertEqual(self.create(self.body(path="/../x")).json()["code"], "invalid_path")
        self.assertEqual(Share.objects.count(), 0)

    def test_the_entries_are_capped_before_anything_else(self):
        many = [{"name": f"f{i}"} for i in range(ENTRIES_MAX + 1)]
        response = self.create(self.body(storage="nowhere", entries=many))
        self.assertEqual(response.status_code, 400)  # not 404 storage_not_found: counted first
        self.assertEqual(response.json()["entries"], [f"一次最多分享 {ENTRIES_MAX} 项。"])
        huge = json.dumps(self.body(entries=[{"name": "x" * 1000}] * 3000))
        response = self.client.post(API, huge, content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_filters_and_folder_passwords(self):
        self.hide("/docs/sub")
        response = self.create(self.body(path="/docs", entries=[{"name": "sub", "type": "folder"}]))
        self.assertEqual((response.status_code, response.json()["code"]), (400, "hidden"))
        self.hide("/top.txt", mode="inaccessible")
        self.assertEqual(self.create(self.body(entries=[{"name": "top.txt", "type": "file"}])).json()["code"], "inaccessible")
        self.lock("/docs")
        locked = self.create(self.body())
        self.assertEqual((locked.status_code, locked.json()["code"]), (403, "password_required"))
        self.assertEqual(self.unlock("/docs").status_code, 200)
        self.assertEqual(self.create(self.body()).status_code, 201)
        # The creator's ignore_password lets him through; visitors still face it (test_browse).
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])
        self.client.logout()
        self.login()
        self.assertEqual(self.create(self.body()).status_code, 201)

    def test_a_disabled_user_cant(self):
        bob = User.objects.create_user("bob", password="pw-bob-2026")
        self.grant(bob, EVERYTHING)
        self.client.logout()
        self.login(bob)
        bob.is_active = False
        bob.save()
        self.assertEqual(self.create(self.body()).status_code, 401)
