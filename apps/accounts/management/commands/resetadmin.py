"""hfile resetadmin [用户名]: a superuser who can't log in any more gets a new password, and their
two-step verification is turned off. A command, not a page: only someone on the server can do it.

With no username: the only superuser, or a list to choose from. The account is enabled again (and
made staff) if it wasn't. Its sessions end with the new password. Refused before HFile is installed.
"""

import getpass
import sys

from django.conf import settings
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils.translation import gettext, ngettext

from apps.accounts import entry
from apps.accounts.backends import find_user
from apps.accounts.models import Kind
from apps.siteconf.models import SiteSettings

TRIES = 3


class Command(BaseCommand):
    @property
    def help(self):  # a property: argparse can't take a lazy text, and it is shown in the command's language
        return gettext("Gives a superuser a new password and turns off their two-step verification (for a forgotten password or a lost phone).")

    def add_arguments(self, parser):
        parser.add_argument("username", nargs="?", help=gettext("the superuser's username; can be left out when there is only one superuser"))

    def handle(self, *args, username=None, **options):
        if settings.SETUP_MODE:
            raise CommandError(gettext("HFile isn't installed yet: install it with the setup wizard first, which creates the first admin."))
        interactive = sys.stdin.isatty()
        user = self.choose(username, interactive)
        if not interactive:
            raise CommandError(gettext("Run this command in a terminal: the new password is entered twice and isn't shown."))
        password = self.ask_password(user)
        with transaction.atomic():
            user = type(user).objects.select_for_update().get(pk=user.pk)
            had_totp, was_active, was_staff = user.totp_enabled, user.is_active, user.is_staff
            user.set_password(password)
            user.totp_secret = ""
            user.totp_last_step = None
            user.totp_enabled_at = None
            user.is_active = user.is_staff = True
            user.save()
        self.stdout.write(gettext("%(username)s has a new password; their logins elsewhere have ended.") % {"username": user.username})
        if had_totp:
            self.stdout.write(gettext("Their two-step verification is off; after logging in it can be set up again from the menu at the top right."))
        if not was_active:
            self.stdout.write(gettext("This account was disabled and is enabled again now."))
        if not was_staff:
            self.stdout.write(gettext("This account couldn't open the Manage page before; now it can."))
        self.stdout.write(self.where_to_log_in())

    def choose(self, username, interactive):
        User = get_user_model()
        superusers = list(User.objects.filter(is_superuser=True, kind=Kind.NORMAL).order_by("id"))
        if username:
            user = find_user(username)
            if user is None:
                raise CommandError(gettext('There is no account with the username "%(username)s".') % {"username": username})
            if not user.is_superuser or user.kind != Kind.NORMAL:
                raise CommandError(gettext("\"%(username)s\" isn't a superuser. %(listing)s") % {"username": user.username, "listing": self.listing(superusers)})
            return user
        if not superusers:
            raise CommandError(gettext("There is no superuser yet: create one with the createsuperuser command."))
        if len(superusers) == 1:
            return superusers[0]
        if not interactive:
            raise CommandError(ngettext("There is %(count)s superuser; please give the "
                                        "username: resetadmin USERNAME. %(listing)s", "There are %(count)s superusers; please give the username: resetadmin USERNAME. %(listing)s", len(superusers)) % {"count": len(superusers), "listing": self.listing(superusers)})
        self.stdout.write(ngettext("There is %(count)s superuser:", "There are %(count)s superusers:", len(superusers)) % {"count": len(superusers)})
        for number, user in enumerate(superusers, 1):
            self.stdout.write(f"  {number}. {self.describe(user)}")
        for _ in range(TRIES):
            answer = input(gettext("Which one gets a new password? Enter a number or username: ")).strip()
            if answer.isdigit() and 1 <= int(answer) <= len(superusers):
                return superusers[int(answer) - 1]
            chosen = find_user(answer)
            if chosen is not None and chosen in superusers:
                return chosen
            self.stderr.write(gettext("There is no such one. Please try again."))
        raise CommandError(gettext("Nothing was chosen; nothing was changed."))

    @staticmethod
    def describe(user):
        notes = [note for note, on in ((gettext("disabled"), not user.is_active), (gettext("two-step verification on"), user.totp_enabled)) if on]
        name = gettext("%(username)s (%(nickname)s)") % {"username": user.username, "nickname": user.nickname} if user.nickname else user.username
        return gettext("%(name)s: %(notes)s") % {"name": name, "notes": gettext(", ").join(notes)} if notes else name

    def listing(self, superusers):
        return gettext("Superusers: ") + gettext(", ").join(user.username for user in superusers) if superusers else ""

    def ask_password(self, user):
        for _ in range(TRIES):
            password = getpass.getpass(gettext("New password for %(username)s: ") % {"username": user.username})
            if not password:
                self.stderr.write(gettext("The password can't be empty."))
                continue
            if password != getpass.getpass(gettext("Again: ")):
                self.stderr.write(gettext("The two don't match."))
                continue
            try:
                password_validation.validate_password(password, user)
            except ValidationError as error:
                self.stderr.write("\n".join(error.messages))
                continue
            return password
        raise CommandError(gettext("No new password was set; nothing was changed."))

    @staticmethod
    def where_to_log_in():
        site = SiteSettings.load()
        base = settings.SITE_URL.rstrip("/")
        secure = entry.current()
        url = base + entry.login_path(secure)
        lines = [(gettext("Login address: %(url)s (a secure login entrance is set; logging in only works here)") if secure else gettext("Login address: %(url)s")) % {"url": url}]
        if site.login_captcha:
            lines.append(gettext("Logging in asks for a captcha (it's on in \"Security settings\")."))
        return "\n".join(lines)
