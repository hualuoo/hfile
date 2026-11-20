"""manage.py backup: the whole database, MySQL or SQLite, into one .sql.gz file (apps/upgrades/backup.py)."""

from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError
from django.utils.translation import gettext as _, ngettext

from apps.upgrades import backup, service


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _("Backs up the whole database into a .sql.gz file, by default in the backup "
                 "folder ([paths] backups in the settings file). Uploaded files aren't in it.")

    # Not Django's system checks: some ask the database, and one that can't be reached then gets the
    # plain message below instead of a traceback.
    requires_system_checks = []

    def add_arguments(self, parser):
        parser.add_argument("-o", "--output", help=_("Path of the backup file, ending in .sql.gz. By default in the backup folder, named after the time"))

    def handle(self, *args, output=None, **options):
        if output and not output.endswith(".sql.gz"):
            raise CommandError(_("The backup file's name must end in .sql.gz."))
        path = Path(output) if output else backup.new_path()
        try:
            tables = backup.dump(path)
        except (OSError, DatabaseError) as error:
            raise CommandError(_("The backup failed: %(error)s") % {"error": service.describe(error)}) from error
        self.stdout.write(
            ngettext("Backed up %(count)s table: %(path)s (%(size)s)", "Backed up %(count)s tables: %(path)s (%(size)s)", tables)
            % {"count": tables, "path": path, "size": backup.size_text(path)}
        )
