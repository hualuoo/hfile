"""/pd/ (apps/files/download.py): signatures, Range, disposition, types and the sandbox, the rules checked
again when a link is opened, redirects, and streams closed when the client goes away."""

import io
import os
import time
import unittest
import urllib.parse
from unittest import mock

import httpx
from django.contrib.auth import get_user_model
from django.core.signals import request_finished
from django.db import close_old_connections
from django.test import RequestFactory, override_settings
from django.utils.http import http_date

from apps.files import download, signing, transfer
from apps.storages import config as storage_config
from apps.storages import instances
from apps.storages.drivers import cloudreve, cloudreve_v3, microsoft
from apps.storages.drivers.base import BackendError
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import FilterMode, StorageSource
from apps.storages.tests.fake_cloudreve import FakeCloudreve
from apps.storages.tests.fake_cloudreve_v3 import FakeCloudreveV3
from apps.storages.tests.fake_graph import FakeGraph

from .drivers import CountingDriver, DirectDriver
from .helpers import API, NOT_DISK, FilesTestCase


class DownloadTestCase(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.data = os.urandom(5000)
        self.files({"docs/data.bin": self.data, "docs/empty.txt": b"", "docs/页面.html": b"<script>alert(1)</script>",
                    "docs/x.svg": b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>",
                    "docs/app.js": b"alert(1)", "docs/style.css": b"body{}", "docs/a.pdf": b"%PDF-1.4",
                    "docs/movie.mp4": b"\x00" * 64})

    def link(self, path, **kwargs):
        kwargs.setdefault("lifetime", 600)
        return signing.sign(self.storage, path, **kwargs)

    def fetch(self, url, **headers):
        response = self.client.get(url, **headers)
        body = b"".join(response.streaming_content) if response.streaming else response.content
        return response, body


class SignatureTests(DownloadTestCase):
    def test_a_link_serves_the_file(self):
        response, body = self.fetch(self.link("/docs/data.bin"))
        self.assertEqual((response.status_code, body), (200, self.data))
        self.assertEqual(response["Content-Length"], "5000")
        self.assertEqual(response["Accept-Ranges"], "bytes")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Cache-Control"].startswith("private, max-age="))
        self.assertIn("ETag", response)
        self.assertIn("Last-Modified", response)

    def test_bad_links(self):
        good = self.link("/docs/data.bin")
        other = StorageSource.objects.create(key="other", name="另一个", type="local", config={})
        cases = {
            "tampered": good[:-3] + ("AAA" if not good.endswith("AAA") else "BBB"),
            "another path": good.replace("/docs/data.bin", "/docs/empty.txt"),
            "another storage": good.replace("/pd/disk/", "/pd/other/"),
            "no signature": good.split("&sign=")[0],
        }
        for what, url in cases.items():
            with self.subTest(what):
                response, _ = self.fetch(url)
                self.assertEqual(response.status_code, 403)
        expired = self.link("/docs/data.bin", lifetime=60)
        link = signing.verify_url(expired)
        with mock.patch("apps.files.signing.time.time", return_value=link.expires + 5):
            response, body = self.fetch(expired)
        self.assertEqual(response.status_code, 403)
        self.assertIn("过期".encode(), body)
        self.assertEqual(self.fetch("/pd/nope/docs/data.bin?sign=x")[0].status_code, 404)
        for key in NOT_DISK:  # API-16: a good link, at a key that isn't the storage's
            with self.subTest(key=key):
                self.assertEqual(self.fetch(good.replace("/pd/disk/", f"/pd/{urllib.parse.quote(key)}/"))[0].status_code, 404)
        del other

    def test_permanent_after_rotation(self):
        url = self.link("/docs/data.bin", lifetime=None)
        self.assertEqual(self.fetch(url)[0].status_code, 200)
        StorageSource.objects.filter(pk=self.storage.pk).update(link_secret="new-secret")
        self.assertEqual(self.fetch(url)[0].status_code, 403)

    def test_a_disabled_storage(self):
        url = self.link("/docs/data.bin")
        self.storage.enabled = False
        self.storage.save()
        response, _ = self.fetch(url)
        self.assertEqual(response.status_code, 403)

    def test_api_callers_get_json(self):
        response = self.client.get(self.link("/docs/data.bin")[:-2] + "xx", HTTP_ACCEPT="application/json")
        self.assertEqual(response.status_code, 403)


class RuleTests(DownloadTestCase):
    def test_rules_added_after_signing(self):
        url = self.link("/docs/data.bin")
        self.hide("/docs", FilterMode.INACCESSIBLE)
        self.assertEqual(self.fetch(url)[0].status_code, 403)

    def test_disable_download_added_after_signing(self):
        url = self.link("/docs/data.bin")
        rule = self.hide("/docs/*.bin", FilterMode.DISABLE_DOWNLOAD)
        self.assertEqual(self.fetch(url)[0].status_code, 403)
        rule.delete()
        self.assertEqual(self.fetch(url)[0].status_code, 200)

    def test_only_hidden_still_serves(self):
        url = self.link("/docs/data.bin")
        self.hide("/docs/data.bin", FilterMode.HIDDEN)
        self.assertEqual(self.fetch(url)[0].status_code, 200)

    def test_a_link_of_someone_with_ignore_hidden(self):
        url = self.link("/docs/data.bin", ignore_hidden=True)
        self.hide("/docs", FilterMode.INACCESSIBLE)
        self.assertEqual(self.fetch(url)[0].status_code, 200)


class RangeTests(DownloadTestCase):
    def test_one_range(self):
        url = self.link("/docs/data.bin")
        for header, start, end in (("bytes=0-99", 0, 99), ("bytes=100-", 100, 4999), ("bytes=-10", 4990, 4999),
                                   ("bytes=4999-4999", 4999, 4999), ("bytes=10-999999", 10, 4999), ("bytes=-99999", 0, 4999)):
            with self.subTest(header):
                response, body = self.fetch(url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 206)
                self.assertEqual(response["Content-Range"], f"bytes {start}-{end}/5000")
                self.assertEqual(response["Content-Length"], str(end - start + 1))
                self.assertEqual(body, self.data[start:end + 1])

    def test_unsatisfiable_or_unparsable(self):
        url = self.link("/docs/data.bin")
        for header in ("bytes=5000-", "bytes=9-3", "bytes=-0", "bytes=-", "lines=1-2", "bytes=a-b", "bytes 0-1", "bytes=1-2-3",
                       "bytes=" + "9" * 40 + "-"):
            with self.subTest(header):
                response, body = self.fetch(url, HTTP_RANGE=header)
                self.assertEqual(response.status_code, 416)
                self.assertEqual(response["Content-Range"], "bytes */5000")
                self.assertEqual(body, b"")
        response, _ = self.fetch(self.link("/docs/empty.txt"), HTTP_RANGE="bytes=0-0")
        self.assertEqual(response.status_code, 416)

    def test_several_ranges_give_the_whole_file(self):
        response, body = self.fetch(self.link("/docs/data.bin"), HTTP_RANGE="bytes=0-1,5-6")
        self.assertEqual((response.status_code, body), (200, self.data))

    def test_the_driver_gets_the_entry_the_site_looked_up(self):
        """For a range the site asks what the file is, then the driver opened it and WebDAV's asked the server
        again: the driver gets the site's entry (open_known)."""
        with mock.patch.object(LocalDriver, "open_known", autospec=True, side_effect=LocalDriver.open_known) as known:
            response, body = self.fetch(self.link("/docs/data.bin"), HTTP_RANGE="bytes=10-19")
        self.assertEqual((response.status_code, body), (206, self.data[10:20]))
        _driver, path, entry, offset, length = known.call_args.args
        self.assertEqual((path, entry.size, offset, length), ("/docs/data.bin", 5000, 10, 10))

    def test_if_range(self):
        url = self.link("/docs/data.bin")
        etag = self.fetch(url)[0]["ETag"]
        response, body = self.fetch(url, HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE=etag)
        self.assertEqual((response.status_code, body), (206, self.data[:10]))
        response, body = self.fetch(url, HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE='"other"')
        self.assertEqual((response.status_code, body), (200, self.data))
        when = http_date(os.stat(self.root / "docs/data.bin").st_mtime)
        self.assertEqual(self.fetch(url, HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE=when)[0].status_code, 206)
        self.assertEqual(self.fetch(url, HTTP_RANGE="bytes=0-9", HTTP_IF_RANGE=http_date(1))[0].status_code, 200)

    def test_head(self):
        response = self.client.head(self.link("/docs/data.bin"))
        self.assertEqual((response.status_code, response["Content-Length"]), (200, "5000"))
        self.assertEqual(response.content, b"")
        self.assertIn(self.client.post(self.link("/docs/data.bin")).status_code, (403, 405))  # CSRF, or GET and HEAD only

    def test_a_backend_that_cant_seek_sends_everything(self):
        with mock.patch("apps.storages.drivers.local.LocalDriver.can_range", False):
            response, body = self.fetch(self.link("/docs/data.bin"), HTTP_RANGE="bytes=0-9")
        self.assertEqual((response.status_code, body), (200, self.data))
        self.assertEqual(response["Accept-Ranges"], "none")


class AnswerTests(DownloadTestCase):
    def test_disposition(self):
        response, _ = self.fetch(self.link("/docs/页面.html"))
        self.assertEqual(response["Content-Disposition"], "attachment; filename*=utf-8''%E9%A1%B5%E9%9D%A2.html")
        self.storage.force_download = False
        self.storage.save()
        self.assertTrue(self.fetch(self.link("/docs/data.bin"))[0]["Content-Disposition"].startswith("inline;"))
        self.assertTrue(self.fetch(self.link("/docs/data.bin", inline=False))[0]["Content-Disposition"].startswith("attachment;"))
        self.storage.force_download = True
        self.storage.save()
        self.assertTrue(self.fetch(self.link("/docs/data.bin", inline=True))[0]["Content-Disposition"].startswith("inline;"))
        named = self.fetch(self.link("/docs/data.bin", filename='a "b".bin'))[0]["Content-Disposition"]
        self.assertEqual(named, 'attachment; filename="a \\"b\\".bin"')

    def test_every_script_type_is_sent_as_text(self):
        from apps.files.download import content_type

        with mock.patch("apps.files.download.filetypes.guess_type", return_value=("text/jscript", None)):
            self.assertEqual(content_type("x.js2"), "text/plain; charset=utf-8")
        with mock.patch("apps.files.download.filetypes.guess_type", return_value=("text/livescript", None)):
            self.assertEqual(content_type("x.ls"), "text/plain; charset=utf-8")
        self.assertEqual(content_type("x.mjs"), "text/plain; charset=utf-8")
        self.assertEqual(content_type("x.tar.gz"), "application/gzip")
        self.assertEqual(content_type("x.unknownext"), "application/octet-stream")

    @unittest.skipIf(os.name == "nt", "Windows has no names with line breaks in them")
    def test_a_name_with_a_line_break(self):
        self.files({"docs/two\nlines.txt": b"nl"})
        response, body = self.fetch(self.link("/docs/two\nlines.txt"))
        self.assertEqual((response.status_code, body), (200, b"nl"))

    def test_types_and_the_sandbox(self):
        sandbox = "sandbox; default-src 'none'"
        for path, kind, sandboxed in (("/docs/页面.html", "text/html; charset=utf-8", True), ("/docs/x.svg", "image/svg+xml", True),
                                      ("/docs/app.js", "text/plain; charset=utf-8", True),
                                      ("/docs/style.css", "text/plain; charset=utf-8", True),
                                      ("/docs/movie.mp4", "video/mp4", True), ("/docs/empty.txt", "text/plain; charset=utf-8", True)):
            for inline in (True, False):
                with self.subTest(path=path, inline=inline):
                    response, _ = self.fetch(self.link(path, inline=inline))
                    self.assertEqual(response["Content-Type"], kind)
                    self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                    self.assertTrue(response["Content-Security-Policy"].startswith(sandbox))
        # PDF viewers don't work in a sandbox: inline PDFs only get frame-ancestors.
        response, _ = self.fetch(self.link("/docs/a.pdf", inline=True))
        self.assertEqual((response["Content-Type"], response["Content-Security-Policy"]), ("application/pdf", "frame-ancestors 'self'"))
        response, _ = self.fetch(self.link("/docs/a.pdf", inline=False))
        self.assertTrue(response["Content-Security-Policy"].startswith(sandbox))

    def test_the_signature_is_masked_in_logs(self):
        from apps.core.masking import mask_address

        url = self.link("/docs/data.bin")
        path, query = url.split("?")
        masked = mask_address(path, query)
        self.assertIn("sign=<sign>", masked)
        self.assertNotIn(signing.verify_url(url).path + "?" + query, masked)


class StreamTests(DownloadTestCase):
    storage_type = "test-counting"

    def setUp(self):
        super().setUp()
        CountingDriver.streams = []
        self.files({"big.bin": os.urandom(2 * 1024 * 1024)})

    def test_a_client_that_goes_away_closes_the_stream(self):
        # The view as the WSGI server runs it: the answer streams; the connection ends after a first
        # piece and the server closes the answer.
        url = self.link("/big.bin")
        key, path, _ = signing.parse_url(url)
        response = download.serve(RequestFactory().get(url), key, path)
        chunks = iter(response.streaming_content)
        next(chunks)
        self.assertEqual(len(CountingDriver.streams), 1)
        self.assertFalse(CountingDriver.streams[0].closed)
        # (request_finished would close the database connection of the test's transaction.)
        request_finished.disconnect(close_old_connections)
        try:
            response.close()
        finally:
            request_finished.connect(close_old_connections)
        self.assertTrue(CountingDriver.streams[0].closed)

    def test_streams_are_closed_after_the_whole_file_and_after_a_range(self):
        self.fetch(self.link("/big.bin"))
        self.fetch(self.link("/big.bin"), HTTP_RANGE="bytes=10-20")
        self.assertEqual(len(CountingDriver.streams), 2)
        for stream in CountingDriver.streams:
            self.assertTrue(stream.closed)


class RedirectTests(DownloadTestCase):
    storage_type = "test-direct"

    def setUp(self):
        super().setUp()
        DirectDriver.calls = []

    def test_a_deferred_link_redirects_to_the_backends_own(self):
        url = signing.sign(self.storage, "/docs/data.bin", lifetime=600, redirect=True)
        response = self.client.get(url, HTTP_USER_AGENT="Mozilla/5.0 test")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "https://cdn.example.test/docs/data.bin?expires=1800&sig=abc")
        self.assertEqual(DirectDriver.calls, [("/docs/data.bin", "data.bin", "Mozilla/5.0 test")])
        self.assertEqual(response["Referrer-Policy"], "no-referrer")
        # The rules are checked first.
        self.hide("/docs", FilterMode.INACCESSIBLE)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_the_backends_link_is_kept_and_misses_are_counted(self):
        url = signing.sign(self.storage, "/docs/data.bin", lifetime=600, redirect=True)
        for _ in range(3):
            self.assertEqual(self.client.get(url, HTTP_USER_AGENT="UA one").status_code, 302)
        self.assertEqual(len(DirectDriver.calls), 1)  # asked once, then kept
        self.client.get(url, HTTP_USER_AGENT="UA two")
        self.assertEqual(len(DirectDriver.calls), 1)  # this backend's links don't depend on the UA
        with mock.patch.object(transfer, "UA_BOUND", frozenset({"test-direct"})):
            self.client.get(url, HTTP_USER_AGENT="UA two")
        self.assertEqual(len(DirectDriver.calls), 2)
        other = signing.sign(self.storage, "/docs/empty.txt", lifetime=600, redirect=True)
        with mock.patch.object(download, "REDIRECT_MISSES", 2):
            response = self.client.get(other)
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)

    def test_a_link_is_kept_half_its_lifetime_at_most(self):
        """Kept 300 seconds whatever it was asked for, a link of 60 went out long dead."""
        self.storage.link_lifetime = 60
        self.storage.save()
        url = signing.sign(self.storage, "/docs/data.bin", lifetime=600, redirect=True)
        now = time.time()
        self.assertEqual(self.client.get(url).status_code, 302)
        with mock.patch("time.time", return_value=now + 25):
            self.client.get(url)
        self.assertEqual(len(DirectDriver.calls), 1)  # kept
        with mock.patch("time.time", return_value=now + 35):
            self.client.get(url)
        self.assertEqual(len(DirectDriver.calls), 2)  # for 30 seconds only

    def test_http_links_arent_handed_to_an_https_page(self):
        # On an https site an http:// link is mixed content (the viewers' <img>, <video> and <audio> are refused it):
        # the site's own link goes out instead. To an http page the backend's, as before.
        self.login()

        def listed(**extra):
            response = self.client.get(f"{API}disk/list/", {"path": "/docs"}, **extra)
            return next(item["url"] for item in response.json()["items"] if item["name"] == "data.bin")

        with mock.patch.object(DirectDriver, "scheme", "http"):
            self.assertTrue(listed().startswith("http://cdn.example.test/docs/data.bin?"))
            self.assertTrue(listed(secure=True).startswith("/pd/disk/docs/data.bin?"))
            plain, secure = self.client.get("/").wsgi_request, self.client.get("/", secure=True).wsgi_request
            self.assertTrue(transfer.download_url(plain, self.storage, "/docs/data.bin").startswith("http://cdn.example.test/"))
            self.assertTrue(transfer.download_url(secure, self.storage, "/docs/data.bin").startswith("/pd/disk/docs/data.bin?"))
            with override_settings(SITE_URL="https://files.example.com"):  # [site] url says the pages are https
                self.assertTrue(listed().startswith("/pd/disk/docs/data.bin?"))
                self.assertTrue(transfer.download_url(plain, self.storage, "/docs/data.bin").startswith("/pd/disk/"))
        self.assertTrue(listed(secure=True).startswith("https://cdn.example.test/docs/data.bin?"))  # an https link stays

    def test_a_deferred_http_link_on_an_https_page_is_sent_through_the_site(self):
        url = signing.sign(self.storage, "/docs/data.bin", lifetime=600, redirect=True)
        with mock.patch.object(DirectDriver, "scheme", "http"):
            response, body = self.fetch(url, secure=True)
            self.assertEqual((response.status_code, body), (200, self.data))
            response = self.client.get(url)  # an http page: the backend's own link, as before
            self.assertEqual((response.status_code, response["Location"][:7]), (302, "http://"))

    def test_with_proxy_download_it_streams(self):
        self.storage.proxy_download = True
        self.storage.save()
        url = signing.sign(self.storage, "/docs/data.bin", lifetime=600, redirect=True)
        response, body = self.fetch(url)
        self.assertEqual((response.status_code, body), (200, self.data))

    def test_listing_and_w1_urls(self):
        self.login()
        url = next(i["url"] for i in self.listing("/docs")["items"] if i["name"] == "data.bin")
        self.assertTrue(url.startswith("https://cdn.example.test/docs/data.bin"))
        with mock.patch.object(transfer, "DEFERRED", frozenset({"test-direct"})):
            url = next(i["url"] for i in self.listing("/docs")["items"] if i["name"] == "data.bin")
            self.assertTrue(url.startswith("/pd/disk/docs/data.bin?") and "&r=1&" in url)
        request = self.client.get("/").wsgi_request
        self.assertTrue(transfer.download_url(request, self.storage, "/docs/data.bin").startswith("https://cdn.example.test/"))
        same = transfer.download_url(request, self.storage, "/docs/data.bin", same_origin=True, inline=True, absolute=True)
        self.assertTrue(same.startswith("http://testserver/pd/disk/docs/data.bin?"))
        self.assertIn("&d=i&", same)
        self.storage.proxy_download = True
        self.storage.save()
        self.assertTrue(transfer.download_url(request, self.storage, "/docs/data.bin").startswith("/pd/disk/"))
        self.assertEqual(transfer.media_sources(self.storage), ())


class FailureTests(DownloadTestCase):
    def test_what_the_storage_said_is_shown_to_staff_only(self):
        url = self.link("/docs/data.bin")
        with mock.patch.object(LocalDriver, "open", side_effect=BackendError("存储那边坏了（测试）。")):
            with self.assertLogs("hfile.files", "WARNING") as logs:
                response, body = self.fetch(url)
            self.assertEqual(response.status_code, 500)
            self.assertIn("从存储读取文件失败，请稍后再试。", body.decode())
            self.assertNotIn("存储那边坏了", body.decode())
            self.assertIn("Download failed (storage disk): 存储那边坏了（测试）。", logs.output[0])
            self.client.force_login(self.alice)  # may do everything there, but isn't staff
            response, body = self.fetch(url)
            self.assertNotIn("存储那边坏了", body.decode())
            admin = get_user_model().objects.create_user("boss", password="pw-boss-2026", is_staff=True)
            self.client.force_login(admin)
            response, body = self.fetch(url)
            self.assertEqual(response.status_code, 500)
            self.assertIn("从存储读取文件失败：存储那边坏了（测试）。", body.decode())


class OneTimeLinkTests(DownloadTestCase):
    """Cloudreve v3's links may work once only (the "one-time download" of its anonymous user group): the site asks for
    one each time, not the one it got minutes ago for everyone."""

    def setUp(self):
        super().setUp()
        self.cloudreve = FakeCloudreveV3("3.8.3").start()
        self.addCleanup(self.cloudreve.close)
        self.cloudreve.one_time = True
        self.cloudreve.add_user("a@example.com", "pw-a-2026")
        settings = {"url": self.cloudreve.url, "email": "a@example.com", "password": "pw-a-2026", "base_path": "/"}
        self.v3 = StorageSource.objects.create(key="v3", name="Cloudreve", type="cloudreve-v3",
                                               config=storage_config.encode(cloudreve_v3.CloudreveV3Driver, settings))
        self.addCleanup(instances.close_all)
        instances.get(self.v3).write("/a.bin", io.BytesIO(b"abc"), 3)

    def test_a_link_that_works_once_is_asked_for_each_time(self):
        url = signing.sign(self.v3, "/a.bin", lifetime=600, redirect=True)
        for _ in range(2):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(httpx.get(response["Location"]).content, b"abc")


class ShortLinkTests(DownloadTestCase):
    """Cloudreve's links last its "File Blob URL TTL": the site doesn't hand one out again longer than the driver reuses
    it, while it has a minute left at least."""

    def setUp(self):
        super().setUp()
        self.cloudreve = FakeCloudreve(4).start()
        self.addCleanup(self.cloudreve.close)
        self.cloudreve.add_user("a@example.com", "pw-a-2026")
        settings = {"url": self.cloudreve.url, "email": "a@example.com", "password": "pw-a-2026", "base_path": "/"}
        self.v4 = StorageSource.objects.create(key="v4", name="Cloudreve", type="cloudreve",
                                               config=storage_config.encode(cloudreve.CloudreveDriver, settings))
        self.addCleanup(instances.close_all)
        instances.get(self.v4).write("/a.bin", io.BytesIO(b"abc"), 3)

    def test_a_short_lived_link_is_asked_for_each_time(self):
        self.cloudreve.url_ttl = 30
        url = signing.sign(self.v4, "/a.bin", lifetime=600, redirect=True)
        for _ in range(2):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302)
            self.assertEqual(httpx.get(response["Location"]).content, b"abc")
        self.assertEqual(len(self.cloudreve.routes("POST", "/file/url")), 2)

    def test_a_link_is_kept_while_the_driver_reuses_it(self):
        self.cloudreve.url_ttl = 120  # reused 60 seconds
        url = signing.sign(self.v4, "/a.bin", lifetime=600, redirect=True)
        now = time.time()
        first = self.client.get(url)["Location"]
        with mock.patch.object(cloudreve.CloudreveDriver, "timed_download_url", autospec=True,
                               side_effect=cloudreve.CloudreveDriver.timed_download_url) as asked:
            with mock.patch("time.time", return_value=now + 50):
                self.assertEqual(self.client.get(url)["Location"], first)
            self.assertEqual(asked.call_count, 0)  # kept
            with mock.patch("time.time", return_value=now + 70):
                self.client.get(url)
            self.assertEqual(asked.call_count, 1)  # for 60 seconds only


class GraphLinkTests(DownloadTestCase):
    """OneDrive on an account that leaves the download link out of the item although it was asked for
    (a real server's 「从存储读取文件失败」): the driver gets it from /content instead."""

    def setUp(self):
        super().setUp()
        self.graph = FakeGraph()
        self.addCleanup(self.graph.close)
        self.graph.select_download_url = False
        for name in ("graph_root", "login_root"):
            patcher = mock.patch.object(microsoft.OneDriveDriver, name, self.graph.url)
            patcher.start()
            self.addCleanup(patcher.stop)
        drive = self.graph.drive("me")
        with self.graph.lock:
            drive.add(drive.root, "数据.bin", False, self.data)
        self.onedrive = StorageSource.objects.create(
            key="od", name="OneDrive", type="onedrive",
            config=storage_config.encode(microsoft.OneDriveDriver, {
                "client_id": "app-id", "client_secret": "app-secret", "tenant": "common",
                "refresh_token": self.graph.issue_refresh_token(), "base_path": "/"}))

    def test_a_download_redirects_to_the_link_from_content(self):
        response = self.client.get(signing.sign(self.onedrive, "/数据.bin", lifetime=600, redirect=True))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith(self.graph.url + "/download/"))
        self.assertEqual(httpx.get(response["Location"]).content, self.data)

    def test_through_the_site(self):
        self.onedrive.proxy_download = True
        self.onedrive.save()
        response, body = self.fetch(signing.sign(self.onedrive, "/数据.bin", lifetime=600, redirect=True))
        self.assertEqual((response.status_code, body), (200, self.data))
        response, body = self.fetch(signing.sign(self.onedrive, "/数据.bin", lifetime=600), HTTP_RANGE="bytes=10-19")
        self.assertEqual((response.status_code, body), (206, self.data[10:20]))
