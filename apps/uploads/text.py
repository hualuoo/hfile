"""The encodings the text editor saves in (PUT /api/v1/uploads/text/).

The browser decodes a file with TextDecoder, whose encodings are the WHATWG ones; saving encodes the
text back in the same one. Python's codecs that match them: WHATWG's "big5" is Big5-HKSCS, "shift_jis"
is Windows-31J (cp932), "euc-kr" is Windows-949, "gbk" is decoded as GB18030 but encoded as GBK.
UTF-16 is written with its byte order mark (TextDecoder drops it on reading); UTF-8 only with
"utf-8-bom".

The rule: what is saved reads back in the browser as the text that was sent, or the save is refused
(EncodeProblem). Python's codecs differ from the browsers' tables (the WHATWG Encoding Standard's
indexes) in a few places, checked character by character against those indexes:
- Big5: browsers read A1FE as ／ and A240 as ＼ (so does Windows' cp950), which big5hkscs writes as A241
  and A242, read back as ∕ and ﹨: those four characters are written with the browsers' bytes.
- GB18030: the 2005 edition (and browsers) swapped ḿ U+1E3F and U+E7C7 of the 2000 tables Python has.
- Characters an encoding lacks, which a codec writes as a look-alike (cp932's ¢ as ￠, big5hkscs's •
  as ‧), or as bytes browsers read as something else (private-use characters): refused. The look-alikes
  Python's own decoder notices are found by decoding again; the rest are listed in `refused`.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from functools import cached_property
from typing import Mapping

from django.utils.translation import gettext as _, gettext_lazy


@dataclass(frozen=True)
class Encoding:
    name: str  # what the API takes and answers
    label: str
    codec: str  # Python's
    bom: bytes = b""
    special: Mapping[str, bytes] = field(default_factory=dict)  # written as the bytes browsers read as them
    swap: Mapping[int, int] = field(default_factory=dict)  # code points exchanged before encoding
    refused: str = ""  # characters browsers would read back as others

    @cached_property
    def special_pattern(self) -> re.Pattern | None:
        return re.compile("(" + "|".join(map(re.escape, self.special)) + ")") if self.special else None

    @cached_property
    def refused_pattern(self) -> re.Pattern | None:
        return re.compile("[" + re.escape(self.refused) + "]") if self.refused else None


BIG5_SPECIAL = {"\uff0f": b"\xa1\xfe", "\uff3c": b"\xa2\x40", "\u2215": b"\xa2\x41", "\ufe68": b"\xa2\x42"}
# Typed characters each codec writes as bytes the browsers read as another character (found by checking
# every code point against the WHATWG indexes, after the fixes above and the round trip in encode()):
BIG5_REFUSED = "\u00a2\u00a3\u00a5\u2022\u203e\u223c\u2609\u2641\uff64"  # ¢ £ ¥ • ‾ ∼ ☉ ♁ ､
GB18030_REFUSED = ("\ue5e5\ue78d\ue78e\ue78f\ue790\ue791\ue792\ue793\ue794\ue795\ue796\ue81e\ue826\ue82b"
                   "\ue82c\ue832\ue843\ue854\ue864")  # private use: 2000's places of characters 2005 added
SHIFT_JIS_REFUSED = "\uf8f0\uf8f1\uf8f2\uf8f3"  # private use, single bytes browsers don't read


ENCODINGS = {
    item.name: item for item in [
        Encoding("utf-8", "UTF-8", "utf-8"),
        Encoding("utf-8-bom", gettext_lazy("UTF-8 (with BOM)"), "utf-8", codecs.BOM_UTF8),
        Encoding("gbk", "GBK", "gbk"),
        Encoding("gb18030", "GB18030", "gb18030", swap={0x1E3F: 0xE7C7, 0xE7C7: 0x1E3F}, refused=GB18030_REFUSED),
        Encoding("big5", "Big5", "big5hkscs", special=BIG5_SPECIAL, refused=BIG5_REFUSED),
        Encoding("shift_jis", "Shift_JIS", "cp932", refused=SHIFT_JIS_REFUSED),
        Encoding("euc-kr", "EUC-KR", "cp949"),
        Encoding("utf-16le", "UTF-16LE", "utf-16-le", codecs.BOM_UTF16_LE),
        Encoding("utf-16be", "UTF-16BE", "utf-16-be", codecs.BOM_UTF16_BE),
        *[Encoding(f"windows-{n}", f"Windows-{n}", f"cp{n}") for n in range(1250, 1259)],
    ]
}

# Other names browsers and people use for them.
ALIASES = {
    "utf8": "utf-8", "unicode-1-1-utf-8": "utf-8", "utf-8-sig": "utf-8-bom",
    "gb2312": "gbk", "x-gbk": "gbk", "cp936": "gbk", "chinese": "gbk",
    "big5-hkscs": "big5", "x-x-big5": "big5",
    "shift-jis": "shift_jis", "sjis": "shift_jis", "windows-31j": "shift_jis", "ms932": "shift_jis",
    "cp949": "euc-kr", "windows-949": "euc-kr",
    "utf-16": "utf-16le", "ucs-2": "utf-16le",
    "iso-8859-1": "windows-1252", "latin1": "windows-1252", "ascii": "windows-1252", "us-ascii": "windows-1252",
}

NEWLINES = {"lf": "\n", "crlf": "\r\n", "cr": "\r", "\n": "\n", "\r\n": "\r\n", "\r": "\r"}

# Unicode's own encodings write every character as bytes that read back as it: nothing to check by
# decoding them again (FILE-04).
_EXACT = frozenset({"utf-8", "utf-16-le", "utf-16-be"})


class EncodeProblem(ValueError):
    """The text can't be written in that encoding; the message (Chinese) says where."""


def lookup(name: str) -> Encoding | None:
    """The encoding of a name, whatever its case, "_" or "-"; None for one the editor doesn't offer."""
    if not isinstance(name, str):
        return None
    key = name.strip().lower().replace("_", "-")
    key = ALIASES.get(key, key)
    return ENCODINGS.get(key)


def _place(text: str, position: int) -> str:
    """Where text[position] is, as the messages say it: 第 2 行第 5 个字符."""
    before = text[:position]
    line = before.count("\n") + 1
    column = position - (before.rfind("\n") + 1) + 1
    return _("line %(line)s, column %(column)s") % {"line": line, "column": column}


def _problem(text: str, position: int, encoding: Encoding) -> EncodeProblem:
    char = text[position]
    shown = char if char.isprintable() else f"U+{ord(char):04X}"
    return EncodeProblem(_("\"%(shown)s\" at %(where)s can't be saved in %(label)s. "
                           "Please choose another encoding (UTF-8, say).") % {"where": _place(text, position), "shown": shown, "label": encoding.label})


def nul_problem(text: str) -> str:
    """Why the editor can't save `text`, if it holds NUL (U+0000), saying where (FILE-03); "" when it doesn't."""
    position = text.find("\x00")
    if position < 0:
        return ""
    return _("There is a NUL character (U+0000) at %(where)s: the online editor "
             "can't save text with NUL characters. Please delete it first.") % {"where": _place(text, position)}


def _changed_by(char: str, encoding: Encoding) -> bool:
    try:
        return char.encode(encoding.codec, "strict").decode(encoding.codec, "strict") != char
    except UnicodeError:
        return False  # only encodable with its neighbour (Big5's letters with a combining mark)


def _encode_part(part: str, text: str, offset: int, encoding: Encoding) -> bytes:
    try:
        data = part.encode(encoding.codec, "strict")
    except UnicodeEncodeError as error:
        raise _problem(text, offset + error.start, encoding) from None
    if encoding.codec not in _EXACT and data.decode(encoding.codec, "strict") != part:
        # A look-alike written for a character the encoding lacks: refused, never another text.
        position = next((index for index, char in enumerate(part) if _changed_by(char, encoding)), 0)
        raise _problem(text, offset + position, encoding)
    return data


def encode(text: str, encoding: Encoding, newline: str | None = None) -> bytes:
    """The bytes of the file, which the browser reads back as `text`; EncodeProblem names the first
    character that can't be written. The editor's textarea gives "\\n" line breaks: `newline` makes every
    line break the file's own ("\\r\\n" for a file from Windows)."""
    if newline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if newline != "\n":
            text = text.replace("\n", newline)
    if encoding.refused_pattern is not None:
        found = encoding.refused_pattern.search(text)
        if found:
            raise _problem(text, found.start(), encoding)
    # One code point for another, so the offsets stay. Only when there's one of them: translate() is a copy, and
    # half a second for 5 MB (FILE-04).
    swapped = any(chr(code) in text for code in encoding.swap)
    source = text.translate(encoding.swap) if swapped else text
    parts = encoding.special_pattern.split(source) if encoding.special_pattern is not None else [source]
    out = [encoding.bom] if encoding.bom else []
    offset = 0
    for index, part in enumerate(parts):
        if index % 2:  # one of `special`
            out.append(encoding.special[part])
        elif part:
            out.append(_encode_part(part, text, offset, encoding))
        offset += len(part)
    return out[0] if len(out) == 1 else b"".join(out)  # one piece is the file itself, not copied again (FILE-04)
