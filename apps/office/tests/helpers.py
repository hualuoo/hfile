"""What the OnlyOffice tests share: the files tests' storage "disk" (a local folder) with a few Office files,
alice with every permission, a fake Document Server (apps/office/tests/fake_ds.py) as the OnlyOffice 服务地址
with SECRET, and helpers that ask for configs and post callbacks as the Document Server does."""

from __future__ import annotations

import json
import logging
import time
from unittest import mock

import jwt
from django.test import Client

from apps.core import secrets
from apps.files.tests.helpers import EVERYTHING, FilesTestCase
from apps.siteconf.models import SiteSettings

from .. import callback as callbacks
from ..models import OfficeSession
from .fake_ds import FakeDocumentServer

CONFIG = "/api/v1/office/config/"
CALLBACK = "/onlyoffice/callback/"
EDITOR = "/onlyoffice/edit/"
SECRET = "office-tests-secret-0123456789-abcdef"  # 32 bytes and more: PyJWT warns below that
DOCX = b"PK\x03\x04 a docx of the tests"
EDITED = b"PK\x03\x04 edited by OnlyOffice"  # a ZIP's first bytes, as a saved .docx or .xlsx has them
NO_DELETE = [code for code in EVERYTHING if code != "delete"]


class OfficeTestCase(FilesTestCase):
    """FilesTestCase's "disk" with docs/a.docx, docs/b.xlsx, docs/old.doc, docs/sub/c.pptx and top.docx; the
    site's OnlyOffice 服务地址 is the fake Document Server (self.ds, shared by the class)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ds = FakeDocumentServer().start()

    @classmethod
    def tearDownClass(cls):
        cls.ds.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.files({"docs/a.docx": DOCX, "docs/b.xlsx": b"PK xlsx", "docs/old.doc": b"\xd0\xcf\x11\xe0 doc",
                    "docs/sub/c.pptx": b"PK pptx", "top.docx": b"PK top"})
        self.configure(url=self.ds.url, secret=SECRET)
        with self.ds.lock:
            self.ds.files.clear()
            self.ds.requests.clear()
        # The Document Server's requests carry no cookie of the site.
        self.ds_client = Client()
        # Refusals and saves are logged; quiet here (assertLogs still sees them).
        logger = logging.getLogger("hfile.office")
        self.addCleanup(logger.setLevel, logger.level)
        logger.setLevel(logging.CRITICAL)
        # A save waits for another one of the same file this long at most (60 s in the site).
        patcher = mock.patch.object(callbacks, "WAIT", 0.5)
        patcher.start()
        self.addCleanup(patcher.stop)

    def configure(self, *, url=None, secret=None):
        site = SiteSettings.load()
        if url is not None:
            site.onlyoffice_url = url
        if secret is not None:
            site.onlyoffice_secret = secrets.encrypt(secret)
        site.save()

    # -- the editor config

    def config(self, path="/docs/a.docx", *, storage="disk", login=True, **extra):
        if login and "_auth_user_id" not in self.client.session:
            self.login()
        if "HTTP_X_CSRFTOKEN" not in self.client._credentials:
            self.csrf()
        body = {"storage": storage, "path": path, **extra}
        return self.client.post(CONFIG, body, format="json")

    def ok_config(self, path="/docs/a.docx", **kwargs):
        response = self.config(path, **kwargs)
        self.assertEqual(response.status_code, 200, response.content[:500])
        return response.json()

    def session_of(self, path="/docs/a.docx", *, editing=True):
        """The file's editing session (editing=False: its viewing session)."""
        return OfficeSession.objects.get(storage=self.storage, path=path, can_edit=editing)

    # -- the Document Server's callbacks

    def token(self, payload, secret=SECRET, algorithm="HS256", expires=300, **headers):
        """A token as the Document Server signs it: with an expiry (exp) unless expires is None."""
        claims = dict(payload)
        if expires is not None and "exp" not in claims:
            claims["exp"] = int(time.time()) + expires
        return jwt.encode(claims, secret, algorithm=algorithm, headers=headers or None)

    def callback(self, payload=None, *, body=None, token=None, bearer=None, raw=None, **extra):
        """Posts a callback: `payload` signed in the body's token (unless `token`/`bearer` say otherwise)."""
        if raw is None:
            if body is None:
                body = dict(payload or {})
                if token is None and bearer is None:
                    body["token"] = self.token(payload)
            if token is not None:
                body["token"] = token
            raw = json.dumps(body)
        headers = {}
        if bearer is not None:
            headers["HTTP_AUTHORIZATION"] = f"Bearer {bearer}"
        return self.ds_client.generic("POST", CALLBACK, raw, content_type="application/json", **headers, **extra)

    def answer(self, response):
        self.assertEqual(response["Content-Type"], "application/json")
        return response.json()["error"]

    def saved_document(self, name="edited", data=EDITED):
        """Puts an edited document on the fake Document Server: its URL."""
        with self.ds.lock:
            self.ds.files[name] = data
        return f"{self.ds.url}/cache/{name}"

    def save(self, key, status=2, *, data=EDITED, **extra):
        url = self.saved_document("edited", data)
        payload = {"key": key, "status": status, "url": url, **extra}
        return self.answer(self.callback(payload))

    def disk(self, path="docs/a.docx"):
        return (self.root / path).read_bytes()
