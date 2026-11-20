"""Where /manage/#ip-geo downloads its database from: MaxMind's GeoLite2-City or GeoLite2-Country, as
ip.adysec.com serves them. The admin chooses which (EDITIONS); the site uses one at a time.

They are MaxMind DB files (.mmdb) of where addresses are, with names in eight languages, English and Chinese
among them. GeoLite2-City (about 50 MB) has the country or region and, for many addresses, the province or
state and the city; GeoLite2-Country (about 3 MB) the country or region only. MaxMind updates them every
Tuesday and Friday. MaxMind's own download wants an account and a license key, and refuses addresses in
mainland China; adysec/IP_database (https://github.com/adysec/IP_database) copies the files every day from
P3TERX/GeoLite.mmdb and serves them at ip.adysec.com with neither. It publishes no checksum: a download is
checked by opening it (apps.ipgeo.lookup.open_reader).

What a record stores is a code, not a name (apps/ipgeo/places.py).
"""

from dataclasses import dataclass

LOCATION_MAX = 40  # characters of the columns that store an IP's location
UNKNOWN = "unknown"  # an address the database doesn't list (lower case: the countries' codes are upper case)

HOST = "ip.adysec.com"
SITE_URL = f"https://{HOST}/"


@dataclass(frozen=True)
class Edition:
    """One of the databases there is to choose from."""

    key: str  # what the API and the page call it
    file: str  # its file name, on ip.adysec.com and in [paths] geoip
    kind: str  # what its metadata's database_type has in it ("GeoLite2-City")

    @property
    def url(self):
        return f"https://{HOST}/geolite/{self.file}"


CITY = Edition("city", "GeoLite2-City.mmdb", "City")
COUNTRY = Edition("country", "GeoLite2-Country.mmdb", "Country")
EDITIONS = {edition.key: edition for edition in (CITY, COUNTRY)}
EDITION_OF_FILE = {edition.file: edition for edition in EDITIONS.values()}  # IpGeoSettings.file_name -> its Edition
