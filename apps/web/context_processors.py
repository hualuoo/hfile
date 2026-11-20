from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from .navigation import NAV


def _nav(request):
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else None
    items = []
    for item in NAV:
        if user is None or not item.visible(user):
            continue
        items.append({
            "item": item,
            "href": reverse(item.url),
            "current": current in (item.key, *item.also_on),
        })
    return items


def shell(request):
    """{% for entry in nav_items %} in base.html: the destinations this user sees, which one is current."""
    return {"nav_items": SimpleLazyObject(lambda: _nav(request))}
