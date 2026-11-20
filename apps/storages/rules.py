"""How storage rules match paths: filters, folder passwords and readmes share one matcher (decision D9).

An expression is a glob over storage-absolute paths (the paths of apps/storages/drivers/paths.py):

- `*` matches any characters within one segment, `?` one character (never "/");
- `**` matches any characters across segments. A whole segment `**` stands for any number of
  segments, none included: `/a/**/b` is /a/b, /a/x/b and /a/x/y/b. At the end, `/a/**` is everything
  below /a, but not /a itself;
- every other character is itself: there are no character classes, braces or escapes;
- an expression without a leading "/" starts at the root (`*.txt` is `/*.txt`), and a trailing "/"
  is dropped.

So `/dir` is the folder itself, `/dir/*` what is directly inside it, `/dir/**` everything below it,
`*.txt` a .txt file at the top, and `**/*.txt` or `**.txt` a .txt file anywhere.

Paths and expressions are compared in canonical() form: NFC, case folded, "\\" taken for "/", and each
segment without its trailing dots and spaces, because Windows-like backends (OneDrive, SMB, NTFS, an FTP
or WebDAV server on Windows) take "Secret", "secret.", "secret " for the same folder and "secret\\x" for
a file in it. This over-matches on purpose: on a case-sensitive storage /Photos and /photos share their
rules, which is the safe side for a protection.

A rule *matches* the entry whose path its expression describes and *covers* that entry and everything
below it (apps/storages/access.py says which kind uses which).

Matching never backtracks without bound: an expression whose regular expression could take long on a long
path (two quantifiers competing for the same characters, like "*-*-*.bak" or "/a/**/b/**/c") runs as a
small automaton instead, in time proportional to the path's length.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterator

from django.utils.translation import gettext as _, ngettext

EXPRESSION_MAX = 512
_WILDCARDS = "*?"


class GlobError(ValueError):
    """An expression that can't be a rule; str() is the reason, in Chinese."""


def _fold(text: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", text).casefold())


def _segment(text: str) -> str:
    folded = _fold(text)
    # "secret." and "secret " name "secret" on Windows-like backends; a name of dots only stays.
    return folded.rstrip(". ") or folded


def canonical(path: str) -> str:
    """The form a clean storage path is compared in: "/Secret./B" -> "/secret/b", "/a\\b" -> "/a/b"."""
    if path == "/":
        return "/"
    return "/" + "/".join(_segment(part) for part in path.replace("\\", "/").split("/") if part)


def prefixes(path: str) -> Iterator[str]:
    """The path and every folder above it, from the root: "/a/b" -> "/", "/a", "/a/b"."""
    yield "/"
    if path == "/":
        return
    start = 1
    while True:
        end = path.find("/", start)
        if end < 0:
            yield path
            return
        yield path[:end]
        start = end + 1


def normalize_expression(expression: str) -> str:
    """The expression as stored and shown: trimmed, from the root, without a trailing "/" (raises GlobError)."""
    if not isinstance(expression, str):
        raise GlobError(_("The expression should be text."))
    text = expression.strip()
    if not text:
        raise GlobError(_("The expression can't be empty."))
    if len(text) > EXPRESSION_MAX:
        raise GlobError(ngettext("The expression is at most %(limit)s character.", "The expression is at most %(limit)s characters.", EXPRESSION_MAX) % {"limit": EXPRESSION_MAX})
    for char in text:
        category = unicodedata.category(char)
        if category in ("Cc", "Cs"):
            raise GlobError(_("The expression can't contain control characters such as line breaks or tabs."))
    parts = [part for part in text.replace("\\", "/").split("/") if part]  # "\\" is "/" (canonical())
    for part in parts:
        if part in (".", ".."):
            raise GlobError(_("The expression can't have a . or .. part: paths never have them."))
        if "***" in part:
            raise GlobError(_("At most two * in a row: * is any characters within a part, ** any number of parts."))
    stored = "/" + "/".join(parts)
    if len(stored) > EXPRESSION_MAX:  # as stored: the "/" put in front counts too (API-11)
        raise GlobError(ngettext("The expression is at most %(limit)s character (counting the leading /).", "The expression is at most %(limit)s characters (counting the leading /).", EXPRESSION_MAX) % {"limit": EXPRESSION_MAX})
    return stored


def _segment_regex(segment: str) -> str:
    out = []
    i = 0
    while i < len(segment):
        if segment.startswith("**", i):
            out.append(".*")
            i += 2
        elif segment[i] == "*":
            out.append("[^/]*")
            i += 1
        elif segment[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(segment[i]))
            i += 1
    return "".join(out)


def _regex_source(segments: list[str]) -> str:
    if not segments:
        return "/"
    parts = []
    for index, segment in enumerate(segments):
        if segment == "**":
            # Any number of whole segments; at the end at least one (below, not the folder itself).
            parts.append("/.+" if index == len(segments) - 1 else "(?:/.*)?")
        else:
            parts.append("/" + _segment_regex(segment))
    return "".join(parts)


def _regex_is_fast(segments: list[str]) -> bool:
    """Can Python's re (which backtracks) be trusted with these segments on a long path? Yes with at most
    one ** that isn't at the very end (there it takes the rest at once), at most one * per segment and no
    * in the segment of such a **: then no two quantifiers compete for the same characters."""
    crossing = 0
    last = len(segments) - 1
    for index, segment in enumerate(segments):
        if segment == "**":
            crossing += index != last
            continue
        inner = segment.count("**")
        if index == last and segment.endswith("**"):
            inner -= 1
        stars = segment.replace("**", "").count("*")
        if stars > 1 or (stars and inner):
            return False
        crossing += inner
    return crossing <= 1


# The automaton's steps: (_LIT, char) one character; (_ONE, crosses) any one character ("/" too when it
# crosses); (_LOOP, crosses) any number of them; (_SPLIT, index) an optional group that ends at index.
_LIT, _ONE, _LOOP, _SPLIT = range(4)


def _steps(segments: list[str]) -> list[tuple[int, object]]:
    if not segments:
        return [(_LIT, "/")]
    steps: list[tuple[int, object]] = []
    for index, segment in enumerate(segments):
        if segment == "**":
            if index == len(segments) - 1:
                steps += [(_LIT, "/"), (_ONE, True), (_LOOP, True)]  # "/.+"
            else:
                start = len(steps)
                steps += [(_SPLIT, None), (_LIT, "/"), (_LOOP, True)]  # "(?:/.*)?"
                steps[start] = (_SPLIT, len(steps))
            continue
        steps.append((_LIT, "/"))
        i = 0
        while i < len(segment):
            if segment.startswith("**", i):
                steps.append((_LOOP, True))
                i += 2
            else:
                char = segment[i]
                steps.append((_LOOP, False) if char == "*" else (_ONE, False) if char == "?" else (_LIT, char))
                i += 1
    return steps


class _Automaton:
    """An expression run over a path one character at a time, with the set of states it can be in as
    the bits of an int: no backtracking, so the time grows with the path's length only."""

    __slots__ = ("literal", "one_any", "one_segment", "loop_any", "loop_segment", "forward", "splits",
                 "start", "final")

    def __init__(self, steps: list[tuple[int, object]]):
        self.literal: dict[object, int] = {}
        self.one_any = self.one_segment = self.loop_any = self.loop_segment = self.forward = 0
        self.splits: list[tuple[int, int]] = []
        for index, (kind, argument) in enumerate(steps):
            bit = 1 << index
            if kind == _LIT:
                self.literal[argument] = self.literal.get(argument, 0) | bit
            elif kind == _ONE:
                if argument:
                    self.one_any |= bit
                else:
                    self.one_segment |= bit
            elif kind == _LOOP:
                if argument:
                    self.loop_any |= bit
                else:
                    self.loop_segment |= bit
                self.forward |= bit  # or none of them: on to the next step
            else:
                self.forward |= bit
                self.splits.append((bit, 1 << int(argument)))
        self.final = 1 << len(steps)
        self.start = self._close(1)

    def _close(self, states: int) -> int:
        while True:
            more = states | ((states & self.forward) << 1)
            for bit, target in self.splits:
                if more & bit:
                    more |= target
            if more == states:
                return states
            states = more

    def _step(self, states: int, char: str) -> int:
        if char == "/":
            advance = self.literal.get(char, 0) | self.one_any
            stay = self.loop_any
        else:
            advance = self.literal.get(char, 0) | self.one_any | self.one_segment
            stay = self.loop_any | self.loop_segment
        return self._close(((states & advance) << 1) | (states & stay))

    def run(self, text: str, *, prefixes: bool = False) -> int:
        """The states after `text`; with `prefixes`, the final state as soon as a folder above it matches."""
        states = self.start
        for index, char in enumerate(text):
            if prefixes and char == "/" and index and states & self.final:
                return self.final
            states = self._step(states, char)
            if not states:
                return 0
        return states


class Glob:
    """One compiled expression. Methods take canonical() paths."""

    __slots__ = ("expression", "pattern", "regex", "cover_regex", "automaton", "covers_root", "literal",
                 "shape", "window", "trailing")

    def __init__(self, expression: str):
        self.expression = normalize_expression(expression)
        segments = [_segment(part) for part in self.expression.split("/") if part]
        self.pattern = "/" + "/".join(segments)
        self.automaton = _Automaton(_steps(segments))
        self.regex = self.cover_regex = None
        if _regex_is_fast(segments):
            source = _regex_source(segments)
            self.regex = re.compile(source, re.DOTALL)
            self.cover_regex = re.compile(source + "(?:/.*)?", re.DOTALL)  # a folder above may match
        self.covers_root = self.matches("/")
        # What every path it matches starts with: the expression up to its first wildcard.
        cut = min((self.pattern.find(ch) for ch in _WILDCARDS if ch in self.pattern), default=len(self.pattern))
        self.literal = self.pattern[:cut]
        self.shape, self.window, self.trailing = _shape(segments)

    def __repr__(self):
        return f"Glob({self.expression!r})"

    def matches(self, path: str) -> bool:
        if self.regex is not None:
            return self.regex.fullmatch(path) is not None
        return bool(self.automaton.run(path) & self.automaton.final)

    def covers(self, path: str) -> bool:
        """Matches the path or a folder above it."""
        if self.covers_root:
            return True
        if self.cover_regex is not None:
            return self.cover_regex.fullmatch(path) is not None
        return bool(self.automaton.run(path, prefixes=True) & self.automaton.final)

    def may_cover_within(self, folder: str) -> bool:
        """Could match the folder, something above it or something below it? Decided from the expression
        alone, without looking at the storage, so it may say yes when nothing there matches (the safe
        side): the static check of folder operations (decision D12)."""
        if self.covers(folder):
            return True
        states = self.automaton.run(folder if folder == "/" else folder + "/")
        return bool(states & ~self.automaton.final)

    def protects_within(self, folder: str) -> bool:
        """Could deleting, renaming, moving or copying the folder as a whole take something this rule
        covers out of its reach? A rule tied to a place (/a/private, /*/x, /a/**/x) may cover anything
        below a folder it may cover within. One that starts with ** follows what it covers:

        - by name ("**/private", "**.mp4", "**/*.bak": its last segment only): wherever the folder goes,
          what is named so inside it stays covered, so only the folder itself counts;
        - by a run of segments ("**/private/**", "**/videos/*.mp4", "**/x/a/secret"): when the folder's
          own last segments begin that run (/x/private, /a/videos, /r/x/a) the move breaks it;
        - anything else that starts with ** (two of them, ** inside a later segment): every folder.

        Deleting a folder deletes what a by-name rule covers in it: that is not looked for (it would
        mean listing the whole folder first)."""
        if self.covers(folder):
            return True
        if self.shape == "name":
            return False
        if self.shape == "window":
            names = [] if folder == "/" else folder[1:].split("/")
            longest = min(len(self.window) if self.trailing else len(self.window) - 1, len(names))
            for count in range(1, longest + 1):
                tail = names[len(names) - count:]
                if all(compile_glob("/" + self.window[i]).matches("/" + tail[i]) for i in range(count)):
                    return True
            return False
        return self.may_cover_within(folder)


def _shape(segments: list[str]) -> tuple[str, tuple[str, ...], bool]:
    """("anchored" | "name" | "window" | "other", the window's segments, whether a whole ** follows them)."""
    if not segments or not segments[0].startswith("**"):
        return "anchored", (), False
    first = segments[0]
    if first != "**":
        # ** at the start of the first segment ("**.mp4"): by name if nothing else crosses segments.
        if len(segments) == 1 and "**" not in first[2:]:
            return "name", (), False
        return "other", (), False
    rest = segments[1:]
    if not rest:
        return "name", (), False  # "/**": everything below the root
    trailing = rest[-1] == "**"
    body = tuple(rest[:-1] if trailing else rest)
    if not body or any("**" in segment for segment in body):
        return "other", (), False
    if len(body) == 1 and not trailing:
        return "name", (), False
    return "window", body, trailing


@functools.lru_cache(maxsize=4096)
def compile_glob(expression: str) -> Glob:
    return Glob(expression)


def problem(expression: str) -> str:
    """Why `expression` can't be a rule; "" when it can."""
    try:
        compile_glob(expression)
    except GlobError as error:
        return str(error)
    return ""
