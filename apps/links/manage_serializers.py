"""What the admin pages 短链管理 and 下载日志 send and get (apps/links/manage_views.py)."""

from __future__ import annotations

from rest_framework import serializers

from django.utils.translation import gettext as _, gettext_lazy, pgettext_lazy

from apps.accounts.validators import USERNAME_MAX
from apps.core import origin
from apps.core.fields import BoundedListField, StringField
from apps.core.query import ChoiceParam, DateParam, IdParam, TextParam

from . import service
from .models import KEY_MAX, TEXT_MAX, DownloadLog, ShortLink

IP_START = "0123456789abcdefABCDEF.:"


def storage_data(storage) -> dict:
    return {"id": storage.pk, "key": storage.key, "name": storage.name, "type": storage.type,
            "type_label": storage.type_label}


def _absolute(path: str, request) -> str:
    try:
        return origin.absolute(path, request)
    except ValueError:  # no [site] url and no request (a script): the path then
        return path


class ShortLinkSerializer(serializers.ModelSerializer):
    url = serializers.SerializerMethodField()
    storage = serializers.SerializerMethodField()
    created_by = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()

    class Meta:
        model = ShortLink
        fields = ["id", "key", "url", "storage", "path", "created_by", "created_at", "expires_at", "expired"]
        read_only_fields = fields

    def get_url(self, obj):
        return _absolute(service.short_link_path(obj.key), self.context.get("request"))

    def get_storage(self, obj):
        return storage_data(obj.storage)

    def get_created_by(self, obj):
        user = obj.created_by
        return {"id": user.pk, "username": user.username, "nickname": user.nickname, "kind": user.kind}

    def get_expired(self, obj):
        return obj.expired(self.context.get("now"))


class DownloadLogSerializer(serializers.ModelSerializer):
    kind_label = StringField(source="get_kind_display", read_only=True)
    storage = serializers.SerializerMethodField()
    url = serializers.SerializerMethodField()

    class Meta:
        model = DownloadLog
        fields = ["id", "kind", "kind_label", "storage", "path", "short_key", "url", "ip", "username", "user_agent",
                  "referer", "created_at"]
        read_only_fields = fields

    def get_storage(self, obj):
        return storage_data(obj.storage)

    def get_url(self, obj):
        """The link it was downloaded by: the short link, or the path link with the prefix of now; None for
        the 下载 button's (the site's own signed link, which isn't kept)."""
        request = self.context.get("request")
        if obj.kind == DownloadLog.Kind.PAGE:
            return None
        if obj.kind == DownloadLog.Kind.SHORT:
            return _absolute(service.short_link_path(obj.short_key), request)
        return _absolute(service.path_link_path(obj.storage, obj.path, self.context.get("prefix")), request)


def _ip_start(value):
    if value and any(char not in IP_START for char in value):
        raise serializers.ValidationError(_("Write the start of an IP address, like 192.168. or 2001:db8:."))
    return value.lower()


class _Days(serializers.Serializer):
    since = DateParam()
    until = DateParam()

    def validate(self, attrs):
        if attrs["since"] and attrs["until"] and attrs["since"] > attrs["until"]:
            raise serializers.ValidationError({"until": [_("The end date can't be before the start date.")]})
        return attrs


class ShortLinkQuery(_Days):
    """GET manage/short-links/ and export/: ?storage= (id), ?path= and ?key= (parts of them, any case),
    ?since= and ?until= (days of creation, both included, in the site's time zone)."""

    storage = IdParam(pgettext_lazy("inside a sentence", "storage source"))
    path = TextParam(max_length=1000)
    key = TextParam(max_length=KEY_MAX)


class DownloadLogQuery(_Days):
    """GET and DELETE manage/download-logs/: ?storage= (id), ?kind= (path, short, page), ?key=, ?path=, ?user=,
    ?user_agent=, ?referer= (parts of them, any case), ?ip= (its start), ?since= and ?until= (days of the
    download); DELETE also ?newest= (the newest id the list showed: what was logged after it stays)."""

    storage = IdParam(pgettext_lazy("inside a sentence", "storage source"))
    kind = ChoiceParam(DownloadLog.Kind.choices)
    key = TextParam(max_length=KEY_MAX)
    path = TextParam(max_length=1000)
    user = TextParam(max_length=USERNAME_MAX)
    ip = TextParam(max_length=64)
    user_agent = TextParam(max_length=TEXT_MAX)
    referer = TextParam(max_length=TEXT_MAX)
    newest = IdParam(gettext_lazy("record"))

    def validate_ip(self, value):
        return _ip_start(value)


class IdsSerializer(serializers.Serializer):
    """POST …/delete/: {"ids": [...]}, the rows ticked on the page."""

    ids = BoundedListField(
        child=serializers.IntegerField(min_value=1, max_value=2**63 - 1),
        allow_empty=False, max_length=1000,
        error_messages={"empty": gettext_lazy("Please select at least one entry."), "max_length": gettext_lazy("At most 1000 can be deleted at a time.")},
    )
