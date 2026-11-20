"""What the sso API sends and takes (views.py)."""

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.text import format_lazy
from django.utils.translation import gettext as _, gettext_lazy
from rest_framework import serializers

from apps.core import origin, secrets
from apps.core.fields import STRING_FIELD_MAPPING, BoundedListField, StringField, SwitchField

from . import validation
from .flow import callback_path
from .models import Identity, Provider, TokenAuth

ORDER_MIN, ORDER_MAX = -1_000_000, 1_000_000


class PublicProviderSerializer(serializers.ModelSerializer):
    """A login button: GET /api/v1/sso/providers/ (anybody)."""

    class Meta:
        model = Provider
        fields = ["code", "name", "icon"]
        read_only_fields = fields


def _text(max_length, *, required=True, blank=False, label="", **kwargs):
    """A text field with its messages (lazy: it is called while the serializers are made, at import); the
    clean_* of validation.py do the rest."""
    return StringField(
        required=required, allow_blank=blank, trim_whitespace=False, max_length=max_length, **kwargs,
        error_messages={
            "required": format_lazy(gettext_lazy("{label} is required."), label=label), "blank": format_lazy(gettext_lazy("{label} can't be empty."), label=label),
            "max_length": format_lazy(gettext_lazy("{label} is too long."), label=label), "invalid": format_lazy(gettext_lazy("{label} should be text."), label=label),
            "null": format_lazy(gettext_lazy("{label} can't be empty."), label=label),
        },
    )


class ProviderSerializer(serializers.ModelSerializer):
    """A provider as /manage/#sso edits it. `client_secret` is write-only: GET says whether one is set
    (`client_secret_set`, and `client_secret_unreadable` when the secret_key it was encrypted with is
    gone); a blank one (or none) leaves it as it is, null clears it. `callback_url` is the address to
    register at the provider; `identities` how many users bound an account of it."""

    serializer_field_mapping = STRING_FIELD_MAPPING  # text columns take JSON strings only (API-07)

    code = _text(64, label=gettext_lazy("Short name"))
    name = _text(200, label=gettext_lazy("Name"))
    icon = _text(validation.ICON_MAX + 1000, required=False, blank=True, label=gettext_lazy("Icon"))
    client_id = _text(1000, label="Client ID")
    client_secret = _text(validation.SECRET_MAX + 1000, required=False, blank=True, label="Client Secret", write_only=True,
                          allow_null=True)
    auth_url = _text(validation.URL_MAX + 100, label=gettext_lazy("Authorization endpoint"))
    token_url = _text(validation.URL_MAX + 100, label=gettext_lazy("Token endpoint"))
    userinfo_url = _text(validation.URL_MAX + 100, label=gettext_lazy("Userinfo endpoint"))
    scope = _text(validation.SCOPE_MAX + 500, required=False, blank=True, label="Scope")
    token_auth = serializers.ChoiceField(
        choices=TokenAuth.choices, required=False,
        error_messages={"invalid_choice": gettext_lazy("How the Client Secret is sent can only be post or basic.")},
    )
    id_field = _text(500, label=gettext_lazy("User ID field"))
    username_field = _text(500, required=False, blank=True, label=gettext_lazy("Username field"))
    nickname_field = _text(500, required=False, blank=True, label=gettext_lazy("Nickname field"))
    enabled = SwitchField(required=False, error_messages={"invalid": gettext_lazy("Enabled should be true or false.")})
    order = serializers.IntegerField(
        required=False, min_value=ORDER_MIN, max_value=ORDER_MAX,
        error_messages={"invalid": gettext_lazy("The order should be an integer."), "min_value": gettext_lazy("The order is at least {min_value}."), "max_value": gettext_lazy("The order is at most {max_value}.")},
    )
    client_secret_set = serializers.SerializerMethodField()
    client_secret_unreadable = serializers.SerializerMethodField()
    callback_url = serializers.SerializerMethodField()
    identities = serializers.SerializerMethodField()

    class Meta:
        model = Provider
        fields = [
            "id", "code", "name", "icon", "client_id", "client_secret", "client_secret_set", "client_secret_unreadable",
            "auth_url", "token_url", "userinfo_url", "scope", "token_auth", "id_field", "username_field",
            "nickname_field", "enabled", "order", "callback_url", "identities", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    # --- answers

    def get_client_secret_set(self, obj):
        return secrets.is_set(obj.client_secret)

    def get_client_secret_unreadable(self, obj):
        try:
            obj.secret()
        except secrets.UnreadableSecret:
            return True
        return False

    def get_callback_url(self, obj):
        request = self.context.get("request")
        try:
            return origin.absolute(callback_path(obj), request._request if request is not None else None)
        except ValueError:
            return callback_path(obj)

    def get_identities(self, obj):
        counted = getattr(obj, "identity_count", None)
        return counted if counted is not None else obj.identities.count()

    # --- checks

    @staticmethod
    def _clean(clean, value, *args):
        try:
            return clean(value, *args)
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from None

    def validate_code(self, value):
        code = self._clean(validation.clean_code, value)
        others = Provider.objects.filter(code=code)
        if self.instance is not None:
            others = others.exclude(pk=self.instance.pk)
        if others.exists():
            raise serializers.ValidationError(_("This short name is taken. Please choose another."))
        return code

    def validate_name(self, value):
        return self._clean(validation.clean_name, value)

    def validate_icon(self, value):
        return self._clean(validation.clean_icon, value)

    def validate_client_id(self, value):
        return self._clean(validation.clean_client_id, value)

    def validate_client_secret(self, value):
        return None if value is None else self._clean(validation.clean_secret, value)

    def validate_auth_url(self, value):
        return self._clean(validation.clean_url, value, _("Authorization endpoint"))

    def validate_token_url(self, value):
        return self._clean(validation.clean_url, value, _("Token endpoint"))

    def validate_userinfo_url(self, value):
        return self._clean(validation.clean_url, value, _("Userinfo endpoint"))

    def validate_scope(self, value):
        return self._clean(validation.clean_scope, value)

    def validate_id_field(self, value):
        return self._clean(validation.clean_path, value)

    def validate_username_field(self, value):
        return self._clean(validation.clean_path, value) if value.strip() else ""

    def validate_nickname_field(self, value):
        return self._clean(validation.clean_path, value) if value.strip() else ""

    def _save(self, provider, validated):
        secret = validated.pop("client_secret", "")
        for name, value in validated.items():
            setattr(provider, name, value)
        if secret is None:  # null: no secret from now on, a public client (ACCT-05)
            provider.set_secret("")
        elif secret:
            provider.set_secret(secret)
        provider.save()
        return provider

    def create(self, validated_data):
        return self._save(Provider(), validated_data)

    def update(self, instance, validated_data):
        return self._save(instance, validated_data)


class OrderSerializer(serializers.Serializer):
    """POST manage/sso/providers/order/ {"ids": [...]}: every provider's id, in the new order."""

    ids = BoundedListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1), allow_empty=False, max_length=1000,
        error_messages={"empty": gettext_lazy("The IDs of all providers are needed."), "max_length": gettext_lazy("Too many providers."), "not_a_list": gettext_lazy("ids should be a list.")},
    )

    def validate_ids(self, value):
        if len(set(value)) != len(value):
            raise serializers.ValidationError(_("IDs can't repeat."))
        return value


class DiscoverSerializer(serializers.Serializer):
    """POST manage/sso/discover/ {"url"}: an OpenID configuration's address."""

    url = _text(validation.URL_MAX + 100, label=gettext_lazy(".well-known address"))

    def validate_url(self, value):
        try:
            return validation.clean_url(value, _(".well-known address"))
        except DjangoValidationError as exc:
            raise serializers.ValidationError(list(exc.messages)) from None


class IdentitySerializer(serializers.ModelSerializer):
    """A bound account: for the user themselves (账号绑定) and for admins (the user dialog)."""

    provider = serializers.SerializerMethodField()

    class Meta:
        model = Identity
        fields = ["id", "provider", "external_id", "created_at", "last_login_at"]
        read_only_fields = fields

    def get_provider(self, obj):
        provider = obj.provider
        return {"id": provider.pk, "code": provider.code, "name": provider.name, "icon": provider.icon, "enabled": provider.enabled}


class NewIdentitySerializer(serializers.Serializer):
    """POST manage/users/{id}/identities/ {"provider": id, "external_id"}: an admin binds an account."""

    # The id read as an integer: 5.9 was the provider of id 5, and 1e400 (infinity) a 500 (API-08).
    provider = serializers.PrimaryKeyRelatedField(
        queryset=Provider.objects.all(), pk_field=serializers.IntegerField(error_messages={"invalid": gettext_lazy("The provider should be an ID.")}),
        error_messages={"required": gettext_lazy("Please choose a provider."), "does_not_exist": gettext_lazy("There is no such provider."), "incorrect_type": gettext_lazy("The provider should be an ID."),
                        "null": gettext_lazy("Please choose a provider.")},
    )
    external_id = _text(validation.EXTERNAL_ID_MAX + 100, label=gettext_lazy("User ID at the provider"))

    def validate_external_id(self, value):
        try:
            return validation.clean_external_id(value)
        except DjangoValidationError as exc:
            raise serializers.ValidationError([_("User ID %(message)s") % {"message": message} for message in exc.messages]) from None


class LinkSerializer(serializers.Serializer):
    """POST sso/links/{code}/ {"password", "next"}: binding an account of this provider to oneself."""

    password = StringField(
        required=False, allow_blank=True, trim_whitespace=False, max_length=4096, write_only=True,
        error_messages={"max_length": gettext_lazy("The password is too long.")},
    )
    # No length limit: flow.safe_next takes a path longer than NEXT_MAX back to the home page, as
    # /sso/<code>/login does; with one, binding failed on a page whose address was long (ACCT-04).
    next = StringField(required=False, allow_blank=True)
