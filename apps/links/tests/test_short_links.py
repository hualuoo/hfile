"""Short links, /s/<key>: making them (inventory 9.4, D40a, D44) and opening them with the creator checked
again (D31)."""

from __future__ import annotations

import datetime
from unittest import mock

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.permissions import Permission
from apps.core.testing import TO_THE_SECOND, mysql_only
from apps.files import signing
from apps.storages.models import FilterMode, StorageSource

from .. import service
from ..models import DownloadLog, ShortLink
from .helpers import EVERYTHING, EVERYTHING_BUT, LINKS, LinksTestCase

LIFETIMES = [{"value": 1, "unit": "d", "seconds": 86400}, {"value": 1, "unit": "forever", "seconds": -1},
             {"value": 2, "unit": "h", "seconds": 7200}]


class MakingTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.site(short_link_expiries=LIFETIMES)
        self.login()

    def test_one_link(self):
        before = timezone.now()
        response = self.make("short", ["/docs/a.txt"], expires=86400, qr=True)
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        [link] = data["links"]
        self.assertEqual((link["path"], link["name"]), ("/docs/a.txt", "a.txt"))
        self.assertRegex(link["key"], r"^[A-Za-z0-9]{8}$")
        self.assertEqual(link["url"], f"http://testserver/s/{link['key']}")
        row = ShortLink.objects.get(key=link["key"])
        self.assertEqual((row.storage, row.path, row.created_by, row.keys), (self.storage, "/docs/a.txt", self.alice, []))
        self.assertAlmostEqual((row.expires_at - before).total_seconds(), 86400, delta=5)
        self.assertTrue(data["qr"]["svg"].startswith("data:image/svg+xml"))
        self.assertTrue(data["qr"]["png"].startswith("data:image/png;base64,"))

    def test_the_site_url_makes_the_links(self):
        with self.settings(SITE_URL="https://files.example.com"):
            link = self.make("short", ["/docs/a.txt"], expires=86400).json()["links"][0]
        self.assertEqual(link["url"], f"https://files.example.com/s/{link['key']}")

    def test_paths_are_the_callers_own(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        link = self.make("short", ["/a.txt"], expires=86400).json()["links"][0]
        self.assertEqual(link["path"], "/a.txt")
        self.assertEqual(ShortLink.objects.get(key=link["key"]).path, "/docs/a.txt")  # storage-absolute
        self.assertEqual(self.make("short", ["/"], expires=86400).status_code, 400)
        self.assertEqual(self.make("short", ["/../top.txt"], expires=86400).status_code, 400)

    def test_permissions(self):
        self.grant(self.alice, EVERYTHING_BUT(Permission.GENERATE_SHORT_LINK))
        response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "permission_denied"))
        # D40a: 生成短链 isn't enough, a link hands out the file.
        self.grant(self.alice, EVERYTHING_BUT(Permission.DOWNLOAD))
        response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual((response.status_code, response.json()["permission"]), (403, "download"))
        self.grant(self.alice, [Permission.GENERATE_SHORT_LINK, Permission.DOWNLOAD])
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires=86400).status_code, 201)
        self.assertEqual(ShortLink.objects.count(), 1)

    def test_the_guest_too(self):
        self.client.logout()
        self.client.credentials()
        response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual(response.status_code, 401)
        self.grant(self.guest, [Permission.GENERATE_SHORT_LINK, Permission.DOWNLOAD])
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires=86400).status_code, 201)
        self.assertEqual(ShortLink.objects.get().created_by, self.guest)

    def test_csrf(self):
        client = self.visitor()
        client.force_login(self.alice)
        response = client.post(f"{LINKS}disk/short/", {"paths": ["/docs/a.txt"], "expires": 86400}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (403, "csrf_failed"))

    def test_lifetimes(self):
        response = self.make("short", ["/docs/a.txt"], expires=3600)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "invalid_expiry"))
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires="x").status_code, 400)
        self.assertEqual(self.make("short", ["/docs/a.txt"]).status_code, 400)
        forever = self.make("short", ["/docs/a.txt"], expires=-1).json()["links"][0]
        self.assertIsNone(forever["expires_at"])
        day = self.make("short", ["/docs/a.txt"], expires=86400).json()["links"][0]
        self.assertRegex(day["expires_at"], TO_THE_SECOND)  # TEXT-01: it was UTC with microseconds
        self.assertIsNone(ShortLink.objects.get(key=forever["key"]).expires_at)
        self.site(short_link_expiries=[{"value": 1, "unit": "d", "seconds": 86400}])
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires=-1).status_code, 400)

    def test_a_permanent_link_is_reused(self):
        first = self.make("short", ["/docs/a.txt"], expires=-1).json()["links"][0]["key"]
        again = self.make("short", ["/docs/a.txt", "/docs/b.txt", "/docs/a.txt"], expires=-1).json()["links"]
        self.assertEqual(again[0]["key"], first)
        self.assertEqual(again[2]["key"], first)
        self.assertNotEqual(again[1]["key"], first)
        self.assertEqual(ShortLink.objects.count(), 2)
        # Expiring ones are new each time; nor is a permanent one made by someone else reused (it lives as long
        # as its maker may make it, D31).
        self.assertNotEqual(self.make("short", ["/docs/a.txt"], expires=86400).json()["links"][0]["key"], first)
        bob = self.make_user("bob")
        self.client.force_login(bob)
        self.csrf()
        self.assertNotEqual(self.make("short", ["/docs/a.txt"], expires=-1).json()["links"][0]["key"], first)

    def test_reuse_compares_the_path_exactly(self):
        self.files({"docs/A.txt": b"other"})
        lower = self.make("short", ["/docs/a.txt"], expires=-1).json()["links"][0]["key"]
        upper = self.make("short", ["/docs/A.txt"], expires=-1).json()["links"][0]["key"]
        self.assertNotEqual(lower, upper)  # MySQL's collation alone would say they are the same
        self.assertEqual(ShortLink.objects.get(key=upper).path, "/docs/A.txt")

    def make_user(self, name):
        from apps.accounts.models import User

        user = User.objects.create_user(name, password=f"pw-{name}-2026x")
        self.grant(user, EVERYTHING)
        return user

    def test_all_or_nothing(self):
        self.hide("/docs/b.txt", mode=FilterMode.DISABLE_DOWNLOAD)
        response = self.make("short", ["/docs/a.txt", "/docs/b.txt"], expires=86400)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "download_disabled"))
        self.assertFalse(ShortLink.objects.exists())
        many = [f"/f{n}.txt" for n in range(1001)]
        self.assertEqual(self.make("short", many, expires=86400).status_code, 400)
        self.assertEqual(self.make("short", [], expires=86400).status_code, 400)

    def test_ignore_hidden_doesnt_make_links(self):
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        self.hide("/docs", mode=FilterMode.INACCESSIBLE)
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires=86400).status_code, 403)
        self.assertEqual(self.make("path", ["/docs/a.txt"]).status_code, 403)

    def test_password_folders(self):
        rule = self.lock("/docs")
        response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "password_required"))
        self.assertEqual(response.json()["rule"], rule.pk)
        self.assertFalse(ShortLink.objects.exists())
        # Unlocked in this browser: the link keeps what was unlocked.
        self.unlock("/docs")
        link = self.short()
        self.assertEqual(link.keys, [[rule.pk, rule.revision]])
        # A password sent with the request (API clients) counts too.
        self.client.logout()
        self.login()
        self.assertEqual(self.make("short", ["/docs/a.txt"], expires=86400, password="nope").json()["code"], "password_wrong")
        link = self.short(password="pw1")
        self.assertEqual(link.keys, [[rule.pk, rule.revision]])
        # 忽略密码: nothing to keep.
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])
        self.assertEqual(self.short(path="/docs/b.txt").keys, [])

    def test_a_reused_link_takes_the_keys_of_now(self):
        rule = self.lock("/docs")
        self.unlock("/docs")
        link = self.short(expires=-1)
        rule.password, rule.revision = "pw2", rule.revision + 1
        rule.save()
        self.unlock("/docs", password="pw2")
        again = self.short(expires=-1)
        self.assertEqual(again.pk, link.pk)
        self.assertEqual(again.keys, [[rule.pk, rule.revision]])

    def test_short_links_off(self):
        self.site(show_short_link=False)
        response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "short_links_off"))
        response = self.make("short", ["/docs/a.txt"], key="nope", expires=86400)
        self.assertEqual(response.json()["code"], "short_links_off")  # before anything else

    def test_rate_limited(self):
        from rest_framework.throttling import SimpleRateThrottle

        with mock.patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"links": "2/minute"}):
            self.assertEqual(self.make("short", ["/docs/a.txt"], expires=86400).status_code, 201)
            self.assertEqual(self.make("path", ["/docs/a.txt"]).status_code, 200)
            response = self.make("short", ["/docs/a.txt"], expires=86400)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(ShortLink.objects.count(), 1)

    def test_keys_drawn_again_when_taken(self):
        taken = ShortLink.objects.create(key="AAAAAA", storage=self.storage, path="/top.txt", created_by=self.alice)
        draws = iter(["AAAAAA", "AAAAAA", "BBBBBB"])
        with mock.patch.object(service, "new_key", lambda: next(draws)):
            link = self.short()
        self.assertEqual(link.key, "BBBBBB")
        self.assertEqual(ShortLink.objects.get(pk=taken.pk).path, "/top.txt")

    def test_a_key_taken_meanwhile(self):
        real = ShortLink.objects.bulk_create
        calls = []

        def racing(rows, *args, **kwargs):
            calls.append(len(rows))
            if len(calls) == 1:
                with transaction.atomic():
                    ShortLink.objects.create(key=rows[0].key, storage=self.storage, path="/x", created_by=self.alice)
                raise IntegrityError("duplicate")
            return real(rows, *args, **kwargs)

        with mock.patch.object(ShortLink.objects, "bulk_create", racing):
            link = self.short()
        self.assertEqual(len(calls), 2)
        self.assertEqual(ShortLink.objects.filter(path="/docs/a.txt").get().key, link.key)


class PathLinkMakingTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_links(self):
        self.files({"docs/名字 #1.txt": b"x"})
        response = self.make("path", ["/docs/a.txt", "/docs/名字 #1.txt"], qr=True)
        self.assertEqual(response.status_code, 200, response.content)
        links = response.json()["links"]
        self.assertEqual(links[0], {"path": "/docs/a.txt", "name": "a.txt", "url": "http://testserver/directlink/disk/docs/a.txt"})
        self.assertEqual(links[1]["url"], "http://testserver/directlink/disk/docs/%E5%90%8D%E5%AD%97%20%231.txt")
        self.assertNotIn("qr", response.json())  # only for one file
        self.assertFalse(ShortLink.objects.exists())
        # They work (alice may download them herself).
        self.assertEqual(self.client.get(links[1]["url"].removeprefix("http://testserver")).status_code, 302)

    def test_with_the_root_and_the_prefix(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.site(direct_link_prefix="dl")
        link = self.make("path", ["/a.txt"]).json()["links"][0]
        self.assertEqual(link["url"], "http://testserver/dl/disk/docs/a.txt")
        self.assertEqual(link["path"], "/a.txt")

    def test_checked_as_opening_will_be(self):
        self.grant(self.alice, EVERYTHING_BUT(Permission.GENERATE_LINK))
        self.assertEqual(self.make("path", ["/docs/a.txt"]).status_code, 403)
        self.grant(self.alice, EVERYTHING_BUT(Permission.DOWNLOAD))
        self.assertEqual(self.make("path", ["/docs/a.txt"]).json()["permission"], "download")
        self.grant(self.alice, EVERYTHING)
        self.lock("/docs")
        self.assertEqual(self.make("path", ["/docs/a.txt", "/top.txt"]).json()["code"], "password_required")
        self.unlock("/docs")
        self.assertEqual(self.make("path", ["/docs/a.txt", "/top.txt"]).status_code, 200)
        self.site(show_path_link=False)
        self.assertEqual(self.make("path", ["/docs/a.txt"]).json()["code"], "path_links_off")

    def test_no_existence_check(self):
        self.assertEqual(self.make("path", ["/docs/missing.txt"]).status_code, 200)


class OpeningTests(LinksTestCase):
    """GET /s/<key>: the visitor needs nothing, the creator is checked (D31)."""

    def setUp(self):
        super().setUp()
        self.login()
        self.link = self.short()
        self.visitor_client = self.visitor()

    def open(self, key=None, **headers):
        return self.visitor_client.get(f"/s/{key or self.link.key}", **headers)

    def assertDownload(self, response, path="/docs/a.txt"):
        self.assertEqual(response.status_code, 302, response.content[:300])
        link = signing.verify_url(response["Location"])
        self.assertEqual(link.path, path)
        self.assertFalse(link.ignore_hidden)
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")

    def assertRefused(self, response, status, text):
        self.assertEqual(response.status_code, status, response.content[:300])
        self.assertIn(text.encode(), response.content)
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")

    def test_anybody_may_open_it(self):
        self.assertDownload(self.open())
        self.assertDownload(self.visitor_client.get(f"/s/{self.link.key}/"))  # with a slash too
        self.assertEqual(DownloadLog.objects.count(), 1)  # one visitor's openings of one link
        log = DownloadLog.objects.first()
        self.assertEqual((log.kind, log.short_key, log.path, log.storage), ("short", self.link.key, "/docs/a.txt", self.storage))

    def test_unknown_and_malformed_keys(self):
        self.assertRefused(self.open("zzzzzz"), 404, "短链不存在")
        self.assertRefused(self.open("a-b.c"), 404, "短链不存在")
        self.assertRefused(self.open("x" * 40), 404, "短链不存在")

    def test_keys_are_case_sensitive(self):
        twin = self.link.key.swapcase()
        if twin == self.link.key:
            twin = "zZzZzZ"
        self.assertRefused(self.open(twin), 404, "短链不存在")

    def test_expired(self):
        ShortLink.objects.filter(pk=self.link.pk).update(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        self.assertRefused(self.open(), 403, "短链已失效")
        self.assertFalse(DownloadLog.objects.exists())

    def test_forever(self):
        ShortLink.objects.filter(pk=self.link.pk).update(expires_at=None)
        self.assertDownload(self.open())

    def test_short_links_off(self):
        self.site(show_short_link=False)
        self.assertRefused(self.open(), 403, "当前系统不允许使用短链")

    def test_the_creator_disabled(self):
        self.alice.is_active = False
        self.alice.save()
        self.assertRefused(self.open(), 403, "生成它的用户已经不能下载这个文件了")

    def test_the_creator_loses_a_permission(self):
        self.grant(self.alice, EVERYTHING_BUT(Permission.GENERATE_SHORT_LINK))
        self.assertRefused(self.open(), 403, "生成它的用户已经不能下载这个文件了")
        self.grant(self.alice, EVERYTHING_BUT(Permission.DOWNLOAD))
        self.assertRefused(self.open(), 403, "生成它的用户已经不能下载这个文件了")
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertRefused(self.open(), 403, "生成它的用户已经不能下载这个文件了")
        self.grant(self.alice, EVERYTHING)
        self.assertDownload(self.open())

    def test_the_creators_root_narrowed(self):
        self.grant(self.alice, EVERYTHING, root="/docs/sub")
        self.assertRefused(self.open(), 403, "生成它的用户已经不能下载这个文件了")
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.assertDownload(self.open())

    def test_rules_added_afterwards(self):
        self.hide("/docs/a.txt")  # only hidden: still fine
        self.assertDownload(self.open())
        DownloadLog.objects.all().delete()
        rule = self.hide("/docs/*.txt", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertRefused(self.open(), 403, "这个文件不能下载")
        rule.delete()
        self.hide("/docs", mode=FilterMode.INACCESSIBLE)
        self.assertRefused(self.open(), 403, "这个位置不能访问")
        # The creator's 忽略隐藏 doesn't count for links.
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        self.assertRefused(self.open(), 403, "这个位置不能访问")
        self.assertFalse(DownloadLog.objects.exists())

    def test_a_password_folder_after_the_link(self):
        self.lock("/docs")
        self.assertRefused(self.open(), 403, "生成短链时还没有输入过")
        # The visitor's own unlocking doesn't count either: the creator's keys do.
        self.visitor_client.force_login(self.alice)
        session = self.visitor_client.session
        session.save()
        self.assertRefused(self.open(), 403, "生成短链时还没有输入过")

    def test_the_keys_kept(self):
        rule = self.lock("/docs")
        self.unlock("/docs")
        link = self.short(path="/docs/b.txt")
        self.assertDownload(self.open(link.key), "/docs/b.txt")  # a visitor who never typed the password
        rule.password, rule.revision = "pw2", rule.revision + 1
        rule.save()
        self.assertRefused(self.open(link.key), 403, "生成短链时还没有输入过")
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])
        self.assertDownload(self.open(link.key), "/docs/b.txt")

    def test_storage_disabled_or_gone(self):
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        self.assertRefused(self.open(), 403, "这个存储源已停用")
        self.storage.delete()
        self.assertFalse(ShortLink.objects.exists())  # CASCADE
        self.assertRefused(self.open(), 404, "短链不存在")

    def test_the_creator_deleted(self):
        self.alice.delete()
        self.assertFalse(ShortLink.objects.exists())
        self.assertRefused(self.open(), 404, "短链不存在")

    def test_only_get_and_head(self):
        self.assertEqual(APIClient().post(f"/s/{self.link.key}").status_code, 405)
        response = self.visitor_client.head(f"/s/{self.link.key}")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(DownloadLog.objects.count(), 1)  # the answer hands out the download URL: logged


@mysql_only
class MySqlKeyTests(LinksTestCase):
    """The binary collation of the key columns (apps.links.models.KeyField)."""

    def test_two_keys_that_differ_in_case(self):
        one = ShortLink.objects.create(key="aB3x9Z", storage=self.storage, path="/docs/a.txt", created_by=self.alice)
        two = ShortLink.objects.create(key="Ab3X9z", storage=self.storage, path="/docs/b.txt", created_by=self.alice)
        self.assertEqual(ShortLink.objects.get(key="aB3x9Z"), one)
        self.assertEqual(ShortLink.objects.get(key="Ab3X9z"), two)
        self.assertFalse(ShortLink.objects.filter(key="ab3x9z").exists())
        DownloadLog.objects.create(kind="short", storage=self.storage, path="/docs/b.txt", short_key="Ab3X9z")
        self.assertFalse(DownloadLog.objects.filter(short_key="aB3x9Z").exists())

    def test_opening_matches_the_case(self):
        ShortLink.objects.create(key="aB3x9Z", storage=self.storage, path="/docs/a.txt", created_by=self.alice)
        self.assertEqual(self.visitor().get("/s/aB3x9Z").status_code, 302)
        self.assertEqual(self.visitor().get("/s/AB3X9Z").status_code, 404)
