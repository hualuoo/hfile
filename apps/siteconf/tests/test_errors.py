"""The error pages, drawn in place (apps/web/errors.py), and JSON under /api/."""

import json

from django.test import RequestFactory, TestCase

from apps.core.testing import admin_url
from apps.web import errors

from ..models import SiteSettings


class ErrorPageTests(TestCase):
    def test_pages_and_json(self):
        factory = RequestFactory()
        page = factory.get("/somewhere/?x=1&y=2")
        api = factory.get("/api/v1/somewhere/")
        for status in (400, 401, 403, 404, 413, 429, 500):
            with self.subTest(status):
                r = errors.error_page(page, status)
                self.assertEqual(r.status_code, status)
                html = r.content.decode()
                self.assertIn(f'<p class="error-code">{status}</p>', html)
                self.assertIn("web/css/app.css", html)
                r = errors.error_page(api, status)
                self.assertEqual((r.status_code, r["Content-Type"]), (status, "application/json"))
                self.assertEqual(json.loads(r.content)["detail"], errors.DEFAULT_MESSAGES[status])

    def test_messages_retry_and_the_login_button(self):
        page = RequestFactory().get("/a/b/?x=1&y=2")
        r = errors.error_page(page, 429, retry_after=29.2)
        self.assertEqual(r["Retry-After"], "30")
        self.assertIn("请 30 秒后再试", r.content.decode())
        r = errors.error_page(page, 404, "这个直链已经失效了。<b>")
        self.assertIn("这个直链已经失效了。&lt;b&gt;", r.content.decode())
        html = errors.error_page(page, 401).content.decode()
        self.assertIn('href="/login/?next=/a/b/%3Fx%3D1%26y%3D2"', html)
        SiteSettings.objects.update_or_create(pk=1, defaults={"secure_login_entry": "door"})
        html = errors.error_page(page, 401).content.decode()
        self.assertNotIn("/login/", html)
        self.assertNotIn("door", html)
        r = errors.error_page(RequestFactory().get("/api/v1/x/"), 403, "不行。", code="blocked")
        self.assertEqual(json.loads(r.content), {"detail": "不行。", "code": "blocked"})

    def test_home_false_leaves_the_button_out(self):
        page = RequestFactory().get("/a/")
        html = errors.error_page(page, 403, "不行。").content.decode()  # other 403 pages keep theirs
        self.assertIn('<a class="btn btn-filled" href="/">回到首页</a>', html)
        html = errors.error_page(page, 403, "不行。", home=False).content.decode()
        self.assertNotIn("回到首页", html)
        self.assertNotIn("error-actions", html)
        self.assertIn("不行。", html)
        html = errors.error_page(page, 403, home=False, actions=[("重新登录", "/sso/x/")]).content.decode()
        self.assertIn('<a class="btn btn-filled" href="/sso/x/">重新登录</a>', html)  # given ways on still show
        self.assertNotIn("回到首页", html)

    def test_djangos_handlers(self):
        r = self.client.get("/api/v1/no-such-thing/")
        self.assertEqual((r.status_code, r.json()), (404, {"detail": "没有找到。"}))
        r = self.client.get("/no-such-page/")
        self.assertIn("找不到这个页面", r.content.decode())
        r = errors.server_error(RequestFactory().get("/api/v1/x/"))
        self.assertEqual((r.status_code, json.loads(r.content)["detail"]), (500, "服务器出错了，请稍后再试。"))
        self.assertIn("网站出错了", errors.server_error(RequestFactory().get("/x/")).content.decode())

    def test_csrf_failures_of_forms(self):
        from django.test import Client

        browser = Client(enforce_csrf_checks=True)
        r = browser.post(admin_url("login/"), {"username": "x", "password": "y"})
        self.assertEqual(r.status_code, 403)
        page = r.content.decode()
        self.assertIn("页面的安全校验（CSRF）没通过", page)
        self.assertIn("web/css/m3.css", page)
