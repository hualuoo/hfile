"""GET /pd/<storage key>/<path>: a file through the site, by a signed link (inventory 8.3, D10, D15).

No login: the signature (apps.files.signing) is the grant, made when a listing or the item endpoint handed
the link out. When it is opened:

- the signature must hold for this storage and path, and not be expired;
- the storage must be enabled, and the filter rules (不可访问, 禁止下载) must still allow the file
  (apps.storages.access.check_served()): a rule added after the link was made, or a link that leaked, stops
  working at once. Only a link signed for someone with 忽略隐藏 skips them, as the policy did for him;
- a link that says so (r=1, OneDrive and 115) redirects to the backend's own link, made now with the
  browser's User-Agent and kept a few minutes (what isn't kept counts against a budget per address, so
  that looping a link can't get the site's account throttled), unless that is an http:// link and the
  site is https (transfer.insecure_for(): mixed content); the others are sent through the site
  from the driver's ReadStream, which is closed when the answer is done or the client goes away;
- a GET takes a place among its storage's and its IP's downloads first (apps/files/limits.py): a storage
  that has none left sends it to the storage's own link (when it is set to and has one, and that isn't an
  http:// link for an https site) or answers 429, an IP that has none gets 429. The answer goes at its IP's
  speed. `hfile serve` answers /pd/ on threads of their own (apps/files/pool.py).

The answer:
- Range: one range answers 206; an unsatisfiable or unparsable one 416; several ranges the whole file
  (200). Backends that can't seek (FTP without REST) always send the whole file;
- Content-Disposition attachment or inline (the link's, else the storage's 下载链接强制下载), with the
  file name in RFC 6266 form;
- Content-Type guessed from the name, X-Content-Type-Options: nosniff. Scripts and style sheets are
  sent as text/plain: with nosniff a browser then never runs or applies them, even if a page of the
  site were made to include one (the site's CSP allows its own origin);
- a sandbox: "Content-Security-Policy: sandbox" with nothing allowed, for everything but PDFs, so a
  stored HTML or SVG file opened at its link renders without scripts, in an origin of its own. PDF
  viewers don't run with sandbox; a PDF's scripts don't run in the site's origin anyway.

The 下载 button of the site's pages (the file list, the viewers) opens the link with dl=1 added (not
signed: signing ignores parameters it doesn't know). A GET with it that starts the download (no Range, or
one from the first byte) sends download_clicked once the file is served or the redirect made: the links
app writes the download log from it (页面下载). A resumed download, a download manager's later parts and
HEAD don't. Answers to links with dl=1 say no-store, so that the next click comes here again rather than
to the browser's cache. The gallery, the players, the viewers' reads and copied links don't say dl=1.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time

from django.core.cache import cache
from django.dispatch import Signal
from django.http import HttpResponse, HttpResponseRedirect, StreamingHttpResponse
from django.utils.http import content_disposition_header, http_date, parse_http_date_safe
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.core import net
from apps.core.throttles import address_key
from apps.storages import access as policy
from apps.storages import instances
from apps.storages.access import AccessDenied
from apps.storages.drivers import filetypes
from apps.storages.drivers.base import DriverError, NotFound
from apps.storages.models import StorageSource
from apps.web.errors import error_page

from . import limits, signing, transfer

logger = logging.getLogger("hfile.files")

SANDBOX = "sandbox; default-src 'none'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; frame-ancestors 'none'"
PDF_POLICY = "frame-ancestors 'self'"
# Types a browser runs or applies when a page includes them (every "script" type: javascript,
# ecmascript, jscript, livescript, ...; style sheets; WebAssembly): sent as text, never as what they are.
_ACTIVE = re.compile(r"(script|^text/css$|^application/wasm$|^text/x-component$)", re.IGNORECASE)
_RANGE = re.compile(r"bytes=(\d*)-(\d*)", re.IGNORECASE)

# A download from the site's 下载 button started (see the module's docstring): sent with request=,
# storage= and path= (the storage path). The links app writes the download log from it.
download_clicked = Signal()


class RangeNotSatisfiable(Exception):
    pass


def content_type(name: str) -> str:
    """The type to send a file with: guessed from its name; active types become text/plain. Text is
    said to be UTF-8 (most is; the viewers decode the bytes themselves, with an encoding to choose)."""
    guessed, encoding = filetypes.guess_type(name, strict=False)
    if encoding or not guessed:  # a .tar.gz is a gzip file to the browser, not a tar
        guessed = {"gzip": "application/gzip", "bzip2": "application/x-bzip2", "xz": "application/x-xz",
                   "br": "application/octet-stream", "compress": "application/x-compress"}.get(encoding or "",
                                                                                        "application/octet-stream")
    if _ACTIVE.search(guessed):
        guessed = "text/plain"
    if guessed.startswith("text/"):
        return f"{guessed}; charset=utf-8"
    return guessed


def parse_range(header: str | None, size: int | None) -> tuple[int, int] | None:
    """The (first, last) byte of a Range header for a file of `size` bytes, or None for the whole
    file (no header, several ranges, an unknown size). RangeNotSatisfiable for one that can't be
    served or read."""
    if not header or size is None:
        return None
    header = header.strip()
    if "," in header:
        if not re.fullmatch(r"bytes=\s*(\d*-\d*\s*,\s*)+\d*-\d*", header, re.IGNORECASE):
            raise RangeNotSatisfiable()
        return None  # several ranges: the whole file
    match = _RANGE.fullmatch(header)
    if not match:
        raise RangeNotSatisfiable()
    first, last = match.groups()
    if not first and not last:
        raise RangeNotSatisfiable()
    if len(first) > 20 or len(last) > 20:
        raise RangeNotSatisfiable()
    if not first:  # the last N bytes
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise RangeNotSatisfiable()
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if start >= size or end < start:
        raise RangeNotSatisfiable()
    return start, min(end, size - 1)


def _etag(size: int | None, modified) -> str | None:
    if size is None or modified is None:
        return None
    return f'"{size:x}-{int(modified.timestamp()):x}"'


def _if_range_matches(header: str | None, etag: str | None, modified) -> bool:
    """Whether the Range may be honoured (If-Range): a strong ETag equal to ours, or our modification
    time (to the second). Without If-Range, yes."""
    if not header:
        return True
    header = header.strip()
    if header.startswith('"') or header.startswith("W/"):
        return etag is not None and header == etag
    when = parse_http_date_safe(header)
    return when is not None and modified is not None and int(modified.timestamp()) == when


def _failed(request, error: Exception, storage: StorageSource):
    if isinstance(error, signing.BadLink):
        return error_page(request, 403, error.message, code=error.code)
    if isinstance(error, AccessDenied):
        return error_page(request, error.status_code, error.message, code=error.code)
    if isinstance(error, instances.StorageUnavailable):
        return error_page(request, 403, error.message, code=policy.STORAGE_DISABLED)
    if isinstance(error, NotFound):
        return error_page(request, 404, _("The file doesn't exist; it may have been deleted or renamed."), code="not_found")
    reason = getattr(error, "message", None) or str(error)
    logger.warning("Download failed (storage %s): %s", storage.key, reason)
    if _staff(request):  # what the storage said, as the file API tells staff (files.service.driver_problem)
        return error_page(request, 500, _("Reading the file from storage failed: %(reason)s") % {"reason": reason})
    return error_page(request, 500, _("Reading the file from storage failed. Please try again later."))


def _staff(request) -> bool:
    user = getattr(request, "user", None)
    return bool(user is not None and user.is_authenticated and user.is_staff)


def _from_start(header: str | None) -> bool:
    """Whether a Range header (or none) asks for the file from its first byte."""
    if not header:
        return True
    match = _RANGE.fullmatch(header.strip())
    return bool(match and match.group(1) and not match.group(1).strip("0"))


def _clicked(request, response, storage: StorageSource, link: signing.Link):
    """The answer to a link with dl=1, a download from the 下载 button: not stored by the browser, and
    download_clicked when this GET starts the download. A failure of what listens doesn't stop the
    download (send_robust logs it)."""
    if request.GET.get("dl") != "1":
        return response
    response["Cache-Control"] = "private, no-store"
    if request.method == "GET" and _from_start(request.META.get("HTTP_RANGE")):
        download_clicked.send_robust(sender=StorageSource, request=request, storage=storage, path=link.path)
    return response


def _headers(response, link: signing.Link, *, ctype: str, inline: bool, etag: str | None, modified,
             can_range: bool) -> None:
    response["Content-Type"] = ctype
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Disposition"] = content_disposition_header(not inline, link.name)
    response["Accept-Ranges"] = "bytes" if can_range else "none"
    response["Content-Security-Policy"] = PDF_POLICY if inline and ctype == "application/pdf" else SANDBOX
    if etag:
        response["ETag"] = etag
    if modified is not None:
        response["Last-Modified"] = http_date(modified.timestamp())
    left = None if link.expires is None else max(0, int(link.expires - time.time()))
    response["Cache-Control"] = f"private, max-age={min(left, 3600) if left is not None else 3600}"
    response["Referrer-Policy"] = "no-referrer"


def _content_length(size) -> str:
    return str(int(size))


# --- r=1 links: the backend's own link, asked for when the link is opened -------------------------

REDIRECT_CACHE_SECONDS = 300  # OneDrive's links last about an hour, 115's a few
REDIRECT_MISSES = 600  # backend requests per address and minute, for links not cached
REDIRECT_WINDOW = 60


class Throttled(Exception):
    def __init__(self, wait: int):
        super().__init__(wait)
        self.wait = wait


def _backend_url(request, storage, driver, link) -> str | None:
    """The backend's link of the file (a request to OneDrive or 115), kept a few minutes per file (and
    per User-Agent where the backend binds its links to it): opening the same link again and again
    doesn't ask the backend again; not a link that may work once only (reusable_links: Cloudreve v3).
    Kept for half the link lifetime at most (one asked for 60 s, kept 300 s, went out long dead), and no
    longer than the driver says (timed_download_url(): Cloudreve's "File Blob URL TTL").
    What isn't cached counts against the address's budget."""
    agent = transfer.user_agent(request)
    bound = agent if storage.type in transfer.UA_BOUND else ""
    key = "hfile:files:backend-url:" + hashlib.sha256(
        repr((storage.pk, storage.config_revision, link.path, link.name, bound)).encode("utf-8", "surrogatepass")).hexdigest()
    reusable = getattr(type(driver), "reusable_links", True)
    url = cache.get(key) if reusable else None
    if url:
        return url
    _count_miss(request)
    url, lasts = driver.timed_download_url(link.path, filename=link.name, expires=storage.link_lifetime,
                                           user_agent=agent)
    kept = min(REDIRECT_CACHE_SECONDS, storage.link_lifetime / 2, REDIRECT_CACHE_SECONDS if lasts is None else lasts)
    if url and reusable and kept >= 1:
        cache.set(key, url, int(kept))
    return url


def _count_miss(request) -> None:
    ip = net.client_ip(request)
    key = f"hfile:files:backend-urls:{address_key(ip) if ip else 'unknown'}"
    now = time.time()
    history = [moment for moment in cache.get(key, []) if moment > now - REDIRECT_WINDOW]
    if len(history) >= REDIRECT_MISSES:
        raise Throttled(max(1, int(history[0] + REDIRECT_WINDOW - now) + 1))
    history.append(now)
    cache.set(key, history, REDIRECT_WINDOW)


@require_safe
def serve(request, key: str, path: str):
    """GET and HEAD /pd/<key>/<path>?<signed query>."""
    storage = StorageSource.by_key(key)
    if storage is None:
        return error_page(request, 404, _("The storage source doesn't exist."), code=policy.STORAGE_NOT_FOUND)
    try:
        link = signing.verify(storage, path, request.GET)
        policy.check_served(storage, link.path, operation=policy.DOWNLOAD, ignore_hidden=link.ignore_hidden)
        driver = instances.get(storage)
    except (signing.BadLink, AccessDenied, instances.StorageUnavailable) as error:
        return _failed(request, error, storage)
    if link.redirect and transfer.serves_directly(storage, driver):
        try:
            url = _backend_url(request, storage, driver, link)
        except Throttled as error:
            return error_page(request, 429, retry_after=error.wait)
        except DriverError as error:
            return _failed(request, error, storage)
        if url and not transfer.insecure_for(request, url):
            return _redirect(request, url, storage, link)
    ticket = None
    if request.method == "GET":  # an answer with a body: it counts (apps/files/limits.py)
        try:
            ticket = limits.take(request, storage)
        except limits.Full as full:
            return _full(request, storage, driver, link, full)
    try:
        response = _answer(request, storage, driver, link, ticket)
    except BaseException:
        if ticket is not None:
            ticket.release()
        raise
    if ticket is not None and not response.streaming:
        ticket.release()
    return response


def _redirect(request, url: str, storage: StorageSource, link: signing.Link):
    response = HttpResponseRedirect(url)
    response["Cache-Control"] = "private, no-store"
    response["Referrer-Policy"] = "no-referrer"
    return _clicked(request, response, storage, link)


def _full(request, storage: StorageSource, driver, link: signing.Link, full: limits.Full):
    """The answer to a download its storage or its address has no place for: the storage's own link when
    the storage says so (and has one), else 429."""
    if full.storage and transfer.falls_back(storage, driver):
        try:
            if storage.type in transfer.DEFERRED:  # a request to OneDrive or 115: kept, and counted
                url = _backend_url(request, storage, driver, link)
            else:
                left = storage.link_lifetime if link.expires is None else max(1, int(link.expires - time.time()))
                url = driver.download_url(link.path, filename=link.name, expires=left,
                                          user_agent=transfer.user_agent(request))
        except (Throttled, DriverError):
            url = None
        if url and not transfer.insecure_for(request, url):
            return _redirect(request, url, storage, link)
    if full.storage:
        message = _("This storage source is sending as many downloads as it may. Please try again in a moment.")
    else:
        message = _("You have as many downloads going as you may. Please try again when one of them has finished.")
    return error_page(request, 429, message, retry_after=limits.RETRY_AFTER)


def _body(stream, ticket: limits.Ticket | None):
    """What a streamed answer sends: the stream, at its IP's speed and holding its place (limits.Metered)."""
    return stream if ticket is None else limits.Metered(stream, ticket)


def _answer(request, storage: StorageSource, driver, link: signing.Link, ticket: limits.Ticket | None):
    """The file, a part of it (Range), its headers (HEAD) or 416."""
    inline = link.inline if link.inline is not None else not storage.force_download
    ctype = content_type(link.name)
    can_range = bool(getattr(driver, "can_range", False))
    wanted = request.META.get("HTTP_RANGE") if can_range else None
    try:
        if request.method == "HEAD" or wanted:
            entry = driver.stat(link.path)
            if entry.is_dir:
                raise NotFound()
            size, modified = entry.size, entry.modified
            etag = _etag(size, modified)
            if request.method == "HEAD":
                response = HttpResponse(status=200)
                _headers(response, link, ctype=ctype, inline=inline, etag=etag, modified=modified, can_range=can_range)
                if size is not None:
                    response["Content-Length"] = _content_length(size)
                return response
            if not _if_range_matches(request.META.get("HTTP_IF_RANGE"), etag, modified):
                wanted = None
            try:
                span = parse_range(wanted, size)
            except RangeNotSatisfiable:
                response = HttpResponse(status=416)
                response["Content-Range"] = f"bytes */{size}"
                response["X-Content-Type-Options"] = "nosniff"
                response["Content-Security-Policy"] = SANDBOX
                return response
            if span is not None:
                start, end = span
                stream = driver.open_known(link.path, entry, start, end - start + 1)
                response = StreamingHttpResponse(_body(stream, ticket), status=206)
                _headers(response, link, ctype=ctype, inline=inline, etag=etag, modified=modified, can_range=True)
                response["Content-Range"] = f"bytes {start}-{end}/{size}"
                response["Content-Length"] = _content_length(end - start + 1)
                return _clicked(request, response, storage, link)
        stream = driver.open(link.path)
    except DriverError as error:
        return _failed(request, error, storage)
    response = StreamingHttpResponse(_body(stream, ticket), status=200)
    modified = stream.modified
    _headers(response, link, ctype=ctype, inline=inline, etag=_etag(stream.total, modified), modified=modified,
             can_range=can_range and stream.total is not None)
    if stream.size is not None:
        response["Content-Length"] = _content_length(stream.size)
    return _clicked(request, response, storage, link)

