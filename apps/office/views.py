"""OnlyOffice's API and the Document Server's callback (inventory 7.3); the editor page is apps/office/pages.py.

    POST /api/v1/office/config/     the editor config of a file (apps/office/editor.py), for everybody the
                                    policy lets read it; CSRF always (visitors too). Either
                                    {"storage": "<key>", "path": "/docs/a.docx", "password"?: "<folder password>"}
                                    or {"share": "<share key>", "path": "/a.docx"} (read only); optional
                                    "mobile" and "dark" (booleans: the phone layout, the dark theme). Answers the
                                    Document Server's config (with "token" when a secret is set), no-store.
                                    Errors {"detail", "code"}: the policy's (apps.storages.access), the share's,
                                    onlyoffice_off (404), not_office, not_a_file, not_found (404), busy (409),
                                    storage_error (502). At most 60 a minute per user or address.
    POST /onlyoffice/callback/      the Document Server's callback (apps/office/callback.py): anonymous,
                                    CSRF-exempt, {"error": 0} or {"error": 1}.
"""

from __future__ import annotations

import logging

from django.http import JsonResponse
from django.utils.cache import add_never_cache_headers
from django.utils.translation import gettext as _
from django.views.decorators.csrf import csrf_exempt
from rest_framework import serializers
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.auth import enforce_csrf
from apps.core.fields import StringField
from apps.core.throttles import ScopedRateThrottle

from . import callback as callbacks
from . import editor

logger = logging.getLogger("hfile.office")

PATH_MAX = 4096
KEY_MAX = 64


class ConfigBody(serializers.Serializer):
    storage = StringField(max_length=KEY_MAX, required=False)
    share = StringField(max_length=KEY_MAX, required=False)
    path = StringField(max_length=PATH_MAX, trim_whitespace=False)
    password = StringField(max_length=1000, required=False, allow_blank=True, allow_null=True,
                                     trim_whitespace=False)
    # How the page wants the editor: OnlyOffice's phone layout, its dark theme (signed with the rest).
    mobile = serializers.BooleanField(required=False, default=False)
    dark = serializers.BooleanField(required=False, default=False)

    def validate(self, data):
        if bool(data.get("storage")) == bool(data.get("share")):
            raise serializers.ValidationError(_("Give either storage (a storage source) or share (a share), not both."))
        return data


class ConfigView(APIView):
    permission_classes = [AllowAny]  # the policy decides, the guest's grant answers visitors
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "office"

    def post(self, request):
        enforce_csrf(request)
        body = ConfigBody(data=request.data if isinstance(request.data, dict) else {})
        body.is_valid(raise_exception=True)
        data = body.validated_data
        display = editor.Display(mobile=data["mobile"], dark=data["dark"])
        if data.get("share"):
            config, access = editor.for_share(request._request, data["share"], data["path"], display=display), None
        else:
            config, access = editor.for_file(request._request, data["storage"], data["path"],
                                             password=data.get("password") or None, display=display)
        response = Response(config)
        if access is not None and access.keys is not None:
            access.keys.write(response)  # a bad remembered folder-password cookie is replaced
        add_never_cache_headers(response)  # a signed link to the file and the editor's token
        return response


@csrf_exempt
def callback(request):
    if request.method != "POST":
        response = JsonResponse({"error": 1}, status=405)
        response["Allow"] = "POST"
        return response
    try:
        error = callbacks.handle(request)
    except Exception:  # noqa: BLE001 - the Document Server gets its answer; the traceback goes to the log
        logger.exception("The OnlyOffice callback failed")
        error = 1
    return JsonResponse({"error": error})
