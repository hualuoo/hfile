"""What the upload API does (apps/uploads/views.py is the HTTP side of it). Every decision about files is
apps.storages.access's: the permission, the root, the filter rules and the folder passwords.

Creating a session (create()):
  - the name per apps.storages.drivers.paths.check_name with the driver's forbidden_chars and max_name_bytes,
    and so is every folder the upload will create;
  - the folders above the file are looked up on the storage (stat), from the file's folder up to the
    first one that exists: a file among them is refused (object stores' write() doesn't check that);
    the missing ones are created by the upload, which then needs 新建文件夹 as well, and each of them
    passes the filter rules and folder passwords on its own (access.check's `created`);
  - an existing file is refused with the code "exists" unless `overwrite`, which needs 删除 as well as
    上传 (decision D17a); a folder of that name is refused;
  - a direct upload (S3 family, OneDrive/SharePoint, 又拍云, unless the storage's 代理上传 is on) gets the
    driver's UploadTarget; otherwise the spool must have room for the file plus a margin, and for what
    the same user's (the same browser's, for the guest) other uploads still have to send.

Proxied sessions then take chunks (receive()) in order, each at most CHUNK_SIZE; the last one makes the
site stream the spool file into driver.write() (finish()). A failed write keeps the session, so it can
be tried again (complete()) until it expires. Direct sessions end with complete(): the site looks at
the file (stat) and compares its size with the one announced.

Every request on a session checks its owner (the user; for the guest also the browser's Django
session) and the permissions again (a grant may be revoked halfway). The folder passwords the maker
had unlocked for it (or sent with it) are kept with the session, as a short link keeps its creator's.

Requests that write a chunk or finish take the session with a lease (locked_until, a conditional
UPDATE, renewed by a thread while a file is written to the storage): two requests on one session, in one
process or several, never write at once; a process that died lets go when its lease runs out; cancelling
and the cleanup leave a held session alone.

The activity log gets an entry for each file that arrives (by the request that marks the session done), each
folder a folder upload makes and each text save.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import re
import secrets
import threading
import time
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from django.db import connections, models, transaction
from django.db.models import F, Q, Sum
from django.utils import timezone
from django.utils.translation import gettext as _, ngettext
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail

from apps.accounts.permissions import Permission
from apps.activity import log as activity
from apps.core import net
from apps.core.throttles import UNKNOWN, address_key
from apps.core.times import iso
from apps.files import signals as file_signals
from apps.files.service import driver_problem, staff_of
from apps.storages import access as policy
from apps.storages.access import DELETE, MKDIR, UPLOAD, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import (
    AlreadyExists,
    DriverError,
    Entry,
    InvalidPath,
    NotFound,
)

from . import spool, text
from .models import Kind, State, UploadSession

logger = logging.getLogger("hfile.uploads")

MiB = 1024 * 1024
CHUNK_SIZE = 16 * MiB  # bytes per chunk of a proxied upload: well under the 64 MiB request limit
MAX_SIZE = 2**50  # bytes announced for one file (1 PiB): a guard against nonsense, not a quota
IDLE = timedelta(hours=24)  # a session nobody touched this long expires
DONE_KEPT = timedelta(hours=1)  # a finished session stays readable this long (the panel may ask)
# A request writing a chunk or a file holds the session this long, and renews it every HEARTBEAT seconds
# while it works (two renewals may fail before it runs out). After a crash the session is free again
# within LEASE; meanwhile requests on it are told "busy", with the seconds to wait.
LEASE = timedelta(seconds=90)
HEARTBEAT = 30
RECENT = timedelta(minutes=30)  # sessions touched this recently count as active
ACTIVE_LIMIT = 20  # active sessions per user (per browser for the guest)
GUEST_ADDRESS_LIMIT = 60  # active sessions of the guest per address (several visitors behind one NAT)
SPACE_MARGIN = 64 * MiB  # the spool keeps this much free besides what its sessions still need
TEXT_MAX = 5 * MiB  # bytes of a file the text editor saves
FOLDERS_MAX = 1000  # folders in one request to make_folders()
OWNER_KEY = "hfile.uploads.owner"


# --- errors ---------------------------------------------------------------------------------------------

class UploadError(APIException):
    """{"detail": "...", "code": "...", ...more}, like apps.storages.access.AccessDenied: the panel branches
    on the code (exists, offset, busy, ...)."""

    def __init__(self, code: str, message: str, *, status_code: int = status.HTTP_400_BAD_REQUEST, **extra: Any):
        super().__init__(message, code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra
        self.detail = {"detail": ErrorDetail(message, code), "code": code, **extra}

    def __str__(self):
        return self.message


# The upload API's codes where apps.files.service.driver_problem's differ (docs/api.md lists them).
_UPLOAD_CODES = {"exists": "conflict", "invalid_name": "invalid_path", "not_a_folder": "invalid_path"}


def driver_error(error: DriverError, *, staff: bool = False) -> UploadError:
    """A driver's error as an answer, in the file API's words (apps.files.service.driver_problem): what the
    backend said (its host and port, an FTP server's reply, a path on the server) only for staff; for
    everybody else it goes to the log. staff: files.service.staff_of(access)."""
    code, message, status_code = driver_problem(error, staff=staff)
    if code in ("storage_auth", "storage_error", "failed"):
        logger.warning("Upload: the storage failed: %s", error.message)
    return UploadError(_UPLOAD_CODES.get(code, code), message, status_code=status_code)


def _spool_error(error: spool.SpoolError) -> UploadError:
    """The answer says what went wrong; where (the spool's path) and the system's words go to the log."""
    logger.warning("Upload spool: %s%s", error, f" ({error.detail})" if error.detail else "")
    return UploadError("spool", str(error), status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)


def _invalid_path(error: InvalidPath) -> AccessDenied:
    return AccessDenied(policy.INVALID_PATH, _("Invalid path: %(message)s") % {"message": error.message}, status=400)


# --- who --------------------------------------------------------------------------------------------------

def owner_of(request, actor: policy.Actor, *, create: bool = False) -> str | None:
    """What binds a session to the browser: "" for a user (the user is enough); for the guest the hash
    of a random token in the Django session, made when `create` (else None without one)."""
    if not actor.is_guest:
        return ""
    session = getattr(request, "session", None)
    if session is None:
        return None
    token = session.get(OWNER_KEY)
    if not isinstance(token, str) or len(token) != 32:
        if not create:
            return None
        token = secrets.token_hex(16)
        session[OWNER_KEY] = token
    return hashlib.sha256(token.encode()).hexdigest()


def _now() -> datetime:
    return timezone.now()


# --- paths -----------------------------------------------------------------------------------------------

def check_new_name(driver, name: str) -> str:
    try:
        return paths.check_name(name, forbidden=str(driver.forbidden_chars), max_bytes=int(driver.max_name_bytes))
    except InvalidPath as error:
        raise UploadError("invalid_name", error.message, name=name) from None


def _stat(access, path: str) -> Entry | None:
    try:
        return access.driver.stat(path)
    except NotFound:
        return None
    except DriverError as error:
        raise driver_error(error, staff=staff_of(access)) from None


def _stat_after_write(driver, path: str) -> Entry | None:
    """The file just written, for the answer; None if the backend can't say now (the write stands)."""
    try:
        return driver.stat(path)
    except DriverError:
        return None


def _relative_or_none(access, storage_path: str) -> str | None:
    try:
        return access.relative(storage_path)
    except AccessDenied:
        return None


def missing_folders(access, folder: str) -> list[str]:
    """The folders from `folder` up that don't exist yet, top first; stops at the first that exists or
    at the actor's root. UploadError when one of them is a file."""
    missing: list[str] = []
    current = folder
    while True:
        entry = _stat(access, current)
        if entry is not None:
            if not entry.is_dir:
                where = _relative_or_none(access, current)
                raise UploadError("parent_is_file", _('"%(where)s" is a file, not a folder.') % {"where": where},
                                  status_code=status.HTTP_409_CONFLICT, path=where)
            break
        if current == paths.ROOT:
            raise UploadError("not_found", _("This storage source's root folder is gone. Please ask the administrator to check its settings."),
                              status_code=status.HTTP_404_NOT_FOUND)
        missing.append(current)
        if current == access.root:
            break
        current = paths.parent(current)
    missing.reverse()
    return missing


def client_join(folder: str | None, *more: str | None) -> str:
    """Paths the client sent, joined (still relative to its root); access.path() checks the result."""
    joined = paths.clean(folder)
    for part in more:
        inner = paths.clean(part)
        if inner != paths.ROOT:
            joined = inner if joined == paths.ROOT else joined + inner
    return joined


def item_of(access, path: str, entry: Entry | None) -> dict[str, Any]:
    modified = entry.modified if entry is not None else None
    return {
        "name": paths.name(path),
        "path": _relative_or_none(access, path),
        "size": entry.size if entry is not None else None,
        "modified": iso(modified),
    }


def _keys_for(access, targets: Iterable[str]) -> list[list[int]]:
    """The folder passwords `targets` need that this request had (unlocked, or sent with it)."""
    found: dict[tuple[int, int], None] = {}
    for target in targets:
        for pair in access.unlocked_keys(target):
            found[tuple(pair)] = None
    return [list(pair) for pair in found]


def overwrite_allowed(access, path: str, *, password: str | None = None, keys=()) -> None:
    """D17a: replacing a file destroys it, so it needs 删除 as well as 上传."""
    if not access.has(Permission.DELETE):
        raise AccessDenied(policy.PERMISSION_DENIED, _('Overwriting an existing file also needs the "Delete" permission.'), permission=Permission.DELETE)
    access.check(DELETE, path, is_dir=False, password=password, keys=keys)


def _changed(storage, changed: Iterable[str]) -> None:
    """apps.files.signals.changed (contract W5): receivers must not fail the upload."""
    changed = list(dict.fromkeys(changed))
    if not changed:
        return
    for _receiver, outcome in file_signals.changed.send_robust(sender=UploadSession, source=storage, paths=changed):
        if isinstance(outcome, Exception):
            logger.error("A receiver of files.signals.changed failed: %r", outcome, exc_info=outcome)


def _log_upload(session: UploadSession) -> None:
    """The activity log's entry for a file that arrived (written after it, see apps/activity/log.py)."""
    detail = {"size": session.size, **({"replaced": True} if session.overwrite else {})}
    activity.record(activity.Kind.UPLOAD, quiet=True, actor=session.user, storage=session.storage, path=session.path,
                    detail=detail)


# --- the cleanup ---------------------------------------------------------------------------------------

ORPHAN_AGE = 3600  # seconds: a spool file without a session (a crash between the two) goes after this
CLEANUP_EVERY = 600  # seconds between the light cleanups done while sessions are made
_cleanup_lock = threading.Lock()
_cleaned_at: float | None = None


CLEANUP_BATCH = 500  # sessions or spool files per query
CLEANUP_MAX = 20000  # per run: the rest goes the next time


def _not_held(now: datetime) -> Q:
    """Sessions no request holds: a lease (a chunk being written, a finish) keeps a session alive."""
    return Q(locked_until__isnull=True) | Q(locked_until__lte=now)


def remove_expired(now: datetime) -> int:
    """`hfile cleanup` (apps.core.cleanup): expired sessions and their spool files, and spool files left
    without a session (older than ORPHAN_AGE). In batches, at most CLEANUP_MAX of each per run. Rows go
    in the transaction, files once it commits (so not with --dry-run), and only for rows really gone."""
    expired = Q(expires_at__lt=now) & _not_held(now)
    ids = list(UploadSession.objects.filter(expired).values_list("id", flat=True)[:CLEANUP_MAX])
    removed = 0
    for start in range(0, len(ids), CLEANUP_BATCH):
        # The same conditions again: a session taken since it was listed stays.
        removed += UploadSession.objects.filter(expired, id__in=ids[start:start + CLEANUP_BATCH]).delete()[0]
    cutoff = now.timestamp() - ORPHAN_AGE
    old = [session_id for session_id, mtime in spool.files() if mtime < cutoff][:CLEANUP_MAX]
    orphans = []
    for start in range(0, len(old), CLEANUP_BATCH):
        batch = old[start:start + CLEANUP_BATCH]
        known = set(UploadSession.objects.filter(id__in=batch).values_list("id", flat=True))
        orphans += [session_id for session_id in batch if session_id not in known]
    candidates = list(dict.fromkeys(ids + orphans))

    def remove_files():
        for start in range(0, len(candidates), CLEANUP_BATCH):
            batch = candidates[start:start + CLEANUP_BATCH]
            alive = set(UploadSession.objects.filter(id__in=batch).values_list("id", flat=True))
            for session_id in batch:
                if session_id in alive:
                    continue
                try:
                    spool.remove(session_id)
                except OSError as error:
                    logger.warning("Couldn't remove the spool file of upload %s: %s", session_id, error)

    if candidates:
        transaction.on_commit(remove_files)
    return removed + len(orphans)


def _light_cleanup() -> None:
    """Expired sessions also go while sessions are made, at most every CLEANUP_EVERY seconds per
    process, so the spool doesn't fill up where nobody set up cron for `hfile cleanup`."""
    global _cleaned_at
    with _cleanup_lock:
        if _cleaned_at is not None and time.monotonic() - _cleaned_at < CLEANUP_EVERY:
            return
        _cleaned_at = time.monotonic()
    try:
        with transaction.atomic():
            remove_expired(_now())
    except Exception:  # noqa: BLE001 - never in the way of an upload
        logger.exception("The light cleanup of upload sessions failed")


# --- creating a session --------------------------------------------------------------------------------

def _client_address(request) -> str:
    ip = net.client_ip(request)
    return address_key(ip) if ip else UNKNOWN


def _check_active(actor, owner: str, address: str, now: datetime) -> None:
    """At most ACTIVE_LIMIT active sessions per user, per browser for the guest; and for the guest at
    most GUEST_ADDRESS_LIMIT per address, so dropping the cookies doesn't make room."""
    recent = UploadSession.objects.filter(user=actor.user, updated_at__gte=now - RECENT).exclude(state=State.DONE)
    if actor.is_guest and recent.filter(address=address).count() >= GUEST_ADDRESS_LIMIT:
        raise UploadError("too_many", _("You have too many uploads going at once. Please wait for the earlier ones to finish."),
                          status_code=status.HTTP_429_TOO_MANY_REQUESTS)
    mine = recent.filter(owner=owner) if actor.is_guest else recent
    if mine.count() >= ACTIVE_LIMIT:
        raise UploadError("too_many", ngettext("Too many uploads at once (at most %(limit)s). Please wait for the earlier ones to finish.", "Too many uploads at once (at most %(limit)s). Please wait for the earlier ones to finish.", ACTIVE_LIMIT) % {"limit": ACTIVE_LIMIT},
                          status_code=status.HTTP_429_TOO_MANY_REQUESTS)


def _reserved(actor, owner: str, now: datetime) -> int:
    """Bytes the actor's own receiving sessions (recent ones) still have to get. Only their own: what
    other people announced but don't send must not keep everybody else out."""
    sessions = UploadSession.objects.filter(kind=Kind.PROXY, state=State.RECEIVING, user=actor.user,
                                            updated_at__gte=now - RECENT)
    if actor.is_guest:
        sessions = sessions.filter(owner=owner)
    left = sessions.aggregate(left=Sum(F("size") - F("received"), output_field=models.BigIntegerField()))["left"]
    return int(left or 0)


def _check_space(size: int, actor, owner: str, now: datetime) -> None:
    try:
        free = spool.free_bytes()
    except spool.SpoolError as error:
        raise _spool_error(error) from None
    if free - _reserved(actor, owner, now) < size + SPACE_MARGIN:
        raise UploadError("no_space", _("The server is out of disk space for uploads in progress. Please "
                                        "try again later, or ask the administrator to free some space."),
                          status_code=status.HTTP_507_INSUFFICIENT_STORAGE)


def create(request, *, storage: str, folder: str | None, name: str, size: int, relative: str | None = None,
           overwrite: bool = False, password: str | None = None) -> tuple[UploadSession, policy.Access]:
    """A new session (see the top). An empty file that goes through the site is written at once: the
    session comes back done."""
    _light_cleanup()
    access = policy.open_storage(request, storage)
    try:
        target_folder = access.path(client_join(folder, relative))
        path = paths.join(target_folder, name)
    except InvalidPath as error:
        if isinstance(name, str) and name and ("/" in name or paths.dots_only(name)):
            raise UploadError("invalid_name", _("A name can't contain /, or be only dots and spaces (like . or ..)."), name=name) from None
        raise _invalid_path(error) from None
    if len(path) > policy.PATH_MAX:
        raise AccessDenied(policy.INVALID_PATH, ngettext("The path is too long: at most %(limit)s character.", "The path is too long: at most %(limit)s characters.", policy.PATH_MAX) % {"limit": policy.PATH_MAX}, status=400)
    # The policy first, for the file itself: nothing is looked up where the actor may not go.
    access.check(UPLOAD, path, password=password)
    driver = access.driver
    check_new_name(driver, name)
    existing = _stat(access, path)
    if existing is not None and existing.is_dir:
        raise UploadError("folder_exists", _("\"%(name)s\" is an existing folder; a file can't overwrite it.") % {"name": name},
                          status_code=status.HTTP_409_CONFLICT, name=name)
    created = missing_folders(access, target_folder) if existing is None else []
    for made in created:
        check_new_name(driver, paths.name(made))
    if created:
        access.check(UPLOAD, path, created=created, password=password)
    if existing is not None:
        if not overwrite:
            raise UploadError("exists", _('"%(name)s" already exists.') % {"name": name}, status_code=status.HTTP_409_CONFLICT, name=name,
                              path=access.relative(path), size=existing.size,
                              can_overwrite=access.has(Permission.DELETE))
        overwrite_allowed(access, path, password=password)
    keys = _keys_for(access, [path, *created])

    now = _now()
    actor = access.actor
    owner = owner_of(request, actor, create=True)
    if owner is None:
        raise UploadError("no_session", _("The browser didn't send the session cookie, so it can't upload. Please allow this site's cookies and try again."))
    address = _client_address(request) if actor.is_guest else ""
    _check_active(actor, owner, address, now)
    target = None
    if driver.can_direct_upload and not access.storage.proxy_upload and size > 0:
        try:
            target = driver.upload_target(path, size)
        except DriverError as error:
            raise driver_error(error, staff=staff_of(access)) from None
    session = UploadSession(
        storage=access.storage, user=actor.user, owner=owner, address=address, path=path, created_folders=created, keys=keys,
        size=size, kind=Kind.DIRECT if target is not None else Kind.PROXY,
        state=State.WAITING if target is not None else State.RECEIVING, overwrite=existing is not None,
        updated_at=now, expires_at=now + IDLE,
    )
    if target is not None:
        session.save()
        session.target = target  # answered once, never stored: OneDrive's URL is a credential
        return session, access
    _check_space(size, actor, owner, now)
    session.save()
    try:
        spool.create(session.id)
    except (OSError, spool.SpoolError) as error:
        session.delete()
        if isinstance(error, OSError):
            error = spool.SpoolError(_("The upload's temporary file couldn't be created. Please try again later."), str(error.strerror or error))
        raise _spool_error(error) from None
    if size == 0:
        try:
            finish(session, access)
        except BaseException:
            UploadSession.objects.filter(pk=session.pk).delete()  # the client never got its id
            spool.remove(session.id)
            raise
        session.refresh_from_db()
    return session, access


# --- the requests on a session ----------------------------------------------------------------------

def load(request, session_id) -> UploadSession:
    """The caller's own session, else 404 (whose it is isn't told)."""
    actor = policy.actor_for(request)
    session = UploadSession.objects.filter(pk=session_id, user=actor.user).select_related("storage").first()
    if session is None or (actor.is_guest and session.owner != owner_of(request, actor)):
        raise UploadError("not_found", _("There is no such upload; it may have expired or been canceled."), status_code=status.HTTP_404_NOT_FOUND)
    return session


def recheck(request, session: UploadSession) -> policy.Access:
    """The permissions again, as when the session was made (a grant can be revoked halfway)."""
    access = policy.open_storage(request, session.storage)
    keys = session.keys or ()
    access.check(UPLOAD, session.path, created=session.created_folders or (), keys=keys)
    if session.overwrite:
        overwrite_allowed(access, session.path, keys=keys)
    return access


_RANGE = re.compile(r"bytes (\d{1,16})-(\d{1,16})/(\d{1,16})")


def parse_range(header: str | None, size: int) -> tuple[int, int]:
    """(first byte, length) of a chunk's Content-Range: bytes a-b/size."""
    match = _RANGE.fullmatch((header or "").strip())
    if not match:
        raise UploadError("bad_range", _("Every chunk needs a Content-Range header, like bytes 0-16777215/file-size."))
    first, last, total = (int(group) for group in match.groups())
    if total != size:
        raise UploadError("bad_range", _("The file size in Content-Range (%(total)s) isn't the size given when the upload started (%(size)s).") % {"total": total, "size": size})
    if last < first or last >= size:
        raise UploadError("bad_range", _("The range in Content-Range is wrong."))
    length = last - first + 1
    if length > CHUNK_SIZE:
        raise UploadError("chunk_too_large", ngettext("A chunk is at most %(limit)s byte.", "A chunk is at most %(limit)s bytes.", CHUNK_SIZE) % {"limit": CHUNK_SIZE},
                          status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, chunk_size=CHUNK_SIZE)
    return first, length


def _expected(session: UploadSession) -> UploadError:
    return UploadError("offset", _("This chunk is in the wrong place: the next one should start at byte %(received)s.") % {"received": session.received},
                       status_code=status.HTTP_409_CONFLICT, expected=session.received)


def _busy(session_id=None) -> UploadError:
    """Another request holds the session: `wait`, the seconds until its lease runs out (it is renewed
    while that request works; a crashed one's ends then)."""
    wait = 1
    if session_id is not None:
        until = UploadSession.objects.filter(pk=session_id).values_list("locked_until", flat=True).first()
        if until is not None:
            wait = max(1, math.ceil((until - _now()).total_seconds()))
    return UploadError("busy", _("This upload is busy with another request. Please try again later."), status_code=status.HTTP_409_CONFLICT, wait=wait)


class _Lease:
    """The session taken by this request until `until`; renew() moves it on while we still hold it."""

    def __init__(self, session_id, until: datetime):
        self.session_id = session_id
        self.until = until

    @classmethod
    def take(cls, session_id, *, states, now: datetime, becomes: str | None = None, **conditions) -> _Lease | None:
        """The session, if it is in one of `states` (and matches `conditions`) and nobody holds it;
        `becomes`: its new state, set in the same UPDATE (no moment where a cancel could slip in)."""
        until = now + LEASE
        changes = {"locked_until": until, "updated_at": now, "expires_at": now + IDLE}
        if becomes is not None:
            changes.update(state=becomes, error="", error_code="")
        taken = (UploadSession.objects.filter(pk=session_id, state__in=states, **conditions)
                 .filter(_not_held(now)).update(**changes))
        return cls(session_id, until) if taken else None

    def mine(self):
        return UploadSession.objects.filter(pk=self.session_id, locked_until=self.until)

    def renew(self) -> bool:
        """Moves the lease on; False when it was lost meanwhile (it ran out and another took it)."""
        until = _now() + LEASE
        if self.mine().update(locked_until=until):
            self.until = until
            return True
        return False


class _Renewer:
    """Keeps a lease while a long write runs, from a thread of its own every HEARTBEAT seconds: the
    driver may not read for minutes (a backend assembling a big object). `lost`: it couldn't."""

    def __init__(self, lease: _Lease):
        self.lease = lease
        self.lost = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"upload-lease-{lease.session_id}", daemon=True)

    def __enter__(self) -> _Renewer:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()
        self._thread.join()

    def _run(self) -> None:
        try:
            while not self._stop.wait(HEARTBEAT):
                try:
                    held = self.lease.renew()
                except Exception:  # noqa: BLE001 - the database away for a moment: try again
                    logger.warning("Renewing the lease of upload %s failed", self.lease.session_id, exc_info=True)
                    continue
                if not held:
                    self.lost = True
                    return
        finally:
            connections.close_all()  # this thread's own database connections


def receive(request, session: UploadSession, *, content_range: str | None, content_length: int | None,
            body) -> tuple[UploadSession, policy.Access]:
    """One chunk of a proxied session, in order; the last one finishes the upload."""
    if session.kind != Kind.PROXY:
        raise UploadError("not_proxy", _("This upload goes straight from the browser to the storage; its data isn't sent here."), status_code=status.HTTP_409_CONFLICT)
    if session.state != State.RECEIVING:
        raise UploadError("not_receiving", _("All of this upload's data has been received."), status_code=status.HTTP_409_CONFLICT,
                          state=session.state, expected=session.received)
    first, length = parse_range(content_range, session.size)
    if content_length != length:
        raise UploadError("bad_length", ngettext("The request's Content-Length should be this chunk's %(length)s byte.", "The request's Content-Length should be this chunk's %(length)s bytes.", length) % {"length": length})
    if first != session.received:
        raise _expected(session)
    access = recheck(request, session)
    lease = _Lease.take(session.pk, states=[State.RECEIVING], now=_now(), received=first)
    if lease is None:
        session.refresh_from_db()
        if session.state == State.RECEIVING and session.received != first:
            raise _expected(session)
        raise _busy(session.pk)
    try:
        with _Renewer(lease):  # a slow client or disk: the lease must not run out under the write
            spool.write_chunk(session.id, first, body, length)
    except BaseException as error:
        lease.mine().update(locked_until=None)
        if isinstance(error, spool.ShortBody):
            raise UploadError("short_body", _("This chunk didn't arrive in full. Please send it again.")) from None
        if isinstance(error, spool.SpoolError):
            raise _spool_error(error) from None
        if isinstance(error, OSError):
            logger.error("Writing a chunk of upload %s failed: %s", session.id, error)
            raise UploadError("spool", _("The server couldn't write the temporary file (is the disk full?). Please try again later."),
                              status_code=status.HTTP_507_INSUFFICIENT_STORAGE) from None
        raise
    moved = lease.mine().filter(received=first).update(received=first + length, locked_until=None, updated_at=_now())
    session.refresh_from_db()
    if not moved:
        raise _expected(session) if session.received != first + length else _busy(session.pk)
    if session.received == session.size:
        finish(session, access)
        session.refresh_from_db()
    return session, access


def _fail(lease: _Lease, error: Exception) -> None:
    code = getattr(error, "code", "") or "storage_error"
    message = getattr(error, "message", "") or str(error) or _("Writing to the storage failed.")
    now = _now()
    lease.mine().update(state=State.FAILED, locked_until=None, error_code=str(code)[:40], error=str(message)[:500],
                        updated_at=now, expires_at=now + IDLE)


def _done(lease: _Lease, session: UploadSession, item: dict[str, Any]) -> bool:
    """Marks the session done if this request still holds it; False when another one took it over."""
    now = _now()
    return bool(lease.mine().update(
        state=State.DONE, locked_until=None, received=session.size, item=item, error="", error_code="",
        updated_at=now, expires_at=now + DONE_KEPT))


def finish(session: UploadSession, access: policy.Access) -> None:
    """Writes a proxied session's spool into the driver, once every byte is in. On a failure the
    session keeps the spool (state failed) and the error is raised; complete() tries again."""
    lease = _Lease.take(session.pk, states=[State.RECEIVING, State.FAILED, State.FINISHING], now=_now(),
                        becomes=State.FINISHING, kind=Kind.PROXY, received=session.size)
    if lease is None:
        raise _busy(session.pk)
    driver = access.driver
    try:
        if spool.size_of(session.id) != session.size:
            raise UploadError("spool", _("The data received doesn't match the file size. Please upload again."), status_code=status.HTTP_409_CONFLICT)
        # A folder above it may have gone since the session was made: write() would make it again, which
        # needs 新建文件夹 and the folder's rules like any folder an upload makes.
        missing = missing_folders(access, paths.parent(session.path))
        if any(folder not in (session.created_folders or ()) for folder in missing):
            access.check(UPLOAD, session.path, created=missing, keys=session.keys or ())
        if not session.overwrite:
            # Somebody may have put a file there since the session was made (D17: never silently).
            found = _stat(access, session.path)
            if found is not None:
                raise UploadError("exists", _('"%(name)s" already exists (someone uploaded a file with the same name meanwhile).') % {"name": paths.name(session.path)},
                                  status_code=status.HTTP_409_CONFLICT, name=paths.name(session.path),
                                  path=_relative_or_none(access, session.path), size=found.size,
                                  can_overwrite=access.has(Permission.DELETE) and not found.is_dir)
        with spool.open_for_reading(session.id) as stream, _Renewer(lease):
            driver.write(session.path, stream, session.size)
        entry = _stat_after_write(driver, session.path)
    except DriverError as error:
        mapped = driver_error(error, staff=staff_of(access))
        _fail(lease, mapped)
        raise mapped from None
    except (UploadError, AccessDenied) as error:
        _fail(lease, error)
        raise
    except spool.SpoolError as error:
        mapped = _spool_error(error)
        _fail(lease, mapped)
        raise mapped from None
    except BaseException:
        logger.exception("Finishing upload %s failed", session.id)
        _fail(lease, UploadError("storage_error", _("Something went wrong writing to the storage. Please try again.")))
        raise
    item = item_of(access, session.path, entry)
    if item["size"] is None:
        item["size"] = session.size
    if not _done(lease, session, item):
        # The lease ran out (the database was away longer than LEASE) and another request is writing
        # the same bytes: it finishes the session.
        logger.warning("Upload %s lost its lease while writing; another request finishes it", session.id)
        return
    try:
        spool.remove(session.id)
    except OSError as error:
        logger.warning("Couldn't remove the spool file of upload %s: %s", session.id, error)
    _changed(session.storage, [*session.created_folders, session.path])
    _log_upload(session)


def complete(request, session: UploadSession, *, overwrite: bool = False) -> tuple[UploadSession, policy.Access]:
    """A direct upload's bytes are with the backend: check the file and finish. A proxied session that
    has every byte (its last write failed, or its answer got lost): write it again; `overwrite` lets
    it replace a file that turned up meanwhile (with 删除, D17a)."""
    if session.state == State.DONE:
        return session, policy.open_storage(request, session.storage)
    if session.kind == Kind.PROXY:
        if overwrite and not session.overwrite:
            overwrite_allowed(policy.open_storage(request, session.storage), session.path, keys=session.keys or ())
            UploadSession.objects.filter(pk=session.pk).update(overwrite=True)
            session.overwrite = True
        access = recheck(request, session)
        if session.received != session.size:
            raise _expected(session)
        finish(session, access)
        session.refresh_from_db()
        return session, access
    access = recheck(request, session)
    lease = _Lease.take(session.pk, states=[State.WAITING, State.FAILED, State.FINISHING], now=_now(),
                        becomes=State.FINISHING)
    if lease is None:
        raise _busy(session.pk)
    try:
        entry = _stat(access, session.path)
        if entry is None or entry.is_dir:
            raise UploadError("missing", _("The file isn't in the storage yet: the browser's upload may not have finished. Please upload again."),
                              status_code=status.HTTP_409_CONFLICT)
        if entry.size is not None and entry.size != session.size:
            raise UploadError("size_mismatch", ngettext("The file in the storage has %(size)s byte, not the %(expected)s bytes given when the upload started. Please upload again.", "The file in the storage has %(size)s bytes, not the %(expected)s bytes given when the upload started. Please upload again.", entry.size) % {"size": entry.size, "expected":session.size},
                              status_code=status.HTTP_409_CONFLICT)
    except (UploadError, AccessDenied) as error:
        _fail(lease, error)
        raise
    if _done(lease, session, item_of(access, session.path, entry)):
        _changed(session.storage, [*session.created_folders, session.path])
        _log_upload(session)
    session.refresh_from_db()
    return session, access


def cancel(session: UploadSession) -> None:
    """DELETE: the session and its spool file go; not while a request holds it (a chunk being written,
    the file being written to the storage)."""
    deleted = UploadSession.objects.filter(_not_held(_now()), pk=session.pk).delete()[0]
    if not deleted:
        busy = _busy(session.pk)
        raise UploadError("busy", _("This upload is being processed (written to the temporary "
                                    "file or the storage). Please cancel it a little later."), status_code=status.HTTP_409_CONFLICT,
                          wait=busy.extra["wait"])
    if session.kind == Kind.PROXY:
        try:
            spool.remove(session.id)
        except OSError as error:
            logger.warning("Couldn't remove the spool file of upload %s: %s", session.id, error)


def describe(session: UploadSession, access: policy.Access | None = None) -> dict[str, Any]:
    """The session as the API answers it."""
    state = session.state
    interrupted = state == State.FINISHING and (session.locked_until is None or session.locked_until <= _now())
    folder, name = paths.split(session.path)
    data: dict[str, Any] = {
        "id": str(session.id),
        "kind": session.kind,
        "state": State.FAILED if interrupted else state,
        "name": name,
        "path": _relative_or_none(access, session.path) if access is not None else None,
        "folder": _relative_or_none(access, folder) if access is not None else None,
        "size": session.size,
        "received": session.received,
        "overwrite": session.overwrite,
        "url": f"/api/v1/uploads/{session.id}/",
        "complete_url": f"/api/v1/uploads/{session.id}/complete/",
        "expires_at": iso(session.expires_at),
        "error": None,
        "item": session.item,
    }
    if session.kind == Kind.PROXY:
        data["chunk_size"] = CHUNK_SIZE
    if interrupted:
        data["error"] = {"code": "interrupted", "detail": _("Writing to the storage was interrupted. You can try again.")}
    elif state == State.FAILED:
        data["error"] = {"code": session.error_code or "storage_error", "detail": session.error or _("The upload failed.")}
    target = getattr(session, "target", None)
    if target is not None:
        data["target"] = {
            "kind": target.kind, "url": target.url, "method": target.method, "headers": dict(target.headers),
            "fields": dict(target.fields), "file_field": target.file_field, "chunk_size": target.chunk_size,
            "expires_in": target.expires_in,
        }
    return data


# --- folders -------------------------------------------------------------------------------------------

def folder_tree(folders: Iterable[str]) -> list[str]:
    """The folders a folder upload names and every folder above them, as "/a", "/a/b", parents first.
    Linear in what was sent: a tree of the names, at most FOLDERS_MAX of them, DEPTH_MAX deep."""
    tree: dict[str, dict] = {}
    count = 0
    for relative in folders:
        parts = paths.segments(paths.clean(relative))
        if len(parts) > policy.DEPTH_MAX:
            raise UploadError("invalid_path", ngettext("Folders are nested too deep: at most %(limit)s level.", "Folders are nested too deep: at most %(limit)s levels.", policy.DEPTH_MAX) % {"limit": policy.DEPTH_MAX})
        node = tree
        for part in parts:
            child = node.get(part)
            if child is None:
                count += 1
                if count > FOLDERS_MAX:
                    raise UploadError("too_many_folders", ngettext("At most %(limit)s folder can be created at a time.", "At most %(limit)s folders can be created at a time.", FOLDERS_MAX) % {"limit": FOLDERS_MAX})
                child = node[part] = {}
            node = child
    ordered: list[str] = []
    level = [("", tree)]
    while level:
        below = []
        for prefix, node in level:
            for name in sorted(node):
                ordered.append(f"{prefix}/{name}")
                below.append((ordered[-1], node[name]))
        level = below
    return ordered


def make_folders(request, *, storage: str, folder: str | None, folders: list[str], password: str | None = None) -> dict:
    """Creates the folders of a folder upload (relative to `folder`, which must exist), parents first,
    empty ones too. 新建文件夹 and the policy for every folder named come first, before anything is
    looked up on the storage (no answer about a place the actor may not go). Existing folders are
    fine; a file in the way is an error for that folder and those below it."""
    access = policy.open_storage(request, storage)
    if not access.has(Permission.NEW_FOLDER):
        raise AccessDenied(policy.PERMISSION_DENIED, _("You don't have the \"%(label)s\" permission.") % {"label": Permission.NEW_FOLDER.label},
                           permission=Permission.NEW_FOLDER)
    try:
        ordered = folder_tree(folders)
        base = access.path(folder)
        targets = {rel: access.path(client_join(folder, rel)) for rel in ordered}
    except InvalidPath as error:
        raise _invalid_path(error) from None
    for rel in ordered:  # every one checked before any is looked up or made
        try:
            access.check(MKDIR, targets[rel], password=password)
        except AccessDenied as error:
            error.detail = {**error.detail, "folder": _relative_or_none(access, targets[rel])}
            raise
    driver = access.driver
    found = _stat(access, base)
    if found is None:
        raise UploadError("not_found", _("The folder to put it in doesn't exist."), status_code=status.HTTP_404_NOT_FOUND)
    if not found.is_dir:
        where = _relative_or_none(access, base)
        raise UploadError("parent_is_file", _('"%(where)s" is a file, not a folder.') % {"where": where}, status_code=status.HTTP_409_CONFLICT,
                          path=where)
    failed: list[dict[str, Any]] = []

    def fail(rel, code, detail):
        failed.append({"path": _relative_or_none(access, targets[rel]), "code": code, "detail": detail})

    # What exists: below a folder that is missing (or can't be made) nothing is looked up.
    state: dict[str, str] = {}  # rel -> exists | missing | blocked
    for rel in ordered:
        parent = rel.rpartition("/")[0]
        if parent and state.get(parent) in ("missing", "blocked"):
            state[rel] = state[parent]
            if state[rel] == "blocked":
                fail(rel, "parent_failed", _("The parent folder wasn't created."))
            continue
        entry = _stat(access, targets[rel])
        state[rel] = "missing" if entry is None else "exists" if entry.is_dir else "blocked"
        if state[rel] == "blocked":
            fail(rel, "file_exists", _("There is already a file with that name there."))
    to_make = [rel for rel in ordered if state[rel] == "missing"]
    for rel in to_make:
        check_new_name(driver, paths.name(targets[rel]))
    created, made, broken = [], [], set()
    for rel in to_make:
        if rel.rpartition("/")[0] in broken:
            broken.add(rel)
            fail(rel, "parent_failed", _("The parent folder wasn't created."))
            continue
        try:
            driver.mkdir(targets[rel])
        except AlreadyExists:
            entry = _stat(access, targets[rel])
            if entry is None or not entry.is_dir:
                broken.add(rel)
                fail(rel, "file_exists", _("There is already a file with that name there."))
            continue  # made meanwhile by somebody else: fine
        except DriverError as error:
            broken.add(rel)
            mapped = driver_error(error, staff=staff_of(access))
            fail(rel, mapped.code, mapped.message)
            continue
        created.append(_relative_or_none(access, targets[rel]))
        made.append(targets[rel])
    _changed(access.storage, made)
    activity.record_many(activity.Kind.MKDIR, [{"path": path, "detail": {"upload": True}} for path in made], quiet=True,
                         actor=access.actor.user, storage=access.storage)
    return {
        "created": created,
        "existing": [_relative_or_none(access, targets[rel]) for rel in ordered if state[rel] == "exists"],
        "failed": failed,
    }


# --- saving text ---------------------------------------------------------------------------------------

def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if timezone.is_naive(moment):
        moment = timezone.make_aware(moment, timezone.get_default_timezone())
    return moment


def unchanged(entry: Entry, expected: dict[str, Any]) -> bool:
    """Is the file still what the editor opened? Times compare to the second (listings may not give
    more), sizes exactly; what either side doesn't know isn't compared."""
    size = expected.get("size")
    if size is not None and entry.size is not None and int(size) != entry.size:
        return False
    when = expected.get("modified")
    if when and entry.modified is not None:
        moment = _parse_time(when)
        if moment is None:
            raise UploadError("bad_expected", _("expected.modified should be a time in ISO 8601 format."))
        if int(moment.timestamp()) != int(entry.modified.timestamp()):
            return False
    return True


def save_text(request, *, storage: str, path: str, content: str, encoding: str = "utf-8",
              expected: dict[str, Any] | None = None, newline: str | None = None,
              password: str | None = None) -> dict[str, Any]:
    """The text editor's save: writes the file, replacing it on purpose (上传 and 删除, D17a)."""
    codec = text.lookup(encoding)
    if codec is None:
        raise UploadError("encoding", _("This encoding isn't supported: %(encoding)s.") % {"encoding": encoding})
    eol = None
    if newline:
        eol = text.NEWLINES.get(newline.lower())
        if eol is None:
            raise UploadError("newline", _("newline can only be lf, crlf or cr."))
    too_large = UploadError("too_large", _("The file is too large: the online editor saves at most %(limit)s MB.") % {"limit": TEXT_MAX // MiB},
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, limit=TEXT_MAX)
    if len(content) > TEXT_MAX:
        raise too_large
    problem = text.nul_problem(content)
    if problem:
        raise UploadError("null_character", problem)
    try:
        data = text.encode(content, codec, eol)
    except text.EncodeProblem as error:
        raise UploadError("unencodable", str(error)) from None
    if len(data) > TEXT_MAX:
        raise too_large
    access = policy.open_storage(request, storage)
    target = access.path(path)
    if target == access.root:
        raise UploadError("invalid_path", _("Say which file to save to."))
    access.check(UPLOAD, target, password=password)
    driver = access.driver
    entry = _stat(access, target)
    if entry is not None and entry.is_dir:
        raise UploadError("folder_exists", _("This is a folder; it can't be saved as text."), status_code=status.HTTP_409_CONFLICT)
    if entry is None:
        if expected:
            raise UploadError("changed", _("The file was deleted or moved after you opened it: copy your changes somewhere first, then refresh and save again."),
                              status_code=status.HTTP_409_CONFLICT, current=None)
        check_new_name(driver, paths.name(target))
        parent = _stat(access, paths.parent(target))
        if parent is None or not parent.is_dir:
            raise UploadError("not_found", _("The folder to save to doesn't exist."), status_code=status.HTTP_404_NOT_FOUND)
    else:
        overwrite_allowed(access, target, password=password)
        if expected and not unchanged(entry, expected):
            raise UploadError("changed", _("The file was changed after you opened it. So as not to overwrite those changes, it wasn't saved: copy your changes somewhere first, then refresh and edit again."),
                              status_code=status.HTTP_409_CONFLICT,
                              current={"size": entry.size, "modified": iso(entry.modified)})
    try:
        driver.write(target, io.BytesIO(data), len(data))
    except DriverError as error:
        raise driver_error(error, staff=staff_of(access)) from None
    item = item_of(access, target, _stat_after_write(driver, target))
    if item["size"] is None:
        item["size"] = len(data)
    item["encoding"] = codec.name
    _changed(access.storage, [target])
    activity.record(activity.Kind.EDIT, quiet=True, actor=access.actor.user, storage=access.storage, path=target,
                    detail={"size": len(data), **({"new": True} if entry is None else {})})
    return item
