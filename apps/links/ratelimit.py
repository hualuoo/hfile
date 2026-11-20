"""The download rate limit of path links and short links (inventory 9.7, decision D38): 下载频率限制,
"单个 IP 在 N 秒内最多下载 M 次", one budget for both kinds of link.

- A fixed window per client address (apps.core.net.client_ip; an IPv6 address counts by its /64, as the
  site's other limits do): it starts with the first request and lasts link_limit_seconds; every
  request in it counts, refused ones too, and the one over link_limit_downloads is answered 429 with
  the seconds left. 0 in either setting is no limit (nothing is counted then).
- In this process's memory, thread-safe, bounded: at most MAX_WINDOWS addresses. A new address is never
  refused for want of room: the windows that ended go first, then the oldest one that isn't over the limit
  (among the oldest EVICTION_SCAN), so that flooding the table from many addresses doesn't free one that is
  being refused; only when all of those are over it, the oldest. `hfile serve` runs one process; behind
  several processes each one would count for itself.

windows() lists the live windows for the admins' 统计信息.

Unknown short keys have a budget of their own (misses): MISSES within MISS_SECONDS per address, then every
/s/ address answers 429 for the rest of that window, so that keys can't be guessed at the speed of the
general limit (people mistype a key now and then, they don't try dozens).

The download log has windows of its own (opened, apps.links.delivery step 6): one per link, IP and user,
of which only the first opening gets a row. Here rather than in Django's cache, whose counters of the
site's other limits (logins, folder passwords) a flood of openings would push out.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

from apps.core import net
from apps.core.throttles import UNKNOWN, address_key

MAX_WINDOWS = 20_000
EVICTION_SCAN = 64
MISSES = 30
MISS_SECONDS = 3600


@dataclass(frozen=True)
class Window:
    address: str
    count: int
    started: float  # time.time()
    expires: float  # time.time()


class Limiter:
    def __init__(self, max_windows: int = MAX_WINDOWS):
        self.max_windows = max_windows
        self._lock = threading.Lock()
        # address -> [start (time.monotonic()), count], in the order the windows started: all windows are
        # equally long, so the first ones are the first to end.
        self._windows: OrderedDict[str, list] = OrderedDict()

    def hit(self, address: str, seconds: int, limit: int, now: float | None = None) -> float:
        """Counts a request of `address`; the seconds it must wait when it is over the limit, else 0."""
        if seconds <= 0 or limit <= 0:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(address)
            if window is not None and now >= window[0] + seconds:
                del self._windows[address]
                window = None
            if window is None:
                self._make_room(now, seconds, limit)
                window = self._windows[address] = [now, 0]
            window[1] += 1
            if window[1] > limit:
                return max(window[0] + seconds - now, 0.001)
            return 0

    def peek(self, address: str, seconds: int, limit: int, now: float | None = None) -> float:
        """The seconds `address` must wait when its window is over the limit, else 0; counts nothing."""
        if seconds <= 0 or limit <= 0:
            return 0
        now = time.monotonic() if now is None else now
        with self._lock:
            window = self._windows.get(address)
            if window is None or now >= window[0] + seconds or window[1] < limit:
                return 0
            return max(window[0] + seconds - now, 0.001)

    def _make_room(self, now: float, seconds: int, limit: int) -> None:
        windows = self._windows
        while windows and now >= next(iter(windows.values()))[0] + seconds:
            windows.popitem(last=False)  # ended: all windows are equally long, the first end first
        if len(windows) < self.max_windows:
            return
        victim = None
        for index, (address, (_start, count)) in enumerate(windows.items()):
            if index >= EVICTION_SCAN:
                break
            if count <= limit:
                victim = address
                break
        if victim is None:
            windows.popitem(last=False)
        else:
            del windows[victim]

    def windows(self, seconds: int, now: float | None = None) -> list[Window]:
        """The windows that haven't ended, the busiest first."""
        if seconds <= 0:
            return []
        now = time.monotonic() if now is None else now
        wall = time.time() - now  # monotonic -> wall clock
        with self._lock:
            live = [(address, start, count) for address, (start, count) in self._windows.items()
                    if now < start + seconds]
        live.sort(key=lambda item: (-item[2], item[1]))
        return [Window(address, count, wall + start, wall + start + seconds) for address, start, count in live]

    def reset(self) -> None:
        with self._lock:
            self._windows.clear()


limiter = Limiter()
misses = Limiter()
opened = Limiter()


def client_address(request) -> str:
    """What a request counts for: its client IP (IPv6 by its /64), or one shared "unknown"."""
    ip = net.client_ip(request)
    return UNKNOWN if ip is None else address_key(ip)


def check(request, site) -> float:
    """Counts this request against the site's limit; the seconds to wait when it is refused, else 0."""
    return limiter.hit(client_address(request), site.link_limit_seconds, site.link_limit_downloads)


def missed_wait(request) -> float:
    """The seconds this address must wait before opening short links again after too many unknown keys
    (missed()), else 0; doesn't count anything."""
    return misses.peek(client_address(request), MISS_SECONDS, MISSES)


def missed(request) -> None:
    """Counts an unknown short key of this address."""
    misses.hit(client_address(request), MISS_SECONDS, MISSES)
