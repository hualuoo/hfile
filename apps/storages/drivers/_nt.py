"""The calls of the local storage driver (local.py) on Windows, which has no *at() calls or O_NOFOLLOW:
NT's own instead. Each takes the arguments of the os function of the same name, with dir_fd, and is used
in its place (local.py's _os). Imported only on Windows.

Every name is opened relative to an open folder (NtCreateFile with a RootDirectory), never as a path,
and a link (a symbolic link or a junction: NT's name-surrogate reparse points) is opened itself, never
followed (FILE_OPEN_REPARSE_POINT): lstat() shows it as S_IFLNK, open() of one gives ELOOP, readlink()
reads it. Handles are wrapped in C runtime file descriptors (msvcrt), so os.read(), os.write(), os.fsync()
and os.close() work on them as on Linux. open() takes local.py's WALK, LIST, READ and CREATE (with
NOFOLLOW), which say what a descriptor is for: on Windows os.open() has no flags that do.

Names are matched without regard to case, as Windows does. A name with "~" is checked against the long
name of what it opened: an 8.3 short name ("SECRE~1") would reach a folder under another name than the
one the site's rules see, so it finds nothing. A name with ":" finds nothing either (an alternate data
stream, "name:stream").

Deleting and renaming use POSIX semantics where the file system has them (NTFS on Windows 10 1709 and
later): the name goes at once, even while another program has the file open (sharing delete), and a
rename may replace such a file. rename_noreplace() never replaces anything, atomically.
"""

import ctypes
import errno
import msvcrt
import ntpath
import os
import stat as st
from ctypes import wintypes

ntdll = ctypes.WinDLL("ntdll")
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

# What a descriptor from open() is for; NOFOLLOW is implied for every name opened in a folder.
WALK, LIST, READ, CREATE, NOFOLLOW = 1 << 20, 1 << 21, 1 << 22, 1 << 23, 1 << 24

FILE_LIST_DIRECTORY = 0x0001
FILE_READ_DATA = 0x0001
FILE_TRAVERSE = 0x0020
FILE_READ_ATTRIBUTES = 0x0080
DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
SHARE_ALL = 0x1 | 0x2 | 0x4  # read, write and delete: as on Linux, nobody is kept out by an open file
FILE_OPEN, FILE_CREATE = 1, 2
FILE_DIRECTORY_FILE = 0x00000001
FILE_SYNCHRONOUS_IO_NONALERT = 0x00000020
FILE_NON_DIRECTORY_FILE = 0x00000040
FILE_OPEN_REPARSE_POINT = 0x00200000
OBJ_CASE_INSENSITIVE = 0x40
FILE_ATTRIBUTE_READONLY = 0x01
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_NORMAL = 0x80
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003
IO_REPARSE_TAG_SYMLINK = 0xA000000C
SYMLINK_FLAG_RELATIVE = 1
FSCTL_GET_REPARSE_POINT = 0x000900A8
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_NAME_NORMALIZED = 0x0
VOLUME_NAME_DOS = 0x0
VOLUME_NAME_NONE = 0x4
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
ERROR_NO_MORE_FILES = 18
CSTR_EQUAL = 2

# FILE_INFORMATION_CLASS (NtSetInformationFile) and FILE_INFO_BY_HANDLE_CLASS (GetFileInformationByHandleEx)
FileRenameInformation = 10
FileDispositionInformation = 13
FileDispositionInformationEx = 64
FileRenameInformationEx = 65
FileAttributeTagInfo = 9
FileIdBothDirectoryInfo = 10
FileIdBothDirectoryRestartInfo = 11
FILE_DISPOSITION_DELETE = 0x01
FILE_DISPOSITION_POSIX_SEMANTICS = 0x02
FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE = 0x10
FILE_RENAME_REPLACE_IF_EXISTS = 0x01
FILE_RENAME_POSIX_SEMANTICS = 0x02

STATUS_INVALID_INFO_CLASS = 0xC0000003
STATUS_INVALID_PARAMETER = 0xC000000D
STATUS_NOT_SUPPORTED = 0xC00000BB
STATUS_NOT_IMPLEMENTED = 0xC0000002
_ERRNO = {
    0xC000000F: errno.ENOENT,  # STATUS_NO_SUCH_FILE
    0xC0000033: errno.ENOENT,  # STATUS_OBJECT_NAME_INVALID: no entry can have that name
    0xC0000034: errno.ENOENT,  # STATUS_OBJECT_NAME_NOT_FOUND
    0xC000003A: errno.ENOENT,  # STATUS_OBJECT_PATH_NOT_FOUND
    0xC0000056: errno.ENOENT,  # STATUS_DELETE_PENDING: being deleted
    0xC0000103: errno.ENOTDIR,  # STATUS_NOT_A_DIRECTORY
    0xC00000BA: errno.EISDIR,  # STATUS_FILE_IS_A_DIRECTORY
    0xC0000035: errno.EEXIST,  # STATUS_OBJECT_NAME_COLLISION
    0xC0000022: errno.EACCES,  # STATUS_ACCESS_DENIED
    0xC0000121: errno.EACCES,  # STATUS_CANNOT_DELETE (read-only)
    0xC0000043: errno.EBUSY,  # STATUS_SHARING_VIOLATION: another program has it open without sharing
    0xC0000101: errno.ENOTEMPTY,  # STATUS_DIRECTORY_NOT_EMPTY
    0xC00000D4: errno.EXDEV,  # STATUS_NOT_SAME_DEVICE
    0xC000000D: errno.EINVAL,  # STATUS_INVALID_PARAMETER (a folder into itself)
    0xC000007F: errno.ENOSPC,  # STATUS_DISK_FULL
    0xC0000044: errno.EDQUOT if hasattr(errno, "EDQUOT") else errno.ENOSPC,  # STATUS_QUOTA_EXCEEDED
    0xC00000A2: errno.EROFS,  # STATUS_MEDIA_WRITE_PROTECTED
    0xC0000106: errno.ENAMETOOLONG,  # STATUS_NAME_TOO_LONG
    0xC000011F: errno.EMFILE,  # STATUS_TOO_MANY_OPENED_FILES
    0x8000002D: errno.ELOOP,  # STATUS_STOPPED_ON_SYMLINK
    0xC0000275: errno.EINVAL,  # STATUS_NOT_A_REPARSE_POINT
}


class UNICODE_STRING(ctypes.Structure):
    _fields_ = [("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", ctypes.c_void_p)]


class OBJECT_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Length", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE),
                ("ObjectName", ctypes.POINTER(UNICODE_STRING)), ("Attributes", wintypes.ULONG),
                ("SecurityDescriptor", ctypes.c_void_p), ("SecurityQualityOfService", ctypes.c_void_p)]


class IO_STATUS_BLOCK(ctypes.Structure):
    _fields_ = [("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t)]


class BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]


class FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]


class FILE_ID_BOTH_DIR_INFO(ctypes.Structure):
    """An entry of a listing up to its name, which follows (FileNameLength bytes): the last entry in the
    buffer may end right after it."""
    _fields_ = [("NextEntryOffset", wintypes.DWORD), ("FileIndex", wintypes.DWORD),
                ("CreationTime", ctypes.c_longlong), ("LastAccessTime", ctypes.c_longlong),
                ("LastWriteTime", ctypes.c_longlong), ("ChangeTime", ctypes.c_longlong),
                ("EndOfFile", ctypes.c_longlong), ("AllocationSize", ctypes.c_longlong),
                ("FileAttributes", wintypes.DWORD), ("FileNameLength", wintypes.DWORD),
                ("EaSize", wintypes.DWORD),  # the reparse tag, for a reparse point (MS-FSCC 2.4.17)
                ("ShortNameLength", ctypes.c_byte), ("ShortName", wintypes.WCHAR * 12),
                ("FileId", ctypes.c_longlong)]


class FILE_DISPOSITION_INFORMATION_EX(ctypes.Structure):
    _fields_ = [("Flags", wintypes.ULONG)]


ntdll.NtCreateFile.argtypes = (ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD, ctypes.POINTER(OBJECT_ATTRIBUTES),
                               ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p, wintypes.ULONG, wintypes.ULONG,
                               wintypes.ULONG, wintypes.ULONG, ctypes.c_void_p, wintypes.ULONG)
ntdll.NtCreateFile.restype = ctypes.c_long
ntdll.NtSetInformationFile.argtypes = (wintypes.HANDLE, ctypes.POINTER(IO_STATUS_BLOCK), ctypes.c_void_p,
                                       wintypes.ULONG, ctypes.c_int)
ntdll.NtSetInformationFile.restype = ctypes.c_long
ntdll.RtlNtStatusToDosError.argtypes = (ctypes.c_long,)
ntdll.RtlNtStatusToDosError.restype = wintypes.ULONG
kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.GetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.POINTER(BY_HANDLE_FILE_INFORMATION))
kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
kernel32.GetFileInformationByHandleEx.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
kernel32.GetFinalPathNameByHandleW.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
kernel32.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
                                     wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p)
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.CompareStringOrdinal.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int, wintypes.BOOL)
kernel32.CompareStringOrdinal.restype = ctypes.c_int

_EPOCH = 116444736000000000  # 1970-01-01 in FILETIME's 100 ns since 1601


def supported():
    return True  # the calls above exist from Windows Vista on; POSIX semantics fall back where missing


# -- errors


def _status_error(status, name=None):
    status &= 0xFFFFFFFF
    code = _ERRNO.get(status)
    if code is not None:
        return OSError(code, os.strerror(code), name)
    winerror = ntdll.RtlNtStatusToDosError(status)
    return OSError(None, ctypes.FormatError(winerror).strip(), name, winerror)


def _last_error(name=None):
    winerror = ctypes.get_last_error()
    return OSError(None, ctypes.FormatError(winerror).strip(), name, winerror)


def _no_entry(name):
    return OSError(errno.ENOENT, os.strerror(errno.ENOENT), name)


# -- handles


def _close(handle):
    kernel32.CloseHandle(handle)


def _utf16(text):
    return text.encode("utf-16-le", "surrogatepass")


def _nt_open(dir_handle, name, access, disposition, options):
    """NtCreateFile of `name` in the folder open as dir_handle: a handle, or OSError."""
    if not name or any(c in name for c in "\\/:\x00") or name in (".", ".."):
        raise _no_entry(name) if disposition == FILE_OPEN else OSError(errno.EINVAL, os.strerror(errno.EINVAL), name)
    data = _utf16(name)
    if len(data) > 0xFFFF:
        raise OSError(errno.ENAMETOOLONG, os.strerror(errno.ENAMETOOLONG), name)
    buffer = ctypes.create_string_buffer(data, len(data))
    uname = UNICODE_STRING(len(data), len(data), ctypes.cast(buffer, ctypes.c_void_p))
    attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), dir_handle, ctypes.pointer(uname),
                                   OBJ_CASE_INSENSITIVE, None, None)
    handle = wintypes.HANDLE()
    status = ntdll.NtCreateFile(ctypes.byref(handle), access | SYNCHRONIZE, ctypes.byref(attributes),
                                ctypes.byref(IO_STATUS_BLOCK()), None, FILE_ATTRIBUTE_NORMAL, SHARE_ALL, disposition,
                                options | FILE_SYNCHRONOUS_IO_NONALERT, None, 0)
    if status < 0:
        raise _status_error(status, name)
    if "~" in name and disposition == FILE_OPEN and not _named(handle.value, name):
        _close(handle.value)
        raise _no_entry(name)  # an 8.3 short name: only the long one finds it
    return handle.value


def _reopen(handle, access, options, name):
    """What `handle` has open, opened again with `access` and `options` (NtCreateFile relative to it with an
    empty name: ReOpenFile() refuses a folder's handle)."""
    uname = UNICODE_STRING(0, 0, None)
    attributes = OBJECT_ATTRIBUTES(ctypes.sizeof(OBJECT_ATTRIBUTES), handle, ctypes.pointer(uname), 0, None, None)
    again = wintypes.HANDLE()
    status = ntdll.NtCreateFile(ctypes.byref(again), access | SYNCHRONIZE, ctypes.byref(attributes),
                                ctypes.byref(IO_STATUS_BLOCK()), None, FILE_ATTRIBUTE_NORMAL, SHARE_ALL, FILE_OPEN,
                                options | FILE_SYNCHRONOUS_IO_NONALERT, None, 0)
    if status < 0:
        raise _status_error(status, name)
    return again.value


def _named(handle, name):
    """Whether what `handle` opened goes by `name` (case aside), not by another name for it."""
    path = _final_path(handle, VOLUME_NAME_NONE)
    own = path.rsplit("\\", 1)[-1]
    return kernel32.CompareStringOrdinal(own, len(_utf16(own)) // 2, name, len(_utf16(name)) // 2, True) == CSTR_EQUAL


def _final_path(handle, flags=VOLUME_NAME_DOS):
    size = 512
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = kernel32.GetFinalPathNameByHandleW(handle, buffer, size, FILE_NAME_NORMALIZED | flags)
        if length == 0:
            raise _last_error()
        if length < size:
            return buffer.value
        size = length + 1


def _path_open(path, access):
    """A handle of the folder at `path`, links on the way followed (the root the admin chose)."""
    handle = kernel32.CreateFileW(path, access | SYNCHRONIZE, SHARE_ALL, None, OPEN_EXISTING,
                                  FILE_FLAG_BACKUP_SEMANTICS, None)
    if handle == INVALID_HANDLE_VALUE:
        raise _last_error(path)
    try:
        if not st.S_ISDIR(_handle_stat(handle).st_mode):
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
    except BaseException:
        _close(handle)
        raise
    return handle


def _handle(fd):
    return msvcrt.get_osfhandle(fd)


def _fd(handle):
    try:
        return msvcrt.open_osfhandle(handle, 0)
    except BaseException:
        _close(handle)
        raise


# -- stat


class Stat:
    """What local.py reads of os.stat_result. A link (symbolic link or junction) is S_IFLNK."""

    __slots__ = ("st_mode", "st_ino", "st_dev", "st_nlink", "st_size", "st_mtime", "st_mtime_ns",
                 "st_file_attributes", "st_reparse_tag")

    def __init__(self, attributes, tag, size, mtime_filetime, ino, dev, nlink):
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT and tag & 0x20000000:  # a name surrogate
            mode = st.S_IFLNK | 0o777
        elif attributes & FILE_ATTRIBUTE_DIRECTORY:
            mode = st.S_IFDIR | 0o777
        else:
            mode = st.S_IFREG | (0o444 if attributes & FILE_ATTRIBUTE_READONLY else 0o666)
        self.st_mode = mode
        self.st_ino = ino
        self.st_dev = dev
        self.st_nlink = nlink
        self.st_size = size
        self.st_mtime_ns = (mtime_filetime - _EPOCH) * 100
        self.st_mtime = self.st_mtime_ns / 1e9
        self.st_file_attributes = attributes
        self.st_reparse_tag = tag


def _handle_stat(handle):
    info = BY_HANDLE_FILE_INFORMATION()
    if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise _last_error()
    tag = 0
    if info.dwFileAttributes & FILE_ATTRIBUTE_REPARSE_POINT:
        tags = FILE_ATTRIBUTE_TAG_INFO()
        if kernel32.GetFileInformationByHandleEx(handle, FileAttributeTagInfo, ctypes.byref(tags), ctypes.sizeof(tags)):
            tag = tags.ReparseTag
    mtime = info.ftLastWriteTime.dwHighDateTime << 32 | info.ftLastWriteTime.dwLowDateTime
    return Stat(info.dwFileAttributes, tag, info.nFileSizeHigh << 32 | info.nFileSizeLow, mtime,
                info.nFileIndexHigh << 32 | info.nFileIndexLow, info.dwVolumeSerialNumber, info.nNumberOfLinks)


def fstat(fd):
    return _handle_stat(_handle(fd))


def stat(path, *, dir_fd=None, follow_symlinks=True):
    """os.stat(fd), or of `path` in dir_fd without following it (follow_symlinks=False)."""
    if isinstance(path, int):
        return fstat(path)
    if dir_fd is None or follow_symlinks:
        raise NotImplementedError("only stat(fd) and stat(name, dir_fd=..., follow_symlinks=False)")
    handle = _nt_open(_handle(dir_fd), path, FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_OPEN_REPARSE_POINT)
    try:
        return _handle_stat(handle)
    finally:
        _close(handle)


def access(path, mode, *, dir_fd=None, follow_symlinks=True):
    """Whether `path` in dir_fd may be written (W_OK): not read-only. Other modes are always granted."""
    info = stat(path, dir_fd=dir_fd, follow_symlinks=False)
    return not (mode & os.W_OK and info.st_file_attributes & FILE_ATTRIBUTE_READONLY)


# -- opening


def open(path, flags, mode=0o777, *, dir_fd=None):  # noqa: A001 - os.open's name and arguments
    """A descriptor of `path` in dir_fd (".": dir_fd itself again, "..": the folder above), or of the
    absolute path `path` (links on the way followed): for walking (WALK), listing (LIST), reading (READ),
    or a new file to write (CREATE, which fails when the name is taken by anything, a link too)."""
    if flags & CREATE:
        access, disposition, options = GENERIC_WRITE | FILE_READ_ATTRIBUTES, FILE_CREATE, FILE_NON_DIRECTORY_FILE
    elif flags & READ:  # a folder opens too, as on Linux: local.py finds it isn't a file (fstat)
        access, disposition, options = GENERIC_READ, FILE_OPEN, 0
    elif flags & LIST:
        access, disposition, options = FILE_LIST_DIRECTORY | FILE_TRAVERSE | FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_DIRECTORY_FILE
    else:
        access, disposition, options = FILE_TRAVERSE | FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_DIRECTORY_FILE
    if dir_fd is None:
        return _fd(_path_open(path, access))
    if path == ".":
        return _fd(_reopen(_handle(dir_fd), access, options | FILE_OPEN_REPARSE_POINT, path))
    if path == "..":
        return _fd(_path_open(ntpath.dirname(_final_path(_handle(dir_fd))), access))
    if disposition == FILE_CREATE:
        return _fd(_nt_open(_handle(dir_fd), path, access, disposition, options))
    handle = _nt_open(_handle(dir_fd), path, access, disposition, options | FILE_OPEN_REPARSE_POINT)
    try:
        info = _handle_stat(handle)
        if st.S_ISLNK(info.st_mode):
            raise OSError(errno.ELOOP, os.strerror(errno.ELOOP), path)  # O_NOFOLLOW on a link
    except BaseException:
        _close(handle)
        raise
    if info.st_file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        # Some other reparse point (deduplication, cloud files): its own driver serves its data, for a
        # handle opened without FILE_OPEN_REPARSE_POINT. Opened again from this handle, not by name: it is
        # still this entry, whatever is put in its place meanwhile.
        try:
            again = _reopen(handle, access, options, path)
        finally:
            _close(handle)
        handle = again
    return _fd(handle)


def pread(fd, length, offset):
    os.lseek(fd, offset, os.SEEK_SET)
    return os.read(fd, length)


def fchmod(fd, mode):
    """Nothing: a new file is never read-only (local.py refuses to write over a read-only one)."""


# -- folders


class DirEntry:
    __slots__ = ("name", "_stat")

    def __init__(self, name, info):
        self.name = name
        self._stat = info

    def is_dir(self, *, follow_symlinks=True):
        return st.S_ISDIR(self._stat.st_mode)

    def stat(self, *, follow_symlinks=True):
        return self._stat


class _Entries:
    def __init__(self, entries):
        self._entries = entries

    def __iter__(self):
        return iter(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def close(self):
        pass


def scandir(fd):
    """The entries of the folder open (for listing) as fd, without "." and "..": names as they are, links
    (symbolic links, junctions) as S_IFLNK, never followed."""
    handle = _handle(fd)
    buffer = ctypes.create_string_buffer(64 * 1024)
    name_at = ctypes.sizeof(FILE_ID_BOTH_DIR_INFO)
    entries = []
    kind = FileIdBothDirectoryRestartInfo
    while True:
        if not kernel32.GetFileInformationByHandleEx(handle, kind, buffer, len(buffer)):
            error = ctypes.get_last_error()
            if error == ERROR_NO_MORE_FILES:
                break
            raise OSError(None, ctypes.FormatError(error).strip(), None, error)
        kind = FileIdBothDirectoryInfo
        raw = buffer.raw
        offset = 0
        while True:
            info = FILE_ID_BOTH_DIR_INFO.from_buffer(buffer, offset)
            start = offset + name_at
            name = raw[start:start + info.FileNameLength].decode("utf-16-le", "surrogatepass")
            if name not in (".", ".."):
                tag = info.EaSize if info.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT else 0
                entries.append(DirEntry(name, Stat(info.FileAttributes, tag, info.EndOfFile, info.LastWriteTime,
                                                   info.FileId & 0xFFFFFFFFFFFFFFFF, 0, 1)))
            if not info.NextEntryOffset:
                break
            offset += info.NextEntryOffset
    return _Entries(entries)


def mkdir(path, mode=0o777, *, dir_fd=None):
    _close(_nt_open(_handle(dir_fd), path, FILE_LIST_DIRECTORY, FILE_CREATE, FILE_DIRECTORY_FILE))


def readlink(path, *, dir_fd=None):
    """The target of the link `path` in dir_fd. A relative one with "/" between its names, as on Linux
    ("../a/b"); an absolute one as a Windows path ("C:\\a\\b", "\\\\server\\share\\a")."""
    handle = _nt_open(_handle(dir_fd), path, FILE_READ_ATTRIBUTES, FILE_OPEN, FILE_OPEN_REPARSE_POINT)
    try:
        buffer = ctypes.create_string_buffer(16 * 1024)
        returned = wintypes.DWORD()
        if not kernel32.DeviceIoControl(handle, FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer),
                                        ctypes.byref(returned), None):
            raise _last_error(path)
    finally:
        _close(handle)
    data = buffer.raw[:returned.value]
    tag = int.from_bytes(data[0:4], "little")
    if tag == IO_REPARSE_TAG_SYMLINK:
        flags = int.from_bytes(data[16:20], "little")
        names = data[20:]
    elif tag == IO_REPARSE_TAG_MOUNT_POINT:
        flags = 0
        names = data[16:]
    else:  # another kind of link (a WSL one): where it leads isn't known here
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), path)
    offset, length = int.from_bytes(data[8:10], "little"), int.from_bytes(data[10:12], "little")
    target = names[offset:offset + length].decode("utf-16-le", "surrogatepass")
    if flags & SYMLINK_FLAG_RELATIVE:
        return target.replace("\\", "/")
    if target.startswith("\\??\\UNC\\"):
        return "\\\\" + target[len("\\??\\UNC\\"):]
    if target.startswith("\\??\\") and target[5:7] == ":\\":
        return target[4:]
    return target  # a volume ("\\??\\Volume{...}\\"): absolute, never inside a root given as C:\...


def is_absolute(target):
    """Whether a link's target (from readlink()) is a path from a drive, a share or a volume."""
    return target.startswith(("\\", "/")) or ntpath.splitdrive(target)[0] != ""


def anchor(target):
    """The drive or share an absolute target starts from ("C:\\", "\\\\server\\share\\"), and the names after it."""
    drive, rest = ntpath.splitdrive(target)
    return drive + "\\", [name for name in rest.split("\\") if name]


def fold(path):
    """`path` for comparing with another, as Windows would."""
    return ntpath.normcase(path).rstrip("\\")


# -- deleting and renaming


def _open_for_change(dir_fd, path, options=0):
    return _nt_open(_handle(dir_fd), path, DELETE | FILE_READ_ATTRIBUTES, FILE_OPEN, options | FILE_OPEN_REPARSE_POINT)


def _delete(handle, name):
    info = FILE_DISPOSITION_INFORMATION_EX(FILE_DISPOSITION_DELETE | FILE_DISPOSITION_POSIX_SEMANTICS
                                           | FILE_DISPOSITION_IGNORE_READONLY_ATTRIBUTE)
    status = ntdll.NtSetInformationFile(handle, ctypes.byref(IO_STATUS_BLOCK()), ctypes.byref(info),
                                        ctypes.sizeof(info), FileDispositionInformationEx)
    if status & 0xFFFFFFFF in (STATUS_INVALID_INFO_CLASS, STATUS_INVALID_PARAMETER, STATUS_NOT_SUPPORTED,
                               STATUS_NOT_IMPLEMENTED):  # no POSIX semantics (older Windows, FAT): delete on close
        old = ctypes.c_ubyte(1)
        status = ntdll.NtSetInformationFile(handle, ctypes.byref(IO_STATUS_BLOCK()), ctypes.byref(old), 1,
                                            FileDispositionInformation)
    if status < 0:
        raise _status_error(status, name)


def unlink(path, *, dir_fd=None):
    """Delete the file or link `path` in dir_fd; a link to a folder goes too, never what it leads to."""
    handle = _open_for_change(dir_fd, path)
    try:
        if st.S_ISDIR(_handle_stat(handle).st_mode):
            raise OSError(errno.EISDIR, os.strerror(errno.EISDIR), path)
        _delete(handle, path)
    finally:
        _close(handle)


def rmdir(path, *, dir_fd=None):
    handle = _open_for_change(dir_fd, path, FILE_DIRECTORY_FILE)
    try:
        if not st.S_ISDIR(_handle_stat(handle).st_mode):  # a link to a folder
            raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), path)
        _delete(handle, path)
    finally:
        _close(handle)


def _rename(src_dir_fd, src, dst_dir_fd, dst, replace):
    if not dst or any(c in dst for c in "\\/:\x00") or dst in (".", ".."):
        raise OSError(errno.EINVAL, os.strerror(errno.EINVAL), dst)
    name = _utf16(dst)

    class FILE_RENAME_INFORMATION(ctypes.Structure):
        # Flags is FileRenameInformation's BOOLEAN ReplaceIfExists in its first byte. FileName is copied in
        # below: a c_char field would stop at the first zero byte, which UTF-16 has in nearly every name.
        _fields_ = [("Flags", wintypes.ULONG), ("RootDirectory", wintypes.HANDLE), ("FileNameLength", wintypes.ULONG),
                    ("FileName", ctypes.c_ubyte * len(name))]

    handle = _open_for_change(src_dir_fd, src)
    try:
        info = FILE_RENAME_INFORMATION((FILE_RENAME_REPLACE_IF_EXISTS if replace else 0) | FILE_RENAME_POSIX_SEMANTICS,
                                       _handle(dst_dir_fd), len(name))
        ctypes.memmove(info.FileName, name, len(name))
        status = ntdll.NtSetInformationFile(handle, ctypes.byref(IO_STATUS_BLOCK()), ctypes.byref(info),
                                            ctypes.sizeof(info), FileRenameInformationEx)
        if status & 0xFFFFFFFF in (STATUS_INVALID_INFO_CLASS, STATUS_INVALID_PARAMETER, STATUS_NOT_SUPPORTED,
                                   STATUS_NOT_IMPLEMENTED):
            info.Flags = 1 if replace else 0
            status = ntdll.NtSetInformationFile(handle, ctypes.byref(IO_STATUS_BLOCK()), ctypes.byref(info),
                                                ctypes.sizeof(info), FileRenameInformation)
        if status < 0:
            raise _status_error(status, src)
    finally:
        _close(handle)


def rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
    """Rename `src` in src_dir_fd to `dst` in dst_dir_fd, replacing a file there (a folder isn't)."""
    _rename(src_dir_fd, src, dst_dir_fd, dst, True)


def rename_noreplace(src_dir_fd, src, dst_dir_fd, dst):
    """rename() that fails with EEXIST instead of replacing anything, atomically."""
    _rename(src_dir_fd, src, dst_dir_fd, dst, False)
