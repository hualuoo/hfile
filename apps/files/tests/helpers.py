"""What the files tests share: storages on folders of their own (local, and the variants of drivers.py),
users with grants, and a client for the API."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import ALL
from apps.storages import config, drivers, instances
from apps.storages.models import FilterRule, FolderPassword, Grant, ReadmeRule, StorageSource

from . import drivers as test_drivers

API = "/api/v1/files/"
EVERYTHING = [code for code in ALL if not code.startswith("ignore_")]
# Not the key "disk", though MySQL's collation compares the first three equal to it (width, accents, NUL)
# and Python's lower() makes the last one (the Kelvin sign) "disk" (API-16). Quoted in addresses.
NOT_DISK = ("ｄｉｓｋ", "dísk", "di\x00sk", "dis\u212a")


class FilesTestCase(TestCase):
    """A local storage "disk" on a new folder with a few files; alice may do everything there, the
    guest has nothing until a test gives him a grant."""

    storage_type = "local"

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(drivers.DRIVERS, test_drivers.TYPES)
        patcher.start()
        self.addCleanup(patcher.stop)
        grace = mock.patch.object(instances, "GRACE", 0)
        grace.start()
        self.addCleanup(grace.stop)
        instances.close_all()
        self.addCleanup(instances.close_all)
        self.root = Path(tempfile.mkdtemp(prefix="hfile-files-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.storage = self.make_storage("disk", self.storage_type, self.root)
        self.guest = User.objects.guest()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.grant(self.alice, EVERYTHING)
        self.client = APIClient(enforce_csrf_checks=True)
        self.files({"docs/a.txt": b"hello", "docs/b.txt": b"world!", "docs/sub/c.txt": b"c", "top.txt": b"top"})

    # -- making things

    def make_storage(self, key, type_, root, **fields):
        cls = drivers.driver_class(type_)
        return StorageSource.objects.create(key=key, name=f"存储 {key}", type=type_,
                                            config=config.encode(cls, {"root": str(root)}), **fields)

    def files(self, tree, root=None):
        base = Path(root or self.root)
        for name, data in tree.items():
            path = base / name
            if data is None:
                path.mkdir(parents=True, exist_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

    def grant(self, user, permissions, root="/", enabled=True, storage=None):
        grant, _ = Grant.objects.update_or_create(
            user=user, storage=storage or self.storage,
            defaults={"permissions": list(permissions), "root_path": root, "enabled": enabled})
        return grant

    def rule(self, model, expression, position=0, **fields):
        return model.objects.create(storage=self.storage, expression=expression, position=position, **fields)

    def hide(self, expression, mode="hidden"):
        return self.rule(FilterRule, expression, mode=mode)

    def lock(self, expression, password="pw1", position=0):
        return self.rule(FolderPassword, expression, password=password, position=position)

    def readme_rule(self, expression, text, **fields):
        return self.rule(ReadmeRule, expression, text=text, **fields)

    # -- asking

    def login(self, user=None):
        self.client.force_login(user or self.alice)
        self.csrf()

    def csrf(self):
        response = self.client.get("/api/v1/session/")
        self.client.credentials(HTTP_X_CSRFTOKEN=response.json()["csrf_token"])

    def get(self, what, key="disk", **params):
        return self.client.get(f"{API}{key}/{what}/", params)

    def post(self, what, body, key="disk"):
        if "HTTP_X_CSRFTOKEN" not in self.client._credentials:
            self.csrf()
        return self.client.post(f"{API}{key}/{what}/", body, format="json")

    def listing(self, path="/", **params):
        response = self.get("list", path=path, **params)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def names(self, path="/", **params):
        return [item["name"] for item in self.listing(path, **params)["items"]]

    def unlock(self, path, password="pw1", remember=False, key="disk"):
        if "HTTP_X_CSRFTOKEN" not in self.client._credentials:
            self.csrf()
        return self.client.post(f"/api/v1/storages/{key}/unlock/", {"path": path, "password": password,
                                                                     "remember": remember}, format="json")

    def code(self, response):
        try:
            return response.json().get("code")
        except ValueError:
            return None
