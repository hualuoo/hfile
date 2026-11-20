"""runserver, plus where to go next: the Django admin's random address, or in setup mode the
installer and its 安装口令 (apps/installer/banner.py). Replaces staticfiles' runserver, which it extends:
the installer app comes before staticfiles in INSTALLED_APPS. For development: a database that
doesn't match the program is a warning here, not a stop (apps/upgrades/startup.py LENIENT)."""

from django.conf import settings
from django.contrib.staticfiles.management.commands.runserver import Command as StaticfilesRunserver

from apps.installer import banner


class Command(StaticfilesRunserver):
    def check_migrations(self):
        if not settings.SETUP_MODE:  # no database yet
            super().check_migrations()

    def get_handler(self, *args, **options):
        from apps.upgrades import startup

        startup.LENIENT = True
        return super().get_handler(*args, **options)

    def on_bind(self, server_port):
        super().on_bind(server_port)
        self.stdout.write(banner.render(banner.origin(self.addr, server_port, self.protocol), self.style))
