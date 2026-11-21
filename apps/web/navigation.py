"""Two small registries: the shell's destinations (the navigation rail, a bar at the bottom on phones;
base.html) and the sections of /manage/ (apps/web/templates/web/manage.html).

A new destination is one NavItem; a new section of /manage/ is one Section, plus its template and
its script. Icons are Material Symbols names that must be in apps/web/static/web/icons.svg
(scripts/icons.py); a destination's selected icon is its "-fill" one.
"""

from dataclasses import dataclass, field
from typing import Callable

from django.conf import settings
from django.utils.translation import gettext_lazy, pgettext_lazy

from apps.core.permissions import is_admin


def everybody(user):
    return True


def member(user):
    """Logged in with an account of one's own (the guest and the template never log in)."""
    return bool(user and user.is_authenticated and getattr(user, "kind", "") == "normal")


@dataclass(frozen=True)
class NavItem:
    key: str  # the url name (request.resolver_match.url_name) it is the current destination on
    label: str
    icon: str
    url: str  # the url name to reverse: "web:index"
    visible: Callable = everybody  # (user) -> bool
    also_on: tuple = field(default=())  # more url names it is the current one on


NAV = [
    NavItem("index", gettext_lazy("Home"), "home", "web:index", also_on=("browse",)),  # the file browser's folders too
    NavItem("my-shares", gettext_lazy("My shares"), "share", "shares:my-shares", visible=member),  # apps/shares/pages.py
    NavItem("manage", gettext_lazy("Manage"), "admin_panel_settings", "web:manage", visible=is_admin),
    # A page of every component in both themes, for whoever builds pages (DEBUG only).
    NavItem("components", gettext_lazy("Components"), "palette", "web:components", visible=lambda user: settings.DEBUG),
]


@dataclass(frozen=True)
class Section:
    key: str  # its address on /manage/: #version
    label: str
    icon: str
    template: str  # included in manage.html
    script: str = ""  # its JS (a static path), loaded after web/js/manage.js; sections may share one
    group: str = ""  # the heading it is listed under in the menu


_SETTINGS_JS = "siteconf/js/manage_settings.js"  # one script for the six pages of settings (siteconf)
# 用户 and 存储源 also head columns ("User"); in the menu (and as the sections' headings) they are plural
# ("Users"), so they have a context there.
_USERS = pgettext_lazy("manage menu", "Users")

SECTIONS = [
    Section("storages", pgettext_lazy("manage menu", "Storage sources"), "storage", "storages/manage/storages.html",
            "storages/js/manage_storages.js", gettext_lazy("Storage")),
    Section("copy-tasks", gettext_lazy("Copy tasks"), "file_copy", "copytasks/manage/copy_tasks.html",
            "copytasks/js/manage_copy_tasks.js", gettext_lazy("Storage")),
    Section("users", _USERS, "group", "accounts/manage/users.html", "accounts/js/manage_users.js", _USERS),
    Section("activity", gettext_lazy("Activity log"), "history", "activity/manage/activity_log.html",
            "activity/js/manage_activity_log.js", _USERS),
    Section("sso", gettext_lazy("Single sign-on"), "key", "sso/manage/providers.html", "sso/js/manage_sso.js", _USERS),
    Section("site", gettext_lazy("Site settings"), "language", "siteconf/manage/site.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("view", gettext_lazy("Display settings"), "tune", "siteconf/manage/view.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("links", gettext_lazy("Direct and short links"), "link", "siteconf/manage/links.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("proxy", gettext_lazy("Proxy download"), "download", "siteconf/manage/proxy.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("security", gettext_lazy("Security settings"), "shield", "siteconf/manage/security.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("access", gettext_lazy("Access control"), "block", "siteconf/manage/access.html", _SETTINGS_JS, pgettext_lazy("manage menu", "Site")),
    Section("short-links", gettext_lazy("Short links"), "add_link", "links/manage/short_links.html", "links/js/manage_short_links.js",
            gettext_lazy("Links")),
    Section("download-logs", gettext_lazy("Download log"), "receipt_long", "links/manage/download_logs.html",
            "links/js/manage_download_logs.js", gettext_lazy("Links")),
    Section("shares", gettext_lazy("Shares"), "share", "shares/manage/shares.html", "shares/js/share_list.js", gettext_lazy("Links")),
    Section("diagnostics", gettext_lazy("Diagnostics"), "troubleshoot", "siteconf/manage/diagnostics.html",
            "siteconf/js/manage_diagnostics.js", pgettext_lazy("manage menu", "System")),
    Section("ip-geo", gettext_lazy("IP location"), "location_on", "ipgeo/manage/ip_geo.html", "ipgeo/js/manage_ip_geo.js",
            pgettext_lazy("manage menu", "System")),
    Section("version", gettext_lazy("Version and upgrades"), "upgrade", "web/manage/version.html", "web/js/manage_version.js", pgettext_lazy("manage menu", "System")),
]
