"""The slow parts of /manage/#ip-geo, run in a background thread so the page doesn't wait for them:
downloading the database (about 50 MB) and 补全 (looking stored IPs up again).

One job at a time for the whole site: claim() takes the job fields of IpGeoSettings in a transaction
and raises Busy while another job runs. The thread writes its progress there (job_done of job_total:
bytes, or addresses) about twice a second, so the page can follow it (GET manage/ip-geo/job/). A running
job whose progress hasn't moved for STALE, because the program stopped half way, counts as failed, and a
new one may start. The thread speaks the language of the admin who started it: its messages are stored.

ip.adysec.com publishes no checksum, so updating downloads the whole file again; one the same as the file
in use changes nothing. A download replaces nothing until it has passed two checks: a size limit, and
opening it as a city database (lookup.open_reader), which a file cut short fails. It is written next to its
final name and then renamed over it.

A download of new data and a 补全 that changed something are logged in the activity log when they end, as the
admin who started them did it, from where they did (`who`, apps.activity.log's actor, ip and user_agent).
"""

import hashlib
import http.client
import logging
import os
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from itertools import batched
from pathlib import Path
from urllib.parse import urlsplit

from django.apps import apps
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone, translation
from django.utils.translation import gettext as _, gettext_lazy, ngettext, pgettext

from config.version import VERSION

from . import lookup
from .models import IpGeoSettings
from .sources import FILE_NAME, HOST, URL

logger = logging.getLogger(__name__)

Job = IpGeoSettings.Job
State = IpGeoSettings.JobState

STALE = timedelta(minutes=3)  # a running job that hasn't written progress for this long has died
PROGRESS_EVERY = 0.5  # seconds between progress writes
READ_TIMEOUT = 30  # seconds without data before a download gives up
DEADLINE = 30 * 60  # seconds for a whole download
MAX_BYTES = 256 * 1024 * 1024  # the file is about 50 MB
CHUNK = 64 * 1024
FILL_CHUNK = 200  # addresses per UPDATE
PART_PREFIX = ".download-"
USER_AGENT = f"HFile/{VERSION}"
JOB_FIELDS = [
    "job_id",
    "job",
    "job_state",
    "job_done",
    "job_total",
    "job_message",
    "job_started_at",
    "job_updated_at",
    "job_finished_at",
]


class Busy(Exception):
    """Another job is running."""


class NotReady(Exception):
    """IP 归属地 is off, or there is no database for what was asked."""


class JobFailed(Exception):
    """Ends a job with this message, which the page shows."""


class Superseded(Exception):
    """A newer job took the job fields over (this one looked dead): stop without writing anything."""


# ------------------------------------------------------------ the job fields

BUSY = {
    Job.DOWNLOAD: gettext_lazy("A database is being downloaded. Try again when that is done."),
    Job.FILL: gettext_lazy("Locations are being filled in. Try again when that is done."),
}


def is_running(config, now=None):
    if config.job_state != State.RUNNING or config.job_updated_at is None:
        return False
    return (now or timezone.now()) - config.job_updated_at <= STALE


def job_status(config, now=None):
    """The last job as the page shows it, or None before the first one."""
    if not config.job:
        return None
    state, message = config.job_state, config.job_message
    if state == State.RUNNING and not is_running(config, now):
        state = State.FAILED
        message = _("It stopped before it was done; the program may have been restarted. It can be started again.")
    return {
        "id": config.job_id,
        "kind": config.job,
        "kind_label": config.get_job_display(),
        "state": state,
        "done": config.job_done,
        "total": config.job_total,
        "message": message,
        "started_at": config.job_started_at,
        "finished_at": config.job_finished_at,
    }


def claim(kind):
    """Start a job of `kind`: its fields reset and marked running. Returns the settings row; raises Busy."""
    IpGeoSettings.load()  # the row exists
    now = timezone.now()
    with transaction.atomic():
        config = IpGeoSettings.objects.select_for_update().get(pk=1)
        if is_running(config, now):
            raise Busy(str(BUSY.get(config.job, BUSY[kind])))
        config.job_id = uuid.uuid4().hex
        config.job = kind
        config.job_state = State.RUNNING
        config.job_done = config.job_total = 0
        config.job_message = ""
        config.job_started_at = config.job_updated_at = now
        config.job_finished_at = None
        config.save(update_fields=JOB_FIELDS)
    return config


class Progress:
    """A running job's fields. Every write checks that the job is still this one."""

    def __init__(self, job_id):
        self.job_id = job_id
        self.done = 0
        self.total = 0
        self._written = time.monotonic()

    def _write(self, **fields):
        fields["job_updated_at"] = timezone.now()
        if not IpGeoSettings.objects.filter(pk=1, job_id=self.job_id).update(**fields):
            raise Superseded

    def tick(self, *, done=None, total=None, force=False):
        if done is not None:
            self.done = done
        if total is not None:
            self.total = total
        now = time.monotonic()
        if force or now - self._written >= PROGRESS_EVERY:
            self._written = now
            self._write(job_done=self.done, job_total=self.total)

    def finish(self, state, message, **fields):
        self._write(
            job_state=state,
            job_message=message[: IpGeoSettings._meta.get_field("job_message").max_length],
            job_done=self.done,
            job_total=self.total,
            job_finished_at=timezone.now(),
            **fields,
        )


def run(work, job_id, *args):
    """Run `work(progress, *args)` and write how it ended; nothing escapes (a thread has nobody to tell).

    `work` returns the message for a job that went well; it finishes the job itself when the result
    goes together with other changes (Progress.finish with the fields it changes).
    """
    progress = Progress(job_id)
    try:
        message = work(progress, *args)
        if message is not None:
            progress.finish(State.DONE, message)
    except Superseded:
        return
    except Exception as exc:
        if not isinstance(exc, JobFailed):
            logger.exception("IP location: the job failed")
        try:
            progress.finish(State.FAILED, str(exc) if isinstance(exc, JobFailed) else _("Something went wrong: %(error)s") % {"error": exc})
        except Superseded:
            pass


def _in_thread(language, work, job_id, *args):
    try:
        with translation.override(language):
            run(work, job_id, *args)
    finally:
        connections.close_all()  # this thread's own


def start_thread(work, job_id, *args):
    """Run the job in a background thread, in the language active now. Tests replace this with run() to do
    it in place."""
    threading.Thread(target=_in_thread, args=(translation.get_language(), work, job_id, *args), name="ipgeo-job",
                     daemon=True).start()


def _ready_config():
    config = IpGeoSettings.load()
    if not config.enabled:
        raise NotReady(_("Turn IP location on first."))
    return config


# ------------------------------------------------------------ downloading a database


class _SourceRedirects(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to https on sources.HOST, the only host a download goes to; any other answers
    as the error it is (e.g. "answered 302"). urllib on its own would go anywhere, http and ftp too."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.hostname != HOST:
            return None
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SourceRedirects)


def fetch(request):
    """Open an address (tests replace this). The https_proxy environment variable, if set, is followed."""
    return _opener.open(request, timeout=READ_TIMEOUT)


def started_by(request):
    """`who` of a job: the activity log's actor, ip and user_agent of the request starting it."""
    from apps.core.net import client_ip

    return {"actor": request.user, "ip": client_ip(request), "user_agent": request.META.get("HTTP_USER_AGENT", "")}


def _log(kind, who, detail):
    from apps.activity import log as activity

    activity.record(kind, **(who or {"actor": None, "ip": None, "user_agent": ""}), detail=detail)


def start_download(who=None):
    """Download the database (or a newer one), in the background. Raises NotReady or Busy."""
    _ready_config()
    config = claim(Job.DOWNLOAD)
    start_thread(download, config.job_id, who)


def _open(url):
    host = urlsplit(url).hostname
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        return fetch(request)
    except urllib.error.HTTPError as exc:
        raise JobFailed(_("%(host)s answered %(status)s; the file wasn't downloaded. Try again later.")
                        % {"host": host, "status": exc.code}) from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            reason = _("no answer in %(seconds)s seconds") % {"seconds": READ_TIMEOUT}
        raise JobFailed(_("Couldn't connect to %(host)s (%(reason)s). Try again later.") % {"host": host, "reason": reason}) from None
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        raise JobFailed(_("Couldn't connect to %(host)s (%(reason)s). Try again later.") % {"host": host, "reason": exc}) from None


def _read(response, size):
    """Up to `size` bytes, as soon as some have come (read() would wait for all of them, and a slow
    server would then go minutes without the progress moving, so that the job looks dead)."""
    try:
        return getattr(response, "read1", response.read)(size)
    except TimeoutError:
        raise JobFailed(_("The download broke off: no data for %(seconds)s seconds. Try again.") % {"seconds": READ_TIMEOUT}) from None
    except (OSError, http.client.HTTPException) as exc:
        raise JobFailed(_("The download broke off (%(error)s). Try again.") % {"error": exc}) from None


def _length(response):
    try:
        length = int(response.headers.get("Content-Length") or 0)
    except (TypeError, ValueError):
        return 0
    return max(length, 0)


def _fetch_file(url, folder, progress):
    """Download into a temporary file in `folder`: (path, size, sha256)."""
    deadline = time.monotonic() + DEADLINE
    limit = MAX_BYTES // 1048576
    with _open(url) as response:
        total = _length(response)
        if total > MAX_BYTES:
            raise JobFailed(_("The file is %(size)s MB, over the limit of %(limit)s MB; it wasn't downloaded.")
                            % {"size": f"{total / 1048576:.1f}", "limit": limit})
        progress.tick(done=0, total=total, force=True)
        digest = hashlib.sha256()
        size = 0
        handle = tempfile.NamedTemporaryFile(dir=folder, prefix=PART_PREFIX, suffix=".part", delete=False)
        try:
            with handle:
                while chunk := _read(response, CHUNK):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise JobFailed(_("The file is over the limit of %(limit)s MB; the download was stopped.") % {"limit": limit})
                    if time.monotonic() > deadline:
                        raise JobFailed(_("The download is too slow: not done after %(minutes)s minutes. Try again "
                                          "later.") % {"minutes": DEADLINE // 60})
                    digest.update(chunk)
                    handle.write(chunk)
                    progress.tick(done=size)
                handle.flush()
                os.fsync(handle.fileno())  # on the disk before it replaces the file in use
            if total and size != total:
                raise JobFailed(_("The download is incomplete: %(got)s bytes came of %(size)s. Please try again.")
                                % {"got": size, "size": total})
            progress.tick(done=size, force=True)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
    return Path(handle.name), size, digest.hexdigest()


def _sha256_of(path):
    """The file's SHA-256, or None when it can't be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while block := handle.read(CHUNK):
                digest.update(block)
    except OSError:
        return None
    return digest.hexdigest()


def _sync_folder(folder):
    """After os.replace(): the new name on the disk too. Windows can't open a folder; it doesn't need to."""
    try:
        descriptor = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def data_date(moment):
    return timezone.localtime(moment).date().isoformat() if moment else pgettext("date", "unknown")


def download(progress, who=None):
    """The download job (see the top). Finishes the job itself, together with the database's fields."""
    folder = Path(settings.GEOIP_DIR)
    final = folder / FILE_NAME
    try:
        folder.mkdir(parents=True, exist_ok=True)
        for leftover in folder.glob(PART_PREFIX + "*"):  # from a job that died half way
            leftover.unlink(missing_ok=True)
    except OSError as exc:
        raise JobFailed(_("Can't write files in %(folder)s (%(error)s).") % {"folder": folder, "error": exc}) from None
    part, size, digest = _fetch_file(URL, folder, progress)
    try:
        config = IpGeoSettings.load()
        # The file in use is checked too: a damaged one is replaced even by the same download.
        if config.sha256 == digest and _sha256_of(final) == digest:
            return _("Already up to date (data of %(date)s); nothing changed.") % {"date": data_date(config.built_at)}
        try:
            reader = lookup.open_reader(part)
        except lookup.DatabaseProblem as exc:
            raise JobFailed(_("The downloaded file can't be used: %(problem)s") % {"problem": exc}) from None
        built_at = datetime.fromtimestamp(reader.metadata().build_epoch, dt_timezone.utc)
        progress.tick(force=True)  # still this job's (Superseded otherwise): only then the file in use is replaced
        os.replace(part, final)
        _sync_folder(folder)
    finally:
        part.unlink(missing_ok=True)

    from apps.activity import log as activity

    with transaction.atomic():
        progress.finish(
            State.DONE,
            _("Downloaded: data of %(date)s.") % {"date": data_date(built_at)},
            file_name=FILE_NAME,
            sha256=digest,
            size=size,
            built_at=built_at,
            downloaded_at=timezone.now(),
        )
        _log(activity.Kind.DOWNLOAD_IP_GEO, who, {"built_at": built_at.isoformat()})
    logger.info("IP location database downloaded: %s, data of %s, from %s", FILE_NAME, data_date(built_at), HOST)
    lookup.forget_cached()
    return None


# ------------------------------------------------------------ 补全: filling in stored locations


@dataclass(frozen=True)
class Target:
    key: str
    label: str
    model: str
    ip: str
    location: str

    def rows(self, redo=False):
        """Rows with an IP; without `redo` only those that have no location yet."""
        rows = apps.get_model(self.model)._default_manager.filter(**{f"{self.ip}__isnull": False})
        return rows if redo else rows.filter(**{self.location: ""})


TARGETS = (
    Target("users", gettext_lazy("Users' last login IPs"), "accounts.User", "last_login_ip", "last_login_location"),
    Target("activity", gettext_lazy("Activity log"), "activity.ActivityLog", "ip", "ip_location"),
    Target("downloads", gettext_lazy("Download log"), "links.DownloadLog", "ip", "ip_location"),
)


def unfilled():
    """The rows that have an IP but no location: {"total", "parts": [{"key", "label", "count"}]}."""
    parts = [{"key": t.key, "label": str(t.label), "count": t.rows().count()} for t in TARGETS]
    return {"total": sum(part["count"] for part in parts), "parts": parts}


def start_fill(redo=False, who=None):
    """补全 in the background: every stored IP without a location (with `redo`, every stored IP) looked up
    in the database in use. Raises NotReady or Busy."""
    config = _ready_config()
    if lookup.locator(config) is None:
        raise NotReady(_("There is no usable database yet; download one first."))
    config = claim(Job.FILL)
    start_thread(fill, config.job_id, redo, who)


def fill(progress, redo=False, who=None):
    """The 补全 job. Each address is looked up once (a login's IP is in the activity log and on the user too),
    then each table gets one UPDATE per location and FILL_CHUNK addresses. Only the location columns
    change. The progress counts the addresses looked up."""
    config = IpGeoSettings.load()
    found = lookup.locator(config) if config.enabled else None
    if found is None:
        raise JobFailed(_("No usable database: turn IP location on, then download a database."))
    plans = [(target, set(target.rows(redo).order_by().values_list(target.ip, flat=True).distinct())) for target in TARGETS]
    addresses = sorted(set().union(*(wanted for _target, wanted in plans)))
    progress.tick(total=len(addresses), force=True)
    located, names = {}, {}
    for address in addresses:
        located[address], place_names = found.place(address)
        names.update(place_names)
        progress.tick(done=progress.done + 1)  # written every PROGRESS_EVERY seconds
    lookup.remember(names)  # the pages show the names of what the records get

    changed = 0
    for target, wanted in plans:
        groups = defaultdict(list)
        for address in sorted(wanted):
            groups[located[address]].append(address)
        for location, members in groups.items():
            if not location:  # "" when the lookup failed: left for another time
                continue
            for chunk in batched(members, FILL_CHUNK):
                rows = target.rows(redo).filter(**{f"{target.ip}__in": chunk})
                if redo:
                    rows = rows.exclude(**{target.location: location})
                changed += rows.update(**{target.location: location})
                progress.tick()  # still alive while the tables are written

    if redo:
        looked = ngettext("Looked up %(count)s IP again", "Looked up %(count)s IPs again", len(addresses)) % {"count": f"{len(addresses):,}"}
        result = ngettext("the location of %(count)s record changed.", "the locations of %(count)s records changed.", changed) % {"count": f"{changed:,}"}
        message = _("%(looked)s; %(result)s") % {"looked": looked, "result": result}
    elif changed:
        message = ngettext("Filled in the location of %(count)s record.", "Filled in the locations of %(count)s records.", changed) % {"count": f"{changed:,}"}
    else:
        message = _("There was nothing to fill in.")
    if not changed:
        return message
    from apps.activity import log as activity

    logger.info("IP location: %s", message)
    with transaction.atomic():
        progress.finish(State.DONE, message)
        _log(activity.Kind.FILL_IP_GEO, who, {"changed": changed, "redo": redo, "addresses": len(addresses)})
    return None
