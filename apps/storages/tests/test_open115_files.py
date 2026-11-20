"""The 115 driver under the files API: a batch delete or move in a big folder lists it once, not
once per item (S5-09). The fake 115 serves 10 entries a page, so a folder of 30 is 3 listing calls."""

from __future__ import annotations

from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import ALL
from apps.storages import config, instances
from apps.storages.drivers import _ids, open115
from apps.storages.models import Grant, StorageSource
from apps.storages.tests.fake_open115 import APP_ID, Fake115
from apps.storages.tests.test_driver_open115 import FrozenClock

EVERYTHING = [code for code in ALL if not code.startswith("ignore_")]


class Open115BatchApiTests(TestCase):
    def setUp(self):
        super().setUp()
        self.fake = Fake115().start()
        self.addCleanup(self.fake.close)
        self.fake.page_limit = 10
        for name, value in (("api_root", self.fake.server.url + "/proapi"),
                            ("passport_root", self.fake.server.url + "/passport"),
                            ("qrcode_root", self.fake.server.url + "/qrcode"), ("page_size", 10)):
            patcher = mock.patch.object(open115.Open115Driver, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        frozen = FrozenClock()  # listings don't age by themselves
        for module in (open115, _ids):
            clock = mock.patch.object(module, "time", frozen)
            clock.start()
            self.addCleanup(clock.stop)
        grace = mock.patch.object(instances, "GRACE", 0)
        grace.start()
        self.addCleanup(grace.stop)
        instances.close_all()
        self.addCleanup(instances.close_all)
        for folder in ("big", "dst"):
            folder_id = self.fake.mkdirs(f"/{folder}")
            for i in range(30):  # 15 folders, 15 files
                if i < 15:
                    self.fake.add(folder_id, f"{folder}{i:02d}", True)
                else:
                    self.fake.add(folder_id, f"{folder}{i:02d}.txt", False, b"x" * i)
        values = {"app_id": APP_ID, "qps": 10, "base_path": "/", **self.fake.issue_tokens()}
        StorageSource.objects.create(key="pan", name="115", type="open115",
                                     config=config.encode(open115.Open115Driver, values))
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        Grant.objects.create(user=alice, storage=StorageSource.objects.get(key="pan"), permissions=EVERYTHING,
                             root_path="/", enabled=True)
        self.client = APIClient(enforce_csrf_checks=True)
        self.client.force_login(alice)
        token = self.client.get("/api/v1/session/").json()["csrf_token"]
        self.client.credentials(HTTP_X_CSRFTOKEN=token)

    def pages(self) -> int:
        return self.fake.counts["/open/ufile/files"]

    def listed(self, path: str) -> list[str]:
        response = self.client.get("/api/v1/files/pan/list/", {"path": path})
        self.assertEqual(response.status_code, 200, response.content[:300])
        return sorted(item["name"] for item in response.json()["items"])

    def in_fake(self, path: str) -> list[str]:
        with self.fake.lock:
            return sorted(item.name for item in self.fake.children(self.fake.find(path).id))

    def test_batch_delete(self):
        self.assertEqual(len(self.listed("/big")), 30)  # what the page shows before one picks
        before = self.pages()
        names = [f"big{i:02d}" for i in range(10, 15)] + [f"big{i:02d}.txt" for i in range(15, 20)]
        response = self.client.post("/api/v1/files/pan/delete/", {"path": "/big", "names": names}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual((response.json()["succeeded"], response.json()["failed"]), (10, 0))
        # The checks and the deletes all use the listing the page's list left (27 pages before).
        self.assertEqual(self.pages() - before, 0)
        self.assertEqual(self.fake.counts["/open/ufile/delete"], 10)
        left = [name for name in self.in_fake("/big") if name not in names]
        self.assertEqual(self.in_fake("/big"), left)
        self.assertEqual(self.listed("/big"), left)

    def test_batch_move(self):
        self.fake.add(self.fake.find("/big").id, "dst20.txt", False, b"taken")  # /dst has this name
        self.listed("/big")  # what the page shows before one picks
        before = self.pages()
        names = [f"big{i:02d}" for i in range(12, 15)] + ["big15.txt", "big16.txt", "dst20.txt"]
        response = self.client.post("/api/v1/files/pan/move/",
                                    {"path": "/big", "names": names, "target": "/dst"}, format="json")
        self.assertEqual(response.status_code, 200, response.content[:300])
        results = {result["name"]: result for result in response.json()["results"]}
        self.assertEqual((response.json()["succeeded"], results["dst20.txt"]["code"]), (5, "exists"))
        # /dst's three pages, once: the checks list it, and the name check before each move uses
        # that listing; /big's comes from the page's list (it was 34 pages before).
        self.assertEqual(self.pages() - before, 3)
        self.assertEqual(self.fake.counts["/open/ufile/move"], 5)
        moved = names[:5]
        self.assertEqual(self.in_fake("/dst"), sorted([f"dst{i:02d}" for i in range(15)]
                                                      + [f"dst{i:02d}.txt" for i in range(15, 30)] + moved))
        self.assertEqual(self.listed("/dst"), self.in_fake("/dst"))
        self.assertEqual(self.listed("/big"), self.in_fake("/big"))
        self.assertIn("dst20.txt", self.in_fake("/big"))
