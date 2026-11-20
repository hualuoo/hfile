import ipaddress
import re
from contextvars import ContextVar

from rest_framework.settings import api_settings

# The request being handled, set by apps.core.middleware.CurrentRequestMiddleware.
_current_request = ContextVar("current_request", default=None)


def client_ip(request):
    """The caller's IP address as text, or None if it can't be read.

    Behind reverse proxies, set [server] proxies in config.toml to how many there are
    (nginx alone: 1). The address is then taken from X-Forwarded-For, counting that many
    entries from the right, because the entries further left are whatever the
    client chose to send. Without proxies (proxies = 0) the header is ignored.
    DRF's rate limits use the same setting and the same rule.
    """
    return ip_from(request.META)


_PORT = re.compile(r"[0-9]{1,5}")


def address_of(text):
    """The IP address in one hop of X-Forwarded-For (or REMOTE_ADDR), as text, or None.

    Some proxies write the port too: 203.0.113.7:51234, or [2001:db8::7]:51234 and [2001:db8::7].
    An IPv6 zone (%eth0) is dropped, and an IPv4-mapped IPv6 address is the IPv4 one.
    """
    text = text.strip()
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not (rest[:1] == ":" and _PORT.fullmatch(rest[1:]))):
            return None
        text = host
    elif text.count(":") == 1:  # IPv4 and a port: an IPv6 address has two colons at least
        text, _, port = text.partition(":")
        if not _PORT.fullmatch(port):
            return None
    try:
        ip = ipaddress.ip_address(text.split("%", 1)[0])
    except ValueError:
        return None
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip)


def ip_from(meta):
    """client_ip() from a request's META, or from a WSGI environ (the request log, apps/core/access_log.py)."""
    address = meta.get("REMOTE_ADDR", "")
    proxies = api_settings.NUM_PROXIES or 0
    forwarded = meta.get("HTTP_X_FORWARDED_FOR")
    if proxies and forwarded:
        hops = forwarded.split(",")
        address = hops[-min(proxies, len(hops))]
    return address_of(address)


def bind_request(request):
    """Make `request` the current one; returns the token for unbind_request()."""
    return _current_request.set(request)


def unbind_request(token):
    _current_request.reset(token)


def current_request():
    """The Django request being handled, or None outside a request (and in background threads)."""
    return _current_request.get()


def request_ip():
    """client_ip() of the request being handled, or None outside a request (management commands, tests without one).

    Code deep below a view (a log entry, say) reads the IP from here, without the request passed down.
    """
    request = _current_request.get()
    return client_ip(request) if request is not None else None
