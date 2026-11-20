"""The shares' pages (apps/shares/urls.py):

    /share/<key>[?path=<folder>&pwd=<password>]   the share page: public (siteconf PublicPageMixin)
    /share/<key>/download?path=<file>              the file's download (the owner's download permission)
    /share/<key>/preview?path=<file>               the file inline, for <img>, <video>, <audio> (preview)
    /shares/                                       我的分享 (logged-in users, not the guest)

The share page is a shell like the file browser's: before the password it holds only what the form
needs (apps.shares.service.public_info); once the share's cookie says the password was given it mounts files'
browser in share mode (files.js, W4) on the share's API, with the viewers files/_assets.html loads, and
lets the storage's media origins into its CSP (apps.files.pages.with_media_sources). ?pwd= only fills in
the form, in the page's script, which then takes it out of the address (history.replaceState): the
password is never checked from a URL, and never put into one by the site.

/download and /preview check the share as its API does (apps/shares/access.py), count a download and
redirect (no-store) to the file's URL (apps.files.transfer.download_url, W1); a preview a script reads
(Sec-Fetch-Dest: empty, hls.js's segments) goes to this site's signed /pd/ link instead, which needs no
CORS of the storage. Opened without the share's cookie (a link copied out of a password share), they
send the visitor to the share's page instead.
"""

from __future__ import annotations

import json
import urllib.parse

from django.contrib.auth.views import redirect_to_login
from django.http import HttpResponseRedirect
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe
from django.views.generic import TemplateView

from apps.core import origin
from apps.files import transfer
from apps.files.pages import assets, with_media_sources
from apps.files.service import FileError, storage_data
from apps.siteconf.models import SiteSettings
from apps.siteconf.pages import PublicPageMixin
from apps.siteconf.serializers import PublicSiteSerializer
from apps.storages.access import DOWNLOAD, PREVIEW, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import InvalidPath
from apps.storages.models import StorageSource
from apps.web.errors import DEFAULT_MESSAGES, error_page

from . import access as shares
from . import service
from .views import is_member

PATH_MAX = 4096


def _error(request, status: int, message: str, code: str = "", **kwargs):
    """The error page; a status without a page of its own (409) is shown as 400."""
    return error_page(request, status if status in DEFAULT_MESSAGES else 400, message, code=code or None, **kwargs)


def _client_path(value) -> str:
    if not isinstance(value, str) or not value or len(value) > PATH_MAX:
        return "/"
    try:
        return paths.clean(value)
    except InvalidPath:
        return "/"


class SharePage(PublicPageMixin, TemplateView):
    """/share/<key>: the password form, or the browser in share mode."""

    template_name = "shares/share.html"

    def get(self, request, key):
        try:
            share = shares.find(key)
        except AccessDenied as error:
            return _error(request, 404, error.message, error.code, heading=_("Share link unavailable"))
        visit = shares.Visit(request, share)
        access = None
        if visit.verified:
            # The owner re-checked before anything of the share is shown: a share he can't give any more
            # is an error page, not a browser that fails.
            try:
                access = shares.open_share(request, share, visit)
            except AccessDenied as error:
                response = _error(request, error.status_code, error.message, error.code, heading=_("The share can't be opened for now"))
                visit.write(response)
                return response
        info = service.public_info(request, share, visit, access)
        site = SiteSettings.load()
        config = {
            "key": share.key,
            "api": "/api/v1/",
            "page": service.page_path(share),
            "info": info,
            "path": _client_path(request.GET.get("path")) if visit.verified else "/",
            "site": PublicSiteSerializer(site).data,
            "origin": origin.site_origin(request),
            "storage": storage_data(share.storage) if visit.verified else None,
        }
        context = self.get_context_data(
            share_config=config, share_config_json=json.dumps(config, ensure_ascii=False), info=info,
            page_title=_("File share"), files_assets=assets(), verified=visit.verified, expires_at=share.expires_at,
        )
        response = self.render_to_response(context)
        if visit.verified:
            try:
                sources = transfer.media_sources(share.storage)
            except Exception:  # noqa: BLE001 - the page works without them (a bucket's pictures don't show)
                sources = ()
            with_media_sources(response, sources)
        visit.write(response)
        add_never_cache_headers(response)  # what it shows depends on the share's cookie
        return response


def _serve(request, key, operation):
    try:
        share = shares.find(key)
        visit = shares.Visit(request, share)
        if not visit.verified:
            folder = paths.parent(_client_path(request.GET.get("path")))
            query = f"?{urllib.parse.urlencode({'path': folder})}" if folder != "/" else ""
            response = HttpResponseRedirect(service.page_path(share) + query)
            add_never_cache_headers(response)
            return response
        # A preview read by a script (fetch, XHR: hls.js's segments) gets this site's /pd/ link, which needs
        # no CORS of the storage; <img>, <video> and <audio> get the storage's own (W1), the bytes don't
        # pass through the site. Sec-Fetch-Dest says which (an old browser without it: the storage's).
        same_origin = operation == PREVIEW and request.headers.get("Sec-Fetch-Dest") == "empty"
        url = service.file_target(request, share, visit, request.GET.get("path", ""), operation, same_origin=same_origin)
    except AccessDenied as error:
        return _error(request, error.status_code, error.message, error.code)
    except FileError as error:
        return _error(request, error.status_code, error.message, error.code)
    response = HttpResponseRedirect(url)
    response["Cache-Control"] = "no-cache, no-store, must-revalidate, private"
    visit.write(response)
    return response


@require_safe
def download(request, key):
    return _serve(request, key, DOWNLOAD)


@require_safe
def preview(request, key):
    return _serve(request, key, PREVIEW)


class MySharesPage(TemplateView):
    """/shares/: 我的分享, for a logged-in user (the guest has no shares, 10.Z.5)."""

    template_name = "shares/my_shares.html"

    def dispatch(self, request, *args, **kwargs):
        if not is_member(request.user):
            return redirect_to_login(request.get_full_path())
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        storages = (StorageSource.objects.filter(shares__owner=self.request.user).distinct()
                    .order_by("position", "id").values("id", "name"))
        return super().get_context_data(**kwargs, share_storages=list(storages))
