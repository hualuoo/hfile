"""The shares API's bodies, queries and rows (apps/shares/views.py)."""

from __future__ import annotations

import datetime
import re

from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy, ngettext, ngettext_lazy, pgettext_lazy
from rest_framework import serializers

from apps.core.fields import BoundedListField, StringField
from apps.core.query import ChoiceParam, DateParam, IdParam, TextParam
from apps.core.secrets import UnreadableSecret
from apps.files.service import storage_data

from . import service
from .models import CUSTOM_KEY_MAX, CUSTOM_KEY_MIN, ENTRIES_MAX, FILE, FOLDER, PASSWORD_MAX, PASSWORD_MIN, Share

PATH_MAX = 4096
NAME_MAX = 1024
CUSTOM_KEY_RE = re.compile(rf"[A-Za-z0-9_-]{{{CUSTOM_KEY_MIN},{CUSTOM_KEY_MAX}}}")
EXPIRY_DAYS = {"7d": 7, "30d": 30}
EXPIRY_MAX = datetime.timedelta(days=100 * 365)
KEY_RULE = gettext_lazy("Letters, digits, underscores and hyphens only, 3 to 8 characters.")


class _Path(StringField):
    def __init__(self, **kwargs):
        super().__init__(max_length=PATH_MAX, allow_blank=True, required=False, default="/", trim_whitespace=False, **kwargs)


class EntryField(serializers.Field):
    """{"name", "type"}: a name in the folder (one segment) and what it is; create() finds exactly that
    in the storage (a file is never widened to a folder of the same name)."""

    default_error_messages = {"invalid": gettext_lazy('Each item should be {{"name", "type"}}, with type file or folder.'), "name": gettext_lazy("Invalid name: {name}")}

    def to_internal_value(self, data):
        if not isinstance(data, dict):
            self.fail("invalid")
        name = data.get("name")
        kind = data.get("type")
        if kind not in (FILE, FOLDER):
            self.fail("invalid")
        if (not isinstance(name, str) or not name or len(name) > NAME_MAX or "/" in name or "\x00" in name
                or name in (".", "..")):
            self.fail("name", name=str(name)[:100])
        return (name, kind)

    def to_representation(self, value):
        return value


class CreateBody(serializers.Serializer):
    """POST /api/v1/shares/."""

    storage = StringField(max_length=64)
    path = _Path()
    entries = BoundedListField(child=EntryField(), allow_empty=False, max_length=ENTRIES_MAX,
                               error_messages={"empty": gettext_lazy("Please select the files or folders to share first."),
                                               "max_length": ngettext_lazy("At most {max_length} item can be shared at a time.", "At most {max_length} items can be shared at a time.",
                                                                             "max_length")})
    key = StringField(max_length=64, allow_blank=True, required=False, default="")
    password = StringField(max_length=200, allow_blank=True, required=False, default="", trim_whitespace=True)
    expiry = serializers.ChoiceField(choices=["forever", "7d", "30d", "custom"], required=False,
                                     error_messages={"invalid_choice": gettext_lazy("The expiry can only be forever, 7d, 30d or custom.")})
    expires_at = serializers.DateTimeField(required=False, allow_null=True, default=None,
                                           error_messages={"invalid": gettext_lazy("The expiry time is malformed.")})

    def validate_entries(self, names):
        return list(dict.fromkeys(names))

    def validate_key(self, key):
        key = key.strip()
        if key and not CUSTOM_KEY_RE.fullmatch(key):
            raise serializers.ValidationError(KEY_RULE)
        return key

    def validate_password(self, password):
        if password and not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            raise serializers.ValidationError(ngettext("The password must be %(min)s to %(max)s character long.", "The password must be %(min)s to %(max)s characters long.", PASSWORD_MAX) % {"min": PASSWORD_MIN, "max": PASSWORD_MAX})
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in password):
            raise serializers.ValidationError(_("The password can't contain control characters."))
        return password

    def validate(self, data):
        now = timezone.now()
        moment = data.get("expires_at")
        # expires_at alone is a custom expiry, and with another expiry a contradiction: it used to be ignored,
        # a share without "expiry" lasting forever (API-04).
        expiry = data.get("expiry") or ("custom" if moment is not None else "forever")
        if moment is not None and expiry != "custom":
            raise serializers.ValidationError(
                {"expires_at": _("With the expiry %(expiry)s, an expiry time can't be given too; for "
                                 "an expiry time of your own, set the expiry to custom or leave it out.") % {"expiry": expiry}})
        if expiry in EXPIRY_DAYS:
            data["expires_at"] = now + datetime.timedelta(days=EXPIRY_DAYS[expiry])
        elif expiry == "custom":
            if moment is None:
                raise serializers.ValidationError({"expires_at": _("Please choose when it expires.")})
            if moment <= now:
                raise serializers.ValidationError({"expires_at": _("The expiry time must be in the future.")})
            if moment - now > EXPIRY_MAX:
                raise serializers.ValidationError({"expires_at": _("The expiry time can be at most 100 years from now.")})
        else:
            data["expires_at"] = None
        return data


class VerifyBody(serializers.Serializer):
    password = StringField(max_length=200, allow_blank=True, trim_whitespace=False)


class UnlockBody(serializers.Serializer):
    """A folder password inside a share: the folder's path (as for files), or the rule's id."""

    path = _Path()
    rule = serializers.IntegerField(required=False, min_value=1)
    password = StringField(max_length=1000, allow_blank=True, trim_whitespace=False)


class ListingQuery(serializers.Serializer):
    path = _Path()
    sort = serializers.ChoiceField(choices=service.SORT_FIELDS, required=False, allow_blank=True, default="",
                                   error_messages={"invalid_choice": gettext_lazy("The sort field can only be name, size or time.")})
    order = serializers.ChoiceField(choices=service.ORDERS, required=False, allow_blank=True, default="",
                                    error_messages={"invalid_choice": gettext_lazy("The sort order can only be asc or desc.")})


class ItemQuery(serializers.Serializer):
    path = _Path()


class ListQuery(serializers.Serializer):
    """?keyword= (the key or a name) &storage= (id) &status=all|active|expired &since= &until= (days)."""

    keyword = TextParam()
    storage = IdParam(pgettext_lazy("inside a sentence", "storage source"))
    status = ChoiceParam(["all", "active", "expired"], default="all", message=gettext_lazy("The status can only be all, active or expired."))
    since = DateParam()
    until = DateParam()


class ManageListQuery(ListQuery):
    user = TextParam()


class ShareRow(serializers.ModelSerializer):
    """A share in 我的分享 and 分享列表: for its owner and admins, so with its password (D32)."""

    url = serializers.SerializerMethodField()
    label = StringField(read_only=True)
    entry_count = serializers.SerializerMethodField()
    storage = serializers.SerializerMethodField()
    has_password = serializers.BooleanField(read_only=True)
    password = serializers.SerializerMethodField()
    password_unreadable = serializers.SerializerMethodField()
    expired = serializers.SerializerMethodField()
    can_delete = serializers.SerializerMethodField()

    class Meta:
        model = Share
        fields = ["key", "url", "label", "entries", "entry_count", "storage", "folder", "has_password", "password",
                  "password_unreadable", "created_at", "expires_at", "expired", "access_count", "download_count",
                  "can_delete"]
        read_only_fields = fields

    def get_url(self, share):
        return service.absolute_url(share, self.context.get("request"))

    def get_entry_count(self, share):
        return len(share.entries or ())

    def get_storage(self, share):
        return {"id": share.storage_id, **storage_data(share.storage)}

    def _password(self, share):
        """(password, unreadable), decrypted once per share."""
        known = getattr(share, "_row_password", None)
        if known is None:
            if not share.has_password:
                known = (None, False)
            else:
                try:
                    known = (share.plain_password(), False)
                except UnreadableSecret:
                    known = (None, True)
            share._row_password = known
        return known

    def get_password(self, share):
        return self._password(share)[0]

    def get_password_unreadable(self, share):
        return self._password(share)[1]

    def get_expired(self, share):
        return share.is_expired()

    def get_can_delete(self, share):
        request = self.context.get("request")
        return service.may_delete(getattr(request, "user", None), share)


class ManageShareRow(ShareRow):
    owner = serializers.SerializerMethodField()

    class Meta(ShareRow.Meta):
        fields = [*ShareRow.Meta.fields, "owner"]
        read_only_fields = fields

    def get_owner(self, share):
        return {"id": share.owner_id, "username": share.owner.username, "nickname": share.owner.nickname}
