"""The 操作日志 for admins, under /api/v1/:

    GET manage/activity/   newest first; ?search= &kind= &category= &storage= &user= &ip= &since= &until= &page=
                           (serializers.ActivityQuery)

Nothing changes or deletes an entry: there is no other method, here or in the Django admin.
"""

import re
from datetime import datetime, time, timedelta

from django.db.models import Q
from django.utils import timezone
from rest_framework.generics import ListAPIView

from apps.core.permissions import IsAdmin
from apps.core.query import QueryParamsMixin

from . import log
from .models import ActivityLog
from .serializers import ActivityQuery, ActivitySerializer

_IP_START = re.compile(r"[0-9A-Fa-f.:]+")


def _start_of(day):
    """Midnight at the start of `day` in the site's time zone (TIME_ZONE)."""
    return timezone.make_aware(datetime.combine(day, time.min))


def search(text):
    """What ?search= finds: user names and nicknames, the storage source's name, the paths and the key, and the
    start of the IP."""
    name = text.removeprefix("@") or text
    match = Q()
    for field in ("actor_name", "target_name", "actor__nickname", "target__nickname", "storage_name"):
        match |= Q(**{f"{field}__icontains": name})
    match |= Q(path__icontains=text) | Q(to_path__icontains=text) | Q(key__icontains=text)
    if _IP_START.fullmatch(text):
        match |= Q(ip__startswith=text.lower())
    return match


class ActivityLogView(QueryParamsMixin, ListAPIView):
    serializer_class = ActivitySerializer
    query_serializer_class = ActivityQuery
    permission_classes = [IsAdmin]
    http_method_names = ["get", "head", "options"]

    def get_queryset(self):
        entries = ActivityLog.objects.select_related("actor", "target", "storage").order_by("-created_at", "-id")
        query = self.query
        if text := query["search"]:
            entries = entries.filter(search(text))
        if query["kind"]:
            entries = entries.filter(kind=query["kind"])
        if query["category"]:
            entries = entries.filter(kind__in=[kind.value for kind in log.CATEGORY_BY_KEY[query["category"]].kinds])
        if query["storage"] is not None:
            entries = entries.filter(storage_id=query["storage"])
        if query["user"] is not None:
            entries = entries.filter(Q(actor_id=query["user"]) | Q(target_id=query["user"]))
        if query["ip"]:
            entries = entries.filter(ip=query["ip"])
        if query["since"]:
            entries = entries.filter(created_at__gte=_start_of(query["since"]))
        if query["until"]:
            entries = entries.filter(created_at__lt=_start_of(query["until"] + timedelta(days=1)))
        return entries
