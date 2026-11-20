"""What the installer does once the form is filled in (views.py).

1. preflight(): checks the database that was chosen, without touching anything but it.
   SQLite: makes the file (and its folder), only its owner may read it, and checks that it can
   be written. MySQL: connects with what was typed in, checks the server's version, and creates
   the database when it's missing. Either way the database has to be empty, or hold an HFile
   that was never finished (tables but no account). The page's 测试连接 and 下一步 do the same
   as a dry run (views.CheckView), which reads only and leaves everything as it was.
2. install(): points the site's database connection at it, creates the tables
   (apps.upgrades.service.upgrade, the same as an upgrade, which empties the database again if a
   migration fails), then the first admin and the site's name.

The view then writes config.toml and the program starts again (restart_soon), now with the
settings from it.

Only in setup mode: until then settings.DATABASES["default"] is Django's dummy backend, and
nothing in this process has used it (no page of the installer reads the database).
use_database() fills it in place with the database that was chosen, so the connections made
from then on go to it.
"""

import io
import itertools
import os
import re
import sqlite3
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import DEFAULT_DB_ALIAS, DatabaseError, connections, transaction
from django.utils.translation import gettext as _, ngettext

from config import cli, configfile, database, private
from apps.activity import log as activity
from apps.siteconf.models import SiteSettings
from apps.upgrades import service as upgrades

CONNECT_TIMEOUT = 8  # seconds


class InstallError(Exception):
    """Something the person installing has to fix. `field` is the form field it is about, if one."""

    def __init__(self, message, field=None, log=""):
        super().__init__(message)
        self.message, self.field, self.log = message, field, log


@dataclass(frozen=True)
class Database:
    """What the form says about the database: SQLite's file, or MySQL's server and account."""

    engine: str = "sqlite"
    file: str = "hfile.sqlite3"  # as typed: relative to config.toml's folder
    host: str = "127.0.0.1"
    port: int = 3306
    name: str = "hfile"
    user: str = "hfile"
    password: str = ""

    def path(self):
        """SQLite's file."""
        return Path(configfile.folder(settings.CONFIG_FILE, self.file))

    def params(self, database=True):
        params = {"user": self.user, "password": self.password, "charset": "utf8mb4", "connect_timeout": CONNECT_TIMEOUT}
        if self.host.startswith("/"):
            params["unix_socket"] = self.host
        else:
            params.update(host=self.host, port=self.port)
        if database:
            params["database"] = self.name
        params.update(_tls())
        return params

    def config(self):
        """As config.toml's [database]: the engine and its settings; the other engine's stay as they are."""
        if self.engine == "sqlite":
            return {"engine": "sqlite", "file": self.file}
        return {"engine": "mysql", "host": self.host, "port": self.port, "name": self.name, "user": self.user, "password": self.password}


def _tls():
    """MySQLdb's arguments for config.toml's [database] tls and ca (SEC-08): the form doesn't ask for
    them, but the site connects with them (use_database), so preflight() does too."""
    return database.mysql_tls(settings.CONFIG["database"], settings.CONFIG_FILE)


def _mysql_error(error, db):
    """What to say about a MySQLdb error, and which field it's about."""
    code = error.args[0] if error.args else None
    text = error.args[1] if len(error.args) > 1 else str(error)
    where = db.host if db.host.startswith("/") else f"{db.host}:{db.port}"
    if code == 1045:
        return InstallError(_("MySQL refused the username %(user)s with this password (%(error)s).") % {"user": db.user, "error": text}, "db_password")
    if code == 2059 and "mysql_native_password" in text:
        return InstallError(_("%(hint)s (%(error)s)") % {"hint": upgrades.NATIVE_PASSWORD_HINT, "error": text}, "db_user")
    if code == 2061 and "secure connection" in text:
        return InstallError(_("%(hint)s (%(error)s)") % {"hint": upgrades.SECURE_CONNECTION_HINT, "error": text})
    if code == 2026:
        return InstallError(_("The TLS connection to MySQL couldn't be set up (%(error)s). %(hint)s") % {"error": text, "hint": upgrades.TLS_HINT})
    if code in (2002, 2003, 2005, 2006, 2013):
        return InstallError(
            _("Can't connect to MySQL at %(address)s: check the address and the port, and "
              "that MySQL is running and accepts connections from this machine (%(error)s).")
            % {"address": where, "error": text},
            "db_host",
        )
    if code in (1044, 1142):
        return InstallError(_("The user %(user)s has no rights on the database %(name)s (%(error)s).") % {"user": db.user, "name": db.name, "error": text}, "db_user")
    return InstallError(_("Database error: %(error)s (error code %(code)s).") % {"error": text, "code": code})


def _version(text):
    numbers = re.match(r"(\d+)\.(\d+)", text)
    return (int(numbers.group(1)), int(numbers.group(2))) if numbers else (0, 0)


def preflight(db, *, dry_run=False):
    """Checks the database and gets it ready: SQLite's file is made, a missing MySQL database created.
    Returns what it found, as the page says it after 测试连接; raises InstallError saying what's wrong.

    dry_run: only looks. What a check has to make is removed again (SQLite's new file and its
    folders); a missing MySQL database isn't created, nor its character set changed."""
    if db.engine == "sqlite":
        return _check_sqlite(db, dry_run)
    try:
        import MySQLdb  # only for MySQL: SQLite sites can do without it
    except ImportError as error:
        raise InstallError(_("This program has no MySQL driver (mysqlclient: %(error)s); only SQLite can be chosen.") % {"error": error}, "db_engine") from error

    try:
        conn = MySQLdb.connect(**db.params(database=False))
    except MySQLdb.Error as error:
        raise _mysql_error(error, db) from error
    try:
        with conn.cursor() as cursor:
            return _check_mysql(cursor, db, dry_run)
    except MySQLdb.Error as error:
        raise _mysql_error(error, db) from error
    finally:
        conn.close()


own_table = upgrades.own_table  # the same rule decides whether an upgrade may go on


# People's accounts, not the built-in guest and template users that the migrations make
# (accounts/migrations/0002). A table without the kind column (none of HFile's has one) counts
# every row.
ACCOUNTS_SQL = "SELECT EXISTS (SELECT 1 FROM {table} WHERE kind = 'normal')"
ANY_ROW_SQL = "SELECT EXISTS (SELECT 1 FROM {table})"


def _has_people(execute, table, missing_column):
    """Whether `table` (accounts_user) has an account someone logs in with. execute(sql) runs a query
    and gives its one value; missing_column(error) says whether an error means "no kind column"."""
    try:
        return execute(ACCOUNTS_SQL.format(table=table))
    except Exception as error:
        if not missing_column(error):
            raise
        return execute(ANY_ROW_SQL.format(table=table))


def _check_tables(tables, has_accounts, field, *, file=None, name=None):
    """`tables` are those of SQLite's `file` or of MySQL's database `name`; has_accounts() says whether
    accounts_user has people's accounts. `field`: the form's field to show the error at."""
    own = own_table()
    others = [table for table in tables if not own.fullmatch(table)]
    if others:
        shown = _(", ").join(others[:5])
        if len(others) > 5:
            shown = ngettext("%(tables)s and others (%(count)s in all)", "%(tables)s and others (%(count)s in all)", len(others)) % {
                "tables": shown, "count": len(others)}
        if file is not None:
            message = _("The database file %(path)s already has another program's tables "
                        "(%(tables)s). Choose a new file name, so as not to mess up other data.")
        else:
            message = _("The database %(name)s already has another program's tables (%(tables)s). Choose an empty database, so as not to mess up other data.")
        raise InstallError(message % {"path": file, "name": name, "tables": shown}, field)
    # An HFile that was never finished has no account at all: install() makes the first one in its
    # last step. Any account means a site in use, whose config.toml is lost; installing again would
    # give it a new secret key.
    if "accounts_user" in tables and has_accounts():
        if file is not None:
            message = _("HFile is already installed in the database file %(path)s, with "
                        "accounts. To go on using it, put the original settings file config.toml "
                        "back and start again; to install anew, choose a new file name.")
        else:
            message = _("HFile is already installed in the database %(name)s, with accounts. "
                        "To go on using it, put the original settings file config.toml back "
                        "and start again; to install anew, choose an empty database.")
        raise InstallError(message % {"path": file, "name": name}, field)


def _sqlite_problem(error, path):
    text = str(error)
    if "not a database" in text:
        return InstallError(_("%(path)s isn't an SQLite database file. Choose another file name, so as not to damage it.") % {"path": path}, "db_file")
    if "locked" in text:
        return InstallError(_("The database file %(path)s is in use by another program (%(error)s).") % {"path": path, "error": text}, "db_file")
    return InstallError(
        _("Can't write the database file %(path)s (%(error)s): check that the file and its folder can be written to.") % {"path": path, "error": text}, "db_file"
    )


def _check_sqlite(db, dry_run):
    if problem := database.sqlite_too_old():
        raise InstallError(problem, "db_engine")
    path = db.path()
    try:
        is_dir = path.is_dir()
    except OSError as error:  # e.g. a name longer than the system allows
        raise InstallError(_("This path can't be used: %(error)s.") % {"error": error.strerror or error}, "db_file") from error
    if is_dir:
        raise InstallError(
            _("%(path)s is a folder. Enter the path of the database file, like %(example)s.") % {"path": path, "example": path / "hfile.sqlite3"},
            "db_file",
        )
    # The folders this makes, the deepest first: a check that fails, or a dry run, removes them again.
    new_folders = list(itertools.takewhile(lambda folder: not os.path.exists(folder), [path.parent, *path.parent.parents]))
    made = False
    try:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise InstallError(
                _("Can't create the folder %(folder)s: %(error)s.") % {"folder": path.parent, "error": error.strerror or error}, "db_file"
            ) from error
        try:
            # Made here (O_EXCL), so that only its owner may read it: it holds password hashes and,
            # later, storages' keys. SQLite gives its -wal and -shm files the same permissions.
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
            private.made(path)
            made = True
        except FileExistsError:
            pass
        except OSError as error:
            raise InstallError(
                _("Can't create the database file in %(folder)s: %(error)s. Choose a folder that can be written to.") % {"folder": path.parent, "error": error.strerror or error},
                "db_file",
            ) from error
        try:
            # WAL is the site's way (config/database.py), set here for good: not on a file a dry run keeps.
            tables = _try_sqlite(path, wal=made or not dry_run)
        except sqlite3.Error as error:
            raise _sqlite_problem(error, path) from error
    except BaseException:
        _remove(path if made else None, new_folders)  # nothing of it is wanted
        raise
    if dry_run:
        _remove(path if made else None, new_folders)
    if made:
        if dry_run and new_folders:
            message = _("OK: installing will create the database file %(path)s, and its folder %(folder)s.")
        elif dry_run:
            message = _("OK: installing will create the database file %(path)s.")
        elif new_folders:
            message = _("Created the database file %(path)s, and its folder %(folder)s.")
        else:
            message = _("Created the database file %(path)s.")
        return message % {"path": path, "folder": path.parent}
    if tables:
        return _("OK: the database file %(path)s holds an HFile whose install wasn't finished; installing will finish it.") % {"path": path}
    return _("OK: the database file %(path)s exists and is empty.") % {"path": path}


def _try_sqlite(path, *, wal):
    """The file's tables, once it is clear that the site may use it and that it can be written."""
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    try:
        # What is in it first: a file of something else is left exactly as it is.
        tables = sorted(
            name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
        )
        _check_tables(
            tables,
            lambda: _has_people(
                lambda sql: conn.execute(sql).fetchone()[0],
                "accounts_user",
                lambda error: isinstance(error, sqlite3.OperationalError) and "no such column" in str(error),
            ),
            "db_file",
            file=path,
        )
        # Then writing, which is what has to work: a read-only file or folder only fails here.
        if wal:
            conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE hfile_install_check (id integer)")
        conn.execute("ROLLBACK")
        return tables
    finally:
        conn.close()


def _remove(file, folders):
    """What a check of SQLite made: the file (with SQLite's files next to it), and the folders made
    for it, the deepest first. A folder that has something in it stays, and so do those above it."""
    if file is not None:
        for leftover in (file, *(file.with_name(file.name + end) for end in ("-wal", "-shm", "-journal"))):
            leftover.unlink(missing_ok=True)
    for folder in folders:
        try:
            folder.rmdir()
        except OSError:
            break


def _check_mysql(cursor, db, dry_run):
    import MySQLdb

    cursor.execute("SELECT VERSION()")
    version_text = cursor.fetchone()[0]
    if "mariadb" in version_text.lower():
        raise InstallError(_("This is MariaDB (%(version)s): HFile supports only MySQL 8.4 or later. Or choose SQLite.") % {"version": version_text}, "db_host")
    minimum = database.MYSQL_MINIMUM
    if _version(version_text) < minimum:
        raise InstallError(
            _("MySQL's version is %(version)s; at least %(minimum)s is needed.") % {"version": version_text, "minimum": f"{minimum[0]}.{minimum[1]}"},
            "db_host",
        )
    number = re.match(r"\d+(\.\d+)*", version_text)
    server = _("Connected to MySQL %(version)s.") % {"version": number.group() if number else version_text}

    quoted = "`" + db.name.replace("`", "``") + "`"
    collation = f" COLLATE {database.MYSQL_COLLATION}"
    # Also when the database is there, but belongs to another user: it isn't in SCHEMATA then.
    no_rights = InstallError(
        _("The user %(user)s has no rights on the database %(name)s: either it doesn't exist yet and this user "
          "can't create databases, or it was created for another user. Run this with MySQL's admin account: %(sql)s") % {
            "user": db.user,
            "name": db.name,
            "sql": f"CREATE DATABASE IF NOT EXISTS {quoted} CHARACTER SET utf8mb4{collation}; "
            f"GRANT ALL PRIVILEGES ON {quoted}.* TO '{db.user}'@'%';",
        },
        "db_name",
    )
    cursor.execute(
        "SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s",
        [db.name],
    )
    row = cursor.fetchone()
    if row is None:
        try:
            if dry_run:
                # Without making it: MySQL calls the name unknown (1049) only to a user who may have a
                # database by that name, and denies it (1044) to one who may not.
                cursor.execute(f"USE {quoted}")
            else:
                cursor.execute(f"CREATE DATABASE {quoted} CHARACTER SET utf8mb4{collation}")
        except MySQLdb.Error as error:
            code = error.args[0] if error.args else None
            if code in (1044, 1007):
                raise no_rights from error
            if not (dry_run and code == 1049):
                raise
        message = _("%(server)s The database %(name)s doesn't exist yet; installing will create it.") if dry_run else _("%(server)s Created the database %(name)s.")
        return message % {"server": server, "name": db.name}

    cursor.execute("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s", [db.name])
    tables = sorted(name for (name,) in cursor.fetchall())

    def has_accounts():
        def execute(sql):
            cursor.execute(sql)
            return cursor.fetchone()[0]

        # MySQL's 1054: Unknown column.
        return _has_people(execute, f"{quoted}.accounts_user", lambda error: getattr(error, "args", [None])[0] == 1054)

    _check_tables(tables, has_accounts, "db_name", name=db.name)
    # What was found, after what the server is.
    found = (
        _("%(server)s The database %(name)s holds an HFile whose install wasn't finished; installing will finish it.") if tables else _("%(server)s The database %(name)s exists and is empty.")
    ) % {"server": server, "name": db.name}

    charset, collation_now = row
    if charset != "utf8mb4" or not collation_now.endswith("_ci"):
        if tables:
            raise InstallError(
                _("The character set of the database %(name)s is %(charset)s (%(collation)s); "
                  "it must be utf8mb4 and case-insensitive. Choose an empty database.")
                % {"name": db.name, "charset": charset, "collation": collation_now},
                "db_name",
            )
        if dry_run:
            return _("%(found)s Its character set is %(charset)s; installing will change it to utf8mb4.") % {"found": found, "charset": charset}
        try:
            cursor.execute(f"ALTER DATABASE {quoted} CHARACTER SET utf8mb4{collation}")
        except MySQLdb.Error as error:
            raise InstallError(
                _("The character set of the database %(name)s is %(charset)s and must become utf8mb4, but "
                  "the user %(user)s has no right to change it. Run this with MySQL's admin account: %(sql)s")
                % {"name": db.name, "charset": charset, "user": db.user, "sql": f"ALTER DATABASE {quoted} CHARACTER SET utf8mb4{collation};"},
                "db_name",
            ) from error
    return found


def use_database(db):
    """Points the default connection at `db` (setup mode only, see the top of this file)."""
    values = {**settings.CONFIG["database"], **db.config()}
    # In place: Django's connection handler holds this dict, with the defaults it filled in.
    settings.DATABASES[DEFAULT_DB_ALIAS].update(database.django_settings(values, settings.CONFIG_FILE))
    # Each thread makes its connection object once, from the settings as they were then: this
    # thread's next query makes a new one.
    try:
        del connections[DEFAULT_DB_ALIAS]
    except AttributeError:  # it had none yet
        pass


_lock = threading.Lock()  # one install at a time (a double click)


def install(*, username, password, site_name, db=None):
    """Creates the tables, the first admin (a superuser) and the site's name. With `db`, first points
    the site at it (setup mode). Returns the admin. Raises InstallError; the database is then as it
    was before the tables were made, except for a database preflight() just created, which stays, empty."""
    User = get_user_model()
    with _lock:
        if db is not None:
            use_database(db)
        out = io.StringIO()
        try:
            upgrades.upgrade(out)
        except upgrades.UpgradeError as error:
            raise InstallError(_("Creating the tables failed. %(error)s") % {"error": error}, log=out.getvalue()) from error
        except DatabaseError as error:
            raise InstallError(upgrades.unreachable(error), log=out.getvalue()) from error
        try:
            with transaction.atomic():
                if User.objects.filter(is_superuser=True).exists():
                    raise InstallError(_("This database already has an admin; there's no need to install again."))
                if User.objects.username_taken(username):
                    raise InstallError(_("The database already has this username. Please choose another."), "username")
                admin = User.objects.create_superuser(username=username, password=password)
                site = SiteSettings.load()
                site.site_name = site_name
                site.save(update_fields=["site_name", "updated_at"])
                activity.record(activity.Kind.INSTALL, actor=admin)
        except DatabaseError as error:
            raise InstallError(upgrades.unreachable(error), log=out.getvalue()) from error
        return admin


def restart_soon(delay=1.5):
    """Starts the program again, so it reads the new config.toml: possible under `hfile` (serve),
    whose parent process starts it again when it exits with 3 (config/cli.py), and under runserver's
    autoreloader, which does the same. Returns False elsewhere: then somebody has to."""
    if os.environ.get(cli.CHILD) != "1" and os.environ.get("RUN_MAIN") != "true":
        return False
    threading.Timer(delay, _exit_for_restart).start()
    return True


def _exit_for_restart():
    for stream in (sys.stdout, sys.stderr):  # os._exit doesn't flush them; a log file would lose the end
        with suppress(Exception):
            stream.flush()
    os._exit(cli.RESTART)
