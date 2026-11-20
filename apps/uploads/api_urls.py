"""The upload API, under /api/v1/ (config/api_urls.py includes it); see apps/uploads/views.py."""

from django.urls import path

from . import views

urlpatterns = [
    path("uploads/", views.UploadsView.as_view(), name="uploads"),
    path("uploads/folders/", views.FoldersView.as_view(), name="uploads-folders"),
    path("uploads/text/", views.TextView.as_view(), name="uploads-text"),
    path("uploads/<uuid:pk>/", views.SessionView.as_view(), name="upload"),
    path("uploads/<uuid:pk>/complete/", views.CompleteView.as_view(), name="upload-complete"),
]
