"""`hfile unblock`: empties the IP and User-Agent blocklists of 访问控制, for an admin whom the rules
shut out of the site (saving refuses rules that block whoever saves them, but an IP can change)."""

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.translation import gettext as _

from apps.siteconf.models import CACHE_SECONDS, SiteSettings


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return _('Empties the IP and UA blocklists of "Access control", for when they shut you out of the site.')

    def handle(self, *args, **options):
        with transaction.atomic():
            SiteSettings.load()  # the row, made if missing
            site = SiteSettings.objects.select_for_update().get(pk=1)
            ips = len([line for line in site.ip_blocklist.splitlines() if line.strip() and not line.strip().startswith("#")])
            agents = len([line for line in site.ua_blocklist.splitlines() if line.strip() and not line.strip().startswith("#")])
            if not ips and not agents:
                self.stdout.write(_("The IP and UA blocklists were already empty; nothing was changed."))
                return
            site.ip_blocklist = site.ua_blocklist = ""
            site.save(update_fields=["ip_blocklist", "ua_blocklist", "updated_at"])
        self.stdout.write(self.style.SUCCESS(
            _("Access control cleared (IP blocklist rules: %(ips)s, UA blocklist rules: "
              "%(agents)s). A running site picks it up within %(seconds)s s; no restart needed.") % {"ips": ips, "agents": agents, "seconds": CACHE_SECONDS}
        ))
