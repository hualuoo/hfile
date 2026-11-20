"""The storages section of /manage/, its scripts and the OAuth callback page: in the shell, under the strict
CSP, with icons that exist."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase

from apps.web.navigation import SECTIONS

User = get_user_model()
APP = Path(settings.BASE_DIR) / "apps" / "storages"
SPRITE = Path(settings.BASE_DIR) / "apps" / "web" / "static" / "web" / "icons.svg"


class ManageSectionTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")

    def test_the_section_is_first_in_the_menu(self):
        self.assertEqual((SECTIONS[0].key, SECTIONS[0].group, SECTIONS[0].label), ("storages", "存储", "存储源"))

    def test_the_page(self):
        self.client.force_login(self.admin)
        page = self.client.get("/manage/").content.decode()
        self.assertIn('id="storages-root"', page)
        self.assertRegex(page, r'<link rel="stylesheet" href="/static/storages/css/storages\.css\?v=[0-9a-f]{12}">')
        # grants.js (App.grantsEditor, for the users section) runs after common.js: deferred.
        self.assertRegex(page, r'<script src="/static/storages/js/grants\.js\?v=[0-9a-f]{12}" defer></script>')
        self.assertRegex(page, r'<script src="/static/storages/js/manage_storages\.js\?v=[0-9a-f]{12}"></script>')
        self.assertLess(page.index("web/js/manage.js"), page.index("storages/js/manage_storages.js"))
        for help_id in ("storages-help-glob", "storages-help-filters", "storages-help-passwords", "storages-help-readmes"):
            self.assertIn(f'<template id="{help_id}">', page)
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertNotRegex(page, r"\sstyle=")
        self.assertNotRegex(page, r"\son[a-z]+=")

    def test_the_oauth_page_is_in_the_shell(self):
        self.client.force_login(self.admin)
        response = self.client.get("/storages/oauth/callback/", {"state": "x", "code": "y"})
        self.assertEqual(response.status_code, 400)
        page = response.content.decode()
        self.assertIn("这次授权已经过期了", page)
        self.assertRegex(page, r'<script src="/static/storages/js/oauth_callback\.js\?v=[0-9a-f]{12}"></script>')
        self.assertIn('href="/manage/#storages"', page)
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")
        self.assertEqual(response["Cache-Control"].count("no-cache") + response["Cache-Control"].count("no-store") > 0, True)


class ScriptTests(SimpleTestCase):
    def scripts(self):
        return {path.name: path.read_text(encoding="utf-8") for path in (APP / "static" / "storages" / "js").glob("*.js")}

    def test_every_icon_in_use_exists(self):
        ids = set(re.findall(r'<symbol id="i-([a-z0-9_]+(?:-fill)?)"', SPRITE.read_text(encoding="utf-8")))
        used = {}
        for path in (APP / "templates").rglob("*.html"):
            for name in re.findall(r'\{% icon "([a-z0-9_-]+)"', path.read_text(encoding="utf-8")):
                used.setdefault(name, path.name)
        call = re.compile(r"\b(?:icon)\(\s*([^,()]*)")
        for name, text in self.scripts().items():
            for first in call.findall(text):
                for found in re.findall(r"'([a-z0-9_]+)'", first):
                    used.setdefault(found, name)
            for block in re.findall(r"_ICONS = \{([^}]*)\}", text):
                for found in re.findall(r":\s*'([a-z0-9_]+)'", block):
                    used.setdefault(found, name)
            for block in re.findall(r"RULE_KINDS = \{(.*?)\n  \};", text, re.S):
                for found in re.findall(r"icon: '([a-z0-9_]+)'", block):
                    used.setdefault(found, name)
        self.assertGreater(len(used), 25)
        self.assertEqual({name: where for name, where in used.items() if name not in ids}, {})

    def test_the_grants_editor_is_defined(self):
        text = self.scripts()["grants.js"]
        self.assertIn("App.grantsEditor = grantsEditor;", text)
        self.assertIn("function grantsEditor(element, { userId })", text)
        self.assertIn("/api/v1/manage/users/${userId}/grants/", text)

    def test_nothing_inline_nor_as_html(self):
        for name, text in self.scripts().items():
            with self.subTest(script=name):
                self.assertNotIn("innerHTML", text)
                self.assertNotIn("insertAdjacentHTML", text)
                self.assertNotIn("eval(", text)
                self.assertNotRegex(text, r"\.setAttribute\('style'")
