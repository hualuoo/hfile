"""The compiled translations, made from config/locale/<language>/LC_MESSAGES/*.po by scripts/i18n.py:

- the .mo files next to them, which Django's gettext reads (and config/i18n.py before Django runs);
- web/i18n/<language>.js, the scripts' catalog for each of settings.LANGUAGES: Django's JavaScript
  catalog (gettext(), ngettext(), interpolate()...) with the djangojs messages. {% js_catalog %}
  links the page's.

apps/web/test_i18n.py checks that both are what the .po files make now.

The .mo files are written here rather than by GNU msgfmt, so that compiling needs nothing but Python.
Entries that are fuzzy or not translated yet are left out, as msgfmt does.
"""

import ast
import struct
from pathlib import Path

from django.conf import settings
from django.utils import translation
from django.views.i18n import JavaScriptCatalog

JS_DIR = Path(__file__).resolve().parent / "static" / "web" / "i18n"


def po_files() -> list[Path]:
    return sorted(po for folder in settings.LOCALE_PATHS for po in Path(folder).glob("*/LC_MESSAGES/*.po"))


def read_po(text: str) -> list[dict]:
    """The entries of a .po file as makemessages writes it (one blank line between entries):
    {"msgid", "msgstr": [...], "fuzzy"}, and "msgctxt", "msgid_plural" when there are. Obsolete
    entries (#~) are left out."""
    entries = []
    for block in text.split("\n\n"):
        entry, field = {"msgstr": [], "fuzzy": False}, None
        for line in block.strip().splitlines():
            line = line.strip()
            if line.startswith("#"):
                if line.startswith("#,") and "fuzzy" in line[2:].replace(",", " ").split():
                    entry["fuzzy"] = True
                continue
            if line.startswith('"'):
                if field == "msgstr":
                    entry["msgstr"][-1] += ast.literal_eval(line)
                else:
                    entry[field] += ast.literal_eval(line)
                continue
            keyword, _, rest = line.partition(" ")
            if keyword.startswith("msgstr"):
                entry["msgstr"].append(ast.literal_eval(rest))
                field = "msgstr"
            else:
                entry[keyword] = ast.literal_eval(rest)
                field = keyword
        if "msgid" in entry:
            entries.append(entry)
    return entries


def mo_bytes(po_text: str) -> bytes:
    """The .mo file of a .po file's text: its header and every translated entry that isn't fuzzy."""
    pairs = {}
    for entry in read_po(po_text):
        strings = entry["msgstr"]
        header = entry["msgid"] == "" and "msgctxt" not in entry
        if not header and (entry["fuzzy"] or not all(strings)):
            continue
        key = entry["msgid"]
        if "msgid_plural" in entry:
            key += "\0" + entry["msgid_plural"]
        if "msgctxt" in entry:
            key = entry["msgctxt"] + "\x04" + key
        pairs[key.encode()] = "\0".join(strings).encode()
    keys = sorted(pairs)
    originals_at = 7 * 4
    translations_at = originals_at + len(keys) * 8
    data_at = translations_at + len(keys) * 8
    table, data = [], b""
    for strings in (keys, [pairs[key] for key in keys]):
        for string in strings:
            table.append(struct.pack("<2I", len(string), data_at + len(data)))
            data += string + b"\0"
    head = struct.pack("<7I", 0x950412DE, 0, len(keys), originals_at, translations_at, 0, data_at)
    return head + b"".join(table) + data


class _Catalog(JavaScriptCatalog):
    """Django's JavaScript catalog of LOCALE_PATHS alone, without the djangojs messages of Django's apps."""

    packages = ["hfile"]

    def get_paths(self, packages):
        return [str(folder) for folder in settings.LOCALE_PATHS]


def js_catalog(language: str) -> bytes:
    with translation.override(language):
        return _Catalog().get(None).content


def compile_all() -> list[Path]:
    """Writes the .mo files and the scripts' catalogs; returns those that changed."""
    changed = []

    def write(path: Path, content: bytes):
        if not path.exists() or path.read_bytes() != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            changed.append(path)

    for po in po_files():
        write(po.with_suffix(".mo"), mo_bytes(po.read_text(encoding="utf-8")))
    for code, _name in settings.LANGUAGES:
        write(JS_DIR / f"{code}.js", js_catalog(code))
    return changed
