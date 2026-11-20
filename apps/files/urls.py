"""The files app's addresses outside the API: signed downloads (/pd/) and the browser's catch-all
/<storage key>/<path>, which config/urls.py includes last (after every other address; apps.core.reserved
keeps storage keys apart from them). The home page "/" is apps/web/urls.py's (apps.files.pages.BrowserPage)."""

from django.urls import re_path

from . import download, pages

# [\s\S], not ".": names may hold a line break (local disks and buckets allow it). (An inline (?s) flag
# would make Django's URL reversing fail.)
urlpatterns = [
    re_path(r"^pd/(?P<key>[^/]+)(?P<path>/[\s\S]*)$", download.serve, name="pd"),
    re_path(r"^(?P<key>[^/]+)(?:/(?P<path>[\s\S]*))?$", pages.BrowserPage.as_view(), name="browse"),
]
