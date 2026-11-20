from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class AccountsConfig(AppConfig):
    """Users, logging in and out (the session API), changing one's password."""

    name = "apps.accounts"
    verbose_name = gettext_lazy("Accounts")

    def ready(self):
        from django.contrib.auth.signals import user_logged_in, user_login_failed

        from apps.core import cleanup, masking

        from . import login_steps
        from .signals import on_login, on_login_failed, remove_expired_sessions

        user_logged_in.connect(on_login, dispatch_uid="accounts.on_login")
        user_login_failed.connect(on_login_failed, dispatch_uid="accounts.on_login_failed")
        login_steps.register()
        cleanup.register("sessions", gettext_lazy("Expired sessions"), remove_expired_sessions)
        # The secure login entry is a secret in its address (apps/accounts/entry.py): logs write /login/<entry>/.
        # Where /login/ starts an address (also in ?next=), or after a site's address; not /sso/x/login/….
        masking.register_path(r"(?:^|(?<=[\s=\"'(]))/login/(?P<secret>[^/?&#\s\"']+)", "entry")
        masking.register_path(r"https?://[^/\s\"']+/login/(?P<secret>[^/?&#\s\"']+)", "entry")
