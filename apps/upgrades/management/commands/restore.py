"""manage.py restore FILE: the whole database back from a backup (apps/upgrades/backup.py).

Before anything changes: the backup must be whole, of the site's kind of database and not from a
newer version (its migrations), and the site must not be running with this settings file. Then
the database as it is goes into a copy (before-restore), and the backup replaces it. SQLite does
that in one transaction; MySQL can't, so when a statement fails half way the copy goes back in.
"""

import os

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, connection
from django.utils.translation import gettext as _, ngettext

from config import configfile
from config.version import VERSION
from apps.upgrades import backup, service


def site_running(command):
    """The message for `command` ("restore" or "upgrade") while serve runs with this settings file, or ""."""
    if not configfile.in_use(settings.CONFIG_FILE):
        return ""
    # The example: stopping the service of docs/deploy.md, systemd's or on Windows WinSW's.
    stop = "Stop-Service HFile" if os.name == "nt" else "systemctl stop hfile"
    if command == "upgrade":
        message = _("The site is still running (with the same settings file %(path)s): nothing was "
                    "upgraded, the database is unchanged. Stop the site first (with %(stop)s, "
                    "say, or Ctrl+C in the terminal running it), and start it again after the upgrade.")
    else:
        message = _("The site is still running (with the same settings file %(path)s): nothing was "
                    "restored, the database is unchanged. Stop the site first (with %(stop)s, "
                    "say, or Ctrl+C in the terminal running it), and start it again after the restore.")
    return message % {"path": settings.CONFIG_FILE, "stop": stop}


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _("Replaces the whole database with a backup file: drops all the tables there are now and puts "
                 "in the backup's. The database as it is now is saved to a copy first. Stop the site first.")

    # Not Django's system checks: some ask the database, and one that can't be reached then gets the
    # plain message below instead of a traceback.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("file", help=_("Backup file (.sql.gz)"))
        parser.add_argument("--yes", action="store_true", help=_("Restore without asking"))

    def handle(self, *args, file, yes=False, **options):
        if problem := site_running("restore"):
            raise CommandError(problem)
        try:
            info = backup.read_info(file)
            backup.check_engine(info)
        except backup.BadBackup as error:
            raise CommandError(_("%(file)s: %(error)s") % {"file": file, "error": error}) from error
        if newer := service.newer_in(info.migrations or []):
            raise CommandError(self.newer_message(file, info, newer))
        database = connection.settings_dict["NAME"]
        if info.schema:
            taken = _("Backed up at %(created)s, %(engine)s database %(database)s, holding the database of version %(schema)s")
        else:
            taken = _("Backed up at %(created)s, %(engine)s database %(database)s")
        self.stdout.write(
            _("Backup file: %(file)s") % {"file": file} + "\n"
            + taken % {"created": info.created, "engine": info.engine, "database": info.database, "schema": info.schema}
        )
        if not yes:
            try:
                answer = input(_("This drops all the tables in the database %(database)s "
                                 "and puts in the backup's. Type yes to go on: ") % {"database": database})
            except EOFError:
                answer = ""
            if answer.strip().lower() != "yes":
                raise CommandError(_("Cancelled; the database is unchanged."))

        try:
            with service.lock(self.stdout):
                saved = backup.new_path("before-restore")
                try:
                    tables = backup.dump(saved)
                except (OSError, DatabaseError) as error:
                    raise CommandError(_("Couldn't save the current database first, so nothing was restored: %(error)s") % {"error": service.describe(error)}) from error
                self.stdout.write(
                    ngettext("The current database is saved to %(path)s (%(count)s table).", "The current database is saved to %(path)s (%(count)s tables).", tables)
                    % {"path": saved, "count": tables}
                )
                try:
                    with service.signals_held(self.stdout):  # half a restore is worse than either database
                        backup.restore(file)
                except backup.BadBackup as error:  # found before anything was dropped
                    raise CommandError(_("%(file)s: %(error)s") % {"file": file, "error": error}) from error
                except Exception as error:
                    raise CommandError(self.failed(error, saved)) from error
        except DatabaseError as error:  # the lock: the database can't be reached at all
            raise CommandError(service.unreachable(error)) from error
        except service.UpgradeError as error:  # the lock
            raise CommandError(str(error)) from error

        self.stdout.write(_("Restored."))
        if info.schema and info.schema != VERSION:
            self.stdout.write(
                _("The backup holds the database of version %(schema)s (this is %(version)s). This version upgrades "
                  "it to its own when it starts (with automatic upgrades off, run the upgrade command first).") % {"schema": info.schema, "version": VERSION}
            )

    @staticmethod
    def newer_message(file, info, newer):
        names = _(", ").join(newer[:5])
        if len(newer) > 5:
            names = ngettext("%(names)s and others (%(count)s in all)", "%(names)s and others (%(count)s in all)", len(newer)) % {"names": names, "count": len(newer)}
        version = info.schema or info.version
        if version and version != VERSION:
            message = _("%(file)s: this backup comes from a newer version (%(version)s), with migrations this program "
                        "(%(current)s) doesn't have: %(names)s. This program couldn't use it once restored, so nothing "
                        "was restored and the database is unchanged. Restore it with version %(version)s or newer.")
        else:
            message = _("%(file)s: this backup comes from a newer version, with migrations this program "
                        "(%(current)s) doesn't have: %(names)s. This program couldn't use it once restored, so "
                        "nothing was restored and the database is unchanged. Restore it with a newer version.")
        return message % {"file": file, "version": version, "current": VERSION, "names": names}

    def failed(self, error, saved):
        """After a restore that failed: the message, once MySQL's database is back as it was."""
        what = _("The restore failed: %(error)s") % {"error": service.describe(error)}
        if connection.vendor == "sqlite":  # one transaction: a failed one changed nothing
            return what + "\n" + _("The database is unchanged.")
        self.stdout.write(what + "\n" + _("Putting the database back as it was before the restore…"))
        try:
            with service.signals_held(self.stdout):
                backup.restore(saved)
        except Exception as again:
            return (
                what + "\n" + _("Putting back the database from before the restore failed too: %(error)s") % {"error": service.describe(again)} + "\n"
                + _("The database is now incomplete and the site can't use it. Once the problem is fixed and "
                    "the site stopped, restore %(path)s (saved before the restore) with the restore command.") % {"path": saved}
            )
        return what + "\n" + _("The database is back as it was before the restore (%(path)s). A restore while "
                                "the site runs fails on the data the site writes: stop the site before restoring.") % {"path": saved}
