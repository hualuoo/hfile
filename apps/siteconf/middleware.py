"""What the site's settings change about every request (config/settings.py MIDDLEWARE).

- AccessControlMiddleware: 访问控制, the IP and User-Agent blocklists (apps/siteconf/blocklist.py). Early,
  before sessions (after the size limit): a blocked request costs one cached read of the settings.
- SiteCSPMiddleware: the Content-Security-Policy of HTML pages, as the settings need it (policy());
  after Django's CSP middleware, which then writes the header.

Neither runs before the site is installed: there is no database yet.
"""

from django.conf import settings
from django.db import DatabaseError
from django.utils.csp import CSP, build_policy
from django.utils.translation import gettext_lazy

from apps.core import net

from . import blocklist
from .models import SiteSettings

BLOCKED = gettext_lazy("Your IP or browser can't open this site (the site's access control).")


def _site():
    try:
        return SiteSettings.load()
    except DatabaseError:  # no table yet (runserver before upgrading): the view will say so
        return None


def _skipped(request):
    """Before the site is installed there is no database; the program's static files (only what
    WhiteNoise doesn't have comes this far: a 404) are nobody's business."""
    return settings.SETUP_MODE or request.path.startswith(settings.STATIC_URL)


class AccessControlMiddleware:
    """403 for a request whose client IP (apps.core.net, [server] proxies) or User-Agent is on a blocklist:
    the error page, JSON for the API. It doesn't say which rule, nor echo the IP or the UA.

    Every address of the site is checked, uploads and the Django admin included, except the static
    files of the program itself: WhiteNoise answers them earlier, and the error page needs them.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not _skipped(request):
            site = _site()
            if site is not None and (site.ip_blocklist or site.ua_blocklist):
                rules = blocklist.compiled(site.ip_blocklist, site.ua_blocklist)
                if rules.blocks(net.client_ip(request), blocklist.user_agent(request.META)):
                    from apps.web.errors import error_page

                    # No 回到首页: the same rule refuses the home page.
                    response = error_page(request, 403, BLOCKED, code="blocked", home=False)
                    # Django's CSP middleware comes later in the list, so it won't see this answer.
                    response[CSP.HEADER_ENFORCE] = build_policy(settings.SECURE_CSP)
                    return response
        return self.get_response(request)


# What apps add to the policy of the file browser and the share pages for what they put there (the
# preview app: kkFileView's origin in frame-src). hook(config, site) changes the policy in place; apps
# register it from their AppConfig.ready() with public_policy(hook).
_public_hooks = []


def public_policy(hook):
    """Registers hook(config, site), run on the CSP of every public page (public == "page" or "login")."""
    if hook not in _public_hooks:
        _public_hooks.append(hook)
    return hook


def policy(site, public=None):
    """The CSP of a page (settings.SECURE_CSP is the strict base):

    - the logo and the icon may be linked from elsewhere: their origins go into img-src;
    - public pages (public == "page": the file browser, shares) while custom JS is set, and the
      custom guest page (public == "custom") get what typical statistics snippets need, decision
      D33: scripts, connections and images from https:. Inline scripts still don't run;
    - the file browser and the shares ("page", or "login" with ?auth=) also get what the apps'
      public_policy() hooks add for their viewers.
    """
    config = {name: list(values) for name, values in settings.SECURE_CSP.items()}
    images = config.setdefault("img-src", [CSP.SELF])
    for origin in site.image_origins:
        if origin not in images:
            images.append(origin)
    if public == "custom" or (public == "page" and site.custom_js.strip()):
        config["script-src"] = [CSP.SELF, "https:"]
        config["connect-src"] = [CSP.SELF, "https:"]
        for source in ("https:", "data:"):
            if source not in images:
                images.append(source)
    if public in ("page", "login"):
        for hook in _public_hooks:
            hook(config, site)
    return config


def with_sources(config, sources):
    """`config` with more sources per directive ({"connect-src": ["https:"]}). A directive the policy
    doesn't name yet starts from default-src, so what the page could do already stays allowed."""
    for directive, more in (sources or {}).items():
        values = config.setdefault(directive, list(config.get("default-src", [CSP.SELF])))
        for source in more:
            if source not in values:
                values.append(source)
    return config


class SiteCSPMiddleware:
    """Gives HTML pages the policy() of the settings; a view that set its own (Django's csp_override)
    keeps it. A public page says what it is in response.public_page (apps/siteconf/pages.py). A page that
    needs more sources than the policy has (the upload panel sending files straight to a bucket) puts
    them in response.csp_sources ({directive: [sources]}): they are added."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if _skipped(request) or hasattr(response, "_csp_config"):
            return response
        if not response.get("Content-Type", "").startswith("text/html"):
            return response
        site = _site()
        if site is not None:
            config = policy(site, getattr(response, "public_page", None))
            response._csp_config = with_sources(config, getattr(response, "csp_sources", None))
        return response
