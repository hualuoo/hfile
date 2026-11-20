"""The threads downloads through the site run on (/pd/, apps/files/download.py).

waitress answers a request on one of its threads ([server] threads in the settings file), the pages and the
API too. A download holds its thread until all but the last part of the answer has gone (waitress keeps up to
16 MB of it), for as long as its client takes: a few slow downloads, or the many connections of one download
tool, took every thread, and the site stopped answering. `hfile serve` (install()) gives /pd/ requests threads
of their own. waitress hands a request to a thread in one place, its server's add_task() (from the connection,
when a request has arrived, or the one before it on the connection has been answered): /pd/ requests go to
this pool there, the others to waitress's threads.

The pool has a thread for each download the storage sources allow at once (their proxy_connections, added up;
apps/files/limits.py refuses more), and SPARE more for the answers that don't stream (429, a redirect, HEAD),
MOST at most. It follows the storages: saving or deleting one resizes it (apps.py). Its threads don't keep a
database connection between requests: hundreds of idle threads would hold as many (MySQL allows 151 by
default). runserver and the tests have no pool: /pd/ is answered where every request is.
"""

from __future__ import annotations

import threading

from django.db import connections
from django.db.models import Sum

PREFIX = "/pd/"
SPARE = 8
MOST = 1000

_lock = threading.Lock()
_dispatcher = None  # waitress's ThreadedTaskDispatcher, once install() has run


def wanted() -> int:
    """The threads the pool should have: the downloads all storages allow at once, and SPARE."""
    from apps.storages.models import StorageSource

    total = StorageSource.objects.aggregate(total=Sum("proxy_connections"))["total"] or 0
    return min(MOST, SPARE + total)


class _Task:
    """A connection's request, answered on a thread of the pool: then the database connections it opened
    are closed."""

    def __init__(self, channel):
        self.channel = channel

    def service(self):
        try:
            self.channel.service()
        finally:
            connections.close_all()

    def cancel(self):
        self.channel.cancel()


def _servers(server):
    from waitress.server import BaseWSGIServer, MultiSocketServer

    if isinstance(server, MultiSocketServer):  # a host name with several addresses: one server each
        return [each for each in server.map.values() if isinstance(each, BaseWSGIServer)]
    return [server]


def is_download(request) -> bool:
    """Whether waitress's request (its parser) is one for the pool. A request waitress couldn't read is
    answered with its error by waitress's threads."""
    return request.error is None and (getattr(request, "path", "") or "").startswith(PREFIX)


def install(server) -> None:
    """Sends the /pd/ requests of `server` (waitress's create_server()) to a pool of their own."""
    from waitress.task import ThreadedTaskDispatcher

    global _dispatcher
    pool = ThreadedTaskDispatcher()
    for each in _servers(server):
        pages = each.task_dispatcher

        def add_task(channel, pages=pages):
            # channel.requests[0] is the request to answer next (waitress calls this under its lock)
            if channel.requests and is_download(channel.requests[0]):
                pool.add_task(_Task(channel))
            else:
                pages.add_task(channel)

        each.add_task = add_task
    with _lock:
        _dispatcher = pool
    resize()


def resize() -> None:
    """Gives the pool the threads it should have now (wanted()); nothing without a pool."""
    with _lock:
        pool = _dispatcher
    if pool is None:
        return
    count = wanted()
    with _lock:
        if _dispatcher is pool:  # not stopped meanwhile
            pool.set_thread_count(count)


def shutdown(timeout: float) -> None:
    """Stops the pool: its downloads may go on for `timeout` seconds, then the program ends without them."""
    global _dispatcher
    with _lock:
        pool, _dispatcher = _dispatcher, None
    if pool is not None:
        pool.shutdown(timeout=timeout)
