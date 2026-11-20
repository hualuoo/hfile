"""The links API (apps/links/views.py, apps/links/manage_views.py), under /api/v1/ (config/api_urls.py includes it)."""

from django.urls import path

from . import manage_views, views

urlpatterns = [
    path("links/<str:key>/path/", views.PathLinksView.as_view(), name="links-path"),
    path("links/<str:key>/short/", views.ShortLinksView.as_view(), name="links-short"),
    path("manage/short-links/", manage_views.ShortLinkListView.as_view(), name="manage-short-links"),
    path("manage/short-links/<int:pk>/", manage_views.ShortLinkView.as_view(), name="manage-short-link"),
    path("manage/short-links/delete/", manage_views.ShortLinkDeleteView.as_view(), name="manage-short-links-delete"),
    path("manage/short-links/delete-expired/", manage_views.ShortLinkExpiredView.as_view(),
         name="manage-short-links-expired"),
    path("manage/short-links/export/", manage_views.ShortLinkExportView.as_view(), name="manage-short-links-export"),
    path("manage/link-limits/", manage_views.LinkLimitsView.as_view(), name="manage-link-limits"),
    path("manage/download-logs/", manage_views.DownloadLogListView.as_view(), name="manage-download-logs"),
    path("manage/download-logs/delete/", manage_views.DownloadLogDeleteView.as_view(),
         name="manage-download-logs-delete"),
]
