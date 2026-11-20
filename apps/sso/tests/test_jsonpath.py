"""The JSON path subset (apps/sso/jsonpath.py) and the checks of provider settings (apps/sso/validation.py)."""

import base64

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase

from apps.sso import flow, jsonpath, validation


class PathTests(SimpleTestCase):
    def test_the_forms(self):
        for path, steps in (
            ("sub", ("sub",)), ("a.b", ("a", "b")), ("$.a.b", ("a", "b")), ("['a']", ("a",)),
            ("$['a']['b']", ("a", "b")), ('$["a"]', ("a",)), ("[0]", (0,)), ("emails[0].value", ("emails", 0, "value")),
            ("$.user['display-name']", ("user", "display-name")), ("['a.b']", ("a.b",)), ("['it\\'s']", ("it's",)),
            ("data.$ref", ("data", "$ref")), ("名字", ("名字",)), ("a[10].b", ("a", 10, "b")),
        ):
            with self.subTest(path=path):
                self.assertEqual(jsonpath.parse(path), steps)

    def test_what_isnt_in_the_subset(self):
        for path in ("", "$", ".a", "a..b", "a.", "a[", "a[x]", "a[-1]", "a[*]", "$..a", "a['b", "a['b']c", "a b",
                     "a[?(@.x)]", "$a", "'a'", "a[100001]", "x" * 201, ".".join("a" * 21)):
            with self.subTest(path=path):
                with self.assertRaises(jsonpath.PathError):
                    jsonpath.parse(path)

    def test_values(self):
        data = {"sub": " u-1 ", "id": 42, "big": 10**30, "flag": True, "ratio": 1.5, "none": None, "obj": {"x": 1},
                "list": [1], "blank": "  ", "emails": [{"value": "a@b.c"}], "n": {"s": "深"}}
        for path, expected in (
            ("sub", ("u-1", "")), ("id", ("42", "")), ("big", (str(10**30), "")), ("emails[0].value", ("a@b.c", "")),
            ("n.s", ("深", "")),
            ("missing", (None, "没有这个字段")), ("emails[3]", (None, "没有这个字段")), ("sub.x", (None, "没有这个字段")),
            ("flag", (None, "是 true 或 false，不是文字或整数")), ("ratio", (None, "是小数，不是文字或整数")),
            ("none", (None, "是 null")), ("obj", (None, "是一个对象，不是文字或整数")),
            ("list", (None, "是一个列表，不是文字或整数")), ("blank", (None, "是空的")),
        ):
            with self.subTest(path=path):
                self.assertEqual(jsonpath.text_at(data, jsonpath.parse(path)), expected)
        self.assertEqual(jsonpath.text_at([{"id": 7}], jsonpath.parse("[0].id")), ("7", ""))


class AddressTests(SimpleTestCase):
    def test_with_query(self):
        for path, params, result in (
            ("/manage/#users", {"x": "1"}, "/manage/?x=1#users"), ("/a?b=1&c=2", {"b": "3"}, "/a?c=2&b=3"),
            ("/", {"next": "/manage/"}, "/?next=/manage/"), ("//evil.example/x", {"a": "1"}, "/?a=1"),
            ("https://evil.example/", {}, "/"), ("/plain", {}, "/plain"),
        ):
            self.assertEqual(flow.with_query(path, **params), result)

    def test_safe_next(self):
        for value, result in (("/files/a?b=1#c", "/files/a?b=1#c"), ("//evil.example", "/"), ("/\\evil", "/"),
                              ("https://evil.example/", "/"), ("javascript:alert(1)", "/"), ("/a\nb", "/"), (None, "/")):
            self.assertEqual(flow.safe_next(value), result)


class ValidationTests(SimpleTestCase):
    def assertRefused(self, clean, value, part=""):
        with self.assertRaises(ValidationError) as caught:
            clean(value)
        if part:
            self.assertIn(part, caught.exception.messages[0])

    def test_codes(self):
        self.assertEqual(validation.clean_code(" GitHub "), "github")
        for bad in ("", "a b", "a/b", "a.b", "x" * 33, "中文"):
            self.assertRefused(validation.clean_code, bad, "小写字母")

    def test_urls(self):
        self.assertEqual(validation.clean_url("HTTPS://Example.COM:8443/o/Auth?x=1"), "https://example.com:8443/o/Auth?x=1")
        self.assertEqual(validation.clean_url("https://例子.中国/auth"), "https://xn--fsqu00a.xn--fiqs8s/auth")
        self.assertEqual(validation.clean_url("http://[::1]:8080/token"), "http://[::1]:8080/token")
        self.assertEqual(validation.clean_url("https://x.example//a?b=1"), "https://x.example//a?b=1")
        for bad, part in (("", "不能为空"), ("ftp://x/y", "http://"), ("javascript:alert(1)", "http://"), ("https://", "http://"),
                          ("https://user:pw@x.com/", "用户名和密码"), ("https://x.com/#a", "#"), ("https://x.com/a b", "空格"),
                          ("https://x.com:99999/", "写得不对"), ("https://x.com/​", "不可见"), ("https://x.com/路径", "非英文"),
                          ("https://" + "a" * 1000 + ".com", "最多"),
                          # API-11: 1000 characters typed, 1017 as punycode
                          ("https://例子.中国/" + "a" * 986, "最多")):
            with self.subTest(bad=bad):
                self.assertRefused(validation.clean_url, bad, part)

    def test_hosts_in_other_scripts_as_browsers_write_them(self):
        """API-13: as the site settings' addresses, faß.de was fass.de (IDNA 2003), another domain."""
        self.assertEqual(validation.clean_url("https://faß.de/auth"), "https://xn--fa-hia.de/auth")
        self.assertRefused(validation.clean_url, "https://☃.net/auth", "xn--")
        self.assertEqual(validation.clean_url("https://xn--n3h.net/auth"), "https://xn--n3h.net/auth")

    def test_icons(self):
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 20).decode()
        svg = "data:image/svg+xml;base64," + base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>').decode()
        for good in ("", png, svg, "data:image/svg+xml,%3Csvg%20xmlns%3D%22x%22%3E%3C%2Fsvg%3E", "https://cdn.example.com/a.png"):
            self.assertEqual(validation.clean_icon(good), good)
        self.assertEqual(validation.clean_icon("HTTPS://例子.中国/a.png"), "https://xn--fsqu00a.xn--fiqs8s/a.png")
        for bad, part in (
            ("<svg onload=alert(1)>", "http(s)"), ("data:text/html;base64,PHNjcmlwdD4=", "http(s)"),
            ("data:image/png;base64,PHN2Zz4=", "不是 PNG"), ("data:image/png;base64,***", "http(s)"),
            ("data:image/svg+xml;base64," + base64.b64encode(b"<html></html>").decode(), "不是 SVG"),
            ("data:image/svg+xml,<svg></svg>", "http(s)"), ("javascript:alert(1)", "http://"),
            ("data:image/png;base64," + "A" * 70000, "64 KB"),
        ):
            with self.subTest(bad=bad[:40]):
                self.assertRefused(validation.clean_icon, bad, part)

    def test_scopes_names_and_ids(self):
        self.assertEqual(validation.clean_scope("  openid   profile\nemail "), "openid profile email")
        self.assertEqual(validation.clean_scope(""), "")
        self.assertRefused(validation.clean_scope, 'openid "x"', "scope")
        self.assertEqual(validation.clean_name("  Git  Hub "), "Git Hub")
        self.assertRefused(validation.clean_name, "  ", "不能为空")
        self.assertRefused(validation.clean_name, "a‮b", "不可见")
        self.assertEqual(validation.clean_client_id(" abc "), "abc")
        self.assertRefused(validation.clean_client_id, "a b", "空格")
        self.assertEqual(validation.clean_secret(" s3cret\n"), "s3cret")
        self.assertEqual(validation.clean_external_id(" A-1 "), "A-1")
        for bad in ("", "  ", "a\x00b", "x" * 256, "\ud800", "a\u202eb", "\u200b", 5, None):
            self.assertRefused(validation.clean_external_id, bad)
        self.assertEqual(validation.clean_path(" $.data.id "), "$.data.id")
        self.assertRefused(validation.clean_path, "a..b")
