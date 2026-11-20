"""What the program does with the database before it serves anything: config/wsgi.py calls
upgrade_on_start(), so whatever server loads the site does it (serve, after it has taken its port).

- [upgrade] auto on (the single executable's default): upgrade (apps/upgrades/service.py), which backs
  the database up first and puts the backup back when a migration fails.
- auto off (from the source code): only look. A database that needs an upgrade, is newer than the
  program, or isn't HFile's stops the program with what to do; one that matches it starts.

When it can't go on it says why and exits: on a database that doesn't match it the program would
break in stranger ways. runserver (development) only warns instead (LENIENT, set by its command):
Django says so about unapplied migrations itself, and a development server that exits under its
autoreloader just stops serving without a word.
"""

import os
import sys

from django.conf import settings
from django.db import DatabaseError, connection
from django.utils.translation import gettext as _, ngettext

from config.version import VERSION

from . import service

LENIENT = False  # runserver's (apps/installer/management/commands/runserver.py)


def upgrade_on_start(out=None):
    if settings.SETUP_MODE:  # not installed yet: no database to upgrade
        return
    out = out or sys.stderr
    try:
        if settings.AUTO_UPGRADE:
            service.upgrade(out)
        else:
            check(out)
    except service.UpgradeError as error:
        stop(out, str(error))
    except DatabaseError as error:
        stop(out, service.unreachable(error))
    finally:
        connection.close()  # the server's threads open their own


def check(out):
    """Raises UpgradeError unless the database matches this program as it is."""
    current = service.status()
    if current.foreign:
        raise service.UpgradeError(service.foreign_message(current))
    if current.newer:
        raise service.UpgradeError(service.newer_message(current))
    if current.pending:
        count = len(current.pending)
        names = _(", ").join(current.pending[:5])
        if count > 5:
            names = ngettext("%(names)s and others (%(count)s in all)", "%(names)s and others (%(count)s in all)", count) % {"names": names, "count": count}
        raise service.UpgradeError(
            ngettext("The database needs an upgrade: %(count)s migration to go (%(names)s).", "The database needs an upgrade: %(count)s migrations to go (%(names)s).", count)
            % {"count": count, "names": names}
            + "\n"
            + _("Run %(command)s first (it backs up the whole database before anything else), or set [upgrade] "
                "auto = true in the settings file config.toml to have the program upgrade by itself when it starts.")
            % {"command": upgrade_command()}
        )


def upgrade_command():
    """How to upgrade by hand here: with the single executable (auto set off), its own command."""
    if settings.FROZEN:
        return f"{os.path.basename(sys.executable)} upgrade"
    return "python -B manage.py upgrade"


def stop(out, message):
    if LENIENT:
        service.say(out, "\n" + _("Note: %(message)s") % {"message": message})
        return
    service.say(out, "\n" + _("HFile %(version)s didn't start. %(message)s") % {"version": VERSION, "message": message})
    raise SystemExit(1)
