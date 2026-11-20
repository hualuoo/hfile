"""IP 归属地: where an IP address is, the country or region and, for many, the province or state and the city.

locate(ip) gives the location stored next to an IP ("CN 1809935 1795565", "US", "PRIVATE", "unknown";
apps/ipgeo/places.py), or "" while IP 归属地 is turned off or no database has been downloaded
(/manage/#ip-geo); shown(location) its name for a page, in the page's language, "" while IP 归属地 is off.
The lookup happens on this server, in the GeoLite2-City file downloaded from ip.adysec.com
(apps/ipgeo/sources.py); addresses are never sent anywhere.

The names come with the records: a lookup keeps the names of the places it met (remember(),
models.PlaceName), once per place and process. Reading them all out of the file when it is downloaded would
mean decoding each of its millions of networks' records, minutes in pure Python.

The file is read with MaxMind's reader in its pure Python mode, into memory: once per process, and
again when the settings name another download.
"""

import ipaddress
import logging
import threading
from pathlib import Path

import maxminddb
from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils.translation import gettext as _

from apps.core import net

from . import places
from .places import PRIVATE
from .sources import LOCATION_MAX, UNKNOWN

logger = logging.getLogger(__name__)


class DatabaseProblem(Exception):
    """A file that can't be used as the IP database; the message says why."""


def database_path(file_name):
    return Path(settings.GEOIP_DIR) / file_name


def parse_ip(value):
    """An ipaddress object for an address as apps.core.net.client_ip() writes it (a zone such as %eth0 dropped,
    an IPv4-mapped IPv6 address as IPv4), or None for anything else."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(str(value).strip().split("%", 1)[0])
    except ValueError:
        return None
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address


def place_for(record):
    """(location, names) of a database record: the location to store (apps/ipgeo/places.py), UNKNOWN for a
    record without a country, and the names of its places, {code: {"en": ..., "zh-CN": ...}} for those that
    have any.

    Records come from a downloaded file, so nothing about their shape is taken for granted. Some networks
    have only the country they are registered in (anycast ones such as 1.1.1.1).
    """
    if not isinstance(record, dict):
        return UNKNOWN, {}
    for key in ("country", "registered_country"):
        country = record.get(key)
        if code := places.country_code(country):
            break
    else:
        return UNKNOWN, {}
    found = [(code, country)]
    subdivisions = record.get("subdivisions")
    if isinstance(subdivisions, list) and subdivisions:
        found.append((places.geoname_id(subdivisions[0]), subdivisions[0]))
    found.append((places.geoname_id(record.get("city")), record.get("city")))
    found = [(code, place) for code, place in found if code]
    names = {code: places.names_of(place) for code, place in found}
    return " ".join(code for code, _place in found), {code: known for code, known in names.items() if known}


class Locator:
    """Looks addresses up in one database file."""

    def __init__(self, reader):
        self.reader = reader

    def __call__(self, ip):
        return self.place(ip)[0]

    def place(self, ip):
        """(location, names) of an address (place_for); ("", {}) for what isn't one or a lookup that failed."""
        address = parse_ip(ip)
        if address is None:
            return "", {}
        # Loopback, LAN, link-local, documentation and the like (the file doesn't list them).
        if not address.is_global:
            return PRIVATE, {}
        try:
            record = self.reader.get(address)
        except Exception as exc:  # a damaged file must not break what is being logged
            logger.warning("IP location: looking up %s failed: %s", address, exc)
            return "", {}
        return place_for(record)


def open_reader(path):
    """Read a database file into memory and check that it is a city database that knows IPv6 too.

    Raises DatabaseProblem.
    """
    try:
        reader = maxminddb.open_database(str(path), mode=maxminddb.MODE_MEMORY)
        meta = reader.metadata()
        database_type, ip_version = str(meta.database_type), meta.ip_version
    except FileNotFoundError:
        raise DatabaseProblem(_("The database file %(path)s isn't there.") % {"path": path}) from None
    except Exception as exc:  # maxminddb raises TypeError, KeyError ... too on a damaged file
        raise DatabaseProblem(_("It isn't a MaxMind database, or the file is damaged (%(error)s).") % {"error": exc}) from None
    if "City" not in database_type or ip_version != 6:
        raise DatabaseProblem(_("It isn't a city database such as GeoLite2-City (type %(type)s, IPv%(version)s).")
                              % {"type": database_type, "version": ip_version})
    try:
        for address in ("1.1.1.1", "2001:4860:4860::8888"):
            reader.get(address)
    except Exception as exc:
        raise DatabaseProblem(_("The database can't be read (%(error)s).") % {"error": exc}) from None
    return reader


# The Locator of the database in use, by (path, checksum, download time): a new download changes the
# key, so the process reads the new file the first time it needs it.
_lock = threading.Lock()
_cached = {}


def locator(config):
    """A Locator for the database IpGeoSettings `config` names, or None without one or when its
    file can't be read (logged once per download)."""
    if not (config.file_name and config.sha256):
        return None
    path = database_path(config.file_name)
    key = (str(path), config.sha256, config.downloaded_at)
    with _lock:
        if key not in _cached:
            try:
                found = Locator(open_reader(path))
            except Exception as exc:  # remembered: the file isn't read again until the next download
                logger.warning("The IP location database can't be used: %s", exc)
                found = None
            _cached.clear()  # the old reader goes when nothing uses it any more
            _cached[key] = found
        return _cached[key]


def forget_cached():
    with _lock:
        _cached.clear()


# The names this process has saved, by code: a place is written once (and again when its names change).
_remembered = {}
_remembered_lock = threading.Lock()


def remember(found):
    """Keep the names of the places in `found` ({code: names}) for the pages (models.PlaceName). Never raises.

    In a savepoint of the caller's transaction, if there is one; this process takes them for saved once
    that is committed (a rollback takes them back too).
    """
    from .models import PlaceName

    with _remembered_lock:
        new = {code: names for code, names in found.items() if code and names and _remembered.get(code) != names}
    if not new:
        return
    # An upsert: MySQL takes the primary key as the conflict; SQLite is told which column it is.
    target = {"unique_fields": ["code"]} if connection.features.supports_update_conflicts_with_target else {}
    try:
        with transaction.atomic():
            PlaceName.objects.bulk_create([PlaceName(code=code, names=names) for code, names in new.items()],
                                          update_conflicts=True, update_fields=["names"], **target)
    except DatabaseError:
        logger.warning("IP location: the names of %s couldn't be saved", ", ".join(new), exc_info=True)
        return

    def saved():
        with _remembered_lock:
            _remembered.update(new)

    transaction.on_commit(saved)


def forget_remembered():
    with _remembered_lock:
        _remembered.clear()


def current_settings():
    """IpGeoSettings, read once per request (apps.core.net keeps the request) and on every call outside one."""
    from .models import IpGeoSettings

    request = net.current_request()
    if request is None:
        return IpGeoSettings.load()
    config = getattr(request, "_ipgeo_settings", None)
    if config is None:
        config = request._ipgeo_settings = IpGeoSettings.load()
    return config


def forget_settings():
    """After changing the settings in a request: read them again."""
    request = net.current_request()
    if request is not None and hasattr(request, "_ipgeo_settings"):
        del request._ipgeo_settings


def current_names():
    """{code: names} of every PlaceName, read once per request, as current_settings()."""
    from .models import PlaceName

    request = net.current_request()
    names = getattr(request, "_ipgeo_names", None) if request is not None else None
    if names is None:
        names = dict(PlaceName.objects.values_list("code", "names"))
        if request is not None:
            request._ipgeo_names = names
    return names


def showing():
    """Whether the admin's pages show locations (IP 归属地 turned on)."""
    return current_settings().enabled


def shown(location):
    """The name of a stored location for a page, or "" while IP 归属地 is turned off."""
    if not location or not current_settings().enabled:
        return ""
    return places.name(location, current_names())


def locate(ip):
    """The location to store with `ip`, or "" when IP 归属地 is off, there is no usable database or no
    address.

    Never raises: logging in and writing the login and download logs call it, and none of them may fail
    because of the database file.
    """
    if not ip:
        return ""
    try:
        config = current_settings()
        if not config.enabled:
            return ""
        found = locator(config)
        if found is None:
            return ""
        location, names = found.place(ip)
        remember(names)
        return location[:LOCATION_MAX]
    except Exception:
        logger.exception("IP location: looking up %s failed", ip)
        return ""
