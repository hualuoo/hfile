"""Rule 3, folder passwords (D11, D12): their effect through every endpoint, 「记住密码」, a changed
password, the ignore_password permission, the operations on a whole folder that holds a protected one, and
what a share's visitor and a short link's creator have unlocked."""

from __future__ import annotations

import json
import time
from unittest import mock

from django.core import signing

from apps.accounts.permissions import Permission
from apps.storages import access as policy

from .base import EVERYTHING, FILE, FOLDER, CrossCase


class PasswordCase(CrossCase):
    """/docs/sub needs "pw1"."""

    def setUp(self):
        super().setUp()
        self.rule = self.lock("/docs/sub", "pw1")
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER), ("top.txt", FILE)])

    def put_rules(self, rules):
        """The admin's rules page (PUT the whole list): changing an expression or a password gives the
        rule a new revision there."""
        response = self.as_staff.put(f"/api/v1/manage/storages/{self.storage.pk}/passwords/", json.dumps(rules),
                                     content_type="application/json")
        self.assertOk(response)
        return response.json()

    def assertLocked(self, response, msg=None):
        self.assertRefused(response, 403, "password_required", msg)


class EffectTests(PasswordCase):
    def test_reading(self):
        self.assertTrue(self.items(self.as_alice, "/docs")["sub"]["locked"])
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        self.assertLocked(self.item(self.as_alice, "/docs/sub/c.txt"))
        self.assertLocked(self.item(self.as_alice, "/docs/sub"))
        error = self.list_(self.as_alice, "/docs/sub").json()
        self.assertEqual((error["rule"], error["path"]), (self.rule.pk, "/docs/sub"))
        # A password sent with the request opens it for that request only.
        self.assertOk(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="pw1"))
        self.assertOk(self.item(self.as_alice, "/docs/sub/c.txt", HTTP_X_FOLDER_PASSWORD="pw1"))
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        self.assertRefused(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="nope"), 403, "password_wrong")

    def test_a_file_the_rule_covers_but_not_its_folder(self):
        """/docs/** covers the files of /docs, not /docs: the listing works, the files are locked."""
        self.rule.delete()
        self.lock("/docs/**", "pw2")
        items = self.items(self.as_alice, "/docs")
        self.assertTrue(items["a.txt"]["locked"])
        self.assertIsNone(items["a.txt"]["url"])
        self.assertLocked(self.item(self.as_alice, "/docs/a.txt"))
        self.assertLocked(self.path_links(self.as_alice, ["/docs/a.txt"]))
        items = self.vitems(self.anon, self.share, "/docs")
        self.assertTrue(items["a.txt"]["locked"])
        self.assertEqual((items["a.txt"]["url"], items["a.txt"]["download_url"]), (None, None))

    def test_links(self):
        self.assertLocked(self.path_links(self.as_alice, ["/docs/sub/c.txt"]))
        self.assertLocked(self.short_links(self.as_alice, ["/docs/sub/c.txt"]))
        self.assertOk(self.path_links(self.as_alice, ["/docs/sub/c.txt"], password="pw1"))
        # A path link is opened as its visitor: his browser must have entered the password.
        self.assertRefused(self.as_alice.get("/directlink/disk/docs/sub/c.txt"), 403)
        self.assertOk(self.unlock(self.as_alice, "/docs/sub"))
        self.assertEqual(self.follow(self.as_alice, "/directlink/disk/docs/sub/c.txt")[2], b"charlie")
        self.assertRefused(self.as_staff.get("/directlink/disk/docs/sub/c.txt"), 403)

    def test_uploads(self):
        self.assertLocked(self.upload_session(self.as_alice, "/docs/sub", "new.txt", 3))
        self.assertLocked(self.upload_session(self.as_alice, "/docs", "new.txt", 3, relative_path="sub/deeper"))
        self.assertLocked(self.make_folders(self.as_alice, "/docs", ["sub/new"]))
        self.assertLocked(self.save_text(self.as_alice, "/docs/sub/c.txt", "x"))
        self.assertLocked(self.save_text(self.as_alice, "/docs/sub/new.txt", "x"))
        self.assertRefused(self.upload_session(self.as_alice, "/docs/sub", "new.txt", 3, password="nope"), 403,
                           "password_wrong")
        self.assertOk(self.upload(self.as_alice, "/docs/sub", "new.txt", b"new", password="pw1"), 201)
        self.assertEqual((self.root / "docs/sub/new.txt").read_bytes(), b"new")
        # The password went with that request only.
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))

    def test_file_operations(self):
        self.files({"elsewhere/x.txt": b"x"})
        self.assertLocked(self.mkdir(self.as_alice, "/docs/sub", "new"))
        self.assertLocked(self.rename(self.as_alice, "/docs/sub", "c.txt", "d.txt"))
        self.assertLocked(self.move(self.as_alice, "/docs/sub", ["c.txt"], "/"))
        self.assertLocked(self.copy(self.as_alice, "/docs/sub", ["c.txt"], "/"))
        self.assertLocked(self.delete(self.as_alice, "/docs/sub", ["c.txt"]))
        self.assertLocked(self.move(self.as_alice, "/elsewhere", ["x.txt"], "/docs/sub"))
        self.assertLocked(self.copy(self.as_alice, "/elsewhere", ["x.txt"], "/docs/sub"))
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/elsewhere", ["x.txt"], "/docs/sub",
                                                target_password="pw1")))
        self.assertTrue((self.root / "docs/sub/c.txt").exists())

    def test_a_whole_folder_that_holds_it(self):
        """Deleting, moving, copying or renaming /docs takes /docs/sub with it: its password first (D12),
        for the whole request, nothing done before."""
        for response in (self.delete(self.as_alice, "/", ["docs", "top.txt"]),
                         self.move(self.as_alice, "/", ["docs"], "/home"),
                         self.copy(self.as_alice, "/", ["docs"], "/home"),
                         self.rename(self.as_alice, "/", "docs", "papers")):
            with self.subTest(response=response):
                self.assertLocked(response)
        self.assertTrue((self.root / "docs/sub/c.txt").exists())
        self.assertTrue((self.root / "top.txt").exists())
        self.assertFalse((self.root / "home/docs").exists())
        # Something put beside it is no business of its password.
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/home", ["bob"], "/docs")))
        # With the password (sent, or unlocked), it goes.
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/", ["docs"], "/home", password="pw1")))
        self.assertOk(self.unlock(self.as_alice, "/docs/sub"))
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/", ["docs"])))

    def test_a_rule_without_a_place(self):
        """**/secret could be inside any folder: every whole-folder operation asks for it (an accepted limit)."""
        self.rule.delete()
        self.files({"home/alice/secret/s.txt": b"s"})
        rule = self.lock("**/secret", "pw3")
        self.assertLocked(self.delete(self.as_alice, "/", ["docs"]))
        self.assertLocked(self.move(self.as_alice, "/home", ["bob"], "/docs"))
        self.assertLocked(self.list_(self.as_alice, "/home/alice/secret"))
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/docs", ["a.txt"])))
        self.assertOk(self.unlock(self.as_alice, rule=rule.pk, password="pw3"))
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/", ["docs"])))

    def test_the_hint_doesnt_name_a_hidden_folder(self):
        """An operation on /docs that needs the password of a folder hidden inside it doesn't say where."""
        from apps.storages.models import FilterMode

        self.hide("/docs/sub", FilterMode.HIDDEN)
        error = self.delete(self.as_alice, "/", ["docs"]).json()
        self.assertEqual((error["code"], error["path"]), ("password_required", None))
        self.assertNotIn("sub", error["detail"])

    def test_share_visitors(self):
        self.assertLocked(self.vlist(self.anon, self.share, "/docs/sub"))
        self.assertLocked(self.vitem(self.anon, self.share, "/docs/sub/c.txt"))
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/sub/c.txt"), 403)
        self.assertRefused(self.vpreview(self.anon, self.share, "/docs/sub/c.txt"), 403)
        self.assertTrue(self.vitems(self.anon, self.share, "/docs")["sub"]["locked"])
        self.assertRefused(self.vunlock(self.anon, self.share, "/docs/sub", "nope"), 403, "password_wrong")
        self.assertOk(self.vunlock(self.anon, self.share, "/docs/sub", "pw1"))
        self.assertEqual(self.vnames(self.anon, self.share, "/docs/sub"), ["c.txt"])
        self.assertEqual(self.served(self.anon, self.vdownload(self.anon, self.share, "/docs/sub/c.txt"))[1], b"charlie")
        # It holds for that share only, in that browser only.
        other = self.share_of(self.alice, "/docs", [("sub", FOLDER)])
        self.assertLocked(self.vlist(self.anon, other, "/sub"))
        self.assertLocked(self.vlist(self.client_for(), self.share, "/docs/sub"))
        # Nor does it unlock the visitor's own browsing (the guest's grant here).
        self.grant(self.guest, EVERYTHING)
        self.assertLocked(self.list_(self.anon, "/docs/sub"))

    def test_a_signed_link_made_before_stands(self):
        """An accepted limit: a /pd/ link stands for the password while it lasts (only the filter rules are
        checked again when it is opened); a new one isn't handed out."""
        self.rule.delete()
        url = self.file_url(self.as_alice, "/docs/sub/c.txt")
        self.lock("/docs/sub", "pw1")
        self.assertEqual(self.fetch(self.anon, url)[1], b"charlie")
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        self.assertLocked(self.item(self.as_alice, "/docs/sub/c.txt"))


class RememberTests(PasswordCase):
    """记住密码: the session only, or a signed cookie for 30 days bound to whoever unlocked."""

    def cookie(self, client):
        return client.cookies.get(policy.COOKIE_NAME)

    def fresh(self, user, cookie=None):
        """Another session of `user` (None: a visitor), with the remembered cookie or not."""
        client = self.client_for(user)
        if cookie is not None:
            client.cookies[policy.COOKIE_NAME] = cookie.value
        return client

    def test_without_remembering(self):
        self.assertOk(self.unlock(self.as_alice, "/docs/sub"))
        self.assertIsNone(self.cookie(self.as_alice))
        self.assertOk(self.list_(self.as_alice, "/docs/sub"))
        self.assertLocked(self.list_(self.fresh(self.alice), "/docs/sub"))

    def test_remembered(self):
        self.assertOk(self.unlock(self.as_alice, "/docs/sub", remember=True))
        cookie = self.cookie(self.as_alice)
        self.assertTrue(cookie["httponly"])
        self.assertOk(self.list_(self.fresh(self.alice, cookie), "/docs/sub"))
        # Bound to alice: not for another user, nor for the guest, with the same cookie.
        self.assertLocked(self.list_(self.fresh(self.staff, cookie), "/docs/sub"))
        self.grant(self.guest, EVERYTHING)
        self.assertLocked(self.list_(self.fresh(None, cookie), "/docs/sub"))
        # Logging out ends the session's unlock; the cookie still holds for alice when she's back.
        self.assertEqual(self.as_alice.delete("/api/v1/session/").status_code, 204)
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        self.as_alice.force_login(self.alice)
        self.assertOk(self.list_(self.as_alice, "/docs/sub"))

    def test_forgotten(self):
        self.assertOk(self.unlock(self.as_alice, "/docs/sub", remember=True))
        self.assertEqual(self.forget(self.as_alice).status_code, 204)
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        self.assertLocked(self.list_(self.fresh(self.alice, self.cookie(self.as_alice)), "/docs/sub"))

    def remembered(self, expiry, *, signed_at=None):
        """A cookie as FolderKeys writes it: alice unlocked the rule, until `expiry`, signed then."""
        entries = [[self.storage.pk, self.rule.pk, self.rule.revision, self.alice.pk, int(expiry)]]
        if signed_at is None:
            return signing.dumps(entries, salt=policy.COOKIE_SALT, compress=True)
        with mock.patch("time.time", return_value=signed_at):
            return signing.dumps(entries, salt=policy.COOKIE_SALT, compress=True)

    def test_thirty_days(self):
        now = time.time()
        client = self.fresh(self.alice)
        client.cookies[policy.COOKIE_NAME] = self.remembered(now + 3600)
        self.assertOk(self.list_(client, "/docs/sub"))
        client.cookies[policy.COOKIE_NAME] = self.remembered(now - 1)
        self.assertLocked(self.list_(client, "/docs/sub"))
        client.cookies[policy.COOKIE_NAME] = self.remembered(now + 3600, signed_at=now - policy.COOKIE_AGE - 60)
        self.assertLocked(self.list_(client, "/docs/sub"))

    def test_a_forged_cookie(self):
        forged = self.fresh(self.alice)
        forged.cookies[policy.COOKIE_NAME] = json.dumps([[self.storage.pk, self.rule.pk, 1, self.alice.pk, 2**40]])
        self.assertLocked(self.list_(forged, "/docs/sub"))


class ChangedPasswordTests(PasswordCase):
    """A new password (or expression) is a new revision: everything unlocked with the old one ends."""

    def change(self, password="pw-new", expression="/docs/sub"):
        self.put_rules([{"id": self.rule.pk, "expression": expression, "password": password}])
        self.rule.refresh_from_db()

    def test_unlocks_end(self):
        self.assertOk(self.unlock(self.as_alice, "/docs/sub", remember=True))
        cookie = self.as_alice.cookies[policy.COOKIE_NAME]
        self.change()
        self.assertEqual(self.rule.revision, 2)
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))
        other = self.client_for(self.alice)
        other.cookies[policy.COOKIE_NAME] = cookie.value
        self.assertLocked(self.list_(other, "/docs/sub"))
        self.assertRefused(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="pw1"), 403, "password_wrong")
        self.assertOk(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="pw-new"))

    def test_a_new_expression_too(self):
        self.assertOk(self.unlock(self.as_alice, "/docs/sub"))
        self.change(password="pw1", expression="/docs/sub/")  # the same once normalized: nothing changes
        self.assertEqual(self.rule.revision, 1)
        self.assertOk(self.list_(self.as_alice, "/docs/sub"))
        self.change(password="pw1", expression="/docs/s*")
        self.assertEqual(self.rule.revision, 2)
        self.assertLocked(self.list_(self.as_alice, "/docs/sub"))

    def test_short_links(self):
        link = self.short_link(self.client_for(self.alice), "/docs/a.txt")  # needs no password
        self.assertOk(self.unlock(self.as_alice, "/docs/sub"))
        locked_link = self.short_link(self.as_alice, "/docs/sub/c.txt")
        visitor = self.client_for()
        self.assertEqual(self.follow(visitor, locked_link)[2], b"charlie")
        self.change()
        response = visitor.get(locked_link)
        self.assertRefused(response, 403)
        self.assertIn("密码".encode(), response.content)
        self.assertEqual(self.follow(visitor, link)[2], b"alpha")
        # The visitor entering the new password doesn't help: the creator's keys count, not his.
        self.grant(self.guest, EVERYTHING)
        self.assertOk(self.unlock(visitor, "/docs/sub", "pw-new"))
        self.assertRefused(visitor.get(locked_link), 403)
        # Made again by the creator, with the new password.
        self.assertOk(self.unlock(self.as_alice, "/docs/sub", "pw-new"))
        again = self.short_link(self.as_alice, "/docs/sub/c.txt")
        self.assertEqual(self.follow(self.client_for(), again)[2], b"charlie")

    def test_uploads_halfway(self):
        made = self.assertOk(self.upload_session(self.as_alice, "/docs/sub", "big.bin", 2500, password="pw1"), 201)
        session = made.json()
        self.assertOk(self.put_chunk(self.as_alice, session, b"x" * 1000, 0, 2500), 202)
        self.change()
        self.assertLocked(self.put_chunk(self.as_alice, session, b"x" * 1000, 1000, 2500))
        self.assertOk(self.unlock(self.as_alice, "/docs/sub", "pw-new"))
        self.assertOk(self.put_chunk(self.as_alice, session, b"x" * 1000, 1000, 2500), 202)

    def test_share_visitors(self):
        self.assertOk(self.vunlock(self.anon, self.share, "/docs/sub", "pw1"))
        self.assertOk(self.vlist(self.anon, self.share, "/docs/sub"))
        self.change()
        self.assertLocked(self.vlist(self.anon, self.share, "/docs/sub"))
        self.assertOk(self.vunlock(self.anon, self.share, "/docs/sub", "pw-new"))
        self.assertOk(self.vlist(self.anon, self.share, "/docs/sub"))


class IgnorePasswordTests(PasswordCase):
    def setUp(self):
        super().setUp()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_PASSWORD])

    def test_his_own_view(self):
        self.assertNotIn("locked", self.items(self.as_alice, "/docs")["sub"])
        self.assertEqual(self.names(self.as_alice, "/docs/sub"), ["c.txt"])
        self.assertOk(self.item(self.as_alice, "/docs/sub/c.txt"))
        self.assertOk(self.upload(self.as_alice, "/docs/sub", "new.txt", b"new"), 201)
        self.assertOk(self.path_links(self.as_alice, ["/docs/sub/c.txt"]))
        self.assertEqual(self.follow(self.as_alice, "/directlink/disk/docs/sub/c.txt")[2], b"charlie")
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/", ["docs"], "/home")))
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/", ["docs"])))

    def test_not_for_others(self):
        """The staff admin has no ignore_password: the folder stays locked for him; visitors of alice's
        share must enter it whatever alice may."""
        self.assertLocked(self.list_(self.as_staff, "/docs/sub"))
        self.assertLocked(self.vlist(self.anon, self.share, "/docs/sub"))
        self.assertRefused(self.vdownload(self.anon, self.share, "/docs/sub/c.txt"), 403)
        self.assertRefused(self.as_staff.get("/directlink/disk/docs/sub/c.txt"), 403)

    def test_short_links_while_he_has_it(self):
        """README: 生成短链时有它就不用记密码，打开时生成者还有它就照样能用."""
        link = self.short_link(self.as_alice, "/docs/sub/c.txt")
        visitor = self.client_for()
        self.assertEqual(self.follow(visitor, link)[2], b"charlie")
        self.grant(self.alice, EVERYTHING)
        self.assertRefused(visitor.get(link), 403)


class UnlockReachTests(PasswordCase):
    """What can be tried where: unlocking by a rule's id only for rules that may decide inside the actor's
    root (a share's folder), and wrong passwords counted per address across endpoints."""

    def test_rule_ids_outside_the_root(self):
        bobs = self.lock("/home/bob/vault", "pw-bob")
        self.assertRefused(self.unlock(self.as_bob, rule=self.rule.pk, password="pw1"), 404, "rule_not_found")
        self.assertOk(self.unlock(self.as_bob, rule=bobs.pk, password="pw-bob"))
        elsewhere = self.share_of(self.alice, "/home", [("bob", FOLDER)])
        self.assertRefused(self.vunlock(self.anon, elsewhere, rule=self.rule.pk, password="pw1"), 404, "rule_not_found")
        self.assertOk(self.vunlock(self.anon, self.share, rule=self.rule.pk, password="pw1"))

    def test_rule_ids_outside_the_shared_entries(self):
        """A share of /docs/a.txt alone: its visitors can't reach /docs/sub (resolve() refuses the path),
        so they shouldn't be able to try its password by the rule's id either (apps.storages.access unlock():
        "not a way to try the passwords of folders the actor can't reach")."""
        share = self.share_of(self.alice, "/docs", [("a.txt", FILE)])
        self.assertRefused(self.vlist(self.anon, share, "/sub"), 404)
        self.assertRefused(self.vunlock(self.anon, share, "/sub", "pw1"), 404)
        self.assertRefused(self.vunlock(self.anon, share, rule=self.rule.pk, password="pw1"), 404, "rule_not_found")

    def test_wrong_passwords_add_up_across_endpoints(self):
        for _ in range(4):
            self.assertRefused(self.unlock(self.as_alice, "/docs/sub", "nope"), 403, "password_wrong")
        for _ in range(3):
            self.assertRefused(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="nope"), 403)
        for _ in range(3):
            self.assertRefused(self.upload_session(self.as_alice, "/docs/sub", "x.txt", 1, password="nope"), 403)
        # Ten in a minute: the right one is refused too, wherever it is sent.
        self.assertRefused(self.unlock(self.as_alice, "/docs/sub", "pw1"), 429, "password_throttled")
        self.assertRefused(self.list_(self.as_alice, "/docs/sub", HTTP_X_FOLDER_PASSWORD="pw1"), 429)
        self.assertRefused(self.vunlock(self.anon, self.share, "/docs/sub", "pw1"), 429)

    def test_unlocking_an_inaccessible_folder(self):
        """A folder that a filter rule makes inaccessible has nothing to try a password on: the share's
        unlock refuses it (apps/shares/access.py unlock_folder), the browser's should too."""
        from apps.storages.models import FilterMode

        self.hide("/docs/sub", FilterMode.INACCESSIBLE)
        self.assertRefused(self.vunlock(self.anon, self.share, "/docs/sub", "nope"), 403, "inaccessible")
        response = self.unlock(self.as_alice, "/docs/sub", "nope")
        self.assertRefused(response, 403, "inaccessible", "a password oracle on a folder alice can't open")
