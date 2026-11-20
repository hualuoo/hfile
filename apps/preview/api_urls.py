from django.urls import path

from . import views

urlpatterns = [
    path("preview/markdown/", views.MarkdownView.as_view(), name="preview-markdown"),
    path("preview/kkfileview/", views.KkFileViewView.as_view(), name="preview-kkfileview"),
]
