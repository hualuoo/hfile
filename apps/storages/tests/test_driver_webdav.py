import base64
import contextlib
import datetime
import gzip
import hashlib
import io
import os
import re
import socket
import tempfile
import threading
import time
import unittest
import urllib.parse
import uuid
from unittest import mock

import httpx
from waitress.server import create_server
from wsgidav.fs_dav_provider import FilesystemProvider
from wsgidav.wsgidav_app import WsgiDAVApp

from apps.storages.drivers import driver_class, webdav
from apps.storages.drivers.base import AlreadyExists, AuthError, BackendError, DriverError, Entry, InvalidPath, NotADirectory, NotFound
from apps.storages.drivers.webdav import WebDAVDriver, normalize_url, parse_multistatus, split_listing
from apps.storages.tests.contract import DriverContract, on_this_disk

USER, PASSWORD = "hfile", "s3cret-pw"


class NoCaseProvider(FilesystemProvider):
    """Finds names whatever their case, as IIS or rclone on Windows do; its answers name what was asked for."""

    def _loc_to_file_path(self, path, environ=None):
        found, names = self.root_folder_path, []
        for part in filter(None, path.strip("/").split("/")):
            with contextlib.suppress(OSError):
                part = next((name for name in os.listdir(found) if name.casefold() == part.casefold()), part)
            names.append(part)
            found = os.path.join(found, part)
        return super()._loc_to_file_path("/" + "/".join(names), environ)


class DAVServer:
    """WsgiDAV serving `root` at /dav/ on 127.0.0.1, run by waitress in a thread; `front(app, root)` wraps it;
    `nocase`: it finds names whatever their case."""

    def __init__(self, root, *, digest=False, anonymous=False, front=None, nocase=False):
        users = True if anonymous else {USER: {"password": PASSWORD}}
        config = {
            "host": "127.0.0.1", "port": 0,
            "provider_mapping": {"/dav": NoCaseProvider(root) if nocase else root},
            "simple_dc": {"user_mapping": {"*": users}},
            "http_authenticator": {"accept_basic": not digest, "accept_digest": digest, "default_to_digest": digest},
            "verbose": 0, "logging": {"enable": False}, "dir_browser": {"enable": False},
        }
        self.app = WsgiDAVApp(config) if front is None else front(WsgiDAVApp(config), root)
        self.server = create_server(self.app, host="127.0.0.1", port=0, threads=8, channel_timeout=10,
                                    asyncore_loop_timeout=0.05, ident=None)
        self.port = self.server.effective_port
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()

    def stop(self):
        # The threads serving requests first: one that finishes later wakes the closed server (EBADF, in the log).
        self.server.task_dispatcher.shutdown()
        # Then the sockets, closed by the server's own thread between two select() calls: one closed from here can be
        # in a select() (WinError 10038, in the log). With none left, its loop ends.
        self.server.trigger.pull_trigger(self._close_all)
        self.thread.join(5)

    def _close_all(self):
        for channel in list(self.server._map.values()):  # the server's own socket and the trigger's too
            try:
                channel.close()
            except Exception:  # noqa: BLE001 - teardown
                pass


class ServerMixin:
    server_options: dict = {}
    server_on_this_disk = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.server_root = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="hfile-dav-"))
        cls.dav = DAVServer(cls.server_root, **cls.server_options)
        cls.addClassCleanup(cls.dav.stop)

    def new_folder(self) -> str:
        name = "t-" + uuid.uuid4().hex[:12]
        os.mkdir(os.path.join(self.server_root, name))
        return name

    def config(self, **extra):
        config = {"url": f"http://127.0.0.1:{self.dav.port}/dav/{self.new_folder()}/", "username": USER,
                  "password": PASSWORD, "verify_tls": True}
        config.update(extra)
        return config

    def driver(self, **extra):
        d = WebDAVDriver(self.config(**extra))
        self.addCleanup(d.close)
        return d

    def on_server(self, d, *names):
        folder = d.config["url"].rstrip("/").rsplit("/", 1)[1]
        return os.path.join(self.server_root, folder, *names)


class WebDAVContractTests(ServerMixin, DriverContract, unittest.TestCase):
    def make_driver(self):
        return WebDAVDriver(self.config())


class NoCaseTests(ServerMixin, unittest.TestCase):
    """A server that ignores case: a MOVE to the new spelling of a name finds the entry itself there (412)."""

    server_options = {"nocase": True}

    def test_a_rename_that_changes_only_the_case(self):
        d = self.driver()
        d.write("/readme.txt", io.BytesIO(b"text"), 4)
        d.mkdir("/docs")
        self.assertEqual(d.stat("/README.TXT").size, 4)  # the server finds it under any case
        d.rename("/readme.txt", "README.txt")
        d.rename("/docs", "Docs")
        self.assertEqual(sorted(os.listdir(self.on_server(d))), ["Docs", "README.txt"])
        self.assertEqual(d.open("/README.txt").read(), b"text")
        with self.assertRaises(AlreadyExists):  # another entry's name, in another case
            d.rename("/Docs", "readme.TXT")


class DigestTests(ServerMixin, unittest.TestCase):
    server_options = {"digest": True}

    def test_digest_login(self):
        d = self.driver()
        d.write("/a/b.txt", io.BytesIO(b"digest"), 6)  # the first PROPFIND learns Digest, PUT streams
        self.assertEqual([e.name for e in d.list("/a")], ["b.txt"])
        self.assertEqual(d.open("/a/b.txt", 1, 3).read(), b"ige")
        with self.assertRaises(AlreadyExists):
            d.copy("/a", "/")  # into its own parent: the name is taken
        d.mkdir("/c")
        d.copy("/a/b.txt", "/c")
        self.assertEqual(d.open("/c/b.txt").read(), b"digest")
        with self.assertRaises(AuthError):
            self.driver(password="wrong").list("/")

    def test_contract_writes_over_digest(self):
        d = self.driver()
        data = os.urandom(600000)
        d.write("/big.bin", io.BytesIO(data), len(data))
        self.assertEqual(d.open("/big.bin").read(), data)


class AnonymousTests(ServerMixin, unittest.TestCase):
    server_options = {"anonymous": True}

    def test_no_credentials(self):
        d = self.driver(username="", password="")
        d.write("/x", io.BytesIO(b"x"), 1)
        self.assertEqual(d.open("/x").read(), b"x")


class ApacheLike:
    """In front of WsgiDAV serving anyone, what Apache's stock WebDAV setup (httpd-dav.conf) does first: AuthType
    Digest, whose answer must be for the request's own URI (mod_auth_digest says 400 otherwise); mod_dir's 301 to the
    form with "/" of a folder asked for without it, whatever the method; mod_dav_fs's 400 to a file asked for with "/"."""

    realm, nonce = "DAV-upload", "n0nce-apache"

    def __init__(self, app, root):
        self.app, self.root = app, root
        self.refused = []  # (method, URI, why)

    def _answer(self, start_response, status, why, environ, headers=()):
        self.refused.append((environ["REQUEST_METHOD"], environ["REQUEST_URI"], why))
        start_response(status, [("Content-Length", "0"), *headers])
        return [b""]

    def __call__(self, environ, start_response):
        method, uri = environ["REQUEST_METHOD"], environ["REQUEST_URI"]
        header = environ.pop("HTTP_AUTHORIZATION", "")
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header[7:])) if header.startswith("Digest ") else {}
        if fields and fields.get("uri") != uri:
            return self._answer(start_response, "400 Bad Request", "uri mismatch", environ)
        ha1, ha2 = _md5(f"{USER}:{self.realm}:{PASSWORD}"), _md5(f"{method}:{fields.get('uri')}")
        expected = _md5(f"{ha1}:{self.nonce}:{fields.get('nc')}:{fields.get('cnonce')}:{fields.get('qop')}:{ha2}")
        if fields.get("username") != USER or fields.get("response") != expected:
            challenge = f'Digest realm="{self.realm}", nonce="{self.nonce}", qop="auth", algorithm=MD5'
            return self._answer(start_response, "401 Unauthorized", "login", environ, [("WWW-Authenticate", challenge)])
        path = urllib.parse.unquote(uri.partition("?")[0])
        on_disk = os.path.join(self.root, *[part for part in path.split("/")[2:] if part])  # below /dav/
        if os.path.isdir(on_disk) and not path.endswith("/"):
            location = f"http://{environ['HTTP_HOST']}{uri}/"
            return self._answer(start_response, "301 Moved Permanently", "slash", environ, [("Location", location)])
        if os.path.isfile(on_disk) and path.endswith("/"):
            return self._answer(start_response, "400 Bad Request", "file with a slash", environ)
        return self.app(environ, start_response)


class ApacheLikeTests(ServerMixin, unittest.TestCase):
    server_options = {"anonymous": True, "front": ApacheLike}

    def test_folders_with_digest(self):
        """A folder asked for without "/" got mod_dir's 301, which httpx followed with the Digest answer for the old URI:
        mod_auth_digest said 400, so on Apache's stock setup nothing in a folder below the root could be uploaded,
        moved or deleted. Folders are asked for with "/" now, and a redirect to that form is a new request."""
        d = self.driver()
        d.mkdir("/sub")
        d.write("/sub/a.txt", io.BytesIO(b"a"), 1)
        self.assertTrue(d.stat("/sub").is_dir)
        self.assertEqual([e.name for e in d.list("/sub")], ["a.txt"])
        with self.assertRaises(NotADirectory):
            d.list("/sub/a.txt")  # Apache's 400 to "a.txt/": asked again without "/"
        with self.assertRaises(NotADirectory):
            d.write("/sub/a.txt/b", io.BytesIO(b"b"), 1)
        d.mkdir("/dst")
        d.move("/sub", "/dst")
        d.copy("/dst/sub", "/")
        d.rename("/sub", "sub2")
        d.delete("/dst")
        self.assertEqual([e.name for e in d.list("/")], ["sub2"])
        self.assertEqual(d.open("/sub2/a.txt").read(), b"a")
        self.assertNotIn("uri mismatch", [why for _method, _uri, why in self.dav.app.refused])


class SlowPutAnswers:
    """Answers a PUT two seconds after it has stored the body, as a server behind a proxy that buffers uploads may."""

    def __init__(self, app, root):
        self.app = app

    def __call__(self, environ, start_response):
        result = self.app(environ, start_response)
        if environ["REQUEST_METHOD"] == "PUT":
            result = list(result)  # stored now
            time.sleep(2)
        return result


class SlowPutAnswerTests(ServerMixin, unittest.TestCase):
    server_options = {"front": SlowPutAnswers}

    def test_a_put_answered_after_the_read_timeout(self):
        """The answer to a PUT was waited for as long as any read (http.TIMEOUT, 60 s): an upload the server took longer
        to store after its last byte (a proxy passing a buffered body on, a large file) failed although it was stored."""
        d = self.driver()
        d._client.close()
        d._client = d._make_client(timeout=1.0)  # stands in for http.TIMEOUT, which a PUT's answer may take longer than
        data = os.urandom(300000)
        d.write("/big.bin", io.BytesIO(data), len(data))
        with open(self.on_server(d, "big.bin"), "rb") as f:
            self.assertEqual(f.read(), data)


class Connections:
    """In front of WsgiDAV: the client's port of each request (REMOTE_PORT), which tells its TCP connections apart."""

    def __init__(self, app, root):
        self.app, self.ports = app, []

    def __call__(self, environ, start_response):
        self.ports.append(environ["REMOTE_PORT"])
        return self.app(environ, start_response)


class ConnectionTests(ServerMixin, unittest.TestCase):
    server_options = {"front": Connections}

    def test_one_connection_for_all(self):
        """Closed before the end of its body, even an empty one (a 201's, a 204's), an answer took its connection with
        it: every change, upload and missing name made a new one (with TLS, a new handshake)."""
        d = self.driver()
        d.mkdir("/sub")
        d.write("/sub/a.txt", io.BytesIO(b"a"), 1)
        d.copy("/sub/a.txt", "/")
        d.rename("/a.txt", "b.txt")
        d.move("/b.txt", "/sub")
        with self.assertRaises(NotFound):
            d.stat("/missing.txt")
        with self.assertRaises(NotFound):
            d.open("/missing.txt")
        d.delete("/sub")  # the last: after its 204, which has no Content-Length, waitress ends the connection
        self.assertGreater(len(self.dav.app.ports), 10)
        self.assertEqual(len(set(self.dav.app.ports)), 1)


class WebDAVBehaviourTests(ServerMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.d = self.driver()

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def test_registered_and_params(self):
        self.assertIs(driver_class("webdav"), WebDAVDriver)
        self.assertEqual([p.key for p in WebDAVDriver.params if p.secret], ["password"])

    def test_base_path_confines(self):
        self.put("/x.txt", b"x")
        self.assertTrue(os.path.isfile(self.on_server(self.d, "x.txt")))
        self.assertEqual(self.driver().list("/"), [])

    def test_url_with_encoded_names(self):
        folder = self.new_folder()
        os.mkdir(os.path.join(self.server_root, folder, "中文 目录"))
        url = f"http://127.0.0.1:{self.dav.port}/dav/{folder}/中文 目录"  # raw, no trailing slash
        d = self.driver(url=url)
        d.write("/f.txt", io.BytesIO(b"f"), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.server_root, folder, "中文 目录", "f.txt")))
        encoded = f"http://127.0.0.1:{self.dav.port}/dav/{folder}/%E4%B8%AD%E6%96%87%20%E7%9B%AE%E5%BD%95/"
        self.assertEqual([e.name for e in self.driver(url=encoded).list("/")], ["f.txt"])

    def test_missing_url_folder(self):
        d = self.driver(url=f"http://127.0.0.1:{self.dav.port}/dav/no-such-folder/")
        with self.assertRaises(BackendError):
            d.list("/")
        with self.assertRaises(BackendError):
            d.check()

    def test_wrong_password(self):
        with self.assertRaises(AuthError):
            self.driver(password="wrong").list("/")

    def test_nothing_listening(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        with self.assertRaises(BackendError):
            self.driver(url=f"http://127.0.0.1:{port}/dav/").list("/")

    def test_list_a_file_and_missing(self):
        self.put("/f", b"1")
        with self.assertRaises(NotADirectory):
            self.d.list("/f")
        with self.assertRaises(NotFound):
            self.d.list("/missing")
        with self.assertRaises(NotADirectory):
            self.put("/f/x", b"2")
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/f")

    def test_odd_names(self):
        for name in filter(on_this_disk, ["%2F", "%", "a%20b", "?", "#", "a;b=c", "~", "' \"", "[]", "+", "&amp;"]):
            with self.subTest(name=name):
                self.put("/" + name, b"x")
                self.assertIn(name, [e.name for e in self.d.list("/")])
                self.assertEqual(self.d.open("/" + name).read(), b"x")
                # WsgiDAV 4.4 unquotes the Destination header before parsing it as a URL, so a "?" or
                # ";" in the new name breaks MOVE there; the header we send is right (see
                # FakeServerTests.test_move_and_copy_requests).
                if "?" not in name and ";" not in name:
                    self.d.rename("/" + name, name + "2")
                    name += "2"
                self.d.delete("/" + name)
        self.assertEqual(self.d.list("/"), [])

    def test_failed_upload(self):
        class Broken(io.RawIOBase):
            def __init__(self):
                self.calls = 0

            def read(self, n=-1):
                self.calls += 1
                if self.calls > 2:
                    raise ConnectionResetError("client went away")
                return b"x" * 1000

        with self.assertRaises(ConnectionResetError):
            self.d.write("/up.bin", Broken(), 100000)
        with self.assertRaises(DriverError):
            self.d.write("/short.bin", io.BytesIO(b"abc"), 10)
        self.assertEqual([e.name for e in self.d.list("/")], [])
        self.put("/after.txt", b"the connection pool still works")
        self.assertEqual(self.d.open("/after.txt").read(), b"the connection pool still works")

    def test_early_close_then_more_requests(self):
        self.put("/big", b"z" * 2000000)
        for _ in range(5):
            stream = self.d.open("/big")
            next(iter(stream))
            stream.close()
        self.assertEqual(self.d.open("/big", 1999990).read(), b"z" * 10)

    def test_validation(self):
        self.assertEqual(WebDAVDriver.validate_config(self.config()), {})
        for extra, key in [({"url": ""}, "url"), ({"url": "ftp://x/"}, "url"), ({"url": "http://u:p@h/"}, "url"),
                           ({"url": "http://h/?a=1"}, "url"), ({"username": "a:b"}, "username"),
                           ({"password": "a\nb"}, "password")]:
            with self.subTest(extra=extra):
                self.assertIn(key, WebDAVDriver.validate_config(self.config(**extra)))


# --- against a fake server, for what WsgiDAV doesn't do --------------------------------------------

def multistatus(*responses: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?><D:multistatus xmlns:D="DAV:">'
            + "".join(responses) + "</D:multistatus>").encode()


def response(href, *, folder=False, size=None, modified="Sat, 07 Nov 2026 15:21:03 GMT", missing=False):
    props = "<D:resourcetype>" + ("<D:collection/>" if folder else "") + "</D:resourcetype>"
    if size is not None:
        props += f"<D:getcontentlength>{size}</D:getcontentlength>"
    if modified:
        props += f"<D:getlastmodified>{modified}</D:getlastmodified>"
    extra = ""
    if missing:
        extra = ("<D:propstat><D:prop><D:getcontentlength>999</D:getcontentlength></D:prop>"
                 "<D:status>HTTP/1.1 404 Not Found</D:status></D:propstat>")
    return (f"<D:response><D:href>{href}</D:href><D:propstat><D:prop>{props}</D:prop>"
            f"<D:status>HTTP/1.1 200 OK</D:status></D:propstat>{extra}</D:response>")


def download(status, data, **kwargs):
    """A GET's answer with its body as a connection gives it: httpx reads one made with content= at once, and the
    raw bytes the driver passes on can't be read from it then."""
    return httpx.Response(status, stream=httpx.ByteStream(data), **kwargs)


def _md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


def digest_for_writes(store: dict, challenged: list, base="/remote.php/dav/files/me/"):
    """A server that wants Digest for writes only (Apache's AuthType Digest in <LimitExcept GET PROPFIND>).
    It reads each body first, as servers do: a request can't be sent again for nothing."""
    realm, nonce = "dav", "n0nce-1"

    def answered(request):
        header = request.headers.get("Authorization", "")
        if not header.startswith("Digest "):
            return False
        fields = dict(re.findall(r'(\w+)="?([^",]*)"?', header[7:]))
        ha1, ha2 = _md5(f"{USER}:{realm}:{PASSWORD}"), _md5(f"{request.method}:{fields.get('uri')}")
        expected = _md5(f"{ha1}:{nonce}:{fields.get('nc')}:{fields.get('cnonce')}:{fields.get('qop')}:{ha2}")
        return fields.get("username") == USER and fields.get("response") == expected

    def handler(request):
        # Off the wire, not request.read(): that would keep the body and make a streamed one sendable again.
        body = b"".join(request.stream)
        path = request.url.path
        if request.method == "PROPFIND":
            if path == base or path.endswith("/"):
                return httpx.Response(207, content=multistatus(response(path, folder=True)))
            if path in store:
                return httpx.Response(207, content=multistatus(response(path, size=len(store[path]))))
            return httpx.Response(404)
        if not answered(request):
            challenged.append(request.method)
            return httpx.Response(401, headers={"WWW-Authenticate": f'Digest realm="{realm}", nonce="{nonce}", qop="auth", algorithm=MD5'})
        if request.method == "PUT":
            store[path] = body
        return httpx.Response(201)

    return handler


class _WireTransport(httpx.BaseTransport):
    """httpx.MockTransport without its request.read(): that keeps every body in memory and so makes a
    streamed one sendable twice, which it isn't over a connection (the handler reads request.stream)."""

    def __init__(self, handler):
        self.handler = handler

    def handle_request(self, request):
        return self.handler(request)


class _Unseekable(io.RawIOBase):
    def __init__(self, data: bytes):
        self._data = io.BytesIO(data)

    def readable(self):
        return True

    def read(self, size=-1):
        return self._data.read(size)


class FakeServerTests(unittest.TestCase):
    def driver(self, handler, url="https://dav.example.com/remote.php/dav/files/me/"):
        d = WebDAVDriver({"url": url, "username": USER, "password": PASSWORD})
        d._client.close()
        d._client = d._make_client(transport=httpx.MockTransport(handler))
        self.addCleanup(d.close)
        return d

    def test_hrefs_in_any_form(self):
        body = multistatus(
            response("https://dav.example.com/remote.php/dav/files/me/a%20b/", folder=True),
            response("/remote.php/dav/files/me/a%20b/%E4%B8%AD%E6%96%87.txt", size=3),
            response("/remote.php/dav/files/me/a%20b/sub%2fname", size=1),  # a "/" in a name: left out
            response("http://other-host/remote.php/dav/files/me/a%20b/inner/", folder=True),
            response("/remote.php/dav/files/me/a%20b/inner/deeper.txt", size=1),  # not a child: ignored
            response("/remote.php/dav/files/me/a%20b/%7Etilde", size=2, missing=True),
            response("/remote.php/dav/files/me/a%20b/latin1-%E9.txt", size=1),  # not UTF-8: left out
        )

        def handler(request):
            self.assertEqual(request.method, "PROPFIND")
            self.assertEqual(request.url.raw_path, b"/remote.php/dav/files/me/a%20b/")  # a folder: with "/"
            self.assertEqual(request.headers["Depth"], "1")
            self.assertTrue(request.headers["Authorization"].startswith("Basic "))
            return httpx.Response(207, content=body)

        entries = {e.name: e for e in self.driver(handler).list("/a b")}
        self.assertEqual(sorted(entries), ["inner", "~tilde", "中文.txt"])
        self.assertEqual(entries["~tilde"].size, 2)  # the 404 propstat doesn't count
        self.assertTrue(entries["inner"].is_dir)
        self.assertEqual(entries["中文.txt"].path, "/a b/中文.txt")
        self.assertEqual(entries["中文.txt"].modified, datetime.datetime(2026, 11, 7, 15, 21, 3, tzinfo=datetime.timezone.utc))

    def test_proxy_that_rewrites_paths(self):
        body = multistatus(response("/d/", folder=True), response("/d/x.txt", size=1), response("/d/y/", folder=True))
        d = self.driver(lambda request: httpx.Response(207, content=body))
        self.assertEqual(sorted(e.name for e in d.list("/d")), ["x.txt", "y"])

    def test_xml_with_entities_is_refused(self):
        evil = (b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;">]>'
                b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>&b;</D:href></D:response></D:multistatus>')
        external = (b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
                    b'<D:multistatus xmlns:D="DAV:"><D:response><D:href>&e;</D:href></D:response></D:multistatus>')
        unknown = b'<?xml version="1.0" encoding="x-no-such"?><D:multistatus xmlns:D="DAV:"/>'  # STOR-04: a 500
        for body in (evil, external, b"<not-closed", b"plain text", unknown):
            with self.subTest(body=body[:30]):
                d = self.driver(lambda request, body=body: httpx.Response(207, content=body))
                with self.assertRaises(BackendError):
                    d.list("/")

    def test_parser_without_namespace_and_times(self):
        body = (b'<multistatus><response><href>/x/</href><propstat><prop><resourcetype><collection/></resourcetype>'
                b'<getlastmodified>2026-11-07T15:21:03Z</getlastmodified></prop></propstat></response></multistatus>')
        resources = parse_multistatus([body[:20], body[20:]])
        self.assertEqual(len(resources), 1)
        self.assertTrue(resources[0].is_dir)
        self.assertEqual(resources[0].modified.year, 2026)
        itself, children = split_listing(resources, "http://h/x/")
        self.assertIs(itself, resources[0])
        self.assertEqual(children, {})

    def test_range_ignored_by_server(self):
        data = bytes(range(256)) * 40

        def handler(request):
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response("/remote.php/dav/files/me/f", size=len(data))))
            self.assertEqual(request.headers["Range"], "bytes=100-149")
            self.assertEqual(request.headers["Accept-Encoding"], "identity")
            return download(200, data)  # Range ignored

        stream = self.driver(handler).open("/f", 100, 50)
        self.assertEqual((stream.offset, stream.size, stream.total), (100, 50, len(data)))
        self.assertEqual(stream.read(), data[100:150])

    def test_wrong_range_answer(self):
        def handler(request):
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response("/remote.php/dav/files/me/f", size=100)))
            return httpx.Response(206, content=b"x", headers={"Content-Range": "bytes 0-0/100"})

        with self.assertRaises(BackendError):
            self.driver(handler).open("/f", 50, 1)

    def test_redirects(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.host, request.headers.get("Authorization")))
            if request.url.host == "cdn.example.net":
                return download(200, b"from the cdn")
            if request.method == "PROPFIND":
                if request.url.path.endswith("/old"):
                    return httpx.Response(301, headers={"Location": "/remote.php/dav/files/me/new"})
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=12)))
            return httpx.Response(302, headers={"Location": "https://cdn.example.net/signed/f?token=t"})

        d = self.driver(handler)
        self.assertEqual(d.stat("/old").size, 12)  # same origin: followed with credentials
        self.assertEqual(d.open("/f").read(), b"from the cdn")
        cdn = [s for s in seen if s[1] == "cdn.example.net"]
        self.assertEqual(cdn, [("GET", "cdn.example.net", None)])  # no credentials to another origin
        self.assertTrue(all(s[2] for s in seen if s[1] == "dav.example.com"))

    def test_no_redirects_for_changes(self):
        d = self.driver(lambda request: httpx.Response(301, headers={"Location": "https://evil.example/"}))
        with self.assertRaises(DriverError):
            d.mkdir("/x")

    def test_status_errors(self):
        for status, error, text in [(401, AuthError, ""), (403, DriverError, "403"), (423, DriverError, "锁定"),
                                    (507, DriverError, "空间"), (500, BackendError, "500"), (502, BackendError, "502")]:
            with self.subTest(status=status):
                d = self.driver(lambda request, status=status: httpx.Response(status))
                with self.assertRaises(error) as caught:
                    d.list("/")
                self.assertIn(text, str(caught.exception))
                self.assertNotIn(PASSWORD, str(caught.exception))

    def test_move_and_copy_requests(self):
        seen = []

        def handler(request):
            seen.append((request.method, request.url.raw_path.decode(), dict(request.headers)))
            if request.method == "PROPFIND":
                path = request.url.path
                if path.endswith("/dst/a b.txt"):
                    return httpx.Response(404)
                folder = path.endswith(("/dst/", "/me/"))
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), folder=folder, size=1)))
            return httpx.Response(201)

        d = self.driver(handler)
        d.move("/a b.txt", "/dst")
        d.copy("/a b.txt", "/dst")
        moves = [s for s in seen if s[0] in ("MOVE", "COPY")]
        self.assertEqual([m[0] for m in moves], ["MOVE", "COPY"])
        for method, raw_path, headers in moves:
            self.assertEqual(raw_path, "/remote.php/dav/files/me/a%20b.txt")
            self.assertEqual(headers["destination"], "https://dav.example.com/remote.php/dav/files/me/dst/a%20b.txt")
            self.assertEqual(headers["overwrite"], "F")
        self.assertEqual(moves[1][2]["depth"], "infinity")

    def test_names_are_one_segment(self):
        d = self.driver(lambda request: httpx.Response(404))
        self.assertEqual(d._url("/a?b/c#d/100%"), "https://dav.example.com/remote.php/dav/files/me/a%3Fb/c%23d/100%25")
        with self.assertRaises(InvalidPath):
            d._url("/../x")
        with self.assertRaises(InvalidPath):
            d.rename("/x", "a/b")

    def test_backslashes(self):
        body = multistatus(response("/remote.php/dav/files/me/", folder=True),
                           response("/remote.php/dav/files/me/a%5Cb.txt", size=1),
                           response("/remote.php/dav/files/me/ok.txt", size=1))
        d = self.driver(lambda request: httpx.Response(207, content=body))
        self.assertEqual([e.name for e in d.list("/")], ["ok.txt"])
        for bad in ["/a\\..\\..\\x", "/x\\y"]:
            with self.subTest(path=bad), self.assertRaises(InvalidPath):
                d.stat(bad)

    def test_names_of_dots_and_spaces_only(self):
        body = multistatus(response("/remote.php/dav/files/me/", folder=True),
                           response("/remote.php/dav/files/me/.../", folder=True),
                           response("/remote.php/dav/files/me/..%20", size=1),
                           response("/remote.php/dav/files/me/ok.txt", size=1))
        d = self.driver(lambda request: httpx.Response(207, content=body))
        self.assertEqual([e.name for e in d.list("/")], ["ok.txt"])

    def test_odd_times_and_paths(self):
        body = multistatus(
            response("/remote.php/dav/files/me/%FF/", folder=True),
            response("/remote.php/dav/files/me/%FF/far.txt", size=1, modified="Sat, 07 Nov 99999999 15:21:03 GMT"),
            response("/remote.php/dav/files/me/%FF/iso.txt", size=1, modified="99999-11-07T15:21:03Z"))

        def handler(request):
            if request.url.raw_path.rstrip(b"/").endswith(b"/me/x"):  # redirected to a path that isn't UTF-8
                return httpx.Response(301, headers={"Location": "/remote.php/dav/files/me/%FF/"})
            return httpx.Response(207, content=body)

        entries = {e.name: e for e in self.driver(handler).list("/x")}
        self.assertEqual(sorted(entries), ["far.txt", "iso.txt"])
        self.assertIsNone(entries["far.txt"].modified)
        self.assertIsNone(entries["iso.txt"].modified)

    def test_digest_challenge_from_elsewhere_is_ignored(self):
        seen = []

        def handler(request):
            seen.append((request.url.host, request.headers.get("Authorization", "")))
            if request.url.host == "cdn.example.net":
                return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'})
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=3)))
            return httpx.Response(302, headers={"Location": "https://cdn.example.net/f"})

        d = self.driver(handler)
        with self.assertRaises(AuthError):
            d.open("/f").read()
        d.stat("/f")
        self.assertFalse(d._auth._use_digest)
        self.assertTrue(seen[-1][1].startswith("Basic "))

    def wire_driver(self, handler):
        d = self.driver(lambda request: httpx.Response(500))
        d._client.close()
        d._client = d._make_client(transport=_WireTransport(handler))
        return d

    def test_digest_for_writes_only(self):
        """STOR-08: the first upload made the server ask for Digest and failed (as a wrong password), the
        second asked again and failed too. Now the first is sent again, the rest answer from the start."""
        store, challenged = {}, []
        d = self.wire_driver(digest_for_writes(store, challenged))
        d.write("/a.txt", io.BytesIO(b"hello"), 5)
        d.write("/b.txt", io.BytesIO(b"world"), 5)
        d.mkdir("/c")
        base = "/remote.php/dav/files/me/"
        self.assertEqual(store, {base + "a.txt": b"hello", base + "b.txt": b"world"})
        self.assertEqual(challenged, ["PUT"])
        self.assertTrue(d._auth.digest)

    def test_digest_for_writes_only_with_data_read_once(self):
        """Data that can't be read again: an honest message, and the next upload goes through."""
        store, challenged = {}, []
        d = self.wire_driver(digest_for_writes(store, challenged))
        with self.assertRaises(BackendError) as caught:
            d.write("/a.txt", _Unseekable(b"hello"), 5)
        self.assertIn("再上传一次", caught.exception.message)
        d.write("/a.txt", _Unseekable(b"hello"), 5)
        self.assertEqual(store, {"/remote.php/dav/files/me/a.txt": b"hello"})

    def test_ntlm_only(self):
        """STOR-10: a server that only takes Windows logins isn't told as a wrong user name and password."""
        d = self.driver(lambda request: httpx.Response(401, headers=[("WWW-Authenticate", "NTLM"),
                                                                     ("WWW-Authenticate", "Negotiate")]))
        with self.assertRaises(AuthError) as caught:
            d.list("/")
        self.assertIn("NTLM", caught.exception.message)

    def test_basic_header(self):
        d = self.driver(lambda request: httpx.Response(404))
        token = base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
        self.assertEqual(d._auth._basic, f"Basic {token}")

    def test_bodies_of_redirects_and_401s_are_read_only_a_little(self):
        """SEC-10: httpx reads the whole body of a redirect it follows and of the 401 a Digest login answers,
        with no limit; a server that isn't to be trusted could send any amount. Up to 64 KiB is read."""
        pulled = []

        class Endless(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(100):  # 100 MiB, if all of it were read
                    pulled.append(1)
                    yield b"x" * (1024 * 1024)

        def handler(request):
            path = request.url.path
            if path.endswith("/old"):
                return httpx.Response(301, headers={"Location": "/remote.php/dav/files/me/new"}, stream=Endless())
            if path.endswith("/locked") and not request.headers.get("Authorization", "").startswith("Digest "):
                return httpx.Response(401, headers={"WWW-Authenticate": 'Digest realm="x", nonce="n", qop="auth"'},
                                      stream=Endless())
            return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=1)))

        d = self.driver(handler)
        self.assertEqual(d.stat("/old").size, 1)
        self.assertEqual(len(pulled), 1)
        self.assertEqual(d.stat("/locked").size, 1)
        self.assertTrue(d._auth.digest)
        self.assertEqual(len(pulled), 2)

    def test_a_read_goes_again_when_the_connection_drops(self):
        """STOR-09: a connection that closed before the answer came (a kept-alive one the server had closed)
        failed the download or the listing at once. A read goes again, once; a change doesn't."""
        calls = []

        def handler(request):
            calls.append(request.method)
            if len(calls) % 2:
                raise httpx.RemoteProtocolError("Server disconnected without sending a response.")
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=2)))
            return download(200, b"ok")

        d = self.driver(handler)
        self.assertEqual(d.stat("/f").size, 2)
        self.assertEqual(d.open("/f").read(), b"ok")
        self.assertEqual(calls, ["PROPFIND"] * 2 + ["PROPFIND"] * 2 + ["GET"] * 2)  # open() asks what it is first
        calls.clear()
        with self.assertRaises(BackendError):
            d.mkdir("/x")
        self.assertEqual(calls, ["MKCOL"])

        def always_dropped(request):
            calls.append(request.method)
            raise httpx.ReadError("Connection reset by peer")

        calls.clear()
        with self.assertRaises(BackendError):
            self.driver(always_dropped).stat("/f")
        self.assertEqual(calls, ["PROPFIND"] * 2)  # once more, not more

    def test_cst_is_china_standard_time(self):
        """STOR-11: email.utils takes CST for US Central Time (UTC-6), 14 hours off what the servers that write it
        mean: China Standard Time (UTC+8), Java's name for Asia/Shanghai."""
        for written, utc in (("Fri, 13 Nov 2026 10:00:00 CST", datetime.datetime(2026, 11, 13, 2)),
                             ("Fri, 13 Nov 2026 10:00:00 cst", datetime.datetime(2026, 11, 13, 2)),
                             ("Fri, 13 Nov 2026 10:00:00 GMT", datetime.datetime(2026, 11, 13, 10)),
                             ("Fri, 13 Nov 2026 10:00:00 CDT", datetime.datetime(2026, 11, 13, 15))):  # US Central only
            with self.subTest(written=written):
                body = multistatus(response("/x", size=1, modified=written))
                modified = parse_multistatus([body])[0].modified
                self.assertEqual(modified, utc.replace(tzinfo=datetime.timezone.utc))

    # -- listings in pages (Jianguoyun)

    def paged(self, names, folder, link, seen):
        """A server that lists `folder` 749 names a page (Jianguoyun's 750 entries, the folder itself one of them), the
        next page in `link(page)`'s URL."""
        def handler(request):
            seen.append((request.method, request.url.raw_path, request.headers.get("Depth"), request.read()))
            page = int(request.url.params.get("page", "1"))
            headers = {"Link": f'<{link(page + 1)}>; rel="next"'} if page * 749 < len(names) else {}
            entries = [response(folder + urllib.parse.quote(name), size=1) for name in names[(page - 1) * 749:page * 749]]
            return httpx.Response(207, headers=headers, content=multistatus(response(folder, folder=True), *entries))
        return handler

    def test_a_listing_in_pages(self):
        """Jianguoyun gives at most 750 entries a PROPFIND, the rest on further pages that "Link: <url>; rel="next""
        points to: a folder of 2000 photos showed 749. Its next page may be named with its path encoded twice, which
        the clients made for it decode once; the requests go to the folder's own URL with the link's query."""
        names = [f"photo{i:04d}.jpg" for i in range(2000)]
        folder = "/remote.php/dav/files/me/%E7%9B%B8%E5%86%8C/"  # 相册
        seen = []

        def link(page):
            path = folder if page == 2 else folder.replace("%", "%25")
            return f"https://dav.example.com{path}?page={page}"

        entries = self.driver(self.paged(names, folder, link, seen)).list("/相册")
        self.assertEqual(sorted(e.name for e in entries), names)
        self.assertEqual([s[1] for s in seen], [folder.encode(), f"{folder}?page=2".encode(), f"{folder}?page=3".encode()])
        self.assertEqual({(s[0], s[2], s[3]) for s in seen}, {("PROPFIND", "1", seen[0][3])})

    def test_pages_of_a_listing_go_to_its_origin_only(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            return httpx.Response(207, headers={"Link": '<https://evil.example/me/?page=2>; rel="next"'},
                                  content=multistatus(response("/remote.php/dav/files/me/", folder=True),
                                                      response("/remote.php/dav/files/me/a", size=1)))

        with self.assertRaises(BackendError):
            self.driver(handler).list("/")
        self.assertEqual(hosts, ["dav.example.com"])

    def test_a_listing_with_a_page_missing_fails(self):
        names = [f"f{i}" for i in range(1000)]
        folder = "/remote.php/dav/files/me/"
        seen = []
        pages = self.paged(names, folder, lambda page: f"{folder}?page={page}", seen)
        with self.assertRaises(BackendError):
            self.driver(lambda request: httpx.Response(503) if request.url.params else pages(request)).list("/")

    def test_pages_of_a_listing_are_limited(self):
        folder = "/remote.php/dav/files/me/"
        names = [f"f{i}" for i in range(749 * 10)]
        seen = []
        handler = self.paged(names, folder, lambda page: f"{folder}?page={page}", seen)
        with mock.patch.object(webdav, "MAX_PAGES", 3), self.assertRaises(BackendError):
            self.driver(handler).list("/")
        self.assertEqual(len(seen), 3)
        seen.clear()
        page = len(multistatus(response(folder, folder=True), *(response(folder + name, size=1) for name in names[:749])))
        with mock.patch.object(webdav, "MAX_LISTING", page * 3 // 2), self.assertRaises(BackendError):
            self.driver(handler).list("/")  # the limit is for all the pages together
        self.assertEqual(len(seen), 2)

    # -- redirects

    def test_a_folder_redirected_to_http_behind_a_tls_proxy(self):
        """Behind a proxy that does TLS, Apache's 301 for a folder asked for without "/" names http://: httpx didn't send
        the credentials there, and the 401 at the end read as a wrong password."""
        seen = []

        def handler(request):
            seen.append(str(request.url))
            if request.url.scheme == "http":
                return httpx.Response(301, headers={"Location": str(request.url.copy_with(scheme="https"))})
            if not request.headers.get("Authorization"):
                return httpx.Response(401, headers={"WWW-Authenticate": 'Basic realm="dav"'})
            if request.url.path == "/remote.php/dav/files/me/sub":
                return httpx.Response(301, headers={"Location": "http://dav.example.com/remote.php/dav/files/me/sub/"})
            return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), folder=True)))

        self.assertTrue(self.driver(handler).stat("/sub").is_dir)
        self.assertEqual(seen, ["https://dav.example.com/remote.php/dav/files/me/sub",
                                "https://dav.example.com/remote.php/dav/files/me/sub/"])

    def test_a_redirect_keeps_propfind(self):
        """httpx makes a 302 or 303 a GET."""
        for status in (302, 303, 307):
            methods = []

            def handler(request, status=status):
                methods.append(request.method)
                if request.url.path.endswith("/old"):
                    return httpx.Response(status, headers={"Location": "/remote.php/dav/files/me/new"})
                if request.method != "PROPFIND" or request.read() != webdav._PROPFIND:
                    return httpx.Response(405)
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=3)))

            with self.subTest(status=status):
                self.assertEqual(self.driver(handler).stat("/old").size, 3)
                self.assertEqual(methods, ["PROPFIND", "PROPFIND"])

    def test_a_redirect_to_another_origin_is_reported(self):
        hosts = []

        def handler(request):
            hosts.append(request.url.host)
            if request.url.host == "other.example":
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=1)))
            return httpx.Response(301, headers={"Location": "https://other.example/remote.php/dav/files/me/x"})

        with self.assertRaises(BackendError) as caught:
            self.driver(handler).stat("/x")
        self.assertEqual(hosts, ["dav.example.com"])
        self.assertIn("https://other.example/remote.php/dav/files/me/x", caught.exception.message)  # where to look

    # -- requests

    def test_destination_names_the_host_as_it_is_sent(self):
        """MOVE's and COPY's Destination had the host as typed, the Host header has it as httpx sends it (lowercase,
        IDNA, no default port), and servers compare the two: x/net/webdav (rclone, Caddy, SFTPGo) and WsgiDAV answer
        502, nginx 400. A host in Chinese made MOVE a UnicodeEncodeError, a 500."""
        for url, host in (("https://DAV.Example.COM:443/dav/", "dav.example.com"),
                          ("https://网盘.例子.中国/dav/", "xn--vzyr4p.xn--fsqu00a.xn--fiqs8s")):
            seen = []

            def handler(request):
                seen.append((request.method, request.headers.get("Host"), request.headers.get("Destination")))
                if request.method != "PROPFIND":
                    return httpx.Response(201)
                if request.url.path.endswith("/dst/a.txt"):
                    return httpx.Response(404)
                folder = request.url.path.endswith(("/dst/", "/dav/"))
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), folder=folder, size=1)))

            with self.subTest(url=url):
                self.driver(handler, url=url).move("/a.txt", "/dst")
                self.assertEqual([s[1:] for s in seen if s[0] == "MOVE"], [(host, f"https://{host}/dav/dst/a.txt")])

    def test_the_source_is_asked_for_once(self):
        """Rename, move and copy asked the server what the source is twice (Jianguoyun allows 600 requests in 30
        minutes)."""
        seen = []

        def handler(request):
            seen.append((request.method, request.url.path))
            if request.method != "PROPFIND":
                return httpx.Response(201)
            if request.url.path.endswith(("/dst/a.txt", "/b.txt")):
                return httpx.Response(404)
            folder = request.url.path.endswith("/dst/")
            return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), folder=folder, size=1)))

        d = self.driver(handler)
        for operation in (lambda: d.rename("/a.txt", "b.txt"), lambda: d.move("/a.txt", "/dst"),
                          lambda: d.copy("/a.txt", "/dst")):
            seen.clear()
            operation()
            self.assertEqual(seen.count(("PROPFIND", "/remote.php/dav/files/me/a.txt")), 1)

    def test_open_known_asks_only_for_the_bytes(self):
        """open() asked the server what the file is, though the site had just done so for a ranged download: one
        request more of Jianguoyun's 600 in 30 minutes. open_known() uses the entry the site has."""
        seen = []

        def handler(request):
            seen.append(request.method)
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), size=10)))
            return download(206, b"2345", headers={"Content-Range": "bytes 2-5/10"})

        d = self.driver(handler)
        entry = d.stat("/a.txt")
        seen.clear()
        stream = d.open_known("/a.txt", entry, 2, 4)
        self.assertEqual((stream.read(), stream.total, stream.modified), (b"2345", 10, entry.modified))
        self.assertEqual(seen, ["GET"])
        with self.assertRaises(NotFound):
            d.open_known("/folder", Entry("folder", "/folder", True), 0, None)  # a folder, as for open()
        seen.clear()
        self.assertEqual(d.open("/a.txt", 2, 4).read(), b"2345")
        self.assertEqual(seen, ["PROPFIND", "GET"])  # without an entry, as before

    def test_upload_refusals(self):
        """After write() found the folder there and no folder in the file's place, a 405 to the PUT said a folder of that
        name was there (nginx says 405 when PUT isn't in dav_methods), and a 404 that the file didn't exist (IIS says 404
        to a body over its maxAllowedContentLength, 30,000,000 bytes by default)."""
        for status, wrong, said in ((405, AlreadyExists, "405"), (404, NotFound, "maxAllowedContentLength")):
            def handler(request, status=status):
                if request.method != "PROPFIND":
                    return httpx.Response(status)
                if request.url.path.endswith("/new.bin"):
                    return httpx.Response(404)
                return httpx.Response(207, content=multistatus(response(request.url.raw_path.decode(), folder=True)))

            with self.subTest(status=status):
                with self.assertRaises(DriverError) as caught:
                    self.driver(handler).write("/new.bin", io.BytesIO(b"x"), 1)
                self.assertNotIsInstance(caught.exception, wrong)
                self.assertIn(said, caught.exception.message)

    def test_a_stored_gz_served_with_content_encoding(self):
        """Apache's AddEncoding gzip .gz (nginx adding the header for .svgz) labels a stored file "Content-Encoding:
        gzip": httpx unpacked it, and the download was the start of the archive's contents, cut to the file's size; a
        part of it failed to unpack."""
        stored = gzip.compress(os.urandom(3000) + b"A" * 50000)

        def handler(request):
            if request.method == "PROPFIND":
                return httpx.Response(207, content=multistatus(response("/remote.php/dav/files/me/b.tar.gz", size=len(stored))))
            if "Range" not in request.headers:
                return download(200, stored, headers={"Content-Encoding": "gzip"})
            start, end = (int(n) for n in request.headers["Range"].removeprefix("bytes=").split("-"))
            return download(206, stored[start:end + 1], headers={
                "Content-Encoding": "gzip", "Content-Range": f"bytes {start}-{end}/{len(stored)}"})

        d = self.driver(handler)
        self.assertEqual(d.open("/b.tar.gz").read(), stored)
        self.assertEqual(d.open("/b.tar.gz", 100, 50).read(), stored[100:150])

    def test_hrefs_with_question_marks_and_hashes_as_they_are(self):
        """Old nginx-dav-ext (and Stalwart before 0.16.19) don't encode "?" and "#" of names in hrefs: "C# notes.txt"
        was listed as "C"."""
        names = ["C# notes.txt", "Issue #12.pdf", "what?.txt", "a?b#c"]
        body = multistatus(response("/remote.php/dav/files/me/", folder=True),
                           *(response("/remote.php/dav/files/me/" + name, size=1) for name in names))
        d = self.driver(lambda request: httpx.Response(207, content=body))
        self.assertEqual(sorted(e.name for e in d.list("/")), sorted(names))


class URLTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_url("https://h/dav"), "https://h/dav/")
        self.assertEqual(normalize_url(" HTTP://h:8080/a%20b/ "), "http://h:8080/a%20b/")
        self.assertEqual(normalize_url("https://h/中 文/"), "https://h/%E4%B8%AD%20%E6%96%87/")
        self.assertEqual(normalize_url("https://h"), "https://h/")
        # The host as httpx sends it: MOVE's Destination must name it so.
        self.assertEqual(normalize_url("https://NAS.Example.COM:443/dav"), "https://nas.example.com/dav/")
        self.assertEqual(normalize_url("http://nas:80/"), "http://nas/")
        self.assertEqual(normalize_url("https://例子.中国:8443/"), "https://xn--fsqu00a.xn--fiqs8s:8443/")
        self.assertEqual(normalize_url("http://[::1]:5005/dav/"), "http://[::1]:5005/dav/")
        for bad in ["", "dav.example.com/x", "ftp://h/", "https://u:p@h/", "https://h/?a", "https://h/#a",
                    "https://h:0x/", "https://h/a/../b", "https://h/a\r\nb", "https:///x", "https://☃️.example/"]:
            with self.subTest(url=bad), self.assertRaises(ValueError):
                normalize_url(bad)


if __name__ == "__main__":
    unittest.main()
