from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class IpGeoConfig(AppConfig):
    """IP 归属地: which country or region the IPs of the admin's lists belong to (/manage/#ip-geo): the users'
    last login IPs, the activity log and the download log."""

    name = "apps.ipgeo"
    verbose_name = gettext_lazy("IP location")
