"""The site's addresses. The Django admin's is random (config/admin_path.py); /admin/ is a plain 404.

A new app adds its pages to apps/web/urls.py (or includes its own urls module here) and its API to
config/api_urls.py. A new first path segment also goes into config/admin_path.py TAKEN.
"""

from django.conf import settings
from django.contrib import admin
from django.urls import include, path, re_path

from apps.core import uploads

urlpatterns = [
    path(f"{settings.ADMIN_PATH}/", admin.site.urls),
    path("api/v1/", include("config.api_urls")),
    # Files the site saved of what was uploaded to it, also with DEBUG off: the single executable is
    # its own web server. Only names the site gave them (apps/core/uploads.py); a reverse proxy in front
    # may serve /media/ from MEDIA_ROOT instead.
    re_path(r"^media/(?P<path>.+)$", uploads.serve, name="media"),
    # The login page, and with a secure login entry only /login/<entry>/ (apps/accounts/entry.py).
    path("login/", include("apps.accounts.urls")),
    path("site/", include("apps.siteconf.urls")),  # the custom CSS and JS of 显示设置
    # Single sign-on: /sso/<code>/login and its callback, the addresses registered at providers.
    path("sso/", include("apps.sso.urls")),
    path("storages/", include("apps.storages.urls")),
    # Short links, /s/<key>. Path links (/<prefix>/<storage key>/<path>) go through the files app's catch-all.
    path("s/", include("apps.links.urls")),
    # OnlyOffice: the editor page (apps/office/pages.py) and the Document Server's callback (apps/office/views.py).
    path("onlyoffice/", include("apps.office.urls")),
    # A page to try the upload panel on, with DEBUG only (apps/uploads/pages.py).
    path("components/uploads/", include("apps.uploads.urls")),
    # The viewers' self-test in the browser, with DEBUG on only (apps/preview/dev.py).
    path("components/preview/", include("apps.preview.dev_urls")),
    # Shares: /share/<key> (and its /download, /preview), /shares/ (我的分享).
    path("", include("apps.shares.urls")),
    path("", include("apps.web.urls")),
    # Signed downloads (/pd/) and the file browser's /<storage key>/<path>: last, it takes every
    # address no other pattern did (apps.core.reserved keeps storage keys apart from the site's own).
    path("", include("apps.files.urls")),
]

# Errors are drawn in place, in Chinese and MD3; JSON under /api/ (apps/web/errors.py).
handler400 = "apps.web.errors.bad_request"
handler403 = "apps.web.errors.permission_denied"
handler404 = "apps.web.errors.page_not_found"
handler500 = "apps.web.errors.server_error"
