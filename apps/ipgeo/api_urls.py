"""The ipgeo app's paths under /api/v1/ (listed in apps/ipgeo/views.py)."""

from django.urls import path

from . import views

urlpatterns = [
    path("manage/ip-geo/", views.IpGeoView.as_view(), name="manage-ip-geo"),
    path("manage/ip-geo/download/", views.IpGeoDownloadView.as_view(), name="manage-ip-geo-download"),
    path("manage/ip-geo/fill/", views.IpGeoFillView.as_view(), name="manage-ip-geo-fill"),
    path("manage/ip-geo/job/", views.IpGeoJobView.as_view(), name="manage-ip-geo-job"),
    path("manage/ip-geo/check/", views.IpGeoCheckView.as_view(), name="manage-ip-geo-check"),
]
