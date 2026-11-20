"""The login's steps besides the password (apps/accounts/login.py), registered by AccountsConfig.ready():

- "captcha", before the password, while SiteSettings.login_captcha is on: the image captcha
  (captchas app). Every password login needs one, admins included (letting an admin with 2FA skip
  it would need a lookup of the username before anything is checked).
- "totp", after the password, for an account with two-step verification on (apps/accounts/totp.py):
  {"step": "totp", "code": "123456"}.
"""

from collections.abc import Mapping

from rest_framework import serializers

from django.utils.translation import gettext as _, gettext_lazy, pgettext_lazy

from apps.captchas import service as captchas
from apps.captchas.models import Scene
from apps.captchas.serializers import AnswerSerializer
from apps.core.secrets import UnreadableSecret

from . import login, loginlog, totp


def captcha_needed(request, user):
    return captchas.required(Scene.LOGIN)


def check_captcha(request, user, data):
    """The fields "captcha_key" and "captcha" (POST /api/v1/captcha/ gave the key). When neither was
    sent, the page didn't know the site wants one: it is asked for (StepRequired). Otherwise the
    picture is used up, right answer or not."""
    if not isinstance(data, Mapping) or ("captcha_key" not in data and "captcha" not in data):
        raise login.StepRequired
    fields = AnswerSerializer(data=data)
    try:
        if not fields.is_valid():  # longer than any key or answer: a wrong answer
            raise captchas.CaptchaError(captchas.WRONG, "captcha_wrong")
        captchas.verify(Scene.LOGIN, fields.validated_data["captcha_key"], fields.validated_data["captcha"])
    except captchas.CaptchaError as exc:
        if exc.code != "captcha_required":  # nothing typed isn't a try
            loginlog.record(request, success=False, username=loginlog.typed_username(data), reason="captcha")
        raise serializers.ValidationError({"captcha": [exc.message]}, code=exc.code) from None


CAPTCHA = login.Step("captcha", pgettext_lazy("login step", "Captcha"), needed=captcha_needed, check=check_captcha, before_password=True)


def totp_needed(request, user):
    return user.totp_enabled


def check_totp(request, user, data):
    """The field "code": a code of the user's device that hasn't been used yet."""
    code = totp.clean_code(data.get("code") if isinstance(data, Mapping) else None)
    if code is None:
        raise serializers.ValidationError({"code": [_("Please enter the 6 digits shown on your phone.")]})
    if totp.locked(user):
        raise serializers.ValidationError({"code": [_("Too many wrong two-step verification codes. Please try again in 15 minutes.")]})
    try:
        right = totp.verify(user, code)
    except UnreadableSecret:
        raise serializers.ValidationError({"code": [
            _("The two-step verification key can't be read any more (secret_key in the settings file has "
              "changed). Please run hfile resetadmin on the server to turn two-step verification off.")
        ]}) from None
    if not right:
        totp.count_failure(user)
        loginlog.record(request, success=False, user=user, reason="totp")
        raise serializers.ValidationError({"code": [_("The two-step verification code is wrong or already used. Please enter a new one.")]})


TOTP = login.Step("totp", gettext_lazy("Two-step verification"), needed=totp_needed, check=check_totp)


def register():
    login.register(CAPTCHA)
    login.register(TOTP)
