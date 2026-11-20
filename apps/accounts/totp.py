"""Two-step verification with time-based one-time passwords (TOTP, RFC 6238): SHA-1, 6 digits,
30 seconds, a secret of 20 random bytes (32 base32 characters), the step before and after now accepted
too. Each superuser binds their own device.

- The secret is stored encrypted (apps.core.secrets) and never sent back once it is on.
- While being set up it waits in the session, encrypted, until a code from the device confirms it
  (a secret the browser posts back is never trusted).
- A code works once: the user's last accepted time step is kept, and only a later one is accepted
  (an atomic update, so two requests with one code can't both pass).
- Wrong codes are counted per user: after FAILURES_MAX within FAILURES_SECONDS, codes aren't checked
  until that time is over (the login rate limit is per address only).
"""

import base64
import hashlib
import hmac
import secrets
import struct
import time
import unicodedata
from urllib.parse import quote

import segno
from django.core.cache import cache
from django.db.models import Q

from apps.core import secrets as stored

PERIOD = 30  # seconds
DIGITS = 6
WINDOW = 1  # steps before and after now that are accepted too (a clock a little off)
SETUP_KEY = "hfile_totp_setup"
SETUP_SECONDS = 600
FAILURES_MAX = 10
FAILURES_SECONDS = 15 * 60


def new_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def code_at(secret, step):
    """The code of `secret` for time step `step` (RFC 4226's HOTP of the step)."""
    key = base64.b32decode(secret, casefold=True)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 10**DIGITS).zfill(DIGITS)


def current_step(now=None):
    return int((time.time() if now is None else now) // PERIOD)


def matching_step(secret, code, now=None):
    """The time step within WINDOW of now whose code `code` is, or None. Every step is compared."""
    step = current_step(now)
    found = None
    for candidate in range(step - WINDOW, step + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, candidate), code):
            found = candidate
    return found


def uri(secret, account, issuer):
    """The otpauth:// address an authenticator app reads from the QR code."""
    label = f"{quote(issuer, safe='')}:{quote(account, safe='')}"
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer, safe='')}&algorithm=SHA1&digits={DIGITS}&period={PERIOD}"


def qr_data_uri(text):
    """The QR code of `text` as an SVG data: URI, for an <img> (an SVG there runs nothing); "" when it doesn't
    fit in one, as with a site name and a username of many characters beyond U+FFFF, 12 bytes each in the
    address. The secret is typed in then (ACCT-03)."""
    try:
        code = segno.make(text, error="m")
    except segno.DataOverflowError:
        return ""
    return code.svg_data_uri(scale=4, border=4, dark="#000", light="#fff")


# ------------------------------------------------------------ a user's codes


def _failures_key(user):
    return f"hfile-totp-failures:{user.pk}"


def locked(user):
    return (cache.get(_failures_key(user)) or 0) >= FAILURES_MAX


def count_failure(user):
    key = _failures_key(user)
    cache.add(key, 0, timeout=FAILURES_SECONDS)
    try:
        cache.incr(key)
    except ValueError:  # gone meanwhile
        cache.set(key, 1, timeout=FAILURES_SECONDS)


def verify(user, code):
    """Whether `code` is a code of `user`'s device that hasn't been used. Raises
    apps.core.secrets.UnreadableSecret when secret_key has changed since it was set."""
    step = matching_step(stored.decrypt(user.totp_secret), code)
    if step is None:
        return False
    # Only a step after the last one used: one code, one login.
    used = type(user)._default_manager.filter(pk=user.pk).filter(
        Q(totp_last_step__isnull=True) | Q(totp_last_step__lt=step)
    ).update(totp_last_step=step)
    if used:
        user.totp_last_step = step
    return bool(used)


def clean_code(value):
    """What was typed, without spaces and with full-width digits as ASCII; None unless 6 digits."""
    if not isinstance(value, str):
        return None
    code = "".join(unicodedata.normalize("NFKC", value).split())
    return code if len(code) == DIGITS and code.isascii() and code.isdigit() else None
