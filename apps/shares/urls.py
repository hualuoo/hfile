"""The shares' pages (apps/shares/pages.py), included by config/urls.py before the file browser's catch-all:
/share/<key> (with or without the slash), its /download and /preview, and /shares/ (我的分享)."""

from django.urls import path, re_path

from . import pages

app_name = "shares"

KEY = r"(?P<key>[A-Za-z0-9_-]{1,16})"

urlpatterns = [
    re_path(rf"^share/{KEY}/?$", pages.SharePage.as_view(), name="share"),
    re_path(rf"^share/{KEY}/download$", pages.download, name="download"),
    re_path(rf"^share/{KEY}/preview$", pages.preview, name="preview"),
    path("shares/", pages.MySharesPage.as_view(), name="my-shares"),
]
