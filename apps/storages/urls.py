"""The storages app's page: /storages/oauth/callback/ (config/urls.py includes it under apps/storages/)."""

from django.urls import path

from . import views

app_name = "storages"

urlpatterns = [
    path("oauth/callback/", views.oauth_callback, name="oauth-callback"),
]
