"""The 安装口令 (install token): proof that whoever fills in the installer started the program.

Until the site is installed, anybody who reaches the server could open the installer and
make themselves its admin. So the installer asks for a token that only the person at the
server can see: the server prints it when it starts (apps/installer/banner.py), and it is saved next
to config.toml, in install-token.txt (readable by the program's user only). Installing deletes
the file.
"""

import errno
import hmac
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

from django.conf import settings
from django.utils.crypto import get_random_string
from django.utils.translation import gettext

from config import private

FILE_NAME = "install-token.txt"
# Four groups of four, without look-alikes: about 80 bits.
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
# A new file, only that; O_BINARY: on Windows the text's own \r\n would get another \r.
_NEW = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)


def path():
    return Path(settings.CONFIG_FILE).parent / FILE_NAME


def _new():
    return "-".join(get_random_string(4, ALPHABET) for _ in range(4))


def _normalize(text):
    return "".join(ch for ch in str(text).upper() if ch.isalnum())


_TOKEN = re.compile(rf"[{ALPHABET}]{{4}}(?:-[{ALPHABET}]{{4}}){{3}}")

# What os.link() fails with on a file system without hard links: vfat, exfat, FUSE ones like rclone's.
_NO_HARD_LINKS = {errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP}


def get():
    """The token, made and saved the first time. Raises OSError when it can't be saved.

    A file that doesn't hold a token (emptied, or edited by hand: other characters, another
    encoding) gets a new one: a token nobody can type would keep the site from being installed.
    """
    file = path()
    try:
        text = file.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return _save(file, replace=False)
    except ValueError:  # not ASCII
        text = ""
    return text if _TOKEN.fullmatch(text) else _save(file, replace=True)


def _save(file, *, replace):
    """A new token in `file`, written whole before it appears there (another process may read it at the
    same moment). Without `replace`, a file another process made meanwhile wins."""
    token = _new()
    part = file.with_name(f"{file.name}.{secrets.token_hex(4)}.part")
    fd = os.open(part, _NEW, 0o600)
    private.made(part)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as f:
            f.write(token + "\n")
        if replace:
            os.replace(part, file)
            return token
        try:
            os.link(part, file)  # unlike rename, fails when the file is there already
        except FileExistsError:  # another process got there first
            return get()
        except OSError as error:
            if error.errno not in _NO_HARD_LINKS:
                raise
            # Made where it goes instead, exclusively; another process may find it empty for a moment
            # (and write one of its own). It couldn't be installed at all (OPS-02).
            return _create(file, token)
        return token
    finally:
        with suppress(FileNotFoundError):
            part.unlink()


def _create(file, token):
    try:
        fd = os.open(file, _NEW, 0o600)
    except FileExistsError:
        return get()
    private.made(file)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(token + "\n")
    return token


def problem(error):
    """Why the token can't be saved, a sentence for the installer page and the banner: not every system
    error is a folder that can't be written to (OPS-02)."""
    if isinstance(error, PermissionError):
        return gettext("The folder of the settings file (%(folder)s) can't be written to, so the install token can't be saved.") % {"folder": path().parent}
    return gettext("The install token can't be saved (%(path)s: %(error)s).") % {"path": path(), "error": error.strerror or error}


def check(text):
    """Whether `text` is the token: case, spaces and dashes don't matter.

    An empty token never matches: the file can be empty for a moment while another thread
    writes it, or for good when writing it failed, and "-" would normalize to "" too.
    """
    try:
        expected = _normalize(get())
    except OSError:
        return False
    given = _normalize(text) if text else ""
    # As bytes: compare_digest() refuses str with non-ASCII characters, which isalnum() lets
    # through (口令 typed in Chinese), and the page would answer 500.
    return bool(expected) and bool(given) and hmac.compare_digest(given.encode(), expected.encode())


def remove():
    path().unlink(missing_ok=True)
