from django.apps import AppConfig
from django.utils.translation import gettext as _, gettext_lazy


def link_prefix_taken(name):
    """apps.core.reserved's check for the path-link prefix (/<prefix>/<storage key>/<path>, D13): storage
    keys and [site] admin_path can't be it."""
    from .models import SiteSettings

    prefix = SiteSettings.load().direct_link_prefix
    if prefix and name.lower() == prefix.lower():
        return _("/%(name)s/ is the address prefix of direct links (set in Direct and short links). Please choose another.") % {"name": name}
    return ""


class SiteconfConfig(AppConfig):
    """The site's own settings, kept in the database (one row): its name, look, links, security and
    access control (/manage/ 站点设置 … 访问控制), and the diagnostics card."""

    name = "apps.siteconf"
    verbose_name = gettext_lazy("Site settings")

    def ready(self):
        from django.core import checks
        from django.db.models.signals import post_delete, post_migrate, post_save

        from apps.core import reserved, uploads

        from . import models
        from .checks import addresses_check

        # What site_upload_to names: the logo and the icon.
        uploads.register(models.UPLOAD_PATTERN)
        reserved.register_check(link_prefix_taken)
        checks.register(addresses_check, "hfile")
        post_save.connect(models._forget_after_change, sender=models.SiteSettings, dispatch_uid="siteconf.saved")
        post_delete.connect(models._forget_after_change, sender=models.SiteSettings, dispatch_uid="siteconf.deleted")
        # A migration may change the row with SQL (apps/upgrades/tests' migrations do).
        post_migrate.connect(lambda **kwargs: models.forget_cached(), dispatch_uid="siteconf.migrated", weak=False)
