"""The files API, under /api/v1/files/<storage key>/ (config/api_urls.py). For everybody: the guest's grant
answers visitors who aren't logged in; every decision is apps.storages.access's (apps/files/service.py).

    GET  list/?path=&sort=name|size|time&order=asc|desc
         a folder in one answer (D39): {"storage", "path", "root", "items": [{"name", "path", "type",
         "size", "modified", "kind", "url", "locked"?}], "readme": {"html", "display", "hash"} | null,
         "permissions": {code: bool}, "capabilities": {...}, "gallery_default", "sort"}
    GET  item/?path=              one file or folder, with "url" and "inline_url" (same-origin, inline)
    POST mkdir/   {"path", "name"}                                    -> {"name", "path", "type"}
    POST rename/  {"path", "name", "new_name"}                        -> {"name", "path", "type"}
    POST move/    {"path", "names": [...], "target"}                  -> {"results": [...], "succeeded", "failed"}
    POST copy/    {"path", "names": [...], "target"}                  -> the same
    POST delete/  {"path", "names": [...]}                            -> the same

`path` is a folder relative to the caller's root ("/" or "" for the root), `names` the items in it. The
writes need `path` (and move/copy `target`): left out or misspelt it would mean the root, and the
wrong files would be deleted or moved; list/ and item/ show the root without one.
Folder passwords: the browser unlocks with POST /api/v1/storages/<key>/unlock/ (storages) and the
answers hold; API clients may also send one with the request ("password", and "target_password" for
the target of a move or copy; GET: the X-Folder-Password header) for that request only.

Errors are {"detail", "code", ...}: the policy's codes (apps.storages.access: password_required with "rule"
and "path" to unlock, password_wrong, inaccessible, ...), and not_found, not_a_folder, exists (目标已存在,
D17), invalid_name, unsupported, storage_error.
"""

from __future__ import annotations

from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django.utils.translation import gettext_lazy, ngettext_lazy

from apps.core.auth import enforce_csrf
from apps.core.fields import BoundedListField, StringField
from apps.core.text import from_header
from apps.storages import access as policy
from apps.storages.access import COPY, MOVE

from . import service

PATH_MAX = 4096


class _Path(StringField):
    """A folder relative to the caller's root; "" or "/" is the root. Required unless `root_default`."""

    def __init__(self, *, root_default=False, **kwargs):
        kwargs.setdefault("max_length", PATH_MAX)
        if root_default:
            kwargs.update(required=False, default="/")
        super().__init__(allow_blank=True, trim_whitespace=False, **kwargs)


class _Name(StringField):
    def __init__(self, **kwargs):
        super().__init__(max_length=1024, allow_blank=True, trim_whitespace=False, **kwargs)


class _Password(StringField):
    def __init__(self, **kwargs):
        super().__init__(max_length=1000, allow_blank=True, required=False, default=None, allow_null=True,
                         trim_whitespace=False, **kwargs)


class ListQuery(serializers.Serializer):
    path = _Path(root_default=True)
    sort = serializers.ChoiceField(choices=service.SORT_FIELDS, required=False, allow_blank=True, default="",
                                   error_messages={"invalid_choice": gettext_lazy("The sort field can only be name, size or time.")})
    order = serializers.ChoiceField(choices=service.ORDERS, required=False, allow_blank=True, default="",
                                    error_messages={"invalid_choice": gettext_lazy("The sort order can only be asc or desc.")})


class ItemQuery(serializers.Serializer):
    path = _Path(root_default=True)


class MkdirBody(serializers.Serializer):
    path = _Path()
    name = _Name()
    password = _Password()


class RenameBody(serializers.Serializer):
    path = _Path()
    name = _Name()
    new_name = _Name()
    password = _Password()


class _Batch(serializers.Serializer):
    path = _Path()
    names = BoundedListField(child=_Name(), allow_empty=True, max_length=service.BATCH_MAX,
                             error_messages={"max_length": ngettext_lazy("At most {max_length} item at a time.", "At most {max_length} items at a time.",
                                                                           "max_length")})
    password = _Password()


class RelocateBody(_Batch):
    target = _Path()
    target_password = _Password()


class DeleteBody(_Batch):
    pass


class _FilesView(APIView):
    permission_classes = [AllowAny]

    def open(self, request, key):
        return policy.open_storage(request._request, key)

    @staticmethod
    def checked(serializer_class, data):
        body = serializer_class(data=data)
        body.is_valid(raise_exception=True)
        return body.validated_data

    @staticmethod
    def header_password(request):
        """X-Folder-Password, as the client sent it: a password that isn't ASCII was compared as latin-1
        mojibake, never right, and each try counted as a wrong one (API-14)."""
        value = request.META.get("HTTP_X_FOLDER_PASSWORD")
        if value is None:
            return None
        return from_header(value)[:1000]

    def answer(self, access, data, status=200):
        response = Response(data, status=status)
        if access.keys is not None:
            access.keys.write(response)  # a bad remembered cookie is replaced
        return response


class ListView(_FilesView):
    def get(self, request, key):
        query = self.checked(ListQuery, request.query_params)
        access = self.open(request, key)
        data = service.listing(request._request, access, query["path"], sort=query["sort"] or None,
                               order=query["order"] or None, password=self.header_password(request))
        return self.answer(access, data)


class ItemView(_FilesView):
    def get(self, request, key):
        query = self.checked(ItemQuery, request.query_params)
        access = self.open(request, key)
        return self.answer(access, service.item(request._request, access, query["path"],
                                                password=self.header_password(request)))


class MkdirView(_FilesView):
    def post(self, request, key):
        enforce_csrf(request)
        body = self.checked(MkdirBody, request.data)
        access = self.open(request, key)
        return self.answer(access, service.mkdir(access, body["path"], body["name"], password=body["password"]), 201)


class RenameView(_FilesView):
    def post(self, request, key):
        enforce_csrf(request)
        body = self.checked(RenameBody, request.data)
        access = self.open(request, key)
        return self.answer(access, service.rename(access, body["path"], body["name"], body["new_name"],
                                                  password=body["password"]))


class _RelocateView(_FilesView):
    operation = MOVE

    def post(self, request, key):
        enforce_csrf(request)
        body = self.checked(RelocateBody, request.data)
        access = self.open(request, key)
        return self.answer(access, service.relocate(access, self.operation, body["path"], body["names"], body["target"],
                                                    password=body["password"], target_password=body["target_password"]))


class MoveView(_RelocateView):
    operation = MOVE


class CopyView(_RelocateView):
    operation = COPY


class DeleteView(_FilesView):
    def post(self, request, key):
        enforce_csrf(request)
        body = self.checked(DeleteBody, request.data)
        access = self.open(request, key)
        return self.answer(access, service.delete(access, body["path"], body["names"], password=body["password"]))
