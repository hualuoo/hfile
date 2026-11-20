"""Backups of the whole database as SQL: `manage.py backup`, and before every upgrade.

Python writes them over the site's own database connection, for MySQL and for SQLite alike, so
the single executable needs neither mysqldump nor sqlite3. A backup is gzip-compressed text: a
few comment lines (the program that wrote it, the kind of database and its name), then the
statements, then a last line saying it is complete. `manage.py restore FILE` reads it back into
a database of the same kind; so do the databases' own programs:

    gunzip -c FILE | mysql -u USER -p DATABASE     (MySQL)
    gunzip -c FILE | sqlite3 NEW_FILE              (SQLite, into a new file)

Every table is read in one snapshot, so a backup taken while the site runs is still
consistent. Uploaded files (MEDIA_ROOT) are not in it, nor the rows of WITHOUT_ROWS.
"""

import gzip
import io
import logging
import math
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from django.conf import settings
from django.db import DatabaseError, connection, transaction
from django.utils import timezone
from django.utils.translation import gettext, gettext_lazy, ngettext

from config import private
from config.version import VERSION
from .models import UpgradeLog

logger = logging.getLogger("hfile.upgrades")

TITLE = "-- HFile 数据库备份"  # i18n: data (the backup format, the same in every language)
TITLES = (TITLE,)
END = "-- 备份结束"  # i18n: data. The last line; a file without it was cut short
# "-- 备份时的程序版本：1.0.0". A backup taken before an upgrade holds the database of the version
# before: "数据库的版本" is the last one in its upgrade log (no line when it has none).
HEADER = {"备份时的程序版本": "version", "数据库的版本": "schema", "数据库类型": "engine", "数据库名": "database", "时间": "created"}  # i18n: data
BATCH_BYTES = 512 * 1024  # per INSERT, far below MySQL's max_allowed_packet (64 MB by default)
# Characters in one line of a backup when reading it: an INSERT is about BATCH_BYTES (one big row can
# make it longer). A line far past it isn't from a backup, and reading it whole could take all memory.
LINE_MAX = 64 * 1024 * 1024
# Tables a backup has without their rows: none yet. A table whose rows must not outlive the day (a
# key that is deleted on purpose) goes here; a restore gives it back empty.
WITHOUT_ROWS = frozenset()


@dataclass
class Info:
    """What a backup's first lines say, and the migrations its database had."""

    version: str = ""  # of the program that made the backup
    schema: str = ""  # of the database in it: the program it matches
    engine: str = ""  # "MySQL" or "SQLite"
    database: str = ""
    created: str = ""
    # (app, name) of the rows of django_migrations in it; None when they can't be read (not as a backup
    # writes them).
    migrations: list | None = None


class BadBackup(Exception):
    """Not a whole HFile backup, or not one of this kind of database."""


def _dialect():
    return _SQLite if connection.vendor == "sqlite" else _MySQL


def engine():
    """The kind of the site's database, as backups name it: "MySQL" or "SQLite"."""
    return _dialect().name


def database_label():
    """The database in file names: MySQL's name, or the SQLite file's without its extension."""
    if connection.vendor == "sqlite":
        name = "memory" if connection.is_in_memory_db() else Path(connection.settings_dict["NAME"]).stem
    else:
        name = connection.settings_dict["NAME"]
    return re.sub(r"[^0-9A-Za-z._-]", "", name) or "hfile"


def new_path(label=""):
    """A file name in settings.BACKUP_DIR that isn't taken: <database>-<date>-<time>[-label].sql.gz."""
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    label = re.sub(r"[^0-9A-Za-z._-]", "", label)
    stem = "-".join(part for part in (database_label(), stamp, label) if part)
    folder = Path(settings.BACKUP_DIR)
    path, n = folder / f"{stem}.sql.gz", 1
    while path.exists() or _partial(path).exists():  # or another backup is being written to it
        n += 1
        path = folder / f"{stem}-{n}.sql.gz"
    return path


def _partial(path):
    return path.with_name(path.name + ".part")


PART_AGE = 24 * 3600  # seconds a backup's .part file has been untouched when its backup was cut short


def remove_parts(now):
    """`hfile cleanup` (apps.core.cleanup): the .part files in BACKUP_DIR of backups cut short (kill -9, a power
    cut), with password hashes in them (OPS-06). A backup being written touches its file all the time, so only
    those untouched for PART_AGE. Removed once the transaction commits: not with --dry-run."""
    cutoff = now.timestamp() - PART_AGE
    try:
        with os.scandir(settings.BACKUP_DIR) as entries:
            left = [
                entry.path for entry in entries
                if entry.name.endswith(".sql.gz.part") and entry.is_file(follow_symlinks=False)
                and entry.stat(follow_symlinks=False).st_mtime < cutoff
            ]
    except FileNotFoundError:  # no backup made yet
        return 0

    def remove():
        for name in left:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass
            except OSError as error:
                logger.warning("Couldn't remove %s, left by a backup cut short: %s", name, error)

    transaction.on_commit(remove)
    return len(left)


def size_text(path):
    size = Path(path).stat().st_size
    return f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, round(size / 1024))} KB"


def dump(path):
    """Writes the whole database to `path` (.sql.gz). Returns the number of tables.

    Writes to a temporary file next to it first, so `path` is either whole or missing.
    Only its owner can read it: it holds password hashes and email addresses.
    """
    path = Path(path)
    private.mkdir(path.parent)
    partial = _partial(path)
    try:
        # O_EXCL: two backups started in the same second must not write into one file.
        # O_BINARY: Windows would write each \n of the gzip data as \r\n.
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        private.made(partial)
    except FileExistsError:
        raise FileExistsError(gettext("%(path)s exists already: another backup is writing this file. Try again once it's done.") % {"path": partial}) from None
    try:
        with open(descriptor, "wb") as file:
            with (
                gzip.GzipFile(filename="", mode="wb", fileobj=file) as packed,
                io.TextIOWrapper(packed, encoding="utf-8", newline="\n") as out,
            ):
                count = _dialect().write(out)
            file.flush()
            os.fsync(file.fileno())  # on disk before an upgrade starts changing tables
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return count


def _header(dialect, schema):  # i18n: data
    return (
        f"{TITLE}\n"
        f"-- 备份时的程序版本：{VERSION}\n"
        + (f"-- 数据库的版本：{schema}\n" if schema else "")
        + f"-- 数据库类型：{dialect.name}\n"
        f"-- 数据库名：{dialect.database_name()}\n"
        f"-- 时间：{timezone.localtime().isoformat(timespec='seconds')}\n"
        f"-- 恢复：hfile restore 这个文件；或者 {dialect.by_hand}\n"
    )


def _open(path):
    return gzip.open(path, "rt", encoding="utf-8", newline="\n")


def _lines(file):
    """The lines of an opened backup. BadBackup for one longer than LINE_MAX."""
    while line := file.readline(LINE_MAX + 1):
        if len(line) > LINE_MAX:
            raise BadBackup(gettext("A line of the backup file is abnormally long: this isn't a backup written by HFile."))
        yield line


CUT_SHORT = gettext_lazy("The backup is incomplete: the file ends before it was finished.")
DAMAGED = gettext_lazy("The backup file is damaged: decompressing it fails, so it can't be read.")


def read_info(path):
    """The first lines of the backup at `path`, and the migrations in it, after reading it all through
    to check that it is whole. Raises BadBackup."""
    info, last, migrations = Info(), "", []
    try:
        with _open(path) as file:
            if file.readline(LINE_MAX).rstrip("\n") not in TITLES:
                raise BadBackup(gettext("This isn't an HFile database backup."))
            for line in _lines(file):
                line = line.rstrip("\n")
                label, _, value = line.removeprefix("-- ").partition("：")  # i18n: data
                if line.startswith("-- ") and label in HEADER and not getattr(info, HEADER[label]):
                    setattr(info, HEADER[label], value)
                elif migrations is not None and (insert := _MIGRATIONS.match(line)):
                    rows = _migration_rows(line, insert)
                    migrations = None if rows is None else migrations + rows
                last = line or last
    except EOFError as error:  # gzip: "Compressed file ended before the end-of-stream marker was reached"
        raise BadBackup(CUT_SHORT) from error
    except gzip.BadGzipFile as error:  # not gzip at all, or a CRC or length that doesn't match
        if str(error).startswith("Not a gzipped file"):
            raise BadBackup(gettext("This isn't an HFile database backup: it isn't a gzip file.")) from error
        raise BadBackup(DAMAGED) from error
    except zlib.error as error:
        raise BadBackup(DAMAGED) from error
    except UnicodeDecodeError as error:  # a damaged file decompresses to bytes that aren't text
        raise BadBackup(gettext("The backup file is damaged: decompressing it fails, so it can't be "
                                "read (or it wasn't written by HFile: it has text that isn't UTF-8).")) from error
    except OSError as error:
        raise BadBackup(file_problem(error)) from error
    if not last.startswith(END):
        raise BadBackup(CUT_SHORT)
    if not info.engine:
        raise BadBackup(gettext("The backup file doesn't start with the type of database: this isn't a backup written by HFile."))
    info.migrations = migrations
    return info


def file_problem(error):
    """Why a file can't be read, in the language of the moment (the system says it in English)."""
    if isinstance(error, FileNotFoundError):
        return gettext("No such file.")
    # Windows gives no IsADirectoryError: opening a folder there is a PermissionError.
    if isinstance(error, IsADirectoryError) or (
            isinstance(error, PermissionError) and error.filename is not None and os.path.isdir(error.filename)):
        return gettext("This is a folder, not a backup file.")
    if isinstance(error, PermissionError):
        return gettext("No permission to read this file.")
    return gettext("Can't read this file: %(error)s") % {"error": error.strerror or error}


# The rows of django_migrations, as both dialects write them: INSERT INTO `django_migrations` (`id`,
# `app`, `name`, `applied`) VALUES (1,'contenttypes','0001_initial','2026-11-08 01:02:03.456789'),(...);
_MIGRATIONS = re.compile(r'INSERT INTO (?:`django_migrations`|"django_migrations") \(([^)]*)\) VALUES ')
_SIMPLE_VALUE = re.compile(r"NULL|-?[0-9]+(?:\.[0-9]+)?|'[^'\\]*'")


def _migration_rows(line, insert):
    """(app, name) of each row of an INSERT into django_migrations; None for one not written as a
    backup writes it."""
    columns = [name.strip().strip('`"') for name in insert.group(1).split(",")]
    if "app" not in columns or "name" not in columns:
        return None
    app, name = columns.index("app"), columns.index("name")
    rows, at = [], insert.end()
    while line.startswith("(", at):
        values, at = [], at + 1
        while (value := _SIMPLE_VALUE.match(line, at)) is not None:
            values.append(value.group())
            at = value.end()
            if not line.startswith(",", at):
                break
            at += 1
        if not line.startswith(")", at) or len(values) != len(columns):
            return None
        rows.append((values[app].strip("'"), values[name].strip("'")))
        at += 1
        if line.startswith(",", at):
            at += 1
    return rows if line[at:] == ";" else None


def check_engine(info):
    """Raises BadBackup unless the backup is of the kind of database the site uses."""
    if info.engine != engine():
        raise BadBackup(
            gettext("This is a backup of a %(engine)s database; the site now uses %(current)s "
                    "([database] engine in the settings file), so it can't be restored into it.")
            % {"engine": info.engine, "current": engine()}
        )


def restore(path):
    """Replaces every table of the database with the ones in the backup at `path`; tables
    that aren't in the backup are dropped too. Returns its Info.

    The whole file is checked before anything is dropped.
    """
    info = read_info(path)
    check_engine(info)
    _dialect().restore(path)
    return info


def drop_tables():
    """Drops every table: the way back from a failed first install, which started empty."""
    _dialect().drop_tables()


# ---- MySQL ----


def quote(name):
    """A MySQL name."""
    return "`" + name.replace("`", "``") + "`"


# What mysql_real_escape_string escapes. The backup turns NO_BACKSLASH_ESCAPES off first.
_ESCAPES = str.maketrans({"\\": "\\\\", "'": "\\'", "\0": "\\0", "\n": "\\n", "\r": "\\r", "\x1a": "\\Z"})


def literal(value):
    """A value read from MySQL (dates and times as text), written so that MySQL reads it back the same."""
    if value is None:
        return "NULL"
    if isinstance(value, (bytes, bytearray)):  # BLOB, BINARY, BIT: any bytes, so as hex
        return f"X'{bytes(value).hex()}'"
    if isinstance(value, bool):  # before int, which it is too
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)  # all 17 digits; MySQL can't store inf or nan
    if isinstance(value, set):  # SET columns
        value = ",".join(sorted(value))
    return "'" + str(value).translate(_ESCAPES) + "'"


class _MySQL:
    """Every table in one InnoDB snapshot. Dates and times are read as MySQL prints them, in UTC
    like mysqldump: the driver can't read every TIME value (Django reads them as times of day)
    and turns zero dates into NULL. One statement per line."""

    name = "MySQL"
    by_hand = "gunzip -c 这个文件 | mysql -u 用户名 -p 数据库名"  # i18n: data
    AS_TEXT = {"date", "datetime", "timestamp", "time"}  # the column types read as text

    @staticmethod
    def database_name():
        return connection.settings_dict["NAME"]

    @staticmethod
    @contextmanager
    def _cursor(cursor_class=None):
        """A cursor of the driver itself: no query log, no %-formatting of the SQL. Its errors
        still come out as Django's DatabaseError."""
        connection.ensure_connection()
        with connection.wrap_database_errors:
            cursor = connection.connection.cursor(*[c for c in [cursor_class] if c])
            try:
                yield cursor
            finally:
                with suppress(Exception):  # e.g. the connection is gone: keep the first error
                    cursor.close()

    @classmethod
    @contextmanager
    def _without_foreign_keys(cls):
        with cls._cursor() as cursor:
            cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            try:
                yield cursor
            except BaseException:
                with suppress(Exception):
                    cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
                raise
            cursor.execute("SET FOREIGN_KEY_CHECKS = 1")

    @staticmethod
    def _tables(cursor):
        cursor.execute(
            "SELECT TABLE_NAME FROM information_schema.TABLES"
            " WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE' ORDER BY TABLE_NAME"
        )
        return [name for (name,) in cursor.fetchall()]

    @staticmethod
    def _columns(cursor, table):
        """(name, type) of the columns to back up: all but generated ones, which MySQL computes."""
        cursor.execute(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
            # MySQL has '' for a column that isn't generated, MariaDB NULL.
            " AND TABLE_NAME = %s AND COALESCE(GENERATION_EXPRESSION, '') = '' ORDER BY ORDINAL_POSITION",
            (table,),
        )
        return cursor.fetchall()

    @classmethod
    def write(cls, out):
        from MySQLdb.cursors import SSCursor

        with cls._cursor() as plain:
            # TIMESTAMP columns as UTC, whatever the server's time zone (the backup says so too).
            plain.execute("SET @hfile_time_zone = @@SESSION.time_zone")
            plain.execute("SET SESSION time_zone = '+00:00'")
            # One snapshot for all the tables. Django's sessions use READ COMMITTED, which has none.
            plain.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            plain.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY")
            try:
                tables = cls._tables(plain)
                schema = ""
                if UpgradeLog._meta.db_table in tables:
                    plain.execute(f"SELECT version FROM {quote(UpgradeLog._meta.db_table)} ORDER BY id DESC LIMIT 1")
                    schema = (plain.fetchone() or [""])[0]
                out.write(
                    _header(cls, schema) + "SET NAMES utf8mb4;\n"
                    "SET @OLD_FOREIGN_KEY_CHECKS = @@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS = 0;\n"
                    "SET @OLD_SQL_MODE = @@SQL_MODE, SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';\n"
                    "SET @OLD_TIME_ZONE = @@TIME_ZONE, TIME_ZONE = '+00:00';\n"
                )
                with cls._cursor(SSCursor) as stream:  # rows a batch at a time, not a whole table in memory
                    for table in tables:
                        cls._write_table(out, plain, stream, table)
                out.write(  # i18n: data
                    "SET TIME_ZONE = @OLD_TIME_ZONE;\n"
                    "SET SQL_MODE = @OLD_SQL_MODE;\n"
                    "SET FOREIGN_KEY_CHECKS = @OLD_FOREIGN_KEY_CHECKS;\n"
                    f"{END}：{len(tables)} 张表\n"
                )
                return len(tables)
            finally:
                # After `stream` is closed: with rows left unread the connection takes no other command.
                with suppress(Exception):
                    plain.execute("ROLLBACK")  # it only read
                with suppress(Exception):
                    plain.execute("SET SESSION time_zone = @hfile_time_zone")

    @classmethod
    def _write_table(cls, out, plain, stream, table):
        plain.execute(f"SHOW CREATE TABLE {quote(table)}")
        create = plain.fetchone()[1]
        columns = cls._columns(plain, table)
        names = ", ".join(quote(name) for name, _ in columns)
        selected = ", ".join(f"CAST({quote(name)} AS CHAR)" if kind in cls.AS_TEXT else quote(name) for name, kind in columns)
        out.write(f"DROP TABLE IF EXISTS {quote(table)};\n")
        # Strings in it are escaped, so its only line breaks are between definitions.
        out.write(create.replace("\n", " ") + ";\n")
        if table in WITHOUT_ROWS:
            return
        _write_rows(out, f"INSERT INTO {quote(table)} ({names}) VALUES ", stream, f"SELECT {selected} FROM {quote(table)}", literal)

    # Every line of a backup that isn't a comment: one statement each (the driver runs one at a time),
    # in exactly the shapes _write_table writes, with one table name and no database in front of it.
    # A restore runs with the site's database user's rights: "CREATE TABLE t SELECT ... FROM
    # mysql.user", "INSERT INTO t ... SELECT" or "DROP TABLE other.t" in a backup someone sent
    # would act outside the site's tables.
    _NAME = r"`(?:[^`]|``)+`"
    DROP = re.compile(rf"DROP TABLE IF EXISTS {_NAME};")
    CREATE = re.compile(rf"CREATE TABLE {_NAME} \(")
    INSERT = re.compile(rf"INSERT INTO {_NAME} \({_NAME}(?:, {_NAME})*\) VALUES ")
    SETTINGS = frozenset({
        "SET NAMES utf8mb4;",
        "SET @OLD_FOREIGN_KEY_CHECKS = @@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS = 0;",
        "SET @OLD_SQL_MODE = @@SQL_MODE, SQL_MODE = 'NO_AUTO_VALUE_ON_ZERO';",
        "SET @OLD_TIME_ZONE = @@TIME_ZONE, TIME_ZONE = '+00:00';",
        "SET TIME_ZONE = @OLD_TIME_ZONE;",
        "SET SQL_MODE = @OLD_SQL_MODE;",
        "SET FOREIGN_KEY_CHECKS = @OLD_FOREIGN_KEY_CHECKS;",
    })

    @classmethod
    def _allowed(cls, line):
        if line in cls.SETTINGS or cls.DROP.fullmatch(line):
            return True
        if match := cls.INSERT.match(line):
            return _rows_only(line, match.end())
        if match := cls.CREATE.match(line):
            return _plain_create(line, match.end())
        return False

    @classmethod
    def _statements(cls, path):
        with _open(path) as file:
            for line in _lines(file):
                line = line.rstrip("\n")
                if line and not line.startswith("--"):
                    if not cls._allowed(line):
                        raise BadBackup(gettext("The backup file has a statement a backup never writes: %(line)s… Nothing was restored.") % {"line": line[:60]})
                    yield line

    @classmethod
    def restore(cls, path):
        """Not in one transaction: MySQL commits every CREATE and DROP at once. A statement that fails
        (a lost connection, a full disk, a row the site wrote meanwhile) leaves the tables part old,
        part new; the restore command then puts back the copy it saved first. A BadBackup comes only
        before anything is dropped."""
        for _ in cls._statements(path):  # all of them, before anything is dropped
            pass
        with cls._without_foreign_keys() as cursor:
            for table in cls._tables(cursor):
                cursor.execute(f"DROP TABLE {quote(table)}")
            try:
                for line in cls._statements(path):
                    cursor.execute(line.removesuffix(";"))
            except BadBackup as error:  # the file changed since it was checked
                raise DatabaseError(str(error)) from error

    @classmethod
    def drop_tables(cls):
        with cls._without_foreign_keys() as cursor:
            for table in cls._tables(cursor):
                cursor.execute(f"DROP TABLE {quote(table)}")


# The values literal() writes: NULL, numbers, X'hex' and 'text' with backslash escapes.
_VALUE = re.compile(r"NULL|X'[0-9a-f]*'|-?[0-9]++(?:\.[0-9]++)?+(?:[eE][+-]?[0-9]++)?+|'(?:[^'\\]++|\\.)*+'")


def _rows_only(line, start):
    """Whether line[start:] is only rows of values, as _write_rows writes them: (v,v),(v,v);"""
    end = len(line) - 1
    if line[end:] != ";":
        return False
    at = start
    while True:
        if line[at:at + 1] != "(":
            return False
        at += 1
        while True:
            value = _VALUE.match(line, at)
            if value is None:
                return False
            at = value.end()
            if line[at:at + 1] == ",":
                at += 1
            elif line[at:at + 1] == ")":
                at += 1
                break
            else:
                return False
        if at == end:
            return True
        if line[at:at + 1] != ",":
            return False
        at += 1


_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_QUOTED = {"'": re.compile(r"'(?:[^'\\]|\\.|'')*+'"), '"': re.compile(r'"(?:[^"\\]|\\.|"")*+"'),
           "`": re.compile(r"`(?:[^`]|``)*+`")}


def _plain_create(line, start):
    """Whether a CREATE TABLE is one as SHOW CREATE TABLE writes it: columns, keys and table options,
    with no table of another database (`other`.`t`) and nothing read from elsewhere (SELECT, LIKE)."""
    if not line.endswith(";"):
        return False
    at = start
    while at < len(line):
        char = line[at]
        if char in _QUOTED:
            quoted = _QUOTED[char].match(line, at)
            if quoted is None:
                return False
            at = quoted.end()
            if char == "`" and line[at:at + 1] == ".":
                return False
        elif word := _WORD.match(line, at):
            if word.group().upper() in ("SELECT", "LIKE", "TABLE", "UNION"):
                return False
            at = word.end()
        else:
            at += 1
    return True


def _write_rows(out, insert, cursor, select, to_sql):
    """The rows `select` gives, as INSERTs of up to BATCH_BYTES each."""
    cursor.execute(select)
    values, size = [], 0
    while rows := cursor.fetchmany(1000):
        for row in rows:
            text = "(" + ",".join(map(to_sql, row)) + ")"
            values.append(text)
            size += len(text)
            if size >= BATCH_BYTES:
                out.write(insert + ",".join(values) + ";\n")
                values, size = [], 0
    if values:
        out.write(insert + ",".join(values) + ";\n")


# ---- SQLite ----


def sqlite_quote(name):
    """A SQLite name."""
    return '"' + name.replace('"', '""') + '"'


def sqlite_literal(value):
    """A value as SQLite stores it (int, float, str, bytes or None), as SQL that gives it back the same."""
    if value is None:
        return "NULL"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if math.isnan(value):
            return "NULL"  # what SQLite stores for it anyway
        if math.isinf(value):
            return "9e999" if value > 0 else "-9e999"
        return repr(value)  # all 17 digits
    if isinstance(value, bytes):
        return f"X'{value.hex()}'"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        # Line breaks, NUL: as its UTF-8 bytes, so that every row stays on its line.
        return f"CAST(X'{value.encode('utf-8').hex()}' AS TEXT)"
    return "'" + value.replace("'", "''") + "'"


# Tables, indexes, views and triggers, but not SQLite's own (sqlite_*), nor the indexes it makes for
# UNIQUE and PRIMARY KEY itself (no sql): tables first, then what needs them.
_OBJECTS = (
    "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
    " ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 WHEN 'view' THEN 2 ELSE 3 END, name"
)


def _statements(file):
    """The statements in a backup, comment lines between them left out. A CREATE TABLE is in it as
    SQLite keeps it, maybe over several lines: sqlite3.complete_statement() says where one ends."""
    lines = []
    for line in _lines(file):
        if not lines and (line.startswith("--") or not line.strip()):
            continue
        lines.append(line)
        statement = "".join(lines)
        if sqlite3.complete_statement(statement):
            yield statement.strip()
            lines = []


class _SQLite:
    """Everything in sqlite_master, rows as SQLite stores them, and the AUTOINCREMENT counters.
    Read in one transaction: with WAL the site goes on writing meanwhile. A restore is one
    transaction too, so it either replaces the whole database or changes nothing."""

    name = "SQLite"
    by_hand = "gunzip -c 这个文件 | sqlite3 新的数据库文件"  # i18n: data
    # For the sqlite3 program; a restore runs in a transaction of its own.
    PREAMBLE = ("PRAGMA foreign_keys = OFF;", "BEGIN;")
    POSTAMBLE = ("COMMIT;",)

    @staticmethod
    def database_name():
        return "memory" if connection.is_in_memory_db() else Path(connection.settings_dict["NAME"]).name

    @staticmethod
    @contextmanager
    def _db():
        """The driver's own connection (sqlite3), its errors as Django's DatabaseError. Outside any
        transaction: what follows begins its own, and a ROLLBACK must never end one of Django's."""
        connection.ensure_connection()
        with connection.wrap_database_errors:
            if connection.connection.in_transaction:
                raise DatabaseError(gettext("Backups and restores can't run inside another transaction."))
            yield connection.connection

    @staticmethod
    def _has_sequence(db):
        return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'").fetchone()

    @classmethod
    def write(cls, out):
        with cls._db() as db:
            db.execute("BEGIN")  # one snapshot, from the first SELECT on
            try:
                objects = db.execute(_OBJECTS).fetchall()
                tables = [name for kind, name, _ in objects if kind == "table"]
                schema = ""
                if UpgradeLog._meta.db_table in tables:
                    row = db.execute(f"SELECT version FROM {sqlite_quote(UpgradeLog._meta.db_table)} ORDER BY id DESC LIMIT 1").fetchone()
                    schema = row[0] if row else ""
                out.write(_header(cls, schema) + "".join(line + "\n" for line in cls.PREAMBLE))
                for kind, name, sql in objects:
                    if kind == "table":
                        out.write(f"DROP TABLE IF EXISTS {sqlite_quote(name)};\n{sql};\n")
                        cls._write_table(out, db, name)
                for kind, _, sql in objects:
                    if kind != "table":
                        out.write(f"{sql};\n")
                if cls._has_sequence(db):  # the AUTOINCREMENT counters
                    out.write("DELETE FROM sqlite_sequence;\n")
                    _write_rows(out, "INSERT INTO sqlite_sequence (name, seq) VALUES ", db.cursor(),
                                "SELECT name, seq FROM sqlite_sequence ORDER BY name", sqlite_literal)
                out.write("".join(line + "\n" for line in cls.POSTAMBLE) + f"{END}：{len(tables)} 张表\n")  # i18n: data
                return len(tables)
            finally:
                with suppress(Exception):
                    db.execute("ROLLBACK")  # it only read

    @staticmethod
    def _write_table(out, db, table):
        if table in WITHOUT_ROWS:
            return
        # Not generated columns (hidden 2 or 3), which SQLite computes again.
        columns = [row[1] for row in db.execute(f"PRAGMA table_xinfo({sqlite_quote(table)})") if row[6] == 0]
        names = ", ".join(map(sqlite_quote, columns))
        # +"x": an expression, which has no declared type, so the driver doesn't convert it (Django has
        # it turn datetime, date, time and bool columns into Python's): each value just as it is stored.
        selected = ", ".join(f"+{sqlite_quote(name)}" for name in columns)
        _write_rows(out, f"INSERT INTO {sqlite_quote(table)} ({names}) VALUES ", db.cursor(),
                    f"SELECT {selected} FROM {sqlite_quote(table)}", sqlite_literal)

    @classmethod
    @contextmanager
    def _rewriting(cls):
        """One write transaction, foreign keys off (they can only be turned off outside one), and
        checked before it commits."""
        with cls._db() as db:
            db.execute("PRAGMA foreign_keys = OFF")
            try:
                db.execute("BEGIN IMMEDIATE")
                try:
                    yield db
                    if problems := db.execute("PRAGMA foreign_key_check").fetchall():
                        raise DatabaseError(
                            ngettext("%(count)s row points to data that doesn't exist (a foreign key): %(example)s.",
                                     "%(count)s rows point to data that doesn't exist (foreign keys), like %(example)s.", len(problems))
                            % {"count": len(problems), "example": problems[0]}
                        )
                    db.execute("COMMIT")
                except BaseException:
                    with suppress(Exception):
                        db.execute("ROLLBACK")
                    raise
            finally:
                db.execute("PRAGMA foreign_keys = ON")

    @classmethod
    def _drop_all(cls, db):
        everything = db.execute(
            "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'"
        ).fetchall()
        for kind, name in everything:  # a table's indexes and triggers go with it
            db.execute(f"DROP {kind.upper()} IF EXISTS {sqlite_quote(name)}")
        if cls._has_sequence(db):
            db.execute("DELETE FROM sqlite_sequence")

    # What a restore's statements may not do: open another file (ATTACH could write one anywhere),
    # end or start the transaction it runs in, or change the connection's settings.
    DENIED = frozenset({sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH, sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_PRAGMA})

    @classmethod
    def _authorize(cls, action, *args):
        return sqlite3.SQLITE_DENY if action in cls.DENIED else sqlite3.SQLITE_OK

    @classmethod
    def restore(cls, path):
        skipped = {*cls.PREAMBLE, *cls.POSTAMBLE}
        with cls._rewriting() as db, _open(path) as file:
            cls._drop_all(db)
            db.set_authorizer(cls._authorize)
            try:
                for statement in _statements(file):
                    if statement not in skipped:
                        db.execute(statement)
                        if not db.in_transaction:  # the authorizer refuses that already
                            raise DatabaseError(gettext("The backup file has a statement that ends the transaction. Nothing was restored."))
            finally:
                db.set_authorizer(None)

    @classmethod
    def drop_tables(cls):
        with cls._rewriting() as db:
            cls._drop_all(db)
