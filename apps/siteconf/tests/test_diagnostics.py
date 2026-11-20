"""诊断信息: GET /api/v1/manage/diagnostics/ (D42)."""

from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings

from ..views import _host, notes
from .helpers import users


class DiagnosticsTests(TestCase):
    URL = "/api/v1/manage/diagnostics/"

    def setUp(self):
        self.boss, self.staff, self.alice = users()

    def test_admins_only(self):
        self.assertEqual(self.client.get(self.URL).status_code, 401)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(self.URL).status_code, 403)

    def test_secrets_are_hidden(self):
        self.client.force_login(self.staff)
        session = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        jwt = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0In0.c2lnbmF0dXJl"
        r = self.client.get(self.URL, HTTP_AUTHORIZATION="Bearer abc.def", HTTP_X_CSRFTOKEN="csrf-123", HTTP_X_AUTH_TOKEN="tok-9",
                            HTTP_CF_ACCESS_JWT_ASSERTION=jwt, HTTP_X_AMZN_OIDC_DATA=jwt, HTTP_X_FORWARDED_USER=f"bob;{jwt}",
                            HTTP_X_API_KEY="key-7", HTTP_USER_AGENT="Probe/1", HTTP_X_FORWARDED_FOR="203.0.113.1",
                            HTTP_REFERER=f"http://testserver/{settings.ADMIN_PATH}/x/?token=tok-55&x=1")
        data = r.json()
        body = r.content.decode()
        for secret in (session, "abc.def", "csrf-123", "tok-9", "key-7", "tok-55", "eyJzdWIiOiIxMjM0In0"):
            self.assertNotIn(secret, body)
        headers = data["headers"]
        self.assertEqual(headers["Cookie"], f"{settings.SESSION_COOKIE_NAME}=<已隐藏>")
        self.assertEqual((headers["Authorization"], headers["X-Csrftoken"], headers["X-Auth-Token"], headers["X-Api-Key"]), ("<已隐藏>",) * 4)
        self.assertEqual(headers["User-Agent"], "Probe/1")
        self.assertEqual((headers["Cf-Access-Jwt-Assertion"], headers["X-Forwarded-User"]), ("<已隐藏>", "<已隐藏>"))
        self.assertEqual(headers["X-Forwarded-For"], "203.0.113.1")
        self.assertEqual(headers["Referer"], "http://testserver/<admin>/x/?token=<token>&x=1")  # as the access log does
        self.assertEqual(list(headers), sorted(headers))

    def test_only_known_headers_are_shown(self):
        """SEC-03: a name doesn't tell a secret (a CDN's key to the origin, a password to the upstream): only
        the values of headers known to be harmless are shown, as the client sent them (API-14: UTF-8)."""
        self.client.force_login(self.staff)
        ua = "测试浏览器/1.0".encode("utf-8").decode("latin-1")  # what WSGI gives
        r = self.client.get(self.URL, HTTP_X_ORIGIN_VERIFY="cdn-secret-1", HTTP_X_PASSWD="pw-2", HTTP_X_UPSTREAM_PASS="pw-3",
                            HTTP_X_FORWARDED_PROTO="https", HTTP_SEC_FETCH_SITE="same-origin", HTTP_USER_AGENT=ua)
        headers = r.json()["headers"]
        for secret in ("cdn-secret-1", "pw-2", "pw-3"):
            self.assertNotIn(secret, r.content.decode())
        self.assertEqual((headers["X-Origin-Verify"], headers["X-Passwd"], headers["X-Upstream-Pass"]), ("<已隐藏>",) * 3)
        self.assertEqual((headers["X-Forwarded-Proto"], headers["Sec-Fetch-Site"]), ("https", "same-origin"))
        self.assertEqual(headers["User-Agent"], "测试浏览器/1.0")

    def test_what_it_sees(self):
        self.client.force_login(self.boss)
        data = self.client.get(self.URL, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.1").json()
        self.assertEqual((data["client_ip"], data["remote_addr"], data["forwarded_for"]), ("127.0.0.1", "127.0.0.1", ["198.51.100.1", "203.0.113.1"]))
        self.assertEqual((data["scheme"], data["host"], data["origin"], data["proxies"]), ("http", "testserver", "http://testserver", 0))
        self.assertEqual(data["proxy_headers"], ["X-Forwarded-For"])
        self.assertEqual(data["notes"][0]["kind"], "warning")  # a proxy's header, and proxies = 0
        with mock.patch("apps.core.net.api_settings") as drf, override_settings(SERVER={**settings.SERVER, "proxies": 1}):
            drf.NUM_PROXIES = 1
            data = self.client.get(self.URL, HTTP_X_FORWARDED_FOR="198.51.100.1, 203.0.113.1").json()
        self.assertEqual(data["client_ip"], "203.0.113.1")

    def test_the_notes(self):
        def found(**changes):
            base = {"proxies": 0, "forwarded_for": [], "proxy_headers": [], "remote_addr": "127.0.0.1", "site_url": "",
                    "scheme": "http", "host": "files.example.com", "origin": "http://files.example.com"}
            return [(note["kind"], note["text"][:12]) for note in notes({**base, **changes})]

        self.assertEqual(found()[0][0], "info")  # no [site] url
        self.assertEqual(found(site_url="http://files.example.com")[-1][0], "ok")
        self.assertEqual(found(site_url="http://files.example.com:80/")[-1][0], "ok")
        self.assertEqual(found(proxy_headers=["X-Forwarded-For"], forwarded_for=["1.2.3.4"])[0][0], "warning")
        self.assertEqual(found(proxies=1)[0][0], "warning")  # no X-Forwarded-For behind a "proxy"
        self.assertEqual(found(proxies=2, forwarded_for=["1.2.3.4"])[0][0], "warning")
        mismatch = notes({"proxies": 1, "forwarded_for": ["1.2.3.4"], "proxy_headers": [], "remote_addr": "127.0.0.1",
                          "site_url": "https://files.example.com", "scheme": "http", "host": "127.0.0.1:8080",
                          "origin": "http://127.0.0.1:8080"})
        self.assertIn("X-Forwarded-Proto", mismatch[0]["text"])
        self.assertIn("proxy_set_header Host", mismatch[0]["text"])

    def test_hosts(self):
        self.assertEqual(_host("https", "Files.Example.com:443"), "files.example.com")
        self.assertEqual(_host("http", "files.example.com:8080"), "files.example.com:8080")
        self.assertEqual(_host("https", "[2001:DB8::1]:443"), "[2001:db8::1]")
        self.assertEqual(_host("http", "[2001:db8::1]:8080"), "[2001:db8::1]:8080")
