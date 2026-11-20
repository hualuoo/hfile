"""Where /manage/#ip-geo downloads its database from: MaxMind's GeoLite2-City, as ip.adysec.com serves it.

GeoLite2-City is a MaxMind DB file (.mmdb, about 50 MB) of where addresses are: the country or region and,
for many of them, the province or state and the city, with names in eight languages, English and Chinese
among them. MaxMind updates it every Tuesday and Friday. MaxMind's own download wants an account and a
license key, and refuses addresses in mainland China; adysec/IP_database (https://github.com/adysec/IP_database)
copies the file every day from P3TERX/GeoLite.mmdb and serves it at ip.adysec.com with neither. It publishes
no checksum: a download is checked by opening it (apps.ipgeo.lookup.open_reader).

What a record stores is a code, not a name (apps/ipgeo/places.py).
"""

LOCATION_MAX = 40  # characters of the columns that store an IP's location
UNKNOWN = "unknown"  # an address the database doesn't list (lower case: the countries' codes are upper case)

FILE_NAME = "GeoLite2-City.mmdb"  # in [paths] geoip
HOST = "ip.adysec.com"
SITE_URL = f"https://{HOST}/"
URL = f"https://{HOST}/geolite/{FILE_NAME}"
