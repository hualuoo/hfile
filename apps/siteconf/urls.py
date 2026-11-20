"""/site/: the admin's custom CSS and JS (显示设置), linked from the public pages (apps/siteconf/pages.py)."""

from django.urls import path

from . import pages

app_name = "siteconf"

urlpatterns = [
    path("custom.css", pages.custom_css, name="custom-css"),
    path("custom.js", pages.custom_js, name="custom-js"),
]
