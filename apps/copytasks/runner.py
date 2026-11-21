"""Running a copy task: one run, from the source folder into the target folder (apps/copytasks/models.py).

A run has threads of its own, and only the first of them touches the task's row:

- the run's thread (execute()) writes the progress into the task's row every TICK seconds, with the files that
  failed (CopyFailure); it ends the run when everything is done or the run must stop, and writes how it ended;
- the scanner walks the source folder, a folder at a time, depth first: it lists the folder, makes it in the target
  (or lists the one there) and puts the files to copy in a queue, deciding with the target's listing which ones are
  there already (the same name and size: same_file; the same name only: same_name). So the copying starts at once,
  and the totals grow until the listing is done (`scanning`). A folder whose listing is the same as one of the
  folders above it isn't gone into: a link back up (the local storage follows links inside its folder) would make
  copies of copies, of two such links without end (on NTFS a folder above may need listing again: _listed_above());
- `threads` workers copy the files of the queue, each a file at a time: the source's stream goes straight into the
  target's write() (SourceReader), nothing is kept on the server's disk. A file that fails is tried again `retries`
  times, waiting 1, 2, 4… seconds (at most BACKOFF_MAX) in between; an error that won't go away (a name the target
  refuses, the file gone from the source) isn't tried again. The scanner's listings and new folders are tried again
  the same way. A storage with a pool of connections (FTP, SFTP: max_transfers) gets no more workers than it has
  connections, less one for the scanner (half of those when it is both the source and the target: each file then
  holds two); more would wait for a connection until they fail.

A run stops early when an admin stops it (status STOPPING, which the run's thread sees within a TICK), when the
account of a storage source stops working (AuthError: every file would fail), when a storage source is deleted,
disabled or gets new settings (its paths may mean something else now), when the program exits (atexit, registered
with the first run: so it runs before the storages' instances are closed, which registered theirs earlier; the run is
written as interrupted) and when another run took the task over (run_id: it writes nothing more then). What was
copied stays. A run whose thread stopped writing without saying how it ended (the program was killed) shows as
interrupted once its last write is STALE (status_of()); it can be resumed then too.

The threads speak the language of the admin who started the run: its messages are stored in it. Drivers may write
the database themselves (a refreshed token), so every thread closes its connections when it ends, and a worker drops
one that broke before each file. The run's thread does so after an error of the database (MySQL restarted): its
next write opens a new connection.
"""

from __future__ import annotations

import atexit
import hashlib
import itertools
import logging
import queue
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from datetime import timedelta

from django.db import DatabaseError, close_old_connections, connections, transaction
from django.utils import timezone, translation
from django.utils.translation import gettext as _, gettext_lazy, ngettext

from apps.storages import instances
from apps.storages.drivers import paths
from apps.storages.drivers.base import (
    AlreadyExists,
    AuthError,
    DriverError,
    InvalidPath,
    NotADirectory,
    NotFound,
    Unsupported,
)
from apps.storages.models import StorageSource

from .models import MESSAGE_MAX, CopyFailure, CopyTask
from .reader import Meter, SourceReader, Stopped

logger = logging.getLogger(__name__)

Status = CopyTask.Status
Policy = CopyTask.Policy

TICK = 1.0  # seconds between two writes of the progress
STALE = timedelta(seconds=60)  # a run that hasn't written for this long has died
SPEED_WINDOW = 5.0  # seconds the speeds are measured over
QUEUE_MAX = 100_000  # files found and not copied yet; the scanner waits while there are more
MAX_DEPTH = 256  # folders in folders, as the site's paths have at most (apps.storages.access)
BACKOFF_MAX = 30  # seconds at most between two tries of a file
STOP_GRACE = 120.0  # seconds a run that must stop waits for its threads to let go before it says it stopped
EXIT_GRACE = 5.0  # seconds the program waits at exit for its runs to write that they were interrupted
INTERRUPTED = gettext_lazy("Interrupted: the site stopped while copying. It can be resumed.")

SOURCE, TARGET = "source", "target"


class Superseded(Exception):
    """Another run has the task now (a 继续 after this one looked dead): write nothing more."""


class _Failed(Exception):
    """A file's try failed: `error`, from the source's side or the target's."""

    def __init__(self, side: str, error: Exception):
        super().__init__(side, error)
        self.side = side
        self.error = error


def _clip(text) -> str:
    return str(text)[:MESSAGE_MAX]


class _Rate:
    """A speed over the last SPEED_WINDOW seconds, from a growing count of bytes."""

    def __init__(self):
        self._samples: deque[tuple[float, int]] = deque()

    def __call__(self, now: float, count: int) -> int:
        self._samples.append((now, count))
        while len(self._samples) > 2 and now - self._samples[1][0] >= SPEED_WINDOW:
            self._samples.popleft()
        first_time, first = self._samples[0]
        return int((count - first) / (now - first_time)) if now > first_time else 0


def _listing(entries) -> bytes:
    """What a folder's listing is, to tell the same one again: its names, kinds, sizes and times, in order."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda entry: (entry.name, entry.is_dir)):
        digest.update(repr((entry.name, entry.is_dir, entry.size, entry.modified)).encode("utf-8", "surrogatepass"))
    return digest.digest()


def _names(entries) -> bytes:
    """Only the names and kinds of a folder's listing (Run._listed_above())."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda entry: (entry.name, entry.is_dir)):
        digest.update(repr((entry.name, entry.is_dir)).encode("utf-8", "surrogatepass"))
    return digest.digest()


@dataclass
class Job:
    source: str  # the file's path in the source storage
    target: str  # where it goes in the target storage
    path: str  # its path in the task's folders: "/photos/a.jpg"
    size: int | None  # as listed


class _File:
    """A file being copied, as the page shows it. Its worker writes it, the run's thread reads it."""

    def __init__(self, job: Job, order: int):
        self.path = job.path
        self.size = job.size
        self.order = order
        self.done = 0  # where its reader is
        self.read = 0  # bytes read from the source, a second pass too
        self.attempt = 1
        self.error = ""  # of the try before
        self.rate = _Rate()

    def add_read(self, count: int) -> None:
        self.read += count

    def at(self, position: int) -> None:
        self.done = position


class Run:
    """One run of a task (see the top)."""

    def __init__(self, task: CopyTask, language: str):
        self.task_id = task.pk
        self.run_id = task.run_id
        self.language = language
        self.storage_ids = {SOURCE: task.source_id, TARGET: task.target_id}
        self.names = {SOURCE: task.source_name, TARGET: task.target_name}
        self.source_path, self.target_path = task.source_path, task.target_path
        self.threads = task.threads
        self.retries = task.retries
        # 继续 goes on where the run before stopped: what it copied is there with the same name and size.
        self.same_file = Policy.SKIP if task.run > 1 else task.same_file
        self.same_name = task.same_name
        self.stop = threading.Event()
        self.idle = threading.Event()  # all its threads have ended
        self.ended = threading.Event()  # the run's thread is done: it has written how the run ended, or mustn't
        self.meter = Meter(task.speed_limit, self.stop)
        self.jobs: queue.Queue[Job | None] = queue.Queue(maxsize=QUEUE_MAX)
        self.lock = threading.Lock()
        self.ending: tuple[str, object] | None = None  # (status, message) of the first reason to stop
        self.grace = STOP_GRACE
        self.revisions: dict[int, int] = {}  # the storages' config_revision when the run began
        self.scanned = False
        self._live = 0
        self._order = itertools.count()
        self._current: dict[int, _File] = {}
        self._failures: list[CopyFailure] = []
        self._rate = _Rate()
        # The counts, under the lock.
        self.files_total = self.files_copied = self.files_skipped = self.files_failed = self.folders_failed = 0
        self.bytes_total = self.bytes_copied = self.bytes_skipped = self.bytes_finished = 0

    # ------------------------------------------------------------ stopping

    def halt(self, status: str, message: object = "", *, grace: float | None = None) -> None:
        """Stop the run: the first reason given is how it ends."""
        with self.lock:
            if self.ending is None:
                self.ending = (status, message)
            if grace is not None:
                self.grace = min(self.grace, grace)
        self.stop.set()

    def _fail_run(self, side: str, message: object) -> None:
        """End the run as failed, because of one of the storages."""
        self.halt(Status.FAILED, _("%(storage)s: %(error)s") % {"storage": self.names[side], "error": message})

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise Stopped

    def _again(self, call, *args):
        """call(*args) of a storage, tried again after an error that may go away, as files are (retries times, waiting
        1, 2, 4… seconds). Raises Stopped when the run stops meanwhile."""
        for attempt in itertools.count(1):
            try:
                return call(*args)
            except (AuthError, NotFound, NotADirectory, AlreadyExists, InvalidPath, Unsupported):
                raise
            except DriverError:
                if attempt > self.retries:
                    raise
            if self.stop.wait(min(BACKOFF_MAX, 2 ** (attempt - 1))):
                raise Stopped

    # ------------------------------------------------------------ the counts

    def _found(self, size: int | None) -> None:
        with self.lock:
            self.files_total += 1
            self.bytes_total += size or 0

    def _skipped(self, size: int | None) -> None:
        with self.lock:
            self.files_skipped += 1
            self.bytes_skipped += size or 0
            self.bytes_finished += size or 0

    def _copied(self, state: _File) -> None:
        with self.lock:  # and no longer under way, at once: a tick in between would count it twice
            self.files_copied += 1
            self.bytes_copied += state.size or 0
            self.bytes_finished += state.size or 0
            self._current.pop(state.order, None)

    def _failed(self, path: str, size: int | None, message: object, attempts: int, *, is_dir: bool = False,
                state: _File | None = None) -> None:
        failure = CopyFailure(task_id=self.task_id, path=path, is_dir=is_dir, size=size, message=_clip(message),
                              attempts=attempts)
        with self.lock:
            if is_dir:
                self.folders_failed += 1
            else:
                self.files_failed += 1
                self.bytes_finished += size or 0
            self._failures.append(failure)
            if state is not None:
                self._current.pop(state.order, None)

    def _resized(self, old: int | None, new: int) -> None:
        """A file is another size now than the listing said."""
        with self.lock:
            self.bytes_total += new - (old or 0)

    # ------------------------------------------------------------ the scanner

    def scan(self, source, target) -> None:
        try:
            with translation.override(self.language):
                self._scan(source, target)
        except Stopped:
            pass
        except Exception as exc:  # noqa: BLE001 - a bug: the run ends with it, the traceback goes to the log
            logger.exception("Copy task %s: listing the files failed", self.task_id)
            with translation.override(self.language):
                self.halt(Status.FAILED, _("Listing the files failed: %(error)s") % {"error": exc})
        finally:
            self.scanned = True
            for _worker in range(self.threads):  # one for each worker: no more files
                while not self.stop.is_set():
                    try:
                        self.jobs.put(None, timeout=0.5)
                        break
                    except queue.Full:
                        continue

    def _scan(self, source, target) -> None:
        root = self.source_path
        try:
            entry = self._again(source.stat, root)
        except NotFound:
            self.halt(Status.FAILED, _("The source folder doesn't exist."))
            return
        except DriverError as error:
            self._fail_run(SOURCE, error.message)
            return
        if not entry.is_dir:
            self.halt(Status.FAILED, _("The source is a file, not a folder."))
            return
        exists = self._target_root(target)
        if exists is None:
            return
        stack = [(root, self.target_path, exists, 0, ())]
        while stack:
            self._check_stop()
            folder, into, exists, depth, above = stack.pop()
            path = paths.relative(folder, root)
            try:
                entries = self._again(source.list, folder)
            except DriverError as error:
                if isinstance(error, AuthError):
                    self._fail_run(SOURCE, error.message)
                    return
                self._failed(path, None, _("Couldn't list the folder: %(error)s") % {"error": error.message}, 1, is_dir=True)
                continue
            listing, names = _listing(entries), _names(entries)
            if self._listed_above(source, listing, names, above):
                self._failed(path, None, _("It has the same files and folders as a folder above it, so it is most likely "
                                           "a link back up; what is in it wasn't copied."), 0, is_dir=True)
                continue
            above = (*above, (folder, names, listing))
            there = self._target_folder(target, into, exists, path)
            if there is None:
                continue
            files = sorted((item for item in entries if not item.is_dir), key=lambda item: item.name)
            folders = sorted((item for item in entries if item.is_dir), key=lambda item: item.name, reverse=True)
            for item in files:
                self._check_stop()
                self._plan(target, item, into, path, there)
            for item in folders:  # the last first: the first comes off the stack first
                inner = f"{path.rstrip('/')}/{item.name}"
                problem = self._name_problem(target, item.name)
                other = there.get(item.name)
                if problem:
                    self._failed(inner, None, problem, 0, is_dir=True)
                elif other is not None and not other.is_dir:
                    self._failed(inner, None, _("The target folder has a file of this name."), 0, is_dir=True)
                elif depth >= MAX_DEPTH:
                    self._failed(inner, None, _("Too many folders inside folders here (at most %(depth)s).")
                                 % {"depth": MAX_DEPTH}, 0, is_dir=True)
                else:
                    stack.append((item.path, paths.join(into, item.name), other is not None, depth + 1, above))

    def _listed_above(self, source, listing: bytes, names: bytes, above) -> bool:
        """Whether a folder above lists as this one does. One with the same names and kinds that listed otherwise is
        listed again first: NTFS lists a folder with the time its parent's index has for it, brought up to date only
        once the folder is next closed, so the folder above may have listed the times its folders had before the walk
        went into them."""
        for folder, their_names, theirs in above:
            if their_names == names and (theirs == listing or self._listed_again(source, folder) == listing):
                return True
        return False

    def _listed_again(self, source, folder: str) -> bytes | None:
        try:
            return _listing(self._again(source.list, folder))
        except DriverError:
            return None  # taken for another folder; an account that stopped working shows at the next listing

    def _target_root(self, target) -> bool | None:
        """Whether the target folder is there already; None when it can't be used (the run fails)."""
        if self.target_path == paths.ROOT:
            return True
        try:
            entry = self._again(target.stat, self.target_path)
        except NotFound:
            return False
        except DriverError as error:
            self._fail_run(TARGET, error.message)
            return None
        if not entry.is_dir:
            self.halt(Status.FAILED, _("The target is a file, not a folder."))
            return None
        return True

    def _target_folder(self, target, into: str, exists: bool, path: str):
        """What the target folder `into` has, by name; made first when it isn't there. None when it can't be."""
        if not exists:
            try:
                self._again(target.mkdir, into)
                return {}
            except AlreadyExists:
                pass  # there since the listing: what is in it is listed below
            except DriverError as error:
                if isinstance(error, AuthError):
                    self._fail_run(TARGET, error.message)
                else:
                    self._failed(path, None, _("Couldn't make the folder in the target: %(error)s")
                                 % {"error": error.message}, 1, is_dir=True)
                return None
        try:
            return {item.name: item for item in self._again(target.list, into)}
        except (NotFound, NotADirectory):
            if exists:  # gone since the listing: made again
                return self._target_folder(target, into, False, path)
            self._failed(path, None, _("The target folder has a file of this name."), 1, is_dir=True)
        except DriverError as error:
            if isinstance(error, AuthError):
                self._fail_run(TARGET, error.message)
            else:
                self._failed(path, None, _("Couldn't list the folder in the target: %(error)s")
                             % {"error": error.message}, 1, is_dir=True)
        return None

    @staticmethod
    def _name_problem(target, name: str) -> str:
        """Why the target can't have this name (the site's rules for new names and the storage's own), or ""."""
        try:
            paths.check_name(name, forbidden=str(getattr(target, "forbidden_chars", "")),
                             max_bytes=int(getattr(target, "max_name_bytes", 255)))
        except InvalidPath as error:
            return str(error.message)
        return ""

    def _plan(self, target, item, into: str, folder: str, there: dict) -> None:
        """A file of the source: to copy, skipped, or failed already."""
        self._found(item.size)
        path = f"{folder.rstrip('/')}/{item.name}"
        problem = self._name_problem(target, item.name)
        if problem:
            self._failed(path, item.size, problem, 0)
            return
        other = there.get(item.name)
        if other is not None:
            if other.is_dir:
                self._failed(path, item.size, _("The target folder has a folder of this name."), 0)
                return
            same = item.size is not None and other.size == item.size
            if (self.same_file if same else self.same_name) == Policy.SKIP:
                self._skipped(item.size)
                return
        job = Job(item.path, paths.join(into, item.name), path, item.size)
        while True:
            self._check_stop()
            try:
                self.jobs.put(job, timeout=0.5)
                return
            except queue.Full:
                continue

    # ------------------------------------------------------------ the workers

    def work(self, source, target) -> None:
        try:
            with translation.override(self.language):
                while not self.stop.is_set():
                    try:
                        job = self.jobs.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if job is None:
                        break
                    close_old_connections()  # a driver's token save may have broken it (MySQL restarted)
                    self._copy(source, target, job)
        except Exception:  # noqa: BLE001 - a bug: the run ends with it
            logger.exception("Copy task %s: a worker failed", self.task_id)
            with translation.override(self.language):
                self.halt(Status.FAILED, _("Something went wrong while copying; the details are in the program's log."))

    def _copy(self, source, target, job: Job) -> None:
        state = _File(job, next(self._order))
        with self.lock:
            self._current[state.order] = state
        try:
            for attempt in itertools.count(1):
                state.attempt = attempt
                try:
                    self._transfer(source, target, job, state)
                except _Failed as failed:
                    if self.stop.is_set():
                        return  # stopped halfway: neither copied nor failed
                    message, again = self._problem(failed.side, failed.error)
                    if self.stop.is_set():
                        return
                    if not again or attempt > self.retries:
                        self._failed(job.path, state.size, message, attempt, state=state)
                        return
                    state.error, state.done = str(message), 0
                    if self.stop.wait(min(BACKOFF_MAX, 2 ** (attempt - 1))):
                        return
                    continue
                self._copied(state)
                return
        finally:
            with self.lock:
                self._current.pop(state.order, None)

    def _transfer(self, source, target, job: Job, state: _File) -> None:
        """One try of a file. Raises _Failed."""
        if self.stop.is_set():
            raise _Failed(SOURCE, Stopped())
        try:
            stream = source.open(job.source)
        except Exception as error:  # noqa: BLE001 - told by _problem()
            raise _Failed(SOURCE, error) from error
        size = stream.size if stream.size is not None else job.size
        reader = SourceReader(source, job.source, size or 0, self.meter, stream=stream,
                              on_read=state.add_read, on_position=state.at)
        try:
            if size is None:
                raise _Failed(SOURCE, Unsupported(_("This file's size is unknown, so it can't be copied.")))
            if size != state.size:
                self._resized(state.size, size)
                state.size = size
            try:
                target.write(job.target, reader, size)
            except Exception as error:  # noqa: BLE001 - told by _problem()
                # The source's error comes out of the target's write() as it is, or changed into the target's.
                if reader.source_error is not None:
                    raise _Failed(SOURCE, reader.source_error) from error
                raise _Failed(TARGET, error) from error
        finally:
            reader.close()

    def _problem(self, side: str, error: Exception) -> tuple[str, bool]:
        """What a failed try says, and whether another try may help. An account that stops working ends the run."""
        if isinstance(error, Stopped):
            return "", False
        if not isinstance(error, DriverError):  # a driver's bug, or an error it didn't expect: tried again all the same
            logger.error("Copy task %s: copying a file failed", self.task_id, exc_info=error)
            return _("Something went wrong: %(error)s") % {"error": error}, True
        if isinstance(error, AuthError):
            self._fail_run(side, error.message)
            return str(error.message), False
        if isinstance(error, Unsupported):
            return str(error.message), False
        if side == SOURCE:
            if isinstance(error, NotFound):
                return _("The file isn't in the source any more."), False
            return _("Couldn't read it from the source: %(error)s") % {"error": error.message}, True
        if isinstance(error, (InvalidPath, AlreadyExists, NotADirectory)):
            return str(error.message), False
        return _("Couldn't write it into the target: %(error)s") % {"error": error.message}, True

    # ------------------------------------------------------------ the run's thread

    def thread(self, work, *args):
        """A thread of the run doing work(*args); the last one to end makes the run idle."""
        def body():
            try:
                work(*args)
            finally:
                connections.close_all()  # its own, of a driver that saved a refreshed token
                with self.lock:
                    self._live -= 1
                    last = not self._live
                if last:
                    self.idle.set()

        with self.lock:
            self._live += 1
        return threading.Thread(target=body, daemon=True, name=f"copytask-{self.task_id}")

    def tick(self, now) -> None:
        """Check what the database says (stopped by an admin, another run, the storages) and write the progress.
        Raises Superseded."""
        with transaction.atomic():
            # Locked (a 继续 locks it too, and waits): it can't take the task over between the two queries.
            status = (CopyTask.objects.select_for_update().filter(pk=self.task_id, run_id=self.run_id)
                      .values_list("status", flat=True).first())
            if status is None:
                raise Superseded
            if status == Status.STOPPING:
                self.halt(Status.STOPPED)
            self._check_storages()
            if not CopyTask.objects.filter(pk=self.task_id, run_id=self.run_id).update(**self.progress(now)):
                raise Superseded
            self._write_failures()

    def _check_storages(self) -> None:
        ids = self.storage_ids
        rows = {row["pk"]: row for row in StorageSource.objects.filter(pk__in=list(ids.values()))
                .values("pk", "enabled", "config_revision")}
        for side in (SOURCE, TARGET):
            row = rows.get(ids[side])
            name = self.names[side]
            if row is None:
                self.halt(Status.FAILED, _("The storage source %(name)s was deleted.") % {"name": name})
            elif not row["enabled"]:
                self.halt(Status.FAILED, _("The storage source %(name)s was disabled.") % {"name": name})
            elif row["config_revision"] != self.revisions.get(ids[side], row["config_revision"]):
                self.halt(Status.FAILED, _("The settings of the storage source %(name)s were changed, so the copying "
                                           "stopped. It can be resumed.") % {"name": name})

    def _write_failures(self) -> None:
        with self.lock:
            failures, self._failures = self._failures, []
        if failures:
            try:
                CopyFailure.objects.bulk_create(failures)
            except BaseException:
                with self.lock:
                    self._failures[:0] = failures
                raise

    def progress(self, now, *, final: bool = False) -> dict:
        """The task's fields of the progress so far."""
        moment = time.monotonic()
        with self.lock:
            states = sorted(self._current.values(), key=lambda state: state.order)
            fields = {
                "files_total": self.files_total, "files_copied": self.files_copied,
                "files_skipped": self.files_skipped, "files_failed": self.files_failed,
                "folders_failed": self.folders_failed, "bytes_total": self.bytes_total,
                "bytes_copied": self.bytes_copied, "bytes_skipped": self.bytes_skipped,
            }
            finished = self.bytes_finished
        current = []
        under_way = 0
        for state in states:
            done = min(state.done, state.size or 0)
            under_way += done
            current.append({"path": state.path, "size": state.size, "done": done, "speed": state.rate(moment, state.read),
                            "attempt": state.attempt, "error": state.error})
        transferred = self.meter.total
        fields.update(
            updated_at=now,
            scanning=not self.scanned and not final,
            bytes_done=finished + (0 if final else under_way),
            transferred=transferred,
            speed=0 if final else self._rate(moment, transferred),
            current=[] if final else current,
        )
        return fields

    def finish(self, now) -> None:
        """Write how the run ended: the reason it stopped, else done, or failed when files failed. Raises Superseded."""
        with self.lock:
            ending = self.ending
            failed, folders = self.files_failed, self.folders_failed
        if ending is not None:
            status, message = ending[0], str(ending[1])
        elif failed or folders:
            status = Status.FAILED
            parts = []
            if failed:
                parts.append(ngettext("%(count)s file couldn't be copied.", "%(count)s files couldn't be copied.",
                                      failed) % {"count": failed})
            if folders:
                parts.append(ngettext("%(count)s folder couldn't be copied, nor anything in it.",
                                      "%(count)s folders couldn't be copied, nor anything in them.",
                                      folders) % {"count": folders})
            message = " ".join(parts)
        else:
            status, message = Status.DONE, ""
        with transaction.atomic():
            updated = CopyTask.objects.filter(pk=self.task_id, run_id=self.run_id).update(
                status=status, message=_clip(message), finished_at=now, **self.progress(now, final=True))
            if not updated:
                raise Superseded
            self._write_failures()


# ------------------------------------------------------------ running one


_active: set[Run] = set()
_active_lock = threading.Lock()


def execute(run: Run) -> None:
    """The run, in this thread (its own, or a test's): see the top. Nothing escapes."""
    with _active_lock:
        _active.add(run)
    try:
        with translation.override(run.language):
            _execute(run)
    except Superseded:
        run.stop.set()
    except Exception:  # noqa: BLE001 - a thread has nobody to tell
        logger.exception("Copy task %s: the run failed", run.task_id)
        run.stop.set()
    finally:
        run.ended.set()
        with _active_lock:
            _active.discard(run)


def _drivers(run: Run):
    """The two storages' drivers, and their config_revision noted; StorageUnavailable says why not."""
    ids = run.storage_ids
    rows = {row.pk: row for row in StorageSource.objects.filter(pk__in=list(ids.values()))}
    drivers = []
    for side in (SOURCE, TARGET):
        row = rows.get(ids[side])
        if row is None:
            raise instances.StorageUnavailable(_("The storage source %(name)s was deleted.") % {"name": run.names[side]})
        run.revisions[row.pk] = row.config_revision
        try:
            drivers.append(instances.get(row))
        except instances.StorageUnavailable as error:
            raise instances.StorageUnavailable(_("%(storage)s: %(error)s") % {"storage": row.name, "error": error.message}) from None
    return drivers


def _execute(run: Run) -> None:
    try:
        source, target = _drivers(run)
    except instances.StorageUnavailable as error:
        run.halt(Status.FAILED, error.message)
        _end(run)
        return
    threads = [run.thread(run.scan, source, target)]
    threads += [run.thread(run.work, source, target) for _n in range(workers(run.threads, source, target))]
    for thread in threads:
        thread.start()
    stopping_since = None
    while not run.idle.wait(TICK):
        if run.stop.is_set():
            stopping_since = stopping_since or time.monotonic()
            if time.monotonic() - stopping_since >= run.grace:
                break  # a thread still waiting for a storage: it ends on its own, and writes nothing
        try:
            run.tick(timezone.now())
        except DatabaseError:
            logger.warning("Copy task %s: couldn't write the progress", run.task_id, exc_info=True)
            _drop_connections()
    _end(run)


def workers(threads: int, source, target) -> int:
    """How many files a run copies at once: `threads`, at most what a storage with a pool of connections can take
    besides the scanner's (each file holds one there, two when that storage is both the source and the target)."""
    for driver in {id(source): source, id(target): target}.values():
        limit = getattr(driver, "max_transfers", None)
        if limit:
            spare = max(1, limit - 1)
            threads = min(threads, max(1, spare // 2) if source is target else spare)
    return threads


def _drop_connections() -> None:
    """After an error of the database: this thread's connections closed, the next query opens a new one (Django only
    does that between requests). Not one inside a transaction: a test's, which runs a run in its own thread."""
    for connection in connections.all(initialized_only=True):
        if not connection.in_atomic_block:
            connection.close()


def _end(run: Run) -> None:
    for attempt in range(3):
        try:
            run.finish(timezone.now())
            return
        except DatabaseError:
            logger.warning("Copy task %s: couldn't write how it ended", run.task_id, exc_info=True)
            _drop_connections()
            time.sleep(1 + attempt)


def _in_thread(run: Run) -> None:
    try:
        execute(run)
    finally:
        connections.close_all()  # this thread's own


def start_thread(run: Run) -> None:
    """Run it in a background thread. Tests replace this with execute(), to do it in place."""
    threading.Thread(target=_in_thread, args=(run,), daemon=True, name=f"copytask-{run.task_id}").start()


def start(task: CopyTask) -> Run:
    """A run of the task (its row says which), in the language active now."""
    _hook_exit()
    run = Run(task, translation.get_language())
    start_thread(run)
    return run


_exit_hooked = False


def _hook_exit() -> None:
    """stop_all() at exit, registered once, with the first run: atexit runs the last registered first, so the runs stop
    before the storages' drivers are closed (apps/storages/apps.py registers that when the apps are ready). The other
    way round, the files under way would fail on closed connections first."""
    global _exit_hooked
    with _active_lock:
        if _exit_hooked:
            return
        _exit_hooked = True
    atexit.register(stop_all)


def new_run_id() -> str:
    return uuid.uuid4().hex


def stop_all(grace: float = EXIT_GRACE) -> None:
    """The program is exiting: every run of this process stops now, as interrupted, and is written so (within about
    `grace` seconds)."""
    with _active_lock:
        runs = list(_active)
    for run in runs:
        run.halt(Status.FAILED, INTERRUPTED, grace=max(0.0, grace - 3))
    deadline = time.monotonic() + grace
    for run in runs:
        run.ended.wait(max(0.0, deadline - time.monotonic()))


# ------------------------------------------------------------ the state the page shows


def is_stale(task: CopyTask, now=None) -> bool:
    """A run that should be going on but stopped writing: the program stopped without saying so."""
    return task.status in (Status.RUNNING, Status.STOPPING) and (now or timezone.now()) - task.updated_at > STALE


def status_of(task: CopyTask, now=None) -> tuple[str, str]:
    """(status, message) as the page shows them: a dead run is a failed one."""
    if is_stale(task, now):
        return Status.FAILED, _("It stopped before it was done; the program may have been restarted. It can be resumed.")
    return task.status, task.message
