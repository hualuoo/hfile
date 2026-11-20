"""Page shells. Each page loads its data from the REST API with JavaScript. The home page "/" is the
file browser (apps/files/pages.py).

Logging in and changing one's password happen in dialogs (base.html). Pages that need a login
redirect to settings.LOGIN_URL, which opens the login dialog on the home page.
"""

from django.conf import settings
from django.contrib.auth.mixins import UserPassesTestMixin
from django.http import Http404, HttpResponseRedirect
from django.templatetags.static import static
from django.views.decorators.cache import cache_control
from django.views.generic import TemplateView

from apps.core.permissions import is_admin

from .navigation import SECTIONS


@cache_control(max_age=3600)
def favicon(request):
    """/favicon.ico, for clients that ask for it without reading the page."""
    return HttpResponseRedirect(static("web/favicon.svg"))


class AdminRequiredMixin(UserPassesTestMixin):
    """Visitors get the login dialog; logged-in users who aren't admins get 403."""

    def test_func(self):
        return is_admin(self.request.user)


class ManagePage(AdminRequiredMixin, TemplateView):
    """/manage/: a menu of sections (apps/web/navigation.py SECTIONS), each a template and a script."""

    template_name = "web/manage.html"

    def get_context_data(self, **kwargs):
        groups = []
        for section in SECTIONS:
            if not groups or groups[-1][0] != section.group:
                groups.append((section.group, []))
            groups[-1][1].append(section)
        # Sections may share one script (siteconf's five settings pages do): each is loaded once.
        scripts = list(dict.fromkeys(section.script for section in SECTIONS if section.script))
        return super().get_context_data(**kwargs, sections=SECTIONS, section_groups=groups, section_scripts=scripts)


class ComponentsPage(TemplateView):
    """/components/: every component of the MD3 styles, in both themes. Only with DEBUG on."""

    template_name = "web/components.html"

    def dispatch(self, request, *args, **kwargs):
        if not settings.DEBUG:
            raise Http404
        return super().dispatch(request, *args, **kwargs)
