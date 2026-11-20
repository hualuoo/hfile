"""Driver instances: one live instance per storage source, and the short-lived ones of the admin form.

The live instance of a storage serves every request (drivers are thread-safe). It is made the first
time a request needs it, from the row as it is in the database then, and lives until an admin saves new
params (StorageSource.config_revision goes up), disables or deletes the storage. The instance it gives
way to is closed a minute later, so that requests still using it can finish. A disabled storage has no
instance.

A driver changes its own config now and then (update_config: a refreshed OAuth token; 115 gives a new
refresh token and kills the old one; SFTP remembers the server's host key the first time). Where such
a change goes depends on the instance (Handle.role):

- LIVE: saved at once (secrets encrypted) without rebuilding anything, and only over what the instance
  knew. If an admin saved new params since the instance was made, a key the admin changed keeps the
  admin's value. A change that comes from an older instance is passed on to the current one, which
  would otherwise go on with a token that no longer works.
- CANDIDATE: an instance tested before an admin's save is committed. Its changes go into the saved
  config; after the commit it becomes the live one.
- TEMPORARY: 测试连接 and the like. They save nothing on their own: afterwards apply_temporary() saves
  a changed secret only where it replaces the stored one (the stored token was used and is spent), and
  hands the rest back to the form.

The open instances of a storage (the live one, an older one finishing its requests, a candidate, a
temporary one) share one lock for their tokens (StorageDriver.token_lock), and new tokens (the secrets a
driver changed) go to each of the others at once, all together, if it still has every value they replace:
it would otherwise refresh with a spent refresh token (Baidu's and 115's work once). What isn't secret
(SFTP's host key) stays with its instance, as above.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Mapping
from typing import Any

from django.db import connection, transaction
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy

from . import config as storage_config
from .drivers.base import DriverError, StorageDriver

logger = logging.getLogger("hfile.storages")

LIVE = "live"
CANDIDATE = "candidate"
TEMPORARY = "temporary"

# Seconds a replaced instance stays open for the requests still using it.
GRACE = 60.0


class StorageUnavailable(Exception):
    """The storage can't be used now: disabled, deleted, or its driver can't start with its settings."""

    def __init__(self, message: str = gettext_lazy("This storage source is disabled.")):
        super().__init__(message)
        self.message = message


class Handle:
    """A driver instance with what it knows of its own config."""

    def __init__(self, storage_id: int | None, revision: int, type_: str, config: Mapping[str, Any], role: str):
        self.storage_id = storage_id
        self.revision = revision
        self.type = type_
        self.cls = storage_config.driver(type_)
        self.role = role
        self._params = storage_config.params(self.cls)
        self._lock = threading.Lock()
        self.known = dict(config)  # the config as this instance has it, its own changes included
        self.changes: dict[str, Any] = {}  # not stored yet (CANDIDATE, TEMPORARY)
        self.before: dict[str, Any] = {}  # the value each of those had when the instance got it
        self.unsaved: dict[str, Any] = {}  # LIVE: changes made inside a transaction, saved after it
        self.unsaved_before: dict[str, Any] = {}
        self.closed = False
        self._peers: _Peers | None = None  # the storage's other instances, once this one is made (_join)
        self.driver: StorageDriver = self.cls(dict(config), on_update=self._on_update)
        if storage_id is not None:
            self._peers = _join(self)

    def __repr__(self):
        return f"<Handle {self.type} storage={self.storage_id} r{self.revision} {self.role}>"

    def _on_update(self, changes: Mapping[str, Any]) -> None:
        changes = {key: value for key, value in dict(changes).items() if key in self._params}
        if not changes:
            return
        with self._lock:
            before = {key: self.known.get(key) for key in changes}
            self.known.update(changes)
            live = self.role == LIVE
            if not live:
                for key, value in changes.items():
                    self.before.setdefault(key, before[key])
                    self.changes[key] = value
        tokens = {key: value for key, value in changes.items() if self._params[key].secret}
        if tokens:
            _hand_on(self, tokens, before)
        if live:
            save_changes(self, changes, before)

    def take_changes(self) -> tuple[dict[str, Any], dict[str, Any]]:
        """The changes not stored yet, and the values they replaced; forgets them."""
        with self._lock:
            changes, self.changes = self.changes, {}
            before = {key: self.before.get(key) for key in changes}
            self.before = {}
            return changes, before

    def put_back(self, changes: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        """Undo take_changes() when what they were taken for didn't happen (the save failed)."""
        with self._lock:
            self.changes = {**changes, **self.changes}
            self.before = {**self.before, **{key: before.get(key) for key in changes}}

    def adopt(self, changes: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        """Take changes that another instance of the same storage made and saved, where this one still
        has the value they replaced. Under the driver's token lock, so that it never reads half of them."""
        with self.driver.token_lock, self._lock:
            for key, value in changes.items():
                if self.known.get(key) == before.get(key):
                    self.known[key] = value
                    self.driver.config[key] = value

    def take_tokens(self, tokens: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        """Tokens another instance of the storage has just got: taken, all at once, when this one still has
        every value they replace. Not when it has another one of them (a token typed into the form), which
        they don't go with."""
        with self.driver.token_lock, self._lock:
            if self.closed or any(self.known.get(key) != before.get(key) for key in tokens):
                return
            self.known.update(tokens)
            self.driver.config.update(tokens)

    def defer(self, changes: Mapping[str, Any], before: Mapping[str, Any]) -> None:
        with self._lock:
            for key, value in changes.items():
                self.unsaved_before.setdefault(key, before.get(key))
                self.unsaved[key] = value

    def save_unsaved(self) -> None:
        """Saves what was changed inside a transaction, once outside of one (also after a rollback)."""
        with self._lock:
            if not self.unsaved or not _outside_transactions():
                return
            unsaved, self.unsaved = self.unsaved, {}
            before, self.unsaved_before = self.unsaved_before, {}
            # Only what the instance still has: a later change (saved, or waiting itself) replaced the rest.
            changes = {key: value for key, value in unsaved.items() if self.known.get(key) == value}
        save_changes(self, changes, before)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        if self._peers is not None:
            with _registry_lock:
                self._peers.handles.discard(self)
        try:
            self.driver.close()
        except Exception:  # noqa: BLE001 - closing is best effort
            logger.warning("Closing the %s driver of storage %s failed", self.type, self.storage_id, exc_info=True)


def _outside_transactions() -> bool:
    """No atomic block is open, apart from those of a TestCase (Django marks them; it does the same
    for atomic(durable=True))."""
    return all(getattr(block, "_from_testcase", False) for block in connection.atomic_blocks)


# A storage's locks are kept only while in use: once kept for good, deleted storages' too (OPS-08).
_save_locks: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()
_registry_lock = threading.Lock()


def _save_lock(storage_id: int) -> threading.Lock:
    with _registry_lock:
        return _save_locks.setdefault(storage_id, threading.Lock())


class _Peers:
    """The open instances of one storage, and the lock of their tokens."""

    def __init__(self):
        self.token_lock = threading.RLock()
        self.handles: weakref.WeakSet[Handle] = weakref.WeakSet()


_peers: weakref.WeakValueDictionary[int, _Peers] = weakref.WeakValueDictionary()  # kept while an instance is


def _join(handle: Handle) -> _Peers:
    with _registry_lock:
        peers = _peers.get(handle.storage_id)
        if peers is None:
            peers = _peers[handle.storage_id] = _Peers()
        handle.driver.token_lock = peers.token_lock
        peers.handles.add(handle)
    return peers


def _hand_on(handle: Handle, tokens: Mapping[str, Any], before: Mapping[str, Any]) -> None:
    """Tokens the driver of `handle` has just got, to the other open instances of its storage."""
    if handle._peers is None:
        return
    with _registry_lock:
        others = [other for other in handle._peers.handles if other is not handle]
    for other in others:
        other.take_tokens(tokens, before)


def save_changes(handle: Handle, changes: Mapping[str, Any], before: Mapping[str, Any], *,
                 only_replacing: bool = False) -> dict[str, Any]:
    """Store changes a driver made, where they replace what the instance had; returns what was stored.

    A key is stored when the instance is still the storage's current revision, or when the stored value
    is the one the change replaced (an admin's save kept it). With `only_replacing` the second rule
    alone applies (temporary instances). Inside an open transaction the save waits for its end: a
    rollback must not lose a token that the backend has already replaced.
    """
    from .models import StorageSource

    if handle.storage_id is None or not changes:
        return {}
    if not _outside_transactions():
        handle.defer(changes, before)
        transaction.on_commit(handle.save_unsaved)
        return {}
    applied: dict[str, Any] = {}
    with _save_lock(handle.storage_id), transaction.atomic():
        row = StorageSource.objects.select_for_update().filter(pk=handle.storage_id).first()
        if row is None or row.type != handle.type:
            return {}
        params = storage_config.params(handle.cls)
        stored = dict(row.config or {})
        current = row.config_revision == handle.revision and not only_replacing
        for key, value in changes.items():
            param = params.get(key)
            if param is None:
                continue
            if not current:
                try:
                    old = storage_config.decode_value(param, stored.get(key))
                except storage_config.core_secrets.UnreadableSecret:
                    old = None
                if old != before.get(key) and not (old is None and before.get(key) in (None, "")):
                    continue
            stored[key] = storage_config.encode_value(param, value)
            applied[key] = value
        if applied:
            StorageSource.objects.filter(pk=row.pk).update(config=stored, tokens_saved_at=timezone.now())
    if applied:
        logger.debug("Storage %s: saved the driver's own changes of %s", handle.storage_id, ", ".join(sorted(applied)))
        live = _live.get(handle.storage_id)
        if live is not None and live is not handle:
            live.adopt(applied, before)
    return applied


# --- the registry ------------------------------------------------------------------------------------

_live: dict[int, Handle] = {}
_building: weakref.WeakValueDictionary[int, threading.Lock] = weakref.WeakValueDictionary()  # as _save_locks
_status: dict[int, dict[str, Any]] = {}


def _build_lock(storage_id: int) -> threading.Lock:
    with _registry_lock:
        return _building.setdefault(storage_id, threading.Lock())


def _install(handle: Handle) -> None:
    with _registry_lock:
        old = _live.get(handle.storage_id)
        _live[handle.storage_id] = handle
    if old is not None and old is not handle:
        _retire(old)


def _retire(handle: Handle) -> None:
    if GRACE <= 0:
        handle.close()
        return
    timer = threading.Timer(GRACE, handle.close)
    timer.daemon = True
    timer.start()


def get(storage) -> StorageDriver:
    """The live driver of an enabled storage (a StorageSource row); StorageUnavailable otherwise.

    Made from the row as the database has it now, not from `storage`, which may be older than a
    token the driver saved meanwhile.
    """
    from .models import StorageSource

    if not storage.enabled:
        raise StorageUnavailable()
    handle = _live.get(storage.pk)
    if handle is not None and handle.revision >= storage.config_revision:
        handle.save_unsaved()
        return handle.driver
    with _build_lock(storage.pk):
        handle = _live.get(storage.pk)
        if handle is not None and handle.revision >= storage.config_revision:
            return handle.driver
        row = StorageSource.objects.filter(pk=storage.pk).first()
        if row is None:
            raise StorageUnavailable(_("This storage source no longer exists."))
        if not row.enabled:
            raise StorageUnavailable()
        try:
            cls = storage_config.driver(row.type)
        except storage_config.ConfigError as error:  # the type is gone, or its library (STOR-01)
            record(row.pk, False, _("; ").join(error.errors.values()))
            raise StorageUnavailable(_("This storage source can't be used now. Please ask the administrator to check its settings.")) from None
        plain, unreadable = storage_config.decode(cls, row.config)
        if unreadable:
            labels = _(", ").join(storage_config.params(cls)[key].label for key in unreadable)
            record(row.pk, False, _("The saved keys can't be read any more (the settings file's "
                                    'secret_key has changed): please fill in "%(labels)s" again.') % {"labels": labels})
        try:
            handle = Handle(row.pk, row.config_revision, row.type, plain, LIVE)
        except DriverError as error:
            record(row.pk, False, error.message)
            raise StorageUnavailable(error.message) from None
        except Exception:  # noqa: BLE001 - a driver's bug: the traceback to the log, the user a plain message
            logger.exception("The %s driver of storage %s can't start", row.type, row.pk)
            record(row.pk, False, _("The storage driver failed to start; the details are in the program's log."))
            raise StorageUnavailable(_("This storage source can't be used now. Please ask the administrator to check its settings.")) from None
        _install(handle)
        return handle.driver


def live(storage_id: int) -> Handle | None:
    return _live.get(storage_id)


def publish(handle: Handle) -> None:
    """A candidate, tested and committed, becomes the live instance; changes it made since its config
    was saved are saved now."""
    with handle._lock:
        handle.role = LIVE
        changes, handle.changes = handle.changes, {}
        before = {key: handle.before.get(key) for key in changes}
        handle.before = {}
    _install(handle)
    if changes:
        save_changes(handle, changes, before)
    _catch_up(handle)


def _catch_up(handle: Handle) -> None:
    """Give a new live instance what the database has that it doesn't: tokens the old instance refreshed
    while the candidate was being tested or saved (the save kept them), and any it saved since."""
    from .models import StorageSource

    with _save_lock(handle.storage_id):
        row = StorageSource.objects.filter(pk=handle.storage_id).only("type", "config", "config_revision").first()
    if row is None or row.type != handle.type or row.config_revision != handle.revision:
        return
    stored, _unreadable = storage_config.decode(handle.cls, row.config)
    with handle._lock:
        newer = {key: value for key, value in stored.items() if handle.known.get(key) != value}
        before = {key: handle.known.get(key) for key in newer}
    if newer:
        handle.adopt(newer, before)


def drop(storage_id: int) -> None:
    """The storage was disabled or deleted: no instance until it is enabled again. Waits for an instance
    being built (get()), which would otherwise be installed just after."""
    with _build_lock(storage_id), _registry_lock:
        old = _live.pop(storage_id, None)
    if old is not None:
        _retire(old)


def candidate(storage_id: int | None, revision: int, type_: str, config: Mapping[str, Any]) -> Handle:
    return Handle(storage_id, revision, type_, config, CANDIDATE)


def temporary(storage_id: int | None, type_: str, config: Mapping[str, Any]) -> Handle:
    return Handle(storage_id, 0, type_, config, TEMPORARY)


def apply_temporary(handle: Handle) -> dict[str, Any]:
    """After a temporary instance (or a candidate whose save didn't happen): store the secrets it
    changed where they replace the stored ones; returns the rest, for the form."""
    changes, before = handle.take_changes()
    if not changes or handle.storage_id is None:
        return changes
    params = storage_config.params(handle.cls)
    secret = {key: value for key, value in changes.items() if params[key].secret}
    saved = save_changes(handle, secret, before, only_replacing=True) if secret else {}
    return {key: value for key, value in changes.items() if key not in saved}


def record(storage_id: int, ok: bool, message: str = "") -> None:
    """The outcome of the last check of a storage, for the admin list."""
    _status[storage_id] = {"ok": ok, "message": message, "at": timezone.now()}


def status(storage_id: int) -> dict[str, Any] | None:
    return _status.get(storage_id)


def forget(storage_id: int) -> None:
    drop(storage_id)
    _status.pop(storage_id, None)


def close_all() -> None:
    """Closes every instance (the end of the program, and tests)."""
    with _registry_lock:
        handles = list(_live.values())
        _live.clear()
        _status.clear()
    for handle in handles:
        handle.close()


def capabilities(storage) -> dict[str, Any]:
    """What the browser needs to know of a storage's driver, read from its instance (FTP sets
    can_range from its params), or from the class for a storage that has none."""
    source: Any
    try:
        source = get(storage)
    except Exception:  # noqa: BLE001 - disabled, or a driver that can't start: the class's flags
        try:
            source = storage_config.driver(storage.type)
        except Exception:  # noqa: BLE001
            source = StorageDriver
    return {
        "range": bool(source.can_range),
        "move": bool(source.can_move),
        "copy": bool(source.can_copy),
        "write": bool(source.can_write),
        "folders": bool(source.has_folders),
        "search": bool(source.can_search),
        "forbidden_chars": str(source.forbidden_chars),
        "max_name_bytes": int(source.max_name_bytes),
    }
