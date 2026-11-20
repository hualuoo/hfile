"""A storage source's driver params: checking what the admin typed, storing it, reading it back.

The driver declares its params (apps/storages/drivers/base.py, Param); `StorageSource.config` stores them
as JSON. Params marked `secret` are stored encrypted with apps.core.secrets ("enc:..." or "" for not set) and
are write-only: the API says whether one is set, never what it is, and a secret left empty on the form
keeps its value. A secret number (115's token expiry) is stored as the text of the number.

clean() checks the values of a form against the params, one message in Chinese per param, then the
driver's own rules across params (validate_config). encode() and decode() turn a checked config into
the stored form and back.
"""

from __future__ import annotations

import os
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from django.conf import settings
from django.utils.translation import gettext as _, ngettext, pgettext

from apps.core import secrets as core_secrets

from .drivers import DRIVERS, UnknownType, driver_class
from .drivers.base import Param, StorageDriver

TEXT_MAX = 2048
TEXTAREA_MAX = 65536
NUMBER_LIMIT = 10**15  # JSON numbers beyond this lose precision in browsers

# Types whose downloads always go through the site (no URL of the backend for the browser): the
# admin may give a download domain instead, which then serves domain + the storage path (a web
# server or CDN in front of the same files).
DOWNLOAD_DOMAIN_TYPES = frozenset({"local", "ftp", "sftp", "webdav"})


class ConfigError(Exception):
    def __init__(self, errors: dict[str, str]):
        super().__init__(errors)
        self.errors = errors


def driver(type_: str) -> type[StorageDriver]:
    """The driver class of a type; ConfigError for an unknown one."""
    try:
        return driver_class(type_)
    except UnknownType:
        raise ConfigError({"type": _("Unknown storage type.")}) from None
    except ImportError as error:  # run from the source code without the library the driver needs (STOR-01)
        raise ConfigError({"type": _("This storage type can't be used now: the program "
                                     "is missing a library it needs (%(module)s).") % {"module": error.name or error}}) from None


def known_type(type_: str) -> bool:
    return type_ in DRIVERS


def params(cls: type[StorageDriver]) -> dict[str, Param]:
    return {param.key: param for param in cls.params}


def secret_keys(cls: type[StorageDriver]) -> list[str]:
    return [param.key for param in cls.params if param.secret]


def setup_keys(cls: type[StorageDriver]) -> list[str]:
    """The params the add wizard's second step asks for, in the form's order: the driver's setup_params
    it has (a subclass may have fewer), or its required ones; never what 授权 fills (auth_params)."""
    wanted = cls.setup_params if cls.setup_params is not None else [p.key for p in cls.params if p.required]
    return [p.key for p in cls.params if p.key in wanted and p.key not in cls.auth_params]


def _empty(value: Any) -> bool:
    return value is None or value == ""


def _has_control(text: str, allowed: str = "") -> bool:
    return any(unicodedata.category(ch) in ("Cc", "Cs") and ch not in allowed for ch in text)


def check_value(param: Param, value: Any) -> Any:
    """The value of one param as the driver gets it: str, int, bool or an option; raises ValueError
    with the message for the form. None stands for "not given"."""
    kind = param.kind
    if value is None:
        return None
    if kind == "bool":
        if not isinstance(value, bool):
            raise ValueError(_("Should be on or off."))
        return value
    if kind == "number":
        if isinstance(value, bool):
            raise ValueError(_("Please enter an integer."))
        if isinstance(value, str):
            text = value.strip()
            if text == "":
                return None
            digits = text[1:] if text.startswith("-") else text
            if not (digits.isascii() and digits.isdigit()):
                raise ValueError(_("Please enter an integer."))
            value = int(text)
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int):
            raise ValueError(_("Please enter an integer."))
        if abs(value) > NUMBER_LIMIT:
            raise ValueError(_("This number is too large."))
        if param.minimum is not None and value < param.minimum:
            raise ValueError(_("Can't be less than %(minimum)s.") % {"minimum": param.minimum})
        if param.maximum is not None and value > param.maximum:
            raise ValueError(_("Can't be more than %(maximum)s.") % {"maximum": param.maximum})
        return value
    if not isinstance(value, str):
        raise ValueError(_("Should be text."))
    if kind == "select":
        values = [option for option, _label in param.options]
        if value not in values:
            raise ValueError(_("Please choose one from the list."))
        return value
    if kind == "textarea":
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if len(text) > TEXTAREA_MAX:
            raise ValueError(ngettext("At most %(limit)s character.", "At most %(limit)s characters.", TEXTAREA_MAX) % {"limit": TEXTAREA_MAX})
        if _has_control(text, "\n\t"):
            raise ValueError(_("It can't contain control characters."))
        return text
    # text, password, path. A password, key or token is kept as typed: spaces at its ends may be part of
    # it, and an FTP, SFTP or WebDAV login with them failed once they were cut (STOR-14). Spaces alone
    # are still "left empty" (a secret keeps its stored value).
    text = value if kind == "password" and value.strip() else value.strip()
    if len(text) > TEXT_MAX:
        raise ValueError(ngettext("At most %(limit)s character.", "At most %(limit)s characters.", TEXT_MAX) % {"limit": TEXT_MAX})
    if _has_control(text):
        raise ValueError(_("It can't contain control characters such as line breaks or tabs."))
    return text


def clean(cls: type[StorageDriver], submitted: Mapping[str, Any] | None, *,
          stored: Mapping[str, Any] | None = None, draft: Mapping[str, Any] | None = None,
          clear: set[str] | frozenset[str] = frozenset(), partial: bool = False,
          later: set[str] | frozenset[str] = frozenset()) -> dict[str, Any]:
    """The full config (plain values) from a form. Raises ConfigError({param key: message}).

    `stored` is the storage's current config (decode()d), `draft` what an authorisation or a QR-code
    login put on the server for this form (apps/storages/drafts.py). A secret left empty keeps the draft's
    value, else the stored one; `clear` names secrets to empty instead. With `partial` (a PATCH) a
    param the form leaves out keeps its stored value; otherwise it gets its default. `later` names
    required params that may stay empty for now (the add wizard's later steps fill them).
    """
    submitted = dict(submitted or {})
    stored = dict(stored or {})
    draft = dict(draft or {})
    defined = params(cls)
    errors: dict[str, str] = {}
    for key in submitted:
        if key not in defined:
            errors[key] = _("This kind of storage has no such setting.")
    for key in clear:
        if key not in defined or not defined[key].secret:
            errors[key] = _("Only secret settings can be cleared.")
    config: dict[str, Any] = {}
    for key, param in defined.items():
        raw = submitted.get(key)
        try:
            value = None if _empty(raw) else check_value(param, raw)
        except ValueError as error:
            errors[key] = str(error)
            continue
        if value == "":
            value = None
        if value is None:
            if param.secret:
                value = None if key in clear else draft.get(key, stored.get(key))
            elif partial and key not in submitted:
                value = stored.get(key, param.default)
            else:
                value = param.default  # left empty: the default
        if _empty(value) and param.required and key not in later:
            errors[key] = _('Please fill in "%(label)s".') % {"label": param.label}
            continue
        if param.kind == "bool":
            value = bool(param.default) if value is None else value
        config[key] = value
    if errors:
        raise ConfigError(errors)
    try:
        more = dict(cls.validate_config(config) or {})
    except Exception as error:  # noqa: BLE001 - a driver's bug must not become a 500 on the form
        more = {"": _("Checking the settings failed: %(error)s") % {"error": error}}
    more.update(site_problems(cls.type, config))
    more = {key: str(message) for key, message in more.items() if message}
    if more:
        raise ConfigError(more)
    return config


def encode_value(param: Param, value: Any) -> Any:
    if param.secret:
        return "" if _empty(value) else core_secrets.encrypt(str(value))
    return value


def encode(cls: type[StorageDriver], config: Mapping[str, Any]) -> dict[str, Any]:
    """The stored form of a checked config. A param it leaves out (or None) is stored with its default,
    as clean() would give it: an S3 storage written without is_private must stay private, not read as
    False (public)."""
    stored = {}
    for key, param in params(cls).items():
        value = config.get(key)
        stored[key] = encode_value(param, param.default if value is None else value)
    return stored


def decode_value(param: Param, stored: Any) -> Any:
    """The plain value of one stored param; raises apps.core.secrets.UnreadableSecret."""
    if not param.secret:
        return stored
    text = core_secrets.decrypt(stored if isinstance(stored, str) else "")
    if text == "":
        return None
    if param.kind == "number":
        try:
            return int(text)
        except ValueError:
            return None
    return text


def decode(cls: type[StorageDriver], stored: Mapping[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """(plain config, keys of secrets that can't be read): a secret encrypted under another
    secret_key reads as not set, and is listed so the form can ask for it again."""
    stored = stored or {}
    config: dict[str, Any] = {}
    unreadable: list[str] = []
    for key, param in params(cls).items():
        if key not in stored:
            continue
        try:
            config[key] = decode_value(param, stored[key])
        except core_secrets.UnreadableSecret:
            unreadable.append(key)
    return config, unreadable


def shown(cls: type[StorageDriver], stored: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, str]]:
    """What the admin API shows of a stored config: (the values that aren't secret, {secret key:
    "set" | "unset" | "unreadable"})."""
    stored = stored or {}
    values: dict[str, Any] = {}
    secrets_state: dict[str, str] = {}
    for key, param in params(cls).items():
        if param.secret:
            raw = stored.get(key)
            if not raw:
                secrets_state[key] = "unset"
                continue
            try:
                core_secrets.decrypt(raw)
                secrets_state[key] = "set"
            except core_secrets.UnreadableSecret:
                secrets_state[key] = "unreadable"
        else:
            values[key] = stored.get(key, param.default)
    return values, secrets_state


# --- the site's own checks of some params --------------------------------------------------------------

def _own_folders() -> list[tuple[Path, str]]:
    """Folders the site's own files live in, which no local storage may serve."""
    found: list[tuple[Path, str]] = []
    config_file = getattr(settings, "CONFIG_FILE", None)
    if config_file:
        found.append((Path(config_file).resolve().parent, _("settings file config.toml")))
    database = settings.DATABASES.get("default", {})
    if database.get("ENGINE", "").endswith("sqlite3") and database.get("NAME") not in (None, "", ":memory:"):
        name = str(database["NAME"])
        if not name.startswith("file:"):
            found.append((Path(name).resolve().parent, pgettext("inside a sentence", "database file")))  # alone: the installer's field
    for setting, label in (("BACKUP_DIR", _("database backups")), ("MEDIA_ROOT", _("uploaded files")), ("UPLOAD_SPOOL_DIR", _("files being uploaded"))):
        value = getattr(settings, setting, None)
        if value:
            found.append((Path(value).resolve(), label))
    found.append((Path(settings.BASE_DIR).resolve(), _("program files")))
    return found


def site_problems(type_: str, config: Mapping[str, Any]) -> dict[str, str]:
    """Rules of the site on top of the driver's: a local storage must not serve the site's own files
    (config.toml holds the secret key and the database password)."""
    if type_ != "local":
        return {}
    root = config.get("root")
    if not isinstance(root, str) or not os.path.isabs(root):
        return {}
    folder = Path(os.path.realpath(root))
    for own, label in _own_folders():
        try:
            if _inside(own, folder):
                return {"root": _("This folder has HFile's own %(label)s (%(folder)s) and can't "
                                  "be a storage source: please choose a folder just for files.") % {"label": label, "folder": own}}
        except (OSError, ValueError):
            continue
    return {}


def _inside(own: Path, folder: Path) -> bool:
    """`own` is `folder` or inside it: by the path, or by the file system's own identity of the folders,
    which also catches another spelling on a case-insensitive disk (macOS, Windows)."""
    if own == folder or own.is_relative_to(folder):
        return True
    try:
        target = folder.stat()
    except OSError:
        return False
    if not target.st_ino:
        return False  # a file system without inode numbers: the path comparison is all there is
    for candidate in (own, *own.parents):
        try:
            found = candidate.stat()
        except OSError:
            continue
        if (found.st_dev, found.st_ino) == (target.st_dev, target.st_ino):
            return True
    return False
