"""What the upload tests share: a local storage in a temporary folder, a spool folder of their own, users
and grants, and helpers that speak the API as the panel does."""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import Client, TestCase, override_settings

from apps.accounts.models import User
from apps.accounts.permissions import ALL
from apps.files import signals as file_signals
from apps.storages import config as storage_config
from apps.storages import instances
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import Grant, StorageSource
from apps.uploads import service

API = "/api/v1/uploads/"


def test_folder() -> Path:
    """A new folder for one test's files: beside the config.toml the tests run with, so that the spool
    files of a test run stay with it; the system's temporary folder if that can't be written."""
    base = Path(settings.CONFIG_FILE).resolve().parent / "uploads-tests"
    try:
        base.mkdir(exist_ok=True)
        return Path(tempfile.mkdtemp(prefix="t-", dir=base))
    except OSError:
        return Path(tempfile.mkdtemp(prefix="hfile-test-uploads-"))

EVERYTHING = [code for code in ALL if not code.startswith("ignore_")]
NO_DELETE = [code for code in EVERYTHING if code != "delete"]


class UploadTestCase(TestCase):
    """A local storage "disk" (self.root) with alice (every permission but the ignore_ ones) logged in
    (self.client); the guest has no grant unless a test gives it one. Chunks are 1000 bytes."""

    chunk = 1000

    def setUp(self):
        super().setUp()
        self.tmp = test_folder()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        self.spool = self.tmp / "spool"
        settings_patch = override_settings(UPLOAD_SPOOL_DIR=str(self.spool))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        for patcher in (mock.patch.object(service, "CHUNK_SIZE", self.chunk),
                        mock.patch.object(service, "_light_cleanup", lambda: None),
                        mock.patch.object(instances, "GRACE", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        instances.close_all()
        self.addCleanup(instances.close_all)
        self.storage = self.make_storage("disk", self.root)
        self.guest = User.objects.guest()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.grant(self.alice, EVERYTHING)
        self.client.force_login(self.alice)
        self.signals = []
        receiver = lambda sender, source, paths, **kw: self.signals.append((source.key, list(paths)))  # noqa: E731
        file_signals.changed.connect(receiver, weak=False, dispatch_uid="uploads-test")
        self.addCleanup(file_signals.changed.disconnect, dispatch_uid="uploads-test")

    def make_storage(self, key, root, **fields):
        return StorageSource.objects.create(
            key=key, name=key, type="local",
            config=storage_config.encode(LocalDriver, {"root": str(root), "allow_external_symlinks": False}), **fields)

    def grant(self, user, permissions, root="/", enabled=True, storage=None):
        grant, _ = Grant.objects.update_or_create(
            user=user, storage=storage or self.storage,
            defaults={"permissions": list(permissions), "root_path": root, "enabled": enabled})
        return grant

    # -- the API, as the panel uses it

    def create(self, name="a.txt", size=0, folder="/", client=None, **more):
        body = {"storage": "disk", "folder": folder, "name": name, "size": size, **more}
        return (client or self.client).post(API, json.dumps(body), content_type="application/json")

    def put(self, session, data: bytes, start: int, total: int | None = None, client=None, **headers):
        total = len(data) if total is None else total
        end = start + len(data) - 1
        url = session["url"] if isinstance(session, dict) else f"{API}{session}/"
        return (client or self.client).put(url, data=data, content_type="application/octet-stream",
                                           HTTP_CONTENT_RANGE=f"bytes {start}-{end}/{total}", **headers)

    def upload(self, name, data: bytes, folder="/", client=None, **more):
        """The whole proxied upload, chunk by chunk; the last answer."""
        made = self.create(name, len(data), folder, client=client, **more)
        self.assertEqual(made.status_code, 201, made.content)
        session = made.json()
        if session["state"] == "done":
            return made
        answer = made
        for start in range(0, len(data), session["chunk_size"]):
            answer = self.put(session, data[start:start + session["chunk_size"]], start, len(data), client=client)
            self.assertIn(answer.status_code, (201, 202), answer.content)
        return answer

    def guest_client(self):
        return Client()

    def assertCode(self, response, status, code):
        self.assertEqual(response.status_code, status, response.content)
        self.assertEqual(response.json().get("code"), code, response.content)
        return response.json()
