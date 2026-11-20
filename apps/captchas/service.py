"""Handing out captchas and checking the answers.

    required(scene)             whether that form asks for a captcha now (登录: SiteSettings.login_captcha)
    issue(scene)                a new picture for it (a Picture: key, image, size, ...)
    issue_allowed(request)      whether a page that draws one itself may: POST /api/v1/captcha/'s budget
    verify(scene, key, answer)  raises CaptchaError unless the answer is right

Each picture is good for one try: verify() deletes it, right answer or not, so a program can't keep
guessing at the same picture. The login checks the captcha before the password, outside any
transaction, so a try is used up even when the login fails afterwards.
"""

import hmac
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import timedelta

from django.utils import timezone
from django.utils.translation import gettext_lazy

from apps.core.throttles import ScopedRateThrottle

from . import images
from .models import LENGTH, Challenge, Scene

TTL = timedelta(minutes=10)
# The rate limit pictures count in (settings.REST_FRAMEWORK, 30 a minute): per address, or per user when
# logged in. POST /api/v1/captcha/ (views.py) and the Django admin's login page share it.
ISSUE_SCOPE = "captcha"
KEY_LENGTH = Challenge._meta.get_field("key").max_length  # hex digits

# Drawn in upper case and compared without case. No 0, 1, I or O: they look like each other.
CHARACTERS = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"

MISSING = gettext_lazy("Please enter the captcha.")
EXPIRED = gettext_lazy("The captcha has expired. Please enter the new one.")
WRONG = gettext_lazy("The captcha is wrong. Please try again.")


class CaptchaError(Exception):
    def __init__(self, message, code):
        super().__init__(message)
        self.message = message
        self.code = code


@dataclass(frozen=True)
class Picture:
    key: str
    image: str  # data: URI of a PNG drawn at twice width × height, for sharp screens
    width: int  # CSS pixels
    height: int
    length: int
    expires_in: int  # seconds


def required(scene):
    if scene == Scene.LOGIN:
        from apps.siteconf.models import SiteSettings

        return SiteSettings.load().login_captcha
    return False


def random_text(length=LENGTH):
    return "".join(secrets.choice(CHARACTERS) for _ in range(length))


def issue(scene):
    text = random_text()
    image = images.data_uri(images.render(text))
    now = timezone.now()
    Challenge.objects.filter(expires_at__lte=now).delete()
    challenge = Challenge.objects.create(key=secrets.token_hex(KEY_LENGTH // 2), scene=scene, answer=text, expires_at=now + TTL)
    return Picture(
        key=challenge.key,
        image=image,
        width=images.width_for(len(text)),
        height=images.HEIGHT,
        length=len(text),
        expires_in=int(TTL.total_seconds()),
    )


class _IssueView:
    throttle_scope = ISSUE_SCOPE


def issue_allowed(request):
    """For a page that draws its picture itself (the Django admin's login page, apps/accounts/admin.py): counts
    one picture in POST /api/v1/captcha/'s budget. (allowed, seconds to wait)."""
    throttle = ScopedRateThrottle()
    if throttle.allow_request(request, _IssueView()):
        return True, 0
    return False, int(throttle.wait() or 0) + 1


def normalize(answer):
    """Full-width characters (from a Chinese input method) become ASCII; spaces go; case doesn't matter."""
    return "".join(unicodedata.normalize("NFKC", answer).split()).upper()


def verify(scene, key, answer):
    answer = normalize(answer) if isinstance(answer, str) else ""
    if not answer or not isinstance(key, str) or not key:
        raise CaptchaError(MISSING, "captcha_required")
    challenge = Challenge.objects.filter(key=key).first() if len(key) == KEY_LENGTH else None
    if challenge is None or challenge.key != key:  # MySQL's collation compares without case
        raise CaptchaError(EXPIRED, "captcha_expired")
    # One try per picture: only the request that deletes it may use it.
    if not Challenge.objects.filter(pk=challenge.pk).delete()[0]:
        raise CaptchaError(EXPIRED, "captcha_expired")
    if challenge.scene != scene or challenge.expires_at <= timezone.now():
        raise CaptchaError(EXPIRED, "captcha_expired")
    if not hmac.compare_digest(answer.encode(), challenge.answer.encode()):
        raise CaptchaError(WRONG, "captcha_wrong")
