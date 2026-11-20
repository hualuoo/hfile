"""What pages carry for the viewers: the scripts and the style sheet, registered with apps.files.pages and on
the browse page; the vendored files and how they are served; the DEBUG-only self-test page."""

import re
import shutil
import tempfile
from pathlib import Path

from django.conf import settings
from django.test import TestCase, override_settings

from apps.accounts.models import User
from apps.accounts.permissions import ADMIN_DEFAULTS
from apps.files import pages
from apps.storages import config, instances
from apps.storages.drivers import driver_class
from apps.storages.models import Grant, StorageSource

from ..apps import SCRIPTS, STYLES

PREVIEW = Path(settings.BASE_DIR) / "apps" / "preview"
VENDOR = PREVIEW / "static" / "preview" / "vendor"


class AssetTests(TestCase):
    def test_registered_with_the_browser(self):
        assets = pages.assets()
        self.assertEqual([path for path in assets["scripts"] if path.startswith("preview/")], list(SCRIPTS))
        self.assertEqual([path for path in assets["styles"] if path.startswith("preview/")], list(STYLES))

    def test_on_the_browse_page(self):
        folder = Path(tempfile.mkdtemp(prefix="hfile-preview-"))
        self.addCleanup(shutil.rmtree, folder, True)
        self.addCleanup(instances.close_all)
        storage = StorageSource.objects.create(
            key="local", name="本地", type="local", config=config.encode(driver_class("local"), {"root": str(folder)}))
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        Grant.objects.create(user=alice, storage=storage, permissions=list(ADMIN_DEFAULTS))
        self.client.force_login(alice)
        html = self.client.get("/local/").content.decode()
        self.assertRegex(html, r'<link rel="stylesheet" href="/static/preview/css/preview\.css\?v=[0-9a-f]{12}">')
        files_js = html.index("/static/files/js/files.js")
        for script in SCRIPTS:
            self.assertRegex(html, rf'<script src="/static/{re.escape(script)}\?v=[0-9a-f]{{12}}"></script>')
            self.assertGreater(html.index(f"/static/{script}"), files_js)  # App.files first
        self.assertNotIn('type="application/json"', html)  # no inline script, not even of JSON

    def test_vendored_files_are_there_with_their_licences(self):
        for path in ("hls.js-1.7.3/hls.min.js", "hls.js-1.7.3/hls.worker.js", "hls.js-1.7.3/LICENSE",
                     "pdf.js-6.4.299/pdf.min.mjs", "pdf.js-6.4.299/pdf.worker.min.mjs", "pdf.js-6.4.299/LICENSE",
                     "pdf.js-6.4.299/cmaps/UniGB-UTF16-H.bcmap", "pdf.js-6.4.299/standard_fonts/FoxitSerif.pfb",
                     "pdf.js-6.4.299/standard_fonts/LICENSE_FOXIT", "pdf.js-6.4.299/wasm/openjpeg.wasm",
                     "pdf.js-6.4.299/wasm/openjpeg_nowasm_fallback.js", "pdf.js-6.4.299/wasm/LICENSE_OPENJPEG",
                     "pdf.js-6.4.299/iccs/CGATS001Compat-v2-micro.icc", "highlight.js-11.12.0/highlight.min.js",
                     "highlight.js-11.12.0/LICENSE",
                     *[f"highlight.js-11.12.0/languages/{name}.min.js" for name in ("dos", "properties", "dockerfile", "nginx", "powershell")]):
            self.assertTrue((VENDOR / path).is_file(), path)

    def test_the_scripts_name_the_vendored_versions(self):
        """preview.js finds the libraries next to itself, by these folder names."""
        script = (PREVIEW / "static" / "preview" / "js" / "preview.js").read_text(encoding="utf-8")
        for folder in (path.name for path in VENDOR.iterdir()):
            self.assertIn(f"${{VENDOR}}{folder}/", script)

    def test_served_with_the_right_types(self):
        for path, kind in (("pdf.js-6.4.299/pdf.min.mjs", "text/javascript"), ("pdf.js-6.4.299/wasm/openjpeg.wasm", "application/wasm"),
                           ("hls.js-1.7.3/hls.worker.js", "text/javascript"), ("pdf.js-6.4.299/cmaps/UniGB-UTF16-H.bcmap", "application/octet-stream")):
            r = self.client.get(f"/static/preview/vendor/{path}")
            self.assertEqual(r.status_code, 200, path)
            self.assertTrue(r["Content-Type"].startswith(kind), (path, r["Content-Type"]))


class SelfTestPageTests(TestCase):
    @override_settings(DEBUG=True)
    def test_with_debug(self):
        r = self.client.get("/components/preview/selftest/")
        self.assertContains(r, 'id="selftest-result"')
        for script in (*SCRIPTS, "preview/js/selftest.js"):
            self.assertContains(r, f"/static/{script}?v=")

    def test_only_with_debug(self):
        self.assertEqual(self.client.get("/components/preview/selftest/").status_code, 404)
