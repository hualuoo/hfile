"""The spool: where the chunks of a proxied upload wait until the last one has come.

settings.UPLOAD_SPOOL_DIR, the folder upload-spool next to config.toml. It is not [paths] uploads
(MEDIA_ROOT), which is served at /media/ and which a reverse proxy may serve by itself: nothing serves
this folder, and its files have names no app registers with apps.core.uploads. The folder is made with mode
700, each file with mode 600 (O_EXCL, O_NOFOLLOW): one file "<session id>.part" per session.

A chunk is written at the offset the session has received so far, then the file is cut there and
synced, before the session counts it: a chunk that failed halfway is simply written again.
"""

from __future__ import annotations

import os
import shutil
import stat as st
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from django.conf import settings
from django.utils.translation import gettext as _

from config import private

SUFFIX = ".part"
PIECE = 1024 * 1024  # bytes per read from the request and per write
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: else os.write() turns each \n into \r\n


class SpoolError(Exception):
    """The spool can't be used (missing, not a folder, a file that isn't ours). str(error) is for the
    visitor: Chinese, without the server's paths. `detail` (the path, the system's words) is only for
    the log (uploads.service._spool_error)."""

    def __init__(self, message: str, detail: str = ""):
        super().__init__(message)
        self.detail = detail


class ShortBody(Exception):
    """The request's body ended before the chunk did."""


def folder() -> Path:
    """The spool folder, made (mode 700) if missing."""
    path = Path(settings.UPLOAD_SPOOL_DIR)
    try:
        private.mkdir(path)
    except OSError as error:
        raise SpoolError(_("The folder for uploads in progress couldn't be created. "
                           "Please ask the administrator to check the site's log."), f"{path}: {error.strerror or error}") from None
    if not path.is_dir():
        raise SpoolError(_("The folder for uploads in progress can't be used. "
                           "Please ask the administrator to check the site's log."), f"{path} is not a folder")
    return path


def path_of(session_id: uuid.UUID) -> Path:
    return Path(settings.UPLOAD_SPOOL_DIR) / f"{uuid.UUID(str(session_id)).hex}{SUFFIX}"


def create(session_id: uuid.UUID) -> None:
    """The session's empty spool file, mode 600."""
    folder()
    fd = os.open(path_of(session_id), os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC, 0o600)
    os.close(fd)
    private.made(path_of(session_id))


def _open(session_id: uuid.UUID, flags: int) -> int:
    try:
        fd = os.open(path_of(session_id), flags | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY)
    except FileNotFoundError:
        raise SpoolError(_("The data received for this upload is gone (it may have been cleaned up). Please upload again.")) from None
    except OSError as error:
        raise SpoolError(_("The upload's temporary file can't be opened. Please try again later."), f"{path_of(session_id)}: {error.strerror or error}") from None
    if not st.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise SpoolError(_("The upload's temporary file is wrong."))
    return fd


def write_chunk(session_id: uuid.UUID, offset: int, body: BinaryIO, length: int) -> None:
    """`length` bytes of `body` at `offset`; the file then ends there. ShortBody when the body ends
    early (the file is cut back to `offset`)."""
    fd = _open(session_id, os.O_WRONLY)
    try:
        if os.fstat(fd).st_size < offset:
            raise SpoolError(_("Part of the data received for the upload is missing (the temporary file was changed). Please upload again."))
        os.lseek(fd, offset, os.SEEK_SET)
        left = length
        while left > 0:
            piece = body.read(min(PIECE, left))
            if not piece:
                os.ftruncate(fd, offset)
                raise ShortBody()
            piece = piece[:left]
            view = memoryview(piece)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            left -= len(piece)
        os.ftruncate(fd, offset + length)
        os.fsync(fd)
    finally:
        os.close(fd)


def size_of(session_id: uuid.UUID) -> int:
    fd = _open(session_id, os.O_RDONLY)
    try:
        return os.fstat(fd).st_size
    finally:
        os.close(fd)


def open_for_reading(session_id: uuid.UUID) -> BinaryIO:
    return os.fdopen(_open(session_id, os.O_RDONLY), "rb")


def remove(session_id: uuid.UUID) -> bool:
    try:
        os.unlink(path_of(session_id))
    except FileNotFoundError:
        return False
    return True


def free_bytes() -> int:
    return shutil.disk_usage(folder()).free


def files() -> Iterator[tuple[uuid.UUID, float]]:
    """(session id, modified time) of every spool file; other files in the folder are left alone."""
    path = Path(settings.UPLOAD_SPOOL_DIR)
    try:
        entries = list(os.scandir(path))
    except FileNotFoundError:
        return
    for entry in entries:
        if not entry.name.endswith(SUFFIX):
            continue
        try:
            session_id = uuid.UUID(entry.name[: -len(SUFFIX)])
            info = entry.stat(follow_symlinks=False)
        except (ValueError, OSError):
            continue
        if st.S_ISREG(info.st_mode):
            yield session_id, info.st_mtime
