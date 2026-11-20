"""The OnlyOffice editor's page (inventory 7.3):

    GET /onlyoffice/edit/?storage=<key>&path=<path>      (in a share: ?share=<key>&path=<path>)

office/js/office.js opens it in a new tab. It asks for the config (POST /api/v1/office/config/), loads the
Document Server's api.js and opens the editor (office/js/editor.js). Its CSP is the site's with the
Document Server's origin added to script-src and frame-src, and nothing else (apps/office/csp.py); it isn't a
public page (no custom CSS or JS). Nothing here checks access: the config does, this page holds nothing.
"""

from __future__ import annotations

import json
import urllib.parse

from django.shortcuts import render
from django.urls import reverse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.views.decorators.http import require_safe

from apps.files.signing import quote_path
from apps.siteconf.models import SiteSettings
from apps.storages.drivers import paths
from apps.storages.drivers.base import InvalidPath

from . import csp
from .views import KEY_MAX, PATH_MAX


def _target(query) -> dict | None:
    """What the editor page opens: {"storage" | "share": key, "path": path}; None when the address is wrong."""
    storage, share, path = query.get("storage", ""), query.get("share", ""), query.get("path", "")
    if bool(storage) == bool(share) or not path or len(path) > PATH_MAX or len(storage or share) > KEY_MAX:
        return None
    try:
        paths.clean(path)
    except InvalidPath:
        return None
    return {"share": share, "path": path} if share else {"storage": storage, "path": path}


def _back(target: dict | None) -> str:
    """The folder the file is in, to go back to."""
    if target is None:
        return "/"
    if "share" in target:
        return f"/share/{urllib.parse.quote(target['share'], safe='')}"
    folder = paths.parent(paths.clean(target["path"]))
    key = urllib.parse.quote(target["storage"], safe="")
    return f"/{key}/" if folder == paths.ROOT else f"/{key}{quote_path(folder)}"


@require_safe
def editor_page(request):
    site = SiteSettings.load()
    target = _target(request.GET)
    server = csp.server_origin(site.onlyoffice_url)
    problem, status = "", 200
    if target is None:
        problem, status = _("Wrong address: it needs a storage source (or share) and the file's path."), 400
    elif not site.onlyoffice_url:
        problem, status = _("The site has no OnlyOffice server address set, so Office files can't be opened online."), 404
    elif not server:
        # A CSP can't name an IPv6 literal (an admin's setting, explained on the page).
        problem = _("The OnlyOffice server address can't be an IPv6 address (the page's security policy can't "
                    "include one). Please ask the administrator to use a domain name or an IPv4 address.")
    page = {
        "config_url": reverse("office-config"),
        "api_url": csp.api_url(site.onlyoffice_url) if not problem else "",
        "request": target if not problem else None,
        "error": problem,
        "back": _back(target),
    }
    title = paths.name(paths.clean(target["path"])) if target is not None else _("Online document")
    response = render(request, "office/editor.html", {
        "page_title": title or _("Online document"), "editor_page_json": json.dumps(page, ensure_ascii=False),
        "editor_error": problem, "editor_back": page["back"],
    }, status=status)
    # The site's policy with the Document Server's origin in script-src and frame-src (SiteCSPMiddleware
    # leaves a response that has its own alone).
    response._csp_config = csp.editor_policy(site, server if not problem else "")
    add_never_cache_headers(response)
    return response
