"""What the copy task API takes (apps/copytasks/views.py)."""

from django.template.defaultfilters import filesizeformat
from django.utils.translation import gettext as _, gettext_lazy
from rest_framework import serializers

from apps.core.fields import StringField
from apps.core.query import ID_MAX, IdParam
from apps.storages.drivers import paths
from apps.storages.drivers.base import InvalidPath

from .models import RETRIES_MAX, SPEED_LIMIT_MAX, THREADS_MAX, CopyTask

Policy = CopyTask.Policy

PATH_MAX = 4096  # characters, as the site's paths have at most (apps.storages.access)
DEPTH_MAX = 256
_NUMBER_ERRORS = ("invalid", "min_value", "max_value", "max_string_length", "required", "null")


def clean_path(value):
    """A folder's path as typed or picked: a clean storage path, or a 400 saying why not."""
    try:
        path = paths.clean(value)
    except InvalidPath as error:
        raise serializers.ValidationError(error.message) from None
    if len(paths.segments(path)) > DEPTH_MAX:
        raise serializers.ValidationError(_("The path has too many levels (at most %(depth)s).") % {"depth": DEPTH_MAX})
    return path


def _path_field():
    return StringField(required=False, allow_blank=True, default="/", max_length=PATH_MAX, trim_whitespace=False)


def _number(minimum, maximum, message, **kwargs):
    """A whole number from `minimum` to `maximum`, and one message for whatever else comes."""
    return serializers.IntegerField(min_value=minimum, max_value=maximum,
                                    error_messages=dict.fromkeys(_NUMBER_ERRORS, message), **kwargs)


class CreateSerializer(serializers.Serializer):
    """POST manage/copy-tasks/: from which storage source's folder into which one's, and how. The numbers are
    source, target, threads, speed_limit (bytes a second, 0: no limit) and retries: get_fields() makes them, with
    their messages in the request's language (a validator keeps the message it was made with)."""

    source_path = _path_field()
    target_path = _path_field()
    same_file = serializers.ChoiceField(choices=Policy.choices, default=Policy.SKIP)
    same_name = serializers.ChoiceField(choices=Policy.choices, default=Policy.OVERWRITE)

    def get_fields(self):
        fields = super().get_fields()
        fields["source"] = _number(1, ID_MAX, _("Choose the storage source to copy from."))
        fields["target"] = _number(1, ID_MAX, _("Choose the storage source to copy into."))
        fields["threads"] = _number(1, THREADS_MAX, _("Threads: a whole number from 1 to %(max)s.") % {"max": THREADS_MAX},
                                    default=4)
        fields["speed_limit"] = _number(0, SPEED_LIMIT_MAX, _(
            "The speed limit is in bytes a second: a whole number from 0 (no limit) to %(max)s.")
            % {"max": filesizeformat(SPEED_LIMIT_MAX)}, default=0)
        fields["retries"] = _number(0, RETRIES_MAX, _("Retries: a whole number from 0 to %(max)s.") % {"max": RETRIES_MAX},
                                    default=3)
        for name in ("same_file", "same_name"):
            fields[name].error_messages["invalid_choice"] = _("Can only be skip or overwrite.")
        return fields

    def validate_source_path(self, value):
        return clean_path(value)

    def validate_target_path(self, value):
        return clean_path(value)


class LogQuery(serializers.Serializer):
    """GET manage/copy-tasks/{id}/log/: ?before= (the id of a line: the ones older than it) or ?after= (newer)."""

    before = IdParam(gettext_lazy("record"))
    after = IdParam(gettext_lazy("record"))


class FoldersQuery(serializers.Serializer):
    """GET manage/copy-tasks/folders/?storage=&path=."""

    storage = IdParam(gettext_lazy("storage source"))
    path = _path_field()

    def validate_storage(self, value):
        if value is None:
            raise serializers.ValidationError(_("Choose a storage source."))
        return value

    def validate_path(self, value):
        return clean_path(value)
