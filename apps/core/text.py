"""Clean-up for short texts that people type: names, descriptions, announcements."""

import re
import unicodedata

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

# Control and invisible characters (zero-width spaces, text-direction overrides, ...).
# They can hide text or make one name look like another.
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
# Characters that show as nothing (or as a blank) but aren't in those categories: the rest of
# Unicode's Default_Ignorable_Code_Point (combining grapheme joiner, variation selectors, Hangul
# fillers, Khmer inherent vowels, Mongolian selectors, the tag characters) and the blank Braille
# pattern. "管͏理员" and "adminㅤ" would otherwise pass for other names.
INVISIBLE_RANGES = (
    (0x034F, 0x034F),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x2800, 0x2800),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0D),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
# U+FE0E and U+FE0F pick the text or emoji style of the symbol before them ("❤️", "1️⃣"): fine
# after a symbol, digit or punctuation, invisible after anything else ("管理️员").
_STYLE_SELECTORS = ("\ufe0e", "\ufe0f")
_STYLED = frozenset({"So", "Sm", "Sk", "Sc", "Nd", "Po"})


def is_invisible(char: str, previous: str = "") -> bool:
    """Whether `char` (coming after `previous`) is a control character or doesn't show."""
    if unicodedata.category(char) in INVISIBLE_CATEGORIES:
        return True
    if char in _STYLE_SELECTORS:
        return not previous or unicodedata.category(previous) not in _STYLED
    code = ord(char)
    return any(low <= code <= high for low, high in INVISIBLE_RANGES)


def from_header(value: str) -> str:
    """A request header's value as the client sent it. WSGI hands over the bytes as latin-1 (PEP 3333);
    browsers and API clients send UTF-8 (API-14). Left as it is when it isn't UTF-8."""
    try:
        return value.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return value


def single_line(value: str) -> str:
    """NFC, trimmed, and every run of whitespace (newlines too) turned into one space."""
    return " ".join(unicodedata.normalize("NFC", value).split())


def multi_line(value: str) -> str:
    """NFC, LF line endings, no spaces at line ends, at most one empty line in a row, trimmed."""
    text = unicodedata.normalize("NFC", value).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def reject_invisible(value: str, what: str, allowed: str = "") -> None:
    """Raise if `value` has a control or invisible character other than those in `allowed`."""
    previous = ""
    for char in value:
        if char not in allowed and is_invisible(char, previous):
            raise ValidationError(
                _("The %(what)s can't contain control or invisible characters "
                  "(such as zero-width or text direction marks); found U+%(code)s.") % {"what": what, "code": format(ord(char), "04X")}
            )
        previous = char
