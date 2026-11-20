"""/components/preview/...: the DEBUG-only self-test of the viewers (apps/preview/dev.py)."""

from django.urls import path

from . import dev

app_name = "preview-dev"

urlpatterns = [
    path("selftest/", dev.SelfTestPage.as_view(), name="selftest"),
]
