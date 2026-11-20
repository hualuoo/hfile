"""The editor page /onlyoffice/edit/ (its CSP adds only the Document Server's origin), and the viewer's
script on the browser's pages."""

from __future__ import annotations

import html
import json
import re

from django.contrib.staticfiles import finders
from django.test import Client, TestCase

from apps.files import pages
from apps.siteconf.models import SiteSettings

from .. import csp
from ..apps import SCRIPTS
from .helpers import EDITOR, OfficeTestCase


def policy_of(response) -> dict[str, list[str]]:
    header = response["Content-Security-Policy"]
    directives = {}
    for part in header.split(";"):
        words = part.split()
        if words:
            directives[words[0]] = words[1:]
    return directives


def page_config(response) -> dict:
    found = re.search(r'id="office" data-config="([^"]*)"', response.content.decode())
    return json.loads(html.unescape(found[1]))


class EditorPageTests(OfficeTestCase):
    def editor(self, **query):
        return self.client.get(EDITOR, query or {"storage": "disk", "path": "/docs/a.docx"})

    def test_the_page(self):
        self.login()
        response = self.editor()
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "<title>a.docx · ")
        self.assertIn("no-store", response["Cache-Control"])
        config = page_config(response)
        self.assertEqual(config["request"], {"storage": "disk", "path": "/docs/a.docx"})
        self.assertEqual(config["api_url"], f"{self.ds.url}/web-apps/apps/api/documents/api.js")
        self.assertEqual(config["config_url"], "/api/v1/office/config/")
        self.assertEqual(config["back"], "/disk/docs")
        self.assertEqual(config["error"], "")
        content = response.content.decode()
        self.assertIn('src="/static/office/js/editor.js', content)
        self.assertNotIn("<script>", content)  # nothing inline

    def test_its_policy_adds_only_the_document_server(self):
        browse = policy_of(self.client.get("/disk/docs"))  # a page of the site, for comparison
        site = policy_of(self.client.get("/manage/"))
        response = self.editor()
        own = policy_of(response)
        self.assertEqual(own["script-src"], ["'self'", self.ds.url])
        self.assertEqual(own["frame-src"], ["'self'", self.ds.url])
        for name in set(own) | set(site):
            if name in ("script-src", "frame-src"):
                continue
            self.assertEqual(own.get(name), site.get(name), name)
        self.assertNotIn(self.ds.url, browse.get("script-src", []))
        self.assertEqual(own["frame-ancestors"], ["'none'"])
        self.assertEqual(own["connect-src"] if "connect-src" in own else own["default-src"], ["'self'"])

    def test_custom_js_doesnt_widen_it(self):
        site = SiteSettings.load()
        site.custom_js = "console.log('stats')"
        site.save()
        response = self.editor()
        own = policy_of(response)
        self.assertEqual(own["script-src"], ["'self'", self.ds.url])
        self.assertNotIn("connect-src", own)
        self.assertNotIn("custom.js", response.content.decode())

    def test_a_server_with_a_path(self):
        self.configure(url=f"{self.ds.url}/office/?x=1#y")
        response = self.editor()
        self.assertEqual(page_config(response)["api_url"], f"{self.ds.url}/office/web-apps/apps/api/documents/api.js")
        self.assertEqual(policy_of(response)["script-src"], ["'self'", self.ds.url])

    def test_without_an_onlyoffice_address(self):
        self.configure(url="")
        response = self.editor()
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "没有设置 OnlyOffice 服务地址", status_code=404)
        self.assertEqual(policy_of(response)["script-src"], ["'self'"])
        self.assertNotIn("frame-src", policy_of(response))
        self.assertEqual(page_config(response)["request"], None)

    def test_an_ipv6_address_cant_be_named(self):
        self.configure(url="http://[::1]:8080")
        response = self.editor()
        self.assertContains(response, "不能是 IPv6 地址")
        self.assertEqual(policy_of(response)["script-src"], ["'self'"])
        self.assertEqual(page_config(response)["api_url"], "")

    def test_bad_addresses(self):
        for query in ({}, {"path": "/a.docx"}, {"storage": "disk"}, {"storage": "disk", "share": "x", "path": "/a.docx"},
                      {"storage": "disk", "path": "/a/../b.docx"}, {"storage": "disk", "path": "/x" * 3000}):
            with self.subTest(query=query):
                response = self.client.get(EDITOR, query)
                self.assertEqual(response.status_code, 400)
                self.assertContains(response, "打开的地址不对", status_code=400)

    def test_a_share(self):
        response = self.editor(share="Sh4reKey", path="/sub/a.docx")
        self.assertEqual(page_config(response)["request"], {"share": "Sh4reKey", "path": "/sub/a.docx"})
        self.assertEqual(page_config(response)["back"], "/share/Sh4reKey")

    def test_names_are_escaped(self):
        response = self.editor(storage="disk", path='/<script>alert(1)</script>".docx')
        content = response.content.decode()
        self.assertNotIn("<script>alert", content)
        self.assertEqual(page_config(response)["request"]["path"], '/<script>alert(1)</script>".docx')

    def test_only_get(self):
        self.assertEqual(Client().post(EDITOR).status_code, 405)
        self.assertEqual(Client().head(EDITOR, {"storage": "disk", "path": "/docs/a.docx"}).status_code, 200)


class AssetTests(TestCase):
    def test_the_browser_pages_load_the_viewer(self):
        for script in SCRIPTS:
            self.assertIn(script, pages.assets()["scripts"])
            self.assertIsNotNone(finders.find(script))
        self.assertIsNotNone(finders.find("office/js/editor.js"))
        self.assertIsNotNone(finders.find("office/css/office.css"))

    def test_origins(self):
        self.assertEqual(csp.server_origin("https://office.example.com/x/"), "https://office.example.com")
        self.assertEqual(csp.server_origin("http://10.0.0.5:8080"), "http://10.0.0.5:8080")
        self.assertEqual(csp.server_origin("http://[::1]:8080"), "")
        self.assertEqual(csp.server_origin("javascript:alert(1)"), "")
        self.assertEqual(csp.server_origin(""), "")
        self.assertEqual(csp.server_origin("https://a b.example.com"), "")
