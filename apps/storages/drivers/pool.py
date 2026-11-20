"""A pool of connections for drivers of stateful protocols (FTP, SFTP).

One driver instance serves many threads. Each operation borrows a connection, uses it alone and
gives it back; a connection whose state is unknown after a failure (a dropped socket, an aborted
transfer) is given back as broken and closed. A connection that sat idle for a while is checked
before it's lent again, and one idle for longer is closed: servers drop idle connections.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Generic, TypeVar

from django.utils.translation import gettext as _, gettext_lazy

from .base import BackendError

T = TypeVar("T")

log = logging.getLogger("hfile.storage.pool")


class Pool(Generic[T]):
    def __init__(self, create: Callable[[], T], close: Callable[[T], None], *, size: int,
                 check: Callable[[T], bool] | None = None, wait: float = 20.0, check_after: float = 15.0,
                 max_idle: float = 240.0, busy: str = gettext_lazy("All connections to the storage are in use. Please try again later.")):
        """create() makes a connection (raising DriverError when it can't), close() closes one and
        mustn't raise, check() tells whether an idle one still works. At most `size` connections
        exist; a borrower waits up to `wait` seconds for one, then gets BackendError(busy)."""
        self._create = create
        self._close = close
        self._check = check
        self.size = max(1, size)
        self._wait = wait
        self._check_after = check_after
        self._max_idle = max_idle
        self._busy = busy
        self._cond = threading.Condition()
        self._idle: list[tuple[T, float]] = []  # (connection, when it was given back); newest last
        self._count = 0  # connections that exist or are being made
        self._closed = False

    def acquire(self) -> T:
        deadline = time.monotonic() + self._wait
        while True:
            expired: list[T] = []
            conn = None
            make = False
            with self._cond:
                while True:
                    if self._closed:
                        raise BackendError(_("The storage has been closed."))
                    now = time.monotonic()
                    while self._idle and now - self._idle[0][1] > self._max_idle:
                        expired.append(self._idle.pop(0)[0])
                        self._count -= 1
                    if self._idle:
                        conn, since = self._idle.pop()
                        stale = now - since > self._check_after
                        break
                    if self._count < self.size:
                        self._count += 1
                        make = True
                        break
                    left = deadline - now
                    if left <= 0:
                        raise BackendError(self._busy)
                    self._cond.wait(left)
            for old in expired:
                self._close_quietly(old)
            if make:
                try:
                    return self._create()
                except BaseException:
                    self._forget()
                    raise
            if stale and self._check is not None:
                try:
                    ok = self._check(conn)
                except Exception:  # noqa: BLE001 - any failure means it's gone
                    ok = False
                if not ok:
                    log.debug("dropped an idle connection that stopped working")
                    self._forget()
                    self._close_quietly(conn)
                    continue
            return conn

    def release(self, conn: T, *, broken: bool = False) -> None:
        with self._cond:
            keep = not broken and not self._closed
            if keep:
                self._idle.append((conn, time.monotonic()))
            else:
                self._count -= 1
            self._cond.notify()
        if not keep:
            self._close_quietly(conn)

    def close(self) -> None:
        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._count -= len(idle)
            self._cond.notify_all()
        for conn, _since in idle:
            self._close_quietly(conn)

    def _forget(self) -> None:
        with self._cond:
            self._count -= 1
            self._cond.notify()

    def _close_quietly(self, conn: T) -> None:
        try:
            self._close(conn)
        except Exception:  # noqa: BLE001 - closing is best effort
            log.debug("closing a connection failed", exc_info=True)
