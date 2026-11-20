"""The languages (settings.LANGUAGES): the choice of the page's, the catalogs in config/locale/ and what
scripts/i18n.py compiles of them, and a check that the code's texts are English (their Chinese is in the
zh_Hans catalog)."""

import ast
import os
import re
import unittest
from html import unescape
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from django.utils import translation

from config import i18n

from .catalogs import JS_DIR, js_catalog, mo_bytes, po_files, read_po

ROOT = Path(settings.BASE_DIR)
CJK = re.compile(r"[\u3400-\u9fff\u3000-\u303f\uff00-\uffef]")


class LanguageTests(TestCase):
    def page(self, **headers):
        return self.client.get("/", **headers).content.decode()

    def test_the_browser_then_the_menu(self):
        self.assertIn('<html lang="zh-hans">', self.page())  # neither: the default
        self.assertIn('<html lang="en">', self.page(HTTP_ACCEPT_LANGUAGE="en-US,en;q=0.9"))
        self.assertIn('<html lang="zh-hans">', self.page(HTTP_ACCEPT_LANGUAGE="zh-CN,zh;q=0.9,en;q=0.8"))
        self.assertIn('<html lang="en">', self.page(HTTP_ACCEPT_LANGUAGE="fr-FR,fr;q=0.9,en;q=0.8"))
        self.assertIn('<html lang="zh-hans">', self.page(HTTP_ACCEPT_LANGUAGE="ja"))
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "zh-hans"  # what the menu chose wins
        self.assertIn('<html lang="zh-hans">', self.page(HTTP_ACCEPT_LANGUAGE="en"))
        self.client.cookies[settings.LANGUAGE_COOKIE_NAME] = "xx"  # not one of the site's: the browser's
        self.assertIn('<html lang="en">', self.page(HTTP_ACCEPT_LANGUAGE="en"))

    def test_the_menu_and_the_scripts_catalog(self):
        page = self.page(HTTP_ACCEPT_LANGUAGE="en")
        menu = page[page.index('id="language-menu"'):]
        menu = menu[: menu.index("</div>")]
        self.assertIn(f'data-language-cookie="{settings.LANGUAGE_COOKIE_NAME}"', menu)
        self.assertEqual(re.findall(r'aria-checked="(\w+)" lang="([\w-]+)" data-language="[\w-]+">([^<]+)<', menu),
                         [("false", "zh-hans", "简体中文"), ("true", "en", "English")])
        self.assertIn('aria-label="Language"', page)
        scripts = re.findall(r'<script src="([^"?]+)', page)
        self.assertEqual(scripts[1:4], ["/static/web/i18n/en.js", "/static/web/js/language.js", "/static/web/js/common.js"])
        self.assertIn("/static/web/i18n/zh-hans.js", self.page())

    def test_an_api_error_in_the_pages_language(self):
        self.assertEqual(self.client.get("/api/v1/no-such-thing/", HTTP_ACCEPT_LANGUAGE="en")["Content-Language"], "en")

    def test_commands_follow_the_system(self):
        cases = [
            ({"LANG": "en_US.UTF-8"}, "en"), ({"LANG": "zh_CN.UTF-8"}, "zh-hans"),
            ({"LANG": "C.UTF-8"}, "zh-hans"), ({"LANG": "de_DE.UTF-8"}, "zh-hans"),
            ({"LANG": "zh_CN.UTF-8", "LC_ALL": "en_GB.UTF-8"}, "en"), ({"LANG": "en_US", "LANGUAGE": "zh_CN:en"}, "zh-hans"),
        ]
        if os.name != "nt":  # none of them: on Windows the language Windows shows (test_commands_follow_windows)
            cases.append(({}, "zh-hans"))
        for environ, expected in cases:
            with self.subTest(environ), mock.patch.dict(os.environ, environ, clear=True):
                self.assertEqual(i18n.system_language(), expected)

    @unittest.skipUnless(os.name == "nt", "Windows' own language")
    def test_commands_follow_windows(self):
        """Windows sets none of the variables: there the language Windows shows, English on GitHub's runner."""
        for shown, environ, expected in (("en_US", {}, "en"), ("zh_TW", {}, "zh-hans"), ("", {}, "zh-hans"),
                                         ("en_US", {"LANG": "zh_CN.UTF-8"}, "zh-hans")):
            with self.subTest(shown=shown, environ=environ), mock.patch.dict(os.environ, environ, clear=True), \
                    mock.patch("config.windows.ui_language", return_value=shown):
                self.assertEqual(i18n.system_language(), expected)

    def test_before_django_starts_the_same_catalog(self):
        fix = ("The settings file %(path)s has %(count)s thing to fix:", "The settings file %(path)s has %(count)s things to fix:")
        with mock.patch("django.apps.apps.ready", False):
            with mock.patch.dict(os.environ, {"LANGUAGE": "zh_CN"}):
                self.assertEqual(i18n._("Language"), "语言")
                self.assertEqual(i18n.ngettext(*fix, 2), "设置文件 %(path)s 里有 %(count)s 处要改：")
            with mock.patch.dict(os.environ, {"LANGUAGE": "en"}):
                self.assertEqual(i18n._("Language"), "Language")
                self.assertEqual(i18n.ngettext(*fix, 2), fix[1])
        with translation.override("zh-hans"):  # Django's, in the active language
            self.assertEqual(i18n._("Language"), "语言")


class EnglishTests(TestCase):
    """A few texts of each part in English, made in the language of the request that shows them."""

    en = {"HTTP_ACCEPT_LANGUAGE": "en"}

    def setUp(self):
        from django.contrib.auth import get_user_model

        self.admin = get_user_model().objects.create_superuser("boss", password="pw-boss-2026")

    def test_the_frame_and_the_error_pages(self):
        page = self.client.get("/no-such-page/", **self.en).content.decode()
        self.assertIn("Page not found", page)
        self.assertIn("Back to home", page)
        self.client.force_login(self.admin)
        page = self.client.get("/", **self.en).content.decode()
        self.assertEqual(re.findall(r'nav-label">([^<]+)<', page), ["Home", "My shares", "Manage"])
        self.assertIn("Change password", page)
        self.assertIn("Log out", page)

    def test_the_apis_errors(self):
        self.assertEqual(self.client.get("/api/v1/manage/users/", **self.en).json()["detail"], "Please log in first.")
        r = self.client.post("/api/v1/session/", data="{bad", content_type="application/json", **self.en)
        self.assertEqual(r.json()["detail"], "Invalid JSON at line 1, column 2: expected a field name in double quotes.")
        # DRF's messages that name a type are read from DRF's own translations, once for each language
        self.client.force_login(self.admin)
        url = "/api/v1/manage/login-logs/delete/"
        for headers, message in ((self.en, "Expected an array ([…]), not a string."), ({}, "要的是一个数组（[…]），不是字符串。"),
                                 (self.en, "Expected an array ([…]), not a string.")):
            with self.subTest(message):
                r = self.client.post(url, {"ids": "x"}, content_type="application/json", **headers)
                self.assertEqual(r.json(), {"ids": [message]})

    def test_the_file_browser(self):
        self.client.force_login(self.admin)
        page = self.client.get("/", **self.en).content.decode()
        self.assertIn("<h2>No storage sources to browse yet</h2>", page)
        self.assertIn("Add a storage source on the Manage page", page)
        # The scripts' texts are the code's English: zh-hans.js has their Chinese, en.js nothing to translate.
        catalog = (JS_DIR / "zh-hans.js").read_text(encoding="utf-8")
        for text in ("Folder readme", "Move %(name)s", "Some items couldn't be moved. View the details?"):
            self.assertIn(f'"{text}": ', catalog)
        self.assertNotIn("Folder readme", (JS_DIR / "en.js").read_text(encoding="utf-8"))

    def test_links_and_shares(self):
        self.assertIn("The short link doesn't exist.", unescape(self.client.get("/s/nosuch", **self.en).content.decode()))
        page = unescape(self.client.get("/share/nosuch", **self.en).content.decode())
        self.assertIn("Share link unavailable", page)
        self.assertIn("The share link doesn't exist; it may have been deleted.", page)
        # 存储源 and 用户 in the menu, as a column and inside a sentence: each in its own form
        self.client.force_login(self.admin)
        page = self.client.get("/manage/", **self.en).content.decode()
        self.assertIn(">Storage sources</a>", page)
        self.assertIn('<p class="manage-group">Users</p>', page)
        self.assertIn("<th>User</th>", page)
        r = self.client.get("/api/v1/manage/download-logs/?storage=x", **self.en)
        self.assertEqual(r.json(), {"storage": ["Should be a storage source id (a positive integer)."]})
        catalog = (JS_DIR / "zh-hans.js").read_text(encoding="utf-8")
        for text in ("Get both links", "Share link copied to the clipboard", "Uploading %(n)s file"):
            self.assertIn(f'"{text}": ', catalog)

    def test_accounts_and_sso(self):
        from django.core.management import CommandError, call_command

        r = self.client.post("/api/v1/session/", {"username": "boss", "password": "wrong"}, content_type="application/json", **self.en)
        self.assertEqual(r.json(), {"non_field_errors": ["Wrong username or password."]})
        self.assertIn("Single sign-on not found", unescape(self.client.get("/sso/nosuch/login", **self.en).content.decode()))
        self.client.force_login(self.admin)
        page = unescape(self.client.get("/manage/", **self.en).content.decode())
        self.assertIn("<h2>Users</h2>", page)
        self.assertIn("Search username, nickname or ID", page)
        self.assertIn("Create an app in GitHub's Settings", page)  # the presets' help, in the page's language
        with translation.override("en"), self.assertRaisesMessage(CommandError, 'There is no account with the username "nobody".'):
            call_command("resetadmin", "nobody")
        # fields made while the serializers are (at import), their messages still in the request's language
        r = self.client.post("/api/v1/manage/sso/providers/", {}, content_type="application/json", **self.en)
        self.assertEqual(r.json()["name"], ["Name is required."])
        r = self.client.put("/api/v1/users/me/password/", {}, content_type="application/json", **self.en)
        self.assertEqual(r.json()["new_password"], ["A password is required."])
        # a message with a number, filled in by the field when it fails
        r = self.client.put("/api/v1/users/me/password/", {"new_password": "x" * 129}, content_type="application/json", **self.en)
        self.assertEqual(r.json()["new_password"], ["The password is at most 128 characters."])

    def test_site_settings(self):
        from io import StringIO

        from django.core.management import call_command

        from apps.siteconf.models import SiteSettings
        from apps.siteconf.serializers import PublicSiteSerializer

        # The default home name is stored as it is (首页) and shown in the page's language; another stays.
        self.assertEqual(self.client.get("/api/v1/site/", **self.en).json()["home_name"], "Home")
        self.assertEqual(self.client.get("/api/v1/site/").json()["home_name"], "首页")
        with translation.override("en"):
            self.assertEqual(PublicSiteSerializer(SiteSettings(home_name="资料")).data["home_name"], "资料")
            out = StringIO()
            call_command("unblock", stdout=out)
        self.assertIn("The IP and UA blocklists were already empty; nothing was changed.", out.getvalue())
        self.client.force_login(self.admin)
        page = unescape(self.client.get("/manage/", **self.en).content.decode())
        self.assertIn("Download rate limit", page)
        self.assertIn("Preview of site icon", page)

    def test_storages(self):
        self.client.force_login(self.admin)
        types = {item["type"]: item for item in self.client.get("/api/v1/manage/storage-types/", **self.en).json()["types"]}
        self.assertEqual(types["local"]["label"], "Local storage")
        self.assertEqual(types["local"]["params"][0]["label"], "Folder")
        self.assertIn("Allow direct uploads from this site (set CORS)", [helper["label"] for helper in types["s3"]["helpers"]])
        # made while the driver classes are, at import: still in the request's language
        self.assertIn("Register an app in \"App registrations\" in the Azure portal (https://portal.azure.com)",
                      types["onedrive"]["params"][0]["help"])
        answer = self.client.get("/api/v1/manage/storages/key-check/?key=bad%20key", **self.en).json()
        self.assertEqual(answer["message"], "The alias can only have lowercase letters, digits, - and _.")

    def test_a_command(self):
        from io import StringIO

        from django.core.management import call_command

        out = StringIO()
        with translation.override("en"):
            call_command("cleanup", "--dry-run", stdout=out)
        self.assertIn("This was a dry run: nothing was deleted.", out.getvalue())

    def test_the_installer(self):
        import tempfile

        from django.core.management.color import no_style
        from django.test import override_settings

        from config import configfile
        from apps.installer import banner

        with tempfile.TemporaryDirectory(prefix="hfile-home-") as home:
            config = Path(home) / "config.toml"
            configfile.create(config)
            setup = {"ROOT_URLCONF": "apps.installer.setup_urls", "SETUP_MODE": True, "ALLOWED_HOSTS": ["*"]}
            with override_settings(CONFIG_FILE=config, **setup):
                page = unescape(self.client.get("/install/", **self.en).content.decode())
                self.assertIn("<h1>Install HFile</h1>", page)
                self.assertIn("/static/web/i18n/en.js", page)
                # the form's labels and choices are made with its class, at import
                self.assertIn('<label for="id_token">Install token', page)
                self.assertIn("1 proxy: Nginx, Caddy, aaPanel, 1Panel, etc.", page)
                self.assertIn("3 proxies", page)
                with self.assertLogs("installer", "WARNING"):
                    r = self.client.post("/install/check/", {"step": "token", "token": "AAAA-BBBB-CCCC-DDDD"}, **self.en)
                self.assertTrue(r.json()["errors"]["token"][0].startswith("The install token is wrong. It is printed in"))
                r = self.client.get("/api/v1/site/", **self.en)
                self.assertEqual(r.json()["detail"], "The site isn't installed yet: open /install/ first and follow the setup wizard.")
                with translation.override("en"):  # what serve prints, in the command's language
                    text = banner.render("http://127.0.0.1:8000", no_style())
                self.assertIn("HFile isn't installed yet. Open http://127.0.0.1:8000/install/?token=", text)

    def test_backups_and_upgrades(self):
        from io import StringIO

        from django.core.management import CommandError, call_command

        out = StringIO()
        with translation.override("en"):
            call_command("upgrade", "--check", stdout=out)
            with self.assertRaisesMessage(CommandError, "The backup file's name must end in .sql.gz."):
                call_command("backup", "--output", "x.txt")
            with self.assertRaisesMessage(CommandError, "nosuch.sql.gz: No such file."):
                call_command("restore", "nosuch.sql.gz", "--yes")
        self.assertIn("The database needs no upgrade.", out.getvalue())

    def test_the_settings_file(self):
        # In the page's language when the installer writes it; before Django, the system's (config/tests.py).
        import tempfile

        from config import configfile

        with tempfile.TemporaryDirectory(prefix="hfile-home-") as home, translation.override("en"):
            text = configfile.render()
            with self.assertRaises(configfile.ConfigError) as caught:
                configfile.clean({"server": {"port": "80"}, "sever": {}}, "config.toml")
            bad = Path(home) / "config.toml"
            bad.write_text("[server]\nhost = 0.0.0.0\n", encoding="utf-8")
            with self.assertRaisesMessage(configfile.ConfigError, "isn't valid: Expected newline or end of document after "
                                          "a statement (line 2, column 11).\n  The format is TOML: "):
                configfile.read(bad)
        self.assertTrue(text.startswith("# HFile's settings. A change takes effect when the program is started again.\n"))
        self.assertIn("\n# Port\nport = 8080\n", text)
        self.assertIn('\nhost = "127.0.0.1"\nport = 3306\nname = "hfile"\n', text)  # nothing above those without a comment
        self.assertIsNone(CJK.search(text))
        self.assertEqual(str(caught.exception), "\n".join([
            "The settings file config.toml has 2 things to fix:",
            "  - [sever]: There is no such section; check the spelling. The sections are: [server], [database], [site], "
            "[paths], [upgrade].",
            '  - [server] port: Must be a whole number from 1 to 65535 (no quotes); it is "80" now.',
        ]))


class CatalogTests(SimpleTestCase):
    def test_compiled_from_the_po_files(self):
        """Run `python scripts/i18n.py compile` after changing a .po file."""
        self.assertTrue(po_files())
        for po in po_files():
            with self.subTest(po.relative_to(ROOT)):
                self.assertEqual(po.with_suffix(".mo").read_bytes(), mo_bytes(po.read_text(encoding="utf-8")))
        for code, _name in settings.LANGUAGES:
            with self.subTest(code):
                self.assertEqual((JS_DIR / f"{code}.js").read_bytes(), js_catalog(code))

    def test_every_text_translated(self):
        """Translate what `python scripts/i18n.py extract` brought in (an empty msgstr, or one marked fuzzy)."""
        for po in po_files():
            entries = read_po(po.read_text(encoding="utf-8"))
            self.assertEqual(entries[0]["msgid"], "")
            self.assertFalse(entries[0]["fuzzy"])
            missing = [entry["msgid"] for entry in entries[1:] if entry["fuzzy"] or not all(entry["msgstr"])]
            self.assertEqual(missing, [], po.relative_to(ROOT))


# ---------------------------------------------------------------- no Chinese in the code

# Calls whose string arguments are translated (Django's, and config/i18n.py's _ before Django runs).
PY_CALLS = {"_", "gettext", "gettext_lazy", "ngettext", "ngettext_lazy", "pgettext", "pgettext_lazy",
            "npgettext", "npgettext_lazy", "gettext_noop"}
PY_LAZY = {"gettext_lazy", "ngettext_lazy", "pgettext_lazy", "npgettext_lazy", "gettext_noop"}
# Chinese that stays as it is: the language names (each in its own words), characters the text
# editor's encodings handle (uploads/text.py: fullwidth slashes, Big5's refused characters), the full
# stop sso/flow.py takes off a reason it puts inside a sentence, the separators a list of suffixes or
# hosts may have (siteconf/serializers.py, installer/forms.py), what storages answer in Chinese: a
# Chinese FTP server's dates, the words 115 errors are told by, and DogeCloud's name for a bucket in the
# S3 driver's messages; and two English texts that quote Chinese (siteconf/serializers.py: the root
# name's default as sites store it, an ICP number).
ALLOWED = {
    ("apps/siteconf/serializers.py", "The first part of the path above the file list. The default, 首页, shows in each "
     "visitor's language (Home in English)."),
    ("apps/siteconf/serializers.py", 'When filled in, shown at the bottom of the home and file pages, linking to the MIIT '
     'filing site, like "京ICP备12345678号".'),
    ("config/settings.py", "简体中文"), ("apps/uploads/text.py", "\uff0f"), ("apps/uploads/text.py", "\uff3c"),
    ("apps/uploads/text.py", "\u00a2\u00a3\u00a5\u2022\u203e\u223c\u2609\u2641\uff64"), ("apps/sso/flow.py", "\u3002."),
    ("apps/siteconf/serializers.py", "[\\s,\uff0c\u3001;\uff1b]+"), ("apps/installer/forms.py", "[,\uff0c;\uff1b\\s]+"),
    ("apps/storages/drivers/dogecloud.py", "存储桶"), ("apps/storages/drivers/dogecloud.py", "存储空间"),
    ("apps/storages/drivers/ftp.py", "月"), ("apps/storages/drivers/ftp.py", "日"), ("apps/storages/drivers/ftp.py", "年"),
    ("apps/storages/drivers/ftp.py", r"(?:^|\s)(?P<size>\d{1,20})\s+(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2}|[A-Za-z]{3}\s+\d{1,2}"
     r"|\d{1,2}\s+[A-Za-z]{3}|\d{1,2}月\s+\d{1,2}日?)\s+(?P<time>\d{1,2}:\d{2}|\d{4}年?)\s(?P<name>.*)"),
    *(("apps/storages/drivers/open115.py", word) for word in ("不存在", "已存在", "同名", "频繁", "限流", "空间不足", "空间已满")),
}
# Pages for developers that only DEBUG shows (the components, trying the upload panel, the viewers'
# self-test): left in Chinese.
DEBUG_ONLY = {
    "apps/web/templates/web/components.html", "apps/web/templates/web/_components.html",
    "apps/web/static/web/js/components.js", "apps/uploads/templates/uploads/try.html",
    "apps/uploads/static/uploads/js/try.js", "apps/preview/templates/preview/selftest.html",
    "apps/preview/static/preview/js/selftest.js",
}
SKIP_DIRS = {"tests", "migrations", "vendor", "node_modules", "locale", "i18n", "__pycache__"}


def _sources():
    for top in ("apps", "config"):
        for path in sorted((ROOT / top).rglob("*")):
            parts = set(path.relative_to(ROOT).parts[:-1])
            name = path.name
            if parts & SKIP_DIRS or name.startswith("test") or name.endswith(".min.js"):
                continue
            if path.suffix in (".py", ".html", ".js"):
                yield path
    yield ROOT / "hfile.py"


def _py_problems(source):
    """Chinese in a string (docstrings aside: texts are English, translated ones too), and gettext that runs
    when the module is imported (it would keep the language of that moment: gettext_lazy there)."""
    tree = ast.parse(source)
    problems = []

    def call_name(node):
        func = node.func
        return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None

    def visit(node, at_import=True, docstring=None):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if CJK.search(node.value) and node is not docstring:
                problems.append((node.lineno, node.value))
            return
        if isinstance(node, ast.JoinedStr):
            if any(isinstance(v, ast.Constant) and CJK.search(v.value) for v in node.values):
                problems.append((node.lineno, "f-string: " + ast.unparse(node)[:80]))
            for value in node.values:
                visit(value, at_import)
            return
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            # A lazy text formatted or joined right away is translated right away: in the language of the
            # moment, at import that of nobody's request (format_lazy, or the field's own {placeholders}).
            sides = (node.left, node.right) if isinstance(node.op, ast.Add) else (node.left,)
            for side in sides:
                if isinstance(side, ast.Call) and call_name(side) in PY_LAZY - {"gettext_noop"}:
                    sign = "%" if isinstance(node.op, ast.Mod) else "+"
                    problems.append((node.lineno, f"{call_name(side)}() {sign}: translated at once, format_lazy"))
        if isinstance(node, ast.Call):
            name = call_name(node)
            if name in PY_CALLS and at_import and name not in PY_LAZY:
                problems.append((node.lineno, f"{name}() when the module is imported: gettext_lazy"))
            for arg in node.args:
                visit(arg, at_import)
            for keyword in node.keywords:
                visit(keyword.value, at_import)
            visit(node.func, at_import)
            return
        body_doc = None
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                body_doc = first.value
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for child in (*node.decorator_list, node.args):
                visit(child, at_import)
            for child in node.body:
                visit(child, False, body_doc)
            return
        if isinstance(node, ast.Lambda):
            visit(node.body, False)
            return
        for child in ast.iter_child_nodes(node):
            visit(child, at_import, body_doc if body_doc is not None else docstring)

    visit(tree)
    return problems


_TEMPLATE_COMMENTS = re.compile(r"\{#.*?#\}|\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S)


def _template_problems(source):
    kept = _TEMPLATE_COMMENTS.sub(lambda m: "\n" * m.group(0).count("\n"), source)
    return [(number, line.strip()) for number, line in enumerate(kept.splitlines(), 1) if CJK.search(line)]


_JS_REGEX_BEFORE = set("(,=:[!&|?{};+-*%<>~^") | {"return", "typeof", "instanceof", "in", "of", "new", "delete",
                                                   "void", "throw", "case", "do", "else", "yield", "await", "=>"}


def js_tokens(source):
    """(kind, text, line) for the strings, template literals ("template", the whole of it), names and
    punctuation of a script; comments and whitespace left out. Enough of JavaScript for this check."""
    tokens, i, line, n = [], 0, 1, len(source)
    braces = []  # for each ${ open in a template: how many { are open inside it
    previous = None

    def add(kind, text):
        nonlocal previous, line
        tokens.append((kind, text, line))
        line += text.count("\n")
        previous = text if kind in ("name", "punct") else kind

    def template_end(start):
        j = start
        while j < n:
            if source[j] == "\\":
                j += 2
            elif source[j] == "`":
                return j + 1, False
            elif source.startswith("${", j):
                return j + 2, True
            else:
                j += 1
        raise ValueError("unterminated template")

    while i < n:
        c = source[i]
        if c in " \t\r\n":
            line += c == "\n"
            i += 1
        elif source.startswith("//", i):
            i = source.find("\n", i)
            i = n if i < 0 else i
        elif source.startswith("/*", i):
            end = source.index("*/", i) + 2
            line += source.count("\n", i, end)
            i = end
        elif c in "'\"":
            j = i + 1
            while source[j] != c:
                j += 2 if source[j] == "\\" else 1
            add("string", source[i:j + 1])
            i = j + 1
        elif c == "`" or (c == "}" and braces and braces[-1] == 0):
            if c == "}":
                braces.pop()
            end, opened = template_end(i + 1)
            add("template", source[i:end])
            if opened:
                braces.append(0)
            i = end
        elif c == "/" and (previous is None or previous in _JS_REGEX_BEFORE):
            j, in_class = i + 1, False
            while True:
                if source[j] == "\\":
                    j += 2
                    continue
                if source[j] == "[":
                    in_class = True
                elif source[j] == "]":
                    in_class = False
                elif source[j] == "/" and not in_class:
                    break
                j += 1
            j += 1
            while j < n and (source[j].isalnum()):
                j += 1
            add("regex", source[i:j])
            i = j
        elif c.isalpha() or c in "_$":
            j = i
            while j < n and (source[j].isalnum() or source[j] in "_$"):
                j += 1
            add("name", source[i:j])
            i = j
        elif c.isdigit():
            j = i
            while j < n and (source[j].isalnum() or source[j] in "._"):
                j += 1
            add("number", source[i:j])
            i = j
        else:
            if c == "{" and braces:
                braces[-1] += 1
            elif c == "}" and braces:
                braces[-1] -= 1
            two = source[i:i + 2]
            if two in ("=>", "?.", "??", "&&", "||", "==", "!=", "<=", ">=", "++", "--", "**"):
                add("punct", two)
                i += 2
            else:
                add("punct", c)
                i += 1
    return tokens


def _js_problems(source):
    """Chinese in a string or a template literal (comments aside)."""
    return [(line, text[:80]) for kind, text, line in js_tokens(source) if kind in ("string", "template") and CJK.search(text)]


# A Python statement whose first line has this comment is data, not text for people (a file format
# the program reads back, like a backup's header): its Chinese stays as it is in every language.
DATA_MARK = "# i18n: data"


def _data_lines(source):
    lines = source.splitlines()
    marked = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.stmt) and DATA_MARK in lines[node.lineno - 1]:
            marked.update(range(node.lineno, node.end_lineno + 1))
    return marked


def untranslated(path):
    source = path.read_text(encoding="utf-8")
    check = {".py": _py_problems, ".html": _template_problems, ".js": _js_problems}[path.suffix]
    relative = path.relative_to(ROOT).as_posix()
    data = _data_lines(source) if path.suffix == ".py" else set()
    return [(number, text) for number, text in check(source) if (relative, text) not in ALLOWED and number not in data]


class UntranslatedTests(SimpleTestCase):
    def test_no_chinese_in_the_code(self):
        """A text on a page, in a script or a message is written in English inside gettext() (Python and
        scripts), gettext_lazy() (what is made when the module is imported) or {% translate %}
        ({% blocktranslate %}), and translated in config/locale/zh_Hans/ (docs/development.md, 翻译)."""
        found = {}
        for path in _sources():
            relative = path.relative_to(ROOT).as_posix()
            if relative in DEBUG_ONLY:
                continue
            if problems := untranslated(path):
                found[relative] = problems[:5]
        self.assertEqual(found, {})

    def test_a_lazy_text_formatted_at_once(self):
        source = (
            'A = ngettext_lazy("At most %(n)s item", "At most %(n)s items", "n") % {"n": 5}\n'
            'B = gettext_lazy("The first sentence.") + " (1)"\n'
            'C = format_lazy(gettext_lazy("{label} can\'t be empty."), label=x)\n'
            'D = {"max_length": ngettext_lazy("At most {max_length} item", "At most {max_length} items", "max_length")}\n'
            'def f():\n'
            '    return pgettext_lazy("c", "Some %(x)s") % {"x": 1}\n'
        )
        self.assertEqual([number for number, _text in _py_problems(source)], [1, 2, 6])

    def test_chinese_even_in_a_translation(self):
        source = '"""模块的说明。"""\nA = gettext_lazy("名字")\nB = gettext_lazy("Name")\n'
        self.assertEqual(_py_problems(source), [(2, "名字")])
        self.assertEqual(_template_problems('{% translate "Name" %}\n{% translate "名字" %}{# 注释 #}\n{# 注释 #}'),
                         [(2, '{% translate "名字" %}')])

    def test_the_script_reader(self):
        tokens = js_tokens("const a = x / 2; const r = /'[/]'/g; const s = `a ${b ? `c${d}` : '}'} e`; f('中')")
        self.assertEqual([t[:2] for t in tokens if t[0] in ("string", "template", "regex")], [
            ("regex", "/'[/]'/g"), ("template", "`a ${"), ("template", "`c${"), ("template", "}`"),
            ("string", "'}'"), ("template", "} e`"), ("string", "'中'")])
        self.assertEqual(_js_problems("gettext('Name'); ngettext('one', 'many', n); f('中'); interpolate(gettext('第 %s 项'), [1])"),
                         [(1, "'中'"), (1, "'第 %s 项'")])
