"""Upgrading the database to this version of the program.

A new version runs upgrade() when it starts, before it serves anything (config/wsgi.py,
with settings.AUTO_UPGRADE); `manage.py upgrade` runs it by hand. It

1. takes a lock (MySQL: a named lock; SQLite: a file next to the database), so that two
   processes starting together don't both migrate;
2. stops when the database has migrations this program doesn't: a newer version
   upgraded it, and this older one could break it;
3. backs the whole database up into settings.BACKUP_DIR (apps/upgrades/backup.py);
4. applies the migrations. MySQL can't roll back changes to tables, and SQLite rolls back
   only the migration that failed, so when one fails the backup is restored: the database is
   as it was, and the old version runs on it;
5. writes an UpgradeLog row: which version, from which, which migrations, which backup.

A version that has no migrations to apply only writes the row.
"""

import math
import os
import re
import signal
import sqlite3
import threading
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError, connection
from django.db.migrations.exceptions import InconsistentMigrationHistory
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy, ngettext

from config.backends.sqlite3.base import owner_only
from config.version import VERSION

from . import backup
from .models import UpgradeLog

LOCK_TIMEOUT = 600  # seconds to wait while another process upgrades or restores
# ... a step at a time: SQLite and the MySQL driver wait in C, and Ctrl+C takes effect only once they
# return. Whole seconds: MySQL rounds GET_LOCK's timeout.
LOCK_STEP = 1


# The MySQL client library in this program (9.x) logs in with caching_sha2_password, MySQL 8.4's
# default, but not with mysql_native_password (error 2059 "Authentication plugin ..."), which an
# account made for an older MySQL may still use.
NATIVE_PASSWORD_HINT = gettext_lazy(
    "This program can't log in to MySQL with mysql_native_password. Switch this user to caching_sha2_password "
    "with MySQL's admin account: ALTER USER 'username'@'%' IDENTIFIED WITH caching_sha2_password BY 'password';"
)
# Error 2061 "Authentication requires secure connection": MySQL without TLS (8.4 has it on by default)
# and an account with caching_sha2_password that nobody has logged in with since MySQL started or
# its password changed. MySQL takes such a first login only over TLS or its own socket, and keeps
# it in a cache for the logins after (OPS-07).
SECURE_CONNECTION_HINT = gettext_lazy(
    "MySQL has no TLS, and this user has caching_sha2_password (MySQL 8.4's default): after MySQL restarts "
    "or the password changes, the first login must use an encrypted connection. Turn on MySQL's TLS (on by "
    "default in MySQL 8.4); or first log in once as this user with the mysql client on the machine MySQL "
    "runs on, and connecting works after that (after every restart of MySQL, log in like that again)."
)
# Error 2026 "SSL connection error": TLS that [database] tls asks for and can't be had (SEC-08).
TLS_HINT = gettext_lazy(
    "Check tls and ca in [database] of the settings file config.toml: MySQL must have TLS on; ca "
    "must be the certificate of the CA that signed MySQL's server certificate (PEM), readable by "
    "this program; with tls = verify_identity, host must also match the name on the certificate."
)


class UpgradeError(Exception):
    """The program can't run on this database as it is. The message says why and what to do;
    the database is as it was before."""


def say(out, text):
    """A line on `out`: sys.stderr, or a management command's self.stdout."""
    out.write(text if text.endswith("\n") else text + "\n")
    out.flush()


def describe(error):
    text = str(error)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


def unreachable(error):
    """What to say when the database can't even be read."""
    if connection.vendor == "sqlite":
        return gettext(
            "Can't read the database file %(path)s (%(error)s). Check file in [database] of the "
            "settings file config.toml, and that the file and its folder can be read and written."
        ) % {"path": connection.settings_dict["NAME"], "error": describe(error)}
    if "mysql_native_password" in str(error):
        return gettext("Can't connect to the database (%(error)s). %(hint)s") % {"error": describe(error), "hint": NATIVE_PASSWORD_HINT}
    code = error.args[0] if error.args else None
    if code == 2061 and "secure connection" in str(error):
        return gettext("Can't connect to the database (%(error)s). %(hint)s") % {"error": describe(error), "hint": SECURE_CONNECTION_HINT}
    if code == 2026:
        return gettext("Can't connect to the database (%(error)s). %(hint)s") % {"error": describe(error), "hint": TLS_HINT}
    return gettext("Can't read the database (%(error)s). Check host, port, name, user and password in [database] of the settings file config.toml.") % {
        "error": describe(error)}


def which():
    """The database, for messages: MySQL's name, or SQLite's file."""
    return connection.settings_dict["NAME"]


@dataclass
class Status:
    pending: list  # the migrations this program would apply, as "app.name", in order
    newer: list  # applied migrations that this program doesn't have
    last: UpgradeLog | None  # the latest UpgradeLog row
    empty: bool  # a new database, without any tables
    # Tables, but not HFile's: another program's database, e.g. a typo in [database] name or
    # file. A few of their names, else [].
    foreign: list


def own_table():
    """A pattern for the tables a HFile has, finished or not: those of its apps (named after them,
    so a new app is counted without being listed here) and Django's own (django_*)."""
    labels = sorted({config.label for config in apps.get_app_configs()} | {"django"})
    return re.compile(rf"(?:{'|'.join(map(re.escape, labels))})_\w+")


def status():
    """How the database stands compared to this program. Only reads (but a missing SQLite file is made,
    600, as by every connection: config/backends/sqlite3/base.py).

    A database is HFile's once its accounts app is there; before that (the install stopped after
    Django's own first migrations, say), when every table it has could be HFile's. Then the
    installer and the upgrade finish it rather than call it another program's.
    """
    executor = MigrationExecutor(connection)
    loader = executor.loader
    tables = connection.introspection.table_names()
    ours = any(app == "accounts" for app, _name in loader.applied_migrations)
    return Status(
        pending=[f"{m.app_label}.{m.name}" for m, _ in executor.migration_plan(loader.graph.leaf_nodes())],
        newer=_newer(loader),
        last=UpgradeLog.objects.first() if UpgradeLog._meta.db_table in tables else None,
        empty=not tables,
        foreign=foreign_tables(tables, ours),
    )


def foreign_tables(tables, ours):
    """A few of the tables that show the database is another program's, or [] when it is HFile's
    (`ours`: its accounts app is there) or may be one that was never finished."""
    if ours:
        return []
    own = own_table()
    return sorted(name for name in tables if not own.fullmatch(name))[:3]


def foreign_message(current):
    return gettext(
        "The database %(name)s has other tables (%(tables)s and more), but it isn't an HFile database: nothing was "
        "upgraded or changed. Check name (for SQLite, file) in [database] of the settings file config.toml for a typo."
    ) % {"name": which(), "tables": gettext(", ").join(current.foreign)}


# ------------------------------------------------------------- signals

def stop_on_sigterm():
    """SIGTERM (systemd, docker stop) as SystemExit, like Ctrl+C is KeyboardInterrupt: a migration then
    stops where Python can still put the backup back (upgrade() catches it). Otherwise the process
    just ends, and MySQL's database is left half upgraded. On Windows Ctrl+Break, which would end it
    as well. Only the main thread can set it."""
    if threading.current_thread() is threading.main_thread():
        for sig in _STOPPING:
            signal.signal(sig, _exit_on_signal)


def _exit_on_signal(signum, frame):
    raise SystemExit(128 + signum)


# The signals that end a process besides Ctrl+C's SIGINT: on Windows Ctrl+Break's too (serve.py stops on it).
_STOPPING = (signal.SIGTERM, signal.SIGBREAK) if os.name == "nt" else (signal.SIGTERM,)


@contextmanager
def signals_held(out):
    """While a backup is being put back: Ctrl+C and SIGTERM (Ctrl+Break) don't cut it short (that would leave the
    database half restored). Both callers stop right after it anyway."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    caught = []

    def hold(signum, frame):
        if not caught:
            say(out, "\n" + gettext("Got a signal to stop: stopping once the restore is done…"))
        caught.append(signum)

    previous = {sig: signal.signal(sig, hold) for sig in (signal.SIGINT, *_STOPPING)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _number(name):
    """0008_downloads -> 8; 0001_squashed_0008_downloads -> 8, the last one it replaces."""
    match = re.match(r"(\d+)(?:_squashed_(\d+))?", name)
    return int(match.group(2) or match.group(1)) if match else None


def newer_in(applied):
    """Of the migrations `applied` ((app, name) pairs: those in a backup), the ones that come from a
    newer version of this program."""
    return _newer(MigrationLoader(None, ignore_no_migrations=True), applied)


def _newer(loader, applied=None):
    """The applied migrations (the database's, or `applied`) that come from a newer version of this
    program.

    Only the apps this program has count. Migrations of an app it doesn't have belong to
    one that a newer version added (this version never touches its tables) or one that
    was removed. Neither do the leftovers of squashed migrations (after `squashmigrations`
    and deleting the old files): they are numbered no higher than what this program has.
    """
    applied = loader.applied_migrations if applied is None else applied
    known = set(loader.disk_migrations)
    for squashed in loader.replacements.values():
        known.update(squashed.replaces)
    top = {}
    for app, name in known:
        top[app] = max(top.get(app, 0), _number(name) or 0)
    newer = []
    for app, name in applied:
        if app in loader.migrated_apps and (app, name) not in known:
            number = _number(name)
            if number is None or number > top.get(app, 0):
                newer.append(f"{app}.{name}")
    return sorted(newer)


def newer_message(current):
    names = gettext(", ").join(current.newer[:5])
    if len(current.newer) > 5:
        names = ngettext("%(names)s and others (%(count)s in all)", "%(names)s and others (%(count)s in all)", len(current.newer)) % {
            "names": names, "count": len(current.newer)}
    values = {"version": current.last.version if current.last else "", "current": VERSION, "names": names,
              "folder": settings.BACKUP_DIR}
    if current.last and current.last.version != VERSION:  # the version that upgraded it
        return (
            gettext("The database was upgraded by version %(version)s and has migrations this program (%(current)s) "
                    "doesn't have: %(names)s. An older program could break on a newer database, so it stops here.")
            + "\n"
            + gettext("Go back to the newer program; to use this version anyway, first restore the "
                      "database from the backup taken before the upgrade (the restore command). Backups "
                      "are in %(folder)s, with the date and the version upgraded to in their names.")
        ) % values
    return (
        gettext("The database was upgraded by a newer version and has migrations this program (%(current)s) "
                "doesn't have: %(names)s. An older program could break on a newer database, so it stops here.")
        + "\n"
        + gettext("Go back to the newer program; to use this version anyway, first restore the database "
                  "from the backup taken before the upgrade (the restore command). Backups are in "
                  "%(folder)s, with the date and the version upgraded to in their names. If the version "
                  "hasn't changed, this program may have been packaged without some migration files.")
    ) % values


@contextmanager
def lock(out, timeout=LOCK_TIMEOUT):
    """One upgrade or restore at a time per database, even from other processes. Either kind of
    lock goes away by itself when the process holding it dies."""
    if connection.vendor != "sqlite":
        with _mysql_lock(out, timeout):
            yield
    elif connection.is_in_memory_db():  # the tests': no other process can open it
        with _process_lock(out, timeout):
            yield
    else:
        with file_lock(lock_file(), out, timeout):
            yield


def _waiting(out):
    say(out, gettext("Another process is upgrading or restoring this database; waiting for it to finish…"))


def _gave_up(timeout):
    return UpgradeError(
        ngettext("Waited %(seconds)s second, and another process is still upgrading or restoring this database. Try again once it's done.",
                 "Waited %(seconds)s seconds, and another process is still "
                 "upgrading or restoring this database. Try again once it's done.", timeout) % {"seconds": timeout}
    )


def lock_file():
    """SQLite's: next to the database file, hfile.sqlite3.lock."""
    database = Path(connection.settings_dict["NAME"])
    return database.with_name(database.name + ".lock")


@contextmanager
def file_lock(path, out, timeout=LOCK_TIMEOUT):
    """An exclusive lock on the file at `path`, through a SQLite connection to it: SQLite locks a
    file the same way on every system, and the system lets go of it when the process ends. The
    file stays (empty); a program that isn't running holds no lock on it. Only its owner may open it
    (600): anyone who can read it can hold a lock on it, and keep every upgrade and restore waiting."""
    owner_only(path, fix=True)
    try:
        holder = sqlite3.connect(path, timeout=0, isolation_level=None)
    except sqlite3.Error as error:
        raise UpgradeError(
            gettext("The database file's folder %(folder)s doesn't exist or can't be written "
                    "to (%(error)s). Check file in [database] of the settings file config.toml.")
            % {"folder": Path(path).parent, "error": error}
        ) from error
    try:
        try:
            holder.execute("BEGIN EXCLUSIVE")
        except sqlite3.Error as error:
            if getattr(error, "sqlite_errorcode", None) not in _BUSY:
                raise _unusable(path, error) from error
            _waiting(out)  # "database is locked": another process has it
            holder.execute(f"PRAGMA busy_timeout = {LOCK_STEP * 1000}")
            for _ in range(math.ceil(timeout / LOCK_STEP)):
                try:
                    holder.execute("BEGIN EXCLUSIVE")
                    break
                except sqlite3.Error as error:
                    if getattr(error, "sqlite_errorcode", None) not in _BUSY:
                        raise _unusable(path, error) from error
            else:
                raise _gave_up(timeout)
        # A file this process may not write is opened read-only, and "locked" then without keeping
        # anybody out (left behind by a run as another user, root say). A write shows which it is;
        # it is never committed, so the file stays empty.
        try:
            holder.execute("PRAGMA user_version = 1")
        except sqlite3.Error as error:
            raise _unusable(path, error) from error
        yield
    finally:
        holder.close()  # ends the transaction, and so the lock


_BUSY = {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}


def _unusable(path, error):
    return UpgradeError(
        gettext("The lock file %(path)s can't be used (%(error)s). It only keeps two processes from upgrading "
                "or restoring at the same time and holds no data: it may be left from a run as another user "
                "(root, say). Delete it, or make it writable by the user running the site, and try again.")
        % {"path": path, "error": error}
    )


_memory_lock = threading.Lock()


@contextmanager
def _process_lock(out, timeout):
    if not _memory_lock.acquire(blocking=False):
        _waiting(out)
        if not _memory_lock.acquire(timeout=timeout):
            raise _gave_up(timeout)
    try:
        yield
    finally:
        _memory_lock.release()


@contextmanager
def _mysql_lock(out, timeout):
    """A MySQL named lock: per database, even from other machines. It goes away with the
    connection if the process dies."""
    name = f"hfile-upgrade-{connection.settings_dict['NAME']}"[:64]
    with connection.cursor() as cursor:
        cursor.execute("SELECT GET_LOCK(%s, 0)", [name])
        if cursor.fetchone()[0] != 1:
            _waiting(out)
            for _ in range(math.ceil(timeout / LOCK_STEP)):
                cursor.execute("SELECT GET_LOCK(%s, %s)", [name, LOCK_STEP])
                if cursor.fetchone()[0] == 1:
                    break
            else:
                raise _gave_up(timeout)
    try:
        yield
    finally:
        with suppress(DatabaseError), connection.cursor() as cursor:  # gone with the connection
            cursor.execute("SELECT RELEASE_LOCK(%s)", [name])


def upgrade(out, lock_timeout=LOCK_TIMEOUT):
    """Brings the database up to this version of the program, as described at the top.
    Returns the UpgradeLog row it wrote, or None when this version had already started on
    this database.

    Raises UpgradeError when it can't. The database is then as it was before.
    """
    with lock(out, lock_timeout):
        current = status()  # under the lock: another process may have just upgraded
        if current.foreign:
            raise UpgradeError(foreign_message(current))
        if current.newer:
            raise UpgradeError(newer_message(current))
        previous = current.last.version if current.last else ""
        changed = previous and previous != VERSION  # from another version: said in parentheses
        values = {"version": VERSION, "previous": previous, "count": len(current.pending)}
        if not current.pending:
            if previous == VERSION:
                return None
            row = _record(out, previous, [], None, timezone.now())
            if changed:
                say(out, gettext("HFile %(version)s starts on this database for the first time (it was %(previous)s); the database needs no changes.") % values)
            else:
                say(out, gettext("HFile %(version)s starts on this database for the first time; the database needs no changes.") % values)
            return row
        try:
            MigrationLoader(connection).check_consistent_history(connection)
        except InconsistentMigrationHistory as error:
            raise UpgradeError(gettext("The database's migration history is inconsistent; nothing was upgraded: %(error)s") % {"error": error}) from error

        started = timezone.now()
        if current.empty:
            saved = None
            say(out, ngettext("A new, empty database (%(name)s): creating the tables (%(count)s migration)…", "A new, empty database (%(name)s): creating the tables (%(count)s migrations)…",
                              len(current.pending)) % {**values, "name": which()})
        else:
            saved = _back_up(out)
            if changed:
                say(out, ngettext("Upgrading the database to %(version)s (from %(previous)s): %(count)s migration…",
                                  "Upgrading the database to %(version)s (from %(previous)s): %(count)s migrations…", len(current.pending)) % values)
            else:
                say(out, ngettext("Upgrading the database to %(version)s: %(count)s migration…", "Upgrading the database to %(version)s: %(count)s migrations…",
                                  len(current.pending)) % values)
        try:
            # A command's self.stdout ends every write with a line break; migrate's "Applying x..." needs none.
            stream = getattr(out, "_out", out)
            call_command("migrate", interactive=False, verbosity=1, stdout=stream, stderr=stream)
        except BaseException as error:  # Ctrl+C too: never leave it half upgraded
            _roll_back(out, saved, error, previous)
        row = _record(out, previous, current.pending, saved, started)
        seconds = f"{(timezone.now() - started).total_seconds():.1f}"
        say(out, gettext("The database is upgraded to %(version)s, in %(seconds)s seconds.") % {"version": VERSION, "seconds": seconds})
        return row


def _back_up(out):
    path = backup.new_path(f"before-{VERSION}")
    say(out, gettext("Backing up the whole database first: %(path)s") % {"path": path})
    try:
        tables = backup.dump(path)
    except (OSError, DatabaseError) as error:
        raise UpgradeError(
            gettext("The backup failed; nothing was upgraded, the database is unchanged: %(error)s") % {"error": describe(error)}
            + "\n"
            + gettext("Backups go to %(folder)s; [paths] backups in the settings file "
                      "config.toml can set another folder, one that can be written to.") % {"folder": path.parent}
        ) from error
    say(out, ngettext("Backed up %(count)s table (%(size)s).", "Backed up %(count)s tables (%(size)s).", tables) % {
        "count": tables, "size": backup.size_text(path)})
    return path


def _roll_back(out, saved, error, previous):
    """After a failed migration: puts the database back as it was, then raises UpgradeError."""
    what = gettext("The upgrade was interrupted.") if isinstance(error, KeyboardInterrupt) else gettext("The upgrade failed: %(error)s") % {"error": describe(error)}
    say(out, "\n" + what + "\n" + gettext("Restoring the database to before the upgrade…"))  # migrate may have stopped mid-line
    try:
        with signals_held(out):
            if saved:
                backup.restore(saved)
            else:
                backup.drop_tables()  # it was empty
    except Exception as again:
        if saved:
            then = gettext("The database may now be only half upgraded, and neither this version "
                           "nor the previous one can use it. Once the problem is fixed, restore "
                           "the backup from before the upgrade with the restore command: %(path)s")
        else:
            then = gettext("The database may now be only half upgraded, and neither this version nor the previous one "
                           "can use it. Once the problem is fixed, drop all the tables in the database and start again.")
        raise UpgradeError(
            what + "\n" + gettext("Restoring failed too: %(error)s") % {"error": describe(again)} + "\n" + then % {"path": saved}
        ) from error
    if saved and previous:
        back = gettext("The database is restored to before the upgrade; the previous version "
                       "(%(previous)s) can go on using it. Please send the output above to the developers.")
    elif saved:
        back = gettext("The database is restored to before the upgrade; the previous version "
                       "can go on using it. Please send the output above to the developers.")
    else:
        back = gettext("The database is empty again. Please send the output above to the developers.")
    raise UpgradeError(what + "\n" + back % {"previous": previous}) from error


def _record(out, previous, migrations, saved, started):
    row = UpgradeLog(
        version=VERSION,
        previous=previous,
        migrations=migrations,
        backup=str(saved or ""),
        started_at=started,
        finished_at=timezone.now(),
    )
    try:
        row.save()
    except DatabaseError as error:  # the upgrade itself went through; only its record is missing
        say(out, gettext("Couldn't write the upgrade record: %(error)s") % {"error": describe(error)})
    return row
