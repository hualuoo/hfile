"""REST API, version 1 (mounted at /api/v1/).

    GET    session/                  login state and the CSRF token:
                                     {"authenticated", "user": {...} or null, "csrf_token", "step": null or "..."}
    POST   session/                  log in: {"username", "password"}. 201 {"authenticated": true, "user"} when
                                     logged in; 200 {"authenticated": false, "step": "<name>", "detail"} when the
                                     account needs another step first (apps/accounts/login.py): POST again with
                                     {"step": "<name>", ...that step's fields}: "captcha" (before the password,
                                     with "captcha_key" and "captcha"), "totp" ({"code"}). With a secure login
                                     entry set, every POST needs "entry" too, else 404. Per address at most 10 a minute
    DELETE session/                  log out (204), also for a session that is no longer anybody's
    PUT    users/me/password/        {"old_password", "new_password"}: this session stays logged in, the
                                     others end (204). An account without a usable password (made by single
                                     sign-on) leaves "old_password" out. At most 10 an hour
    GET    users/me/totp/            two-step verification: {"enabled", "enabled_at", "available"}
    POST   users/me/totp/setup/      superusers: a new secret {"secret", "uri", "qr"}, kept in the session
    POST   users/me/totp/            {"code", "password"}: turns it on with that secret
    POST   users/me/totp/disable/    {"password"}: turns it off. These three: 20 an hour
    POST   captcha/                  {"scene": "login"}: a captcha picture {"required": true, "key", "image", "width",
                                     "height", "length", "expires_in"}, or {"required": false}. 30 a minute

    GET    site/                     the site's public settings, for everybody (siteconf PublicSiteSerializer)

    GET    manage/version/           staff: the program's version, whether the database matches it
                                     ("pending", "newer"), "auto_upgrade", "backup_dir" and the upgrade log
    GET    manage/settings/<group>/  staff: a page of the site's settings, group = site, view, link,
    PATCH                            security or access (apps/siteconf/serializers.py); secrets write-only
    PUT    manage/settings/site/<favicon|home_logo>/   staff: upload the icon or the logo (multipart "file")
    DELETE                           ... back to HFile's own
    POST   manage/settings/access/test/                staff: would these blocklists block this IP / UA
    GET    manage/diagnostics/       staff: the request as the site sees it (client IP, scheme, host,
                                     [site] url, [server] proxies, headers with the secrets hidden)

    GET    manage/users/             staff: users (?search= &is_active= &kind= &ordering= &page=), POST creates one
    GET    manage/users/{id}/        one user; PATCH changes them, DELETE deletes them (apps/accounts/manage_views.py)
    POST   manage/users/{id}/copy/   a new user with their default permissions and grants
    GET    manage/login-logs/        staff: the login log (?username= &ip= &result= &since= &until=); DELETE
                                     with the same filters deletes what they find
    POST   manage/login-logs/delete/ staff: {"ids": [...]} deletes those entries
    GET    manage/ip-geo/            staff: IP 归属地: on or off, the database in use, the last job, how many stored
                                     IPs have no location yet; PATCH {"enabled"}; POST download/, fill/; GET job/,
                                     check/ (apps/ipgeo/views.py)

    GET    sso/providers/            anybody: the login buttons of single sign-on [{"code", "name", "icon"}]
    GET    sso/links/                the user's providers and bound accounts (账号绑定): {"has_password", "providers"}
    POST   sso/links/{code}/         {"password", "next"}: begins binding, answers {"url"} to go to
    DELETE sso/links/{code}/         unbinds (refused for the last way to log in)
    GET    manage/sso/providers/     staff: every provider (not in pages); POST creates one (superusers)
    GET    manage/sso/providers/{id}/  PATCH changes it ("client_secret": "" keeps it), DELETE deletes it
                                     (superusers: a provider's settings decide who logs in as whom)
    POST   manage/sso/providers/order/ superusers: {"ids": [...]}: the order on the login page
    POST   manage/sso/discover/      superusers: {"url"}: an OpenID configuration's endpoints, read by the server
    GET    manage/users/{id}/identities/   a user's bound accounts; POST {"provider", "external_id"} binds one
    DELETE manage/users/{id}/identities/{identity id}/   (not one's own: 账号绑定; others' admins: superusers)
    apps/storages/... and manage/storages/..., manage/storage-types/, manage/users/<id>/grants/,
    manage/grant-options/: the storages app (apps/storages/api_urls.py, listed in apps/storages/views.py)
    files/<key>/list/, item/, mkdir/, rename/, move/, copy/, delete/: browsing and the file operations
                                     (apps/files/views.py)
    POST   preview/markdown/         {"url": a viewer's signed same-origin URL}: the Markdown file as HTML,
                                     rendered on the server (apps/preview/views.py)
    POST   preview/kkfileview/       {"url"}: the file's kkFileView address. These two: 60 a minute

    POST   apps/uploads/                  an upload session; GET, PUT (a chunk), DELETE uploads/<id>/;
    POST   uploads/<id>/complete/    POST uploads/folders/ (a folder upload's folders); PUT uploads/text/ (the
                                     text editor's save): the uploads app (apps/uploads/api_urls.py, listed in apps/uploads/views.py)
    POST   links/<key>/path/, links/<key>/short/   path links and short links of files (apps/links/views.py);
                                     manage/short-links/..., manage/link-limits/, manage/download-logs/...: 短链管理
                                     and 下载日志 (apps/links/manage_views.py)
    apps/shares/, shares/<key>/... and manage/shares/: share links, 我的分享 and 分享列表 (apps/shares/views.py)
    POST   office/config/            the OnlyOffice editor config of a file, {"storage", "path"} or {"share", "path"}:
                                     the office app (apps/office/views.py)

A new app adds its paths below (or registers a viewset on the router), with the same conventions:
JSON in and out (apps/core/parsers.py, apps/core/renderers.py), errors as {"detail": "..."} or {field: [...]}
in Chinese (apps/core/exceptions.py), pages of 20 by ?page= and ?page_size= (apps/core/pagination.py), and
only logged-in users unless a view says otherwise (settings.REST_FRAMEWORK).
"""

from django.urls import include, path, re_path
from rest_framework.routers import SimpleRouter

from apps.accounts.manage_views import ManageLoginLogDeleteView, ManageLoginLogView, ManageUserViewSet
from apps.accounts.views import PasswordView, SessionView, TotpDisableView, TotpSetupView, TotpView
from apps.captchas.views import CaptchaView
from apps.siteconf.views import AccessTestView, DiagnosticsView, SettingsView, SiteImageView, SiteView
from apps.sso.views import (
    DiscoverView,
    LinksView,
    LinkView,
    ManageIdentitiesView,
    ManageIdentityView,
    ManageProviderViewSet,
    PublicProvidersView,
)
from apps.upgrades.views import VersionView

router = SimpleRouter()
router.register("manage/users", ManageUserViewSet, basename="manage-user")
router.register("manage/sso/providers", ManageProviderViewSet, basename="manage-sso-provider")

urlpatterns = [
    path("session/", SessionView.as_view(), name="session"),
    path("users/me/password/", PasswordView.as_view(), name="user-me-password"),
    path("users/me/totp/", TotpView.as_view(), name="user-me-totp"),
    path("users/me/totp/setup/", TotpSetupView.as_view(), name="user-me-totp-setup"),
    path("users/me/totp/disable/", TotpDisableView.as_view(), name="user-me-totp-disable"),
    path("captcha/", CaptchaView.as_view(), name="captcha"),
    path("site/", SiteView.as_view(), name="site"),
    path("manage/version/", VersionView.as_view(), name="manage-version"),
    path("manage/login-logs/", ManageLoginLogView.as_view(), name="manage-login-logs"),
    path("manage/login-logs/delete/", ManageLoginLogDeleteView.as_view(), name="manage-login-logs-delete"),
    path("manage/settings/access/test/", AccessTestView.as_view(), name="manage-settings-access-test"),
    path("manage/settings/site/<str:field>/", SiteImageView.as_view(), name="manage-settings-image"),
    re_path(r"^manage/settings/(?P<group>[a-z]+)/$", SettingsView.as_view(), name="manage-settings"),
    path("manage/diagnostics/", DiagnosticsView.as_view(), name="manage-diagnostics"),
    path("sso/providers/", PublicProvidersView.as_view(), name="sso-providers"),
    path("sso/links/", LinksView.as_view(), name="sso-links"),
    path("sso/links/<str:code>/", LinkView.as_view(), name="sso-link"),
    path("manage/sso/discover/", DiscoverView.as_view(), name="manage-sso-discover"),
    path("manage/users/<int:user_id>/identities/", ManageIdentitiesView.as_view(), name="manage-user-identities"),
    path("manage/users/<int:user_id>/identities/<int:pk>/", ManageIdentityView.as_view(), name="manage-user-identity"),
    path("", include("apps.storages.api_urls")),
    path("", include("apps.files.api_urls")),
    path("", include("apps.uploads.api_urls")),
    path("", include("apps.ipgeo.api_urls")),
    path("", include("apps.links.api_urls")),
    path("", include("apps.preview.api_urls")),
    path("", include("apps.shares.api_urls")),
    path("", include("apps.office.api_urls")),
    *router.urls,
]
