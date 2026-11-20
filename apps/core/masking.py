"""Secrets in addresses, kept out of the access log (apps/core/access_log.py) and the console (apps/core/logs.py).

Apps register what in their addresses is secret, from their AppConfig.ready():

- a pattern of the path, whose group "secret" is replaced by <label>; it is searched in the whole
  address (the query decoded too, as in ?next=/s/…), so the group should stop at / ? & and #:

      masking.register_path(r"/s/(?P<secret>[^/?&#]+)", "key")       /s/Ab3dE/  ->  /s/<key>/

- query parameters by name (any case): their values become <name>:

      masking.register_query("sign")                                  ?sign=9f8e  ->  ?sign=<sign>

The Django admin's random path is always masked as /<admin>. The query parameters below are masked
from the start: the installer's ?token=, signatures, OAuth's code and state, passwords.
"""

import re
import threading
from urllib.parse import unquote, unquote_plus

from django.conf import settings

_lock = threading.Lock()
_paths = []  # (compiled pattern, label)
_queries = set()  # lower-case names
_query_re = None  # rebuilt when a name is added


def register_path(pattern, label="secret"):
    """Masks the group "secret" of `pattern` (a regex) wherever it matches an address, as <label>."""
    compiled = re.compile(pattern)
    if "secret" not in compiled.groupindex:
        raise ValueError(f'{pattern!r} has no group named "secret"')
    with _lock:
        if (compiled.pattern, label) not in [(p.pattern, l) for p, l in _paths]:
            _paths.append((compiled, label))


def register_query(*names):
    """Masks the values of these query parameters: ?name=<name>."""
    global _query_re
    with _lock:
        _queries.update(name.lower() for name in names)
        alternatives = "|".join(re.escape(name) for name in sorted(_queries, key=len, reverse=True))
        # In free text (a log message): name=value after ? or & (or &amp; in HTML), up to the next &, #,
        # space or quote. A value masked already (<name>) is left as it is.
        _query_re = re.compile(rf"([?&](?:amp;)?(?:{alternatives})=)(?!<)[^&#\s\"']*", re.IGNORECASE)


register_query("token", "sign", "code", "state", "password", "pwd")


def _admin(text):
    path = getattr(settings, "ADMIN_PATH", "")
    if not path:
        return text
    return re.sub(rf"/{re.escape(path)}(?=[/?&#\s]|$)", "/<admin>", text)


def _paths_masked(text):
    for pattern, label in list(_paths):
        text = pattern.sub(lambda match, label=label: _replace_group(match, label), text)
    return text


def _replace_group(match, label):
    start, end = match.span("secret")
    if start < 0:  # the group didn't take part
        return match.group(0)
    whole = match.group(0)
    offset = match.start(0)
    return whole[: start - offset] + f"<{label}>" + whole[end - offset :]


def mask_query(query):
    """A raw query string (as sent, still %-encoded) made readable, with the registered parameters'
    values masked. Masked before decoding: an encoded & in a value can't hide part of it."""
    parts = []
    for part in query.split("&"):
        name, equals, _value = part.partition("=")
        if unquote_plus(name, errors="replace").strip().lower() in _queries and equals:
            parts.append(f"{unquote_plus(name, errors='replace')}=<{unquote_plus(name, errors='replace').strip().lower()}>")
        else:
            parts.append(unquote(part, errors="replace"))
    return "&".join(parts)


def mask_address(path, query=""):
    """The address of a request for the access log: its path (decoded, as WSGI gives it) and query
    (raw), with every secret masked, also in the decoded values (?next=/install/%3Ftoken%3D…)."""
    return mask_text(path + ("?" + mask_query(query) if query else ""))


def mask_text(text):
    """Any text (a log message): the admin's path, the registered paths and query parameters masked."""
    text = _admin(text)
    text = _paths_masked(text)
    if _query_re is not None:
        text = _query_re.sub(lambda match: match.group(1) + f"<{_query_name(match.group(1))}>", text)
    return text


def _query_name(prefix):
    return prefix.lstrip("?&").removeprefix("amp;").rstrip("=").lower()
