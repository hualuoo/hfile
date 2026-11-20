"""/components/uploads/: a page to try the upload panel on, with DEBUG only (config/urls.py)."""

from django.urls import path

from . import pages

app_name = "uploads"

urlpatterns = [
    path("", pages.TryPage.as_view(), name="try"),
]
