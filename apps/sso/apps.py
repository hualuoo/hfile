from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class SsoConfig(AppConfig):
    """Single sign-on (OAuth 2 / OpenID Connect providers), binding their accounts to users."""

    name = "apps.sso"
    verbose_name = gettext_lazy("Single sign-on")

    def ready(self):
        from django.contrib.auth.signals import user_logged_in
        from django.db.models.signals import post_delete, post_save

        from . import icons
        from .models import Provider
        from .signals import on_login

        user_logged_in.connect(on_login, dispatch_uid="sso.on_login")
        post_save.connect(icons.forget, sender=Provider, dispatch_uid="apps.sso.icons.saved")
        post_delete.connect(icons.forget, sender=Provider, dispatch_uid="apps.sso.icons.deleted")
