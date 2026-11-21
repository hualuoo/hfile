from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class UploadsConfig(AppConfig):
    """Uploads: chunked sessions for proxied storages, direct uploads to the backend, folder uploads,
    the text editor's save, and the upload panel (App.uploads)."""

    name = "apps.uploads"
    verbose_name = gettext_lazy("Upload")

    def ready(self):
        from apps.core import cleanup

        from . import checks  # noqa: F401 - registers the system check
        from .service import remove_expired

        cleanup.register("uploads", remove_expired)
