"""What the preview tests share: a storage of the fake type (an in-memory tree) and files' signed links."""

from django.test import TestCase

from apps.accounts.models import User
from apps.accounts.permissions import Permission
from apps.files import signing
from apps.storages import config
from apps.storages.models import Grant, StorageSource
from apps.storages.tests.fakes import FakeDriver, FakeTypeMixin


class PreviewTestCase(FakeTypeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.storage = StorageSource.objects.create(
            key="files", name="文件", type="fake", config=config.encode(FakeDriver, {"endpoint": "x"}))
        self.guest = User.objects.guest()
        self.grant(self.guest, [Permission.PREVIEW])
        FakeDriver.tree = {
            "/docs": None,
            "/docs/README.md": "# 标题\n\n![图](pic.png) [另一个](other.md)\n".encode(),
            "/docs/other.md": b"# Other\n",
            "/docs/notes.txt": b"plain text",
            "/docs/drawing.dwg": b"AC1015",
        }

    def grant(self, user, permissions, root="/", storage=None):
        Grant.objects.update_or_create(
            user=user, storage=storage or self.storage,
            defaults={"permissions": list(permissions), "root_path": root, "enabled": True})

    def signed(self, path, storage=None):
        """What the item endpoint gives a viewer as inline_url (W1 same_origin, inline)."""
        return signing.sign(storage or self.storage, path, lifetime=600, inline=True)

    def post(self, endpoint, url):
        return self.client.post(f"/api/v1/preview/{endpoint}/", {"url": url}, content_type="application/json")
