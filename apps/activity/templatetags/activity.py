"""{% activity_categories as categories %}: the groups of kinds of the 操作日志's filter (apps/activity/log.py)."""

from django import template

from apps.activity import log

register = template.Library()


@register.simple_tag
def activity_categories():
    return log.CATEGORIES
