"""The pages: the MD3 frame (base.html) with its registries, the login and password dialogs, /manage/,
the DEBUG-only /components/, error pages and icons."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.template import Context, Template, TemplateSyntaxError
from django.test import SimpleTestCase, TestCase, override_settings

from apps.core.testing import admin_url
from apps.storages import icons as storage_icons

from .navigation import NAV, SECTIONS

User = get_user_model()
WEB = Path(settings.BASE_DIR) / "apps" / "web"


class FrameTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026", nickname="老板")
        self.user = User.objects.create_user("alice", password="pw-alice-2026")

    def nav(self, page):
        part = page[page.index('<nav class="nav"'):]
        return part[: part.index("</nav>")]

    def test_a_strict_csp_and_nothing_inline(self):
        for user in (None, self.user, self.admin):
            if user:
                self.client.force_login(user)
            for path in ("/", "/manage/") if user is self.admin else ("/",):
                with self.subTest(user=user, path=path):
                    r = self.client.get(path)
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r["Content-Security-Policy"].split("; ")[:3], ["default-src 'self'", "script-src 'self'", "style-src 'self'"])
                    page = r.content.decode()
                    self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")  # no inline scripts
                    self.assertNotRegex(page, r"\sstyle=")  # no style attributes
                    self.assertNotRegex(page, r"\son[a-z]+=")  # no inline event handlers

    def test_the_head(self):
        page = self.client.get("/").content.decode()
        head = page[: page.index("</head>")]
        # The theme is set before anything is drawn, then the tokens, then the components.
        self.assertLess(head.index("web/js/theme.js"), head.index("web/css/m3.css"))
        self.assertLess(head.index("web/css/m3.css"), head.index("web/css/app.css"))
        self.assertRegex(head, r'<meta name="icons" content="/static/web/icons\.svg\?v=[0-9a-f]{12}">')
        self.assertIn('<meta name="color-scheme" content="light dark">', head)
        self.assertIn("<title>首页 · HFile</title>", head)
        self.assertIn('<html lang="zh-hans">', page)

    def test_the_destinations_follow_the_user(self):
        nav = self.nav(self.client.get("/").content.decode())
        self.assertEqual(re.findall(r'nav-label">([^<]+)<', nav), ["首页"])
        self.assertIn('href="/" aria-current="page"', nav)
        self.client.force_login(self.user)  # 我的分享: logged-in users (shares)
        self.assertEqual(re.findall(r'nav-label">([^<]+)<', self.nav(self.client.get("/").content.decode())), ["首页", "我的分享"])
        self.client.force_login(self.admin)
        nav = self.nav(self.client.get("/manage/").content.decode())
        self.assertEqual(re.findall(r'nav-label">([^<]+)<', nav), ["首页", "我的分享", "管理"])
        self.assertIn('href="/manage/" aria-current="page"', nav)
        self.assertNotIn('href="/" aria-current', nav)
        self.assertIn("#i-admin_panel_settings-fill", nav)  # the current one's icon is the filled form
        with override_settings(DEBUG=True):
            nav = self.nav(self.client.get("/").content.decode())
        self.assertEqual(re.findall(r'nav-label">([^<]+)<', nav), ["首页", "我的分享", "管理", "组件"])

    def test_visitors_get_the_login_dialog_and_a_theme_menu(self):
        page = self.client.get("/").content.decode()
        self.assertIn('id="login-dialog"', page)
        self.assertIn("data-login>", page)
        self.assertIn('id="theme-menu" role="menu"', page)
        for choice in ("system", "light", "dark"):
            self.assertIn(f'role="menuitemradio" aria-checked="false" data-theme-choice="{choice}"', page)
        self.assertNotIn('id="password-dialog"', page)
        self.assertNotIn('id="user-menu"', page)

    def test_users_get_their_menu(self):
        self.client.force_login(self.admin)
        page = self.client.get("/").content.decode()
        menu = page[page.index('id="user-menu"'):]
        menu = menu[: menu.index("</div>")]
        # 两步验证 is for superusers (accounts/js/totp.js).
        self.assertEqual(re.findall(r'role="menuitem[a-z]*"[^>]*>(?:<svg.*?</svg>)?([^<]+)<', menu), ["修改密码", "两步验证", "跟随系统", "浅色", "深色", "退出"])
        self.assertIn('aria-haspopup="menu" aria-expanded="false" aria-controls="user-menu"', page)
        self.assertIn('id="password-dialog"', page)
        self.assertIn(">老板<", page)
        self.assertNotIn('id="login-dialog"', page)

    def test_names_are_escaped(self):
        self.user.nickname = "<b>&"
        self.user.save()
        self.client.force_login(self.user)
        page = self.client.get("/").content.decode()
        self.assertIn("&lt;b&gt;&amp;", page)
        self.assertNotIn("<b>&", page)

    def test_manage(self):
        self.assertRedirects(self.client.get("/manage/"), "/?auth=login&next=/manage/", fetch_redirect_response=False)
        self.client.force_login(self.user)
        r = self.client.get("/manage/")
        self.assertEqual(r.status_code, 403)
        self.assertContains(r, "没有权限", status_code=403)
        self.client.force_login(self.admin)
        page = self.client.get("/manage/").content.decode()
        for section in SECTIONS:
            self.assertIn(f'href="#{section.key}" data-section="{section.key}"', page)
            self.assertIn(f'data-section="{section.key}" aria-label="{section.label}" hidden', page)
            if section.script:
                self.assertRegex(page, rf'<script src="/static/{re.escape(section.script)}\?v=[0-9a-f]{{12}}"></script>')
        self.assertIn('id="version-rows"', page)  # 版本与升级's own template
        self.assertIn(f'href="{admin_url()}"', page)  # the Django admin, at its random path
        self.assertLess(page.index("web/js/manage.js"), page.index("web/js/manage_version.js"))

    def test_components_only_while_debugging(self):
        self.assertEqual(self.client.get("/components/").status_code, 404)
        with override_settings(DEBUG=True):
            page = self.client.get("/components/").content.decode()
        self.assertIn('<div data-theme="light">', page)
        self.assertIn('<div data-theme="dark">', page)
        for part in ("btn-filled", "btn-tonal", 'class="btn"', "btn-text", "icon-button", "fab", "segmented", 'role="menu"',
                     'role="tablist"', "breadcrumbs", 'class="field', "<select", 'type="checkbox"', 'type="radio"', 'class="switch"',
                     "chip", "card", "list-item", "<dialog", "linear-progress", "data-demo-spinners", "badge", "data-tooltip",
                     "notice", "data-table", "divider", "icon-picker"):
            self.assertIn(part, page)
        ids = re.findall(r'\sid="([^"]+)"', page)
        self.assertEqual(len(ids), len(set(ids)))  # the two halves don't share ids

    def test_favicon(self):
        r = self.client.get("/favicon.ico")
        self.assertEqual(r.status_code, 302)
        self.assertRegex(r["Location"], r"^/static/web/favicon\.svg\?v=[0-9a-f]{12}$")


class ErrorPageTests(TestCase):
    def test_in_chinese_and_in_the_sites_style(self):
        r = self.client.get("/no-such-page/")
        self.assertEqual(r.status_code, 404)
        page = r.content.decode()
        self.assertIn("找不到这个页面", page)
        self.assertIn("web/css/m3.css", page)
        self.assertNotIn("Not Found", page)


class TokensTests(SimpleTestCase):
    """m3.css: generated from the seed, light and dark, the manual switch, the other token groups."""

    def test_the_color_tokens(self):
        css = (WEB / "static" / "web" / "css" / "m3.css").read_text(encoding="utf-8")
        self.assertIn("#1E6FD9", css.split("*/", 1)[0])  # the recipe at the top
        self.assertIn("primary (H, max(48, C)), secondary (H, 16), tertiary (H + 60, 24)", css)
        for selector in (':root, [data-theme="light"] {', ':root:not([data-theme="light"]) {', '[data-theme="dark"] {'):
            self.assertIn(selector, css)
        self.assertIn("@media (prefers-color-scheme: dark)", css)
        blocks = re.findall(r"\{([^{}]*--md-sys-color-primary:[^{}]*)\}", css)
        self.assertEqual(len(blocks), 3)
        light, dark_auto, dark = [dict(re.findall(r"(--md-sys-color-[a-z-]+): (#[0-9a-f]{6})", block)) for block in blocks]
        self.assertEqual(dark_auto, dark)  # the system's dark and the chosen one are the same
        self.assertEqual(set(light), set(dark))
        for role in ("primary", "on-primary", "surface-container-high", "outline-variant", "success", "warning-container"):
            self.assertIn(f"--md-sys-color-{role}", light)
        for group in ("--md-sys-typescale-", "--md-sys-shape-corner-", "--md-sys-elevation-level", "--md-sys-motion-",
                      "--md-sys-state-hover-state-layer-opacity"):
            self.assertIn(group, css)
        self.assertIn("prefers-reduced-motion", css)


class IconTests(SimpleTestCase):
    """apps/web/static/web/icons.svg (Material Symbols): every icon the templates, scripts and registries use is in it."""

    STATIC = WEB / "static" / "web"

    def ids(self):
        return set(re.findall(r'<symbol id="i-([a-z0-9_]+(?:-fill)?)"', (self.STATIC / "icons.svg").read_text(encoding="utf-8")))

    def test_the_tag_points_into_the_sprite(self):
        html = Template('{% load ui %}{% icon "home" "on" %}').render(Context())
        self.assertRegex(html, r'^<svg class="icon on" aria-hidden="true"><use href="/static/web/icons\.svg\?v=[0-9a-f]{12}#i-home"></use></svg>$')
        with self.assertRaises(TemplateSyntaxError):
            Template('{% load ui %}{% icon "x\\" onload=\\"" %}').render(Context())

    def test_every_icon_in_use_exists(self):
        ids = self.ids()
        used = {}
        apps = [Path(settings.BASE_DIR, *app.split(".")) for app in (*settings.HFILE_APPS, "apps.installer")]
        for folder in (app / "templates" for app in apps):
            for path in folder.rglob("*.html"):
                for name in re.findall(r'\{% icon "([a-z0-9_-]+)"', path.read_text(encoding="utf-8")):
                    used.setdefault(name, path.name)
        # In the scripts: icon('x'), iconHref('x') and THEME_ICONS-like maps of names; a choice such as
        # icon(a ? 'edit' : 'add') counts both.
        call = re.compile(r"\b(?:icon|iconHref|spinner)\(\s*([^,()]*)")
        for path in (path for app in apps for path in (app / "static").rglob("*.js") if "vendor" not in path.parts):
            text = path.read_text(encoding="utf-8")
            for first in call.findall(text):
                for found in re.findall(r"'([a-z0-9_]+)'", first):
                    used.setdefault(found, path.name)
            for block in re.findall(r"_ICONS = \{([^}]*)\}", text):
                for found in re.findall(r"'([a-z0-9_]+)'", block):
                    used.setdefault(found, path.name)
        for item in [*NAV, *SECTIONS]:  # a destination shows its "-fill" form when it is the current one
            used.setdefault(item.icon, "web/navigation.py")
            used.setdefault(f"{item.icon}-fill", "web/navigation.py")
        for name in storage_icons.ICONS:  # a storage's icon: in the navigation too, so with its "-fill" form
            used.setdefault(name, "storages/icons.py")
            used.setdefault(f"{name}-fill", "storages/icons.py")
        self.assertGreater(len(used), 40)
        self.assertEqual({name: where for name, where in used.items() if name not in ids}, {})

    def test_the_file_browser_has_what_it_needs(self):
        ids = self.ids()
        wanted = """folder folder_open draft description image movie music_note audio_file picture_as_pdf code article
            table_chart slideshow folder_zip upload upload_file drive_folder_upload download link share qr_code_2
            content_copy drive_file_move drive_file_rename_outline delete create_new_folder search sort grid_view
            view_list filter_list tune more_vert menu close check arrow_back arrow_forward chevron_left chevron_right
            expand_more expand_less home open_in_new settings storage cloud dns person group lock lock_open key
            visibility visibility_off login logout admin_panel_settings shield badge language policy receipt_long
            history upgrade backup restore light_mode dark_mode contrast refresh info warning error help schedule
            play_arrow pause skip_next skip_previous volume_up fullscreen zoom_in zoom_out rotate_right add remove
            edit save""".split()
        self.assertEqual([name for name in wanted if name not in ids], [])


class StylesheetTests(SimpleTestCase):
    """Every app's own stylesheets are well-formed: a brace lost in a merge puts every rule after it
    inside the block before it (an @media block, then they apply only on small screens)."""

    def test_braces_are_balanced(self):
        apps = [Path(settings.BASE_DIR, *app.split(".")) for app in (*settings.HFILE_APPS, "apps.installer")]
        sheets = [path for app in apps for path in (app / "static").rglob("*.css") if "vendor" not in path.parts]
        self.assertTrue(sheets)
        for path in sheets:
            with self.subTest(str(path.relative_to(settings.BASE_DIR))):
                text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8"), flags=re.S)
                text = re.sub(r"\"(?:[^\"\\\n]|\\.)*\"|'(?:[^'\\\n]|\\.)*'", '""', text)  # braces in strings
                depth = 0
                for char in text:
                    depth += {"{": 1, "}": -1}.get(char, 0)
                    self.assertGreaterEqual(depth, 0, "a } too many")
                self.assertEqual(depth, 0, "a { is never closed")

    def test_dialog_actions_stay_at_the_bottom(self):
        # M3's layout for every dialog: the content scrolls, the actions don't (they stick to the bottom of
        # .dialog-body, the part that scrolls, reaching over its bottom padding).
        css = (Path(settings.BASE_DIR) / "apps/web/static/web/css/app.css").read_text(encoding="utf-8")
        rule = re.search(r"\.dialog \.dialog-foot, \.dialog \.grants-foot \{([^}]*)\}", css)
        self.assertIsNotNone(rule)
        self.assertIn("position: sticky", rule.group(1))
        self.assertIn("bottom: calc(-1 * var(--dialog-pad))", rule.group(1))
        self.assertNotIn("margin-top", rule.group(1))  # short dialogs keep their spacing (.dialog-foot's 24px)
        self.assertRegex(css, r"\.dialog-body \{ padding: 16px var\(--dialog-pad\) var\(--dialog-pad\); overflow-y: auto; \}")

    def test_checkboxes_show_keyboard_focus(self):
        # A ticked box or a switch that is on is primary already, so the text fields' focus look (a primary
        # border) doesn't show on it: keyboard focus gets the focus ring, as buttons do.
        css = (Path(settings.BASE_DIR) / "apps/web/static/web/css/app.css").read_text(encoding="utf-8")
        rule = re.search(r'\ninput\[type="checkbox"\]:focus-visible \{([^}]*)\}', css)
        self.assertIsNotNone(rule)
        self.assertIn("outline: 3px solid var(--md-sys-color-secondary)", rule.group(1))

    def test_the_icon_picker_without_has(self):
        # Its chosen icon, focus ring and state layers come from the radio buttons' own states (input:checked +,
        # input:focus-visible +): a browser without :has() drops every rule that has it, and would show none.
        css = (Path(settings.BASE_DIR) / "apps/web/static/web/css/app.css").read_text(encoding="utf-8")
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        selectors = [selector for selector in re.findall(r"([^{}]+)\{", css) if "icon-choice" in selector]
        self.assertGreater(len(selectors), 3)
        for selector in selectors:
            self.assertNotIn(":has(", selector)

    def test_checkboxes_show_the_mixed_state(self):
        # 全选 over some of the rows only (indeterminate): drawn boxes (appearance: none) need a look of their
        # own, or they look unticked.
        css = (Path(settings.BASE_DIR) / "apps/web/static/web/css/app.css").read_text(encoding="utf-8")
        rule = re.search(r'\ninput\[type="checkbox"\]:not\(\.switch\):indeterminate \{([^}]*)\}', css)
        self.assertIsNotNone(rule)
        self.assertIn("background-color: var(--md-sys-color-primary)", rule.group(1))
