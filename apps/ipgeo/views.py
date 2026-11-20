"""IP 归属地 on /manage/#ip-geo, under /api/v1/. Admins only.

    GET    manage/ip-geo/            the settings, the database in use, the last job and how many stored IPs
                                     have no location yet
    PATCH  manage/ip-geo/            {"enabled": true|false}
    POST   manage/ip-geo/download/   download (or update) the database in the background (202)
    POST   manage/ip-geo/fill/       {"redo": false}: 补全 the stored IPs in the background (202)
    GET    manage/ip-geo/job/        the last job: {"job": {"kind", "state", "done", "total", "message", ...}}
    GET    manage/ip-geo/check/      ?ip= -> {"ip", "code", "location"}

Downloading and 补全 answer 409 while IP 归属地 is off, without a database (补全, check) and while
another job runs.
"""

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.exceptions import Conflict
from apps.core.permissions import IsAdmin
from apps.core.times import iso

from . import jobs, lookup, places
from .models import IpGeoSettings
from .serializers import CheckQuery, FillSerializer, IpGeoSettingsSerializer


def describe(config):
    """GET manage/ip-geo/."""
    database = None
    if config.file_name:
        database = {
            "file": config.file_name,
            "size": config.size,
            "sha256": config.sha256,
            "built_at": iso(config.built_at),
            "downloaded_at": iso(config.downloaded_at),
            "missing": not lookup.database_path(config.file_name).is_file(),
        }
    return {
        "enabled": config.enabled,
        "folder": str(settings.GEOIP_DIR),
        "database": database,
        "job": _job(config),
        "unfilled": jobs.unfilled(),
    }


def _job(config):
    job = jobs.job_status(config)
    if job:
        job["started_at"] = iso(job["started_at"])
        job["finished_at"] = iso(job["finished_at"])
    return job


class IpGeoView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(describe(IpGeoSettings.load()))

    def patch(self, request):
        serializer = IpGeoSettingsSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        enabled = serializer.validated_data["enabled"]
        IpGeoSettings.load()
        with transaction.atomic():
            config = IpGeoSettings.objects.select_for_update().get(pk=1)
            if config.enabled != enabled:
                config.enabled = enabled
                config.save(update_fields=["enabled", "updated_at"])
                lookup.forget_settings()
        return Response(describe(config))


class IpGeoDownloadView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        try:
            jobs.start_download()
        except (jobs.NotReady, jobs.Busy) as exc:
            raise Conflict(str(exc)) from None
        return Response(describe(IpGeoSettings.load()), status=status.HTTP_202_ACCEPTED)


class IpGeoFillView(APIView):
    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = FillSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            jobs.start_fill(serializer.validated_data["redo"])
        except (jobs.NotReady, jobs.Busy) as exc:
            raise Conflict(str(exc)) from None
        return Response(describe(IpGeoSettings.load()), status=status.HTTP_202_ACCEPTED)


class IpGeoJobView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        return Response({"job": _job(IpGeoSettings.load())})


class IpGeoCheckView(APIView):
    permission_classes = [IsAdmin]

    def get(self, request):
        serializer = CheckQuery(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        ip = serializer.validated_data["ip"]
        config = IpGeoSettings.load()
        if not config.enabled:
            raise Conflict(_("Turn IP location on first."))
        found = lookup.locator(config)
        if found is None:
            raise Conflict(_("There is no usable database yet; download one first."))
        location, names = found.place(ip)
        lookup.remember(names)
        return Response({"ip": ip, "code": location, "location": places.name(location, {**lookup.current_names(), **names})})
