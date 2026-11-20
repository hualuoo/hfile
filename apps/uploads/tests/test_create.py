"""Making an upload session (POST /api/v1/uploads/): permissions through apps.storages.access, new folders,
overwriting (D17, D17a), folder passwords (D12), names per driver, paths, limits."""

import json
import os
from unittest import mock

from django.test import Client

from apps.accounts.models import User
from apps.core.testing import TO_THE_SECOND, assert_private
from apps.storages import instances
from apps.storages.models import FilterMode, FilterRule, FolderPassword
from apps.uploads import service, spool
from apps.uploads.models import Kind, State, UploadSession
from apps.uploads.views import CreateThrottle

from .base import API, EVERYTHING, NO_DELETE, UploadTestCase


class CreateTests(UploadTestCase):
    def test_a_session_for_a_proxied_storage(self):
        made = self.create("报告.txt", 3500, "/")
        self.assertEqual(made.status_code, 201, made.content)
        body = made.json()
        self.assertEqual(body["kind"], "proxy")
        self.assertEqual(body["state"], "receiving")
        self.assertEqual(body["received"], 0)
        self.assertEqual(body["chunk_size"], self.chunk)
        self.assertEqual(body["name"], "报告.txt")
        self.assertEqual(body["path"], "/报告.txt")
        self.assertEqual(body["folder"], "/")
        self.assertEqual(body["url"], f"{API}{body['id']}/")
        self.assertRegex(body["expires_at"], TO_THE_SECOND)  # TEXT-01: it was UTC with microseconds
        self.assertNotIn("target", body)
        session = UploadSession.objects.get(pk=body["id"])
        self.assertEqual((session.user, session.owner, session.path, session.size), (self.alice, "", "/报告.txt", 3500))
        # The spool: a folder of mode 700, a file of mode 600 per session.
        assert_private(self, self.spool, 0o700)
        part = spool.path_of(session.id)
        assert_private(self, part)
        self.assertEqual(os.path.getsize(part), 0)

    def test_an_empty_file_is_written_at_once(self):
        made = self.create("empty.txt", 0)
        self.assertEqual(made.status_code, 201, made.content)
        body = made.json()
        self.assertEqual(body["state"], "done")
        self.assertEqual(body["item"]["name"], "empty.txt")
        self.assertEqual((self.root / "empty.txt").read_bytes(), b"")
        self.assertFalse(spool.path_of(body["id"]).exists())
        self.assertEqual(self.signals, [("disk", ["/empty.txt"])])

    def test_an_empty_file_that_cannot_be_written(self):
        from apps.storages.drivers.base import BackendError

        with mock.patch.object(instances.get(self.storage), "write", side_effect=BackendError("坏了。")):
            self.assertCode(self.create("empty.txt", 0), 502, "storage_error")
        self.assertFalse(UploadSession.objects.exists())
        self.assertEqual(list(self.spool.iterdir()), [])

    def test_what_the_storage_said_is_only_for_admins(self):
        """SEC-01: the backend's own words (its address, a server's reply) go to the log; the uploader
        gets the file API's plain ones, the guest too; an admin gets what the storage said."""
        from apps.storages.drivers.base import AuthError, BackendError, DriverError

        said = "connect to 10.0.0.5:21 failed: 530 Login incorrect"
        self.grant(self.guest, ["upload"])
        admin = User.objects.create_user("boss", password="pw-boss-2026", is_staff=True)
        self.grant(admin, EVERYTHING)
        admin_client = Client()
        admin_client.force_login(admin)
        driver = instances.get(self.storage)
        for error, status, code, plain in (
            (BackendError(said), 502, "storage_error", "连接存储失败，请稍后再试。"),
            (AuthError(said), 502, "storage_auth", "存储源的账号或授权出了问题，请联系管理员。"),
            (DriverError(said), 400, "failed", "操作没有完成：存储返回了错误。"),  # like the file API: not 502
        ):
            for who, client, expected in (("alice", self.client, plain), ("guest", Client(), plain), ("admin", admin_client, said)):
                with self.subTest(error=type(error).__name__, who=who), mock.patch.object(driver, "stat", side_effect=error):
                    with self.assertLogs("hfile.uploads", "WARNING") as logged:
                        body = self.assertCode(self.create("a.txt", 10, client=client), status, code)
                    self.assertEqual(body["detail"], expected)
                    self.assertIn(said, "\n".join(logged.output))
        self.assertFalse(UploadSession.objects.exists())

    def test_the_root_is_the_users(self):
        (self.root / "home").mkdir()
        self.grant(self.alice, EVERYTHING, root="/home")
        body = self.create("a.txt", 10, "/").json()
        self.assertEqual(body["path"], "/a.txt")
        self.assertEqual(UploadSession.objects.get(pk=body["id"]).path, "/home/a.txt")

    def test_without_the_upload_permission(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
        self.assertCode(self.create("a.txt", 10), 403, "permission_denied")
        self.assertFalse(UploadSession.objects.exists())

    def test_the_guest(self):
        guest = Client()
        self.assertCode(self.create("a.txt", 10, client=guest), 401, "login_required")
        self.grant(self.guest, ["upload"])
        made = self.create("a.txt", 10, client=guest)
        self.assertEqual(made.status_code, 201, made.content)
        session = UploadSession.objects.get(pk=made.json()["id"])
        self.assertEqual(session.user, self.guest)
        self.assertEqual(len(session.owner), 64)  # the hash of the token in the visitor's Django session
        token = guest.session[service.OWNER_KEY]
        self.assertNotEqual(session.owner, token)

    def test_a_disabled_storage_user_or_grant(self):
        self.storage.enabled = False
        self.storage.save()
        self.assertCode(self.create("a.txt", 10), 403, "storage_disabled")
        self.storage.enabled = True
        self.storage.save()
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertCode(self.create("a.txt", 10), 403, "no_grant")

    def test_new_folders_need_new_folder(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != "new_folder"])
        self.assertCode(self.create("a.txt", 10, "/", relative_path="new/deeper"), 403, "permission_denied")
        self.assertCode(self.create("a.txt", 10, "/missing"), 403, "permission_denied")
        # Into a folder that exists, no folder is made: upload alone is enough.
        (self.root / "there").mkdir()
        self.assertEqual(self.create("a.txt", 10, "/there").status_code, 201)
        self.grant(self.alice, EVERYTHING)
        made = self.create("a.txt", 10, "/there", relative_path="new/deeper")
        self.assertEqual(made.status_code, 201, made.content)
        self.assertEqual(made.json()["path"], "/there/new/deeper/a.txt")
        session = UploadSession.objects.get(pk=made.json()["id"])
        self.assertEqual(session.created_folders, ["/there/new", "/there/new/deeper"])

    def test_a_parent_that_is_a_file(self):
        (self.root / "file.txt").write_text("x")
        body = self.assertCode(self.create("a.txt", 10, "/file.txt"), 409, "parent_is_file")
        self.assertEqual(body["path"], "/file.txt")
        self.assertCode(self.create("a.txt", 10, "/", relative_path="file.txt/inner"), 409, "parent_is_file")

    def test_a_storage_whose_root_is_gone(self):
        from apps.storages.drivers.base import NotFound

        with mock.patch.object(instances.get(self.storage), "stat", side_effect=NotFound()):
            body = self.assertCode(self.create("a.txt", 10, "/a/b"), 404, "not_found")
        self.assertIn("根目录", body["detail"])
        self.assertFalse(UploadSession.objects.exists())

    def test_an_existing_file(self):
        (self.root / "a.txt").write_text("old")
        body = self.assertCode(self.create("a.txt", 10), 409, "exists")
        self.assertEqual((body["name"], body["path"], body["can_overwrite"], body["size"]), ("a.txt", "/a.txt", True, 3))
        made = self.create("a.txt", 10, overwrite=True)
        self.assertEqual(made.status_code, 201, made.content)
        self.assertTrue(made.json()["overwrite"])

    def test_overwriting_needs_delete(self):
        """D17a: an overwrite destroys the old content."""
        (self.root / "a.txt").write_text("old")
        self.grant(self.alice, NO_DELETE)
        body = self.assertCode(self.create("a.txt", 10), 409, "exists")
        self.assertFalse(body["can_overwrite"])
        body = self.assertCode(self.create("a.txt", 10, overwrite=True), 403, "permission_denied")
        self.assertIn("删除", body["detail"])
        self.assertEqual(body["permission"], "delete")
        self.assertEqual((self.root / "a.txt").read_text(), "old")
        # A new name needs no delete.
        self.assertEqual(self.create("b.txt", 10).status_code, 201)

    def test_a_folder_of_that_name(self):
        (self.root / "same").mkdir()
        self.assertCode(self.create("same", 10), 409, "folder_exists")
        self.assertCode(self.create("same", 10, overwrite=True), 409, "folder_exists")

    def test_names(self):
        for name, code in [("a/b", "invalid_name"), ("..", "invalid_name"), (".", "invalid_name"),
                           ("a\\b", "invalid_name"), ("a\tb", "invalid_name"), ("   ", "invalid_name"),
                           ("名" * 86, "invalid_name"), (".hfile-upload-x", None)]:
            with self.subTest(name=name):
                made = self.create(name, 10)
                if code is None:
                    # The site's checks pass; the local driver refuses its own staging names when writing.
                    self.assertIn(made.status_code, (201, 400), made.content)
                else:
                    self.assertCode(made, 400, code)
        self.assertEqual(self.create("名" * 85, 10).status_code, 201)  # 255 bytes
        self.assertEqual(self.create("", 10).status_code, 400)

    def test_the_drivers_forbidden_characters(self):
        driver = instances.get(self.storage)
        driver.forbidden_chars = '*:?'  # as OneDrive's: read from the instance
        driver.max_name_bytes = 10
        body = self.assertCode(self.create("a*b.txt", 10), 400, "invalid_name")
        self.assertIn("* : ?", body["detail"])
        self.assertCode(self.create("abcdefghijk", 10), 400, "invalid_name")
        # Folders a folder upload creates are new names too.
        self.assertCode(self.create("ok.txt", 10, relative_path="bad:folder"), 400, "invalid_name")

    def test_path_escapes(self):
        (self.root.parent / "outside").mkdir()
        self.grant(self.alice, EVERYTHING, root="/")
        for folder in ["/../outside", "..", "/a/../..", "/a/./b", "a\\..\\b"]:
            with self.subTest(folder=folder):
                self.assertCode(self.create("a.txt", 10, folder), 400, "invalid_path")
        self.assertEqual(self.create("a.txt", 10, "/x\x00y").status_code, 400)  # the serializer refuses NUL
        for relative in ["../outside", "a/../../x", "a\\..\\b"]:
            with self.subTest(relative=relative):
                self.assertCode(self.create("a.txt", 10, "/", relative_path=relative), 400, "invalid_path")
        # A lone surrogate never gets that far: the JSON parser refuses it.
        raw = '{"storage": "disk", "folder": "/", "name": "\\ud800.txt", "size": 1}'
        self.assertEqual(self.client.post(API, raw, content_type="application/json").status_code, 400)
        self.assertFalse(UploadSession.objects.exists())

    def test_a_root_below_cannot_be_left(self):
        (self.root / "home").mkdir()
        (self.root / "other").mkdir()
        self.grant(self.alice, EVERYTHING, root="/home")
        made = self.create("a.txt", 10, "/../other")
        self.assertCode(made, 400, "invalid_path")
        # "/other" is the user's own /other, below /home.
        body = self.create("a.txt", 10, "/other").json()
        self.assertEqual(UploadSession.objects.get(pk=body["id"]).path, "/home/other/a.txt")

    def test_filter_rules(self):
        FilterRule.objects.create(storage=self.storage, expression="/private", mode=FilterMode.INACCESSIBLE)
        (self.root / "private").mkdir()
        self.assertCode(self.create("a.txt", 10, "/private"), 403, "inaccessible")
        self.assertCode(self.create("private", 10, "/"), 403, "inaccessible")
        self.assertCode(self.create("a.txt", 10, "/", relative_path="private/x"), 403, "inaccessible")

    def test_the_size(self):
        self.assertEqual(self.create("a.txt", -1).status_code, 400)
        self.assertEqual(self.create("a.txt", service.MAX_SIZE + 1).status_code, 400)
        self.assertEqual(self.create("a.txt", "12").status_code, 201)

    def test_no_space_in_the_spool(self):
        with mock.patch.object(spool, "free_bytes", return_value=service.SPACE_MARGIN + 99):
            self.assertCode(self.create("a.txt", 100), 507, "no_space")
            self.assertEqual(self.create("a.txt", 99).status_code, 201)
            # What one's own receiving sessions still have to get is counted as taken...
            self.assertCode(self.create("b.txt", 1), 507, "no_space")
            # ...but not what somebody else announced: that would let one visitor keep everybody out.
            self.grant(self.guest, ["upload"])
            self.assertEqual(self.create("c.txt", 99, client=Client()).status_code, 201)
            self.assertEqual(self.create("d.txt", 99, client=Client()).status_code, 201)
        self.assertEqual(UploadSession.objects.count(), 3)

    def test_active_sessions_per_user(self):
        with mock.patch.object(service, "ACTIVE_LIMIT", 3):
            for n in range(3):
                self.assertEqual(self.create(f"{n}.txt", 10).status_code, 201)
            self.assertCode(self.create("x.txt", 10), 429, "too_many")
            # Finished ones don't count; neither do the guests' of other browsers.
            UploadSession.objects.filter(path="/0.txt").update(state=State.DONE)
            self.assertEqual(self.create("x.txt", 10).status_code, 201)

    def test_active_sessions_of_the_guest_count_per_browser(self):
        self.grant(self.guest, ["upload"])
        one, two = Client(), Client()
        with mock.patch.object(service, "ACTIVE_LIMIT", 2):
            for n in range(2):
                self.assertEqual(self.create(f"{n}.txt", 10, client=one).status_code, 201)
            self.assertCode(self.create("x.txt", 10, client=one), 429, "too_many")
            self.assertEqual(self.create("x.txt", 10, client=two).status_code, 201)

    def test_a_visitor_who_drops_his_cookies(self):
        """A new browser session is a new owner, but the address stays: the guest's sessions are counted
        per address too."""
        self.grant(self.guest, ["upload"])
        with mock.patch.object(service, "GUEST_ADDRESS_LIMIT", 3):
            for n in range(3):
                self.assertEqual(self.create(f"{n}.txt", 10, client=Client()).status_code, 201)
            self.assertCode(self.create("x.txt", 10, client=Client()), 429, "too_many")
            self.assertEqual(self.create("y.txt", 10, client=Client(REMOTE_ADDR="198.51.100.9")).status_code, 201)
            # Users aren't counted by address.
            self.assertEqual(self.create("z.txt", 10).status_code, 201)
        self.assertEqual(UploadSession.objects.filter(user=self.guest).values_list("address", flat=True).distinct().count(), 2)

    def test_a_throttle_per_address(self):
        with mock.patch.dict(CreateThrottle.THROTTLE_RATES, {"upload": "3/minute"}):
            for n in range(3):
                self.assertEqual(self.create(f"{n}.txt", 10).status_code, 201)
            answer = self.create("x.txt", 10)
            self.assertEqual(answer.status_code, 429)
            self.assertIn("Retry-After", answer)
            # Per address, not per user: another visitor from there is counted with alice.
            other = Client()
            self.grant(self.guest, ["upload"])
            self.assertEqual(self.create("y.txt", 10, client=other).status_code, 429)
            elsewhere = self.create("z.txt", 10, client=Client(REMOTE_ADDR="198.51.100.7"))
            self.assertEqual(elsewhere.status_code, 201)

    def test_the_guests_csrf(self):
        """DRF skips the CSRF check for visitors who aren't logged in; the uploads API does it itself."""
        self.grant(self.guest, ["upload"])
        guest = Client(enforce_csrf_checks=True)
        refused = self.create("a.txt", 10, client=guest)
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["code"], "csrf_failed")
        token = guest.get("/api/v1/session/").json()["csrf_token"]
        made = guest.post(API, json.dumps({"storage": "disk", "name": "a.txt", "size": 10}),
                          content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(made.status_code, 201, made.content)

    def test_the_kind_of_session(self):
        made = self.create("a.txt", 10).json()
        self.assertEqual(UploadSession.objects.get(pk=made["id"]).kind, Kind.PROXY)


class PasswordTests(UploadTestCase):
    """Folder passwords (D11, D12) on the target folder and on the folders an upload creates."""

    def setUp(self):
        super().setUp()
        (self.root / "secret").mkdir()
        self.rule = FolderPassword.objects.create(storage=self.storage, expression="/secret", password="s3cret")

    def test_the_target_folder(self):
        body = self.assertCode(self.create("a.txt", 10, "/secret"), 403, "password_required")
        # Where to unlock it: the path asked for (the rule that decides there is this one).
        self.assertEqual((body["rule"], body["path"]), (self.rule.pk, "/secret/a.txt"))
        self.assertCode(self.create("a.txt", 10, "/secret", password="nope"), 403, "password_wrong")
        made = self.create("a.txt", 10, "/secret", password="s3cret")
        self.assertEqual(made.status_code, 201, made.content)
        # The password sent once is kept with the session, for its chunks.
        session = UploadSession.objects.get(pk=made.json()["id"])
        self.assertEqual(session.keys, [[self.rule.pk, self.rule.revision]])
        self.assertEqual(self.put(made.json(), b"0123456789", 0).status_code, 201)
        self.assertEqual((self.root / "secret" / "a.txt").read_bytes(), b"0123456789")

    def test_unlocked_in_this_browser(self):
        unlocked = self.client.post("/api/v1/storages/disk/unlock/", {"path": "/secret", "password": "s3cret"},
                                    content_type="application/json")
        self.assertEqual(unlocked.status_code, 200, unlocked.content)
        self.assertEqual(self.upload("a.txt", b"abc", "/secret").status_code, 201)

    def test_created_folders(self):
        FolderPassword.objects.create(storage=self.storage, expression="/open/*/locked", password="pw2", position=1)
        (self.root / "open").mkdir()
        self.assertEqual(self.create("a.txt", 10, "/open", relative_path="x").status_code, 201)
        self.assertCode(self.create("a.txt", 10, "/open", relative_path="x/locked"), 403, "password_required")
        self.assertEqual(self.create("a.txt", 10, "/open", relative_path="x/locked", password="pw2").status_code, 201)

    def test_a_revision_ends_what_was_unlocked(self):
        made = self.create("a.txt", 20, "/secret", password="s3cret").json()
        self.assertEqual(self.put(made, b"0" * 10, 0, 20).status_code, 202)
        self.rule.password = "changed"
        self.rule.revision += 1
        self.rule.save()
        self.assertCode(self.put(made, b"1" * 10, 10, 20), 403, "password_required")

    def test_ignore_password(self):
        self.grant(self.alice, [*EVERYTHING, "ignore_password"])
        self.assertEqual(self.create("a.txt", 10, "/secret").status_code, 201)
