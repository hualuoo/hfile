"""The storages API, under /api/v1/ (config/api_urls.py includes it); see apps/storages/views.py."""

from django.urls import path

from . import views

urlpatterns = [
    path("manage/storage-types/", views.StorageTypesView.as_view(), name="manage-storage-types"),
    path("manage/storages/", views.StorageListView.as_view(), name="manage-storages"),
    path("manage/storages/sort/", views.StorageSortView.as_view(), name="manage-storages-sort"),
    path("manage/storages/key-check/", views.KeyCheckView.as_view(), name="manage-storages-key-check"),
    path("manage/storages/test/", views.FormTestView.as_view(), name="manage-storages-test"),
    path("manage/storages/helpers/", views.HelperView.as_view(), name="manage-storages-helpers"),
    path("manage/storages/oauth/", views.OAuthStartView.as_view(), name="manage-storages-oauth"),
    path("manage/storages/drafts/<str:draft_id>/", views.DraftView.as_view(), name="manage-storages-draft"),
    path("manage/storages/<int:pk>/", views.StorageDetailView.as_view(), name="manage-storage"),
    path("manage/storages/<int:pk>/enable/", views.StorageEnableView.as_view(), name="manage-storage-enable"),
    path("manage/storages/<int:pk>/disable/", views.StorageDisableView.as_view(), name="manage-storage-disable"),
    path("manage/storages/<int:pk>/check/", views.StorageCheckView.as_view(), name="manage-storage-check"),
    path("manage/storages/<int:pk>/copy/", views.StorageCopyView.as_view(), name="manage-storage-copy"),
    path("manage/storages/<int:pk>/rotate-link-secret/", views.StorageRotateView.as_view(), name="manage-storage-rotate"),
    path("manage/storages/<int:pk>/check-path/", views.CheckPathView.as_view(), name="manage-storage-check-path"),
    path("manage/storages/<int:pk>/filters/", views.FiltersView.as_view(), name="manage-storage-filters"),
    path("manage/storages/<int:pk>/passwords/", views.PasswordsView.as_view(), name="manage-storage-passwords"),
    path("manage/storages/<int:pk>/readmes/", views.ReadmesView.as_view(), name="manage-storage-readmes"),
    path("manage/storages/<int:pk>/rules/test/", views.RuleTestView.as_view(), name="manage-storage-rule-test"),
    path("manage/users/<int:user_id>/grants/", views.UserGrantsView.as_view(), name="manage-user-grants"),
    path("manage/grant-options/", views.GrantOptionsView.as_view(), name="manage-grant-options"),
    path("storages/", views.PublicStoragesView.as_view(), name="storages"),
    path("storages/unlocks/", views.UnlocksView.as_view(), name="storages-unlocks"),
    path("storages/<str:key>/unlock/", views.UnlockView.as_view(), name="storage-unlock"),
]
