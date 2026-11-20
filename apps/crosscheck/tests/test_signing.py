"""Rule 4, signed URLs (apps.files.signing): a signature made for one storage, path, signer (忽略隐藏) or
disposition works for no other, nor after it expires; what the preview sources accept; and the links and
shares that hand them out, re-checked as their creator and their sharer (README 直链和短链, 分享)."""

from __future__ import annotations

import datetime
import time
import unittest
from unittest import mock

from django.test import override_settings
from django.utils import timezone

from apps.accounts.permissions import Permission
from apps.files import signing
from apps.links.models import ShortLink
from apps.shares.models import Share
from apps.storages.models import Grant

from .base import EVERYTHING, FILE, FOLDER, CrossCase, FilterMode, but, query_of


def with_query(url, **changes):
    """The link with these query parameters changed (None removes one), in the order they were."""
    base, _, query = url.partition("?")
    pairs = [pair.split("=", 1) for pair in query.split("&") if pair]
    seen = set()
    out = []
    for name, value in pairs:
        if name in changes:
            seen.add(name)
            if changes[name] is None:
                continue
            value = changes[name]
        out.append(f"{name}={value}")
    for name, value in changes.items():
        if name not in seen and value is not None:
            out.insert(len(out) - 1, f"{name}={value}")  # before sign=
    return f"{base}?{'&'.join(out)}"


class SignatureTests(CrossCase):
    def setUp(self):
        super().setUp()
        self.link = signing.sign(self.storage, "/docs/a.txt", lifetime=600)
        self.inline = signing.sign(self.storage, "/docs/a.txt", lifetime=600, inline=True)

    def get(self, url):
        return self.anon.get(url)

    def assertInvalid(self, url, code="link_invalid"):
        response = self.get(url)
        self.assertEqual(response.status_code, 403, url)
        self.assertEqual(response.headers.get("Content-Type", "").split(";")[0], "text/html")
        return response

    def test_the_links_work(self):
        self.assertEqual(self.body(self.get(self.link)), b"alpha")
        self.assertEqual(self.body(self.get(self.inline)), b"alpha")

    def test_another_storage(self):
        twin = self.make_storage("twin", self.root)  # the same folder, another storage
        self.assertInvalid(self.link.replace("/pd/disk/", "/pd/twin/"))
        twin_link = signing.sign(twin, "/docs/a.txt", lifetime=600)
        self.assertEqual(self.body(self.get(twin_link)), b"alpha")
        self.assertInvalid(twin_link.replace("/pd/twin/", "/pd/disk/"))
        # A storage deleted and made again under the same key: a new link secret.
        old = signing.sign(twin, "/docs/a.txt", lifetime=600)
        twin.delete()
        self.make_storage("twin", self.root)
        self.assertInvalid(old)

    def test_another_path(self):
        for path in ("/docs/b.txt", "/Docs/a.txt", "/docs/A.txt", "/docs/a.txt.", "/docs//a.txt", "/docs/./a.txt",
                     "/docs/sub/../a.txt", "/docs/a.txt/", "/docs%2Fa.txt%2F"):
            with self.subTest(path=path):
                self.assertNotEqual(self.get(self.link.replace("/docs/a.txt", path, 1)).status_code, 200)
        # The same path spelled with escapes is the same path.
        self.assertEqual(self.body(self.get(self.link.replace("/docs/a.txt", "/docs/%61.txt", 1))), b"alpha")

    def test_another_signer(self):
        """h=1 (the signer had 忽略隐藏) is part of the signature: it can't be added, nor taken away."""
        self.assertInvalid(with_query(self.link, h="1"))
        # Links are bearer links, not bound to a user (an accepted limit): only to the file they name.
        self.assertEqual(self.body(self.client_for(self.bob).get(self.link)), b"alpha")
        self.hide("/docs/a.txt", FilterMode.INACCESSIBLE)
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        bypassing = self.file_url(self.as_alice, "/docs/a.txt")
        self.assertEqual(query_of(bypassing)["h"], ["1"])
        self.assertEqual(self.body(self.get(bypassing)), b"alpha")
        self.assertInvalid(with_query(bypassing, h=None))
        self.assertInvalid(with_query(bypassing, h=None).replace("/docs/a.txt", "/docs/b.txt"))
        self.assertInvalid(self.link)  # a.txt is inaccessible now, for a link signed without h=1

    def test_another_disposition(self):
        self.assertEqual(query_of(self.inline)["d"], ["i"])
        for url in (with_query(self.inline, d="a"), with_query(self.inline, d=None), with_query(self.link, d="i"),
                    with_query(self.link, d="a"), with_query(self.link, r="1"), with_query(self.link, n="evil.html"),
                    with_query(self.link, e=str(int(query_of(self.link)["e"][0]) + 3600)),
                    with_query(self.link, e=None)):
            with self.subTest(url=url):
                self.assertInvalid(url)

    def test_twice_the_same_parameter(self):
        self.assertInvalid(self.link + "&sign=x")
        self.assertInvalid(self.inline.replace("d=i", "d=i&d=a"))

    def test_expired(self):
        expires = signing.verify_url(self.link).expires
        with mock.patch("apps.files.signing.time.time", return_value=expires + 1):
            response = self.get(self.link)
        self.assertEqual(response.status_code, 403)
        self.assertIn("过期".encode(), response.content)
        with mock.patch("apps.files.signing.time.time", return_value=expires - 1):
            self.assertEqual(self.get(self.link).status_code, 200)

    def test_a_new_link_secret(self):
        self.storage.permanent_links = True
        self.storage.save()
        permanent = signing.sign(self.storage, "/docs/a.txt", lifetime=None)
        self.assertNotIn("e", query_of(permanent))
        self.assertEqual(self.get(permanent).status_code, 200)
        self.assertOk(self.as_staff.post(f"/api/v1/manage/storages/{self.storage.pk}/rotate-link-secret/"), 204)
        for url in (permanent, self.link, self.inline):
            with self.subTest(url=url):
                self.assertInvalid(url)

    def test_a_new_secret_key(self):
        with override_settings(SECRET_KEY="x" * 50, SECRET_KEY_FALLBACKS=[]):
            self.assertInvalid(self.link)
        from django.conf import settings

        with override_settings(SECRET_KEY="x" * 50, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            self.assertEqual(self.get(self.link).status_code, 200)

    def test_the_preview_sources(self):
        """The Markdown and kkFileView endpoints take a viewer's /pd/ link as the proof, checked the same."""
        self.files({"docs/n.md": b"# hi"})
        self.site(kkfileview_url="https://kk.example.net")
        good = signing.sign(self.storage, "/docs/n.md", lifetime=600, inline=True)
        self.assertOk(self.preview_markdown(self.anon, good))
        self.assertOk(self.kkfileview(self.anon, good))
        expires = signing.verify_url(good).expires
        bad = {
            "tampered": good[:-2] + ("AA" if not good.endswith("AA") else "BB"),
            "another file": good.replace("/docs/n.md", "/docs/a.txt"),
            "another storage": good.replace("/pd/disk/", "/pd/twin/"),
            "absolute": "https://evil.example" + good,
            "not /pd/": good.replace("/pd/", "/pdx/"),
            "unsigned": good.split("&sign=")[0],
        }
        self.make_storage("twin", self.root)
        for what, url in bad.items():
            with self.subTest(what):
                self.assertRefused(self.preview_markdown(self.anon, url), 403)
                self.assertRefused(self.kkfileview(self.anon, url), 403)
        with mock.patch("apps.files.signing.time.time", return_value=expires + 1):
            self.assertRefused(self.preview_markdown(self.anon, good), 403, "link_expired")
            self.assertRefused(self.kkfileview(self.anon, good), 403, "link_expired")
        self.disable_storage()
        self.assertRefused(self.preview_markdown(self.anon, good), 403, "storage_disabled")


class ShortLinkCreatorTests(CrossCase):
    """D31: a short link is opened as its creator, as he is now; nothing of the visitor's counts."""

    def setUp(self):
        super().setUp()
        self.link = self.short_link(self.as_alice, "/docs/a.txt")
        self.visitor = self.client_for()

    def opens(self):
        response, target, body = self.follow(self.visitor, self.link)
        return response.status_code == 302 and body == b"alpha"

    def assertStopped(self, status=403):
        response = self.visitor.get(self.link)
        self.assertEqual(response.status_code, status, response.content[:300])

    def test_his_account(self):
        self.assertTrue(self.opens())
        self.alice.is_active = False
        self.alice.save()
        self.assertStopped()
        self.alice.is_active = True
        self.alice.save()
        self.assertTrue(self.opens())

    def test_his_grant_and_permissions(self):
        for change in ({"enabled": False}, {"permissions": but(Permission.GENERATE_SHORT_LINK)},
                       {"permissions": but(Permission.DOWNLOAD)}):
            with self.subTest(change=change):
                Grant.objects.filter(user=self.alice, storage=self.storage).update(**change)
                self.assertStopped()
                Grant.objects.filter(user=self.alice, storage=self.storage).update(enabled=True, permissions=EVERYTHING)
                self.assertTrue(self.opens())
        Grant.objects.filter(user=self.alice).delete()
        self.assertStopped()

    def test_the_rules_now(self):
        for mode in (FilterMode.INACCESSIBLE, FilterMode.DISABLE_DOWNLOAD):
            with self.subTest(mode=mode):
                rule = self.hide("/docs", mode)
                self.assertStopped()
                rule.delete()
                self.assertTrue(self.opens())
        rule = self.hide("/docs/a.txt", FilterMode.HIDDEN)
        self.assertTrue(self.opens())
        rule.delete()
        self.lock("/docs", "pw1")
        self.assertStopped()

    def test_the_storage_and_the_site(self):
        self.disable_storage()
        self.assertStopped()
        self.storage.enabled = True
        self.storage.save()
        self.assertTrue(self.opens())
        self.site(show_short_link=False)
        self.assertStopped()
        self.site(show_short_link=True)
        ShortLink.objects.update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertStopped()

    def test_his_deletion(self):
        self.alice.delete()
        self.assertStopped(404)

    def test_not_the_visitors_state(self):
        """A visitor with every bypass can't open a link its creator may no longer open; one with
        nothing at all opens a good one."""
        self.hide("/docs", FilterMode.INACCESSIBLE)
        self.grant(self.staff, [*EVERYTHING, Permission.IGNORE_HIDDEN, Permission.IGNORE_PASSWORD])
        self.assertEqual(self.as_staff.get(self.link).status_code, 403)
        from apps.storages.models import FilterRule

        FilterRule.objects.all().delete()
        Grant.objects.filter(user=self.staff).delete()
        response, _, body = self.follow(self.as_staff, self.link)
        self.assertEqual(body, b"alpha")

    def test_what_it_sends_to(self):
        """The /pd/ link it redirects to: never permanent, never h=1, valid ten minutes at most."""
        self.storage.permanent_links = True
        self.storage.save()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        location = self.visitor.get(self.link)["Location"]
        query = query_of(location)
        self.assertNotIn("h", query)
        self.assertLessEqual(int(query["e"][0]), time.time() + 600 + 300)
        self.assertEqual(self.visitor.get(self.link)["Cache-Control"], "no-cache, no-store, must-revalidate, private")


class PathLinkVisitorTests(CrossCase):
    """A path link is opened as its visitor (D40a: 下载, his root, the rules, his unlocked folders); with
    未登录的人也能用直链下载 (D40b) anybody, the rules still applied."""

    def setUp(self):
        super().setUp()
        self.link = self.path_link(self.as_alice, "/docs/a.txt")

    def test_the_visitor_decides(self):
        self.assertRefused(self.anon.get(self.link), 401)
        self.grant(self.guest, [Permission.PREVIEW])
        self.assertRefused(self.anon.get(self.link), 403)
        self.grant(self.guest, [Permission.DOWNLOAD])
        self.assertEqual(self.follow(self.anon, self.link)[2], b"alpha")
        self.assertRefused(self.as_bob.get(self.link), 403)  # outside bob's root
        self.grant(self.alice, but(Permission.DOWNLOAD))
        self.assertRefused(self.as_alice.get(self.link), 403)

    def test_anybody_when_the_site_says_so(self):
        self.site(path_link_anonymous=True)
        self.assertEqual(self.follow(self.anon, self.link)[2], b"alpha")
        self.assertEqual(self.follow(self.as_bob, "/directlink/disk/top.txt")[2], b"top")
        self.hide("/docs", FilterMode.DISABLE_DOWNLOAD)
        self.assertRefused(self.anon.get(self.link), 403)
        self.disable_storage()
        self.assertRefused(self.anon.get("/directlink/disk/top.txt"), 403)

    def test_switched_off(self):
        self.site(show_path_link=False)
        self.assertRefused(self.as_alice.get(self.link), 403)
        self.assertRefused(self.path_links(self.as_alice, ["/docs/a.txt"]), 403, "path_links_off")

    def test_what_it_sends_to(self):
        self.storage.permanent_links = True
        self.storage.save()
        location = self.as_alice.get(self.link)["Location"]
        self.assertTrue(location.startswith("/pd/disk/docs/a.txt?"), location)
        self.assertLessEqual(int(query_of(location)["e"][0]), time.time() + 600 + 300)


class ShareSharerTests(CrossCase):
    """A share is visited as its sharer, as he is now (apps/shares/access.py open_share)."""

    def setUp(self):
        super().setUp()
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER), ("top.txt", FILE)])

    def works(self):
        response = self.vdownload(self.anon, self.share, "/docs/a.txt")
        return response.status_code == 302 and self.served(self.anon, response)[1] == b"alpha"

    def assertStopped(self, status=403, code=None):
        self.assertRefused(self.vlist(self.anon, self.share, "/docs"), status, code)
        self.assertRefused(self.vitem(self.anon, self.share, "/docs/a.txt"), status, code)
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/a.txt"), status)
        self.assertRefused(self.vpreview(self.anon, self.share, "/docs/a.txt"), status)
        self.assertNotIn("entries", self.vinfo(self.anon, self.share).json())

    def test_his_account(self):
        self.assertTrue(self.works())
        self.alice.is_active = False
        self.alice.save()
        self.assertStopped(403, "actor_disabled")
        self.alice.is_active = True
        self.alice.save()
        self.assertTrue(self.works())

    def test_his_grant(self):
        Grant.objects.filter(user=self.alice).update(enabled=False)
        self.assertStopped(403, "no_grant")
        Grant.objects.filter(user=self.alice).update(enabled=True, permissions=but(Permission.CREATE_SHARE_LINK))
        self.assertStopped(403, "share_unavailable")
        Grant.objects.filter(user=self.alice).update(permissions=EVERYTHING)
        self.assertTrue(self.works())
        Grant.objects.filter(user=self.alice).delete()
        self.assertStopped(403, "no_grant")

    def test_his_preview_and_download(self):
        self.grant(self.alice, but(Permission.DOWNLOAD))
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/a.txt"), 403)
        self.assertEqual(self.vpreview(self.anon, self.share, "/docs/a.txt").status_code, 302)
        self.assertIsNone(self.vitems(self.anon, self.share, "/docs")["a.txt"]["download_url"])
        self.grant(self.alice, but(Permission.PREVIEW))
        self.assertRefused(self.vpreview(self.anon, self.share, "/docs/a.txt"), 403)
        self.assertIsNone(self.assertOk(self.vitem(self.anon, self.share, "/docs/a.txt")).json()["inline_url"])
        self.assertTrue(self.works())

    def test_the_storage(self):
        self.disable_storage()
        self.assertRefused(self.vlist(self.anon, self.share, "/docs"), 403, "storage_disabled")
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/a.txt"), 403)

    def test_his_deletion_and_the_expiry(self):
        Share.objects.filter(pk=self.share.pk).update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertRefused(self.vlist(self.anon, self.share, "/docs"), 404, "share_expired")
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/a.txt"), 404)
        Share.objects.filter(pk=self.share.pk).update(expires_at=None)
        self.alice.delete()
        self.assertRefused(self.vlist(self.anon, self.share, "/docs"), 404, "share_not_found")

    def test_a_password(self):
        share = self.share_of(self.alice, "/", [("docs", FOLDER)], password="pass-1234")
        info = self.vinfo(self.anon, share).json()
        self.assertEqual(set(info), {"key", "need_password", "verified", "expired", "expires_at", "entry_count", "owner"})
        self.assertRefused(self.vlist(self.anon, share, "/docs"), 403, "share_password_required")
        self.assertRefused(self.vitem(self.anon, share, "/docs/a.txt"), 403, "share_password_required")
        response = self.vdownload(self.anon, share, "/docs/a.txt")
        self.assertEqual(response.status_code, 302)
        self.assertFalse(response["Location"].startswith("/pd/"), response["Location"])
        self.assertRefused(self.vverify(self.anon, share, "wrong-one"), 403, "share_password_wrong")
        self.assertOk(self.vverify(self.anon, share, "pass-1234"))
        self.assertEqual(self.vnames(self.anon, share, "/docs"), ["a.txt", "b.txt", "sub"])
        # The cookie is this share's: another share with the same password still asks.
        other = self.share_of(self.alice, "/", [("docs", FOLDER)], password="pass-1234")
        self.assertRefused(self.vlist(self.anon, other, "/docs"), 403, "share_password_required")

    def test_a_password_in_the_address_is_never_checked(self):
        """?pwd= only fills in the form (10.Z.1): the page doesn't verify the visitor with it."""
        share = self.share_of(self.alice, "/", [("docs", FOLDER)], password="pass-1234")
        page = self.anon.get(f"/share/{share.key}?pwd=pass-1234")
        self.assertEqual(page.status_code, 200)
        self.assertRefused(self.vlist(self.anon, share, "/docs"), 403, "share_password_required")
        self.assertRefused(self.anon.get(f"/api/v1/shares/{share.key}/list/?path=/docs&pwd=pass-1234"), 403)
        self.assertNotEqual(self.anon.get(f"/share/{share.key}/download?path=/docs/a.txt&pwd=pass-1234")["Location"][:4],
                            "/pd/")

    def test_the_lists_and_deleting(self):
        """我的分享 shows one's own; 分享列表 is for admins; deleting another's share is for superusers (D32a)."""
        bobs = self.share_of(self.bob, "/home/bob", [("bob.txt", FILE)])
        self.assertEqual([row["key"] for row in self.as_alice.get("/api/v1/shares/").json()["results"]], [self.share.key])
        self.assertRefused(self.as_alice.get("/api/v1/manage/shares/"), 403)
        self.assertEqual(self.as_staff.get("/api/v1/manage/shares/").json()["count"], 2)
        self.assertRefused(self.as_alice.delete(f"/api/v1/shares/{bobs.key}/"), 403, "not_owner")
        self.assertRefused(self.as_staff.delete(f"/api/v1/manage/shares/{bobs.key}/"), 403, "not_owner")
        self.assertEqual(self.client_for(self.boss).delete(f"/api/v1/manage/shares/{bobs.key}/").status_code, 204)
        self.assertEqual(self.as_alice.delete(f"/api/v1/shares/{self.share.key}/").status_code, 204)

    def test_its_links_last_an_hour_at_most(self):
        self.storage.permanent_links = True
        self.storage.save()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        for response in (self.vdownload(self.anon, self.share, "/docs/a.txt"),
                         self.vpreview(self.anon, self.share, "/docs/a.txt")):
            query = query_of(response["Location"])
            self.assertNotIn("h", query)
            self.assertLessEqual(int(query["e"][0]), time.time() + 3600 + 300)
        inline = self.assertOk(self.vitem(self.anon, self.share, "/docs/a.txt")).json()["inline_url"]
        self.assertLessEqual(int(query_of(inline)["e"][0]), time.time() + 3600 + 300)

    def test_its_links_end_with_it(self):
        """README 分享: 从分享交出去的本站链接最多一小时，也不会比分享晚过期. A share that ends in ten
        seconds hands out links that outlive it (the 60 s minimum and the signing's rounding up)."""
        ends = timezone.now() + datetime.timedelta(seconds=10)
        Share.objects.filter(pk=self.share.pk).update(expires_at=ends)
        location = self.vdownload(self.anon, self.share, "/docs/a.txt")["Location"]
        inline = self.assertOk(self.vitem(self.anon, self.share, "/docs/a.txt")).json()["inline_url"]
        for url in (location, inline):
            with self.subTest(url=url):
                self.assertLessEqual(int(query_of(url)["e"][0]), int(ends.timestamp()), "the link outlives the share")
