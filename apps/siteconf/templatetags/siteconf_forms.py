"""{% settings_fields "view" as f %}: the fields of a page of settings (apps/siteconf/serializers.py
form_fields), for its template in /manage/: labels, help, choices and limits come from the checks."""

from django import template

from apps.core import images

from .. import blocklist
from ..serializers import form_fields

register = template.Library()


@register.simple_tag
def settings_fields(group):
    return form_fields(group)


@register.simple_tag
def image_accept(name):
    """<input type="file" accept="..."> for the logo or the icon."""
    return images.FAVICON.accept if name == "favicon" else images.LOGO.accept


@register.simple_tag
def thunder_rules():
    """The UA rules 屏蔽迅雷下载 adds (apps/siteconf/blocklist.py), one per line."""
    return "\n".join(blocklist.THUNDER_RULES)
