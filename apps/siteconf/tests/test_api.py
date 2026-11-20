"""The settings API: who may use it, the public part, and the checks of every page of settings."""

import os

from rest_framework.test import APITestCase

from apps.core import reserved, secrets
from apps.core.testing import TempMediaMixin

from ..models import DEFAULT_IMAGE_SUFFIXES, DEFAULT_SUFFIXES, PREVIEW_KINDS, SiteSettings
from ..serializers import GROUPS, form_fields
from .helpers import png, users

URL = "/api/v1/manage/settings/{}/"


class PermissionTests(APITestCase):
    def setUp(self):
        self.boss, self.staff, self.alice = users()

    def test_admins_only(self):
        for group in GROUPS:
            with self.subTest(group):
                url = URL.format(group)
                self.client.logout()
                self.assertEqual(self.client.get(url).status_code, 401)
                self.assertEqual(self.client.patch(url, {}).status_code, 401)
                self.client.force_login(self.alice)
                self.assertEqual(self.client.get(url).status_code, 403)
                self.assertEqual(self.client.patch(url, {}).status_code, 403)
                self.client.force_login(self.staff)
                self.assertEqual(self.client.get(url).status_code, 200)
        for url, method in (("/api/v1/manage/settings/site/favicon/", "put"), ("/api/v1/manage/settings/site/favicon/", "delete"),
                            ("/api/v1/manage/settings/access/test/", "post"), ("/api/v1/manage/diagnostics/", "get")):
            with self.subTest(url, method=method):
                self.client.logout()
                self.assertEqual(getattr(self.client, method)(url).status_code, 401)
                self.client.force_login(self.alice)
                self.assertEqual(getattr(self.client, method)(url).status_code, 403)

    def test_unknown_pages_and_methods(self):
        self.client.force_login(self.boss)
        self.assertEqual(self.client.get(URL.format("nope")).json(), {"detail": "没有这一页设置。"})
        self.assertEqual(self.client.put(URL.format("site"), {}).status_code, 405)
        self.assertEqual(self.client.post(URL.format("site"), {}).status_code, 405)
        self.assertEqual(self.client.put("/api/v1/manage/settings/site/icp/", {}).status_code, 404)

    def test_raw_html_and_js_only_by_superusers(self):
        # They come out as they are on the public pages (D33): a staff user who could write them could
        # run code as any superuser who opens the site.
        self.client.force_login(self.staff)
        for group, field in (("view", "custom_js"), ("view", "custom_css"), ("view", "announcement"), ("security", "guest_home_html")):
            with self.subTest(field):
                r = self.client.patch(URL.format(group), {field: "<b>x</b>"})
                self.assertEqual(r.status_code, 403)
                self.assertIn("只有超级管理员可以改", r.json()["detail"])
        # Sending them unchanged (the page sends every field) and other fields is fine.
        data = self.client.get(URL.format("view")).json()
        r = self.client.patch(URL.format("view"), {"custom_js": data["custom_js"], "layout": "center"})
        self.assertEqual((r.status_code, r.json()["layout"]), (200, "center"))
        self.client.force_login(self.boss)
        r = self.client.patch(URL.format("view"), {"custom_js": "console.log(1)"})
        self.assertEqual((r.status_code, r.json()["custom_js"]), (200, "console.log(1)"))

    def test_office_servers_only_by_superusers(self):
        # D49: the editor page runs OnlyOffice's api.js on the site's origin with the visitor's session,
        # the secret signs the callbacks that save files, and kkFileView gets the signed URLs of files.
        url = URL.format("view")
        self.client.force_login(self.staff)
        for field, value in (("onlyoffice_url", "https://office.example.com"), ("kkfileview_url", "https://kk.example.com"),
                             ("onlyoffice_secret", "staff-secret"), ("onlyoffice_secret", "")):
            with self.subTest(field, value=value):
                r = self.client.patch(url, {field: value, "layout": "card"})
                self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["detail"], "只有超级管理员可以改「OnlyOffice Secret」：能改的人，就能把自己的代码或内容"
                                             "放进别人打开的页面，或者拿到、改写网站上的文件。")
        r = self.client.patch(url, {"custom_js": "x", "onlyoffice_url": "https://office.example.com"})
        self.assertEqual(r.json()["detail"], "只有超级管理员可以改「自定义 JS、OnlyOffice 服务地址」：能改的人，就能把自己的代码或内容"
                                             "放进别人打开的页面，或者拿到、改写网站上的文件。")
        site = SiteSettings.objects.get()
        self.assertEqual((site.onlyoffice_url, site.kkfileview_url, site.onlyoffice_secret, site.layout), ("", "", "", "full"))
        # The other fields of 显示设置 are theirs, and so are the addresses sent unchanged.
        r = self.client.patch(url, {"layout": "card", "office_preview": False, "office_suffixes": "docx", "kkfileview_preview": False,
                                    "kkfileview_suffixes": "dwg", "kkfileview_open_mode": "new_tab", "onlyoffice_url": "",
                                    "kkfileview_url": ""})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["layout"], r.json()["office_suffixes"], r.json()["kkfileview_open_mode"]), ("card", "docx", "new_tab"))

        self.client.force_login(self.boss)
        r = self.client.patch(url, {"onlyoffice_url": "https://office.example.com/", "onlyoffice_secret": "boss-secret",
                                    "kkfileview_url": "https://kk.example.com"})
        self.assertEqual(r.status_code, 200, r.content)
        self.assertEqual((r.json()["onlyoffice_url"], r.json()["kkfileview_url"], r.json()["onlyoffice_secret_set"]),
                         ("https://office.example.com", "https://kk.example.com", True))

        # A staff admin can't remove the superuser's secret either, nor send it back as it is (it can't
        # be compared: it is stored encrypted); the addresses as they are saved are fine.
        self.client.force_login(self.staff)
        for value in ("", "boss-secret"):
            with self.subTest("secret again", value=value):
                self.assertEqual(self.client.patch(url, {"onlyoffice_secret": value}).status_code, 403)
        r = self.client.patch(url, {"onlyoffice_url": "https://office.example.com", "kkfileview_url": "https://KK.example.com/",
                                    "max_show_size": 300})
        self.assertEqual((r.status_code, r.json()["max_show_size"]), (200, 300))
        self.assertEqual(self.client.patch(url, {"onlyoffice_url": "https://office.example.net"}).status_code, 403)
        site = SiteSettings.objects.get()
        self.assertEqual((site.onlyoffice_url, secrets.decrypt(site.onlyoffice_secret)), ("https://office.example.com", "boss-secret"))

    def test_staff_change_the_rest_of_the_view_page(self):
        # D49 locks three fields of 显示设置, not the page: every other field is still theirs.
        fields = form_fields("view")
        self.assertEqual({name for name, spec in fields.items() if spec.superuser_only},
                         {"announcement", "custom_css", "custom_js", "onlyoffice_url", "onlyoffice_secret", "kkfileview_url"})
        url = URL.format("view")
        self.client.force_login(self.staff)
        current = self.client.get(url).json()
        for name, spec in fields.items():
            if spec.superuser_only:
                continue
            if spec.kind == "switch":
                value = not current[name]
            elif spec.kind == "select":
                value = next(choice for choice, _ in spec.choices if choice != current[name])
            elif spec.kind == "number":
                value = current[name] + 1 if current[name] < spec.max_value else current[name] - 1
            elif spec.kind == "suffixes":
                value = "abc"
            else:
                self.fail(f"what to try for {name} ({spec.kind})?")
            with self.subTest(name):
                r = self.client.patch(url, {name: value})
                self.assertEqual((r.status_code, r.json()[name]), (200, value))


class PublicTests(APITestCase):
    PUBLIC = {
        "site_name", "home_name", "home_logo", "home_logo_link", "home_logo_target", "icp", "max_file_uploads",
        "layout", "mobile_layout", "mobile_show_size", "root_show_storage", "default_save_password",
        "download_confirm", "file_click_mode", "mobile_file_click_mode", "max_show_size", "load_more_size",
        "default_sort_field", "default_sort_order", "show_document", "preview_suffixes", "text_preview_max_kb",
        "onlyoffice_url", "kkfileview_url", "kkfileview_open_mode", "show_path_link", "direct_link_prefix",
        "show_short_link", "short_link_expiries", "show_login",
    }

    def test_anybody_reads_the_public_part(self):
        data = self.client.get("/api/v1/site/").json()
        self.assertEqual(set(data), self.PUBLIC)
        self.assertEqual(data["site_name"], "HFile")
        self.assertEqual(data["preview_suffixes"]["image"], DEFAULT_IMAGE_SUFFIXES.split(","))
        self.assertEqual(data["short_link_expiries"], [{"value": 1, "unit": "d", "seconds": 86400}])
        self.assertEqual(SiteSettings.objects.count(), 1)

    def test_never_secrets_or_internal_values(self):
        site = SiteSettings.load()
        site.onlyoffice_secret = secrets.encrypt("jwt-secret-value")
        site.secure_login_entry = "hidden-door"
        site.ip_blocklist = "198.51.100.7"
        site.ua_blocklist = "*Bot*"
        site.referer_rules = "https://private.example"
        site.custom_js = "var key = 'k';"
        site.guest_home_html = "<p>guest</p>"
        site.save()
        body = self.client.get("/api/v1/site/").content.decode()
        for value in ("jwt-secret-value", "enc:", "hidden-door", "198.51.100.7", "*Bot*", "private.example", "var key", "<p>guest"):
            self.assertNotIn(value, body)
        self.assertFalse(self.client.get("/api/v1/site/").json()["show_login"])  # a secure entry hides it

    def test_preview_switches_and_lists(self):
        site = SiteSettings.load()
        site.video_preview = False
        site.audio_suffixes = "mp3,opus"
        site.save()
        lists = self.client.get("/api/v1/site/").json()["preview_suffixes"]
        self.assertEqual(lists["video"], [])
        self.assertEqual(lists["audio"], ["mp3", "opus"])
        self.assertEqual(set(lists), set(PREVIEW_KINDS))

    def test_it_changes_nothing(self):
        boss, _, _ = users()
        self.client.force_authenticate(boss)
        r = self.client.patch("/api/v1/site/", {"site_name": "x"})
        self.assertEqual((r.status_code, r.json()["detail"]), (405, "这个地址不支持 PATCH 请求。"))


class CheckTestCase(APITestCase):
    group = ""

    def setUp(self):
        self.boss, self.staff, self.alice = users()
        self.client.force_login(self.boss)

    def patch(self, data, group=None):
        return self.client.patch(URL.format(group or self.group), data)

    def assertSaved(self, data, expected, group=None):
        r = self.patch(data, group)
        self.assertEqual(r.status_code, 200, r.content.decode())
        body = r.json()
        for field, value in expected.items():
            self.assertEqual(body[field], value, field)
        return body

    def assertRefused(self, data, field, fragment="", group=None):
        r = self.patch(data, group)
        self.assertEqual(r.status_code, 400, r.content.decode())
        self.assertIn(field, r.json())
        if fragment:
            self.assertIn(fragment, " ".join(map(str, r.json()[field])))
        return r.json()


class SiteGroupTests(CheckTestCase):
    group = "site"

    def test_names(self):
        self.assertSaved({"site_name": "  我的\n 网盘  ", "home_name": " 根 "}, {"site_name": "我的 网盘", "home_name": "根"})
        self.assertEqual(SiteSettings.load().site_name, "我的 网盘")
        self.assertRefused({"site_name": "   "}, "site_name", "不能为空")
        self.assertRefused({"site_name": "名" * 31}, "site_name", "最多 30 个字")
        self.assertRefused({"site_name": "管\u200b理"}, "site_name", "不可见字符")
        self.assertRefused({"home_name": ""}, "home_name", "不能为空")
        self.assertRefused({"icp": "备" * 101}, "icp", "最多 100 个字")
        self.assertSaved({"icp": " 京ICP备12345678号 "}, {"icp": "京ICP备12345678号"})

    def test_every_problem_at_once(self):
        body = self.assertRefused(
            {"site_name": "", "favicon": "javascript:alert(1)", "home_logo_link": "//evil.example/", "max_file_uploads": 0}, "site_name"
        )
        self.assertEqual(set(body), {"site_name", "favicon", "home_logo_link", "max_file_uploads"})
        self.assertEqual(SiteSettings.load().site_name, "HFile")  # nothing saved

    def test_image_addresses(self):
        self.assertSaved({"favicon": " HTTPS://cdn.example.com/icon.png "}, {"favicon": "https://cdn.example.com/icon.png"})
        # Hosts in other scripts as punycode: the pages' CSP names this origin, and must stay ASCII.
        self.assertSaved({"favicon": "https://例子.中国/图标.png"}, {"favicon": "https://xn--fsqu00a.xn--fiqs8s/图标.png"})
        self.assertSaved({"home_logo": "https://Café.fr:8443/l.png"}, {"home_logo": "https://xn--caf-dma.fr:8443/l.png"})
        self.assertRefused({"favicon": "https://cdn.example.com:99999/i.png"}, "favicon")
        for bad in ("javascript:alert(1)", "data:image/png;base64,AAAA", "ftp://x.example/a.png", "/media/x.png",
                    "https://user:pw@cdn.example.com/a.png", "https://a b.example/", "site/0123.png"):
            with self.subTest(bad):
                self.assertRefused({"favicon": bad}, "favicon")
        self.assertSaved({"favicon": ""}, {"favicon": ""})

    def test_hosts_in_other_scripts_as_browsers_write_them(self):
        """API-13: Python's own idna codec (IDNA 2003) makes ß ss and ς σ: https://faß.de/ was saved as
        https://fass.de/, another domain, where browsers go to xn--fa-hia.de (UTS #46)."""
        self.assertSaved({"favicon": "https://faß.de/i.png"}, {"favicon": "https://xn--fa-hia.de/i.png"})
        self.assertSaved({"favicon": "https://ς.gr/i.png"}, {"favicon": "https://xn--3xa.gr/i.png"})
        # What IDNA 2008 refuses is refused (the joiner was dropped: another domain again), saying what to write.
        self.assertRefused({"favicon": f"https://a{chr(0x200D)}b.例子/i.png"}, "favicon", "xn--")
        self.assertRefused({"favicon": "https://☃.net/i.png"}, "favicon", "xn--")
        self.assertSaved({"favicon": "https://xn--n3h.net/i.png"}, {"favicon": "https://xn--n3h.net/i.png"})

    def test_a_name_that_isnt_a_setting_of_the_page(self):
        """API-05: it was ignored, and the answer, 200 with the settings as they were, looked as if it had been
        saved: ip_blacklist for ip_blocklist blocked nobody."""
        r = self.patch({"ip_blacklist": "203.0.113.7"}, group="access")
        self.assertEqual((r.status_code, r.json()), (400, {"ip_blacklist": ["这一页设置里没有这一项。"]}))
        # Answered with the other problems; a setting of another page isn't one of this page's.
        body = self.assertRefused({"site_name": "", "ip_blocklist": "203.0.113.7"}, "site_name")
        self.assertEqual(body["ip_blocklist"], ["这一页设置里没有这一项。"])
        self.assertEqual(SiteSettings.load().ip_blocklist, "")
        # What GET answers may be sent back as it is: its read-only names are known.
        page = self.client.get(URL.format("access")).json()
        self.assertIn("your_ip", page)
        self.assertSaved(page, {}, group="access")
        # 20 named at most, each name cut at 100 characters.
        r = self.patch({f"{n:02}" + "x" * 200: 1 for n in range(30)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(r.json()), 20)
        self.assertEqual({len(name) for name in r.json()}, {100})

    def test_an_upload_can_only_be_kept(self):
        site = SiteSettings.load()
        site.home_logo = "site/" + "a" * 32 + ".png"
        site.save()
        self.assertSaved({"home_logo": site.home_logo, "site_name": "x"}, {"home_logo": site.home_logo})
        self.assertRefused({"home_logo": "site/" + "b" * 32 + ".png"}, "home_logo", "只能保留现在上传的这张")

    def test_logo_link(self):
        for good in ("/", "/a/b?c=1#d", "https://blog.example.com/", ""):
            with self.subTest(good):
                self.assertSaved({"home_logo_link": good}, {"home_logo_link": good})
        for bad in ("//evil.example", "/\\evil.example", "\\\\evil", "javascript:alert(1)", "data:text/html,x", "evil.example",
                    "/a b", "/a\tb", "https://"):
            with self.subTest(bad):
                self.assertRefused({"home_logo_link": bad}, "home_logo_link")
        self.assertRefused({"home_logo_target": "_top"}, "home_logo_target")

    def test_upload_limits(self):
        self.assertRefused({"max_file_uploads": 21}, "max_file_uploads")
        self.assertRefused({"max_file_uploads": "x"}, "max_file_uploads")
        self.assertSaved({"max_file_uploads": "3"}, {"max_file_uploads": 3})

    def test_previews_of_the_images(self):
        body = self.client.get(URL.format("site")).json()
        self.assertRegex(body["favicon_preview"], r"^/static/web/favicon\.svg\?v=")
        self.assertSaved({"home_logo": "https://cdn.example.com/l.png"}, {"home_logo_preview": "https://cdn.example.com/l.png"})


class ViewGroupTests(CheckTestCase):
    group = "view"

    def test_choices_and_ranges(self):
        self.assertSaved({"layout": "card", "mobile_layout": "center", "file_click_mode": "click", "default_sort_field": "time",
                          "default_sort_order": "desc", "kkfileview_open_mode": "new_tab", "max_show_size": 200, "load_more_size": 20},
                         {"layout": "card", "default_sort_order": "desc", "max_show_size": 200})
        for field, value in (("layout", "wide"), ("file_click_mode", "hover"), ("default_sort_field", "type"),
                             ("max_show_size", 0), ("load_more_size", 100_001), ("text_preview_max_kb", 0), ("text_preview_max_kb", 10_241)):
            with self.subTest(field, value=value):
                self.assertRefused({field: value}, field)

    def test_suffix_lists(self):
        body = self.assertSaved({"image_suffixes": " .PNG, jpg，Webp、 *.avif;jpg  ", "video_suffixes": ""},
                                {"image_suffixes": "png, jpg, webp, avif"})
        self.assertEqual(body["video_suffixes"], DEFAULT_SUFFIXES["video"].replace(",", ", "))  # empty: the default, shown
        site = SiteSettings.load()
        self.assertEqual((site.image_suffixes, site.video_suffixes), ("png,jpg,webp,avif", ""))
        # The default list in another order is the default: saved as "", so a newer version's default applies.
        self.assertSaved({"text_suffixes": ",".join(reversed(DEFAULT_SUFFIXES["text"].split(",")))}, {})
        self.assertEqual(SiteSettings.load().text_suffixes, "")
        for bad in ("tar.gz", "a/b", "<b>", "x" * 21):
            with self.subTest(bad):
                self.assertRefused({"audio_suffixes": f"mp3,{bad}"}, "audio_suffixes", "不是文件后缀")

    def test_a_real_off_per_kind(self):
        self.assertSaved({"office_preview": False}, {"office_preview": False})
        self.assertEqual(SiteSettings.load().preview_suffixes()["office"], [])

    def test_service_addresses(self):
        self.assertSaved({"onlyoffice_url": "https://office.example.com/", "kkfileview_url": "http://10.0.0.5:8012//"},
                         {"onlyoffice_url": "https://office.example.com", "kkfileview_url": "http://10.0.0.5:8012"})
        self.assertRefused({"onlyoffice_url": "office.example.com"}, "onlyoffice_url", "http:// 或 https://")
        self.assertRefused({"kkfileview_url": "javascript:alert(1)"}, "kkfileview_url")

    def test_the_onlyoffice_secret_is_write_only(self):
        body = self.client.get(URL.format("view")).json()
        self.assertNotIn("onlyoffice_secret", body)
        self.assertFalse(body["onlyoffice_secret_set"])
        body = self.assertSaved({"onlyoffice_secret": "jwt-秘密"}, {"onlyoffice_secret_set": True})
        self.assertNotIn("onlyoffice_secret", body)
        stored = SiteSettings.load().onlyoffice_secret
        self.assertTrue(stored.startswith("enc:"))
        self.assertNotIn("jwt", stored)
        self.assertEqual(secrets.decrypt(stored), "jwt-秘密")
        self.assertSaved({"layout": "center"}, {"onlyoffice_secret_set": True})  # left out: kept
        self.assertEqual(secrets.decrypt(SiteSettings.load().onlyoffice_secret), "jwt-秘密")
        self.assertNotIn("jwt", self.client.get(URL.format("view")).content.decode())
        self.assertRefused({"onlyoffice_secret": "a\nb"}, "onlyoffice_secret")
        self.assertSaved({"onlyoffice_secret": ""}, {"onlyoffice_secret_set": False})  # "": removed
        self.assertEqual(SiteSettings.load().onlyoffice_secret, "")

    def test_texts_of_the_public_pages(self):
        self.assertSaved({"announcement": "**公告**  \r\n第二行\r\n"}, {"announcement": "**公告**  \n第二行"})
        self.assertRefused({"announcement": "a\x01b"}, "announcement", "控制字符")
        self.assertRefused({"announcement": "a\x00b"}, "announcement")  # DRF's own check
        self.assertRefused({"custom_css": "a" * 100_001}, "custom_css", "最多 100000 个字符")
        self.assertSaved({"custom_js": "\n\n  "}, {"custom_js": ""})


class LinkGroupTests(CheckTestCase):
    group = "link"

    def test_the_prefix(self):
        self.assertSaved({"direct_link_prefix": " /dl/ "}, {"direct_link_prefix": "dl"})
        self.assertSaved({"direct_link_prefix": "directlink"}, {"direct_link_prefix": "directlink"})  # the default is one of the site's names
        for bad, fragment in (("api", "网站自己要用"), ("Static", "网站自己要用"), ("admin", "网站自己要用"), ("s", "网站自己要用"),
                              ("site", "网站自己要用"), ("a b", "只能用"), ("目录", "只能用"), ("x" * 33, "只能用"), ("", "只能用")):
            with self.subTest(bad):
                self.assertRefused({"direct_link_prefix": bad}, "direct_link_prefix", fragment)
        from django.conf import settings

        self.assertRefused({"direct_link_prefix": settings.ADMIN_PATH.upper()}, "direct_link_prefix", "后台的地址")

    def test_the_prefix_against_storage_keys(self):
        def storage_keys(name):
            return "/photos/ 是存储源「照片」的别名。" if name.lower() == "photos" else ""

        reserved.register_check(storage_keys)
        self.addCleanup(reserved._checks.remove, storage_keys)
        self.assertRefused({"direct_link_prefix": "Photos"}, "direct_link_prefix", "存储源「照片」")
        # And what siteconf registers: a storage key can't be the prefix.
        self.assertSaved({"direct_link_prefix": "dl"}, {})
        self.assertIn("直链的地址前缀", reserved.problem("DL", own=storage_keys))
        self.assertEqual(reserved.problem("downloads", own=storage_keys), "")

    def test_lifetimes(self):
        body = self.assertSaved(
            {"short_link_expiries": [{"value": 30, "unit": "m"}, {"value": 1, "unit": "M", "seconds": 2592000}, {"value": 9, "unit": "forever"}]},
            {},
        )
        self.assertEqual(body["short_link_expiries"], [
            {"value": 30, "unit": "m", "seconds": 1800},
            {"value": 1, "unit": "M", "seconds": 2592000},
            {"value": 1, "unit": "forever", "seconds": -1},
        ])
        cases = [
            ([], "至少要有一个"),
            ([{"value": 1, "unit": "forever"}, {"value": 2, "unit": "forever"}], "「永久」只能有一个"),
            ([{"value": 1, "unit": "d"}, {"value": 24, "unit": "h"}], "一样长"),
            ([{"value": 0, "unit": "d"}], ""),
            ([{"value": -1, "unit": "d"}], ""),
            ([{"value": 1, "unit": "d", "seconds": 60}], "seconds 应该是 86400"),
            ([{"value": 1, "unit": "decade"}], ""),
            ([{"value": 101, "unit": "y"}], "最长 100 年"),
            ([{"value": n, "unit": "d"} for n in range(1, 22)], "最多 20 个"),
            ("1d", ""),
        ]
        for value, fragment in cases:
            with self.subTest(value):
                body = self.assertRefused({"short_link_expiries": value}, "short_link_expiries")
                if fragment:
                    self.assertIn(fragment, str(body))

    def test_referer_rules_and_limits(self):
        self.assertSaved({"referer_type": "allowlist", "referer_rules": " https://*.example.com \n\n*://a.example\nhttps://*.example.com\r\nhttp://b.example/**"},
                         {"referer_rules": "https://*.example.com\n*://a.example\nhttp://b.example/**"})
        body = self.assertRefused({"referer_rules": "https://ok.example\nexample.com\nhttps://a b"}, "referer_rules")
        self.assertEqual(len(body["referer_rules"]), 2)
        self.assertIn("第 2 行「example.com」", body["referer_rules"][0])
        self.assertIn("第 3 行", body["referer_rules"][1])
        self.assertRefused({"referer_type": "everything"}, "referer_type")
        self.assertSaved({"link_limit_seconds": 0, "link_limit_downloads": 0}, {"link_limit_seconds": 0})
        self.assertRefused({"link_limit_seconds": -1}, "link_limit_seconds")
        self.assertRefused({"link_limit_seconds": 30 * 86400 + 1}, "link_limit_seconds")


class ProxyGroupTests(CheckTestCase):
    group = "proxy"

    def test_the_limits_per_ip(self):
        body = self.client.get(URL.format("proxy")).json()
        self.assertEqual((body["proxy_ip_connections"], body["proxy_ip_speed"]), (0, 0))  # no limits until set
        self.assertSaved({"proxy_ip_connections": 8, "proxy_ip_speed": 2048}, {"proxy_ip_connections": 8, "proxy_ip_speed": 2048})
        site = SiteSettings.load()
        self.assertEqual((site.proxy_ip_connections, site.proxy_ip_speed), (8, 2048))
        for field, bad in (("proxy_ip_connections", -1), ("proxy_ip_connections", 1001), ("proxy_ip_speed", -1),
                           ("proxy_ip_speed", 10_000_001), ("proxy_ip_speed", "fast")):
            with self.subTest(field, bad=bad):
                self.assertRefused({field: bad}, field)


class SecurityGroupTests(CheckTestCase):
    group = "security"

    def test_the_secure_entry(self):
        self.assertSaved({"secure_login_entry": " /my-Door_2/ "}, {"secure_login_entry": "my-Door_2"})
        self.assertSaved({"secure_login_entry": ""}, {"secure_login_entry": ""})
        for bad in ("a b", "门", "a/b", "x" * 33, "a?b"):
            with self.subTest(bad):
                self.assertRefused({"secure_login_entry": bad}, "secure_login_entry", "只能用英文字母")

    def test_the_custom_guest_page_needs_its_html(self):
        self.assertRefused({"guest_home": "custom"}, "guest_home_html", "要写这个页面的 HTML")
        self.assertSaved({"guest_home": "custom", "guest_home_html": "<h1>欢迎</h1>"}, {"guest_home": "custom"})
        self.assertRefused({"guest_home_html": "  "}, "guest_home_html")  # still custom
        self.assertSaved({"guest_home": "login"}, {"guest_home": "login"})
        self.assertRefused({"guest_home": "elsewhere"}, "guest_home")

    def test_switches(self):
        self.assertSaved({"login_log": False, "login_captcha": True, "show_login": False, "sso_auto_register": True},
                         {"login_log": False, "login_captcha": True, "show_login": False, "sso_auto_register": True})


class AccessGroupTests(CheckTestCase):
    group = "access"

    def test_ip_rules_are_cleaned(self):
        body = self.assertSaved(
            {"ip_blocklist": " 203.0.113.7 \n\n# 爬虫\n192.168.1.9/24\n2001:DB8::/32\n::ffff:198.51.100.1\n203.0.113.7\n10.0.0.1/32"},
            {},
        )
        self.assertEqual(body["ip_blocklist"],
                         "203.0.113.7\n# 爬虫\n192.168.1.0/24\n2001:db8::/32\n198.51.100.1\n10.0.0.1")

    def test_every_bad_line_is_named(self):
        body = self.assertRefused({"ip_blocklist": "203.0.113.7\nlocalhost\n10.0.0.0/33\nfe80::1%eth0\n1.2.3"}, "ip_blocklist")
        self.assertEqual([message[:6] for message in body["ip_blocklist"]], ["第 2 行「", "第 3 行「", "第 4 行「", "第 5 行「"])
        self.assertRefused({"ua_blocklist": "ok\nbad\x01rule"}, "ua_blocklist", "第 2 行不能有控制字符")

    def test_only_the_first_bad_lines_are_named(self):
        """SEC-06: every bad line was named, and the page checks the whole list as the admin types: a long list
        was answered with up to 11 MB. Now the first 20, and how many more there are."""
        cases = (("ip_blocklist", "access", "# ok", "bad{}"), ("ua_blocklist", "access", "# ok", "bad{}" + chr(1)),
                 ("referer_rules", "link", "https://ok.example", "bad{}"))
        for field, group, good, bad in cases:
            with self.subTest(field):
                text = "\n".join([good] + [bad.format(n) for n in range(25)])
                messages = self.assertRefused({field: text}, field, group=group)[field]
                self.assertEqual(len(messages), 21)
                self.assertTrue(messages[19].startswith("第 21 行"), messages[19])
                self.assertEqual(messages[20], "还有 5 行也不对。")
        r = self.client.post("/api/v1/manage/settings/access/test/", {"ip_blocklist": "\n".join(["x"] * 2000)})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(len(r.json()["ip_blocklist"]), 21)
        self.assertEqual(r.json()["ip_blocklist"][-1], "还有 1980 行也不对。")
        # A list with too many rules says so after them.
        text = "\n".join([f"10.0.{n // 256}.{n % 256}" for n in range(2001)] + ["x"] * 21)
        messages = self.assertRefused({"ip_blocklist": text}, "ip_blocklist")["ip_blocklist"]
        self.assertEqual(messages[20:], ["还有 1 行也不对。", "最多 2000 条，现在有 2001 条。"])

    def test_you_cant_block_yourself(self):
        r = self.client.patch(URL.format("access"), {"ip_blocklist": "127.0.0.0/8"}, REMOTE_ADDR="127.0.0.1")
        self.assertEqual(r.status_code, 400)
        self.assertIn("会挡住你自己", r.json()["ip_blocklist"][0])
        r = self.client.patch(URL.format("access"), {"ua_blocklist": "*Firefox*"}, HTTP_USER_AGENT="Mozilla/5.0 Firefox/150")
        self.assertIn("会挡住你现在用的浏览器", r.json()["ua_blocklist"][0])
        self.assertEqual(SiteSettings.load().ip_blocklist, "")
        self.assertSaved({"ip_blocklist": "198.51.100.0/24", "ua_blocklist": "*Thunder*"}, {"ua_blocklist": "*Thunder*"})

    def test_it_says_who_you_are(self):
        body = self.client.get(URL.format("access"), HTTP_USER_AGENT="TestAgent/1").json()
        self.assertEqual((body["your_ip"], body["your_user_agent"]), ("127.0.0.1", "TestAgent/1"))

    def test_the_live_test(self):
        r = self.client.post("/api/v1/manage/settings/access/test/",
                             {"ip_blocklist": "10.0.0.0/8\n# x", "ip": "10.1.2.3", "ua_blocklist": "*Thunder*", "user_agent": "Thunder 11"})
        self.assertEqual(r.json(), {"ip": {"value": "10.1.2.3", "rule": "10.0.0.0/8"}, "user_agent": {"value": "Thunder 11", "rule": "*Thunder*"}})
        r = self.client.post("/api/v1/manage/settings/access/test/", {"ip": "192.0.2.1"})
        self.assertEqual(r.json()["ip"], {"value": "192.0.2.1", "rule": None})  # the saved (empty) lists
        r = self.client.post("/api/v1/manage/settings/access/test/", {"ip": "not-an-ip"})
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/v1/manage/settings/access/test/", {"ip_blocklist": "nonsense", "ip": "192.0.2.1"})
        self.assertIn("第 1 行", r.json()["ip_blocklist"][0])


class UploadTests(TempMediaMixin, APITestCase):
    URL = "/api/v1/manage/settings/site/{}/"

    def setUp(self):
        self.boss, self.staff, _ = users()
        self.client.force_login(self.staff)  # admins, not only superusers

    def upload(self, field, file):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.put(self.URL.format(field), {"file": file}, format="multipart")

    def test_upload_replace_and_reset(self):
        r = self.upload("favicon", png(300, 150))
        self.assertEqual(r.status_code, 200, r.content)
        first = r.json()["favicon"]
        self.assertRegex(first, r"^site/[0-9a-f]{32}\.png$")
        self.assertEqual(r.json()["favicon_preview"], f"/media/{first}")
        path = os.path.join(self.__class__._media_dir, first)
        self.assertTrue(os.path.exists(path))
        served = self.client.get(f"/media/{first}")
        self.assertEqual((served.status_code, served["Cache-Control"]), (200, "private, max-age=31536000, immutable"))
        # Read to the end, as the server does: the test client then closes the file (Windows can't delete one that is
        # still open). Its close() would also close the database connection, and the test's transaction with it (MySQL).
        self.assertEqual(b"".join(served.streaming_content)[:4], b"\x89PNG")
        # A new one replaces it; the old file goes once the change is committed.
        second = self.upload("favicon", png(64, 64)).json()["favicon"]
        self.assertNotEqual(first, second)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(SiteSettings.load().favicon, second)
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.delete(self.URL.format("favicon"))
        self.assertEqual((r.json()["favicon"], SiteSettings.load().favicon), ("", ""))
        self.assertFalse(os.path.exists(os.path.join(self.__class__._media_dir, second)))

    def test_only_upload_names_are_ever_deleted(self):
        from ..serializers import forget_upload

        victim = os.path.join(self.__class__._media_dir, "keep.txt")
        with open(victim, "w") as file:
            file.write("x")
        self.addCleanup(os.remove, victim)
        for name in ("site/../keep.txt", "keep.txt", "site/" + "a" * 32 + ".png/../../keep.txt", "/etc/passwd", ""):
            with self.captureOnCommitCallbacks(execute=True):
                forget_upload(name)
        self.assertTrue(os.path.exists(victim))

    def test_switching_to_an_address_deletes_the_upload(self):
        name = self.upload("home_logo", png(400, 100)).json()["home_logo"]
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.patch("/api/v1/manage/settings/site/", {"home_logo": "https://cdn.example.com/logo.png"})
        self.assertEqual(r.json()["home_logo"], "https://cdn.example.com/logo.png")
        self.assertFalse(os.path.exists(os.path.join(self.__class__._media_dir, name)))

    def test_what_isnt_a_picture_is_refused(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        cases = [
            (SimpleUploadedFile("x.png", b"<svg onload=alert(1)>", content_type="image/png"), "格式"),
            (SimpleUploadedFile("x.png", b"", content_type="image/png"), ""),
            (png(2000, 16), "接近正方形"),
        ]
        for file, fragment in cases:
            with self.subTest(fragment):
                r = self.upload("favicon", file)
                self.assertEqual(r.status_code, 400)
                self.assertIn(fragment, str(r.json()["file"]))
        self.assertEqual(SiteSettings.load().favicon, "")
        self.assertEqual([files for _, _, files in os.walk(self.__class__._media_dir) if files], [])  # nothing left behind
        r = self.client.put(self.URL.format("favicon"), {"file": [png(), png()]}, format="multipart")
        self.assertEqual(r.json()["file"], ["只能上传一张。"])
        self.assertEqual(self.client.put(self.URL.format("favicon"), {}, format="multipart").status_code, 400)


class FormFieldTests(APITestCase):
    def test_every_writable_field_has_a_label_and_a_kind(self):
        for group in GROUPS:
            for name, spec in form_fields(group).items():
                with self.subTest(group=group, field=name):
                    self.assertTrue(spec.label and spec.label != name, spec.label)
                    self.assertIn(spec.kind, {"switch", "select", "number", "text", "textarea", "code", "image", "secret",
                                              "suffixes", "expiries"})
        fields = form_fields("view")
        self.assertEqual(fields["image_suffixes"].default, "gif, jpg, jpeg, png, bmp, webp, ico")
        self.assertTrue(fields["custom_js"].superuser_only)
        self.assertEqual(fields["layout"].choices, (("full", "全屏"), ("center", "居中"), ("card", "卡片")))
        self.assertEqual((fields["max_show_size"].min_value, fields["max_show_size"].max_value), (1, 100_000))
        self.assertEqual(form_fields("site")["favicon"].kind, "image")
        self.assertEqual(form_fields("view")["onlyoffice_secret"].kind, "secret")
        self.assertEqual(form_fields("link")["short_link_expiries"].kind, "expiries")


class LockTests(APITestCase):
    def test_two_saves_dont_undo_each_other(self):
        boss, _, _ = users()
        self.client.force_login(boss)
        self.client.patch(URL.format("site"), {"site_name": "甲"})
        self.client.patch(URL.format("view"), {"layout": "card"})
        site = SiteSettings.load()
        self.assertEqual((site.site_name, site.layout), ("甲", "card"))


class UrlLengthTests(APITestCase):
    def test_the_length_as_saved(self):
        """API-11: a host in another script is longer as punycode, and the column holds URL_MAX."""
        from rest_framework.exceptions import ValidationError

        from ..models import URL_MAX
        from ..serializers import _http_url

        typed = "https://例子.中国/" + "a" * (URL_MAX - 14)
        self.assertEqual(len(typed), URL_MAX)
        with self.assertRaises(ValidationError):
            _http_url(typed, "网站图标")
        self.assertEqual(len(_http_url("https://example.cn/" + "a" * (URL_MAX - 19), "网站图标")), URL_MAX)
