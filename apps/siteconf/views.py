"""The settings API (/api/v1/, config/api_urls.py):

    GET   site/                                the public part, for everybody (PublicSiteSerializer)
    GET   manage/settings/<group>/             admins: one page of settings (serializers.GROUPS)
    PATCH manage/settings/<group>/             admins: change some of them; every problem by field
    PUT   manage/settings/site/<image>/        admins: upload the favicon or home_logo (multipart "file")
    DELETE manage/settings/site/<image>/       admins: back to HFile's own
    POST  manage/settings/access/test/         admins: would these blocklists block this IP / UA?
    GET   manage/diagnostics/                  admins: the request as the site sees it (D42)
"""

import re
from urllib.parse import urlsplit

from django.conf import settings
from django.db import transaction
from django.utils.translation import gettext as _, ngettext
from rest_framework import generics, serializers
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.utils import html
from rest_framework.views import APIView

from apps.activity import log as activity
from apps.core import images, masking, net
from apps.core.exceptions import Unavailable
from apps.core.permissions import IsAdmin
from apps.core.text import from_header
from apps.core.views import LockedSettingsMixin

from . import blocklist
from .models import SiteSettings, site_upload_to
from .serializers import GROUPS, AccessTestSerializer, PublicSiteSerializer, SiteGroupSerializer, forget_upload


class SiteView(APIView):
    """GET /site/: the site's public settings, for everybody. (The installer's last page asks it until
    the program has started again: before that, the installer answers 503 for every API address.)"""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(PublicSiteSerializer(SiteSettings.load()).data)


class SettingsView(LockedSettingsMixin, generics.RetrieveUpdateAPIView):
    """GET and PATCH /manage/settings/<group>/. A change reads the row again under its lock first
    (apps.core.views.LockedSettingsMixin): two admins saving at once don't undo each other's fields. The
    activity log names the fields that changed, not their values."""

    permission_classes = [IsAdmin]
    http_method_names = ["get", "patch", "head", "options"]
    settings_model = SiteSettings

    def get_serializer_class(self):
        try:
            return GROUPS[self.kwargs["group"]]
        except KeyError:
            raise NotFound(_("There is no such page of settings.")) from None

    def perform_update(self, serializer):
        site = serializer.instance
        sent = [name for name in serializer.validated_data if hasattr(site, name)]
        before = {name: getattr(site, name) for name in sent}
        site = serializer.save()
        # A write-only one (the OnlyOffice secret) is stored encrypted: sending it counts as a change.
        changed = [name for name in sent if serializer.fields[name].write_only or getattr(site, name) != before[name]]
        if changed:
            activity.record(activity.Kind.UPDATE_SETTINGS, actor=self.request.user,
                            detail={"group": self.kwargs["group"], "fields": changed})


class _ImageUpload(serializers.Serializer):
    file = serializers.FileField()

    def to_internal_value(self, data):
        # With two pictures, DRF would check and keep only the last one.
        if html.is_html_input(data) and len(data.getlist("file")) > 1:
            raise serializers.ValidationError({"file": [_("Only one picture can be uploaded.")]})
        return super().to_internal_value(data)

    def validate_file(self, value):
        from django.core.exceptions import ValidationError

        try:
            return self.context["process"](value)
        except ValidationError as error:
            raise serializers.ValidationError(error.messages) from None


class SiteImageView(APIView):
    """PUT /manage/settings/site/<favicon|home_logo>/ (multipart, field "file") checks the picture,
    re-encodes it as PNG (apps/core/images.py) and saves it as site/<random>.png, the new value of the
    setting; DELETE goes back to HFile's own (""). The file it replaces is deleted once the change is
    committed. Both answer the whole 站点设置 group, like GET."""

    permission_classes = [IsAdmin]
    PROCESS = {"favicon": images.process_favicon, "home_logo": images.process_logo}

    def _field(self):
        field = self.kwargs["field"]
        if field not in self.PROCESS:
            raise NotFound(_("There is no such picture setting."))
        return field

    def put(self, request, field):
        field = self._field()
        upload = _ImageUpload(data=request.data, context={"process": self.PROCESS[field]})
        upload.is_valid(raise_exception=True)
        image = upload.validated_data["file"]
        from django.core.files.storage import default_storage

        try:
            name = default_storage.save(site_upload_to(None, image.file.name), image.file)
        except OSError as error:
            raise Unavailable(_("The picture can't be saved: the folder of [paths] uploads in "
                                "the settings file (%(folder)s) can't be written to (%(error)s).") % {"folder": settings.MEDIA_ROOT, "error": error.strerror or error}) from None
        try:
            site = self._replace(field, name)
        except BaseException:
            default_storage.delete(name)
            raise
        return Response(SiteGroupSerializer(site, context={"request": request}).data)

    def delete(self, request, field):
        field = self._field()
        site = self._replace(field, "")
        return Response(SiteGroupSerializer(site, context={"request": request}).data)

    @staticmethod
    def _replace(field, value):
        with transaction.atomic():
            site = SiteSettings.objects.select_for_update().get(pk=SiteSettings.load().pk)
            old = getattr(site, field)
            if old != value:
                setattr(site, field, value)
                site.save(update_fields=[field, "updated_at"])
                activity.record(activity.Kind.UPDATE_SETTINGS, detail={"group": "site", "fields": [field]})
                forget_upload(old)
        return site


class AccessTestView(APIView):
    """POST /manage/settings/access/test/ {ip_blocklist?, ua_blocklist?, ip?, user_agent?}: whether the
    lists (the saved ones when left out) block the IP and the User-Agent (yours when left out), and
    by which rule. For the live test on 访问控制; changes nothing."""

    permission_classes = [IsAdmin]

    def post(self, request):
        serializer = AccessTestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        site = SiteSettings.load()
        rules = blocklist.parse(data.get("ip_blocklist", site.ip_blocklist), data.get("ua_blocklist", site.ua_blocklist))
        ip = data["ip"] if data.get("ip") else net.client_ip(request)
        user_agent = data["user_agent"] if "user_agent" in data else blocklist.user_agent(request.META)
        return Response({
            "ip": {"value": ip, "rule": rules.ip_rule(ip)},
            "user_agent": {"value": user_agent, "rule": rules.ua_rule(user_agent)},
        })


# ------------------------------------------------------------------ 诊断信息

# Request headers whose values are shown: what browsers send, and what proxies and CDNs add to say where
# a request came from (what the checks of spec 6 are about). Every other value is <已隐藏>: a header's
# name doesn't say whether it is a secret (X-Origin-Verify is a CDN's key to the origin, X-Upstream-Pass
# a password), and the page's 复制 makes it easy to paste them into a forum post (SEC-03).
_SHOWN_HEADERS = frozenset({
    "accept", "accept-encoding", "accept-language", "cache-control", "connection", "content-length", "content-type",
    "dnt", "host", "origin", "pragma", "priority", "referer", "te", "upgrade", "upgrade-insecure-requests",
    "user-agent", "sec-gpc", "x-requested-with",
    "forwarded", "via", "x-forwarded-for", "x-forwarded-host", "x-forwarded-port", "x-forwarded-proto",
    "x-forwarded-scheme", "x-forwarded-server", "x-forwarded-ssl", "x-real-ip", "x-scheme", "front-end-https",
    "cdn-loop", "cf-connecting-ip", "cf-connecting-ipv6", "cf-ipcountry", "cf-ray", "cf-visitor", "true-client-ip",
    "x-client-ip", "x-cluster-client-ip", "fastly-client-ip", "x-request-id", "x-amz-cf-id", "traceparent",
})
_SHOWN_PREFIXES = ("sec-fetch-", "sec-ch-ua")
# A JSON Web Token in a value that is shown anyway (a Referer, say).
_JWT = re.compile(r"eyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*")


def _header_name(meta_key):
    name = meta_key.removeprefix("HTTP_")
    return "-".join(part.capitalize() for part in name.split("_"))


def masked_headers(request):
    """The request's headers, as {name: value} in the order of the name: the values of the known harmless
    ones (_SHOWN_HEADERS) as the client sent them (UTF-8, not latin-1), through apps.core.masking as the
    access log does (a Referer with ?token= or the admin's path); Cookie with its cookie names only (which
    cookies came is useful to know); every other value hidden."""
    headers = {}
    for key, value in request.META.items():
        if not (key.startswith("HTTP_") or key in ("CONTENT_TYPE", "CONTENT_LENGTH")):
            continue
        name = _header_name(key)
        lowered = name.lower()
        if lowered == "cookie":
            names = [part.split("=", 1)[0].strip() for part in str(value).split(";") if part.strip()]
            value = "; ".join(_("%(cookie)s=<hidden>") % {"cookie": cookie} for cookie in names)
        elif lowered in _SHOWN_HEADERS or lowered.startswith(_SHOWN_PREFIXES):
            value = _JWT.sub(_("<hidden>"), masking.mask_text(from_header(str(value))))
        else:
            value = _("<hidden>")
        headers[name] = str(value)
    return dict(sorted(headers.items()))


def diagnostics(request):
    """What the reverse-proxy checks of spec 6 need to know about this request."""
    forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    hops = [hop.strip() for hop in forwarded_for.split(",") if hop.strip()] if forwarded_for else []
    proxies = settings.SERVER["proxies"]
    try:
        host = request.get_host()
    except Exception:  # DisallowedHost can't reach a view; anything else isn't worth a 500 here
        host = request.META.get("HTTP_HOST", "")
    scheme = "https" if request.is_secure() else "http"
    proxy_headers = [name for name in ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST",
                                        "HTTP_X_REAL_IP", "HTTP_FORWARDED") if name in request.META]
    found = {
        "client_ip": net.client_ip(request),
        "remote_addr": request.META.get("REMOTE_ADDR", ""),
        "forwarded_for": hops,
        "scheme": scheme,
        "host": host,
        "origin": f"{scheme}://{host}",
        "site_url": settings.SITE_URL,
        "proxies": proxies,
        "proxy_headers": [_header_name(name) for name in proxy_headers],
        "allowed_hosts": list(settings.ALLOWED_HOSTS),
        "headers": masked_headers(request),
    }
    found["notes"] = notes(found)
    return found


_DEFAULT_PORTS = {"http": "80", "https": "443"}


def _host(scheme, netloc):
    """host[:port] in lower case, without the scheme's default port: example.com:443 is example.com."""
    netloc = netloc.lower()
    host, port = netloc, ""
    if netloc.startswith("["):  # [2001:db8::1] or [2001:db8::1]:8080
        end = netloc.find("]")
        host, rest = netloc[: end + 1], netloc[end + 1:]
        port = rest[1:] if rest.startswith(":") else ""
    elif netloc.count(":") == 1:
        host, port = netloc.split(":")
    return host if not port or port == _DEFAULT_PORTS.get(scheme) else f"{host}:{port}"


def notes(found):
    """What looks wrong in `found` (diagnostics()), as [{"kind": "warning" | "info" | "ok", "text"}]."""
    result = []
    proxies, hops = found["proxies"], found["forwarded_for"]
    if not proxies and found["proxy_headers"]:
        result.append({"kind": "warning", "text": (
            _("The request has headers a reverse proxy adds (%(headers)s), but [server] proxies in the settings file "
              "is 0: the site doesn't trust those headers and sees every visitor's IP as %(remote_addr)s (the proxy's "
              "own), so rate limits, logs and the IP blocklist go wrong. Set proxies to the number of proxies in front.") % {"headers": _(", ").join(found["proxy_headers"]), "remote_addr": found["remote_addr"]}
        )})
    if proxies and not hops:
        result.append({"kind": "warning", "text": (
            _("[server] proxies in the settings file is %(proxies)s, but the request has no X-Forwarded-For: "
              "either there is no reverse proxy in front after all (set proxies to 0), or the proxy doesn't "
              "pass the header (Nginx needs proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;).") % {"proxies": proxies}
        )})
    elif proxies and len(hops) < proxies:
        result.append({"kind": "warning", "text": (
            ngettext("X-Forwarded-For has only %(count)s address, fewer than [server] proxies = "
                     "%(proxies)s: the visitor's IP is taken from the leftmost one, which the client fills "
                     "in itself and can fake. proxies must equal the number of proxies in front.", "X-Forwarded-For has only %(count)s addresses, fewer than [server] proxies = %(proxies)s: the visitor's IP is taken from the leftmost one, which the client fills in itself and can fake. proxies must equal the number of proxies in front.", len(hops)) % {"count": len(hops), "proxies": proxies}
        )})
    site_url = found["site_url"]
    if not site_url:
        result.append({"kind": "info", "text": _("[site] url isn't set: full links copied out (direct links, shares) use each request's own address.")})
    else:
        parts = urlsplit(site_url)
        scheme_differs = parts.scheme != found["scheme"]
        host_differs = _host(parts.scheme, parts.netloc) != _host(found["scheme"], found["host"])
        if scheme_differs or host_differs:
            hints = []
            if scheme_differs:
                hints.append(
                    _("the proxy must pass X-Forwarded-Proto (Nginx: proxy_set_header X-Forwarded-Proto "
                      "$scheme;), and [server] proxies must be right for the site to trust it")
                    if parts.scheme == "https" else _("the site gets https, but [site] url says http")
                )
            if host_differs:
                hints.append(_("the proxy must pass the domain the browser used in Host (Nginx: proxy_set_header Host $host;)"))
            result.append({"kind": "warning", "text": (
                _("[site] url in the settings file is %(site_url)s, but this request sees %(origin)s. If you're using the [site] url address: %(hints)s.")
                % {"site_url": site_url, "origin": found["origin"], "hints": _("; ").join(hints)}
            )})
    if not any(note["kind"] == "warning" for note in result):
        result.append({"kind": "ok", "text": _("No problems found: how the site sees its address and visitors' IPs matches the settings file.")})
    return result


class DiagnosticsView(APIView):
    """GET /manage/diagnostics/: the client IP, scheme and host as the site sees them, [site] url,
    [server] proxies and the request's headers with the secret ones hidden (诊断信息, D42)."""

    permission_classes = [IsAdmin]

    def get(self, request):
        return Response(diagnostics(request))
