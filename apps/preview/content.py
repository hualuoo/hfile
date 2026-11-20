"""Reading a file for a viewer on the server, and the addresses handed to kkFileView."""

from __future__ import annotations

import base64
import codecs
import logging
import re
import uuid
from urllib.parse import quote

from rest_framework.exceptions import APIException, ErrorDetail

from django.utils.translation import gettext as _

from apps.files.service import driver_problem
from apps.storages import instances
from apps.storages.access import STORAGE_DISABLED, AccessDenied
from apps.storages.drivers.base import DriverError, NotFound

logger = logging.getLogger("hfile.preview")

# Markdown is rendered on the server (apps.core.markdown, about 4 MB a second of CPU): at most this much,
# and less when the site's 文本预览的最大文件 is smaller. Larger files open as text in the browser.
MARKDOWN_MAX = 2 * 1024 * 1024


class PreviewError(APIException):
    """An answer with a stable code the viewers branch on: {"detail", "code", ...more}."""

    status_code = 400

    def __init__(self, code, message, *, status=400, **extra):
        super().__init__(message, code)
        self.status_code = status
        self.code = code
        self.detail = {"detail": ErrorDetail(message, code), "code": code, **extra}


def fmt_size(size):
    """1.5 MB, 512 KB, 300 B: as the pages show sizes."""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{int(value)} B" if unit == "B" else f"{value:.3g} {unit}"
        value /= 1024
    return f"{value:.3g} GB"


def markdown_limit(site):
    return min(site.text_preview_max_kb * 1024, MARKDOWN_MAX)


def _driver(source):
    try:
        return instances.get(source)
    except instances.StorageUnavailable as error:
        raise AccessDenied(STORAGE_DISABLED, error.message) from None


def read_capped(source, path, limit, *, staff=False):
    """The bytes of the file at `path`, at most `limit` of them: PreviewError too_large when it has
    more. Read through the storage's driver and closed as soon as it is known to be too large.
    A storage's failure: what the backend said only for `staff`, as the file API does; else the log."""
    driver = _driver(source)
    try:
        entry = driver.stat(path)
        if entry.is_dir:
            raise PreviewError("not_a_file", _("This is a folder."))
        if entry.size is not None and entry.size > limit:
            raise _too_large(entry.size, limit)
        if entry.size == 0:
            return b""
        stream = driver.open(path)
        try:
            chunks, total = [], 0
            for chunk in stream:
                total += len(chunk)
                if total > limit:
                    raise _too_large(entry.size or total, limit)
                chunks.append(chunk)
        finally:
            stream.close()
    except NotFound:
        raise PreviewError("not_found", _("The file doesn't exist; it may have been moved or deleted."), status=404) from None
    except DriverError as error:
        _code, message, _status = driver_problem(error, staff=staff)
        logger.warning("Preview of %s on storage %s failed: %s", path, source.key, error.message)
        raise PreviewError("storage_error", message, status=502) from None
    return b"".join(chunks)


def _too_large(size, limit):
    return PreviewError(
        "too_large", _("The file is too large (%(size)s); the page shows at most %(limit)s.") % {"size": fmt_size(size), "limit": fmt_size(limit)}, size=size, limit=limit,
    )


def decode(data: bytes) -> str:
    """Text by its byte-order mark, else UTF-8 when it is valid, else GB18030 (the viewers' guess)."""
    for bom, encoding in ((codecs.BOM_UTF8, "utf-8"), (codecs.BOM_UTF16_LE, "utf-16-le"), (codecs.BOM_UTF16_BE, "utf-16-be")):
        if data.startswith(bom):
            return data[len(bom):].decode(encoding, "replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("gb18030", "replace")


# --- kkFileView (inventory 7.4) ---------------------------------------------------------------------------

_KK_SUFFIX = re.compile(r"[a-z0-9]{1,16}")


def suffix(name: str) -> str:
    """The text after the last dot, lower case; "" without one."""
    return name.rsplit(".", 1)[1].lower() if "." in name else ""


def kkfileview_url(server: str, file_url: str, name: str) -> str:
    """<server>/onlinePreview?url=<base64 of the file's URL>: what kkFileView opens. The file's URL gets
    fullfilename=<random>.<suffix>, from which kkFileView picks its converter, without learning the
    file's name; url= is encodeURIComponent(base64(UTF-8))."""
    ext = suffix(name)
    random_name = uuid.uuid4().hex + (f".{ext}" if _KK_SUFFIX.fullmatch(ext) else "")
    full = f"{file_url}{'&' if '?' in file_url else '?'}fullfilename={random_name}"
    encoded = base64.b64encode(full.encode("utf-8")).decode("ascii")
    return f"{server.rstrip('/')}/onlinePreview?url={quote(encoded, safe='')}"
