"""Local storage: a folder on the server.

Paths
    The storage path "/a/b" is <root>/a/b, where root is the configured folder. Every operation
    walks the path one name at a time from an open descriptor of the root, with *at() system calls
    (openat, fstatat, mkdirat, renameat, unlinkat) and O_NOFOLLOW, never by handing a joined path to
    the kernel. So swapping a folder for a symlink while we work can't lead anywhere else. A walk
    holds at most two descriptors however deep it goes, and so do deleting and copying a folder:
    they open each folder again for its turn. Paths have at most MAX_DEPTH (256) folders.

Symlinks
    Symlinks are resolved by that walk, not by the kernel. A link whose target stays inside the root
    is followed: it shows as the file or folder it points to, and reading or writing it acts on the
    target. A link that leads outside the root (absolute, or with too many "..") is left out of
    listings and treated as missing, unless the admin allows such links. Deleting, renaming or
    moving a link acts on the link itself, never on its target; copying follows links inside the
    root and skips the others.

Params
    root (the folder: an absolute path that exists) and allow_external_symlinks.

Changes
    Copies are built under a temporary name the same way as writes (below), with the original's
    permission bits less the umask, as cp makes them. Renames and moves never replace anything
    (renameat2 RENAME_NOREPLACE, else link() for files). A move to another file system mounted
    inside the root copies everything as it is (links, FIFOs, names that aren't UTF-8, the
    permission bits and times of files and folders), then deletes the original; a device or a socket
    in it stops the move.

Uploads
    Writes go to a hidden temporary file ".hfile-upload-<random>" in the target's folder, which is
    synced and then renamed over the target: readers see the old or the new content, never a mix,
    and a failed upload leaves nothing behind. The new file keeps the permission bits of the one it
    replaces. Temporary files never appear in listings and can't be opened; ones left by a crash are
    deleted when their folder is listed a day later.

Limits
    Linux, macOS, BSD, and Windows, where NT's own calls do what the *at() ones do here (_nt.py):
    there links are symbolic links and junctions, names match without regard to case, a name can't
    be one Windows refuses or other programs there can't open (a dot or a space at its end, CON),
    and a move to another volume can't keep links or times. Names that aren't valid UTF-8 are left
    out of listings, and so are dangling links, FIFOs, sockets and devices. Hard links aren't
    detected (only the server's admin can make one that points outside). Replacing a file breaks
    hard links to it and doesn't keep its owner, ACLs or extended attributes. No size limit beyond
    the disk's.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import errno
import logging
import os
import re
import secrets
import stat as st
import sys
import time
from collections import deque
from collections.abc import Iterator
from typing import Any, BinaryIO

from django.utils.translation import gettext as _, gettext_lazy, ngettext

from . import paths
from .base import (
    AlreadyExists,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
    Unsupported,
    only_case_differs,
    rename_through,
)

log = logging.getLogger("hfile.storage.local")

TEMP_PREFIX = ".hfile-upload-"
STALE_TEMP_SECONDS = 24 * 3600
CHUNK = 256 * 1024  # bytes per read when sending a file
WRITE_CHUNK = 1024 * 1024  # bytes per read when receiving or copying one
MAX_LINKS = 40  # symlinks followed in one path, like the kernel's limit
MAX_DEPTH = 256  # folders in a path, and below a folder that is deleted or copied

_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
# Folders on a walk are only looked up in: O_PATH (Linux) works for folders we may enter but not
# list. Listing needs a descriptor opened for reading.
_WALK = (getattr(os, "O_PATH", 0) or os.O_RDONLY) | _O_DIRECTORY | _O_CLOEXEC
_LIST = os.O_RDONLY | _O_DIRECTORY | _O_CLOEXEC
# O_NONBLOCK: opening a FIFO must not hang; it's refused right after (not a regular file).
_READ = os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | _O_CLOEXEC
_CREATE = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_CLOEXEC

if os.name == "nt":  # NT's own calls in place of the *at() ones, which take these instead (_nt.py)
    from . import _nt

    _os = _nt
    _WALK, _LIST, _READ, _CREATE, _O_NOFOLLOW = _nt.WALK, _nt.LIST, _nt.READ, _nt.CREATE, _nt.NOFOLLOW
else:
    _nt = None
    _os = os

_RENAME_NOREPLACE = 1

# What Windows refuses in a name (after ":" an alternate data stream), and the names of its devices, which
# other programs there can't open as files even with an extension (CON.txt).
_WINDOWS_REFUSED = frozenset('<>:"\\|?*') | frozenset(map(chr, range(32)))
_WINDOWS_DEVICES = re.compile("(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\\..*)?", re.IGNORECASE)


# --- system calls --------------------------------------------------------------------------------

def _supported() -> bool:
    if _nt is not None:
        return _nt.supported()
    needed = (os.open, os.stat, os.mkdir, os.rename, os.unlink, os.rmdir, os.readlink, os.symlink)
    return (os.name == "posix" and bool(_O_DIRECTORY and _O_NOFOLLOW)
            and all(func in os.supports_dir_fd for func in needed) and os.scandir in os.supports_fd)


_SUPPORTED = _supported()


def supported() -> bool:
    """The system has what the safe walk needs (directory descriptors, O_NOFOLLOW; on Windows NT's calls)."""
    return _SUPPORTED


def _load_renameat2() -> Any:
    if not sys.platform.startswith("linux"):
        return None
    try:
        func = ctypes.CDLL(None, use_errno=True).renameat2
    except (OSError, AttributeError):
        return None
    func.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    func.restype = ctypes.c_int
    return func


_renameat2 = _load_renameat2()


def _rename_noreplace(src_fd: int, src: str, dst_fd: int, dst: str) -> None:
    """rename() that fails with EEXIST instead of replacing `dst`.

    Linux does it atomically (renameat2 RENAME_NOREPLACE). Where that's missing (other systems,
    NFS, CIFS, older ZFS), a file or link is hard-linked under the new name, which never
    replaces anything, then unlinked under the old one; only a folder on such a file system is
    checked first and then renamed, which leaves a short race. Windows does it atomically too (_nt.py)."""
    if _nt is not None:
        _nt.rename_noreplace(src_fd, src, dst_fd, dst)
        return
    if _renameat2 is not None:
        if _renameat2(src_fd, os.fsencode(src), dst_fd, os.fsencode(dst), _RENAME_NOREPLACE) == 0:
            return
        code = ctypes.get_errno()
        # EINVAL: the file system can't do it, or the rename itself is invalid (a folder into
        # itself), which rename() below reports.
        if code not in (errno.EINVAL, errno.ENOSYS, getattr(errno, "EOPNOTSUPP", errno.EINVAL)):
            raise OSError(code, os.strerror(code))
    if not st.S_ISDIR(os.stat(src, dir_fd=src_fd, follow_symlinks=False).st_mode):
        try:
            os.link(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd, follow_symlinks=False)
        except OSError as e:
            if e.errno in (errno.EEXIST, errno.EXDEV):
                raise
            # No hard links on this file system: check, then rename.
        else:
            try:
                os.unlink(src, dir_fd=src_fd)
            except OSError:
                with contextlib.suppress(OSError):
                    os.unlink(dst, dir_fd=dst_fd)  # back to one name
                raise
            return
    try:
        os.stat(dst, dir_fd=dst_fd, follow_symlinks=False)
    except FileNotFoundError:
        os.rename(src, dst, src_dir_fd=src_fd, dst_dir_fd=dst_fd)
        return
    raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST))


# --- entries and errors --------------------------------------------------------------------------

def _utf8(name: str) -> bool:
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:  # bytes that weren't UTF-8, kept as surrogates by os
        return False
    return True


def _mtime(info: os.stat_result) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromtimestamp(info.st_mtime, tz=datetime.timezone.utc)
    except (ValueError, OverflowError, OSError):  # a time outside the years 1 to 9999
        return None


def _etag(info: os.stat_result) -> str:
    return f'"{info.st_ino:x}-{info.st_mtime_ns:x}-{info.st_size:x}"'


def _entry(path: str, info: os.stat_result) -> Entry:
    is_dir = st.S_ISDIR(info.st_mode)
    return Entry(name=paths.name(path), path=path, is_dir=is_dir,
                 size=None if is_dir else info.st_size, modified=_mtime(info))


def _error(e: OSError) -> DriverError:
    code = e.errno
    if code in (errno.ENOENT, errno.ENOTDIR):
        return NotFound()
    if code == errno.EEXIST:
        return AlreadyExists()
    if code == errno.EISDIR:
        return AlreadyExists(_("There is already a folder with that name there."))
    if code in (errno.EACCES, errno.EPERM):
        return DriverError(_("The file permissions on the server don't allow this."))
    if code in (errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)):
        return DriverError(_("The server is out of disk space."))
    if code == errno.EROFS:
        return DriverError(_("The storage's disk is read-only."))
    if code == errno.ENAMETOOLONG:
        return InvalidPath(_("The name or path is too long."))
    if code == errno.ELOOP:
        return DriverError(_("Too many levels of symbolic links, or a loop."))
    if code == errno.ENOTEMPTY:
        return DriverError(_("The folder isn't empty."))
    if code in (errno.EMFILE, errno.ENFILE):
        return BackendError(_("The server has too many files open. Please try again later."))
    if code == errno.EBUSY:  # on Windows another program has it open without sharing it (_nt.py), on Linux a mount point
        return DriverError(_("The file or folder is in use on the server (another program may have it open). Please try again later."))
    return BackendError(_("Reading or writing a file on the server failed: %(error)s") % {"error": os.strerror(code) if code else e})


class _Outside(Exception):
    """A symlink led out of the root (and such links aren't allowed)."""


class _ReadFailed(Exception):
    """Reading the data to store failed: the caller's error, passed on as it is."""

    def __init__(self, error: BaseException):
        super().__init__(error)
        self.error = error


# --- walking paths safely ------------------------------------------------------------------------

def _open_below(base: int, names: list[str], flags: int = _WALK) -> int:
    """A new descriptor of base/names[0]/.../names[-1], opened one real folder at a time without
    following links, with at most two descriptors open at any moment. Only the last folder is
    opened with `flags`; the ones on the way are only entered, so they needn't be readable."""
    fd = _os.open(".", flags if not names else _WALK, dir_fd=base)
    try:
        for i, name in enumerate(names):
            child = _os.open(name, (flags if i == len(names) - 1 else _WALK) | _O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
    except BaseException:
        os.close(fd)
        raise
    return fd


class _Walk:
    """Where a walk is: `base`, the folder it started from (the root, or a folder outside it
    that an allowed link led to); `names`, the real folders walked down from there; `fd`, a
    descriptor of the last of them. It holds at most two descriptors however deep it goes:
    going up opens the way down again from the base."""

    __slots__ = ("base", "names", "fd", "outside")

    def __init__(self, base: int):
        self.base = base
        self.names: list[str] = []
        self.fd = base
        self.outside = False  # an allowed link took the walk out of the root

    def down(self, name: str) -> None:
        fd = _os.open(name, _WALK | _O_NOFOLLOW, dir_fd=self.fd)
        self._move_to(fd)
        self.names.append(name)

    def up(self) -> bool:
        """To the folder above; False when the walk is at its base."""
        if not self.names:
            return False
        self._move_to(_open_below(self.base, self.names[:-1]) if len(self.names) > 1 else self.base)
        self.names.pop()
        return True

    def restart(self, base: int) -> None:
        self.close()
        self.base = self.fd = base
        self.names = []

    def _move_to(self, fd: int) -> None:
        if self.fd != self.base:
            os.close(self.fd)
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0 and self.fd != self.base:
            os.close(self.fd)
        if self.base >= 0:
            os.close(self.base)
        self.fd = self.base = -1


def _parts(path: str) -> list[str]:
    if not isinstance(path, str) or not paths.is_clean(path):
        raise InvalidPath(_("Invalid path."))
    parts = paths.segments(path)
    if len(parts) > MAX_DEPTH:
        raise InvalidPath(ngettext("The path is too deep: at most %(limit)s folder level.", "The path is too deep: at most %(limit)s folder levels.", MAX_DEPTH) % {"limit": MAX_DEPTH})
    for part in parts:
        if part.startswith(TEMP_PREFIX) or not _utf8(part):
            raise NotFound()  # hidden from listings, so hidden from everything
        if _nt is not None and (":" in part or "\\" in part):
            raise NotFound()  # Windows: an alternate data stream, a path
    return parts


def _absolute(path: str) -> bool:
    """An absolute path; on Windows one with its drive or share (D:\\files, \\\\server\\share\\files), as \\files
    would be on whichever drive the program runs from."""
    return os.path.isabs(path) and (os.name != "nt" or os.path.splitdrive(path)[0] != "")


# --- reading and writing -------------------------------------------------------------------------

class _FileChunks:
    """The bytes of an open file from `offset`, `size` of them; owns the descriptor."""

    def __init__(self, fd: int, offset: int, size: int):
        self._fd = fd
        self._pos = offset
        self._left = size

    def __iter__(self) -> Iterator[bytes]:
        return self

    def __next__(self) -> bytes:
        if self._left <= 0 or self._fd < 0:
            raise StopIteration
        try:
            data = _os.pread(self._fd, min(CHUNK, self._left), self._pos)
        except OSError as e:
            raise _error(e) from e
        if not data:
            raise BackendError(_("The file got shorter while being read. Please download it again."))
        self._pos += len(data)
        self._left -= len(data)
        return data

    def close(self) -> None:
        fd, self._fd = self._fd, -1
        if fd >= 0:
            os.close(fd)


def _check_new_name(name: str) -> None:
    """Defence in depth for names of new entries; the site checks them properly first."""
    if not name or "/" in name or "\x00" in name or name in (".", ".."):
        raise InvalidPath(_("Invalid name."))
    if not _utf8(name):
        raise InvalidPath(_("The name has characters that can't be used."))
    if name.startswith(TEMP_PREFIX):
        raise InvalidPath(_("Names starting with %(prefix)s are for the program's own use. Please choose another name.") % {"prefix": TEMP_PREFIX})
    if _nt is not None:
        if any(c in _WINDOWS_REFUSED for c in name):
            raise InvalidPath(_('On this server (Windows) a name can\'t have < > : " \\ | ? * or control characters in it.'))
        if name[-1] in ". ":
            raise InvalidPath(_("On this server (Windows) a name can't end with a dot or a space."))
        if _WINDOWS_DEVICES.fullmatch(name):
            raise InvalidPath(_("On this server (Windows) %(name)s is a device's name, which files and folders can't have.") % {"name": name})


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]


# --- copying and deleting ------------------------------------------------------------------------

def _copy_fd(src: int, dst: int) -> None:
    while True:
        data = os.read(src, WRITE_CHUNK)
        if not data:
            return
        _write_all(dst, data)


def _copy_link(src_fd: int, name: str, dst_fd: int, new_name: str) -> None:
    """The link `name` in src_fd made again as `new_name` in dst_fd, for a move across file systems. Windows
    makes links only with a privilege a site doesn't have: the move is refused there."""
    if _nt is not None:
        raise DriverError(_("On this server (Windows) symbolic links and junctions can't be moved to another disk."))
    _os.symlink(_os.readlink(name, dir_fd=src_fd), new_name, dir_fd=dst_fd)


def _made_folder_mode(info: os.stat_result) -> int:
    """The mode the copy of a folder is made with: the original's permission bits (less the umask, as cp makes
    them) and the owner's, so that it can be filled; _copy_tree takes away those the original hasn't."""
    return st.S_IMODE(info.st_mode) & 0o777 | 0o700


def _keep(fd: int, info: os.stat_result) -> None:
    """The permission bits and times of the original (`info`) on its copy open as fd, for a move to another file
    system: as a move on one file system, it keeps them. Not on Windows (_nt.py has neither)."""
    os.fchmod(fd, st.S_IMODE(info.st_mode) & 0o777)
    os.utime(fd, ns=(info.st_atime_ns, info.st_mtime_ns))


def _scan(fd: int) -> list[tuple[str, bool]]:
    """(name, is a real folder) of the entries of the folder open as fd."""
    with _os.scandir(fd) as entries:
        return [(entry.name, entry.is_dir(follow_symlinks=False)) for entry in entries]


def _at_parent(dir_fd: int, names: list[str], action) -> None:
    """action(parent_fd, name) for base/names, the parent opened again from dir_fd."""
    parent = _open_below(dir_fd, names[:-1]) if len(names) > 1 else dir_fd
    try:
        action(parent, names[-1])
    finally:
        if parent != dir_fd:
            os.close(parent)


def _remove_tree(dir_fd: int, name: str) -> None:
    """Delete the folder `name` in dir_fd with everything in it, never following symlinks.

    Each folder is opened again from dir_fd for its turn, so a deep tree doesn't hold a
    descriptor per level."""
    todo: list[tuple[bool, list[str]]] = [(False, [name])]  # (emptied already, names below dir_fd)
    while todo:
        emptied, names = todo.pop()
        if emptied:
            _at_parent(dir_fd, names, lambda parent, folder: _os.rmdir(folder, dir_fd=parent))
            continue
        if len(names) > MAX_DEPTH:
            raise DriverError(ngettext("The folders are nested too deep (over %(limit)s level); only "
                                       "part was deleted, so please delete the deeper part on the server.", "The folders are nested too deep (over %(limit)s levels); only part was deleted, so please delete the deeper part on the server.", MAX_DEPTH) % {"limit": MAX_DEPTH})
        try:
            fd = _open_below(dir_fd, names, _LIST)
        except OSError as e:
            if e.errno not in (errno.ENOTDIR, errno.ELOOP):
                raise
            # Swapped for a link or a file since it was listed: delete that, not what it points to.
            _at_parent(dir_fd, names, lambda parent, entry: _os.unlink(entry, dir_fd=parent))
            continue
        try:
            entries = _scan(fd)
            for child, is_dir in entries:
                if not is_dir:
                    with contextlib.suppress(FileNotFoundError):
                        _os.unlink(child, dir_fd=fd)
        finally:
            os.close(fd)
        todo.append((True, names))
        todo.extend((False, names + [child]) for child, is_dir in entries if is_dir)


# --- the driver ----------------------------------------------------------------------------------

class LocalDriver(StorageDriver):
    type = "local"
    label = gettext_lazy("Local storage")
    params = (
        Param("root", gettext_lazy("Folder"), placeholder="D:\\files" if os.name == "nt" else "/data/files",
              help=gettext_lazy("A folder on the server, as an absolute path with its drive like D:\\files; it must "
                                "already exist. Choose a folder just for files, not the program's own folder.")
              if os.name == "nt" else
              gettext_lazy("A folder on the server, as an absolute path like /data/files; it must already exist. "
                   "With Docker, enter the path inside the container, and mount the host's folder into "
                   "the container first. Choose a folder just for files, not the program's own folder.")),
        Param("allow_external_symlinks", gettext_lazy("Allow symbolic links pointing outside the folder"), kind="bool", required=False, default=False,
              advanced=True,
              help=gettext_lazy("Off by default: symbolic links in the folder that point outside it aren't shown "
                   "and can't be opened. When on, they work like ordinary files and folders: they can "
                   "be seen and downloaded, and people with the permissions can upload, create, "
                   "rename and delete through them in the folders outside (copying refuses them). "
                   "First make sure what they point to may be public and may be changed.")),
    )

    can_range = True
    can_direct_download = False
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    # Windows' (the site refuses "\" and control characters itself): new names are refused with the site's
    # message before they get here; _check_new_name() has the rest of its rules.
    forbidden_chars = '<>:"|?*' if os.name == "nt" else ""

    def __init__(self, config, *, on_update=None):
        super().__init__(config, on_update=on_update)
        root = self.config.get("root") or ""
        self._root = os.path.normpath(root) if isinstance(root, str) and _absolute(root) else ""
        self._allow_external = bool(self.config.get("allow_external_symlinks"))

    # -- the admin form

    @classmethod
    def validate_config(cls, config):
        root = config.get("root")
        if not supported():
            return {"root": _("Local storage needs a system such as Linux or macOS: this server's system doesn't support it.")}
        if not isinstance(root, str) or not root.strip():
            return {"root": _("Please fill in the folder on the server.")}
        if "\x00" in root:
            return {"root": _("The path can't contain NUL characters.")}
        if not _absolute(root):
            if os.name == "nt":
                return {"root": _("Enter an absolute path with its drive, like D:\\files.")}
            return {"root": _("Enter an absolute path, like /data/files.")}
        root = os.path.normpath(root)
        if os.path.dirname(root) == root:
            return {"root": _("The system's root can't be a storage. Please choose a folder just for files.")}
        if not os.path.isdir(root):
            return {"root": _("This folder doesn't exist, or isn't a folder.")}
        return {}

    # -- walking paths safely

    def _open_root(self) -> int:
        if not supported():
            raise Unsupported(_("Local storage needs a system such as Linux or macOS: this server's system doesn't support it."))
        if not self._root:
            raise BackendError(_("The storage's folder is set wrong. Please ask the administrator to check the storage source's settings."))
        try:
            # The root itself may be a symlink the admin chose: it's followed.
            return _os.open(self._root, _WALK)
        except (FileNotFoundError, NotADirectoryError):
            raise BackendError(_("The storage's folder no longer exists. Please ask the administrator to check the storage source's settings.")) from None
        except OSError as e:
            raise _error(e) from e

    def _root_prefixes(self) -> list[str]:
        """The root's path as configured and as resolved: absolute link targets under either are
        inside."""
        prefixes = [self._root]
        real = os.path.realpath(self._root)
        if real != self._root:
            prefixes.append(real)
        return prefixes

    def _restart_at(self, walk: _Walk, target: str) -> list[str]:
        """An absolute link target: restart the walk at the root (or at "/" when it leads out and
        that's allowed) and give the names still to walk. On Windows a target is a Windows path
        (C:\\a, \\\\server\\share\\a) and case doesn't matter; one leading out starts at its drive or share."""
        if _nt is not None:
            folded = _nt.fold(target)
            for prefix in self._root_prefixes():
                inside = _nt.fold(prefix)
                if folded == inside or folded.startswith(inside + "\\"):
                    walk.restart(self._open_root())
                    walk.outside = False
                    return folded[len(inside):].split("\\")  # names match whatever their case
            if not self._allow_external:
                raise _Outside()
            anchor, names = _nt.anchor(target)
            walk.restart(_os.open(anchor, _WALK))
            walk.outside = True
            return names
        for prefix in self._root_prefixes():
            if target == prefix or target.startswith(prefix.rstrip("/") + "/"):
                walk.restart(self._open_root())
                walk.outside = False
                return target[len(prefix):].split("/")
        if not self._allow_external:
            raise _Outside()
        walk.restart(os.open("/", _WALK))
        walk.outside = True
        return target.split("/")

    def _up(self, walk: _Walk) -> None:
        """'..' in a link target: the folder above, which must still be inside the root unless
        external links are allowed."""
        if walk.up():
            return
        if not self._allow_external:
            raise _Outside()
        walk.restart(_os.open("..", _WALK, dir_fd=walk.base))
        walk.outside = True

    def _resolve(self, parts: list[str], *, follow_last: bool = True, create: bool = False) -> tuple[_Walk, str | None]:
        """Walk `parts` from the root: (walk, name), where walk.fd is the folder holding `name`, or
        name is None when the walk ended on a folder itself (the root, or a link to a folder).

        Links on the way are followed by this walk; one that leads outside raises _Outside unless
        that's allowed. With follow_last, `name` is never a link (it may not exist); without it,
        `name` is the last part as it is. create makes missing folders on the way. Raises OSError:
        ENOENT for a missing folder on the way, ENOTDIR, ELOOP."""
        walk = _Walk(self._open_root())
        try:
            pending = deque(parts)
            links = 0
            steps = 0
            while pending:
                steps += 1
                if steps > 100000:  # someone keeps swapping entries under us
                    raise OSError(errno.ELOOP, os.strerror(errno.ELOOP))
                name = pending.popleft()
                if name in ("", "."):
                    continue
                if name == "..":
                    self._up(walk)
                    continue
                if name.startswith(TEMP_PREFIX):  # temporary files are invisible, also through links
                    raise OSError(errno.ENOENT, os.strerror(errno.ENOENT))
                last = not pending
                if last and not follow_last:
                    return walk, name
                try:
                    info = _os.stat(name, dir_fd=walk.fd, follow_symlinks=False)
                except FileNotFoundError:
                    if last:
                        return walk, name
                    if not create:
                        raise
                    with contextlib.suppress(FileExistsError):
                        _os.mkdir(name, 0o777, dir_fd=walk.fd)
                    pending.appendleft(name)
                    continue
                if st.S_ISLNK(info.st_mode):
                    links += 1
                    if links > MAX_LINKS:
                        raise OSError(errno.ELOOP, os.strerror(errno.ELOOP))
                    target = _os.readlink(name, dir_fd=walk.fd)
                    absolute = _nt.is_absolute(target) if _nt is not None else target.startswith("/")
                    rest = self._restart_at(walk, target) if absolute else target.split("/")
                    pending.extendleft(reversed(rest))
                    continue
                if last:
                    return walk, name
                if not st.S_ISDIR(info.st_mode):
                    raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR))
                if len(walk.names) >= MAX_DEPTH:
                    raise OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG))
                try:
                    walk.down(name)
                except OSError as e:
                    if e.errno not in (errno.ENOTDIR, errno.ELOOP):
                        raise
                    pending.appendleft(name)  # swapped for a link or a file since the stat
            return walk, None
        except BaseException:
            walk.close()
            raise

    @contextlib.contextmanager
    def _at(self, path: str, *, follow_last: bool = True, create: bool = False) -> Iterator[tuple[_Walk, str | None]]:
        """_resolve() for a storage path, with errors as DriverErrors, also those of the block."""
        parts = _parts(path)
        try:
            walk, name = self._resolve(parts, follow_last=follow_last, create=create)
        except _Outside:
            raise NotFound() from None
        except OSError as e:
            raise _error(e) from e
        try:
            yield walk, name
        except OSError as e:
            raise _error(e) from e
        finally:
            walk.close()

    def _inside(self, info: os.stat_result, folder: str) -> bool:
        """Whether the folder at storage path `folder` is the folder `info` is the stat of, or inside it, told by what
        the folders it is in really are (links followed): under other capitals where the file system ignores case
        (NTFS, APFS: /A/b is inside /a), or through a link inside the root."""
        try:
            walk, name = self._resolve(_parts(folder))
        except (_Outside, OSError):
            return False  # opening the folder says what's wrong
        try:
            if walk.outside:
                return False
            chain = walk.names + ([] if name is None else [name])  # real folders from the root, none a link
        finally:
            walk.close()
        for depth in range(1, len(chain) + 1):
            found = self._target(chain[:depth])
            if found is not None and (found[0].st_dev, found[0].st_ino) == (info.st_dev, info.st_ino):
                return True
        return False

    def _target(self, parts: list[str]) -> tuple[os.stat_result, bool] | None:
        """What the path leads to (links followed) and whether it's outside the root; None when it
        leads nowhere (missing, a dangling link, outside and not allowed, a loop)."""
        try:
            walk, name = self._resolve(parts)
        except (_Outside, OSError):
            return None
        try:
            if name is None:
                return _os.stat(walk.fd), walk.outside
            info = _os.stat(name, dir_fd=walk.fd, follow_symlinks=False)
            if st.S_ISLNK(info.st_mode):  # swapped for a link since the walk
                return None
            return info, walk.outside
        except OSError:
            return None
        finally:
            walk.close()

    def _visible(self, dir_fd: int, name: str, parts: list[str]) -> os.stat_result:
        """lstat of an existing entry that listings show; NotFound otherwise (a link leading
        nowhere or outside, a FIFO, a device)."""
        info = kind = _os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if st.S_ISLNK(info.st_mode):
            target = self._target(parts)
            if target is None:
                raise NotFound()
            kind = target[0]
        if not (st.S_ISDIR(kind.st_mode) or st.S_ISREG(kind.st_mode)):
            raise NotFound()
        return info

    @contextlib.contextmanager
    def _folder(self, path: str) -> Iterator[int]:
        """A descriptor of the folder at `path` (links followed) to create things in."""
        with self._at(path) as (walk, name):
            if name is None:
                yield walk.fd
                return
            try:
                fd = _os.open(name, _WALK | _O_NOFOLLOW, dir_fd=walk.fd)
            except OSError as e:
                if e.errno == errno.ENOTDIR:
                    raise NotADirectory() from None
                raise
            try:
                yield fd
            finally:
                os.close(fd)

    # -- copying

    def _copy_into(self, src_dir: int, source: str | None, parts: list[str], dest: int, name: str, *,
                   keep_links: bool) -> None:
        """Copy `source` in src_dir (None: src_dir itself), the entry at storage path `parts`, to
        `name` in dest, through a temporary name, so that the copy appears complete or not at all.

        keep_links (a move across file systems) copies everything as it is; otherwise links are
        followed while they stay inside the root, and skipped when they lead outside or nowhere.
        See _copy_tree."""
        temp = TEMP_PREFIX + secrets.token_hex(16)
        if source is None:
            info = _os.stat(src_dir)
        else:
            info = _os.stat(source, dir_fd=src_dir, follow_symlinks=False)
        try:
            if st.S_ISLNK(info.st_mode) and keep_links:
                _copy_link(src_dir, source, dest, temp)
            elif st.S_ISDIR(info.st_mode):
                self._copy_tree(parts, dest, temp, keep_links, info)
            elif st.S_ISREG(info.st_mode):
                fd = _os.open(source, _READ, dir_fd=src_dir)
                try:
                    self._copy_file(fd, dest, temp, keep_links)
                finally:
                    os.close(fd)
            elif st.S_ISFIFO(info.st_mode) and keep_links:
                _os.mkfifo(temp, st.S_IMODE(info.st_mode), dir_fd=dest)
            else:
                raise NotFound()
            _rename_noreplace(dest, temp, dest, name)
        except BaseException:
            with contextlib.suppress(OSError, DriverError):
                if st.S_ISDIR(_os.stat(temp, dir_fd=dest, follow_symlinks=False).st_mode):
                    _remove_tree(dest, temp)
                else:
                    _os.unlink(temp, dir_fd=dest)
            raise

    @staticmethod
    def _copy_file(src_fd: int, dir_fd: int, name: str, move: bool) -> None:
        """The file open as src_fd copied as `name` in dir_fd, with its permission bits less the umask, as cp makes
        it; for a move (`move`), with its bits and times as they are."""
        info = _os.fstat(src_fd)
        out = _os.open(name, _CREATE, st.S_IMODE(info.st_mode) & 0o777, dir_fd=dir_fd)
        try:
            _copy_fd(src_fd, out)
            if move and _nt is None:
                _keep(out, info)
        finally:
            os.close(out)

    def _copy_tree(self, parts: list[str], dest: int, name: str, keep_links: bool, info: os.stat_result) -> None:
        """Copy the folder at storage path `parts` (`info`, its stat) to a new folder `name` in dest.

        Each folder is opened again for its turn, the original from the root and the copy from
        dest, so a deep tree doesn't hold a descriptor per level.

        keep_links (a move across file systems, which deletes the original afterwards) copies
        everything as it is: links as links, FIFOs, names that aren't UTF-8; a device or a socket
        can't be copied and stops the move. Otherwise links are followed while they stay inside
        the root, links that lead outside or nowhere are skipped, and so are the entries listings
        don't show (special files, names that aren't UTF-8).

        Folders get their permission bits (and on a move their times) once everything is in them, the
        deepest first: the owner's are needed to fill them, and filling one changes its time."""
        _os.mkdir(name, _made_folder_mode(info), dir_fd=dest)
        todo: list[tuple[list[str], list[str], frozenset]] = [(parts, [name], frozenset())]
        made: list[tuple[list[str], os.stat_result]] = []  # copies whose bits or times are set last
        while todo:
            src_parts, dst_names, ancestors = todo.pop()
            if len(dst_names) > MAX_DEPTH:
                raise DriverError(ngettext("The folders are nested too deep (over %(limit)s level).", "The folders are nested too deep (over %(limit)s levels).", MAX_DEPTH) % {"limit": MAX_DEPTH})
            src, info = self._open_folder(src_parts)
            try:
                if _nt is None and (keep_links or info.st_mode & 0o700 != 0o700):
                    made.append((dst_names, info))
                here = ancestors | {(info.st_dev, info.st_ino)}
                out = _open_below(dest, dst_names)
                try:
                    for child, _is_dir in _scan(src):
                        self._copy_child(src, child, src_parts + [child], out, dst_names + [child], here,
                                         keep_links, todo)
                finally:
                    os.close(out)
            finally:
                os.close(src)
        for dst_names, info in reversed(made):  # every folder after the ones in it
            fd = _open_below(dest, dst_names, _LIST)
            try:
                if keep_links:
                    _keep(fd, info)
                else:  # the owner's bits that the original hasn't
                    os.fchmod(fd, st.S_IMODE(os.fstat(fd).st_mode) & (st.S_IMODE(info.st_mode) | 0o077))
            finally:
                os.close(fd)

    def _copy_child(self, src: int, child: str, child_parts: list[str], out: int, dst_names: list[str],
                    here: frozenset, keep_links: bool, todo: list) -> None:
        """Copy one entry of a folder being copied; folders go on `todo`."""
        if child.startswith(TEMP_PREFIX) or (not keep_links and not _utf8(child)):
            return  # uploads in progress; names listings don't show
        try:
            info = _os.stat(child, dir_fd=src, follow_symlinks=False)
        except FileNotFoundError:
            return
        mode = info.st_mode
        if st.S_ISLNK(mode):
            if keep_links:
                _copy_link(src, child, out, child)
                return
            fd, info = self._open_link_target(child_parts)
            if fd < 0:
                log.debug("copy skipped a link that leads nowhere or outside: %s", "/".join(child_parts))
                return
            try:
                if st.S_ISDIR(info.st_mode):
                    if (info.st_dev, info.st_ino) in here:
                        log.debug("copy skipped a link back into a folder being copied: %s", "/".join(child_parts))
                        return
                    _os.mkdir(child, _made_folder_mode(info), dir_fd=out)
                    todo.append((child_parts, dst_names, here))
                elif st.S_ISREG(info.st_mode):
                    self._copy_file(fd, out, child, keep_links)
            finally:
                os.close(fd)
        elif st.S_ISDIR(mode):
            if (info.st_dev, info.st_ino) not in here:
                _os.mkdir(child, _made_folder_mode(info), dir_fd=out)
                todo.append((child_parts, dst_names, here))
        elif st.S_ISREG(mode):
            fd = _os.open(child, _READ, dir_fd=src)
            try:
                self._copy_file(fd, out, child, keep_links)
            finally:
                os.close(fd)
        elif keep_links:
            if not st.S_ISFIFO(mode):
                raise DriverError(_("The folder has device files or sockets, so it can't be moved to another disk."))
            _os.mkfifo(child, st.S_IMODE(mode), dir_fd=out)

    def _open_folder(self, parts: list[str]) -> tuple[int, os.stat_result]:
        """A descriptor to list the folder at storage path `parts` (links followed), its stat."""
        try:
            walk, name = self._resolve(parts)
        except _Outside:
            raise OSError(errno.ENOENT, os.strerror(errno.ENOENT)) from None
        try:
            fd = _os.open("." if name is None else name, _LIST | _O_NOFOLLOW, dir_fd=walk.fd)
        finally:
            walk.close()
        try:
            return fd, _os.fstat(fd)
        except BaseException:
            os.close(fd)
            raise

    def _open_link_target(self, parts: list[str]) -> tuple[int, os.stat_result | None]:
        """Open what a link inside a copied folder leads to: (fd, its stat), or (-1, None) when it
        leads nowhere or outside the root (copies never follow links out, even allowed ones)."""
        try:
            walk, name = self._resolve(parts)
        except (_Outside, OSError):
            return -1, None
        try:
            if walk.outside:
                return -1, None
            if name is None:
                fd = _os.open(".", _LIST, dir_fd=walk.fd)
            else:
                info = _os.stat(name, dir_fd=walk.fd, follow_symlinks=False)
                flags = _LIST | _O_NOFOLLOW if st.S_ISDIR(info.st_mode) else _READ
                fd = _os.open(name, flags, dir_fd=walk.fd)
        except OSError:
            return -1, None
        finally:
            walk.close()
        try:
            return fd, _os.fstat(fd)
        except OSError:
            os.close(fd)
            return -1, None

    # -- reading

    def check(self) -> None:
        errors = self.validate_config(self.config)
        if errors:
            raise DriverError(next(iter(errors.values())))
        if not os.access(self._root, os.R_OK | os.X_OK):
            raise DriverError(_("The program has no permission to read this folder."))
        self.list("/")

    @staticmethod
    def _drop_stale_temp(fd: int, name: str, info: os.stat_result) -> None:
        """A temporary file left by a crashed upload: delete it once it's a day old."""
        if st.S_ISREG(info.st_mode) and time.time() - info.st_mtime > STALE_TEMP_SECONDS:
            try:
                _os.unlink(name, dir_fd=fd)
                log.info("deleted a temporary file left by an interrupted upload: %s", name)
            except OSError:
                pass

    def _entries(self, fd: int, path: str, parts: list[str]) -> list[Entry]:
        entries = []
        with _os.scandir(fd) as it:
            for item in it:
                name = item.name
                if not _utf8(name):
                    log.debug("left out a name that isn't UTF-8: %r in %s", name, path)
                    continue
                if paths.dots_only(name):  # D46: can't be addressed, and must not make the folder unlistable
                    log.debug("left out a name of dots and spaces only: %r in %s", name, path)
                    continue
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if name.startswith(TEMP_PREFIX):
                    self._drop_stale_temp(fd, name, info)
                    continue
                if st.S_ISLNK(info.st_mode):
                    target = self._target(parts + [name])
                    if target is None:
                        log.debug("left out a link that leads nowhere or outside: %s/%s", path.rstrip("/"), name)
                        continue
                    info = target[0]
                if st.S_ISDIR(info.st_mode) or st.S_ISREG(info.st_mode):
                    entries.append(_entry(paths.join(path, name), info))
        return entries

    def list(self, path: str) -> list[Entry]:
        parts = _parts(path)
        with self._at(path) as (walk, name):
            try:
                fd = _os.open("." if name is None else name, _LIST | _O_NOFOLLOW, dir_fd=walk.fd)
            except OSError as e:
                if e.errno in (errno.ENOTDIR, errno.ELOOP):
                    raise NotADirectory() from None
                raise
            try:
                return self._entries(fd, path, parts)
            finally:
                os.close(fd)

    def stat(self, path: str) -> Entry:
        for _attempt in range(3):
            with self._at(path) as (walk, name):
                info = _os.stat(walk.fd) if name is None else _os.stat(name, dir_fd=walk.fd, follow_symlinks=False)
            if not st.S_ISLNK(info.st_mode):  # else it was swapped for a link since the walk
                break
        else:
            raise NotFound()
        if not (st.S_ISDIR(info.st_mode) or st.S_ISREG(info.st_mode)):
            raise NotFound()
        return _entry(path, info)

    def _open_file(self, path: str) -> int:
        for _attempt in range(3):
            with self._at(path) as (walk, name):
                if name is None:
                    raise NotFound()  # a folder
                try:
                    return _os.open(name, _READ, dir_fd=walk.fd)
                except OSError as e:
                    if e.errno != errno.ELOOP:  # ELOOP: swapped for a link since the walk
                        raise
        raise NotFound()

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        fd = self._open_file(path)
        try:
            info = _os.fstat(fd)
            if not st.S_ISREG(info.st_mode):
                raise NotFound()
            total = info.st_size
            offset = min(max(offset, 0), total)
            size = total - offset if length is None else max(0, min(length, total - offset))
            modified, etag = _mtime(info), _etag(info)
            chunks = _FileChunks(fd, offset, size)
        except BaseException:
            os.close(fd)
            raise
        return ReadStream(chunks, size=size, total=total, offset=offset, modified=modified, etag=etag,
                          close=chunks.close)

    # -- writing

    @staticmethod
    def _store(dir_fd: int, name: str, data: BinaryIO, size: int, old: os.stat_result | None) -> None:
        temp = TEMP_PREFIX + secrets.token_hex(16)
        try:
            fd = _os.open(temp, _CREATE, 0o666, dir_fd=dir_fd)
        except PermissionError:
            raise DriverError(_("The file's folder can't be written to (file permissions on the server).")) from None
        done = False
        try:
            try:
                left = size
                while left > 0:
                    try:
                        chunk = data.read(min(WRITE_CHUNK, left))
                    except Exception as e:
                        raise _ReadFailed(e) from e
                    if not chunk:
                        raise DriverError(ngettext("The uploaded data is incomplete: %(missing)s byte missing.", "The uploaded data is incomplete: %(missing)s bytes missing.", left) % {"missing": left})
                    if len(chunk) > left:
                        chunk = chunk[:left]
                    _write_all(fd, chunk)
                    left -= len(chunk)
                if old is not None and st.S_ISREG(old.st_mode):
                    _os.fchmod(fd, st.S_IMODE(old.st_mode) & 0o777)
                os.fsync(fd)
            finally:
                os.close(fd)
            _os.rename(temp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
            done = True
        finally:
            if not done:
                with contextlib.suppress(OSError):
                    _os.unlink(temp, dir_fd=dir_fd)

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists(_("This is the storage's root folder."))
        _check_new_name(paths.name(path))
        try:
            for _attempt in range(3):
                with self._at(path, create=True) as (walk, name):
                    if name is None:
                        raise AlreadyExists(_("There is already a folder with that name there."))
                    try:
                        old = _os.stat(name, dir_fd=walk.fd, follow_symlinks=False)
                    except FileNotFoundError:
                        old = None
                    if old is not None and st.S_ISLNK(old.st_mode):
                        continue  # swapped for a link since the walk
                    if old is not None and st.S_ISDIR(old.st_mode):
                        raise AlreadyExists(_("There is already a folder with that name there."))
                    # The new file is renamed over the old one, which the folder's permissions allow whatever the
                    # file's own: a read-only one (0444) was replaced all the same (FILE-05).
                    if old is not None and not _os.access(name, os.W_OK, dir_fd=walk.fd, follow_symlinks=False):
                        raise DriverError(_("This file is read-only (file permissions on the server) and can't be overwritten."))
                    self._store(walk.fd, name, data, size, old)
                    return
        except _ReadFailed as e:
            raise e.error from None
        raise DriverError(_("Another program keeps changing the file. Please try again later."))

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        _check_new_name(paths.name(path))
        with self._at(path, follow_last=False, create=True) as (walk, name):
            _os.mkdir(name, 0o777, dir_fd=walk.fd)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage's root folder can't be deleted."))
        parts = _parts(path)
        with self._at(path, follow_last=False) as (walk, name):
            info = self._visible(walk.fd, name, parts)
            if st.S_ISDIR(info.st_mode):
                _remove_tree(walk.fd, name)
            else:
                _os.unlink(name, dir_fd=walk.fd)  # a link goes, its target stays

    @staticmethod
    def _has_name(walk_fd: int, name: str) -> bool:
        """Whether the folder open as walk_fd (to walk through) has an entry named exactly `name`."""
        fd = _os.open(".", _LIST, dir_fd=walk_fd)
        try:
            return any(child == name for child, _ in _scan(fd))
        finally:
            os.close(fd)

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage's root folder can't be renamed."))
        _check_new_name(new_name)
        parts = _parts(path)
        with self._at(path, follow_last=False) as (walk, name):
            self._visible(walk.fd, name, parts)
            if new_name == name:
                return
            try:
                _rename_noreplace(walk.fd, name, walk.fd, new_name)
            except FileExistsError:
                # A file system that ignores case (Windows, macOS, vfat, ext4 casefold) takes the new spelling for
                # the entry itself; its folder's entries have the names as they are.
                if not only_case_differs(name, new_name) or self._has_name(walk.fd, new_name):
                    raise AlreadyExists() from None
                rename_through(lambda old, new: _rename_noreplace(walk.fd, paths.name(old), walk.fd, paths.name(new)),
                               path, new_name)

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(_("A folder can't be moved into itself or a folder inside it."))
        parts = _parts(path)
        name = paths.name(path)
        with self._at(path, follow_last=False) as (walk, source):
            info = self._visible(walk.fd, source, parts)
            if paths.parent(path) == folder:
                return
            if st.S_ISDIR(info.st_mode) and self._inside(info, folder):
                raise DriverError(_("A folder can't be moved into itself or a folder inside it."))
            with self._folder(folder) as dest:
                try:
                    _rename_noreplace(walk.fd, source, dest, name)
                except FileExistsError:
                    raise AlreadyExists() from None
                except OSError as e:
                    if e.errno == errno.EINVAL:
                        raise DriverError(_("A folder can't be moved into itself or a folder inside it.")) from None
                    if e.errno != errno.EXDEV:
                        raise
                    # Another file system mounted inside the root: copy, then delete.
                    self._copy_into(walk.fd, source, parts, dest, name, keep_links=True)
                    try:
                        info = _os.stat(source, dir_fd=walk.fd, follow_symlinks=False)
                        if st.S_ISDIR(info.st_mode):
                            _remove_tree(walk.fd, source)
                        else:
                            _os.unlink(source, dir_fd=walk.fd)
                    except OSError as error:
                        log.warning("moved %s across file systems but couldn't delete the source: %s", path, error)
                        raise DriverError(_("It was copied to the new place, but not everything at the old place could be deleted.")) from error

    def copy(self, path: str, folder: str) -> None:
        if path == paths.ROOT or paths.is_inside(folder, path):
            raise DriverError(_("A folder can't be copied into itself or a folder inside it."))
        parts = _parts(path)
        name = paths.name(path)
        with self._at(path) as (walk, source):
            if walk.outside:
                raise DriverError(_("This is a symbolic link pointing outside the storage's folder and can't be copied."))
            info = _os.stat(walk.fd) if source is None else _os.stat(source, dir_fd=walk.fd, follow_symlinks=False)
            if st.S_ISDIR(info.st_mode) and self._inside(info, folder):
                raise DriverError(_("A folder can't be copied into itself or a folder inside it."))
            with self._folder(folder) as dest:
                try:
                    _os.stat(name, dir_fd=dest, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise AlreadyExists()
                self._copy_into(walk.fd, source, parts, dest, name, keep_links=False)
