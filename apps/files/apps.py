from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class FilesConfig(AppConfig):
    """Browsing the storage sources: listings, file operations, downloads and the file browser."""

    name = "apps.files"
    verbose_name = gettext_lazy("Files")

    def ready(self):
        from django.db import transaction
        from django.db.models.signals import post_delete, post_save

        from apps.core import masking
        from apps.storages.models import StorageSource

        from . import pool

        # /pd/ links: the signature is the grant (apps/files/signing.py); the access log shows <sign>.
        masking.register_query("sign")

        # The download threads follow the storages' proxy_connections (apps/files/pool.py).
        def storages_changed(**kwargs):
            transaction.on_commit(pool.resize)

        post_save.connect(storages_changed, sender=StorageSource, dispatch_uid="files.pool.saved", weak=False)
        post_delete.connect(storages_changed, sender=StorageSource, dispatch_uid="files.pool.deleted", weak=False)
