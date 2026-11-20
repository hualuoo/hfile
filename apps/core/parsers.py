"""The site's JSON: JSONParser for the API (settings.REST_FRAMEWORK), and parse_object() for a body of a
known shape that may be larger than the API's 2.5 MB (the text editor's save, apps/uploads/views.py).
Both say in Chinese what is wrong with bad JSON and where (json_error())."""

import codecs
import io
import json
import math
import re
from json import JSONDecodeError, JSONDecoder
from json.decoder import WHITESPACE, scanstring

from rest_framework import serializers
from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser as DRFJSONParser
from rest_framework.parsers import get_encoding

from django.utils.translation import gettext as _, gettext_lazy, ngettext

MAX_CONTAINERS = 50_000  # objects and arrays in one body: 10 000 grants of a user, the most one takes, are 30 001

_LONE_SURROGATE = re.compile("[\ud800-\udfff]")
_LONE_SURROGATE_MESSAGE = gettext_lazy("Invalid JSON: an unpaired UTF-16 surrogate (\\ud800 to \\udfff).")


def _has_lone_surrogate(data):
    """Whether a string anywhere in the parsed JSON (keys too) holds half of a UTF-16 pair."""
    stack = [data]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            if not item.isascii() and _LONE_SURROGATE.search(item):  # C speed: the editor's text is MBs
                return True
        elif isinstance(item, dict):
            stack.extend(item.keys())
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
    return False


class _Refused(ValueError):
    """JSON the site doesn't take; the text says why."""


def _constant(name):
    """NaN, Infinity and -Infinity, refused with REST_FRAMEWORK["STRICT_JSON"] (DRF's default)."""
    raise _Refused(_("%(name)s isn't allowed (JSON has no such number)") % {"name": name})


def _float(text):
    """A number with a fraction or an exponent. One too large for a float was infinity, which the strict JSON
    refuses written as Infinity (API-08)."""
    value = float(text)
    if math.isinf(value):
        raise _Refused(_("the number %(text)s is too large") % {"text": text[:30]})
    return value


def _hooks(strict):
    return {"parse_constant": _constant, "parse_float": _float} if strict else {}


# json's messages (Python 3.12, the C scanner's and json.decoder's), as they go on after "第 1 行第 9 列".
_JSON_ERRORS = {
    "Expecting value": gettext_lazy("a value is missing"),
    "Expecting property name enclosed in double quotes": gettext_lazy("expected a field name in double quotes"),
    "Expecting ':' delimiter": gettext_lazy("expected a colon (:)"),
    "Expecting ',' delimiter": gettext_lazy("expected a comma (,)"),
    "Extra data": gettext_lazy("extra content: the JSON had already ended"),
    "Unterminated string starting at": gettext_lazy("a string starting here isn't closed"),
    "Invalid control character at": gettext_lazy("an unescaped control character"),
    "Invalid \\escape": gettext_lazy("an unknown escape (after \\)"),
    "Invalid \\uXXXX escape": gettext_lazy("a malformed \\uXXXX escape"),
}


def json_error(exc: ValueError) -> ParseError:
    """Bad JSON, said in Chinese: what is wrong and where. DRF passed json's English on (TEXT-02)."""
    if isinstance(exc, _Refused):
        return ParseError(_("Invalid JSON: %(exc)s.") % {"exc": exc})
    if isinstance(exc, UnicodeDecodeError):
        return ParseError(_("Invalid JSON: not %(encoding)s text (byte %(n)s).") % {"encoding": exc.encoding.upper(), "n": exc.start + 1})
    if not isinstance(exc, JSONDecodeError):  # int(): more digits than Python reads (sys.get_int_max_str_digits())
        return ParseError(_("Invalid JSON: an integer has too many digits."))
    if exc.msg.startswith("Unexpected UTF-8 BOM"):
        return ParseError(_("Invalid JSON: it can't start with a BOM (U+FEFF)."))
    return ParseError(_("Invalid JSON at line %(line)s, column %(column)s: %(problem)s.") % {"line": exc.lineno, "column": exc.colno, "problem": _JSON_ERRORS.get(exc.msg, exc.msg)})


_STRING = re.compile(r'"[^"\\]*+(?:\\.[^"\\]*+)*+"', re.S)


def _containers(text) -> int:
    """How many objects and arrays the JSON has (at least): counted outside its strings once the text has more
    { and [ than MAX_CONTAINERS."""
    found = text.count("{") + text.count("[")
    if found > MAX_CONTAINERS:
        outside = _STRING.sub("", text)
        found = outside.count("{") + outside.count("[")
    return found


class JSONParser(DRFJSONParser):
    """DRF's JSON parser, saying in Chinese what is wrong (json_error()), and with more kinds of bad JSON
    answered 400 like the rest:

    - nested deeper than Python can follow ([[[[...): DRF only turns ValueError into a 400; this was a 500;
    - "\\ud800" and the like, half of a UTF-16 pair: JSON allows the escape, but the text can't be
      written out as UTF-8 again, so an error message that repeats the value (DRF's "is not a valid
      choice") was a 500. Whole pairs ("\\ud83d\\ude00") come in as the one character they are;
    - a number too large for a float (1e400), with STRICT_JSON, as NaN and Infinity (API-08);
    - more than MAX_CONTAINERS objects and arrays: parsed, one takes some 60 bytes of memory for its 2 or 3
      bytes of JSON, and a 2.5 MB body of [{}, {}, ...] took 60 MB (SEC-09).
    """

    def parse(self, stream, media_type=None, parser_context=None):
        text = decode(stream.read(), parser_context)
        if _containers(text) > MAX_CONTAINERS:
            raise ParseError(ngettext("Invalid JSON: too many objects and arrays, at most %(limit)s.", "Invalid JSON: too many objects and arrays, at most %(limit)s.", MAX_CONTAINERS) % {"limit": MAX_CONTAINERS})
        try:
            data = json.loads(text, **_hooks(self.strict))
        except RecursionError as exc:
            raise ParseError(_("Invalid JSON: nested too deeply.")) from exc
        except ValueError as exc:
            raise json_error(exc) from None
        if _has_lone_surrogate(data):
            raise ParseError(_LONE_SURROGATE_MESSAGE)
        return data


# -- a body of a known shape

_NOT_AN_OBJECT = gettext_lazy("Invalid JSON: expected an object ({…}).")


def fields_of(serializer) -> dict:
    """The keys a serializer reads, as parse_object() takes them: None for a value, a dict of the same
    kind for a nested serializer's object."""
    return {name: fields_of(field) if isinstance(field, serializers.Serializer) else None
            for name, field in serializer.fields.items() if not field.read_only}


def decode(raw: bytes, parser_context=None) -> str:
    """A JSON body's text, decoded as DRF's JSONParser decodes it (Content-Type's charset, through a codecs
    StreamReader, which also drops a character cut off at the very end)."""
    context = parser_context or {}
    try:
        encoding = get_encoding(context)
    except ParseError:  # DRF's message is English
        raise ParseError(_("The charset %(charset)s in Content-Type isn't supported.") % {"charset": context.get("encoding")}) from None
    try:
        return codecs.getreader(encoding)(io.BytesIO(raw)).read()
    except ValueError as exc:
        raise json_error(exc) from None


def parse_object(text: str, fields: dict) -> dict:
    """The JSON object in `text`, which may only have the keys in `fields` (fields_of()), each at most once;
    every value a string, a number, true, false or null, or where `fields` has a dict for the key, also an
    object of those keys (one level deeper, same rules).

    Anything else is refused (ParseError, a message in Chinese) where it starts, before the rest is read:
    an unknown or repeated key, an array anywhere, an object where it doesn't belong. The general parser
    builds every element before a serializer looks at the keys, an object for every 3 bytes of
    {"junk": [{}, {}, ...]}; here the work and the memory are the strings' and no more. The strings and
    numbers are json's own (its C scanner), so a long text stays fast.

    Otherwise this is JSONParser: what passes comes out the same, and bad JSON gets JSONParser's message
    for the same place: the object's syntax below is the C scanner's (_parse_object_unicode, Python 3.12)
    step by step. NaN and Infinity follow REST_FRAMEWORK["STRICT_JSON"]; half UTF-16 pairs are refused.
    A string or a number instead of the object is refused once its own syntax is checked.
    """
    decoder = JSONDecoder(**_hooks(JSONParser.strict))
    try:
        if text.startswith("\ufeff"):  # as json.loads()
            raise JSONDecodeError("Unexpected UTF-8 BOM (decode using utf-8-sig)", text, 0)
        data, end = _value(text, _skip(text, 0), fields, decoder.scan_once, "")
        end = _skip(text, end)
        if end != len(text):
            raise JSONDecodeError("Extra data", text, end)
    except ValueError as exc:
        raise json_error(exc) from None
    if _has_lone_surrogate(data):
        raise ParseError(_LONE_SURROGATE_MESSAGE)
    if not isinstance(data, dict):
        raise ParseError(_NOT_AN_OBJECT)
    return data


def _skip(text, end):
    return WHITESPACE.match(text, end).end()


def _value(text, end, fields, scan_once, name):
    """The value at `end` and the index after it: an object only where `fields` is a dict, an array never."""
    char = text[end:end + 1]
    if char == "[":
        raise ParseError(_("Invalid JSON: %(name)s can't be an array.") % {"name": name} if name else _NOT_AN_OBJECT)
    if char == "{":
        if fields is None:
            raise ParseError(_("Invalid JSON: %(name)s can't be an object.") % {"name": name})
        return _object(text, end + 1, fields, scan_once, name)
    try:
        return scan_once(text, end)
    except StopIteration as stop:
        raise JSONDecodeError("Expecting value", text, stop.value) from None


def _object(text, end, fields, scan_once, name):
    """The object from just after its "{" and the index after its "}"."""
    values = {}
    end = _skip(text, end)
    if text[end:end + 1] == "}":
        return values, end + 1
    while True:
        if text[end:end + 1] != '"':
            raise JSONDecodeError("Expecting property name enclosed in double quotes", text, end)
        key, end = scanstring(text, end + 1, True)
        if key not in fields:  # not repeated in the message: it may be anything, of any length
            allowed = {"name": name, "fields": _(", ").join(fields)}
            if name:
                raise ParseError(_("Invalid JSON: unknown field in %(name)s; allowed are %(fields)s.") % allowed)
            raise ParseError(_("Invalid JSON: unknown field; allowed are %(fields)s.") % allowed)
        path = f"{name}.{key}" if name else key
        if key in values:
            raise ParseError(_("Invalid JSON: %(path)s appears more than once.") % {"path": path})
        end = _skip(text, end)
        if text[end:end + 1] != ":":
            raise JSONDecodeError("Expecting ':' delimiter", text, end)
        values[key], end = _value(text, _skip(text, end + 1), fields[key], scan_once, path)
        end = _skip(text, end)
        if text[end:end + 1] == "}":
            return values, end + 1
        if text[end:end + 1] != ",":
            raise JSONDecodeError("Expecting ',' delimiter", text, end)
        end = _skip(text, end + 1)
