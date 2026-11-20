"""The site's own address, for the few absolute URLs it hands out (decision D14): OAuth and SSO
callbacks, links users copy, addresses given to OnlyOffice and kkFileView. Everything else links with
paths.

It is [site] url when that is set (config/configfile.py keeps it as "scheme://host[:port]"), else the
address the request came in on: its host is one of allowed_hosts (Django refuses others), and its
scheme is https behind a proxy only with [server] proxies set (SECURE_PROXY_SSL_HEADER). Callbacks
registered at a provider should use [site] url, so that they don't change with the address someone
typed; pages that show one say so.
"""

from __future__ import annotations

from django.conf import settings


def site_origin(request=None) -> str:
    """"https://files.example.com", without a slash at the end; "" when there is neither [site] url
    nor a request."""
    if settings.SITE_URL:
        return settings.SITE_URL.rstrip("/")
    if request is None:
        return ""
    return f"{request.scheme}://{request.get_host()}"


def absolute(path: str, request=None) -> str:
    """The absolute URL of `path`, a path of this site ("/sso/github/login/callback"). Raises
    ValueError for anything else, and when the origin isn't known."""
    if not isinstance(path, str) or not path.startswith("/") or path.startswith("//"):
        raise ValueError("a path of this site starts with a single /")
    origin = site_origin(request)
    if not origin:
        raise ValueError("no [site] url and no request: the site's address isn't known")
    return origin + path


def from_site_url() -> bool:
    """Whether absolute URLs come from [site] url (stable) rather than from each request."""
    return bool(settings.SITE_URL)
