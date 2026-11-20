import time

from django.contrib.auth import logout, update_session_auth_hash
from django.db import transaction
from django.http import Http404, HttpResponseRedirect
from django.middleware.csrf import get_token
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _
from django.views.generic import TemplateView
from rest_framework import status
from rest_framework.exceptions import APIException, NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.activity import log as activity
from apps.core import secrets
from apps.core.auth import enforce_csrf
from apps.core.exceptions import Conflict
from apps.core.throttles import LoginThrottle, ScopedRateThrottle, login_allowed, login_left
from apps.core.times import iso
from apps.siteconf.models import SiteSettings
from apps.web.errors import error_page

from . import entry as login_entry
from . import login as steps
from . import loginlog, totp
from .serializers import (
    LoginSerializer,
    PasswordChangeSerializer,
    TotpConfirmSerializer,
    TotpDisableSerializer,
    UserSerializer,
)


class SessionView(APIView):
    """The caller's login session: GET reads it, POST logs in (maybe in steps, apps/accounts/login.py),
    DELETE logs out.

    Logging in is limited per address (apps.core.throttles.LoginThrottle), also for someone logged in
    already, and every step of it counts. While a secure login entry is set (apps/accounts/entry.py), every
    POST must carry it as "entry", else 404. Logging out works for anybody.
    """

    permission_classes = [AllowAny]

    def get_throttles(self):
        return [LoginThrottle()] if self.request.method == "POST" else []

    def throttled(self, request, wait):
        # In the activity log once per address and minute (loginlog.record_throttled), then the 429.
        try:
            username = loginlog.typed_username(request.data)
        except APIException:  # a body that can't be read: still a 429, not its own error
            username = ""
        loginlog.record_throttled(request._request, username, seconds=wait or 60)
        super().throttled(request, wait)

    def get(self, request):
        user = request.user if request.user.is_authenticated else None
        step = None if user else steps.pending(request._request)
        return Response(
            {
                "authenticated": user is not None,
                "user": UserSerializer(user).data if user else None,
                "step": step.name if step else None,
                # Also sets the CSRF cookie; send it back as X-CSRFToken on writes.
                "csrf_token": get_token(request._request),
            }
        )

    def post(self, request):
        enforce_csrf(request)  # login CSRF: also for nobody logged in
        entry = login_entry.current()
        if entry and not login_entry.matches(request.data.get("entry") if isinstance(request.data, dict) else None, entry):
            raise NotFound()
        django_request = request._request
        name = request.data.get("step") if isinstance(request.data, dict) else None
        if name:
            answer, logged_in = steps.continue_step(django_request, str(name), request.data)
        else:
            asked = steps.before_password(django_request, request.data)
            if asked is not None:
                return Response(asked, status=status.HTTP_200_OK)
            serializer = LoginSerializer(data=request.data, context={"request": django_request})
            serializer.is_valid(raise_exception=True)
            answer, logged_in = steps.after_password(django_request, serializer.validated_data["user"])
        return Response(answer, status=status.HTTP_201_CREATED if logged_in else status.HTTP_200_OK)

    def delete(self, request):
        enforce_csrf(request)
        logout(request._request)  # also ends a login that waits for a step
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordView(APIView):
    """PUT /users/me/password/ {"old_password": "...", "new_password": "..."} changes the password. An
    account without a usable password (made by single sign-on) sets one without "old_password".

    This session stays logged in; the user's other sessions are logged out (Django checks a hash of
    the password on every request).
    """

    permission_classes = [IsAuthenticated]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password"

    def put(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        user = request.user
        first = not user.has_usable_password()
        user.set_password(serializer.validated_data["new_password"])
        with transaction.atomic():
            user.save(update_fields=["password"])
            activity.record(activity.Kind.CHANGE_PASSWORD, actor=user, detail={"first": True} if first else None)
        update_session_auth_hash(request._request, user)
        return Response(status=status.HTTP_204_NO_CONTENT)


def totp_state(user):
    return {
        "enabled": user.totp_enabled,
        "enabled_at": iso(user.totp_enabled_at),
        # Offered to superusers; anyone who has it on may turn it off.
        "available": user.is_superuser or user.totp_enabled,
    }


def keep_session_after_check(request, user, stored):
    """After a serializer checked the account's password: Django re-hashes an old-style hash on the way
    (check_password saves the new one), which would end this very session (the session hash is made
    from it). Then the session follows the new hash."""
    if user.password != stored:
        update_session_auth_hash(request._request, user)


class TotpThrottled(APIView):
    """Writes are limited per user (scope "totp"): each checks the password."""

    permission_classes = [IsAuthenticated]
    throttle_scope = "totp"

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method != "GET" else []


class TotpView(TotpThrottled):
    """GET users/me/totp/: {"enabled", "enabled_at", "available"}. POST {"code", "password"} turns it on
    with the secret users/me/totp/setup/ made, once a code from the device shows it was read right."""

    def get(self, request):
        return Response(totp_state(request.user))

    def post(self, request):
        user = request.user
        if not user.is_superuser:
            raise PermissionDenied(_("Two-step verification is for superusers only for now."))
        if user.totp_enabled:
            raise Conflict(_("Two-step verification is already on."))
        stored = user.password
        form = TotpConfirmSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        keep_session_after_check(request, user, stored)
        pending = request.session.get(totp.SETUP_KEY) or {}
        if pending.get("user") != user.pk or pending.get("until", 0) < time.time():
            raise Conflict(_("The setup has expired. Please start again."))
        try:
            secret = secrets.decrypt(pending.get("secret"))
        except secrets.UnreadableSecret:
            raise Conflict(_("The setup has expired. Please start again.")) from None
        code = totp.clean_code(form.validated_data["code"])
        step = totp.matching_step(secret, code) if code else None
        if step is None:
            raise ValidationError({"code": [_("Wrong: enter the 6 digits the authenticator app shows for this site now.")]})
        user.totp_secret = secrets.encrypt(secret)
        user.totp_last_step = step  # the code just shown can't log in as well
        user.totp_enabled_at = timezone.now()
        with transaction.atomic():
            user.save(update_fields=["totp_secret", "totp_last_step", "totp_enabled_at"])
            activity.record(activity.Kind.TOTP_ON, actor=user)
        request.session.pop(totp.SETUP_KEY, None)
        return Response(totp_state(user))


class TotpSetupView(TotpThrottled):
    """POST users/me/totp/setup/: a new secret for the device, kept in this session (encrypted) for
    totp.SETUP_SECONDS until POST users/me/totp/ confirms it. Answers {"secret", "uri", "qr"} ("qr" is ""
    when the address doesn't fit in a QR code): the only time the secret is shown."""

    def post(self, request):
        user = request.user
        if not user.is_superuser:
            raise PermissionDenied(_("Two-step verification is for superusers only for now."))
        if user.totp_enabled:
            raise Conflict(_("Two-step verification is already on. To move to another phone, turn it off and set it up again."))
        secret = totp.new_secret()
        request.session[totp.SETUP_KEY] = {
            "user": user.pk, "secret": secrets.encrypt(secret), "until": time.time() + totp.SETUP_SECONDS,
        }
        address = totp.uri(secret, user.get_username(), SiteSettings.load().site_name or "HFile")
        return Response(
            {"secret": secret, "uri": address, "qr": totp.qr_data_uri(address), "expires_in": totp.SETUP_SECONDS},
            headers={"Cache-Control": "no-store"},
        )


class TotpDisableView(TotpThrottled):
    """POST users/me/totp/disable/ {"password"}: turns two-step verification off."""

    def post(self, request):
        user = request.user
        stored = user.password
        form = TotpDisableSerializer(data=request.data, context={"request": request})
        form.is_valid(raise_exception=True)
        keep_session_after_check(request, user, stored)
        had = user.totp_enabled
        user.totp_secret = ""
        user.totp_last_step = None
        user.totp_enabled_at = None
        with transaction.atomic():
            user.save(update_fields=["totp_secret", "totp_last_step", "totp_enabled_at"])
            if had:
                activity.record(activity.Kind.TOTP_OFF, actor=user)
        return Response(totp_state(user))


class LoginPage(TemplateView):
    """/login/, or /login/<entry>/ while a secure login entry is set (apps/accounts/entry.py): a page with the
    login form, which sends the entry along. Every other /login/... is 404. Someone logged in already
    goes on to ?next= (a path of this site) or the home page.

    While an entry is set, each wrong one counts in the address's login budget (apps.core.throttles.LoginThrottle,
    10 a minute, an IPv6 address by its /64, shared with POST /api/v1/session/). With the budget spent,
    every /login/... answers 429, the right entry too, so that a right guess looks like a wrong one;
    opening the right entry costs nothing."""

    template_name = "accounts/login.html"
    http_method_names = ["get", "head", "options"]

    def get(self, request, entry=None):
        configured = login_entry.current()
        if configured:
            allowed, wait = login_left(request)
            if not allowed:
                return error_page(request, 429, retry_after=wait)
            if entry is None or not login_entry.matches(entry, configured):
                login_allowed(request)  # this guess counts
                raise Http404
        elif entry is not None:
            raise Http404
        if request.user.is_authenticated:
            target = request.GET.get("next", "")
            safe = url_has_allowed_host_and_scheme(target, allowed_hosts={request.get_host()}, require_https=request.is_secure())
            return HttpResponseRedirect(target if target.startswith("/") and safe else "/")
        return self.render_to_response(self.get_context_data(entry=entry or ""))
