"""{% ipgeo_source as geo %}: where /manage/#ip-geo downloads from (apps/ipgeo/templates/ipgeo/manage/ip_geo.html)."""

from django import template

from apps.ipgeo import sources

register = template.Library()


@register.simple_tag
def ipgeo_source():
    """{"host", "site_url", "city", "country"} of apps/ipgeo/sources.py: the site, and the two databases' file names."""
    return {"host": sources.HOST, "site_url": sources.SITE_URL, "city": sources.CITY.file, "country": sources.COUNTRY.file}
