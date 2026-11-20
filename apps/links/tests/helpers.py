"""What the links tests share: the files tests' storage "disk" (a local folder with docs/a.txt, docs/b.txt,
docs/sub/c.txt, top.txt) and users, plus the site's settings, links and the rate limit reset per test."""

from __future__ import annotations

from rest_framework.test import APIClient

from apps.accounts.permissions import Permission
from apps.files import signing
from apps.files.tests.helpers import EVERYTHING, FilesTestCase
from apps.siteconf.models import SiteSettings

from .. import ratelimit
from ..models import ShortLink

LINKS = "/api/v1/links/"


class LinksTestCase(FilesTestCase):
    def setUp(self):
        super().setUp()
        for limiter in (ratelimit.limiter, ratelimit.misses, ratelimit.opened):
            limiter.reset()
            self.addCleanup(limiter.reset)

    # -- the site

    def site(self, **fields):
        site = SiteSettings.load()
        for name, value in fields.items():
            setattr(site, name, value)
        site.save()
        return site

    # -- addresses

    @staticmethod
    def path_link(storage_path, prefix="directlink", key="disk"):
        return f"/{prefix}/{key}{signing.quote_path(storage_path)}"

    def visitor(self):
        """A browser of its own (no session, no unlocked folders)."""
        return APIClient(enforce_csrf_checks=True)

    # -- making links through the API

    def make(self, kind, paths, key="disk", **body):
        if "HTTP_X_CSRFTOKEN" not in self.client._credentials:
            self.csrf()
        return self.client.post(f"{LINKS}{key}/{kind}/", {"paths": paths, **body}, format="json")

    def short(self, path="/docs/a.txt", expires=86400, **body):
        response = self.make("short", [path], expires=expires, **body)
        self.assertEqual(response.status_code, 201, response.content[:500])
        return ShortLink.objects.get(key=response.json()["links"][0]["key"])

    def short_link_of(self, user, path="/docs/a.txt", **fields):
        """A link stored directly, as `user` made it (nothing checked)."""
        fields.setdefault("storage", self.storage)
        return ShortLink.objects.create(key=fields.pop("key", None) or self._key(), path=path, created_by=user, **fields)

    _n = 0

    def _key(self):
        LinksTestCase._n += 1
        return f"T{LinksTestCase._n:05d}"


EVERYTHING_BUT = lambda *codes: [code for code in EVERYTHING if code not in codes]  # noqa: E731
DOWNLOAD = Permission.DOWNLOAD
