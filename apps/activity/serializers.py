"""What GET /api/v1/manage/activity/ answers and takes (apps/activity/views.py)."""

import ipaddress

from django.utils.translation import gettext as _, gettext_lazy
from rest_framework import serializers

from apps.accounts.models import BUILTIN_NICKNAMES
from apps.core.fields import StringField
from apps.core.query import ChoiceParam, DateParam, IdParam, TextParam
from apps.ipgeo.lookup import shown

from . import log
from .describe import describe, settings_group_label
from .models import ActivityLog

Kind = ActivityLog.Kind

# The section of /manage/ an entry's kind is about (apps/web/navigation.py SECTIONS), for a link to it.
PAGES = {
    Kind.CREATE_PROVIDER: "sso", Kind.UPDATE_PROVIDER: "sso", Kind.DELETE_PROVIDER: "sso", Kind.SORT_PROVIDERS: "sso",
    Kind.DELETE_SHORT_LINKS: "short-links", Kind.DELETE_DOWNLOAD_LOGS: "download-logs",
    Kind.UPDATE_IP_GEO: "ip-geo", Kind.DOWNLOAD_IP_GEO: "ip-geo", Kind.FILL_IP_GEO: "ip-geo",
    Kind.SORT_STORAGES: "storages",
    Kind.CREATE_COPY_TASK: "copy-tasks", Kind.STOP_COPY_TASK: "copy-tasks", Kind.RESUME_COPY_TASK: "copy-tasks",
}
SETTINGS_PAGES = {"site": "site", "view": "view", "link": "links", "proxy": "proxy", "security": "security",
                  "access": "access"}


def _section_label(key):
    from apps.web.navigation import SECTIONS

    return next((str(section.label) for section in SECTIONS if section.key == key), key)


def _person(user, name):
    """A user an entry names: {"id" (None once deleted), "username", "nickname", "kind"}."""
    if user is None:
        return {"id": None, "username": name, "nickname": "", "kind": ""} if name else None
    nickname = _(BUILTIN_NICKNAMES[user.kind]) if user.kind in BUILTIN_NICKNAMES else user.nickname
    return {"id": user.pk, "username": user.username, "nickname": nickname, "kind": user.kind}


class ActivitySerializer(serializers.ModelSerializer):
    """One entry. `detail` as stored (codes and values), `detail_text` what it says in the page's language;
    `page` the section of /manage/ it is about, if any."""

    kind_label = StringField(source="get_kind_display", read_only=True)
    category = serializers.SerializerMethodField()
    actor = serializers.SerializerMethodField()
    target = serializers.SerializerMethodField()
    storage = serializers.SerializerMethodField()
    detail_text = serializers.SerializerMethodField()
    page = serializers.SerializerMethodField()
    ip_location = serializers.SerializerMethodField()

    class Meta:
        model = ActivityLog
        fields = ["id", "created_at", "kind", "kind_label", "category", "actor", "target", "storage", "path",
                  "to_path", "key", "detail", "detail_text", "page", "ip", "ip_location", "user_agent"]
        read_only_fields = fields

    def get_category(self, obj):
        return log.CATEGORY_OF.get(obj.kind, "")

    def get_actor(self, obj):
        return _person(obj.actor, obj.actor_name)

    def get_target(self, obj):
        return _person(obj.target, obj.target_name)

    def get_storage(self, obj):
        if obj.storage is None:
            return {"id": None, "name": obj.storage_name, "key": ""} if obj.storage_name else None
        return {"id": obj.storage_id, "name": obj.storage_name or obj.storage.name, "key": obj.storage.key}

    def get_detail_text(self, obj):
        return describe(obj)

    def get_page(self, obj):
        if obj.kind == Kind.UPDATE_SETTINGS:
            group = (obj.detail or {}).get("group", "")
            key = SETTINGS_PAGES.get(group)
            return {"section": key, "label": str(settings_group_label(group))} if key else None
        key = PAGES.get(obj.kind)
        return {"section": key, "label": _section_label(key)} if key else None

    def get_ip_location(self, obj):
        return shown(obj.ip_location)


class ActivityQuery(serializers.Serializer):
    """GET manage/activity/: ?search= (usernames and nicknames, storage source names, paths, keys, or the start
    of an IP), ?kind= or ?category=, ?storage= and ?user= (ids; a user: what they did and what was about them),
    ?ip= (one address), ?since= and ?until= (days, both included, in the site's time zone)."""

    search = TextParam()
    kind = ChoiceParam(Kind.choices, message=gettext_lazy("There is no such action."))
    category = ChoiceParam([category.key for category in log.CATEGORIES])
    storage = IdParam(gettext_lazy("storage source"))
    user = IdParam(gettext_lazy("user"))
    ip = TextParam(max_length=64)
    since = DateParam()
    until = DateParam()

    def validate_ip(self, value):
        """As entries store them (apps.core.net.address_of): IPv6 compressed, without a zone; IPv4 for an
        IPv4-mapped one."""
        if not value:
            return ""
        try:
            ip = ipaddress.ip_address(value.split("%", 1)[0])
        except ValueError:
            raise serializers.ValidationError(_("That isn't an IP address.")) from None
        if ip.version == 6 and ip.ipv4_mapped:
            ip = ip.ipv4_mapped
        return str(ip)

    def validate(self, attrs):
        if attrs["since"] and attrs["until"] and attrs["since"] > attrs["until"]:
            raise serializers.ValidationError({"until": [_("The end date can't be before the start date.")]})
        return attrs
