"""Single sign-on's pages and API.

Pages (apps/sso/urls.py, at /sso/; outside /api/v1/, so that the callbacks registered at providers stay):
    GET  /sso/<code>/login?next=/path      to the provider (303), for a login
    GET  /sso/<code>/login/callback        back from it: logged in, or a page saying why not
Both are limited per address (scope "sso"); they work whether or not a secure login entry is set: the
entry shields the password form from scanners, and a login here can't be guessed.

API (config/api_urls.py):
    GET    sso/providers/                    anybody: the login buttons [{code, name, icon}]
    GET    sso/links/                        the user's providers and what is bound
    POST   sso/links/{code}/                 {"password", "next"}: binding, answers {"url"} to go to
    DELETE sso/links/{code}/                 unbinding (not the last way to log in)
    GET    manage/sso/providers/             admins: every provider; POST creates one (superusers)
    GET    manage/sso/providers/{id}/        PATCH changes it, DELETE deletes it and its bindings (superusers)
    POST   manage/sso/providers/order/       {"ids": [...]}: the order on the login page (superusers)
    POST   manage/sso/discover/              {"url"}: reads an OpenID configuration (superusers)
    GET    manage/users/{id}/identities/     a user's bound accounts; POST {"provider", "external_id"} binds one
    DELETE manage/users/{id}/identities/{identity id}/

A provider's settings decide which of its accounts logs in as which user (the endpoints, the id field,
the client): whoever may change them may log in as anybody bound to it. So only superusers change them,
as only superusers change other admins' accounts (apps/accounts/managing.py); other staff see the list.
"""

from django.contrib.auth import get_user_model, update_session_auth_hash
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import HttpResponseRedirect
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import NotFound, PermissionDenied, ValidationError
from rest_framework.permissions import SAFE_METHODS, AllowAny, BasePermission, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import entry as login_entry
from apps.accounts import loginlog, managing
from apps.core.exceptions import Conflict
from apps.core.permissions import IsAdmin, is_admin
from apps.core.throttles import ScopedRateThrottle, UserRateThrottle
from apps.core.times import iso
from apps.siteconf.models import SiteSettings
from apps.web.errors import error_page

from . import flow, outbound
from .models import Identity, Provider
from .serializers import (
    DiscoverSerializer,
    IdentitySerializer,
    LinkSerializer,
    NewIdentitySerializer,
    OrderSerializer,
    ProviderSerializer,
    PublicProviderSerializer,
)

User = get_user_model()

NOT_FOUND = gettext_lazy("This single sign-on doesn't exist or isn't enabled.")


class SsoThrottle(UserRateThrottle):
    """Scope "sso": per user when logged in, else per address (apps.core.throttles)."""

    scope = "sso"


class IsSuperuserAdmin(BasePermission):
    """Changing providers: superusers only (see the top)."""

    message = gettext_lazy("Only superusers can change the single sign-on settings: they decide which provider account logs in as which user.")

    def has_permission(self, request, view):
        return is_admin(request.user) and request.user.is_superuser


# ------------------------------------------------------------------ pages


HOME = (gettext_lazy("Back to home"), "/")


def failure_page(request, failure):
    """The error page of a login or binding that didn't happen (flow.Failure), with the way on:
    back where the binding began, again with the same provider, or a password login for an account
    that isn't bound yet, where the site shows a way to log in (SiteSettings.login_visible: not with a
    secure login entry, whose address isn't shown, nor with the login button off)."""
    provider = failure.provider
    actions = []
    if failure.intent == flow.LINK:
        actions.append((_("Back"), failure.next))
    elif failure.retry and provider is not None and provider.enabled:
        params = {"next": failure.next} if failure.next != "/" else {}
        actions.append((_("Log in again"), flow.with_query(f"/sso/{provider.code}/login", **params)))
    elif failure.reason == "sso_not_bound" and not request.user.is_authenticated and SiteSettings.load().login_visible:
        actions.append((_("Log in with a password"), flow.with_query(login_entry.login_path(), next=failure.next)))
    if actions:
        actions.append(HOME)
    response = error_page(request, failure.status, failure.message, heading=failure.heading, actions=actions)
    if failure.status >= 500:
        # The provider failed, not the site: the hfile.sso logger said why (outbound.py, flow.py); Django
        # would add an ERROR line for the 502 on top.
        response._has_been_logged = True
    return response


def _throttled(request):
    throttle = SsoThrottle()
    if throttle.allow_request(request, None):
        return None
    return error_page(request, 429, retry_after=int(throttle.wait() or 0) + 1)


@never_cache
@require_GET
def login_start(request, code):
    """To the provider, with a new state in the session (flow.begin). Someone logged in already just goes on."""
    if (response := _throttled(request)) is not None:
        return response
    next_path = flow.safe_next(request.GET.get("next"))
    provider = Provider.objects.filter(code=code, enabled=True).first()
    if provider is None:
        return error_page(request, 404, NOT_FOUND, heading=_("Single sign-on not found"))
    if request.user.is_authenticated:
        return HttpResponseRedirect(next_path)
    response = HttpResponseRedirect(flow.begin(request, provider, intent=flow.LOGIN, next_path=next_path))
    response.status_code = 303
    return response


@never_cache
@require_GET
def login_callback(request, code):
    """Back from the provider (flow.complete). A login that fails is written to the login log."""
    if (response := _throttled(request)) is not None:
        return response
    try:
        target = flow.complete(request, code, request.GET)
    except flow.Failure as failure:
        if failure.reason and failure.intent == flow.LOGIN:
            loginlog.record(request, success=False, username=failure.username, user=failure.user, reason=failure.reason)
        return failure_page(request, failure)
    return HttpResponseRedirect(target)


# ------------------------------------------------------------------ the login buttons and 账号绑定


class PublicProvidersView(APIView):
    """GET sso/providers/: the enabled providers, in their order, for the login form."""

    permission_classes = [AllowAny]

    def get(self, request):
        return Response(PublicProviderSerializer(Provider.objects.filter(enabled=True), many=True).data)


def _enabled(code):
    provider = Provider.objects.filter(code=code, enabled=True).first()
    if provider is None:
        raise NotFound(NOT_FOUND)
    return provider


class LinksView(APIView):
    """GET sso/links/: {"has_password", "providers": [{code, name, icon, enabled, linked, external_id,
    created_at, last_login_at}]}: the enabled providers, and those the user has an account bound of."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        user = request.user
        bound = {identity.provider_id: identity for identity in Identity.objects.filter(user=user)}
        rows = []
        for provider in Provider.objects.filter(Q(enabled=True) | Q(pk__in=list(bound))):
            identity = bound.get(provider.pk)
            rows.append({
                "code": provider.code, "name": provider.name, "icon": provider.icon, "enabled": provider.enabled,
                "linked": identity is not None,
                "external_id": identity.external_id if identity else None,
                "created_at": iso(identity.created_at) if identity else None,
                "last_login_at": iso(identity.last_login_at) if identity else None,
            })
        return Response({"has_password": user.has_usable_password(), "providers": rows})


class LinkView(APIView):
    """POST sso/links/{code}/ {"password", "next"}: begins binding an account of this provider to the
    user; answers {"url"}, the provider's address to go to. A user who has a password types it: a
    session left open somewhere must not be enough to add a login that outlives a new password (as
    turning on two-step verification asks for it). Checks of the password count in the "password"
    rate limit, with 修改密码's.

    DELETE sso/links/{code}/: unbinds it, unless it is the user's last way to log in (no usable
    password and no other bound account)."""

    permission_classes = [IsAuthenticated]

    def get_throttles(self):
        return [ScopedRateThrottle()] if self.request.method == "POST" else []

    throttle_scope = "password"

    def post(self, request, code):
        provider = _enabled(code)
        user = request.user
        form = LinkSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        if user.has_usable_password():
            password = form.validated_data.get("password") or ""
            if not password:
                raise ValidationError({"password": [_("To confirm it's you, please enter your login password.")]})
            stored = user.password
            if not user.check_password(password):
                raise ValidationError({"password": [_("Wrong password.")]})
            if user.password != stored:  # re-hashed with today's settings: the session's hash follows it
                update_session_auth_hash(request._request, user)
        if Identity.objects.filter(provider=provider, user=user).exists():
            raise Conflict(_("You have already linked a %(name)s account. To use another, unlink this one first.") % {"name": provider.name})
        url = flow.begin(request._request, provider, intent=flow.LINK, next_path=form.validated_data.get("next") or "/", user=user)
        return Response({"url": url}, headers={"Cache-Control": "no-store"})

    def delete(self, request, code):
        provider = Provider.objects.filter(code=code).first()
        if provider is None:
            raise NotFound(_("There is no such single sign-on."))
        with transaction.atomic():
            # The user's row is locked: two unbindings at once can't both see the other account as left.
            user = User.objects.select_for_update().get(pk=request.user.pk)
            identity = Identity.objects.filter(provider=provider, user=user).first()
            if identity is None:
                raise NotFound(_("You haven't linked a %(name)s account.") % {"name": provider.name})
            # Another way in: a password, or an account at a provider that is enabled.
            others = Identity.objects.filter(user=user, provider__enabled=True).exclude(pk=identity.pk)
            if not user.has_usable_password() and not others.exists():
                raise Conflict(
                    _("This is your only way to log in now: after unlinking you couldn't log in. Set a "
                      'password in "Set password" in the menu at the top right first, then unlink %(name)s.') % {"name": provider.name}
                )
            identity.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


# ------------------------------------------------------------------ admins


class ManageProviderViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin,
                            viewsets.GenericViewSet):
    """/manage/#sso: the providers (all of them, not in pages: there are a few). Deleting one deletes
    the accounts bound of it."""

    serializer_class = ProviderSerializer
    pagination_class = None
    lookup_value_regex = r"[0-9]+"
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]

    def get_permissions(self):
        return [IsAdmin()] if self.request.method in SAFE_METHODS else [IsSuperuserAdmin()]

    def get_queryset(self):
        return Provider.objects.annotate(identity_count=Count("identities")).order_by("order", "id")

    def _saved(self, serializer, code=status.HTTP_200_OK):
        serializer.is_valid(raise_exception=True)
        try:
            with transaction.atomic():
                provider = serializer.save()
        except IntegrityError:  # the same code saved at the same moment
            if Provider.objects.filter(code=serializer.validated_data.get("code", "")).exists():
                raise ValidationError({"code": [_("This short name is taken. Please choose another.")]}) from None
            raise
        return Response(self.get_serializer(self.get_queryset().get(pk=provider.pk)).data, status=code)

    def create(self, request):
        return self._saved(self.get_serializer(data=request.data), status.HTTP_201_CREATED)

    def partial_update(self, request, pk=None):
        return self._saved(self.get_serializer(self.get_object(), data=request.data, partial=True))

    @action(detail=False, methods=["post"])
    def order(self, request):
        form = OrderSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        ids = form.validated_data["ids"]
        with transaction.atomic():
            existing = set(Provider.objects.select_for_update().values_list("pk", flat=True))
            if set(ids) != existing:
                raise Conflict(_("The provider list just changed. Please reload the page and sort again."))
            for index, pk in enumerate(ids):
                Provider.objects.filter(pk=pk).update(order=(index + 1) * 10)
        return Response(self.get_serializer(self.get_queryset(), many=True).data)


class DiscoverView(APIView):
    """POST manage/sso/discover/ {"url"}: the endpoints, scopes and client authentication of an OpenID
    configuration (.well-known/openid-configuration), fetched by the server: the page's CSP lets it
    reach only this site. Superusers only (it is for their form), with the limits of every outbound
    request (outbound.py)."""

    permission_classes = [IsSuperuserAdmin]
    throttle_classes = [SsoThrottle]

    def post(self, request):
        form = DiscoverSerializer(data=request.data)
        form.is_valid(raise_exception=True)
        try:
            found = outbound.discover(form.validated_data["url"])
        except outbound.ProviderError as exc:
            raise ValidationError({"url": [exc.message]}) from None
        return Response(found)


def _user(user_id, *, lock=False):
    users = User.objects.select_for_update() if lock else User.objects
    user = users.filter(pk=user_id).first()
    if user is None:
        raise NotFound(_("This user doesn't exist any more; it may have just been deleted."))
    return user


BUILTIN = gettext_lazy("Built-in users don't log in and can't link single sign-on.")
OWN = gettext_lazy('To link or unlink your own single sign-on accounts, use "Linked accounts" '
                   "in the menu at the top right (linking asks for your login password).")


def _refusal(actor, user):
    """Why `actor` may not change `user`'s bound accounts here, or "": the built-in users have none;
    other admins' are changed only by superusers (apps.accounts.managing); one's own go through 账号绑定,
    which asks for the password (here an open admin session alone would add a lasting way in)."""
    if user.is_builtin:
        return BUILTIN
    if user.pk == actor.pk:
        return OWN
    return managing.manager_refusal(actor, user)


class ManageIdentitiesView(APIView):
    """GET manage/users/{id}/identities/: the accounts bound to a user (not other admins' for staff
    who aren't superusers). POST {"provider": id, "external_id"}: an admin binds one (to set a user up
    before they first log in). Who may change whom is apps.accounts.managing's, and nobody binds their own
    account here (_refusal)."""

    permission_classes = [IsAdmin]

    def get(self, request, user_id):
        user = _user(user_id)
        if reason := managing.manager_refusal(request.user, user):
            raise PermissionDenied(reason)
        identities = Identity.objects.filter(user=user).select_related("provider").order_by("provider__order", "provider__id")
        return Response(IdentitySerializer(identities, many=True).data)

    def post(self, request, user_id):
        form = NewIdentitySerializer(data=request.data)
        with transaction.atomic():
            user = _user(user_id, lock=True)
            if reason := _refusal(request.user, user):
                raise PermissionDenied(reason)
            form.is_valid(raise_exception=True)
            provider = form.validated_data["provider"]
            external_id = form.validated_data["external_id"]
            existing = flow.find_identity(provider, external_id)
            if existing is not None:
                if existing.user_id == user.pk:
                    taken = _("This %(name)s account is already linked to this user.") % {"name": provider.name}
                elif managing.manager_refusal(request.user, existing.user):
                    # Whose it is (a superuser, say) isn't for an admin who may not look at that account.
                    taken = _("This account is linked to another user.")
                else:
                    taken = _('This %(name)s account is already linked to the user "%(username)s".') % {"name": provider.name, "username": existing.user.username}
                raise ValidationError({"external_id": [taken]})
            if Identity.objects.filter(provider=provider, user=user).exists():
                raise ValidationError({"provider": [_("This user has already linked a %(name)s account; to change it, unlink that one first.") % {"name": provider.name}]})
            try:
                with transaction.atomic():
                    identity = Identity.objects.create(provider=provider, external_id=external_id, user=user)
            except IntegrityError:
                raise ValidationError({"external_id": [_("This account was just linked. Please reload and look again.")]}) from None
        return Response(IdentitySerializer(identity).data, status=status.HTTP_201_CREATED)


class ManageIdentityView(APIView):
    """DELETE manage/users/{id}/identities/{identity id}/: unbinds it."""

    permission_classes = [IsAdmin]

    def delete(self, request, user_id, pk):
        identity = Identity.objects.select_related("user").filter(pk=pk, user_id=user_id).first()
        if identity is None:
            raise NotFound(_("This link doesn't exist any more."))
        if reason := _refusal(request.user, identity.user):
            raise PermissionDenied(reason)
        identity.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
