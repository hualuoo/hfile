"""manage.py upgrade: by hand, what a new version does when it starts (apps/upgrades/service.py).

`upgrade --check` only looks. Its exit status: 0 nothing to do, 1 an upgrade is needed, 2 the
database is newer than the program, 3 it isn't HFile's, 4 it can't be read at all.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils import timezone
from django.utils.translation import gettext as _, ngettext

from config.version import VERSION
from apps.upgrades import service

from .restore import site_running

UNREADABLE = 4  # upgrade --check: the database can't be read (not 1, which says it needs an upgrade)


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _("Upgrades the database to this version of the program: backs up the whole database first, and restores the backup if a migration fails.")

    # Not Django's system checks: some ask the database, and one that can't be reached then gets the
    # plain message below instead of a traceback.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help=_("Only check, don't change the database. Exit status: 0 no upgrade needed, 1 an upgrade is needed, "
                   "2 the database is newer than the program, 3 not an HFile database, 4 the database can't be read."),
        )

    def handle(self, *args, check=False, **options):
        if not check and (problem := site_running("upgrade")):  # the site would run on tables changing under it
            raise CommandError(problem)
        service.stop_on_sigterm()  # stopped mid-way, the backup still goes back in
        try:
            if check:
                code = self.report()
            else:
                row = service.upgrade(self.stdout)
        except service.UpgradeError as error:
            raise CommandError(str(error)) from error
        except DatabaseError as error:  # can't even read it; nothing was changed
            raise CommandError(service.unreachable(error), returncode=UNREADABLE if check else 1) from error
        if check:
            if code:
                raise SystemExit(code)
        elif row is None:
            self.stdout.write(_("The database is at version %(version)s already; no upgrade needed.") % {"version": VERSION})

    def report(self):
        current = service.status()
        self.stdout.write(_("Program version: %(version)s") % {"version": VERSION})
        if current.last:
            self.stdout.write(_("Last database upgrade: %(version)s, %(time)s") % {
                "version": current.last.version, "time": f"{timezone.localtime(current.last.finished_at):%Y-%m-%d %H:%M}"})
        if current.foreign:  # upgrade would refuse it: not 1, "needs an upgrade"
            self.stdout.write(service.foreign_message(current))
            return 3
        if current.newer:
            self.stdout.write(service.newer_message(current))
            return 2
        if current.pending:
            self.stdout.write(ngettext("Upgrade needed: %(count)s migration", "Upgrade needed: %(count)s migrations", len(current.pending)) % {"count": len(current.pending)})
            for name in current.pending:
                self.stdout.write(f"  {name}")
            return 1
        self.stdout.write(_("The database needs no upgrade."))
        return 0
