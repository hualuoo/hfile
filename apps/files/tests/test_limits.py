"""Downloads through the site: their limits (apps/files/limits.py, download.py) and the threads they run on
(apps/files/pool.py)."""

import os
import threading
import time
import types
from unittest import mock

from django.core.signals import request_finished
from django.db import close_old_connections
from django.test import RequestFactory, SimpleTestCase, TestCase
from waitress.task import ThreadedTaskDispatcher

from apps.files import download, limits, pool, signing, transfer
from apps.siteconf.models import SiteSettings
from apps.storages.drivers.base import BackendError
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import StorageSource, WhenFull

from .drivers import DirectDriver
from .test_download import DownloadTestCase


class Clock:
    """`time` for apps.files.limits: monotonic() moves on only when sleep() is called."""

    def __init__(self):
        self.now = 1000.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


class CounterTests(SimpleTestCase):
    def test_each_key_up_to_its_limit(self):
        counter = limits.Counter()
        self.assertTrue(counter.take("a", 2))
        self.assertTrue(counter.take("a", 2))
        self.assertFalse(counter.take("a", 2))
        self.assertTrue(counter.take("b", 2))
        counter.give_back("a")
        self.assertTrue(counter.take("a", 2))
        self.assertEqual((counter.used("a"), counter.used("b")), (2, 1))
        for _ in range(5):
            self.assertTrue(counter.take("c", 0))  # 0: no limit
        for _ in range(6):
            counter.give_back("c")
        self.assertEqual(counter.used("c"), 0)


class SpeedTests(SimpleTestCase):
    def test_an_ips_connections_share_its_speed(self):
        clock = Clock()
        speed = limits.Speed()
        with mock.patch.object(limits, "time", clock):
            # Four pieces of 10 KB at 10 KB/s, from any of its connections: one after the other.
            self.assertEqual([speed.wait("1.2.3.4", 10240, 10240) for _ in range(4)], [0.75, 1.75, 2.75, 3.75])
            self.assertEqual(speed.wait("5.6.7.8", 10240, 10240), 0.75)  # another IP has its own
            clock.sleep(10)  # what it reserved is over, and idle time doesn't pile up
            self.assertEqual(speed.wait("1.2.3.4", 1024, 10240), 0)
            self.assertAlmostEqual(speed.wait("1.2.3.4", 10240, 10240), 0.85)


class LimitTestCase(DownloadTestCase):
    def setUp(self):
        super().setUp()
        for each in (limits.storages, limits.addresses, limits.speeds):
            each.reset()
            self.addCleanup(each.reset)

    def going(self, url, **headers):
        """A download under way: its answer has started, and isn't read to the end."""
        response = self.client.get(url, **headers)
        self.assertIn(response.status_code, (200, 206))
        self.addCleanup(self.finish, response)
        return response

    def finish(self, response):
        b"".join(response.streaming_content)  # the end closes the answer (the test client's wrapper)

    def site(self, **values):
        site = SiteSettings.load()
        for name, value in values.items():
            setattr(site, name, value)
        site.save()


class StorageLimitTests(LimitTestCase):
    def test_a_storage_sends_as_many_at_once_as_it_may(self):
        self.storage.proxy_connections = 2
        self.storage.save()
        url = self.link("/docs/data.bin")
        first = self.going(url)
        second = self.going(url, HTTP_RANGE="bytes=0-9")
        self.assertEqual(limits.storages.used(self.storage.pk), 2)
        response, body = self.fetch(url)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response["Retry-After"], "5")
        self.assertIn("这个存储源同时发送的下载已经满了", body.decode())
        self.finish(first)
        response, body = self.fetch(url)
        self.assertEqual((response.status_code, body), (200, self.data))
        self.finish(second)
        self.assertEqual((limits.storages.used(self.storage.pk), limits.addresses.used("127.0.0.1")), (0, 0))

    def test_what_sends_no_body_keeps_no_place(self):
        self.storage.proxy_connections = 1
        self.storage.save()
        url = self.link("/docs/data.bin")
        self.assertEqual(self.client.head(url).status_code, 200)
        self.assertEqual(self.fetch(url, HTTP_RANGE="bytes=9999-")[0].status_code, 416)
        self.assertEqual(self.fetch(self.link("/docs/nope.bin"))[0].status_code, 404)
        with mock.patch.object(LocalDriver, "open", side_effect=BackendError("坏了")):
            with self.assertLogs("hfile.files", "WARNING"):
                self.assertEqual(self.fetch(url)[0].status_code, 500)
        self.assertEqual(limits.storages.used(self.storage.pk), 0)
        response, body = self.fetch(url)
        self.assertEqual((response.status_code, body), (200, self.data))

    def test_a_client_that_goes_away_gives_its_place_back(self):
        url = self.link("/docs/data.bin")
        key, path, _ = signing.parse_url(url)
        response = download.serve(RequestFactory().get(url), key, path)  # as the WSGI server runs it
        self.assertEqual(limits.storages.used(self.storage.pk), 1)
        # (request_finished would close the database connection of the test's transaction.)
        request_finished.disconnect(close_old_connections)
        try:
            response.close()
        finally:
            request_finished.connect(close_old_connections)
        self.assertEqual(limits.storages.used(self.storage.pk), 0)

    def test_a_storage_without_a_link_of_its_own_can_only_refuse(self):
        self.storage.proxy_connections = 1
        self.storage.proxy_when_full = WhenFull.LINK  # not through the API, which keeps it "refuse"
        self.storage.save()
        url = self.link("/docs/data.bin")
        self.going(url)
        self.assertEqual(self.fetch(url)[0].status_code, 429)
        self.assertEqual(transfer.media_sources(self.storage), ())


class AddressLimitTests(LimitTestCase):
    def test_an_ip_has_as_many_at_once_as_it_may(self):
        self.site(proxy_ip_connections=1)
        url = self.link("/docs/data.bin")
        first = self.going(url, REMOTE_ADDR="2001:db8::1")
        response, body = self.fetch(url, REMOTE_ADDR="2001:db8::2")  # the same /64
        self.assertEqual((response.status_code, response["Retry-After"]), (429, "5"))
        self.assertIn("你同时进行的下载已经满了", body.decode())
        self.assertEqual(self.fetch(url, REMOTE_ADDR="2001:db8:0:1::1")[0].status_code, 200)  # another /64
        self.assertEqual(limits.storages.used(self.storage.pk), 1)  # the refused one took nothing
        self.finish(first)
        self.assertEqual(self.fetch(url, REMOTE_ADDR="2001:db8::2")[0].status_code, 200)

    def test_an_ips_downloads_go_at_its_speed(self):
        data = os.urandom(1024 * 1024)
        self.files({"big.bin": data})
        self.site(proxy_ip_speed=100)  # KB/s
        clock = Clock()
        with mock.patch.object(limits, "time", clock):
            response, body = self.fetch(self.link("/big.bin"))
        self.assertEqual(body, data)
        self.assertAlmostEqual(sum(clock.slept), len(data) / (100 * 1024) - limits.BURST, places=6)
        self.assertGreater(len(clock.slept), 70)  # in pieces of a few KB, not in big jumps


class FallbackTests(LimitTestCase):
    """A storage with links of its own (S3, OneDrive, 115), with 代理下载 on."""

    storage_type = "test-direct"

    def setUp(self):
        super().setUp()
        DirectDriver.calls = []
        self.storage.proxy_download = True
        self.storage.proxy_connections = 1
        self.storage.save()

    def test_a_full_storage_sends_to_its_own_link_when_set_to(self):
        url = self.link("/docs/data.bin")
        self.going(url)
        self.assertEqual(self.fetch(url)[0].status_code, 429)  # "refuse", the default
        self.assertEqual(transfer.media_sources(self.storage), ())
        self.storage.proxy_when_full = WhenFull.LINK
        self.storage.save()
        response = self.client.get(url + "&dl=1", HTTP_USER_AGENT="Mozilla/5.0 test")
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].startswith("https://cdn.example.test/docs/data.bin?expires="), response["Location"])
        self.assertEqual((response["Cache-Control"], response["Referrer-Policy"]), ("private, no-store", "no-referrer"))
        self.assertEqual(DirectDriver.calls, [("/docs/data.bin", "data.bin", "Mozilla/5.0 test")])
        self.assertEqual(transfer.media_sources(self.storage), ("http:", "https:"))  # a gallery's pictures may go there
        # An IP's own limit isn't got round that way.
        self.site(proxy_ip_connections=1)
        self.assertEqual(self.fetch(url)[0].status_code, 429)

    def test_a_full_storage_doesnt_send_an_https_page_to_an_http_link(self):
        self.storage.proxy_when_full = WhenFull.LINK
        self.storage.save()
        url = self.link("/docs/data.bin")
        self.going(url)
        with mock.patch.object(DirectDriver, "scheme", "http"):
            self.assertEqual(self.client.get(url, secure=True).status_code, 429)  # as without a link of its own
            self.assertEqual(self.client.get(url).status_code, 302)  # an http page: the backend's link

    def test_onedrives_and_115s_links_are_kept_and_counted(self):
        self.storage.proxy_when_full = WhenFull.LINK
        self.storage.save()
        url = self.link("/docs/data.bin")
        self.going(url)
        with mock.patch.object(transfer, "DEFERRED", frozenset({"test-direct"})):
            for _ in range(2):
                self.assertEqual(self.client.get(url).status_code, 302)
            self.assertEqual(len(DirectDriver.calls), 1)  # asked once, then kept
            with mock.patch.object(download, "REDIRECT_MISSES", 0):  # its budget spent: 429
                self.assertEqual(self.fetch(self.link("/docs/a.txt"))[0].status_code, 429)


class Dispatcher:
    """waitress's ThreadedTaskDispatcher as far as the pool uses it."""

    def __init__(self):
        self.tasks = []
        self.count = 0

    def add_task(self, task):
        self.tasks.append(task)

    def set_thread_count(self, count):
        self.count = count

    def shutdown(self, timeout=5):
        self.count = 0


class PoolTests(TestCase):
    def setUp(self):
        self.pages = Dispatcher()
        self.server = types.SimpleNamespace(task_dispatcher=self.pages)
        with mock.patch("waitress.task.ThreadedTaskDispatcher", Dispatcher):
            pool.install(self.server)
        self.pool = pool._dispatcher
        self.addCleanup(pool.shutdown, 0)

    def test_downloads_go_to_threads_of_their_own(self):
        def channel(path, error=None):
            return types.SimpleNamespace(requests=[types.SimpleNamespace(path=path, error=error)])

        download_, page, unreadable = channel("/pd/disk/a.bin"), channel("/api/v1/site/"), channel("/pd/x", Exception("bad"))
        for each in (download_, page, unreadable):
            self.server.add_task(each)
        self.assertEqual([task.channel for task in self.pool.tasks], [download_])
        self.assertEqual(self.pages.tasks, [page, unreadable])

    def test_as_many_threads_as_the_storages_allow_at_once(self):
        self.assertEqual(self.pool.count, pool.SPARE)
        with self.captureOnCommitCallbacks(execute=True):
            first = StorageSource.objects.create(key="a", name="A", type="local", config={}, proxy_connections=10)
        self.assertEqual(self.pool.count, pool.SPARE + 10)
        with self.captureOnCommitCallbacks(execute=True):
            StorageSource.objects.create(key="b", name="B", type="local", config={})  # 16, the default
        self.assertEqual(self.pool.count, pool.SPARE + 26)
        with self.captureOnCommitCallbacks(execute=True):
            first.delete()
        self.assertEqual(self.pool.count, pool.SPARE + 16)
        with mock.patch.object(pool, "MOST", 20):
            pool.resize()
        self.assertEqual(self.pool.count, 20)
        pool.shutdown(0)
        pool.resize()  # stopped: nothing to resize
        self.assertEqual(self.pool.count, 0)

    def test_a_request_on_the_pool_lets_go_of_its_database_connections(self):
        channel = mock.Mock()
        with mock.patch.object(pool.connections, "close_all") as close_all:
            pool._Task(channel).service()
        channel.service.assert_called_once_with()
        close_all.assert_called_once_with()


class StopWhenDoneTests(SimpleTestCase):
    """pool.stop_when_done(), what `hfile serve` does to its threads when it stops: waitress's own dispatcher,
    with one thread."""

    def setUp(self):
        self.dispatcher = ThreadedTaskDispatcher()
        self.dispatcher.logger = self.dispatcher.queue_logger = mock.Mock()  # its warnings
        self.dispatcher.set_thread_count(1)
        self.answered, self.dropped = [], []

    def task(self, n, service=None):
        def answer():
            time.sleep(0.05)
            self.answered.append(n)

        return types.SimpleNamespace(service=service or answer, cancel=lambda: self.dropped.append(n))

    def test_the_requests_waiting_for_a_thread_are_answered_too(self):
        for n in range(3):
            self.dispatcher.add_task(self.task(n))
        pool.stop_when_done(self.dispatcher, time.monotonic() + 30)
        self.assertEqual((self.answered, self.dropped, self.dispatcher.threads), ([0, 1, 2], [], set()))

    def test_not_past_the_deadline(self):
        began, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def long():
            began.set()
            release.wait(10)
            self.answered.append(0)

        self.dispatcher.add_task(self.task(0, long))
        self.dispatcher.add_task(self.task(1))
        self.assertTrue(began.wait(10))
        started = time.monotonic()
        pool.stop_when_done(self.dispatcher, started + 0.3)
        self.assertLess(time.monotonic() - started, 2)
        self.assertEqual((self.answered, self.dropped), ([], [1]))  # the program ends without them
        release.set()
        deadline = time.monotonic() + 10
        while self.dispatcher.threads and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual((self.answered, self.dispatcher.threads), ([0], set()))
