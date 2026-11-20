"""POST /api/v1/office/config/: the checks, who may edit, the signed config, the sessions and shares."""

from __future__ import annotations

import time
import urllib.parse
from datetime import timedelta
from unittest import mock

import jwt
from django.conf import settings
from django.utils import timezone

from apps.accounts.models import User
from apps.files import signing
from apps.storages import access as policy
from apps.storages.models import FilterMode

from .. import editor, sessions, tokens
from .. import shares as office_shares
from ..models import OfficeSession
from .helpers import CONFIG, EVERYTHING, NO_DELETE, SECRET, OfficeTestCase


class RefusalTests(OfficeTestCase):
    def test_without_an_onlyoffice_address(self):
        self.configure(url="")
        response = self.config()
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.code(response), "onlyoffice_off")
        self.assertFalse(OfficeSession.objects.exists())

    def test_not_an_office_file(self):
        response = self.config("/docs/a.txt")
        self.assertEqual((response.status_code, self.code(response)), (400, "not_office"))
        self.assertEqual(self.code(self.config("/docs")), "not_office")

    def test_office_preview_switched_off(self):
        from apps.siteconf.models import SiteSettings

        site = SiteSettings.load()
        site.office_preview = False
        site.save()
        self.assertEqual(self.code(self.config()), "not_office")

    def test_without_preview_or_download(self):
        self.grant(self.alice, [code for code in EVERYTHING if code not in ("preview", "download")])
        response = self.config()
        self.assertEqual((response.status_code, self.code(response)), (403, "permission_denied"))
        self.assertFalse(OfficeSession.objects.exists())

    def test_preview_needs_download(self):
        # 预览 needs 下载 (apps/accounts/permissions.py): saved without it, it opens nothing; with it, the editor
        # offers downloading and printing (D37).
        self.grant(self.alice, ["preview"])
        response = self.config()
        self.assertEqual((response.status_code, self.code(response)), (403, "permission_denied"))
        self.assertFalse(OfficeSession.objects.exists())
        self.grant(self.alice, ["preview", "download"])
        config = self.ok_config()
        self.assertEqual(config["document"]["permissions"], {"edit": False, "download": True, "print": True})

    def test_download_alone_is_enough(self):
        self.grant(self.alice, ["download"])
        config = self.ok_config()
        self.assertEqual(config["document"]["permissions"]["download"], True)

    def test_filter_rules(self):
        self.hide("/docs/a.docx", mode=FilterMode.INACCESSIBLE)
        self.assertEqual(self.code(self.config()), "inaccessible")
        self.hide("/docs/b.xlsx", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertEqual(self.code(self.config("/docs/b.xlsx")), "download_disabled")
        self.hide("/docs/sub", mode=FilterMode.INACCESSIBLE)
        self.assertEqual(self.code(self.config("/docs/sub/c.pptx")), "inaccessible")
        self.assertFalse(OfficeSession.objects.exists())

    def test_a_hidden_file_opens_by_its_path(self):
        self.hide("/docs/a.docx")  # hidden from listings, not refused
        self.ok_config()

    def test_folder_password(self):
        rule = self.lock("/docs", password="pw1")
        response = self.config()
        self.assertEqual((response.status_code, self.code(response)), (403, "password_required"))
        self.assertEqual(self.code(self.config(password="wrong")), "password_wrong")
        config = self.ok_config(password="pw1")
        self.assertTrue(config["document"]["permissions"]["edit"])
        # What the editor had unlocked is kept with the session, for the save.
        self.assertEqual(self.session_of().keys, [[rule.pk, rule.revision]])

    def test_unlocked_in_the_browser(self):
        self.lock("/docs", password="pw1")
        self.login()
        self.assertEqual(self.unlock("/docs", "pw1").status_code, 200)
        self.ok_config()

    def test_guest_without_a_grant(self):
        response = self.config(login=False)
        self.assertEqual((response.status_code, self.code(response)), (401, "login_required"))

    def test_guest_with_preview(self):
        self.grant(self.guest, ["preview", "download"])
        config = self.ok_config(login=False)
        self.assertEqual(config["editorConfig"]["user"], {"name": "匿名用户"})
        self.assertEqual(config["editorConfig"]["mode"], "view")

    def test_the_guest_may_edit_with_the_permissions(self):
        self.grant(self.guest, ["preview", "download", "upload", "delete"])
        config = self.ok_config(login=False)
        self.assertTrue(config["document"]["permissions"]["edit"])
        self.assertEqual(self.session_of().user, self.guest)

    def test_csrf_also_for_visitors(self):
        self.grant(self.guest, ["preview"])
        response = self.client.post(CONFIG, {"storage": "disk", "path": "/docs/a.docx"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(OfficeSession.objects.exists())

    def test_storage_disabled_or_unknown(self):
        self.storage.enabled = False
        self.storage.save()
        self.assertEqual(self.code(self.config()), "storage_disabled")
        self.assertEqual(self.code(self.config(storage="nope")), "storage_not_found")

    def test_outside_the_root(self):
        self.grant(self.alice, EVERYTHING, root="/docs/sub")
        config = self.ok_config("/c.pptx")  # the root's own paths
        self.assertEqual(config["document"]["title"], "c.pptx")
        self.assertEqual(self.session_of("/docs/sub/c.pptx").path, "/docs/sub/c.pptx")
        self.assertEqual(self.code(self.config("/../a.docx")), "invalid_path")

    def test_missing_file_and_folder(self):
        response = self.config("/docs/missing.docx")
        self.assertEqual((response.status_code, self.code(response)), (404, "not_found"))
        (self.root / "docs" / "folder.docx").mkdir()
        self.assertEqual(self.code(self.config("/docs/folder.docx")), "not_a_file")
        self.assertFalse(OfficeSession.objects.exists())

    def test_the_body(self):
        self.login()
        self.csrf()
        both = self.client.post(CONFIG, {"storage": "disk", "share": "abc", "path": "/a.docx"}, format="json")
        self.assertEqual(both.status_code, 400)
        neither = self.client.post(CONFIG, {"path": "/a.docx"}, format="json")
        self.assertEqual(neither.status_code, 400)
        self.assertEqual(self.client.post(CONFIG, {"storage": "disk"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(CONFIG, ["x"], format="json").status_code, 400)
        self.assertEqual(self.client.get(CONFIG).status_code, 405)


class EditingTests(OfficeTestCase):
    def test_with_upload_delete_and_a_secret(self):
        config = self.ok_config()
        self.assertEqual(config["document"]["permissions"], {"edit": True, "download": True, "print": True})
        self.assertEqual(config["editorConfig"]["mode"], "edit")
        self.assertEqual(config["editorConfig"]["customization"], {"forcesave": True})
        session = self.session_of()
        self.assertTrue(session.can_edit)
        self.assertEqual(session.user, self.alice)

    def test_without_delete(self):  # D17a: saving replaces the file
        self.grant(self.alice, NO_DELETE)
        config = self.ok_config()
        self.assertFalse(config["document"]["permissions"]["edit"])
        self.assertEqual(config["editorConfig"]["mode"], "view")
        self.assertNotIn("customization", config["editorConfig"])
        self.assertFalse(OfficeSession.objects.filter(can_edit=True).exists())  # the viewing session only
        self.assertEqual(self.session_of(editing=False).key, config["document"]["key"])

    def test_without_upload(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
        self.assertFalse(self.ok_config()["document"]["permissions"]["edit"])

    def test_without_a_secret(self):
        self.configure(secret="")
        config = self.ok_config()
        self.assertFalse(config["document"]["permissions"]["edit"])
        self.assertNotIn("token", config)

    def test_an_unreadable_secret(self):  # secret_key changed since it was saved
        from apps.siteconf.models import SiteSettings

        site = SiteSettings.load()
        site.onlyoffice_secret = "enc:garbage"
        site.save()
        with self.assertLogs("hfile.office", "WARNING"):
            config = self.ok_config()
        self.assertFalse(config["document"]["permissions"]["edit"])
        self.assertNotIn("token", config)

    def test_old_binary_formats_open_to_view(self):
        config = self.ok_config("/docs/old.doc")
        self.assertFalse(config["document"]["permissions"]["edit"])
        self.assertEqual(config["documentType"], "word")

    def test_ignore_hidden_carries_over_to_the_documents_link(self):
        self.hide("/docs/a.docx", mode=FilterMode.DISABLE_DOWNLOAD)
        self.grant(self.alice, [*EVERYTHING, "ignore_hidden"])
        config = self.ok_config()  # 忽略隐藏: the rule doesn't apply to alice
        link = signing.verify_url(config["document"]["url"])
        self.assertTrue(link.ignore_hidden)  # and the Document Server's download skips it the same way

    def test_password_folders_for_editing(self):
        self.lock("/docs", password="pw1")
        config = self.ok_config(password="pw1")
        self.assertTrue(config["document"]["permissions"]["edit"])


class ConfigTests(OfficeTestCase):
    def test_the_answer(self):
        config = self.ok_config()
        self.assertEqual(config["width"], "100%")
        self.assertEqual(config["height"], "100%")
        self.assertEqual(config["documentType"], "word")
        document = config["document"]
        self.assertEqual(document["fileType"], "docx")
        self.assertEqual(document["title"], "a.docx")
        self.assertEqual(document["key"], self.session_of().key)
        self.assertTrue(sessions.is_key(document["key"]))
        editor_config = config["editorConfig"]
        self.assertEqual(editor_config["callbackUrl"], "http://testserver/onlyoffice/callback/")
        self.assertEqual(editor_config["lang"], "zh-CN")
        self.assertEqual(editor_config["user"], {"id": f"hfile-{self.alice.pk}", "name": "alice"})

    def test_in_the_pages_language(self):
        """editorConfig.lang: the language of the page that asks (the menu's choice, else the browser's)."""
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "en"
        self.assertEqual(self.ok_config()["editorConfig"]["lang"], "en")
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"
        self.assertEqual(self.ok_config()["editorConfig"]["lang"], "zh-CN")

    def test_types(self):
        self.assertEqual(self.ok_config("/docs/b.xlsx")["documentType"], "cell")
        self.assertEqual(self.ok_config("/docs/sub/c.pptx")["documentType"], "slide")

    def test_the_token_signs_the_whole_config(self):
        config = self.ok_config()
        token = config.pop("token")
        self.assertEqual(jwt.get_unverified_header(token)["alg"], "HS256")
        claims = jwt.decode(token, SECRET, algorithms=["HS256"])
        issued, expires = claims.pop("iat"), claims.pop("exp")
        self.assertEqual(claims, config)  # the whole config; the config itself has no iat and exp
        self.assertNotIn("exp", config)
        with self.assertRaises(jwt.InvalidSignatureError):
            jwt.decode(token, SECRET + "x", algorithms=["HS256"])
        # It opens the document for ten minutes: then the Document Server refuses it.
        self.assertLessEqual(abs(issued - time.time()), 5)
        self.assertEqual(expires - issued, tokens.CONFIG_LIFETIME)
        self.assertEqual(tokens.CONFIG_LIFETIME, 600)
        with self.assertRaises(jwt.ExpiredSignatureError):  # as checked 11 minutes from now
            jwt.decode(token, SECRET, algorithms=["HS256"], leeway=-(expires - time.time() + 60),
                       options={"verify_iat": False})

    def test_the_documents_url(self):
        config = self.ok_config()
        url = config["document"]["url"]
        parts = urllib.parse.urlsplit(url)
        self.assertEqual((parts.scheme, parts.netloc), ("http", "testserver"))
        link = signing.verify_url(url)
        self.assertEqual((link.storage, link.path), (self.storage, "/docs/a.docx"))
        self.assertGreaterEqual(link.expires, time.time() + 590)
        self.assertLessEqual(link.expires, time.time() + 600 + 300)
        # The Document Server fetches it without the site's cookies.
        response = self.ds_client.get(parts.path + "?" + parts.query)
        self.assertEqual(response.status_code, 200)
        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.assertEqual(body, b"PK\x03\x04 a docx of the tests")

    def test_site_url_makes_the_absolute_addresses(self):
        with self.settings(SITE_URL="https://files.example.com"):
            config = self.ok_config()
        self.assertTrue(config["document"]["url"].startswith("https://files.example.com/pd/disk/docs/a.docx?"))
        self.assertEqual(config["editorConfig"]["callbackUrl"], "https://files.example.com/onlyoffice/callback/")

    def test_phone_layout_and_dark_theme(self):
        config = self.ok_config(mobile=True, dark=True)
        self.assertEqual(config["type"], "mobile")
        self.assertEqual(config["editorConfig"]["customization"], {"forcesave": True, "uiTheme": "theme-dark"})
        claims = jwt.decode(config.pop("token"), SECRET, algorithms=["HS256"])
        self.assertEqual({k: v for k, v in claims.items() if k not in ("iat", "exp")}, config)
        plain = self.ok_config()
        self.assertNotIn("type", plain)
        self.assertEqual(plain["editorConfig"]["customization"], {"forcesave": True})
        self.grant(self.alice, ["preview", "download"])
        self.assertEqual(self.ok_config(dark=True)["editorConfig"]["customization"], {"uiTheme": "theme-dark"})
        self.assertEqual(self.config(mobile="yes please").status_code, 400)

    def test_no_store(self):
        response = self.config()
        self.assertIn("no-store", response["Cache-Control"])


class SessionTests(OfficeTestCase):
    def setUp(self):
        super().setUp()
        self.bob = User.objects.create_user("bob", password="pw-bob-2026")
        self.grant(self.bob, ["preview", "download"])

    def as_user(self, user, path="/docs/a.docx"):
        self.client.force_login(user)
        self.csrf()
        return self.ok_config(path)

    def test_one_key_per_file_for_its_editors(self):
        carol = User.objects.create_user("carol", password="pw-carol-2026")
        self.grant(carol, EVERYTHING)
        first = self.as_user(self.alice)["document"]["key"]
        self.assertEqual(self.as_user(carol)["document"]["key"], first)  # co-editing
        self.assertNotEqual(self.as_user(self.alice, "/docs/b.xlsx")["document"]["key"], first)
        self.assertEqual(OfficeSession.objects.count(), 2)

    def test_viewers_have_a_key_of_their_own(self):
        # They see the file as it was saved last, not the editors nor their changes not saved yet.
        editing = self.as_user(self.alice)["document"]["key"]
        viewing = self.as_user(self.bob)
        self.assertNotEqual(viewing["document"]["key"], editing)
        self.assertEqual(viewing["editorConfig"]["mode"], "view")
        dave = User.objects.create_user("dave", password="pw-dave-2026")
        self.grant(dave, ["download"])
        self.assertEqual(self.as_user(dave)["document"]["key"], viewing["document"]["key"])
        self.assertEqual((self.session_of().user, self.session_of(editing=False).can_edit), (self.alice, False))

    def test_a_viewer_doesnt_take_over(self):
        self.as_user(self.alice)
        self.as_user(self.bob)
        session = self.session_of()
        self.assertEqual((session.user, session.can_edit), (self.alice, True))
        self.assertEqual(self.session_of(editing=False).keys, [])

    def test_the_last_editor_saves(self):
        carol = User.objects.create_user("carol", password="pw-carol-2026")
        self.grant(carol, EVERYTHING)
        self.as_user(self.alice)
        self.as_user(carol)
        self.assertEqual(self.session_of().user, carol)

    def test_paths_differing_in_case_are_different_documents(self):
        self.files({"docs/A.docx": b"PK upper"})
        lower = self.as_user(self.alice)["document"]["key"]
        upper = self.as_user(self.alice, "/docs/A.docx")["document"]["key"]
        self.assertNotEqual(lower, upper)

    def test_an_expired_session_gets_a_new_key(self):
        key = self.as_user(self.alice)["document"]["key"]
        OfficeSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertNotEqual(self.as_user(self.alice)["document"]["key"], key)
        self.assertEqual(OfficeSession.objects.count(), 1)

    def test_opening_renews_the_session(self):
        self.as_user(self.alice)
        OfficeSession.objects.update(expires_at=timezone.now() + timedelta(hours=1))
        self.as_user(self.alice)
        self.assertGreater(self.session_of().expires_at, timezone.now() + sessions.LIFETIME - timedelta(minutes=1))

    def test_a_file_changed_outside_the_site_gets_a_new_key(self):
        key = self.as_user(self.alice)["document"]["key"]
        (self.root / "docs" / "a.docx").write_bytes(b"PK changed elsewhere, longer")
        self.assertNotEqual(self.as_user(self.alice)["document"]["key"], key)

    def test_but_not_while_somebody_edits_it(self):
        key = self.as_user(self.alice)["document"]["key"]
        viewing = self.as_user(self.bob)["document"]["key"]
        sessions.note_in_use(key, True)
        (self.root / "docs" / "a.docx").write_bytes(b"PK changed elsewhere, longer")
        self.assertEqual(self.as_user(self.alice)["document"]["key"], key)
        # The viewers' key changes all the same: nobody edits through it.
        self.assertNotEqual(self.as_user(self.bob)["document"]["key"], viewing)

    def test_nor_while_it_is_being_saved(self):
        key = self.as_user(self.alice)["document"]["key"]
        OfficeSession.objects.update(saving_until=timezone.now() + timedelta(minutes=1))
        (self.root / "docs" / "a.docx").write_bytes(b"PK changed elsewhere, longer")
        self.assertEqual(self.as_user(self.alice)["document"]["key"], key)

    def test_no_new_key_while_the_final_save_runs(self):
        key = self.as_user(self.alice)["document"]["key"]
        old = self.session_of()
        sessions.detach(old)  # status 2 arrived: its save is under way
        response = self.client.post(CONFIG, {"storage": "disk", "path": "/docs/a.docx"}, format="json")
        self.assertEqual((response.status_code, self.code(response)), (409, "closing"))
        self.assertNotEqual(self.as_user(self.bob)["document"]["key"], key)  # viewers see the last saved version
        self.assertEqual(self.as_user(self.bob, "/docs/b.xlsx")["document"]["key"] != key, True)  # other files open
        sessions.drop(old)  # saved
        self.assertNotEqual(self.as_user(self.alice)["document"]["key"], key)

    def test_a_final_save_whose_process_died_closes_the_file_for_a_while_only(self):
        self.as_user(self.alice)
        old = self.session_of()
        sessions.detach(old)
        OfficeSession.objects.filter(pk=old.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.as_user(self.alice)  # opens again: a new key
        # ...unless its lease says it is still being written.
        sessions.forget(self.storage, ["/docs/a.docx"])
        self.as_user(self.alice)
        current = self.session_of()
        sessions.detach(current)
        OfficeSession.objects.filter(pk=current.pk).update(expires_at=timezone.now() - timedelta(seconds=1),
                                                           saving_until=timezone.now() + timedelta(minutes=1))
        self.assertEqual(self.code(self.client.post(CONFIG, {"storage": "disk", "path": "/docs/a.docx"},
                                                    format="json")), "closing")

    def test_a_session_gone_between_finding_and_joining_it(self):
        self.as_user(self.alice)
        stale = self.session_of()
        OfficeSession.objects.filter(pk=stale.pk).delete()  # a rename's forget(), say
        real = sessions.current
        calls = []

        def current(storage, path, editing=True):
            calls.append(path)
            return stale if len(calls) == 1 else real(storage, path, editing=editing)

        with mock.patch.object(sessions, "current", side_effect=current):
            again = sessions.open_session(self.storage, "/docs/a.docx", user=self.alice, can_edit=True)
        self.assertNotEqual(again.key, stale.key)
        self.assertTrue(OfficeSession.objects.filter(key=again.key).exists())

    def test_status_1_keeps_a_session_alive(self):
        key = self.as_user(self.alice)["document"]["key"]
        OfficeSession.objects.update(expires_at=timezone.now() + timedelta(hours=1))
        sessions.note_in_use(key, True)
        session = self.session_of()
        self.assertTrue(session.in_use)
        self.assertGreater(session.expires_at, timezone.now() + sessions.LIFETIME - timedelta(minutes=1))
        OfficeSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        sessions.note_in_use(key, True)  # an expired session isn't brought back
        self.assertLess(self.session_of().expires_at, timezone.now())

    def test_racing_creations_share_one_session(self):
        self.as_user(self.alice)
        session = self.session_of()
        with mock.patch.object(sessions, "current", side_effect=[None, session]):
            again = sessions.open_session(self.storage, "/docs/a.docx", user=self.alice, can_edit=True)
        self.assertEqual(again.key, session.key)
        self.assertEqual(OfficeSession.objects.count(), 1)


class ShareTests(OfficeTestCase):
    """The share variant, against a stand-in for contract S1 (apps.shares.access.share_file)."""

    def setUp(self):
        super().setUp()
        self.owner = User.objects.create_user("owner", password="pw-owner-2026")
        self.grant(self.owner, EVERYTHING)
        self.calls = []
        self.unlocked = []

        def stand_in(request, key, path):
            self.calls.append((key, path))
            if key != "Sh4reKey":
                raise policy.AccessDenied("share_not_found", "分享不存在。", status=404)
            access = policy.open_storage(request, self.storage, actor=policy.share_actor(self.owner, "/docs"))
            access.opened.update(self.unlocked)  # folder passwords the visitor entered in this share
            return access, access.path(path)

        patcher = mock.patch.object(office_shares, "share_file", side_effect=stand_in)
        patcher.start()
        self.addCleanup(patcher.stop)

    def share_config(self, key="Sh4reKey", path="/a.docx"):
        self.csrf()
        return self.client.post(CONFIG, {"share": key, "path": path}, format="json")

    def test_read_only_with_the_owner_as_identity(self):
        response = self.share_config()
        self.assertEqual(response.status_code, 200, response.content)
        config = response.json()
        self.assertEqual(self.calls, [("Sh4reKey", "/a.docx")])
        self.assertEqual(config["document"]["permissions"]["edit"], False)
        self.assertEqual(config["editorConfig"]["mode"], "view")
        self.assertEqual(config["editorConfig"]["user"], {"name": "匿名用户"})  # a visitor
        session = self.session_of(editing=False)
        self.assertEqual((session.user, session.can_edit), (self.owner, False))
        jwt.decode(config["token"], SECRET, algorithms=["HS256"])

    def test_visitors_dont_join_the_editing_session(self):
        # Not the editors' names, ids, nor their changes not saved yet: the file as it was saved last.
        key = self.ok_config()["document"]["key"]  # alice edits
        self.client.logout()
        config = self.share_config().json()
        self.assertNotEqual(config["document"]["key"], key)
        self.assertEqual(self.session_of().user, self.alice)
        self.assertEqual(config["document"]["key"], self.session_of(editing=False).key)

    def test_the_share_refuses(self):
        response = self.share_config(key="nope")
        self.assertEqual((response.status_code, self.code(response)), (404, "share_not_found"))
        self.assertFalse(OfficeSession.objects.exists())

    def test_the_owners_permissions_and_the_rules(self):
        self.grant(self.owner, ["upload"])
        self.assertEqual(self.code(self.share_config()), "permission_denied")
        self.grant(self.owner, EVERYTHING)
        self.hide("/docs/a.docx", mode=FilterMode.INACCESSIBLE)
        self.assertEqual(self.code(self.share_config()), "inaccessible")

    def test_ignore_hidden_doesnt_apply_in_a_share(self):
        self.grant(self.owner, [*EVERYTHING, "ignore_hidden"])
        self.hide("/docs/a.docx", mode=FilterMode.DISABLE_DOWNLOAD)
        self.assertEqual(self.code(self.share_config()), "download_disabled")

    def test_folder_passwords_inside_the_share(self):
        rule = self.lock("/docs", password="pw1")
        self.assertEqual(self.code(self.share_config()), "password_required")
        self.unlocked = [(rule.pk, rule.revision)]
        self.assertEqual(self.share_config().status_code, 200)

    def test_not_office_and_outside(self):
        self.assertEqual(self.code(self.share_config(path="/a.txt")), "not_office")
        self.assertEqual(self.code(self.share_config(path="/../top.docx")), "invalid_path")

    def test_a_logged_in_visitor_is_shown_as_themselves(self):
        self.login()
        config = self.share_config().json()
        self.assertEqual(config["editorConfig"]["user"]["name"], "alice")
        self.assertFalse(config["document"]["permissions"]["edit"])  # even though alice may edit the file


class WithoutTheSharesAppTests(OfficeTestCase):
    def test_no_shares_without_the_app(self):
        with mock.patch.object(office_shares, "apps", mock.Mock(is_installed=lambda name: False)):
            self.csrf()
            response = self.client.post(CONFIG, {"share": "Sh4reKey", "path": "/a.docx"}, format="json")
        self.assertEqual((response.status_code, self.code(response)), (404, "share_not_found"))


class BuildTests(OfficeTestCase):
    def test_editable_types_are_document_types(self):
        self.assertTrue(editor.EDITABLE <= set(editor.DOCUMENT_TYPES))
