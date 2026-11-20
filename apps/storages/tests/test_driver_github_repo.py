"""Tests of the GitHub repository driver (apps.storages.drivers.github_repo) against the fake of GitHub's API.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_github_repo
"""

from __future__ import annotations

import io
import threading
import time
import unittest
from unittest import mock

import httpx

from apps.storages.drivers import github_repo
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response
from apps.storages.tests.fake_github_repo import FakeGitHubRepo

JSON = {"Content-Type": "application/json"}

WRITE, READ = "gh-write", "gh-read"  # the fake's tokens


def cached_entries(driver) -> int:
    """The entries of the trees a driver keeps: its tree cache's values (a TTLCache keeps them as (time, value))."""
    return sum(len(value[1] if isinstance(value, tuple) else value) for value in driver._trees._data.values())


class FakeMixin:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeGitHubRepo().start()
        cls.Driver = type("FakeGitHubRepoDriver", (github_repo.GitHubRepoDriver,),
                          {"api_root": cls.fake.api_url, "raw_root": cls.fake.raw_url, "writes_per_second": 0.0})

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def new_driver(self, driver_class=None, **config):
        settings = {"owner": "o", "repo": "r", "token": WRITE, **config}
        driver = (driver_class or self.Driver)({key: value for key, value in settings.items() if value is not None})
        self.addCleanup(driver.close)
        return driver


class GitHubRepoContractTests(FakeMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        self.fake.reset()
        self.fake.add_repo("o", "r")  # one commit, holding the root's .gitkeep only
        return self.new_driver()


class GitHubRepoBasePathContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """A folder of a branch other than the default one; what is around it stays as it is."""

    AROUND = {"README.md": b"readme", "keep/out.txt": b"not in the base"}

    def make_driver(self):
        self.fake.reset()
        self.fake.add_repo("o", "r", {"README.md": b"main"})
        self.fake.add_branch("o", "r", "files", {**self.AROUND, "base/here/.gitkeep": b""})
        self.addCleanup(self.around_kept)
        return self.new_driver(branch="files", base_path="/base/here")

    def around_kept(self):
        files = self.fake.files("o", "r", "files")
        self.assertEqual({path: files.get(path) for path in self.AROUND}, self.AROUND)
        self.assertEqual(self.fake.files("o", "r"), {"README.md": b"main"})


class GitHubRepoTests(FakeMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake.reset()
        self.fake.add_repo("o", "r")

    def put(self, driver, path, data):
        driver.write(path, io.BytesIO(data), len(data))

    def names(self, driver, path):
        return sorted(entry.name for entry in driver.list(path))

    def requests(self, method):
        return [path for verb, path in self.fake.calls if verb == method]

    def test_every_change_is_one_commit(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        d.mkdir("/f")
        d.rename("/a.txt", "b.txt")
        d.copy("/b.txt", "/f")
        d.mkdir("/g")
        d.move("/f", "/g")
        d.delete("/b.txt")
        self.assertEqual(self.fake.commits("o", "r"), [
            "HFile: delete b.txt", "HFile: move f to g", "HFile: create folder g", "HFile: copy b.txt to f",
            "HFile: rename a.txt to b.txt", "HFile: create folder f", "HFile: upload a.txt", "Initial commit"])
        self.assertEqual(self.fake.files("o", "r"), {"g/f/b.txt": b"a"})
        self.assertEqual(len(self.requests("PATCH")), 7)

    def test_empty_folders_keep_a_placeholder(self):
        d = self.new_driver()
        d.mkdir("/a/b")
        self.assertEqual(set(self.fake.files("o", "r")), {"a/b/.gitkeep"})  # the root's went: it holds a now
        self.assertEqual((self.names(d, "/"), d.list("/a/b")), (["a"], []))
        self.put(d, "/a/b/x.txt", b"x")
        self.assertEqual(set(self.fake.files("o", "r")), {"a/b/x.txt"})
        d.move("/a/b/x.txt", "/")  # /a/b stays, empty
        self.assertEqual(set(self.fake.files("o", "r")), {"a/b/.gitkeep", "x.txt"})
        self.assertEqual(d.list("/a/b"), [])
        d.delete("/a")
        d.delete("/x.txt")  # the root keeps one too: a commit's tree is never empty
        self.assertEqual(self.fake.files("o", "r"), {".gitkeep": b""})
        self.assertEqual(d.list("/"), [])
        # Someone else's placeholder is hidden too, and left alone.
        self.fake.push("o", "r", {"docs/.gitkeep": b"keep me", "docs/a.md": b"# A"})
        d = self.new_driver()
        self.assertEqual(self.names(d, "/docs"), ["a.md"])
        with self.assertRaises(NotFound):
            d.stat("/docs/.gitkeep")
        d.delete("/docs/a.md")
        self.assertEqual(self.fake.files("o", "r")["docs/.gitkeep"], b"keep me")

    def test_names_git_keeps_for_itself(self):
        d = self.new_driver()
        d.mkdir("/f")
        for change in (lambda: self.put(d, "/.gitkeep", b""), lambda: d.mkdir("/f/.gitkeep"), lambda: d.rename("/f", ".gitkeep"),
                       lambda: d.mkdir("/.git"), lambda: self.put(d, "/f/.GIT", b"x"), lambda: d.rename("/f", ".Git")):
            with self.assertRaises(InvalidPath):
                change()
        self.assertEqual(set(self.fake.files("o", "r")), {"f/.gitkeep"})

    def test_what_git_keeps_besides_files_and_folders(self):
        pointer = b"version https://git-lfs.github.com/spec/v1\noid sha256:" + b"4c" * 32 + b"\nsize 84977953\n"
        self.fake.add_repo("o", "r", {"lib": ("160000", "a" * 40), "run.sh": ("100755", b"#!/bin/sh\n"),
                                      "link": ("120000", b"run.sh"), "video.mp4": pointer})
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["link", "run.sh", "video.mp4"])  # not the submodule
        with self.assertRaises(NotFound):
            d.stat("/lib")
        with self.assertRaises(AlreadyExists):  # its name is taken all the same
            self.put(d, "/lib", b"x")
        self.assertEqual(d.open("/link").read(), b"run.sh")  # a symbolic link: the path it points to
        self.assertEqual((d.stat("/video.mp4").size, d.open("/video.mp4").read()), (len(pointer), pointer))  # Git LFS
        self.put(d, "/run.sh", b"#!/bin/sh\necho hi\n")
        d.rename("/run.sh", "start.sh")
        self.assertEqual(self.fake.entries("o", "r")["start.sh"], ("100755", b"#!/bin/sh\necho hi\n"))  # still executable
        self.assertIsNone(d.stat("/start.sh").modified)  # git keeps times per commit, not per file

    def test_a_folder_mode_as_git_writes_it(self):
        self.fake.add_repo("o", "r", {"d/a.txt": b"a"})
        self.fake.git_modes = True  # 40000, not the docs' 040000: still a folder, sent back as 040000
        d = self.new_driver()
        self.assertTrue(d.stat("/d").is_dir)
        d.rename("/d", "e")
        self.assertEqual(self.fake.files("o", "r"), {"e/a.txt": b"a"})

    def test_a_push_meanwhile_is_kept(self):
        d = self.new_driver()
        self.put(d, "/mine.txt", b"1")
        self.fake.foreign_commits = 1  # someone pushes just before the branch is moved to the new commit
        self.put(d, "/mine2.txt", b"2")
        self.assertEqual(self.fake.files("o", "r"), {"mine.txt": b"1", "foreign-1.txt": b"pushed by someone else", "mine2.txt": b"2"})
        self.assertEqual(self.fake.commits("o", "r")[:3], ["HFile: upload mine2.txt", "Someone else's change", "HFile: upload mine.txt"])
        self.assertEqual(self.names(d, "/"), ["foreign-1.txt", "mine.txt", "mine2.txt"])

    def test_a_change_is_checked_again_on_the_new_head(self):
        d = self.new_driver()
        self.put(d, "/a.txt", b"a")
        self.fake.foreign_commits, self.fake.foreign_change = 1, {"b.txt": b"theirs"}
        with self.assertRaises(AlreadyExists):
            d.rename("/a.txt", "b.txt")
        self.fake.foreign_commits, self.fake.foreign_change = 1, {"a.txt": None}
        with self.assertRaises(NotFound):
            d.delete("/a.txt")
        self.assertEqual(self.fake.files("o", "r"), {"b.txt": b"theirs"})

    def test_the_branch_keeps_moving(self):
        d = self.new_driver()
        self.fake.foreign_commits = 5
        with self.assertRaises(BackendError):
            self.put(d, "/mine.txt", b"x")
        self.assertNotIn("mine.txt", self.fake.files("o", "r"))
        self.assertEqual(len(self.requests("PATCH")), github_repo.GitHubRepoDriver.commit_attempts)

    def test_changes_from_several_threads(self):
        d = self.new_driver()
        failures = []

        def upload(number):
            try:
                self.put(d, f"/t/{number}.txt", str(number).encode())
            except Exception as error:  # noqa: BLE001 - shown by the assertion below
                failures.append(error)

        threads = [threading.Thread(target=upload, args=(number,)) for number in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(failures, [])
        self.assertEqual(self.names(d, "/t"), sorted(f"{number}.txt" for number in range(8)))
        self.assertEqual(len(self.fake.commits("o", "r")), 9)
        self.assertEqual(len(self.requests("PATCH")), 8)  # one change at a time: none had to be made again

    def test_a_protected_branch(self):
        self.fake.protect("o", "r", "main")
        with self.assertRaises(DriverError) as caught:
            self.new_driver().mkdir("/f")
        self.assertIn("Protected branch", caught.exception.message)
        self.assertEqual(len(self.requests("PATCH")), 1)

    def test_a_big_folder(self):
        self.fake.add_repo("o", "r", {f"big/{number:04}.txt": b"x" for number in range(1500)})
        d = self.new_driver()
        self.assertEqual(len(d.list("/big")), 1500)  # the contents API would stop at 1,000
        self.assertEqual(len([path for path in self.requests("GET") if "/git/trees/" in path]), 2)  # the root, /big
        self.fake.tree_limit = 1000  # a tree GitHub cuts off ("truncated"): an error, not a short listing
        with self.assertRaises(BackendError) as caught:
            self.new_driver().list("/big")
        self.assertIn("/big", caught.exception.message)

    def test_trees_are_cached_and_the_head_asked_for_again(self):
        self.fake.add_repo("o", "r", {"d/a.txt": b"a", "e/b.txt": b"b"})
        d = self.new_driver()
        self.assertEqual((self.names(d, "/d"), self.names(d, "/e")), (["a.txt"], ["b.txt"]))
        before = len(self.fake.calls)
        for _ in range(3):
            d.list("/d")
            d.stat("/e/b.txt")
        self.assertEqual(len(self.fake.calls), before)
        self.fake.push("o", "r", {"d/c.txt": b"c"})
        self.assertEqual(self.names(d, "/d"), ["a.txt"])  # within head_ttl seconds
        d.head_ttl = 0
        before = len(self.fake.calls)
        self.assertEqual(self.names(d, "/d"), ["a.txt", "c.txt"])
        self.assertEqual([path.split("/")[5] for _, path in self.fake.calls[before:]], ["ref", "commits", "trees", "trees"])
        before = len(self.fake.calls)
        self.assertEqual(self.names(d, "/e"), ["b.txt"])
        self.assertEqual(len(self.fake.calls), before + 1)  # the head; /e is the same tree as before

    def test_the_tree_cache_is_bounded_by_its_entries(self):
        """Each change of a folder makes a new tree of it, cached next to the old ones, which nothing asks for again:
        the cache is bounded by the entries its trees hold. A tree bigger than the bound is kept, alone."""
        self.fake.add_repo("o", "r", {f"img/{n:04}.jpg": b"x" for n in range(1000)})
        d = self.new_driver(type("Small", (self.Driver,), {"tree_cache_entries": 3000}))
        for k in range(10):
            self.put(d, f"/img/new-{k}.jpg", b"y")
            self.assertEqual(len(d.list("/img")), 1001 + k)
        self.assertLessEqual(cached_entries(d), 3000)
        d = self.new_driver(type("Tiny", (self.Driver,), {"tree_cache_entries": 10}))
        self.assertEqual(len(d.list("/img")), 1010)
        self.assertEqual(cached_entries(d), 1010)  # the folder's tree; the root's, used before it, went

    def test_a_lost_answer_to_the_branchs_update(self):
        """GitHub made the update, its answer was lost (a 502 in front of it, a timeout): the branch points at the new
        commit, so the change is made."""
        d = self.new_driver()
        self.fake.lose_ref_answers = 1
        self.put(d, "/a.txt", b"a")
        self.assertEqual(self.fake.files("o", "r"), {"a.txt": b"a"})
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(self.fake.commits("o", "r"), ["HFile: upload a.txt", "Initial commit"])
        send = d._http.send

        def timing_out(request, **kwargs):
            response = send(request, **kwargs)
            if request.method == "PATCH":
                response.close()
                raise httpx.ReadTimeout("timed out", request=request)
            return response

        with mock.patch.object(d._http, "send", timing_out):
            self.put(d, "/b.txt", b"b")
        self.assertEqual(self.names(d, "/"), ["a.txt", "b.txt"])
        self.fake.ref_errors = 1  # not made: an error, as before
        with self.assertRaises(BackendError):
            self.put(d, "/c.txt", b"c")
        self.assertEqual(set(self.fake.files("o", "r")), {"a.txt", "b.txt"})

    def test_size_limits(self):
        d = self.new_driver()
        d.max_file_size, d.warn_file_size = 100, 50
        with self.assertRaises(DriverError):
            self.put(d, "/big.bin", b"x" * 101)
        self.assertEqual(self.fake.calls, [])  # refused before anything was sent
        with self.assertLogs("hfile.storage", "WARNING") as logs:
            self.put(d, "/fat.bin", b"x" * 60)
        self.assertIn("fat.bin", logs.output[0])
        self.fake.max_blob = 10  # GitHub's own refusal, with its message
        with self.assertRaises(DriverError) as caught:
            self.put(d, "/b.bin", b"x" * 20)
        self.assertIn("too large", caught.exception.message)
        self.assertEqual(set(self.fake.files("o", "r")), {"fat.bin"})

    def test_an_upload_streams_its_blob(self):
        d = self.new_driver()
        data = bytes(range(256)) * 4099  # not a multiple of 3, nor of the pieces it is read in
        stream = io.BytesIO(data)
        stream.read = lambda size=-1, read=stream.read: read(min(size, 1000) if size > 0 else 1000)  # short reads
        d.write("/s.bin", stream, len(data))
        self.assertEqual(self.fake.files("o", "r"), {"s.bin": data})
        with self.assertRaises(DriverError):  # the data ends early: nothing is stored
            d.write("/cut.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual(set(self.fake.files("o", "r")), {"s.bin"})
        self.assertEqual(len(self.fake.commits("o", "r")), 2)

    def test_a_rate_limit_stops_the_requests_until_it_resets(self):
        self.put(self.new_driver(), "/a.txt", b"a")
        d = self.new_driver()
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        d.head_ttl = 0
        self.fake.rate_limited = 1
        with self.assertRaises(BackendError) as caught:
            d.list("/")
        self.assertIn("00:00", caught.exception.message)  # when it resets (UTC)
        before = len(self.fake.calls)
        self.assertEqual(self.names(d, "/"), ["a.txt"])  # the last head, without asking
        for change in (lambda: d.open("/a.txt"), lambda: d.mkdir("/f")):
            with self.assertRaises(BackendError):
                change()
        self.assertEqual(len(self.fake.calls), before)  # GitHub isn't asked meanwhile
        self.assertEqual(self.names(self.new_driver(), "/"), ["a.txt"])

    def test_a_secondary_limit(self):
        d = self.new_driver()
        d.list("/")
        self.fake.secondary_limited = 1
        with self.assertRaises(BackendError) as caught:
            self.put(d, "/a.txt", b"a")
        self.assertIn("30", caught.exception.message)  # retry-after's seconds
        before = len(self.fake.calls)
        with self.assertRaises(BackendError):
            d.mkdir("/f")
        self.assertEqual(len(self.fake.calls), before)

    def test_changes_are_paced(self):
        d = self.new_driver(type("Paced", (self.Driver,), {"writes_per_second": 20.0}))
        d.list("/")
        start = time.monotonic()
        self.put(d, "/a.txt", b"a")
        self.put(d, "/b.txt", b"b")  # 8 POST and PATCH requests, a 20th of a second apart
        self.assertGreaterEqual(time.monotonic() - start, 7 / 20 - 0.01)

    def test_a_token_that_may_only_read(self):
        self.put(self.new_driver(), "/a.txt", b"a")
        d = self.new_driver(token=READ)
        self.assertEqual(self.names(d, "/"), ["a.txt"])
        self.assertEqual(d.open("/a.txt").read(), b"a")
        for change in (lambda: self.put(d, "/b.txt", b"b"), lambda: d.mkdir("/f"), lambda: d.delete("/a.txt")):
            with self.assertRaises(DriverError) as caught:
                change()
            self.assertNotIsInstance(caught.exception, AuthError)  # the token is right, its permission isn't
            self.assertIn("Contents", caught.exception.message)
        self.assertEqual(self.fake.files("o", "r"), {"a.txt": b"a"})

    def test_errors_and_their_messages(self):
        with self.assertRaises(AuthError):
            self.new_driver(token="wrong").list("/")
        for config, name in (({"repo": "nope"}, "o/nope"), ({"branch": "dev"}, "dev"), ({"base_path": "/no/such"}, "/no/such")):
            with self.subTest(config=config), self.assertRaises(NotFound) as caught:
                self.new_driver(**config).check()
            self.assertIn(name, caught.exception.message)
        self.fake.add_repo("o", "empty", empty=True)
        self.fake.moved.add("o/old")
        for repo in ("empty", "old"):  # without a commit; renamed
            with self.subTest(repo=repo), self.assertRaises(DriverError) as caught:
                self.new_driver(repo=repo).check()
            self.assertIn(f"o/{repo}", caught.exception.message)
        self.fake.server_errors = 1
        with self.assertRaises(BackendError) as caught:
            self.new_driver().list("/")
        self.assertIn("502", caught.exception.message)
        d = self.new_driver()
        self.put(d, "/f.txt", b"f")
        self.put(d, "/g.txt", b"g")
        with self.assertRaises(NotADirectory) as caught:
            self.put(d, "/f.txt/x", b"x")
        self.assertIn("/f.txt", caught.exception.message)
        with self.assertRaises(NotADirectory):
            d.move("/f.txt", "/g.txt")
        with self.assertRaises(NotADirectory):
            d.copy("/f.txt", "/g.txt")

    def test_open_asks_for_the_raw_blob(self):
        data = bytes(range(256)) * 300
        d = self.new_driver()
        self.put(d, "/b.bin", data)
        self.assertFalse(d.can_range)
        with d.open("/b.bin") as stream:
            self.assertEqual((stream.size, stream.total, b"".join(stream)), (len(data), len(data), data))
        self.assertEqual(self.fake.blob_requests, [{"accept": "application/vnd.github.raw+json", "range": None, "encoding": "identity"}])

    def test_links_of_public_repositories(self):
        d = self.new_driver()
        self.put(d, "/dir/a b#c?.txt", b"first")
        url = d.download_url("/dir/a b#c?.txt", filename="a b#c?.txt")
        self.assertEqual(url, f"{self.fake.raw_url}/o/r/{self.fake.head('o', 'r')}/dir/a%20b%23c%3F.txt")
        self.put(d, "/dir/a b#c?.txt", b"second")
        new_url = d.download_url("/dir/a b#c?.txt", filename="a b#c?.txt")
        self.assertEqual(httpx.get(url).content, b"first")  # a commit's link never changes...
        self.assertEqual(httpx.get(new_url).content, b"second")  # ...so the new one has the new bytes at once
        self.assertEqual(httpx.get(new_url, headers={"Range": "bytes=1-3"}).content, b"eco")
        with self.assertRaises(NotFound):
            d.download_url("/dir", filename="dir")
        # A private repository's files, and GitHub Enterprise Server's, go through the site.
        self.fake.add_repo("o", "p", {"s.txt": b"secret"}, private=True)
        self.assertIsNone(self.new_driver(repo="p").download_url("/s.txt", filename="s.txt"))
        ghes = self.new_driver(api_url=self.fake.ghes_url)
        self.assertEqual(self.names(ghes, "/dir"), ["a b#c?.txt"])
        self.assertIsNone(ghes.download_url("/dir/a b#c?.txt", filename="a b#c?.txt"))

    def test_branches(self):
        self.fake.add_repo("o", "r", {"a.txt": b"a"}, branch="trunk")
        self.fake.add_branch("o", "r", "feature/x#1", {"f.txt": b"f"})
        self.put(self.new_driver(), "/b.txt", b"b")  # the default branch
        self.assertEqual(self.fake.files("o", "r"), {"a.txt": b"a", "b.txt": b"b"})
        d = self.new_driver(branch="feature/x#1")
        self.put(d, "/g.txt", b"g")
        self.assertEqual(self.fake.files("o", "r", "feature/x#1"), {"f.txt": b"f", "g.txt": b"g"})

    def test_settings(self):
        check = github_repo.GitHubRepoDriver.validate_config
        good = {"owner": "octo-cat", "repo": "my.files_1", "token": "t"}
        self.assertEqual(check(good), {})
        self.assertEqual(check({**good, "base_path": "/a/b", "api_url": "https://github.example.com/api/v3"}), {})
        for key, value in (("owner", "-bad"), ("owner", "o/r"), ("owner", None), ("repo", "r.git"), ("repo", ".."),
                           ("repo", "a b"), ("base_path", "/a/../b"), ("api_url", "github.example.com/api/v3")):
            with self.subTest(key=key, value=value):
                self.assertIn(key, check({**good, key: value}))

    def test_a_long_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        d = self.new_driver()
        self.put(d, "/a.txt", b"abc")
        body = LongBody(b'{"message": "the blob is too large to read"}')
        answer = lambda request: (Response(422, body, JSON, length=LONG)
                                  if request.method == "GET" and "/git/blobs/" in request.path else None)
        with self.fake.api.intercepting(answer), self.assertRaisesRegex(DriverError, "the blob is too large to read"):
            d.open("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
