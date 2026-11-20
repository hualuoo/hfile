"""Opening path links, /<prefix>/<storage key>/<storage path> (inventory 9.3, D13, D40, D40a)."""

from __future__ import annotations

import os
import unittest
import urllib.parse

from apps.accounts.permissions import Permission
from apps.files import signing
from apps.files.tests.helpers import NOT_DISK
from apps.storages.models import FilterMode, StorageSource

from ..models import DownloadLog
from .helpers import EVERYTHING, EVERYTHING_BUT, LinksTestCase


class PathLinkTests(LinksTestCase):
    def assertDownload(self, response, path="/docs/a.txt"):
        self.assertEqual(response.status_code, 302, response.content[:300])
        link = signing.verify_url(response["Location"])
        self.assertEqual((link.storage.key, link.path), ("disk", path))
        self.assertFalse(link.ignore_hidden)  # never for links
        return link

    def assertRefused(self, response, status, text=""):
        self.assertEqual(response.status_code, status, response.content[:300])
        self.assertIn("text/html", response["Content-Type"])  # the site's page, at the link's address
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")
        if text:
            self.assertIn(text.encode(), response.content)

    # -- who may download (未登录的人也能用直链下载 off, the default)

    def test_a_logged_in_user_with_download(self):
        self.login()
        response = self.client.get(self.path_link("/docs/a.txt"))
        self.assertDownload(response)
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        # The /pd/ link it leads to gives the file.
        self.assertEqual(b"".join(self.client.get(response["Location"]).streaming_content), b"hello")

    def test_the_guest_needs_a_grant_and_download(self):
        response = self.client.get(self.path_link("/docs/a.txt"))
        self.assertEqual(response.status_code, 401)  # the page offers to log in
        self.assertIn(b"/login/?next=", response.content)
        self.grant(self.guest, [Permission.PREVIEW])
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 403)  # D40a: preview isn't enough
        self.grant(self.guest, [Permission.DOWNLOAD])
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))

    def test_download_is_needed_not_generate_link(self):
        self.grant(self.alice, [Permission.DOWNLOAD])
        self.login()
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))
        self.grant(self.alice, EVERYTHING_BUT(Permission.DOWNLOAD))
        self.assertRefused(self.client.get(self.path_link("/docs/a.txt")), 403, "下载")

    def test_inside_the_visitors_own_root(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.login()
        # The path in a link is storage-absolute: it includes the maker's root.
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))
        self.assertRefused(self.client.get(self.path_link("/top.txt")), 403, "这个位置不能访问")
        self.assertRefused(self.client.get("/directlink/disk/docs/../top.txt"), 400)

    def test_tricky_paths(self):
        self.login()
        self.assertEqual(self.client.get("/directlink/disk/docs\\..\\top.txt").status_code, 400)
        self.assertEqual(self.client.get("/directlink/disk/").status_code, 404)
        self.assertEqual(self.client.get("/directlink/disk").status_code, 404)
        self.assertEqual(self.client.get("/directlink/").status_code, 404)
        self.assertEqual(self.client.get("/directlink").status_code, 404)
        self.assertEqual(self.client.get("/directlink/nope/docs/a.txt").status_code, 404)
        self.assertDownload(self.client.get("/directlink/DISK/docs/a.txt"))  # keys compare without case
        self.assertDownload(self.client.get("/directlink/disk//docs//a.txt/"), "/docs/a.txt")
        self.assertRefused(self.client.get("/directlink/disk/docs/./a.txt"), 400)
        self.assertEqual(self.client.post("/directlink/disk/docs/a.txt").status_code, 405)

    @unittest.skipIf(os.name == "nt", "Windows has no names with ? in them")
    def test_a_name_that_needs_encoding(self):
        self.files({"docs/50% #1?.txt": b"x"})
        self.login()
        self.assertDownload(self.client.get(self.path_link("/docs/50% #1?.txt")), "/docs/50% #1?.txt")

    def test_filter_rules_apply_whatever_ignore_hidden_says(self):
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        self.login()
        self.hide("/docs/a.txt")  # only hidden: still reachable by its path
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))
        self.hide("/docs/b.txt", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertRefused(self.client.get(self.path_link("/docs/b.txt")), 403, "这个文件不能下载")
        self.hide("/docs/sub", mode=FilterMode.INACCESSIBLE)
        self.assertRefused(self.client.get(self.path_link("/docs/sub/c.txt")), 403, "这个位置不能访问")

    def test_a_password_folder(self):
        rule = self.lock("/docs")
        self.login()
        response = self.client.get(self.path_link("/docs/a.txt"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("需要密码".encode(), response.content)
        self.assertIn(b'href="/disk/docs"', response.content)  # where to enter it
        # Unlocked in this browser: the link works there, and only there.
        self.assertEqual(self.unlock("/docs").status_code, 200)
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))
        other = self.visitor()
        other.force_login(self.alice)
        self.assertEqual(other.get(self.path_link("/docs/a.txt")).status_code, 403)
        # A new password locks it again (the rules page gives the rule a new revision).
        rule.password, rule.revision = "pw2", rule.revision + 1
        rule.save()
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 403)

    def test_ignore_password(self):
        self.lock("/docs")
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])
        self.login()
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt")))

    def test_a_disabled_storage(self):
        self.login()
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 403)

    def test_path_links_off(self):
        self.login()
        self.site(show_path_link=False)
        response = self.client.get(self.path_link("/docs/a.txt"))
        self.assertEqual(response.status_code, 403)
        self.assertIn("当前系统不允许使用直链".encode(), response.content)
        self.site(show_path_link=True, path_link_anonymous=True)
        self.site(show_path_link=False)
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 403)

    # -- 未登录的人也能用直链下载 on

    def test_anonymous_links(self):
        self.site(path_link_anonymous=True)
        visitor = self.visitor()
        self.assertDownload(visitor.get(self.path_link("/docs/a.txt")))  # the guest has no grant at all
        self.lock("/docs")  # anybody: the link carries no password and none is asked for
        self.assertDownload(visitor.get(self.path_link("/docs/a.txt")))
        # check_served() still: the filter rules and the storage's switch.
        self.hide("/docs/b.txt", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertEqual(visitor.get(self.path_link("/docs/b.txt")).status_code, 403)
        self.hide("/docs/sub", mode=FilterMode.INACCESSIBLE)
        self.assertEqual(visitor.get(self.path_link("/docs/sub/c.txt")).status_code, 403)
        self.assertEqual(visitor.get("/directlink/nope/docs/a.txt").status_code, 404)
        self.assertDownload(visitor.get("/directlink/DISK/docs/a.txt"))
        for key in NOT_DISK:  # API-16
            with self.subTest(key=key):
                self.assertEqual(visitor.get(f"/directlink/{urllib.parse.quote(key)}/docs/a.txt").status_code, 404)
        self.assertEqual(visitor.get("/directlink/disk/docs\\..\\top.txt").status_code, 400)
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        self.assertEqual(visitor.get(self.path_link("/docs/a.txt")).status_code, 403)

    # -- the prefix

    def test_the_prefix_can_change(self):
        self.login()
        self.site(direct_link_prefix="dl")
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt", prefix="dl")))
        self.assertDownload(self.client.get(self.path_link("/docs/a.txt", prefix="DL")))  # without case, as apps.core.reserved
        # The old one ends at once: /directlink/ is one of the site's own names, a plain 404 now.
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 404)
        self.assertEqual(DownloadLog.objects.count(), 1)  # one person's openings of one file, not the 404

    def test_a_prefix_never_equals_a_storage_key(self):
        admin = self.make_admin()
        self.client.force_login(admin)
        self.csrf()
        response = self.client.patch("/api/v1/manage/settings/link/", {"direct_link_prefix": "disk"}, format="json")
        self.assertEqual(response.status_code, 400, response.content)
        self.site(direct_link_prefix="files2")
        response = self.client.get("/api/v1/manage/storages/key-check/", {"key": "files2"})
        self.assertFalse(response.json()["ok"])
        self.assertIn("直链", response.json()["message"])

    def make_admin(self):
        from apps.accounts.models import User

        return User.objects.create_superuser("root", password="pw-root-2026x")

    def test_the_browser_still_has_the_rest(self):
        self.login()
        self.assertEqual(self.client.get("/disk/docs/").status_code, 200)
        self.site(direct_link_prefix="docs")
        # A folder named like the prefix inside a storage is no problem: only the first segment is the prefix.
        self.assertEqual(self.client.get("/disk/docs/").status_code, 200)
