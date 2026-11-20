"""{% load sso %}: single sign-on in the page frame (apps/web/templates/web/base.html) and /manage/.

    {% sso_login_buttons %}               the enabled providers' buttons, under the login form
    {% sso_links_offered as name %}       whether the user menu offers 账号绑定 (once per request)
    {% sso_manage as sso %}               what /manage/#sso shows besides the API's data
"""

import json

from django import template
from django.db import DatabaseError

from apps.core import origin
from apps.siteconf.models import SiteSettings

from ..models import Identity, Provider
from ..presets import PRESETS

register = template.Library()


@register.inclusion_tag("sso/_login_buttons.html")
def sso_login_buttons():
    try:
        providers = list(Provider.objects.filter(enabled=True).only("code", "name", "icon"))
    except DatabaseError:  # no table yet (runserver before the upgrade)
        providers = []
    return {"providers": providers}


@register.simple_tag(takes_context=True)
def sso_links_offered(context):
    """账号绑定 is for logged-in users while a provider is enabled, or they have an account bound."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated or user.is_builtin:
        return False
    offered = getattr(request, "_sso_links_offered", None)
    if offered is None:
        try:
            offered = Provider.objects.filter(enabled=True).exists() or Identity.objects.filter(user=user).exists()
        except DatabaseError:
            offered = False
        request._sso_links_offered = offered
    return offered


@register.simple_tag(takes_context=True)
def sso_manage(context):
    request = context.get("request")
    return {
        "presets": PRESETS,
        # For the form's script, in an attribute: the CSP allows no inline <script>, not even of data.
        # (default=str: the presets' help texts are lazy, made in the page's language here)
        "presets_json": json.dumps(PRESETS, ensure_ascii=False, default=str),
        # The callbacks are this plus /sso/<code>/login/callback.
        "origin": origin.site_origin(request),
        "from_site_url": origin.from_site_url(),
        "auto_register": SiteSettings.load().sso_auto_register,
        # Changing providers is for superusers (apps/sso/views.py): the others see the list.
        "can_edit": bool(getattr(request, "user", None) and request.user.is_superuser),
    }
