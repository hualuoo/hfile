"""页面下载 (D54): a download from the 下载 button of the site's pages (dl=1 on a /pd/ link) goes into the
download log, with who was logged in. The rest of what /pd/ answers (the gallery, the players, copied links,
resumed downloads, a download manager's later parts, HEAD) doesn't."""

from __future__ import annotations

from unittest import mock

from django.db import DatabaseError

from apps.files import download, signing

from .. import delivery
from ..models import DownloadLog
from .helpers import LinksTestCase


class PageDownloadTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.plain = signing.sign(self.storage, "/docs/a.txt", lifetime=600)
        self.url = self.plain + "&dl=1"

    def fetch(self, url, **headers):
        response = self.client.get(url, **headers)
        body = b"".join(response.streaming_content) if response.streaming else response.content
        return response, body

    def test_a_click_is_logged_with_who_was_logged_in(self):
        self.login()
        response, body = self.fetch(self.url, HTTP_USER_AGENT="Mozilla/5.0 test", REMOTE_ADDR="203.0.113.9")
        self.assertEqual((response.status_code, body), (200, b"hello"))
        self.assertEqual(response["Cache-Control"], "private, no-store")  # the next click comes here again
        log = DownloadLog.objects.get()
        self.assertEqual((log.kind, log.storage, log.path, log.short_key, log.username),
                         ("page", self.storage, "/docs/a.txt", "", "alice"))
        self.assertEqual((log.ip, log.user_agent, log.referer), ("203.0.113.9", "Mozilla/5.0 test", ""))

    def test_a_visitor_has_no_name(self):
        response, body = self.fetch(self.url)
        self.assertEqual((response.status_code, body), (200, b"hello"))
        self.assertEqual(DownloadLog.objects.get().username, "")

    def test_only_a_download_from_the_button_as_it_starts(self):
        response, _ = self.fetch(self.plain)  # the gallery, a player, a copied link
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response["Cache-Control"], "private, no-store")
        self.assertEqual(self.client.head(self.url).status_code, 200)
        for later in ("bytes=2-", "bytes=1-3", "bytes=-2", "bytes=0-1,3-4", "bytes=x"):
            self.fetch(self.url, HTTP_RANGE=later)  # resumed, a later part, the end, several, nonsense
        self.assertFalse(DownloadLog.objects.exists())
        for start in ("bytes=0-", "bytes=00-2"):
            response, body = self.fetch(self.url, HTTP_RANGE=start)
            self.assertEqual(response.status_code, 206)
        self.assertEqual(DownloadLog.objects.count(), 2)

    def test_not_for_refusals_and_failures_or_when_off(self):
        missing = signing.sign(self.storage, "/docs/missing.txt", lifetime=600) + "&dl=1"
        self.assertEqual(self.fetch(missing)[0].status_code, 404)
        forged = signing.sign(self.storage, "/docs/b.txt", lifetime=600).replace("sign=", "sign=x") + "&dl=1"
        self.assertEqual(self.fetch(forged)[0].status_code, 403)
        rule = self.hide("/docs", "inaccessible")
        self.assertEqual(self.fetch(self.url)[0].status_code, 403)
        self.assertFalse(DownloadLog.objects.exists())
        rule.delete()
        self.site(record_download_log=False)
        self.assertEqual(self.fetch(self.url)[0].status_code, 200)
        self.assertFalse(DownloadLog.objects.exists())

    def test_a_failing_log_doesnt_stop_the_download(self):
        with mock.patch.object(DownloadLog.objects, "create", side_effect=DatabaseError("x")), \
                self.assertLogs("hfile.links", "WARNING"):
            response, body = self.fetch(self.url)
        self.assertEqual((response.status_code, body), (200, b"hello"))
        with mock.patch.object(delivery, "record", side_effect=RuntimeError("a bug")), \
                self.assertLogs("django.dispatch", "ERROR"):
            response, body = self.fetch(self.url)
        self.assertEqual((response.status_code, body), (200, b"hello"))
        self.assertFalse(DownloadLog.objects.exists())

    def test_a_redirect_to_the_backends_link(self):
        bucket = self.make_storage("bucket", "test-direct", self.root)
        url = signing.sign(bucket, "/docs/a.txt", lifetime=600, redirect=True) + "&dl=1"
        response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://cdn.example.test/docs/a.txt?"))
        self.assertEqual((DownloadLog.objects.get().kind, DownloadLog.objects.get().storage), ("page", bucket))
        self.client.head(url)
        self.assertEqual(DownloadLog.objects.count(), 1)

    def test_a_path_link_is_logged_once(self):
        self.login()
        response = self.client.get(self.path_link("/docs/a.txt"))
        self.assertEqual(response.status_code, 302)
        target, body = self.fetch(response["Location"])  # the /pd/ link it leads to: no dl=1
        self.assertEqual((target.status_code, body), (200, b"hello"))
        self.assertEqual(list(DownloadLog.objects.values_list("kind", "username")), [("path", "alice")])

    def test_the_signal(self):
        heard = []

        def listener(sender, **kwargs):
            heard.append((kwargs["storage"].key, kwargs["path"], kwargs["request"].method))

        download.download_clicked.connect(listener)
        self.addCleanup(download.download_clicked.disconnect, listener)
        self.fetch(self.url)
        self.fetch(self.plain)
        self.assertEqual(heard, [("disk", "/docs/a.txt", "GET")])
