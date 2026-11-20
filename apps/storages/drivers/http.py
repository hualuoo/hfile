"""The HTTP client drivers use for cloud storage APIs.

TLS certificates: ssl.create_default_context() reads the CA certificates OpenSSL is told about.
In the single-file program config/frozen.py points SSL_CERT_FILE at the system's CA bundle (or at
the bundled certifi when the system has none) before anything runs, so this context works there
too. httpx on its own would always use certifi, ignoring the system's certificates.

The site's own network: a server on the Internet may redirect a request to an address of the network the
site runs in (127.0.0.1, 192.168.x.x, a cloud's metadata service at 169.254.169.254 or 100.100.100.200),
and the site would fetch from there for whoever downloads the file. A client made with guard=True refuses
that with InsideNetwork, a DriverError:
- before each request, the first and every redirect: its host must resolve only to public addresses
  (public(): ipaddress's is_global), kept RESOLVE_SECONDS; a name that doesn't resolve here passes (the
  connection fails, or a proxy resolves it);
- once connected, before anything is sent (httpcore's trace extension), the server connected to must be
  public too, which DNS rebinding can't get round; checked again on the answer.
Through a proxy of the environment (httpx's trust_env: urllib.request.getproxies(), proxy_bypass()) the
connection is the proxy's, so only the names are checked. A request that starts at an address the admin
configured in the site's own network (a server on the LAN: inside()) is checked not at all, nor its
redirects: configured(url) gives the extensions that say so.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import socket
import ssl
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from typing import Any

import httpx

from django.utils.translation import gettext

from .base import DriverError

USER_AGENT = "HFile"

# Seconds: connecting, and waiting for each read or write. Downloads stream, so a long file
# isn't limited by these.
CONNECT_TIMEOUT = 10.0
TIMEOUT = 60.0
# drain() and read_error(): the bytes of an answer read at most, and the seconds for them.
DRAIN_BYTES = 64 * 1024
DRAIN_SECONDS = 10.0
# guard: the seconds the addresses a host resolves to are kept, and the request extension of configured().
RESOLVE_SECONDS = 30.0
INSIDE = "hfile.inside"
_CALLERS_TRACE = "hfile.trace"  # the request's own trace extension, under the guard's


class InsideNetwork(DriverError):
    """A guarded request, or a redirect it followed, would go to an address of the site's own network: nothing was
    sent there (the module's docstring)."""


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    return ssl.create_default_context()


def client(*, timeout: float = TIMEOUT, headers: dict[str, str] | None = None, verify: bool = True,
           guard: bool = False, **kwargs: Any) -> httpx.Client:
    """An httpx.Client with the site's TLS settings, timeouts and User-Agent.

    Redirects aren't followed unless follow_redirects=True is passed: a driver that follows one
    must not send its credentials to another host.
    `verify=False` is only for a storage the admin set to skip certificate checks.
    guard=True: the requests, and the redirects they follow, go only to public addresses, unless they start at
    an address of the site's own network the admin configured (extensions=configured(url)); see the module's
    docstring.
    """
    all_headers = {"User-Agent": USER_AGENT}
    if headers:
        all_headers.update(headers)
    kwargs.setdefault("follow_redirects", False)
    if guard:
        hooks = {name: list(functions) for name, functions in (kwargs.get("event_hooks") or {}).items()}
        for name, function in _guard(kwargs.get("trust_env", True), kwargs.get("proxy")).items():
            hooks.setdefault(name, []).append(function)
        kwargs["event_hooks"] = hooks
    return httpx.Client(
        timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT),
        verify=ssl_context() if verify else False,
        headers=all_headers,
        **kwargs,
    )



@contextlib.contextmanager
def cut_off(response: httpx.Response, ends: float | None) -> Iterator[None]:
    """While the block runs, a watchdog shuts the response's connection down at `ends` (a time.monotonic()), and a
    read waiting then ends at once: httpx only limits each read to TIMEOUT, which a server answering slowly but
    steadily never runs into (STOR-07). Nothing without `ends`, or without a socket (a test's mock transport)."""
    stream = response.extensions.get("network_stream") if ends is not None else None
    sock = stream.get_extra_info("socket") if stream is not None else None
    if sock is None:
        yield
        return

    def shut_down():
        try:
            socket.socket.shutdown(sock, socket.SHUT_RDWR)  # the TCP connection, under any TLS
        except OSError:
            pass

    watchdog = threading.Timer(max(ends - time.monotonic(), 0.0), shut_down)
    watchdog.daemon = True
    watchdog.start()
    try:
        yield
    finally:
        watchdog.cancel()


def drain(response: httpx.Response) -> None:
    """Close a streamed answer whose body isn't wanted (an error's, a write's, a HEAD's), read to its end first when
    it is short (DRAIN_BYTES in DRAIN_SECONDS at most): its connection can then be used again, and a long body isn't
    read. Closed before the end of its body, even an empty one (a 204's, a HEAD's), an answer takes its connection
    with it. One read already, or begun, is only closed."""
    try:
        if not response.is_stream_consumed:
            with cut_off(response, time.monotonic() + DRAIN_SECONDS):
                read = 0
                for piece in response.iter_raw():
                    read += len(piece)
                    if read > DRAIN_BYTES:
                        break
    except httpx.HTTPError:
        pass
    finally:
        response.close()


def read_error(response: httpx.Response) -> httpx.Response:
    """A streamed answer that is an error, with its body read for the words in it (JSON, a text): the first
    DRAIN_BYTES of it, in DRAIN_SECONDS at most, so that a server can't make the site hold a long body or wait for
    one without end; what breaks off, or can't be decoded, ends it there. Closes `response`, and returns an answer
    with its status, headers and that body, read: json() and text work on it."""
    body = bytearray()
    try:
        with cut_off(response, time.monotonic() + DRAIN_SECONDS):
            for piece in response.iter_bytes():
                body += piece[:DRAIN_BYTES - len(body)]
                if len(body) >= DRAIN_BYTES:
                    break
    except httpx.HTTPError:
        pass
    finally:
        response.close()
    headers = [(name, value) for name, value in response.headers.multi_items()
               if name.lower() not in ("content-encoding", "content-length", "transfer-encoding")]  # of the body as sent
    return httpx.Response(response.status_code, headers=headers, content=bytes(body), request=response.request)


# --- the site's own network -----------------------------------------------------------------------

def public(address: str, port: int) -> bool:
    """Whether a server at this IP address is on the Internet: ipaddress's is_global, false for loopback, private,
    link-local, shared (100.64.0.0/10), unique local and the like; an IPv4-mapped IPv6 address is its IPv4 one.
    `port` isn't used: tests patch this to tell two servers on 127.0.0.1 apart."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


@functools.lru_cache(maxsize=1024)
def _resolve(host: str, port: int, period: int) -> tuple[str, ...] | None:
    try:
        return tuple(info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM))
    except (OSError, UnicodeError):  # socket.gaierror is an OSError
        return None


def _addresses(url: httpx.URL) -> tuple[tuple[str, ...] | None, int]:
    """The IP addresses the host of `url` resolves to (None when it doesn't), kept RESOLVE_SECONDS, and its port."""
    port = url.port or (443 if url.scheme == "https" else 80)
    host = url.raw_host.decode("ascii")
    try:
        return (str(ipaddress.ip_address(host)),), port
    except ValueError:
        return _resolve(host, port, int(time.monotonic() // RESOLVE_SECONDS)), port


def inside(url: str | httpx.URL) -> bool:
    """Whether `url` is in the site's own network: its host resolves, and only to addresses that aren't public (a
    server on the LAN)."""
    try:
        found, port = _addresses(url if isinstance(url, httpx.URL) else httpx.URL(url))
    except (httpx.InvalidURL, UnicodeError):
        return False
    return bool(found) and not any(public(address, port) for address in found)


def configured(url: str) -> dict[str, Any]:
    """The extensions of a guarded client's request to `url`, an address the admin configured: when that is in
    the site's own network (inside()), the request and its redirects aren't checked; else every hop is."""
    return {INSIDE: True} if inside(url) else {}


def _refusal(host: str) -> InsideNetwork:
    return InsideNetwork(gettext("%(host)s is an address inside this site's own network: a server on the Internet "
                                 "can't send this site there.") % {"host": host})


def _guard(trust_env: bool, proxy: Any) -> dict[str, Callable[..., None]]:
    """The event hooks of a guarded client: what is checked, and when, the module's docstring says."""

    def proxied(url: httpx.URL) -> bool:
        if proxy is not None:
            return True
        if not trust_env:
            return False
        proxies = urllib.request.getproxies()
        return bool(proxies.get(url.scheme) or proxies.get("all")) and not urllib.request.proxy_bypass(url.host)

    def peer_check(host: str, callers: Callable[..., Any] | None) -> Callable[[str, dict[str, Any]], None]:
        def trace(event: str, info: dict[str, Any]) -> None:
            if callers is not None:
                callers(event, info)
            if event == "connection.connect_tcp.complete":
                stream = info.get("return_value")
                peer = stream.get_extra_info("server_addr") if stream is not None else None
                if peer and not public(peer[0], peer[1]):
                    stream.close()
                    raise _refusal(host)
        return trace

    def before_sending(request: httpx.Request) -> None:
        if request.extensions.get(INSIDE):
            return
        found, port = _addresses(request.url)
        if found is not None and not all(public(address, port) for address in found):
            raise _refusal(request.url.host)
        if not proxied(request.url):
            callers = request.extensions.setdefault(_CALLERS_TRACE, request.extensions.get("trace"))
            request.extensions["trace"] = peer_check(request.url.host, callers)

    def on_the_answer(response: httpx.Response) -> None:
        if response.request.extensions.get(INSIDE) or proxied(response.request.url):
            return
        stream = response.extensions.get("network_stream")
        peer = stream.get_extra_info("server_addr") if stream is not None else None
        if peer and not public(peer[0], peer[1]):
            raise _refusal(response.request.url.host)

    return {"request": before_sending, "response": on_the_answer}
