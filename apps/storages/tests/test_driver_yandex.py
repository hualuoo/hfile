"""Tests of the Yandex Disk driver (apps.storages.drivers.yandex) against the fake of its API.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_yandex
"""

from __future__ import annotations

import io
import threading
import time
import unittest
import urllib.parse

import httpx

from apps.storages.drivers import paths, yandex
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotFound
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import FakeServer, Response
from apps.storages.tests.fake_yandex import FakeYandex


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeYandex().start()
        cls.Driver = type("FakeYandexDiskDriver", (yandex.YandexDiskDriver,),
                          {"api_root": cls.fake.api_url, "token_endpoint": cls.fake.token_url, "poll_interval": 0.0})

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, **config):
        self.updates = []
        settings = {"client_id": self.fake.client_id, "client_secret": self.fake.client_secret,
                    "refresh_token": self.fake.issue_refresh_token(), **config}
        driver = self.Driver(settings, on_update=self.updates.append)
        self.addCleanup(driver.close)
        return driver


class YandexContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        return self.new_driver()


class YandexBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        with self.fake.lock:
            self.fake.tree["/apps"] = self.fake._node(True)
            self.fake.tree["/apps/hfile"] = self.fake._node(True)
        return self.new_driver(base_path="/apps/hfile")


class YandexTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def put(self, driver, path, data):
        driver.write(path, io.BytesIO(data), len(data))

    def test_authorizing(self):
        url = self.Driver.authorize_url({"client_id": "yandex-app"}, redirect_uri="https://files.example/cb", state="s1")
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual((query["client_id"], query["redirect_uri"], query["state"], query["response_type"]),
                         (["yandex-app"], ["https://files.example/cb"], ["s1"], ["code"]))
        config = {"client_id": self.fake.client_id, "client_secret": self.fake.client_secret}
        saved = self.Driver.exchange_code(config, code=self.fake.issue_code(), redirect_uri="https://files.example/cb")
        self.assertIn(saved["refresh_token"], self.fake.refresh_tokens)
        with self.assertRaises(AuthError):
            self.Driver.exchange_code(config, code="used-or-wrong", redirect_uri="https://files.example/cb")
        with self.assertRaises(AuthError):
            self.Driver.authorize_url({}, redirect_uri="https://files.example/cb", state="s")

    def test_a_refused_token_is_renewed_once(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        refreshes = self.fake.refreshes
        self.fake.expire_tokens()
        self.assertEqual([e.name for e in d.list("/")], ["a.txt"])
        self.assertEqual(self.fake.refreshes, refreshes + 1)
        with self.assertRaises(AuthError):
            self.new_driver(refresh_token="revoked").list("/")

    def test_folders_over_several_pages(self):
        d = self.new_driver()
        d.page_size = 3
        for n in range(8):
            self.put(d, f"/many/{n}.txt", b"x")
        self.assertEqual(sorted(e.name for e in d.list("/many")), [f"{n}.txt" for n in range(8)])

    def test_folder_operations_that_run_in_the_background(self):
        d = self.new_driver()
        self.put(d, "/src/a.txt", b"a")
        d.mkdir("/dst")
        d.copy("/src", "/dst")  # 202, an operation asked about until "success"
        self.assertEqual(d.open("/dst/src/a.txt").read(), b"a")
        d.delete("/src")  # an operation too
        d.move("/dst/src", "/")
        with self.assertRaises(AlreadyExists):
            d.copy("/src", "/")  # into its own folder: the name is taken, by itself
        d.delete("/dst")
        with self.assertRaises(NotFound):
            d.stat("/dst")

    def test_links_go_without_the_token(self):
        d = self.new_driver()
        data = bytes(range(256)) * 50
        self.put(d, "/dl/文件 1.bin", data)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertEqual(httpx.get(url).content, data)
        self.assertEqual(d.open("/dl/文件 1.bin", 10, 20).read(), data[10:30])
        self.assertTrue(all(r["authorization"] is None for r in self.fake.link_requests))

    def test_throttling_is_waited_out(self):
        d = self.new_driver()
        self.fake.throttle = 2
        self.assertEqual(d.list("/"), [])

    # -- operations (202)

    def test_operation_links_in_the_documented_form(self):
        """The docs' 202 examples give the link as .../operations?id=<id> (reference/copy, move, delete): it is asked
        about like .../operations/<id>, until the operation is over."""
        d = self.new_driver()
        self.put(d, "/src/a.txt", b"a")
        d.mkdir("/dst")
        self.fake.operation_form, self.fake.operation_polls = "query", 2
        d.copy("/src", "/dst")
        self.assertEqual(self.fake.operation_requests, 3)  # in progress twice, then success
        self.assertEqual(d.open("/dst/src/a.txt").read(), b"a")

    def test_a_failed_operation_is_an_error(self):
        d = self.new_driver()
        self.put(d, "/src/a.txt", b"a")
        d.mkdir("/dst")
        self.fake.operation_form, self.fake.operation_result = "query", "failed"
        with self.assertRaises(DriverError) as caught:
            d.copy("/src", "/dst")
        self.assertNotIsInstance(caught.exception, BackendError)

    def test_an_operation_link_elsewhere_isnt_followed(self):
        """The link would get the token: one on another host isn't asked; the paths tell when the change is done."""
        elsewhere = []

        def record(request):
            elsewhere.append(request.header("Authorization"))
            return Response(404)

        other = FakeServer(record).start()
        self.addCleanup(other.close)
        d = self.new_driver()
        d.operation_wait, d.poll_interval = 0.3, 0.02
        self.put(d, "/a/x.txt", b"x")
        self.put(d, "/b/y.txt", b"y")
        self.fake.operation_href = other.url + "/v1/disk/operations/1"
        d.delete("/a")  # gone at once in the fake
        with self.assertRaises(NotFound):
            d.stat("/a")
        self.fake.apply_operations = False  # Yandex never does it
        d.mkdir("/dst")
        with self.assertRaises(BackendError):  # not done when the time is up: not taken for done
            d.copy("/b", "/dst")
        self.assertEqual(elsewhere, [])

    # -- tokens

    def test_the_access_token_is_kept_for_the_next_instance(self):
        """It lasts a year: an instance made later (a restart, new settings) uses it instead of refreshing again."""
        d = self.new_driver()
        d.list("/")
        saved = {key: value for update in self.updates for key, value in update.items()}
        self.assertEqual(sorted(saved), ["access_token", "refresh_token", "token_expires_at"])
        self.assertGreater(saved["token_expires_at"], time.time() + 30 * 24 * 3600)
        refreshes = self.fake.refreshes
        later = self.new_driver(**saved)
        later.list("/")
        self.assertEqual(self.fake.refreshes, refreshes)

    def test_two_instances_refresh_one_at_a_time(self):
        """Each refresh gives a new refresh token; here the old one stops working. Two instances of one storage
        (one token_lock, new tokens handed on, as apps/storages/instances.py does) start at once: one refreshes,
        the other takes its tokens."""
        self.fake.retire_refresh_tokens, self.fake.token_delay = True, 0.2
        self.addCleanup(setattr, self.fake, "retire_refresh_tokens", False)
        self.addCleanup(setattr, self.fake, "token_delay", 0.0)
        settings = {"client_id": self.fake.client_id, "client_secret": self.fake.client_secret,
                    "refresh_token": self.fake.issue_refresh_token()}
        lock = threading.RLock()
        drivers = []

        def hand_on(me):
            def on_update(changes):
                for other in drivers:
                    if other is not me:
                        with other.token_lock:
                            other.config.update(changes)
            return on_update

        for _ in range(2):
            driver = self.Driver(dict(settings))
            driver._on_update = hand_on(driver)
            driver.token_lock = lock
            self.addCleanup(driver.close)
            drivers.append(driver)
        results = [None, None]
        start = threading.Barrier(2)
        refreshes = self.fake.refreshes

        def work(i):
            start.wait()
            try:
                drivers[i].list("/")
                results[i] = "ok"
            except AuthError as error:
                results[i] = error.message

        threads = [threading.Thread(target=work, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, ["ok", "ok"])
        self.assertEqual(self.fake.refreshes - refreshes, 1)

    # -- names

    def test_names_of_up_to_255_characters(self):
        """Yandex counts a name's characters, at most 255 (reference/upload), not its bytes."""
        d = self.new_driver()
        name = "名" * 100  # 300 bytes
        paths.check_name(name, forbidden=d.forbidden_chars, max_bytes=d.max_name_bytes)  # the site's check
        self.put(d, "/" + name, b"x")
        self.assertEqual([e.name for e in d.list("/")], [name])
        long = "a" * 256
        for call in (lambda: d.mkdir("/" + long), lambda: d.mkdir("/" + long + "/inner"),
                     lambda: self.put(d, "/" + long, b"x"), lambda: d.rename("/" + name, long)):
            with self.assertRaises(InvalidPath):
                call()
        self.assertEqual([e.name for e in d.list("/")], [name])


if __name__ == "__main__":
    unittest.main()
