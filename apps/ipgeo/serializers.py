from django.utils.translation import gettext as _
from rest_framework import serializers

from apps.core.fields import StringField, SwitchField

from .lookup import parse_ip


class IpGeoSettingsSerializer(serializers.Serializer):
    """PATCH manage/ip-geo/."""

    enabled = SwitchField()


class FillSerializer(serializers.Serializer):
    """POST manage/ip-geo/fill/: `redo` looks up the IPs that have a location already again too."""

    redo = serializers.BooleanField(default=False)


class CheckQuery(serializers.Serializer):
    """GET manage/ip-geo/check/?ip=."""

    ip = StringField(max_length=64)

    def validate_ip(self, value):
        address = parse_ip(value)
        if address is None:
            raise serializers.ValidationError(_("Write an IP address, like 8.8.8.8 or 2001:4860:4860::8888."))
        return str(address)
