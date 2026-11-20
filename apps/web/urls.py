from django.urls import path

from apps.files.pages import BrowserPage

from . import views

app_name = "web"

urlpatterns = [
    path("", BrowserPage.as_view(), name="index"),  # the file browser (apps/files/pages.py)
    path("favicon.ico", views.favicon, name="favicon"),
    path("manage/", views.ManagePage.as_view(), name="manage"),
    path("components/", views.ComponentsPage.as_view(), name="components"),
]
