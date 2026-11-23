from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from .navigation import NAV


def _storages(request, browsing: str) -> list[dict]:
    """The storages the admin put in the rail (show_in_nav) that this caller may use, in order; `browsing`,
    the key of the storage the page is in ("" when none)."""
    from apps.storages import access as policy

    return [{"storage": storage, "href": f"/{storage.key}/", "icon": storage.shown_icon, "current": storage.key == browsing}
            for storage, _grant in policy.visible_storages(policy.actor_for(request)) if storage.show_in_nav]


def _nav(request):
    user = getattr(request, "user", None)
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else None
    browsing = (match.kwargs.get("key") or "").lower() if current == "browse" else ""
    storages = _storages(request, browsing) if user is not None else []
    # In a storage of the rail, that storage is the current destination, not the file browser's 首页; phones,
    # whose bar leaves the storages out, still show 首页 as the current one (phone_current).
    in_rail = any(entry["current"] for entry in storages)
    items = []
    for item in NAV:
        if user is None or not item.visible(user):
            continue
        here = current in (item.key, *item.also_on)
        items.append({
            "item": item,
            "href": reverse(item.url),
            "current": here and not (in_rail and current == "browse"),
            "phone_current": here and in_rail and current == "browse",
        })
    return {"items": items, "storages": storages}


def shell(request):
    """{% for entry in nav_items %} and {% for entry in nav_storages %} in base.html: the destinations this
    user sees and the storages of the rail, which one is current."""
    nav = SimpleLazyObject(lambda: _nav(request))
    return {"nav_items": SimpleLazyObject(lambda: nav["items"]), "nav_storages": SimpleLazyObject(lambda: nav["storages"])}
