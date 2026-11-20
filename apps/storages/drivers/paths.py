"""Storage paths: clean what users send, join and split, check new names.

A clean path starts with "/", has no empty, "." or ".." segments and no trailing "/"; the root
is "/". clean() makes one from user input, or raises InvalidPath. check_name() is for names the
user gives to something new (a folder, an upload, a rename); names that already exist on the
backend are used as they are, so files the site can't create can still be listed, downloaded,
renamed and deleted.
"""

from __future__ import annotations

import unicodedata

from django.utils.translation import gettext, ngettext

from .base import InvalidPath

ROOT = "/"


def dots_only(segment: str) -> bool:
    """A segment made of dots and spaces only: ".", "..", but also "...", ".. " or ". .". Servers on
    Windows drop the dots and spaces at the end of a name, so they may take such a segment for ".."
    (the folder above); no real name needs one."""
    return segment.strip(" .") == ""


def clean(path: str | None) -> str:
    """A clean storage path from user input: "", "/", "a", "/a/", "//a//b" are fine; segments of dots
    and spaces only (".", "..", ".. ", dots_only()) and NUL are refused. Backslashes are ordinary
    characters here."""
    if path is None or path == "":
        return ROOT
    if not isinstance(path, str):
        raise InvalidPath(gettext("The path should be text."))
    if "\x00" in path:
        raise InvalidPath(gettext("The path can't contain NUL characters."))
    if any("\ud800" <= ch <= "\udfff" for ch in path):
        # A lone surrogate can't be encoded: not in a URL, not in JSON, not on a backend.
        raise InvalidPath(gettext("The path has invalid characters."))
    parts = [part for part in path.split("/") if part]
    for part in parts:
        if dots_only(part):
            raise InvalidPath(gettext("The path can't have a part of only dots and spaces, like . or .."))
    return "/" + "/".join(parts)


def is_clean(path: str) -> bool:
    try:
        return clean(path) == path
    except InvalidPath:
        return False


def join(folder: str, *names: str) -> str:
    """folder + names; each name is one segment (no "/")."""
    path = folder
    for name in names:
        if not name or "/" in name or dots_only(name):
            raise InvalidPath(gettext("Invalid name."))
        path = (path.rstrip("/") or "") + "/" + name
    return path or ROOT


def split(path: str) -> tuple[str, str]:
    """(parent folder, name); split("/") is ("/", "")."""
    if path == ROOT:
        return ROOT, ""
    parent, _, name = path.rpartition("/")
    return parent or ROOT, name


def parent(path: str) -> str:
    return split(path)[0]


def name(path: str) -> str:
    return split(path)[1]


def segments(path: str) -> list[str]:
    return [part for part in path.split("/") if part]


def is_inside(path: str, folder: str) -> bool:
    """path is folder itself or something below it."""
    if folder == ROOT:
        return True
    return path == folder or path.startswith(folder + "/")


def relative(path: str, folder: str) -> str:
    """path below folder, as a path rooted at folder: relative("/a/b/c", "/a") is "/b/c"."""
    if not is_inside(path, folder):
        raise InvalidPath(gettext("The path isn't inside this folder."))
    if folder == ROOT:
        return path
    rest = path[len(folder):]
    return rest or ROOT


def rebase(path: str, folder: str) -> str:
    """The inverse of relative(): rebase("/b/c", "/a") is "/a/b/c"."""
    if folder == ROOT:
        return path
    if path == ROOT:
        return folder
    return folder + path


# Unicode's Bidi_Control characters: the text-direction marks, embeddings, overrides and isolates. A new
# name with one can show as another: "invoice\u202etxt.exe" is listed as "invoiceexe.txt" (SEC-07).
# The rest of Cf stays allowed: the ZWJ of emoji sequences, the ZWNJ of Persian words.
BIDI_CONTROLS = frozenset("\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def _bad_char(ch: str) -> bool:
    if ch in "/\\" or ch in BIDI_CONTROLS:
        return True
    category = unicodedata.category(ch)
    # Control characters (C0, DEL, C1), and lone surrogates that can't be encoded.
    return category in ("Cc", "Cs")


def check_name(new_name: str, *, forbidden: str = "", max_bytes: int = 255) -> str:
    """A name the user gives something new, checked; returns it unchanged. Raises InvalidPath."""
    if not isinstance(new_name, str) or new_name == "":
        raise InvalidPath(gettext("The name can't be empty."))
    if new_name.strip() == "":
        raise InvalidPath(gettext("The name can't be only spaces."))
    if dots_only(new_name):
        raise InvalidPath(gettext("The name can't be only dots and spaces (like . or ..)."))
    for ch in new_name:
        if _bad_char(ch):
            if ch == "/":
                raise InvalidPath(gettext("The name can't contain /."))
            if ch == "\\":
                raise InvalidPath(gettext("The name can't contain \\."))
            if ch in BIDI_CONTROLS:
                raise InvalidPath(gettext("The name can't contain text direction marks (found U+%(code)s): they make a name show as something else.") % {"code": format(ord(ch), "04X")})
            raise InvalidPath(gettext("The name can't contain control characters such as line breaks or tabs."))
        if ch in forbidden:
            raise InvalidPath(gettext("Names in this kind of storage can't contain these characters: %(chars)s") % {"chars": ' '.join(forbidden)})
    if len(new_name.encode("utf-8")) > max_bytes:
        raise InvalidPath(ngettext("The name is too long: at most %(limit)s byte (a Chinese character counts as 3).", "The name is too long: at most %(limit)s bytes (a Chinese character counts as 3).", max_bytes) % {"limit": max_bytes})
    return new_name
