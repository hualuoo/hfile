"""Helpers for the API views."""

from django.db import transaction


class LockedSettingsMixin:
    """For a view of settings kept in one row (`settings_model`, with a load() that makes it if missing).

    A change locks the row and reads it again first, inside one transaction: two admins saving at
    the same moment then go one after the other, so neither undoes the other's fields, and the
    log entry of the change compares with what was really there before.
    """

    settings_model = None

    def get_object(self):
        config = self.settings_model.load()
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return config
        return type(config).objects.select_for_update().get(pk=config.pk)

    def update(self, request, *args, **kwargs):
        with transaction.atomic():
            return super().update(request, *args, **kwargs)
