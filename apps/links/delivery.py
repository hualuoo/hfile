"""Opening a path link or a short link: the pipeline both share (inventory 9.5, decision D38).

1. the rate limit (apps.links.ratelimit): 429 with Retry-After;
2. the Referer rules (apps.links.referer): 403;
3. the site's switch for the kind of link (允许使用直链 / 允许使用短链): 403; for a short link then its
   key (404 短链不存在) and expiry (403 短链已失效);
4. the access decision (apps.links.service: the visitor for a path link, the creator for a short link);
5. the download: a .m3u8 playlist is read through the driver and answered as it is (its relative
   segments then resolve next to a path link; a short link's resolve to other /s/ addresses, which don't
   exist); anything else is a 302 to apps.files.transfer.download_url(), valid at most a few minutes
   (service.target_lifetime(): the link's own checks end any access soon after). A conditional request
   for a file that hasn't changed since (If-Modified-Since: wget -N, curl -z) gets 304 instead, nothing
   else (_unchanged());
6. the download log (记录下载日志), once the download URL was made or the playlist read (for HEAD:
   found), for the first opening of the link by its IP and user within service.TARGET_LIFETIME, the
   longest the URL stays valid: a download tool opens the link again for each of its parts and to
   resume, and some send HEAD (whose answer carries the URL too) and then go to the URL itself.

The download log also gets the downloads from the 下载 button of the site's pages that go through the
site (page_download(), connected to apps.files.download.download_clicked; D54), once per file, IP and user
within service.TARGET_LIFETIME as in step 6: a download manager that takes a download over from the browser
asks for it from the first byte again. Every row says who was logged in.

A short link's unknown keys also count against a budget of their own (ratelimit.missed()): too many, and
the address gets 429 for every /s/ address for a while, so that keys can't be guessed quickly.

Every answer, errors included, says Cache-Control: no-cache, no-store, must-revalidate, private: a link
is decided anew each time. Errors are the site's MD3 pages, in place (apps.web.errors.error_page).
"""

from __future__ import annotations

import hashlib
import logging
import urllib.parse

from django.db import DatabaseError
from django.http import HttpResponse, HttpResponseNotAllowed, HttpResponseNotModified, HttpResponseRedirect
from django.utils.http import content_disposition_header, parse_http_date_safe
from django.utils.translation import gettext as _, gettext_lazy

from apps.accounts.validators import USERNAME_MAX
from apps.core import net, reserved
from apps.core.text import from_header
from apps.files import download, signing, transfer
from apps.siteconf.models import DEFAULT_DIRECT_LINK_PREFIX, SiteSettings
from apps.storages import access as policy
from apps.storages import instances
from apps.storages.access import AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError, NotFound
from apps.web.errors import error_page

from . import ratelimit, referer, service
from .models import TEXT_MAX, DownloadLog, ShortLink

logger = logging.getLogger("hfile.links")

NO_CACHE = "no-cache, no-store, must-revalidate, private"
PLAYLIST_MAX = 2 * 1024 * 1024  # bytes of an .m3u8 file read for a link
PLAYLIST_TYPE = "application/vnd.apple.mpegurl"
METHODS = ("GET", "HEAD")

# What a visitor of a short link is told when its creator may no longer make it (D31): his own
# situation is none of the visitor's business.
_CREATOR_GONE = gettext_lazy("This short link no longer works: the user who created it can no longer download the file.")
_CREATOR_PASSWORD = gettext_lazy("This short link no longer works: the file's folder has a password "
                                 "(or a new one) that hadn't been entered when the link was created.")
_FILE_CODES = (policy.STORAGE_NOT_FOUND, policy.STORAGE_DISABLED, policy.INACCESSIBLE, policy.DOWNLOAD_DISABLED)


def _private(response):
    response["Cache-Control"] = NO_CACHE
    return response


def _error(request, status, message="", **kwargs):
    return _private(error_page(request, status, message, **kwargs))


def _gate(request, site):
    """Steps 1 and 2: an answer when the request is refused, else None."""
    if request.method not in METHODS:
        return _private(HttpResponseNotAllowed(METHODS))
    wait = ratelimit.check(request, site)
    if wait:
        return _error(request, 429, retry_after=wait)
    refused = referer.refusal(request, site)
    if refused:
        return _error(request, 403, refused, code="referer_refused")
    return None


# --- the two kinds of link ------------------------------------------------------------------------

def path_link(request, site, rest: str):
    """/<prefix>/<rest>: rest is <storage key>/<storage path>."""
    refused = _gate(request, site)
    if refused is not None:
        return refused
    if not site.show_path_link:
        return _error(request, 403, _("Direct links aren't allowed on this site."), code=service.PATH_LINKS_OFF)
    storage_key, _slash, path = (rest or "").partition("/")
    if not storage_key or not path.strip("/"):
        return _error(request, 404, _("Wrong direct link address: it should be /prefix/storage-alias/file-path."), code="not_found")
    try:
        storage, storage_path = service.open_path_link(request, site, storage_key, path)
    except AccessDenied as error:
        actions = None
        hint = error.extra.get("path") if error.code == policy.PASSWORD_REQUIRED else None
        if isinstance(hint, str):
            # The file itself (the rule covers it): its folder in the browser asks for the password.
            actions = [(_("Open the folder to enter the password"), _folder_url(storage_key, paths.parent(hint)))]
        return _error(request, error.status_code, error.message, code=error.code, actions=actions)
    return deliver(request, site, storage, storage_path, kind=DownloadLog.Kind.PATH)


def _folder_url(storage_key: str, client_path: str) -> str:
    """The browser's address of a folder (apps.files.readme.browse_url() without the storage at hand)."""
    key = urllib.parse.quote(storage_key.lower(), safe="")
    return f"/{key}/" if client_path in ("", paths.ROOT) else f"/{key}{signing.quote_path(client_path)}"


def short_link(request, key: str):
    """/s/<key>."""
    site = SiteSettings.load()
    refused = _gate(request, site)
    if refused is not None:
        return refused
    wait = ratelimit.missed_wait(request)
    if wait:
        return _error(request, 429, retry_after=wait)
    if not site.show_short_link:
        return _error(request, 403, _("Short links aren't allowed on this site."), code=service.SHORT_LINKS_OFF)
    link = None
    if service.is_key(key):
        link = ShortLink.objects.select_related("storage", "created_by").filter(key=key).first()
    if link is None:
        ratelimit.missed(request)
        return _error(request, 404, _("The short link doesn't exist."), code="link_not_found")
    if link.expired():
        return _error(request, 403, _("The short link no longer works."), code="link_expired")
    try:
        service.check_creator(link)
    except AccessDenied as error:
        if error.code in _FILE_CODES:
            return _error(request, 404 if error.code == policy.STORAGE_NOT_FOUND else 403, error.message,
                          code=error.code)
        message = _CREATOR_PASSWORD if error.code in (policy.PASSWORD_REQUIRED, policy.PASSWORD_WRONG) else _CREATOR_GONE
        return _error(request, 403, message, code="creator_denied")
    return deliver(request, site, link.storage, link.path, kind=DownloadLog.Kind.SHORT, short_key=link.key,
                   expires_at=link.expires_at)


# --- the download ---------------------------------------------------------------------------------

class PlaylistTooLarge(Exception):
    pass


def read_playlist(storage, storage_path: str) -> bytes:
    """An .m3u8 file's bytes, read through the driver (never the site's own /pd/ over HTTP), at most
    PLAYLIST_MAX of them."""
    driver = instances.get(storage)
    stream = driver.open(storage_path)
    try:
        if stream.size is not None and stream.size > PLAYLIST_MAX:
            raise PlaylistTooLarge()
        chunks, total = [], 0
        for chunk in stream:
            total += len(chunk)
            if total > PLAYLIST_MAX:
                raise PlaylistTooLarge()
            chunks.append(chunk)
    finally:
        stream.close()
    return b"".join(chunks)


def playlist_size(storage, storage_path: str) -> int | None:
    """What HEAD answers for an .m3u8 file, from the driver's stat (None: it doesn't know), or the errors
    read_playlist() would give."""
    entry = instances.get(storage).stat(storage_path)
    if entry.is_dir:
        raise NotFound()
    if entry.size is not None and entry.size > PLAYLIST_MAX:
        raise PlaylistTooLarge()
    return entry.size


def _unchanged(request, storage, storage_path: str) -> bool:
    """Whether the request asks with If-Modified-Since about a file that hasn't changed since (wget -N,
    curl -z, aria2c --conditional-get): by the driver's modification time, which /pd/ sends as Last-Modified
    (whole seconds). The tool keeps its copy and downloads nothing. If-None-Match comes first (RFC 9110
    13.2.2) and the link has no ETag to compare; when the driver can't tell (no time, an error), the
    request goes on as without the header."""
    since = parse_http_date_safe(request.META.get("HTTP_IF_MODIFIED_SINCE", ""))
    if since is None or request.META.get("HTTP_IF_NONE_MATCH"):
        return False
    try:
        entry = instances.get(storage).stat(storage_path)
    except (instances.StorageUnavailable, DriverError):
        return False
    return not entry.is_dir and entry.modified is not None and int(entry.modified.timestamp()) <= since


def deliver(request, site, storage, storage_path: str, *, kind: str, short_key: str = "", expires_at=None):
    """Steps 5 and 6 for a file the request may download (expires_at: a short link's)."""
    if _unchanged(request, storage, storage_path):
        return _private(HttpResponseNotModified())  # no download, so no row in the log
    name = paths.name(storage_path)
    try:
        if name.lower().endswith(".m3u8"):
            response = HttpResponse(content_type=f"{PLAYLIST_TYPE}; charset=utf-8")
            if request.method == "HEAD":  # the size only: HEAD goes out without a body (config/wsgi.py)
                size = playlist_size(storage, storage_path)
                if size is not None:
                    response["Content-Length"] = str(size)
            else:
                response.content = read_playlist(storage, storage_path)
            response["Content-Disposition"] = content_disposition_header(True, name)
            response["X-Content-Type-Options"] = "nosniff"
            response["Content-Security-Policy"] = download.SANDBOX
        else:
            url = transfer.download_url(request, storage, storage_path, filename=name,
                                        lifetime=service.target_lifetime(expires_at))
            response = HttpResponseRedirect(url)
            # The storage needn't learn which page the link was opened from.
            response["Referrer-Policy"] = "no-referrer"
    except PlaylistTooLarge:  # refused, not a failure of the site's (a 5xx would be logged as one)
        return _error(request, 403, _("This m3u8 playlist is over %(limit)s MB, so it can't be opened through a link.") % {"limit": PLAYLIST_MAX // 1024 // 1024},
                      code="playlist_too_large")
    except instances.StorageUnavailable as error:
        return _error(request, 403, error.message, code=policy.STORAGE_DISABLED)
    except NotFound:
        return _error(request, 404, _("The file doesn't exist; it may have been deleted or renamed."), code="not_found")
    except DriverError as error:
        logger.warning("Storage %s: no download for a link (%s)", storage.key, error.message)
        return _error(request, 502, _("Getting a download address from the storage source failed. Please try again later."), code="storage_error")
    if site.record_download_log and _first_opening(request, storage, storage_path, kind=kind, short_key=short_key):
        record(request, site, storage, storage_path, kind=kind, short_key=short_key)
    return _private(response)


def _header(request, name: str) -> str:
    """A header as text (WSGI gives latin-1; browsers send UTF-8), without NULs, at most TEXT_MAX long."""
    return from_header(request.META.get(name, "")).replace("\x00", "")[:TEXT_MAX]


def _username(request) -> str:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return ""
    return user.get_username()[:USERNAME_MAX]


def _first_opening(request, storage, storage_path: str, *, kind: str, short_key: str) -> bool:
    """Step 6: whether the request's IP and user haven't opened this link within service.TARGET_LIFETIME."""
    opening = (kind, storage.pk, storage_path, short_key, net.client_ip(request), _username(request))
    key = hashlib.sha256(repr(opening).encode("utf-8", "surrogatepass")).hexdigest()
    return not ratelimit.opened.hit(key, service.TARGET_LIFETIME, 1)


def record(request, site, storage, storage_path: str, *, kind: str, short_key: str = "") -> None:
    """A row of the download log, while 记录下载日志 is on, with who was logged in. A failure to write it
    doesn't stop the download."""
    if not site.record_download_log:
        return
    try:
        DownloadLog.objects.create(
            kind=kind, storage=storage, path=storage_path, short_key=short_key, ip=net.client_ip(request),
            username=_username(request), user_agent=_header(request, "HTTP_USER_AGENT"),
            referer=_header(request, "HTTP_REFERER"),
        )
    except DatabaseError:
        logger.warning("Could not write the download log", exc_info=True)


def page_download(sender, *, request, storage, path, **kwargs) -> None:
    """apps.files.download.download_clicked: a download from the 下载 button of the site's pages started. Only the
    first of the file by its IP and user within service.TARGET_LIFETIME, as for links (_first_opening()): a download
    manager that takes the download over from the browser (IDM) asks for it from the first byte again."""
    site = SiteSettings.load()
    if site.record_download_log and _first_opening(request, storage, path, kind=DownloadLog.Kind.PAGE, short_key=""):
        record(request, site, storage, path, kind=DownloadLog.Kind.PAGE)


# --- the files app's catch-all asks this first (apps.files.pages.add_route) ----------------------------

def route(request, first: str, rest: str | None):
    """/<first>/<rest> when `first` is the path-link prefix (compared without case, as apps.core.reserved
    compares names), else None: the browser's then. The site's own first segments (/static/, /api/, …)
    can't be the prefix, except its default: those are left alone without asking the database."""
    lowered = first.lower()
    if lowered in reserved.static_names() and lowered != DEFAULT_DIRECT_LINK_PREFIX:
        return None
    site = SiteSettings.load()
    prefix = site.direct_link_prefix
    if not prefix or first.lower() != prefix.lower():
        return None
    return path_link(request, site, rest or "")
