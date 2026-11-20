from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class SharesConfig(AppConfig):
    """Share links: creating them in the file browser, the share page /share/<key>, 我的分享 and the
    admins' 分享列表."""

    name = "apps.shares"
    verbose_name = gettext_lazy("Share")

    def ready(self):
        from apps.core import cleanup, masking, reserved
        from apps.files import pages

        from .service import remove_expired

        cleanup.register("shares", gettext_lazy("Expired shares"), remove_expired)
        # A share's key is what lets anybody in when it has no password: the access log shows <key>.
        masking.register_path(r"/share/(?P<secret>[A-Za-z0-9_-]+)", "key")
        masking.register_path(r"/api/v1/(?:manage/)?shares/(?!clear-expired/)(?P<secret>[A-Za-z0-9_-]+)/", "key")
        reserved.register("share", "shares")
        # The browser's 创建分享 (and its dialog) on every page that shows files' browser.
        pages.add_assets(scripts=["shares/js/create.js"], styles=["shares/css/shares.css"])
