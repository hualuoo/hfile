from django.apps import AppConfig
from django.utils.translation import gettext_lazy

# What the file browser's pages (the browse pages and the share pages) load of this app.
SCRIPTS = ("office/js/office.js",)


class OfficeConfig(AppConfig):
    """OnlyOffice: previewing and editing Office files with an OnlyOffice Document Server (inventory 7.3).
    Editor sessions in the database (apps.office.models), the signed editor config, the Document Server's
    callback that saves, the editor page, and the viewer the file browser opens Office files with."""

    name = "apps.office"
    verbose_name = "OnlyOffice"

    def ready(self):
        from django.db.models.signals import post_save

        from apps.core import cleanup
        from apps.files import pages
        from apps.files import signals as file_signals
        from apps.storages.models import StorageSource

        from . import sessions

        cleanup.register("office", gettext_lazy("Expired OnlyOffice editing sessions"), sessions.remove_expired)
        # Renamed, moved, deleted or replaced files: their document keys go (the next open gets a new one).
        file_signals.changed.connect(sessions.forget_changed, dispatch_uid="office.forget_changed")
        # New params for a storage (another root, bucket or base path), or the storage saved disabled: its keys go.
        post_save.connect(sessions.storage_saved, sender=StorageSource, dispatch_uid="office.storage_saved")
        pages.add_assets(scripts=SCRIPTS)
