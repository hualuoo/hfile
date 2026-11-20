"""A page to try the upload panel on (DEBUG only), until the file browser mounts it: it gives the panel
a ctx (contract W4's) for a storage and a folder the visitor picks, and shows the hfile:files-changed
events the panel sends. The file browser does the same with its own ctx."""

from django.conf import settings
from django.http import Http404
from django.views.generic import TemplateView

from apps.storages import access as policy

from .csp import allow_direct_uploads


class TryPage(TemplateView):
    template_name = "uploads/try.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get(self, request, *args, **kwargs):
        response = super().get(request, *args, **kwargs)
        storages = [storage for storage, _grant in policy.visible_storages(policy.actor_for(request))]
        allow_direct_uploads(response, storages)
        return response
