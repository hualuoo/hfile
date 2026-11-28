from django.urls import reverse
from django.utils.functional import SimpleLazyObject

from .navigation import NAV


def _storages(request) -> list[dict]:
    """The storages the admin put in the rail (show_in_nav) that this caller may use, in order, and which of
    them is the page's destination: what the file browser handed over (navigation.in_file_browser()), else
    asked for (the rail's only)."""
    from apps.files.readme import browse_url
    from apps.siteconf.pages import browser_for_visitors
    from apps.storages import access as policy

    if not request.user.is_authenticated and not browser_for_visitors():
        return []  # every page of the file browser gives visitors the 匿名用户首页: no links that lead there
    handed = getattr(request, "nav_file_browser", None)
    if handed is None:
        storages, current = [storage for storage, _grant in policy.visible_storages(policy.actor_for(request), in_nav=True)], None
    else:
        storages, current = [storage for storage in handed[0] if storage.show_in_nav], handed[1]
    return [{"storage": storage, "href": browse_url(storage, "/"), "icon": storage.shown_icon,
             "current": current is not None and storage.pk == current.pk} for storage in storages]


def _nav(request):
    user = getattr(request, "user", None)
    if user is None:
        return {"destinations": [], "storages": []}
    match = getattr(request, "resolver_match", None)
    current = match.url_name if match else None
    storages = _storages(request)
    # In a storage of the rail, that storage is the current destination, not the file browser's 首页; phones,
    # whose bar leaves the storages out, still show 首页 as the current one (phone_current).
    in_rail = any(entry["current"] for entry in storages)
    destinations = []
    for item in NAV:
        if not item.visible(user):
            continue
        here = current in (item.key, *item.also_on)
        destinations.append({"item": item, "href": reverse(item.url), "current": here and not in_rail,
                             "phone_current": here and in_rail})
    return {"destinations": destinations, "storages": storages}


def shell(request):
    """{{ nav.destinations }} and {{ nav.storages }} in base.html: the destinations this user sees and the
    storages of the rail, which one is current; worked out when a page shows them."""
    return {"nav": SimpleLazyObject(lambda: _nav(request))}
