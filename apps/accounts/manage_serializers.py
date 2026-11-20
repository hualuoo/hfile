"""What /manage/ sends and gets about users (apps/accounts/manage_views.py). Admins only."""

from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext_lazy
from rest_framework import serializers

from apps.core.fields import BoundedListField, StringField, SwitchField
from apps.core.query import ChoiceParam, DateParam, IdParam, TextParam

from . import loginlog, managing
from .models import BUILTIN_NICKNAMES, Kind, LoginLog, User
from .permissions import check_requires, clean_permissions, usable
from .serializers import PASSWORD_MAX
from .validators import check_username, clean_nickname

USERNAME_INPUT_MAX = 300  # before NFKC and trimming; check_username has the real limit
NICKNAME_INPUT_MAX = 200  # before whitespace is collapsed


def _messages(exc):
    return list(exc.messages)


class PermissionsField(serializers.Field):
    """A list of permission codes (apps/accounts/permissions.py), kept without duplicates in their order; a
    code without what it needs first is refused (D55), and left out of a list saved before."""

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError(_("Permissions should be a list."))
        if len(data) > 100:
            raise serializers.ValidationError(_("The permission list is too long."))
        try:
            codes = clean_permissions(data)
            check_requires(codes)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(_messages(exc)) from None
        return codes

    def to_representation(self, value):
        return usable(value)


ROLE_LABELS = {"superuser": gettext_lazy("Superuser"), "staff": gettext_lazy("Admin"), "user": gettext_lazy("Regular user"), "guest": gettext_lazy("Anonymous user"), "template": gettext_lazy("New user template")}


def role_of(user):
    if user.kind != Kind.NORMAL:
        return user.kind
    if user.is_superuser:
        return "superuser"
    return "staff" if user.is_staff else "user"


class ManagedUserSerializer(serializers.ModelSerializer):
    """A user as the list and the edit dialog show them. `can` says what the page may offer the
    admin looking at it, `why` why not (managing.rights)."""

    nickname = serializers.SerializerMethodField()
    kind_label = StringField(source="get_kind_display", read_only=True)
    role = serializers.SerializerMethodField()
    role_label = serializers.SerializerMethodField()
    has_password = serializers.SerializerMethodField()
    totp_enabled = serializers.BooleanField(read_only=True)
    default_permissions = PermissionsField(read_only=True)
    can = serializers.SerializerMethodField()
    why = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = [
            "id", "username", "nickname", "kind", "kind_label", "role", "role_label", "is_active", "is_staff",
            "is_superuser", "has_password", "totp_enabled", "default_permissions", "date_joined", "last_login",
            "last_login_ip", "can", "why",
        ]
        read_only_fields = fields

    def get_nickname(self, obj):
        return _(BUILTIN_NICKNAMES[obj.kind]) if obj.kind in BUILTIN_NICKNAMES else obj.nickname

    def get_role(self, obj):
        return role_of(obj)

    def get_role_label(self, obj):
        return ROLE_LABELS[role_of(obj)]

    def get_has_password(self, obj):
        return obj.has_usable_password()

    def _rights(self, obj):
        cache = self.context.setdefault("_rights", {})
        if obj.pk not in cache:
            cache[obj.pk] = managing.rights(self.context["request"].user, obj, admins=self.context.get("admins"))
        return cache[obj.pk]

    def get_can(self, obj):
        return {action: not reason for action, reason in self._rights(obj).items()}

    def get_why(self, obj):
        return {action: reason for action, reason in self._rights(obj).items() if reason}


class UserFormSerializer(serializers.Serializer):
    """Creating a user (POST) or changing one (PATCH, every field optional). A password "" means
    none (creating) or unchanged (changing). The rules of who may change what are managing's."""

    username = StringField(
        max_length=USERNAME_INPUT_MAX,
        error_messages={"required": gettext_lazy("A username is required."), "blank": gettext_lazy("The username can't be empty."), "max_length": gettext_lazy("The username is too long.")},
    )
    nickname = StringField(
        required=False, allow_blank=True, max_length=NICKNAME_INPUT_MAX, trim_whitespace=False,
        error_messages={"max_length": gettext_lazy("The nickname is too long.")},
    )
    password = StringField(
        required=False, allow_blank=True, write_only=True, trim_whitespace=False, max_length=PASSWORD_MAX,
        error_messages={"max_length": ngettext_lazy("The password is at most {max_length} character.", "The password is at most {max_length} characters.", "max_length")},
    )
    is_active = SwitchField(required=False)
    default_permissions = PermissionsField(required=False)

    def validate_username(self, value):
        if self.instance is not None and value == self.instance.username:
            return value  # unchanged: also a built-in's, or a name the Django admin allowed
        username = User.normalize_username(value.strip())
        try:
            check_username(username)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(_messages(exc)) from None
        if User.objects.username_taken(username, exclude=self.instance.pk if self.instance is not None else None):
            raise serializers.ValidationError(managing.USERNAME_TAKEN)
        return username

    def validate_nickname(self, value):
        try:
            return clean_nickname(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(_messages(exc)) from None

    def validate(self, attrs):
        password = attrs.get("password")
        if password:
            # Checked against the names the account will have (UserAttributeSimilarityValidator).
            base = self.instance
            candidate = User(
                username=attrs.get("username", base.username if base else ""),
                nickname=attrs.get("nickname", base.nickname if base else ""),
                email=base.email if base else "",
            )
            try:
                password_validation.validate_password(password, candidate)
            except DjangoValidationError as exc:
                raise serializers.ValidationError({"password": _messages(exc)}) from None
        return attrs


class CopyUserSerializer(UserFormSerializer):
    """POST manage/users/{id}/copy/: the new user's username, and maybe a nickname and a password."""

    is_active = None
    default_permissions = None


class UserListQuery(serializers.Serializer):
    """GET manage/users/: ?search= (username, nickname or id), ?is_active=, ?kind=. ?ordering= is apps.core.filters'."""

    search = TextParam()
    is_active = ChoiceParam(["true", "false"])
    kind = ChoiceParam(Kind.values)


# ------------------------------------------------------------ the login log (/manage/#login-log)


class LoginLogSerializer(serializers.ModelSerializer):
    """One entry. `user` is the account the typed name matched, while it exists."""

    result_label = StringField(source="get_result_display", read_only=True)
    reason_label = serializers.SerializerMethodField()
    source_label = serializers.SerializerMethodField()
    user = serializers.SerializerMethodField()

    class Meta:
        model = LoginLog
        fields = [
            "id", "created_at", "username", "user", "ip", "user_agent", "result", "result_label", "reason",
            "reason_label", "source", "source_label",
        ]
        read_only_fields = fields

    def get_reason_label(self, obj):
        return loginlog.REASONS.get(obj.reason, obj.reason)

    def get_source_label(self, obj):
        return loginlog.SOURCES.get(obj.source, obj.source)

    def get_user(self, obj):
        user = obj.user
        return {"id": user.pk, "username": user.username, "nickname": user.nickname} if user else None


IP_START = "0123456789abcdefABCDEF.:"


class LoginLogQuery(serializers.Serializer):
    """GET and DELETE manage/login-logs/: ?username= (part of it), ?ip= (its start), ?result=,
    ?since= and ?until= (days, both included, in the site's time zone); DELETE also ?newest= (the newest
    id the list showed: what was logged after it stays, API-10)."""

    username = TextParam(max_length=150)
    ip = TextParam(max_length=64)
    result = ChoiceParam(LoginLog.Result.choices)
    since = DateParam()
    until = DateParam()
    newest = IdParam(gettext_lazy("record"))

    def validate_ip(self, value):
        if value and any(char not in IP_START for char in value):
            raise serializers.ValidationError(_("Write the start of an IP address, like 192.168. or 2001:db8:."))
        return value.lower()

    def validate(self, attrs):
        if attrs["since"] and attrs["until"] and attrs["since"] > attrs["until"]:
            raise serializers.ValidationError({"until": [_("The end date can't be before the start date.")]})
        return attrs


class LoginLogIdsSerializer(serializers.Serializer):
    """POST manage/login-logs/delete/: {"ids": [...]}, the entries ticked on the page."""

    ids = BoundedListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1),
        allow_empty=False, max_length=1000,
        error_messages={"empty": gettext_lazy("No records were selected to delete."), "max_length": gettext_lazy("At most 1000 can be deleted at a time.")},
    )
