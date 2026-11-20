"""Tests of the CNB Releases driver (apps.storages.drivers.cnb_releases) against the fake of CNB.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_cnb_releases
"""

from __future__ import annotations

import unittest

import httpx

from apps.storages.drivers import github_releases
from apps.storages.drivers.base import AuthError, NotFound
from apps.storages.drivers.cnb_releases import CNBReleasesDriver
from apps.storages.tests.contract import READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_cnb import FakeCNB

# READ_ONLY_TREE as CNB can have it: the latest release of five repositories (two of them in a group), shown at
# the root, at /d (which holds two more), and at /empty (a release without files).
CONTRACT_REPOSITORIES = "o/g/root\nd:o/d\nd/sub:o/g/sub\nd/a b:o/ab\nempty:o/empty"


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeCNB().start()
        cls.Driver = type("FakeCNBReleasesDriver", (CNBReleasesDriver,),
                          {"api_root": cls.fake.api_url, "web_root": cls.fake.web_url})

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, repositories: str, **config):
        driver = self.Driver({"repositories": repositories, "token": self.fake.token, **config})
        self.addCleanup(driver.close)
        return driver


class CNBReleasesContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        fake = self.fake
        fake.reset()
        for repo in ("o/g/root", "o/d", "o/g/sub", "o/ab", "o/empty"):
            fake.add_repo(repo)
        fake.add_release("o/g/root", "v1", {"old.txt": b"old"})
        fake.add_release("o/g/root", "v2", {"a.txt": READ_ONLY_TREE["/a.txt"]})
        fake.add_release("o/g/root", "v3-rc", {"pre.txt": b"pre"}, prerelease=True)  # not the latest
        fake.add_release("o/d", "v1", {"b.bin": READ_ONLY_TREE["/d/b.bin"], "中文 名字.txt": READ_ONLY_TREE["/d/中文 名字.txt"]})
        fake.add_release("o/g/sub", "v1", {"c.txt": READ_ONLY_TREE["/d/sub/c.txt"]})
        fake.add_release("o/ab", "v1", {"100%.txt": READ_ONLY_TREE["/d/a b/100%.txt"]})
        fake.add_release("o/empty", "v1", {})
        return self.new_driver(CONTRACT_REPOSITORIES)


class CNBReleasesTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()

    def test_the_token(self):
        self.fake.add_repo("o/r")
        self.fake.add_release("o/r", "v1", {"a.txt": b"a"})
        with self.assertRaises(AuthError):
            self.new_driver("o/r", token="wrong").list("/")
        with self.assertRaises(AuthError):  # CNB answers nobody without one
            self.new_driver("o/r", token="").list("/")
        self.assertEqual([e.name for e in self.new_driver("o/r").list("/")], ["a.txt"])

    def test_names_with_groups(self):
        mounts, errors = github_releases.parse_repositories("org/group/repo\nx/y:org/repo", nested=True)
        self.assertEqual(errors, [])
        self.assertEqual([(m.path, m.name) for m in mounts], [("/", "org/group/repo"), ("/x/y", "org/repo")])
        for bad in ("repo", "org/../repo", "org//repo", "org/group/re po"):
            self.assertTrue(github_releases.parse_repositories(bad, nested=True)[1], bad)
        self.assertTrue(github_releases.parse_repositories("org/group/repo")[1])  # GitHub has no groups
        self.assertEqual(CNBReleasesDriver.validate_config({"repositories": "org/group/repo", "token": "t"}), {})
        self.assertIn("repositories", CNBReleasesDriver.validate_config({"repositories": "repo", "token": "t"}))

    def test_downloads(self):
        self.fake.add_repo("o/g/pub")
        self.fake.add_repo("o/g/private", visibility="Private")
        self.fake.add_release("o/g/pub", "v1", {"p.bin": b"0123456789"})
        self.fake.add_release("o/g/private", "v 1", {"s.bin": b"secret data"})
        d = self.new_driver("pub:o/g/pub\nprivate:o/g/private")
        url = d.download_url("/pub/p.bin", filename="p.bin")
        self.assertTrue(url.startswith(self.fake.web_url + "/o/g/pub/-/releases/download/v1/"))
        self.assertEqual(httpx.get(url, follow_redirects=True, timeout=30).content, b"0123456789")
        self.assertIsNone(d.download_url("/private/s.bin", filename="s.bin"))  # needs the token: the site sends it
        self.assertEqual(d.open("/private/s.bin", 3, 4).read(), b"ret ")
        self.assertEqual(d.open("/private/s.bin").read(), b"secret data")
        self.assertTrue(all(r["authorization"] is None for r in self.fake.file_requests))  # the token stays with CNB's API
        self.assertIn("bytes=3-6", [r["range"] for r in self.fake.file_requests])

    def test_all_releases_in_pages(self):
        self.fake.add_repo("o/many")
        for n in range(4):
            self.fake.add_release("o/many", f"v{n}", {f"f{n}.txt": b"x"})
        self.fake.add_release("o/many", "draft", {"d.txt": b"d"}, draft=True)
        Driver = type("SmallPages", (self.Driver,), {"page_size": 2})
        d = Driver({"repositories": "o/many", "token": self.fake.token, "all_releases": True})
        self.addCleanup(d.close)
        self.assertEqual(sorted(e.name for e in d.list("/")), ["v0", "v1", "v2", "v3"])  # not the draft
        self.assertEqual([e.name for e in d.list("/v3")], ["f3.txt"])
        self.assertEqual(len([c for c in self.fake.calls if c == "/o/many/-/releases"]), 3)  # 5 releases, 2 a page

    def test_a_repository_that_isnt_there(self):
        self.fake.add_repo("o/norelease")
        d = self.new_driver("o/missing")
        with self.assertRaises(NotFound) as caught:
            d.list("/")
        self.assertIn("o/missing", caught.exception.message)
        self.assertEqual(self.new_driver("o/norelease").list("/"), [])  # a repository without a release yet

    def test_a_token_that_may_only_read_releases(self):
        """CNB gives a token read access to public repositories; on a private one, /{repo} (repo-basic-info:r) is refused
        unless it was granted. Its files are private all the same, and one without releases is an empty folder."""
        self.fake.basic_info = False
        self.fake.add_repo("o/private", visibility="Private")
        self.fake.add_release("o/private", "v1", {"s.bin": b"secret"})
        self.fake.add_repo("o/new", visibility="Secret")  # no release yet
        self.fake.add_repo("o/pub")
        self.fake.add_release("o/pub", "v1", {"p.bin": b"public"})
        d = self.new_driver("p:o/private\nn:o/new\npub:o/pub")
        self.assertIsNone(d.download_url("/p/s.bin", filename="s.bin"))  # through the site
        self.assertEqual(d.open("/p/s.bin").read(), b"secret")
        self.assertEqual(d.list("/n"), [])
        self.assertTrue(d.download_url("/pub/p.bin", filename="p.bin").startswith(self.fake.web_url))

    def test_a_latest_release_whose_tag_has_a_slash(self):
        """The tag names no folder without all_releases; CNB is asked for the file with its "/" as %2F (not tried on CNB)."""
        self.fake.add_repo("o/r")
        self.fake.add_release("o/r", "cli/v2.0.0", {"cli.tar.gz": b"two"})
        d = self.new_driver("o/r")
        self.assertEqual([e.name for e in d.list("/")], ["cli.tar.gz"])
        self.assertEqual(d.open("/cli.tar.gz").read(), b"two")


if __name__ == "__main__":
    unittest.main()
