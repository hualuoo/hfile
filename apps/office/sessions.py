"""Editor sessions: one document key per physical file for its editors, one for its viewers (apps.office.models
has why).

    session = open_session(storage, path, user=..., can_edit=..., keys=..., entry=...)
    session = by_key(key)                       # the callback's, None when unknown or expired
    forget(storage, paths)                      # the keys at and below these storage paths go

open_session() joins the file's current session or makes one. Openers who may edit share the editing
session (co-editing); each of them becomes the identity that saves (the last authorised editor saves,
inventory 7.3). Openers who may only view (users who may not edit, visitors, shares) share another key, the
viewing session: it never saves, and they see the file as it was last saved, not the editors' names nor
their changes not saved yet. Every save through the site ends the viewing key (forget_changed), so the
next viewer gets the new version. A file whose size or time differs from what a session saw (while
nobody has it open for editing) gets a new key: the Document Server would otherwise show its cached copy
of the old content, and a save from that copy would undo the change made outside the site.

The keys at and below a changed path go when apps.files.signals.changed says so (forget_changed): renames,
moves, deletes, uploads and text saves. Paths match by segments ("/a" is not "/ab"), compared exactly.
All the keys of a storage go when an admin saves new params for it (another root, bucket or base path:
the same path is another file then) or saves it disabled (storage_saved); deleting it deletes them with
it (CASCADE). A session also remembers the storage's config_revision, which the open and the save
compare: the storages app may change a row without a signal (QuerySet.update).
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import string
from collections.abc import Iterable
from datetime import datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext
from rest_framework import status
from rest_framework.exceptions import APIException, ErrorDetail

from apps.storages.drivers import paths

from .models import OfficeSession

logger = logging.getLogger("hfile.office")

# A session lasts this long after the last open (and the last save through it): the Document Server
# calls back when its users close the document, which may be hours later.
LIFETIME = timedelta(days=7)
# The final save (status 2) of a key keeps the file closed to new openers until it is done: this long at most
# when its process dies before; while it writes, its lease (renewed by a heartbeat) keeps it closed too.
CLOSING = timedelta(minutes=30)
KEY_ALPHABET = string.ascii_lowercase + string.digits  # one case: MySQL's collation ignores it
KEY_LENGTH = 24  # 124 bits; the Document Server takes up to 128 characters
DELETE_BATCH = 500
MATCH_BATCH = 100  # changed paths per query (SQLite parses a long OR as a deep expression)


class OfficeError(APIException):
    """A refusal of the OnlyOffice API: {"detail", "code"} with a stable code (D38)."""

    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message, code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.detail = {"detail": ErrorDetail(message, code), "code": code}

    def __str__(self):
        return self.message


def new_key() -> str:
    return "".join(secrets.choice(KEY_ALPHABET) for _ in range(KEY_LENGTH))


def is_key(value) -> bool:
    return (isinstance(value, str) and len(value) == KEY_LENGTH
            and all(char in KEY_ALPHABET for char in value))


def file_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8", "surrogatepass")).hexdigest()


def slot(path: str, editing: bool) -> str:
    """What `current` holds for the file's current editing session (file_hash) or viewing session."""
    if editing:
        return file_hash(path)
    return hashlib.sha256(b"view\0" + path.encode("utf-8", "surrogatepass")).hexdigest()


def version_of(entry) -> tuple[int | None, int | None]:
    """(bytes, time in whole seconds) of a driver's Entry; None for what the storage didn't say."""
    if entry is None:
        return None, None
    modified = getattr(entry, "modified", None)
    return getattr(entry, "size", None), (int(modified.timestamp()) if modified is not None else None)


def _not_saving(now: datetime) -> Q:
    return Q(saving_until__isnull=True) | Q(saving_until__lte=now)


def _saving(session: OfficeSession, now: datetime) -> bool:
    return session.saving_until is not None and session.saving_until > now


def _differs(session: OfficeSession, size: int | None, seconds: int | None) -> bool:
    """The file isn't what the session saw (only what both sides know is compared)."""
    if size is not None and session.file_size is not None and size != session.file_size:
        return True
    return seconds is not None and session.file_time is not None and seconds != session.file_time


def current(storage, path: str, *, editing: bool = True) -> OfficeSession | None:
    """The file's current editing (or viewing) session, expired or not."""
    found = OfficeSession.objects.filter(storage=storage, current=slot(path, editing)).first()
    return found if found is not None and found.path == path else None


def by_key(key: str) -> OfficeSession | None:
    """The unexpired session of a document key, with its storage and user; None when there is none."""
    if not is_key(key):
        return None
    session = (OfficeSession.objects.select_related("storage", "user")
               .filter(key=key, expires_at__gt=timezone.now()).first())
    return session if session is not None and session.key == key else None


def open_session(storage, path: str, *, user, can_edit: bool, keys: Iterable = (), entry=None) -> OfficeSession:
    """The session a config hands out for the file at `path` (a storage path), as described above.
    `keys`: the folder passwords the opener had unlocked for it (access.unlocked_keys()); `entry`: the
    file's stat, for its version."""
    keys = [list(pair) for pair in keys] if can_edit else []
    size, seconds = version_of(entry)
    for _attempt in range(5):
        now = timezone.now()
        session = current(storage, path, editing=can_edit)
        if session is not None and session.storage_revision != storage.config_revision:
            # The storage has new params since: the session's file is another one now.
            OfficeSession.objects.filter(pk=session.pk).delete()
            session = None
        if session is not None and not _saving(session, now):
            expired = session.expires_at <= now
            # Nobody edits through a viewing session: its key changes with the file.
            changed = (not session.in_use or not can_edit) and _differs(session, size, seconds)
            if expired or changed:
                # Gone stale: a new key below. Only if nothing changed it meanwhile (an editor came in).
                gone = OfficeSession.objects.filter(_not_saving(now), pk=session.pk, in_use=session.in_use,
                                                    expires_at=session.expires_at).delete()[0]
                if not gone:
                    continue
                logger.info("OnlyOffice: a new key for %s:%s (%s)", storage.key, path,
                            "the old one expired" if expired else "the file changed outside the site")
                session = None
        if session is None:
            if can_edit and _closing(storage, path, now):
                # The Document Server's final save of the file's last key is under way: a key handed out now
                # would load the content from before it, and its saves would undo it.
                raise OfficeError("closing", gettext("This file was just edited and is being saved. Please open it again in a moment."),
                                  status_code=status.HTTP_409_CONFLICT)
            try:
                with transaction.atomic():
                    return OfficeSession.objects.create(
                        key=new_key(), storage=storage, storage_revision=storage.config_revision, path=path,
                        file=file_hash(path), current=slot(path, can_edit), user=user, can_edit=can_edit,
                        keys=keys, file_size=size, file_time=seconds, expires_at=now + LIFETIME)
            except IntegrityError:
                continue  # another request made the file's session at the same moment: join it
        updates = {"expires_at": max(session.expires_at, now + LIFETIME)}
        if can_edit:
            updates.update(user=user, keys=keys)
        if session.file_size is None and session.file_time is None and (size, seconds) != (None, None):
            updates.update(file_size=size, file_time=seconds)
        if not OfficeSession.objects.filter(pk=session.pk).update(**updates):
            continue  # gone meanwhile (the file changed, the final save): a key without a row would never save
        for name, value in updates.items():
            setattr(session, name, value)
        return session
    raise OfficeError("busy", gettext("Someone else has this file open. Please try again later."), status_code=status.HTTP_409_CONFLICT)


def note_in_use(key: str, in_use: bool) -> None:
    """Callback status 1 (somebody opened the document for editing: the session lives on) and 4 (closed
    without changes)."""
    if not is_key(key):
        return
    sessions = OfficeSession.objects.filter(key=key)
    if in_use:
        sessions.filter(expires_at__gt=timezone.now()).update(in_use=True, expires_at=timezone.now() + LIFETIME)
    else:
        sessions.update(in_use=False)


def saved(session: OfficeSession, entry) -> None:
    """After a force-save (status 6): the file is what the session holds now; it stays open."""
    size, seconds = version_of(entry)
    OfficeSession.objects.filter(pk=session.pk).update(
        file_size=size, file_time=seconds, expires_at=max(session.expires_at, timezone.now() + LIFETIME))


def detach(session: OfficeSession) -> None:
    """The final save (status 2) drops the key: the row stays until the save is done (it holds the save's
    lease), and until then (CLOSING at most, should the process die) the file opens to nobody (_closing())."""
    OfficeSession.objects.filter(pk=session.pk).update(current=None, expires_at=timezone.now() + CLOSING)


def _closing(storage, path: str, now: datetime) -> bool:
    """A final save of the file is under way: a detached session of it hasn't ended yet (or is still
    being written: its lease is renewed while it writes)."""
    rows = (OfficeSession.objects.filter(storage=storage, file=file_hash(path), current__isnull=True)
            .filter(Q(expires_at__gt=now) | Q(saving_until__gt=now)).values_list("path", flat=True))
    return any(found == path for found in rows)


def drop(session_or_key) -> int:
    """The session goes (status 2 once saved, status 3)."""
    if isinstance(session_or_key, OfficeSession):
        return OfficeSession.objects.filter(pk=session_or_key.pk).delete()[0]
    if not is_key(session_or_key):
        return 0
    return OfficeSession.objects.filter(key=session_or_key).delete()[0]


def _self_and_above(path: str):
    while path != paths.ROOT:
        yield path
        path = paths.parent(path)


def forget(storage, changed: Iterable[str], *, viewing_only: bool = False) -> int:
    """Drops the sessions of `storage` at and below the storage paths `changed`: their files are gone,
    moved or have other content now (with viewing_only, only the viewing ones). Returns how many went."""
    targets = {path for path in changed if isinstance(path, str) and paths.is_clean(path)}
    if not targets:
        return 0
    sessions = OfficeSession.objects.filter(storage=storage)
    if viewing_only:
        sessions = sessions.filter(can_edit=False)
    if paths.ROOT in targets:
        return sessions.delete()[0]
    # The database narrows it down (LIKE 'path%': on MySQL without case and accents, so more than needed),
    # Python decides by exact segments. A changed path is often one of many (an upload, a batch delete).
    ordered = sorted(targets)
    found: set[int] = set()
    for start in range(0, len(ordered), MATCH_BATCH):
        near = Q()
        for target in ordered[start:start + MATCH_BATCH]:
            near |= Q(path__startswith=target)
        found.update(pk for pk, path in sessions.filter(near).values_list("pk", "path")
                     if any(part in targets for part in _self_and_above(path)))
    doomed = sorted(found)
    removed = 0
    for start in range(0, len(doomed), DELETE_BATCH):
        removed += OfficeSession.objects.filter(pk__in=doomed[start:start + DELETE_BATCH]).delete()[0]
    if removed:
        logger.info("OnlyOffice: %d document key(s) of %s dropped (the files changed)", removed, storage.key)
    return removed


def forget_changed(sender, source=None, paths=(), **kwargs) -> None:
    """The receiver of apps.files.signals.changed (W5). The callback's own saves (sender OfficeSession) keep
    the editing session (a force-save goes on editing, the final save ends its session itself) and end
    the viewing one: its viewers' next open gets the version just saved."""
    if source is None:
        return
    forget(source, paths, viewing_only=sender is OfficeSession)


def storage_saved(sender, instance=None, created=False, **kwargs) -> None:
    """post_save of StorageSource: an admin saved new params (config_revision went up) or saved it
    disabled; the storage's sessions whose revision isn't the current one, or all of them, end. The row
    is read again: the saved instance may have been loaded before another admin's change."""
    if created or instance is None or instance.pk is None:
        return
    row = type(instance).objects.filter(pk=instance.pk).values("config_revision", "enabled").first()
    sessions = OfficeSession.objects.filter(storage_id=instance.pk)
    if row is not None and row["enabled"]:
        sessions = sessions.exclude(storage_revision=row["config_revision"])
    ended = sessions.delete()[0]
    if ended:
        logger.info("OnlyOffice: %d document key(s) of storage %s ended (its params changed or it was disabled)",
                    ended, instance.pk)


def remove_expired(now: datetime) -> int:
    """`hfile cleanup` (apps.core.cleanup): expired sessions, except one that is being saved."""
    return OfficeSession.objects.filter(_not_saving(now), expires_at__lt=now).delete()[0]
