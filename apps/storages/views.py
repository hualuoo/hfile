"""The storages API (config/api_urls.py) and the OAuth callback page.

Admin (apps.core.permissions.IsAdmin), under /api/v1/manage/:
    storage-types/                       the 18 types with their params, for the form
    apps/storages/                            GET the list, POST a new one
    storages/<id>/                       GET, PATCH, DELETE
    storages/<id>/enable/, disable/, check/, copy/, rotate-link-secret/, check-path/
    storages/<id>/filters/, passwords/, readmes/     GET, PUT (the whole list)
    storages/<id>/rules/test/            the rule tester
    storages/sort/, key-check/, test/, helpers/, oauth/, drafts/<id>/
    users/<id>/grants/                   GET, PUT (contract 1)
    grant-options/                       the permission codes, and a user's default permissions
Everybody:
    apps/storages/                            the storages the caller may use
    storages/<key>/unlock/               a folder password (D11)
    storages/unlocks/                    DELETE: forget the unlocked folders
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404, render
from django.utils.translation import gettext as _
from django.views.decorators.cache import never_cache
from rest_framework import serializers, status
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts import managing
from apps.accounts.permissions import ALL, REQUIRES, Permission, help_for, usable
from apps.core.auth import enforce_csrf
from apps.core.fields import StringField
from apps.core.permissions import IsAdmin, is_admin
from apps.core.text import single_line
from apps.core.times import iso

from . import access as policy
from . import config as storage_config
from . import drafts, grants, instances, service
from .drivers import DRIVERS, GROUPS, group_of, paths
from .drivers.base import DriverError, InvalidPath, NotFound
from .models import FilterRule, FolderPassword, Grant, ReadmeRule, StorageSource
from .rules import canonical, compile_glob
from .serializers import grant_list, key_problem, rule_list


def _error(err: service.ActionError) -> Response:
    code = err.status or (status.HTTP_404_NOT_FOUND if err.code == "not_found" else (
        status.HTTP_409_CONFLICT if err.code == "conflict" else status.HTTP_400_BAD_REQUEST))
    return Response(err.data(), status=code)


def _param(param) -> dict:
    return {
        "key": param.key, "label": param.label, "kind": param.kind, "required": param.required,
        "default": None if param.secret else param.default, "help": param.help, "placeholder": param.placeholder,
        "options": [{"value": value, "label": label} for value, label in param.options],
        "secret": param.secret, "advanced": param.advanced, "minimum": param.minimum, "maximum": param.maximum,
    }


def type_info(type_: str) -> dict:
    cls = storage_config.driver(type_)
    group = group_of(type_)
    return {
        "type": type_, "label": cls.label, "oauth": cls.oauth,
        "group": group, "group_label": GROUPS[group][0] if group else "",
        "helpers": [{"name": name, "label": label} for name, label in cls.helpers.items()],
        "params": [_param(param) for param in cls.params],
        # The add wizard: the params of its second step, and what its third step (授权) fills, and how.
        "setup": storage_config.setup_keys(cls),
        "auth": {"kind": "oauth" if cls.oauth else ("qrcode" if "qrcode" in cls.helpers else ""),
                 "params": [key for key in cls.auth_params if key in storage_config.params(cls)]},
        # D50: only superusers create this type, and change these params
        "superuser_only": type_ in service.SUPERUSER_PARAMS,
        "superuser_params": list(service.SUPERUSER_PARAMS.get(type_, ())),
        "options": {
            "download_domain": type_ in storage_config.DOWNLOAD_DOMAIN_TYPES,
            "proxy_download": bool(cls.can_direct_download),
            "proxy_upload": bool(cls.can_direct_upload),
        },
    }


def _status(storage: StorageSource):
    state = instances.status(storage.pk)
    if state is None:
        return None
    return {"ok": state["ok"], "message": state["message"], "at": iso(state["at"])}


def storage_data(storage: StorageSource, *, detail: bool = False) -> dict:
    data = {
        "id": storage.pk, "key": storage.key, "name": storage.name, "type": storage.type,
        "type_label": storage.type_label, "enabled": storage.enabled, "position": storage.position,
        "remark": storage.remark, "status": _status(storage),
        "tokens_saved_at": iso(storage.tokens_saved_at),
    }
    if detail:
        values, secrets_state = ({}, {})
        if storage.type in DRIVERS:
            try:
                values, secrets_state = storage_config.shown(storage_config.driver(storage.type), storage.config)
            except storage_config.ConfigError:
                pass
        data.update({
            "config": values, "secrets": secrets_state,
            "download_domain": storage.download_domain, "proxy_download": storage.proxy_download,
            "proxy_connections": storage.proxy_connections, "proxy_when_full": storage.proxy_when_full,
            "proxy_upload": storage.proxy_upload, "force_download": storage.force_download,
            "link_lifetime": storage.link_lifetime, "permanent_links": storage.permanent_links,
            "gallery_default": storage.gallery_default, "readme_from_file": storage.readme_from_file,
            "created_at": iso(storage.created_at), "updated_at": iso(storage.updated_at),
        })
    return data


class AdminView(APIView):
    permission_classes = [IsAdmin]

    def handle_exception(self, exc):
        # What any action on a saved storage whose type can't be used now answers (gone, or its library
        # missing: STOR-01): 400 saying so, not a 500.
        if isinstance(exc, storage_config.ConfigError):
            return _error(service.ActionError(service.unusable_type(exc), code="type_unavailable"))
        return super().handle_exception(exc)


class StorageTypesView(AdminView):
    """GET: every type in the form's order, with its params, helper buttons and options."""

    def get(self, request):
        types = []
        order = list(GROUPS)
        for type_ in sorted(DRIVERS, key=lambda type_: order.index(group_of(type_)) if group_of(type_) else len(order)):
            try:
                types.append(type_info(type_))
            except Exception:  # noqa: BLE001 - a driver whose library is missing: left out of the form
                continue
        return Response({"types": types, "oauth_redirect_uri": service.callback_url(request),
                         "superuser": bool(request.user.is_superuser)})


class StorageListView(AdminView):
    def get(self, request):
        return Response([storage_data(storage) for storage in StorageSource.objects.order_by("position", "id")])

    def post(self, request):
        try:
            storage, extra = service.save(request, None, request.data)
        except service.ActionError as err:
            return _error(err)
        return Response({**storage_data(storage, detail=True), **extra}, status=status.HTTP_201_CREATED)


class StorageDetailView(AdminView):
    def get(self, request, pk):
        return Response(storage_data(get_object_or_404(StorageSource, pk=pk), detail=True))

    def patch(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        try:
            storage, _extra = service.save(request, storage, request.data)
        except service.ActionError as err:
            return _error(err)
        return Response(storage_data(storage, detail=True))

    def delete(self, request, pk):
        service.delete(get_object_or_404(StorageSource, pk=pk))
        return Response(status=status.HTTP_204_NO_CONTENT)


class StorageEnableView(AdminView):
    enabled = True

    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        try:
            storage = service.set_enabled(storage, self.enabled)
        except service.ActionError as err:
            return _error(err)
        return Response(storage_data(storage))


class StorageDisableView(StorageEnableView):
    enabled = False


class StorageCheckView(AdminView):
    """重试: checks the saved params."""

    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        service.recheck(storage)
        return Response(storage_data(storage))


class _CopySerializer(serializers.Serializer):
    key = StringField(max_length=200, allow_blank=True)
    name = StringField(max_length=200, allow_blank=True)


class StorageCopyView(AdminView):
    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        body = _CopySerializer(data=request.data)
        body.is_valid(raise_exception=True)
        try:
            copy = service.copy(storage, key=body.validated_data["key"], name=body.validated_data["name"])
        except service.ActionError as err:
            return _error(err)
        return Response(storage_data(copy, detail=True), status=status.HTTP_201_CREATED)


class StorageRotateView(AdminView):
    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        service.rotate_link_secret(storage)
        return Response(status=status.HTTP_204_NO_CONTENT)


class StorageSortView(AdminView):
    def post(self, request):
        ids = request.data.get("ids") if isinstance(request.data, dict) else None
        try:
            service.sort(ids)
        except service.ActionError as err:
            return _error(err)
        return Response([storage_data(storage) for storage in StorageSource.objects.order_by("position", "id")])


class KeyCheckView(AdminView):
    """GET ?key=&id=: whether a key is free (the form checks while typing)."""

    def get(self, request):
        key = str(request.query_params.get("key", "")).strip().lower()
        raw_id = request.query_params.get("id")
        storage_id = int(raw_id) if raw_id and raw_id.isascii() and raw_id.isdigit() else None
        problem = key_problem(key, storage_id=storage_id)
        return Response({"key": key, "ok": not problem, "message": problem})


class FormTestView(AdminView):
    def post(self, request):
        try:
            return Response(service.test_form(request, request.data))
        except service.ActionError as err:
            return _error(err)


class HelperView(AdminView):
    def post(self, request):
        try:
            return Response(service.run_helper(request, request.data))
        except service.ActionError as err:
            return _error(err)


class OAuthStartView(AdminView):
    def post(self, request):
        try:
            return Response(service.start_oauth(request, request.data))
        except service.ActionError as err:
            return _error(err)


class DraftView(AdminView):
    def get(self, request, draft_id):
        draft = drafts.find(request, draft_id)
        if draft is None:
            return Response({"detail": _("This draft has expired.")}, status=status.HTTP_404_NOT_FOUND)
        return Response(drafts.shown(draft))

    def delete(self, request, draft_id):
        drafts.drop(request, draft_id)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CheckPathView(AdminView):
    """POST {"path"}: does this folder exist in the storage? For root folders of grants: only a warning."""

    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        raw = request.data.get("path") if isinstance(request.data, dict) else None
        try:
            path = grants.clean_root(raw)
        except Exception as error:  # noqa: BLE001 - ValidationError
            return Response({"path": [getattr(error, "messages", [str(error)])[0]]}, status=status.HTTP_400_BAD_REQUEST)
        if not storage.enabled:
            return Response({"path": path, "checked": False, "message": _("The storage source is disabled, so it wasn't checked.")})
        try:
            entry = instances.get(storage).stat(path)
        except NotFound:
            return Response({"path": path, "checked": True, "exists": False,
                             "message": _('The storage source "%(name)s" has no folder %(path)s: users opening it will get an error.') % {"name": storage.name, "path": path}})
        except (DriverError, instances.StorageUnavailable) as error:
            return Response({"path": path, "checked": False, "message": _("Couldn't check: %(error)s") % {"error": getattr(error, 'message', error)}})
        if not entry.is_dir:
            return Response({"path": path, "checked": True, "exists": True, "is_dir": False,
                             "message": _("%(path)s in the storage source \"%(name)s\" isn't a folder.") % {"name": storage.name, "path": path}})
        return Response({"path": path, "checked": True, "exists": True, "is_dir": True, "message": ""})


# --- rules ---------------------------------------------------------------------------------------------

RULE_MODELS = {"filters": FilterRule, "passwords": FolderPassword, "readmes": ReadmeRule}
RULE_FIELDS = {
    "filters": ("expression", "description", "mode"),
    "passwords": ("expression", "description", "password"),
    "readmes": ("expression", "description", "text", "path_mode", "display"),
}


def rule_data(kind: str, rule) -> dict:
    return {"id": rule.pk, **{name: getattr(rule, name) for name in RULE_FIELDS[kind]}}


class RulesView(AdminView):
    """GET the rules of one kind in order; PUT the whole list. A rule sent with its id is changed in
    place (a folder password keeps what visitors unlocked unless its expression or password changes);
    the others are new; rules left out are deleted."""

    kind = ""

    def get(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        model = RULE_MODELS[self.kind]
        return Response([rule_data(self.kind, rule) for rule in model.objects.filter(storage=storage)])

    def put(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        body = rule_list(self.kind, request.data)
        body.is_valid(raise_exception=True)
        model = RULE_MODELS[self.kind]
        fields = RULE_FIELDS[self.kind]
        with transaction.atomic():
            StorageSource.objects.select_for_update().filter(pk=storage.pk).first()
            existing = {rule.pk: rule for rule in model.objects.filter(storage=storage)}
            keep = set()
            for position, item in enumerate(body.validated_data, start=1):
                rule = existing.get(item.get("id")) if item.get("id") else None
                if rule is None:
                    rule = model(storage=storage)
                elif self.kind == "passwords" and (rule.expression != item["expression"] or rule.password != item["password"]):
                    rule.revision += 1
                rule.position = position
                for name in fields:
                    setattr(rule, name, item[name])
                rule.save()
                keep.add(rule.pk)
            model.objects.filter(storage=storage).exclude(pk__in=keep).delete()
        return Response([rule_data(self.kind, rule) for rule in model.objects.filter(storage=storage)])


class FiltersView(RulesView):
    kind = "filters"


class PasswordsView(RulesView):
    kind = "passwords"


class ReadmesView(RulesView):
    kind = "readmes"


class _TesterSerializer(serializers.Serializer):
    kind = serializers.ChoiceField(choices=["filters", "passwords", "readmes"])
    rules = serializers.JSONField(required=False)
    path = StringField(max_length=4096, allow_blank=True, trim_whitespace=False)
    root = StringField(max_length=4096, allow_blank=True, default="/", trim_whitespace=False)


class _Rule:
    """A rule of the tester's list that isn't saved: what the matcher needs of it."""

    def __init__(self, index, item):
        self.pk = index
        self.position = index
        self.expression = item["expression"]
        self.mode = item.get("mode")
        self.path_mode = item.get("path_mode")


class RuleTestView(AdminView):
    """POST {"kind", "rules" (the page's list, saved or not; default: the saved ones), "path", "root"}:
    which rules match the storage path, and what they make of it (decision D9's examples)."""

    def post(self, request, pk):
        storage = get_object_or_404(StorageSource, pk=pk)
        body = _TesterSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        kind = body.validated_data["kind"]
        cleaned, errors = {}, {}
        for name in ("path", "root"):  # each its own errors: the root's were the path's (API-15)
            try:
                cleaned[name] = paths.clean(body.validated_data[name])
            except InvalidPath as error:
                errors[name] = [error.message]
        if errors:
            return Response(errors, status=status.HTTP_400_BAD_REQUEST)
        path, root = cleaned["path"], cleaned["root"]
        if "rules" in body.validated_data:
            items = rule_list(kind, body.validated_data["rules"])
            items.is_valid(raise_exception=True)
            rules = [_Rule(index, item) for index, item in enumerate(items.validated_data)]
        else:
            rules = [_Rule(index, rule_data(kind, rule)) for index, rule in
                     enumerate(RULE_MODELS[kind].objects.filter(storage=storage))]
        compiled = [(rule, compile_glob(rule.expression)) for rule in rules]
        folded = canonical(path)
        result: dict = {"path": path, "canonical": folded, "matches": []}
        for rule, glob in compiled:
            if glob.matches(folded):
                result["matches"].append({"index": rule.pk, "how": "self"})
            elif glob.covers(folded):
                result["matches"].append({"index": rule.pk, "how": "below"})
        if kind == "filters":
            result.update(policy.filter_state(policy.StorageRules(filters=rules), path))
        elif kind == "passwords":
            decider = next((rule.pk for rule, glob in compiled if glob.covers(folded)), None)
            result["decides"] = decider
        else:
            relative = paths.relative(path, root) if paths.is_inside(path, root) else None
            chosen = None
            for rule, glob in compiled:
                target = folded if rule.path_mode == "absolute" else (canonical(relative) if relative else None)
                if target is not None and glob.matches(target):
                    chosen = rule.pk
                    break
            result["relative"] = relative
            result["decides"] = chosen
        return Response(result)


# --- grants --------------------------------------------------------------------------------------------

class UserGrantsView(AdminView):
    """Contract 1: GET one row per storage, in display order; PUT the same list to replace the user's
    grants (storages left out of the list keep theirs; `granted: false` removes one)."""

    def _user(self, user_id):
        return get_object_or_404(get_user_model(), pk=user_id)

    def get(self, request, user_id):
        return Response(grants.rows_for(self._user(user_id)))

    def put(self, request, user_id):
        user = self._user(user_id)
        # As for the user himself (apps/accounts/managing.py): another admin's grants are for superusers.
        if reason := managing.manager_refusal(request.user, user):
            raise PermissionDenied(reason)
        body = grant_list(request.data)
        body.is_valid(raise_exception=True)
        with transaction.atomic():
            existing = {grant.storage_id: grant for grant in Grant.objects.select_for_update().filter(user=user)}
            for item in body.validated_data:
                grant = existing.get(item["storage"])
                if not item["granted"]:
                    if grant is not None:
                        grant.delete()
                    continue
                grant = grant or Grant(user=user, storage_id=item["storage"])
                grant.enabled = item["enabled"]
                grant.root_path = item["root_path"]
                grant.permissions = item["permissions"]
                grant.save()
        return Response(grants.rows_for(user))


class GrantOptionsView(AdminView):
    """The permission codes with their labels, help and what each needs first (`requires`: one of these,
    D55; apps/accounts/permissions.py), and with ?user= that user's default permissions, for the grants
    editor."""

    def get(self, request):
        answer = {"permissions": [{"code": code, "label": Permission(code).label, "help": help_for(code),
                                   "requires": list(REQUIRES.get(code, ()))} for code in ALL]}
        raw = request.query_params.get("user")
        if raw and raw.isascii() and raw.isdigit():
            user = get_user_model().objects.filter(pk=int(raw)).first()
            if user is not None:
                answer["default_permissions"] = usable(user.default_permissions or [])
        return Response(answer)


# --- for everybody -------------------------------------------------------------------------------------

class PublicStoragesView(APIView):
    """GET: the storages the caller may use (logged in: his grants; else the guest's), in order, with
    what the browser needs. No params, no secrets, no root folders."""

    permission_classes = [AllowAny]

    def get(self, request):
        actor = policy.actor_for(request)
        answer = []
        for storage, grant in policy.visible_storages(actor):
            answer.append({
                "key": storage.key, "name": storage.name, "type": storage.type, "type_label": storage.type_label,
                "gallery_default": storage.gallery_default,
                "capabilities": instances.capabilities(storage),
                "permissions": usable(grant.permissions or []),
            })
        return Response(answer)


class _UnlockSerializer(serializers.Serializer):
    path = StringField(max_length=4096, allow_blank=True, required=False, trim_whitespace=False)
    rule = serializers.IntegerField(required=False, min_value=1)
    password = StringField(max_length=1000, allow_blank=True, trim_whitespace=False)
    remember = serializers.BooleanField(default=False)


class UnlockView(APIView):
    """POST {"path" | "rule", "password", "remember"}: unlock a folder password for this browser (in the
    session; 记住密码 also for 30 days in a cookie). 200 {"unlocked", "rule"}, or 403 password_wrong,
    404 rule_not_found, 429 password_throttled."""

    permission_classes = [AllowAny]

    def post(self, request, key):
        enforce_csrf(request)
        body = _UnlockSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        access = policy.open_storage(request._request, key)
        data = body.validated_data
        if not data["password"]:
            return Response({"password": [_("Please enter the password.")]}, status=status.HTTP_400_BAD_REQUEST)
        if "rule" in data:
            rule = access.unlock(password=data["password"], rule_id=data["rule"], remember=data["remember"])
        else:
            rule = access.unlock(access.path(data.get("path")), password=data["password"], remember=data["remember"])
        response = Response({"unlocked": rule is not None, "rule": rule.pk if rule is not None else None})
        access.keys.write(response)
        return response


class UnlocksView(APIView):
    """DELETE: 清理密码文件夹缓存, everything this browser unlocked."""

    permission_classes = [AllowAny]

    def delete(self, request):
        enforce_csrf(request)
        keys = policy.FolderKeys(request._request)
        keys.forget()
        response = Response(status=status.HTTP_204_NO_CONTENT)
        keys.write(response)
        return response


# --- the OAuth callback page ---------------------------------------------------------------------------

@never_cache
def oauth_callback(request):
    """/storages/oauth/callback/: where the provider sends the admin back. The state names a flow of
    this session (drafts.take_oauth); the tokens go into the form's draft and never into the page."""
    context = {"ok": False, "message": "", "back": "/manage/#storages"}
    if not is_admin(request.user):
        context["message"] = _("Please log in with an admin account first, then go back to the storage source's settings and click \"Authorize\" again.")
        return render(request, "storages/oauth_callback.html", context, status=403)
    flow = drafts.take_oauth(request, request.GET.get("state"))
    if flow is None:
        context["message"] = _("This authorization has expired, or wasn't started from this browser: please "
                               "go back to the storage source's settings and click \"Authorize\" again.")
        return render(request, "storages/oauth_callback.html", context, status=400)
    # Back to the form, or to the add wizard it was started from (#storages/<id>/setup saves the tokens).
    to_setup = bool(flow.get("setup") and flow.get("storage"))
    page = f"{flow['storage']}/setup" if to_setup else str(flow["storage"] or "new")
    back = f"/manage/#storages/{page}?draft={flow['draft']}"
    context["back"] = back
    context["back_label"] = _("Back to adding the storage source") if to_setup else _("Back to the storage source's settings")
    context["draft"] = context["asked"] = flow["draft"]
    if request.GET.get("error"):
        reason = single_line(request.GET.get("error_description") or request.GET.get("error") or "")[:300]
        context["message"] = _("Authorization didn't finish: %(reason)s") % {"reason": reason}
        return render(request, "storages/oauth_callback.html", context, status=400)
    code = request.GET.get("code")
    if not code or len(code) > 4096:
        context["message"] = _("The provider gave no authorization code. Please authorize again.")
        return render(request, "storages/oauth_callback.html", context, status=400)
    try:
        result = service.finish_oauth(request, flow, code)
    except DriverError as error:
        context["message"] = _("Getting the token failed: %(message)s") % {"message": error.message}
        return render(request, "storages/oauth_callback.html", context, status=400)
    # The draft the form named may have expired meanwhile: the tokens are then in a new one.
    draft_id = result["draft"]["id"]
    message = (_("Authorized. Go back to the page adding the storage source; the token is saved for you.") if to_setup
               else _("Authorized: the token is filled into the form. Go back to the storage source's settings and click \"Save\" to apply it."))
    context.update(ok=True, message=message, draft=draft_id, back=f"/manage/#storages/{page}?draft={draft_id}")
    return render(request, "storages/oauth_callback.html", context)
