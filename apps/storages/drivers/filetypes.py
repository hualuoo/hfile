"""The type of a file by its name (mimetypes.guess_type()), the same wherever the site runs: from mime.types next to
this file, Debian's list, and Python's own for what that hasn't. Not the system's: Linux distributions have other
lists or none, and Windows keeps its in the registry, where any program installed may change a type (.js as
text/plain, .xml as text/xml) and many the site previews are missing (.mkv, .flac, .docx)."""

import mimetypes
from pathlib import Path

_types = mimetypes.MimeTypes(filenames=[Path(__file__).with_name("mime.types")])


def guess_type(name: str, strict: bool = True) -> tuple[str | None, str | None]:
    """mimetypes.guess_type(name, strict) from the lists above: (type, encoding), each None when unknown."""
    return _types.guess_type(name, strict=strict)
