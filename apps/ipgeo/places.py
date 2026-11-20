"""The locations stored with the IPs, and their names in the page's language.

A location is stored as codes with spaces between them, from the largest place to the smallest:
- the country or region: its ISO code (CN, US, HK);
- then those of these two the database knows: the province or state (the first subdivision: England, not
  Suffolk in it) and the city, by their GeoNames IDs, which are numbers. "CN 1809935 1795565" is Shenzhen,
  in Guangdong, China;
- or a single PRIVATE (an address that isn't a public one) or UNKNOWN (one the database doesn't list).

The names are the database's, in English and Chinese (zh-CN), kept by code as the lookups met them
(models.PlaceName, apps.ipgeo.lookup.remember). A place without a name in the page's language has its
English one; one without any is left out, a country shown as its code. Hong Kong, Macao and Taiwan are
written as Chinese sites write them: 中国香港, 中国澳门, 中国台湾.
"""

import re

from django.utils import translation
from django.utils.translation import pgettext, pgettext_lazy

from .sources import UNKNOWN

PRIVATE = "PRIVATE"  # every address that isn't a public one
NAME_MAX = 100  # characters of a name kept from the database

# The locations that aren't a place.
OWN_NAMES = {
    PRIVATE: pgettext_lazy("IP location", "Private network"),
    UNKNOWN: pgettext_lazy("IP location", "Unknown"),
}
# The countries and regions the site names itself.
REGIONS = {
    "HK": pgettext_lazy("IP location", "Hong Kong"),
    "MO": pgettext_lazy("IP location", "Macao"),
    "TW": pgettext_lazy("IP location", "Taiwan"),
}

LANGUAGES = ("en", "zh-CN")  # the names kept of each place, of the eight in the file
FILE_LANGUAGE = {"zh-hans": "zh-CN"}  # a page's language as the file names it; en is the same

_COUNTRY_CODE = re.compile(r"[A-Z]{2}")
_GEONAME_MAX = 10**10  # GeoNames IDs have eight digits now


def clean(value):
    """Text from a downloaded file as it may be kept and shown: printable, at most NAME_MAX long."""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value if ch.isprintable()).strip()[:NAME_MAX].strip()


def country_code(place):
    """The ISO code of a record's country ({"iso_code": "US", "names": {...}}), "" without one."""
    code = place.get("iso_code") if isinstance(place, dict) else None
    code = code.strip().upper() if isinstance(code, str) else ""
    return code if _COUNTRY_CODE.fullmatch(code) else ""


def geoname_id(place):
    """The GeoNames ID of a record's subdivision or city ({"geoname_id": 1795565, ...}) as text, "" without one."""
    value = place.get("geoname_id") if isinstance(place, dict) else None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < _GEONAME_MAX:
        return ""
    return str(value)


def names_of(place):
    """{"en": ..., "zh-CN": ...} of a place of a record, those of LANGUAGES it has."""
    names = place.get("names") if isinstance(place, dict) else None
    names = names if isinstance(names, dict) else {}
    return {language: text for language in LANGUAGES if (text := clean(names.get(language)))}


def _known(code, names):
    known = names.get(code)
    if not isinstance(known, dict):
        return {}
    return {language: text for language in LANGUAGES if isinstance(text := known.get(language), str) and text}


def _same(one, other):
    """Whether two parts, (name shown, names), are one place: the same name in a language."""
    return one[0] == other[0] or any(one[1].get(language) and one[1].get(language) == other[1].get(language)
                                     for language in LANGUAGES)


def name(location, names):
    """A stored location as the pages show it, in the active language ("中国 广东 深圳", "Shenzhen,
    Guangdong, China"); `names` is {code: {"en": ..., "zh-CN": ...}}."""
    codes = location.split() if isinstance(location, str) else []
    if not codes:
        return ""
    if location in OWN_NAMES:
        return str(OWN_NAMES[location])
    names = names if isinstance(names, dict) else {}
    language = translation.get_language() or ""
    language = FILE_LANGUAGE.get(language, language)

    def shown(known):
        return known.get(language) or known.get("en") or ""

    country_code, *smaller = codes
    known = _known(country_code, names)
    country = (str(REGIONS[country_code]) if country_code in REGIONS else shown(known) or country_code, known)
    parts = []
    for code in smaller:
        known = _known(code, names)
        part = (shown(known), known)
        if part[0] and not _same(part, country):  # Singapore's city is Singapore, Hong Kong's Hong Kong
            parts.append(part)
    if len(parts) == 2 and _same(*parts):  # a province named as its city (北京市 and 北京): the city says it
        del parts[0]
    text = country[0]
    for part in parts:
        text = pgettext("IP location: a place in a larger one", "%(smaller)s, %(larger)s") % {"smaller": part[0], "larger": text}
    return text
