from rest_framework import serializers

from django.utils.translation import gettext_lazy

from apps.core.fields import StringField

from .models import LENGTH, Challenge, Scene

# What a person may type for an answer: the characters, and spaces or full-width forms that
# service.normalize() takes out.
ANSWER_INPUT_MAX = 4 * LENGTH


class IssueSerializer(serializers.Serializer):
    scene = serializers.ChoiceField(choices=Scene.choices, error_messages={"invalid_choice": gettext_lazy("There is no such form.")})


class AnswerSerializer(serializers.Serializer):
    """The two captcha fields a form sends with the rest. Left out, they are ""."""

    captcha_key = StringField(
        required=False, allow_blank=True, default="", max_length=Challenge._meta.get_field("key").max_length
    )
    captcha = StringField(required=False, allow_blank=True, default="", max_length=ANSWER_INPUT_MAX)
