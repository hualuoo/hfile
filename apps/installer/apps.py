from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class InstallerConfig(AppConfig):
    """The installer: the first start of a new copy, before config.toml has a secret_key (settings.SETUP_MODE).
    Also the `serve` command (waitress) and runserver, which print where to go next."""

    name = "apps.installer"
    verbose_name = gettext_lazy("Installer")
