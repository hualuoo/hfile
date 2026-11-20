"""The activity app's paths under /api/v1/ (apps/activity/views.py)."""

from django.urls import path

from . import views

urlpatterns = [
    path("manage/activity/", views.ActivityLogView.as_view(), name="manage-activity"),
]
