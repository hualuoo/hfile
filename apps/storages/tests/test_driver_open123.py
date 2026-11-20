"""Tests of the 123 Cloud Drive driver (apps.storages.drivers.open123) against the in-memory fake of its open platform.

HFILE_CONFIG=/tmp/hfile-test/config.toml python -B manage.py test apps.storages.tests.test_driver_open123
"""

from __future__ import annotations

import datetime
import io
import os
import threading
import time
import unittest
import uuid

from apps.storages.drivers import open123
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    NotADirectory,
    NotFound,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_open123 import CLIENT_ID, CLIENT_SECRET, NAME_TAKEN, Fail, Fake123

LIST, CREATE, COMPLETE, SLICE = open123.LIST, open123.CREATE, open123.COMPLETE, open123.SLICE
TOKEN = open123.TOKEN


def driver_class(fake: Fake123, **attrs):
    """Open123Driver pointed at the fake, unpaced and with short waits (the type stays "open123")."""
    attrs = {"api_root": fake.server.url, "rates": {}, "throttle_delay": 0.01, "throttle_wait": 0.5,
             "retry_delay": 0.0, "poll_interval": 0.01, "complete_wait": 2.0, **attrs}
    return type("FakeOpen123Driver", (open123.Open123Driver,), attrs)


def read(driver, path: str, offset: int = 0, length: int | None = None) -> bytes:
    stream = driver.open(path, offset, length)
    try:
        return b"".join(stream)
    finally:
        stream.close()


def names(entries) -> list[str]:
    return sorted(entry.name for entry in entries)


def run_threads(jobs) -> list[Exception]:
    errors: list[Exception] = []

    def work(job):
        try:
            job()
        except Exception as error:  # noqa: BLE001 - collected for the assertion
            errors.append(error)

    threads = [threading.Thread(target=work, args=(job,)) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return errors


class Unseekable:
    """Like a request body: read() only, a few bytes at a time."""

    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._data.read(min(size, 1000) if size and size > 0 else 1000)

    def seekable(self) -> bool:
        return False


class FakeMixin:
    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = Fake123().start()
        cls.Driver = driver_class(cls.fake, **cls.driver_attrs)

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def settings(self, base: str = "/", **config) -> dict:
        """Params with a token the fake gave, saved as the driver saves it."""
        token, expires = self.fake.issue_token()
        return {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "access_token": f"{CLIENT_ID}:{token}",
                "token_expires_at": expires, "base_path": base, **config}

    def new_driver(self, base: str = "/", driver_class=None, **config):
        self.updates: list[dict] = []
        driver = (driver_class or self.Driver)(self.settings(base, **config), on_update=self.updates.append)
        self.addCleanup(driver.close)
        return driver


class Open123ContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract in a fresh folder of a shared account (base_path is that folder)."""

    def make_driver(self):
        base = f"/hfile-tests/{uuid.uuid4().hex[:10]}"
        self.fake.mkdirs(base)
        return self.new_driver(base)


class Open123RootContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract again with base_path "/" on a fresh account for every test, and 256 KiB slices, so that the
    larger files go up in several."""

    def make_driver(self):
        self.fake.reset()
        self.fake.slice_size = 256 * 1024
        return self.new_driver("/")


class Open123Tests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def knob(self, name, value):
        old = getattr(self.fake, name)
        setattr(self.fake, name, value)
        self.addCleanup(setattr, self.fake, name, old)

    def tokens_asked(self) -> int:
        return self.fake.counts[TOKEN]

    # -- tokens

    def test_the_saved_token_is_used(self):
        driver = self.new_driver()
        driver.list("/")
        driver.list("/")
        self.assertEqual(self.tokens_asked(), 0)
        self.assertEqual(self.updates, [])

    def test_a_token_is_got_and_saved(self):
        settings = self.settings()
        del settings["access_token"], settings["token_expires_at"]
        updates = []
        driver = self.Driver(settings, on_update=updates.append)
        self.addCleanup(driver.close)
        self.assertEqual(driver.list("/"), [])
        self.assertEqual(self.tokens_asked(), 1)
        [change] = updates
        self.assertEqual(set(change), {"access_token", "token_expires_at"})
        owner, _, token = change["access_token"].partition(":")
        self.assertEqual(owner, CLIENT_ID)
        self.assertIn(token, self.fake.tokens)
        self.assertLess(abs(change["token_expires_at"] - (time.time() + 30 * 24 * 3600)), 60)
        again = self.Driver({**settings, **change})  # the saved token: no new one
        self.addCleanup(again.close)
        again.list("/")
        self.assertEqual(self.tokens_asked(), 1)

    def test_a_token_about_to_expire_is_replaced(self):
        driver = self.new_driver(token_expires_at=int(time.time()) + 600)  # within renew_before
        old = driver.config["access_token"]
        driver.list("/")
        self.assertEqual(self.tokens_asked(), 1)
        self.assertNotEqual(self.updates[0]["access_token"], old)
        driver.list("/")
        self.assertEqual(self.tokens_asked(), 1)

    def test_a_refused_token_is_replaced_once_for_all_threads(self):
        driver = self.new_driver()
        self.fake.expire_tokens()  # the saved expiry still says it's good
        errors = run_threads([lambda: driver.list("/")] * 8)
        self.assertEqual(errors, [])
        self.assertEqual(self.tokens_asked(), 1)
        self.assertEqual(len(self.updates), 1)

    def test_a_token_logged_out_by_others(self):
        # 123 lets a client ID have 3 tokens in use; a 4th logs the oldest out.
        Driver = driver_class(self.fake, token_gap=1.0)
        driver = self.new_driver(driver_class=Driver)
        for _ in range(3):
            self.fake.issue_token()  # another program with the same client ID
        driver.list("/")  # refused: gets another, which logs out the oldest of the others
        self.assertEqual(self.tokens_asked(), 1)
        for _ in range(3):
            self.fake.issue_token()
        with self.assertRaises(AuthError) as caught:
            driver.list("/")  # refused again at once: not replaced again (that would log another one out)
        self.assertIn("3", caught.exception.message)
        self.assertEqual(self.tokens_asked(), 1)
        time.sleep(1.05)
        self.assertEqual(driver.list("/"), [])  # later it is
        self.assertEqual(self.tokens_asked(), 2)

    def test_refused_credentials_are_remembered(self):
        settings = self.settings(client_secret="not-the-secret")
        del settings["access_token"]
        driver = self.Driver(settings)
        self.addCleanup(driver.close)
        for _ in range(3):
            with self.assertRaises(AuthError):
                driver.list("/")
        self.assertEqual(self.tokens_asked(), 1)

    def test_another_client_ids_token_is_not_used(self):
        token, expires = self.fake.issue_token()
        driver = self.new_driver(access_token=f"0another0client0id:{token}", token_expires_at=expires)
        driver.list("/")
        self.assertEqual(self.tokens_asked(), 1)  # the admin changed the client ID: maybe another account
        self.assertTrue(self.updates[0]["access_token"].startswith(CLIENT_ID + ":"))
        typed = self.new_driver(access_token=self.fake.issue_token()[0], token_expires_at=0)
        typed.list("/")  # a token the admin typed in is used as it is
        self.assertEqual(self.tokens_asked(), 1)

    def test_without_credentials(self):
        driver = self.Driver({"base_path": "/"})
        self.addCleanup(driver.close)
        with self.assertRaises(AuthError):
            driver.list("/")
        self.assertEqual(self.tokens_asked(), 0)

    def test_the_token_service_failing_is_not_a_refusal(self):
        settings = self.settings()
        del settings["access_token"]
        driver = self.Driver(settings)
        self.addCleanup(driver.close)
        self.fake.fail_next[TOKEN] = Fail(1, "内部错误")
        with self.assertRaises(BackendError):
            driver.list("/")
        self.assertEqual(driver.list("/"), [])  # asked again at once: the credentials weren't refused
        self.assertEqual(self.tokens_asked(), 2)

    def test_logged_in_in_too_many_places(self):
        # Code 401, as the screenshot in 123's FAQ shows it (its heading says "imit"). Not a refused token to replace:
        # another one would be one more login.
        driver = self.new_driver()
        message = "tokens number has exceeded the limit"
        self.fake.fail_next[LIST] = Fail(401, message)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertEqual(caught.exception.message, open123._error(401, {"message": message}).message)
        self.assertEqual(self.tokens_asked(), 0)
        self.assertEqual(self.updates, [])
        self.assertEqual(driver.list("/"), [])  # the same token, once the account is logged out elsewhere

    # -- listings and lookups

    def test_paging_and_the_recycle_bin(self):
        self.fake.page_limit = 3
        for i in range(8):
            self.fake.put_file(f"/many/f{i}.txt", b"x" * i)
        folder = self.fake.find("/many").id
        self.fake.add(folder, "gone.txt", False, b"gone").trashed = True
        self.fake.add(folder, "gone", True).trashed = True
        driver = self.new_driver()
        entries = {entry.name: entry for entry in driver.list("/many")}
        self.assertEqual(sorted(entries), [f"f{i}.txt" for i in range(8)])
        self.assertEqual(entries["f5.txt"].size, 5)
        self.assertEqual(self.fake.counts[LIST], 1 + 4)  # "/", then 10 entries 3 a page

    def test_times_are_chinas(self):
        item = self.fake.put_file("/t.txt", b"t")
        item.updated = int(datetime.datetime(2025, 2, 24, 9, 56, 45, tzinfo=datetime.timezone.utc).timestamp())
        entry = self.new_driver().stat("/t.txt")  # listed as "2025-02-24 17:56:45"
        self.assertEqual(entry.modified, datetime.datetime(2025, 2, 24, 9, 56, 45, tzinfo=datetime.timezone.utc))
        self.assertIsNone(open123._time("0000-00-00 00:00:00"))
        self.assertEqual(open123._time("2025-03-23T15:48:37+08:00").timestamp(),
                         datetime.datetime(2025, 3, 23, 7, 48, 37, tzinfo=datetime.timezone.utc).timestamp())

    def test_a_folder_deleted_in_123s_app(self):
        self.fake.put_file("/sub/a.txt", b"a")
        driver = self.new_driver()
        self.assertEqual(names(driver.list("/sub")), ["a.txt"])
        with self.fake.lock:
            self.fake.find("/sub").trashed = True
        with self.assertRaises(NotFound):
            driver.list("/sub")  # 123 doesn't list the old id (5066): looked up again, it's gone
        self.fake.put_file("/sub/b.txt", b"b")  # made again in 123's app
        self.assertEqual(names(driver.list("/")), ["sub"])
        self.assertEqual(names(driver.list("/sub")), ["b.txt"])

    def test_missing_base_path(self):
        driver = self.new_driver("/no/such/base")
        with self.assertRaises(NotFound) as caught:
            driver.check()
        self.assertIn("/no/such/base", caught.exception.message)

    def test_odd_names_left_out(self):
        folder = self.fake.mkdirs("/odd")
        self.fake.add(folder, "a/b", False, b"x")
        self.fake.add(folder, "..", True)
        self.fake.add(folder, "fine.txt", False, b"y")
        self.assertEqual(names(self.new_driver().list("/odd")), ["fine.txt"])

    def test_list_and_stat_errors(self):
        driver = self.new_driver()
        driver.write("/f.txt", io.BytesIO(b"x"), 1)
        with self.assertRaises(NotADirectory):
            driver.list("/f.txt")
        with self.assertRaises(NotADirectory):
            driver.mkdir("/f.txt/inner")
        with self.assertRaises(NotFound):
            driver.stat("/f.txt/inner")

    # -- uploads

    def test_instant_upload(self):
        data = os.urandom(5000)
        driver = self.new_driver()
        driver.write("/a.bin", io.BytesIO(data), len(data))
        self.assertEqual(len(self.fake.slices), 1)
        driver.write("/copy/b.bin", io.BytesIO(data), len(data))  # 123 has these bytes: 秒传
        self.assertEqual(len(self.fake.slices), 1)
        self.assertEqual(self.fake.counts[COMPLETE], 1)
        self.assertEqual(read(driver, "/copy/b.bin"), data)
        driver.write("/empty", io.BytesIO(b""), 0)
        self.assertEqual(read(driver, "/empty"), b"")

    def test_slices(self):
        self.fake.slice_size = 64 * 1024
        driver = self.new_driver()
        small, big = os.urandom(1000), os.urandom(5 * 64 * 1024 + 7)
        driver.write("/small.bin", io.BytesIO(small), len(small))
        self.assertEqual([number for _, number in self.fake.slices], [1])
        driver.write("/big.bin", io.BytesIO(big), len(big))  # the fake checks each slice's size and MD5
        self.assertEqual([number for _, number in self.fake.slices], [1, 1, 2, 3, 4, 5, 6])
        self.assertEqual(read(driver, "/big.bin"), big)
        self.assertEqual(driver.stat("/big.bin").size, len(big))
        self.assertEqual(read(driver, "/small.bin"), small)

    def test_unseekable_data_is_spooled(self):
        self.fake.slice_size = 64 * 1024
        driver = self.new_driver()
        data = os.urandom(150 * 1024)
        driver.write("/s.bin", Unseekable(data), len(data))
        self.assertEqual(read(driver, "/s.bin"), data)
        with self.assertRaises(DriverError):
            driver.write("/short.bin", Unseekable(b"abc"), 10)
        with self.assertRaises(NotFound):
            driver.stat("/short.bin")
        self.assertEqual(self.fake.counts[CREATE], 1)  # the short one never got to 123

    def test_overwrite_puts_the_old_file_in_the_recycle_bin(self):
        driver = self.new_driver()
        driver.write("/o.txt", io.BytesIO(b"first"), 5)
        old = self.fake.find("/o.txt")
        driver.write("/o.txt", io.BytesIO(b"second"), 6)  # duplicate 2: 123 replaces it
        with self.fake.lock:
            self.assertEqual([item.name for item in self.fake.children(0)], ["o.txt"])
            self.assertTrue(old.trashed)
        self.assertEqual(read(driver, "/o.txt"), b"second")

    def test_completion_is_asked_again(self):
        self.fake.complete_busy = 2  # code 20103, twice
        self.fake.complete_pending = 1  # then completed false
        driver = self.new_driver()
        driver.write("/c.bin", io.BytesIO(b"complete"), 8)
        self.assertEqual(self.fake.counts[COMPLETE], 4)
        self.assertEqual(read(driver, "/c.bin"), b"complete")

    def test_completion_that_never_comes(self):
        self.fake.complete_pending = 10 ** 6
        driver = self.new_driver(driver_class=driver_class(self.fake, complete_wait=0.2))
        with self.assertRaises(BackendError):
            driver.write("/n.bin", io.BytesIO(b"never"), 5)
        with self.assertRaises(NotFound):
            driver.stat("/n.bin")

    def test_slices_tried_again_on_the_next_server(self):
        self.fake.slice_size = 64 * 1024
        self.fake.servers = ["http://127.0.0.1:1", self.fake.uploads.url]  # the first can't be reached
        self.fake.slice_fail[2] = 1  # and the second fails slice 2 once
        driver = self.new_driver()
        data = os.urandom(3 * 64 * 1024)
        driver.write("/r.bin", io.BytesIO(data), len(data))
        self.assertEqual([number for _, number in self.fake.slices], [1, 2, 3])
        self.assertEqual(self.fake.counts[SLICE], 4)  # slice 2 twice
        self.assertEqual(read(driver, "/r.bin"), data)

    def test_a_slice_that_keeps_failing(self):
        self.fake.slice_fail[1] = 99
        driver = self.new_driver()
        with self.assertRaises(BackendError) as caught:
            driver.write("/f.bin", io.BytesIO(b"fails"), 5)
        self.assertIn("HTTP 500", caught.exception.message)
        self.assertEqual(self.fake.counts[SLICE], self.Driver.slice_tries)
        with self.assertRaises(NotFound):
            driver.stat("/f.bin")

    def test_upload_servers(self):
        server = open123._upload_server
        self.assertEqual(server("http://openapi-upload.123242.com", secure=True), "https://openapi-upload.123242.com")
        self.assertEqual(server("http://openapi-upload.123242.com:80/", secure=True), "https://openapi-upload.123242.com")
        self.assertEqual(server("https://openapi-upload.123pan.com", secure=True), "https://openapi-upload.123pan.com")
        self.assertEqual(server("http://127.0.0.1:5000", secure=False), "http://127.0.0.1:5000")
        for bad in (None, "", "ftp://example.com", "openapi-upload.123pan.com", 5):
            with self.subTest(bad=bad):
                self.assertIsNone(server(bad, secure=True))

    def test_too_big_for_123(self):
        class Unread:
            def read(self, size=-1):
                raise AssertionError("read")

        driver = self.new_driver(driver_class=driver_class(self.fake, max_file_size=10))
        with self.assertRaises(DriverError):
            driver.write("/big.bin", Unread(), 11)
        self.assertEqual(self.fake.counts[CREATE], 0)

    def test_parallel_writes_share_new_folders(self):
        driver = self.new_driver()
        errors = run_threads([lambda i=i: driver.write(f"/par/sub/f{i}.txt", io.BytesIO(b"%d" % i), len(b"%d" % i))
                              for i in range(6)])
        self.assertEqual(errors, [])
        with self.fake.lock:
            self.assertEqual([item.name for item in self.fake.children(0)], ["par"])
            self.assertEqual([item.name for item in self.fake.children(self.fake.find("/par").id)], ["sub"])
        self.assertEqual(names(driver.list("/par/sub")), [f"f{i}.txt" for i in range(6)])

    # -- copies

    def test_copies_are_123s(self):
        driver = self.new_driver()
        driver.write("/src/a.txt", io.BytesIO(b"a"), 1)
        driver.write("/src/d/b.txt", io.BytesIO(b"bb"), 2)
        driver.write("/src/d/e/c.txt", io.BytesIO(b"ccc"), 3)
        driver.mkdir("/dst")
        creates = self.fake.counts[CREATE]
        driver.copy("/src/a.txt", "/dst")
        driver.copy("/src/d", "/dst")
        self.assertEqual(self.fake.counts[open123.COPY], 3)
        self.assertEqual(self.fake.counts[CREATE], creates)  # nothing uploaded again
        self.assertEqual(read(driver, "/dst/a.txt"), b"a")
        self.assertEqual(read(driver, "/dst/d/e/c.txt"), b"ccc")
        self.assertEqual(names(driver.list("/dst/d")), ["b.txt", "e"])
        self.assertEqual(read(driver, "/src/d/b.txt"), b"bb")

    # -- downloads

    def test_downloads_go_through_the_site(self):
        driver = self.new_driver()
        data = os.urandom(3000)
        driver.write("/l.bin", io.BytesIO(data), len(data))
        self.assertIsNone(driver.download_url("/l.bin", filename="l.bin", user_agent="Mozilla/5.0"))
        self.assertEqual(read(driver, "/l.bin"), data)
        self.assertEqual(read(driver, "/l.bin", 100, 50), data[100:150])
        self.assertEqual(self.fake.counts[open123.DOWNLOAD], 1)  # the link is reused
        self.fake.expire_links()
        self.assertEqual(read(driver, "/l.bin", 2990), data[2990:])  # 403: another link
        self.assertEqual(self.fake.counts[open123.DOWNLOAD], 2)
        self.assertEqual(self.fake.leaks, [])  # the token never went to the CDN

    def test_a_replaced_file_gets_another_link(self):
        # Whether a file 123 replaces keeps its id isn't documented: a link is reused for the same id, size and time.
        self.fake.replace_keeps_id = True
        driver = self.new_driver()
        driver.write("/r.bin", io.BytesIO(b"old"), 3)
        self.assertEqual(read(driver, "/r.bin"), b"old")
        driver.write("/r.bin", io.BytesIO(b"newer"), 5)
        self.assertEqual(read(driver, "/r.bin"), b"newer")
        self.assertEqual(self.fake.counts[open123.DOWNLOAD], 2)

    def test_the_days_download_traffic_used_up(self):
        driver = self.new_driver()
        driver.write("/t.bin", io.BytesIO(b"traffic"), 7)
        self.fake.traffic_used_up = True
        with self.assertRaises(DriverError) as caught:
            read(driver, "/t.bin")
        self.assertNotIsInstance(caught.exception, BackendError)  # trying again soon won't help
        self.assertIn("5113", caught.exception.message)

    # -- pacing and errors

    def test_throttled_calls_wait(self):
        driver = self.new_driver()
        self.fake.throttle[LIST] = 2  # code 429, twice
        self.assertEqual(driver.list("/"), [])
        self.assertEqual(self.fake.counts[LIST], 3)
        self.fake.fail_next[LIST] = 429  # the gateway's HTTP 429, with Retry-After: 0
        self.assertEqual(driver.list("/"), [])
        self.fake.throttle[LIST] = 10 ** 6
        with self.assertRaises(BackendError):
            driver.list("/")  # throttled for longer than throttle_wait

    def test_calls_are_paced_below_123s_limits(self):
        self.fake.qps = {LIST: 6}
        driver = self.new_driver(driver_class=driver_class(self.fake, rates={LIST: 5.0}))
        start = time.monotonic()
        for _ in range(8):
            driver.list("/")
        self.assertGreaterEqual(time.monotonic() - start, 7 / 5 - 0.05)
        self.assertEqual(self.fake.throttled[LIST], 0)
        self.assertLessEqual(set(open123.Open123Driver.rates), {
            open123.TOKEN, LIST, open123.MKDIR, open123.RENAME, open123.MOVE, open123.TRASH, open123.COPY,
            open123.DOWNLOAD, CREATE, COMPLETE})
        for api, rate in open123.Open123Driver.rates.items():
            self.assertLessEqual(rate, 10.0, api)

    def test_the_queue_has_a_limit(self):
        # A burst can't hold the site's threads for ever: a call whose turn is more than queue_wait away fails.
        driver = self.new_driver(driver_class=driver_class(self.fake, rates={LIST: 1.0}, queue_wait=1.5))
        driver.list("/")  # takes the first turn
        errors = run_threads([lambda: driver.list("/")] * 4)
        self.assertEqual(len(errors), 3)
        self.assertTrue(all(isinstance(error, BackendError) for error in errors))

    def test_codes_and_messages(self):
        error = open123._error
        self.assertIsInstance(error(401, {"message": "token不存在"}), AuthError)  # a refused token, whatever the words
        self.assertIsInstance(error(5066, {"message": ""}), NotFound)
        self.assertIsInstance(error(1, {"message": "该目录下已经有同名文件夹"}), AlreadyExists)  # the words over code 1
        self.assertIsInstance(error(1, {"message": "内部错误"}), BackendError)
        self.assertIsInstance(error(429, {"message": "请求太频繁"}), BackendError)
        self.assertNotIsInstance(error(5113, {"message": "流量"}), BackendError)
        self.assertIs(type(error(777, {"message": "x"})), DriverError)

    def test_error_mapping(self):
        driver = self.new_driver()
        driver.mkdir("/x")
        self.fake.fail_next[open123.MKDIR] = Fail(NAME_TAKEN, "该目录下已经有同名文件夹,无法进行创建")  # made meanwhile
        with self.assertRaises(AlreadyExists):
            driver.mkdir("/y")
        self.fake.fail_next[open123.TRASH] = Fail(5066, "文件不存在")
        with self.assertRaises(NotFound):
            driver.delete("/x")
        self.fake.fail_next[open123.RENAME] = Fail(1, "内部错误")
        with self.assertRaises(BackendError):
            driver.rename("/x", "z")
        driver.mkdir("/dst")
        self.fake.fail_next[open123.MOVE] = Fail(12345, "奇怪的错误")
        with self.assertRaises(DriverError) as caught:
            driver.move("/x", "/dst")
        self.assertIs(type(caught.exception), DriverError)
        self.assertIn("12345", caught.exception.message)
        for injected, text in ((500, "HTTP 500"), (404, "HTTP 404"), (b"not json", "123")):
            self.fake.fail_next[LIST] = injected
            with self.subTest(injected=injected), self.assertRaises(BackendError) as caught:
                driver.list("/")
            self.assertIn(text, caught.exception.message)
        self.assertEqual(names(driver.list("/")), ["dst", "x"])
        unreachable = driver_class(self.fake, api_root="http://127.0.0.1:1")(self.settings())
        self.addCleanup(unreachable.close)
        with self.assertRaises(BackendError):
            unreachable.list("/")

    # -- the form

    def test_validate_config(self):
        validate = open123.Open123Driver.validate_config
        self.assertEqual(validate({"client_id": CLIENT_ID, "base_path": "/a", "token_expires_at": 0}), {})
        self.assertEqual(validate({"client_id": " 123456789 "}), {})
        errors = validate({"client_id": "", "base_path": "/a/../b", "token_expires_at": -1})
        self.assertEqual(set(errors), {"client_id", "base_path", "token_expires_at"})
        for bad in (None, "abc def", "客户端", "x" * 129):
            with self.subTest(bad=bad):
                self.assertEqual(set(validate({"client_id": bad})), {"client_id"})

    def test_capabilities(self):
        cls = open123.Open123Driver
        self.assertEqual(cls.type, "open123")
        self.assertTrue(cls.can_range and cls.can_move and cls.can_copy and cls.can_write)
        self.assertFalse(cls.can_direct_download or cls.can_direct_upload or cls.can_search or cls.oauth)
        self.assertEqual(cls.helpers, {})
        self.assertEqual({param.key for param in cls.params if param.secret},
                         {"client_secret", "access_token", "token_expires_at"})
        self.assertEqual(cls.setup_params, ("client_id", "client_secret"))
        self.assertEqual(sorted(cls.forbidden_chars), sorted('"\\:*?|><'))


if __name__ == "__main__":
    unittest.main()
