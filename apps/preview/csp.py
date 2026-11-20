"""What the viewers need of the CSP on the pages that show them (the file browser and the shares).

The site-wide part is in config/settings.py SECURE_CSP: worker-src 'self' (pdf.js and hls.js load their
workers from /static/, so script-src never has to allow blob: or eval, and custom JS widening script-src
doesn't widen workers). Here, on those pages only (apps.siteconf.middleware.public_policy):
- media-src 'self' blob:: hls.js plays through MediaSource, and subtitles are made in the page;
- kkFileView's origin in frame-src while it is set to open in a frame. Nothing else may be framed there
  then. A kkFileView on the site's own origin is never framed (it would show stored files in it).
"""

import re
from urllib.parse import urlsplit

from django.conf import settings
from django.utils.csp import CSP

from apps.siteconf.models import KkFileViewMode

# What a CSP source can name (the settings save hosts as punycode; an IPv6 literal can't be named).
_ORIGIN = re.compile(r"https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url):
    """(scheme, host, port) of an http(s) URL, for comparing origins; None for anything else."""
    try:
        parts = urlsplit(url or "")
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    return parts.scheme, parts.hostname.lower(), port or _DEFAULT_PORTS[parts.scheme]


def same_origin(url, other):
    """Whether two http(s) addresses share an origin (scheme, host and port, default ports filled in)."""
    first, second = _origin(url), _origin(other)
    return first is not None and first == second


def kkfileview_origin(site):
    """The origin of the kkFileView server when it opens in a frame and isn't the site's own, else ""."""
    if not site.kkfileview_url or site.kkfileview_open_mode != KkFileViewMode.IFRAME:
        return ""
    if settings.SITE_URL and same_origin(site.kkfileview_url, settings.SITE_URL):
        return ""
    parts = urlsplit(site.kkfileview_url)
    found = f"{parts.scheme}://{parts.netloc}".lower()
    return found if _ORIGIN.fullmatch(found) else ""


def extend(config, site):
    """apps.siteconf.middleware.public_policy hook: blob: media, kkFileView's origin in frame-src."""
    media = config.setdefault("media-src", list(config.get("default-src", [CSP.SELF])))
    if "blob:" not in media:
        media.append("blob:")
    found = kkfileview_origin(site)
    if found:
        frames = config.setdefault("frame-src", [])
        if found not in frames:
            frames.append(found)
