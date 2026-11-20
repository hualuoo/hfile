import unicodedata

from django.contrib.auth import authenticate, get_user_model, password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext_lazy
from rest_framework import serializers

from apps.core.fields import StringField

User = get_user_model()

# New passwords: at most this long (the installer has the same limit).
PASSWORD_MAX = 128
# A password being checked rather than set (logging in, 原密码) may be longer: one set some other way
# (createsuperuser, the Django admin) still works.
PASSWORD_INPUT_MAX = 4096
LOGIN_FAILED = gettext_lazy("Wrong username or password.")


def _password_field(max_length, **kwargs):
    # Called while the serializers are made (at import): its messages stay lazy, {max_length} filled in by
    # the field when it fails.
    return StringField(
        write_only=True,
        trim_whitespace=False,
        max_length=max_length,
        error_messages={"max_length": ngettext_lazy("The password is at most {max_length} character.", "The password is at most {max_length} characters.", "max_length"),
                        "blank": gettext_lazy("The password can't be empty."), "required": gettext_lazy("A password is required.")},
        style={"input_type": "password"},
        **kwargs,
    )


class UserSerializer(serializers.ModelSerializer):
    """The logged-in user, as GET /api/v1/session/ gives it."""

    class Meta:
        model = User
        fields = ["id", "username", "nickname", "is_staff", "is_superuser", "date_joined", "last_login"]
        read_only_fields = fields


class LoginSerializer(serializers.Serializer):
    username = StringField(
        max_length=User._meta.get_field("username").max_length,
        error_messages={"blank": gettext_lazy("The username can't be empty."), "required": gettext_lazy("A username is required."), "max_length": LOGIN_FAILED},
    )
    password = _password_field(PASSWORD_INPUT_MAX)

    def validate_username(self, value):
        # As usernames are kept (NFKC, User.normalize_username): an input method may type full-width letters.
        return unicodedata.normalize("NFKC", value)

    def validate(self, attrs):
        user = authenticate(self.context.get("request"), username=attrs["username"], password=attrs["password"])
        if user is None:
            raise serializers.ValidationError(LOGIN_FAILED, code="authorization")
        attrs["user"] = user
        return attrs


class PasswordChangeSerializer(serializers.Serializer):
    """修改密码. An account without a usable password (one single sign-on made, or an admin made without
    one) sets its first one without an old password: there is none to type. Every other account needs
    the old one."""

    old_password = _password_field(PASSWORD_INPUT_MAX, required=False, allow_blank=True)
    new_password = _password_field(PASSWORD_MAX)

    def validate_old_password(self, value):
        user = self.context["request"].user
        if user.has_usable_password() and value and not user.check_password(value):
            raise serializers.ValidationError(_("The current password is wrong."))
        return value

    def validate(self, attrs):
        user = self.context["request"].user
        if user.has_usable_password():
            if not attrs.get("old_password"):
                raise serializers.ValidationError({"old_password": [_("The current password is required.")]})
            if attrs["new_password"] == attrs["old_password"]:
                raise serializers.ValidationError({"new_password": [_("The new password can't be the same as the current one.")]})
        try:
            password_validation.validate_password(attrs["new_password"], self.context["request"].user)
        except DjangoValidationError as exc:
            raise serializers.ValidationError({"new_password": list(exc.messages)}) from exc
        return attrs


class TotpConfirmSerializer(serializers.Serializer):
    """Turning two-step verification on: a code from the device just set up, and the password (a
    session left open somewhere must not be enough to lock the owner out with another device)."""

    code = StringField(max_length=32, error_messages={"required": gettext_lazy("Enter the 6 digits shown on your phone."), "blank": gettext_lazy("Enter the 6 digits shown on your phone.")})
    password = _password_field(PASSWORD_INPUT_MAX)

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(_("Wrong password."))
        return value


class TotpDisableSerializer(serializers.Serializer):
    password = _password_field(PASSWORD_INPUT_MAX)

    def validate_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError(_("Wrong password."))
        return value
