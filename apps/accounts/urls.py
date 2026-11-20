"""The login page (config/urls.py mounts it at /login/): /login/ while no secure login entry is set,
else only /login/<entry>/ (apps/accounts/entry.py)."""

from django.urls import path

from .views import LoginPage

app_name = "accounts"

urlpatterns = [
    path("", LoginPage.as_view(), name="login"),
    path("<str:entry>/", LoginPage.as_view(), name="login-entry"),
]
