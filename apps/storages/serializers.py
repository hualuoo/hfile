"""Checks of what the admin pages send: a storage's own fields, its rules, a user's grants."""

from __future__ import annotations

import urllib.parse

from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext, ngettext_lazy
from rest_framework import serializers
from rest_framework.settings import api_settings

from apps.accounts.permissions import check_requires, clean_permissions
from apps.core import reserved
from apps.core.fields import StringField
from apps.core.query import ID_MAX
from apps.core.text import multi_line, reject_invisible, single_line

from . import config as storage_config
from . import icons as storage_icons
from .grants import clean_root
from .models import (
    DESCRIPTION_MAX,
    DOMAIN_MAX,
    FOLDER_PASSWORD_MAX,
    KEY_MAX,
    KEY_RE,
    LINK_LIFETIME_MAX,
    LINK_LIFETIME_MIN,
    NAME_MAX,
    PROXY_CONNECTIONS_DEFAULT,
    PROXY_CONNECTIONS_MAX,
    PROXY_CONNECTIONS_MIN,
    README_MAX,
    REMARK_MAX,
    FilterMode,
    ReadmeDisplay,
    ReadmePathMode,
    StorageSource,
    WhenFull,
)
from .rules import GlobError, normalize_expression

RULES_MAX = 200  # rules of one kind per storage
GRANTS_MAX = 10_000  # rows of one PUT of a user's grants: one per storage


def key_taken(name: str) -> str:
    """apps.core.reserved's check for storage keys (registered in apps.py): why `name` is taken, or ""."""
    from django.core.exceptions import ImproperlyConfigured
    from django.db import DatabaseError

    try:
        storage = StorageSource.objects.filter(key=name.lower()).only("name").first()
    except (DatabaseError, ImproperlyConfigured):  # no tables yet, or the installer's dummy database
        return ""
    if storage is None:
        return ""
    return _('/%(name)s/ is the address of the storage source "%(storage)s". Please choose another.') % {"name": name, "storage": storage.name}


def key_problem(key: str, *, storage_id: int | None = None) -> str:
    """What's wrong with a storage key; "" when nothing is."""
    if not key:
        return _("Please fill in the alias.")
    if len(key) > KEY_MAX:
        return ngettext("The alias is at most %(limit)s character.", "The alias is at most %(limit)s characters.", KEY_MAX) % {"limit": KEY_MAX}
    if not KEY_RE.fullmatch(key):
        return _("The alias can only have lowercase letters, digits, - and _.")
    problem = reserved.problem(key, own=key_taken)
    if problem:
        return problem
    if StorageSource.objects.filter(key=key).exclude(pk=storage_id).exists():
        return _("Another storage source has this alias. Please choose another.")
    return ""


def clean_domain(value: str) -> str:
    """A download domain: "" or an http(s) address, without a query; no trailing "/"."""
    text = (value or "").strip()
    if not text:
        return ""
    if len(text) > DOMAIN_MAX:
        raise serializers.ValidationError(ngettext("At most %(limit)s character.", "At most %(limit)s characters.", DOMAIN_MAX) % {"limit": DOMAIN_MAX})
    try:
        split = urllib.parse.urlsplit(text)
        port = split.port
    except ValueError:
        raise serializers.ValidationError(_("Invalid address.")) from None
    if split.scheme not in ("http", "https") or not split.hostname:
        raise serializers.ValidationError(_("Write it as https://domain, starting with http:// or https://."))
    if split.username or split.password or split.query or split.fragment:
        raise serializers.ValidationError(_("The address can't have a username, password, ? or #."))
    if any(ch.isspace() for ch in text):
        raise serializers.ValidationError(_("The address can't contain spaces."))
    del port
    return text.rstrip("/")


class StorageFieldsSerializer(serializers.Serializer):
    """A storage's own fields (the driver's params are checked by apps/storages/config.py)."""

    key = StringField(max_length=200, allow_blank=True, trim_whitespace=True)
    name = StringField(max_length=200, allow_blank=True, trim_whitespace=True)
    type = StringField(max_length=40)
    enabled = serializers.BooleanField(default=True)
    remark = StringField(allow_blank=True, max_length=REMARK_MAX * 2, default="", trim_whitespace=False)
    download_domain = StringField(allow_blank=True, max_length=DOMAIN_MAX * 2, default="")
    proxy_download = serializers.BooleanField(default=False)
    proxy_connections = serializers.IntegerField(
        default=PROXY_CONNECTIONS_DEFAULT, min_value=PROXY_CONNECTIONS_MIN, max_value=PROXY_CONNECTIONS_MAX,
        error_messages={"min_value": ngettext_lazy("At least {min_value} connection.", "At least {min_value} connections.", "min_value"),
                        "max_value": ngettext_lazy("At most {max_value} connection.", "At most {max_value} connections.", "max_value"),
                        "invalid": gettext_lazy("Please enter an integer.")},
    )
    proxy_when_full = serializers.ChoiceField(choices=WhenFull.choices, default=WhenFull.REFUSE)
    proxy_upload = serializers.BooleanField(default=False)
    force_download = serializers.BooleanField(default=True)
    link_lifetime = serializers.IntegerField(
        default=1800, min_value=LINK_LIFETIME_MIN, max_value=LINK_LIFETIME_MAX,
        error_messages={"min_value": ngettext_lazy("Can't be less than {min_value} second.", "Can't be less than {min_value} seconds.", "min_value"), "max_value": gettext_lazy("At most 7 days (604800 seconds)."),
                        "invalid": gettext_lazy("Please enter an integer (seconds).")},
    )
    permanent_links = serializers.BooleanField(default=False)
    gallery_default = serializers.BooleanField(default=False)
    readme_from_file = serializers.BooleanField(default=False)
    icon = serializers.ChoiceField(choices=list(storage_icons.ICONS), default=storage_icons.DEFAULT,
                                   error_messages={"invalid_choice": gettext_lazy("Please choose one of the icons.")})
    show_in_nav = serializers.BooleanField(default=False)

    def __init__(self, *args, storage: StorageSource | None = None, **kwargs):
        self.storage = storage
        super().__init__(*args, **kwargs)

    def validate_key(self, value):
        key = value.strip().lower()
        problem = key_problem(key, storage_id=self.storage.pk if self.storage else None)
        if problem:
            raise serializers.ValidationError(problem)
        return key

    def validate_name(self, value):
        name = single_line(value)
        if not name:
            raise serializers.ValidationError(_("Please fill in the name."))
        if len(name) > NAME_MAX:
            raise serializers.ValidationError(ngettext("The name is at most %(limit)s character.", "The name is at most %(limit)s characters.", NAME_MAX) % {"limit": NAME_MAX})
        try:
            reject_invisible(name, _("Name"))
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages[0]) from None
        return name

    def validate_type(self, value):
        if self.storage is not None and value != self.storage.type:
            raise serializers.ValidationError(_("A storage source's type can't change once created: its settings and paths mean nothing for another type. To change the type, create a new storage source."))
        if not storage_config.known_type(value):
            raise serializers.ValidationError(_("Unknown storage type."))
        return value

    def validate_remark(self, value):
        text = multi_line(value)
        if len(text) > REMARK_MAX:
            raise serializers.ValidationError(ngettext("A remark is at most %(limit)s character.", "A remark is at most %(limit)s characters.", REMARK_MAX) % {"limit": REMARK_MAX})
        try:
            reject_invisible(text, _("Remark"), allowed="\n")
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages[0]) from None
        return text

    def validate_download_domain(self, value):
        return clean_domain(value)

    def validate(self, attrs):
        type_ = attrs.get("type") or (self.storage.type if self.storage else None)
        if type_ and attrs.get("download_domain") and type_ not in storage_config.DOWNLOAD_DOMAIN_TYPES:
            raise serializers.ValidationError({"download_domain": _("This kind of storage has its own download addresses; no download domain is needed.")})
        return attrs


def _rule_expression(value) -> str:
    try:
        return normalize_expression(value)
    except GlobError as error:
        raise serializers.ValidationError(str(error)) from None


def _description(value) -> str:
    text = single_line(value or "")
    if len(text) > DESCRIPTION_MAX:
        raise serializers.ValidationError(ngettext("A description is at most %(limit)s character.", "A description is at most %(limit)s characters.", DESCRIPTION_MAX) % {"limit": DESCRIPTION_MAX})
    return text


class _RuleSerializer(serializers.Serializer):
    id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    expression = StringField(max_length=2000, allow_blank=True, trim_whitespace=False,
                                       error_messages={"blank": gettext_lazy("Please fill in the expression."), "required": gettext_lazy("Please fill in the expression.")})
    description = StringField(max_length=2000, allow_blank=True, default="")

    def validate_expression(self, value):
        return _rule_expression(value)

    def validate_description(self, value):
        return _description(value)


class FilterRuleSerializer(_RuleSerializer):
    mode = serializers.ChoiceField(choices=FilterMode.choices, default=FilterMode.HIDDEN,
                                   error_messages={"invalid_choice": gettext_lazy("The mode can only be hide only, hide and block access, or hide and block downloads.")})


class FolderPasswordSerializer(_RuleSerializer):
    password = StringField(max_length=2000, allow_blank=True, trim_whitespace=False,
                                     error_messages={"blank": gettext_lazy("Please fill in the password."), "required": gettext_lazy("Please fill in the password.")})

    def validate_password(self, value):
        text = value.replace("\r", "").replace("\n", "").strip()
        if not text:
            raise serializers.ValidationError(_("Please fill in the password."))
        if len(text) > FOLDER_PASSWORD_MAX:
            raise serializers.ValidationError(ngettext("The password is at most %(limit)s character.", "The password is at most %(limit)s characters.", FOLDER_PASSWORD_MAX) % {"limit": FOLDER_PASSWORD_MAX})
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
            raise serializers.ValidationError(_("The password can't contain control characters."))
        return text


class ReadmeRuleSerializer(_RuleSerializer):
    text = StringField(max_length=README_MAX * 2, allow_blank=True, default="", trim_whitespace=False)
    path_mode = serializers.ChoiceField(choices=ReadmePathMode.choices, default=ReadmePathMode.RELATIVE,
                                        error_messages={"invalid_choice": gettext_lazy("The path mode can only be relative or absolute.")})
    display = serializers.ChoiceField(choices=ReadmeDisplay.choices, default=ReadmeDisplay.TOP,
                                      error_messages={"invalid_choice": gettext_lazy("Where it's shown can only be the top, the bottom or a dialog.")})

    def validate_text(self, value):
        text = value.replace("\r\n", "\n").replace("\r", "\n")
        if len(text) > README_MAX:
            raise serializers.ValidationError(ngettext("A readme is at most %(limit)s character.", "A readme is at most %(limit)s characters.", README_MAX) % {"limit": README_MAX})
        if not text.strip():
            raise serializers.ValidationError(_("Please fill in the readme's content."))
        return text


RULE_SERIALIZERS = {
    "filters": FilterRuleSerializer,
    "passwords": FolderPasswordSerializer,
    "readmes": ReadmeRuleSerializer,
}


class JsonListSerializer(serializers.ListSerializer):
    """A whole list that replaces what is saved (PUT), sent as a JSON list. DRF reads a form (multipart,
    urlencoded) as the empty list whatever it holds (html.parse_html_list), and the empty list of rules
    deletes all of them, folder passwords too (API-03): a body that isn't a list is refused."""

    def to_internal_value(self, data):
        if not isinstance(data, list):
            raise serializers.ValidationError(
                {api_settings.NON_FIELD_ERRORS_KEY: [_("Send a JSON list with an object for each item.")]}, code="not_a_list")
        return super().to_internal_value(data)


class RuleListSerializer(JsonListSerializer):
    def validate(self, attrs):
        ids = [item.get("id") for item in attrs if item.get("id")]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(_("A rule id repeats."))
        return attrs


def rule_list(kind: str, data) -> RuleListSerializer:
    # max_length is checked before the items are (a long list costs nothing).
    return RuleListSerializer(child=RULE_SERIALIZERS[kind](), data=data, allow_empty=True, max_length=RULES_MAX,
                              error_messages={"max_length": ngettext("At most %(limit)s rule of a kind.", "At most %(limit)s rules of a kind.", RULES_MAX) % {"limit": RULES_MAX}})


class GrantItemSerializer(serializers.Serializer):
    """One row of a user's grants, as GET gives it; `storage` may also be just its id."""

    storage = serializers.JSONField()
    granted = serializers.BooleanField(default=True)
    enabled = serializers.BooleanField(default=False)
    root_path = StringField(max_length=4096, allow_blank=True, default="/", trim_whitespace=False)
    permissions = serializers.JSONField(default=list)

    def validate_storage(self, value):
        storage_id = value.get("id") if isinstance(value, dict) else value
        # A larger one doesn't fit in the id column: a 500 on SQLite (API-09).
        if type(storage_id) is not int or not 1 <= storage_id <= ID_MAX:
            raise serializers.ValidationError(_("Should be a storage source id."))
        return storage_id

    def validate_root_path(self, value):
        try:
            return clean_root(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages[0]) from None

    def validate_permissions(self, value):
        try:
            codes = clean_permissions(value)
            check_requires(codes)  # D55: one that needs another first isn't taken alone
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from None
        return codes


class GrantListSerializer(JsonListSerializer):
    def validate(self, attrs):
        ids = [item["storage"] for item in attrs]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError(_("The same storage source appears more than once."))
        known = set(StorageSource.objects.filter(pk__in=ids).values_list("pk", flat=True))
        missing = [str(pk) for pk in ids if pk not in known]
        if missing:
            raise serializers.ValidationError(_("These storage sources no longer exist: %(names)s. Please refresh and save again.") % {"names": ', '.join(missing)})
        return attrs


def grant_list(data) -> GrantListSerializer:
    return GrantListSerializer(child=GrantItemSerializer(), data=data, allow_empty=True, max_length=GRANTS_MAX,
                               error_messages={"max_length": ngettext("At most %(limit)s storage source at a time.", "At most %(limit)s storage sources at a time.", GRANTS_MAX) % {"limit": GRANTS_MAX}})
