"""What the links app adds elsewhere: the policy's link rules (apps.storages.access), the access log's mask for
/s/<key>, the cleanup job, the browser's catch-all route and its scripts."""

from __future__ import annotations

from django.test import SimpleTestCase

from apps.accounts.permissions import Permission
from apps.core import cleanup, masking
from apps.files import pages
from apps.storages import access as policy
from apps.storages.access import AccessDenied
from apps.storages.models import FilterMode

from .. import delivery
from .helpers import EVERYTHING, LinksTestCase


class PolicyTests(LinksTestCase):
    def access(self, user=None):
        return policy.open_storage(None, self.storage, actor=policy.Actor(user or self.alice))

    def test_clean_path(self):
        self.assertEqual(policy.clean_path("docs//a.txt/"), "/docs/a.txt")
        self.assertEqual(policy.clean_path(""), "/")
        for bad in ("/a/../b", "/a/./b", "/a\\..\\b", "/x/.\\y", "/a\x00b", "/" + "a/" * 300, "/" + "a" * 5000):
            with self.assertRaises(AccessDenied) as caught:
                policy.clean_path(bad)
            self.assertEqual((caught.exception.code, caught.exception.status_code), ("invalid_path", 400), bad)
        self.assertEqual(policy.clean_path("/a\\b"), "/a\\b")  # a name with a backslash is still a name

    def test_open_link_needs_download_only(self):
        self.grant(self.alice, [Permission.DOWNLOAD])
        self.access().check(policy.OPEN_LINK, "/docs/a.txt")
        self.grant(self.alice, [Permission.PREVIEW, Permission.GENERATE_LINK])
        with self.assertRaises(AccessDenied):
            self.access().check(policy.OPEN_LINK, "/docs/a.txt")

    def test_links_never_skip_the_filter_rules(self):
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        self.hide("/docs", mode=FilterMode.INACCESSIBLE)
        access = self.access()
        access.check(policy.DOWNLOAD, "/docs/a.txt")  # 忽略隐藏 for the user himself
        for operation in (policy.LINK, policy.SHORT_LINK, policy.OPEN_LINK):
            with self.assertRaises(AccessDenied) as caught:
                access.check(operation, "/docs/a.txt")
            self.assertEqual(caught.exception.code, "inaccessible")

    def test_access_path_unchanged(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        access = policy.open_storage(None, self.storage, actor=policy.Actor(self.alice))
        self.assertEqual(access.path("/sub/c.txt"), "/docs/sub/c.txt")
        self.assertEqual(access.path(""), "/docs")
        with self.assertRaises(AccessDenied):
            access.path("/a\\..\\b")


class MaskingTests(SimpleTestCase):
    def test_short_keys_are_masked(self):
        self.assertEqual(masking.mask_address("/s/Ab3dE9"), "/s/<key>")
        self.assertEqual(masking.mask_address("/s/Ab3dE9/"), "/s/<key>/")
        self.assertEqual(masking.mask_address("/login/", "next=%2Fs%2FAb3dE9"), "/login/?next=/s/<key>")
        self.assertEqual(masking.mask_text("GET https://files.example.com/s/Ab3dE9 failed"),
                         "GET https://files.example.com/s/<key> failed")
        # Not a folder named "s" deeper in a path.
        self.assertEqual(masking.mask_address("/disk/s/notes.txt"), "/disk/s/notes.txt")
        self.assertEqual(masking.mask_address("/directlink/disk/s/notes.txt"), "/directlink/disk/s/notes.txt")


class RegistrationTests(SimpleTestCase):
    def test_registered(self):
        self.assertIn("short_links", [job.name for job in cleanup.jobs()])
        self.assertIn(delivery.route, pages._routes)
        self.assertIn("links/js/links.js", pages.assets()["scripts"])
        self.assertIn("links/css/links.css", pages.assets()["styles"])


class RouteTests(LinksTestCase):
    def test_only_the_prefix(self):
        self.login()
        self.assertEqual(self.client.get("/disk/docs/").status_code, 200)  # the browser's own
        self.assertEqual(self.client.get("/directlink/disk/docs/a.txt").status_code, 302)
        self.assertEqual(self.client.get("/manage").status_code, 301)  # APPEND_SLASH still
        self.assertEqual(self.client.get("/api/nothing").status_code, 404)


class ReservedSegmentTests(SimpleTestCase):
    """The site's own first segments that nothing took stay a plain 404 without the database (a static file
    that isn't there passes no middleware that reads it)."""

    def test_no_database(self):
        self.assertEqual(self.client.get("/static/web/css/nope.css").status_code, 404)
