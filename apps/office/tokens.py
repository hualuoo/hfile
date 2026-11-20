"""OnlyOffice's JWTs: HS256 with the secret of 显示设置 → OnlyOffice (stored encrypted, apps.core.secrets).

The editor config is signed whole (its "token"), so the Document Server takes nothing from the browser
that the site didn't decide (the key, the file's URL, whether it may be edited). The token also has an
issue time and an expiry (CONFIG_LIFETIME): the Document Server checks the config's token when an editor
opens the document, and later reconnections of that editor use the session token the Document Server
gives it then (30 days by default), so the expiry only bounds how long a config can be handed to the
Document Server again (after its holder lost the permission, after a share was deleted). The Document Server
signs its callbacks with the same secret: in the body ("token"), or in "Authorization: Bearer", where
the fields are wrapped in "payload". Only HS256 is accepted (an "alg: none" token, or any other
algorithm, is refused before its claims are read), and only with an expiry ("exp": the Document Server
signs its callbacks for five minutes by default), so a callback captured on the way can't be replayed
for long.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

import jwt

from apps.core import secrets as stored
from apps.siteconf.models import SiteSettings

logger = logging.getLogger("hfile.office")

ALGORITHM = "HS256"
# Seconds an editor config's token can open the document: the editor page uses it at once, and the
# document's link inside it lasts as long (editor.URL_LIFETIME).
CONFIG_LIFETIME = 600
LEEWAY = 60  # seconds of clock difference between the site and the Document Server (exp, nbf, iat)
TOKEN_MAX = 1024 * 1024  # characters
SAVES = (2, 6)  # statuses whose callback carries the URL of the document to save


def secret(site: SiteSettings | None = None) -> str:
    """The OnlyOffice secret in the clear; "" when none is set, or when it can't be read any more
    (secret_key was changed): editing is then off and every callback is refused."""
    site = site or SiteSettings.load()
    try:
        return stored.decrypt(site.onlyoffice_secret)
    except stored.UnreadableSecret:
        logger.warning("The OnlyOffice secret can't be read (secret_key was changed?): enter it again in the display settings")
        return ""


def sign(payload: dict[str, Any], key: str) -> str:
    return jwt.encode(payload, key, algorithm=ALGORITHM)


def sign_config(config: dict[str, Any], key: str) -> str:
    """The editor config's token: the whole config, with iat and exp (the config itself keeps neither)."""
    now = int(time.time())
    return sign({**config, "iat": now, "exp": now + CONFIG_LIFETIME}, key)


@dataclass(frozen=True)
class Callback:
    """What a callback's token signs and the site uses."""

    key: str
    status: int
    url: str | None  # statuses 2 and 6
    filetype: str | None
    signed: dict[str, Any]  # every signed field (to compare the body with)


def callback_claims(token: Any, key: str) -> Callback | None:
    """The signed fields of a callback token, or None: a token that isn't HS256 with `key`, that has no
    expiry or has expired, or whose fields (top level, or in "payload") lack the document key and an
    integer status, and the URL for statuses 2 and 6. An editor config's token (fields under "document")
    is never one."""
    if not key or not isinstance(token, str) or not token or len(token) > TOKEN_MAX or not token.isascii():
        return None
    try:
        claims = jwt.decode(token, key, algorithms=[ALGORITHM], leeway=LEEWAY, options={"require": ["exp"]})
    except (jwt.PyJWTError, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    signed = claims["payload"] if "payload" in claims else claims
    if not isinstance(signed, dict) or "document" in signed or "editorConfig" in signed:
        return None
    document_key, status = signed.get("key"), signed.get("status")
    if not isinstance(document_key, str) or not document_key or type(status) is not int:
        return None
    url = signed.get("url")
    if status in SAVES:
        if not isinstance(url, str) or not url.strip():
            return None
    elif not isinstance(url, str):
        url = None
    filetype = signed.get("filetype")
    return Callback(key=document_key, status=status, url=url,
                    filetype=filetype if isinstance(filetype, str) else None, signed=signed)
