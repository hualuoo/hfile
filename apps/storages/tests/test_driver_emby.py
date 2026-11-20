"""Tests of the Emby / Jellyfin driver (apps.storages.drivers.emby) against the fake of a media server.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_emby
"""

from __future__ import annotations

import unittest

from apps.storages.drivers.base import AuthError, DriverError
from apps.storages.drivers.emby import EmbyDriver
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_emby import USER_ID, FakeEmby


class FakeMixin:
    mode = "jellyfin"  # Jellyfin 12, as installed: legacy authorization off

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        files = {"/Lib" + path: data for path, data in READ_ONLY_TREE.items()}
        cls.fake = FakeEmby(files, folders=("/Lib" + EMPTY_FOLDER, "/Other"), mode=cls.mode).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, **config):
        settings = {"url": self.fake.url, "username": "alice", "password": "pw-alice", "base_path": "/Lib", **config}
        driver = EmbyDriver({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class JellyfinContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        return self.new_driver()


class JellyfinApiKeyContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        return self.new_driver(username=None, password=None, api_key=self.fake.api_key, user_id=USER_ID)


class EmbyContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    """Emby reads its own headers (X-Emby-Authorization, X-Emby-Token) only."""

    mode = "emby"

    def make_driver(self):
        return self.new_driver()


class EmbyStreamContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        return self.new_driver(link_method="stream")


class EmbyTests(FakeMixin, unittest.TestCase):
    def test_the_libraries_and_names(self):
        d = self.new_driver(base_path="/")
        self.assertEqual(sorted(e.name for e in d.list("/")), ["Lib", "Other"])
        names = sorted(e.name for e in d.list("/Lib/d"))
        self.assertIn("b.bin", names)  # the file's name on the server, not its title
        self.assertNotIn("Title of b.bin", names)

    def test_an_api_key_and_a_new_login(self):
        d = self.new_driver(username=None, password=None, api_key=self.fake.api_key, user_id=USER_ID)
        self.assertEqual(d.open("/a.txt").read(), READ_ONLY_TREE["/a.txt"])
        d = self.new_driver()
        d.list("/")
        self.fake.expire_tokens()
        d.listing_ttl = 0
        self.assertTrue(d.list("/"))  # logged in again
        with self.assertRaises(AuthError):
            self.new_driver(password="wrong").list("/")

    def test_without_the_download_permission(self):
        self.fake.can_download = False
        self.addCleanup(setattr, self.fake, "can_download", True)
        with self.assertRaises(DriverError):
            self.new_driver().open("/a.txt")
        self.assertEqual(self.new_driver(link_method="stream").open("/a.txt").read(), READ_ONLY_TREE["/a.txt"])

    def test_settings(self):
        check = EmbyDriver.validate_config
        self.assertEqual(check({"url": "http://192.168.1.10:8096", "username": "a"}), {})
        self.assertIn("user_id", check({"url": "http://x.example", "api_key": "k"}))
        self.assertIn("username", check({"url": "http://x.example"}))
        self.assertIn("url", check({"url": "x.example", "username": "a"}))


class SessionChecks:
    """The sessions of a login and of an API key, as Jellyfin and Emby keep them."""

    def test_two_storages_of_one_account(self):
        """Jellyfin ends a user's other sessions of a device at each login: two storages of one account (other
        base paths) are two devices, and don't log each other out."""
        whole, library = self.new_driver(base_path="/"), self.new_driver(base_path="/Lib")
        for _ in range(3):
            self.assertIn("Lib", [e.name for e in whole.list("/")])
            self.assertEqual(library.open("/a.txt").read(), READ_ONLY_TREE["/a.txt"])
            self.assertEqual(whole.open("/Lib/a.txt").read(), READ_ONLY_TREE["/a.txt"])
            self.assertIn("a.txt", [e.name for e in library.list("/")])

    def test_a_download_after_the_token_expired(self):
        d = self.new_driver()
        d.list("/")
        self.fake.expire_tokens()  # logged out on the server: a download logs in again too
        self.assertEqual(d.open("/a.txt").read(), READ_ONLY_TREE["/a.txt"])

    def test_a_wrong_user_id(self):
        d = self.new_driver(username=None, password=None, api_key=self.fake.api_key, user_id="0" * 32, base_path="/")
        with self.assertRaises(AuthError):
            d.check()


class JellyfinSessionTests(FakeMixin, SessionChecks, unittest.TestCase):
    pass


class EmbySessionTests(FakeMixin, SessionChecks, unittest.TestCase):
    mode = "emby"


if __name__ == "__main__":
    unittest.main()
