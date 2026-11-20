"""Where a browser gets a file's bytes from (contract W1; inventory section 8.2).

    url = download_url(request, storage, "/docs/a.pdf")                         # the file's download URL
    url = download_url(request, storage, path, same_origin=True, inline=True)   # for the viewers' JS
    url = download_url(request, storage, path, same_origin=True, absolute=True) # for OnlyOffice, kkFileView

The caller has made its access decision (apps.storages.access). The URL is, by the storage's transfer
options (its columns) and its driver:

1. same_origin=True: always a signed /pd/ link of this site, valid for the storage's link lifetime (one
   hour when its links are permanent). Viewers read text, Markdown and PDF from it with JS, so no
   storage needs CORS for them; OnlyOffice and kkFileView fetch it from outside (absolute=True).
2. a download domain (local, FTP, SFTP, WebDAV: 下载域名): the domain + the storage path, unsigned. A
   web server or CDN in front of the same files serves it.
3. a storage that serves its own downloads (S3 and the like, OneDrive, 115) unless 代理下载 is on: the
   driver's download_url(), signed by the backend where it signs. Where the driver gives none (115
   without the browser's User-Agent), the next. So does an http:// link when the site is https
   (insecure_for()): a page of the site would load it as mixed content, which browsers block or upgrade
   (a server on the LAN, an http mirror, 123's direct links).
4. else a signed /pd/ link (proxied through the site), permanent when the storage's 永久下载链接 is on.

URLs are paths of this site, or absolute when the backend's (or the domain's) are; absolute=True makes
the site's own absolute through apps.core.origin (D14).

For listings, listing_url() gives the same choice without a request to the backend per file: the
drivers whose links cost one (DEFERRED: OneDrive, SharePoint, 115) get a signed /pd/ link that
redirects to the backend's link when it is opened (inventory 8.7, the 115 redirect, signed like /pd/);
an http:// link of theirs on an https site is sent through the site then (apps/files/download.py).
"""

from __future__ import annotations

import logging
import time
import urllib.parse

import idna

from apps.core import origin
from apps.storages import config as storage_config
from apps.storages import instances
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError
from apps.storages.models import WhenFull

from . import signing

logger = logging.getLogger("hfile.files")

# Lifetime of same-origin links of a storage whose links are permanent (W1).
SAME_ORIGIN_PERMANENT_LIFETIME = 3600
# Types whose driver's download_url() asks the backend for each file.
DEFERRED = frozenset({"onedrive", "onedrive-china", "onedrive-app", "onedrive-app-china", "sharepoint",
                      "sharepoint-china", "open115", "dropbox",
                      "yandex-disk", "openlist", "openlist-share", "seafile", "ipfs", "cloudreve", "cloudreve-v3", "lenovo-nas-share",
                      "febbox", "onedrive-share"})
# Types whose links only work for the User-Agent that asked for them.
UA_BOUND = frozenset({"open115"})
# Types whose download_url() is computed locally (a signature), so a sample shows its origin (csp). Not ImgBed: its
# links are made locally too, but redirect a file of its External channel to the host of the link it keeps.
LOCAL_URLS = frozenset({"s3", "aliyun", "tencent", "huawei", "minio", "qiniu", "upyun", "azure-blob", "bunny-storage"})


def user_agent(request) -> str | None:
    if request is None:
        return None
    return request.META.get("HTTP_USER_AGENT") or None


def _driver(storage):
    return instances.get(storage)


def insecure_for(request, url: str | None) -> bool:
    """Whether `url` is an http:// link and the site's pages are https (apps.core.origin.site_origin(): [site] url,
    else the request's own scheme): a page would load it as mixed content, which browsers block (the viewers'
    images, video and audio) or warn about (a download). The site's own link goes out instead."""
    return bool(url) and url[:7].lower() == "http://" and origin.site_origin(request).lower().startswith("https://")


def _lifetime(storage, *, same_origin: bool = False, cap: int | None = None) -> int | None:
    if cap is not None:  # links: never permanent, never longer than asked
        return max(1, min(cap, storage.link_lifetime or cap))
    if storage.permanent_links:
        return SAME_ORIGIN_PERMANENT_LIFETIME if same_origin else None
    return storage.link_lifetime


def _domain_url(storage, storage_path: str) -> str | None:
    domain = (storage.download_domain or "").rstrip("/")
    if not domain or storage.type not in storage_config.DOWNLOAD_DOMAIN_TYPES:
        return None
    return domain + urllib.parse.quote(storage_path, safe="/")


def serves_directly(storage, driver=None) -> bool:
    """Whether the storage's files are downloaded from the backend itself (not 代理下载)."""
    if storage.proxy_download:
        return False
    driver = driver if driver is not None else _driver(storage)
    return bool(getattr(driver, "can_direct_download", False))


def falls_back(storage, driver=None) -> bool:
    """Whether a download through the site that finds the storage full goes to the backend's own link
    (proxy_when_full; apps/files/limits.py), the storage having one."""
    if storage.proxy_when_full != WhenFull.LINK:
        return False
    driver = driver if driver is not None else _driver(storage)
    return bool(getattr(driver, "can_direct_download", False))


def _absolute(url: str, request, absolute: bool) -> str:
    if absolute and url.startswith("/"):
        return origin.absolute(url, request)
    return url


def signed_url(request, storage, storage_path: str, *, inline: bool | None = None, filename: str | None = None,
               absolute: bool = False, same_origin: bool = False, redirect: bool = False,
               ignore_hidden: bool = False, lifetime: int | None = None, not_after: int | None = None) -> str:
    """A signed /pd/ link of the file (always this site's). lifetime, not_after: see download_url()."""
    url = signing.sign(storage, storage_path, lifetime=_lifetime(storage, same_origin=same_origin, cap=lifetime),
                       inline=inline, redirect=redirect, ignore_hidden=ignore_hidden, filename=filename,
                       not_after=not_after)
    return _absolute(url, request, absolute)


def download_url(request, source, storage_path: str, *, filename: str | None = None, inline: bool | None = None,
                 absolute: bool = False, same_origin: bool = False, ignore_hidden: bool = False,
                 lifetime: int | None = None, not_after: int | None = None) -> str:
    """The URL of one file's bytes (W1). `source` is the StorageSource, `storage_path` the file's
    storage path. ignore_hidden: the file needed the actor's 忽略隐藏 to be read, a filter rule would
    refuse it (apps.files.service.needs_bypass()); a /pd/ link then skips the filter rules when it is served,
    as the policy did when it was issued. May raise DriverError (the backend couldn't give its link) and
    apps.storages.instances.StorageUnavailable.

    lifetime (the links app): seconds the URL may stay valid at most, so that what a path or short link
    redirects to doesn't outlive the link's own checks: the site's signed link is then never permanent and
    a bucket's presigned link expires as soon; the backends that ask their server for every link
    (DEFERRED: OneDrive, SharePoint, 115) get the site's signed r=1 link instead, which asks when it is
    opened, with its cache and budget (apps/files/download.py). A download domain's URL can't be limited.

    not_after (the shares app, with lifetime): a Unix time the URL must not outlive, a share's end. The
    site's signed link expires then at the latest (signing rounds expiries up, this caps them), and a
    bucket's presigned link lasts no longer."""
    if not paths.is_clean(storage_path) or storage_path == paths.ROOT:
        raise ValueError("a clean storage path of a file")
    if same_origin:
        return signed_url(request, source, storage_path, inline=inline, filename=filename, absolute=absolute,
                          same_origin=True, ignore_hidden=ignore_hidden, lifetime=lifetime, not_after=not_after)
    url = _domain_url(source, storage_path)
    if url is not None:
        return url
    driver = _driver(source)
    if serves_directly(source, driver):
        if lifetime is not None and source.type in DEFERRED:
            return signed_url(request, source, storage_path, filename=filename, absolute=absolute, redirect=True,
                              ignore_hidden=ignore_hidden, lifetime=lifetime, not_after=not_after)
        expires = source.link_lifetime if lifetime is None else _lifetime(source, cap=lifetime)
        if not_after is not None:
            expires = max(1, min(expires, int(not_after - time.time())))
        url = driver.download_url(storage_path, filename=filename or paths.name(storage_path),
                                  expires=expires, user_agent=user_agent(request))
        if url and not insecure_for(request, url):
            return url
    return signed_url(request, source, storage_path, inline=inline, filename=filename, absolute=absolute,
                      ignore_hidden=ignore_hidden, lifetime=lifetime, not_after=not_after)


class ListingUrls:
    """download_url() for each file of a listing, without asking the backend per file."""

    def __init__(self, request, storage):
        self.request = request
        self.storage = storage
        self.driver = _driver(storage)
        self.direct = serves_directly(storage, self.driver)
        self.failed = False

    def url(self, storage_path: str, *, ignore_hidden: bool = False) -> str:
        """ignore_hidden: this file needed the actor's 忽略隐藏 (apps.files.service.needs_bypass())."""
        url = _domain_url(self.storage, storage_path)
        if url is not None:
            return url
        if self.direct and self.storage.type in DEFERRED:
            return signed_url(self.request, self.storage, storage_path, redirect=True, ignore_hidden=ignore_hidden)
        if self.direct and not self.failed:
            try:
                url = self.driver.download_url(storage_path, filename=paths.name(storage_path),
                                               expires=self.storage.link_lifetime, user_agent=user_agent(self.request))
            except DriverError as error:
                # The listing itself worked: the files are served through the site this time.
                logger.warning("Storage %s: no download link from the backend (%s)", self.storage.key, error.message)
                self.failed = True
                url = None
            if url and not insecure_for(self.request, url):
                return url
        return signed_url(self.request, self.storage, storage_path, ignore_hidden=ignore_hidden)


def listing_url(request, storage, storage_path: str, *, ignore_hidden: bool = False) -> str:
    return ListingUrls(request, storage).url(storage_path, ignore_hidden=ignore_hidden)


# --- the pages' Content-Security-Policy -------------------------------------------------------------

_sources: dict[tuple, tuple[str, ...]] = {}


def _origin_of(url: str) -> str | None:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname
    if not host.isascii():
        try:  # UTS #46, as browsers: Python's own codec is IDNA 2003, which made faß.de fass.de (API-13)
            host = idna.encode(host, uts46=True).decode("ascii")
        except UnicodeError:  # IDNA 2008 refuses it (written as xn--, it is named)
            return None
    if ":" in host:  # an IPv6 literal can't be a CSP host-source
        return f"{parts.scheme}:"
    return f"{parts.scheme}://{host}{f':{parts.port}' if parts.port else ''}".lower()


def media_sources(storage) -> tuple[str, ...]:
    """The CSP sources (img-src, media-src) a page needs for this storage's download URLs: () when they
    are this site's (/pd/) and stay so; the download domain's origin; the backend's origin when it can be
    told from a sample link made locally (buckets); else "http:" and "https:": the hosts can't be told
    (OneDrive and 115 serve from hosts of their own; a web server's listing, a URL tree and 123's direct links
    from whatever their addresses are; ImgBed's External channel from wherever its links go), and an http:// link
    loads on an http site (an https one gets the site's own link instead, insecure_for()). The backend's also when
    the site's links may send a picture there (falls_back())."""
    key = (storage.pk, storage.config_revision, storage.download_domain, storage.proxy_download, storage.type,
           storage.enabled, storage.proxy_when_full)
    if key in _sources:
        return _sources[key]
    found: tuple[str, ...] = ()
    domain = _domain_url(storage, "/x")
    if domain is not None:
        source = _origin_of(domain)
        found = (source,) if source else ()
    else:
        try:
            driver = _driver(storage)
            direct = serves_directly(storage, driver) or falls_back(storage, driver)
        except Exception:  # noqa: BLE001 - a storage that can't start: its links go nowhere anyway
            direct = False
        if direct and storage.type in LOCAL_URLS:
            try:
                sample = driver.download_url("/hfile-csp-probe", filename="probe", expires=60)
            except Exception:  # noqa: BLE001 - fall back to any https origin
                sample = None
            source = _origin_of(sample) if sample else None
            found = (source,) if source else ("https:",)
        elif direct:  # hosts that can't be told, http:// ones among them
            found = ("http:", "https:")
    if len(_sources) > 1000:
        _sources.clear()
    _sources[key] = found
    return found
