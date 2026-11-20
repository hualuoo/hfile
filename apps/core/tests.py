"""core: the access log and the secrets it masks, client addresses and rate limits, the request size
limit, charsets (and the names of unknown ones, which Python doesn't keep) and bad JSON, errors in
Chinese, uploads and static files; and the report of a subtest that fails in a worker of --parallel."""

import datetime
import encodings
import json
import logging
import pickle
import secrets
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest import mock
from wsgiref.util import setup_testing_defaults
from xml.parsers import expat

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.handlers.wsgi import WSGIHandler
from django.core.signals import request_finished, request_started
from django.db import close_old_connections
from django.templatetags.static import static
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from rest_framework.test import APITestCase

from . import masking, throttles, uploads
from .access_log import AccessLog, size, wrapped
from .middleware import head_without_body, utf8_only
from .net import address_of, client_ip
from .testing import TO_THE_SECOND, TempMediaMixin, _FreshCacheRemoteResult, symlink

User = get_user_model()


def site(status="200 OK", length="2458"):
    """A WSGI app that answers `status`."""

    def app(environ, start_response):
        start_response(status, [("Content-Type", "text/html"), ("Content-Length", length)])
        return [b"x"]

    return app


def request(path="/", method="GET", query="", **headers):
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "REMOTE_ADDR": "203.0.113.5",
        "HTTP_USER_AGENT": "python-requests/2.32.3",
    }
    environ.update(headers)
    return environ


def wsgi(text):
    """`text` as a WSGI server hands it over: the UTF-8 bytes as latin-1."""
    return text.encode("utf-8").decode("latin-1")


def no_start(status, headers, exc_info=None):
    return None


def through_wsgi(app, path, *, method="GET", query="", content_type="", body=b"", **meta):
    """(status, headers, body) of a request through a WSGI app, as a server would hand it over; `meta`: more of
    the environ (HTTP_ACCEPT=…)."""
    environ = {"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "HTTP_HOST": "testserver",
               "CONTENT_TYPE": content_type, "CONTENT_LENGTH": str(len(body)), "wsgi.input": BytesIO(body), **meta}
    setup_testing_defaults(environ)
    answer = {}

    def start_response(status, headers, exc_info=None):
        answer.update(status=int(status[:3]), headers=dict(headers))

    chunks = app(environ, start_response)
    data = b"".join(chunks)
    if hasattr(chunks, "close"):
        chunks.close()
    return answer["status"], answer["headers"], data


class NoCloseMixin:
    """A request through WSGIHandler closes the database connections on its way in and out: not in a
    test, as Django's own test client doesn't (MySQL's would be gone; SQLite's in memory stays)."""

    def setUp(self):
        super().setUp()
        for signal in (request_started, request_finished):
            signal.disconnect(close_old_connections)
            self.addCleanup(signal.connect, close_old_connections)


class AccessLogTests(SimpleTestCase):
    def line(self, app, **kwargs):
        with self.assertLogs("access", "INFO") as logs:
            app(request(**kwargs), no_start)
        self.assertEqual(len(logs.records), 1)
        return logs.records[0].getMessage()

    def test_a_line_per_request(self):
        line = self.line(AccessLog(site("403 Forbidden")), method="POST")
        self.assertRegex(line, r'^403 \|\s+\d+\.\dms \|\s+203\.0\.113\.5 \| POST\s+"/" \|\s+2\.4KB \| "python-requests/2\.32\.3"$')
        line = self.line(AccessLog(site()), path="/manage/", query="tab=3", HTTP_USER_AGENT="")
        self.assertRegex(line, r'^200 \|.*\| GET\s+"/manage/\?tab=3" \|\s+2\.4KB \| -$')

    def test_static_files_and_uploads_too(self):
        for path in ("/static/web/js/common.js", "/media/site/a.png", "/favicon.ico"):
            for status in ("200 OK", "304 Not Modified"):
                with self.subTest(path=path, status=status):
                    self.assertIn(f'"{path}"', self.line(AccessLog(site(status)), path=path))

    def test_the_visitor_behind_a_proxy(self):
        forwarded = {"REMOTE_ADDR": "127.0.0.1", "HTTP_X_FORWARDED_FOR": "10.9.9.9, 198.51.100.23"}
        self.assertRegex(self.line(AccessLog(site()), **forwarded), r"\|\s+127\.0\.0\.1 \|")  # proxies = 0: made up
        with override_settings(REST_FRAMEWORK={**settings.REST_FRAMEWORK, "NUM_PROXIES": 1}):
            self.assertRegex(self.line(AccessLog(site()), **forwarded), r"\|\s+198\.51\.100\.23 \|")

    @override_settings(ADMIN_PATH="s3cret-admin")
    def test_secrets_stay_out(self):
        app = AccessLog(site())
        for path, query, shown in [
            ("/s3cret-admin/login/", "", '"/<admin>/login/"'),
            ("/s3cret-admin", "", '"/<admin>"'),
            ("/s3cret-admin/login/", "next=/s3cret-admin/", '"/<admin>/login/?next=/<admin>/"'),  # Django's redirect
            ("/s3cret-admin/login/", "next=%2Fs3cret-admin%2Faccounts%2F", '"/<admin>/login/?next=/<admin>/accounts/"'),
            ("/install/", "token=ABCD-EFGH-JKLM-NPQR", '"/install/?token=<token>"'),
            ("/x/", "a=1&Sign=9f8e7d&b=2", '"/x/?a=1&Sign=<sign>&b=2"'),
            ("/sso/callback/", "code=Zm9vYmFy&state=c2VjcmV0", '"/sso/callback/?code=<code>&state=<state>"'),
            ("/x/", "password=hunter2&pwd=hunter3", '"/x/?password=<password>&pwd=<pwd>"'),
            # An encoded & in a value can't hide part of it, nor can an encoded name.
            ("/x/", "token=Zm9v%26YmFy", '"/x/?token=<token>"'),
            ("/x/", "%74oken=Zm9vYmFy", '"/x/?token=<token>"'),
            # A secret inside another parameter, as it is decoded.
            ("/", "auth=login&next=/install/%3Ftoken%3DZm9vYmFy", '"/?auth=login&next=/install/?token=<token>"'),
            ("/s3cret-adminx/", "", '"/s3cret-adminx/"'),  # another address
            ("/x/", "tokens=1&my_token=2", '"/x/?tokens=1&my_token=2"'),  # other names
        ]:
            with self.subTest(path=path, query=query):
                line = self.line(app, path=path, query=query)
                self.assertIn(shown, line)
                self.assertNotRegex(line, r"Zm9v|YmFy|c2VjcmV0|ABCD|hunter|9f8e7d|s3cret-admin[/?\"]")

    def test_apps_register_their_own(self):
        masking.register_path(r"/s/(?P<secret>[^/?&#]+)", "key")
        masking.register_query("x-signature")
        self.addCleanup(masking._paths.pop)
        self.addCleanup(masking.register_query)  # then the pattern again, without it
        self.addCleanup(masking._queries.discard, "x-signature")
        line = self.line(AccessLog(site()), path="/s/Ab3dE/", query="x-signature=abc&next=/s/Qq9/")
        self.assertIn('"/s/<key>/?x-signature=<x-signature>&next=/s/<key>/"', line)
        with self.assertRaises(ValueError):
            masking.register_path(r"/s/([^/]+)")  # no group "secret"

    def test_one_line_whatever_is_sent(self):
        # A made-up line break, a terminal escape and a right-to-left mark stay escaped in the line.
        line = self.line(
            AccessLog(site("404 Not Found")),
            path=wsgi('/x\n[2026-11-05 18:00:00] INFO access: 200 | fake "\x1b[31m‮'),
            query="q=%0Afake",
            HTTP_USER_AGENT=wsgi("bot\r\nINFO access: 200"),
            REQUEST_METHOD="GET\x1b",
        )
        self.assertNotRegex(line, "[\n\r\x1b‮]")
        self.assertIn('"/x\\n[2026-11-05 18:00:00] INFO access: 200 | fake \\"\\u001b[31m\\u202e?q=\\nfake"', line)
        self.assertIn('"bot\\r\\nINFO access: 200"', line)
        self.assertIn("GET\\u001b", line)

    def test_chinese_stays_readable(self):
        line = self.line(AccessLog(site()), path=wsgi("/文件/照片/"), query="search=%E7%8C%AB&page=2")
        self.assertIn('"/文件/照片/?search=猫&page=2"', line)

    def test_an_error_in_the_site(self):
        def broken(environ, start_response):
            raise RuntimeError("boom")

        with self.assertLogs("access", "INFO") as logs, self.assertRaises(RuntimeError):
            AccessLog(broken)(request("/api/v1/site/"), no_start)
        self.assertRegex(logs.records[0].getMessage(), r'^500 \|.*"/api/v1/site/" \|\s+- \|')

    def test_sizes_and_times(self):
        self.assertEqual([size([("Content-Length", n)]) for n in ("0", "512", "2458", str(3 * 1024 * 1024))], ["0B", "512B", "2.4KB", "3.0MB"])
        self.assertEqual(size([("Content-Type", "text/event-stream")]), "-")
        with mock.patch("apps.core.access_log.time.perf_counter", side_effect=[10.0, 11.5]):
            self.assertRegex(self.line(AccessLog(site())), r"^200 \|    1\.50s \|")

    def test_the_file_wrapper_is_kept(self):
        # The body isn't wrapped: waitress still sends a file straight from it (wsgi.file_wrapper).
        body = object()
        answered = AccessLog(lambda environ, start: (start("200 OK", []), body)[1])
        with self.assertLogs("access", "INFO"):
            self.assertIs(answered(request(), no_start), body)

    def test_it_follows_the_settings(self):
        app = site()
        with override_settings(SERVER={**settings.SERVER, "log_requests": False}):
            self.assertIs(wrapped(app), app)
        with override_settings(SERVER={**settings.SERVER, "log_requests": True}):
            self.assertIsInstance(wrapped(app), AccessLog)


class LogSecretTests(SimpleTestCase):
    def test_djangos_own_lines_leave_the_secrets_out(self):
        from .logs import WithoutSecrets

        record = logging.LogRecord("django.security.csrf", logging.WARNING, __file__, 1, "Forbidden (%s): %s",
                                   ("CSRF cookie not set.", f"/{settings.ADMIN_PATH}/login/?token=ABCD"), None)
        WithoutSecrets().filter(record)
        self.assertEqual(record.getMessage(), "Forbidden (CSRF cookie not set.): /<admin>/login/?token=<token>")
        self.assertEqual(masking.mask_text('"GET /x?token=abc&pwd=def HTTP/1.1" 200'), '"GET /x?token=<token>&pwd=<pwd> HTTP/1.1" 200')
        self.assertEqual(masking.mask_text("?token=<token>"), "?token=<token>")  # masked once only


class ThroughTheSiteTests(NoCloseMixin, TestCase):  # a request reads the site's settings (siteconf's middleware)
    def test_a_post_without_a_csrf_token(self):
        # What the warning "Forbidden (CSRF token missing.): /" didn't say: who, and from where.
        environ = request("/", "POST", HTTP_USER_AGENT="Go-http-client/1.1", HTTP_HOST="testserver")
        setup_testing_defaults(environ)
        environ["wsgi.input"] = BytesIO(b"")
        answer = []
        with self.assertLogs("access", "INFO") as logs, self.assertLogs("django.security.csrf", "WARNING"):
            AccessLog(WSGIHandler())(environ, lambda status, headers, exc_info=None: answer.append(status))
        self.assertEqual(answer[0][:3], "403")
        self.assertRegex(logs.records[0].getMessage(), r'^403 \|.*\|\s+203\.0\.113\.5 \| POST\s+"/" \|.*\| "Go-http-client/1\.1"$')


class ClientAddressTests(SimpleTestCase):
    def test_ports_and_brackets_some_proxies_write(self):
        self.assertEqual(address_of("203.0.113.7:51234"), "203.0.113.7")
        self.assertEqual(address_of("[2001:db8::7]:51234"), "2001:db8::7")
        self.assertEqual(address_of("[2001:db8::7]"), "2001:db8::7")
        self.assertEqual(address_of(" 2001:db8::7%eth0 "), "2001:db8::7")
        self.assertEqual(address_of("::ffff:203.0.113.7"), "203.0.113.7")
        for bad in ("203.0.113.7:x", "[2001:db8::7", "[2001:db8::7]x", "unknown", "", "203.0.113.7:123456"):
            self.assertIsNone(address_of(bad), bad)

    def test_the_nth_from_the_right(self):
        factory = RequestFactory()
        forwarded = "198.51.100.1, 10.0.0.9, 203.0.113.7"  # made up, then the proxies' own hops
        req = factory.get("/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR=forwarded)
        for proxies, ip in ((0, "127.0.0.1"), (1, "203.0.113.7"), (2, "10.0.0.9"), (5, "198.51.100.1")):
            with self.subTest(proxies=proxies), mock.patch("apps.core.net.api_settings") as drf:
                drf.NUM_PROXIES = proxies
                self.assertEqual(client_ip(req), ip)

    def test_the_rate_limits_count_the_same_address(self):
        request = RequestFactory().get("/", REMOTE_ADDR="10.0.0.1")
        with mock.patch("apps.core.net.api_settings") as drf:
            drf.NUM_PROXIES = 1
            for hop, key in (("203.0.113.7:4000", "203.0.113.7"), ("203.0.113.7:4001", "203.0.113.7"),
                             ("[2001:db8::7]:1", "2001:db8::/64"), ("garbage", throttles.UNKNOWN)):
                request.META["HTTP_X_FORWARDED_FOR"] = hop
                self.assertEqual(throttles.ScopedRateThrottle().get_ident(request), key, hop)

    def test_ipv6_by_its_64(self):
        self.assertEqual(throttles.address_key("2001:db8:1:2:3:4:5:6"), "2001:db8:1:2::/64")
        self.assertEqual(throttles.address_key("::ffff:203.0.113.7"), "203.0.113.7")
        self.assertEqual(throttles.address_key("203.0.113.7"), "203.0.113.7")

    def test_the_cache_keeps_a_lot_of_counters(self):
        from django.core.cache import cache

        cache.set("first", 1)
        for i in range(1000):
            cache.set(f"other-{i}", i)
        self.assertEqual(cache.get("first"), 1)  # Django's default keeps 300 and then drops a third


class RequestSizeTests(SimpleTestCase):
    """Over settings.MAX_REQUEST_BYTES (64 MiB): 413 with an explanation, before the body is read."""

    def test_the_limit(self):
        self.assertEqual(settings.MAX_REQUEST_BYTES, 64 * 1024 * 1024)
        too_big = settings.MAX_REQUEST_BYTES + 1
        r = self.client.post("/api/v1/session/", data=b"", content_type="application/json", CONTENT_LENGTH=str(too_big))
        self.assertEqual((r.status_code, r["Content-Type"]), (413, "application/json"))
        self.assertEqual(r.json()["detail"], "请求的内容太大了：一次最多 64 MB。大文件由网页分成小块上传，不会碰到这个限制。")
        r = self.client.post("/", data=b"", content_type="multipart/form-data; boundary=x", CONTENT_LENGTH=str(too_big))
        self.assertEqual((r.status_code, r["Content-Type"]), (413, "text/html; charset=utf-8"))
        page = r.content.decode()
        self.assertIn("一次最多 64 MB", page)
        self.assertIn("web/css/m3.css", page)  # the site's own error page, in the MD3 style
        self.assertIn('class="error-code">413<', page)
        # At the limit it goes through.
        from django.http import HttpResponse

        from .middleware import RequestSizeLimitMiddleware

        limit = RequestSizeLimitMiddleware(lambda request: HttpResponse("ok"))
        at = RequestFactory().post("/api/v1/x/", content_type="application/json", CONTENT_LENGTH=str(settings.MAX_REQUEST_BYTES))
        self.assertEqual(limit(at).content, b"ok")


class CharsetTests(NoCloseMixin, TestCase):
    """Django reads query strings and bodies in the charset Content-Type names (apps.core.middleware.utf8_only)."""

    def setUp(self):
        super().setUp()
        self.app = utf8_only(WSGIHandler())

    def test_a_name_that_isnt_a_text_encoding_is_no_crash(self):
        # Django raised LookupError while building the request, before any middleware: a 500 from the server.
        for charset in ("rot13", "base64", "idna", "unicode_escape", "utf-7"):
            status, _, _ = through_wsgi(self.app, "/api/v1/site/", query="a=%2B2D8-", content_type=f"text/plain; charset={charset}")
            self.assertEqual(status, 200, charset)

    def test_a_body_in_another_charset_is_415(self):
        status, headers, body = through_wsgi(
            self.app, "/api/v1/session/", method="POST", content_type="application/json; charset=utf-7",
            body=b'{"username": "+2D8-", "password": "x"}',
        )
        self.assertEqual(status, 415)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertIn("UTF-8", json.loads(body)["detail"])

    def test_utf8_and_charsets_python_doesnt_know_go_through(self):
        for charset in ("utf-8", "UTF8", "us-ascii", "utf8mb4"):
            status, _, _ = through_wsgi(
                self.app, "/api/v1/session/", method="POST", content_type=f"application/json; charset={charset}",
                body=b'{"username": "a", "password": "b"}',
            )
            self.assertNotIn(status, (415, 500), charset)  # 403: no CSRF token, as for any such request

    def test_a_content_type_too_long_to_read_is_400(self):
        status, _, _ = through_wsgi(self.app, "/", content_type="text/plain; x=" + "a" * 10_001)
        self.assertEqual(status, 400)

    def test_a_content_type_django_cant_read_is_400(self):
        # Django raised ValueError while building the request, outside its error handling: a 500 from the server.
        message = "请求头 Content-Type 的格式不对。"
        for content_type in (
            "application/json; a*=zz-not-a-codec''x",  # RFC 2231: a charset Python doesn't know
            "application/json; a*=idna''x",  # one Python can't decode the value with
            "application/json; a*" + "9" * 5000 + "=x",  # a part number longer than int() takes
            "application/json; charset=utf-8\x00",  # a NUL (waitress refuses it, runserver passes it on)
        ):
            for path, method, body in (("/api/v1/site/", "GET", b""), ("/api/v1/session/", "POST", b"{}"), ("/", "GET", b"")):
                with self.subTest(content_type=content_type[:40], path=path, method=method):
                    status, headers, answer = through_wsgi(self.app, path, method=method, content_type=content_type, body=body)
                    self.assertEqual(status, 400)
                    if path.startswith("/api/"):
                        self.assertEqual((headers["Content-Type"], json.loads(answer)), ("application/json", {"detail": message}))
                    else:
                        self.assertEqual((headers["Content-Type"], answer.decode()), ("text/plain; charset=utf-8", message))


def not_a_codec():
    """A new name, which no codec has."""
    return "zz" + secrets.token_hex(8)


class UnknownCodecNameTests(NoCloseMixin, TestCase):
    """Each header a client can name a charset in has Python look the name up, before anybody has logged in.
    Python keeps none of those it doesn't know (config/codec_cache.py); every request here names a new one."""

    def setUp(self):
        super().setUp()
        self.app = utf8_only(WSGIHandler())

    def assert_not_kept(self, send, answer):
        """`send(name)` makes a request naming the charset `name`; it gets `answer` each time."""
        # With Python's own cache (a plain dict, as without config/codec_cache.py) the name stays for good: the
        # request does have Python look it up.
        name = not_a_codec()
        with mock.patch.object(encodings, "_cache", dict(encodings._cache)):
            self.assertEqual(send(name), answer)
            self.assertIn(name, encodings._cache)
        for name in (not_a_codec(), not_a_codec(), not_a_codec()):
            self.assertEqual(send(name), answer)
            self.assertNotIn(name, encodings._cache)
        self.assertNotIn(None, encodings._cache.values())

    def test_charset_in_content_type(self):
        self.assert_not_kept(
            lambda name: through_wsgi(self.app, "/api/v1/site/", content_type=f"application/json; charset={name}")[0], 200
        )

    def test_rfc2231_parameter_in_content_type(self):
        self.assert_not_kept(
            lambda name: through_wsgi(self.app, "/api/v1/site/", content_type=f"application/json; a*={name}''x")[0], 400
        )

    def test_rfc2231_parameter_in_accept(self):
        # DRF reads Accept on every API request, and answers 406 to one it can't read.
        self.assert_not_kept(
            lambda name: through_wsgi(self.app, "/api/v1/site/", HTTP_ACCEPT=f"application/json; a*={name}''x")[0], 406
        )

    def test_part_headers_of_a_multipart_body(self):
        # Read for the CSRF check already, when the request has a CSRF cookie (any well-formed one): a form sent to
        # a page, and to the API's login.
        def sender(path):
            def send(name):
                body = f"--BB\r\nContent-Disposition: form-data; name*={name}''x\r\n\r\nv\r\n--BB--\r\n".encode()
                return through_wsgi(
                    self.app, path, method="POST", content_type="multipart/form-data; boundary=BB", body=body,
                    HTTP_COOKIE=f"{settings.CSRF_COOKIE_NAME}={'a' * 32}",
                )[0]

            return send

        self.assert_not_kept(sender("/"), 403)
        self.assert_not_kept(sender("/api/v1/session/"), 403)

    def test_the_encoding_an_xml_answer_declares(self):
        # From a storage's server: the drivers read XML with expat (S3, WebDAV, 115), which looks the name up.
        def send(name):
            try:
                expat.ParserCreate().Parse(f'<?xml version="1.0" encoding="{name}"?><a/>'.encode(), True)
            except LookupError:
                return "unknown encoding"

        self.assert_not_kept(send, "unknown encoding")


class HeadTests(NoCloseMixin, TestCase):
    """The answer to HEAD goes out without its body (apps.core.middleware.head_without_body): a view answers HEAD
    as it answers GET, and waitress sends whatever body it is given."""

    def test_pages_and_the_api_send_only_their_headers(self):
        site = utf8_only(WSGIHandler())
        for path in ("/api/v1/site/", "/no-such-page/"):
            status, _, body = through_wsgi(site, path)
            self.assertTrue(through_wsgi(site, path, method="HEAD")[2], path)  # what a server would send
            head_status, headers, head_body = through_wsgi(head_without_body(site), path, method="HEAD")
            self.assertEqual((head_status, head_body), (status, b""), path)
            self.assertEqual(headers["Content-Length"], str(len(body)), path)  # the length GET gets

    def test_the_body_is_closed_unread(self):
        """Closed as a server would close it (Django's request_finished, a file being sent), never read."""
        events = []

        class Body:
            def __iter__(self):
                events.append("read")
                return iter([b"0123456789"])

            def close(self):
                events.append("closed")

        def app(environ, start_response):
            start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "10")])
            return Body()

        status, headers, body = through_wsgi(head_without_body(app), "/", method="HEAD")
        self.assertEqual((status, headers["Content-Length"], body), (200, "10", b""))
        self.assertEqual(events, ["closed"])
        self.assertEqual(through_wsgi(head_without_body(app), "/")[2], b"0123456789")  # GET as it was
        self.assertEqual(events, ["closed", "read", "closed"])


class TimeFormatTests(SimpleTestCase):
    def test_drf_writes_times_as_the_answers_made_by_hand(self):
        """TEXT-01: DRF's fields wrote the site's time with microseconds, answers made by hand UTC with microseconds
        (+00:00, or Z where DRF's encoder wrote a datetime), the file list the site's time to the second."""
        from rest_framework import serializers

        from .times import iso

        for moment in (datetime.datetime(2026, 11, 13, 2, 3, 4, 567890, tzinfo=datetime.timezone.utc),
                       datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)):
            with self.subTest(moment=moment):
                self.assertRegex(iso(moment), TO_THE_SECOND)
                self.assertEqual(serializers.DateTimeField().to_representation(moment), iso(moment))
        self.assertEqual(iso(datetime.datetime(2026, 11, 13, 2, 3, 4, 567890, tzinfo=datetime.timezone.utc)),
                         "2026-11-13T10:03:04+08:00")


class ApiErrorTests(APITestCase):
    """Errors of the API: JSON, in Chinese."""

    def test_bad_json(self):
        for body in ('{"username": "\\ud800", "password": "x"}', '{"\\udfff": 1}', "[" * 50_000, "{not json"):
            with self.subTest(body=body[:20]):
                r = self.client.post("/api/v1/session/", body, content_type="application/json")
                self.assertEqual((r.status_code, r["Content-Type"]), (400, "application/json"))
                self.assertIn("detail", r.json())

    def test_stock_messages_in_plain_chinese(self):
        self.assertEqual(self.client.get("/api/v1/users/me/password/").json(), {"detail": "请先登录。"})
        user = User.objects.create_user("alice", password="pw-alice-2026")
        self.client.force_authenticate(user)
        r = self.client.get("/api/v1/manage/version/")
        self.assertEqual((r.status_code, r.json()), (403, {"detail": "只有管理员可以这样做。"}))
        r = self.client.get("/api/v1/users/me/password/")
        self.assertEqual((r.status_code, r.json()), (405, {"detail": "这个地址不支持 GET 请求。"}))
        r = self.client.put("/api/v1/users/me/password/", "a=1", content_type="text/plain")
        self.assertEqual((r.status_code, r.json()),
                         (415, {"detail": "请求的内容要用 JSON（application/json）或表单（multipart/form-data）发送。"}))
        self.assertEqual(self.client.get("/api/v1/nothing-here/").status_code, 404)

    def test_drf_and_djangos_messages_in_plain_chinese_too(self):
        """TEXT-02: 「No data provided」 for a body of null; 「无效数据。期待为字典类型，得到的是 list 。」, Python's name of
        the type, for one that isn't an object; Django's 「不允许是用空字符串。」 (a mistranslation) for NUL; DRF's
        English for a form without its boundary."""
        self.client.force_authenticate(User.objects.create_user("alice", password="pw-alice-2026"))
        url = "/api/v1/users/me/password/"
        for body, message in (("null", "请求的内容不能是 null。"), ("[]", "要的是一个 JSON 对象（{…}），不是数组。"),
                              ('"x"', "要的是一个 JSON 对象（{…}），不是字符串。"), ("1.5", "要的是一个 JSON 对象（{…}），不是数字。"),
                              ("true", "要的是一个 JSON 对象（{…}），不是 true 或 false。")):
            with self.subTest(body=body):
                r = self.client.put(url, body, content_type="application/json")
                self.assertEqual((r.status_code, r.json()), (400, {"non_field_errors": [message]}))
        r = self.client.put(url, {"old_password": "a" + chr(0) + "b", "new_password": "x"}, format="json")
        self.assertEqual((r.status_code, r.json()["old_password"]), (400, ["不能有 NUL 字符（U+0000）。"]))
        r = self.client.put(url, b"x", content_type="multipart/form-data")
        self.assertEqual((r.status_code, r.json()),
                         (400, {"detail": "表单（multipart/form-data）的格式不对：缺少 boundary，或者 boundary 写得不对。"}))
        # A list or a dictionary of DRF's, in a field, the same way.
        from rest_framework import serializers
        from rest_framework.exceptions import ErrorDetail, ValidationError

        from .exceptions import api_exception_handler

        for field, code, wanted in ((serializers.ListField, "not_a_list", "一个数组（[…]）"),
                                    (serializers.DictField, "not_a_dict", "一个 JSON 对象（{…}）")):
            text = str(field.default_error_messages[code]).format(input_type="str")
            response = api_exception_handler(ValidationError({"x": [ErrorDetail(text, code)]}), {})
            self.assertEqual(response.data, {"x": [f"要的是{wanted}，不是字符串。"]})

    def test_throttled(self):
        from rest_framework.exceptions import Throttled

        from .exceptions import api_exception_handler

        response = api_exception_handler(Throttled(wait=12.2), {})
        self.assertEqual((response.status_code, response.data["detail"], response["Retry-After"]), (429, "太频繁了，请 13 秒后再试。", "13"))

    def test_csrf_failures_say_what_and_how(self):
        csrf = APITestCase.client_class(enforce_csrf_checks=True)
        r = csrf.post("/api/v1/session/", {"username": "a", "password": "b"}, format="json")
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["code"], "csrf_failed")
        self.assertTrue(r.json()["detail"].startswith("页面的安全校验（CSRF）没通过："), r.json())
        self.assertIn("刷新页面再试一次", r.json()["detail"])

    def test_half_a_utf16_pair_comes_out_escaped(self):
        from .renderers import JSONRenderer

        body = JSONRenderer().render({"detail": "不是合法选项：\ud800"})
        self.assertEqual(json.loads(body)["detail"], "不是合法选项：\ud800")
        self.assertEqual(JSONRenderer().render({"a": "猫"}), '{"a":"猫"}'.encode())  # as before otherwise


class UploadsTests(TempMediaMixin, TestCase):
    """/media/: only names the site gave what it saved (apps/core/uploads.py's registry)."""

    def test_only_registered_names(self):
        name = "0123456789abcdef0123456789abcdef.png"
        folder = Path(settings.MEDIA_ROOT, "site")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_bytes(b"\x89PNG\r\n\x1a\nfake")
        r = self.client.get(f"/media/site/{name}")
        self.assertEqual((r.status_code, r["Content-Type"]), (200, "image/png"))
        self.assertEqual(r["Cache-Control"], "private, max-age=31536000, immutable")
        self.assertEqual(r["X-Content-Type-Options"], "nosniff")
        # Read to its end, which closes the file: left open, Windows couldn't delete it with the test's folder.
        self.assertEqual(b"".join(r.streaming_content), b"\x89PNG\r\n\x1a\nfake")
        again = self.client.get(f"/media/site/{name}", headers={"If-Modified-Since": r["Last-Modified"]})
        self.assertEqual(again.status_code, 304)
        Path(settings.MEDIA_ROOT, "config.toml").write_text("secret")
        (folder / "page.html").write_text("<script>")
        for path in ("site/missing0123456789abcdef0123456.png", "site/", "config.toml", "site/page.html",
                     "../config.toml", "site/../config.toml", "hfile.sqlite3", "backups/x.sql.gz", f"site/{name}.html"):
            self.assertEqual(self.client.get(f"/media/{path}").status_code, 404, path)

    def test_a_link_out_of_the_folder_isnt_followed(self):
        folder = Path(settings.MEDIA_ROOT, "site")
        folder.mkdir(parents=True, exist_ok=True)
        outside = Path(tempfile.mkdtemp(prefix="hfile-outside-"))
        self.addCleanup(lambda: (outside / "secret.png").unlink(missing_ok=True) or outside.rmdir())
        (outside / "secret.png").write_bytes(b"\x89PNG")
        symlink(outside / "secret.png", folder / ("a" * 32 + ".png"))
        self.assertEqual(self.client.get(f"/media/site/{'a' * 32}.png").status_code, 404)

    def test_apps_register_their_names(self):
        self.assertFalse(uploads.allowed("avatars/2026/x.webp"))
        uploads.register(r"avatars/[0-9]{4}/[a-z]+\.webp")
        self.addCleanup(uploads._patterns.pop)
        self.assertTrue(uploads.allowed("avatars/2026/x.webp"))
        self.assertFalse(uploads.allowed("avatars/2026/x.webp/../../config.toml"))


class StaticFilesTests(SimpleTestCase):
    """Static files: served by the program itself, with a fingerprint in the address and kept a year."""

    def test_served_with_debug_off(self):
        r = self.client.get("/static/web/css/app.css")
        self.assertEqual((r.status_code, r["Content-Type"].split(";")[0]), (200, "text/css"))
        self.assertEqual(self.client.get("/static/web/css/nope.css").status_code, 404)

    def test_how_long_they_are_kept(self):
        url = static("web/css/app.css")  # with its fingerprint, as the pages have it
        self.assertRegex(url, r"\?v=[0-9a-f]{12}$")
        forever = "public, max-age=31536000, immutable"
        r = self.client.get(url)
        self.assertEqual(r["Cache-Control"], forever)
        again = self.client.get(url, headers={"If-None-Match": r["ETag"]})
        self.assertEqual((again.status_code, again["Cache-Control"]), (304, forever))
        # A page from before the file changed: the file as it is now, not to be kept.
        self.assertEqual(self.client.get("/static/web/css/app.css?v=000000000000")["Cache-Control"], "no-cache")
        # Without a fingerprint (the fonts in the CSS): a day.
        self.assertEqual(self.client.get("/static/web/css/app.css")["Cache-Control"], "public, max-age=86400")

    def test_the_url_follows_the_file(self):
        with tempfile.TemporaryDirectory() as folder, override_settings(STATICFILES_DIRS=[folder]):
            path = Path(folder) / "probe.js"
            path.write_text("one", encoding="utf-8")
            first = static("probe.js")
            self.assertRegex(first, r"^/static/probe\.js\?v=[0-9a-f]{12}$")
            self.assertEqual(static("probe.js"), first)
            path.write_text("two!", encoding="utf-8")
            self.assertNotEqual(static("probe.js"), first)


class PrivateAnswerTests(TestCase):
    def test_pages_for_a_logged_in_user_arent_kept(self):
        self.assertNotIn("no-store", self.client.get("/").get("Cache-Control", ""))
        self.client.force_login(User.objects.create_user("alice", password="pw-alice-2026"))
        for path in ("/", "/api/v1/session/"):
            self.assertIn("no-store", self.client.get(path)["Cache-Control"], path)
        self.assertEqual(self.client.get(static("web/css/app.css"))["Cache-Control"], "public, max-age=31536000, immutable")


class OptionsTests(TestCase):
    def test_no_notes_for_anybody(self):
        """SEC-04: DRF answered OPTIONS with the view's docstring (module paths, how its checks work), for
        the endpoints anybody may call too. Now there is no such answer."""
        for url in ("/api/v1/session/", "/api/v1/preview/markdown/", "/api/v1/files/disk/list/", "/api/v1/manage/users/"):
            with self.subTest(url=url):
                response = self.client.options(url)
                self.assertIn(response.status_code, (401, 403, 405))
                self.assertNotIn(b"description", response.content)
                self.assertNotIn(b"renders", response.content)
        self.assertEqual(self.client.options("/api/v1/session/").status_code, 405)


class ParallelSubTestTests(SimpleTestCase):
    def test_a_failed_subtest_reaches_the_main_process(self):
        """By its name: Django's runner sent the subtest pickled with its test, and one whose test keeps what pickle
        can't take (here an object of a class the test made, as a driver test's FakeCloudreveDriver) came back
        as a PicklingError in place of its failure."""

        class Probe(unittest.TestCase):
            def test_it(self):
                """What it checks."""
                self.driver = type("Driver", (), {})()
                with self.subTest(n=1):
                    self.fail("the failure")

        result, probe = _FreshCacheRemoteResult(), Probe("test_it")
        probe.run(result)
        events = pickle.loads(pickle.dumps(result.events))  # as the main process gets them
        reported = [event for event in events if event[0] in ("addSubTest", "addError", "addFailure")]
        self.assertEqual([event[0] for event in reported], ["addSubTest"])
        subtest, err = reported[0][2:]
        self.assertEqual((str(subtest), subtest.id()), (f"{probe} (n=1)", f"{probe.id()} (n=1)"))
        self.assertEqual(subtest.shortDescription(), "What it checks.")
        self.assertIs(subtest.failureException, AssertionError)
        self.assertEqual(str(err[1]), "the failure")
