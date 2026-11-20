import atexit

from django.apps import AppConfig
from django.db.models.signals import post_delete
from django.utils.translation import gettext_lazy


class StoragesConfig(AppConfig):
    """Storage sources: their drivers (drivers/), the admin's pages and API, grants, rules and the access
    policy (access.py) everything else that touches files goes through."""

    name = "apps.storages"
    verbose_name = gettext_lazy("Storage source")

    def ready(self):
        from apps.accounts.signals import user_copied
        from apps.core import masking, reserved

        from . import instances
        from .grants import copy_user_grants
        from .serializers import key_taken

        # Storage keys are first path segments (/<key>/<path>, decision D13).
        reserved.register_check(key_taken)
        # The OAuth callback's code and state are secrets (apps.core.masking has them from the start).
        masking.register_query("code", "state")
        user_copied.connect(copy_user_grants, dispatch_uid="storages.copy_user_grants")
        # A storage deleted some other way (the Django admin, a shell) loses its instance too.
        post_delete.connect(_forget_instance, sender="storages.StorageSource", dispatch_uid="storages.forget_instance")
        atexit.register(instances.close_all)


def _forget_instance(sender, instance, **kwargs):
    from django.db import transaction

    from . import instances

    storage_id = instance.pk  # Django sets it to None once the row is gone
    transaction.on_commit(lambda: instances.forget(storage_id))
