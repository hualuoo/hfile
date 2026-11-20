"""A system check: the spool must not be inside [paths] uploads, which is served at /media/ (a reverse
proxy may serve that folder by itself, past the site's checks of names)."""

from pathlib import Path

from django.conf import settings
from django.core.checks import Warning, register
from django.utils.translation import gettext as _


@register()
def spool_not_served(app_configs, **kwargs):
    spool = Path(settings.UPLOAD_SPOOL_DIR).resolve()
    media = Path(settings.MEDIA_ROOT).resolve()
    if spool == media or media in spool.parents:
        return [Warning(
            _("Files being uploaded are kept in %(spool)s, which is inside the uploads folder "
              "([paths] uploads = %(media)s), so they could be downloaded through /media/.") % {"spool": spool, "media": media},
            hint=_("Set [paths] uploads in the settings file to a folder of its own (the default is uploads)."),
            id="uploads.W001",
        )]
    return []
