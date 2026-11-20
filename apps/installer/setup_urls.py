"""The whole site in setup mode (settings.SETUP_MODE: no secret_key in config.toml yet): the installer.
Its CSS and JS come from WhiteNoise (apps/core/staticfiles.py), before these patterns."""

from django.urls import path, re_path

from . import views

urlpatterns = [
    path("install/", views.InstallView.as_view(), name="install"),
    path("install/check/", views.CheckView.as_view(), name="install-check"),
    re_path(r"", views.not_installed),
]
