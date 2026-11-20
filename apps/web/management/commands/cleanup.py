"""`hfile cleanup`: removes what has expired (stale upload sessions and their files, expired short links
and their download logs; later shares and OnlyOffice sessions), the jobs the apps registered in
apps.core.cleanup. Meant for cron, e.g. once an hour:

    17 * * * *  /srv/hfile/hfile cleanup --config /srv/hfile/config.toml

One line per job with its count; the exit code is 1 when a job failed (the others still ran).
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils.translation import gettext as _, ngettext

from apps.core import cleanup


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _("Deletes what has expired (unfinished upload sessions and their temporary files, and "
                 "more); meant to run from cron. One line per job; the exit code is 1 when a job fails.")

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help=_("only count what could be deleted; delete nothing"))
        parser.add_argument("--only", action="append", metavar=_("NAME"), help=_("run only this job (can be given several times); see --list for the names"))
        parser.add_argument("--list", action="store_true", dest="show_list", help=_("list all cleanup jobs"))

    def handle(self, *args, dry_run=False, only=None, show_list=False, **options):
        jobs = cleanup.jobs()
        if show_list:
            for job in jobs:
                self.stdout.write(f"{job.name}\t{job.label}")
            if not jobs:
                self.stdout.write(_("No cleanup jobs."))
            return
        errors = []
        try:
            results = cleanup.run(only=only, dry_run=dry_run, errors=errors)
        except ValueError as error:
            raise CommandError(str(error)) from None
        failed = {name: error for name, _label, error in errors}
        for name, label, count in results:
            if count is None:
                reason = str(failed.get(name) or "").strip() or type(failed.get(name)).__name__
                self.stderr.write(_("%(label)s: failed: %(reason)s") % {"label": label, "reason": reason})
            elif dry_run:
                self.stdout.write(ngettext("%(label)s: %(count)s can be deleted", "%(label)s: %(count)s can be deleted", count) % {"label": label, "count": count})
            else:
                self.stdout.write(ngettext("%(label)s: deleted %(count)s", "%(label)s: deleted %(count)s", count) % {"label": label, "count": count})
        if not results:
            self.stdout.write(_("No cleanup jobs."))
        if dry_run and results:
            self.stdout.write(_("This was a dry run: nothing was deleted."))
        if errors:
            raise CommandError(ngettext("%(count)s cleanup job failed; see above (the details are in the log).", "%(count)s cleanup jobs failed; see above (the details are in the log).", len(errors)) % {"count": len(errors)})
