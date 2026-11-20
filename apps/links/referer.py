"""Referer 防盗链 for path links and short links (inventory 9.6): off, an allowlist or a blocklist of the
pages a link may be opened from, and whether a request without a Referer passes (referer_allow_empty).
Only the links check it: once a link has redirected to the storage's own URL, nothing can.

The rules are siteconf's text, one per line, each starting with http://, https:// or *://
(apps.siteconf.serializers checks that when they are saved):

    https://*.example.com          example.com and every host under it, any page
    *://example.com                example.com over http or https
    https://example.com/**         every page of example.com (the same as without "/**")
    https://example.com/docs/*     the pages directly in /docs/

- The scheme: http, https, or * for either.
- The host: * stands for any characters, and a leading "*." for nothing too, so that the domain itself
  matches as well. Without a :port the rule takes any port (a page moved to :8443 doesn't escape a
  blocklist); with one, only that port (the scheme's own is the same as none). A trailing dot of a name
  doesn't count (example.com. is example.com), and a name in another script is compared as punycode, the
  way browsers send it.
- The path: none (or a bare "/") is any page. Otherwise the Referer's path must match it as a whole: *
  stands for any characters but "/", ** for any characters at all, and a ** that is a whole segment for
  no segment too (/a/**/b is also /a/b, /a/** also /a). The query counts only when the rule has a "?".
- Letters compare without case.

Matching doesn't backtrack: the host is matched piece by piece with str.find, the path by an automaton
that reads it once, so a request costs time in proportion to its Referer, which is cut off at
REFERER_MAX characters: a longer one isn't matched and counts as refused in both modes. Rules are
compiled once per text of the setting.
"""

from __future__ import annotations

import functools
from urllib.parse import urlsplit

from django.utils.translation import gettext as _

from apps.siteconf.models import RefererType

try:  # IDNA 2008 / UTS 46, as browsers encode names (httpx's dependency); the standard library's is IDNA 2003
    import idna as _idna
except ImportError:  # pragma: no cover - httpx brings it
    _idna = None

REFERER_MAX = 2048
_SCHEMES = ("http", "https")
_DEFAULT_PORTS = {"http": ":80", "https": ":443"}


def header(request) -> str:
    """The Referer of a request as text: WSGI hands headers over as latin-1, browsers send UTF-8."""
    raw = request.META.get("HTTP_REFERER", "")
    try:
        return raw.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return raw


# --- hosts ----------------------------------------------------------------------------------------

def _pieces(pattern: str) -> list[str]:
    """A host pattern split at its *s (a run of them is one)."""
    pieces = [""]
    for char in pattern:
        if char == "*":
            if pieces[-1] or len(pieces) == 1:
                pieces.append("")
        else:
            pieces[-1] += char
    return pieces


def _host_match(pieces: list[str], text: str) -> bool:
    """Whether `text` as a whole matches a pattern split at its *s. Linear: the first piece begins the
    text, the last ends it, the others are found in order; with * the only wildcard the leftmost place
    each piece fits is always right."""
    if len(pieces) == 1:
        return text == pieces[0]
    first, last = pieces[0], pieces[-1]
    if len(text) < len(first) + len(last) or not text.startswith(first) or not text.endswith(last):
        return False
    position, end = len(first), len(text) - len(last)
    for piece in pieces[1:-1]:
        found = text.find(piece, position, end)
        if found < 0:
            return False
        position = found + len(piece)
    return True


def _label(label: str) -> str:
    if not label or "*" in label or label.isascii():
        return label
    try:
        if _idna is not None:
            return _idna.encode(label, uts46=True).decode("ascii")
        return label.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):  # idna's errors are UnicodeErrors and ValueErrors
        return label


def _without_port(host: str) -> str:
    if host.startswith("["):
        return host.partition("]")[0] + "]"
    return host.partition(":")[0]


def _host(text: str, scheme: str) -> str:
    """A host[:port] as compared: lower case, punycode, no trailing dot, no default port of `scheme`. (lower(),
    not casefold(): that would make "ß" "ss" before IDNA sees it.)"""
    text = text.lower()
    if text.startswith("["):  # an IPv6 literal
        name, port = text.partition("]")[0] + "]", text.partition("]")[2]
    else:
        name, colon, port = text.partition(":")
        port = colon + port
        name = ".".join(_label(label) for label in name.split(".")).rstrip(".") or name
    if port == _DEFAULT_PORTS.get(scheme):
        port = ""
    return name + port


# --- paths ----------------------------------------------------------------------------------------

_SEGMENT, _ANY = 1, 2  # the steps of a pattern besides its characters: * and **


class PathGlob:
    """A path pattern run as an automaton over the text, one character at a time, with the set of its states
    as the bits of an int: no backtracking, whatever the pattern."""

    __slots__ = ("literal", "segment_loops", "any_loops", "loops", "jumps", "start", "final")

    def __init__(self, pattern: str):
        steps: list[str | int] = []
        index = 0
        while index < len(pattern):
            if pattern[index] == "*":
                end = index
                while end < len(pattern) and pattern[end] == "*":
                    end += 1
                steps.append(_ANY if end - index > 1 else _SEGMENT)
                index = end
            else:
                steps.append(pattern[index])
                index += 1
        steps = _whole_any_once(steps)
        self.literal: dict[str, int] = {}
        self.segment_loops = self.any_loops = 0
        # A whole-segment ** may be no segment at all: "/a/**/b" is "/a/b" too, "/a/**" is "/a". From the "/"
        # before such a ** straight to what follows it: two steps on, without reading a character.
        self.jumps = 0
        for position, step in enumerate(steps):
            bit = 1 << position
            if step == _SEGMENT:
                self.segment_loops |= bit
            elif step == _ANY:
                self.any_loops |= bit
                if _whole(steps, position):
                    self.jumps |= bit >> 1
            else:
                self.literal[step] = self.literal.get(step, 0) | bit
        self.loops = self.segment_loops | self.any_loops
        self.final = 1 << len(steps)
        self.start = self._close(1)

    def _close(self, states: int) -> int:
        # A star may match nothing (on to the next step), a whole-segment ** no segment (two steps on). These
        # moves only go forward and can't chain far (runs of stars are one step, "/**/**" is one **), so a
        # few rounds of a handful of int operations do it, whatever the pattern.
        while True:
            more = states | ((states & self.loops) << 1) | ((states & self.jumps) << 2)
            if more == states:
                return states
            states = more

    def match(self, text: str) -> bool:
        states = self.start
        literal, any_loops = self.literal, self.any_loops
        stay_in_segment = self.any_loops | self.segment_loops
        for char in text:
            stay = any_loops if char == "/" else stay_in_segment
            states = self._close(((states & literal.get(char, 0)) << 1) | (states & stay))
            if not states:
                return False
        return bool(states & self.final)


def _whole(steps: list, position: int) -> bool:
    """Whether the ** at `position` is a whole segment: after a "/", and before a "/" or the end."""
    return (steps[position] == _ANY and position > 0 and steps[position - 1] == "/"
            and (position + 1 == len(steps) or steps[position + 1] == "/"))


def _whole_any_once(steps: list) -> list:
    """The steps with "/**/**" (whole segments both) as "/**": the same paths, and no chains of jumps."""
    out: list = []
    for position, step in enumerate(steps):
        if (_whole(steps, position) and len(out) >= 3 and out[-1] == "/" and out[-2] == _ANY
                and out[-3] == "/"):
            out.pop()
            continue
        out.append(step)
    return out


# --- rules ----------------------------------------------------------------------------------------

class Rule:
    __slots__ = ("text", "hosts", "path", "query")

    def __init__(self, text: str):
        self.text = text
        scheme, separator, rest = text.partition("://")
        scheme = scheme.lower()
        if not separator or scheme not in (*_SCHEMES, "*"):
            raise ValueError(text)
        host, slash, path = rest.partition("/")
        # scheme -> (pieces, the pieces without a leading "*.", whether any port goes) for each scheme it takes
        self.hosts = {}
        for each in (_SCHEMES if scheme == "*" else (scheme,)):
            name = _host(host, each)
            self.hosts[each] = (_pieces(name), _pieces(name[2:]) if name.startswith("*.") else None,
                                _without_port(name) == name)
        self.path = PathGlob("/" + path.casefold()) if slash and path else None
        self.query = "?" in path

    def matches(self, scheme: str, host: str, path: str, query: str) -> bool:
        hosts = self.hosts.get(scheme)
        if hosts is None:
            return False
        pieces, bare, any_port = hosts
        if any_port:
            host = _without_port(host)
        if not (_host_match(pieces, host) or (bare is not None and _host_match(bare, host))):
            return False
        if self.path is None:
            return True
        return self.path.match(f"{path}?{query}" if self.query else path)


class Rules:
    def __init__(self, text: str):
        self.rules = []
        for line in (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                self.rules.append(Rule(line))
            except ValueError:  # not a rule any more; the settings page refuses such lines
                continue

    def match(self, referer: str) -> Rule | None:
        """The first rule that matches the URL, or None (also for one that isn't an http(s) URL)."""
        parts = _parts(referer)
        if parts is None:
            return None
        for rule in self.rules:
            if rule.matches(*parts):
                return rule
        return None


def _parts(referer: str) -> tuple[str, str, str, str] | None:
    """(scheme, host[:port], path, query) of a Referer, as rules compare them."""
    try:
        parts = urlsplit(referer.strip())
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _SCHEMES:
        return None
    host = parts.netloc.rpartition("@")[2]
    if not host:
        return None
    return scheme, _host(host, scheme), (parts.path or "/").casefold(), parts.query.casefold()


@functools.lru_cache(maxsize=8)
def compiled(text: str) -> Rules:
    return Rules(text)


def refusal(request, site) -> str:
    """Why the site's Referer rules refuse this request (Chinese, for the error page), or "" when they let
    it through."""
    mode = site.referer_type
    if mode not in (RefererType.ALLOWLIST, RefererType.BLOCKLIST):
        return ""
    referer = header(request).strip()
    if not referer:
        return "" if site.referer_allow_empty else _("This link can't be opened directly: the site has hotlink protection on. Please open it from an allowed web page.")
    if len(referer) > REFERER_MAX:
        return _("This link can't be opened from this web page: the referring page's address (Referer) is too long.")
    hit = compiled(site.referer_rules).match(referer)
    if mode == RefererType.ALLOWLIST and hit is None:
        return _("This link can't be opened from this web page: the site has hotlink protection on and only allows certain sites.")
    if mode == RefererType.BLOCKLIST and hit is not None:
        return _("This link can't be opened from this web page: the site has hotlink protection on, and this site is on its block list.")
    return ""
