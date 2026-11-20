"""The page to try the panel on (DEBUG only), the panel's files, and what a page with the panel adds to
its CSP for direct uploads (response.csp_sources, apps.siteconf.middleware.with_sources)."""

from pathlib import Path

from django.conf import settings
from django.test import override_settings
from django.utils.csp import CSP

from apps.siteconf.middleware import with_sources
from apps.storages import config as storage_config
from apps.storages.drivers.s3 import S3Driver
from apps.storages.models import StorageSource

from .base import EVERYTHING, UploadTestCase

STATIC = Path(settings.BASE_DIR) / "apps" / "uploads" / "static" / "uploads"


class TryPageTests(UploadTestCase):
    def test_only_with_debug(self):
        with override_settings(DEBUG=False):
            self.assertEqual(self.client.get("/components/uploads/").status_code, 404)
        with override_settings(DEBUG=True):
            page = self.client.get("/components/uploads/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertIn("uploads/js/uploads.js", html)
        self.assertIn("uploads/css/uploads.css", html)
        # Only local storages: nothing goes elsewhere, connect-src stays 'self'.
        self.assertNotIn("connect-src", page["Content-Security-Policy"])

    def test_a_direct_upload_storage_opens_connect_src(self):
        bucket = StorageSource.objects.create(key="b", name="b", type="s3", config=storage_config.encode(S3Driver, {
            "endpoint": "http://minio.lan:9000", "access_key": "AKIDTEST", "secret_key": "x", "bucket": "files",
            "base_path": "/", "path_style": "virtual", "is_private": True, "domain": "", "region": ""}))
        self.grant(self.alice, EVERYTHING, storage=bucket)
        with override_settings(DEBUG=True):
            policy = self.client.get("/components/uploads/")["Content-Security-Policy"]
        directive = next(part.strip() for part in policy.split(";") if part.strip().startswith("connect-src"))
        self.assertEqual(directive, "connect-src 'self' https: http://minio.lan:9000 http://*.minio.lan:9000")
        self.assertIn("script-src 'self'", policy)  # nothing else changes

    def test_the_panels_files(self):
        script = (STATIC / "js" / "uploads.js").read_text(encoding="utf-8")
        for needed in ("App.uploads", "hfile:files-changed", "Cancel uploading %(name)s?", "Upload %(count)s file?",
                       "Overwrite all", "Skip all", "upload under another name",
                       "This kind of storage doesn't support uploading empty files", "Uploaded; the server is passing it on",
                       "the browser blocked the cross-origin request", "max_file_uploads", "Content-Range"):
            self.assertIn(needed, script)
        # Names go in as text only.
        self.assertNotIn("innerHTML", script)
        self.assertNotIn("insertAdjacentHTML", script)
        css = (STATIC / "css" / "uploads.css").read_text(encoding="utf-8")
        self.assertEqual(css.count("{"), css.count("}"))


class SourcesTests(UploadTestCase):
    def test_with_sources(self):
        base = {"default-src": [CSP.SELF], "img-src": [CSP.SELF, "data:"]}
        merged = with_sources(base, {"connect-src": ["https:"], "img-src": ["data:", "https://cdn.example"]})
        self.assertEqual(merged["connect-src"], [CSP.SELF, "https:"])
        self.assertEqual(merged["img-src"], [CSP.SELF, "data:", "https://cdn.example"])
        self.assertEqual(with_sources({"default-src": [CSP.SELF]}, None), {"default-src": [CSP.SELF]})


class PlainHttpTests(UploadTestCase):
    """apps.uploads.csp: only well-formed plain-http hosts go into connect-src, never anything that could
    change the policy's syntax."""

    def test_hosts(self):
        from apps.uploads.csp import _plain_http

        self.assertEqual(_plain_http("http://minio.lan:9000/"), ["http://minio.lan:9000", "http://*.minio.lan:9000"])
        self.assertEqual(_plain_http("HTTP://10.0.0.5:9000"), ["http://10.0.0.5:9000"])
        self.assertEqual(_plain_http("http://files.example"), ["http://files.example", "http://*.files.example"])
        for value in ["https://s3.amazonaws.com", "http://[::1]:9000", "http://a b", "http://x;script-src *",
                      "http://例子.中国", "http://host:99999", "", None, 42, "ftp://x"]:
            with self.subTest(value=value):
                self.assertEqual(_plain_http(value), [])
