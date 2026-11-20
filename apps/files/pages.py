"""The file browser's pages: / (the storages, or the first one) and /<storage key>/<path> (D13).

Both are public pages in siteconf's sense (PublicPageMixin: the announcement, the footer, the admin's
custom CSS and JS); visitors may get the 匿名用户首页 instead (guest_landing()). The page is a shell:
the browser (apps/files/static/files/js/files.js, contract W4) loads the folder from the API. The config it
starts from is in the page as JSON, in the mount element's data-config (no inline script): the storages,
the site's public settings, where it is.

Other apps add their scripts and style sheets to the browser's pages (the viewers, links, office):

    from apps.files import pages
    pages.add_assets(scripts=["preview/js/viewers.js"], styles=["preview/css/viewers.css"])

in their AppConfig.ready(); files/_assets.html includes them, after files.js (share pages include it
too). The pages' CSP lets in the origins the storage's files are served from (apps.files.transfer
media_sources()) for images and media, so a gallery of a bucket's presigned links shows.

An app with addresses under a first segment chosen at run time (the links app's path links,
/<prefix>/<storage key>/<path>) adds a route with add_route(): the catch-all asks it first.
"""

from __future__ import annotations

import json

from django.conf import settings
from django.http import Http404, HttpResponsePermanentRedirect, HttpResponseRedirect
from django.urls import Resolver404, resolve
from django.utils.translation import gettext as _
from django.views.generic import TemplateView

from apps.core import origin, reserved
from apps.siteconf.models import SiteSettings
from apps.siteconf.pages import PublicPageMixin, guest_landing
from apps.siteconf.serializers import PublicSiteSerializer
from apps.storages import access as policy
from apps.storages.access import AccessDenied
from apps.storages.drivers import paths
from apps.uploads.csp import allow_direct_uploads
from apps.web.errors import error_page

from . import readme, transfer
from .service import storage_data

_scripts: list[str] = []
_styles: list[str] = []
# Addresses of other apps under a first segment chosen at run time, asked before the browser's: the
# links app's path links, /<prefix>/<storage key>/<path> (D13). route(request, first, rest) answers, or
# gives None when the first segment isn't its own.
_routes: list = []


def add_route(route) -> None:
    """Lets `route` answer /<first>/<rest> before the browser does (an app's AppConfig.ready())."""
    if route not in _routes:
        _routes.append(route)


def add_assets(*, scripts=(), styles=()) -> None:
    """Static paths of scripts and style sheets every browser page loads (after files.js)."""
    for path in scripts:
        if path not in _scripts:
            _scripts.append(path)
    for path in styles:
        if path not in _styles:
            _styles.append(path)


def assets() -> dict[str, list[str]]:
    return {"scripts": list(_scripts), "styles": list(_styles)}


def with_media_sources(response, sources) -> None:
    """Adds `sources` to the page's img-src and media-src (blob: with them, for the video player's
    streams): response.csp_sources, which apps.siteconf.middleware.SiteCSPMiddleware adds to the site's
    policy (as apps.uploads.csp.allow_direct_uploads() does for connect-src)."""
    if not sources:
        return
    extra = dict(getattr(response, "csp_sources", None) or {})
    for directive, base in (("img-src", ()), ("media-src", ("blob:",))):
        values = list(extra.get(directive, []))
        for source in (*base, *sources):
            if source not in values:
                values.append(source)
        extra[directive] = values
    response.csp_sources = extra


def page_config(request, *, storage=None, path="/", storages=(), mode="browse") -> dict:
    """What files.js starts from (files_config in the page)."""
    site = SiteSettings.load()
    user = request.user
    return {
        "mode": mode,
        "api": "/api/v1/",
        "storage": storage_data(storage) if storage is not None else None,
        "path": path,
        "storages": [storage_data(item) for item in storages],
        "site": PublicSiteSerializer(site).data,
        "origin": origin.site_origin(request),
        "user": {
            "authenticated": bool(user.is_authenticated),
            "admin": bool(user.is_authenticated and user.is_staff),
            "name": user.display_name if user.is_authenticated else "",
        },
        "login": site.login_visible and not user.is_authenticated,
    }


class BrowserPage(PublicPageMixin, TemplateView):
    """/ and /<storage key>/<path>."""

    template_name = "files/browser.html"

    def dispatch(self, request, *args, key=None, path=None, **kwargs):
        if key is not None:
            for route in _routes:
                answer = route(request, key, path)
                if answer is not None:
                    return answer
            # What APPEND_SLASH did before this address took every other one: /manage -> /manage/,
            # /login/<entry> -> /login/<entry>/. The site's own first segments are never storage keys
            # (apps.core.reserved): /api/..., /static/... that nothing else took are a plain 404, without
            # asking the database.
            redirect = _slash_redirect(request)
            if redirect is not None:
                return redirect
            if key.lower() in reserved.static_names() or key.lower() == settings.ADMIN_PATH.lower():
                raise Http404
        return super().dispatch(request, *args, key=key, path=path, **kwargs)

    def get(self, request, key=None, path=None, **kwargs):
        landing = guest_landing(request)
        if landing is not None:
            return landing
        actor = policy.actor_for(request)
        storages = [storage for storage, _grant in policy.visible_storages(actor)]
        site = SiteSettings.load()
        if key is None:
            if storages and not site.root_show_storage:
                return HttpResponseRedirect(readme.browse_url(storages[0], "/"))
            return self.page(request, storage=None, path="/", storages=storages, title=_("Home"))
        try:
            access = policy.open_storage(request, key)
            storage_path = access.path(path)
        except AccessDenied as error:
            # /no/such/page reads its first part as a storage: say what a 404 says, not 「存储源不存在」.
            message = "" if error.code == policy.STORAGE_NOT_FOUND else error.message
            return error_page(request, error.status_code, message, code=error.code)
        client = access.relative(storage_path)
        title = paths.name(client) or access.storage.name
        response = self.page(request, storage=access.storage, path=client, storages=storages, title=title)
        try:
            sources = transfer.media_sources(access.storage)
        except Exception:  # noqa: BLE001 - the page works without them (pictures of the backend don't show)
            sources = ()
        with_media_sources(response, sources)
        # The upload panel may send files straight to the backend (contract U): connect-src.
        allow_direct_uploads(response, [access.storage])
        return response

    def page(self, request, *, storage, path, storages, title):
        config = page_config(request, storage=storage, path=path, storages=storages)
        context = self.get_context_data(files_config=config, files_config_json=json.dumps(config, ensure_ascii=False),
                                        page_title=title, storage=storage, files_assets=assets())
        return self.render_to_response(context)


def _slash_redirect(request):
    """CommonMiddleware's APPEND_SLASH, which this catch-all address takes away: a GET of an address
    that resolves elsewhere with a "/" at the end goes there (301)."""
    if request.method not in ("GET", "HEAD") or request.path.endswith("/"):
        return None
    try:
        match = resolve(request.path + "/")
    except Resolver404:
        return None
    if match.url_name == "browse":
        return None
    query = request.META.get("QUERY_STRING", "")
    return HttpResponsePermanentRedirect(request.path + "/" + (f"?{query}" if query else ""))

