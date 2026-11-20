"""apps.files.signing (W2): what a /pd/ link's signature binds, and what breaks it."""

import time
import urllib.parse

from django.http import QueryDict
from django.test import TestCase, override_settings

from apps.files import signing
from apps.storages.models import StorageSource

from .helpers import NOT_DISK


def parts(url):
    split = urllib.parse.urlsplit(url)
    return split.path, QueryDict(split.query)


class SigningTests(TestCase):
    def setUp(self):
        self.storage = StorageSource.objects.create(key="disk", name="盘", type="local", config={})
        self.other = StorageSource.objects.create(key="other", name="另一个", type="local", config={})

    def verify(self, url, storage=None, path=None, **kwargs):
        link_path, query = parts(url)
        key, storage_path, _ = signing.parse_url(url)
        return signing.verify(storage or StorageSource.objects.get(key=key), path or storage_path, query, **kwargs)

    def test_a_link_and_what_it_grants(self):
        url = signing.sign(self.storage, "/docs/报告 1.pdf", lifetime=600, inline=True)
        path, query = parts(url)
        self.assertEqual(path, "/pd/disk/docs/%E6%8A%A5%E5%91%8A%201.pdf")
        self.assertEqual(sorted(query), ["d", "e", "sign"])
        link = self.verify(url)
        self.assertEqual((link.storage, link.path, link.inline, link.redirect, link.ignore_hidden, link.name),
                         (self.storage, "/docs/报告 1.pdf", True, False, False, "报告 1.pdf"))
        self.assertGreaterEqual(link.expires, time.time() + 600)
        self.assertLessEqual(link.expires, time.time() + 600 + 100)  # rounded up to a step (a sixth)
        self.assertEqual(signing.verify_url(url).path, "/docs/报告 1.pdf")
        self.assertEqual(signing.verify_url("https://files.example.com" + url).path, "/docs/报告 1.pdf")

    def test_the_same_file_gets_the_same_link_for_a_while(self):
        self.assertEqual(signing.sign(self.storage, "/a.png", lifetime=1800), signing.sign(self.storage, "/a.png", lifetime=1800))

    def test_tampered_links(self):
        url = signing.sign(self.storage, "/docs/a.txt", lifetime=600)
        path, query = parts(url)
        signature = query["sign"]
        tampered = {
            "another file": url.replace("/docs/a.txt", "/docs/b.txt"),
            "another folder": url.replace("/docs/a.txt", "/secret/a.txt"),
            "another storage": url.replace("/pd/disk/", "/pd/other/"),
            "a later expiry": url.replace(f"e={query['e']}", f"e={int(query['e']) + 3600}"),
            "no expiry": url.replace(f"e={query['e']}&", ""),
            "inline added": url + "&d=i",
            "redirect added": url + "&r=1",
            "filters skipped": url + "&h=1",
            "another name": url + "&n=x.exe",
            "a signature changed": url.replace(signature, signature[:-2] + ("AA" if signature[-2:] != "AA" else "BB")),
            "no signature": url.split("&sign=")[0],
            "the signature twice": url + "&sign=" + signature,
        }
        for what, bad in tampered.items():
            with self.subTest(what):
                with self.assertRaises(signing.BadLink) as caught:
                    self.verify(bad)
                self.assertEqual(caught.exception.code, signing.INVALID)
        # Other parameters a client adds don't matter.
        self.assertEqual(self.verify(url + "&v=1").path, "/docs/a.txt")

    def test_expired(self):
        url = signing.sign(self.storage, "/a.txt", lifetime=60)
        link = self.verify(url)
        with self.assertRaises(signing.BadLink) as caught:
            self.verify(url, now=link.expires + 1)
        self.assertEqual(caught.exception.code, signing.EXPIRED)
        self.assertIn("过期", caught.exception.message)

    def test_permanent_links_end_when_the_secret_is_rotated(self):
        url = signing.sign(self.storage, "/a.txt", lifetime=None)
        self.assertNotIn("e=", url)
        self.assertIsNone(self.verify(url, now=time.time() + 10 * 365 * 86400).expires)
        expiring = signing.sign(self.storage, "/a.txt", lifetime=600)
        StorageSource.objects.filter(pk=self.storage.pk).update(link_secret="rotated-secret")
        for old in (url, expiring):
            with self.assertRaises(signing.BadLink):
                self.verify(old)

    def test_a_new_secret_key_ends_them_and_a_fallback_keeps_them(self):
        url = signing.sign(self.storage, "/a.txt", lifetime=600)
        with override_settings(SECRET_KEY="another-secret-key-" * 3):
            with self.assertRaises(signing.BadLink):
                self.verify(url)
        from django.conf import settings

        with override_settings(SECRET_KEY="another-secret-key-" * 3, SECRET_KEY_FALLBACKS=[settings.SECRET_KEY]):
            self.assertEqual(self.verify(url).path, "/a.txt")

    def test_not_after_caps_the_expiry(self):
        """A share's links (apps.shares.service.share_link): the expiry, rounded up to its step, never past a
        given moment; without one nothing changes."""
        now = time.time()
        plain = self.verify(signing.sign(self.storage, "/a.txt", lifetime=600))
        self.assertGreaterEqual(plain.expires, now + 600)
        capped = self.verify(signing.sign(self.storage, "/a.txt", lifetime=600, not_after=int(now) + 100))
        self.assertEqual(capped.expires, int(now) + 100)
        later = self.verify(signing.sign(self.storage, "/a.txt", lifetime=600, not_after=int(now) + 100000))
        self.assertEqual(later.expires, plain.expires)  # a later bound changes nothing
        permanent = self.verify(signing.sign(self.storage, "/a.txt", lifetime=None, not_after=int(now) + 100))
        self.assertEqual(permanent.expires, int(now) + 100)

    def test_the_other_flags(self):
        url = signing.sign(self.storage, "/a.txt", lifetime=600, inline=False, redirect=True, ignore_hidden=True,
                           filename="下载.txt")
        link = self.verify(url)
        self.assertEqual((link.inline, link.redirect, link.ignore_hidden, link.filename, link.name),
                         (False, True, True, "下载.txt", "下载.txt"))
        # The path's own name isn't repeated.
        self.assertNotIn("n", parts(signing.sign(self.storage, "/a.txt", lifetime=600, filename="a.txt"))[1])

    def test_odd_input(self):
        for bad in ("/pd/disk", "/pd/", "/elsewhere/disk/a.txt", "/pd/disk/a.txt?e=12x&sign=x", "/pd/disk//a.txt?sign=x",
                    "/pd/disk/../a.txt?sign=x", "/pd/nope/a.txt?sign=x", "/pd/disk/a.txt?e=" + "9" * 30 + "&sign=x"):
            with self.subTest(bad):
                with self.assertRaises(signing.BadLink):
                    signing.verify_url(bad)
        good = signing.sign(self.storage, "/a.txt", lifetime=600)
        self.assertEqual(signing.verify_url(good.replace("/pd/disk/", "/pd/DISK/")).storage, self.storage)
        for key in NOT_DISK:  # API-16
            with self.subTest(key=key), self.assertRaises(signing.BadLink):
                signing.verify_url(good.replace("/pd/disk/", f"/pd/{urllib.parse.quote(key)}/"))
        with self.assertRaises(ValueError):
            signing.sign(self.storage, "/", lifetime=60)
        with self.assertRaises(ValueError):
            signing.sign(self.storage, "relative.txt", lifetime=60)
        with self.assertRaises(ValueError):
            signing.sign(self.storage, "/a.txt", lifetime=0)
