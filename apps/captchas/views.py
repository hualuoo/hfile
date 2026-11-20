from dataclasses import asdict

from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.throttles import ScopedRateThrottle

from . import service
from .serializers import IssueSerializer


class CaptchaView(APIView):
    """POST /captcha/ {"scene": "login"}: a new picture for that form, or {"required": false}.

    The form sends the answer back as "captcha", with "captcha_key" from here. The picture is in the
    answer (a data: URI), never at an address of its own that could be asked for again.
    """

    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = service.ISSUE_SCOPE

    def post(self, request):
        serializer = IssueSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        scene = serializer.validated_data["scene"]
        if not service.required(scene):
            return Response({"required": False}, headers={"Cache-Control": "no-store"})
        return Response({"required": True, **asdict(service.issue(scene))}, headers={"Cache-Control": "no-store"})
