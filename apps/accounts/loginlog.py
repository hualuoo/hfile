"""The 登录日志 (models.LoginLog): a row for each login attempt that was checked, while
SiteSettings.login_log is on. Never a password; the username as it was typed.

Rows come from:
- Django's user_logged_in and user_login_failed signals (signals.py): every password login, through
  the session API and the Django admin's login page, and single sign-on's (the sso app logs in with
  login() too, and records its own failures here);
- the login steps, when an answer is wrong (the image captcha, two-step verification);
- the login rate limit: once per address and minute, not for every request it turns away (a
  program trying thousands of passwords would otherwise fill the table).
Requests that never get to a check (a wrong secure login entry, a form without a password) aren't
logged.
"""

import logging

from django.conf import settings
from django.core.cache import cache
from django.db import DatabaseError, transaction
from django.utils.translation import gettext_lazy

from apps.core.net import client_ip
from apps.core.throttles import UNKNOWN, address_key
from apps.ipgeo.lookup import locate
from apps.siteconf.models import SiteSettings

from .models import LoginLog, User

logger = logging.getLogger(__name__)

REASONS = {
    "unknown_user": gettext_lazy("No such user"),
    "wrong_password": gettext_lazy("Wrong password"),
    "inactive": gettext_lazy("Account disabled"),
    "builtin": gettext_lazy("Built-in users can't log in"),
    "captcha": gettext_lazy("Wrong captcha"),
    "totp": gettext_lazy("Two-step verification failed"),
    "throttled": gettext_lazy("Stopped: too many"),
    "needs_steps": gettext_lazy("The Django admin's login page can't do two-step verification"),
    # Single sign-on (the sso app): the provider's account isn't bound to a user (and new users aren't
    # made), the provider said no (error=, the person cancelled, say), the provider failed (the token or
    # the userinfo request), its userinfo had no usable id.
    "sso_not_bound": gettext_lazy("Single sign-on: account not linked"),
    "sso_denied": gettext_lazy("Single sign-on: the provider refused"),
    "sso_provider": gettext_lazy("Single sign-on: provider error"),
    "sso_userinfo": gettext_lazy("Single sign-on: no usable ID in the user info"),
}
SOURCES = {"site": gettext_lazy("Site"), "admin": gettext_lazy("Django admin"), "sso": gettext_lazy("Single sign-on")}

USERNAME_MAX = LoginLog._meta.get_field("username").max_length
USER_AGENT_MAX = LoginLog._meta.get_field("user_agent").max_length


def source_of(request):
    """Where a login came from: the Django admin's login page, single sign-on, or the site. A login that
    began with single sign-on and finishes a step through the session API says so itself
    (request.login_source, apps/accounts/login.py)."""
    given = getattr(request, "login_source", "")
    if given in SOURCES:
        return given
    path = getattr(request, "path", "") or ""
    if path.startswith("/sso/"):
        return "sso"
    return "admin" if path.startswith(f"/{settings.ADMIN_PATH}/") else "site"


def _text(value, limit):
    return str(value or "").replace("\x00", "")[:limit]


def record(request, *, success, username="", user=None, reason=""):
    """Writes a row, unless login_log is off, with the IP's location while IP 归属地 is on (ipgeo app). A
    failure to write it doesn't stop the login."""
    if request is None:
        return
    try:
        if not SiteSettings.load().login_log:
            return
        ip = client_ip(request)
        location = locate(ip)
        with transaction.atomic():
            LoginLog.objects.create(
                username=_text(username or (user.get_username() if user else ""), USERNAME_MAX),
                user=user if user is not None and user.pk else None,
                ip=ip,
                ip_location=location,
                user_agent=_text(request.META.get("HTTP_USER_AGENT"), USER_AGENT_MAX),
                result=LoginLog.Result.SUCCESS if success else LoginLog.Result.FAILURE,
                reason="" if success else reason,
                source=source_of(request),
            )
    except DatabaseError:
        logger.warning("Could not write the login log", exc_info=True)


def failure_reason(account):
    """Why a password login of `account` (None: no such user) failed, as a code of REASONS."""
    if account is None:
        return "unknown_user"
    if account.is_builtin:
        return "builtin"
    if not account.is_active:
        return "inactive"
    return "wrong_password"


THROTTLED_KEY = "hfile-loginlog-throttled:{}"


def record_throttled(request, username="", seconds=60):
    """A login the rate limit turned away: logged once per address for `seconds`."""
    ip = client_ip(request)
    key = THROTTLED_KEY.format(UNKNOWN if ip is None else address_key(ip))
    if cache.add(key, 1, timeout=max(int(seconds), 1)):
        record(request, success=False, username=username, user=None, reason="throttled")


def typed_username(data):
    """The username a login form sent, if any (for the log of a request turned away early)."""
    value = data.get(User.USERNAME_FIELD) if isinstance(data, dict) else None
    return value if isinstance(value, str) else ""
