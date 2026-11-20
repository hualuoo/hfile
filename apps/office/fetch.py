"""Downloading what the Document Server saved (the callback's `url`), with an SSRF guard.

The URL comes in a callback, so it is only fetched from the OnlyOffice server itself: http or https,
the same scheme, host and port as 显示设置's OnlyOffice 服务地址 (default ports filled in), no user name
or password in it, and only ASCII (the setting is saved with its host as punycode; IDNA 2003 and 2008
read some names differently). The URL is read by urllib and by httpx, which makes the request, and both
must find the same origin. Redirects are followed by hand, at most MAX_REDIRECTS, each hop checked the
same way. 5 seconds to connect and 30 for each read, DEADLINE for the whole download, and MAX_BYTES at
most (a Content-Length above it is refused before reading). httpx's timeouts are per operation: a server
that sends a byte every few seconds would hold the request (and a thread of the site) for as long as it
likes, so a timer shuts the connection's sockets down when the deadline is up, whatever the download
is doing then (as apps/sso/outbound.py does; httpcore's trace extension hands over each socket once it is
connected). Only resolving the host name isn't covered. The bytes go to `dest`, a file the caller
then streams into the storage: a download cut short never reaches the storage.

Errors are FetchError, whose `reason` is for the log (English, without the URL's query: the Document
Server's links to its saved files are credentials of their own).
"""

from __future__ import annotations

import socket
import threading
import time
import urllib.parse
from typing import BinaryIO

import httpx

from apps.storages.drivers import http

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 30.0
MAX_REDIRECTS = 5
DEADLINE = 300.0  # seconds for the whole download
MAX_BYTES = 200 * 1024 * 1024  # an edited document; the Document Server's own limit is 100 MB by default
PIECE = 64 * 1024
_REDIRECTS = frozenset({301, 302, 303, 307, 308})
_PORTS = {"http": 80, "https": 443}


class FetchError(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def origin(url: str) -> tuple[str, str, int] | None:
    """(scheme, host, port) of an ASCII http(s) URL without credentials, the host in lower case, as both
    urllib and httpx read it; None for anything else."""
    if (not isinstance(url, str) or not url.isascii()
            or any(ord(char) < 0x21 or ord(char) == 0x7F for char in url)):
        return None
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
        parsed = httpx.URL(url)
    except (ValueError, TypeError, httpx.InvalidURL):
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _PORTS or not host or parts.username is not None or parts.password is not None:
        return None
    found = (scheme, host, port or _PORTS[scheme])
    try:
        theirs = (parsed.scheme, parsed.raw_host.decode("ascii").lower(), parsed.port or _PORTS.get(parsed.scheme))
    except UnicodeDecodeError:
        return None
    if parsed.userinfo or theirs != found:
        return None
    return found


def host_of(url: str) -> str:
    """The host of a URL, for the log; "" when there is none."""
    try:
        return urllib.parse.urlsplit(url).hostname or ""
    except (ValueError, TypeError, AttributeError):
        return ""


def check(url: str, server: tuple[str, str, int]) -> None:
    found = origin(url)
    if found is None:
        raise FetchError("not an http(s) URL without credentials")
    if found != server:
        raise FetchError(f"another origin than the OnlyOffice server's ({found[0]}://{found[1]}:{found[2]})")


def client() -> httpx.Client:
    return httpx.Client(
        timeout=httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT),
        verify=http.ssl_context(),
        follow_redirects=False,
        # No proxy or .netrc from the environment: the request goes where the checked URL says.
        trust_env=False,
        headers={"User-Agent": http.USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"},
    )


def download(url: str, server_url: str, dest: BinaryIO, *, max_bytes: int | None = None,
             deadline: float | None = None) -> int:
    """Writes the document at `url` into `dest`; returns its size. `server_url` is the OnlyOffice
    服务地址 the URL must share an origin with. Raises FetchError."""
    max_bytes = MAX_BYTES if max_bytes is None else max_bytes
    server = origin(server_url)
    if server is None:
        raise FetchError("no usable OnlyOffice address in the settings")
    allowed = DEADLINE if deadline is None else deadline
    ends = time.monotonic() + allowed
    sockets = []

    def trace(event, info):
        # Each socket the download opens (and its TLS layer's): the watchdog's to shut down.
        if event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            stream = info.get("return_value")
            sock = stream.get_extra_info("socket") if stream is not None else None
            if sock is not None:
                sockets.append(sock)

    def cut_off():
        for sock in list(sockets):
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)  # the TCP connection, under any TLS
            except OSError:
                pass

    watchdog = threading.Timer(max(allowed, 0.0), cut_off)
    watchdog.daemon = True
    watchdog.start()
    current = url
    try:
        with client() as connection:
            for _hop in range(MAX_REDIRECTS + 1):
                check(current, server)
                with connection.stream("GET", current, extensions={"trace": trace}) as response:
                    if response.status_code in _REDIRECTS:
                        location = response.headers.get("Location")
                        if not location:
                            raise FetchError(f"a redirect ({response.status_code}) without Location")
                        current = urllib.parse.urljoin(current, location)
                        continue
                    if response.status_code != 200:
                        raise FetchError(f"HTTP {response.status_code}")
                    size = _body(response, dest, max_bytes, ends)
                    if time.monotonic() >= ends:  # cut off: an answer without a length looks complete
                        raise FetchError("took too long")
                    return size
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, OSError) as error:
        if time.monotonic() >= ends:
            raise FetchError("took too long") from None
        raise FetchError(type(error).__name__) from None
    finally:
        watchdog.cancel()
    raise FetchError(f"more than {MAX_REDIRECTS} redirects")


def _body(response: httpx.Response, dest: BinaryIO, max_bytes: int, ends: float) -> int:
    raw = response.headers.get("Content-Length")
    length = None
    if raw is not None:
        raw = raw.strip()
        if not raw.isascii() or not raw.isdigit() or len(raw) > 18:
            raise FetchError("a bad Content-Length")
        length = int(raw)
        if length > max_bytes:
            raise FetchError(f"too large ({length} bytes, at most {max_bytes})")
    encoded = response.headers.get("Content-Encoding", "identity").strip().lower() not in ("", "identity")
    total = 0
    for piece in response.iter_bytes(PIECE):
        total += len(piece)
        if total > max_bytes:
            raise FetchError(f"too large (more than {max_bytes} bytes)")
        if time.monotonic() > ends:
            raise FetchError("took too long")
        dest.write(piece)
    if length is not None and not encoded and total != length:
        raise FetchError(f"cut short ({total} of {length} bytes)")
    return total
