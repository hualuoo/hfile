"""The pipeline both kinds of link share (inventory 9.5-9.8): the rate limit, the Referer rules, the download
log, .m3u8 playlists, the answer's headers and the storage's errors."""

from __future__ import annotations

import datetime
import os
import time
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone
from django.utils.http import http_date

from apps.files import signing, transfer
from apps.files.tests.drivers import DirectDriver
from apps.siteconf.models import RefererType
from apps.storages.drivers.base import BackendError
from apps.storages.models import StorageSource

from .. import delivery, ratelimit, referer, service
from ..models import DownloadLog, ShortLink
from .helpers import EVERYTHING, LinksTestCase


class LimiterTests(SimpleTestCase):
    def test_a_fixed_window(self):
        limiter = ratelimit.Limiter()
        self.assertEqual([limiter.hit("a", 60, 2, now=t) for t in (100, 110, 120)], [0, 0, 40])
        self.assertEqual(limiter.hit("a", 60, 2, now=150), 10)  # refused ones count too; the window doesn't move
        self.assertEqual(limiter.hit("b", 60, 2, now=150), 0)  # an address of its own
        self.assertEqual(limiter.hit("a", 60, 2, now=160), 0)  # a new window
        [other, window] = limiter.windows(60, now=161)  # the busiest first, then the oldest
        self.assertEqual((other.address, other.count), ("b", 1))
        self.assertEqual((window.address, window.count), ("a", 1))
        self.assertAlmostEqual(window.expires - window.started, 60)

    def test_no_limit(self):
        limiter = ratelimit.Limiter()
        for seconds, limit in ((0, 5), (60, 0), (0, 0)):
            self.assertEqual([limiter.hit("a", seconds, limit, now=1) for _ in range(10)], [0] * 10)
        self.assertEqual(limiter.windows(60, now=1), [])

    def test_bounded_and_never_refusing_a_new_address(self):
        limiter = ratelimit.Limiter(max_windows=3)
        for n, address in enumerate("abc"):
            limiter.hit(address, 60, 1, now=n)
        self.assertEqual(limiter.hit("d", 60, 1, now=10), 0)  # "a", the oldest, makes room
        self.assertEqual(sorted(window.address for window in limiter.windows(60, now=10)), ["b", "c", "d"])
        self.assertEqual(limiter.hit("a", 60, 1, now=11), 0)
        # Windows that ended go first.
        self.assertEqual(limiter.hit("e", 60, 1, now=62), 0)
        self.assertEqual(limiter.hit("b", 60, 1, now=62), 0)
        self.assertEqual(len(limiter._windows), 3)

    def test_a_refused_address_isnt_freed_by_a_flood(self):
        limiter = ratelimit.Limiter(max_windows=4)
        for t in range(3):
            limiter.hit("attacker", 60, 2, now=t)
        self.assertGreater(limiter.hit("attacker", 60, 2, now=3), 0)
        for n in range(50):  # new addresses push the oldest that isn't over the limit out, not the attacker
            self.assertEqual(limiter.hit(f"flood-{n}", 60, 2, now=4 + n / 100), 0)
        self.assertGreater(limiter.hit("attacker", 60, 2, now=5), 0)
        self.assertEqual(len(limiter._windows), 4)
        # Only when every window it looks at is over the limit does the oldest go.
        limiter = ratelimit.Limiter(max_windows=2)
        for address in ("a", "b"):
            for t in range(3):
                limiter.hit(address, 60, 1, now=t)
        self.assertEqual(limiter.hit("c", 60, 1, now=4), 0)
        self.assertEqual(sorted(limiter._windows), ["b", "c"])

    def test_thread_safe(self):
        import threading

        limiter = ratelimit.Limiter()
        refused = []

        def hammer():
            for _ in range(200):
                if limiter.hit("a", 3600, 1000):
                    refused.append(1)

        threads = [threading.Thread(target=hammer) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(refused), 600)  # 1600 requests, exactly 1000 let through
        self.assertEqual(limiter.windows(3600)[0].count, 1600)


class RefererRuleTests(SimpleTestCase):
    def matches(self, rules, url):
        return referer.Rules(rules).match(url) is not None

    def test_the_examples_of_the_settings_page(self):
        subdomains = "https://*.example.com"
        for url in ("https://example.com/", "https://www.example.com/", "https://a.b.example.com/page?x=1",
                    "https://EXAMPLE.com", "https://example.com:443/x", "https://example.com:8443/"):
            self.assertTrue(self.matches(subdomains, url), url)  # without a port in the rule: any port
        for url in ("http://example.com/", "https://example.com.evil.net/", "https://evilexample.com/",
                    "https://evil.net/https://www.example.com/",
                    "https://evil.net/?https://example.com", "https://example.com@evil.net/"):
            self.assertFalse(self.matches(subdomains, url), url)
        either = "*://example.com"
        self.assertTrue(self.matches(either, "http://example.com/a"))
        self.assertTrue(self.matches(either, "https://example.com/a"))
        self.assertFalse(self.matches(either, "ftp://example.com/a"))
        self.assertFalse(self.matches(either, "android-app://example.com/"))
        pages = "https://example.com/**"
        self.assertTrue(self.matches(pages, "https://example.com/"))
        self.assertTrue(self.matches(pages, "https://example.com/a/b/c.html?q=1"))
        self.assertFalse(self.matches(pages, "https://www.example.com/"))

    def test_paths(self):
        rules = "https://example.com/docs/*"
        self.assertTrue(self.matches(rules, "https://example.com/docs/a.html"))
        self.assertTrue(self.matches(rules, "https://example.com/Docs/A.html"))  # without case
        self.assertTrue(self.matches(rules, "https://example.com/docs/"))
        self.assertFalse(self.matches(rules, "https://example.com/docs/a/b.html"))
        self.assertFalse(self.matches(rules, "https://example.com/docs"))
        self.assertTrue(self.matches("https://example.com/**/*.html", "https://example.com/a/b/c.html"))
        self.assertFalse(self.matches("https://example.com/**/*.html", "https://example.com/a/b/c.htm"))
        self.assertTrue(self.matches("https://example.com/a**z", "https://example.com/a/b/c/z"))
        self.assertTrue(self.matches("https://example.com/page", "https://example.com/page"))
        # The query counts only when the rule has one.
        self.assertTrue(self.matches("https://example.com/page", "https://example.com/page?x=1"))
        self.assertTrue(self.matches("https://example.com/docs/*", "https://example.com/docs/a?next=/x/y"))
        self.assertTrue(self.matches("https://example.com/p?id=*", "https://example.com/p?id=7"))
        self.assertFalse(self.matches("https://example.com/p?id=*", "https://example.com/p?id=7&x=/y"))
        self.assertFalse(self.matches("https://example.com/p?id=*", "https://example.com/p"))
        self.assertTrue(self.matches("https://example.com/", "https://example.com/anything"))  # a bare "/": any page
        # A whole-segment ** may be no segment at all.
        self.assertTrue(self.matches("https://example.com/a/**/b", "https://example.com/a/b"))
        self.assertTrue(self.matches("https://example.com/a/**/b", "https://example.com/a/x/y/b"))
        self.assertTrue(self.matches("https://example.com/a/**", "https://example.com/a"))
        self.assertTrue(self.matches("https://example.com/a/**", "https://example.com/a/x"))
        self.assertFalse(self.matches("https://example.com/a/**", "https://example.com/ab"))
        self.assertFalse(self.matches("https://example.com/a/**/b", "https://example.com/a/xb"))

    def test_hosts(self):
        self.assertTrue(self.matches("http://*example.com", "http://demo.example.com/"))
        self.assertTrue(self.matches("https://*.例子.中国", "https://www.xn--fsqu00a.xn--fiqs8s/"))
        self.assertTrue(self.matches("https://straße.de", "https://xn--strae-oqa.de/"))  # as browsers encode it
        # A name with its trailing dot is the same name (a page at evil.com. is still evil.com).
        self.assertTrue(self.matches("https://evil.com", "https://evil.com./x"))
        self.assertTrue(self.matches("https://*.evil.com", "https://www.evil.com.:443/x"))
        self.assertTrue(self.matches("https://evil.com.", "https://evil.com/"))
        # The scheme's own port never counts, also for *:// rules.
        self.assertTrue(self.matches("*://example.com:443", "https://example.com/"))
        self.assertTrue(self.matches("*://example.com", "http://example.com:80/"))
        self.assertFalse(self.matches("*://example.com:443", "http://example.com/"))
        self.assertTrue(self.matches("https://example.com:8443", "https://example.com:8443/"))
        self.assertFalse(self.matches("https://example.com:8443", "https://example.com/"))  # a port: only that one
        self.assertFalse(self.matches("https://example.com:8443", "https://example.com:9443/"))
        self.assertTrue(self.matches("https://[::1]", "https://[::1]:8080/"))
        self.assertTrue(self.matches("https://[::1]:8080", "https://[::1]:8080/"))
        self.assertTrue(self.matches("https://*", "https://anything.at.all/x"))
        self.assertFalse(self.matches("https://*", "http://anything/"))

    def test_lines_and_bad_input(self):
        rules = "\n  https://a.example  \r\nnot a rule\n\nhttps://b.example\n"
        self.assertTrue(self.matches(rules, "https://b.example/"))
        self.assertFalse(self.matches(rules, "not a url"))
        self.assertFalse(self.matches(rules, "https://[::1/"))
        self.assertFalse(self.matches(rules, "https:///nohost"))
        self.assertFalse(self.matches("", "https://a.example/"))

    def test_linear_time(self):
        # Patterns whose regular expressions would backtrack for ages on a long path.
        for rule, url in (("https://example.com/" + "**a" * 40 + "**b", "https://example.com/" + "a" * 2000),
                          ("https://example.com/" + "*/**/" * 80 + "b", "https://example.com/" + "a/" * 1000),
                          ("https://example.com/" + "**/" * 160 + "b", "https://example.com/" + "a/" * 1000)):
            rules = referer.Rules("\n".join([rule] * 200))
            started = time.process_time()
            self.assertIsNone(rules.match(url))
            self.assertLess(time.process_time() - started, 2.0)
        host = "https://" + "*a" * 100 + ".com"
        started = time.process_time()
        self.assertIsNone(referer.Rules("\n".join([host] * 200)).match("https://" + "a" * 2000 + ".org/"))
        self.assertLess(time.process_time() - started, 1.0)


class PipelineTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.link = self.short()
        self.url = self.path_link("/docs/a.txt")

    # -- the rate limit

    def test_the_rate_limit(self):
        self.site(link_limit_seconds=60, link_limit_downloads=2)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertEqual(self.client.get(f"/s/{self.link.key}").status_code, 302)  # one budget for both kinds
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 429)
        self.assertGreaterEqual(int(response["Retry-After"]), 59)
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")
        self.assertEqual(self.client.get(f"/s/{self.link.key}").status_code, 429)
        self.assertEqual(self.client.get("/directlink/disk/missing/x").status_code, 429)  # every attempt counts
        self.assertEqual(DownloadLog.objects.count(), 2)
        # Another address has a budget of its own.
        self.assertEqual(self.client.get(self.url, REMOTE_ADDR="203.0.113.9").status_code, 302)
        self.site(link_limit_downloads=0)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_ipv6_counts_by_its_64(self):
        self.site(link_limit_seconds=60, link_limit_downloads=1)
        self.assertEqual(self.client.get(self.url, REMOTE_ADDR="2001:db8:1:2::1").status_code, 302)
        self.assertEqual(self.client.get(self.url, REMOTE_ADDR="2001:db8:1:2:ffff::9").status_code, 429)
        self.assertEqual(self.client.get(self.url, REMOTE_ADDR="2001:db8:1:3::1").status_code, 302)

    def test_behind_a_proxy(self):
        self.site(link_limit_seconds=60, link_limit_downloads=1)
        with mock.patch("rest_framework.settings.api_settings.NUM_PROXIES", 1):
            self.assertEqual(self.client.get(self.url, HTTP_X_FORWARDED_FOR="198.51.100.1").status_code, 302)
            self.assertEqual(self.client.get(self.url, HTTP_X_FORWARDED_FOR="198.51.100.2").status_code, 302)
            self.assertEqual(self.client.get(self.url, HTTP_X_FORWARDED_FOR="9.9.9.9, 198.51.100.2").status_code, 429)
        self.assertEqual(DownloadLog.objects.order_by("id").first().ip, "198.51.100.1")

    # -- Referer

    def test_allowlist(self):
        self.site(referer_type=RefererType.ALLOWLIST, referer_rules="https://*.example.com\nhttps://partner.test/page/*",
                  referer_allow_empty=True)
        self.assertEqual(self.client.get(self.url).status_code, 302)  # no Referer, allowed
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://www.example.com/x").status_code, 302)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://partner.test/page/1").status_code, 302)
        for bad in ("https://evil.test/", "https://partner.test/other", "https://example.com.evil.test/",
                    "http://www.example.com/", "nonsense", "https://example.com/" + "x" * 3000):
            response = self.client.get(self.url, HTTP_REFERER=bad)
            self.assertEqual(response.status_code, 403, bad)
            self.assertIn("防盗链".encode() if len(bad) < 3000 else "太长".encode(), response.content)
        self.assertEqual(self.client.get(f"/s/{self.link.key}", HTTP_REFERER="https://evil.test/").status_code, 403)
        self.site(referer_allow_empty=False)
        self.assertEqual(self.client.get(self.url).status_code, 403)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="  ").status_code, 403)
        self.assertEqual(DownloadLog.objects.count(), 1)  # the three let through: one address's openings of one link

    def test_blocklist(self):
        self.site(referer_type=RefererType.BLOCKLIST, referer_rules="*://*.evil.test")
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="http://evil.test/").status_code, 403)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://a.evil.test/x").status_code, 403)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://good.test/?r=https://evil.test/").status_code, 302)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://good.test/" + "x" * 3000).status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.site(referer_allow_empty=False)
        self.assertEqual(self.client.get(self.url).status_code, 403)

    def test_off(self):
        self.site(referer_type=RefererType.OFF, referer_rules="https://only.test", referer_allow_empty=False)
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://evil.test/").status_code, 302)
        self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_the_rate_limit_comes_first(self):
        self.site(link_limit_seconds=60, link_limit_downloads=1, referer_type=RefererType.ALLOWLIST, referer_rules="")
        self.assertEqual(self.client.get(self.url, HTTP_REFERER="https://evil.test/").status_code, 403)
        self.assertEqual(self.client.get(self.url).status_code, 429)  # the refused one counted

    # -- the download log

    def test_the_log(self):
        response = self.client.get(self.url, HTTP_USER_AGENT="Mozilla/5.0 测试".encode().decode("latin-1"),
                                   HTTP_REFERER="https://example.com/a?b=c", REMOTE_ADDR="203.0.113.7")
        self.assertEqual(response.status_code, 302)
        log = DownloadLog.objects.get()
        self.assertEqual((log.kind, log.storage, log.path, log.short_key), ("path", self.storage, "/docs/a.txt", ""))
        self.assertEqual((log.ip, log.user_agent, log.referer), ("203.0.113.7", "Mozilla/5.0 测试", "https://example.com/a?b=c"))
        self.client.get(self.url, HTTP_USER_AGENT="x" * 5000 + "\x00", HTTP_REFERER="y" * 2000)
        self.assertEqual(len(DownloadLog.objects.first().user_agent), 2048)

    def test_no_log_for_refusals_or_when_off(self):
        self.client.get(self.path_link("/docs/missing.txt"))  # /pd/ answers that one; the link itself worked
        self.assertEqual(DownloadLog.objects.count(), 1)
        DownloadLog.objects.all().delete()
        self.lock("/top.txt")
        self.client.get(self.path_link("/top.txt"))
        self.client.get("/s/nothing")
        self.assertFalse(DownloadLog.objects.exists())
        self.site(record_download_log=False)
        self.assertEqual(self.client.get(self.url).status_code, 302)
        self.assertFalse(DownloadLog.objects.exists())

    def test_once_per_address_user_and_link(self):
        # A download tool opens the link again for each of its parts, maybe after a HEAD, and to resume: one
        # row while what the link redirects to may still be valid.
        short = f"/s/{self.link.key}"
        for url in (self.url, short):
            self.assertEqual(self.client.head(url).status_code, 302)  # its answer hands out the URL too
            self.assertEqual(self.client.get(url).status_code, 302)
            for part in ("bytes=0-", "bytes=2-", "bytes=-2", "bytes=0-1,3-4"):  # from the start, resumed, the end, several
                self.assertEqual(self.client.get(url, HTTP_RANGE=part).status_code, 302)
        self.client.get(self.url, REMOTE_ADDR="203.0.113.9")  # another address
        self.visitor().get(short)  # nobody logged in, at the same address
        with mock.patch("time.monotonic", return_value=time.monotonic() + service.TARGET_LIFETIME - 5):
            self.client.get(self.url)
        with mock.patch("time.monotonic", return_value=time.monotonic() + service.TARGET_LIFETIME + 5):
            self.client.get(self.url)  # another download
        self.assertEqual(list(DownloadLog.objects.order_by("id").values_list("kind", "ip", "username")),
                         [("path", "127.0.0.1", "alice"), ("short", "127.0.0.1", "alice"), ("path", "203.0.113.9", "alice"),
                          ("short", "127.0.0.1", ""), ("path", "127.0.0.1", "alice")])

    def test_a_failing_log_doesnt_stop_the_download(self):
        with mock.patch.object(DownloadLog.objects, "create", side_effect=__import__("django").db.DatabaseError("x")), \
                self.assertLogs("hfile.links", "WARNING"):
            self.assertEqual(self.client.get(self.url).status_code, 302)

    def test_an_unchanged_file_asked_about_with_if_modified_since(self):
        # wget -N, curl -z: If-Modified-Since asks whether the file changed since the copy the tool has. The link
        # answers 304 itself when it didn't, by the driver's time (which /pd/ sends as Last-Modified, in whole
        # seconds), and logs no download: the tool doesn't download one.
        stamp = 1704164645
        os.utime(self.root / "docs/a.txt", (stamp + 0.75, stamp + 0.75))
        self.files({"video/a.m3u8": b"#EXTM3U\n"})
        os.utime(self.root / "video/a.m3u8", (stamp, stamp))
        short = f"/s/{self.link.key}"
        with mock.patch.object(delivery, "read_playlist", side_effect=AssertionError("read for a 304")):
            for url in (self.url, short, self.path_link("/video/a.m3u8")):
                for send in (self.client.get, self.client.head):
                    for since in (stamp, stamp + 3600):
                        response = send(url, HTTP_IF_MODIFIED_SINCE=http_date(since))
                        self.assertEqual(response.status_code, 304, url)
                        self.assertEqual(response["Cache-Control"], delivery.NO_CACHE)
                        self.assertNotIn("Location", response)
        self.assertFalse(DownloadLog.objects.exists())
        # Changed since then, a date that can't be read, an ETag asked about (If-None-Match comes first and the
        # link has none to compare), a file that isn't there (/pd/ says so): as without the header.
        for headers in ({"HTTP_IF_MODIFIED_SINCE": http_date(stamp - 1)}, {"HTTP_IF_MODIFIED_SINCE": "yesterday"},
                        {"HTTP_IF_MODIFIED_SINCE": http_date(stamp), "HTTP_IF_NONE_MATCH": '"abc"'}):
            self.assertEqual(self.client.get(self.url, **headers).status_code, 302)
        self.assertEqual(self.client.get(self.path_link("/docs/missing.txt"),
                                         HTTP_IF_MODIFIED_SINCE=http_date(stamp)).status_code, 302)
        self.assertEqual(list(DownloadLog.objects.order_by("id").values_list("path", flat=True)),
                         ["/docs/a.txt", "/docs/missing.txt"])

    # -- the download itself

    def test_a_storage_that_serves_its_own_downloads(self):
        bucket = self.make_storage("bucket", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        response = self.client.get(self.path_link("/docs/a.txt", key="bucket"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://cdn.example.test/docs/a.txt?"))
        self.assertEqual(DirectDriver.calls[-1], ("/docs/a.txt", "a.txt", None))

    def test_backend_errors(self):
        bucket = self.make_storage("bucket", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        with mock.patch.object(DirectDriver, "download_url", side_effect=BackendError("down")), \
                self.assertLogs("hfile.links", "WARNING"), self.assertLogs("django.request", "ERROR"):
            response = self.client.get(self.path_link("/docs/a.txt", key="bucket"))
        self.assertEqual(response.status_code, 502)
        self.assertFalse(DownloadLog.objects.exists())

    def test_m3u8(self):
        playlist = b"#EXTM3U\n#EXTINF:10,\nseg-1.ts\n#EXT-X-ENDLIST\n"
        self.files({"video/a.m3u8": playlist, "video/big.M3U8": b"#" * (delivery.PLAYLIST_MAX + 1)})
        response = self.client.get(self.path_link("/video/a.m3u8"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, playlist)
        self.assertEqual(response["Content-Type"], "application/vnd.apple.mpegurl; charset=utf-8")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertTrue(response["Content-Security-Policy"].startswith("sandbox"))
        self.assertEqual(response["Cache-Control"], "no-cache, no-store, must-revalidate, private")
        self.assertEqual(DownloadLog.objects.get().path, "/video/a.m3u8")
        self.assertEqual(self.client.get(self.path_link("/video/big.M3U8")).status_code, 403)
        self.assertEqual(self.client.get(self.path_link("/video/none.m3u8")).status_code, 404)
        # HEAD: the size, from stat. waitress sends whatever body a view returns, for HEAD too.
        with mock.patch.object(delivery, "read_playlist", side_effect=AssertionError("read for HEAD")):
            response = self.client.head(self.path_link("/video/a.m3u8"))
            self.assertEqual((response.status_code, response["Content-Length"]), (200, str(len(playlist))))
            self.assertEqual(response["Content-Type"], "application/vnd.apple.mpegurl; charset=utf-8")
            self.assertEqual(self.client.head(self.path_link("/video/big.M3U8")).status_code, 403)
            self.assertEqual(self.client.head(self.path_link("/video/none.m3u8")).status_code, 404)
        # Read through the driver for a bucket as well: never through a URL.
        bucket = self.make_storage("bucket", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        calls = len(DirectDriver.calls)
        response = self.client.get(self.path_link("/video/a.m3u8", key="bucket"))
        self.assertEqual((response.status_code, response.content), (200, playlist))
        self.assertEqual(len(DirectDriver.calls), calls)


class TargetLifetimeTests(LinksTestCase):
    """What a link redirects to doesn't outlive the link's own checks for long (review M1)."""

    def setUp(self):
        super().setUp()
        self.login()

    def expiry(self, response):
        self.assertEqual(response.status_code, 302, response.content[:300])
        link = signing.verify_url(response["Location"])
        return link.expires

    def test_never_permanent_and_at_most_a_few_minutes(self):
        StorageSource.objects.filter(pk=self.storage.pk).update(permanent_links=True, link_lifetime=7 * 86400)
        expires = self.expiry(self.client.get(self.path_link("/docs/a.txt")))
        self.assertIsNotNone(expires)
        self.assertLessEqual(expires - time.time(), service.TARGET_LIFETIME + 300 + 5)  # signing rounds up to 5 min
        # The browser's own links keep the storage's settings.
        listing = self.listing("/docs")
        self.assertNotIn("e=", next(item["url"] for item in listing["items"] if item["name"] == "a.txt"))

    def test_a_short_link_about_to_expire(self):
        link = self.short()
        ShortLink.objects.filter(pk=link.pk).update(expires_at=timezone.now() + datetime.timedelta(seconds=5))
        expires = self.expiry(self.visitor().get(f"/s/{link.key}"))
        self.assertLessEqual(expires - time.time(), service.TARGET_LIFETIME_MIN + 300 + 5)

    def test_presigned_links_expire_as_soon(self):
        bucket = self.make_storage("bucket", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        response = self.client.get(self.path_link("/docs/a.txt", key="bucket"))
        self.assertIn(f"expires={service.TARGET_LIFETIME}&", response["Location"])

    def test_backends_that_ask_for_each_link(self):
        bucket = self.make_storage("bucket", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=bucket)
        calls = len(DirectDriver.calls)
        with mock.patch.object(transfer, "DEFERRED", transfer.DEFERRED | {"test-direct"}):
            response = self.client.get(self.path_link("/docs/a.txt", key="bucket"))
            link = signing.verify_url(response["Location"])
            self.assertTrue(link.redirect)  # /pd/ asks the backend when opened, with its cache
            self.assertEqual(len(DirectDriver.calls), calls)
            self.assertTrue(self.client.get(response["Location"])["Location"].startswith("https://cdn.example.test/"))


class GuessingTests(LinksTestCase):
    """Unknown short keys have a small budget of their own (review L4)."""

    def test_too_many_unknown_keys(self):
        self.login()
        link = self.short()
        visitor = self.visitor()
        for n in range(ratelimit.MISSES):
            self.assertEqual(visitor.get(f"/s/miss{n:02d}").status_code, 404)
        response = visitor.get(f"/s/{link.key}")  # even a real key now: no way to tell hits from misses
        self.assertEqual(response.status_code, 429)
        self.assertIn("Retry-After", response)
        self.assertEqual(self.visitor().get(f"/s/{link.key}", REMOTE_ADDR="203.0.113.50").status_code, 302)
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 302)  # path links aren't keys


class CreationBudgetTests(LinksTestCase):
    def test_links_a_minute_per_creator(self):
        self.login()
        with mock.patch.object(service, "SHORT_LINKS_PER_MINUTE", 3):
            self.assertEqual(self.make("short", ["/docs/a.txt", "/docs/b.txt"], expires=86400).status_code, 201)
            response = self.make("short", ["/docs/a.txt", "/top.txt"], expires=86400)
            self.assertEqual((response.status_code, response.json()["code"]), (429, "too_many_links"))
            self.assertEqual(self.make("short", ["/top.txt"], expires=86400).status_code, 201)
        self.assertEqual(ShortLink.objects.count(), 3)
