"""`hfile cleanup`: removing what has expired, from a job that cron runs (decision D29). No scheduler
thread: the command (apps/web/management/commands/cleanup.py) runs the jobs the apps registered.

An app registers its job from its AppConfig.ready():

    from apps.core import cleanup
    cleanup.register("uploads", gettext_lazy("Expired upload sessions"), remove_expired)

`func(now)` removes what has expired at `now` (an aware datetime) and returns how many rows or files
it removed. `label` is what the command prints before the count: gettext_lazy, as it is made at
import and printed in the command's language.

run() calls the jobs in the order they were registered, each in a transaction of its own: a job that
raises is rolled back and reported (the command then exits with an error), and the others still run.
A job changes only the database inside its transaction and leaves everything else, deleting files
say, to transaction.on_commit(): `--dry-run` rolls every transaction back, so that work never
happens and nothing changes, while the counts are the real ones.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

logger = logging.getLogger("hfile.cleanup")


@dataclass(frozen=True)
class Job:
    name: str
    label: str
    func: Callable[[datetime], int]


_lock = threading.Lock()
_jobs: dict[str, Job] = {}


def register(name: str, label: str, func: Callable[[datetime], int]) -> None:
    """Adds a job, or replaces the one of that name (ready() may run again in tests)."""
    if not name or not isinstance(name, str):
        raise ValueError("a cleanup job needs a name")
    with _lock:
        _jobs[name] = Job(name, label, func)


def jobs() -> list[Job]:
    with _lock:
        return list(_jobs.values())


def run(now: datetime | None = None, only: Iterable[str] | None = None, *, dry_run: bool = False,
        errors: list | None = None) -> list[tuple[str, str, int | None]]:
    """Runs the jobs (those named in `only`, else all): [(name, label, count)], count None for a job
    that failed; `errors`, when given, gets (name, label, exception) for each of those. With dry_run
    each job's transaction is rolled back. ValueError for a name in `only` that isn't registered."""
    now = now or timezone.now()
    selected = jobs()
    if only is not None:
        wanted = list(dict.fromkeys(only))
        known = {job.name for job in selected}
        unknown = [name for name in wanted if name not in known]
        if unknown:
            raise ValueError(_("No such cleanup job: ") + _(", ").join(unknown))
        selected = [job for job in selected if job.name in wanted]
    results: list[tuple[str, str, int | None]] = []
    for job in selected:
        try:
            with transaction.atomic():
                count = int(job.func(now) or 0)
                if dry_run:
                    transaction.set_rollback(True)
        except Exception as error:  # noqa: BLE001 - reported, and the other jobs still run
            logger.exception("The cleanup job %s failed", job.name)
            if errors is not None:
                errors.append((job.name, job.label, error))
            results.append((job.name, job.label, None))
            continue
        results.append((job.name, job.label, count))
    return results
