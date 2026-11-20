"""The preview API (mounted at /api/v1/preview/). Both endpoints take the signed same-origin URL a
viewer got from ctx.inlineUrl(item) (W1) as the proof that the visitor may read the file, so they work
the same in the browser and in shares, for visitors too:

    POST markdown/    {"url"}  the Markdown file rendered on the server (apps.core.markdown.render(), untrusted):
                               {"html", "size"}; too_large (400) over the limit, not_markdown (400)
    POST kkfileview/  {"url"}  the kkFileView address of the file: {"url", "mode"} (mode iframe or new_tab);
                               kkfileview_off (404) while 显示设置 has no kkFileView 服务地址;
                               kkfileview_same_origin (400) when it is on the site's own origin

The URL is checked by apps.files.signing (apps/preview/sources.py) and apps.storages.access.check_served(): signed for
this file and not expired (link_invalid, link_expired), the storage still enabled, the filter rules still
letting it be read. Errors are {"detail", "code"}. At most 60 a minute per user or address.
"""

from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core import markdown, origin
from apps.core.fields import StringField
from apps.core.permissions import is_admin
from apps.core.throttles import ScopedRateThrottle
from apps.siteconf.models import SiteSettings
from apps.storages import access
from apps.storages.drivers import paths

from . import content, csp, sources


class _UrlSerializer(serializers.Serializer):
    url = StringField(max_length=10_000, trim_whitespace=False)


def _signed(request):
    """(the link the request's signed URL is, that URL), once the policy still lets the file be read."""
    serializer = _UrlSerializer(data=request.data if isinstance(request.data, dict) else {})
    serializer.is_valid(raise_exception=True)
    url = serializer.validated_data["url"]
    link = sources.verify(url)
    access.check_served(link.storage, link.path, operation=access.PREVIEW, ignore_hidden=link.ignore_hidden)
    return link, url


class _PreviewView(APIView):
    permission_classes = [AllowAny]  # the signed URL is the permission
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "preview"

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        add_never_cache_headers(response)  # a file's content, or an address that unlocks it
        return response


class MarkdownView(_PreviewView):
    def post(self, request):
        link, _url = _signed(request)
        site = SiteSettings.load()
        if sources.kind_of(paths.name(link.path), site) != "markdown":
            raise content.PreviewError("not_markdown", _("This isn't a Markdown file (.md or .markdown)."))
        data = content.read_capped(link.storage, link.path, content.markdown_limit(site), staff=is_admin(request.user))
        return Response({"html": markdown.render(content.decode(data)), "size": len(data)})


class KkFileViewView(_PreviewView):
    def post(self, request):
        site = SiteSettings.load()
        if not site.kkfileview_url:
            raise content.PreviewError("kkfileview_off", _("No kkFileView server address is set."), status=404)
        if csp.same_origin(site.kkfileview_url, origin.site_origin(request)):
            raise content.PreviewError(
                "kkfileview_same_origin", _("kkFileView can't be at the same address as the site (the same domain and port). Please ask the administrator to move it to another domain or port."))
        link, url = _signed(request)
        # The kkFileView server fetches the file itself: the signed URL the viewer has, as an absolute
        # address of this site (W1), so it ends when that one ends; never a new signature.
        file_url = origin.absolute(url.split("#", 1)[0], request)
        address = content.kkfileview_url(site.kkfileview_url, file_url, paths.name(link.path))
        return Response({"url": address, "mode": site.kkfileview_open_mode})
