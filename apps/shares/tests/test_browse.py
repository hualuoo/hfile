"""Inside a share: the paths, the owner re-checked on every request, the rules visitors always face, the
owner's preview and download, the downloads through the share and the counts, and contract S1
(inventory 10.3-10.7, D32)."""

from __future__ import annotations

import datetime
import urllib.parse

from django.test import RequestFactory
from django.utils import timezone

from apps.accounts.permissions import Permission
from apps.files import signing
from apps.shares import access as shares
from apps.shares.models import FILE, FOLDER
from apps.storages.access import AccessDenied

from .helpers import EVERYTHING, SharesTestCase


class PathTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.files({"docs/sub/deep/d.txt": b"d", "other/o.txt": b"o"})
        self.share_ = self.files_share()

    def test_the_root_shows_only_the_entries(self):
        self.assertEqual(self.vnames(self.share_), ["docs", "top.txt"])  # not "other"
        data = self.vlist(self.share_).json()
        self.assertEqual((data["path"], data["root"], data["readme"]), ("/", None, None))
        self.assertEqual(self.vnames(self.share_, "/docs"), ["sub", "a.txt", "b.txt"])
        self.assertEqual(self.vnames(self.share_, "/docs/sub/deep"), ["d.txt"])

    def test_outside_the_entries(self):
        for path in ("/other", "/other/o.txt", "/nothing", "/top.txt/x"):
            with self.subTest(path=path):
                response = self.vlist(self.share_, path)
                self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        self.assertEqual(self.vlist(self.share_, "/top.txt").json()["code"], "not_found")  # a file isn't a folder
        self.assertEqual(self.vget(self.share_, "item", path="/other/o.txt").status_code, 404)
        download = self.visitor.get(f"/share/{self.share_.key}/download", {"path": "/other/o.txt"})
        self.assertEqual(download.status_code, 404)

    def test_dot_dot(self):
        for path in ("/docs/../other", "/../", "/docs/sub/../../other/o.txt", "/docs\\..\\..\\other"):
            with self.subTest(path=path):
                response = self.vlist(self.share_, path)
                self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_path"))
        response = self.visitor.get(f"/share/{self.share_.key}/download", {"path": "/docs/../other/o.txt"})
        self.assertEqual(response.status_code, 400)

    def test_a_share_of_a_subfolder(self):
        share = self.share((("sub", FOLDER),), folder="/docs")
        self.assertEqual(self.vnames(share), ["sub"])
        self.assertEqual(self.vnames(share, "/sub"), ["deep", "c.txt"])
        item = self.vget(share, "item", path="/sub/c.txt").json()
        self.assertEqual(item["path"], "/sub/c.txt")  # paths in the share, never the storage's
        self.assertEqual(self.vget(share, "item", path="/a.txt").status_code, 404)  # docs/a.txt isn't shared

    def test_a_shared_file_replaced_by_a_folder(self):
        share = self.share((("top.txt", FILE),))
        (self.root / "top.txt").unlink()
        (self.root / "top.txt").mkdir()
        self.assertEqual(self.vnames(share), [])  # by name and type
        self.assertEqual(self.vget(share, "item", path="/top.txt").status_code, 404)

    def test_the_shared_folder_moved_away(self):
        share = self.share((("sub", FOLDER),), folder="/docs")
        (self.root / "docs").rename(self.root / "moved")
        response = self.vlist(share)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        self.assertEqual(self.reload(share).access_count, 0)

    def test_folders_arent_downloaded(self):
        response = self.visitor.get(f"/share/{self.share_.key}/download", {"path": "/docs"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("暂不支持下载文件夹", response.content.decode())
        self.assertEqual(self.visitor.get(f"/share/{self.share_.key}/download", {"path": "/"}).status_code, 404)


class OwnerTests(SharesTestCase):
    """The owner is re-checked on every request; giving back what was taken revives the share."""

    def setUp(self):
        super().setUp()
        self.share_ = self.files_share()

    def refused(self, code):
        response = self.vlist(self.share_)
        self.assertEqual((response.status_code, response.json().get("code")), (403, code), response.content)
        download = self.visitor.get(f"/share/{self.share_.key}/download", {"path": "/top.txt"})
        self.assertEqual(download.status_code, 403)
        self.assertEqual(self.vget(self.share_, "item", path="/top.txt").status_code, 403)

    def works(self):
        self.assertEqual(self.vnames(self.share_), ["docs", "top.txt"])

    def test_disabled(self):
        self.alice.is_active = False
        self.alice.save()
        self.refused("actor_disabled")
        self.alice.is_active = True
        self.alice.save()
        self.works()

    def test_grant_revoked(self):
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.refused("no_grant")
        self.grant(self.alice, EVERYTHING)
        self.works()

    def test_permission_revoked(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != Permission.CREATE_SHARE_LINK])
        self.refused("share_unavailable")
        self.grant(self.alice, EVERYTHING)
        self.works()

    def test_root_narrowed(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.refused("outside_root")
        self.grant(self.alice, EVERYTHING, root="/")
        self.works()

    def test_storage_disabled(self):
        self.storage.enabled = False
        self.storage.save()
        self.refused("storage_disabled")


class VisitorRuleTests(SharesTestCase):
    """Filters and folder passwords apply to visitors whatever the owner's bypasses (10.Z.3)."""

    def test_filters_always(self):
        share = self.files_share()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN, Permission.IGNORE_PASSWORD])
        self.hide("/docs/b.txt")
        self.hide("/docs/sub", mode="inaccessible")
        self.hide("/docs/a.txt", mode="disable_download")
        self.assertEqual(self.vnames(share, "/docs"), [])
        # Hidden is only hidden: reachable by its path (D10). Inaccessible and no-download are refused.
        self.assertEqual(self.vget(share, "item", path="/docs/b.txt").status_code, 200)
        self.assertEqual(self.vlist(share, "/docs/sub").json()["code"], "inaccessible")
        self.assertEqual(self.vget(share, "item", path="/docs/a.txt").json()["code"], "download_disabled")
        response = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}).status_code, 403)
        # The owner, with ignore_hidden, sees them in his own browsing.
        self.login()
        self.assertEqual(self.names("/docs"), ["sub", "a.txt", "b.txt"])

    def test_folder_passwords_always_and_bound_to_the_share(self):
        share = self.files_share()
        other = self.files_share()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])
        rule = self.lock("/docs", password="pw1")
        locked = self.vlist(share, "/docs")
        self.assertEqual((locked.status_code, locked.json()["code"]), (403, "password_required"))
        self.assertEqual(locked.json()["rule"], rule.pk)
        self.assertEqual(self.vnames(share), ["docs", "top.txt"])  # the folder shows, with its lock
        self.assertTrue(next(i for i in self.vlist(share).json()["items"] if i["name"] == "docs")["locked"])
        # Unlocking the visitor's own way (the storages API) doesn't open it inside the share.
        self.assertEqual(self.unlock("/docs", key="disk").status_code, 401)  # the guest has no grant
        wrong = self.vpost(share, "unlock", {"path": "/docs", "password": "nope"})
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (403, "password_wrong"))
        right = self.vpost(share, "unlock", {"path": "/docs", "password": "pw1", "remember": True})
        self.assertEqual(right.status_code, 200)
        self.assertNotIn("hfile_folder_keys", right.cookies)  # never 记住密码
        self.assertEqual(self.vnames(share, "/docs"), ["sub", "a.txt", "b.txt"])
        self.assertEqual(self.vlist(other, "/docs").json()["code"], "password_required")  # that share only
        by_rule = self.vpost(other, "unlock", {"rule": rule.pk, "password": "pw1"})
        self.assertEqual(by_rule.status_code, 200)
        self.assertEqual(self.vlist(other, "/docs").status_code, 200)
        # 清理密码文件夹缓存
        self.assertEqual(self.client_delete(share, "unlocks").status_code, 204)
        self.assertEqual(self.vlist(share, "/docs").json()["code"], "password_required")
        self.assertEqual(self.vlist(other, "/docs").status_code, 200)

    def test_a_rule_outside_the_share_cant_be_tried(self):
        share = self.share((("sub", FOLDER),), folder="/docs")
        rule = self.lock("/other", password="pw1")
        response = self.vpost(share, "unlock", {"rule": rule.pk, "password": "pw1"})
        self.assertEqual((response.status_code, response.json()["code"]), (404, "rule_not_found"))

    def test_a_password_share_and_its_folder_passwords(self):
        share = self.files_share(password="abcd")
        self.lock("/docs", password="pw1")
        self.assertEqual(self.vpost(share, "unlock", {"path": "/docs", "password": "pw1"}).json()["code"],
                         "share_password_required")
        self.verify(share, "abcd")
        self.assertEqual(self.vpost(share, "unlock", {"path": "/docs", "password": "pw1"}).status_code, 200)
        self.assertEqual(self.vnames(share, "/docs"), ["sub", "a.txt", "b.txt"])

    def client_delete(self, share, what):
        if "HTTP_X_CSRFTOKEN" not in self.visitor._credentials:
            self.vcsrf()
        return self.visitor.delete(f"/api/v1/shares/{share.key}/{what}/")

    def test_preview_and_download_are_the_owners(self):
        share = self.files_share()
        self.grant(self.alice, EVERYTHING)
        listed = {item["name"]: item for item in self.vlist(share, "/docs").json()["items"]}
        self.assertTrue(listed["a.txt"]["url"].startswith(f"/share/{share.key}/preview?path="))
        self.assertTrue(listed["a.txt"]["download_url"].startswith(f"/share/{share.key}/download?path="))
        permissions = self.vlist(share).json()["permissions"]
        self.assertEqual((permissions["preview"], permissions["download"], permissions["copy_download_link"]),
                         (True, True, True))
        self.assertEqual(self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}).status_code, 302)
        self.assertTrue(self.vget(share, "item", path="/docs/a.txt").json()["inline_url"])
        # Without 下载 nothing of the content: 预览 needs it too.
        self.grant(self.alice, [code for code in EVERYTHING if code != Permission.DOWNLOAD])
        listed = {item["name"]: item for item in self.vlist(share, "/docs").json()["items"]}
        self.assertEqual((listed["a.txt"]["url"], listed["a.txt"]["download_url"]), (None, None))
        permissions = self.vlist(share).json()["permissions"]
        self.assertEqual((permissions["preview"], permissions["download"], permissions["copy_download_link"]),
                         (False, False, False))
        self.assertEqual(self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"}).status_code, 403)
        self.assertEqual(self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}).status_code, 403)
        self.assertIsNone(self.vget(share, "item", path="/docs/a.txt").json()["inline_url"])
        self.grant(self.alice, [code for code in EVERYTHING if code != Permission.PREVIEW])
        listed = {item["name"]: item for item in self.vlist(share, "/docs").json()["items"]}
        self.assertTrue(listed["a.txt"]["url"].startswith(f"/share/{share.key}/download?path="))
        self.assertEqual(listed["a.txt"]["url"], listed["a.txt"]["download_url"])
        self.assertEqual(self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}).status_code, 403)
        self.assertIsNone(self.vget(share, "item", path="/docs/a.txt").json()["inline_url"])
        self.grant(self.alice, [code for code in EVERYTHING if code not in (Permission.PREVIEW, Permission.DOWNLOAD)])
        listed = {item["name"]: item for item in self.vlist(share, "/docs").json()["items"]}
        self.assertEqual((listed["a.txt"]["url"], listed["a.txt"]["download_url"]), (None, None))

    def test_visitors_may_do_nothing_else(self):
        share = self.files_share()
        permissions = self.vlist(share).json()["permissions"]
        self.assertEqual({code for code, on in permissions.items() if on}, {"preview", "download", "copy_download_link"})
        # The files API is the visitor's own (the guest's): nothing there without a grant.
        self.assertEqual(self.visitor.get("/api/v1/files/disk/list/", {"path": "/docs"}).status_code, 401)


class DownloadTests(SharesTestCase):
    def test_listing_items_carry_the_shares_urls(self):
        share = self.files_share()
        items = self.vlist(share, "/docs").json()["items"]
        for item in items:
            if item["type"] == "file":
                self.assertTrue(item["url"].startswith(f"/share/{share.key}/"), item)
                self.assertNotIn("/pd/", item["url"])
                self.assertNotIn("/pd/", item["download_url"])
            else:
                self.assertIsNone(item["url"])

    def test_the_download_redirects_to_the_file_and_counts(self):
        share = self.files_share()
        response = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")
        target = response["Location"]
        self.assertTrue(target.startswith("/pd/disk/docs/a.txt?"))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(target).query)
        link = signing.verify(self.storage, "/docs/a.txt", query)
        self.assertFalse(link.ignore_hidden)  # never a bypass from a share
        self.assertEqual(self.visitor.get(target).getvalue(), b"hello")
        self.assertEqual(self.reload(share).download_count, 1)
        preview = self.visitor.get(f"/share/{share.key}/preview", {"path": "/top.txt"})
        self.assertIn("d=i", preview["Location"])  # inline
        item = self.vget(share, "item", path="/top.txt").json()
        self.assertTrue(item["inline_url"].startswith("/pd/disk/top.txt?"))  # files-signed, same origin, inline
        self.assertIn("d=i", item["inline_url"])
        self.assertEqual(self.reload(share).download_count, 3)
        self.assertEqual(self.vget(share, "item", path="/docs").json()["inline_url"], None)
        self.assertEqual(self.reload(share).download_count, 3)  # a folder hands out no URL

    def test_a_preview_read_by_a_script_stays_on_this_site(self):
        """hls.js fetches segments by the listing's URL: a redirect to another origin would need CORS."""
        bucket = self.make_storage("direct", "test-direct", self.root)  # files' bucket-like local variant
        self.grant(self.alice, EVERYTHING, storage=bucket)
        share = self.share((("docs", FOLDER),), storage=bucket)
        media = self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}, HTTP_SEC_FETCH_DEST="video")
        self.assertTrue(media["Location"].startswith("https://cdn.example.test/docs/a.txt?"))  # the storage's own
        script = self.visitor.get(f"/share/{share.key}/preview", {"path": "/docs/a.txt"}, HTTP_SEC_FETCH_DEST="empty")
        self.assertTrue(script["Location"].startswith("/pd/direct/docs/a.txt?"), script["Location"])
        self.assertIn("d=i", script["Location"])
        download = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"}, HTTP_SEC_FETCH_DEST="empty")
        self.assertTrue(download["Location"].startswith("https://cdn.example.test/"))  # downloads: as W1 says

    def test_access_count_is_for_the_root_only(self):
        share = self.files_share()
        self.vlist(share)
        self.vlist(share)
        self.vlist(share, "/docs")
        self.assertEqual(self.reload(share).access_count, 2)
        self.vlist(share, "/nothing")
        self.assertEqual(self.reload(share).access_count, 2)

    def test_head_and_post(self):
        share = self.files_share()
        self.assertEqual(self.visitor.head(f"/share/{share.key}/download", {"path": "/top.txt"}).status_code, 302)
        self.assertIn(self.visitor.post(f"/share/{share.key}/download?path=/top.txt").status_code, (403, 405))
        self.assertEqual(self.reload(share).download_count, 1)  # the HEAD


class ContractS1Tests(SharesTestCase):
    """apps.shares.access.share_file(request, key, path) -> (access, storage_path), for office."""

    def request(self, client=None):
        request = RequestFactory().get("/api/v1/office/config/")
        request.COOKIES = {name: morsel.value for name, morsel in (client or self.visitor).cookies.items()}
        request.session = {}
        request.user = self.guest
        return request

    def test_a_verified_visitor(self):
        share = self.share((("sub", FOLDER), ("a.txt", FILE)), folder="/docs", password="abcd")
        with self.assertRaises(AccessDenied) as caught:
            shares.share_file(self.request(), share.key, "/a.txt")
        self.assertEqual(caught.exception.code, "share_password_required")
        self.verify(share, "abcd")
        access, path = shares.share_file(self.request(), share.key, "/a.txt")
        self.assertEqual(path, "/docs/a.txt")
        self.assertTrue(access.actor.via_share)
        self.assertEqual((access.actor.user, access.root), (self.alice, "/docs"))
        self.assertFalse(access.bypass_filters or access.bypass_passwords)
        access.check("preview", path, is_dir=False)
        self.assertEqual(shares.share_file(self.request(), share.key, "/sub/c.txt")[1], "/docs/sub/c.txt")

    def test_refusals(self):
        share = self.share((("sub", FOLDER), ("a.txt", FILE)), folder="/docs")
        for key, path, code in (("nope1234", "/a.txt", "share_not_found"), (share.key, "/b.txt", "not_found"),
                                (share.key, "/", "not_found"), (share.key, "/sub", "is_folder"),
                                (share.key, "/sub/../../top.txt", "invalid_path")):
            with self.subTest(path=path):
                with self.assertRaises(AccessDenied) as caught:
                    shares.share_file(self.request(), key, path)
                self.assertEqual(caught.exception.code, code)
        self.alice.is_active = False
        self.alice.save()
        with self.assertRaises(AccessDenied) as caught:
            shares.share_file(self.request(), share.key, "/a.txt")
        self.assertEqual(caught.exception.code, "actor_disabled")
        self.alice.is_active = True
        self.alice.save()
        share.expires_at = timezone.now() - datetime.timedelta(seconds=1)
        share.save()
        with self.assertRaises(AccessDenied) as caught:
            shares.share_file(self.request(), share.key, "/a.txt")
        self.assertEqual((caught.exception.code, caught.exception.status_code), ("share_expired", 404))

    def test_folder_passwords_unlocked_in_the_share(self):
        share = self.share((("sub", FOLDER),), folder="/docs")
        self.lock("/docs/sub", password="pw1")
        access, path = shares.share_file(self.request(), share.key, "/sub/c.txt")
        with self.assertRaises(AccessDenied) as caught:
            access.check("preview", path, is_dir=False)
        self.assertEqual(caught.exception.code, "password_required")
        self.vpost(share, "unlock", {"path": "/sub", "password": "pw1"})
        access, path = shares.share_file(self.request(), share.key, "/sub/c.txt")
        access.check("preview", path, is_dir=False)
