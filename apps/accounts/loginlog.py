"""Logins in the 操作日志 (apps/activity): an entry for each login attempt that was checked, a login or a
failed one. Never a password; for a failed one the username as it was typed.

Entries come from:
- Django's user_logged_in and user_login_failed signals (signals.py): every password login, through
  the session API and the Django admin's login page, and single sign-on's (the sso app logs in with
  login() too, and records its own failures here);
- the login steps, when an answer is wrong (the image captcha, two-step verification);
- the login rate limit: once per address and minute, not for every request it turns away (a
  program trying thousands of passwords would otherwise fill the log).
Requests that never get to a check (a wrong secure login entry, a form without a password) aren't
logged.

A login is ActivityLog.Kind.LOGIN with the user as the actor; a failed one LOGIN_FAILED, about the account it
was for (`target`) or, when no account has that name, what was typed (`target_name`). `detail` has "source"
(SOURCES), and for a failed one "reason" (REASONS) and "username" when what was typed isn't the account's
name.
"""

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy

from apps.activity import log as activity
from apps.core.net import client_ip
from apps.core.throttles import UNKNOWN, address_key

from .models import User

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


def record(request, *, success, username="", user=None, reason=""):
    """Writes the entry, with the IP's location while IP 归属地 is on (ipgeo app). A failure to write it doesn't
    stop the login."""
    if request is None:
        return
    account = user if user is not None and user.pk else None
    common = {"quiet": True, "ip": client_ip(request), "user_agent": request.META.get("HTTP_USER_AGENT", "")}
    detail = {"source": source_of(request)}
    if success:
        activity.record(activity.Kind.LOGIN, actor=account, detail=detail, **common)
        return
    typed = activity.text(username, activity.NAME_MAX)
    detail["reason"] = reason
    if account is not None and typed and typed != account.get_username():
        detail["username"] = typed
    activity.record(activity.Kind.LOGIN_FAILED, actor=None, target=account,
                    target_name=account.get_username() if account is not None else typed, detail=detail, **common)


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
