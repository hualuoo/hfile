"""Browsing and the file operations, every decision through apps.storages.access (decisions D8, D10, D12, D17).

    access = open_storage(request, key)
    listing(request, access, "/docs", sort="name", order="asc")    # the folder, its readme and config
    item(request, access, "/docs/a.pdf")                           # one file or folder
    mkdir(access, "/docs", "new"), rename(access, "/docs", "a.txt", "b.txt"),
    relocate(access, MOVE | COPY, "/docs", ["a", "b.txt"], "/archive"), delete(access, "/docs", ["a"])

Paths here are the client's (relative to the actor's root); access.path() makes them storage paths and
the policy decides before any driver is called for them. Operations on several items (move, copy,
delete) check every item first and then run those that passed, each with its own result: {"name",
"type", "ok", "code", "message"}. A missing folder password stops the whole request instead (403
password_required, before anything ran), so that the browser asks for it and sends the request again.

D17: an existing target is refused with 目标已存在 (code "exists"), checked before calling a driver
that would overwrite and mapped from the driver's AlreadyExists. Moving into the folder an item is in
(spelled in other case too, on a backend that ignores case), and renaming to the same name, do
nothing; copying into the same folder is 目标已存在. Drivers without a copy of their own (FTP, SFTP)
get files copied through the site (read and written in chunks, never whole in memory); folders there
are refused (unsupported).

Driver errors reach the client as plain messages by kind; a backend's own text (a server's reply,
addresses) only for staff, and in the log.
"""

from __future__ import annotations

import logging
import re
from typing import Any, BinaryIO

from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail

from django.utils.translation import gettext, gettext_lazy, ngettext, pgettext_lazy

from apps.accounts.permissions import Permission
from apps.core.times import iso
from apps.storages import access as policy
from apps.storages import instances
from apps.storages.access import COPY, DELETE, LINK, LIST, MKDIR, MOVE, READ, RENAME, SHORT_LINK, STAT, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
    Unsupported,
)

from . import readme as readmes
from . import signals, transfer
from .kinds import Kinds

logger = logging.getLogger("hfile.files")

SORT_FIELDS = ("name", "size", "time")
ORDERS = ("asc", "desc")
BATCH_MAX = 1000  # items of one move, copy or delete

# Stops the whole request (the browser asks for the password, or waits).
_WHOLE = frozenset({policy.PASSWORD_REQUIRED, policy.PASSWORD_WRONG, policy.PASSWORD_THROTTLED,
                    policy.PERMISSION_DENIED, policy.LOGIN_REQUIRED, policy.STORAGE_DISABLED})

EXISTS = gettext_lazy("The target already exists")


class FileError(APIException):
    """A refusal of the files API: {"detail", "code"} with a stable code (D38)."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message, code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = {"detail": ErrorDetail(message, code), "code": code}


def driver_problem(error: DriverError, *, staff: bool = False) -> tuple[str, str, int]:
    """(code, message, HTTP status) for a driver's error. The messages of InvalidPath are the site's
    own rules for names; the other kinds get plain words, and a backend's text only for staff."""
    if isinstance(error, NotFound):
        return "not_found", gettext("The file or folder doesn't exist."), 404
    if isinstance(error, AlreadyExists):
        return "exists", EXISTS, 409
    if isinstance(error, NotADirectory):
        return "not_a_folder", gettext("This isn't a folder."), 400
    if isinstance(error, InvalidPath):
        return "invalid_name", error.message, 400
    if isinstance(error, Unsupported):
        return "unsupported", gettext("This kind of storage doesn't support this."), 400
    if isinstance(error, AuthError):
        return "storage_auth", (error.message if staff else gettext("There is a problem with the storage source's account or authorization. Please contact the administrator.")), 502
    if isinstance(error, BackendError):
        return "storage_error", (error.message if staff else gettext("Couldn't connect to the storage. Please try again later.")), 502
    return "failed", (error.message if staff else gettext("Not done: the storage returned an error.")), 400


def staff_of(access) -> bool:
    """driver_problem's `staff` for a request's access: an admin, and not inside a share (the uploads use it too)."""
    user = access.actor.user
    return bool(getattr(user, "is_staff", False)) and not access.actor.via_share


def _driver_error(access, error: DriverError, what: str) -> FileError:
    """The API's error for a driver's (raise it); the backend's own words go to the log."""
    code, message, status_code = driver_problem(error, staff=staff_of(access))
    if code in ("storage_auth", "storage_error", "failed"):
        logger.warning("Storage %s: %s failed: %s", access.storage.key, what, error.message)
    return FileError(code, message, status_code=status_code)


def _driver(access):
    return access.driver


# --- sorting (D39: on the server, case-insensitive natural order, folders first) -------------------

_DIGITS = re.compile(r"(\d+)")


def natural_key(name: str) -> tuple:
    """Windows-like: "file2" before "file10", case ignored. Digit runs compare by value (without
    turning them into ints: a name may hold thousands of digits), then more leading zeros first
    ("x01" before "x1")."""
    parts = _DIGITS.split(name.casefold())
    key: list[Any] = []
    for index, part in enumerate(parts):
        if index % 2:
            digits = part.lstrip("0") or "0"
            key.append((len(digits), digits, -len(part)))
        else:
            key.append(part)
    return (tuple(key), name.casefold(), name)


def sort_entries(entries, field: str = "name", order: str = "asc") -> list:
    """Folders first in both orders; within each, by name, size or time (unknown first when
    ascending), names breaking ties; desc reverses within each."""
    def by(entry):
        if field == "size":
            return (entry.size is not None, entry.size or 0, natural_key(entry.name))
        if field == "time":
            modified = entry.modified.timestamp() if entry.modified is not None else 0.0
            return (entry.modified is not None, modified, natural_key(entry.name))
        return natural_key(entry.name)

    reverse = order == "desc"
    folders = sorted((entry for entry in entries if entry.is_dir), key=by, reverse=reverse)
    files = sorted((entry for entry in entries if not entry.is_dir), key=by, reverse=reverse)
    return folders + files


# --- items ----------------------------------------------------------------------------------------

def client_path(access, storage_path: str) -> str:
    return access.relative(storage_path)


def item_data(access, entry, kinds: Kinds, urls: transfer.ListingUrls | None) -> dict:
    """One entry as the API gives it. url: where its bytes are; null for folders, without the preview
    and download permissions (§6.2 step 6), below a 禁止下载 rule (/pd/ would refuse it), and for a file
    a folder password still locks (a rule that covers the file but not its folder, `/dir/**` or
    `**/*.pdf`: the listing of the folder needs no password, the file does). `locked` marks a folder or
    file whose password this browser hasn't entered."""
    data = {
        "name": entry.name,
        "path": client_path(access, entry.path),
        "type": "folder" if entry.is_dir else "file",
        "size": entry.size,
        "modified": iso(entry.modified),
        "kind": None if entry.is_dir else kinds.of(entry.name),
        "url": None,
    }
    locked = None
    if access.password_rule(entry.path) is not None:
        locked = not access.is_unlocked(entry.path, keys=access.opened)
        data["locked"] = locked
    if not entry.is_dir and urls is not None and not locked and not _download_disabled(access, entry.path):
        data["url"] = urls.url(entry.path, ignore_hidden=needs_bypass(access, entry.path))
    return data


def _download_disabled(access, storage_path: str) -> bool:
    """A 禁止下载 rule covers it (a file shown inside a folder the rule matches)."""
    if access.bypass_filters or not access.rules.no_download:
        return False
    return policy.filter_state(access.rules, storage_path)["download_disabled"]


def needs_bypass(access, storage_path: str) -> bool:
    """Whether a link of this file must skip the filter rules when served (h=1): only for an actor with
    忽略隐藏, and only when a rule would refuse it, so that a rule added later still stops the others."""
    if not access.bypass_filters or not (access.rules.inaccessible or access.rules.no_download):
        return False
    state = policy.filter_state(access.rules, storage_path)
    return state["inaccessible"] or state["download_disabled"]


def permissions(access) -> dict[str, bool]:
    """What the caller may do here, as the policy will apply it (§6.8 change): the grant's codes, links
    with the download permission they need too (D40a), the bypasses only where they apply."""
    granted = {code: access.has(code) for code in Permission.values}
    granted[Permission.GENERATE_LINK] = access.may(LINK)
    granted[Permission.GENERATE_SHORT_LINK] = access.may(SHORT_LINK)
    granted[Permission.IGNORE_HIDDEN] = access.bypass_filters
    granted[Permission.IGNORE_PASSWORD] = access.bypass_passwords
    return granted


def capabilities(access) -> dict[str, Any]:
    """The driver's flags (read from its instance), and how copying works here: files are copied
    through the site where the backend has no copy of its own, folders only by the backend."""
    flags = instances.capabilities(access.storage)
    flags["copy_files"] = True
    flags["copy_folders"] = bool(flags.get("copy"))
    return flags


def storage_data(storage) -> dict:
    return {"key": storage.key, "name": storage.name, "type": storage.type, "type_label": storage.type_label}


def listing(request, access, folder_path: str | None, *, sort: str | None = None, order: str | None = None,
            password: str | None = None, site=None) -> dict:
    """A folder in one answer (D39): its items (sorted, hidden ones left out), its readme, the caller's
    permissions, the storage's capabilities, the gallery default and the actor's root."""
    from apps.siteconf.models import SiteSettings

    site = site or SiteSettings.load()
    sort = sort if sort in SORT_FIELDS else site.default_sort_field
    order = order if order in ORDERS else site.default_sort_order
    folder = access.path(folder_path)
    access.check(LIST, folder, password=password)
    driver = _driver(access)
    try:
        entries = driver.list(folder)
    except NotFound:
        raise FileError("not_found", gettext("The folder doesn't exist."), status_code=404) from None
    except NotADirectory:
        raise FileError("not_a_folder", gettext("This isn't a folder."), status_code=400) from None
    except DriverError as error:
        raise _driver_error(access, error, f"listing {folder}") from None
    # A password sent with the request (API clients) opens the folder for this request: its entries too.
    shown = sort_entries(access.visible(entries, keys=access.opened), sort, order)
    kinds = Kinds(site)
    urls = transfer.ListingUrls(request, access.storage) if access.may(READ) else None
    items = [item_data(access, entry, kinds, urls) for entry in shown]
    readme = None
    if site.show_document:
        readme = readmes.folder_readme(request, access, folder, shown, site=site)
    return {
        "storage": storage_data(access.storage),
        "path": client_path(access, folder),
        "root": access.root,
        "items": items,
        "readme": readme,
        "permissions": permissions(access),
        "capabilities": capabilities(access),
        "gallery_default": access.storage.gallery_default,
        "sort": {"field": sort, "order": order},
    }


def item(request, access, path: str | None, *, password: str | None = None, site=None) -> dict:
    """One file or folder (§6.10), with the filter checks of a listing: a file 禁止下载 is refused,
    hidden ones are found by their path (D10). Files the caller may read carry `url` and
    `inline_url`, a same-origin signed link with inline disposition for the viewers (W1)."""
    from apps.siteconf.models import SiteSettings

    site = site or SiteSettings.load()
    storage_path = access.path(path)
    access.check(STAT, storage_path, password=password)
    if _download_disabled(access, storage_path):
        raise AccessDenied(policy.DOWNLOAD_DISABLED, gettext("This file can't be downloaded."))
    driver = _driver(access)
    try:
        entry = driver.stat(storage_path)
    except DriverError as error:
        raise _driver_error(access, error, f"stat {storage_path}") from None
    readable = access.may(READ) and not entry.is_dir
    urls = transfer.ListingUrls(request, access.storage) if readable else None
    data = item_data(access, entry, Kinds(site), urls)
    if storage_path == access.root:
        data["name"] = ""
    data["inline_url"] = None
    if readable:
        data["inline_url"] = transfer.download_url(request, access.storage, storage_path, inline=True, same_origin=True,
                                                   ignore_hidden=needs_bypass(access, storage_path))
    return data


# --- operations -----------------------------------------------------------------------------------

def _changed(access, changed_paths: list[str]) -> None:
    if not changed_paths:
        return
    # A receiver that fails doesn't fail the operation (Django logs its error).
    signals.changed.send_robust(sender=type(access.storage), source=access.storage, paths=list(changed_paths))


def _existing_name(name) -> str:
    """A name of something that exists, as the client sends it."""
    if not isinstance(name, str) or not name or "/" in name or name in (".", ".."):
        raise FileError("invalid_name", gettext("Invalid name."))
    return name


def _join(folder: str, name: str) -> str:
    return (folder.rstrip("/") or "") + "/" + name


def _new_name(access, name) -> str:
    driver = _driver(access)
    if not isinstance(name, str):
        raise FileError("invalid_name", gettext("The name should be text."))
    try:
        return paths.check_name(name, forbidden=str(getattr(driver, "forbidden_chars", "")),
                                max_bytes=int(getattr(driver, "max_name_bytes", 255)))
    except InvalidPath as error:
        raise FileError("invalid_name", error.message) from None


def _stat(access, storage_path: str):
    """The entry, or None when it isn't there. Other driver errors go up."""
    try:
        return _driver(access).stat(storage_path)
    except NotFound:
        return None


def _folder(access, client: str | None, what: str = pgettext_lazy("inside a sentence", "folder")):
    """The storage path of a folder that must exist."""
    storage_path = access.path(client)
    try:
        entry = _driver(access).stat(storage_path)
    except NotFound:
        raise FileError("not_found", gettext("The %(what)s doesn't exist.") % {"what": what}, status_code=404) from None
    except DriverError as error:
        raise _driver_error(access, error, f"stat {storage_path}") from None
    if not entry.is_dir:
        raise FileError("not_a_folder", gettext("The %(what)s isn't a folder.") % {"what": what})
    return storage_path


def mkdir(access, folder_path: str | None, name, *, password: str | None = None) -> dict:
    """新建文件夹: one name segment in an existing folder (§6.11 change)."""
    if isinstance(name, str) and "/" in name:
        raise FileError("invalid_name", gettext("A folder name can't contain / (nested folders can't be created at once)"))
    new_name = _new_name(access, name)
    folder = access.path(folder_path)
    target = access.path(_join(client_path(access, folder), new_name))
    access.check(MKDIR, target, password=password)
    _folder(access, client_path(access, folder))
    try:
        if _stat(access, target) is not None:
            raise FileError("exists", EXISTS, status_code=409)
        _driver(access).mkdir(target)
    except DriverError as error:
        raise _driver_error(access, error, f"mkdir {target}") from None
    _changed(access, [target])
    return {"name": new_name, "path": client_path(access, target), "type": "folder"}


def rename(access, folder_path: str | None, name, new_name, *, password: str | None = None) -> dict:
    """重命名 in place. The same name does nothing; an existing name is 目标已存在 (D17), except the same
    entry when only the case changes on a backend that ignores case (_same_entry)."""
    _require(access, RENAME)
    name = _existing_name(name)
    new_name = _new_name(access, new_name)
    folder = access.path(folder_path)
    source = access.path(_join(client_path(access, folder), name))
    access.check(STAT, source, password=password)  # before asking the backend anything of it
    try:
        entry = _stat(access, source)
    except DriverError as error:
        raise _driver_error(access, error, f"stat {source}") from None
    if entry is None:
        raise FileError("not_found", gettext("The file or folder doesn't exist."), status_code=404)
    target = access.path(_join(client_path(access, folder), new_name))
    access.check(RENAME, source, is_dir=entry.is_dir, password=password)
    if new_name == name:
        return {"name": name, "path": client_path(access, source), "type": "folder" if entry.is_dir else "file"}
    access.check_target(RENAME, target, is_dir=entry.is_dir, password=password)
    try:
        existing = _stat(access, target)
        if existing is not None and not _same_entry(access, folder, entry, existing, new_name):
            raise FileError("exists", EXISTS, status_code=409)
        _driver(access).rename(source, new_name)
    except DriverError as error:
        raise _driver_error(access, error, f"rename {source}") from None
    _changed(access, [source, target])
    return {"name": new_name, "path": client_path(access, target), "type": "folder" if entry.is_dir else "file"}


def _same_entry(access, folder: str, entry, existing, new_name: str) -> bool:
    """Whether `existing`, what the backend found at the new name, is the entry being renamed: only the case
    changes, and the backend ignores case. Drivers may name what they find as it was asked for, so unless both
    have the entry's own name, the folder's listing tells: an entry with exactly the new name is another one."""
    if new_name.casefold() != entry.name.casefold():
        return False
    if existing.name == entry.name:
        return True
    return all(item.name != new_name for item in _driver(access).list(folder))


def _same_folder(access, source: str, folder: str) -> bool:
    """Whether `folder`, where a move found an item of the name already, is the one `source` is in, spelled in other
    case on a backend that ignores case: that item is `source` itself. Where the two spellings differ, a listing with
    both names exactly in it has two folders."""
    here = paths.parent(source)
    if here.casefold() != folder.casefold():
        return False
    above = paths.ROOT
    for mine, theirs in zip(paths.segments(here), paths.segments(folder)):
        if mine != theirs:
            names = {item.name for item in _driver(access).list(above)}
            if mine in names and theirs in names:
                return False
        above = paths.join(above, theirs)
    return True


def _require(access, operation: str) -> None:
    """The operation's permission, for the whole request (the same for every item)."""
    if not access.may(operation):
        label = {MOVE: gettext("Move"), COPY: gettext("Copy"), DELETE: gettext("Delete"), RENAME: gettext("Rename")}[operation]
        raise AccessDenied(policy.PERMISSION_DENIED, gettext("You don't have the \"%(label)s\" permission.") % {"label": label}, permission=operation)


def _result(name: str, entry, ok: bool, code: str | None = None, message: str = "") -> dict:
    return {"name": name, "type": None if entry is None else ("folder" if entry.is_dir else "file"),
            "ok": ok, "code": code, "message": message}


class _Skip(Exception):
    """An item that won't run: its result."""

    def __init__(self, code: str, message: str, entry=None):
        super().__init__(message)
        self.code, self.message, self.entry = code, message, entry

    def result(self, name: str) -> dict:
        return _result(name, self.entry, False, self.code, self.message)


def _checked_stat(access, source: str, password: str | None):
    """An item's entry: the policy first (filters and folder passwords of the path; no type needed),
    so that the backend is never asked about what the caller can't reach and the answers don't tell
    what exists there; then the backend."""
    try:
        access.check(STAT, source, password=password)
    except AccessDenied as error:
        raise _Skip(*_denied(error)) from None
    try:
        entry = _stat(access, source)
    except DriverError as error:
        code, message, _ = driver_problem(error, staff=staff_of(access))
        raise _Skip(code, message) from None
    if entry is None:
        raise _Skip("not_found", gettext("The file or folder doesn't exist."))
    return entry


def _names(names) -> list[str]:
    if not isinstance(names, list) or not names:
        raise FileError("invalid", gettext("Please select the files or folders to act on."))
    if len(names) > BATCH_MAX:
        raise FileError("invalid", ngettext("At most %(limit)s item at a time.", "At most %(limit)s items at a time.", BATCH_MAX) % {"limit": BATCH_MAX})
    seen = []
    for name in names:
        name = _existing_name(name)
        if name not in seen:
            seen.append(name)
    return seen


def _denied(error: AccessDenied) -> tuple[str, str]:
    if error.code in _WHOLE:
        raise error
    return error.code, error.message


class _Reader:
    """A ReadStream as the file a driver's write() reads, a chunk at a time."""

    def __init__(self, stream):
        self._chunks = iter(stream)
        self._buffer = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._buffer) < size:
            try:
                chunk = next(self._chunks)
            except StopIteration:
                break
            self._buffer += chunk
            if size < 0:
                continue
        if size < 0:
            data, self._buffer = self._buffer, b""
        else:
            data, self._buffer = self._buffer[:size], self._buffer[size:]
        return data


def copy_through_site(driver, source: str, target: str) -> None:
    """Copy one file with open() and write(), for backends without a copy of their own (FTP, SFTP)."""
    stream = driver.open(source)
    try:
        if stream.size is None:
            raise Unsupported(gettext("This file's size is unknown, so it can't be copied."))
        reader: BinaryIO = _Reader(stream)  # type: ignore[assignment]
        driver.write(target, reader, stream.size)
    finally:
        stream.close()


def relocate(access, operation: str, folder_path: str | None, names, target_path: str | None, *,
             password: str | None = None, target_password: str | None = None) -> dict:
    """移动 or 复制 items of one folder into another folder of the same storage, keeping their names.
    Every item is checked first (with its own type, from the backend), then those that passed run."""
    if operation not in (MOVE, COPY):
        raise ValueError(operation)
    _require(access, operation)
    names = _names(names)
    driver = _driver(access)
    if operation == MOVE and not driver.can_move:
        raise FileError("unsupported", gettext("This kind of storage doesn't support moving."))
    folder = access.path(folder_path)
    target_password = target_password if target_password is not None else password
    access.check(STAT, access.path(target_path), password=target_password)
    target_folder = _folder(access, target_path, gettext("target folder"))
    results: list[dict] = []
    planned: list[tuple[int, str, str, Any]] = []
    for name in names:
        source = access.path(_join(client_path(access, folder), name))
        try:
            entry = _checked_stat(access, source, password)
        except _Skip as skip:
            results.append(skip.result(name))
            continue
        target = _join(target_folder, name)
        if entry.is_dir and paths.is_inside(target_folder, source):
            into_itself = (gettext("A folder can't be moved into itself or a folder inside it.") if operation == MOVE
                           else gettext("A folder can't be copied into itself or a folder inside it."))
            results.append(_result(name, entry, False, "into_itself", into_itself))
            continue
        try:
            access.check(operation, source, is_dir=entry.is_dir, password=password)
            if paths.parent(source) == target_folder and operation == MOVE:
                results.append(_result(name, entry, True, "same_folder", gettext("Already in this folder.")))
                continue
            access.check_target(operation, target, is_dir=entry.is_dir, password=target_password)
        except AccessDenied as error:
            code, message = _denied(error)
            results.append(_result(name, entry, False, code, message))
            continue
        if operation == COPY and entry.is_dir and not driver.can_copy:
            results.append(_result(name, entry, False, "unsupported", gettext("This kind of storage doesn't support copying folders.")))
            continue
        try:
            exists = _stat(access, target) is not None
            same = exists and operation == MOVE and _same_folder(access, source, target_folder)
        except DriverError as error:
            code, message, _ = driver_problem(error, staff=staff_of(access))
            results.append(_result(name, entry, False, code, message))
            continue
        if same:
            results.append(_result(name, entry, True, "same_folder", gettext("Already in this folder.")))
            continue
        if exists:
            results.append(_result(name, entry, False, "exists", EXISTS))
            continue
        planned.append((len(results), source, target, entry))
        results.append(_result(name, entry, True))
    changed: list[str] = []
    for index, source, target, entry in planned:
        try:
            if operation == MOVE:
                driver.move(source, target_folder)
                changed.extend([source, target])
            elif driver.can_copy:
                driver.copy(source, target_folder)
                changed.append(target)
            else:
                copy_through_site(driver, source, target)
                changed.append(target)
        except DriverError as error:
            code, message, _ = driver_problem(error, staff=staff_of(access))
            if code in ("storage_auth", "storage_error", "failed"):
                logger.warning("Storage %s: %s %s failed: %s", access.storage.key, operation, source, error.message)
            results[index] = _result(results[index]["name"], entry, False, code, message)
    _changed(access, changed)
    return _summary(results)


def delete(access, folder_path: str | None, names, *, password: str | None = None) -> dict:
    """删除 items of one folder (a folder with everything in it). Names are required: never the folder
    itself, never the root (§6.11 change)."""
    _require(access, DELETE)
    names = _names(names)
    driver = _driver(access)
    folder = access.path(folder_path)
    results: list[dict] = []
    planned: list[tuple[int, str, Any]] = []
    for name in names:
        source = access.path(_join(client_path(access, folder), name))
        if source in (paths.ROOT, access.root):
            raise FileError("invalid_name", gettext("The root folder can't be deleted."))
        try:
            entry = _checked_stat(access, source, password)
        except _Skip as skip:
            results.append(skip.result(name))
            continue
        try:
            access.check(DELETE, source, is_dir=entry.is_dir, password=password)
        except AccessDenied as error:
            code, message = _denied(error)
            results.append(_result(name, entry, False, code, message))
            continue
        planned.append((len(results), source, entry))
        results.append(_result(name, entry, True))
    changed: list[str] = []
    for index, source, entry in planned:
        try:
            driver.delete(source)
            changed.append(source)
        except DriverError as error:
            code, message, _ = driver_problem(error, staff=staff_of(access))
            if code in ("storage_auth", "storage_error", "failed"):
                logger.warning("Storage %s: delete %s failed: %s", access.storage.key, source, error.message)
            results[index] = _result(results[index]["name"], entry, False, code, message)
    _changed(access, changed)
    return _summary(results)


def _summary(results: list[dict]) -> dict:
    succeeded = sum(1 for result in results if result["ok"])
    return {"results": results, "succeeded": succeeded, "failed": len(results) - succeeded}

