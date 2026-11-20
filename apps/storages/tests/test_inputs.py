"""Odd request bodies answer 400 with a message, never 500: every write endpoint of the storages API gets
values of the wrong shape."""

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.storages import config
from apps.storages.models import StorageSource

from .fakes import FakeDriver, FakeTypeMixin

API = "/api/v1/"


class Raw(str):
    """A request body sent as it is (JSON that Python's encoder won't write, like 1e400)."""


ODD = [[], [1, 2], "text", 5, None, True, {"nested": {"deep": [1]}}, {"config": []}, {"config": "x"},
       {"config": {"port": [1]}}, {"config": {"endpoint": {"a": 1}}}, {"clear_secrets": "token"},
       {"clear_secrets": [1]}, {"key": ["a"], "name": {"x": 1}, "type": 5}, {"type": "fake", "storage": "1"},
       {"type": "fake", "storage": True}, {"type": ["fake"]}, {"draft": ["x"], "type": "fake"},
       {"type": "fake", "name": 5}, {"type": "fake", "name": "qr", "poll_token": ["x"]},
       Raw('{"type": "fake", "config": {"port": 1e400}}'), Raw('{"type": "fake", "config": {"port": NaN}}'),
       Raw('[' * 600 + ']' * 600), {"type": "fake", "config": {"port": "1" * 50}},
       {"type": "fake", "config": {"endpoint": "x" * 100_000}}]


class OddInputTests(FakeTypeMixin, TestCase):
    def setUp(self):
        super().setUp()
        admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client = APIClient()
        self.client.force_login(admin)
        self.storage = StorageSource.objects.create(key="docs", name="文档", type="fake",
                                                    config=config.encode(FakeDriver, {"endpoint": "x"}))
        self.user = User.objects.create_user("alice", password="pw-alice-2026")

    def check(self, method, url, body):
        if isinstance(body, Raw):
            response = getattr(self.client, method)(url, str(body), content_type="application/json")
        else:
            response = getattr(self.client, method)(url, body, format="json")
        self.assertLess(response.status_code, 500, (url, body, response.content[:300]))
        if response.status_code >= 400:
            self.assertIsInstance(response.json(), (dict, list), (url, body))
        return response

    def test_admin_endpoints(self):
        pk = self.storage.pk
        endpoints = [
            ("post", "manage/storages/"), ("patch", f"manage/storages/{pk}/"), ("post", f"manage/storages/{pk}/copy/"),
            ("post", f"manage/storages/{pk}/check-path/"), ("put", f"manage/storages/{pk}/filters/"),
            ("put", f"manage/storages/{pk}/passwords/"), ("put", f"manage/storages/{pk}/readmes/"),
            ("post", f"manage/storages/{pk}/rules/test/"), ("post", "manage/storages/sort/"),
            ("post", "manage/storages/test/"), ("post", "manage/storages/helpers/"), ("post", "manage/storages/oauth/"),
            ("put", f"manage/users/{self.user.pk}/grants/"),
        ]
        for method, url in endpoints:
            for body in ODD:
                with self.subTest(url=url, body=str(body)[:60]):
                    self.check(method, API + url, body)
        self.assertTrue(StorageSource.objects.filter(pk=pk, key="docs").exists())

    def test_rule_lists_with_odd_items(self):
        url = f"{API}manage/storages/{self.storage.pk}/filters/"
        # (A number for a text field is taken as its text, as DRF does: {"expression": 5} is "/5".)
        for body in ([None], [5], ["x"], [[]], [{"expression": None}], [{"id": "x", "expression": "/a"}],
                     [{"id": -1, "expression": "/a"}], [{"expression": "/a", "mode": None}], [{"expression": "/a", "description": []}]):
            with self.subTest(body=body):
                self.assertEqual(self.check("put", url, body).status_code, 400)

    def test_grant_lists_with_odd_items(self):
        url = f"{API}manage/users/{self.user.pk}/grants/"
        for body in ([None], [{"storage": None}], [{"storage": {"id": "1"}}], [{"storage": self.storage.pk, "enabled": "maybe"}],
                     [{"storage": self.storage.pk, "permissions": "upload"}], [{"storage": self.storage.pk, "permissions": [None]}],
                     [{"storage": self.storage.pk, "root_path": None}], [{"storage": self.storage.pk, "root_path": ["/a"]}]):
            with self.subTest(body=body):
                response = self.check("put", url, body)
                if body == [{"storage": self.storage.pk, "root_path": None}]:
                    continue  # null is "/" (left out), not an error
                self.assertEqual(response.status_code, 400)

    def test_public_endpoints(self):
        visitor = APIClient()
        for body in ODD + [{"path": None, "password": "x"}, {"path": "/a", "password": None}, {"rule": "x", "password": "x"},
                           {"path": "/a/../b", "password": "x"}, {"path": "/a", "password": "x" * 2000}]:
            with self.subTest(body=str(body)[:60]):
                if isinstance(body, Raw):
                    response = visitor.post(f"{API}storages/docs/unlock/", str(body), content_type="application/json")
                else:
                    response = visitor.post(f"{API}storages/docs/unlock/", body, format="json")
                self.assertLess(response.status_code, 500, response.content[:300])
        self.assertLess(visitor.get(f"{API}storages/", {"x": "y"}).status_code, 500)
