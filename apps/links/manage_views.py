"""For admins (apps.core.permissions.IsAdmin): 短链管理 and 下载日志 in /manage/, the group 链接 (inventory 9.9,
9.10; decision D40), under /api/v1/:

    GET    manage/short-links/                ?storage= &path= &key= &since= &until= &ordering= &page= &page_size=
                                              newest first; ordering by created_at, expires_at, key, id
    DELETE manage/short-links/<id>/           one link (its download log stays)
    POST   manage/short-links/delete/         {"ids": [...]}: those links (their logs stay)
    POST   manage/short-links/delete-expired/ the links expired now and their download logs, in one transaction
                                              -> {"deleted", "logs"}
    GET    manage/short-links/export/         the same filters: every matching link as CSV (UTF-8 with a BOM)
    GET    manage/link-limits/                the rate limit's live windows (统计信息): {"seconds", "downloads",
                                              "count", "windows": [{"address", "count", "started_at", "expires_at"}]}

    GET    manage/download-logs/              ?storage= &kind= &key= &path= &user= &ip= &user_agent= &referer= &since=
                                              &until= &page= newest first; the answer's "newest" is the newest
                                              matching id
    DELETE manage/download-logs/              the same filters, and ?newest= from the list: deletes every entry they
                                              find that the list could have shown (none logged after it)
    POST   manage/download-logs/delete/       {"ids": [...]}

The deletes answer {"deleted": n} and are logged in the activity log (how many, not which). Text filters are
Django lookups (LIKE with its wildcards escaped).
"""

from __future__ import annotations

import csv
import datetime
import io

from django.db import transaction
from django.db.models import F, Max
from django.db.models.functions import Lower
from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.http import content_disposition_header
from django.utils.translation import gettext, gettext_lazy, pgettext_lazy
from rest_framework.generics import ListAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activity import log as activity
from apps.core.filters import StableOrderingFilter
from apps.core.permissions import IsAdmin
from apps.core.query import QueryParamsMixin
from apps.core.times import iso
from apps.siteconf.models import SiteSettings

from . import ratelimit, service
from .manage_serializers import (
    DownloadLogQuery,
    DownloadLogSerializer,
    IdsSerializer,
    ShortLinkQuery,
    ShortLinkSerializer,
)
from .models import DownloadLog, ShortLink

WINDOWS_SHOWN = 500


def _start_of(day):
    """Midnight at the start of `day` in the site's time zone (TIME_ZONE)."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time.min))


def _days(queryset, field, query):
    if query["since"]:
        queryset = queryset.filter(**{f"{field}__gte": _start_of(query["since"])})
    if query["until"]:
        queryset = queryset.filter(**{f"{field}__lt": _start_of(query["until"] + datetime.timedelta(days=1))})
    return queryset


def _key_contains(queryset, field, text):
    """Keys compare exactly in the database (a binary collation on MySQL): a part typed in any case is
    looked for in their lower-case form, the same on both databases."""
    if not text:
        return queryset
    return queryset.annotate(_key_lower=Lower(field)).filter(_key_lower__contains=text.lower())


def short_links(query):
    links = ShortLink.objects.select_related("storage", "created_by")
    if query["storage"]:
        links = links.filter(storage_id=query["storage"])
    if query["path"]:
        links = links.filter(path__icontains=query["path"])
    links = _key_contains(links, "key", query["key"])
    return _days(links, "created_at", query)


def download_logs(query):
    logs = DownloadLog.objects.select_related("storage")
    if query["storage"]:
        logs = logs.filter(storage_id=query["storage"])
    if query["kind"]:
        logs = logs.filter(kind=query["kind"])
    logs = _key_contains(logs, "short_key", query["key"])
    if query["path"]:
        logs = logs.filter(path__icontains=query["path"])
    if query["user"]:
        logs = logs.filter(username__icontains=query["user"])
    if query["ip"]:
        logs = logs.filter(ip__startswith=query["ip"])
    if query["user_agent"]:
        logs = logs.filter(user_agent__icontains=query["user_agent"])
    if query["referer"]:
        logs = logs.filter(referer__icontains=query["referer"])
    return _days(logs, "created_at", query)


# --- 短链管理 ---------------------------------------------------------------------------------------

class LinkOrdering(StableOrderingFilter):
    """?ordering= by the view's fields; by expires_at the links that never expire come last (first when
    descending), on both databases."""

    def filter_queryset(self, request, queryset, view):
        terms = []
        for term in self.get_ordering(request, queryset, view):
            if term.lstrip("-") == "expires_at":
                term = F("expires_at").desc(nulls_first=True) if term.startswith("-") else F("expires_at").asc(nulls_last=True)
            terms.append(term)
        return queryset.order_by(*terms)


class ShortLinkListView(QueryParamsMixin, ListAPIView):
    permission_classes = [IsAdmin]
    serializer_class = ShortLinkSerializer
    query_serializer_class = ShortLinkQuery
    filter_backends = [LinkOrdering]
    ordering_fields = ["created_at", "expires_at", "key", "id"]
    ordering = ["-created_at"]

    def get_queryset(self):
        return short_links(self.query)

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "now": timezone.now()}


class ShortLinkView(APIView):
    permission_classes = [IsAdmin]
    http_method_names = ["delete", "options"]

    def delete(self, request, pk):
        link = get_object_or_404(ShortLink.objects.select_related("storage", "created_by"), pk=pk)
        with transaction.atomic():
            activity.record(activity.Kind.DELETE_SHORT_LINKS, actor=request.user, target=link.created_by,
                            storage=link.storage, path=link.path, key=link.key, detail={"count": 1})
            link.delete()
        return Response({"deleted": 1})


class ShortLinkDeleteView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        form = IdsSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        with transaction.atomic():
            deleted, _ = ShortLink.objects.filter(pk__in=form.validated_data["ids"]).delete()
            if deleted:
                activity.record(activity.Kind.DELETE_SHORT_LINKS, actor=request.user, detail={"count": deleted})
        return Response({"deleted": deleted})


class ShortLinkExpiredView(APIView):
    """删除过期短链: those expired at one moment, and the download logs of exactly those keys."""

    permission_classes = [IsAdmin]

    def post(self, request):
        with transaction.atomic():
            deleted, logs = service.delete_expired(timezone.now())
            if deleted:
                activity.record(activity.Kind.DELETE_SHORT_LINKS, actor=request.user,
                                detail={"count": deleted, "expired": True, "logs": logs})
        return Response({"deleted": deleted, "logs": logs})


# The columns of the export (inventory 9.9).
EXPORT_COLUMNS = ["ID", gettext_lazy("Storage source name"), gettext_lazy("Storage type"), gettext_lazy("Short link key"), pgettext_lazy("address", "Short link"), gettext_lazy("File path"), gettext_lazy("Created by"), gettext_lazy("Created at"), gettext_lazy("Expires at")]


def _cell(value) -> str:
    """A CSV cell that a spreadsheet won't run: text starting with = + - @ (a formula) gets a ' in front."""
    text = "" if value is None else str(value)
    if text[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + text
    return text


def _local(moment) -> str:
    return timezone.localtime(moment).strftime("%Y-%m-%d %H:%M:%S") if moment else ""


class ShortLinkExportView(QueryParamsMixin, APIView):
    """导出: every link the filters find (not just a page), newest first."""

    permission_classes = [IsAdmin]
    query_serializer_class = ShortLinkQuery

    def get(self, request):
        links = short_links(self.query).order_by("-created_at", "-id")
        serializer = ShortLinkSerializer(context={"request": request})

        def rows():
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            yield "\ufeff"  # the BOM: Excel then reads the file as UTF-8
            writer.writerow(EXPORT_COLUMNS)
            for link in links.iterator(chunk_size=1000):
                user = link.created_by
                writer.writerow([_cell(item) for item in (
                    link.pk, link.storage.name, link.storage.type_label, link.key, serializer.get_url(link),
                    link.path, user.username, _local(link.created_at), _local(link.expires_at) or gettext("Never"),
                )])
                if buffer.tell() > 64 * 1024:
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate()
            yield buffer.getvalue()

        name = gettext("HFile short links - %(time)s.csv") % {"time": timezone.localtime().strftime("%Y-%m-%d %H%M%S")}
        response = StreamingHttpResponse(rows(), content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = content_disposition_header(True, name)
        response["Cache-Control"] = "no-store"
        return response


class LinkLimitsView(APIView):
    """统计信息: the rate limit's windows that haven't ended, the busiest first (at most WINDOWS_SHOWN)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        site = SiteSettings.load()
        seconds, downloads = site.link_limit_seconds, site.link_limit_downloads
        windows = ratelimit.limiter.windows(seconds) if seconds and downloads else []

        def moment(value):
            return iso(datetime.datetime.fromtimestamp(value, tz=datetime.timezone.utc))

        return Response({
            "seconds": seconds, "downloads": downloads, "count": len(windows),
            "windows": [{"address": window.address, "count": window.count, "started_at": moment(window.started),
                         "expires_at": moment(window.expires)} for window in windows[:WINDOWS_SHOWN]],
        })


# --- 下载日志 ---------------------------------------------------------------------------------------

class DownloadLogListView(QueryParamsMixin, ListAPIView):
    permission_classes = [IsAdmin]
    serializer_class = DownloadLogSerializer
    query_serializer_class = DownloadLogQuery
    http_method_names = ["get", "delete", "head", "options"]

    def get_queryset(self):
        return download_logs(self.query).order_by("-created_at", "-id")

    def get_serializer_context(self):
        return {**super().get_serializer_context(), "prefix": SiteSettings.load().direct_link_prefix}

    def list(self, request, *args, **kwargs):
        response = super().list(request, *args, **kwargs)
        response.data["newest"] = download_logs(self.query).aggregate(newest=Max("id"))["newest"]
        return response

    def delete(self, request):
        query = self.strict_query()
        logs = download_logs(query)
        if query["newest"]:
            logs = logs.filter(pk__lte=query["newest"])
        with transaction.atomic():
            deleted, _ = logs.delete()
            if deleted:
                activity.record(activity.Kind.DELETE_DOWNLOAD_LOGS, actor=request.user, detail={"count": deleted})
        return Response({"deleted": deleted})


class DownloadLogDeleteView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        form = IdsSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        with transaction.atomic():
            deleted, _ = DownloadLog.objects.filter(pk__in=form.validated_data["ids"]).delete()
            if deleted:
                activity.record(activity.Kind.DELETE_DOWNLOAD_LOGS, actor=request.user, detail={"count": deleted})
        return Response({"deleted": deleted})

