"""The shares API (config/api_urls.py includes apps/shares/api_urls.py). Inventory 10, decision D32.

The owner's (logged in, not the guest, 10.Z.5):

    GET    apps/shares/                       我的分享: ?keyword= &storage= (id) &status=all|active|expired &since= &until= &page=
    POST   apps/shares/                       创建分享: {"storage", "path" (the folder, as files has it), "entries": [{"name",
                                         "type"}], "key" (custom_share_key), "password" (4-20), "expiry": "forever" | "7d"
                                         | "30d" | "custom", "expires_at" (custom, or alone)} -> 201, the share as the list has it
    POST   shares/clear-expired/         清理所有已过期分享 (one's own): {"deleted"}
    DELETE shares/<key>/                 the owner or a superuser: 204

Visitors' (anybody; a password share after verify/):

    GET    shares/<key>/                 the share: before the password only {"key", "need_password", "verified",
                                         "expired", "expires_at", "entry_count", "owner"}; then "entries", "storage" too
    POST   shares/<key>/verify/          {"password"}: sets the share's cookie. 403 share_password_wrong, 429 throttled
    GET    shares/<key>/list/?path=&sort=&order=   a folder of the share, as files' listing (W4)
    GET    shares/<key>/item/?path=      one item; a file gets "inline_url" with the owner's preview
    POST   shares/<key>/unlock/          {"path" | "rule", "password"}: a folder password inside the share
    DELETE shares/<key>/unlocks/         forgets those (清理密码文件夹缓存)

Admins' (IsAdmin):

    GET    manage/shares/                分享列表: the same filters and &user= (username or nickname)
    DELETE manage/shares/<key>/          the owner or a superuser: 204
    POST   manage/shares/clear-expired/  everybody's expired shares: {"deleted"}

Errors: {"detail", "code"}: share_not_found and share_expired (404), share_password_required (403),
share_password_wrong, share_unavailable (the owner can't share there any more), key_taken (409), and
apps.storages.access's and files' codes. The downloads themselves are pages: /share/<key>/download and
/share/<key>/preview (apps/shares/pages.py).
"""

from __future__ import annotations

from django.db import transaction
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _, gettext_lazy, ngettext
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import AllowAny, BasePermission
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import Kind
from apps.activity import log as activity
from apps.core.auth import enforce_csrf
from apps.core.exceptions import PayloadTooLarge
from apps.core.permissions import IsAdmin
from apps.core.query import QueryParamsMixin

from . import access as shares
from . import service
from .models import ENTRIES_MAX, Share
from .serializers import (
    CreateBody,
    ItemQuery,
    ListingQuery,
    ListQuery,
    ManageListQuery,
    ManageShareRow,
    ShareRow,
    UnlockBody,
    VerifyBody,
)

CREATE_BODY_MAX = 2 * 1024 * 1024  # 1000 entries with long names, in bytes of JSON
VISITOR_BODY_MAX = 16 * 1024  # verify/ and unlock/: a password and a path


def _body_at_most(request, limit: int, message: str) -> None:
    """Refuses a body over `limit` bytes before it is read: anybody may post to the visitors' endpoints,
    and parsing a JSON body of the site's 64 MB limit costs seconds of CPU and GBs of memory."""
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0
    if length > limit:
        raise PayloadTooLarge(message)


def is_member(user) -> bool:
    """Logged in with an account of one's own (not the guest or the template: they never log in)."""
    return bool(user and user.is_authenticated and user.is_active and getattr(user, "kind", None) == Kind.NORMAL)


class IsMember(BasePermission):
    message = gettext_lazy("Please log in first.")

    def has_permission(self, request, view):
        return is_member(request.user)


def _checked(serializer_class, data, **kwargs):
    body = serializer_class(data=data, **kwargs)
    body.is_valid(raise_exception=True)
    return body.validated_data


class MySharesView(QueryParamsMixin, ListAPIView):
    """GET 我的分享, POST 创建分享."""

    permission_classes = [IsMember]
    serializer_class = ShareRow
    query_serializer_class = ListQuery

    def get_queryset(self):
        mine = Share.objects.filter(owner=self.request.user).select_related("storage", "owner")
        return service.filtered(mine, self.query)

    def post(self, request):
        # Before parsing: a body of a million entries costs seconds of CPU.
        _body_at_most(request, CREATE_BODY_MAX, _("What was sent is too large: at most 1000 items can be shared at a time."))
        raw = request.data.get("entries") if isinstance(request.data, dict) else None
        if isinstance(raw, list) and len(raw) > ENTRIES_MAX:
            return Response({"entries": [ngettext("At most %(limit)s item can be shared at a time.", "At most %(limit)s items can be shared at a time.", ENTRIES_MAX) % {"limit": ENTRIES_MAX}]}, status=status.HTTP_400_BAD_REQUEST)
        body = _checked(CreateBody, request.data)
        share = service.create(request._request, request.user, body)
        return Response(ShareRow(share, context={"request": request}).data, status=status.HTTP_201_CREATED)


class ClearExpiredView(APIView):
    permission_classes = [IsMember]

    def post(self, request):
        return Response({"deleted": _clear_expired(request, owner=request.user)})


def _clear_expired(request, owner=None):
    """清理所有已过期分享 (the owner's; owner None: everybody's), logged in the activity log when it deletes some."""
    with transaction.atomic():
        deleted = service.clear_expired(owner=owner)
        if deleted:
            activity.record(activity.Kind.CLEAR_SHARES, actor=request.user,
                            detail={"count": deleted, **({"everyone": True} if owner is None else {})})
    return deleted


class _VisitorView(APIView):
    """A visitor's request to one share: the share (else 404), this browser's visit of it. Every answer,
    refusals too, carries the visit's cookie when it changed (a bad one removed, a renewal)."""

    permission_classes = [AllowAny]
    _visit = None

    def visit(self, request, key):
        share = shares.find(key)
        self._visit = shares.Visit(request._request, share)
        return share, self._visit

    @staticmethod
    def answer(visit, data, status_code=200):
        return Response(data, status=status_code)

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        if self._visit is not None:
            self._visit.write(response)
        add_never_cache_headers(response)  # what it says depends on the share's cookie
        return response


class ShareView(_VisitorView):
    """GET the share's public info; DELETE it (the owner or a superuser)."""

    def get_permissions(self):
        if self.request.method == "DELETE":
            return [IsMember()]
        return [AllowAny()]

    def get(self, request, key):
        share, visit = self.visit(request, key)
        return self.answer(visit, service.public_info(request._request, share, visit))

    def delete(self, request, key):
        return _delete(request, key)


def _delete(request, key):
    share = Share.objects.filter(key=key).first() if isinstance(key, str) and shares.KEY_RE.fullmatch(key) else None
    if share is None:
        raise shares.ShareError(shares.SHARE_NOT_FOUND, _("The share link doesn't exist; it may have been deleted."), status=404)
    if not service.may_delete(request.user, share):
        raise shares.ShareError("not_owner", _("Only the person who shared it or a superuser can delete this share."), status=403)
    with transaction.atomic():
        activity.record(activity.Kind.DELETE_SHARE, actor=request.user,
                        target=share.owner if share.owner_id != request.user.pk else None, storage=share.storage,
                        path=share.folder, key=share.key, detail=service.log_detail(share))
        share.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


class VerifyView(_VisitorView):
    def post(self, request, key):
        enforce_csrf(request)
        _body_at_most(request, VISITOR_BODY_MAX, _("What was sent is too large."))
        share, visit = self.visit(request, key)
        body = _checked(VerifyBody, request.data)
        shares.check_password(request._request, share, visit, body["password"])
        return self.answer(visit, {"verified": True})


class ListingView(_VisitorView):
    def get(self, request, key):
        query = _checked(ListingQuery, request.query_params)
        share, visit = self.visit(request, key)
        shares.require_verified(share, visit)
        data = service.listing(request._request, share, visit, query["path"], sort=query["sort"] or None,
                               order=query["order"] or None)
        return self.answer(visit, data)


class ItemView(_VisitorView):
    def get(self, request, key):
        query = _checked(ItemQuery, request.query_params)
        share, visit = self.visit(request, key)
        shares.require_verified(share, visit)
        return self.answer(visit, service.item(request._request, share, visit, query["path"]))


class UnlockView(_VisitorView):
    """A folder password inside the share, kept in the share's cookie (never 记住密码)."""

    def post(self, request, key):
        enforce_csrf(request)
        _body_at_most(request, VISITOR_BODY_MAX, _("What was sent is too large."))
        share, visit = self.visit(request, key)
        shares.require_verified(share, visit)
        body = _checked(UnlockBody, request.data)
        if not body["password"]:
            return Response({"password": [_("Please enter the password.")]}, status=status.HTTP_400_BAD_REQUEST)
        access = shares.open_share(request._request, share, visit)
        if "rule" in body:
            rule = shares.unlock_folder(share, access, password=body["password"], rule_id=body["rule"])
        else:
            rule = shares.unlock_folder(share, access, password=body["password"],
                                        path=shares.resolve(access, share, body["path"]))
        return self.answer(visit, {"unlocked": rule is not None, "rule": rule.pk if rule is not None else None})


class UnlocksView(_VisitorView):
    def delete(self, request, key):
        enforce_csrf(request)
        share, visit = self.visit(request, key)
        visit.forget()
        return self.answer(visit, None, status.HTTP_204_NO_CONTENT)


class ManageSharesView(QueryParamsMixin, ListAPIView):
    """分享列表: everybody's shares."""

    permission_classes = [IsAdmin]
    serializer_class = ManageShareRow
    query_serializer_class = ManageListQuery

    def get_queryset(self):
        return service.filtered(Share.objects.select_related("storage", "owner"), self.query)


class ManageShareView(APIView):
    permission_classes = [IsAdmin]

    def delete(self, request, key):
        return _delete(request, key)


class ManageClearExpiredView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        return Response({"deleted": _clear_expired(request)})
