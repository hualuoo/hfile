"""The OnlyOffice API (apps/office/views.py), under /api/v1/ (config/api_urls.py includes it)."""

from django.urls import path

from . import views

urlpatterns = [
    path("office/config/", views.ConfigView.as_view(), name="office-config"),
]
