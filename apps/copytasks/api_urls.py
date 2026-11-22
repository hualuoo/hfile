"""The copytasks app's paths under /api/v1/ (listed in apps/copytasks/views.py)."""

from django.urls import path

from . import views

urlpatterns = [
    path("manage/copy-tasks/", views.CopyTasksView.as_view(), name="manage-copy-tasks"),
    path("manage/copy-tasks/storages/", views.CopyStoragesView.as_view(), name="manage-copy-task-storages"),
    path("manage/copy-tasks/folders/", views.CopyFoldersView.as_view(), name="manage-copy-task-folders"),
    path("manage/copy-tasks/<int:pk>/", views.CopyTaskView.as_view(), name="manage-copy-task"),
    path("manage/copy-tasks/<int:pk>/failures/", views.CopyTaskFailuresView.as_view(), name="manage-copy-task-failures"),
    path("manage/copy-tasks/<int:pk>/log/", views.CopyTaskLogView.as_view(), name="manage-copy-task-log"),
    path("manage/copy-tasks/<int:pk>/stop/", views.CopyTaskStopView.as_view(), name="manage-copy-task-stop"),
    path("manage/copy-tasks/<int:pk>/resume/", views.CopyTaskResumeView.as_view(), name="manage-copy-task-resume"),
]
