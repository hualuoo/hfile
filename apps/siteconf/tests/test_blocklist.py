"""访问控制: the rules (apps/siteconf/blocklist.py) and the middleware that answers 403."""

import time
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from .. import blocklist
from ..middleware import AccessControlMiddleware
from ..models import SiteSettings
from .helpers import users


class RuleTests(SimpleTestCase):
    def test_ip_rules(self):
        rules = blocklist.parse("203.0.113.7\n# a comment\n10.0.0.0/8\n192.168.1.9/24\n2001:db8::/32\n::ffff:198.51.100.1\nnonsense")
        for ip, rule in (("203.0.113.7", "203.0.113.7"), ("10.255.0.1", "10.0.0.0/8"), ("192.168.1.200", "192.168.1.9/24"),
                         ("2001:db8:ffff::1", "2001:db8::/32"), ("198.51.100.1", "::ffff:198.51.100.1")):
            with self.subTest(ip):
                self.assertEqual(rules.ip_rule(ip), rule)
        for ip in ("203.0.113.8", "11.0.0.1", "192.168.2.1", "2001:db9::1", "::ffff:10.0.0.1x", "", None, "unknown"):
            with self.subTest(ip):
                self.assertIsNone(rules.ip_rule(ip))
        self.assertEqual(rules.problems, [("ip", 7, "不是 IP 地址，也不是 CIDR 写法的 IP 段（比如 192.168.0.0/24、2001:db8::/32）")])

    def test_every_address_and_everything(self):
        rules = blocklist.parse("0.0.0.0/0\n::/0")
        self.assertEqual(rules.ip_rule("8.8.8.8"), "0.0.0.0/0")
        self.assertEqual(rules.ip_rule("2001:4860::8888"), "::/0")

    def test_user_agent_rules(self):
        rules = blocklist.parse(ua_text="*Thunder*\nMozilla/4.0*\ncurl/8.0\n*a*b*c*")
        for ua, rule in (("Mozilla/5.0 Thunder/11", "*Thunder*"), ("Thunder", "*Thunder*"), ("Mozilla/4.0 (compatible)", "Mozilla/4.0*"),
                         ("curl/8.0", "curl/8.0"), ("xaybzc", "*a*b*c*"), ("abc", "*a*b*c*")):
            with self.subTest(ua):
                self.assertEqual(rules.ua_rule(ua), rule)
        for ua in ("Mozilla/5.0 thunder/11", "curl/8.0.1", "acb", "", None, "Mozilla/5.0"):
            with self.subTest(ua):
                self.assertIsNone(rules.ua_rule(ua))  # whole UA, case kept
        self.assertEqual(blocklist.parse(ua_text="*").ua_rule("anything"), "*")
        self.assertIsNone(blocklist.parse(ua_text="*").ua_rule(""))  # no User-Agent never matches

    def test_a_very_long_user_agent(self):
        rules = blocklist.parse(ua_text="curl/*")
        self.assertIsNotNone(rules.ua_rule("x" * (blocklist.MAX_USER_AGENT + 1)))  # not matched rule by rule
        self.assertIsNone(rules.ua_rule("x" * blocklist.MAX_USER_AGENT))
        self.assertIsNone(blocklist.parse().ua_rule("x" * 100_000))  # no UA rules, nothing to block

    def test_user_agents_are_read_as_utf8(self):
        # WSGI gives header values as latin-1; a rule in Chinese still matches what the browser sent.
        meta = {"HTTP_USER_AGENT": "某浏览器/1.0".encode("utf-8").decode("latin-1")}
        self.assertEqual(blocklist.user_agent(meta), "某浏览器/1.0")
        self.assertEqual(blocklist.parse(ua_text="某浏览器*").ua_rule(blocklist.user_agent(meta)), "某浏览器*")
        self.assertEqual(blocklist.user_agent({"HTTP_USER_AGENT": "caf\xe9"}), "caf\xe9")  # not UTF-8: as it came
        self.assertEqual(blocklist.user_agent({}), "")

    def test_matching_stays_fast(self):
        # Many stars against a long User-Agent: linear, no regex backtracking.
        rules = blocklist.parse(ua_text="\n".join("*a*a*a*a*a*a*a*a*a*a*b" + str(i) for i in range(500)))
        start = time.process_time()
        self.assertIsNone(rules.ua_rule("a" * 4000))
        self.assertLess(time.process_time() - start, 1.0)
        ips = blocklist.parse("\n".join(f"10.{i // 256}.{i % 256}.0/24" for i in range(2000)))
        start = time.process_time()
        for _ in range(1000):
            ips.ip_rule("203.0.113.9")
        self.assertLess(time.process_time() - start, 1.0)

    def test_saved_forms(self):
        self.assertEqual(blocklist.normalized_ip_text("1.2.3.4\r\n\r\n 1.2.3.4 \n#注释\n2001:DB8::1/128"), "1.2.3.4\n#注释\n2001:db8::1")
        self.assertEqual(blocklist.normalized_ua_text("  a*b \n\na*b\n# c"), "a*b\n# c")
        with self.assertRaises(blocklist.RuleError) as caught:
            blocklist.normalized_ip_text("\n".join(f"10.0.{i // 256}.{i % 256}" for i in range(2001)))
        self.assertIn("最多 2000 条", str(caught.exception))

    def test_thunder(self):
        rules = blocklist.parse(ua_text="\n".join(blocklist.THUNDER_RULES))
        self.assertEqual(rules.ua_rule("Mozilla/5.0 Thunder/12.0"), "*Thunder*")


class MiddlewareTests(TestCase):
    def setUp(self):
        self.site = SiteSettings.load()

    def block(self, ip="", ua=""):
        self.site.ip_blocklist, self.site.ua_blocklist = ip, ua
        self.site.save()

    def test_a_blocked_ip_gets_403_everywhere(self):
        self.block(ip="203.0.113.0/24")
        for path in ("/", "/manage/", "/media/site/x.png", "/no-such-page/", "/site/custom.css"):
            with self.subTest(path):
                r = self.client.get(path, REMOTE_ADDR="203.0.113.9")
                self.assertEqual(r.status_code, 403)
                page = r.content.decode()
                self.assertIn("访问控制", page)
                self.assertIn("web/css/m3.css", page)  # the site's error page
                self.assertNotIn("203.0.113", page)  # never echoes the IP
                self.assertNotIn("回到首页", page)  # the same rule refuses the home page: no buttons at all
                self.assertNotIn("error-actions", page)
                self.assertTrue(r["Content-Security-Policy"].startswith("default-src 'self'"))
        r = self.client.get("/api/v1/site/", REMOTE_ADDR="203.0.113.9")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "blocked"))
        self.assertEqual(self.client.get("/", REMOTE_ADDR="198.51.100.9").status_code, 200)

    def test_static_files_still_load(self):
        self.block(ip="203.0.113.0/24")
        r = self.client.get("/static/web/css/m3.css", REMOTE_ADDR="203.0.113.9")
        self.assertEqual(r.status_code, 200)  # the 403 page needs them; they are the program's own

    def test_a_blocked_user_agent(self):
        self.block(ua="*迅雷*\n*Thunder*")
        self.assertEqual(self.client.get("/", HTTP_USER_AGENT="Mozilla/5.0 迅雷/12".encode("utf-8").decode("latin-1")).status_code, 403)
        self.assertEqual(self.client.get("/", HTTP_USER_AGENT="Mozilla/5.0 Thunder/12").status_code, 403)
        self.assertEqual(self.client.get("/", HTTP_USER_AGENT="Mozilla/5.0 Firefox/150").status_code, 200)
        self.assertEqual(self.client.get("/").status_code, 200)  # without a User-Agent

    def test_the_client_ip_follows_the_proxies_setting(self):
        self.block(ip="198.51.100.7")
        forwarded = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "203.0.113.1, 198.51.100.7"}
        self.assertEqual(self.client.get("/", **forwarded).status_code, 200)  # proxies = 0: the header isn't believed
        with mock.patch("apps.core.net.api_settings") as drf:
            drf.NUM_PROXIES = 1
            self.assertEqual(self.client.get("/", **forwarded).status_code, 403)
            # Behind one proxy, only its own hop counts: what the client wrote left of it doesn't.
            self.assertEqual(self.client.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="198.51.100.7, 203.0.113.1").status_code, 200)

    def test_a_saved_change_applies_at_once(self):
        self.assertEqual(self.client.get("/", REMOTE_ADDR="203.0.113.9").status_code, 200)
        self.block(ip="203.0.113.9")
        self.assertEqual(self.client.get("/", REMOTE_ADDR="203.0.113.9").status_code, 403)
        self.block()
        self.assertEqual(self.client.get("/", REMOTE_ADDR="203.0.113.9").status_code, 200)

    def test_the_django_admin_too(self):
        from apps.core.testing import admin_url

        self.block(ip="203.0.113.9")
        self.assertEqual(self.client.get(admin_url("login/"), REMOTE_ADDR="203.0.113.9").status_code, 403)

    def test_not_before_the_site_is_installed(self):
        request = RequestFactory().get("/install/", REMOTE_ADDR="203.0.113.9")
        middleware = AccessControlMiddleware(lambda request: HttpResponse("ok"))
        with override_settings(SETUP_MODE=True), mock.patch.object(SiteSettings, "load", side_effect=AssertionError("no database")):
            self.assertEqual(middleware(request).content, b"ok")

    def test_admins_too(self):
        # The settings page refuses rules that block whoever saves them (test_api); rules saved from
        # elsewhere apply to admins as to everybody (`hfile unblock` empties the lists).
        boss, _, _ = users()
        self.client.force_login(boss)
        self.block(ip="203.0.113.9")
        self.assertEqual(self.client.get("/manage/", REMOTE_ADDR="203.0.113.9").status_code, 403)


class UnblockCommandTests(TestCase):
    def setUp(self):
        self.site = SiteSettings.load()

    def change(self, **fields):
        for name, value in fields.items():
            setattr(self.site, name, value)
        self.site.save()

    def test_it_empties_both_lists(self):
        self.change(ip_blocklist="# 注释\n203.0.113.9\n10.0.0.0/8", ua_blocklist="*Thunder*")
        out = StringIO()
        call_command("unblock", stdout=out)
        self.assertIn("IP 黑名单 2 条、UA 黑名单 1 条", out.getvalue())
        site = SiteSettings.load()
        self.assertEqual((site.ip_blocklist, site.ua_blocklist), ("", ""))
        out = StringIO()
        call_command("unblock", stdout=out)
        self.assertIn("本来就是空的", out.getvalue())
