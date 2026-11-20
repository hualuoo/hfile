"""The access policy (apps/storages/access.py): who, which grant, which permission, root confinement, the
three filter modes, folder passwords (nested, revisions, remembered, throttled, D12's static check),
readmes, shares and the aliases."""

import time
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth.models import AnonymousUser
from django.contrib.sessions.middleware import SessionMiddleware
from django.core import signing
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from apps.accounts.models import User
from apps.accounts.permissions import ALL, Permission
from apps.storages import access, config
from apps.storages.access import (
    COPY,
    DELETE,
    DOWNLOAD,
    LINK,
    LIST,
    MKDIR,
    MOVE,
    PREVIEW,
    READ,
    RENAME,
    SHARE,
    SHORT_LINK,
    STAT,
    UPLOAD,
    AccessDenied,
)
from apps.storages.models import FilterMode, FilterRule, FolderPassword, Grant, ReadmeRule, StorageSource
from apps.storages.rules import compile_glob

from .fakes import FakeDriver, FakeTypeMixin

EVERYTHING = [code for code in ALL if not code.startswith("ignore_")]


def entry(path, is_dir=False):
    return SimpleNamespace(path=path, name=path.rsplit("/", 1)[1], is_dir=is_dir)


class PolicyTestCase(FakeTypeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.storage = StorageSource.objects.create(
            key="files", name="文件", type="fake", config=config.encode(FakeDriver, {"endpoint": "x"}))
        self.guest = User.objects.guest()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.factory = RequestFactory()
        self.grant(self.alice, EVERYTHING)

    def grant(self, user, permissions, root="/", enabled=True, storage=None):
        grant, _ = Grant.objects.update_or_create(
            user=user, storage=storage or self.storage,
            defaults={"permissions": list(permissions), "root_path": root, "enabled": enabled})
        return grant

    def request(self, user=None, cookies=None, ip="203.0.113.5"):
        request = self.factory.get("/", REMOTE_ADDR=ip)
        SessionMiddleware(lambda r: HttpResponse()).process_request(request)
        request.user = user if user is not None else AnonymousUser()
        if cookies:
            request.COOKIES.update(cookies)
        return request

    def open(self, user=None, request=None, **kwargs):
        return access.open_storage(request or self.request(user), "files", **kwargs)

    def denied(self, code, call, *args, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception

    def rule(self, model, expression, position=0, **fields):
        return model.objects.create(storage=self.storage, expression=expression, position=position, **fields)


class OpenTests(PolicyTestCase):
    def test_unknown_and_disabled_storages(self):
        error = self.denied(access.STORAGE_NOT_FOUND, access.open_storage, self.request(self.alice), "nope")
        self.assertEqual(error.status_code, 404)
        self.assertEqual(error.detail["code"], "storage_not_found")
        self.storage.enabled = False
        self.storage.save()
        self.denied(access.STORAGE_DISABLED, self.open, self.alice)

    def test_keys_whatever_their_case(self):
        self.assertEqual(access.open_storage(self.request(self.alice), "FILES").storage, self.storage)

    def test_the_guest_needs_a_grant_and_to_be_active(self):
        error = self.denied(access.LOGIN_REQUIRED, self.open)
        self.assertEqual(error.status_code, 401)
        self.grant(self.guest, ["download"])
        self.assertEqual(self.open().actor.user, self.guest)
        self.guest.is_active = False  # anonymous access switched off
        self.guest.save()
        self.denied(access.LOGIN_REQUIRED, self.open)
        self.guest.is_active = True
        self.guest.save()
        self.grant(self.guest, ["download"], enabled=False)
        self.denied(access.LOGIN_REQUIRED, self.open)

    def test_users(self):
        self.assertEqual(self.open(self.alice).actor.user, self.alice)
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.denied(access.NO_GRANT, self.open, self.alice)
        Grant.objects.filter(user=self.alice).delete()
        self.denied(access.NO_GRANT, self.open, self.alice)
        bob = User.objects.create_user("bob", password="pw-bob-2026x", is_active=False)
        self.grant(bob, EVERYTHING)
        self.denied(access.ACTOR_DISABLED, self.open, bob)

    def test_a_logged_in_user_does_not_get_the_guests_grant(self):
        self.grant(self.guest, EVERYTHING)
        carol = User.objects.create_user("carol", password="pw-carol-2026")
        self.denied(access.NO_GRANT, self.open, carol)

    def test_visible_storages_in_order(self):
        other = StorageSource.objects.create(key="other", name="其他", type="fake", position=0)
        off = StorageSource.objects.create(key="off", name="停用", type="fake", enabled=False)
        self.storage.position = 5
        self.storage.save()
        for storage in (other, off):
            self.grant(self.alice, ["download"], storage=storage)
        self.assertEqual([s.key for s, _g in access.visible_storages(access.Actor(self.alice))], ["other", "files"])
        self.grant(self.alice, ["download"], storage=other, enabled=False)
        self.assertEqual([s.key for s, _g in access.visible_storages(access.Actor(self.alice))], ["files"])
        self.alice.is_active = False
        self.assertEqual(access.visible_storages(access.Actor(self.alice)), [])


class PathTests(PolicyTestCase):
    def test_root_confinement(self):
        self.grant(self.alice, EVERYTHING, root="/home/alice")
        a = self.open(self.alice)
        self.assertEqual(a.root, "/home/alice")
        self.assertEqual(a.path(""), "/home/alice")
        self.assertEqual(a.path("/"), "/home/alice")
        self.assertEqual(a.path("docs//a.txt/"), "/home/alice/docs/a.txt")
        self.assertEqual(a.relative("/home/alice/docs"), "/docs")
        self.assertEqual(a.relative("/home/alice"), "/")
        self.assertTrue(a.contains("/home/alice/x"))
        self.assertFalse(a.contains("/home/alicex"))
        self.assertFalse(a.contains("/home"))
        self.assertFalse(a.contains("/home/alice/../bob"))

    def test_escapes_are_refused(self):
        self.grant(self.alice, EVERYTHING, root="/home/alice")
        a = self.open(self.alice)
        for bad in ["..", "../bob", "/a/../../bob", "/./a", "a/\x00", "\ud800", "a\\..\\b", "..\\x", "x\\.",
                    ".. /bob", "a/... /b", "a\\.. \\b", "x\\. ."]:
            with self.subTest(path=bad):
                error = self.denied(access.INVALID_PATH, a.path, bad)
                self.assertEqual(error.status_code, 400)
        self.assertEqual(a.path("a\\b"), "/home/alice/a\\b")  # a backslash alone is a character
        for outside in ["/home/bob", "/home", "/", "relative", "/home/alice/../bob"]:
            with self.subTest(outside=outside):
                self.denied(access.OUTSIDE_ROOT, a.check, LIST, outside)
                self.denied(access.OUTSIDE_ROOT, a.relative, outside)

    def test_a_broken_root_fails_closed(self):
        Grant.objects.filter(user=self.alice).update(root_path="../etc")
        self.denied(access.NO_GRANT, self.open, self.alice)


class PermissionTests(PolicyTestCase):
    def test_each_operation_needs_its_permission(self):
        needs = {UPLOAD: "upload", MKDIR: "new_folder", DELETE: "delete", RENAME: "rename", MOVE: "move",
                 COPY: "copy", LINK: "generate_link", SHORT_LINK: "generate_short_link", SHARE: "create_share_link",
                 PREVIEW: "preview", DOWNLOAD: "download"}
        for operation, code in needs.items():
            with self.subTest(operation=operation):
                self.grant(self.alice, [c for c in EVERYTHING if c != code])
                error = self.denied(access.PERMISSION_DENIED, self.open(self.alice).check, operation, "/x", is_dir=False)
                self.assertEqual(error.detail["permission"], code)
                self.grant(self.alice, [code, "download"] if operation in (LINK, SHORT_LINK) else [code])
                self.open(self.alice).check(operation, "/x", is_dir=False)

    def test_listing_needs_only_the_grant(self):
        self.grant(self.alice, [])
        a = self.open(self.alice)
        a.check(LIST, "/")
        a.check(STAT, "/x")
        self.denied(access.PERMISSION_DENIED, a.check, READ, "/x")

    def test_read_takes_preview_or_download(self):
        for codes in (["preview"], ["download"], ["preview", "download"]):
            self.grant(self.alice, codes)
            self.open(self.alice).check(READ, "/x")

    def test_uploading_folders_needs_new_folder(self):
        self.grant(self.alice, ["upload"])
        a = self.open(self.alice)
        a.check(UPLOAD, "/a.txt")
        error = self.denied(access.PERMISSION_DENIED, a.check, UPLOAD, "/new/a.txt", created=["/new"])
        self.assertEqual(error.detail["permission"], "new_folder")

    def test_a_read_only_storage_gives_no_permission_that_changes_it(self):
        changes = {UPLOAD: "upload", MKDIR: "new_folder", DELETE: "delete", RENAME: "rename", MOVE: "move",
                   COPY: "copy"}
        with mock.patch.object(FakeDriver, "can_write", False):
            a = self.open(self.alice)
            for operation, code in changes.items():
                with self.subTest(operation=operation):
                    self.assertFalse(a.has(code))
                    error = self.denied(access.PERMISSION_DENIED, a.check, operation, "/x", is_dir=False)
                    self.assertEqual(error.detail["permission"], code)
                    self.assertEqual(error.message, "这个存储源是只读的。")
            for operation in (LIST, STAT, READ, PREVIEW, DOWNLOAD, LINK, SHORT_LINK, SHARE):
                a.check(operation, "/x")
        a = self.open(self.alice)
        for operation in changes:
            a.check(operation, "/x", is_dir=False)

    def test_a_storage_without_uploads_gives_no_upload_permission(self):
        with mock.patch.object(FakeDriver, "can_upload", False):
            a = self.open(self.alice)
            self.assertFalse(a.has("upload"))
            error = self.denied(access.PERMISSION_DENIED, a.check, UPLOAD, "/x", is_dir=False)
            self.assertEqual(error.detail["permission"], "upload")
            self.assertEqual(error.message, "这个存储源不能上传文件。")
            for operation in (MKDIR, DELETE, RENAME, MOVE, COPY, LIST, READ, DOWNLOAD, SHARE):
                a.check(operation, "/x", is_dir=False)
            with mock.patch.object(FakeDriver, "can_write", False):  # read-only says so
                error = self.denied(access.PERMISSION_DENIED, self.open(self.alice).check, UPLOAD, "/x", is_dir=False)
                self.assertEqual(error.message, "这个存储源是只读的。")
        self.assertTrue(self.open(self.alice).has("upload"))

    def test_unknown_operations_are_a_bug(self):
        with self.assertRaises(ValueError):
            self.open(self.alice).check("frobnicate", "/x")
        with self.assertRaises(ValueError):
            self.open(self.alice).check_target(DELETE, "/x", is_dir=False)


class FilterTests(PolicyTestCase):
    def filter(self, expression, mode):
        return self.rule(FilterRule, expression, mode=mode)

    def listing(self, a, *paths):
        return [e.path for e in a.visible([entry(p) for p in paths])]

    def test_hidden_is_left_out_but_reachable(self):
        self.filter("/secret", FilterMode.HIDDEN)
        self.filter("*.tmp", FilterMode.HIDDEN)
        a = self.open(self.alice)
        self.assertEqual(self.listing(a, "/secret", "/a.tmp", "/b.txt", "/d"), ["/b.txt", "/d"])
        self.assertEqual(self.listing(a, "/d/a.tmp"), ["/d/a.tmp"])  # *.tmp is a top-level file
        for operation in (LIST, STAT, READ, DOWNLOAD, DELETE, MOVE, LINK):
            a.check(operation, "/secret", is_dir=operation == LIST)
        self.assertEqual(self.listing(a, "/secret/inner"), ["/secret/inner"])  # only the entry itself

    def test_inaccessible_at_and_below(self):
        self.filter("/private", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        self.assertEqual(self.listing(a, "/private", "/public", "/privatex"), ["/public", "/privatex"])
        self.assertEqual(self.listing(a, "/private/a"), [])
        for path in ("/private", "/private/a", "/private/a/b.txt"):
            for operation in (LIST, STAT, READ, UPLOAD, MKDIR, DELETE, RENAME, MOVE, COPY, LINK, SHORT_LINK, SHARE):
                with self.subTest(path=path, operation=operation):
                    self.denied(access.INACCESSIBLE, a.check, operation, path, is_dir=False)
        a.check(LIST, "/public")
        self.denied(access.INACCESSIBLE, a.check, UPLOAD, "/public/x/y.txt", created=["/public/x", "/private"])

    def test_inaccessible_targets(self):
        self.filter("/private", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        for operation in (RENAME, MOVE, COPY):
            self.denied(access.INACCESSIBLE, a.check_target, operation, "/private/landing", is_dir=True)
            a.check_target(operation, "/public/landing", is_dir=True)

    def test_whole_folders_with_inaccessible_content(self):
        self.filter("/data/private", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        for operation in (DELETE, RENAME, MOVE, COPY):
            with self.subTest(operation=operation):
                self.denied(access.INACCESSIBLE, a.check, operation, "/data", is_dir=True)
                self.denied(access.INACCESSIBLE, a.check, operation, "/", is_dir=True)
                a.check(operation, "/other", is_dir=True)
                a.check(operation, "/data/public", is_dir=True)
        a.check(LIST, "/data", is_dir=True)  # only operations on the whole folder

    def test_rules_that_match_by_name_anywhere_move_along(self):
        # "**/.git" still matches a .git folder after the folder above it moved: nothing to refuse.
        self.filter("**/.git", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        for operation in (DELETE, MOVE, COPY, RENAME):
            a.check(operation, "/project", is_dir=True)
        self.denied(access.INACCESSIBLE, a.check, DELETE, "/project/.git", is_dir=True)
        self.denied(access.INACCESSIBLE, a.check, READ, "/project/.git/config")
        self.assertEqual(self.listing(a, "/project/.git", "/project/src"), ["/project/src"])

    def test_disable_download(self):
        self.filter("/videos/*.mp4", FilterMode.DISABLE_DOWNLOAD)
        a = self.open(self.alice)
        self.assertEqual(self.listing(a, "/videos/a.mp4", "/videos/a.txt"), ["/videos/a.txt"])
        for operation in (READ, PREVIEW, DOWNLOAD, LINK, SHORT_LINK):
            self.denied(access.DOWNLOAD_DISABLED, a.check, operation, "/videos/a.mp4")
            a.check(operation, "/videos/a.txt")
        a.check(STAT, "/videos/a.mp4")
        a.check(DELETE, "/videos/a.mp4", is_dir=False)
        # A new name, a copy elsewhere or a move would make it downloadable.
        for operation in (RENAME, MOVE, COPY):
            self.denied(access.DOWNLOAD_DISABLED, a.check, operation, "/videos/a.mp4", is_dir=False)
            self.denied(access.DOWNLOAD_DISABLED, a.check, operation, "/videos", is_dir=True)
        a.check(DELETE, "/videos", is_dir=True)

    def test_disable_download_covers_below_a_folder(self):
        self.filter("/locked", FilterMode.DISABLE_DOWNLOAD)
        a = self.open(self.alice)
        self.denied(access.DOWNLOAD_DISABLED, a.check, DOWNLOAD, "/locked/deep/file.bin")
        a.check(LIST, "/locked/deep", is_dir=True)

    def test_aliases_on_case_insensitive_backends(self):
        self.filter("/Secret", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        for alias in ("/secret", "/SECRET/x", "/secret./x", "/secret /x", "/sEcReT. /y.txt"):
            with self.subTest(alias=alias):
                self.denied(access.INACCESSIBLE, a.check, READ, alias)
        self.assertEqual(self.listing(a, "/SECRET.", "/secrets"), ["/secrets"])

    def test_ignore_hidden_bypasses_all_three(self):
        self.filter("/private", FilterMode.INACCESSIBLE)
        self.filter("/hidden", FilterMode.HIDDEN)
        self.filter("*.mp4", FilterMode.DISABLE_DOWNLOAD)
        self.grant(self.alice, [*EVERYTHING, "ignore_hidden"])
        a = self.open(self.alice)
        self.assertEqual(self.listing(a, "/private", "/hidden", "/a.mp4"), ["/private", "/hidden", "/a.mp4"])
        a.check(READ, "/private/x")
        a.check(DOWNLOAD, "/a.mp4")
        a.check(DELETE, "/", is_dir=True)

    def test_check_served(self):
        self.filter("/private", FilterMode.INACCESSIBLE)
        self.filter("*.mp4", FilterMode.DISABLE_DOWNLOAD)
        access.check_served(self.storage, "/ok.txt")
        self.denied(access.INACCESSIBLE, access.check_served, self.storage, "/private/x")
        self.denied(access.DOWNLOAD_DISABLED, access.check_served, self.storage, "/a.mp4")
        access.check_served(self.storage, "/a.mp4", ignore_hidden=True)
        self.denied(access.INVALID_PATH, access.check_served, self.storage, "/a/../b")
        self.storage.enabled = False
        self.denied(access.STORAGE_DISABLED, access.check_served, self.storage, "/ok.txt")


class PasswordTests(PolicyTestCase):
    def password(self, expression, password, position=0):
        return self.rule(FolderPassword, expression, position=position, password=password)

    def test_no_rules_no_passwords(self):
        a = self.open(self.alice)
        a.check(LIST, "/anything")
        self.assertTrue(a.is_unlocked("/x"))
        self.assertIsNone(a.password_rule("/x"))

    def test_a_protected_folder_and_what_is_in_it(self):
        rule = self.password("/music", "pw1")
        a = self.open(self.alice)
        a.check(LIST, "/")
        for path in ("/music", "/MUSIC/a.mp3", "/music/sub/x"):
            with self.subTest(path=path):
                error = self.denied(access.PASSWORD_REQUIRED, a.check, READ if "." in path else LIST, path)
                self.assertEqual(error.detail["rule"], rule.pk)
                self.assertEqual(error.message, "这个文件夹需要密码。")
        self.assertEqual(self.denied(access.PASSWORD_REQUIRED, a.check, LIST, "/music").detail["path"], "/music")

    def test_a_password_sent_with_the_request(self):
        self.password("/music", " pw1\n")
        a = self.open(self.alice)
        a.check(LIST, "/music", password="pw1")
        a.check(LIST, "/music", password="\r\npw1 ")  # line breaks and spaces at either end don't count
        self.denied(access.PASSWORD_REQUIRED, a.check, LIST, "/music")  # nothing was kept
        self.denied(access.PASSWORD_WRONG, a.check, LIST, "/music", password="pw2")

    def test_unlocking_keeps_it_in_the_session(self):
        rule = self.password("/music", "pw1")
        request = self.request(self.alice)
        a = self.open(request=request)
        self.denied(access.PASSWORD_WRONG, a.unlock, "/music", password="nope")
        self.assertEqual(a.unlock("/music/a/b", password="pw1"), rule)
        a.check(LIST, "/music")
        self.open(request=request).check(READ, "/music/a.mp3")  # another Access, the same session
        self.assertEqual(request.session[access.SESSION_KEY], {f"{self.storage.pk}:{rule.pk}": 1})
        self.assertIsNone(a.unlock("/elsewhere", password="x"))  # nothing to unlock there

    def test_a_new_revision_locks_it_again(self):
        rule = self.password("/music", "pw1")
        request = self.request(self.alice)
        self.open(request=request).unlock("/music", password="pw1")
        rule.password, rule.revision = "pw2", 2
        rule.save()
        self.denied(access.PASSWORD_REQUIRED, self.open(request=request).check, LIST, "/music")
        rule.delete()
        self.password("/music", "pw1")  # the same rule again: a new id
        self.denied(access.PASSWORD_REQUIRED, self.open(request=request).check, LIST, "/music")

    def test_remembered_in_a_signed_cookie(self):
        rule = self.password("/music", "pw1")
        request = self.request(self.alice)
        a = self.open(request=request)
        a.unlock("/music", password="pw1", remember=True)
        response = HttpResponse()
        a.keys.write(response)
        cookie = response.cookies[access.COOKIE_NAME]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Lax")
        self.assertEqual(int(cookie["max-age"]), access.COOKIE_AGE)
        # A new session (the browser was closed), the cookie only:
        later = self.request(self.alice, cookies={access.COOKIE_NAME: cookie.value})
        self.open(request=later).check(LIST, "/music")
        # Bound to who unlocked it: not for somebody else on the same browser, not for the guest.
        bob = User.objects.create_user("bob", password="pw-bob-2026x")
        self.grant(bob, EVERYTHING)
        self.denied(access.PASSWORD_REQUIRED, self.open(request=self.request(bob, cookies={access.COOKIE_NAME: cookie.value})).check, LIST, "/music")
        # A changed rule ends it.
        rule.revision = 2
        rule.save()
        self.denied(access.PASSWORD_REQUIRED, self.open(request=later).check, LIST, "/music")

    def test_a_bad_or_expired_cookie_counts_for_nothing(self):
        rule = self.password("/music", "pw1")
        expired = signing.dumps([[self.storage.pk, rule.pk, 1, self.alice.pk, int(time.time()) - 1]],
                                salt=access.COOKIE_SALT, compress=True)
        for value in ("garbage", expired, signing.dumps("x", salt=access.COOKIE_SALT),
                      signing.dumps([[self.storage.pk, rule.pk, 1, self.alice.pk, int(time.time()) + 99]], salt="other")):
            with self.subTest(value=value[:20]):
                request = self.request(self.alice, cookies={access.COOKIE_NAME: value})
                self.denied(access.PASSWORD_REQUIRED, self.open(request=request).check, LIST, "/music")

    def test_forgetting(self):
        self.password("/music", "pw1")
        request = self.request(self.alice)
        a = self.open(request=request)
        a.unlock("/music", password="pw1", remember=True)
        a.keys.forget()
        response = HttpResponse()
        a.keys.write(response)
        self.assertEqual(response.cookies[access.COOKIE_NAME].value, "")
        self.denied(access.PASSWORD_REQUIRED, self.open(request=request).check, LIST, "/music")

    def test_wrong_passwords_are_throttled_per_address(self):
        self.password("/music", "pw1")
        a = self.open(request=self.request(self.alice, ip="198.51.100.7"))
        for _ in range(access.THROTTLE_FAILURES):
            self.denied(access.PASSWORD_WRONG, a.unlock, "/music", password="wrong")
        error = self.denied(access.PASSWORD_THROTTLED, a.unlock, "/music", password="pw1")  # right, but too late
        self.assertEqual(error.status_code, 429)
        self.assertGreater(error.wait, 0)
        self.denied(access.PASSWORD_THROTTLED, a.check, LIST, "/music", password="pw1")
        other = self.open(request=self.request(self.alice, ip="198.51.100.8"))
        other.unlock("/music", password="pw1")
        # IPv6 counts by /64.
        v6 = self.open(request=self.request(self.alice, ip="2001:db8::1"))
        for _ in range(access.THROTTLE_FAILURES):
            self.denied(access.PASSWORD_WRONG, v6.check, LIST, "/music", password="wrong")
        self.denied(access.PASSWORD_THROTTLED, self.open(request=self.request(self.alice, ip="2001:db8::ffff")).unlock,
                    "/music", password="pw1")

    def test_nested_passwords_first_rule_decides(self):
        inner = self.password("/a/b", "inner", position=1)
        outer = self.password("/a", "outer", position=2)
        a = self.open(self.alice)
        self.assertEqual(a.password_rule("/a/b/x"), inner)
        self.assertEqual(a.password_rule("/a/b"), inner)
        self.assertEqual(a.password_rule("/a/x"), outer)
        self.assertEqual(a.password_rule("/a"), outer)
        a.check(LIST, "/a/b/x", password="inner")
        self.denied(access.PASSWORD_WRONG, a.check, LIST, "/a/b/x", password="outer")
        a.check(LIST, "/a/x", password="outer")
        # The other way round, the inner rule never decides: the outer one covers all of /a first.
        FolderPassword.objects.filter(pk=inner.pk).update(position=3)
        a = self.open(self.alice)
        self.assertEqual(a.password_rule("/a/b/x"), outer)

    def test_whole_folder_operations_need_every_rule_inside(self):
        """D12: a folder operation needs every rule that could cover something at or below the folder,
        up to the first that covers the folder itself (no walk of the storage)."""
        deep = self.password("/a/b/secret", "deep", position=1)
        outer = self.password("/a", "outer", position=2)
        request = self.request(self.alice)
        a = self.open(request=request)
        error = self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/a", is_dir=True)
        self.assertEqual(error.detail["rule"], deep.pk)
        self.assertEqual(error.detail["path"], "/a/b/secret")
        self.assertIn("/a/b/secret", error.message)
        a.unlock("/a/b/secret", password="deep")
        error = self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/a", is_dir=True)
        self.assertEqual(error.detail["rule"], outer.pk)
        a.unlock("/a", password="outer")
        a.check(DELETE, "/a", is_dir=True)
        # A single file needs only the rule that decides for it.
        b = self.open(self.alice)
        b.check(DELETE, "/a/b/secret/x.txt", is_dir=False, password="deep")
        # The folder holding the deeper rule: the first rule covers it, later ones don't matter.
        c = self.open(self.alice)
        c.check(DELETE, "/a/b/secret", is_dir=True, password="deep")

    def test_whole_folder_rules_stop_at_the_first_that_covers_it(self):
        self.password("/a", "outer", position=1)
        self.password("/a/b/secret", "never", position=2)  # dead: /a decides for all of /a first
        self.open(self.alice).check(DELETE, "/a/b", is_dir=True, password="outer")

    def test_rules_with_a_leading_wildcard_are_needed_everywhere(self):
        rule = self.password("**/private", "pp")
        a = self.open(self.alice)
        error = self.denied(access.PASSWORD_REQUIRED, a.check, MOVE, "/projects", is_dir=True)
        self.assertEqual(error.detail["rule"], rule.pk)
        self.assertIsNone(error.detail["path"])  # nowhere to unlock it by path: by its id
        a.unlock(rule_id=rule.pk, password="pp")
        a.check(MOVE, "/projects", is_dir=True)
        self.denied(access.RULE_NOT_FOUND, a.unlock, rule_id=999999, password="pp")
        self.open(self.alice).check(MOVE, "/projects/readme.txt", is_dir=False)  # a file: only its own rule

    def test_ways_around_a_folder_password(self):
        """Operations that could get around a folder's password."""
        self.password("/s1", "s1")
        a = self.open(self.alice)
        # Copying, moving or deleting a protected folder from its open parent.
        for operation in (COPY, MOVE, DELETE, RENAME):
            self.denied(access.PASSWORD_REQUIRED, a.check, operation, "/s1", is_dir=True)
        # Deleting a folder that holds a protected one.
        self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/", is_dir=True)
        a.check(DELETE, "/pub", is_dir=True)
        # Moving or renaming something into the protected place: its landing.
        self.denied(access.PASSWORD_REQUIRED, a.check_target, MOVE, "/s1/x", is_dir=True)
        self.denied(access.PASSWORD_REQUIRED, a.check_target, RENAME, "/s1", is_dir=True)
        self.denied(access.PASSWORD_REQUIRED, a.check_target, COPY, "/s1/x.txt", is_dir=False)
        a.check_target(MOVE, "/pub/x", is_dir=True)
        # Names with "/" and new parent levels: each level on its own.
        self.denied(access.PASSWORD_REQUIRED, a.check, MKDIR, "/s1/new/deeper", created=["/s1/new"])
        self.denied(access.PASSWORD_REQUIRED, a.check, UPLOAD, "/s1/new/x.txt", created=["/s1/new"])
        self.denied(access.PASSWORD_REQUIRED, a.check, MKDIR, "/s1")  # making the protected folder itself
        a.check(UPLOAD, "/pub/new/x.txt", created=["/pub/new"])
        a.check(UPLOAD, "/pub/s1/x.txt", created=["/pub/s1"])  # /s1 is a place, not a name
        # A rule for a level the upload creates, nested before the one of the file's folder.
        made = self.password("/pub/made", "made")
        b = self.open(self.alice)
        error = self.denied(access.PASSWORD_REQUIRED, b.check, UPLOAD, "/pub/made/x.txt", created=["/pub/made"])
        self.assertEqual(error.detail["rule"], made.pk)
        b.unlock(b.path(error.detail["path"]), password="made")  # the hint unlocks it
        b.check(UPLOAD, "/pub/made/x.txt", created=["/pub/made"])

    def test_only_subfolders_protected(self):
        self.password("/secret/*/**", "sub")
        a = self.open(self.alice)
        a.check(LIST, "/secret")
        a.check(DELETE, "/secret/file.txt", is_dir=False)
        self.denied(access.PASSWORD_REQUIRED, a.check, READ, "/secret/sub/file.txt")
        self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/secret", is_dir=True)

    def test_keys_of_a_link(self):
        rule = self.password("/music", "pw1")
        a = self.open(self.alice)
        a.check(READ, "/music/a.mp3", password="pw1")
        self.assertEqual(a.unlocked_keys("/music/a.mp3"), [(rule.pk, 1)])
        b = self.open(self.alice)
        b.check(READ, "/music/a.mp3", keys=[(rule.pk, 1)])
        self.denied(access.PASSWORD_REQUIRED, b.check, READ, "/music/a.mp3", keys=[(rule.pk, 2)])

    def test_ignore_password(self):
        self.password("/music", "pw1")
        self.grant(self.alice, [*EVERYTHING, "ignore_password"])
        a = self.open(self.alice)
        a.check(LIST, "/music")
        a.check(DELETE, "/", is_dir=True)
        self.assertIsNone(a.password_rule("/music"))
        self.assertIsNone(a.unlock("/music", password="whatever"))
        self.assertEqual(a.unlocked_keys("/music/x"), [])

    def test_aliases(self):
        self.password("/Music", "pw1")
        a = self.open(self.alice)
        for alias in ("/music", "/MUSIC/x", "/music./x", "/music /x", "/musıc"):
            with self.subTest(alias=alias):
                if alias == "/musıc":  # a dotless i is another letter
                    a.check(LIST, alias)
                    continue
                self.denied(access.PASSWORD_REQUIRED, a.check, LIST, alias)

    def test_listing_rules_first(self):
        """Hidden rules don't stop a password prompt, an inaccessible one comes first."""
        self.rule(FilterRule, "/music", mode=FilterMode.INACCESSIBLE)
        self.password("/music", "pw1")
        self.denied(access.INACCESSIBLE, self.open(self.alice).check, LIST, "/music", password="pw1")


class MoreEdgeTests(PolicyTestCase):
    def test_open_with_the_row_itself(self):
        self.assertEqual(access.open_storage(self.request(self.alice), self.storage).storage, self.storage)

    def test_search_results_below_an_inaccessible_folder_are_left_out(self):
        self.rule(FilterRule, "/private", mode=FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        found = [entry("/private/deep/a.txt"), entry("/public/a.txt"), entry("/PRIVATE./b")]
        self.assertEqual([e.path for e in a.visible(found)], ["/public/a.txt"])

    def test_the_root_folder(self):
        rule = self.rule(FolderPassword, "/", password="root-pw")
        a = self.open(self.alice)
        self.assertEqual(a.password_rule("/"), rule)
        self.assertFalse(a.is_unlocked("/x/y"))
        self.denied(access.PASSWORD_REQUIRED, a.check, LIST, "/")
        a.unlock("/", password="root-pw")
        self.assertTrue(a.is_unlocked("/x/y"))

    def test_without_remember_no_cookie(self):
        self.rule(FolderPassword, "/music", password="pw1")
        a = self.open(self.alice)
        a.unlock("/music", password="pw1")
        response = HttpResponse()
        a.keys.write(response)
        self.assertNotIn(access.COOKIE_NAME, response.cookies)

    def test_a_password_for_two_rules_at_once(self):
        """One password sent with a folder operation opens every rule it is the password of."""
        self.rule(FolderPassword, "/a/x", password="same", position=1)
        self.rule(FolderPassword, "/a/y", password="same", position=2)
        a = self.open(self.alice)
        a.check(DELETE, "/a", is_dir=True, password="same")
        self.rule(FolderPassword, "/a/z", password="other", position=3)
        b = self.open(self.alice)
        error = self.denied(access.PASSWORD_REQUIRED, b.check, DELETE, "/a", is_dir=True, password="same")
        self.assertEqual(error.detail["path"], "/a/z")

    def test_without_a_request(self):
        """A background job (request None): nothing unlocked, passwords still work per call."""
        self.rule(FolderPassword, "/music", password="pw1")
        a = access.open_storage(None, "files", actor=access.Actor(self.alice))
        self.denied(access.PASSWORD_REQUIRED, a.check, LIST, "/music")
        a.check(LIST, "/music", password="pw1")
        self.assertIsNone(a.keys)


class ShareTests(PolicyTestCase):
    def test_the_share_folder_is_the_root(self):
        self.grant(self.alice, EVERYTHING, root="/home/alice")
        actor = access.share_actor(self.alice, "/home/alice/shared")
        a = self.open(request=self.request(), actor=actor)
        self.assertEqual(a.root, "/home/alice/shared")
        self.assertEqual(a.path("/x"), "/home/alice/shared/x")
        self.denied(access.OUTSIDE_ROOT, a.check, LIST, "/home/alice")
        outside = access.share_actor(self.alice, "/home/bob")
        self.denied(access.OUTSIDE_ROOT, self.open, request=self.request(), actor=outside)
        self.denied(access.OUTSIDE_ROOT, self.open, request=self.request(), actor=access.share_actor(self.alice, "../x"))

    def test_the_owner_must_still_be_able_to(self):
        actor = access.share_actor(self.alice, "/shared")
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertIn("分享者", self.denied(access.NO_GRANT, self.open, request=self.request(), actor=actor).message)
        self.grant(self.alice, EVERYTHING)
        self.alice.is_active = False
        self.alice.save()
        self.assertIn("分享者", self.denied(access.ACTOR_DISABLED, self.open, request=self.request(), actor=actor).message)

    def test_no_bypass_inside_a_share(self):
        """D32: visitors get the filters and folder passwords whatever the owner may skip."""
        self.grant(self.alice, [*EVERYTHING, "ignore_hidden", "ignore_password"])
        self.rule(FilterRule, "/shared/private", mode=FilterMode.INACCESSIBLE)
        self.rule(FolderPassword, "/shared/locked", password="pw")
        visitor = self.request()
        a = self.open(request=visitor, actor=access.share_actor(self.alice, "/shared"))
        self.denied(access.INACCESSIBLE, a.check, LIST, "/shared/private")
        self.denied(access.PASSWORD_REQUIRED, a.check, LIST, "/shared/locked")
        a.unlock("/shared/locked", password="pw")  # the visitor's own session
        a.check(LIST, "/shared/locked")
        own = self.open(self.alice)
        own.check(LIST, "/shared/private")


class ReadmeTests(PolicyTestCase):
    def test_relative_and_absolute(self):
        relative = ReadmeRule.objects.create(storage=self.storage, expression="/", text="r", position=1)
        absolute = ReadmeRule.objects.create(storage=self.storage, expression="/music", text="a", position=2,
                                             path_mode="absolute")
        admin = self.open(self.alice)
        self.assertEqual(admin.readme("/"), relative)
        self.assertEqual(admin.readme("/music"), absolute)
        self.grant(self.guest, ["download"], root="/music")
        guest = self.open()
        self.assertEqual(guest.readme("/music"), relative)  # the guest's "/" is /music: the relative rule first
        relative.position = 3
        relative.save()
        self.assertEqual(self.open().readme("/music"), absolute)
        self.assertIsNone(self.open(self.alice).readme("/other"))
        self.assertEqual(self.open(self.alice).readme("/"), relative)

    def test_readme_files_replace_the_rules(self):
        ReadmeRule.objects.create(storage=self.storage, expression="/", text="r")
        self.storage.readme_from_file = True
        self.storage.save()
        self.assertIsNone(self.open(self.alice).readme("/"))

    def test_first_match_and_globs(self):
        music = ReadmeRule.objects.create(storage=self.storage, expression="/music/**", text="m", position=1)
        ReadmeRule.objects.create(storage=self.storage, expression="/**", text="all", position=2)
        a = self.open(self.alice)
        self.assertEqual(a.readme("/MUSIC/rock"), music)
        self.assertEqual(a.readme("/music").text, "all")
        self.assertIsNone(a.readme("/"))


class DriverTests(PolicyTestCase):
    def test_the_access_gives_the_live_driver(self):
        a = self.open(self.alice)
        self.assertIsInstance(a.driver, FakeDriver)
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        a.storage.enabled = False
        self.denied(access.STORAGE_DISABLED, getattr, a, "driver")


class PermissionCodeTests(TestCase):
    def test_every_code_is_known_to_the_policy(self):
        needed = {code for needs in access._NEEDS.values() for code in needs}
        self.assertTrue(needed <= set(ALL))
        self.assertEqual(set(ALL) - needed - {"ignore_password", "ignore_hidden", "copy_download_link",
                                               "custom_share_key"}, set())
        self.assertEqual(Permission.IGNORE_HIDDEN, "ignore_hidden")


class ReviewedEdgeTests(PolicyTestCase):
    """What an independent review found, kept from coming back."""

    def filter(self, expression, mode):
        return self.rule(FilterRule, expression, mode=mode)

    def test_a_run_of_segments_after_double_star_holds_its_folder(self):
        """**/private/** covers what is inside any "private": renaming that folder would free it."""
        self.filter("**/private/**", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        self.denied(access.INACCESSIBLE, a.check, READ, "/x/private/s.txt")
        for operation in (RENAME, MOVE, COPY, DELETE):
            with self.subTest(operation=operation):
                self.denied(access.INACCESSIBLE, a.check, operation, "/x/private", is_dir=True)
        a.check(RENAME, "/x", is_dir=True)  # what is in /x/private stays in a "private" wherever /x goes
        a.check(RENAME, "/x/public", is_dir=True)

    def test_a_longer_run_and_a_file_pattern(self):
        self.filter("**/videos/*.mp4", FilterMode.DISABLE_DOWNLOAD)
        self.filter("**/x/a/secret", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        self.denied(access.DOWNLOAD_DISABLED, a.check, RENAME, "/a/videos", is_dir=True)
        a.check(RENAME, "/a", is_dir=True)
        a.check(DELETE, "/a/videos", is_dir=True)  # not downloadable is not untouchable
        for folder in ("/r/x/a", "/r/x"):
            with self.subTest(folder=folder):
                self.denied(access.INACCESSIBLE, a.check, MOVE, folder, is_dir=True)
        a.check(MOVE, "/r", is_dir=True)

    def test_a_name_rule_follows_what_it_covers(self):
        self.filter("**/private", FilterMode.INACCESSIBLE)
        self.filter("**.key", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        self.denied(access.INACCESSIBLE, a.check, DELETE, "/x/private", is_dir=True)
        a.check(MOVE, "/x", is_dir=True)
        a.check_target(MOVE, "/y", is_dir=True)
        self.denied(access.INACCESSIBLE, a.check_target, MOVE, "/y/private", is_dir=True)

    def test_a_rule_that_cant_be_followed_holds_every_folder(self):
        self.filter("**/a/**/b", FilterMode.INACCESSIBLE)
        a = self.open(self.alice)
        self.denied(access.INACCESSIBLE, a.check, RENAME, "/anything", is_dir=True)
        a.check(RENAME, "/anything.txt", is_dir=False)

    def test_a_top_level_pattern_holds_only_the_top(self):
        self.filter("*.bak", FilterMode.INACCESSIBLE)
        self.rule(FolderPassword, "/*/x", password="pw")
        a = self.open(self.alice)
        a.check(DELETE, "/deep/a/b/c", is_dir=True)
        a.check(DELETE, "/a/b", is_dir=True)
        self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/a", is_dir=True)  # /a/x may be inside
        self.denied(access.INACCESSIBLE, a.check, DELETE, "/old.bak", is_dir=True)

    def test_a_backslash_is_a_separator_for_the_rules(self):
        self.filter("/secret", FilterMode.INACCESSIBLE)
        self.rule(FolderPassword, "/locked", password="pw")
        a = self.open(self.alice)
        self.denied(access.INACCESSIBLE, a.check, READ, a.path("secret\\file.txt"))
        self.denied(access.PASSWORD_REQUIRED, a.check, LIST, a.path("locked\\sub"))
        rule = self.filter("/a\\b", FilterMode.INACCESSIBLE)
        self.assertEqual(compile_glob(rule.expression).expression, "/a/b")
        self.denied(access.INACCESSIBLE, self.open(self.alice).check, LIST, "/a/b")

    def test_paths_have_a_limit(self):
        a = self.open(self.alice)
        self.denied(access.INVALID_PATH, a.path, "a/" * 300)
        self.denied(access.INVALID_PATH, a.path, "x" * 5000)
        self.assertEqual(a.path("a/" * 100), "/" + "/".join(["a"] * 100))

    def test_a_long_path_against_hard_rules_is_quick(self):
        self.filter("/a/**/b/**/c", FilterMode.INACCESSIBLE)
        self.filter("*-*-*.bak", FilterMode.INACCESSIBLE)
        self.filter("**/**/**/x", FilterMode.HIDDEN)
        a = self.open(self.alice)
        started = time.monotonic()
        a.check(LIST, a.path("a" + "/b" * 250))
        a.check(LIST, a.path("-" * 4000))
        a.visible([entry("/" + "a/" * 1000 + "y")])
        self.assertLess(time.monotonic() - started, 2)

    def test_unlocking_by_id_only_inside_the_root(self):
        inside = self.rule(FolderPassword, "/shared/locked", password="pw1")
        outside = self.rule(FolderPassword, "/elsewhere", password="pw2", position=1)
        anywhere = self.rule(FolderPassword, "**/vault", password="pw3", position=2)
        a = self.open(request=self.request(), actor=access.share_actor(self.alice, "/shared"))
        self.denied(access.RULE_NOT_FOUND, a.unlock, password="pw2", rule_id=outside.pk)
        self.assertEqual(a.unlock(password="pw1", rule_id=inside.pk), inside)
        self.assertEqual(a.unlock(password="pw3", rule_id=anywhere.pk), anywhere)

    def test_link_keys(self):
        rule = self.rule(FolderPassword, "/music", password="pw1")
        a = self.open(self.alice)
        self.assertEqual(a.unlocked_keys("/music/a.mp3"), [])  # nothing entered yet
        b = self.open(self.alice)
        b.check(READ, "/music/a.mp3", keys=[[rule.pk, 1]])  # as JSON gives them back
        b.check(READ, "/music/a.mp3", keys=[(rule.pk, 1), "junk", [1], None])
        self.denied(access.PASSWORD_REQUIRED, b.check, READ, "/music/a.mp3", keys=[[rule.pk, "1"]])

    def test_whole_folder_operations_must_say_is_dir(self):
        a = self.open(self.alice)
        for operation in (DELETE, RENAME, MOVE, COPY):
            with self.subTest(operation=operation):
                with self.assertRaises(TypeError):
                    a.check(operation, "/x")
        with self.assertRaises(TypeError):
            a.check_target(MOVE, "/y")
        a.check(LIST, "/x")

    def test_search_results_leave_out_what_is_behind_a_password(self):
        self.rule(FolderPassword, "/locked", password="pw")
        a = self.open(self.alice)
        found = [entry("/locked", is_dir=True), entry("/locked/inside.txt"), entry("/open/x.txt")]
        self.assertEqual([e.path for e in a.visible(found)], ["/locked", "/open/x.txt"])
        a.unlock("/locked", password="pw")
        self.assertEqual(len(a.visible(found)), 3)

    def test_a_password_sent_with_a_check_opens_the_listing_of_that_request(self):
        # The files app lists a folder with check(LIST, password=...) and then visible(keys=access.opened).
        rule = self.rule(FolderPassword, "/locked", password="pw")
        a = self.open(self.alice)
        found = [entry("/locked/a.txt"), entry("/locked/b.txt")]
        a.check(LIST, "/locked", password="pw")
        self.assertEqual(a.visible(found), [])  # without the keys: as before
        self.assertEqual(len(a.visible(found, keys=a.opened)), 2)
        self.assertFalse(a.is_unlocked("/locked"))
        self.assertTrue(a.is_unlocked("/locked", keys=[[rule.pk, rule.revision]]))
        self.assertFalse(self.open(self.alice).is_unlocked("/locked"))  # nothing kept

    def test_links_need_download_too(self):
        self.grant(self.alice, ["generate_link", "generate_short_link"])
        a = self.open(self.alice)
        for operation in (LINK, SHORT_LINK):
            error = self.denied(access.PERMISSION_DENIED, a.check, operation, "/x")
            self.assertEqual(error.detail["permission"], "download")
        self.assertFalse(a.may(LINK))

    def test_a_hidden_folder_is_not_named_in_a_hint(self):
        self.filter("/a/hidden", FilterMode.HIDDEN)
        self.rule(FolderPassword, "/a/hidden", password="pw")
        self.rule(FolderPassword, "/a/shown", password="pw", position=1)
        a = self.open(self.alice)
        error = self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/a", is_dir=True)
        self.assertIsNone(error.detail.get("path"))
        a.unlock(password="pw", rule_id=error.detail["rule"])
        error = self.denied(access.PASSWORD_REQUIRED, a.check, DELETE, "/a", is_dir=True)
        self.assertEqual(error.detail["path"], "/a/shown")

    def test_password_attempts_at_the_same_moment_count_one_by_one(self):
        import threading

        self.rule(FolderPassword, "/music", password="pw1")
        results = []
        barrier = threading.Barrier(16)
        opened = [self.open(self.alice) for _ in range(16)]  # the database work here, in the test's transaction
        for a in opened:
            a.rules  # noqa: B018

        def attempt():
            a = opened.pop()
            barrier.wait()
            try:
                a.unlock("/music", password="wrong")
            except AccessDenied as error:
                results.append(error.code)

        threads = [threading.Thread(target=attempt) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(results.count(access.PASSWORD_WRONG), access.THROTTLE_FAILURES)
        self.assertEqual(results.count(access.PASSWORD_THROTTLED), 16 - access.THROTTLE_FAILURES)


class UnlockReachEdgeTests(PolicyTestCase):
    """What a password may be tried on: only what the actor can reach (access-tests FINDING 3 and 4)."""

    def test_by_id_only_inside_the_entries_a_share_holds(self):
        sub = self.rule(FolderPassword, "/docs/sub", password="pw1")
        top = self.rule(FolderPassword, "/docs", password="pw2", position=1)
        vault = self.rule(FolderPassword, "**/vault", password="pw3", position=2)
        only_a_file = access.share_actor(self.alice, "/docs", [("a.txt", False)])
        a = self.open(request=self.request(), actor=only_a_file)
        self.denied(access.RULE_NOT_FOUND, a.unlock, password="pw1", rule_id=sub.pk)
        self.denied(access.RULE_NOT_FOUND, a.unlock, password="pw3", rule_id=vault.pk)  # nothing below a file
        self.assertEqual(a.unlock(password="pw2", rule_id=top.pk), top)  # it decides for a.txt
        with_the_folder = access.share_actor(self.alice, "/docs", [("a.txt", False), ("sub", True), ("../x", True)])
        self.assertEqual(with_the_folder.share_entries, (("/docs/a.txt", False), ("/docs/sub", True)))
        b = self.open(request=self.request(), actor=with_the_folder)
        self.assertEqual(b.unlock(password="pw1", rule_id=sub.pk), sub)
        self.assertEqual(b.unlock(password="pw3", rule_id=vault.pk), vault)
        whole = self.open(request=self.request(), actor=access.share_actor(self.alice, "/docs"))
        self.assertEqual(whole.unlock(password="pw1", rule_id=sub.pk), sub)

    def test_not_on_a_folder_that_cant_be_opened(self):
        self.rule(FilterRule, "/locked", mode=FilterMode.INACCESSIBLE)
        self.rule(FolderPassword, "/locked", password="pw")
        a = self.open(self.alice)
        self.denied(access.INACCESSIBLE, a.unlock, "/locked/inside", password="nope")
        self.denied(access.INACCESSIBLE, a.unlock, "/locked", password="pw")
        self.assertEqual(access.password_wait(a.request), 0)  # nothing was tried
        self.grant(self.alice, [*EVERYTHING, "ignore_hidden"])
        self.assertIsNotNone(self.open(self.alice).unlock("/locked", password="pw"))
