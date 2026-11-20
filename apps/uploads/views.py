"""The upload API, under /api/v1/ (config/api_urls.py), for everybody the access policy lets upload (the
guest too, when the admin gave it 上传). Every request that changes something checks the CSRF token, also
for visitors who aren't logged in (DRF skips it for them).

    POST   apps/uploads/                  a session: {"storage", "folder", "name", "size", "relative_path",
                                     "overwrite", "password"} -> 201 the session (below); 409 {"code": "exists",
                                     "can_overwrite"} when the file is there and overwrite is false
    GET    uploads/<id>/             the session: how much arrived, its state, the error, the stored item
    PUT    uploads/<id>/             a chunk of a proxied session, the raw bytes with Content-Range: bytes
                                     a-b/size, in order, at most chunk_size: 202 until the last, then 201
                                     with the item; 409 {"code": "offset", "expected"} for a chunk out of order
    DELETE uploads/<id>/             cancel (204)
    POST   uploads/<id>/complete/    a direct upload is done (the site looks at the file); a proxied one
                                     whose last write failed: try again ({"overwrite": true} to replace a
                                     file that turned up meanwhile)
    POST   uploads/folders/          {"storage", "folder", "folders": ["a", "a/b", ...]}: the folders of a
                                     folder upload, parents first, inside `folder` (which must exist): 新建文件夹
                                     and the policy for all of them first; {"created", "existing", "failed"}
    PUT    uploads/text/             the text editor's save: {"storage", "path", "content", "encoding",
                                     "newline", "expected": {"modified", "size"}} -> the item

A session: {"id", "kind": "proxy" | "direct", "state": "receiving" | "waiting" | "finishing" | "failed" |
"done", "name", "path", "folder", "size", "received", "chunk_size" (proxy), "url", "complete_url",
"target" (direct, only in the answer that made it: kind single | form | chunks, url, method, headers,
fields, file_field, chunk_size, expires_in), "overwrite", "expires_at", "error": {"code", "detail"} or
null, "item": {"name", "path", "size", "modified"} or null}. Paths are relative to the caller's root.
"""

from __future__ import annotations

import threading

from django.core.validators import ProhibitNullCharactersValidator
from django.utils.translation import gettext as _
from rest_framework import serializers, status, throttling
from rest_framework.exceptions import UnsupportedMediaType
from rest_framework.parsers import BaseParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.validators import ProhibitSurrogateCharactersValidator
from rest_framework.views import APIView

from apps.accounts.permissions import Permission
from apps.core import net, parsers
from apps.core.auth import enforce_csrf
from apps.core.exceptions import PayloadTooLarge
from apps.core.fields import BoundedListField, StringField
from apps.core.throttles import UNKNOWN, ScopedRateThrottle, address_key
from apps.storages import access as policy
from apps.storages.access import AccessDenied

from . import service
from .models import State

TEXT_REQUEST_MAX = 16 * 1024 * 1024  # bytes of the JSON a text save may send (5 MiB of text, escaped)
# Text saves read, parsed and written at the same time, in one process. A save holds a few times its body
# while it is parsed and written (2 to 3 times for UTF-8 text, up to 4 with CRLF line breaks; about 7 at most,
# for GB18030 text with a character beyond U+FFFF, which makes Python keep 4 bytes for every character, or
# ASCII text written as UTF-16): with every thread of the server on a 16 MB save, a small server runs out of
# memory. The others wait their turn.
TEXT_SAVES_AT_ONCE = 2
TEXT_SAVE_WAIT = 5  # seconds a save waits for its turn before the 503 (a thread waits meanwhile)
_text_saves = threading.BoundedSemaphore(TEXT_SAVES_AT_ONCE)


class TextJSONParser(BaseParser):
    """The site's JSON for a text save, read from the stream up to TEXT_REQUEST_MAX: DRF parses JSON from
    request.body, which Django limits to DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB), less than the editor's
    5 MiB. Every other endpoint keeps that limit.

    Parsed by its shape, the keys _TextSerializer reads (apps.core.parsers.parse_object): an unknown key,
    an array or a deeper object is refused where it starts, so 16 MB of {"junk": [{}, {}, ...]} costs its
    bytes and no more (parsed whole, it was 24 bytes of memory for every byte). The bytes go once decoded."""

    media_type = "application/json"

    def parse(self, stream, media_type=None, parser_context=None):
        raw = stream.read(TEXT_REQUEST_MAX + 1) if stream is not None else b""
        if len(raw) > TEXT_REQUEST_MAX:
            raise PayloadTooLarge(_("What was sent is too large: the online editor saves at most %(limit)s MB.") % {"limit": service.TEXT_MAX // service.MiB})
        text = parsers.decode(raw, parser_context)
        del raw  # the text is all that's needed from here
        return parsers.parse_object(text, _TEXT_FIELDS)


class CreateThrottle(throttling.SimpleRateThrottle):
    """Making sessions (and folders): per address, logged in or not (settings: "upload")."""

    scope = "upload"

    def get_cache_key(self, request, view):
        ip = net.client_ip(request)
        return self.cache_format % {"scope": self.scope, "ident": address_key(ip) if ip else UNKNOWN}


class _CreateSerializer(serializers.Serializer):
    storage = StringField(max_length=64)
    folder = StringField(max_length=4096, allow_blank=True, required=False, default="", trim_whitespace=False)
    name = StringField(max_length=1024, trim_whitespace=False)
    relative_path = StringField(max_length=4096, allow_blank=True, required=False, default="",
                                          trim_whitespace=False)
    size = serializers.IntegerField(min_value=0, max_value=service.MAX_SIZE)
    overwrite = serializers.BooleanField(default=False)
    password = StringField(max_length=1000, allow_blank=True, required=False, trim_whitespace=False)


class _CompleteSerializer(serializers.Serializer):
    overwrite = serializers.BooleanField(default=False)


class _FoldersSerializer(serializers.Serializer):
    storage = StringField(max_length=64)
    folder = StringField(max_length=4096, allow_blank=True, required=False, default="", trim_whitespace=False)
    folders = BoundedListField(child=StringField(max_length=4096, trim_whitespace=False),
                               allow_empty=False, max_length=service.FOLDERS_MAX)
    password = StringField(max_length=1000, allow_blank=True, required=False, trim_whitespace=False)


class _ExpectedSerializer(serializers.Serializer):
    modified = StringField(max_length=64, required=False, allow_null=True, allow_blank=True)
    size = serializers.IntegerField(min_value=0, required=False, allow_null=True)


class _ContentField(StringField):
    """The editor's text, without CharField's checks for NUL and lone surrogates: save_text refuses NUL saying what
    and where, not with Django's 「不允许是用空字符串。」 (FILE-03); the parser has refused lone surrogates
    already, and this check of them went through the text in Python, 0.3 s for 5 MB (FILE-04)."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.validators = [validator for validator in self.validators if not isinstance(
            validator, (ProhibitNullCharactersValidator, ProhibitSurrogateCharactersValidator))]


class _TextSerializer(serializers.Serializer):
    storage = StringField(max_length=64)
    path = StringField(max_length=4096, trim_whitespace=False)
    content = _ContentField(allow_blank=True, trim_whitespace=False, max_length=service.TEXT_MAX)
    encoding = StringField(max_length=40, required=False, default="utf-8")
    newline = StringField(max_length=8, required=False, allow_blank=True, default="", trim_whitespace=False)
    expected = _ExpectedSerializer(required=False, allow_null=True, default=None)
    password = StringField(max_length=1000, allow_blank=True, required=False, trim_whitespace=False)


_TEXT_FIELDS = parsers.fields_of(_TextSerializer())  # what TextJSONParser lets through


def _password(data) -> str | None:
    return data.get("password") or None


class UploadsView(APIView):
    """POST: a new upload session (service.create)."""

    permission_classes = [AllowAny]
    throttle_classes = [CreateThrottle]

    def post(self, request):
        enforce_csrf(request)
        body = _CreateSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        session, access = service.create(
            request._request, storage=data["storage"], folder=data["folder"], name=data["name"], size=data["size"],
            relative=data["relative_path"], overwrite=data["overwrite"], password=_password(data))
        return Response(service.describe(session, access), status=status.HTTP_201_CREATED)


class SessionView(APIView):
    """GET the session, PUT a chunk, DELETE to cancel. The chunk's body is read as it comes, never
    parsed (request.data isn't touched)."""

    permission_classes = [AllowAny]

    def get(self, request, pk):
        session = service.load(request._request, pk)
        try:
            access = policy.open_storage(request._request, session.storage)
        except AccessDenied:
            access = None
        return Response(service.describe(session, access))

    def put(self, request, pk):
        enforce_csrf(request)
        session = service.load(request._request, pk)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or -1)
        except ValueError:
            length = -1
        session, access = service.receive(
            request._request, session, content_range=request.META.get("HTTP_CONTENT_RANGE"), content_length=length,
            body=request._request)
        done = session.state == State.DONE
        return Response(service.describe(session, access), status=status.HTTP_201_CREATED if done else status.HTTP_202_ACCEPTED)

    def delete(self, request, pk):
        enforce_csrf(request)
        service.cancel(service.load(request._request, pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


class CompleteView(APIView):
    """POST: finish a session (service.complete)."""

    permission_classes = [AllowAny]

    def post(self, request, pk):
        enforce_csrf(request)
        body = _CompleteSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        session = service.load(request._request, pk)
        session, access = service.complete(request._request, session, overwrite=body.validated_data["overwrite"])
        return Response(service.describe(session, access))


class FoldersView(APIView):
    """POST: the folders of a folder upload (service.make_folders)."""

    permission_classes = [AllowAny]
    throttle_classes = [CreateThrottle]

    def post(self, request):
        enforce_csrf(request)
        body = _FoldersSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        answer = service.make_folders(request._request, storage=data["storage"], folder=data["folder"],
                                      folders=data["folders"], password=_password(data))
        return Response(answer)


class TextView(APIView):
    """PUT: the text editor's save (service.save_text), TEXT_SAVES_AT_ONCE at a time."""

    permission_classes = [AllowAny]
    parser_classes = [TextJSONParser]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "text_save"

    def put(self, request):
        enforce_csrf(request)
        # Before the body is read and parsed (several MB): may the caller upload anywhere at all?
        actor = policy.actor_for(request._request)
        if not any(Permission.UPLOAD in (grant.permissions or ()) for _storage, grant in policy.visible_storages(actor)):
            if actor.is_guest:
                raise AccessDenied(policy.LOGIN_REQUIRED, _("Please log in first: files can't be saved without logging in."), status=401)
            raise AccessDenied(policy.PERMISSION_DENIED, _("You don't have the \"%(label)s\" permission.") % {"label": Permission.UPLOAD.label}, permission=Permission.UPLOAD)
        try:
            length = int(request.META.get("CONTENT_LENGTH") or 0)
        except ValueError:
            length = 0
        if length > TEXT_REQUEST_MAX:
            raise PayloadTooLarge(_("What was sent is too large: the online editor saves at most %(limit)s MB.") % {"limit": service.TEXT_MAX // service.MiB})
        if not _text_saves.acquire(timeout=TEXT_SAVE_WAIT):
            busy = Response({"detail": _("Too many files are being saved at once. Please save again in a moment.")}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
            busy._has_been_logged = True  # expected under load: Django logs every 5xx unless told it has been
            return busy
        try:
            item = _save_text(request)
        finally:
            _text_saves.release()
        return Response(item)


def _save_text(request) -> dict:
    """Reads, checks and writes a text save, in its turn. Nothing that holds the text outlives the call: the
    body isn't parsed through request.data, which keeps it on the request, and the dicts the serializer
    keeps are emptied. The request and the serializer are reference cycles, which only the garbage
    collector frees (maybe minutes later): with the text in them, the turns wouldn't bound the memory."""
    fields = data = None
    try:
        fields = _text_body(request)
        body = _TextSerializer(data=fields)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        return service.save_text(
            request._request, storage=data["storage"], path=data["path"], content=data["content"],
            encoding=data["encoding"], expected=data["expected"] or None, newline=data["newline"] or None,
            password=_password(data))
    finally:
        for held in (fields, data):
            if held is not None:
                held.clear()


def _text_body(request):
    """What request.data would be (DRF's Request._parse with this view's parser), not kept on the request."""
    if request.stream is None:  # no body
        return {}
    parser = request.negotiator.select_parser(request, request.parsers)
    if parser is None:
        raise UnsupportedMediaType(request.content_type)
    return parser.parse(request.stream, request.content_type, request.parser_context)
