"""A visitor before and after the password: what the info says, verify/, the throttle, the share's cookie
(inventory 10.3, decisions 10.Z.1, 10.Z.2, 10.Z.4)."""

from __future__ import annotations

import datetime
import time
from unittest import mock

from django.core import signing
from django.utils import timezone

from apps.shares import access as shares
from apps.shares.models import Share

from .helpers import SharesTestCase


def at(moment):
    """The clock at `moment` (time.time(): the cookie's own times and its signature's)."""
    return mock.patch("time.time", return_value=moment)


class InfoTests(SharesTestCase):
    def test_before_the_password_only_what_the_form_needs(self):
        self.alice.nickname = "爱丽丝"
        self.alice.save()
        later = timezone.now() + datetime.timedelta(days=2)
        share = self.files_share(password="abcd", expires_at=later)
        response = self.vget(share)
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(set(data), {"key", "need_password", "verified", "expired", "expires_at", "entry_count", "owner"})
        self.assertEqual((data["need_password"], data["verified"], data["expired"], data["entry_count"], data["owner"]),
                         (True, False, False, 2, "爱丽丝"))
        text = response.content.decode()
        for secret in ("docs", "top.txt", "disk", "alice", "abcd", share.folder + "docs"):
            self.assertNotIn(secret, text)
        # The listing, an item and the downloads are refused too.
        for what, params in (("list", {"path": "/"}), ("item", {"path": "/top.txt"})):
            refused = self.vget(share, what, **params)
            self.assertEqual((refused.status_code, refused.json()["code"]), (403, "share_password_required"))
            self.assertNotIn("docs", refused.content.decode())
        download = self.visitor.get(f"/share/{share.key}/download", {"path": "/docs/a.txt"})
        self.assertRedirects(download, f"/share/{share.key}?path=%2Fdocs", fetch_redirect_response=False)
        self.assertEqual(self.reload(share).download_count, 0)

    def test_after_the_password_the_entries(self):
        share = self.files_share(password="abcd")
        self.assertEqual(self.verify(share, "abcd").status_code, 200)
        data = self.vget(share).json()
        self.assertTrue(data["verified"])
        self.assertEqual(data["entries"], [{"name": "docs", "type": "folder"}, {"name": "top.txt", "type": "file"}])
        self.assertEqual(data["storage"]["key"], "disk")
        self.assertNotIn("folder", data)  # the owner's path is nobody else's business

    def test_without_a_password(self):
        share = self.files_share()
        data = self.vget(share).json()
        self.assertEqual((data["need_password"], data["verified"]), (False, True))
        self.assertIn("entries", data)
        self.assertEqual(self.verify(share, "whatever").status_code, 200)  # nothing to check

    def test_unknown_and_expired(self):
        response = self.vget("nope1234")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "share_not_found"))
        self.assertEqual(self.vget("../x").status_code, 404)
        share = self.files_share(expires_at=timezone.now() - datetime.timedelta(seconds=1))
        response = self.vget(share)
        self.assertEqual((response.status_code, response.json()["code"]), (404, "share_expired"))
        self.assertIn("expires_at", response.json())
        self.assertEqual(self.vlist(share).json()["code"], "share_expired")
        self.assertEqual(self.visitor.get(f"/share/{share.key}/download", {"path": "/top.txt"}).status_code, 404)

    def test_no_owner_nickname(self):
        share = self.files_share()
        self.assertIsNone(self.vget(share).json()["owner"])  # never the username


class PasswordTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.share_ = self.files_share(password="abcd")

    def cookie(self, share=None, client=None):
        share = share or self.share_
        client = client or self.visitor
        return client.cookies.get(f"hfile_share_{share.pk}")

    def test_right_and_wrong(self):
        wrong = self.verify(self.share_, "abce")
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (403, "share_password_wrong"))
        self.assertEqual(wrong.json()["detail"], "密码错误，请重新输入。")
        self.assertIsNone(self.cookie())
        empty = self.verify(self.share_, "  ")
        self.assertEqual(empty.status_code, 400)
        right = self.verify(self.share_, " abcd ")  # spaces at either end don't count
        self.assertEqual(right.status_code, 200)
        cookie = self.cookie()
        self.assertTrue(cookie["httponly"])
        self.assertEqual((cookie["samesite"], cookie["path"], cookie["max-age"]), ("Lax", "/", shares.IDLE))
        self.assertNotIn("abcd", cookie.value)
        self.assertEqual(self.vlist(self.share_).status_code, 200)

    def test_csrf(self):
        from rest_framework.test import APIClient

        client = APIClient(enforce_csrf_checks=True)
        response = client.post(f"/api/v1/shares/{self.share_.key}/verify/", {"password": "abcd"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (403, "csrf_failed"))

    def test_throttled_per_address(self):
        for _ in range(shares.ADDRESS_FAILURES):
            self.assertEqual(self.verify(self.share_, "nope", REMOTE_ADDR="198.51.100.7").status_code, 403)
        throttled = self.verify(self.share_, "abcd", REMOTE_ADDR="198.51.100.7")  # the right one too
        self.assertEqual((throttled.status_code, throttled.json()["code"]), (429, "share_password_throttled"))
        self.assertGreater(throttled.json()["wait"], 0)
        self.assertIsNone(self.cookie())
        # Another address may still try; a minute later this one may too.
        self.assertEqual(self.verify(self.share_, "abcd", REMOTE_ADDR="198.51.100.8").status_code, 200)
        with at(time.time() + shares.ADDRESS_WINDOW + 1):
            self.assertEqual(self.verify(self.share_, "abcd", REMOTE_ADDR="198.51.100.7").status_code, 200)

    def test_an_ipv6_network_counts_as_one_address(self):
        for number in range(shares.ADDRESS_FAILURES):
            self.assertEqual(self.verify(self.share_, "nope", REMOTE_ADDR=f"2001:db8:1:2::{number + 1:x}").status_code, 403)
        throttled = self.verify(self.share_, "abcd", REMOTE_ADDR="2001:db8:1:2:ffff::1")  # the same /64
        self.assertEqual(throttled.status_code, 429)
        self.assertEqual(self.verify(self.share_, "abcd", REMOTE_ADDR="2001:db8:1:3::1").status_code, 200)

    def test_throttled_per_share(self):
        for number in range(shares.SHARE_FAILURES):
            self.assertEqual(self.verify(self.share_, "nope", REMOTE_ADDR=f"203.0.113.{number + 1}").status_code, 403)
        throttled = self.verify(self.share_, "abcd", REMOTE_ADDR="192.0.2.200")
        self.assertEqual((throttled.status_code, throttled.json()["code"]), (429, "share_password_throttled"))
        other = self.share(password="abcd")
        self.assertEqual(self.verify(other, "abcd", REMOTE_ADDR="192.0.2.200").status_code, 200)

    def test_the_cookie_is_bound_to_the_share(self):
        self.verify(self.share_, "abcd")
        other = self.files_share(password="abcd")
        # This share's cookie under the other one's name: another salt, it doesn't verify.
        self.visitor.cookies[f"hfile_share_{other.pk}"] = self.cookie().value
        self.assertEqual(self.vlist(other).json()["code"], "share_password_required")
        self.assertEqual(self.vlist(self.share_).status_code, 200)

    def test_and_to_its_revision_and_nonce(self):
        self.verify(self.share_, "abcd")
        Share.objects.filter(pk=self.share_.pk).update(revision=2)  # a new password would do this
        self.assertEqual(self.vlist(self.share_).json()["code"], "share_password_required")
        self.verify(self.share_, "abcd")
        self.assertEqual(self.vlist(self.share_).status_code, 200)
        Share.objects.filter(pk=self.share_.pk).update(nonce="another")
        self.assertEqual(self.vlist(self.share_).json()["code"], "share_password_required")

    def test_a_forged_cookie(self):
        forged = signing.dumps({"t": int(time.time()), "v": int(time.time()), "u": []}, salt="something else")
        self.visitor.cookies[f"hfile_share_{self.share_.pk}"] = forged
        response = self.vlist(self.share_)
        self.assertEqual(response.json()["code"], "share_password_required")
        self.assertEqual(response.cookies[f"hfile_share_{self.share_.pk}"].value, "")  # taken away

    def test_idle_and_age(self):
        self.verify(self.share_, "abcd")
        start = time.time()
        # Used within the idle time it is renewed (sliding), so it lasts while it is used.
        for step in range(1, 4):
            with at(start + step * (shares.IDLE - 60)):
                self.assertEqual(self.vlist(self.share_).status_code, 200, step)
        # Not used for longer than the idle time: over.
        with at(start + 3 * (shares.IDLE - 60) + shares.IDLE + 1):
            self.assertEqual(self.vlist(self.share_).json()["code"], "share_password_required")

    def test_asked_again_after_a_week(self):
        self.verify(self.share_, "abcd")
        start = time.time()
        step = shares.IDLE - 60
        moment = start
        while moment + step <= start + shares.VERIFIED_FOR - 5:  # busy all week: renewed each time
            moment += step
            with at(moment):
                self.assertEqual(self.vlist(self.share_).status_code, 200)
        with at(start + shares.VERIFIED_FOR + 1):
            self.assertEqual(self.vlist(self.share_).json()["code"], "share_password_required")

    def test_pwd_in_the_address_is_never_checked(self):
        page = self.visitor.get(f"/share/{self.share_.key}", {"pwd": "abcd"})
        self.assertEqual(page.status_code, 200)
        self.assertIsNone(self.cookie())
        self.assertIn('id="share-password-form"', page.content.decode())
        refused = self.visitor.get(f"/api/v1/shares/{self.share_.key}/list/", {"path": "/", "pwd": "abcd", "password": "abcd"})
        self.assertEqual(refused.json()["code"], "share_password_required")

    def test_an_unreadable_password(self):
        Share.objects.filter(pk=self.share_.pk).update(password="enc:not-a-token")
        response = self.verify(self.share_, "abcd")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "share_password_unreadable"))
