"""The first path segments the site keeps for itself.

The file browser lives at /<storage key>/<path> and path links at /<prefix>/<storage key>/<path>
(decision D13), so a storage key, the path-link prefix and [site] admin_path must not take an address
of the site, nor each other's. Every place that lets an admin choose one asks problem() first.

- The static names are every top-level address of the program: config/admin_path.TAKEN (/site/,
  custom CSS and JS, among them) and /admin/ (always a 404). An app with a top-level address of its
  own adds its first segment with register() in its AppConfig.ready() (and to admin_path.TAKEN, which
  config.toml's admin_path is checked against before Django starts).
- The admin path (settings.ADMIN_PATH). config.toml can change it while storages and the prefix
  exist: admin_path_problem() checks it against the rest, at startup (apps/siteconf/checks.py).
- Checks that apps register for names chosen at run time: the storages app for its storage keys,
  siteconf for the path-link prefix. A check gives the reason a name is taken, or "".

Names compare without case: MySQL's collation does, and so does a browser user typing an address.
"""

from __future__ import annotations

from collections.abc import Callable

from django.conf import settings
from django.utils.translation import gettext as _

from config import admin_path

_names: set[str] = {name.lower() for name in admin_path.TAKEN} | {admin_path.DJANGO_DEFAULT}
_checks: list[Callable[[str], str]] = []


def register(*names: str) -> None:
    _names.update(name.lower() for name in names)


def register_check(check: Callable[[str], str]) -> None:
    """check(name) -> why `name` is taken (Chinese, for the admin), or ""."""
    if check not in _checks:
        _checks.append(check)


def static_names() -> frozenset[str]:
    return frozenset(_names)


def problem(name: str, *, own: Callable[[str], str] | None = None, static: bool = True) -> str:
    """Why `name` can't be a top-level address an admin chooses; "" when it can. `own` is the
    caller's own check (saving a storage key skips the storage keys), so a value isn't taken by
    itself. static=False skips the static names: the path-link prefix's default, "directlink", is
    one of them (kept for it)."""
    lowered = name.lower()
    if static and lowered in _names:
        return _("/%(name)s/ is an address the site uses itself. Please choose another.") % {"name": name}
    if lowered == settings.ADMIN_PATH.lower():
        return _("/%(name)s/ is the address of the Django admin. Please choose another.") % {"name": name}
    for check in _checks:
        if check is own:
            continue
        reason = check(name)
        if reason:
            return reason
    return ""


def admin_path_problem() -> str:
    """Why [site] admin_path can't be the Django admin's address on this site as it is now: a name
    an app registered, a storage key, the path-link prefix. "" when nothing takes it. The run-time
    checks read the database."""
    name = settings.ADMIN_PATH
    if name.lower() in _names:
        return _("/%(name)s/ is an address the site uses itself.") % {"name": name}
    for check in _checks:
        reason = check(name)
        if reason:
            return reason
    return ""
