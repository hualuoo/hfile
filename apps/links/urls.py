"""The short links' page, /s/<key> (config/urls.py includes it under s/). Path links are answered by the
files app's catch-all, which asks apps.links.delivery.route() first (apps/links/apps.py)."""

from django.urls import re_path

from . import views

urlpatterns = [
    re_path(r"^(?P<key>[^/]+)/?$", views.short_link, name="short-link"),
]
