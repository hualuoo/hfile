"""OnlyOffice's pages under /onlyoffice/ (config/urls.py; a first segment config/admin_path.py keeps)."""

from django.urls import path

from . import pages, views

app_name = "office"

urlpatterns = [
    # Registered at the Document Server through each config's editorConfig.callbackUrl.
    path("callback/", views.callback, name="callback"),
    path("edit/", pages.editor_page, name="editor"),
]
