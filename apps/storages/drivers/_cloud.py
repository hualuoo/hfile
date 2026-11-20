"""Pieces shared by the cloud drive drivers (microsoft, google, open115); not a driver itself.

- TokenCache: an OAuth access token shared by the threads of one driver, refreshed under a lock.
- TTLCache: a small thread-safe LRU cache with expiry (folder ids, download links).
- RateLimiter: paces the calls of one driver (115 bans clients that call too often).
- KeyedLocks: one lock per (folder, name), so that two requests can't both create a name on
  backends that allow duplicates (Google Drive, 115).
- Streaming: exact-size upload bodies, and ReadStream from a streamed httpx response that also
  copes with a server that ignored the Range header.
- Small helpers: names usable as path segments, timestamps, short backend messages, origins.
"""

from __future__ import annotations

import contextlib
import datetime
import email.utils
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator
from typing import Any, BinaryIO

import httpx

from django.utils.translation import gettext as _, gettext_lazy

from . import http, paths
from .base import AuthError, BackendError, DriverError, ReadStream

#: Bytes per chunk when streaming a download to the site.
CHUNK = 256 * 1024

_INTEGER = re.compile(r"[+-]?[0-9]+")  # ASCII digits only: str.isdigit() also takes "²"

# httpx logs the full URL of every request at INFO, and some of our URLs are credentials
# (OneDrive's pre-authenticated download and upload-session links, 115's CDN links). Keep that
# logger at WARNING unless the site configured it itself.
_log = logging.getLogger("hfile.storage")

if logging.getLogger("httpx").level == logging.NOTSET:
    logging.getLogger("httpx").setLevel(logging.WARNING)


# --- names, times, messages -----------------------------------------------------------------------

def usable_name(name: Any) -> bool:
    """A name from the backend that can be one segment of a storage path.

    Backends that allow "/" (Google Drive) or odd names would otherwise give paths the site can't
    use; such entries are left out of listings. So are names of dots and spaces only ("...", ".. "),
    which paths refuse (D46).
    """
    if not isinstance(name, str) or paths.dots_only(name) or "/" in name or "\x00" in name:
        return False
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:  # lone surrogates from JSON escapes
        return False
    return True


def parse_time(value: Any) -> datetime.datetime | None:
    """An aware datetime from ISO 8601 text or Unix seconds; None when missing or unreadable."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and _INTEGER.fullmatch(value.strip())):
            seconds = int(value)
            if seconds <= 0:
                return None
            return datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
        if not isinstance(value, str):
            return None
        moment = datetime.datetime.fromisoformat(value.strip())
    except (ValueError, OverflowError, OSError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment


def short(text: Any, limit: int = 200) -> str:
    """A backend's message made fit for ours: one line, at most `limit` characters."""
    line = " ".join(str(text if text is not None else "").split())
    return line if len(line) <= limit else line[: limit - 1] + "…"


def as_int(value: Any) -> int | None:
    """An int from a JSON number or digit string; None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INTEGER.fullmatch(value.strip()):
        return int(value.strip())
    return None


def origin(url: str | httpx.URL) -> tuple[str, str, int]:
    """(scheme, host, port) of a URL, to check where credentials may go; ("", "", 0) when it
    isn't an absolute http(s) URL."""
    try:
        parsed = httpx.URL(url) if not isinstance(url, httpx.URL) else url
    except (httpx.InvalidURL, TypeError, ValueError):
        return "", "", 0
    if parsed.scheme not in ("http", "https") or not parsed.host:
        return "", "", 0
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return parsed.scheme, parsed.host, port


def json_or_none(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def redirected(response: httpx.Response, service: str) -> BackendError:
    """The error for a redirect (3xx) where the API makes none: a proxy in the way answered it. It was taken for
    success, and its body for the answer (STOR-15). Closes the response."""
    http.drain(response)
    return BackendError(_("%(service)s answered with an unexpected redirect (HTTP "
                          "%(status_code)s); a proxy between the site and it may be at fault.") % {"service": service, "status_code": response.status_code})


def transport_error(error: Exception, service: str) -> BackendError:
    """BackendError for an httpx failure; the text names the kind only (URLs may carry tokens)."""
    if isinstance(error, httpx.TimeoutException):
        return BackendError(_("Connecting to %(service)s timed out. Please try again later.") % {"service": service})
    return BackendError(_("Connecting to %(service)s failed (%(error)s). Please try again later.") % {"service": service, "error": type(error).__name__})


def retry_after(response: httpx.Response, *, default: float = 1.0, longest: float = 10.0) -> float | None:
    """Seconds to wait before repeating a request the backend throttled (429/503), or None when
    it asks for longer than `longest` (the caller then gives up instead of holding the request)."""
    value = response.headers.get("retry-after")
    seconds = default
    if value:
        value = value.strip()
        if _INTEGER.fullmatch(value):
            seconds = float(value)
        else:
            try:
                moment = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError):
                moment = None
            if moment is not None:
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=datetime.timezone.utc)
                seconds = (moment - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
    seconds = max(seconds, 0.0)
    return seconds if seconds <= longest else None


# --- tokens, caches, pacing -----------------------------------------------------------------------

class TokenCache:
    """The access token of one driver instance, shared by its threads.

    `refresh()` asks the provider for a new one and returns (token, lifetime in seconds); it saves
    a rotated refresh token itself (driver.update_config). Refreshes are serialised: threads that
    need a token while another one refreshes wait for its result. A refusal (AuthError) is
    remembered for `failure_ttl` seconds, so a dead authorisation doesn't send every request of
    the site to the token endpoint; new settings make a new driver, and so a new cache.
    """

    failure_ttl = 30.0

    def __init__(self, refresh: Callable[[], tuple[str, float]]):
        self._refresh = refresh
        self._lock = threading.Lock()
        self._token: str | None = None
        self._renew_at = 0.0
        self._refused: tuple[float, str] | None = None  # (until, message)

    def get(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() >= self._renew_at:
                if self._refused is not None and time.monotonic() < self._refused[0]:
                    raise AuthError(self._refused[1])
                try:
                    token, lifetime = self._refresh()
                except AuthError as error:
                    self._refused = (time.monotonic() + self.failure_ttl, error.message)
                    raise
                self._refused = None
                lifetime = max(float(lifetime), 0.0)
                self._token = token
                # Renew a little early, so a request never starts with a token about to expire.
                self._renew_at = time.monotonic() + lifetime - min(300.0, lifetime / 10)
            return self._token

    def invalidate(self, token: str | None) -> None:
        """The backend refused `token`: the next get() refreshes, unless another thread already did."""
        with self._lock:
            if token is not None and self._token == token:
                self._token = None


class TTLCache:
    """A thread-safe LRU mapping whose entries expire `ttl` seconds after they were put."""

    def __init__(self, maxsize: int, ttl: float | None = None):
        self._maxsize = maxsize
        self._ttl = ttl
        self._data: OrderedDict[Any, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Any, default: Any = None) -> Any:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return default
            stored, value = item
            if self._ttl is not None and time.monotonic() - stored > self._ttl:
                del self._data[key]
                return default
            self._data.move_to_end(key)
            return value

    def put(self, key: Any, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic(), value)
            self._data.move_to_end(key)
            while len(self._data) > self._maxsize:
                self._data.popitem(last=False)

    def pop(self, key: Any) -> None:
        with self._lock:
            self._data.pop(key, None)

    def discard_if(self, predicate: Callable[[Any], bool]) -> None:
        """Drop the entries whose key matches, e.g. everything below a renamed folder."""
        with self._lock:
            for key in [key for key in self._data if predicate(key)]:
                del self._data[key]

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


class KeyedLocks:
    """One lock per key, kept while someone holds or waits for it."""

    def __init__(self):
        self._guard = threading.Lock()
        self._locks: dict[Any, list] = {}

    @contextlib.contextmanager
    def hold(self, key: Any) -> Iterator[None]:
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = self._locks[key] = [threading.Lock(), 0]
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    del self._locks[key]


class RateLimiter:
    """At most `per_second` calls a second over all threads; callers wait their turn.

    A caller whose turn is more than `longest` seconds away gets BackendError at once instead of
    holding one of the site's threads that long (a burst of requests would stall the whole site).
    """

    def __init__(self, per_second: float, *, longest: float | None = None, service: str = gettext_lazy("Storage")):
        self._interval = 1.0 / per_second if per_second and per_second > 0 else 0.0
        self._longest = longest
        self._service = service
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self._interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            if self._longest is not None and start - now > self._longest:
                raise BackendError(_("Too many requests to %(service)s; they're queued "
                                     "by its rate limit. Please try again later.") % {"service": self._service})
            self._next = start + self._interval
        if start > now:
            time.sleep(start - now)


# --- streaming --------------------------------------------------------------------------------------

def read_exact(data: BinaryIO, size: int) -> bytes:
    """`size` bytes from `data`; fewer only when the data ends."""
    parts: list[bytes] = []
    left = size
    while left > 0:
        piece = data.read(left)
        if not piece:
            break
        parts.append(piece)
        left -= len(piece)
    return b"".join(parts)


def position(data: BinaryIO) -> int | None:
    """Where `data` is now, when it can be read again from there (seek); None if it can't."""
    try:
        return data.tell() if data.seekable() else None
    except (AttributeError, OSError, ValueError):
        return None


def exact_body(data: BinaryIO, size: int, chunk: int = 1024 * 1024) -> Iterator[bytes]:
    """Exactly `size` bytes of `data`, as chunks for a request body.

    Raises DriverError when the data ends early: a short upload must fail, not store a cut file.
    """
    left = size
    while left > 0:
        piece = data.read(min(chunk, left))
        if not piece:
            raise DriverError(_("The uploaded data is incomplete: less than the size given."))
        if len(piece) > left:
            piece = piece[:left]
        left -= len(piece)
        yield piece


def _body_chunks(response: httpx.Response, skip: int, limit: int | None, expected: int | None,
                 service: str) -> Iterator[bytes]:
    delivered = 0
    try:
        for chunk in response.iter_raw(CHUNK):
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk = chunk[skip:]
                skip = 0
            if limit is not None:
                if limit <= 0:
                    break
                if len(chunk) > limit:
                    chunk = chunk[:limit]
                limit -= len(chunk)
            elif expected is not None and delivered + len(chunk) > expected:
                # Longer than the size promised to the browser (its Content-Length): passed on, the end would be cut
                # off without a word and the file look complete. Ended here, the download fails as it should.
                raise BackendError(_("The file read from %(service)s isn't the size it should be. Please try again later.") % {"service": service})
            delivered += len(chunk)
            yield chunk
    except httpx.HTTPError as error:
        raise BackendError(_("The connection dropped while reading the file from %(service)s. Please try again later.") % {"service": service}) from error
    finally:
        response.close()
    if expected is not None and delivered < expected:  # the body ended early without an error
        raise BackendError(_("The file read from %(service)s is incomplete. Please try again later.") % {"service": service})


def response_stream(response: httpx.Response, *, offset: int, length: int | None, total: int | None,
                    service: str, etag: str | None = None,
                    modified: datetime.datetime | None = None) -> ReadStream:
    """A ReadStream of a streamed download answer (send(..., stream=True)) for bytes `offset`
    to `offset + length` (or the end).

    A 206 answer must start at `offset`; a 200 answer to a range request is cut down here.
    Ask for "Accept-Encoding: identity": the raw bytes are passed on as they come.
    """
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if encoding not in ("", "identity"):
        response.close()
        raise BackendError(_("%(service)s returned a compressed file (%(encoding)s), which can't be passed on as it is.") % {"service": service, "encoding": short(encoding, 20)})
    skip = 0
    if response.status_code == 206:
        content_range = response.headers.get("content-range", "")
        start = content_range.partition(" ")[2].partition("-")[0]
        if not (start.isascii() and start.isdigit()) or int(start) != offset:  # isdigit() takes "²" (STOR-17)
            response.close()
            raise BackendError(_("%(service)s returned the wrong range of the file.") % {"service": service})
    elif response.status_code == 200:
        skip = offset
    else:
        http.drain(response)
        raise BackendError(_("Downloading from %(service)s failed (HTTP %(status_code)s).") % {"service": service, "status_code": response.status_code})
    served = _served_total(response)
    if served is not None and served != total:
        # The size a listing gives can differ from what the download has (SharePoint changes Office files after they
        # are uploaded, OneDrive gives Live Photos smaller than it says): the download's own length is the one to
        # promise the browser.
        if total is not None:
            _log.warning("%s: a file of %s bytes by its listing has %s in its download", service, total, served)
        if offset > served:
            response.close()
            raise BackendError(_("%(service)s returned the wrong range of the file.") % {"service": service})
        total = served
    if length is not None:
        size: int | None = length
    elif total is not None:
        size = total - offset
    else:
        size = None
    return ReadStream(
        _body_chunks(response, skip, length, size, service),
        size=size,
        total=total,
        offset=offset,
        content_type=response.headers.get("content-type"),
        etag=etag,
        modified=modified,
        close=response.close,
    )


def _served_total(response: httpx.Response) -> int | None:
    """The length of the whole file as a download answer gives it: the total of a 206's Content-Range, a 200's
    Content-Length; None when it doesn't say."""
    if response.status_code == 206:
        value = response.headers.get("content-range", "").rpartition("/")[2].strip()
    else:
        value = response.headers.get("content-length", "").strip()
    return int(value) if value.isascii() and value.isdigit() else None


def range_header(offset: int, length: int | None, total: int | None) -> str | None:
    """The Range header for open(offset, length), or None for the whole file."""
    if offset == 0 and (length is None or (total is not None and length >= total)):
        return None
    if length is None:
        return f"bytes={offset}-"
    return f"bytes={offset}-{offset + length - 1}"
