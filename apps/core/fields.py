"""Serializer fields the apps share."""

from django.db import models
from django.utils.translation import gettext_lazy
from rest_framework import serializers
from rest_framework.fields import empty


class StringField(serializers.CharField):
    """DRF's CharField taking a JSON string only (API-07). DRF's own turns a number into Python's way of
    writing it: 98765432.10 becomes "98765432.1" and 1e2 "100.0", so a folder password, a share's, the
    OnlyOffice secret or a name sent as a number was saved as another one. Every text field of the API
    is one of these (apps/core/test_fields.py checks the sources)."""

    default_error_messages = {"invalid": gettext_lazy("Should be text (a string in JSON).")}

    def to_internal_value(self, data):
        if not isinstance(data, str):
            self.fail("invalid")
        return super().to_internal_value(data)


# ModelSerializer.serializer_field_mapping with StringField for text columns, for the serializers that
# write models: the fields DRF makes from the model's are its CharField otherwise.
STRING_FIELD_MAPPING = {
    **serializers.ModelSerializer.serializer_field_mapping,
    models.CharField: StringField,
    models.TextField: StringField,
}


class SwitchField(serializers.BooleanField):
    """A true/false field that a form (multipart) may leave out as JSON may: then it isn't set. DRF's own
    BooleanField takes a form without it for an unticked checkbox, false, which created users and single
    sign-on providers disabled (API-06)."""

    default_empty_html = empty


class BoundedListField(serializers.ListField):
    """A ListField that refuses a list longer than max_length before it looks at the items (D52).

    DRF 3.18's ListField checks max_length with a validator, after it has validated every item and
    kept one error per bad item: a body of a few MB holding a million items costs seconds of CPU
    and a GB of memory before it is refused. ListSerializer already checks the length first."""

    def to_internal_value(self, data):
        if self.max_length is not None and isinstance(data, (list, tuple)) and len(data) > self.max_length:
            self.fail("max_length", max_length=self.max_length)
        return super().to_internal_value(data)
