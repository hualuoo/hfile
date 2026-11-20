"""{% icon %}: an icon of the sprite web/icons.svg (Material Symbols Rounded). {% language_menu %} and
{% js_catalog %}: the page's language (settings.LANGUAGES)."""

import re

from django import template
from django.conf import settings
from django.templatetags.static import static
from django.utils import translation
from django.utils.html import format_html

register = template.Library()

ICON_NAME = re.compile(r"[a-z0-9_]+(?:-fill)?")
_SPRITE = "web/icons.svg"


@register.simple_tag(takes_context=True)
def icon(context, name, extra_class=""):
    """An SVG that shows a symbol of the sprite. The sprite is a file of its own, so browsers cache it
    once for every page; App.icon() in common.js makes the same markup.

    The sprite's address (with its fingerprint, apps/core/staticfiles.py, which looks the file up and stats
    it) is worked out once per page rather than for each of its icons."""
    if not ICON_NAME.fullmatch(name):
        raise template.TemplateSyntaxError(f"bad icon name: {name!r}")
    sprite = context.render_context.get(_SPRITE)
    if sprite is None:
        sprite = context.render_context[_SPRITE] = static(_SPRITE)
    css = f"icon {extra_class}".strip()
    return format_html('<svg class="{}" aria-hidden="true"><use href="{}#i-{}"></use></svg>', css, sprite, name)


def _language() -> str:
    """The page's language, one of settings.LANGUAGES."""
    current = translation.get_language()
    return current if current in dict(settings.LANGUAGES) else settings.LANGUAGE_CODE


@register.inclusion_tag("web/_language_menu.html")
def language_menu(inline=False):
    """The choice of language: a menu of the top app bar, or (inline) a row of buttons, for the installer,
    whose page has no menus. web/js/language.js keeps it in the cookie LocaleMiddleware reads."""
    return {
        "languages": settings.LANGUAGES,
        "current": _language(),
        "cookie": settings.LANGUAGE_COOKIE_NAME,
        "max_age": settings.LANGUAGE_COOKIE_AGE,
        "inline": inline,
    }


@register.simple_tag
def js_catalog():
    """The scripts' translations in the page's language (web/i18n/<code>.js, written by scripts/i18n.py):
    gettext(), ngettext(), interpolate() and the rest of Django's JavaScript catalog. Before the page's
    other scripts."""
    return format_html('<script src="{}"></script>', static(f"web/i18n/{_language()}.js"))
