"""Errors of the API: always JSON, always in Chinese.

The answer is DRF's {"detail": "..."} (or {field: [...]} for a form), with:
- DRF's and Django's stock messages in plain Chinese (DRF's own translation is used for the rest);
- a CSRF failure explained, with "code": "csrf_failed" so the page can fetch a new token
  (apps/web/static/web/js/common.js) and the reason Django gives translated;
- a few of Django's errors that would be an HTML page or a 500 answered like the others.
"""

import functools
import logging
import math
import re

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist, RequestDataTooBig, TooManyFieldsSent, TooManyFilesSent
from django.http import Http404
from django.utils import translation
from django.utils.translation import gettext as _, gettext_lazy, ngettext, pgettext
from rest_framework import exceptions, serializers, status
from rest_framework.exceptions import APIException, ErrorDetail, NotFound, ParseError
from rest_framework.views import exception_handler

logger = logging.getLogger(__name__)


class Conflict(APIException):
    """The request is valid, but the resource's current state does not allow it (HTTP 409)."""

    status_code = status.HTTP_409_CONFLICT
    default_detail = gettext_lazy("The resource's current state doesn't allow this.")
    default_code = "conflict"


class PayloadTooLarge(APIException):
    status_code = status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
    default_detail = gettext_lazy("What was sent is too large.")
    default_code = "payload_too_large"


class Unavailable(APIException):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = gettext_lazy("Unavailable right now. Please try again later.")
    default_code = "unavailable"


# DRF's messages, when a view raised them without one of its own.
PLAIN = {
    exceptions.NotAuthenticated: gettext_lazy("Please log in first."),
    exceptions.AuthenticationFailed: gettext_lazy("The login details are wrong. Please log in again."),
    exceptions.PermissionDenied: gettext_lazy("You don't have permission to do this."),
    exceptions.NotFound: gettext_lazy("Not found."),
    exceptions.NotAcceptable: gettext_lazy("This address can only answer in JSON."),
    exceptions.ParseError: gettext_lazy("The request is malformed."),
}

# What Django says when a CSRF check fails (django/middleware/csrf.py), in Chinese.
CSRF_REASONS = [
    (re.compile(r"CSRF cookie not set\.?"), gettext_lazy("the browser didn't send the CSRF cookie")),
    (re.compile(r"CSRF token missing\.?"), gettext_lazy("the CSRF token is missing (the X-CSRFToken header)")),
    (re.compile(r"CSRF token (?:incorrect|from .* has incorrect length|from .* has invalid characters)\.?"), gettext_lazy("the CSRF token is wrong")),
    (re.compile(r"Origin checking failed - (.*) does not match any trusted origins\.?"),
     gettext_lazy("the request's origin \\1 isn't one the site trusts (check [site] url in the "
                  "settings file, and that the reverse proxy passes Host and X-Forwarded-Proto)")),
    (re.compile(r"Referer checking failed - no Referer\.?"), gettext_lazy("over https the browser didn't say which page the request came from (Referer)")),
    (re.compile(r"Referer checking failed - .*"), gettext_lazy("the page the request came from isn't on this site")),
]
_CJK = re.compile(r"[\u3400-\u9fff]")


# How a 415 names what an address takes: its parsers' media types.
MEDIA_TYPES = {"application/json": gettext_lazy("JSON (application/json)"), "multipart/form-data": gettext_lazy("a form (multipart/form-data)")}

# Python's names of the types in DRF's 「得到的是 list」, as JSON calls them.
_JSON_TYPES = {"list": gettext_lazy("an array"), "tuple": gettext_lazy("an array"), "dict": gettext_lazy("an object"), "str": gettext_lazy("a string"), "int": gettext_lazy("a number"), "float": gettext_lazy("a number"),
               "bool": gettext_lazy("true or false"), "NoneType": "null"}


@functools.cache
def _type_messages(language):
    """{code: [(pattern, what was wanted)]} for DRF's messages that name the type that came as Python does, read
    from DRF's own (translated) ones; made once per language, once the translations are loaded."""
    wanted = [(serializers.Serializer, "invalid", "datatype", _("a JSON object ({…})")),
              (serializers.DictField, "not_a_dict", "input_type", _("a JSON object ({…})")),
              (serializers.ListField, "not_a_list", "input_type", _("an array ([…])")),
              (serializers.ListSerializer, "not_a_list", "input_type", _("an array ([…])"))]
    found = {}
    for kind, code, name, what in wanted:
        text = re.escape(str(kind.default_error_messages[code])).replace(re.escape("{%s}" % name), r"(\w+)")
        found.setdefault(code, []).append((re.compile(text), what))
    return found


def _plain(detail):
    """A ValidationError's detail with DRF's and Django's messages that aren't plain Chinese put into it (TEXT-02):
    「No data provided」 for a body of null, DRF's 「得到的是 list」 with Python's name of the type, and Django's
    「不允许是用空字符串。」, which is the wrong translation of "Null characters are not allowed"."""
    if isinstance(detail, dict):
        return {key: _plain(value) for key, value in detail.items()}
    if isinstance(detail, list):
        return [_plain(item) for item in detail]
    if not isinstance(detail, ErrorDetail):
        return detail
    if detail.code == "null" and detail == "No data provided":
        return ErrorDetail(_("The request body can't be null."), detail.code)
    if detail.code == "null_characters_not_allowed":
        return ErrorDetail(_("NUL characters (U+0000) aren't allowed."), detail.code)
    for pattern, what in _type_messages(translation.get_language()).get(detail.code, ()):
        match = pattern.fullmatch(detail)
        if match:
            got = str(_JSON_TYPES.get(match[1], match[1]))
            if got[0].isascii():  # a space between Chinese and Latin letters
                return ErrorDetail(pgettext("before a Latin word", "Expected %(what)s, not %(got)s.") % {"what": what, "got": got}, detail.code)
            return ErrorDetail(_("Expected %(what)s, not %(got)s.") % {"what": what, "got": got}, detail.code)
    return detail


def csrf_message(reason):
    for pattern, text in CSRF_REASONS:
        if pattern.fullmatch(reason):
            reason = pattern.sub(str(text), reason)
            break
    return _("The page's security check (CSRF) failed: %(reason)s. Reload the page and try again.") % {"reason": reason}


def api_exception_handler(exc, context):
    """settings.REST_FRAMEWORK["EXCEPTION_HANDLER"]: DRF's, after turning a few of Django's errors into
    answers in JSON like the others, and DRF's stock messages into plain Chinese (see the top).

    - A body (other than uploaded files) over DATA_UPLOAD_MAX_MEMORY_SIZE: 413. The whole request
      is limited earlier, by apps.core.middleware.RequestSizeLimitMiddleware.
    - A form with more fields or files than Django reads: 400.
    - A row that was there when the request started and is gone (deleted by a request at the same
      moment): 404, as if it had been gone from the start.
    """
    csrf_failed = False
    if isinstance(exc, RequestDataTooBig):
        limit = settings.DATA_UPLOAD_MAX_MEMORY_SIZE / (1024 * 1024)
        exc = PayloadTooLarge(_("What was sent is too large: at most %(limit)s MB besides uploaded files.") % {"limit": format(limit, "g")})
    elif isinstance(exc, (TooManyFieldsSent, TooManyFilesSent)):
        exc = ParseError(_("The form has too many fields."))
    elif isinstance(exc, ObjectDoesNotExist):
        request = context.get("request")
        logger.warning("Answered 404 for %s: %s", getattr(request, "path", "?"), exc)
        exc = NotFound(_("What you're looking for no longer exists; it may have just been deleted."))
    elif isinstance(exc, Http404):
        # get_object_or_404's message is English ("No User matches the given query.").
        message = str(exc)
        exc = NotFound(message if _CJK.search(message) else PLAIN[exceptions.NotFound])
    elif isinstance(exc, exceptions.PermissionDenied) and str(exc.detail).startswith("CSRF Failed: "):
        csrf_failed = True
        exc = exceptions.PermissionDenied(csrf_message(str(exc.detail).removeprefix("CSRF Failed: ")))
    elif isinstance(exc, exceptions.MethodNotAllowed):
        method = context["request"].method if context.get("request") else ""
        exc = exceptions.MethodNotAllowed(method, _("This address doesn't support %(method)s requests.") % {"method": method})
    elif isinstance(exc, exceptions.UnsupportedMediaType):
        # What this address takes: the text editor's save takes JSON alone (TEXT-02).
        parsers = getattr(context.get("request"), "parsers", None) or ()
        takes = _(" or ").join(str(MEDIA_TYPES.get(parser.media_type, parser.media_type)) for parser in parsers)
        exc = exceptions.UnsupportedMediaType("", _("The request body must be sent as %(types)s.") % {"types": takes or MEDIA_TYPES["application/json"]})
    elif isinstance(exc, ParseError) and str(exc.detail).startswith("Multipart form parse error - "):
        # DRF's English, with Django's reason (TEXT-02).
        boundary = "Invalid boundary" in str(exc.detail)
        if boundary:
            exc = ParseError(_("The form (multipart/form-data) is malformed: the boundary is missing or wrong."))
        else:
            exc = ParseError(_("The form (multipart/form-data) is malformed."))
    elif isinstance(exc, exceptions.ValidationError):
        exc.detail = _plain(exc.detail)
    elif isinstance(exc, exceptions.Throttled):
        wait = math.ceil(exc.wait) if exc.wait is not None else None
        # Not Throttled(wait, ...): it would add DRF's own "预计 N 秒后可用" to the message.
        exc = exceptions.Throttled(None, ngettext("Too many requests. Please try again in %(wait)s second.", "Too many requests. Please try again in %(wait)s seconds.", wait) % {"wait": wait} if wait else _("Too many requests. Please try again later."))
        exc.wait = wait  # the Retry-After header
    elif type(exc) in PLAIN and str(exc.detail) == str(type(exc).default_detail):
        exc = type(exc)(PLAIN[type(exc)])
    response = exception_handler(exc, context)
    if response is not None and csrf_failed:
        response.data["code"] = "csrf_failed"
    return response
