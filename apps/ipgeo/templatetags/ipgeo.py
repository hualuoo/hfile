"""{% ipgeo_source as geo %}: where /manage/#ip-geo downloads from (apps/ipgeo/templates/ipgeo/manage/ip_geo.html)."""

from django import template

from apps.ipgeo import sources

register = template.Library()


@register.simple_tag
def ipgeo_source():
    """{"file", "host", "site_url"} of apps/ipgeo/sources.py."""
    return {"file": sources.FILE_NAME, "host": sources.HOST, "site_url": sources.SITE_URL}
