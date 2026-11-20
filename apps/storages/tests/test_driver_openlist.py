"""Tests of the OpenList / AList driver (apps.storages.drivers.openlist) against the fake of their API.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_openlist
"""

from __future__ import annotations

import io
import time
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages.drivers import http
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, NotADirectory, NotFound
from apps.storages.drivers.openlist import OpenListDriver, OpenListShareDriver
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, DriverContract, ReadOnlyContract
from apps.storages.tests.fake_cloud_http import FakeServer, Response
from apps.storages.tests.fake_openlist import FakeOpenList


def driver_class(**attrs):
    """OpenListDriver with other tuning (the type stays "openlist")."""
    return type("FakeOpenListDriver", (OpenListDriver,), attrs)


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeOpenList().start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, driver_class=OpenListDriver, **config):
        settings = {"url": self.fake.url + "/", "username": "admin", "password": "pw-admin", **config}
        driver = driver_class({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class OpenListContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        return self.new_driver()


class OpenListBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        with self.fake.lock:
            self.fake._make_dirs("/mnt/files")
        return self.new_driver(base_path="/mnt/files")


class OpenListAccountBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract for an account whose OpenList user has a base path of its own (/home/alice)."""

    def make_driver(self):
        self.fake.reset()
        self.fake.bases["alice"] = "/home/alice"
        with self.fake.lock:
            self.fake._make_dirs("/home/alice")
        return self.new_driver(username="alice", password="pw-alice")


class OpenListTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def put(self, driver, path, data):
        driver.write(path, io.BytesIO(data), len(data))

    def test_an_expired_token_logs_in_again(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        logins = self.fake.calls.count("/api/auth/login")
        self.fake.expire_tokens()
        self.assertEqual([e.name for e in d.list("/")], ["a.txt"])
        self.assertEqual(self.fake.calls.count("/api/auth/login"), logins + 1)
        self.fake.expire_tokens()
        self.put(d, "/b.txt", b"b")  # an upload too, from the start of the data
        self.assertEqual(d.open("/b.txt").read(), b"b")

    def test_a_fixed_token(self):
        d = self.new_driver(username=None, password=None, token=self.fake.admin_token)
        self.put(d, "/t.txt", b"t")
        self.assertNotIn("/api/auth/login", self.fake.calls)
        with self.assertRaises(AuthError):
            self.new_driver(username=None, password=None, token="wrong").list("/")
        with self.assertRaises(DriverError):
            self.new_driver(password="wrong").list("/")

    def test_a_refused_login_is_remembered(self):
        """A refused login (AList's code 400, a 2FA account's 402, a locked-out IP's 429) is an AuthError, and isn't
        tried again for longer than the servers lock logins after failed attempts (5 minutes)."""
        d = self.new_driver(password="wrong")
        for _ in range(8):
            with self.assertRaises(AuthError):
                d.list("/")
        self.assertEqual(self.fake.calls.count("/api/auth/login"), 1)
        later = time.monotonic() + 6 * 60
        with mock.patch("apps.storages.drivers._cloud.time", mock.Mock(monotonic=lambda: later)):
            with self.assertRaises(AuthError):
                d.list("/")
        self.assertEqual(self.fake.calls.count("/api/auth/login"), 1)
        for code, message in ((402, "Invalid 2FA code"), (429, "Too many unsuccessful sign-in attempts")):
            self.fake.login_refusal = (code, message)
            with self.assertRaises(AuthError):
                self.new_driver().list("/")

    def test_the_guest_account(self):
        self.put(self.new_driver(), "/pub.txt", b"public")
        guest = self.new_driver(username=None, password=None)
        self.assertEqual([e.name for e in guest.list("/")], ["pub.txt"])
        with self.assertRaises(DriverError):
            guest.mkdir("/new")  # the guest may only read
        self.fake.guest = False
        with self.assertRaises(AuthError):
            self.new_driver(username=None, password=None).list("/")

    def test_folder_passwords(self):
        self.put(self.new_driver(), "/locked/in.txt", b"in")  # the admin needs none
        alice = self.new_driver(username="alice", password="pw-alice")
        with self.assertRaises(DriverError):
            alice.list("/locked")
        opened = self.new_driver(username="alice", password="pw-alice", folder_password=self.fake.locked_password)
        self.assertEqual([e.name for e in opened.list("/locked")], ["in.txt"])
        self.assertEqual(opened.open("/locked/in.txt").read(), b"in")

    def test_downloads_go_without_the_token(self):
        d = self.new_driver()
        data = bytes(range(256)) * 100
        self.put(d, "/dl/文件 1.bin", data)
        url = d.download_url("/dl/文件 1.bin", filename="文件 1.bin")
        self.assertIn("sign=", url)
        self.assertEqual(httpx.get(url).content, data)
        stream = d.open("/dl/文件 1.bin", 100, 50)
        self.assertEqual(stream.read(), data[100:150])
        self.assertEqual(self.fake.download_requests[-1], {"authorization": None, "range": "bytes=100-149"})

    def test_an_account_with_a_base_path(self):
        """The API takes paths below the user's own base path; /d the server's full path, as OpenList's web UI
        builds it (me().base_path joined in)."""
        self.fake.bases["alice"] = "/alice"
        with self.fake.lock:
            self.fake._make_dirs("/alice")
            self.fake.tree["/alice/report.txt"] = self.fake._node(False, b"alice's report")
            self.fake.tree["/report.txt"] = self.fake._node(False, b"another file, outside alice's folder")
        alice = self.new_driver(username="alice", password="pw-alice")
        self.assertEqual([e.name for e in alice.list("/")], ["report.txt"])
        self.assertEqual(alice.open("/report.txt").read(), b"alice's report")
        self.assertEqual(alice.open("/report.txt", 2, 5).read(), b"ice's")
        url = alice.download_url("/report.txt", filename="report.txt")
        self.assertEqual(urllib.parse.urlsplit(url).path, "/d/alice/report.txt")
        self.assertEqual(httpx.get(url).content, b"alice's report")
        self.fake.sign_all = False  # without signs, the old link would give the other file
        self.assertEqual(alice.open("/report.txt").read(), b"alice's report")
        self.fake.sign_all = True
        self.fake.bases["alice"] = "/alice2"  # the admin gave alice another folder: /d answers 401, /api/me again
        with self.fake.lock:
            self.fake._make_dirs("/alice2")
            self.fake.tree["/alice2/report.txt"] = self.fake._node(False, b"the new folder's report")
        self.assertEqual(alice.open("/report.txt").read(), b"the new folder's report")

    def test_uploads_wait_for_openlists_own_upload(self):
        """OpenList answers an upload once its own storage has the file (a cloud drive it buffers the upload for
        first): the PUT waits longer than the other calls, a while and more for each MiB."""
        d = self.new_driver(driver_class(upload_wait=5.0))
        d._http.close()
        d._http = http.client(timeout=1.0)  # every other call gives up after a second
        self.fake.put_delay = 2.0
        self.put(d, "/slow.bin", b"x" * 1000)
        self.fake.put_delay = 0.0
        self.assertEqual(d.open("/slow.bin").read(), b"x" * 1000)

    def test_lookups_ask_for_no_download_link(self):
        """/api/fs/get of a file asks its storage for a download link: lookups go through the folder's listing."""
        d = self.new_driver()
        self.put(d, "/flagged.mp4", b"x" * 10)
        self.put(d, "/dst/keep.txt", b"k")
        self.fake.no_links = {"/flagged.mp4", "/other.mp4", "/dst/other.mp4"}  # their storage gives no links
        self.assertEqual(d.stat("/flagged.mp4").size, 10)
        self.put(d, "/flagged.mp4", b"y" * 5)  # over it
        d.rename("/flagged.mp4", "other.mp4")
        d.move("/other.mp4", "/dst")
        d.copy("/dst/other.mp4", "/")
        d.delete("/dst/other.mp4")
        self.assertEqual(sorted(e.name for e in d.list("/")), ["dst", "other.mp4"])
        self.assertEqual(d.open("/other.mp4").read(), b"y" * 5)

    def test_moves_that_openlist_runs_as_tasks(self):
        """Between two of OpenList's storages a move or copy is a task: it is waited for, and its failure told."""
        d = self.new_driver(driver_class(poll_interval=0.01))
        self.put(d, "/a.txt", b"a")
        self.put(d, "/b.txt", b"b")
        self.put(d, "/dst/keep.txt", b"k")
        d.mkdir("/dst2")
        self.fake.task_moves = True
        d.move("/a.txt", "/dst")
        self.assertEqual(sorted(e.name for e in d.list("/dst")), ["a.txt", "keep.txt"])
        d.copy("/b.txt", "/dst")
        self.assertEqual(d.open("/dst/b.txt").read(), b"b")
        self.fake.task_error = "the target storage refused it"
        with self.assertRaises(DriverError) as caught:
            d.move("/b.txt", "/dst2")
        self.assertNotIsInstance(caught.exception, BackendError)
        self.assertIn("the target storage refused it", caught.exception.message)
        self.fake.task_error = None
        self.fake.task_polls = 10 ** 6  # still running when the wait is over
        with self.assertRaises(BackendError):
            self.new_driver(driver_class(task_wait=0.2, poll_interval=0.05)).move("/b.txt", "/dst2")

    def test_uploads_say_the_files_type(self):
        d = self.new_driver()
        self.put(d, "/v.mp4", b"v")
        self.put(d, "/x.unknown-type", b"x")
        self.assertEqual([r["content_type"] for r in self.fake.put_requests], ["video/mp4", None])

    def test_answers_that_arent_openlists(self):
        d = self.new_driver()
        d.list("/")
        self.fake.put_status = 413  # a proxy in front of OpenList
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/big.bin", b"x" * 100)
        self.assertNotIsInstance(caught.exception, BackendError)
        self.assertIn("413", caught.exception.message)
        self.fake.put_status = 0
        self.fake.fail_status = 401  # a proxy that wants its own login
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("401", caught.exception.message)

    def test_changing_only_the_case_on_a_storage_that_ignores_it(self):
        """OpenList's Local storage on Windows finds "case.txt" as "Case.txt", so the rename goes through a third name."""
        d = self.new_driver()
        self.put(d, "/case.txt", b"c")
        self.fake.ignore_case = True
        d.rename("/case.txt", "Case.txt")
        self.assertEqual([e.name for e in d.list("/")], ["Case.txt"])
        self.assertEqual(d.open("/Case.txt").read(), b"c")

    def test_error_messages(self):
        d = self.new_driver()
        for message in ("the share does not exist", "failed get obj: failed to get obj: NoSuchKey: The specified key does not exist.",
                        "storage not init: root folder /data not exists", "failed get objs: failed get dir: object not found"):
            with self.subTest(message=message):
                self.assertIsInstance(d._error(500, message), NotFound)
        self.assertIsInstance(d._error(403, "file [a.txt] exists"), AlreadyExists)
        self.assertIsInstance(d._error(500, "failed get objs: not a folder"), NotADirectory)

    def test_settings(self):
        check = OpenListDriver.validate_config
        self.assertEqual(check({"url": "https://alist.example.com", "username": "a", "password": "b"}), {})
        self.assertEqual(check({"url": "https://alist.example.com"}), {})  # the guest account
        self.assertIn("url", check({"url": "alist.example.com"}))
        self.assertIn("password", check({"url": "https://x.example", "username": "a"}))
        self.assertIn("base_path", check({"url": "https://x.example", "base_path": "/a/../b"}))

    def test_a_server_that_isnt_one(self):
        other = FakeServer(lambda req: Response(200, b"<html>hello</html>", {"Content-Type": "text/html"})).start()
        self.addCleanup(other.close)
        d = OpenListDriver({"url": other.url, "username": "admin", "password": "x"})
        self.addCleanup(d.close)
        with self.assertRaises(BackendError):
            d.list("/")

    def test_missing_things(self):
        d = self.new_driver()
        with self.assertRaises(NotFound):
            d.delete("/nope")
        with self.assertRaises(NotFound):
            d.rename("/nope", "x")


class ShareFixture:
    def make_driver(self):
        self.fake.reset()
        with self.fake.lock:
            for path, data in READ_ONLY_TREE.items():
                self.fake._make_dirs("/shared/folder" + path.rsplit("/", 1)[0])
                self.fake.tree["/shared/folder" + path] = self.fake._node(False, data)
            self.fake._make_dirs("/shared/folder" + EMPTY_FOLDER)
            self.fake.tree["/outside.txt"] = self.fake._node(False, b"not shared")
            self.fake.shares["AbC123"] = ("/shared/folder", "s3cret")
            self.fake.shares["Pub456"] = ("/shared/folder", None)
        return self.make_share(**self.share)

    share = {"share_id": "AbC123", "share_password": "s3cret"}

    def make_share(self, **config):
        driver = OpenListShareDriver({"url": self.fake.url, **{key: value for key, value in config.items() if value is not None}})
        self.addCleanup(driver.close)
        return driver


class OpenListShareContractTests(FakeMixin, ShareFixture, ReadOnlyContract, unittest.TestCase):
    """A share with a password: its files go through this site (the password stays here)."""

    def test_a_wrong_password_and_settings(self):
        with self.assertRaises(DriverError):
            self.make_share(share_id="AbC123", share_password="wrong").list("/")
        self.assertTrue(all(r["authorization"] is None for r in self.fake.download_requests))
        check = OpenListShareDriver.validate_config
        self.assertEqual(check({"url": "https://ol.example", "share_id": "AbC123"}), {})
        self.assertIn("share_id", check({"url": "https://ol.example", "share_id": "https://ol.example/s/AbC123"}))

    def test_the_password_stays_on_this_site(self):
        self.assertFalse(self.d.can_direct_download)
        self.assertIsNone(self.d.download_url("/a.txt", filename="a.txt"))
        public = self.make_share(share_id="Pub456")
        self.assertTrue(public.can_direct_download)
        url = public.download_url("/a.txt", filename="a.txt")
        self.assertNotIn("pwd", url)
        self.assertEqual(httpx.get(url).content, READ_ONLY_TREE["/a.txt"])

    def test_a_share_that_is_gone(self):
        with self.assertRaises(NotFound):
            self.make_share(share_id="Gone12").check()

    def test_a_pasted_share_link(self):
        check = OpenListShareDriver.validate_config
        for link in ("https://ol.example/@s/AbC123", "https://ol.example/@s/AbC123?pwd=s3cret", "https://ol.example/sd/AbC123"):
            with self.subTest(link=link):
                self.assertEqual(check({"url": "https://ol.example", "share_id": link}), {})
        pasted = self.make_share(share_id=f"{self.fake.url}/@s/AbC123", share_password="s3cret")
        self.assertEqual(sorted(e.name for e in pasted.list("/")), ["a.txt", "d", "empty"])

    def test_a_share_of_one_file(self):
        self.fake.shares["OneFile"] = ("/shared/folder/a.txt", None)
        with self.assertRaises(DriverError) as caught:
            self.make_share(share_id="OneFile").check()
        self.assertIs(type(caught.exception), DriverError)


class OpenListPublicShareContractTests(FakeMixin, ShareFixture, ReadOnlyContract, unittest.TestCase):
    """A share without a password: the browser downloads from OpenList's /sd links."""

    share = {"share_id": "Pub456"}


if __name__ == "__main__":
    unittest.main()
