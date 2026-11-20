"""The pages of single sign-on, at /sso/ (config/urls.py): the addresses registered at providers."""

from django.urls import path

from . import views

app_name = "sso"

urlpatterns = [
    path("<str:code>/login", views.login_start, name="login"),
    path("<str:code>/login/callback", views.login_callback, name="callback"),
]
