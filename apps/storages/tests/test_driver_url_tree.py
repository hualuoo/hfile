"""Tests of the URL tree driver (apps.storages.drivers.url_tree) against a small web server.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_url_tree
"""

from __future__ import annotations

import base64
import gzip
import threading
import unittest
import urllib.parse
from unittest import mock

import httpx

from apps.storages.drivers import http
from apps.storages.drivers.base import AuthError, BackendError, DriverError, NotFound
from apps.storages.drivers.url_tree import URLTreeDriver, parse_tree
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_cloud_http import FakeServer, HTTPError, Response, file_response


class Files:
    """A web server of {path: bytes}; counts the requests and the HEAD requests. Knobs: head_status (HEAD answers
    with it), status (every request answers with it), precompressed (paths sent as their .gz to a request that
    accepts gzip and has no Range, as nginx's gzip_static does), auth ((username, password) for HTTP Basic)."""

    def __init__(self, files):
        self.files = files
        self.requests = 0
        self.heads = 0
        self.head_status = None
        self.status = None
        self.precompressed = set()
        self.auth = None
        self.lock = threading.Lock()
        self.server = FakeServer(self.handle).start()

    def handle(self, req):
        with self.lock:
            self.requests += 1
        if self.auth is not None and req.header("Authorization") != "Basic " + base64.b64encode(":".join(self.auth).encode()).decode():
            raise HTTPError(401, b"Unauthorized", {"WWW-Authenticate": 'Basic realm="files"'})
        if req.method == "HEAD":
            with self.lock:
                self.heads += 1
            if self.head_status is not None:
                raise HTTPError(self.head_status, b"")
        if self.status is not None:
            raise HTTPError(self.status, b"<html><body>Refused</body></html>")
        path = urllib.parse.unquote(req.path)
        data = self.files.get(path)
        if data is None:
            raise HTTPError(404, b"Not Found")
        if path in self.precompressed and "gzip" in (req.header("Accept-Encoding") or "") and not req.header("Range"):
            return Response(200, gzip.compress(data, mtime=0), {"Content-Encoding": "gzip", "Content-Type": "text/plain"})
        return file_response(data, req.header("Range"), {"Last-Modified": "Tue, 17 Nov 2026 12:00:00 GMT"})

    def url(self, path):
        return self.server.url + urllib.parse.quote(path)


def contract_tree(files: Files) -> str:
    """READ_ONLY_TREE as a tree: some files named and sized, some named after their address."""
    return "\n".join([
        f"a.txt:{files.url('/a.txt')}",
        "d:",
        f"  b.bin:{len(READ_ONLY_TREE['/d/b.bin'])}:{files.url('/d/b.bin')}",
        "  sub:",
        f"    {files.url('/d/sub/c.txt')}",
        f"  {files.url('/d/中文 名字.txt')}",
        "  a b:",
        f"    100%.txt:{files.url('/d/a b/100%.txt')}",
        EMPTY_FOLDER[1:] + ":",
    ])


class URLTreeContractTests(ReadOnlyContract, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.files = Files(READ_ONLY_TREE)

    @classmethod
    def tearDownClass(cls):
        cls.files.server.close()
        super().tearDownClass()

    def make_driver(self):
        return URLTreeDriver({"tree": contract_tree(self.files), "head_sizes": True})


class URLTreeTests(unittest.TestCase):
    def setUp(self):
        self.files = Files({"/x.bin": b"x" * 100, "/y.bin": b"y" * 7})
        self.addCleanup(self.files.server.close)

    def test_the_tree_setting(self):
        tree, errors = parse_tree("a:\n  b:\n    https://h.example/f.txt\n  g.txt:12:https://h.example/g?x=1\nhttps://h.example/dir/")
        self.assertEqual(errors, [])
        self.assertEqual(sorted(tree.children), ["a", "dir"])  # an address ending in "/": named after its last segment
        self.assertEqual(tree.children["a"].children["g.txt"].size, 12)
        self.assertEqual(list(tree.children["a"].children["b"].children), ["f.txt"])
        for text in ("", "a:\n    too-deep:", "a:\n\tb:", "no address here", "a:\na:", "x:https://h.example/1\nx:https://h.example/2"):
            with self.subTest(text=text):
                self.assertIn("tree", URLTreeDriver.validate_config({"tree": text}))

    def test_sizes_by_head(self):
        tree = f"{self.files.url('/x.bin')}\ny.bin:7:{self.files.url('/y.bin')}"
        d = URLTreeDriver({"tree": tree})
        self.assertEqual({e.name: e.size for e in d.list("/")}, {"x.bin": None, "y.bin": 7})
        self.assertEqual(self.files.heads, 0)
        self.assertEqual(d.stat("/x.bin").size, 100)
        self.assertEqual(d.open("/x.bin", 10, 5).read(), b"x" * 5)
        self.assertEqual(self.files.heads, 1)  # cached
        listed = URLTreeDriver({"tree": tree, "head_sizes": True})
        self.assertEqual({e.name: e.size for e in listed.list("/")}, {"x.bin": 100, "y.bin": 7})

    def test_a_missing_address(self):
        d = URLTreeDriver({"tree": f"gone.txt:{self.files.url('/gone.txt')}"})
        self.assertIsNone(d.list("/")[0].size)
        with self.assertRaises(NotFound):
            d.open("/gone.txt")
        self.assertEqual(d.download_url("/gone.txt", filename="gone.txt"), self.files.url("/gone.txt"))

    def test_open_doesnt_need_head(self):
        # A server that refuses HEAD (a presigned link's signature has the method in it): the GET's answer decides.
        self.files.head_status = 403
        d = URLTreeDriver({"tree": self.files.url("/x.bin")})
        self.assertEqual(d.open("/x.bin").read(), b"x" * 100)
        self.assertEqual(d.open("/x.bin", 90, 5).read(), b"x" * 5)
        self.assertEqual(self.files.heads, 0)
        self.assertIsNone(d.stat("/x.bin").size)

    def test_a_password_in_an_address(self):
        self.files.auth = ("u", "p")
        d = URLTreeDriver({"tree": self.files.url("/x.bin").replace("http://", "http://u:p@")})
        self.assertEqual(d.open("/x.bin").read(), b"x" * 100)
        self.assertIsNone(d.download_url("/x.bin", filename="x.bin"))  # the site sends it: never hand them out

    def test_precompressed_files(self):
        data = b"".join(b"log line %d\n" % n for n in range(5000))
        self.files.files["/app.log"] = data
        self.files.precompressed = {"/app.log"}
        d = URLTreeDriver({"tree": self.files.url("/app.log")})
        entry = d.stat("/app.log")
        self.assertEqual(entry.size, len(data))  # not the .gz's length
        self.assertEqual(d.open_known("/app.log", entry, 0, entry.size).read(), data)

    def test_refused_addresses(self):
        self.files.status = 403
        d = URLTreeDriver({"tree": self.files.url("/x.bin")})
        with self.assertRaises(DriverError) as caught:
            d.open("/x.bin")
        self.assertNotIsInstance(caught.exception, AuthError)  # there is no username and password to check
        self.assertIn("403", caught.exception.message)

    def test_failed_heads_are_remembered(self):
        tree = f"{self.files.url('/gone.txt')}\n{self.files.url('/x.bin')}"
        d = URLTreeDriver({"tree": tree, "head_sizes": True})
        for _ in range(3):
            self.assertEqual({e.name: e.size for e in d.list("/")}, {"gone.txt": None, "x.bin": 100})
        self.assertEqual(self.files.heads, 2)

    def test_addresses_httpx_refuses(self):
        for address in ("http://host:abc/x.iso", "http://host:80:80/x.iso", "http://host/\x7fx.iso"):
            with self.subTest(address=address):
                tree = f"x.iso:{address}\n{self.files.url('/x.bin')}"
                self.assertTrue(parse_tree(tree)[1])  # the form names the line
                d = URLTreeDriver({"tree": tree})  # saved before the check: the line is left out
                self.assertEqual([e.name for e in d.list("/")], ["x.bin"])

        class Signed(URLTreeDriver):  # an address a subclass makes (123's signed links)
            def _address(self, file, expires=None):
                return "http://host:abc" + urllib.parse.urlsplit(file.url).path

        d = Signed({"tree": self.files.url("/x.bin"), "head_sizes": True})
        self.assertEqual([(e.name, e.size) for e in d.list("/")], [("x.bin", None)])
        with self.assertRaises(DriverError):
            d.open("/x.bin")

    def test_error_answers(self):
        # A short error page is read to its end, so that its connection can be used again; of a long one, 64 KiB.
        taken: dict[str, int] = {}

        def handle(request):
            path = request.url.path
            pieces, size = (3, 100) if path == "/short.bin" else (50, 1024 * 1024)

            def body():
                for _ in range(pieces):
                    taken[path] = taken.get(path, 0) + 1
                    yield b"e" * size
            return httpx.Response(500, headers={"Content-Type": "text/html"}, content=body())

        d = URLTreeDriver({"tree": "short.bin:5:http://files.invalid/short.bin\nlong.bin:5:http://files.invalid/long.bin"})
        d._http.close()
        d._http = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(d.close)
        for name in ("/short.bin", "/long.bin"):
            with self.assertRaises(BackendError):
                d.open(name)
        self.assertEqual(taken.get("/short.bin", 0), 3)
        self.assertLessEqual(taken.get("/long.bin", 0), 2)  # of 50 pieces of 1 MiB


def port(url: str) -> int:
    return urllib.parse.urlsplit(url).port


class TheSitesOwnNetworkTests(unittest.TestCase):
    """http.client(guard=True). Every fake is on 127.0.0.1, so http.public is patched to count the ports of `outside`
    and `other` as public, the Internet; the rest are the site's own network."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.inside = Files({"/secret.json": b'{"AccessKeyId": "inside-only"}', "/lan.bin": b"lan"})
        cls.other = Files({"/y.bin": b"public"})
        targets = {"/x.bin": cls.inside.url("/secret.json"), "/y.bin": cls.other.url("/y.bin")}
        cls.outside = FakeServer(lambda req: Response(302, b"", {"Location": targets[req.path]})).start()
        cls.lan = FakeServer(lambda req: Response(302, b"", {"Location": cls.inside.url("/lan.bin")})).start()

    @classmethod
    def tearDownClass(cls):
        for server in (cls.inside.server, cls.other.server, cls.outside, cls.lan):
            server.close()
        super().tearDownClass()

    def setUp(self):
        self.inside.requests = 0
        public = {port(self.outside.url), port(self.other.server.url)}
        self.real_public = http.public
        patcher = mock.patch.object(http, "public", lambda address, number: number in public)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_redirect_from_the_internet_into_the_sites_network(self):
        d = URLTreeDriver({"tree": f"x.bin:{self.outside.url}/x.bin", "head_sizes": True})
        self.addCleanup(d.close)
        self.assertEqual([(e.name, e.size) for e in d.list("/")], [("x.bin", None)])  # its HEAD refused
        with self.assertRaises(DriverError) as caught:
            d.open("/x.bin")
        self.assertIn("127.0.0.1", caught.exception.message)
        self.assertEqual(self.inside.requests, 0)  # nothing went there

    def test_a_server_of_the_sites_network_redirects_where_it_likes(self):
        d = URLTreeDriver({"tree": f"lan.bin:{self.lan.url}/lan.bin"})
        self.addCleanup(d.close)
        self.assertEqual((d.stat("/lan.bin").size, d.open("/lan.bin").read()), (3, b"lan"))

    def test_a_redirect_from_the_internet_to_the_internet(self):
        d = URLTreeDriver({"tree": f"y.bin:{self.outside.url}/y.bin"})
        self.addCleanup(d.close)
        self.assertEqual((d.stat("/y.bin").size, d.open("/y.bin").read()), (6, b"public"))

    def test_the_server_connected_to_is_checked_too(self):
        # The name said a public address, the connection went to the site's own network (DNS rebinding): refused
        # before anything is sent.
        with mock.patch.object(http, "_addresses", lambda url: (("203.0.113.9",), url.port)), \
                mock.patch.object(http, "public", lambda address, number: address == "203.0.113.9"):
            d = URLTreeDriver({"tree": self.inside.url("/lan.bin")})
            self.addCleanup(d.close)
            with self.assertRaises(DriverError):
                d.open("/lan.bin")
        self.assertEqual(self.inside.requests, 0)

    def test_through_a_proxy_only_the_names_are_checked(self):
        # The connection is the proxy's (it may be on the site's network): only where the names lead is checked.
        with mock.patch.object(http, "_addresses", lambda url: (("203.0.113.9",), url.port)), \
                mock.patch.object(http, "public", lambda address, number: address == "203.0.113.9"):
            d = URLTreeDriver({"tree": self.inside.url("/lan.bin")})
            self.addCleanup(d.close)
            with mock.patch("urllib.request.getproxies", return_value={"http": "http://proxy.example:3128"}), \
                    mock.patch("urllib.request.proxy_bypass", return_value=False):
                self.assertEqual(d.open("/lan.bin").read(), b"lan")

    def test_what_is_public(self):
        for address in ("127.0.0.1", "10.0.0.1", "192.168.1.5", "169.254.169.254", "100.100.100.200", "::1",
                        "fc00::1", "fe80::1%eth0", "::ffff:127.0.0.1", "0.0.0.0", "not an address"):
            with self.subTest(address=address):
                self.assertFalse(self.real_public(address, 80))
        for address in ("8.8.8.8", "2001:4860:4860::8888", "::ffff:8.8.8.8"):
            with self.subTest(address=address):
                self.assertTrue(self.real_public(address, 80))


if __name__ == "__main__":
    unittest.main()
