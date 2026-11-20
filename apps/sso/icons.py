"""Providers' icons that are http(s) addresses: the pages that show them (the login form, /manage/,
账号绑定) need their origins in the Content-Security-Policy's img-src (middleware.py). data: icons
(the presets') need nothing.

The origins are kept in this process's cache (Django's local cache) for CACHE_SECONDS; saving or deleting
a provider forgets them (apps.py), at once and again on commit, so that a request reading meanwhile
doesn't keep the old ones. Other processes see a change within CACHE_SECONDS.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.db.models import Q

CACHE_SECONDS = 60
CACHE_KEY = "hfile-sso-icon-origins"
# What a CSP host-source can name: a scheme, an ASCII host name (saved as punycode, validation.py), a
# port. A header with other characters is no policy at all; an IPv6 literal can't be named there.
_CSP_ORIGIN = re.compile(r"https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


def origin_of(icon: str) -> str:
    """"https://img.example.com" for an http(s) icon, "" for anything else (and for an origin a CSP
    can't name)."""
    try:
        parts = urlsplit(icon)
    except ValueError:
        return ""
    origin = f"{parts.scheme}://{parts.netloc}".lower()
    return origin if _CSP_ORIGIN.fullmatch(origin) else ""


def origins() -> tuple[str, ...]:
    """The origins of every provider's http(s) icon, enabled or not (/manage/ shows them all)."""
    found = cache.get(CACHE_KEY)
    if found is not None:
        return tuple(found)
    from .models import Provider

    try:
        icons = Provider.objects.filter(Q(icon__startswith="http://") | Q(icon__startswith="https://")).values_list("icon", flat=True)
        found = tuple(sorted({origin for origin in map(origin_of, icons) if origin}))
    except DatabaseError:  # no table yet (before the upgrade ran)
        return ()
    cache.set(CACHE_KEY, found, CACHE_SECONDS)
    return found


def forget(**kwargs):
    """post_save and post_delete of a Provider."""
    cache.delete(CACHE_KEY)
    transaction.on_commit(lambda: cache.delete(CACHE_KEY), robust=True)
