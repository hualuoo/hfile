"""Release check (0.1.0): static files, media and secrets in the logs.

The tests marked expectedFailure show a problem found by the release check; each says what to change.
Remove the decorator with the fix: the test then has to pass. The one whose comment says "Accepted
known limit" keeps its mark.
"""

import io
import logging
import unittest

from django.conf import settings
from django.test import SimpleTestCase, override_settings

from . import masking
from .access_log import AccessLog

ENTRY = "SEKRETENTRY1"  # a made-up secure login entry (登录安全入口)


def _site(environ, start_response):
    start_response("200 OK", [("Content-Type", "text/html"), ("Content-Length", "10")])
    return [b"x"]


def _line(path, query=""):
    """The access log's line for a request."""
    environ = {"REQUEST_METHOD": "GET", "PATH_INFO": path, "QUERY_STRING": query, "REMOTE_ADDR": "203.0.113.5",
               "HTTP_USER_AGENT": "probe"}
    logger = logging.getLogger("access")
    records = []
    handler = logging.Handler()
    handler.emit = records.append
    kept, logger.handlers = logger.handlers, [handler]  # not on the console while the tests run
    try:
        AccessLog(_site)(environ, lambda status, headers, exc_info=None: None)
    finally:
        logger.handlers = kept
    return records[0].getMessage()


class StaticFileServingTests(SimpleTestCase):
    """What WhiteNoise serves with DEBUG off (apps/core/staticfiles.py): sound at 70cc913."""

    def test_js_modules_are_javascript_with_nosniff(self):
        for path in ("preview/vendor/pdf.js-6.4.299/pdf.min.mjs", "preview/vendor/pdf.js-6.4.299/pdf.worker.min.mjs",
                     "web/js/common.js"):
            r = self.client.get(f"/static/{path}")
            self.assertEqual((r.status_code, r["Content-Type"].split(";")[0]), (200, "text/javascript"), path)
            self.assertEqual(r["X-Content-Type-Options"], "nosniff", path)

    def test_nothing_outside_the_static_folders(self):
        for path in ("../config/settings.py", "web/../../config/settings.py", "%2e%2e/config/settings.py",
                     "web/%2e%2e%2f%2e%2e%2fconfig%2fsettings.py", "..%2fconfig.example.toml", "/etc/passwd",
                     "web/apps.py", "web/", "", "config.example.toml", "web/css/app.css/"):
            r = self.client.get(f"/static/{path}")
            self.assertEqual(r.status_code, 404, path)

    def test_head_and_ranges(self):
        r = self.client.get("/static/web/css/app.css", headers={"Range": "bytes=0-9"})
        self.assertEqual((r.status_code, len(b"".join(r.streaming_content))), (206, 10))
        self.assertTrue(r["Content-Range"].startswith("bytes 0-9/"))


class LoginEntryLogTests(SimpleTestCase):
    def test_the_secure_login_entry_stays_out_of_the_access_log(self):
        # The entry hides the login form from scanners, as the admin path hides the Django admin; the admin
        # path is masked (/<admin>), the entry is not: /login/<entry>/ shows in every line of its page.
        for path, query in ((f"/login/{ENTRY}/", ""), (f"/login/{ENTRY}/", "next=/manage/"),
                            ("/", f"auth=login&next=/login/{ENTRY}/")):
            with self.subTest(path=path, query=query):
                self.assertNotIn(ENTRY, _line(path, query))

    def test_the_secure_login_entry_stays_out_of_runservers_lines(self):
        self.assertNotIn(ENTRY, masking.mask_text(f'"GET /login/{ENTRY}/ HTTP/1.1" 200 4674'))


class RunserverLineTests(SimpleTestCase):
    # Accepted known limit: runserver is for development only and prints the
    # request line as sent. mask_text() masks the raw line; serve's access log decodes the query first.
    @unittest.expectedFailure
    def test_an_encoded_secret_in_next_stays_out_of_runservers_lines(self):
        # The access log has "/?next=/s/<key>" for this request (mask_address decodes the query first);
        # runserver's line is the request line as sent, %-encoded, and keeps the key.
        line = masking.mask_text('"GET /?next=%2Fs%2FSEKRETSHORT3 HTTP/1.1" 200 13442')
        self.assertNotIn("SEKRETSHORT3", line)


class TracebackLogTests(SimpleTestCase):
    """A traceback in the console (logger.exception, Django's 500s): apps.core.logs.WithoutSecrets masks the
    message only (record.msg/args), not record.exc_text, which the formatter adds below it."""

    def console(self, logger_name):
        """What the configured console handler of `logger_name` writes, while the test runs."""
        logger = logging.getLogger(logger_name)
        handler = next(h for h in logger.handlers if isinstance(h, logging.StreamHandler))
        stream = io.StringIO()
        old = handler.setStream(stream)
        self.addCleanup(handler.setStream, old)
        return logger, stream

    def test_the_message_is_masked(self):
        logger, stream = self.console("hfile")
        logger.warning("Fetching /pd/disk/a.txt?e=1&sign=SEKRETSIGN1 failed")
        self.assertIn("sign=<sign>", stream.getvalue())
        self.assertNotIn("SEKRETSIGN1", stream.getvalue())

    def test_the_traceback_is_masked_too(self):
        logger, stream = self.console("hfile")
        try:
            raise RuntimeError("GET /pd/disk/a.txt?e=1&sign=SEKRETSIGN2 answered 500, see /s/SEKRETSHORT3")
        except RuntimeError:
            logger.exception("Finishing upload %s failed", "1234")
        text = stream.getvalue()
        self.assertIn("Finishing upload 1234 failed", text)
        self.assertIn("RuntimeError", text)
        self.assertNotIn("SEKRETSIGN2", text)
        self.assertNotIn("SEKRETSHORT3", text)

    @override_settings(ADMIN_PATH="s3cret-admin")
    def test_a_500s_traceback_is_masked_too(self):
        logger, stream = self.console("django")  # django.request propagates to "django"'s console handler
        try:
            raise ValueError(f"bad value from /{settings.ADMIN_PATH}/x/?token=SEKRETTOKEN4")
        except ValueError:
            logging.getLogger("django.request").error("Internal Server Error: %s", "/api/v1/x/", exc_info=True)
        text = stream.getvalue()
        self.assertIn("Internal Server Error: /api/v1/x/", text)
        self.assertNotIn("SEKRETTOKEN4", text)
        self.assertNotIn("s3cret-admin", text)
