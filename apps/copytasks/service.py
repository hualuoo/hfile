"""What /manage/#copy-tasks does (apps/copytasks/views.py): making a task, stopping one, resuming one, and the tasks
as the page shows them. Admins only; a task reads and writes the storages through their drivers, as the admin
pages do, so the users' permissions, filter rules and folder passwords don't apply to it.
"""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from rest_framework.exceptions import NotFound, ValidationError

from apps.activity import log as activity
from apps.core.exceptions import Conflict
from apps.core.times import iso
from apps.storages import config as storage_config, instances
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError
from apps.storages.drivers.base import NotFound as Missing
from apps.storages.models import StorageSource

from . import runner
from .models import CopyTask

Status = CopyTask.Status

NOT_ENABLED = gettext_lazy("This storage source is disabled; enable it first.")
NOT_WRITABLE = gettext_lazy("Files can't be copied into this storage source: it is read-only, or can't take uploads.")
NO_STORAGE = gettext_lazy("There is no such storage source.")
RESET = {  # a new run's progress
    "scanning": True, "files_total": 0, "files_copied": 0, "files_skipped": 0, "files_failed": 0, "folders_failed": 0,
    "bytes_total": 0, "bytes_copied": 0, "bytes_skipped": 0, "bytes_done": 0, "transferred": 0, "speed": 0,
    "current": [], "message": "", "finished_at": None,
}


def writable(storage: StorageSource) -> bool:
    """Whether a task can copy into it: its type can be written to and take files (not read-only, not FebBox)."""
    try:
        cls = storage_config.driver(storage.type)
    except storage_config.ConfigError:
        return False
    return bool(cls.can_write and cls.can_upload)


def storage_choices() -> list[dict]:
    """The storage sources a task can copy from (all enabled ones) and into (`writable`)."""
    return [{"id": storage.pk, "name": storage.name, "key": storage.key, "type_label": storage.type_label,
             "writable": writable(storage)} for storage in StorageSource.objects.filter(enabled=True)]


def _storage(pk, field):
    storage = StorageSource.objects.filter(pk=pk).first()
    if storage is None:
        raise ValidationError({field: [NO_STORAGE]})
    if not storage.enabled:
        raise ValidationError({field: [NOT_ENABLED]})
    return storage


def _driver(storage, field):
    try:
        return instances.get(storage)
    except instances.StorageUnavailable as error:
        raise ValidationError({field: [error.message]}) from None


def folders(storage_id, path) -> dict:
    """The folder picker: the folders in `path` of an enabled storage, by name."""
    storage = StorageSource.objects.filter(pk=storage_id, enabled=True).first()
    if storage is None:
        raise NotFound(NO_STORAGE)
    driver = _driver(storage, "storage")
    try:
        entries = driver.list(path)
    except Missing:
        raise NotFound(_("This folder doesn't exist.")) from None
    except DriverError as error:
        raise Conflict(error.message) from None
    names = sorted((entry.name for entry in entries if entry.is_dir), key=lambda name: (name.casefold(), name))
    return {"storage": storage.pk, "path": path, "folders": names}


def destination(source_path: str, target_path: str) -> str:
    """Where a task copies what is in the source folder: the folder of its name in the target folder picked, so that
    the source folder itself is copied (from the root, which has no name, straight into the folder picked). Worked out
    when the task is made: it is the task's target_path, and a 继续 goes on in the same folder."""
    if source_path == paths.ROOT:
        return target_path
    return paths.join(target_path, paths.name(source_path))


def _entry(driver, path):
    """What the target has at `path`, or None: nothing yet (the task makes it)."""
    try:
        return driver.stat(path)
    except Missing:
        return None
    except DriverError as error:
        raise ValidationError({"target_path": [error.message]}) from None


def _check_folders(source, source_path, target, target_path) -> None:
    """The source folder is a folder, of a name the target can have; the target folder is one, or isn't there yet
    (the task makes it), and so is the folder of that name in it (destination())."""
    source_driver, target_driver = _driver(source, "source"), _driver(target, "target")
    try:
        entry = source_driver.stat(source_path)
    except Missing:
        raise ValidationError({"source_path": [_("This folder doesn't exist.")]}) from None
    except DriverError as error:
        raise ValidationError({"source_path": [error.message]}) from None
    if not entry.is_dir:
        raise ValidationError({"source_path": [_("This is a file, not a folder.")]})
    name = paths.name(source_path)  # "" for the root: what is in it goes straight into the target folder
    problem = runner.name_problem(target_driver, name) if name else ""
    if problem:
        raise ValidationError({"source_path": [_("The target storage source can't take this folder's name: %(error)s")
                                               % {"error": problem}]})
    if target_path != paths.ROOT:
        entry = _entry(target_driver, target_path)
        if entry is None:
            return  # the task makes it, and the folder in it
        if not entry.is_dir:
            raise ValidationError({"target_path": [_("This is a file, not a folder.")]})
    entry = _entry(target_driver, destination(source_path, target_path)) if name else None
    if entry is not None and not entry.is_dir:
        raise ValidationError({"target_path": [_("This folder has a file of the same name as the source folder "
                                                 "(%(name)s).") % {"name": name}]})


def create(data, user) -> CopyTask:
    """A new task from the create serializer's data, made by `user` and started at once (in the request's
    language)."""
    source = _storage(data["source"], "source")
    target = _storage(data["target"], "target")
    if not writable(target):
        raise ValidationError({"target": [NOT_WRITABLE]})
    source_path, target_path = data["source_path"], data["target_path"]
    into = destination(source_path, target_path)
    if source.pk == target.pk and (paths.is_inside(into, source_path) or paths.is_inside(source_path, into)):
        raise ValidationError({"target_path": [_("In the same storage source, neither the source folder nor its copy "
                                                 "(%(path)s) can be the other one or inside it.") % {"path": into}]})
    _check_folders(source, source_path, target, target_path)
    now = timezone.now()
    with transaction.atomic():
        task = CopyTask.objects.create(
            created_by=user, created_by_name=user.get_username(),
            source=source, source_name=source.name, source_path=source_path,
            target=target, target_name=target.name, target_path=into,
            threads=data["threads"], speed_limit=data["speed_limit"], same_file=data["same_file"],
            same_name=data["same_name"], retries=data["retries"],
            status=Status.RUNNING, run=1, run_id=runner.new_run_id(), started_at=now, updated_at=now,
        )
        _log(activity.Kind.CREATE_COPY_TASK, task, {
            "threads": task.threads, "speed_limit": task.speed_limit, "same_file": task.same_file,
            "same_name": task.same_name, "retries": task.retries})
    runner.start(task)
    return task


def _log(kind, task, detail=None) -> None:
    activity.record(kind, storage=task.source, path=task.source_path, to_path=task.target_path,
                    detail={"task": task.pk, "to_storage": task.target_name, "to_storage_id": task.target_id,
                            **(detail or {})})


def _locked(pk) -> CopyTask:
    task = CopyTask.objects.select_for_update().filter(pk=pk).first()
    if task is None:
        raise NotFound(_("There is no such copy task."))
    return task


def stop(pk) -> CopyTask:
    """Stop a running task: its run sees it within a second and stops copying; what it copied stays."""
    with transaction.atomic():
        task = _locked(pk)
        status, _message = runner.status_of(task)
        if status == Status.STOPPING:
            return task
        if status != Status.RUNNING:
            raise Conflict(_("This task isn't running."))
        task.status = Status.STOPPING
        task.save(update_fields=["status"])
        _log(activity.Kind.STOP_COPY_TASK, task)
    return task


def resume(pk) -> CopyTask:
    """Run a task that failed or was stopped again: the files already in the target with the same name and size
    are skipped, the others copied (in the request's language)."""
    now = timezone.now()
    with transaction.atomic():
        task = _locked(pk)
        status, _message = runner.status_of(task, now)
        if status not in (Status.FAILED, Status.STOPPED):
            raise Conflict(_("Only a task that failed or was stopped can be resumed."))
        for storage, name in ((task.source, task.source_name), (task.target, task.target_name)):
            if storage is None:
                raise Conflict(_("The storage source %(name)s was deleted.") % {"name": name})
            if not storage.enabled:
                raise Conflict(_("The storage source %(name)s is disabled; enable it first.") % {"name": storage.name})
        if not writable(task.target):
            raise Conflict(NOT_WRITABLE)
        for field, value in RESET.items():
            setattr(task, field, value)
        task.status = Status.RUNNING
        task.run += 1
        task.run_id = runner.new_run_id()
        task.started_at = task.updated_at = now
        task.save()
        task.failures.all().delete()
        _log(activity.Kind.RESUME_COPY_TASK, task, {"run": task.run})
    runner.start(task)
    return task


# ------------------------------------------------------------ the tasks as the page shows them


def _storage_of(storage, name):
    return {"id": storage.pk if storage is not None else None, "name": storage.name if storage is not None else name,
            "key": storage.key if storage is not None else ""}


def summary(task: CopyTask, now) -> dict:
    status, message = runner.status_of(task, now)
    live = status in (Status.RUNNING, Status.STOPPING)
    end = task.finished_at or (now if live else task.updated_at)
    seconds = max((end - task.started_at).total_seconds(), 0)
    return {
        "id": task.pk,
        "created_at": iso(task.created_at),
        "created_by": {"id": task.created_by_id, "username": task.created_by_name} if task.created_by_name else None,
        "source": _storage_of(task.source, task.source_name),
        "source_path": task.source_path,
        "target": _storage_of(task.target, task.target_name),
        "target_path": task.target_path,
        "threads": task.threads,
        "speed_limit": task.speed_limit,
        "same_file": task.same_file,
        "same_name": task.same_name,
        "retries": task.retries,
        "status": status,
        "status_label": str(Status(status).label),
        "message": message,
        "run": task.run,
        "started_at": iso(task.started_at),
        "finished_at": iso(task.finished_at) if not live else None,
        "seconds": int(seconds),
        "scanning": task.scanning and live,
        "files": {"total": task.files_total, "copied": task.files_copied, "skipped": task.files_skipped,
                  "failed": task.files_failed},
        "folders_failed": task.folders_failed,
        "bytes": {"total": task.bytes_total, "copied": task.bytes_copied, "skipped": task.bytes_skipped,
                  "done": min(task.bytes_done, task.bytes_total) if task.bytes_total else task.bytes_done},
        "transferred": task.transferred,
        "speed": task.speed if live else 0,
        "average_speed": int(task.transferred / seconds) if seconds >= 1 else 0,
        "can_stop": status == Status.RUNNING,
        "can_resume": status in (Status.FAILED, Status.STOPPED),
    }


def detail(task: CopyTask, now) -> dict:
    """summary() with the files being copied and how many failed."""
    data = summary(task, now)
    live = data["status"] in (Status.RUNNING, Status.STOPPING)
    data["current"] = task.current if live else []
    data["failures_count"] = task.failures.count()
    return data
