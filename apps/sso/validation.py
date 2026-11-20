"""What a provider's settings and an external id may be. Each clean_*() returns the value as it is kept,
or raises django.core.exceptions.ValidationError with a Chinese message (the serializers show it under
the field)."""

from __future__ import annotations

import base64
import binascii
import re
from urllib.parse import unquote_to_bytes, urlsplit

import idna
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext

from apps.core.text import is_invisible, reject_invisible, single_line

from . import jsonpath
from .models import CODE_PATTERN

NAME_MAX = 50
CLIENT_ID_MAX = 255
SECRET_MAX = 2000
URL_MAX = 1000
SCOPE_MAX = 500
EXTERNAL_ID_MAX = 255
ICON_MAX = 64 * 1024  # characters of the whole URL or data: URI

_CODE = re.compile(CODE_PATTERN)
# A scope token (RFC 6749 3.3): visible ASCII but " and \.
_SCOPE_TOKEN = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]+")
_BASE64_URI = re.compile(r"data:image/(png|jpeg|gif|webp|svg\+xml);base64,([A-Za-z0-9+/]+={0,2})")
# An SVG written out with %-escapes (data:image/svg+xml,%3Csvg…): only characters a URL may have.
_SVG_TEXT_URI = re.compile(r"data:image/svg\+xml(?:;charset=utf-8|;utf8)?,([A-Za-z0-9\-._~!$&'()*+,;=:@/?%]+)", re.IGNORECASE)
_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "gif": (b"GIF87a", b"GIF89a"),
}


def clean_code(value: str) -> str:
    code = value.strip().lower()
    if not _CODE.fullmatch(code):
        raise ValidationError(_("Lowercase letters, digits, - and _ only, 1 to 32 characters, like github."))
    return code


def clean_name(value: str) -> str:
    text = single_line(value)
    if not text:
        raise ValidationError(_("The name can't be empty."))
    if len(text) > NAME_MAX:
        raise ValidationError(ngettext("The name is at most %(limit)s character.", "The name is at most %(limit)s characters.", NAME_MAX) % {"limit": NAME_MAX})
    reject_invisible(text, _("Name"))
    return text


def _no_spaces(text: str, what: str) -> None:
    if any(char.isspace() for char in text):
        raise ValidationError(_("%(what)s can't contain spaces or line breaks.") % {"what": what})
    reject_invisible(text, what)


def clean_client_id(value: str) -> str:
    text = value.strip()
    if not text:
        raise ValidationError(_("Client ID can't be empty."))
    if len(text) > CLIENT_ID_MAX:
        raise ValidationError(ngettext("Client ID is at most %(limit)s character.", "Client ID is at most %(limit)s characters.", CLIENT_ID_MAX) % {"limit": CLIENT_ID_MAX})
    _no_spaces(text, "Client ID ")
    return text


def clean_secret(value: str) -> str:
    """A new client secret (the spaces and line breaks around a pasted one go)."""
    text = value.strip()
    if len(text) > SECRET_MAX:
        raise ValidationError(ngettext("Client Secret is at most %(limit)s character.", "Client Secret is at most %(limit)s characters.", SECRET_MAX) % {"limit": SECRET_MAX})
    _no_spaces(text, "Client Secret ")
    return text


def clean_url(value: str, what: str = gettext_lazy("Address"), *, limit: int = URL_MAX) -> str:
    """An http(s) address with a host, without a user name and password in it, nor a #fragment. Kept
    with the scheme and the host in lower case, a host in another script as punycode (例子.中国 as
    xn--fsqu00a.xn--fiqs8s): the authorization endpoint goes into a redirect's Location, an icon's
    origin into the CSP header, and both take ASCII only."""
    text = value.strip()
    if not text:
        raise ValidationError(_("%(what)s can't be empty.") % {"what": what})
    if len(text) > limit:
        raise ValidationError(ngettext("%(what)s is at most %(limit)s character.", "%(what)s is at most %(limit)s characters.", limit) % {"what": what, "limit": limit})
    _no_spaces(text, what)
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValidationError(_("%(what)s is malformed.") % {"what": what}) from None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise ValidationError(_("%(what)s must start with http:// or https:// followed by the server's address.") % {"what": what})
    if parts.username is not None or parts.password is not None:
        raise ValidationError(_("%(what)s can't contain a username and password.") % {"what": what})
    if parts.fragment or text.endswith("#"):
        raise ValidationError(_("%(what)s can't contain #.") % {"what": what})
    host = parts.hostname
    if not host.isascii():
        try:  # UTS #46, as browsers: Python's own codec is IDNA 2003, which made faß.de fass.de (API-13)
            host = idna.encode(host, uts46=True).decode("ascii")
        except UnicodeError:  # idna's errors are UnicodeErrors; IDNA 2008 refuses some that browsers take (☃.net)
            raise ValidationError(_("The domain in %(what)s is wrong, or has to be written in its xn-- form.") % {"what": what}) from None
    netloc = f"[{host}]" if ":" in host else host
    if port is not None:
        netloc += f":{port}"
    rest = parts.path + (f"?{parts.query}" if parts.query else "")  # not urlunsplit: "//a" would be "////a"
    if not rest.isascii():
        raise ValidationError(_("The path in %(what)s has non-ASCII characters; please write them %%xx-encoded.") % {"what": what})
    url = f"{parts.scheme.lower()}://{netloc.lower()}{rest}"
    if len(url) > limit:  # as stored: a host in another script is longer as punycode (API-11)
        raise ValidationError(ngettext("%(what)s is at most %(limit)s character (a "
                                       "non-ASCII domain counts in its xn-- form).", "%(what)s is at most %(limit)s characters (a non-ASCII domain counts in its xn-- form).", limit) % {"what": what, "limit": limit})
    return url


def clean_icon(value: str) -> str:
    """"" (the name's first character is shown), an http(s) address, or a data:image/… URI of a PNG,
    JPEG, GIF, WebP or SVG, all at most ICON_MAX characters. Shown only as an <img>'s src."""
    text = value.strip()
    if not text:
        return ""
    if len(text) > ICON_MAX:
        raise ValidationError(_("The icon is at most 64 KB: choose a smaller picture."))
    if text.startswith("<"):
        raise ValidationError(_("The icon should be an http(s) address or a data:image/… address. "
                              "SVG code must be turned into data:image/svg+xml;base64,… first "
                              "(pasting SVG code into the form on the Manage page does that)."))
    if text[:5].lower() != "data:":
        return clean_url(text, _("Icon address"), limit=ICON_MAX)
    match = _BASE64_URI.fullmatch(text)
    if match:
        kind, encoded = match.groups()
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise ValidationError(_("The icon's base64 data is wrong.")) from None
        if not _looks_like(kind, data):
            raise ValidationError(_("The icon's data isn't a %(kind)s image.") % {"kind": kind.split("+")[0].upper()})
        return text
    match = _SVG_TEXT_URI.fullmatch(text)
    if match:
        if not _looks_like("svg+xml", unquote_to_bytes(match.group(1))):
            raise ValidationError(_("The icon's data isn't an SVG image."))
        return text
    raise ValidationError(_("The icon should be an http(s) address, or an address like data:image/png;base64,… (PNG, JPEG, GIF, WebP, SVG)."))


def _looks_like(kind: str, data: bytes) -> bool:
    if kind in _MAGIC:
        return data.startswith(_MAGIC[kind])
    if kind == "webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return b"<svg" in data[:4096].lower()  # svg+xml


def clean_scope(value: str) -> str:
    tokens = value.split()
    for token in tokens:
        if not _SCOPE_TOKEN.fullmatch(token):
            raise ValidationError(_("\"%(token)s\" has characters a scope can't have (only English "
                                    "letters, digits and common symbols, separated by spaces).") % {"token": token})
    text = " ".join(tokens)
    if len(text) > SCOPE_MAX:
        raise ValidationError(ngettext("Scope is at most %(limit)s character.", "Scope is at most %(limit)s characters.", SCOPE_MAX) % {"limit": SCOPE_MAX})
    return text


def clean_path(value: str) -> str:
    """A JSON path of jsonpath's subset, as typed (without the spaces around it)."""
    text = value.strip()
    try:
        jsonpath.parse(text)
    except jsonpath.PathError as exc:
        raise ValidationError(str(exc)) from None
    return text


def clean_external_id(value) -> str:
    """An id at a provider, as text: stripped, 1 to EXTERNAL_ID_MAX characters, no control or invisible
    characters (an id with a text-direction override would read as another one in the activity log and in
    the user dialog). Compared exactly (models.key_of), so nothing else is changed."""
    if not isinstance(value, str):
        raise ValidationError(_("Should be text."))
    text = value.strip()
    if not text:
        raise ValidationError(_("is empty."))
    if len(text) > EXTERNAL_ID_MAX:
        raise ValidationError(ngettext("is too long (at most %(limit)s character).", "is too long (at most %(limit)s characters).", EXTERNAL_ID_MAX) % {"limit": EXTERNAL_ID_MAX})
    if any(is_invisible(char, text[i - 1] if i else "") for i, char in enumerate(text)):
        raise ValidationError(_("has control or invisible characters."))
    return text
