"""What the shares tests share: files' test storage (a local folder "disk" with docs/a.txt, docs/b.txt,
docs/sub/c.txt and top.txt; alice may do everything), shares made directly, and a visitor's client."""

from __future__ import annotations

from rest_framework.test import APIClient

from apps.files.tests.helpers import EVERYTHING, FilesTestCase
from apps.shares.models import FILE, FOLDER, Share

API = "/api/v1/shares/"


class SharesTestCase(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.visitor = APIClient(enforce_csrf_checks=True)

    # -- making shares

    def share(self, entries=(("docs", FOLDER),), *, folder="/", password="", owner=None, key=None, expires_at=None,
              storage=None) -> Share:
        share = Share(key=key or f"k{Share.objects.count() + 1:07d}", storage=storage or self.storage, folder=folder,
                      entries=[{"name": name, "type": kind} for name, kind in entries],
                      names="\n".join(name for name, _ in entries), owner=owner or self.alice, expires_at=expires_at)
        share.set_password(password)
        share.save()
        return share

    def files_share(self, **kwargs) -> Share:
        """docs/ and top.txt of the root."""
        return self.share((("docs", FOLDER), ("top.txt", FILE)), **kwargs)

    # -- the owner's API (self.client, alice)

    def create(self, body, client=None):
        client = client or self.client
        if "HTTP_X_CSRFTOKEN" not in client._credentials:
            token = client.get("/api/v1/session/").json()["csrf_token"]
            client.credentials(HTTP_X_CSRFTOKEN=token)
        return client.post(API, body, format="json")

    def body(self, **fields):
        data = {"storage": "disk", "path": "/", "entries": [{"name": "docs", "type": "folder"}]}
        data.update(fields)
        return data

    # -- a visitor (self.visitor, anonymous unless logged in)

    def vcsrf(self, client=None):
        client = client or self.visitor
        token = client.get("/api/v1/session/").json()["csrf_token"]
        client.credentials(HTTP_X_CSRFTOKEN=token)

    def vpost(self, share, what, body, client=None, **extra):
        client = client or self.visitor
        if "HTTP_X_CSRFTOKEN" not in client._credentials:
            self.vcsrf(client)
        return client.post(f"{API}{self.key_of(share)}/{what}/", body, format="json", **extra)

    def vget(self, share, what="", client=None, **params):
        client = client or self.visitor
        path = f"{API}{self.key_of(share)}/" + (f"{what}/" if what else "")
        return client.get(path, params)

    def verify(self, share, password, client=None, **extra):
        return self.vpost(share, "verify", {"password": password}, client=client, **extra)

    def vlist(self, share, path="/", client=None, **params):
        return self.vget(share, "list", client=client, path=path, **params)

    def vnames(self, share, path="/", client=None):
        response = self.vlist(share, path, client=client)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return [item["name"] for item in response.json()["items"]]

    @staticmethod
    def key_of(share):
        return share if isinstance(share, str) else share.key

    def reload(self, share) -> Share:
        return Share.objects.get(pk=share.pk)


__all__ = ["API", "EVERYTHING", "FILE", "FOLDER", "SharesTestCase"]
