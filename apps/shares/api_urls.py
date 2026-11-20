"""The shares API (apps/shares/views.py), under /api/v1/ (config/api_urls.py includes it)."""

from django.urls import path, re_path

from . import views

KEY = r"(?P<key>[A-Za-z0-9_-]{1,16})"

urlpatterns = [
    path("shares/", views.MySharesView.as_view(), name="shares"),
    path("shares/clear-expired/", views.ClearExpiredView.as_view(), name="shares-clear-expired"),
    re_path(rf"^shares/{KEY}/$", views.ShareView.as_view(), name="share-info"),
    re_path(rf"^shares/{KEY}/verify/$", views.VerifyView.as_view(), name="share-verify"),
    re_path(rf"^shares/{KEY}/list/$", views.ListingView.as_view(), name="share-list"),
    re_path(rf"^shares/{KEY}/item/$", views.ItemView.as_view(), name="share-item"),
    re_path(rf"^shares/{KEY}/unlock/$", views.UnlockView.as_view(), name="share-unlock"),
    re_path(rf"^shares/{KEY}/unlocks/$", views.UnlocksView.as_view(), name="share-unlocks"),
    path("manage/shares/", views.ManageSharesView.as_view(), name="manage-shares"),
    path("manage/shares/clear-expired/", views.ManageClearExpiredView.as_view(), name="manage-shares-clear-expired"),
    re_path(rf"^manage/shares/{KEY}/$", views.ManageShareView.as_view(), name="manage-share"),
]
