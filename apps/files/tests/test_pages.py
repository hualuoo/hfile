"""The browser's pages: / and /<storage key>/<path>, the reserved names around the catch-all, the guest
landing page, errors, the CSP for storages that serve their own files; and an S3 storage against moto."""

import html
import json
import os
import re
import unittest
from pathlib import Path

from django.conf import settings
from django.test import Client

from apps.files import transfer
from apps.siteconf.models import SiteSettings
from apps.storages import config
from apps.storages.drivers.autoindex import AutoIndexDriver
from apps.storages.drivers.cf_imgbed import ImgBedDriver
from apps.storages.drivers.s3 import S3Driver
from apps.storages.models import StorageSource

from .helpers import EVERYTHING, FilesTestCase


def page_config(response):
    match = re.search(r'data-config="([^"]*)"', response.content.decode())
    return json.loads(html.unescape(match.group(1)))


def csp(response):
    return dict(part.split(" ", 1) for part in response["Content-Security-Policy"].split("; "))


class PageTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.browser = Client()

    def change(self, **fields):
        site = SiteSettings.load()
        for name, value in fields.items():
            setattr(site, name, value)
        site.save()

    def test_the_home_page_lists_the_storages(self):
        self.browser.force_login(self.alice)
        response = self.browser.get("/")
        self.assertEqual(response.status_code, 200)
        data = page_config(response)
        self.assertEqual((data["storage"], data["path"], [s["key"] for s in data["storages"]]), (None, "/", ["disk"]))
        self.assertEqual(data["site"]["home_name"], "首页")
        self.assertTrue(data["user"]["authenticated"])
        page = response.content.decode()
        self.assertIn("files/js/files.js", page)
        self.assertIn("files/js/browse.js", page)
        self.assertIn("uploads/js/uploads.js", page)  # contract U: the upload panel
        self.assertIn("uploads/css/uploads.css", page)
        self.assertNotRegex(page, r"<script(?![^>]*\bsrc=)[^>]*>")  # no inline scripts
        self.assertNotRegex(page, r"\sstyle=")
        self.assertIn("Powered by HFile", page)  # a public page: the footer

    def test_without_the_storage_list_the_first_storage(self):
        self.change(root_show_storage=False)
        self.browser.force_login(self.alice)
        self.assertRedirects(self.browser.get("/"), "/disk/", fetch_redirect_response=False)

    def test_nothing_to_browse(self):
        response = self.browser.get("/")
        self.assertContains(response, "还没有可以浏览的存储源")
        self.assertContains(response, "没有可以匿名浏览的存储源")

    def test_nothing_to_browse_for_an_admin(self):
        # /manage/ is the 管理页; 后台 is the Django admin, elsewhere on the site.
        from apps.accounts.models import User
        StorageSource.objects.all().delete()
        self.browser.force_login(User.objects.create_user("boss", password="pw-boss-2026", is_staff=True))
        response = self.browser.get("/")
        self.assertContains(response, "前往管理页添加存储源")
        self.assertNotContains(response, "后台")

    def test_a_folder_page(self):
        self.browser.force_login(self.alice)
        for url, path in (("/disk", "/"), ("/disk/", "/"), ("/disk/docs", "/docs"), ("/disk/docs/sub/", "/docs/sub"),
                          ("/DISK/docs", "/docs"), ("/disk/%E6%96%87%E4%BB%B6", "/文件")):
            with self.subTest(url):
                response = self.browser.get(url)
                self.assertEqual(response.status_code, 200)
                data = page_config(response)
                self.assertEqual((data["storage"]["key"], data["path"]), ("disk", path))
        self.assertContains(self.browser.get("/disk/docs"), "<title>docs · HFile</title>")
        nav = self.browser.get("/disk/docs").content.decode()
        self.assertIn('aria-current="page"', nav[nav.index('<nav class="nav"'):])  # 首页 is current

    @unittest.skipIf(os.name == "nt", "Windows has no names with line breaks in them")
    def test_a_folder_with_a_line_break_in_its_name(self):
        self.files({"two\nlines/x.txt": b"x"})
        self.browser.force_login(self.alice)
        self.assertEqual(page_config(self.browser.get("/disk/two%0Alines"))["path"], "/two\nlines")

    def test_errors_are_pages(self):
        self.browser.force_login(self.alice)
        response = self.browser.get("/nope/x")
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "找不到这个页面", status_code=404)  # the first part may not be meant as a storage
        self.assertNotContains(response, "存储源不存在", status_code=404)
        self.assertEqual(self.browser.get("/disk/..").status_code, 400)
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertEqual(self.browser.get("/disk/").status_code, 403)
        visitor = Client()
        response = visitor.get("/disk/docs")
        self.assertEqual(response.status_code, 401)
        self.assertContains(response, "/login/?next=", status_code=401)

    def test_the_sites_own_addresses(self):
        self.assertRedirects(self.browser.get("/manage"), "/manage/", status_code=301, fetch_redirect_response=False)
        self.assertRedirects(self.browser.get("/login?next=/x"), "/login/?next=/x", status_code=301, fetch_redirect_response=False)
        for url in ("/api/v1/nothing", "/static/nothing.css", "/media/x.png", "/pd", "/manage/x/y", "/admin/"):
            with self.subTest(url):
                self.assertEqual(self.browser.get(url).status_code, 404)
        response = self.browser.get("/api/v1/nothing/")
        self.assertEqual(response.json(), {"detail": "没有找到。"})

    def test_the_guest_landing_page(self):
        self.grant(self.guest, ["preview"])
        self.change(guest_home="login")
        self.assertRedirects(self.browser.get("/disk/docs"), "/login/?next=%2Fdisk%2Fdocs", fetch_redirect_response=False)
        self.browser.force_login(self.alice)
        self.assertEqual(self.browser.get("/disk/docs").status_code, 200)

    def test_custom_code_and_the_csp(self):
        self.browser.force_login(self.alice)
        self.change(custom_css="body{}", custom_js="var x = 1;")
        response = self.browser.get("/disk/")
        page = response.content.decode()
        self.assertIn("/site/custom.css", page)
        self.assertIn("/site/custom.js", page)
        self.assertEqual(csp(response)["script-src"], "'self' https:")

    def test_the_csp_lets_in_where_a_storage_serves_its_files(self):
        self.browser.force_login(self.alice)
        policy = csp(self.browser.get("/disk/"))
        self.assertEqual(policy["img-src"], "'self' data: blob:")
        self.assertEqual(policy["media-src"], "'self' blob:")  # the viewers' (apps/preview/csp.py), on every browse page
        self.assertNotIn("connect-src", policy)  # uploads go through the site
        self.storage.download_domain = "https://dl.example.com/files"
        self.storage.save()
        policy = csp(self.browser.get("/disk/"))
        self.assertEqual(policy["img-src"], "'self' data: blob: https://dl.example.com")
        self.assertEqual(policy["media-src"], "'self' blob: https://dl.example.com")
        self.assertEqual(policy["script-src"], "'self'")
        direct = self.make_storage("cdn", "test-direct", self.root)
        self.grant(self.alice, EVERYTHING, storage=direct)
        policy = csp(self.browser.get("/cdn/"))
        self.assertIn("https:", policy["img-src"].split())  # a backend whose hosts can't be told
        self.assertIn("http:", policy["img-src"].split())  # nor whether they are http://: on an http site they load

    def test_the_csp_of_a_listing_server_at_an_http_address(self):
        mirror = StorageSource.objects.create(key="mirror", name="镜像", type="autoindex",
                                              config=config.encode(AutoIndexDriver, {"url": "http://192.168.1.5/pub/"}))
        self.assertIn("http:", transfer.media_sources(mirror))

    def test_the_csp_of_an_imgbed(self):
        """ImgBed's links are its own, but a file of its External channel redirects to the link ImgBed keeps, on any
        host: its pages let pictures and videos in from anywhere (in a browser: apps/storages/tests/test_browser.py)."""
        settings = {"url": "https://img.example.com", "token": "imgbed_t", "base_path": "/", "channel": "telegram"}
        bed = StorageSource.objects.create(key="bed", name="图床", type="cf-imgbed", config=config.encode(ImgBedDriver, settings))
        self.assertEqual(transfer.media_sources(bed), ("http:", "https:"))

    def test_a_download_domain_in_another_script(self):
        """API-13: the CSP named it as Python's own idna codec (IDNA 2003) writes it, faß.de as fass.de, while
        browsers load the files from xn--fa-hia.de. One IDNA 2008 refuses isn't named (written as xn--, it is)."""
        self.browser.force_login(self.alice)
        for domain, sources in (("https://faß.de/files", ["https://xn--fa-hia.de"]),
                                ("https://例子.中国", ["https://xn--fsqu00a.xn--fiqs8s"]),
                                ("https://☃.net", []), ("https://a..例子", []), ("https://xn--n3h.net", ["https://xn--n3h.net"])):
            with self.subTest(domain):
                self.storage.download_domain = domain
                self.storage.save()
                self.assertEqual(csp(self.browser.get("/disk/"))["img-src"], " ".join(["'self'", "data:", "blob:", *sources]))

    def test_a_gallery_picture_that_doesnt_load_gets_the_placeholder(self):
        # files.js draws the gallery, the share page's too: a picture that fails to load is swapped for the
        # placeholder of a file without a link, not left as the browser's broken image.
        static = Path(settings.BASE_DIR) / "apps" / "files" / "static" / "files"
        script = (static / "js" / "files.js").read_text(encoding="utf-8")
        gallery = script[script.index("    renderGallery() {"):script.index("    finishRows(total) {")]
        self.assertIn("const placeholder = () => h('span', { class: 'files-photo-missing'", gallery)
        self.assertIn(": placeholder();", gallery)
        self.assertIn("picture.addEventListener('error', () => picture.replaceWith(placeholder()), { once: true });", gallery)
        self.assertNotIn(".broken", (static / "css" / "files.css").read_text(encoding="utf-8"))


class S3Tests(FilesTestCase):
    """An S3 storage against moto: links of the bucket in listings, proxied ones with 代理下载, the
    operations, and the CSP of its pages."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from apps.storages.tests.moto_s3 import MotoS3

        cls.moto = MotoS3()

    @classmethod
    def tearDownClass(cls):
        cls.moto.stop()
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.bucket = self.moto.bucket()
        values = {"endpoint": self.moto.endpoint, "access_key": "AKIDTEST", "secret_key": "secret-test-key",
                  "bucket": self.bucket, "base_path": "/", "path_style": "path", "is_private": True, "domain": "", "region": ""}
        self.s3 = StorageSource.objects.create(key="bucket", name="桶", type="s3", config=config.encode(S3Driver, values))
        self.grant(self.alice, EVERYTHING, storage=self.s3)
        for key, data in (("docs/a.txt", b"hello s3"), ("docs/b.png", b"\x89PNG"), ("top.txt", b"top")):
            self.moto.put_raw(self.bucket, key, data)
        self.login()

    def test_listing_with_presigned_links(self):
        items = self.client.get("/api/v1/files/bucket/list/", {"path": "/docs"}).json()["items"]
        a = next(i for i in items if i["name"] == "a.txt")
        self.assertTrue(a["url"].startswith(f"{self.moto.endpoint}/{self.bucket}/docs/a.txt?"), a["url"])
        self.assertIn("X-Amz-Signature=", a["url"])
        self.assertEqual(a["kind"], "text")
        item = self.client.get("/api/v1/files/bucket/item/", {"path": "/docs/a.txt"}).json()
        self.assertTrue(item["inline_url"].startswith("/pd/bucket/docs/a.txt?"))
        response = self.client.get(item["inline_url"])
        self.assertEqual(b"".join(response.streaming_content), b"hello s3")
        response = self.client.get(item["inline_url"], HTTP_RANGE="bytes=6-7")
        self.assertEqual((response.status_code, b"".join(response.streaming_content)), (206, b"s3"))

    def test_proxy_download(self):
        self.s3.proxy_download = True
        self.s3.save()
        items = self.client.get("/api/v1/files/bucket/list/", {"path": "/docs"}).json()["items"]
        url = next(i["url"] for i in items if i["name"] == "a.txt")
        self.assertTrue(url.startswith("/pd/bucket/docs/a.txt?"))
        self.assertEqual(b"".join(self.client.get(url).streaming_content), b"hello s3")

    def test_operations(self):
        self.assertEqual(self.post("mkdir", {"path": "/", "name": "dir"}, key="bucket").status_code, 201)
        response = self.post("copy", {"path": "/docs", "names": ["a.txt"], "target": "/dir"}, key="bucket")
        self.assertEqual(response.json()["succeeded"], 1)
        response = self.post("copy", {"path": "/docs", "names": ["a.txt"], "target": "/dir"}, key="bucket")
        self.assertEqual(response.json()["results"][0]["code"], "exists")
        response = self.post("move", {"path": "/", "names": ["docs"], "target": "/dir"}, key="bucket")
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual(self.post("rename", {"path": "/", "name": "top.txt", "new_name": "顶.txt"}, key="bucket").status_code, 200)
        response = self.post("delete", {"path": "/dir", "names": ["a.txt"]}, key="bucket")
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual(self.moto.keys(self.bucket), ["dir/", "dir/docs/a.txt", "dir/docs/b.png", "顶.txt"])

    def test_the_page_lets_the_bucket_in(self):
        browser = Client()
        browser.force_login(self.alice)
        policy = csp(browser.get("/bucket/"))
        origin = self.moto.endpoint
        self.assertIn(origin, policy["img-src"].split())
        self.assertIn(origin, policy["media-src"].split())
        self.assertNotIn("https:", policy["img-src"].split())
        # The upload panel's direct uploads (apps.uploads.csp): the bucket's address, and https: for others.
        self.assertIn(origin, policy["connect-src"].split())
        self.assertIn("https:", policy["connect-src"].split())
        self.assertEqual(policy["script-src"], "'self'")
