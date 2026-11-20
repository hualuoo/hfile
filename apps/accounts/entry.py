"""The secure login entry (登录安全入口, SiteSettings.secure_login_entry).

While it is set, a password logs in only from /login/<entry>/: that page sends the entry with every
POST /api/v1/session/, which answers 404 to a POST without it; /login/ and any other /login/... are
404 too; base.html leaves out the login button and the login dialog; and the Django admin's login page
refuses (log in at the entry, then open the admin). It shields the login form from scanners; the
admin path does the same for the Django admin. Compared exactly, in constant time.

`hfile resetadmin` prints the address, for an admin who has forgotten it.
"""

import hmac

from apps.siteconf.models import SiteSettings


def current():
    """The entry, or "" when none is set."""
    return (SiteSettings.load().secure_login_entry or "").strip()


def matches(given, entry):
    return isinstance(given, str) and hmac.compare_digest(given.encode("utf-8"), entry.encode("utf-8"))


def login_path(entry=None):
    """The address of the login page: /login/<entry>/ while one is set, else /login/."""
    entry = current() if entry is None else entry
    return f"/login/{entry}/" if entry else "/login/"
