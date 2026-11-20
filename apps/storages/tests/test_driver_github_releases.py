"""Tests of the GitHub Releases driver (apps.storages.drivers.github_releases) against the fake of GitHub.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_github_releases
"""

from __future__ import annotations

import unittest

import httpx

from apps.storages.drivers import github_releases
from apps.storages.drivers.base import AuthError, BackendError, NotADirectory, NotFound
from apps.storages.tests.contract import READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_github import FakeGitHub

JSON = {"Content-Type": "application/json"}

# READ_ONLY_TREE as GitHub can have it: the latest release of five repositories, shown at the root, at /d (which
# holds two more), and at /empty (a release without files).
CONTRACT_REPOSITORIES = "o/root\nd:o/d\nd/sub:o/sub\nd/a b:o/ab\nempty:o/empty"


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeGitHub().start()
        cls.Driver = type("FakeGitHubReleasesDriver", (github_releases.GitHubReleasesDriver,),
                          {"api_root": cls.fake.api_url, "web_root": cls.fake.web_url})

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, repositories: str, **config):
        driver = self.Driver({"repositories": repositories, **config})
        self.addCleanup(driver.close)
        return driver


class GitHubReleasesContractTests(FakeMixin, ReadOnlyContract, unittest.TestCase):
    def make_driver(self):
        fake = self.fake
        fake.reset()
        for repo in ("root", "d", "sub", "ab", "empty"):
            fake.add_repo("o", repo)
        fake.add_release("o", "root", "v1", {"old.txt": b"old"})
        fake.add_release("o", "root", "v2", {"a.txt": READ_ONLY_TREE["/a.txt"]})
        fake.add_release("o", "root", "v3-rc", {"pre.txt": b"pre"}, prerelease=True)  # not the latest
        fake.add_release("o", "d", "v1", {"b.bin": READ_ONLY_TREE["/d/b.bin"], "中文 名字.txt": READ_ONLY_TREE["/d/中文 名字.txt"]})
        fake.add_release("o", "sub", "v1", {"c.txt": READ_ONLY_TREE["/d/sub/c.txt"]})
        fake.add_release("o", "ab", "v1", {"100%.txt": READ_ONLY_TREE["/d/a b/100%.txt"]})
        fake.add_release("o", "empty", "v1", {})
        return self.new_driver(CONTRACT_REPOSITORIES)


class GitHubReleasesTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.fake.add_repo("o", "r")

    def names(self, driver, path):
        return sorted(entry.name for entry in driver.list(path))

    def test_the_repositories_setting(self):
        mounts, errors = github_releases.parse_repositories("o/r\n\n  tools/x : a-b/c.d_e  \n")
        self.assertEqual(errors, [])
        self.assertEqual([(m.path, m.owner, m.repo) for m in mounts], [("/", "o", "r"), ("/tools/x", "a-b", "c.d_e")])
        cases = {
            "": "repositories",
            "just-a-name": "repositories",
            "o/r\no/s": "repositories",  # two at the root
            "a/../b:o/r": "repositories",
            "-bad/r": "repositories",
            "o/..": "repositories",
        }
        for text, key in cases.items():
            with self.subTest(text=text):
                self.assertIn(key, github_releases.GitHubReleasesDriver.validate_config({"repositories": text}))
        self.assertEqual(github_releases.GitHubReleasesDriver.validate_config(
            {"repositories": "o/r", "download_mirror": "https://ghproxy.example/https://github.com"}), {})
        self.assertIn("download_mirror", github_releases.GitHubReleasesDriver.validate_config(
            {"repositories": "o/r", "download_mirror": "ftp://x"}))

    def test_all_releases_as_folders_over_several_pages(self):
        self.fake.per_page_max = 2
        for n in range(5):
            self.fake.add_release("o", "r", f"v{n}", {f"f{n}.txt": b"x" * n})
        self.fake.add_release("o", "r", "v9-rc", {"pre.txt": b"p"}, prerelease=True)
        self.fake.add_release("o", "r", "draft", {"d.txt": b"d"}, draft=True)
        self.fake.add_release("o", "r", "a/b", {"slash.txt": b"s"})  # a tag that can't be a folder's name
        d = self.new_driver("o/r", all_releases=True)
        self.assertEqual(self.names(d, "/"), ["v0", "v1", "v2", "v3", "v4", "v9-rc"])
        self.assertEqual(self.fake.calls.count("/repos/o/r/releases"), 4)  # 8 releases, 2 a page
        self.assertEqual(self.names(d, "/v3"), ["f3.txt"])
        self.assertEqual(d.open("/v3/f3.txt").read(), b"xxx")
        self.assertTrue(d.stat("/v3").is_dir)
        self.assertEqual(d.stat("/v3/f3.txt").size, 3)
        with self.assertRaises(NotFound):
            d.stat("/draft")
        with self.assertRaises(NotFound):
            d.stat("/v3/f4.txt")
        with self.assertRaises(NotADirectory):
            d.list("/v3/f3.txt")
        # At most max_pages pages: the newest releases (the first page has "a/b", left out, and v9-rc).
        d = self.new_driver("o/r", all_releases=True)
        d.max_pages = 1
        self.assertEqual(self.names(d, "/"), ["v9-rc"])

    def test_answers_are_cached(self):
        self.fake.add_release("o", "r", "v1", {"a.txt": b"a"})
        d = self.new_driver("o/r")
        for _ in range(3):
            self.assertEqual(self.names(d, "/"), ["a.txt"])
            d.stat("/a.txt")
        self.assertEqual(self.fake.calls, ["/repos/o/r/releases/latest"])

    def test_repositories_without_releases_or_missing(self):
        d = self.new_driver("o/r\nx:o/missing")
        self.assertEqual(self.names(d, "/"), ["x"])  # o/r has no release yet: an empty folder
        with self.assertRaises(NotFound) as caught:
            d.list("/x")
        self.assertIn("o/missing", caught.exception.message)
        with self.assertRaises(NotFound) as caught:
            d.check()
        self.assertIn("o/missing", caught.exception.message)

    def test_a_made_up_folder_wins_over_a_file_of_its_name(self):
        self.fake.add_repo("o", "s")
        self.fake.add_release("o", "r", "v1", {"tools": b"a file", "a.txt": b"a"})
        self.fake.add_release("o", "s", "v1", {"s.txt": b"s"})
        d = self.new_driver("o/r\ntools/s:o/s")
        entries = {entry.name: entry for entry in d.list("/")}
        self.assertTrue(entries["tools"].is_dir)
        self.assertEqual(self.names(d, "/tools"), ["s"])
        self.assertEqual(self.names(d, "/tools/s"), ["s.txt"])
        with self.assertRaises(NotFound):
            d.open("/tools")

    def test_a_private_repository(self):
        self.fake.add_repo("o", "p", private=True)
        self.fake.add_release("o", "p", "v1", {"secret.bin": bytes(range(256)) * 10})
        with self.assertRaises(NotFound) as caught:
            self.new_driver("o/p").check()  # without the token GitHub doesn't show it
        self.assertIn("私有", caught.exception.message)  # it said "or the token can't see it" without one, too
        with self.assertRaises(NotFound) as caught:
            self.new_driver("o/nothing", token=self.fake.token).check()
        self.assertIn("看不到", caught.exception.message)
        d = self.new_driver("o/p", token=self.fake.token)
        self.assertEqual(self.names(d, "/"), ["secret.bin"])
        self.assertIsNone(d.download_url("/secret.bin", filename="secret.bin"))  # the browser has no token
        stream = d.open("/secret.bin", 10, 20)
        self.assertEqual(stream.read(), (bytes(range(256)) * 10)[10:30])
        self.assertEqual(self.fake.file_requests[-1], {"authorization": None, "range": "bytes=10-29"})
        with self.assertRaises(AuthError):
            self.new_driver("o/p", token="wrong").list("/")

    def test_direct_links_of_public_repositories(self):
        self.fake.add_release("o", "r", "v1.0", {"a b.txt": b"public"})
        d = self.new_driver("o/r", token=self.fake.token)
        url = d.download_url("/a b.txt", filename="a b.txt")
        self.assertEqual(url, f"{self.fake.web_url}/o/r/releases/download/v1.0/a%20b.txt")
        self.assertEqual(httpx.get(url, follow_redirects=True).content, b"public")
        mirrored = self.new_driver("o/r", download_mirror="https://ghproxy.example/https://github.com/")
        self.assertEqual(mirrored.download_url("/a b.txt", filename="a b.txt"),
                         "https://ghproxy.example/https://github.com/o/r/releases/download/v1.0/a%20b.txt")

    def test_the_rate_limit(self):
        self.fake.add_release("o", "r", "v1", {"a.txt": b"a"})
        self.fake.rate_limited = 1
        with self.assertRaises(BackendError) as caught:
            self.new_driver("o/r").list("/")
        self.assertIn("00:00", caught.exception.message)  # when it resets (UTC)
        self.assertEqual(self.names(self.new_driver("o/r"), "/"), ["a.txt"])

    def test_nothing_is_asked_while_the_rate_limit_holds(self):
        """GitHub: "Continuing to make requests while you are rate limited may result in the banning of your
        integration." Not until it resets (x-ratelimit-reset, a retry-after, or a minute)."""
        self.fake.add_release("o", "r", "v1", {"a.txt": b"a"})
        d = self.new_driver("o/r")
        self.fake.rate_limited = 1
        with self.assertRaises(BackendError):
            d.list("/")
        before = len(self.fake.calls)
        with self.assertRaises(BackendError) as caught:
            d.list("/")  # GitHub would answer now: the limit was one call
        self.assertIn("00:00", caught.exception.message)
        self.assertEqual(len(self.fake.calls), before)

    def test_the_rate_limit_with_a_token(self):
        self.fake.add_release("o", "r", "v1", {"a.txt": b"a"})
        self.fake.rate_limited = 1
        with self.assertRaises(BackendError) as caught:
            self.new_driver("o/r", token=self.fake.token).list("/")
        self.assertIn("00:00", caught.exception.message)
        self.assertNotIn("60", caught.exception.message)  # it has a token: no word of getting one

    def test_public_files_are_read_from_their_download_links(self):
        """A public repository's file is read from its browser_download_url (github.com, not the REST API: none of the 60
        requests an hour without a token), not through download_mirror, which is for browsers."""
        data = bytes(range(256))
        self.fake.add_release("o", "r", "v1", {"a.bin": data})
        for config in ({}, {"download_mirror": "https://ghproxy.invalid/https://github.com"}):
            with self.subTest(config=config):
                d = self.new_driver("o/r", **config)
                d.list("/")
                before = len(self.fake.calls)
                for offset in (0, 10, 100):
                    self.assertEqual(d.open("/a.bin", offset, 5).read(), data[offset:offset + 5])
                self.assertEqual(self.fake.calls[before:], [])
        self.assertEqual(self.fake.web_requests[-1], "/o/r/releases/download/v1/a.bin")
        self.assertTrue(all(r["authorization"] is None for r in self.fake.file_requests))

    def test_a_latest_release_whose_tag_has_a_slash(self):
        """Tags like kustomize's "kustomize/v5.8.0" can't be a folder's name; without all_releases they name no folder."""
        self.fake.add_release("o", "r", "cli/v2.0.0", {"cli.tar.gz": b"two"})
        d = self.new_driver("o/r")
        self.assertEqual(self.names(d, "/"), ["cli.tar.gz"])
        self.assertEqual(d.open("/cli.tar.gz").read(), b"two")
        self.assertEqual(self.fake.web_requests[-1], "/o/r/releases/download/cli/v2.0.0/cli.tar.gz")  # %2F, decoded
        self.assertEqual(self.names(self.new_driver("o/r", all_releases=True), "/"), [])  # as a folder: left out

    def test_names_that_cant_be_paths_are_left_out(self):
        self.fake.add_release("o", "r", "v1", {"ok.txt": b"1", "..": b"2"})
        self.assertEqual(self.names(self.new_driver("o/r"), "/"), ["ok.txt"])

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        self.fake.add_repo("o", "p", private=True)  # its files come through the API
        self.fake.add_release("o", "p", "v1", {"secret.bin": b"secret"})
        body = LongBody(b'{"message": "the asset store failed"}')
        answer = lambda request: Response(500, body, JSON, length=LONG) if "/releases/assets/" in request.path else None
        with self.fake.api.intercepting(answer), self.assertRaisesRegex(BackendError, "the asset store failed"):
            self.new_driver("o/p", token=self.fake.token).open("/secret.bin")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
