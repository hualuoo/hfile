"""What a page with the upload panel adds to its Content-Security-Policy.

A direct upload is a request of the page's script to the backend (a presigned S3 URL, a OneDrive upload
session, 又拍云's form API): connect-src must allow it, and the site's policy only allows 'self'. The
upload URLs aren't known before a session is made (bucket hosts, OneDrive's hosts), so a page that may
upload straight to a storage allows https: in connect-src, plus the plain-http address of a bucket
endpoint or download domain where the admin gave one (MinIO on the local network). Pages without
such a storage keep 'self'.

The page's view calls allow_direct_uploads(response, storages); apps.siteconf.middleware.SiteCSPMiddleware
adds response.csp_sources to the policy.
"""

from __future__ import annotations

from collections.abc import Iterable
from urllib.parse import urlsplit

from apps.storages import config as storage_config

_ADDRESS_PARAMS = ("endpoint", "domain")  # the S3 family's: where its presigned URLs point


def _plain_http(value) -> list[str]:
    if not isinstance(value, str) or not value.lower().startswith("http://"):
        return []
    try:
        parts = urlsplit(value.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return []
    if not host or ":" in host or not all(ch.isalnum() or ch in "-." for ch in host) or not host.isascii():
        return []  # IPv6 can't be written in a CSP; odd hosts aren't written either
    origin = f"http://{host}{f':{port}' if port else ''}"
    found = [origin]
    if not host.replace(".", "").isdigit():  # a virtual-hosted bucket is a subdomain of the endpoint
        found.append(f"http://*.{host}{f':{port}' if port else ''}")
    return found


def connect_sources(storage) -> list[str]:
    """connect-src sources for direct uploads to one storage; [] when its uploads go through the site."""
    try:
        cls = storage_config.driver(storage.type)
    except storage_config.ConfigError:
        return []
    if not cls.can_direct_upload or storage.proxy_upload or not storage.enabled:
        return []
    sources = ["https:"]
    stored = storage.config if isinstance(storage.config, dict) else {}
    for key in _ADDRESS_PARAMS:
        for source in _plain_http(stored.get(key)):
            if source not in sources:
                sources.append(source)
    return sources


def allow_direct_uploads(response, storages: Iterable) -> None:
    """Adds what the storages' direct uploads need to the page's policy (response.csp_sources)."""
    wanted: list[str] = []
    for storage in storages:
        for source in connect_sources(storage):
            if source not in wanted:
                wanted.append(source)
    if not wanted:
        return
    extra = dict(getattr(response, "csp_sources", None) or {})
    current = list(extra.get("connect-src", []))
    extra["connect-src"] = current + [source for source in wanted if source not in current]
    response.csp_sources = extra
