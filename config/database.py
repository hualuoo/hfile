"""settings.DATABASES["default"] from config.toml's [database] (configfile.py checks it): SQLite,
a file next to config.toml, or MySQL. settings.py uses it, and so does the installer when it
switches the site over to the database it was given (apps/installer/service.py use_database).

No Django imports here, as in configfile.py.
"""

import sqlite3

from . import configfile
from .i18n import _

# Seconds a write waits while another one finishes: SQLite has one writer at a time.
SQLITE_TIMEOUT = 20
SQLITE_MINIMUM = (3, 37)  # Django 6.1's
MYSQL_MINIMUM = (8, 4)
MYSQL_COLLATION = "utf8mb4_0900_ai_ci"  # case-insensitive, as the site compares names


def sqlite_file(values, config_path):
    """The SQLite file of [database] (`values`): relative to config.toml's folder."""
    return configfile.folder(config_path, values["file"])


def django_settings(values, config_path):
    common = {"CONN_MAX_AGE": 60, "CONN_HEALTH_CHECKS": True}
    if values["engine"] == "sqlite":
        return {
            **common,
            # Django's, but it makes a new database file 600 (config/backends/sqlite3/base.py).
            "ENGINE": "config.backends.sqlite3",
            "NAME": str(sqlite_file(values, config_path)),
            "OPTIONS": {
                # A transaction takes the write lock when it begins, so two of them never both read and
                # then both want to write ("database is locked" at once): the second waits its turn.
                "transaction_mode": "IMMEDIATE",
                "timeout": SQLITE_TIMEOUT,
                # WAL: reading never waits for a write, nor a write for reading. NORMAL is safe with WAL:
                # a power cut can lose the last moments, but never breaks the file.
                "init_command": "PRAGMA journal_mode = WAL; PRAGMA synchronous = NORMAL",
            },
        }
    name = values["name"]
    return {
        **common,
        "ENGINE": "django.db.backends.mysql",
        "NAME": name,
        "USER": values["user"],
        "PASSWORD": values["password"],
        "HOST": values["host"],
        "PORT": str(values["port"]),
        "OPTIONS": {
            "charset": "utf8mb4",
            # Strict, as MySQL is by default (Django checks it, mysql.W002): a server set up without it
            # would cut a value that is too long instead of refusing it. Added to the server's own modes.
            "init_command": "SET SESSION sql_mode = CONCAT_WS(',', NULLIF(@@SESSION.sql_mode, ''), 'STRICT_TRANS_TABLES')",
            **mysql_tls(values, config_path),
        },
        # utf8mb4_0900_ai_ci compares case-insensitively, as the site does everywhere (the unique
        # indexes on Lower(...) are what SQLite has, apps/accounts/models.py).
        "TEST": {"NAME": f"test_{name}", "CHARSET": "utf8mb4", "COLLATION": MYSQL_COLLATION},
    }


def mysql_tls(values, config_path):
    """MySQLdb.connect()'s arguments for [database] tls and ca (SEC-08). Nothing for "preferred", which is
    MySQL's own default: TLS when the server has it, its certificate unchecked."""
    if values["tls"] == "preferred":
        return {}
    arguments = {"ssl_mode": values["tls"].upper()}
    if values["ca"]:  # relative to config.toml's folder
        arguments["ssl"] = {"ca": str(configfile.folder(config_path, values["ca"]))}
    return arguments


def where(values, config_path):
    """Which database [database] means, for messages: "SQLite，文件 /srv/hfile/hfile.sqlite3" (SQLite, file …)."""
    if values["engine"] == "sqlite":
        return _("SQLite, file %(path)s") % {"path": sqlite_file(values, config_path)}
    host = values["host"]
    server = host if host.startswith("/") else f"{host}:{values['port']}"
    return _("MySQL, %(name)s on %(server)s") % {"server": server, "name": values["name"]}


def readable_sqlite_files(values, config_path):
    """The files of a SQLite [database] that other users of the machine may read (made before the
    program made them 600, or by hand): the database itself, -wal and -shm. [] for MySQL."""
    if values["engine"] != "sqlite":
        return []
    from .backends.sqlite3.base import readable_by_others  # no Django in this module otherwise

    database = str(sqlite_file(values, config_path))
    return [name for name in (database, database + "-wal", database + "-shm") if readable_by_others(name)]


def sqlite_too_old():
    """What to say when the SQLite this program has is older than Django needs, else ""."""
    if sqlite3.sqlite_version_info >= SQLITE_MINIMUM:
        return ""
    return _("The SQLite of this program is version %(version)s; at least %(minimum)s is needed. Use MySQL, or a newer Python.") % {
        "version": sqlite3.sqlite_version, "minimum": ".".join(map(str, SQLITE_MINIMUM))}
