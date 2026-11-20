"""Secrets the site keeps in the database: the keys and tokens of storage sources, the OnlyOffice
secret, the client secrets of SSO providers.

They are stored encrypted (Fernet: AES-128-CBC with an HMAC-SHA256), with a key derived from
[site] secret_key, so neither the database nor its backups (.sql.gz) hold them in the clear. A
stored value is "" (not set) or "enc:" followed by the Fernet token.

Changing secret_key makes every stored secret unreadable: decrypt() then raises UnreadableSecret
and the admin has to enter them again (README, 设置文件). The API never sends a secret back; it only
says whether one is set.
"""

from __future__ import annotations

import functools

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from django.conf import settings
from django.utils.translation import gettext as _, gettext_lazy

PREFIX = "enc:"


class UnreadableSecret(Exception):
    """The value was encrypted with another secret_key, or isn't one of ours."""

    def __init__(self, message=gettext_lazy("The saved secret can't be read: secret_key in the settings file has changed. Please enter this secret again.")):
        super().__init__(message)
        self.message = message


@functools.lru_cache(maxsize=4)
def _fernet(secret_key: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"hfile.core.secrets", info=b"fernet").derive(
        secret_key.encode("utf-8")
    )
    import base64

    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    """The stored form of `value`; "" stays "" (not set)."""
    if not isinstance(value, str):
        raise TypeError("a secret is text")
    if value == "":
        return ""
    return PREFIX + _fernet(settings.SECRET_KEY).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(stored: str | None) -> str:
    """The secret in a stored value; "" for one that isn't set. Raises UnreadableSecret."""
    if not stored:
        return ""
    if not stored.startswith(PREFIX):
        raise UnreadableSecret(_("The saved secret is malformed. Please enter this secret again."))
    try:
        return _fernet(settings.SECRET_KEY).decrypt(stored[len(PREFIX):].encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise UnreadableSecret() from None


def is_set(stored: str | None) -> bool:
    return bool(stored)
