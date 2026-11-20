"""Tests of the web server directory listing driver (apps.storages.drivers.autoindex) against listings in the
styles of nginx, Apache, lighttpd, Caddy and Python's http.server (apps/storages/tests/fake_autoindex.py).

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_autoindex
"""

from __future__ import annotations

import datetime
import gzip
import time
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages.drivers import http
from apps.storages.drivers.autoindex import AutoIndexDriver
from apps.storages.drivers.base import AuthError, BackendError, DriverError, NotFound
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_autoindex import FakeAutoIndex
from apps.storages.tests.fake_cloud_http import FakeServer, Response


class StyleMixin:
    style = "nginx"
    caddy_json = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakeAutoIndex(READ_ONLY_TREE, folders=(EMPTY_FOLDER,), style=cls.style).start()
        cls.fake.caddy_json = cls.caddy_json

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def make_driver(self):
        return AutoIndexDriver({"url": self.fake.url})


class NginxContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    style = "nginx"


class NginxJsonContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    style = "json"


class ApacheContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    style = "apache"


class PythonContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    style = "python"


class LighttpdContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    style = "lighttpd"


class CaddyContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    """Caddy's browse answers JSON: the driver's Accept has application/json."""

    style = "caddy"


class CaddyHtmlContractTests(StyleMixin, ReadOnlyContract, unittest.TestCase):
    """Caddy's HTML page, where something makes it answer HTML."""

    style = "caddy"
    caddy_json = False


class AutoIndexTests(unittest.TestCase):
    def serve(self, files, **kwargs):
        fake = FakeAutoIndex(files, **kwargs).start()
        self.addCleanup(fake.close)
        return fake

    def test_what_the_listing_says(self):
        fake = self.serve({"/" + "long name " * 8 + ".txt": b"x" * 10, "/b.bin": b"y" * 2048})
        nginx = {e.name: e for e in AutoIndexDriver({"url": fake.url}).list("/")}
        self.assertIn("long name " * 8 + ".txt", nginx)  # from the href, not the cut text
        self.assertEqual(nginx["b.bin"].size, 2048)
        self.assertEqual(nginx["b.bin"].modified, datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.timezone.utc))
        fake.style = "apache"
        apache = {e.name: e for e in AutoIndexDriver({"url": fake.url}).list("/")}
        self.assertEqual(apache["b.bin"].size, 2048)  # "2.0K" isn't a size: HEAD gave it
        self.assertNotIn("Parent Directory", apache)

    def test_a_password(self):
        fake = self.serve({"/a.txt": b"a"})
        fake.auth = ("u", "p")
        with self.assertRaises(AuthError) as asked:  # 401 without a username: the settings need one
            AutoIndexDriver({"url": fake.url}).list("/")
        self.assertIn("401", asked.exception.message)
        with self.assertRaises(AuthError) as wrong:
            AutoIndexDriver({"url": fake.url, "username": "u", "password": "wrong"}).list("/")
        self.assertNotEqual(asked.exception.message, wrong.exception.message)  # asks for them; says to check them
        d = AutoIndexDriver({"url": fake.url, "username": "u", "password": "p"})
        self.assertEqual(d.open("/a.txt").read(), b"a")
        self.assertIsNone(d.download_url("/a.txt", filename="a.txt"))  # the browser has no password

    def test_settings(self):
        self.assertEqual(AutoIndexDriver.validate_config({"url": "https://mirror.example.com/pub/"}), {})
        self.assertIn("url", AutoIndexDriver.validate_config({"url": "mirror.example.com"}))
        self.assertIn("url", AutoIndexDriver.validate_config({"url": "https://x.example/?C=N"}))
        self.assertIn("url", AutoIndexDriver.validate_config({"url": "https://mirror.example.com/pub/#files"}))

    def test_a_server_that_fails(self):
        fake = self.serve({})
        with self.assertRaises(NotFound):
            AutoIndexDriver({"url": fake.server.url + "/nowhere/"}).list("/")
        broken = FakeServer(lambda req: Response(502, b"Bad Gateway")).start()
        self.addCleanup(broken.close)
        with self.assertRaises(BackendError):
            AutoIndexDriver({"url": broken.url + "/"}).list("/")

    def test_sizes_the_listing_doesnt_give_come_from_head(self):
        fake = self.serve({f"/f{n}.txt": b"x" * n for n in range(5)}, style="python")
        d = AutoIndexDriver({"url": fake.url})
        self.assertEqual(sorted((e.name, e.size) for e in d.list("/")), [(f"f{n}.txt", n) for n in range(5)])
        d = AutoIndexDriver({"url": fake.url})
        d.head_limit = 2
        self.assertEqual(sum(e.size is None for e in d.list("/")), 3)

    def test_caddy(self):
        # Caddy's browse answers JSON to an Accept with application/json: "sub/" is a folder, mod_time its time.
        fake = self.serve({"/a.txt": b"a" * 1500, "/sub/b.txt": b"b"}, style="caddy")
        d = AutoIndexDriver({"url": fake.url})
        entries = {e.name: e for e in d.list("/")}
        self.assertEqual(sorted(entries), ["a.txt", "sub"])
        self.assertEqual((entries["a.txt"].size, entries["sub"].is_dir), (1500, True))
        self.assertEqual(entries["a.txt"].modified, datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.timezone.utc))
        self.assertEqual([e.name for e in d.list("/sub")], ["b.txt"])
        self.assertIn("application/json", fake.listing_requests[0][0])

    def test_exact_sizes_in_the_cells(self):
        # lighttpd's data-value and Caddy's data-size: no HEAD. Caddy's <time> text ("... PM +00:00") isn't a size.
        for style in ("lighttpd", "caddy"):
            with self.subTest(style=style):
                fake = self.serve({"/a.txt": b"a" * 1500}, style=style)
                fake.caddy_json = False
                entry = AutoIndexDriver({"url": fake.url}).list("/")[0]
                self.assertEqual((entry.size, entry.modified), (1500, datetime.datetime(2026, 11, 17, 12, 0, tzinfo=datetime.timezone.utc)))
                self.assertEqual(fake.heads, 0)

    def test_the_size_column(self):
        # Apache gives bytes below 973 and rounds the rest; a number in its description column isn't the size.
        fake = self.serve({"/small.txt": b"s" * 900, "/debian-13.iso": b"i" * 5000}, style="apache")
        fake.descriptions = {"/debian-13.iso": "Release 13 DVD 1"}
        entries = {e.name: e for e in AutoIndexDriver({"url": fake.url}).list("/")}
        self.assertEqual((entries["small.txt"].size, entries["debian-13.iso"].size), (900, 5000))
        self.assertEqual(fake.heads, 1)  # small.txt's size is in the page

    def test_hrefs_written_as_whole_paths(self):
        # IIS writes "/pub/C++%20(1)/a.txt"; the driver's own address of that folder is "/pub/C%2B%2B%20%281%29/".
        fake = self.serve({"/C++ (1)/a.txt": b"a", "/plain/b.txt": b"b"})
        fake.absolute = True
        d = AutoIndexDriver({"url": fake.url})
        self.assertEqual(sorted(e.name for e in d.list("/")), ["C++ (1)", "plain"])
        self.assertEqual([e.name for e in d.list("/C++ (1)")], ["a.txt"])

    def test_a_charset_python_doesnt_know(self):
        fake = self.serve({"/中文.txt": b"a"})
        fake.charset = "utf8mb4"
        self.assertEqual([e.name for e in AutoIndexDriver({"url": fake.url}).list("/")], ["中文.txt"])

    def test_a_password_in_the_address(self):
        for url in ("https://u:p@nas.example.com/pub/", "https://u@nas.example.com/pub/"):
            with self.subTest(url=url):
                self.assertIn("url", AutoIndexDriver.validate_config({"url": url}))
        fake = self.serve({"/a.txt": b"a"})
        fake.auth = ("u", "p")
        d = AutoIndexDriver({"url": fake.url.replace("http://", "http://u:p@")})  # saved without the form's check
        self.assertEqual([e.name for e in d.list("/")], ["a.txt"])
        self.assertIsNone(d.download_url("/a.txt", filename="a.txt"))  # never handed to a browser

    def test_what_head_said_is_kept(self):
        # A listing asks HEAD for head_limit files; download_url() asks for none, a size HEAD gave is asked for once.
        fake = self.serve({f"/f{n}.txt": b"x" * n for n in range(20)}, style="python")

        class Few(AutoIndexDriver):
            head_limit = 5

        class Uncached(Few):
            cache_ttl = -1.0  # the listing page is asked for each time

        d = Few({"url": fake.url})
        for _ in range(2):
            for entry in d.list("/"):
                self.assertTrue(d.download_url(entry.path, filename=entry.name).endswith(entry.name))
        self.assertEqual(fake.heads, 5)
        for _ in range(2):
            self.assertEqual(d.stat("/f19.txt").size, 19)
        self.assertEqual(fake.heads, 6)
        few = self.serve({f"/f{n}.txt": b"x" * n for n in range(5)}, style="python")
        d = Uncached({"url": few.url})
        d.list("/")
        self.assertEqual([e.size for e in sorted(d.list("/"), key=lambda e: e.name)], list(range(5)))
        self.assertEqual(few.heads, 5)

    def test_precompressed_files(self):
        data = b"".join(b"log line %d\n" % n for n in range(5000))
        fake = self.serve({"/app.log": data}, style="python")
        fake.precompressed = {"/app.log"}  # nginx's gzip_static: the .gz to whoever accepts gzip
        d = AutoIndexDriver({"url": fake.url})
        entry = d.stat("/app.log")
        self.assertEqual(entry.size, len(data))
        self.assertEqual(d.open_known("/app.log", entry, 0, entry.size).read(), data)
        fake.gzip_static = "always"  # the .gz whatever the request accepts: its length isn't the file's
        self.assertIsNone(AutoIndexDriver({"url": fake.url}).stat("/app.log").size)
        self.assertLess(len(gzip.compress(data)), len(data))

    def test_a_server_that_refuses_head(self):
        fake = self.serve({"/a.txt": b"abc"}, style="python")
        fake.head_status = 405
        d = AutoIndexDriver({"url": fake.url})
        self.assertIsNone(d.stat("/a.txt").size)
        self.assertEqual(d.open("/a.txt").read(), b"abc")

    def test_refusals(self):
        fake = self.serve({"/d/a.txt": b"a"})
        fake.forbidden = {"/d"}  # nginx answers 403 for a folder where autoindex is off
        with self.assertRaises(DriverError) as caught:
            AutoIndexDriver({"url": fake.url}).list("/d")
        self.assertNotIsInstance(caught.exception, AuthError)
        self.assertIn("403", caught.exception.message)

    def test_error_answers(self):
        # A short error page is read to its end, so that its connection can be used again; of a long one, 64 KiB.
        taken: dict[str, int] = {}

        def handle(request):
            path = request.url.path
            if path == "/pub/":
                return httpx.Response(200, headers={"Content-Type": "text/html"}, content=(
                    b'<a href="short/">short/</a>\n<a href="long/">long/</a>\n'
                    b'<a href="short.bin">short.bin</a>  17-Nov-2026 12:00  5\n'
                    b'<a href="long.bin">long.bin</a>  17-Nov-2026 12:00  5\n'))
            pieces, size = (3, 100) if "/short" in path else (50, 1024 * 1024)

            def body():
                for _ in range(pieces):
                    taken[path] = taken.get(path, 0) + 1
                    yield b"e" * size
            return httpx.Response(404 if path.endswith("/") else 500, headers={"Content-Type": "text/html"}, content=body())

        d = AutoIndexDriver({"url": "http://autoindex.invalid/pub/"})
        d._http.close()
        d._http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(d.close)
        for folder in ("/short", "/long"):
            with self.assertRaises(NotFound):
                d.list(folder)
        for name in ("/short.bin", "/long.bin"):
            with self.assertRaises(BackendError):
                d.open(name)
        self.assertEqual((taken.get("/pub/short/", 0), taken.get("/pub/short.bin", 0)), (3, 3))
        self.assertLessEqual(max(taken.get("/pub/long/", 0), taken.get("/pub/long.bin", 0)), 2)  # of 50 pieces of 1 MiB

    def test_the_listing_asks_for_no_compression(self):
        fake = self.serve({"/a.txt": b"a"})
        AutoIndexDriver({"url": fake.url}).list("/")
        self.assertEqual(fake.listing_requests[-1][1], "identity")

    def test_a_page_in_small_pieces(self):
        # Gathered and joined once: `page += piece` copies the page so far for each piece (tens of seconds here).
        page = (b'<html><body><pre><a href="a.txt">a.txt</a>  17-Nov-2026 12:00  5\n</pre><!--'
                + b" " * (6 * 1024 * 1024) + b"--></body></html>")

        def handle(request):
            return httpx.Response(200, headers={"Content-Type": "text/html"},
                                  content=(page[i:i + 64] for i in range(0, len(page), 64)))

        d = AutoIndexDriver({"url": "http://autoindex.invalid/pub/"})
        d._http.close()
        d._http = httpx.Client(transport=httpx.MockTransport(handle))
        start = time.monotonic()
        self.assertEqual([(e.name, e.size) for e in d.list("/")], [("a.txt", 5)])
        self.assertLess(time.monotonic() - start, 5)

    def test_a_page_that_takes_too_long(self):
        def handle(req):
            def slowly():
                for _ in range(20):
                    time.sleep(0.25)
                    yield b" "
            return Response(200, slowly(), {"Content-Type": "text/html"}, length=20)

        server = FakeServer(handle).start()
        self.addCleanup(server.close)

        class Impatient(AutoIndexDriver):
            page_seconds = 1.0

        start = time.monotonic()
        with self.assertRaises(BackendError):
            Impatient({"url": server.url + "/pub/"}).list("/")
        self.assertLess(time.monotonic() - start, 4)



class TheSitesOwnNetworkTests(unittest.TestCase):
    """http.client(guard=True), as test_driver_url_tree tests it: http.public says the port of `outside` is public,
    the Internet; `inside` is the site's own network."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inside = FakeAutoIndex({"/f.bin": b"inside"}, style="python").start()

        def outside(req):
            if req.path == "/pub/":  # a listing that redirects into the site's network
                return Response(302, b"", {"Location": cls.inside.url})
            if req.path == "/mirror/":  # a listing whose file does
                return Response(200, b'<a href="f.bin">f.bin</a>', {"Content-Type": "text/html"})
            return Response(302, b"", {"Location": cls.inside.url + "f.bin"})

        cls.outside = FakeServer(outside).start()
        cls.lan = FakeServer(lambda req: Response(302, b"", {"Location": cls.inside.url + req.path[len("/pub/"):]})).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.inside, cls.outside, cls.lan):
            server.close()
        super().tearDownClass()

    def setUp(self):
        self.inside.requests = 0
        number = urllib.parse.urlsplit(self.outside.url).port
        patcher = mock.patch.object(http, "public", lambda address, port: port == number)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_listing_redirected_into_the_sites_network(self):
        d = AutoIndexDriver({"url": self.outside.url + "/pub/"})
        self.addCleanup(d.close)
        with self.assertRaises(DriverError) as caught:
            d.list("/")
        self.assertIn("127.0.0.1", caught.exception.message)
        self.assertEqual(self.inside.requests, 0)  # nothing went there

    def test_a_file_redirected_into_the_sites_network(self):
        d = AutoIndexDriver({"url": self.outside.url + "/mirror/"})
        self.addCleanup(d.close)
        self.assertEqual([(e.name, e.size) for e in d.list("/")], [("f.bin", None)])  # its HEAD refused
        with self.assertRaises(DriverError):
            d.open("/f.bin")
        self.assertEqual(self.inside.requests, 0)

    def test_a_server_of_the_sites_network_redirects_where_it_likes(self):
        d = AutoIndexDriver({"url": self.lan.url + "/pub/"})
        self.addCleanup(d.close)
        self.assertEqual([(e.name, e.size) for e in d.list("/")], [("f.bin", 6)])
        self.assertEqual(d.open("/f.bin").read(), b"inside")


if __name__ == "__main__":
    unittest.main()
