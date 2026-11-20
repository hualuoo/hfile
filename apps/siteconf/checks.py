"""[site] admin_path against the addresses admins choose on the site: storage keys and the path-link
prefix (apps.core.reserved, decision D13). Saving a key or the prefix refuses the admin's address, but
config.toml can be changed under them, and the Django admin would then shadow a storage's pages or
the path links.

- A system check (a warning: runserver and `check` show it, other commands still run);
- serve stops at startup with the same message (config/wsgi.py), as for any other mistake in
  config.toml; runserver only warns (apps.upgrades.startup.LENIENT).
"""

import sys

from django.conf import settings
from django.core import checks
from django.db import DatabaseError, connection
from django.utils.translation import gettext as _

from apps.core import reserved


def address_problem():
    """What is wrong with [site] admin_path on this site, or "" (also before the tables exist)."""
    if settings.SETUP_MODE:  # no database yet
        return ""
    try:
        reason = reserved.admin_path_problem()
    except DatabaseError:  # before the first upgrade, or no database at all: the upgrade says so
        return ""
    if not reason:
        return ""
    return (
        _('The Django admin address in config.toml, [site] admin_path = "%(path)s", clashes with an address on the site: %(reason)s\n'
          "Change admin_path (or delete the line to use a random address made from secret_key), then start again.") % {"path": settings.ADMIN_PATH, "reason": reason}
    )


def addresses_check(app_configs, **kwargs):
    problem = address_problem()
    return [checks.Warning(problem, id="siteconf.W001")] if problem else []


def stop_on_conflict(out=None):
    """At startup, after the database is upgraded (config/wsgi.py)."""
    if settings.SETUP_MODE:
        return
    try:
        problem = address_problem()
    finally:
        connection.close()  # the server's threads open their own
    if problem:
        from apps.upgrades import startup

        startup.stop(out or sys.stderr, problem)
