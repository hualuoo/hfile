"""apps.core.fields: BoundedListField (D52), a list's length checked before its items; StringField
(API-07), text fields that take JSON strings only."""

import re
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.test import SimpleTestCase
from rest_framework import serializers

from apps.core.fields import BoundedListField, StringField


class BoundedListFieldTests(SimpleTestCase):
    def field(self, **kwargs):
        return BoundedListField(child=serializers.IntegerField(), max_length=3, **kwargs)

    def test_a_long_list_is_refused_before_its_items(self):
        field = self.field(error_messages={"max_length": "一次最多 3 个。"})
        with mock.patch.object(field.child, "run_validation") as child, \
                self.assertRaises(serializers.ValidationError) as caught:
            field.run_validation([{}] * 1000)
        child.assert_not_called()
        self.assertEqual(caught.exception.detail, ["一次最多 3 个。"])

    def test_within_the_length_each_item_is_checked(self):
        field = self.field()
        self.assertEqual(field.run_validation([1, "2"]), [1, 2])
        with self.assertRaises(serializers.ValidationError) as caught:
            field.run_validation([1, "x"])
        self.assertIn(1, caught.exception.detail)

    def test_not_a_list(self):
        with self.assertRaises(serializers.ValidationError):
            self.field().run_validation("abcd")


class StringFieldTests(SimpleTestCase):
    def test_only_strings(self):
        field = StringField(allow_blank=True)
        self.assertEqual(field.run_validation("98765432.10"), "98765432.10")
        for value in (98765432.10, 1e2, 7, True, [1], {"a": 1}):
            with self.subTest(value=value), self.assertRaises(serializers.ValidationError) as caught:
                field.run_validation(value)
            self.assertEqual(caught.exception.detail, ["应该是文字（JSON 里的字符串）。"])

    def test_the_options_still_apply(self):
        field = StringField(max_length=3, error_messages={"max_length": "太长了。"})
        self.assertEqual(field.run_validation("  ab "), "ab")
        with self.assertRaises(serializers.ValidationError) as caught:
            field.run_validation("abcd")
        self.assertEqual(caught.exception.detail, ["太长了。"])

    def test_every_text_field_is_one(self):
        """DRF's CharField takes numbers and writes them its own way (98765432.10 -> "98765432.1"):
        the API's sources use StringField (or STRING_FIELD_MAPPING for a ModelSerializer that writes)."""
        base = Path(settings.BASE_DIR)
        found = []
        for path in sorted([*base.glob("apps/**/*.py"), *base.glob("config/**/*.py")]):
            relative = path.relative_to(base).as_posix()
            if "/tests/" in relative or path.name.startswith("test") or relative == "apps/core/fields.py":
                continue
            text = path.read_text(encoding="utf-8")
            found += [f"{relative}:{text.count(chr(10), 0, m.start()) + 1}"
                      for m in re.finditer(r"serializers\.CharField\b", text)]
        self.assertEqual(found, [])

    def test_the_serializers_that_write_models(self):
        from apps.siteconf import serializers as siteconf
        from apps.sso.serializers import ProviderSerializer

        writers = [ProviderSerializer, *[cls for cls in vars(siteconf).values()
                                          if isinstance(cls, type) and issubclass(cls, siteconf.SettingsSerializer)]]
        self.assertGreater(len(writers), 3)
        for cls in writers:
            for name, field in cls().fields.items():
                if isinstance(field, serializers.CharField) and not field.read_only:
                    with self.subTest(serializer=cls.__name__, field=name):
                        self.assertIsInstance(field, StringField)
