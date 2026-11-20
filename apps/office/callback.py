"""The Document Server's callback, POST /onlyoffice/callback/ (inventory 7.3). It is anonymous and it
writes files, so it rests on the JWT, checked before anything else; every failure answers {"error": 1}:

1. POST, a JSON object of at most BODY_MAX bytes;
2. an OnlyOffice secret is set, and a token is there: the body's "token", else Authorization: Bearer;
3. HS256 with that secret and an expiry (apps/office/tokens.py); the signed fields (top level, or in
   "payload") hold the document key and an integer status, and the URL for 2 and 6; every body field
   that is signed too must be the same. An editor config's token is no callback token.

Then the status (OnlyOffice's meaning, and what the site does):

    1  somebody opened the document for editing   nothing (the session notes it is in use, lives on)
    4  closed without changes                     nothing (the session notes it isn't)
    2  ready for saving (everybody closed it)     the key is dropped, then the document is saved; the
                                                  file opens to nobody until that is done
    3  saving failed                              the key is dropped
    6  force-save (the editor's 保存)              saved, the key stays: editing goes on
    7  force-save failed                          the key stays

Saving: the session must allow editing; its user is authorised again through apps.storages.access, as a
request of theirs would be (the storage enabled, the user active, the grant enabled, 上传 and 删除
(D17a), the file still inside their root, the filter rules, the folder passwords they had unlocked);
the document is downloaded from the OnlyOffice server only (apps/office/fetch.py) into a temporary file and
must look like a document of the file's type; the session and the user are checked once more, the
storage must still have the params the document was opened with (and the write goes to the driver
instance made from them), the file must still be there, and the driver's write() replaces it from that
file. The activity log gets an entry as the session's user did it, without an IP (the request is the
Document Server's).

One save of a file at a time: a lease on its sessions' rows (saving_until, taken in a short transaction
that locks them, renewed by a heartbeat thread while the save runs, checked before the write and let go
only while it is still this save's), not a transaction held open over the download and the write, which
on SQLite (transaction_mode IMMEDIATE) would hold up every other write of the site. A second save waits
up to WAIT seconds. Then apps.files.signals.changed.

The log never has a token, the document's URL (only its host: the Document Server's links are
credentials of their own) nor its content.
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from datetime import timedelta

from django.db import OperationalError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext

from apps.activity import log as activity
from apps.files import kinds
from apps.files import signals as file_signals
from apps.siteconf.models import SiteSettings
from apps.storages import access as policy
from apps.storages import instances
from apps.storages.access import UPLOAD, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError, NotFound
from apps.storages.instances import StorageUnavailable
from apps.uploads.service import overwrite_allowed

from . import fetch, sessions, tokens
from .models import OfficeSession

logger = logging.getLogger("hfile.office")

BODY_MAX = 1024 * 1024  # bytes of a callback's JSON
LEASE = timedelta(minutes=5)  # a save holds its file this long, renewed every HEARTBEAT while it runs
HEARTBEAT = 60.0  # seconds
WAIT = 60.0  # seconds a save waits for another save of the same file
POLL = 0.25
SPOOL_MEMORY = 4 * 1024 * 1024  # the downloaded document stays in memory up to this, then on disk
# What a saved document of these types starts with: a ZIP (Office Open XML, OpenDocument), RTF. A Document
# Server answering with an empty body or a proxy's error page doesn't replace the file.
ZIPPED = frozenset("docx docm dotx dotm xlsx xlsm xltx xltm pptx pptm potx potm ppsx ppsm odt ott ods ots odp otp"
                   .split())

_BUSY = object()


def _refused(reason: str, callback=None) -> int:
    if callback is None:
        logger.warning("OnlyOffice callback refused: %s", reason)
    else:
        logger.warning("OnlyOffice callback refused: %s (key %s, status %s)", reason, callback.key, callback.status)
    return 1


def _body(request) -> dict | None:
    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        return None
    if length <= 0 or length > BODY_MAX:
        return None
    raw = request.read(BODY_MAX + 1)
    if len(raw) > BODY_MAX:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return data if isinstance(data, dict) else None


def _bearer(request) -> str | None:
    scheme, _, value = request.META.get("HTTP_AUTHORIZATION", "").partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def handle(request) -> int:
    """The answer's "error": 0 when done, 1 when refused or failed."""
    secret = tokens.secret()
    if not secret:
        return _refused("no OnlyOffice secret is set")
    body = _body(request)
    if body is None:
        return _refused("not a JSON object of at most 1 MB")
    token = body.get("token")
    if not isinstance(token, str) or not token:
        token = _bearer(request)
    if not token:
        return _refused("no token")
    callback = tokens.callback_claims(token, secret)
    if callback is None:
        return _refused("the token isn't a valid callback token")
    for name, value in body.items():
        if name != "token" and name in callback.signed and callback.signed[name] != value:
            return _refused(f"the body's {name} isn't the signed one", callback)
    return act(callback)


def act(callback: tokens.Callback) -> int:
    status = callback.status
    if status in (1, 4):
        sessions.note_in_use(callback.key, status == 1)
        return 0
    if status not in (2, 3, 6, 7):
        return 0
    session = sessions.by_key(callback.key)
    if session is None:
        return _refused("no such document key (dropped, or expired)", callback)
    if status == 3:
        sessions.drop(session)
        return 0
    if status == 7:
        return 0
    return 0 if save(session, callback) else 1


def save(session: OfficeSession, callback: tokens.Callback) -> bool:
    final = callback.status == 2
    if not session.can_edit:
        # A viewing session: nobody edits through it, nothing is saved (and its key isn't the editors').
        _refused("the session may not edit", callback)
        if final:
            sessions.drop(session)
        return False
    if final:
        sessions.detach(session)  # whatever happens below, the next open gets a new key
    try:
        extension = kinds.extension(paths.name(session.path))
        if callback.filetype is not None and callback.filetype.strip(".").lower() != extension:
            _refused(f"the document came back as {callback.filetype[:20]!r}, not {extension!r}", callback)
            return False
        lease = take_lease(session.pk)
        if lease is None:
            _refused("the session went away (the file was renamed, moved or deleted)", callback)
            return False
        if lease is _BUSY:
            _refused("another save of the file took too long", callback)
            return False
        try:
            with _Heartbeat(lease):
                return _write(lease, callback)
        finally:
            lease.release()
    finally:
        if final:
            sessions.drop(session)


def _reload(pk) -> OfficeSession | None:
    return OfficeSession.objects.select_related("storage", "user").filter(pk=pk).first()


class Lease:
    """A save's hold on its session (saving_until = until): renewed, checked and let go only while it is
    still this one (another save may have taken over a lease that ran out)."""

    def __init__(self, session: OfficeSession, until):
        self.session = session
        self.until = until

    def _mine(self):
        return OfficeSession.objects.filter(pk=self.session.pk, saving_until=self.until)

    def held(self) -> bool:
        return self._mine().exists()

    def renew(self) -> bool:
        until = timezone.now() + LEASE
        if self._mine().update(saving_until=until):
            self.until = until
            return True
        return False

    def release(self) -> None:
        self._mine().update(saving_until=None)


class _Heartbeat:
    """Renews a lease every HEARTBEAT seconds while a save runs (a write to a slow storage may take
    longer than LEASE), from a thread with a database connection of its own."""

    def __init__(self, lease: Lease):
        self.lease = lease
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="office-save-heartbeat", daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join(timeout=10)

    def _run(self):
        try:
            while not self.stop.wait(HEARTBEAT):
                try:
                    if not self.lease.renew():
                        return
                except Exception:  # noqa: BLE001 - the save checks its lease before writing
                    logger.warning("OnlyOffice: renewing the save's lease failed", exc_info=True)
        finally:
            connection.close()


def _try_lease(pk):
    """A Lease of the session for this save; None when it is gone; _BUSY while a save of the same file
    (through this session or another one of the file) holds it."""
    row = OfficeSession.objects.filter(pk=pk).values("storage_id", "file").first()
    if row is None:
        return None
    now = timezone.now()
    try:
        with transaction.atomic():
            # One lock per save: the rows of the file (this session's among them), in index order.
            rows = list(OfficeSession.objects.select_for_update()
                        .filter(storage_id=row["storage_id"], file=row["file"])
                        .values_list("pk", "saving_until"))
            if all(found != pk for found, _until in rows):
                return None
            if any(until is not None and until > now for _found, until in rows):
                return _BUSY
            until = now + LEASE
            OfficeSession.objects.filter(pk=pk).update(saving_until=until)
    except OperationalError:
        return _BUSY  # the database is busy (SQLite) or chose this one as a deadlock's victim (MySQL)
    session = _reload(pk)
    return Lease(session, until) if session is not None else None


def take_lease(pk):
    deadline = time.monotonic() + WAIT
    while True:
        outcome = _try_lease(pk)
        if outcome is not _BUSY or time.monotonic() >= deadline:
            return outcome
        time.sleep(POLL)


def authorise(session: OfficeSession) -> policy.Access:
    """The session's user may still replace the file, as a request of theirs would be checked."""
    access = policy.open_storage(None, session.storage, actor=policy.Actor(session.user))
    if not access.contains(session.path):
        raise AccessDenied(policy.OUTSIDE_ROOT, gettext("The file is no longer within what this user can access."))
    access.check(UPLOAD, session.path, is_dir=False, keys=session.keys or ())
    overwrite_allowed(access, session.path, keys=session.keys or ())
    return access


def _spool_dir() -> str | None:
    """The upload spool (next to config.toml, mode 700), else the system's temporary folder."""
    try:
        from apps.uploads import spool

        return str(spool.folder())
    except Exception:  # noqa: BLE001 - the system's temporary folder will do
        return None


def _same_storage(session: OfficeSession, driver) -> bool:
    """The storage still has the params the session's file was opened with, and `driver` is the live
    instance made from them (the write then goes there, even if an admin saves new params meanwhile)."""
    if session.storage.config_revision != session.storage_revision:
        return False
    handle = instances.live(session.storage_id)
    return handle is not None and handle.driver is driver and handle.revision == session.storage_revision


def plausible(extension: str, head: bytes) -> bool:
    """Whether a saved document's first bytes fit its type (ZIP for OOXML and OpenDocument, RTF); text and
    CSV may be anything, empty too."""
    if extension in ZIPPED:
        return head.startswith(b"PK\x03\x04")
    if extension == "rtf":
        return head.startswith(b"{\\rtf")
    return True


def _write(lease: Lease, callback: tokens.Callback) -> bool:
    session = lease.session
    site = SiteSettings.load()
    try:
        authorise(session)
        with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY, dir=_spool_dir()) as data:
            size = fetch.download(callback.url, site.onlyoffice_url, data)
            data.seek(0)
            if not plausible(kinds.extension(paths.name(session.path)), data.read(8)):
                _refused("what came back isn't a document of the file's type", callback)
                return False
            data.seek(0)
            # The download took a while: the file may have been renamed or deleted (its session went
            # with it), the user disabled or their permissions changed meanwhile.
            fresh = _reload(session.pk)
            if fresh is None:
                _refused("the session went away during the download", callback)
                return False
            access = authorise(fresh)
            driver = access.driver
            if not _same_storage(fresh, driver):
                # New params (another root, bucket, base path): the path names another file now.
                _refused("the storage's params changed since the document was opened", callback)
                return False
            try:
                entry = driver.stat(fresh.path)
            except NotFound:
                # Deleted or moved outside the site: saving would bring it back (and its folders).
                _refused("the file isn't there any more", callback)
                return False
            if entry.is_dir:
                _refused("a folder is there now", callback)
                return False
            if not lease.held():
                # The lease ran out (the database was away longer than LEASE) and another save may be on it.
                _refused("the save lost its hold on the file", callback)
                return False
            driver.write(fresh.path, data, size)
    except AccessDenied as error:
        _refused(f"the user may not save it ({error.code})", callback)
        return False
    except fetch.FetchError as error:
        _refused(f"downloading from {fetch.host_of(callback.url) or '?'} failed: {error.reason}", callback)
        return False
    except DriverError as error:
        _refused(f"the storage failed: {type(error).__name__} {error.message}", callback)
        return False
    except StorageUnavailable as error:
        _refused(f"the storage isn't available: {error.message}", callback)
        return False
    except OSError as error:
        _refused(f"the temporary file failed: {error.strerror or type(error).__name__}", callback)
        return False
    try:
        written = driver.stat(fresh.path)
    except DriverError:
        written = None
    if callback.status == 6:
        sessions.saved(fresh, written)
    logger.info("OnlyOffice saved %s:%s (%d bytes, status %d)", fresh.storage.key, fresh.path, size, callback.status)
    activity.record(activity.Kind.EDIT, quiet=True, actor=fresh.user, storage=fresh.storage, path=fresh.path,
                    detail={"size": size, "via": "onlyoffice"}, ip=None, user_agent="")
    for _receiver, outcome in file_signals.changed.send_robust(sender=OfficeSession, source=fresh.storage,
                                                                 paths=[fresh.path]):
        if isinstance(outcome, Exception):
            logger.error("A receiver of files.signals.changed failed: %r", outcome, exc_info=outcome)
    return True
