"""The paths a provider's settings point into its userinfo answer with: a small subset of JSONPath,
enough for the plain fields providers have ("sub", "id", "data.login", "$.user['display-name']",
"emails[0]").

    a   a.b   $.a.b   ['a']   $['a']['b']   ["a"]   [0]   a.b[2].c

A leading $ is the answer itself. Names after a dot are anything but . [ ] quotes and spaces; any
other key goes in brackets and quotes, with \\ before a quote or a backslash in it. Indexes count from
0. Nothing else of JSONPath (wildcards, filters, .., negative indexes, functions).

What a path finds is used only when it is a string or an integer (not true/false, a decimal, null, an
object or a list): as text, stripped; a blank one counts as missing.
"""

from __future__ import annotations

import re

from django.utils.translation import gettext as _, ngettext

MAX_LENGTH = 200
MAX_STEPS = 20
MAX_INDEX = 10_000

_NAME = re.compile(r"[^.\[\]'\"\s]+")
_INDEX = re.compile(r"[0-9]+")


class PathError(ValueError):
    """What is wrong with a path, in Chinese (the admin form shows it)."""


def parse(path: str) -> tuple[str | int, ...]:
    """The steps of `path`: a key (str) of an object or an index (int) of a list. Raises PathError."""
    if not isinstance(path, str):
        raise PathError(_("Should be text."))
    if not path:
        raise PathError(_("Can't be empty."))
    if len(path) > MAX_LENGTH:
        raise PathError(ngettext("At most %(limit)s character.", "At most %(limit)s characters.", MAX_LENGTH) % {"limit": MAX_LENGTH})
    steps: list[str | int] = []
    i = 0
    if path[0] == "$":
        i = 1
        if i == len(path):
            raise PathError(_("$ alone is the whole answer; point to a field inside it, like $.sub."))
    elif path[0] == ".":
        raise PathError(_("Can't start with \".\": write sub or $.sub."))
    elif path[0] != "[":
        match = _NAME.match(path, 0)
        if match is None:
            raise PathError(_("Character 1, \"%(char)s\", is out of place (write a field name with special characters as ['name']).") % {"char": path[0]})
        steps.append(match.group())
        i = match.end()
    while i < len(path):
        char = path[i]
        if char == ".":
            match = _NAME.match(path, i + 1)
            if match is None:
                raise PathError(_("The . at character %(n)s must be followed by a field name.") % {"n": i + 1})
            steps.append(match.group())
            i = match.end()
        elif char == "[":
            step, i = _bracket(path, i)
            steps.append(step)
        else:
            raise PathError(_('Character %(n)s, "%(char)s", is out of place (write '
                              "a field name with special characters as ['name']).") % {"n": i + 1, "char": char})
        if len(steps) > MAX_STEPS:
            raise PathError(ngettext("At most %(limit)s level.", "At most %(limit)s levels.", MAX_STEPS) % {"limit": MAX_STEPS})
    return tuple(steps)


def _bracket(path: str, start: int) -> tuple[str | int, int]:
    """The step in brackets at `start` ("[" there) and where it ends."""
    i = start + 1
    if i < len(path) and path[i] in "'\"":
        quote = path[i]
        i += 1
        name = []
        while i < len(path) and path[i] != quote:
            if path[i] == "\\" and i + 1 < len(path) and path[i + 1] in (quote, "\\"):
                i += 1
            name.append(path[i])
            i += 1
        if i + 1 >= len(path) or path[i + 1] != "]":
            raise PathError(_("The [%(quote)s…%(quote)s] starting at character %(n)s isn't finished.") % {"n": start + 1, "quote": quote})
        return "".join(name), i + 2
    match = _INDEX.match(path, i)
    if match is None or match.end() >= len(path) or path[match.end()] != "]":
        raise PathError(_("The [ ] starting at character %(n)s needs an index (like [0]) or a quoted field name (like ['name']).") % {"n": start + 1})
    index = int(match.group())
    if index > MAX_INDEX:
        raise PathError(_("The index is at most %(max)s.") % {"max": MAX_INDEX})
    return index, match.end() + 1


_MISSING = object()


def find(data, steps):
    """What `steps` point at in `data`, or _MISSING."""
    current = data
    for step in steps:
        if isinstance(step, int):
            if not isinstance(current, list) or step >= len(current):
                return _MISSING
        elif not isinstance(current, dict) or step not in current:
            return _MISSING
        current = current[step]
    return current


def text_at(data, steps) -> tuple[str | None, str]:
    """(text, "") for a string or an integer at `steps`, stripped; (None, why not) otherwise, the reason
    in Chinese ("没有这个字段", "是空的", "是一个对象", ...)."""
    value = find(data, steps)
    if value is _MISSING:
        return None, _("doesn't exist")
    if isinstance(value, bool):
        return None, _("is true or false, not text or an integer")
    if isinstance(value, int):
        return str(value), ""
    if isinstance(value, str):
        value = value.strip()
        return (value, "") if value else (None, _("is empty"))
    if value is None:
        return None, _("is null")
    if isinstance(value, float):
        return None, _("is a decimal, not text or an integer")
    if isinstance(value, dict):
        return None, _("is an object, not text or an integer")
    if isinstance(value, list):
        return None, _("is a list, not text or an integer")
    return None, _("isn't text or an integer")
