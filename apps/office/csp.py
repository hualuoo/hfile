"""The editor page's Content-Security-Policy and where OnlyOffice's script is.

OnlyOffice's editor is a script of the Document Server (api.js) that runs in the page and puts the
editor in a frame of its own origin. The site's pages allow scripts from 'self' only, so the editor has
a page of its own (/onlyoffice/edit/), whose policy is the site's with exactly one origin more, the
Document Server's, in script-src and frame-src. Nothing else is widened: connect-src, img-src and the
rest stay the site's, and the page links no custom CSS or JS (it isn't a public page).
"""

from __future__ import annotations

import re
import urllib.parse

from apps.siteconf.middleware import policy, with_sources

API_SCRIPT = "/web-apps/apps/api/documents/api.js"
# What a CSP source can name: an http(s) origin with a host name or IPv4 address (the settings save hosts
# in lower case, as punycode); not an IPv6 literal.
_ORIGIN = re.compile(r"https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


def server_origin(url: str) -> str:
    """"https://office.example.com" of the OnlyOffice 服务地址, or "" when it isn't one a policy can name."""
    try:
        parts = urllib.parse.urlsplit(url or "")
    except ValueError:
        return ""
    found = f"{parts.scheme}://{parts.netloc}".lower()
    return found if parts.scheme in ("http", "https") and _ORIGIN.fullmatch(found) else ""


def api_url(url: str) -> str:
    """The editor's script: <OnlyOffice 服务地址>/web-apps/apps/api/documents/api.js (the address may have
    a path, a Document Server behind a proxy's /office/; not its query or fragment)."""
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/") + API_SCRIPT, "", ""))


def editor_policy(site, server: str) -> dict:
    """The site's policy (apps.siteconf.middleware.policy, not relaxed: no public page) plus `server`."""
    config = policy(site)
    if server:
        with_sources(config, {"script-src": [server], "frame-src": [server]})
    return config
