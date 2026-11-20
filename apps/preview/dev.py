"""/components/preview/selftest/: the viewers' own tests in the browser (selftest.js), with DEBUG on only
(404 otherwise): the parts only a browser can run (TextDecoder, DOMParser, URL)."""

from django.conf import settings
from django.http import Http404
from django.views.generic import TemplateView

from .apps import SCRIPTS


class SelfTestPage(TemplateView):
    template_name = "preview/selftest.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        return {**super().get_context_data(**kwargs), "preview_scripts": SCRIPTS}
