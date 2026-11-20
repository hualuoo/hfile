"""The admin APIs of 短链管理 and 下载日志 (inventory 9.9-9.11, D40), and `hfile cleanup`."""

from __future__ import annotations

import csv
import datetime
import io

from django.core.management import call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.core.testing import mysql_only

from .. import ratelimit
from ..models import DownloadLog, ShortLink
from .helpers import LinksTestCase

API = "/api/v1/manage/"
ENDPOINTS = [
    ("get", "short-links/"), ("delete", "short-links/1/"), ("post", "short-links/delete/"),
    ("post", "short-links/delete-expired/"), ("get", "short-links/export/"), ("get", "link-limits/"),
    ("get", "download-logs/"), ("delete", "download-logs/"), ("post", "download-logs/delete/"),
]


def days_ago(days):
    return timezone.now() - datetime.timedelta(days=days)


class ManageTestCase(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_user("root", password="pw-root-2026x", is_staff=True)
        self.client.force_login(self.admin)
        self.csrf()
        self.other = self.make_storage("other", "local", self.root)

    def api(self, method, endpoint, data=None, **params):
        url = f"{API}{endpoint}"
        if params:
            from urllib.parse import urlencode

            url += "?" + urlencode(params)
        return getattr(self.client, method)(url, data, format="json")

    def log(self, kind="path", storage=None, path="/docs/a.txt", short_key="", **fields):
        return DownloadLog.objects.create(kind=kind, storage=storage or self.storage, path=path, short_key=short_key,
                                          **fields)


class AccessTests(ManageTestCase):
    def test_admins_only(self):
        visitor = self.visitor()
        for method, path in ENDPOINTS:
            self.assertEqual(getattr(visitor, method)(f"{API}{path}", {}, format="json").status_code, 401, path)
        self.client.force_login(self.alice)
        self.csrf()
        for method, path in ENDPOINTS:
            self.assertEqual(self.api(method, path, {}).status_code, 403, path)


class ShortLinkAdminTests(ManageTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.short_link_of(self.alice, "/docs/a.txt", key="Alpha1", created_at=days_ago(3))
        self.b = self.short_link_of(self.alice, "/docs/sub/c.txt", key="beta22", storage=self.other,
                                    created_at=days_ago(1), expires_at=days_ago(-1))
        self.c = self.short_link_of(self.guest, "/top.txt", key="Gamma3", expires_at=days_ago(0.5))

    def keys(self, **params):
        response = self.api("get", "short-links/", **params)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["key"] for row in response.json()["results"]]

    def test_the_list(self):
        data = self.api("get", "short-links/").json()
        self.assertEqual(data["count"], 3)
        first = data["results"][0]
        self.assertEqual(first["key"], "Gamma3")  # newest first
        self.assertEqual(first["url"], "http://testserver/s/Gamma3")
        self.assertEqual(first["storage"]["key"], "disk")
        self.assertEqual(first["created_by"]["username"], self.guest.username)
        self.assertTrue(first["expired"])
        self.assertFalse(data["results"][1]["expired"])

    def test_filters(self):
        self.assertEqual(self.keys(storage=self.other.pk), ["beta22"])
        self.assertEqual(self.keys(path="SUB"), ["beta22"])
        self.assertEqual(self.keys(path="%"), [])  # LIKE's wildcards are plain characters
        self.assertEqual(self.keys(key="ALPHA"), ["Alpha1"])  # any case
        today = timezone.localdate()
        self.assertEqual(self.keys(since=str(today - datetime.timedelta(days=2))), ["Gamma3", "beta22"])
        self.assertEqual(self.keys(until=str(today - datetime.timedelta(days=2))), ["Alpha1"])
        self.assertEqual(self.api("get", "short-links/", since="2026-1-1").status_code, 400)
        self.assertEqual(self.api("get", "short-links/", since=str(today), until=str(today - datetime.timedelta(days=1))).status_code, 400)

    def test_ordering(self):
        # By code points on both databases (SQLite's BINARY, MySQL's utf8mb4_bin for keys).
        self.assertEqual(self.keys(ordering="key"), ["Alpha1", "Gamma3", "beta22"])
        # The links that never expire last, or first when descending.
        self.assertEqual(self.keys(ordering="expires_at"), ["Gamma3", "beta22", "Alpha1"])
        self.assertEqual(self.keys(ordering="-expires_at"), ["Alpha1", "beta22", "Gamma3"])
        self.assertEqual(self.keys(ordering="created_at"), ["Alpha1", "beta22", "Gamma3"])
        response = self.api("get", "short-links/", ordering="path")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.api("get", "short-links/", ordering="created_by__password").status_code, 400)

    def test_pages(self):
        for n in range(25):
            self.short_link_of(self.alice, f"/f{n}.txt", key=f"Page{n:02d}")
        data = self.api("get", "short-links/", page_size=10, page=3).json()
        self.assertEqual((data["count"], len(data["results"])), (28, 8))
        self.assertEqual(self.api("get", "short-links/", page=9).status_code, 404)

    def test_deleting(self):
        self.log("short", short_key="Alpha1")
        self.assertEqual(self.api("delete", f"short-links/{self.a.pk}/").json(), {"deleted": 1})
        self.assertEqual(self.api("delete", f"short-links/{self.a.pk}/").status_code, 404)
        self.assertTrue(DownloadLog.objects.filter(short_key="Alpha1").exists())  # its log stays
        self.assertEqual(self.api("post", "short-links/delete/", {"ids": [self.b.pk, self.c.pk, 999999]}).json(),
                         {"deleted": 2})
        self.assertEqual(self.api("post", "short-links/delete/", {"ids": []}).status_code, 400)
        self.assertFalse(ShortLink.objects.exists())

    def test_deleting_the_expired_ones(self):
        self.log("short", short_key="Gamma3")
        self.log("short", short_key="Gamma3")
        self.log("short", short_key="beta22")
        self.log("path", path="/top.txt", short_key="")
        answer = self.api("post", "short-links/delete-expired/").json()
        self.assertEqual(answer, {"deleted": 1, "logs": 2})
        self.assertEqual(set(ShortLink.objects.values_list("key", flat=True)), {"Alpha1", "beta22"})
        self.assertEqual(DownloadLog.objects.count(), 2)

    def test_export(self):
        for n in range(60):  # more than a page
            self.short_link_of(self.alice, f"/f{n}.txt", key=f"Exp{n:03d}")
        self.short_link_of(self.alice, "/=cmd|' /C calc'!A0.txt", key="Formula")
        response = self.api("get", "short-links/export/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment", response["Content-Disposition"])
        body = b"".join(response.streaming_content)
        self.assertTrue(body.startswith("\ufeff".encode("utf-8")))
        rows = list(csv.reader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(rows[0][:4], ["ID", "存储源名称", "存储源类型", "短链 Key"])
        self.assertEqual(len(rows), 1 + 64)
        by_key = {row[3]: row for row in rows[1:]}
        self.assertEqual(by_key["Alpha1"][4], "http://testserver/s/Alpha1")
        self.assertEqual(by_key["Alpha1"][8], "永久")
        self.assertEqual(by_key["Formula"][5], "/=cmd|' /C calc'!A0.txt")  # a path starts with "/": safe as it is
        # The filters of the list.
        rows = list(csv.reader(io.StringIO(b"".join(self.api("get", "short-links/export/", key="exp00").streaming_content)
                                           .decode("utf-8-sig"))))
        self.assertEqual(sorted(row[3] for row in rows[1:]), [f"Exp00{n}" for n in range(10)])

    def test_formulas_in_names_are_defused(self):
        bob = User.objects.create_user("+bob", password="pw-bob-2026x")
        self.short_link_of(bob, "/top.txt", key="Bobby1")
        rows = list(csv.reader(io.StringIO(b"".join(self.api("get", "short-links/export/", key="bobby").streaming_content)
                                           .decode("utf-8-sig"))))
        self.assertEqual(rows[1][6], "'+bob")

    def test_rate_limit_statistics(self):
        self.site(link_limit_seconds=600, link_limit_downloads=5)
        ratelimit.limiter.hit("203.0.113.5", 600, 5)
        ratelimit.limiter.hit("203.0.113.5", 600, 5)
        ratelimit.limiter.hit("2001:db8::/64", 600, 5)
        data = self.api("get", "link-limits/").json()
        self.assertEqual((data["seconds"], data["downloads"], data["count"]), (600, 5, 2))
        self.assertEqual([(w["address"], w["count"]) for w in data["windows"]], [("203.0.113.5", 2), ("2001:db8::/64", 1)])
        started = datetime.datetime.fromisoformat(data["windows"][0]["started_at"])
        expires = datetime.datetime.fromisoformat(data["windows"][0]["expires_at"])
        self.assertAlmostEqual((expires - started).total_seconds(), 600, delta=1)
        self.site(link_limit_downloads=0)
        self.assertEqual(self.api("get", "link-limits/").json()["windows"], [])


class DownloadLogAdminTests(ManageTestCase):
    def setUp(self):
        super().setUp()
        self.one = self.log("path", path="/docs/a.txt", ip="203.0.113.5", user_agent="Mozilla/5.0 Firefox",
                            referer="https://example.com/a", created_at=days_ago(2))
        self.two = self.log("short", path="/docs/b.txt", short_key="Ab3X9z", ip="2001:db8::1", user_agent="curl/8",
                            referer="", storage=self.other)
        self.three = self.log("short", path="/top.txt", short_key="zz9Q", ip="198.51.100.4", user_agent="Wget")

    def ids(self, **params):
        response = self.api("get", "download-logs/", **params)
        self.assertEqual(response.status_code, 200, response.content)
        return [row["id"] for row in response.json()["results"]]

    def test_the_list(self):
        data = self.api("get", "download-logs/").json()
        self.assertEqual((data["count"], data["newest"]), (3, self.three.pk))
        rows = {row["id"]: row for row in data["results"]}
        self.assertEqual(rows[self.one.pk]["url"], "http://testserver/directlink/disk/docs/a.txt")
        self.assertEqual(rows[self.two.pk]["url"], "http://testserver/s/Ab3X9z")
        self.assertEqual(rows[self.two.pk]["kind_label"], "短链")
        self.assertEqual(rows[self.two.pk]["storage"]["key"], "other")
        self.site(direct_link_prefix="dl")  # the prefix of now
        rows = {row["id"]: row for row in self.api("get", "download-logs/").json()["results"]}
        self.assertEqual(rows[self.one.pk]["url"], "http://testserver/dl/disk/docs/a.txt")

    def test_filters(self):
        self.assertEqual(self.ids(storage=self.other.pk), [self.two.pk])
        self.assertEqual(self.ids(kind="short"), [self.three.pk, self.two.pk])
        self.assertEqual(self.ids(kind="path"), [self.one.pk])
        self.assertEqual(self.api("get", "download-logs/", kind="directLink").status_code, 400)
        self.assertEqual(self.ids(key="ab3x"), [self.two.pk])
        self.assertEqual(self.ids(path="DOCS"), [self.two.pk, self.one.pk])
        self.assertEqual(self.ids(ip="2001:DB8:"), [self.two.pk])
        self.assertEqual(self.ids(ip="203.0"), [self.one.pk])
        self.assertEqual(self.api("get", "download-logs/", ip="x").status_code, 400)
        self.assertEqual(self.ids(user_agent="firefox"), [self.one.pk])
        self.assertEqual(self.ids(referer="example.com"), [self.one.pk])
        today = timezone.localdate()
        self.assertEqual(self.ids(since=str(today)), [self.three.pk, self.two.pk])
        self.assertEqual(self.ids(until=str(today - datetime.timedelta(days=1))), [self.one.pk])

    def test_page_downloads_and_who(self):
        page = self.log("page", path="/docs/a.txt", username="Alice", ip="203.0.113.6")
        self.one.username = "bob"
        self.one.save()
        row = next(row for row in self.api("get", "download-logs/").json()["results"] if row["id"] == page.pk)
        self.assertEqual((row["kind"], row["kind_label"], row["username"], row["url"]), ("page", "页面下载", "Alice", None))
        self.assertEqual(self.ids(kind="page"), [page.pk])
        self.assertEqual(self.ids(user="ALI"), [page.pk])
        self.assertEqual(self.ids(user="b"), [self.one.pk])
        self.assertEqual(self.api("get", "download-logs/", user="x" * 151).status_code, 400)
        self.assertEqual(self.api("delete", "download-logs/", kind="page").json(), {"deleted": 1})

    def test_delete_selected(self):
        self.assertEqual(self.api("post", "download-logs/delete/", {"ids": [self.one.pk, self.three.pk]}).json(),
                         {"deleted": 2})
        self.assertEqual(list(DownloadLog.objects.values_list("pk", flat=True)), [self.two.pk])
        self.assertEqual(self.api("post", "download-logs/delete/", {"ids": list(range(1, 1002))}).status_code, 400)

    def test_delete_what_the_filters_show(self):
        listed = self.api("get", "download-logs/", kind="short").json()
        late = self.log("short", path="/late.txt", short_key="Late01")  # logged after the list was shown
        self.assertEqual(self.api("delete", "download-logs/", kind="short", newest=listed["newest"]).json(),
                         {"deleted": 2})  # the kind filter counts, the late one stays
        self.assertEqual(set(DownloadLog.objects.values_list("pk", flat=True)), {self.one.pk, late.pk})
        self.assertEqual(self.api("delete", "download-logs/", path="nothing-like-it").json(), {"deleted": 0})
        self.assertEqual(self.api("delete", "download-logs/").json(), {"deleted": 2})  # no filters: all

    def test_a_condition_that_isnt_read_deletes_nothing(self):
        """API-02: a parameter the filters don't know, or the conditions in a body, is a 400, not "all"."""
        before = DownloadLog.objects.count()
        for params in ({"knd": "short"}, {"kind[0]": "short"}, {"Kind": "short"}, {"kind": "short", "page": "1"}):
            with self.subTest(params=params):
                self.assertEqual(self.api("delete", "download-logs/", **params).status_code, 400)
        for body in ({"ids": [self.one.pk]}, {"kind": "path"}):
            with self.subTest(body=body):
                self.assertEqual(self.api("delete", "download-logs/", body).status_code, 400)
        self.assertEqual(DownloadLog.objects.count(), before)

    @mysql_only
    def test_a_key_that_differs_in_case(self):
        self.log("short", short_key="aB3x9Z")
        self.assertEqual(self.ids(key="AB3X9Z").__len__(), 2)  # the filter ignores case on purpose
        ShortLink.objects.create(key="aB3x9Z", storage=self.storage, path="/docs/a.txt", created_by=self.alice,
                                 expires_at=days_ago(1))
        self.api("post", "short-links/delete-expired/")
        self.assertEqual(list(DownloadLog.objects.filter(short_key__in=["Ab3X9z", "aB3x9Z"]).values_list("short_key", flat=True)),
                         ["Ab3X9z"])


class CleanupTests(ManageTestCase):
    def test_the_job(self):
        expired = self.short_link_of(self.alice, key="Old001", expires_at=days_ago(1))
        live = self.short_link_of(self.alice, key="New001", expires_at=days_ago(-1))
        self.short_link_of(self.alice, key="Ever01")
        self.log("short", short_key=expired.key)
        self.log("short", short_key=live.key)
        out = io.StringIO()
        call_command("cleanup", "--only", "short_links", "--dry-run", stdout=out)
        self.assertIn("过期的短链（连同它们的下载日志）：可以删除 1 个", out.getvalue())
        self.assertEqual(ShortLink.objects.count(), 3)
        out = io.StringIO()
        call_command("cleanup", "--only", "short_links", stdout=out)
        self.assertIn("删除了 1 个", out.getvalue())
        self.assertEqual(set(ShortLink.objects.values_list("key", flat=True)), {"New001", "Ever01"})
        self.assertEqual(list(DownloadLog.objects.values_list("short_key", flat=True)), ["New001"])
