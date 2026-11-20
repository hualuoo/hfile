from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class LinksConfig(AppConfig):
    """Path links (/<prefix>/<storage key>/<path>) and short links (/s/<key>): making them in the file browser,
    opening them (rate limit, Referer rules, the access decision, the download log), and the admin pages
    短链管理 and 下载日志 (which also lists the 下载 button's downloads through /pd/)."""

    name = "apps.links"
    verbose_name = gettext_lazy("Direct and short links")

    def ready(self):
        from apps.core import cleanup, masking
        from apps.files import download, pages

        from . import delivery, service

        # A short link's key is a bearer token: the access log shows /s/<key>. At the start of an address, after
        # = (?next=/s/…), a space or a quote, and in a full URL; not "/s/" deeper in a path (a folder named s).
        masking.register_path(r"(?:^|(?<=[\s=\"'(]))/s/(?P<secret>[^/?&#\s\"']+)", "key")
        masking.register_path(r"https?://[^/\s\"']+/s/(?P<secret>[^/?&#\s\"']+)", "key")
        # Path links take the first segment the admin chose (直链地址前缀): the browser's catch-all asks first.
        pages.add_route(delivery.route)
        pages.add_assets(scripts=["links/js/links.js"], styles=["links/css/links.css"])
        # The 下载 button's downloads through /pd/ go into the download log too (页面下载).
        download.download_clicked.connect(delivery.page_download, dispatch_uid="links.page_download")
        cleanup.register("short_links", gettext_lazy("Expired short links (with their download log entries)"), service.remove_expired)
