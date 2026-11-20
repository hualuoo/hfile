"""Tests of the Google Drive driver against the in-memory fake of the Drive API (no Django)."""

from __future__ import annotations

import io
import os
import threading
import unittest
import urllib.parse
import uuid
from unittest import mock

import httpx
from apps.storages.drivers import filetypes, google
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    NotADirectory,
    NotFound,
    Unsupported,
)
from apps.storages.tests.contract import DriverContract
from apps.storages.tests.fake_cloud_http import LONG, LongBody, Response, json_response
from apps.storages.tests.fake_gdrive import (
    CLIENT_ID,
    CLIENT_SECRET,
    DOC,
    DOCX,
    DRAWING,
    FOLDER,
    FORM,
    SCRIPT,
    SHEET,
    SHORTCUT,
    SLIDES,
    XLSX,
    FakeDrive,
)

JSON = {"Content-Type": "application/json"}

KIB = 1024


class OneWay(io.BytesIO):
    """Upload data that can't be read again (seek)."""

    def seekable(self):
        return False


def driver_class(fake: FakeDrive, **attrs):
    attrs.update(api_root=fake.url, token_endpoint=fake.url + "/token",
                 auth_endpoint=fake.url + "/o/oauth2/v2/auth")
    return type("FakeGoogleDriveDriver", (google.GoogleDriveDriver,), attrs)


def drive_error(status: int, reason: str, message: str = "fake", headers=None) -> Response:
    return json_response({"error": {"code": status, "message": message,
                                    "errors": [{"domain": "global", "reason": reason, "message": message}]}},
                         status, headers)


class FakeMixin:
    """One fake Drive per test class; drivers get a fresh refresh token each."""

    driver_attrs: dict = {}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeDrive()
        cls.addClassCleanup(cls.fake.close)
        cls.Driver = driver_class(cls.fake, **cls.driver_attrs)

    def config(self, **changes):
        config = {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                  "refresh_token": self.fake.issue_refresh_token(), "drive_id": "", "base_path": "/"}
        config.update(changes)
        return config

    def new_driver(self, cls=None, **changes):
        updates: list[dict] = []
        driver = (cls or self.Driver)(self.config(**changes), on_update=updates.append)
        driver.saved = updates
        self.addCleanup(driver.close)
        return driver


class ContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The shared contract in a fresh folder of My Drive (base path below the root)."""

    def make_driver(self):
        base = "/contract/" + uuid.uuid4().hex[:10]
        self.fake.folder(base)
        return self.Driver(self.config(base_path=base))


class SharedDriveContractTests(FakeMixin, DriverContract, unittest.TestCase):
    """The contract at the root ("/") of a fresh shared drive, with 256 KiB upload chunks and
    pages of 7 entries, so multi-chunk uploads and paging run everywhere."""

    driver_attrs = {"chunk_size": 256 * KIB, "page_size": 7}

    def make_driver(self):
        drive = self.fake.create_drive("团队盘 " + uuid.uuid4().hex[:6])
        return self.Driver(self.config(drive_id=drive, base_path="/"))


class Base(FakeMixin, unittest.TestCase):
    """Tests in a fresh base folder of My Drive: self.d, self.base (its id)."""

    def setUp(self):
        super().setUp()
        self.base_path = "/t/" + uuid.uuid4().hex[:10]
        self.base = self.fake.folder(self.base_path)
        self.d = self.new_driver(base_path=self.base_path)

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def get(self, path, offset=0, length=None):
        with self.d.open(path, offset, length) as stream:
            return b"".join(stream)

    def names(self, path="/"):
        return sorted(entry.name for entry in self.d.list(path))

    def knob(self, name, value):
        old = getattr(self.fake, name)
        setattr(self.fake, name, value)
        self.addCleanup(setattr, self.fake, name, old)


class OAuthTests(Base):
    REDIRECT = "https://files.example.com/admin/storages/oauth/callback"

    def test_authorize_url_and_code_exchange(self):
        config = self.config(refresh_token="")
        url = self.Driver.authorize_url(config, redirect_uri=self.REDIRECT, state="st-123")
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        self.assertTrue(url.startswith(self.fake.url + "/o/oauth2/v2/auth?"))
        self.assertEqual(query, {"client_id": CLIENT_ID, "redirect_uri": self.REDIRECT, "response_type": "code",
                                 "scope": "https://www.googleapis.com/auth/drive", "access_type": "offline",
                                 "prompt": "consent", "state": "st-123"})
        code = self.fake.consent(url)
        saved = self.Driver.exchange_code(config, code=code, redirect_uri=self.REDIRECT)
        self.assertEqual(list(saved), ["refresh_token"])
        self.assertIn(saved["refresh_token"], self.fake.refresh_tokens)
        # The new token works.
        driver = self.new_driver(refresh_token=saved["refresh_token"], base_path=self.base_path)
        self.assertEqual(driver.list("/"), [])
        # A code is used once, and only with its redirect URI.
        with self.assertRaises(AuthError):
            self.Driver.exchange_code(config, code=code, redirect_uri=self.REDIRECT)
        code = self.fake.consent(url)
        with self.assertRaises(AuthError):
            self.Driver.exchange_code(config, code=code, redirect_uri="https://evil.example/cb")

    def test_bad_client(self):
        with self.assertRaises(AuthError):
            self.Driver.authorize_url(self.config(client_id=""), redirect_uri=self.REDIRECT, state="s")
        code = self.fake.consent(self.Driver.authorize_url(self.config(), redirect_uri=self.REDIRECT, state="s"))
        with self.assertRaises(AuthError) as caught:
            self.Driver.exchange_code(self.config(client_secret="wrong"), code=code, redirect_uri=self.REDIRECT)
        self.assertIn("invalid_client", caught.exception.message)
        driver = self.new_driver(client_secret="wrong")
        with self.assertRaises(AuthError):
            driver.list("/")

    def test_token_is_refreshed_once_and_shared(self):
        driver = self.new_driver(base_path=self.base_path)
        before = self.fake.token_requests
        results = []
        threads = [threading.Thread(target=lambda: results.append(driver.list("/"))) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results, [[]] * 8)
        driver.list("/")
        self.assertEqual(self.fake.token_requests - before, 1)
        self.assertEqual(driver.saved, [])  # Google keeps the refresh token: nothing to save

    def test_expired_token_is_refreshed(self):
        driver = self.new_driver(base_path=self.base_path)
        driver.list("/")
        before = self.fake.token_requests
        self.fake.expire_access_tokens()
        self.assertEqual(driver.list("/"), [])
        self.assertEqual(self.fake.token_requests - before, 1)

    def test_rotated_refresh_token_is_saved(self):
        self.knob("rotate_refresh", True)
        config = self.config(base_path=self.base_path)
        old = config["refresh_token"]
        saved = []
        driver = self.Driver(config, on_update=saved.append)
        self.addCleanup(driver.close)
        driver.list("/")
        self.assertEqual(len(saved), 1)
        new = saved[0]["refresh_token"]
        self.assertNotEqual(new, old)
        self.assertEqual(driver.config["refresh_token"], new)
        self.assertNotIn(old, self.fake.refresh_tokens)

    def test_revoked_or_missing_refresh_token(self):
        with self.assertRaises(AuthError) as caught:
            self.new_driver(refresh_token="1//revoked").list("/")
        self.assertIn("重新授权", caught.exception.message)
        with self.assertRaises(AuthError) as caught:
            self.new_driver(refresh_token="").list("/")
        self.assertIn("还没有授权", caught.exception.message)

    def test_401_is_retried_once_with_a_new_token(self):
        self.d.list("/")
        before = self.fake.token_requests
        self.fake.inject("GET", r"^/drive/v3/files$", drive_error(401, "authError", "Invalid Credentials"))
        self.assertEqual(self.d.list("/"), [])
        self.assertEqual(self.fake.token_requests - before, 1)
        self.fake.inject("GET", r"^/drive/v3/files$", drive_error(401, "authError"), times=2)
        with self.assertRaises(AuthError):
            self.d.list("/")


class DigitTests(unittest.TestCase):
    def test_the_upload_progress_in_ascii_digits_only(self):
        """STOR-17: a 308's Range of "bytes=0-²" passed isdigit(), and int() raised a ValueError."""
        odd = httpx.Response(308, headers=[(b"range", "bytes=0-²".encode())])
        with self.assertRaises(BackendError):
            google.GoogleDriveDriver._persisted(odd)


class ListingTests(Base):
    def test_a_redirect_the_api_doesnt_make(self):
        """STOR-15: a 3xx was taken for success, its body for the answer: the Drive API makes none, only a proxy
        in the way answers one."""
        self.fake.inject("GET", r"^/drive/v3/files$",
                         Response(302, b"<html>moved</html>", {"Location": "https://proxy.example/"}))
        with self.assertRaises(BackendError) as caught:
            self.d.list("/")
        self.assertIn("HTTP 302", caught.exception.message)
        self.assertEqual(self.d.list("/"), [])

    def test_paging(self):
        self.knob("max_page_size", 3)
        for i in range(10):
            self.fake.add(self.base, f"f{i:02}.txt", data=b"x" * i)
        entries = self.d.list("/")
        self.assertEqual(sorted(e.name for e in entries), [f"f{i:02}.txt" for i in range(10)])
        self.assertEqual({e.name: e.size for e in entries}["f07.txt"], 7)

    def test_duplicates_are_listed_once(self):
        self.fake.add(self.base, "dup.txt", data=b"old", modified="2020-01-01T00:00:00.000Z")
        self.fake.add(self.base, "dup.txt", data=b"newer", modified="2024-01-01T00:00:00.000Z")
        self.fake.add(self.base, "same", data=b"a file")
        self.fake.add(self.base, "same", FOLDER)
        entries = {e.name: e for e in self.d.list("/")}
        self.assertEqual(sorted(entries), ["dup.txt", "same"])
        self.assertEqual(entries["dup.txt"].size, 5)  # the most recently modified one
        self.assertTrue(entries["same"].is_dir)  # a folder before a file
        self.assertEqual(self.get("/dup.txt"), b"newer")
        self.assertTrue(self.d.stat("/same").is_dir)
        # HFile never makes another one.
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/same")
        self.put("/other.txt", b"o")
        with self.assertRaises(AlreadyExists):
            self.d.rename("/other.txt", "dup.txt")
        # Writing replaces the content of the one that is shown.
        self.put("/dup.txt", b"third")
        self.assertEqual(self.get("/dup.txt"), b"third")
        self.assertEqual(len([i for i in self.fake.children(self.base) if i["name"] == "dup.txt"]), 2)

    def test_concurrent_writes_make_no_duplicates(self):
        errors = []

        def write(n):
            try:
                self.put("/race.txt", f"writer {n}".encode())
            except Exception as error:  # pragma: no cover - shown by the assertion
                errors.append(error)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len([i for i in self.fake.children(self.base) if i["name"] == "race.txt"]), 1)

    def test_names_that_cant_be_paths_are_left_out(self):
        for name in ["a/b.txt", "..", ".", ""]:
            self.fake.add(self.base, name, data=b"x")
        self.fake.add(self.base, "fine.txt", data=b"x")
        self.assertEqual(self.names(), ["fine.txt"])

    def test_odd_names_in_queries(self):
        for name in ["it's.txt", "back\\slash.txt", 'quo"te', "a' and name = 'b", "x\\'y", "it's a 'plan'.docx"]:
            with self.subTest(name=name):
                path = "/" + name
                self.put(path, name.encode())
                self.assertIn(name, self.names())
                self.assertEqual(self.get(path), name.encode())
                self.d.rename(path, name + " 2")
                self.assertEqual(self.get(path + " 2"), name.encode())
                self.d.mkdir("/dir " + name)
                self.put("/dir " + name + "/in", b"in")
                self.assertEqual(self.names("/dir " + name), ["in"])

    def test_confined_to_the_base_folder(self):
        parent = self.fake.folder("/t")
        self.fake.add(parent, "secret.txt", data=b"secret")
        sibling = self.fake.folder(self.base_path + "-sibling")
        self.fake.add(sibling, "secret.txt", data=b"secret")
        self.assertEqual(self.d.list("/"), [])
        with self.assertRaises(NotFound):
            self.d.stat("/secret.txt")
        self.assertEqual(self.d.search("/", "secret", 10), [])

    def test_base_path_missing(self):
        driver = self.new_driver(base_path="/no/such/folder")
        with self.assertRaises(NotFound) as caught:
            driver.check()
        self.assertIn("基路径", caught.exception.message)
        self.fake.add(self.base, "file", data=b"x")
        with self.assertRaises(NotADirectory):
            self.new_driver(base_path=self.base_path + "/file").check()

    def test_root_of_my_drive(self):
        fake = FakeDrive()
        self.addCleanup(fake.close)
        driver = driver_class(fake)({"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET,
                                     "refresh_token": fake.issue_refresh_token(), "base_path": "/"})
        self.addCleanup(driver.close)
        self.assertEqual(driver.list("/"), [])
        driver.write("/top.txt", io.BytesIO(b"top"), 3)
        self.assertEqual(fake.child(fake.root_id, "top.txt")["data"], b"top")
        self.assertEqual([e.path for e in driver.list("/")], ["/top.txt"])
        self.assertTrue(driver.stat("/").is_dir)

    def test_root_is_protected(self):
        with self.assertRaises(DriverError):
            self.d.delete("/")
        with self.assertRaises(DriverError):
            self.d.rename("/", "x")
        self.d.mkdir("/a")
        with self.assertRaises(DriverError):
            self.d.move("/", "/a")
        with self.assertRaises(DriverError):
            self.d.copy("/", "/a")
        self.assertFalse(self.fake.items[self.base]["trashed"])

    def test_folders_changed_elsewhere_are_found_again(self):
        self.d.mkdir("/cached")
        self.assertEqual(self.d.list("/cached"), [])  # its id is cached now
        old = self.fake.child(self.base, "cached")
        old["trashed"] = True
        new = self.fake.add(self.base, "cached", FOLDER)
        self.fake.add(new, "new.txt", data=b"new")
        self.assertEqual(self.d.stat("/cached/new.txt").size, 3)
        self.assertEqual(self.names("/cached"), ["new.txt"])

    def test_changes_check_cached_folders(self):
        # Renamed in Google Drive, and a new folder took the old name: a write goes to the new one.
        self.d.mkdir("/inbox")
        self.d.list("/inbox")  # cached now
        old = self.fake.child(self.base, "inbox")
        with self.fake.lock:
            old["name"] = "inbox-archived"
        fresh = self.fake.add(self.base, "inbox", FOLDER)
        self.put("/inbox/new.txt", b"n")
        self.assertEqual([i["name"] for i in self.fake.children(fresh)], ["new.txt"])
        self.assertEqual(self.fake.children(old["id"]), [])
        # Moved out of the base: neither written into nor deleted through the old path. (A check
        # counts for verify_ttl seconds; none here, as if the move came after it. Not 0: on Windows the
        # clock moves every 16 ms, and a check in the same tick would still count.)
        d = self.new_driver(driver_class(self.fake, verify_ttl=-1.0), base_path=self.base_path)
        outside = self.fake.folder("/outside-" + uuid.uuid4().hex[:8])
        d.mkdir("/share")
        d.write("/share/keep.txt", io.BytesIO(b"k"), 1)
        d.list("/share")
        share = self.fake.child(self.base, "share")
        with self.fake.lock:
            share["parents"] = [outside]
        with self.assertRaises(NotFound):
            d.delete("/share/keep.txt")
        d.write("/share/planted.txt", io.BytesIO(b"x"), 1)  # makes a new /share in the base
        self.assertEqual(sorted(i["name"] for i in self.fake.children(share["id"])), ["keep.txt"])
        self.assertFalse(self.fake.child(share["id"], "keep.txt")["trashed"])

    def test_listing_forgets_folders_that_went_away(self):
        self.d.mkdir("/gone")
        self.put("/gone/a.txt", b"a")
        self.d.list("/gone")  # cached
        outside = self.fake.folder("/outside-" + uuid.uuid4().hex[:8])
        with self.fake.lock:
            self.fake.child(self.base, "gone")["parents"] = [outside]
        self.assertEqual(self.d.list("/"), [])
        with self.assertRaises(NotFound):
            self.d.stat("/gone/a.txt")

    def test_misses_are_cheap(self):
        # A missing name below cached folders (a site probing for readme files) costs its query
        # and one check of the nearest cached folder, and keeps the cache.
        self.put("/a/b/c/x.txt", b"x")
        self.put("/other/y.txt", b"y")
        self.d.stat("/a/b/c/x.txt")
        self.d.stat("/other/y.txt")
        count = len(self.fake.log)
        self.d.stat("/other/y.txt")
        hit = len(self.fake.log) - count
        count = len(self.fake.log)
        with self.assertRaises(NotFound):
            self.d.stat("/a/b/c/README.md")
        self.assertEqual(len(self.fake.log) - count, 2)
        count = len(self.fake.log)
        self.d.stat("/other/y.txt")
        self.assertEqual(len(self.fake.log) - count, hit)


class ShortcutAndWorkspaceTests(Base):
    def test_shortcuts(self):
        target_dir = self.fake.add(self.base, "target", FOLDER)
        self.fake.add(target_dir, "in.txt", data=b"inside")
        target_file = self.fake.add(self.base, "f.bin", data=b"0123456789")
        self.fake.add(self.base, "sc-dir", SHORTCUT, target=target_dir)
        file_shortcut = self.fake.add(self.base, "sc-file", SHORTCUT, target=target_file)
        entries = {e.name: e for e in self.d.list("/")}
        self.assertTrue(entries["sc-dir"].is_dir)
        self.assertFalse(entries["sc-file"].is_dir)
        self.assertEqual(self.names("/sc-dir"), ["in.txt"])
        self.assertEqual(self.get("/sc-dir/in.txt"), b"inside")
        self.assertEqual(self.d.stat("/sc-file").size, 10)
        self.assertEqual(self.get("/sc-file", 2, 3), b"234")
        with self.assertRaises(DriverError):
            self.put("/sc-file", b"no")
        # rename, move and delete act on the shortcut itself
        self.d.rename("/sc-dir", "sc2")
        self.assertEqual(self.fake.items[target_dir]["name"], "target")
        self.d.mkdir("/dst")
        self.d.copy("/sc2", "/dst")
        copied = self.fake.child(self.fake.child(self.base, "dst")["id"], "sc2")
        self.assertEqual(copied["mimeType"], SHORTCUT)
        self.assertEqual(copied["shortcutDetails"]["targetId"], target_dir)
        self.d.delete("/sc-file")
        self.assertTrue(self.fake.items[file_shortcut]["trashed"])
        self.assertFalse(self.fake.items[target_file]["trashed"])
        self.assertEqual(self.get("/f.bin"), b"0123456789")

    def test_shortcuts_outside_the_base_are_hidden(self):
        # A shortcut can point anywhere in Drive: like a symlink of local storage, one whose
        # target is outside the base folder is left out unless outside_shortcuts is on.
        private = self.fake.folder("/private-" + uuid.uuid4().hex[:8])
        self.fake.add(private, "salary.xlsx", data=b"secret numbers")
        self.fake.add(self.base, "link", SHORTCUT, target=private)
        self.fake.add(self.base, "plain.txt", data=b"p")
        self.assertEqual(self.names("/"), ["plain.txt"])
        for call in (lambda: self.d.list("/link"), lambda: self.d.stat("/link"),
                     lambda: self.get("/link/salary.xlsx"), lambda: self.d.delete("/link/salary.xlsx")):
            with self.assertRaises(NotFound):
                call()
        with self.assertRaises(AlreadyExists):  # "link" can't become a folder: the name is taken
            self.put("/link/planted.txt", b"x")
        self.assertEqual(sorted(i["name"] for i in self.fake.children(private)), ["salary.xlsx"])
        self.assertFalse(self.fake.items[self.fake.child(private, "salary.xlsx")["id"]]["trashed"])
        with self.assertRaises(AlreadyExists):  # still no second "link" next to it
            self.d.mkdir("/link")
        self.assertEqual(self.d.search("/", "link", 10), [])
        allowed = self.new_driver(base_path=self.base_path, outside_shortcuts=True)
        self.assertEqual(sorted(e.name for e in allowed.list("/")), ["link", "plain.txt"])
        self.assertEqual([e.path for e in allowed.search("/", "link", 10)], ["/link"])
        self.assertEqual(allowed.open("/link/salary.xlsx").read(), b"secret numbers")

    def test_looping_shortcuts_are_hidden(self):
        sub = self.fake.add(self.base, "sub", FOLDER)
        self.fake.add(sub, "to-base", SHORTCUT, target=self.base)  # the base itself: a loop
        self.fake.add(sub, "to-sub", SHORTCUT, target=sub)  # its own folder: a loop
        self.fake.add(self.base, "to-sub", SHORTCUT, target=sub)  # elsewhere in the base: fine
        above = self.fake.folder(self.base_path.rsplit("/", 1)[0])
        self.fake.add(sub, "to-above", SHORTCUT, target=above)  # above the base: a loop too
        for driver in (self.d, self.new_driver(base_path=self.base_path, outside_shortcuts=True)):
            self.assertEqual(sorted(e.name for e in driver.list("/")), ["sub", "to-sub"])
            self.assertEqual(driver.list("/sub"), [])
            self.assertEqual(sorted(e.name for e in driver.list("/to-sub")), [])

    def test_workspace_files_are_exported(self):
        docx = b"PK-docx-" + os.urandom(100)
        self.fake.add(self.base, "Plan", DOC, exports={DOCX: docx})
        self.fake.add(self.base, "Budget", SHEET, exports={XLSX: b"PK-xlsx"})
        self.fake.add(self.base, "Survey", FORM)
        entries = {e.name: e for e in self.d.list("/")}
        self.assertEqual(sorted(entries), ["Budget.xlsx", "Plan.docx"])  # forms can't be exported: left out
        self.assertIsNone(entries["Plan.docx"].size)  # not Drive's storage size
        self.assertFalse(entries["Plan.docx"].is_dir)
        self.assertIsNone(self.d.stat("/Plan.docx").size)
        self.assertEqual(self.get("/Plan.docx"), docx)
        self.assertEqual(self.get("/Plan.docx", 3, 5), docx[3:8])
        self.assertEqual(self.get("/Plan.docx", 100), docx[100:])
        with self.d.open("/Budget.xlsx") as stream:
            self.assertEqual(stream.content_type, XLSX)
            self.assertEqual(b"".join(stream), b"PK-xlsx")
        with self.assertRaises(DriverError):
            self.put("/Plan.docx", b"binary")
        for path in ("/Survey", "/Plan", "/Budget"):
            with self.assertRaises(NotFound):
                self.d.stat(path)
        self.knob("export_limit", 10)
        with self.assertRaises(DriverError) as caught:
            self.get("/Plan.docx")
        self.assertIn("10 MB", caught.exception.message)

    def test_workspace_files_are_listed_with_the_extension_of_their_export(self):
        """The site takes a download's file name and type from the entry's name: without the extension a
        Doc downloads as application/octet-stream, a file "Plan" no program opens."""
        for name, mime in {"Plan": DOC, "Budget": SHEET, "Deck": SLIDES, "Diagram": DRAWING, "Code": SCRIPT}.items():
            self.fake.add(self.base, name, mime)
        notes = self.fake.add(self.base, "Notes.docx", DOC)  # a Doc whose Drive name ends in .docx already
        self.fake.add(self.base, "Notes link", SHORTCUT, target=notes)
        names = self.names()
        self.assertEqual(names, ["Budget.xlsx", "Code.json", "Deck.pptx", "Diagram.pdf", "Notes link.docx",
                                 "Notes.docx.docx", "Plan.docx"])
        for name in names:
            with self.subTest(name=name), self.d.open("/" + name) as stream:
                if not name.endswith(".json"):  # Apps Script's own JSON type; the site sends application/json
                    self.assertEqual(filetypes.guess_type(name)[0], stream.content_type)
                self.assertTrue(b"".join(stream).startswith(b"exported "))
                self.assertEqual(self.d.stat("/" + name).name, name)
        self.assertEqual(self.get("/Notes link.docx"), self.get("/Notes.docx.docx"))
        self.assertIn("/Plan.docx", [e.path for e in self.d.search("/", "plan", 10)])

    def test_a_file_of_the_listed_name_comes_first(self):
        self.fake.add(self.base, "Plan", DOC, exports={DOCX: b"exported"})
        real = self.fake.add(self.base, "Plan.docx", data=b"PK a real docx")
        entries = {e.name: e for e in self.d.list("/")}
        self.assertEqual(sorted(entries), ["Plan.docx"])
        self.assertEqual(entries["Plan.docx"].size, 14)
        self.assertEqual(self.get("/Plan.docx"), b"PK a real docx")
        self.assertEqual([e.size for e in self.d.search("/", "plan", 10)], [14])
        self.put("/Plan.docx", b"new content")  # the real file's, the Doc keeps its own
        self.assertEqual(self.fake.items[real]["data"], b"new content")
        self.d.delete("/Plan.docx")
        self.assertTrue(self.fake.items[real]["trashed"])
        self.assertEqual(self.get("/Plan.docx"), b"exported")  # the Doc is the one there now

    def test_writes_go_by_the_listed_name(self):
        plan = self.fake.add(self.base, "Plan", DOC, exports={DOCX: b"the plan"})
        # An upload never replaces or converts it; "Plan" without the extension is another name.
        with self.assertRaises(DriverError) as caught:
            self.put("/Plan.docx", b"PK a real docx")
        self.assertNotIsInstance(caught.exception, AlreadyExists)
        self.assertEqual([i["name"] for i in self.fake.children(self.base)], ["Plan"])
        self.put("/Plan", b"a file called Plan")
        self.assertEqual(self.names(), ["Plan", "Plan.docx"])
        self.assertEqual(self.get("/Plan.docx"), b"the plan")
        self.assertEqual(self.get("/Plan"), b"a file called Plan")
        # The name it is listed under is taken.
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/Plan.docx")
        self.put("/other.txt", b"o")
        with self.assertRaises(AlreadyExists):
            self.d.rename("/other.txt", "Plan.docx")
        # A rename keeps the extension; Drive gets the name without it.
        self.d.rename("/Plan.docx", "Q3.docx")
        self.assertEqual(self.fake.items[plan]["name"], "Q3")
        self.assertEqual(self.get("/Q3.docx"), b"the plan")
        for wrong in ("Q4", "Q4.pdf", "Q4.DOCX"):
            with self.subTest(new_name=wrong), self.assertRaises(DriverError) as caught:
                self.d.rename("/Q3.docx", wrong)
            self.assertIn(".docx", caught.exception.message)
        self.assertEqual(self.fake.items[plan]["name"], "Q3")
        self.put("/Q3", b"no clash")  # a file "Q3" next to the Doc "Q3": listed apart
        # Move, copy and delete act on the Doc itself.
        self.d.mkdir("/dst")
        self.d.move("/Q3.docx", "/dst")
        dst = self.fake.child(self.base, "dst")["id"]
        self.assertEqual(self.fake.items[plan]["parents"], [dst])
        self.d.mkdir("/copies")
        self.d.copy("/dst/Q3.docx", "/copies")
        copied = self.fake.children(self.fake.child(self.base, "copies")["id"])
        self.assertEqual([(i["name"], i["mimeType"]) for i in copied], [("Q3", DOC)])
        self.assertEqual(self.get("/copies/Q3.docx"), b"the plan")
        with self.assertRaises(AlreadyExists):
            self.d.copy("/dst/Q3.docx", "/copies")
        self.d.delete("/dst/Q3.docx")
        self.assertTrue(self.fake.items[plan]["trashed"])
        self.assertEqual(self.get("/Q3"), b"no clash")

    def test_shortcuts_to_trashed_targets_are_broken(self):
        """Drive keeps a trashed file readable by id, so through a shortcut too: HFile takes such a shortcut
        for a broken one, and doesn't serve what its owner threw away."""
        keep = self.fake.add(self.base, "keep", FOLDER)
        secret = self.fake.add(keep, "contract.pdf", data=b"confidential")
        docs = self.fake.add(keep, "docs", FOLDER)
        self.fake.add(docs, "inside.txt", data=b"inside")
        self.fake.add(self.base, "contract-link", SHORTCUT, target=secret)
        self.fake.add(self.base, "docs-link", SHORTCUT, target=docs)
        self.assertEqual(self.names(), ["contract-link", "docs-link", "keep"])
        self.assertEqual(self.get("/contract-link"), b"confidential")
        with self.fake.lock:
            self.fake.items[secret]["trashed"] = True  # the file itself
            self.fake.items[keep]["trashed"] = True  # "docs" with the folder it is in
        for call in (lambda: self.get("/contract-link"), lambda: self.d.stat("/contract-link")):
            with self.assertRaises(NotFound):
                call()
        for driver in (self.new_driver(base_path=self.base_path),
                       self.new_driver(base_path=self.base_path, outside_shortcuts=True)):
            self.assertEqual(driver.list("/"), [])
            for call in (lambda: driver.stat("/contract-link"), lambda: driver.open("/contract-link"),
                         lambda: driver.list("/docs-link"), lambda: driver.stat("/docs-link/inside.txt")):
                with self.assertRaises(NotFound):
                    call()

    def test_no_uploads_into_a_trashed_target(self):
        docs = self.fake.add(self.base, "docs", FOLDER)
        self.fake.add(self.base, "docs-link", SHORTCUT, target=docs)
        sub = self.fake.add(self.base, "sub", FOLDER)
        self.fake.add(sub, "docs-link2", SHORTCUT, target=docs)  # not looked at before the change
        self.assertEqual(self.names("/docs-link"), [])  # the shortcut's folder is cached now
        with self.fake.lock:
            self.fake.items[docs]["trashed"] = True
        for driver in (self.d, self.new_driver(base_path=self.base_path)):
            for path in ("/sub/docs-link2/new.txt", "/docs-link/new.txt"):
                with self.subTest(path=path), self.assertRaises(DriverError):
                    driver.write(path, io.BytesIO(b"hello"), 5)
        self.assertEqual(self.fake.children(docs, trashed=True), [])

    def test_a_shortcut_that_isnt_shown_doesnt_hide_a_file(self):
        outside = self.fake.folder("/private-" + uuid.uuid4().hex[:8])
        real = self.fake.add(self.base, "x", data=b"a real file")
        self.fake.add(self.base, "x", SHORTCUT, target=outside)  # a folder before a file, but not followed
        self.assertEqual(self.names(), ["x"])
        self.assertFalse(self.d.stat("/x").is_dir)
        self.assertEqual(self.get("/x"), b"a real file")
        self.put("/x", b"replaced")  # the file that is shown
        self.assertEqual(self.fake.items[real]["data"], b"replaced")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/x")
        self.assertEqual(len([i for i in self.fake.children(self.base) if i["name"] == "x"]), 2)

    def test_shortcut_targets_in_one_folder_share_the_walk(self):
        deep = self.fake.folder(self.base_path + "/a/b/c/d/e")
        links = self.fake.add(self.base, "links", FOLDER)
        for i in range(20):
            self.fake.add(links, f"link{i}", SHORTCUT, target=self.fake.add(deep, f"f{i}.txt", data=b"x"))
        self.d.list("/")
        count = len(self.fake.log)
        self.assertEqual(len(self.d.list("/links")), 20)
        gets = [p for m, p, q, _ in self.fake.log[count:] if m == "GET" and p.startswith("/drive/v3/files/")]
        self.assertEqual(len(gets), len(set(gets)))  # each target, and each folder above them, once
        self.assertLessEqual(len(gets), 20 + 5 + 4)  # targets, a-e, and links up to My Drive (loop check)

    def test_folders_in_another_drive(self):
        """A shortcut can lead out of the storage's drive (from a shared drive into My Drive, into another
        shared drive): such a folder's children aren't in the storage's corpus, so they are asked for in all
        of the account's drives. Without that the folder looked empty and every upload made a duplicate."""
        team = self.fake.create_drive("Team " + uuid.uuid4().hex[:6])
        personal = self.fake.folder("/personal-" + uuid.uuid4().hex[:8])
        self.fake.add(personal, "report.pdf", data=b"%PDF real")
        self.fake.add(team, "to-personal", SHORTCUT, target=personal)
        d = self.new_driver(drive_id=team, outside_shortcuts=True)
        self.assertEqual(sorted(e.name for e in d.list("/to-personal")), ["report.pdf"])
        self.assertEqual(d.stat("/to-personal/report.pdf").size, 9)
        for n in range(3):
            d.write("/to-personal/report.pdf", io.BytesIO(b"upload %d" % n), 8)
        d.mkdir("/to-personal/sub")
        with self.assertRaises(AlreadyExists):
            d.mkdir("/to-personal/sub")
        d.write("/to-personal/sub/deeper.txt", io.BytesIO(b"d"), 1)  # a folder of My Drive below it
        d.write("/to-personal/sub/deeper.txt", io.BytesIO(b"e"), 1)
        self.assertEqual(sorted(i["name"] for i in self.fake.children(personal)), ["report.pdf", "sub"])
        self.assertEqual(d.open("/to-personal/report.pdf").read(), b"upload 2")
        self.assertEqual(d.open("/to-personal/sub/deeper.txt").read(), b"e")
        # The storage's own folders are still asked for in its drive only.
        count = len(self.fake.log)
        d.list("/")
        corpora = [q.get("corpora") for m, p, q, _ in self.fake.log[count:] if p == "/drive/v3/files"]
        self.assertEqual(corpora, [["drive"]])
        # From My Drive into a shared drive (the fake's "user" corpus has those items anyway).
        inbox = self.fake.add(team, "inbox", FOLDER)
        self.fake.add(self.base, "to-team", SHORTCUT, target=inbox)
        mine = self.new_driver(base_path=self.base_path, outside_shortcuts=True)
        count = len(self.fake.log)
        self.assertEqual(mine.list("/to-team"), [])
        corpora = [q.get("corpora") for m, p, q, _ in self.fake.log[count:] if p == "/drive/v3/files"]
        self.assertEqual(corpora[-1], ["allDrives"])


class WriteTests(Base):
    driver_attrs = {"chunk_size": 256 * KIB, "retry_delay": 0.01}

    def test_delete_moves_to_the_trash(self):
        self.put("/d/a.txt", b"a")
        folder = self.fake.child(self.base, "d")["id"]
        self.d.delete("/d")
        self.assertTrue(self.fake.items[folder]["trashed"])
        self.assertEqual(self.fake.deleted, [])
        with self.assertRaises(NotFound):
            self.d.stat("/d/a.txt")

    def test_overwrite_keeps_the_file(self):
        self.put("/f.bin", b"one")
        first = self.fake.child(self.base, "f.bin")["id"]
        self.put("/f.bin", b"two")
        self.assertEqual(self.fake.child(self.base, "f.bin")["id"], first)
        self.assertEqual(self.get("/f.bin"), b"two")

    def test_resumable_upload_resumes_after_a_partial_chunk(self):
        # Chunks of 1 MiB, of which Google keeps only 256 KiB three times.
        driver = self.new_driver(driver_class(self.fake, chunk_size=1024 * KIB, retry_delay=0.01),
                                 base_path=self.base_path)
        data = os.urandom(2 * 1024 * KIB + 300 * KIB + 17)
        self.knob("short_puts", 3)
        driver.write("/partial.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.fake.short_puts, 0)
        self.assertEqual(self.get("/partial.bin"), data)

    def test_resumable_upload_resumes_after_server_errors(self):
        data = os.urandom(3 * 256 * KIB + 5)
        self.knob("fail_puts", 2)
        self.put("/flaky.bin", data)
        self.assertEqual(self.get("/flaky.bin"), data)

    def test_resumable_upload_rides_out_an_outage(self):
        """Google: resume after 5xx with exponential backoff (1, 2, 4 ... seconds, plus up to one at random,
        for chunks and status queries alike); HFile used to give up after 3 s."""
        driver = self.new_driver(driver_class(self.fake, chunk_size=256 * KIB), base_path=self.base_path)
        data = os.urandom(3 * 256 * KIB + 5)
        waits = []
        self.knob("fail_puts", 6)
        with mock.patch.object(google.time, "sleep", waits.append):
            driver.write("/outage.bin", io.BytesIO(data), len(data))
        self.assertEqual(self.get("/outage.bin"), data)
        self.assertEqual(len(waits), 6)
        for n, wait in enumerate(waits):
            self.assertTrue(2 ** n <= wait <= 2 ** n + 1, waits)
        # Bounded: an outage that doesn't end fails the upload (after about a minute and a half).
        waits.clear()
        self.knob("fail_puts", 1000)
        with mock.patch.object(google.time, "sleep", waits.append), self.assertRaises(BackendError):
            driver.write("/down.bin", io.BytesIO(data), len(data))
        self.assertEqual(len(waits), 7)
        self.assertTrue(90 < sum(waits) < 110, waits)

    def test_throttled_upload_requests_are_waited_out(self):
        """403 rate limits and 429 on a chunk and on the status query after it: "retry the upload"."""
        data = os.urandom(300 * KIB)
        for status, reason in ((429, "rateLimitExceeded"), (403, "userRateLimitExceeded")):
            with self.subTest(status=status):
                self.fake.inject("PUT", r"^/upload/drive/v3/files", drive_error(status, reason), times=3)
                self.put(f"/throttled-{status}.bin", data)
                self.assertEqual(self.get(f"/throttled-{status}.bin"), data)
        # Throttled while the session is started: waited out too (an upload isn't a page to show).
        self.fake.inject("POST", r"^/upload/drive/v3/files$", drive_error(403, "userRateLimitExceeded"), times=4)
        self.put("/throttled-start.bin", data)
        self.assertEqual(self.get("/throttled-start.bin"), data)

    def test_a_lost_upload_session_starts_over_once(self):
        """Google: a 404 means the session expired, "the upload must be restarted from the beginning"."""
        data = os.urandom(600 * KIB)
        lost = drive_error(404, "notFound", "Upload session not found.")
        self.fake.inject("PUT", r"^/upload/drive/v3/files", lost)
        count = len(self.fake.log)
        self.put("/restarted.bin", data)
        self.assertEqual(self.get("/restarted.bin"), data)
        starts = [e for e in self.fake.log[count:] if e[0] == "POST" and e[1].startswith("/upload/")]
        self.assertEqual(len(starts), 2)
        self.assertEqual(len([i for i in self.fake.children(self.base) if i["name"] == "restarted.bin"]), 1)
        # New content for an existing file: the same (a 410 too).
        self.fake.inject("PUT", r"^/upload/drive/v3/files", drive_error(410, "gone"))
        self.put("/restarted.bin", b"second")
        self.assertEqual(self.get("/restarted.bin"), b"second")
        # Once only, and only when the data can be read again.
        self.fake.inject("PUT", r"^/upload/drive/v3/files", lost, times=2)
        with self.assertRaises(BackendError) as caught:
            self.put("/twice.bin", data)
        self.assertIn("过期", caught.exception.message)
        self.fake.inject("PUT", r"^/upload/drive/v3/files", lost)
        with self.assertRaises(BackendError) as caught:
            self.d.write("/one-way.bin", OneWay(data), len(data))
        self.assertIn("过期", caught.exception.message)
        for name in ("twice.bin", "one-way.bin"):
            with self.assertRaises(NotFound):
                self.d.stat("/" + name)

    def test_no_new_session_when_the_file_is_there_after_all(self):
        """The last chunk got through but its answer was lost, and then the session is gone: starting
        over would make a second file of that name."""

        class LostAnswer(driver_class(self.fake, chunk_size=256 * KIB, retry_delay=0.01)):
            def _session_put(self, session, body, content_range):
                response = super()._session_put(session, body, content_range)
                if content_range.startswith("bytes */"):  # Google has lost the session since
                    return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found",
                                                               "errors": [{"reason": "notFound"}]}})
                if response is not None and response.status_code in (200, 201):
                    return None  # the answer to the last chunk didn't come
                return response

        driver = self.new_driver(LostAnswer, base_path=self.base_path)
        data = os.urandom(300 * KIB)
        with self.assertRaises(BackendError):
            driver.write("/lost.bin", io.BytesIO(data), len(data))
        self.assertEqual([i["data"] for i in self.fake.children(self.base) if i["name"] == "lost.bin"], [data])

    def test_the_start_of_an_upload_is_repeated_after_a_server_error(self):
        """Nothing exists until an upload's data is all there ("The files resource is returned only after you
        successfully upload the file data"), so a session start that failed can be asked for again."""
        self.fake.inject("POST", r"^/upload/drive/v3/files$", drive_error(503, "backendError"), times=2)
        self.put("/started.bin", b"data")
        self.assertEqual(self.get("/started.bin"), b"data")
        self.assertEqual(len([i for i in self.fake.children(self.base) if i["name"] == "started.bin"]), 1)

    def test_resumable_upload_gives_up(self):
        self.knob("fail_puts", 50)
        with self.assertRaises(BackendError):
            self.put("/down.bin", os.urandom(300 * KIB))
        self.fake.fail_puts = 0
        self.fake.inject("PUT", r"^/upload/drive/v3/files", drive_error(404, "notFound"), times=2)
        with self.assertRaises(BackendError) as caught:
            self.put("/gone.bin", b"x")
        self.assertIn("过期", caught.exception.message)

    def test_short_data_fails(self):
        with self.assertRaises(DriverError):
            self.d.write("/short.bin", io.BytesIO(b"abc"), 10)
        with self.assertRaises(NotFound):
            self.d.stat("/short.bin")

    def test_recursive_folder_copy(self):
        self.put("/src/a.txt", b"a")
        self.put("/src/sub/b.txt", b"b")
        self.put("/src/sub/deeper/c.txt", b"c")
        sub = self.fake.child(self.fake.child(self.base, "src")["id"], "sub")["id"]
        self.fake.add(sub, "Notes", DOC, exports={DOCX: b"notes"})
        self.d.mkdir("/dst")
        self.d.copy("/src", "/dst")
        self.assertEqual(self.get("/dst/src/a.txt"), b"a")
        self.assertEqual(self.get("/dst/src/sub/deeper/c.txt"), b"c")
        self.assertEqual(self.get("/dst/src/sub/Notes.docx"), b"notes")
        self.assertEqual(self.get("/src/sub/b.txt"), b"b")

    def test_a_throttled_folder_copy_waits_it_out(self):
        """Drive's quota is per user and minute: a folder copy, one request per item, waits with Google's
        exponential backoff instead of stopping half-done after 1.5 s (and a second try then finding its
        name taken)."""
        for i in range(6):
            self.put(f"/src/file{i}.txt", b"x")
        self.d.mkdir("/dst")
        driver = self.new_driver(driver_class(self.fake), base_path=self.base_path)  # Google's waits
        self.fake.inject("POST", r"/copy$", drive_error(403, "userRateLimitExceeded", "User Rate Limit Exceeded"),
                         times=5)
        waits = []
        with mock.patch.object(google.time, "sleep", waits.append):
            driver.copy("/src", "/dst")
        self.assertEqual(self.names("/dst/src"), [f"file{i}.txt" for i in range(6)])
        self.assertEqual(len(waits), 5)
        for n, wait in enumerate(waits):  # 1, 2, 4 ... seconds, plus up to one at random
            self.assertTrue(2 ** n <= wait <= 2 ** n + 1, waits)

    def test_copy_into_a_shortcut_to_itself_ends(self):
        self.put("/a/x.txt", b"x")
        a = self.fake.child(self.base, "a")["id"]
        self.fake.add(self.base, "sc", SHORTCUT, target=a)  # "/sc" is "/a" under another path
        self.d.copy("/a", "/sc")
        self.assertEqual(self.names("/a"), ["a", "x.txt"])
        self.assertEqual(self.names("/a/a"), ["x.txt"])

    def test_upload_sessions_get_no_token(self):
        del self.fake.log[:]
        self.put("/s.bin", os.urandom(600 * KIB))
        puts = [entry for entry in self.fake.log if entry[0] == "PUT"]
        self.assertEqual(len(puts), 3)  # 256 KiB chunks
        self.assertFalse(any(entry[3] for entry in puts))
        self.assertTrue(all(entry[3] for entry in self.fake.log if entry[0] != "PUT" and entry[1] != "/token"))

    def test_move_keeps_one_parent(self):
        self.put("/a/x.txt", b"x")
        self.d.mkdir("/b")
        self.d.move("/a/x.txt", "/b")
        item = self.fake.child(self.fake.child(self.base, "b")["id"], "x.txt")
        self.assertEqual(len(item["parents"]), 1)
        with self.assertRaises(DriverError):
            self.d.move("/b/x.txt", "/b/x.txt")  # into itself
        self.put("/file", b"f")
        with self.assertRaises(NotADirectory):
            self.d.move("/b/x.txt", "/file")


class SearchAndHelperTests(Base):
    def test_search(self):
        self.put("/a/report-2024.txt", b"1")
        self.put("/b/c/Final REPORT.txt", b"22")
        self.put("/b/other.txt", b"3")
        self.d.mkdir("/reports")
        outside = self.fake.folder("/elsewhere-" + uuid.uuid4().hex[:6])
        self.fake.add(outside, "report outside.txt", data=b"x")
        found = {e.path: e for e in self.d.search("/", "report", 10)}
        self.assertEqual(sorted(found), ["/a/report-2024.txt", "/b/c/Final REPORT.txt", "/reports"])
        self.assertEqual(found["/b/c/Final REPORT.txt"].size, 2)
        self.assertTrue(found["/reports"].is_dir)
        self.assertEqual([e.path for e in self.d.search("/b", "report", 10)], ["/b/c/Final REPORT.txt"])
        self.assertEqual(len(self.d.search("/", "report", 2)), 2)
        self.assertEqual(self.d.search("/", "nothing-like-this", 10), [])
        with self.assertRaises(NotFound):
            self.d.search("/nope", "report", 10)

    def test_search_pages(self):
        self.knob("max_page_size", 2)
        for i in range(7):
            self.put(f"/s{i}/match {i}.txt", b"m")
        self.assertEqual(len(self.d.search("/", "match", 100)), 7)

    def test_search_gives_what_the_paths_open(self):
        """Duplicates made in Google Drive: a result in the folder "a" that isn't listed, or the copy of a file
        that isn't listed, would show a path that opens something else, or nothing."""
        old = self.fake.add(self.base, "a", FOLDER, modified="2020-01-01T00:00:00.000Z")
        new = self.fake.add(self.base, "a", FOLDER, modified="2024-01-01T00:00:00.000Z")
        self.fake.add(old, "report.txt", data=b"OLD report, 22 bytes.")
        self.fake.add(new, "report.txt", data=b"new")
        self.fake.add(new, "report.txt", data=b"older copy", modified="2019-01-01T00:00:00.000Z")
        self.fake.add(old, "only-in-old.txt", data=b"x")
        found = self.d.search("/", "report", 10)
        self.assertEqual([(e.path, e.size) for e in found], [("/a/report.txt", 3)])
        self.assertEqual(self.d.stat("/a/report.txt").size, 3)
        self.assertEqual(self.d.search("/", "only", 10), [])

    def test_drives_helper(self):
        team = self.fake.create_drive("Team 团队")
        other = self.fake.create_drive("Other")
        answer = self.Driver.helper("drives", self.config(), {})
        self.assertEqual(answer["param"], "drive_id")
        self.assertEqual(answer["choices"][0], {"value": "", "label": "我的云端硬盘"})
        self.assertIn({"value": team, "label": "Team 团队"}, answer["choices"])
        self.assertIn({"value": other, "label": "Other"}, answer["choices"])
        with self.assertRaises(AuthError):
            self.Driver.helper("drives", self.config(refresh_token=""), {})
        with self.assertRaises(Unsupported):
            self.Driver.helper("buckets", self.config(), {})
        driver = self.new_driver(drive_id=team)
        driver.write("/in-team.txt", io.BytesIO(b"t"), 1)
        self.assertEqual(self.fake.child(team, "in-team.txt")["driveId"], team)
        with self.assertRaises(NotFound):
            self.new_driver(drive_id="0AnoSuchDrive").list("/")


class ErrorTests(Base):
    driver_attrs = {"retry_delay": 0.01}

    def test_quota(self):
        # quotaExceeded: what Drive said for "The user has exceeded their Drive storage quota" before
        # storageQuotaExceeded; not the API quota.
        for reason in ("storageQuotaExceeded", "quotaExceeded"):
            with self.subTest(reason=reason):
                self.fake.inject("POST", r"^/upload/", drive_error(403, reason))
                with self.assertRaises(DriverError) as caught:
                    self.put("/big.bin", b"x")
                self.assertIn("空间不够", caught.exception.message)

    def test_drive_api_not_enabled(self):
        """The most common setup error. Google's message holds the console page that enables the API, which
        the usual cut to 200 characters lost."""
        url = "https://console.developers.google.com/apis/api/drive.googleapis.com/overview?project=123456789012"
        message = ("Google Drive API has not been used in project 123456789012 before or it is disabled. Enable it "
                   f"by visiting {url} then retry. If you enabled this API recently, wait a few minutes for the "
                   "action to propagate to our systems and retry.")
        self.fake.inject("GET", r"^/drive/v3/files", drive_error(403, "accessNotConfigured", message))
        driver = self.new_driver(base_path=self.base_path)
        with self.assertRaises(AuthError) as caught:
            driver.list("/")
        self.assertIn(url, caught.exception.message)
        self.fake.inject("GET", r"^/drive/v3/files", drive_error(403, "accessNotConfigured", "Access Not Configured."))
        with self.assertRaises(AuthError) as caught:
            self.new_driver(base_path=self.base_path).list("/")
        self.assertIn("https://console.cloud.google.com/apis/library/drive.googleapis.com", caught.exception.message)

    def test_throttling_is_retried_then_reported(self):
        self.fake.inject("GET", r"^/drive/v3/files$",
                         drive_error(429, "rateLimitExceeded", headers={"Retry-After": "0"}))
        self.assertEqual(self.d.list("/"), [])
        self.fake.inject("GET", r"^/drive/v3/files$",
                         drive_error(403, "userRateLimitExceeded", headers={"Retry-After": "0"}), times=3)
        with self.assertRaises(BackendError) as caught:
            self.d.list("/")
        self.assertIn("限流", caught.exception.message)

    def test_reads_for_a_page_wait_little(self):
        """What a page shows is retried twice, soon (0.5 s, 1 s), with jitter: throttled requests of many
        visitors shouldn't all come back at the same moment."""
        driver = self.new_driver(driver_class(self.fake), base_path=self.base_path)  # Google's waits
        driver.list("/")
        self.fake.inject("GET", r"^/drive/v3/files$", drive_error(403, "userRateLimitExceeded"), times=3)
        waits = []
        with mock.patch.object(google.time, "sleep", waits.append), \
                mock.patch.object(google.random, "random", return_value=0.5), self.assertRaises(BackendError):
            driver.list("/")
        self.assertEqual(waits, [0.75, 1.5])

    def test_server_errors(self):
        self.fake.inject("GET", r"^/drive/v3/files$", drive_error(503, "backendError", headers={"Retry-After": "0"}))
        self.assertEqual(self.d.list("/"), [])
        self.fake.inject("GET", r"^/drive/v3/files$", drive_error(500, "backendError", headers={"Retry-After": "0"}),
                         times=3)
        with self.assertRaises(BackendError):
            self.d.list("/")
        self.put("/a.txt", b"a")
        self.fake.inject("PATCH", r"^/drive/v3/files/", drive_error(500, "backendError"))
        with self.assertRaises(BackendError):  # not repeated: a PATCH may have happened
            self.d.rename("/a.txt", "b.txt")

    def test_permissions_and_bad_requests(self):
        self.put("/a.txt", b"a")
        self.fake.inject("PATCH", r"^/drive/v3/files/", drive_error(403, "insufficientFilePermissions",
                                                                     "The user does not have sufficient permissions."))
        with self.assertRaises(DriverError) as caught:
            self.d.delete("/a.txt")
        self.assertIn("没有权限", caught.exception.message)
        self.fake.inject("PATCH", r"^/drive/v3/files/", drive_error(400, "invalidSomething", "Bad thing."))
        with self.assertRaises(DriverError) as caught:
            self.d.delete("/a.txt")
        self.assertIn("invalidSomething", caught.exception.message)
        self.assertIn("Bad thing", caught.exception.message)

    def test_not_found(self):
        with self.assertRaises(NotFound):
            self.d.list("/nope")
        self.put("/f", b"x")
        item = self.fake.child(self.base, "f")
        self.fake.inject("GET", r"^/drive/v3/files/" + item["id"], drive_error(404, "notFound"))
        with self.assertRaises(NotFound):
            self.get("/f")

    def test_unreachable(self):
        cls = type("Unreachable", (self.Driver,), {"api_root": "http://127.0.0.1:9"})
        driver = self.new_driver(cls)
        with self.assertRaises(BackendError):
            driver.list("/")

    def test_validate_config(self):
        self.assertEqual(self.Driver.validate_config(self.config(base_path="/a/b", drive_id="0AbC-d_E")), {})
        errors = self.Driver.validate_config(self.config(base_path="/a/../b", drive_id="0A/../x",
                                                         client_id="id with spaces"))
        self.assertEqual(sorted(errors), ["base_path", "client_id", "drive_id"])
        self.assertEqual(google.GoogleDriveDriver.type, "google-drive")
        self.assertEqual(google.GoogleDriveDriver.chunk_size % (256 * KIB), 0)
        keys = [p.key for p in google.GoogleDriveDriver.params]
        self.assertEqual(keys, ["client_id", "client_secret", "refresh_token", "drive_id", "base_path",
                                "outside_shortcuts"])
        secrets = {p.key for p in google.GoogleDriveDriver.params if p.secret}
        self.assertEqual(secrets, {"client_secret", "refresh_token"})

    def test_a_long_download_error_isnt_read_whole(self):
        """The body of an error answer: its first 64 KiB, for the words in it (http.read_error()). It was read whole,
        however long."""
        self.put("/a.txt", b"abc")
        body = LongBody(b'{"error": {"code": 403, "message": "This file is malware", "errors": [{"reason": "cannotDownloadAbusiveFile"}]}}')
        answer = lambda request: (Response(403, body, JSON, length=LONG)
                                  if request.method == "GET" and request.arg("alt") == "media" else None)
        with self.fake.server.intercepting(answer), self.assertRaisesRegex(DriverError, "可能有害"):
            self.get("/a.txt")
        self.assertLess(body.sent, LONG // 4)


if __name__ == "__main__":
    unittest.main()
