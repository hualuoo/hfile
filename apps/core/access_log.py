"""A line in the log for every request, as Go's web frameworks print them (Gin's Logger, chi's
middleware.Logger, the loggers of Echo and Fiber): the status, how long the site took, the
visitor's IP, the method and the address, the size of the answer and the browser.

    [2026-11-05 18:51:11] INFO access: 403 |    1.2ms |     203.0.113.5 | POST    "/" |   2.4KB | "python-requests/2.32.3"

`hfile serve` puts AccessLog around the site when [server] log_requests is on (wrapped());
runserver prints a line per request of its own. The line is written once the site has answered:
the time is the site's, without waiting in waitress's queue or sending the body. The body isn't
wrapped, so waitress still sends files straight from the file (wsgi.file_wrapper).

- Every request, static files and uploads included.
- The IP is the one rate limits and logins use (apps/core/net.py): from X-Forwarded-For only with [server] proxies.
- The address is decoded, so Chinese stays readable, and written in quotes with every character that
  isn't printable escaped, as Gin does since it was shown how to fake lines with them: a line break
  or a terminal escape in an address can't make a line of its own. The browser likewise.
- Secrets in addresses stay out, in the path and in the query (apps/core/masking.py): the Django admin's
  path becomes /<admin>, the installer's ?token= <token>, and what other apps register.
"""

import logging
import time

from django.conf import settings

from .masking import mask_address
from .net import ip_from

logger = logging.getLogger("access")

ESCAPES = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def wrapped(app):
    """The site as `serve` runs it: with the request log when [server] log_requests is on."""
    if not settings.SERVER["log_requests"]:
        return app
    return AccessLog(app)


class AccessLog:
    """WSGI middleware: a line per request (see the top)."""

    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        started = time.perf_counter()
        answer = []

        def start(status, headers, exc_info=None):
            answer[:] = [status, headers]
            return start_response(status, headers, exc_info)

        try:
            body = self.app(environ, start)
        except Exception:
            self.write(environ, "500", [], started)  # waitress answers 500 in the site's place
            raise
        self.write(environ, *(answer or ["-", []]), started)
        return body

    def write(self, environ, status, headers, started):
        took = time.perf_counter() - started
        path = wsgi_text(environ.get("PATH_INFO", "")) or "/"
        address = mask_address(path, wsgi_text(environ.get("QUERY_STRING", "")))
        agent = wsgi_text(environ.get("HTTP_USER_AGENT", ""))
        logger.info(
            "%3s | %8s | %15s | %-7s %s | %7s | %s",
            status[:3],
            f"{took * 1000:.1f}ms" if took < 1 else f"{took:.2f}s",
            ip_from(environ) or "-",
            printable(environ.get("REQUEST_METHOD", "-"), 16),
            quoted(address, 300),
            size(headers),
            quoted(agent, 200) if agent else "-",
        )


def wsgi_text(value):
    """A WSGI string (the bytes as latin-1) as the UTF-8 text it was sent as."""
    try:
        return value.encode("latin-1").decode("utf-8", "replace")
    except UnicodeEncodeError:  # already text
        return value


def printable(value, longest):
    """`value` cut at `longest` characters, with what isn't printable written as \\n, \\uXXXX ...:
    line breaks, terminal escapes, right-to-left marks."""
    if len(value) > longest:
        value = value[:longest] + "…"
    out = []
    for ch in value:
        if ch in ESCAPES:
            out.append(ESCAPES[ch])
        elif ch.isprintable():
            out.append(ch)
        else:
            out.append(f"\\u{ord(ch):04x}" if ord(ch) < 0x10000 else f"\\U{ord(ch):08x}")
    return "".join(out)


def quoted(value, longest):
    return f'"{printable(value, longest)}"'


def size(headers):
    """The answer's Content-Length, readable; - when it has none (a stream)."""
    length = next((value for name, value in headers if name.lower() == "content-length"), "")
    if not length.isdigit():
        return "-"
    n = int(length)
    if n < 1024:
        return f"{n}B"
    return f"{n / 1024:.1f}KB" if n < 1024 * 1024 else f"{n / 1024 / 1024:.1f}MB"
