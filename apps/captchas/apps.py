from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class CaptchasConfig(AppConfig):
    """Image captchas (the pictures come from the `captcha` package; this app is `captchas`). The login
    asks for one while SiteSettings.login_captcha is on (apps/accounts/login_steps.py, apps/accounts/admin.py)."""

    name = "apps.captchas"
    verbose_name = gettext_lazy("Captcha")
