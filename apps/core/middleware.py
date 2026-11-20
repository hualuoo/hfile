import codecs
import json

from django.conf import settings
from django.http import JsonResponse
from django.template.response import TemplateResponse
from django.utils.cache import add_never_cache_headers
from django.utils.http import MAX_HEADER_LENGTH, parse_header_parameters
from django.utils.translation import gettext as _

from . import net

_UTF8 = {"utf-8", "utf-8-sig", "ascii"}  # codec names (codecs.lookup().name) that are UTF-8, or part of it


def utf8_only(app):
    """WSGI: query strings and request bodies are read as UTF-8, whatever charset Content-Type names.

    Django decodes both with the charset a client puts in Content-Type, even for a GET. With
    charset=utf-7 that makes half UTF-16 pairs ("\\ud800"), which can't be written back out (a 500
    wherever an error message quotes the value); with a name that isn't a text encoding (rot13,
    base64, idna...) it raises before any middleware runs. So, before Django sees the request:

    * without a body, the charset is dropped (the query string is UTF-8, as URLs are);
    * a body in another charset is refused with 415;
    * a Content-Type Django can't read is refused with 400: one too long; an RFC 2231 parameter
      (x*=NAME''…) in a charset Python doesn't know or can't decode it with, or numbered with more
      digits than int() takes; a charset name with a NUL in it. Django raises ValueError for those
      while it builds the request, outside its error handling: a 500 from the server, with a
      traceback in the log.

    A charset Python doesn't know is left alone: Django ignores it too, and reads UTF-8. Python
    doesn't keep such names (config/codec_cache.py).
    """

    def guarded(environ, start_response):
        content_type = environ.get("CONTENT_TYPE", "")
        if not content_type:
            return app(environ, start_response)
        if len(content_type) > MAX_HEADER_LENGTH:
            return _refuse(environ, start_response, "400 Bad Request", _("The Content-Type header is too long."))
        try:
            main, params = parse_header_parameters(content_type)
            charset = params.get("charset")
            utf8 = charset is None or _is_utf8(charset)
        except ValueError:  # as Django's own reading of it would raise
            return _refuse(environ, start_response, "400 Bad Request", _("The Content-Type header is malformed."))
        if utf8:
            return app(environ, start_response)
        if _has_body(environ):
            return _refuse(
                environ, start_response, "415 Unsupported Media Type",
                _("The request body must be UTF-8: the charset in Content-Type can only be utf-8."),
            )
        return app({**environ, "CONTENT_TYPE": main}, start_response)

    return guarded


def _is_utf8(charset):
    """Raises ValueError for a name with a NUL in it (codecs.lookup does, in Django too)."""
    try:
        return codecs.lookup(charset).name in _UTF8
    except LookupError:
        return True  # unknown: Django ignores it and reads UTF-8


def _has_body(environ):
    try:
        length = int(environ.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return True
    return length > 0 or "HTTP_TRANSFER_ENCODING" in environ


def _refuse(environ, start_response, status, message):
    if environ.get("PATH_INFO", "").startswith("/api/"):
        body, kind = json.dumps({"detail": message}, ensure_ascii=False).encode(), "application/json"
    else:
        body, kind = message.encode(), "text/plain; charset=utf-8"
    start_response(status, [
        ("Content-Type", kind), ("Content-Length", str(len(body))), ("X-Content-Type-Options", "nosniff"),
    ])
    return [body]


def head_without_body(app):
    """WSGI: the answer to HEAD goes out without its body, its headers as they are (Content-Length is what
    GET would get).

    A view answers HEAD as it answers GET, body and all: Django leaves dropping it to the server, as runserver
    and the test client do. waitress (serve) sends the body of any answer but a 1xx, 204 or 304. A client
    reads no body after HEAD, so the next answer on its connection (a proxy's to the site too) would start
    with those bytes. The body is closed unread: Django sends request_finished then, and a file being
    served is closed. An answer without Content-Length still ends with an empty chunk (waitress sends such
    answers chunked), but waitress closes the connection after it, so no next answer reads it.
    """

    def guarded(environ, start_response):
        body = app(environ, start_response)
        if environ.get("REQUEST_METHOD") != "HEAD":
            return body
        if hasattr(body, "close"):
            body.close()
        return []

    return guarded


class CurrentRequestMiddleware:
    """Keeps the request being handled where apps.core.net.request_ip() and current_request() can find it,
    for code that is not given the request (a log entry written from a service, say)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        token = net.bind_request(request)
        try:
            return self.get_response(request)
        finally:
            net.unbind_request(token)


class PrivateAnswersMiddleware:
    """What a logged-in user is sent isn't kept by the browser (Cache-Control: no-store, private).

    Otherwise Back, after logging out, brings a page back from the browser's back/forward cache with
    what it showed (on /manage/: the users and their IPs), to whoever uses the computer next. Answers
    that say how long they are kept (static files, uploads) are left as they are; so are pages for
    visitors who aren't logged in.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated and "Cache-Control" not in response:
            add_never_cache_headers(response)
        return response


def too_large_message():
    limit = settings.MAX_REQUEST_BYTES // (1024 * 1024)
    return _("The request is too large: at most %(limit)s MB at a time. The page "
             "uploads large files in small chunks, which stay under this limit.") % {"limit": limit}


class RequestSizeLimitMiddleware:
    """Answers 413 to a request whose body is larger than settings.MAX_REQUEST_BYTES, before reading it:
    JSON {"detail"} for the API, the site's error page (apps/web/templates/413.html) for the rest.

    waitress takes bodies up to twice that and cuts larger ones off itself, without an explanation
    (apps/installer/management/commands/serve.py). A web server in front should have the site's limit
    (nginx: client_max_body_size 64m).
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > settings.MAX_REQUEST_BYTES:
            message = too_large_message()
            if request.path.startswith("/api/"):
                return JsonResponse({"detail": message}, status=413)
            response = TemplateResponse(request, "413.html", {"message": message}, status=413)
            return response.render()
        return self.get_response(request)
