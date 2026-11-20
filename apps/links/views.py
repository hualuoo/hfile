"""Making links, for the file browser's 获取直链 / 生成短链 / 同时获取 (inventory 9.2-9.4), and the short
links' page. Path links themselves are answered through the files app's catch-all (delivery.route).

    GET  /s/<key>                        a short link (delivery.short_link)

Under /api/v1/ (apps/links/api_urls.py), for everybody (the guest's grant answers visitors), CSRF always:

    POST links/<storage key>/path/       {"paths": [...], "password"?, "qr"?}: path links of those files
                                         -> {"links": [{"path", "name", "url"}], "qr"?: {"svg", "png"} | null}
    POST links/<storage key>/short/      {"paths": [...], "expires": seconds, "password"?, "qr"?}: short links
                                         -> 201 {"links": [{"path", "name", "key", "url", "expires_at"}], "qr"?}

`paths` are the files' paths relative to the caller's root (at most service.BATCH_MAX); every one is
checked before anything is answered or stored, and one refusal refuses the request (403 with the
policy's code, password_required with "rule" and "path" to unlock). `expires` is the `seconds` of one
of the site's 短链有效期 (-1 for 永久), else 400 invalid_expiry. `password`: a folder password for this
request only (API clients; the browser unlocks with storages' unlock/). `qr`: with one file, the QR code
of its link as data: URLs. The URLs are absolute (apps.core.origin: [site] url, else the request's address).
At most 60 requests a minute per user, per address for visitors (throttle scope "links"), and at most
service.SHORT_LINKS_PER_MINUTE short links a minute per maker (429 too_many_links).
"""

from __future__ import annotations

from rest_framework import serializers, status
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from django.utils.translation import gettext_lazy, ngettext_lazy

from apps.core.auth import enforce_csrf
from apps.core.fields import BoundedListField, StringField
from apps.core.throttles import ScopedRateThrottle
from apps.siteconf.models import SiteSettings
from apps.storages import access as policy

from . import delivery, service

PATH_MAX = 4096


def short_link(request, key):
    return delivery.short_link(request, key)


class _Path(StringField):
    def __init__(self, **kwargs):
        super().__init__(max_length=PATH_MAX, allow_blank=False, trim_whitespace=False, **kwargs)


class PathLinksBody(serializers.Serializer):
    paths = BoundedListField(
        child=_Path(), allow_empty=False, max_length=service.BATCH_MAX,
        error_messages={"empty": gettext_lazy("Please select at least one file."),
                        "max_length": ngettext_lazy("At most {max_length} file at a time.", "At most {max_length} files at a time.", "max_length")},
    )
    password = StringField(max_length=1000, required=False, allow_blank=True, allow_null=True,
                                     default=None, trim_whitespace=False)
    qr = serializers.BooleanField(required=False, default=False)


class ShortLinksBody(PathLinksBody):
    expires = serializers.IntegerField(error_messages={"required": gettext_lazy("Please choose a short link lifetime."), "invalid": gettext_lazy("The short link lifetime should be a number of seconds.")})


class _LinksView(APIView):
    permission_classes = [AllowAny]
    # A short link is a row: a loop asking for them can't fill the table at full speed.
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "links"
    body_class = PathLinksBody
    kind = "path"

    def post(self, request, key):
        enforce_csrf(request)
        body = self.body_class(data=request.data)
        body.is_valid(raise_exception=True)
        data = body.validated_data
        site = SiteSettings.load()
        service.require(site, self.kind)
        access = policy.open_storage(request._request, key)
        links = self.make(request._request, site, access, data)
        answer = {"links": links}
        if data["qr"] and len(links) == 1:
            answer["qr"] = service.qr_code(links[0]["url"])
        return Response(answer, status=self.status)


class PathLinksView(_LinksView):
    status = status.HTTP_200_OK

    def make(self, request, site, access, data):
        return service.make_path_links(request, site, access, data["paths"], password=data["password"] or None)


class ShortLinksView(_LinksView):
    body_class = ShortLinksBody
    kind = "short"
    status = status.HTTP_201_CREATED

    def make(self, request, site, access, data):
        return service.make_short_links(request, site, access, data["paths"], data["expires"],
                                        password=data["password"] or None)
