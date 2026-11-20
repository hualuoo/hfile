"""What HFile needs from Windows that Python's standard library doesn't offer (ctypes, kernel32). Imported
only on Windows.

- lock(): the lock serve holds on its settings file while it runs (configfile.hold_while_serving), which
  `restore` and `upgrade` look for (configfile.in_use). Where Linux has flock(), this locks one byte far past
  the end of the file: Windows' locks keep others from reading what they cover, and the file is still read
  and even replaced (the installer writes config.toml while serve runs) by others.
- end_children_with_this_process(): serve's child (config/cli.py) ends when the outer process does, however
  that ends, as PR_SET_PDEATHSIG does it on Linux: a job object that Windows closes with the process.
- end_with_parent(): in the single executable the outer process ends when PyInstaller's bootloader, which
  started it, is killed.
- ui_language(): the language of the user's Windows, for the commands (config/i18n.py).
- replace(): os.replace() that may replace config.toml while serve holds its lock on it (configfile.write).
- make_private(), readable_by_others(): what mode 600 is on Linux. Windows ignores the mode a file is made
  with; the file takes its folder's access list, which on a folder like C:\\hfile lets every user of the
  machine read it. So the files with secrets in them (config.toml, the SQLite database, backups) get one of
  their own: this user, SYSTEM and the Administrators, who can read any file anyway.
"""

import ctypes
import locale
import os
import threading
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

GENERIC_READ = 0x80000000
FILE_SHARE_READ_WRITE_DELETE = 0x1 | 0x2 | 0x4
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
LOCKFILE_FAIL_IMMEDIATELY = 0x1
LOCKFILE_EXCLUSIVE_LOCK = 0x2
ERROR_LOCK_VIOLATION = 33
LOCK_OFFSET = (1 << 63) - 2  # the byte locked: past the end of any settings file

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9  # JOBOBJECTINFOCLASS


class OVERLAPPED(ctypes.Structure):
    _fields_ = [("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),
                ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD), ("hEvent", wintypes.HANDLE)]


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", wintypes.LARGE_INTEGER), ("PerJobUserTimeLimit", wintypes.LARGE_INTEGER),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]


class IO_COUNTERS(ctypes.Structure):
    _fields_ = [(name, ctypes.c_ulonglong) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                         "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


kernel32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                 wintypes.DWORD, wintypes.HANDLE)
kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.LockFileEx.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                ctypes.POINTER(OVERLAPPED))
kernel32.LockFileEx.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CreateJobObjectW.argtypes = (wintypes.LPVOID, wintypes.LPCWSTR)
kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.GetCurrentProcess.argtypes = ()
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.GetUserDefaultUILanguage.argtypes = ()
kernel32.GetUserDefaultUILanguage.restype = wintypes.WORD

_jobs = []  # kept open: Windows closes them, and ends what is in them, when this process ends


class Lock:
    """A held lock: close() lets go of it, and so does the end of the process."""

    def __init__(self, handle):
        self._handle = handle

    def close(self):
        handle, self._handle = self._handle, None
        if handle:
            kernel32.CloseHandle(handle)

    def __del__(self):
        self.close()


def lock(path, *, exclusive):
    """A lock on the settings file at `path`: shared, or exclusive. None when another holds one that keeps
    this one out; OSError when the file can't be opened."""
    handle = kernel32.CreateFileW(str(path), GENERIC_READ, FILE_SHARE_READ_WRITE_DELETE, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    where = OVERLAPPED(Offset=LOCK_OFFSET & 0xFFFFFFFF, OffsetHigh=LOCK_OFFSET >> 32)
    flags = LOCKFILE_FAIL_IMMEDIATELY | (LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
    if not kernel32.LockFileEx(handle, flags, 0, 1, 0, ctypes.byref(where)):
        error = ctypes.get_last_error()
        kernel32.CloseHandle(handle)
        if error == ERROR_LOCK_VIOLATION:
            return None
        raise ctypes.WinError(error)
    return Lock(handle)


def end_children_with_this_process():
    """Puts this process in a job that ends everything in it once this process has ended, however it ended:
    the processes it starts are in it too. Whether that took (it can't inside some other jobs)."""
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return False
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (kernel32.SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, ctypes.byref(info),
                                             ctypes.sizeof(info))
            and kernel32.AssignProcessToJobObject(job, kernel32.GetCurrentProcess())):
        kernel32.CloseHandle(job)
        return False
    _jobs.append(job)
    return True


PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
INFINITE = 0xFFFFFFFF
ERROR_INVALID_PARAMETER = 87  # OpenProcess of a process that has ended

kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.QueryFullProcessImageNameW.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
kernel32.WaitForSingleObject.restype = wintypes.DWORD


def end_with_parent(program):
    """This process ends once its parent has, when the parent runs `program` too: in the single executable,
    PyInstaller's bootloader, which started this process and waits for it. Killed (Task Manager, taskkill /F)
    it can pass nothing on, and this process and the server would go on holding the port. Ended at once
    (os._exit), so the server with it (end_children_with_this_process()), its requests in hand cut short:
    on Windows this process can stop it no other way. False when the parent has ended already."""
    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, os.getppid())
    if not handle:
        return ctypes.get_last_error() != ERROR_INVALID_PARAMETER
    size = wintypes.DWORD(32768)
    image = ctypes.create_unicode_buffer(size.value)
    if not (kernel32.QueryFullProcessImageNameW(handle, 0, image, ctypes.byref(size))
            and os.path.normcase(os.path.realpath(image.value)) == os.path.normcase(os.path.realpath(program))):
        kernel32.CloseHandle(handle)
        return True  # not the bootloader: a shell, a service manager

    def watch():
        kernel32.WaitForSingleObject(handle, INFINITE)
        os._exit(1)

    threading.Thread(target=watch, name="end-with-parent", daemon=True).start()
    return True


def ui_language():
    """The language Windows shows to this user, as a locale name: "zh_CN", "en_US"; "" when unknown."""
    return locale.windows_locale.get(kernel32.GetUserDefaultUILanguage(), "")


# -- replacing a file that is open

DELETE = 0x00010000
SYNCHRONIZE = 0x00100000
FILE_RENAME_INFO_EX = 22  # FILE_INFO_BY_HANDLE_CLASS: FileRenameInfoEx, Windows 10 1809
FILE_RENAME_FLAG_REPLACE_IF_EXISTS = 0x1
FILE_RENAME_FLAG_POSIX_SEMANTICS = 0x2
NOT_HERE = (1, 50, 87)  # ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED, ERROR_INVALID_PARAMETER: older Windows, FAT

kernel32.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
kernel32.SetFileInformationByHandle.restype = wintypes.BOOL


def replace(src, dst):
    """os.replace(src, dst), which also replaces a file another process has open (and lets others delete):
    serve's lock() on config.toml. os.replace() is refused that one; a rename with POSIX semantics isn't, as
    on Linux. os.replace() where there are none."""
    name = os.path.abspath(dst).encode("utf-16-le")  # a name alone would go to the current folder

    class FILE_RENAME_INFO(ctypes.Structure):
        # FileName is copied in below, with room for a closing zero.
        _fields_ = [("Flags", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE), ("FileNameLength", wintypes.DWORD),
                    ("FileName", ctypes.c_ubyte * (len(name) + 2))]

    handle = kernel32.CreateFileW(str(src), DELETE | SYNCHRONIZE, FILE_SHARE_READ_WRITE_DELETE, None, OPEN_EXISTING, 0, None)
    if handle == INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        info = FILE_RENAME_INFO(FILE_RENAME_FLAG_REPLACE_IF_EXISTS | FILE_RENAME_FLAG_POSIX_SEMANTICS, None, len(name))
        ctypes.memmove(info.FileName, name, len(name))
        if kernel32.SetFileInformationByHandle(handle, FILE_RENAME_INFO_EX, ctypes.byref(info), ctypes.sizeof(info)):
            return
        error = ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)
    if error not in NOT_HERE:
        raise ctypes.WinError(error)
    os.replace(src, dst)


# -- files only this user may read (what mode 600 is on Linux)

SE_FILE_OBJECT = 1
DACL_SECURITY_INFORMATION = 0x00000004
PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
TOKEN_QUERY = 0x0008
TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS
ACL_SIZE_INFORMATION_CLASS = 2  # AclSizeInformation
ACCESS_ALLOWED_ACE_TYPE = 0
OBJECT_INHERIT_ACE = 0x01
INHERIT_ONLY_ACE = 0x08
READING = 0x00000001 | 0x80000000 | 0x10000000  # FILE_READ_DATA, GENERIC_READ, GENERIC_ALL
SYSTEM, ADMINISTRATORS, OWNER_RIGHTS = "S-1-5-18", "S-1-5-32-544", "S-1-3-4"


class ACE_HEADER(ctypes.Structure):
    _fields_ = [("AceType", wintypes.BYTE), ("AceFlags", wintypes.BYTE), ("AceSize", wintypes.WORD)]


class ACCESS_ALLOWED_ACE(ctypes.Structure):
    _fields_ = [("Header", ACE_HEADER), ("Mask", wintypes.DWORD), ("SidStart", wintypes.DWORD)]


class ACL_SIZE_INFORMATION(ctypes.Structure):
    _fields_ = [("AceCount", wintypes.DWORD), ("AclBytesInUse", wintypes.DWORD), ("AclBytesFree", wintypes.DWORD)]


advapi32.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
advapi32.OpenProcessToken.restype = wintypes.BOOL
advapi32.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                         ctypes.POINTER(wintypes.DWORD))
advapi32.GetTokenInformation.restype = wintypes.BOOL
advapi32.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD,
                                                                          ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
advapi32.GetSecurityDescriptorDacl.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p),
                                               ctypes.POINTER(wintypes.BOOL))
advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
advapi32.SetNamedSecurityInfoW.argtypes = (wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p)
advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
advapi32.GetNamedSecurityInfoW.argtypes = (wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
advapi32.GetAclInformation.argtypes = (ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int)
advapi32.GetAclInformation.restype = wintypes.BOOL
advapi32.GetAce.argtypes = (ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p))
advapi32.GetAce.restype = wintypes.BOOL
kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
kernel32.LocalFree.restype = ctypes.c_void_p


def _sid_string(sid):
    text = ctypes.c_void_p()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.wstring_at(text.value)
    finally:
        kernel32.LocalFree(text)


def user_sid():
    """The user this process runs as: "S-1-5-21-…" ("S-1-5-18" for a service run as LocalSystem)."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, TOKEN_USER, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        if not advapi32.GetTokenInformation(token, TOKEN_USER, buffer, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        return _sid_string(ctypes.c_void_p.from_buffer(buffer).value)  # TOKEN_USER starts with the SID's address
    finally:
        kernel32.CloseHandle(token)


def make_private(path, *, folder=False):
    """Only this user, SYSTEM and the Administrators may open `path`, whatever its folder allows; a folder's
    files and folders take that too. OSError when it can't be done."""
    inherit = "OICI" if folder else ""
    sddl = f"D:P(A;{inherit};FA;;;{user_sid()})(A;{inherit};FA;;;SY)(A;{inherit};FA;;;BA)"
    descriptor = ctypes.c_void_p()
    if not advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present, dacl, defaulted = wintypes.BOOL(), ctypes.c_void_p(), wintypes.BOOL()
        if not advapi32.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl),
                                                  ctypes.byref(defaulted)):
            raise ctypes.WinError(ctypes.get_last_error())
        error = advapi32.SetNamedSecurityInfoW(str(path), SE_FILE_OBJECT,
                                               DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
                                               None, None, dacl, None)
        if error:
            raise ctypes.WinError(error)
    finally:
        kernel32.LocalFree(descriptor)


def readable_by_others(path, *, for_new_files=False):
    """Whether the access list of `path` lets others than this user, SYSTEM and the Administrators read it;
    for_new_files: a folder's, read for the files made in it later, which take its entries marked so
    (OBJECT_INHERIT). False when it can't be read, as for a missing file."""
    dacl, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
    if advapi32.GetNamedSecurityInfoW(str(path), SE_FILE_OBJECT, DACL_SECURITY_INFORMATION, None, None,
                                      ctypes.byref(dacl), None, ctypes.byref(descriptor)):
        return False
    try:
        if not dacl.value:
            return True  # no access list at all: everyone may
        size = ACL_SIZE_INFORMATION()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), ACL_SIZE_INFORMATION_CLASS):
            return False
        trusted = {user_sid(), SYSTEM, ADMINISTRATORS, OWNER_RIGHTS}
        for index in range(size.AceCount):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                continue
            entry = ctypes.cast(ace, ctypes.POINTER(ACCESS_ALLOWED_ACE)).contents
            if entry.Header.AceType != ACCESS_ALLOWED_ACE_TYPE:
                continue
            if not entry.Header.AceFlags & OBJECT_INHERIT_ACE if for_new_files else entry.Header.AceFlags & INHERIT_ONLY_ACE:
                continue
            if entry.Mask & READING and _sid_string(ace.value + ACCESS_ALLOWED_ACE.SidStart.offset) not in trusted:
                return True
        return False
    finally:
        kernel32.LocalFree(descriptor)
