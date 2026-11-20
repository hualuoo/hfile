"""Rate limits (settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"]), counted per user or per address.

The address is apps.core.net.client_ip's: REMOTE_ADDR, or X-Forwarded-For behind [server] proxies,
with a port some proxies add left out (DRF's own reading keeps it, which gave every port a budget
of its own). An IPv6 address counts by its /64: a connection usually gets a whole /64, and each of
its addresses would otherwise have a budget of its own.
"""

import ipaddress

from rest_framework import throttling

from . import net

UNKNOWN = "unknown"  # what isn't an address shares one budget, rather than one per spelling


def address_key(ident):
    """The address a limit counts for: an IPv6 one as its /64, an IPv4-mapped one as IPv4."""
    try:
        ip = ipaddress.ip_address(str(ident).split("%", 1)[0])
    except ValueError:
        return ident
    if ip.version == 6:
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f"{ip}/64", strict=False))
    return str(ip)


class _ByAddress:
    def get_ident(self, request):
        ip = net.client_ip(request)
        return UNKNOWN if ip is None else address_key(ip)


class ScopedRateThrottle(_ByAddress, throttling.ScopedRateThrottle):
    """DRF's: the view's throttle_scope, per user when logged in, else per address."""


class UserRateThrottle(_ByAddress, throttling.UserRateThrottle):
    """Per user (an address when nobody is logged in); a subclass sets the scope."""


class LoginThrottle(_ByAddress, throttling.SimpleRateThrottle):
    """Logging in (scope "login"): per address, whoever is logged in already. Per user, every account
    one logged in with would bring a fresh budget for guessing another one's password. The Django
    admin's login page (accounts/admin_login.py) counts in the same budget as POST /api/v1/session/."""

    scope = "login"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


def login_allowed(request):
    """For a login outside DRF (the Django admin's page): (allowed, seconds to wait). Counts this request."""
    throttle = LoginThrottle()
    if throttle.allow_request(request, None):
        return True, 0
    return False, int(throttle.wait() or 0) + 1


def login_left(request):
    """Whether this address has a login try left, without counting this request: (allowed, seconds to
    wait). For a page that only counts what fails (a wrong secure login entry, apps/accounts/views.py)."""
    throttle = LoginThrottle()
    throttle.key = throttle.get_cache_key(request, None)
    throttle.now = throttle.timer()
    throttle.history = [moment for moment in throttle.cache.get(throttle.key, []) if moment > throttle.now - throttle.duration]
    if len(throttle.history) < throttle.num_requests:
        return True, 0
    return False, int(throttle.wait() or 0) + 1
