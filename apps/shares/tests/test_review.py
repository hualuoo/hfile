"""What the independent review found (2026-11-08), each kept from coming back: links that outlive the
share, a backend request per hit, folder passwords tried from many addresses, entries shown after the
owner lost the share or a rule hid them, a shared folder turned file, the cache, big bodies."""

from __future__ import annotations

import datetime
import json
import time
import urllib.parse
from unittest import mock

from django.utils import timezone

from apps.accounts.permissions import Permission
from apps.files import transfer
from apps.files.tests import drivers as test_drivers
from apps.shares import access as shares
from apps.shares.models import FILE, FOLDER

from .helpers import API, EVERYTHING, SharesTestCase


def expiry_of(url):
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    return int(query["e"][0]) if "e" in query else None


class LinkTests(SharesTestCase):
    def test_never_a_permanent_link(self):
        self.storage.permanent_links = True
        self.storage.save()
        share = self.files_share()
        for what in ("download", "preview"):
            url = self.visitor.get(f"/share/{share.key}/{what}", {"path": "/top.txt"})["Location"]
            self.assertTrue(url.startswith("/pd/disk/top.txt?"), url)
            self.assertIsNotNone(expiry_of(url), url)
            self.assertLessEqual(expiry_of(url), time.time() + 3600 + 300)
        inline = self.vget(share, "item", path="/top.txt").json()["inline_url"]
        self.assertLessEqual(expiry_of(inline), time.time() + 3600 + 300)

    def test_not_past_the_shares_expiry(self):
        """Signing rounds expiries up to a step (up to 5 minutes); a share's links still end with it."""
        for seconds in (10, 61, 599, 7200):
            with self.subTest(seconds=seconds):
                ends = timezone.now() + datetime.timedelta(seconds=seconds)
                share = self.files_share(expires_at=ends)
                download = self.visitor.get(f"/share/{share.key}/download", {"path": "/top.txt"})["Location"]
                preview = self.visitor.get(f"/share/{share.key}/preview", {"path": "/top.txt"},
                                           HTTP_SEC_FETCH_DEST="empty")["Location"]
                inline = self.vget(share, "item", path="/top.txt").json()["inline_url"]
                for url in (download, preview, inline):
                    self.assertLessEqual(expiry_of(url), int(ends.timestamp()), url)
                    self.assertGreater(expiry_of(url), time.time())  # still usable now

    def test_a_buckets_presigned_link_ends_with_the_share(self):
        bucket = self.make_storage("direct", "test-direct", self.root)  # files' bucket-like local variant
        self.grant(self.alice, EVERYTHING, storage=bucket)
        share = self.share((("docs", FOLDER),), storage=bucket,
                           expires_at=timezone.now() + datetime.timedelta(seconds=90))
        test_drivers.DirectDriver.calls.clear()
        url = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"})["Location"]
        self.assertTrue(url.startswith("https://cdn.example.test/docs/a.txt?expires="), url)
        self.assertLessEqual(int(url.split("expires=")[1].split("&")[0]), 90)

    def test_deferred_storages_get_a_redirecting_link(self):
        """OneDrive, SharePoint, 115: no request to the backend per hit; /pd/ asks it (cached, budgeted)."""
        bucket = self.make_storage("cloud", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        share = self.share((("docs", FOLDER),), storage=bucket)
        test_drivers.DirectDriver.calls.clear()
        with mock.patch.object(transfer, "DEFERRED", frozenset({*transfer.DEFERRED, "test-direct"})):
            url = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"})["Location"]
        self.assertTrue(url.startswith("/pd/cloud/docs/a.txt?"), url)
        self.assertIn("r=1", url)
        self.assertEqual(test_drivers.DirectDriver.calls, [])


class UnlockTests(SharesTestCase):
    def test_folder_passwords_per_share(self):
        share = self.files_share()
        self.lock("/docs", password="pw1")
        for number in range(shares.UNLOCK_FAILURES):
            response = self.vpost(share, "unlock", {"path": "/docs", "password": "nope"}, REMOTE_ADDR=f"198.51.100.{number + 1}")
            self.assertEqual(response.status_code, 403)
        throttled = self.vpost(share, "unlock", {"path": "/docs", "password": "pw1"}, REMOTE_ADDR="192.0.2.99")
        self.assertEqual((throttled.status_code, throttled.json()["code"]), (429, "password_throttled"))

    def test_rule_ids_that_arent_there_count(self):
        share = self.files_share()
        for number in range(shares.UNLOCK_FAILURES):
            response = self.vpost(share, "unlock", {"rule": 1000 + number, "password": "x"}, REMOTE_ADDR=f"198.51.100.{number + 1}")
            self.assertEqual(response.status_code, 404)
        self.assertEqual(self.vpost(share, "unlock", {"rule": 1, "password": "x"}, REMOTE_ADDR="192.0.2.99").status_code, 429)

    def test_an_inaccessible_folder_isnt_tried(self):
        share = self.files_share()
        self.lock("/docs/sub", password="pw1")
        self.hide("/docs/sub", mode="inaccessible")
        response = self.vpost(share, "unlock", {"path": "/docs/sub", "password": "pw1"})
        self.assertEqual((response.status_code, response.json()["code"]), (403, "inaccessible"))


class InfoTests(SharesTestCase):
    def test_entries_as_the_visitor_may_see_them_now(self):
        share = self.files_share()
        self.hide("/top.txt")
        self.assertEqual(self.vget(share).json()["entries"], [{"name": "docs", "type": "folder"}])

    def test_nothing_more_once_the_owner_cant_share(self):
        share = self.files_share()
        self.alice.is_active = False
        self.alice.save()
        data = self.vget(share).json()
        self.assertNotIn("entries", data)
        self.assertNotIn("storage", data)
        page = self.visitor.get(f"/share/{share.key}")
        self.assertEqual(page.status_code, 403)
        self.assertIn("分享暂时不能访问", page.content.decode())
        self.assertNotIn("top.txt", page.content.decode())

    def test_a_shared_folder_that_became_a_file(self):
        share = self.share((("top.txt", FOLDER),))  # top.txt is a file in the storage
        response = self.vget(share, "item", path="/top.txt")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "not_found"))
        self.assertEqual(self.vlist(share).json()["items"], [])

    def test_answers_are_not_cached(self):
        share = self.files_share()
        for response in (self.vget(share), self.vlist(share), self.vget(share, "item", path="/top.txt")):
            self.assertIn("no-store", response["Cache-Control"])


class BodyTests(SharesTestCase):
    def test_big_bodies_are_refused_before_reading(self):
        share = self.files_share(password="abcd")
        self.vcsrf()
        big = json.dumps({"password": "x" * 20000})
        for key, what in ((share.key, "verify"), (share.key, "unlock"), ("nokey123", "verify")):
            response = self.visitor.post(f"{API}{key}/{what}/", big, content_type="application/json")
            self.assertEqual(response.status_code, 413, (key, what))


class TypesTests(SharesTestCase):
    def test_the_owners_permission_map_at_item_level(self):
        """The share's permissions come from the owner's grant, never more than preview/download/copy."""
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        share = self.share((("top.txt", FILE),))
        data = self.vlist(share).json()
        self.assertEqual({code for code, on in data["permissions"].items() if on}, {"preview", "download", "copy_download_link"})
