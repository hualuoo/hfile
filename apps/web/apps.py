from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class WebConfig(AppConfig):
    """The pages: the shell (base.html), the MD3 styles and scripts, the home page and /manage/."""

    name = "apps.web"
    verbose_name = gettext_lazy("Pages")
