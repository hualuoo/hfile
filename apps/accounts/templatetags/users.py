"""{% load users %}: what the user pages need from accounts."""

from django import template

from apps.accounts.permissions import REQUIRES, Permission, help_for

register = template.Library()


@register.simple_tag
def permission_choices():
    """The permission codes with their labels, help and what each needs first (apps/accounts/permissions.py),
    in their order: [(code, label, help, requires)], requires as "code code" (one of them, D55)."""
    return [(code, label, help_for(code), " ".join(REQUIRES.get(code, ()))) for code, label in Permission.choices]
