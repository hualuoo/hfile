"""The files API (apps/files/views.py), under /api/v1/ (config/api_urls.py includes it)."""

from django.urls import path

from . import views

urlpatterns = [
    path("files/<str:key>/list/", views.ListView.as_view(), name="files-list"),
    path("files/<str:key>/item/", views.ItemView.as_view(), name="files-item"),
    path("files/<str:key>/mkdir/", views.MkdirView.as_view(), name="files-mkdir"),
    path("files/<str:key>/rename/", views.RenameView.as_view(), name="files-rename"),
    path("files/<str:key>/move/", views.MoveView.as_view(), name="files-move"),
    path("files/<str:key>/copy/", views.CopyView.as_view(), name="files-copy"),
    path("files/<str:key>/delete/", views.DeleteView.as_view(), name="files-delete"),
]
