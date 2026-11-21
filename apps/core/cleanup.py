"""Removing what has expired, by the site itself: a thread of each process that serves the site runs the
jobs the apps registered, a few minutes after it starts and then every hour. Expired shares and short links
aren't among them: they show as expired until somebody deletes them.

An app registers its job from its AppConfig.ready():

    from apps.core import cleanup
    cleanup.register("uploads", remove_expired)

`func(now)` removes what has expired at `now` (an aware datetime) and returns how many rows or files it
removed. run() calls the jobs in the order they were registered, each in a transaction of its own: a job that
raises is rolled back and logged, and the others still run. A job changes only the database inside its
transaction and leaves everything else, deleting files say, to transaction.on_commit(), so that a file goes
only with a row that is really gone. A job that may find a great many removes a bounded number per run and
leaves the rest to the next: on SQLite a transaction holds the write lock from its start
(config/database.py), and the site's other writes wait for it.

config/wsgi.py starts the thread (start()) once the database is checked or upgraded, whatever server loads
the site: `hfile serve`, or each worker of another server. Several processes, or machines, may each run the
jobs: they only delete what has expired, and two of them clean files of the machine they run on (the upload
spool, the backups). Not before the site is installed, when there is no database; the commands and the
tests don't load config/wsgi.py.
"""

from __future__ import annotations

import atexit
import logging
import random
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.db import DatabaseError, connections, transaction
from django.utils import timezone

logger = logging.getLogger("hfile.cleanup")

FIRST_AFTER = 300  # seconds from the start to the first run, plus up to SPREAD
SPREAD = 300  # so that processes started together don't all run at once
EVERY = 3600  # seconds between runs
EXIT_WAIT = 10  # seconds a run under way has to finish its job when the program exits


@dataclass(frozen=True)
class Job:
    name: str
    func: Callable[[datetime], int]


_lock = threading.Lock()
_jobs: dict[str, Job] = {}
_thread: threading.Thread | None = None
_stop = threading.Event()


def register(name: str, func: Callable[[datetime], int]) -> None:
    """Adds a job, or replaces the one of that name (ready() may run again in tests)."""
    if not name or not isinstance(name, str):
        raise ValueError("a cleanup job needs a name")
    with _lock:
        _jobs[name] = Job(name, func)


def jobs() -> list[Job]:
    with _lock:
        return list(_jobs.values())


def run(now: datetime | None = None) -> list[tuple[str, int | None]]:
    """Runs the jobs: [(name, count)], count None for a job that failed (logged). Stops between jobs once
    the program is exiting."""
    now = now or timezone.now()
    results: list[tuple[str, int | None]] = []
    for job in jobs():
        if _stop.is_set():
            break
        try:
            with transaction.atomic():
                count = int(job.func(now) or 0)
        except Exception:  # noqa: BLE001 - logged, and the other jobs still run
            logger.exception("The cleanup job %s failed", job.name)
            count = None
        results.append((job.name, count))
    removed = [f"{name} {count}" for name, count in results if count]
    if removed:
        logger.info("Removed what had expired: %s", ", ".join(removed))
    return results


def start() -> None:
    """Starts the thread that runs the jobs, once per process (config/wsgi.py)."""
    global _thread
    if settings.SETUP_MODE:  # not installed yet: no database; the installed site runs in a new process
        return
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_loop, name="hfile-cleanup", daemon=True)
        _thread.start()
    atexit.register(_stop_at_exit)


def _loop() -> None:
    wait = FIRST_AFTER + random.uniform(0, SPREAD)
    while not _stop.wait(wait):
        wait = EVERY
        run()
        try:
            connections.close_all()  # this thread's own: by the next run MySQL may have dropped them
        except DatabaseError:
            pass


def _stop_at_exit() -> None:
    _stop.set()
    if _thread is not None:
        _thread.join(EXIT_WAIT)
