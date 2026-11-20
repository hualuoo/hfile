"""POST /onlyoffice/callback/: the token checks, the statuses, saving (who may, the SSRF guard), one save at
a time, and the keys forgotten when files change."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
import threading
import time
import warnings
from datetime import timedelta
from pathlib import Path
from unittest import mock, skipIf

import jwt
from django.db import connection
from django.db.models import F
from django.test import Client, TransactionTestCase
from django.utils import timezone

from apps.accounts.models import User
from apps.core import secrets
from apps.files import signals as file_signals
from apps.siteconf.models import SiteSettings
from apps.storages import config as storage_config
from apps.storages import instances
from apps.storages.drivers.local import LocalDriver
from apps.storages.models import FilterMode, FolderPassword, Grant, StorageSource

from .. import callback as callbacks
from .. import fetch, sessions
from ..models import OfficeSession
from .fake_ds import FakeDocumentServer
from .helpers import CALLBACK, DOCX, EDITED, EVERYTHING, SECRET, OfficeTestCase


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


class _Opened(OfficeTestCase):
    """alice has docs/a.docx open for editing: self.key."""

    def setUp(self):
        super().setUp()
        self.key = self.ok_config()["document"]["key"]
        self.changed = []

        def receiver(sender, source, paths, **kwargs):
            self.changed.append((sender, source.key, list(paths)))

        file_signals.changed.connect(receiver, weak=False, dispatch_uid="office-tests")
        self.addCleanup(file_signals.changed.disconnect, dispatch_uid="office-tests")


class TokenTests(_Opened):
    """Every way a token can fail answers {"error": 1} and changes nothing."""

    def assert_refused(self, response):
        self.assertEqual(self.answer(response), 1)
        self.assertEqual(self.disk(), DOCX)
        self.assertTrue(OfficeSession.objects.filter(key=self.key).exists())

    def payload(self, **extra):
        return {"key": self.key, "status": 2, "url": self.saved_document(), **extra}

    def test_a_valid_one_saves(self):  # the reference for the others
        self.assertEqual(self.answer(self.callback(self.payload())), 0)
        self.assertEqual(self.disk(), EDITED)

    def test_no_secret(self):
        self.configure(secret="")
        self.assert_refused(self.callback(self.payload()))

    def test_no_token(self):
        self.assert_refused(self.callback(body=self.payload()))
        self.assert_refused(self.callback(body={**self.payload(), "token": ""}))
        self.assert_refused(self.callback(body={**self.payload(), "token": 12}))

    def test_another_secret(self):
        self.assert_refused(self.callback(self.payload(), token=self.token(self.payload(), secret=SECRET + "!")))

    def test_alg_none(self):
        token = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(self.payload()) + "."
        self.assert_refused(self.callback(self.payload(), token=token))
        unsigned = _b64({"alg": "none", "typ": "JWT"}) + "." + _b64(self.payload()) + ".c2lnbmF0dXJl"
        self.assert_refused(self.callback(self.payload(), token=unsigned))

    def test_other_algorithms(self):
        with warnings.catch_warnings():  # PyJWT finds the secret short for these: the point is the algorithm
            warnings.simplefilter("ignore")
            for algorithm in ("HS384", "HS512"):
                self.assert_refused(self.callback(self.payload(), token=self.token(self.payload(), algorithm=algorithm)))

    def test_garbage(self):
        for token in ("x", "a.b.c", "....", "€." * 10, "eyJ" * 100):
            self.assert_refused(self.callback(self.payload(), token=token))

    def test_expired(self):
        stale = self.token({**self.payload(), "exp": int(time.time()) - 3600})
        self.assert_refused(self.callback(self.payload(), token=stale))

    def test_without_an_expiry(self):  # the Document Server always signs one
        self.assert_refused(self.callback(self.payload(), token=self.token(self.payload(), expires=None)))
        self.assert_refused(self.callback(self.payload(), token=self.token({**self.payload(), "exp": "soon"})))

    def test_a_token_that_isnt_ascii(self):  # a lone surrogate made PyJWT raise UnicodeEncodeError
        with self.assertNoLogs("hfile.office", "ERROR"), self.assertNoLogs("django.request", "ERROR"):
            self.assert_refused(self.callback(raw='{"token": "\\ud800eyJ"}'))
            self.assert_refused(self.callback(raw='{"token": "eyJ€.a.b"}'))

    def test_the_signed_fields(self):
        for claims in ({"status": 2, "url": self.saved_document()},  # no key
                       {"key": self.key, "url": self.saved_document()},  # no status
                       {"key": self.key, "status": "2", "url": self.saved_document()},  # status not an integer
                       {"key": self.key, "status": True, "url": self.saved_document()},
                       {"key": self.key, "status": 2.0, "url": self.saved_document()},
                       {"key": self.key, "status": 2},  # no url for 2
                       {"key": self.key, "status": 6, "url": ""},
                       {"key": 7, "status": 2, "url": self.saved_document()}):
            with self.subTest(claims=claims):
                self.assert_refused(self.callback(body={"token": self.token(claims)}))

    def test_body_fields_must_be_the_signed_ones(self):
        signed = self.payload()
        other = self.ok_config("/docs/b.xlsx")["document"]["key"]
        for name, value in (("key", other), ("status", 6), ("url", self.saved_document("other", b"PK\x03\x04 evil")),
                            ("users", ["somebody"])):
            with self.subTest(name=name):
                body = {**signed, "users": ["hfile-1"], name: value, "token": self.token({**signed, "users": ["hfile-1"]})}
                self.assert_refused(self.callback(body=body))
        self.assertEqual(self.disk("docs/b.xlsx"), b"PK xlsx")

    def test_missing_body_fields_come_from_the_token(self):
        self.assertEqual(self.answer(self.callback(body={"token": self.token(self.payload())})), 0)
        self.assertEqual(self.disk(), EDITED)

    def test_an_editor_config_token_is_no_callback_token(self):
        config = self.ok_config()
        token = config["token"]
        self.assert_refused(self.callback(body={"key": self.key, "status": 2, "url": self.saved_document(), "token": token}))
        # Not even when its claims are wrapped like a header token's.
        wrapped = self.token({"payload": {k: v for k, v in config.items() if k != "token"}})
        self.assert_refused(self.callback(body={"token": wrapped}))

    def test_the_authorization_header(self):
        payload = self.payload()
        response = self.callback(body=payload, bearer=self.token({"payload": payload}))
        self.assertEqual(self.answer(response), 0)
        self.assertEqual(self.disk(), EDITED)

    def test_the_authorization_header_with_top_level_claims(self):
        payload = self.payload()
        self.assertEqual(self.answer(self.callback(body=payload, bearer=self.token(payload))), 0)

    def test_a_bad_header_token(self):
        payload = self.payload()
        self.assert_refused(self.callback(body=payload, bearer=self.token({"payload": payload}, secret="nope" * 9)))
        self.assert_refused(self.callback(body=payload, bearer=""))
        response = self.ds_client.generic("POST", CALLBACK, json.dumps(payload), content_type="application/json",
                                          HTTP_AUTHORIZATION="Basic " + self.token(payload))
        self.assert_refused(response)

    def test_a_payload_that_isnt_an_object(self):
        self.assert_refused(self.callback(body={"token": self.token({"payload": "x", "key": self.key, "status": 2})}))

    def test_the_body(self):
        for raw in ("", "not json", "[1, 2]", '"text"', "null", "[" * 100_000):
            with self.subTest(raw=raw[:20]):
                self.assert_refused(self.callback(raw=raw))

    def test_a_body_too_large(self):
        payload = self.payload(padding="x" * (callbacks.BODY_MAX + 1))
        self.assert_refused(self.callback(payload))

    def test_only_post(self):
        for method in ("GET", "PUT", "DELETE", "PATCH"):
            response = self.ds_client.generic(method, CALLBACK)
            self.assertEqual(response.status_code, 405)
            self.assertEqual(response.json(), {"error": 1})
        self.assertEqual(self.disk(), DOCX)

    def test_csrf_exempt_and_anonymous(self):
        response = self.ds_client.generic("POST", CALLBACK, json.dumps({**self.payload(), "token": self.token(self.payload())}),
                                          content_type="application/json")
        self.assertEqual(self.answer(response), 0)

    def test_the_log_has_no_token_nor_url_query(self):
        payload = self.payload()
        payload["url"] = f"{self.ds.url}/cache/missing?md5=SECRETLINK&expires=1"
        token = self.token(payload)
        with self.assertLogs("hfile.office", "WARNING") as logs:
            self.assertEqual(self.answer(self.callback(body={**payload, "token": token})), 1)
        text = "\n".join(logs.output)
        self.assertNotIn(token, text)
        self.assertNotIn("SECRETLINK", text)
        self.assertIn("127.0.0.1", text)


class StatusTests(_Opened):
    def test_1_and_4_do_nothing_to_the_file(self):
        self.assertEqual(self.answer(self.callback({"key": self.key, "status": 1, "users": ["hfile-1"]})), 0)
        self.assertTrue(OfficeSession.objects.get(key=self.key).in_use)
        self.assertEqual(self.answer(self.callback({"key": self.key, "status": 4})), 0)
        session = OfficeSession.objects.get(key=self.key)
        self.assertFalse(session.in_use)
        self.assertEqual(session.current, session.file)
        self.assertEqual(self.disk(), DOCX)
        self.assertEqual(self.ds.paths(), [])
        self.assertEqual(self.changed, [])

    def test_1_for_a_key_nobody_knows(self):
        self.assertEqual(self.answer(self.callback({"key": "x" * 24, "status": 1})), 0)

    def test_other_statuses_do_nothing(self):
        for status in (0, 5, 8, -1, 99):
            self.assertEqual(self.answer(self.callback({"key": self.key, "status": status})), 0)
        self.assertEqual(self.disk(), DOCX)

    def test_2_saves_and_drops_the_key(self):
        self.assertEqual(self.save(self.key, 2, filetype="docx"), 0)
        self.assertEqual(self.disk(), EDITED)
        self.assertFalse(OfficeSession.objects.exists())
        self.assertEqual(self.changed, [(OfficeSession, "disk", ["/docs/a.docx"])])
        # The next open gets a new key.
        self.assertNotEqual(self.ok_config()["document"]["key"], self.key)

    def test_6_saves_and_keeps_the_key(self):
        self.assertEqual(self.save(self.key, 6, data=b"PK\x03\x04 force-saved"), 0)
        self.assertEqual(self.disk(), b"PK\x03\x04 force-saved")
        session = OfficeSession.objects.get(key=self.key)
        self.assertEqual(session.current, session.file)
        self.assertIsNone(session.saving_until)
        self.assertEqual(session.file_size, len(b"PK\x03\x04 force-saved"))
        self.assertEqual(self.changed, [(OfficeSession, "disk", ["/docs/a.docx"])])
        # Editing goes on with the same key; the file changed through it, so that is no outside change.
        self.assertEqual(self.ok_config()["document"]["key"], self.key)
        self.assertEqual(self.save(self.key, 2, data=b"PK\x03\x04 final"), 0)
        self.assertEqual(self.disk(), b"PK\x03\x04 final")

    def test_3_drops_the_key_without_saving(self):
        self.assertEqual(self.save(self.key, 3), 0)
        self.assertFalse(OfficeSession.objects.exists())
        self.assertEqual(self.disk(), DOCX)
        self.assertEqual(self.ds.paths(), [])

    def test_7_keeps_the_key_without_saving(self):
        self.assertEqual(self.save(self.key, 7), 0)
        self.assertTrue(OfficeSession.objects.filter(key=self.key).exists())
        self.assertEqual(self.disk(), DOCX)
        self.assertEqual(self.ds.paths(), [])

    def test_unknown_or_expired_keys(self):
        for status in (2, 3, 6, 7):
            self.assertEqual(self.save("y" * 24, status), 1)
            self.assertEqual(self.save("not a key", status), 1)
        OfficeSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.save(self.key, 2), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_a_read_only_session_never_saves(self):
        self.grant(self.alice, ["preview", "download"])
        key = self.ok_config("/docs/b.xlsx")["document"]["key"]
        self.assertEqual(self.save(key, 6), 1)
        self.assertEqual(self.save(key, 2), 1)
        self.assertEqual(self.disk("docs/b.xlsx"), b"PK xlsx")
        self.assertFalse(OfficeSession.objects.filter(key=key).exists())  # 2 dropped it all the same
        self.assertEqual(self.ds.paths(), [])

    def test_another_format_back_is_refused(self):
        self.assertEqual(self.save(self.key, 6, filetype="pdf"), 1)
        self.assertEqual(self.disk(), DOCX)
        self.assertEqual(self.save(self.key, 6, filetype="DOCX"), 0)

    def test_what_comes_back_must_look_like_the_document(self):
        for data in (b"", b"<html><body>502 Bad Gateway</body></html>", b"PK", b"{\\rtf1"):
            with self.subTest(data=data[:10]):
                self.assertEqual(self.save(self.key, 6, data=data), 1)
                self.assertEqual(self.disk(), DOCX)
        self.files({"docs/list.csv": b"a,b\n"})
        key = self.ok_config("/docs/list.csv")["document"]["key"]
        self.assertEqual(self.save(key, 6, data=b""), 0)  # text may be anything, empty too
        self.assertEqual(self.disk("docs/list.csv"), b"")
        self.assertTrue(callbacks.plausible("rtf", b"{\\rtf1\\ansi"))
        self.assertFalse(callbacks.plausible("rtf", b"PK\x03\x04"))
        self.assertTrue(callbacks.plausible("odt", b"PK\x03\x04"))

    def test_the_file_gone_meanwhile(self):
        (self.root / "docs" / "a.docx").unlink()  # outside the site: no signal
        self.assertEqual(self.save(self.key, 6), 1)
        self.assertFalse((self.root / "docs" / "a.docx").exists())
        (self.root / "docs" / "a.docx").mkdir()
        self.assertEqual(self.save(self.key, 6), 1)

    def test_a_save_into_a_password_folder(self):
        rule = self.lock("/docs", password="pw1")
        key = self.ok_config("/docs/b.xlsx", password="pw1")["document"]["key"]
        self.assertEqual(self.save(key, 6), 0)  # what alice unlocked holds for the save
        FolderPassword.objects.filter(pk=rule.pk).update(revision=rule.revision + 1)  # the password changed
        self.assertEqual(self.save(key, 6, data=b"PK\x03\x04 later"), 1)
        self.assertEqual(self.disk("docs/b.xlsx"), EDITED)


class RecheckTests(_Opened):
    """The save authorises the session's user again."""

    def assert_not_saved(self):
        self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_disabled_user(self):
        User.objects.filter(pk=self.alice.pk).update(is_active=False)
        self.assert_not_saved()

    def test_grant_disabled_or_gone(self):
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assert_not_saved()
        Grant.objects.filter(user=self.alice).delete()
        self.assert_not_saved()

    def test_permission_revoked(self):
        for missing in ("upload", "delete"):
            with self.subTest(missing=missing):
                self.grant(self.alice, [code for code in EVERYTHING if code != missing])
                self.assert_not_saved()

    def test_root_narrowed(self):
        self.grant(self.alice, EVERYTHING, root="/docs/sub")
        self.assert_not_saved()

    def test_storage_disabled(self):
        self.storage.enabled = False
        self.storage.save()
        self.assert_not_saved()

    def test_a_rule_added_meanwhile(self):
        self.hide("/docs", mode=FilterMode.INACCESSIBLE)
        self.assert_not_saved()

    def test_a_folder_password_added_meanwhile(self):
        self.lock("/docs/a.docx", password="new")
        self.assert_not_saved()

    def test_the_user_deleted(self):
        self.alice.delete()  # the session goes with them
        self.assertEqual(self.save(self.key, 2), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_after_the_download_too(self):
        # The permission is revoked while the document is downloaded.
        real = fetch.download

        def download(*args, **kwargs):
            size = real(*args, **kwargs)
            self.grant(self.alice, [code for code in EVERYTHING if code != "delete"])
            return size

        with mock.patch.object(fetch, "download", side_effect=download):
            self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_renamed_during_the_download(self):
        real = fetch.download

        def download(*args, **kwargs):
            size = real(*args, **kwargs)
            sessions.forget(self.storage, ["/docs"])
            return size

        with mock.patch.object(fetch, "download", side_effect=download):
            self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)


class StorageChangeTests(_Opened):
    """New params for the storage (another root, bucket or base path) end its sessions: the same path is
    another file then, and a save would write the old file's edits over it."""

    def new_root(self):
        """Another folder with a file of the same name, as an admin's new root."""
        other = self.root.parent / f"{self.root.name}-new"
        (other / "docs").mkdir(parents=True)
        (other / "docs" / "a.docx").write_bytes(b"PK\x03\x04 the new root's own file")
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        return other

    def save_params(self, root):
        storage = StorageSource.objects.get(pk=self.storage.pk)
        storage.config = storage_config.encode(type(instances.get(storage)), {"root": str(root)})
        storage.config_revision += 1
        storage.save()  # what apps.storages.service.save does when an admin changes the params
        return storage

    def test_new_params_end_the_storages_sessions(self):
        other_storage = self.make_storage("other", "local", self.root)
        self.grant(self.alice, EVERYTHING, storage=other_storage)
        kept = self.ok_config("/docs/b.xlsx", storage="other")["document"]["key"]
        other = self.new_root()
        self.save_params(other)
        self.assertFalse(OfficeSession.objects.filter(key=self.key).exists())
        self.assertTrue(OfficeSession.objects.filter(key=kept).exists())  # another storage's
        self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual((other / "docs" / "a.docx").read_bytes(), b"PK\x03\x04 the new root's own file")
        self.assertEqual(self.disk(), DOCX)
        # The next open is a new key, on the new root.
        self.assertNotEqual(self.ok_config()["document"]["key"], self.key)

    def test_through_the_admin_api(self):
        admin = User.objects.create_superuser("root", password="pw-root-2026")
        self.client.force_login(admin)
        self.csrf()
        url = f"/api/v1/manage/storages/{self.storage.pk}/"
        renamed = self.client.patch(url, {"name": "新名字"}, format="json")
        self.assertEqual(renamed.status_code, 200, renamed.content[:300])
        self.assertTrue(OfficeSession.objects.filter(key=self.key).exists())  # not a params change
        moved = self.client.patch(url, {"config": {"root": str(self.new_root())}}, format="json")
        self.assertEqual(moved.status_code, 200, moved.content[:300])
        self.assertFalse(OfficeSession.objects.filter(key=self.key).exists())
        self.assertEqual(self.save(self.key, 6), 1)

    def test_other_changes_keep_them(self):
        storage = StorageSource.objects.get(pk=self.storage.pk)
        storage.name = "改个名字"
        storage.link_lifetime = 60
        storage.save()
        self.assertTrue(OfficeSession.objects.filter(key=self.key).exists())
        self.assertEqual(self.save(self.key, 6), 0)

    def test_new_params_without_a_signal(self):
        # A change of the row the receiver doesn't see (QuerySet.update): the save compares the revision.
        StorageSource.objects.filter(pk=self.storage.pk).update(config_revision=F("config_revision") + 1)
        self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)
        self.assertNotEqual(self.ok_config()["document"]["key"], self.key)  # and the open rotates it

    def test_a_driver_instance_of_other_params(self):
        # An admin saved new params between the checks and the write: the live instance isn't the session's.
        with mock.patch.object(instances, "live", return_value=None):
            self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_saved_disabled(self):
        storage = StorageSource.objects.get(pk=self.storage.pk)
        storage.enabled = False
        storage.save()
        self.assertFalse(OfficeSession.objects.filter(key=self.key).exists())
        self.assertEqual(self.save(self.key, 6), 1)

    def test_disabled_and_enabled_again_by_the_list(self):
        # The storages list's switch (QuerySet.update, no signal): while it is off nothing is saved; on again
        # with the same params, the session saves into the same file.
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        self.assertEqual(self.save(self.key, 6), 1)
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=True)
        self.assertEqual(self.save(self.key, 6), 0)

    def test_deleted(self):
        self.storage.delete()
        self.assertFalse(OfficeSession.objects.exists())
        self.assertEqual(self.save(self.key, 2), 1)


class SsrfTests(_Opened):
    """The document is downloaded from the OnlyOffice server only."""

    def setUp(self):
        super().setUp()
        self.other = FakeDocumentServer().start()  # another origin: another port of the same host
        self.addCleanup(self.other.stop)
        with self.other.lock:
            self.other.files["edited"] = b"PK\x03\x04 from elsewhere"

    def save_url(self, url, status=6):
        return self.answer(self.callback({"key": self.key, "status": status, "url": url}))

    def assert_not_saved(self, url):
        self.assertEqual(self.save_url(url), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_another_host_or_port(self):
        self.assert_not_saved(f"{self.other.url}/cache/edited")
        self.assertEqual(self.other.paths(), [])
        self.assert_not_saved(self.saved_document().replace("127.0.0.1", "localhost"))
        self.assert_not_saved(self.saved_document().replace("http://", "https://"))

    def test_other_schemes_and_credentials(self):
        for url in ("file:///etc/passwd", "ftp://127.0.0.1/x", "gopher://127.0.0.1/", "//127.0.0.1/cache/edited",
                    "/cache/edited", self.saved_document().replace("http://", "http://user:pw@"),
                    self.saved_document() + "\n", "http://[::1]/x"):
            with self.subTest(url=url):
                self.assert_not_saved(url)
        self.assertEqual(self.ds.paths(), [])

    def test_a_redirect_to_another_host(self):
        self.assert_not_saved(f"{self.ds.url}/redirect?to={self.other.url}/cache/edited")
        self.assertEqual(self.other.paths(), [])
        self.assertEqual(self.ds.paths(), ["/redirect"])

    def test_hosts_in_other_scripts(self):
        # IDNA 2003 (Python's codec) and 2008 (httpx) read ß differently: such hosts are refused outright.
        self.assertIsNone(fetch.origin("https://office.glaß.io/"))
        self.assertIsNone(fetch.origin("http://１２７.0.0.1/x"))
        self.assertIsNone(fetch.origin("http://127.0.0.1\u00ad/x"))
        self.assertEqual(fetch.origin("https://office.xn--gla-7ka.io/"), ("https", "office.xn--gla-7ka.io", 443))
        port = self.ds.url.rsplit(":", 1)[1]
        self.assert_not_saved(f"http://１２７.0.0.1:{port}/cache/edited")
        self.assertEqual(self.ds.paths(), [])

    def test_a_location_httpx_cant_read(self):
        self.assert_not_saved(f"{self.ds.url}/redirect?to=http://127.0.0.1:%20bad/x")

    def test_redirects_on_the_server_are_followed(self):
        self.saved_document()
        self.assertEqual(self.save_url(f"{self.ds.url}/chain/2?end=/cache/edited"), 0)
        self.assertEqual(self.disk(), EDITED)
        self.assertEqual(self.save_url(f"{self.ds.url}/redirect?to=/cache/edited"), 0)

    def test_too_many_redirects(self):
        self.saved_document()
        self.assert_not_saved(f"{self.ds.url}/chain/{fetch.MAX_REDIRECTS + 1}?end=/cache/edited")
        self.assertEqual(self.ds.paths().count("/cache/edited"), 0)
        self.assertEqual(len(self.ds.paths()), fetch.MAX_REDIRECTS + 1)

    def test_a_body_too_large(self):
        self.assert_not_saved(f"{self.ds.url}/big")  # its Content-Length says so: refused before reading
        with mock.patch.object(fetch, "MAX_BYTES", 100_000):
            self.assert_not_saved(f"{self.ds.url}/stream?size=200000")  # no Content-Length: counted
            self.assertEqual(self.save_url(f"{self.ds.url}/stream?size=1000"), 0)
        self.assertEqual(self.disk(), b"PK\x03\x04" + b"y" * 996)

    def test_cut_short_or_failing(self):
        self.assert_not_saved(f"{self.ds.url}/short")
        self.assert_not_saved(f"{self.ds.url}/status/404")
        self.assert_not_saved(f"{self.ds.url}/status/500")
        self.assert_not_saved(f"{self.ds.url}/status/204")

    def test_too_slow(self):
        with mock.patch.object(fetch, "DEADLINE", 0.0):
            self.assert_not_saved(f"{self.ds.url}/stream?size=200000")

    def test_a_server_that_drips_a_byte_now_and_then(self):
        # Each read gets its byte well inside 30 seconds: only the deadline on the sockets stops it.
        with mock.patch.object(fetch, "DEADLINE", 0.8):
            began = time.monotonic()
            self.assert_not_saved(f"{self.ds.url}/drip?seconds=20&every=0.2")
            self.assertLess(time.monotonic() - began, 5)

    def test_unreachable(self):
        self.configure(url="http://127.0.0.1:9")  # nothing listens on the discard port
        self.assert_not_saved("http://127.0.0.1:9/cache/edited")

    def test_the_server_address_with_a_path_and_default_ports(self):
        self.configure(url=f"{self.ds.url}/office/")
        self.assertEqual(self.save_url(self.saved_document()), 0)
        self.assertEqual(fetch.origin("HTTPS://Office.Example.COM/x"), ("https", "office.example.com", 443))
        self.assertIsNone(fetch.origin("http://例子.中国:8080/"))  # the setting has it as punycode
        self.assertEqual(fetch.origin("http://xn--fsqu00a.xn--fiqs8s:8080/"), ("http", "xn--fsqu00a.xn--fiqs8s", 8080))
        self.assertIsNone(fetch.origin("http://a:b@host/"))
        self.assertIsNone(fetch.origin("http://host:99999/"))


class OneSaveAtATimeTests(_Opened):
    def test_a_held_lease_makes_the_next_save_wait(self):
        OfficeSession.objects.filter(key=self.key).update(saving_until=timezone.now() + timedelta(minutes=5))
        sleeps = []

        def other_save_done(seconds):  # the other save finishes while this one waits
            sleeps.append(seconds)
            OfficeSession.objects.filter(key=self.key).update(saving_until=None)

        with mock.patch.object(callbacks.time, "sleep", side_effect=other_save_done):
            self.assertEqual(self.save(self.key, 6), 0)
        self.assertEqual(len(sleeps), 1)
        self.assertEqual(self.disk(), EDITED)
        self.assertIsNone(OfficeSession.objects.get(key=self.key).saving_until)

    def test_it_gives_up_after_a_while(self):
        OfficeSession.objects.filter(key=self.key).update(saving_until=timezone.now() + timedelta(minutes=5))
        with mock.patch.object(callbacks, "WAIT", 0.05), mock.patch.object(callbacks, "POLL", 0.01):
            self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_a_lease_left_by_a_crash_runs_out(self):
        OfficeSession.objects.filter(key=self.key).update(saving_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.save(self.key, 6), 0)

    def test_the_final_save_of_the_old_key_blocks_a_new_keys_save(self):
        # Status 2 of the old key is saving (its row detached, holding the lease). The file opens to nobody
        # meanwhile (sessions._closing); should a session of it exist all the same, its saves wait too.
        old = OfficeSession.objects.get(key=self.key)
        sessions.detach(old)
        OfficeSession.objects.filter(pk=old.pk).update(saving_until=timezone.now() + timedelta(minutes=5))
        self.assertEqual(self.code(self.config()), "closing")
        new = OfficeSession.objects.create(
            key=sessions.new_key(), storage=self.storage, storage_revision=old.storage_revision, path="/docs/a.docx",
            file=old.file, current=old.file, user=self.alice, can_edit=True,
            expires_at=timezone.now() + timedelta(hours=1))
        with mock.patch.object(callbacks, "WAIT", 0.05), mock.patch.object(callbacks, "POLL", 0.01):
            self.assertEqual(self.save(new.key, 6), 1)
        OfficeSession.objects.filter(pk=old.pk).update(saving_until=None)
        self.assertEqual(self.save(new.key, 6), 0)

    def test_a_save_that_lost_its_lease_doesnt_write(self):
        with mock.patch.object(callbacks.Lease, "held", return_value=False):
            self.assertEqual(self.save(self.key, 6), 1)
        self.assertEqual(self.disk(), DOCX)

    def test_a_lease_is_renewed_and_let_go_only_by_its_save(self):
        session = OfficeSession.objects.get(key=self.key)
        lease = callbacks._try_lease(session.pk)
        first = lease.until
        self.assertTrue(lease.held())
        self.assertTrue(lease.renew())
        self.assertGreaterEqual(lease.until, first)
        self.assertEqual(OfficeSession.objects.get(pk=session.pk).saving_until, lease.until)
        # Another save took over after this lease ran out: this one renews and lets go of nothing.
        other = timezone.now() + timedelta(minutes=3)
        OfficeSession.objects.filter(pk=session.pk).update(saving_until=other)
        self.assertFalse(lease.held())
        self.assertFalse(lease.renew())
        lease.release()
        self.assertEqual(OfficeSession.objects.get(pk=session.pk).saving_until, other)

    def test_the_lease_is_let_go_after_a_failure(self):
        self.assertEqual(self.answer(self.callback({"key": self.key, "status": 6, "url": f"{self.ds.url}/status/500"})), 1)
        self.assertIsNone(OfficeSession.objects.get(key=self.key).saving_until)
        self.assertEqual(self.save(self.key, 6), 0)


@skipIf(connection.vendor == "sqlite", "the in-memory SQLite test database is one shared cache for all threads")
class ConcurrentSavesTests(TransactionTestCase):
    """Two callbacks at once for one file, in two threads with connections of their own (MySQL): the second
    save starts only once the first one has written."""

    def setUp(self):
        self.ds = FakeDocumentServer().start()
        self.addCleanup(self.ds.stop)
        patcher = mock.patch.object(instances, "GRACE", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        instances.close_all()
        self.addCleanup(instances.close_all)
        self.root = Path(tempfile.mkdtemp(prefix="hfile-office-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        (self.root / "a.docx").write_bytes(DOCX)
        self.storage = StorageSource.objects.create(
            key="disk", name="disk", type="local", config=storage_config.encode(LocalDriver, {"root": str(self.root)}))
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        Grant.objects.create(user=alice, storage=self.storage, permissions=EVERYTHING, root_path="/", enabled=True)
        site = SiteSettings.load()
        site.onlyoffice_url = self.ds.url
        site.onlyoffice_secret = secrets.encrypt(SECRET)
        site.save()
        self.key = sessions.open_session(self.storage, "/a.docx", user=alice, can_edit=True).key

    def test_saves_of_one_file_run_one_after_the_other(self, first_write=0.3, second_start=0.1):
        with self.ds.lock:
            self.ds.files["first"] = b"PK\x03\x04 first"
            self.ds.files["second"] = b"PK\x03\x04 second"
        driver = instances.get(self.storage)
        original = driver.write
        events = []

        def write(path, data, size):
            head = data.read(13)
            events.append(("starts", head))
            data.seek(0)
            time.sleep(first_write if head.endswith(b"first") else 0.3)
            original(path, data, size)
            events.append(("ends", None))

        answers = []

        def post(name, delay):
            time.sleep(delay)
            payload = {"key": self.key, "status": 6, "url": f"{self.ds.url}/cache/{name}"}
            token = jwt.encode({**payload, "exp": int(time.time()) + 300}, SECRET, algorithm="HS256")
            body = json.dumps({**payload, "token": token})
            try:
                response = Client().generic("POST", CALLBACK, body, content_type="application/json")
                answers.append(response.json()["error"])
            finally:
                connection.close()

        with mock.patch.object(driver, "write", side_effect=write):
            threads = [threading.Thread(target=post, args=("first", 0)),
                       threading.Thread(target=post, args=("second", second_start))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(30)
        self.assertEqual(answers, [0, 0])
        self.assertEqual(events, [("starts", b"PK\x03\x04 first"), ("ends", None), ("starts", b"PK\x03\x04 second"), ("ends", None)])
        self.assertEqual((self.root / "a.docx").read_bytes(), b"PK\x03\x04 second")

    def test_the_heartbeat_keeps_a_long_write_its_lease(self):
        # A lease of one second, renewed every 0.2 s: the first save writes for 2 s, the second (begun at
        # 1.3 s, when an unrenewed lease would have run out) still waits for it.
        with mock.patch.object(callbacks, "LEASE", timedelta(seconds=1)), \
                mock.patch.object(callbacks, "HEARTBEAT", 0.2):
            self.test_saves_of_one_file_run_one_after_the_other(first_write=2.0, second_start=1.3)


class ForgetTests(_Opened):
    """apps.files.signals.changed drops the keys at and below the changed paths."""

    def keys(self):
        return set(OfficeSession.objects.values_list("path", flat=True))

    def open_more(self):
        for path in ("/docs/b.xlsx", "/docs/sub/c.pptx", "/top.docx"):
            self.ok_config(path)

    def test_rename_through_the_api(self):
        self.open_more()
        response = self.post("rename", {"path": "/docs", "name": "a.docx", "new_name": "renamed.docx"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.keys(), {"/docs/b.xlsx", "/docs/sub/c.pptx", "/top.docx"})

    def test_moving_a_folder(self):
        self.open_more()
        (self.root / "elsewhere").mkdir()
        response = self.post("move", {"path": "/docs", "names": ["sub"], "target": "/elsewhere"})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.keys(), {"/docs/a.docx", "/docs/b.xlsx", "/top.docx"})

    def test_deleting(self):
        self.open_more()
        self.post("delete", {"path": "/", "names": ["docs"]})
        self.assertEqual(self.keys(), {"/top.docx"})

    def test_an_overwrite_by_the_text_editor(self):
        response = self.client.put("/api/v1/uploads/text/", {"storage": "disk", "path": "/docs/a.docx",
                                                              "content": "plain", "encoding": "utf-8"}, format="json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(self.keys(), set())

    def test_by_segments(self):
        self.files({"docs-2/x.docx": b"PK", "do/y.docx": b"PK"})
        self.ok_config("/docs-2/x.docx")
        self.ok_config("/do/y.docx")
        sessions.forget(self.storage, ["/docs"])
        self.assertEqual(self.keys(), {"/docs-2/x.docx", "/do/y.docx", })
        sessions.forget(self.storage, ["/do"])
        self.assertEqual(self.keys(), {"/docs-2/x.docx"})

    def test_case_and_wildcards_match_exactly(self):
        self.files({"Docs/z.docx": b"PK", "d%cs/q.docx": b"PK", "d_cs/r.docx": b"PK"})
        for path in ("/Docs/z.docx", "/d%cs/q.docx", "/d_cs/r.docx"):
            self.ok_config(path)
        sessions.forget(self.storage, ["/docs"])  # a.docx only
        self.assertEqual(self.keys(), {"/Docs/z.docx", "/d%cs/q.docx", "/d_cs/r.docx"})
        sessions.forget(self.storage, ["/d%cs", "/d_cs"])
        self.assertEqual(self.keys(), {"/Docs/z.docx"})

    def test_the_root_and_another_storage(self):
        self.open_more()
        other = self.make_storage("other", "local", self.root)
        sessions.forget(other, ["/"])
        self.assertEqual(len(self.keys()), 4)
        sessions.forget(self.storage, ["/"])
        self.assertEqual(self.keys(), set())

    def test_many_paths_at_once(self):  # a batch delete of a thousand files sends them all in one signal
        self.open_more()
        many = [f"/docs/other-{n}.docx" for n in range(1000)] + ["/docs/sub", "/top.docx"]
        self.assertEqual(sessions.forget(self.storage, many), 2)
        self.assertEqual(self.keys(), {"/docs/a.docx", "/docs/b.xlsx"})

    def test_bad_paths_are_ignored(self):
        self.assertEqual(sessions.forget(self.storage, ["", "docs", None, "/a/../b"]), 0)
        self.assertEqual(self.keys(), {"/docs/a.docx"})

    def test_our_own_save_keeps_the_editing_key_and_ends_the_viewing_one(self):
        bob = User.objects.create_user("bob", password="pw-bob-2026")
        self.grant(bob, ["preview", "download"])
        self.client.force_login(bob)
        self.csrf()
        viewing = self.ok_config()["document"]["key"]
        file_signals.changed.send_robust(sender=OfficeSession, source=self.storage, paths=["/docs/a.docx"])
        self.assertEqual(set(OfficeSession.objects.values_list("key", flat=True)), {self.key})
        self.ok_config()
        file_signals.changed.send_robust(sender=None, source=self.storage, paths=["/docs/a.docx"])
        self.assertEqual(self.keys(), set())
        self.assertNotEqual(viewing, self.key)

    def test_a_save_gives_viewers_the_new_version(self):
        bob = User.objects.create_user("bob", password="pw-bob-2026")
        self.grant(bob, ["preview", "download"])
        self.client.force_login(bob)
        self.csrf()
        viewing = self.ok_config()["document"]["key"]
        self.assertEqual(self.save(self.key, 6), 0)  # alice's force-save
        self.assertFalse(OfficeSession.objects.filter(key=viewing).exists())
        self.assertNotEqual(self.ok_config()["document"]["key"], viewing)
        self.assertTrue(OfficeSession.objects.filter(key=self.key).exists())

    def test_a_key_dropped_while_editing_ends_the_save(self):
        self.post("rename", {"path": "/docs", "name": "a.docx", "new_name": "renamed.docx"})
        self.assertEqual(self.save(self.key, 2), 1)
        self.assertFalse((self.root / "docs" / "a.docx").exists())


class CleanupTests(_Opened):
    def test_the_job(self):
        from apps.core import cleanup

        self.assertIn("office", [job.name for job in cleanup.jobs()])
        self.ok_config("/docs/b.xlsx")
        now = timezone.now()
        OfficeSession.objects.filter(key=self.key).update(expires_at=now - timedelta(seconds=1))
        self.assertEqual(sessions.remove_expired(now), 1)
        self.assertEqual(list(OfficeSession.objects.values_list("path", flat=True)), ["/docs/b.xlsx"])

    def test_a_session_being_saved_stays(self):
        now = timezone.now()
        OfficeSession.objects.update(expires_at=now - timedelta(seconds=1), saving_until=now + timedelta(minutes=1))
        self.assertEqual(sessions.remove_expired(now), 0)

    def test_through_the_command(self):
        from apps.core import cleanup

        OfficeSession.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        results = cleanup.run(only=["office"])
        self.assertEqual(results, [("office", "过期的 OnlyOffice 编辑会话", 1)])
        self.assertFalse(OfficeSession.objects.exists())
