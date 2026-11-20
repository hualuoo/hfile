"""Limits on downloads through the site (/pd/, apps/files/download.py), in this process's memory, like the
links' rate limit:

- a storage source serves its proxy_connections at once; one more is sent to the storage's own link
  (WhenFull.LINK, for the storages that have one: apps.files.transfer.falls_back()) or gets 429;
- an IP (IPv6 by its /64, as the site's other limits) gets SiteSettings.proxy_ip_connections at once, then
  429: sending it to the storage's own link would get it round its limits;
- an IP's downloads through the site together go at proxy_ip_speed KB/s at most: each piece reserves its time
  after the ones before it (Speed), so more connections don't make it faster. A change of the setting reaches
  the downloads under way within RATE_EVERY seconds.

Every connection counts (take()), from the moment its answer is decided until the answer is closed: waitress
has handed it all to the connection (the last 16 MB at most may still be on the way), or the client went
away. A download tool's connections count, and so do a gallery's pictures and a player's ranges; HEAD and
answers without a body (416, errors, redirects) don't.
"""

from __future__ import annotations

import threading
import time

from django.db import connections

from apps.core import net
from apps.core.throttles import UNKNOWN, address_key

RETRY_AFTER = 5  # seconds a refused download is told to wait: a place frees when another download ends
PIECE = 64 * 1024  # bytes sent at most between two looks at the speed
BURST = 0.25  # seconds of its speed an IP may be ahead
RATE_EVERY = 5  # seconds between two reads of the speed during a download


class Full(Exception):
    """No place for the download: its storage's (storage=True) or its address's."""

    def __init__(self, *, storage: bool):
        super().__init__(storage)
        self.storage = storage


class Counter:
    """What is in use per key, each up to a limit of its own. Thread-safe."""

    def __init__(self):
        self._lock = threading.Lock()
        self._used: dict = {}

    def take(self, key, limit: int) -> bool:
        """One more for `key`, unless it has `limit` already (0: no limit)."""
        with self._lock:
            used = self._used.get(key, 0)
            if limit and used >= limit:
                return False
            self._used[key] = used + 1
            return True

    def give_back(self, key) -> None:
        with self._lock:
            used = self._used.get(key, 0) - 1
            if used > 0:
                self._used[key] = used
            else:
                self._used.pop(key, None)

    def used(self, key) -> int:
        with self._lock:
            return self._used.get(key, 0)

    def reset(self) -> None:
        with self._lock:
            self._used.clear()


class Speed:
    """The time an IP's downloads have reserved (GCRA): a piece of `size` bytes takes size / rate seconds
    after the pieces before it, of any of the IP's connections, and may go BURST seconds before its turn."""

    def __init__(self):
        self._lock = threading.Lock()
        self._free: dict[str, float] = {}  # address -> when what it reserved ends (time.monotonic())

    def wait(self, address: str, size: int, rate: int) -> float:
        """The seconds to wait before sending `size` bytes at `rate` bytes a second."""
        with self._lock:
            now = time.monotonic()
            free = max(self._free.get(address, now), now) + size / rate
            self._free[address] = free
            if len(self._free) > 10_000:  # the addresses that reserved nothing still to come
                for key in [key for key, end in self._free.items() if end < now]:
                    del self._free[key]
            return max(0.0, free - now - BURST)

    def reset(self) -> None:
        with self._lock:
            self._free.clear()


storages = Counter()  # by storage source id
addresses = Counter()  # by client address
speeds = Speed()


def client_address(request) -> str:
    """What a request counts for: its client IP (IPv6 by its /64), or one shared "unknown"."""
    ip = net.client_ip(request)
    return UNKNOWN if ip is None else address_key(ip)


class Ticket:
    """A download's place in its storage's count and its address's, until release()."""

    def __init__(self, storage_id: int, address: str):
        self.storage_id = storage_id
        self.address = address
        self._lock = threading.Lock()
        self._held = True

    def release(self) -> None:
        with self._lock:
            if not self._held:
                return
            self._held = False
        storages.give_back(self.storage_id)
        addresses.give_back(self.address)


def take(request, storage) -> Ticket:
    """A place for one download of `storage` through the site. Full when its address, or the storage, has
    as many as it may."""
    from apps.siteconf.models import SiteSettings

    address = client_address(request)
    if not addresses.take(address, SiteSettings.load().proxy_ip_connections):
        raise Full(storage=False)
    if not storages.take(storage.pk, storage.proxy_connections):
        addresses.give_back(address)
        raise Full(storage=True)
    return Ticket(storage.pk, address)


def _rate() -> int:
    """The speed of an IP's downloads, in bytes a second (0: no limit)."""
    from apps.siteconf.models import SiteSettings

    return SiteSettings.load().proxy_ip_speed * 1024


def _close_database() -> None:
    """The request's database connections, closed once its answer streams: a long download doesn't hold one
    (the pool's threads close them anyway when the request ends). Not one in a transaction (a test's)."""
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close()


class Metered:
    """A download's stream (apps.storages.drivers.base.ReadStream) as its answer sends it: at its IP's speed,
    and its place released when the answer is closed."""

    def __init__(self, stream, ticket: Ticket):
        self._stream = stream
        self._ticket = ticket

    def __iter__(self):
        rate, read = _rate(), time.monotonic()
        first = True
        for chunk in self._stream:
            if first:
                _close_database()
                first = False
            if time.monotonic() - read >= RATE_EVERY:
                rate, read = _rate(), time.monotonic()
            if not rate:
                yield chunk
                continue
            step = min(PIECE, max(4096, rate // 8))  # several pieces a second, even at a low speed
            for start in range(0, len(chunk), step):
                piece = chunk[start:start + step]
                delay = speeds.wait(self._ticket.address, len(piece), rate)
                if delay:
                    time.sleep(delay)
                yield piece

    def close(self) -> None:
        try:
            self._stream.close()
        finally:
            self._ticket.release()
