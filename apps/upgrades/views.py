import platform

import django
from django.conf import settings
from django.db import connection
from rest_framework.response import Response
from rest_framework.views import APIView

from config.version import VERSION
from apps.core.permissions import IsAdmin

from . import service
from .models import UpgradeLog
from .serializers import UpgradeLogSerializer

HISTORY = 100  # rows of the upgrade log shown


def database_version():
    """"SQLite 3.50.4" or "MySQL 8.4.11"."""
    if connection.vendor == "sqlite":
        return f"SQLite {connection.Database.sqlite_version}"
    return f"MySQL {connection.mysql_server_info}"


class VersionView(APIView):
    """GET /manage/version/: the program's version, whether the database matches it, and the upgrade
    log (/manage/#version). Admins only; it changes nothing."""

    permission_classes = [IsAdmin]

    def get(self, request):
        current = service.status()
        upgrades = UpgradeLog.objects.all()[:HISTORY] if current.last else []  # no table yet, or empty
        return Response(
            {
                "version": VERSION,
                "django": django.get_version(),
                "python": platform.python_version(),
                "database": database_version(),
                "auto_upgrade": settings.AUTO_UPGRADE,
                "frozen": settings.FROZEN,
                "backup_dir": str(settings.BACKUP_DIR),
                "pending": current.pending,
                "newer": current.newer,
                "upgrades": UpgradeLogSerializer(upgrades, many=True).data,
            }
        )
