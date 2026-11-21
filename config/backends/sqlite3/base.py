"""Django's SQLite backend, except that a database file it makes is for its owner only (600).

The file holds password hashes, session keys and the storages' keys. Django's driver would make it
with the umask's mode, usually readable by every user of the machine. settings.DATABASES names
this backend (config/database.py), so whichever command connects first (check, migrate,
createsuperuser, backup, serve, the installer) makes it the same way.
"""

import os
import stat

from django.db.backends.sqlite3 import base

from config import private


class DatabaseWrapper(base.DatabaseWrapper):
    def get_new_connection(self, conn_params):
        database = str(conn_params["database"])
        if not self.is_in_memory_db() and not database.startswith("file:"):
            owner_only(database)
        return super().get_new_connection(conn_params)


def owner_only(path, *, fix=False):
    """Makes the file at `path`, empty and 600, when it isn't there yet (an empty file is an empty
    SQLite database; SQLite gives its -wal and -shm files the same mode). With `fix`, others lose
    their access to one that is there: for the program's own files that hold no data. A missing
    folder or one that can't be written is left for SQLite to say. On Windows an access list does
    what the mode does (config/private.py)."""
    try:
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        private.made(path)
    except FileExistsError:
        if fix:
            try:
                if os.name == "nt":
                    if readable_by_others(path):
                        from config import windows

                        windows.make_private(path)
                    return
                mode = stat.S_IMODE(os.stat(path).st_mode)
                if mode & 0o077:
                    os.chmod(path, mode & 0o700)
            except OSError:  # another user's: SQLite says what it can do with it
                pass
    except OSError:
        pass


def readable_by_others(path):
    """Whether others than the owner may read or write the file at `path` (False when it's missing). On
    Windows by its access list, or by its folder's: the -wal and -shm files SQLite makes next to it take
    that one (config/windows.py)."""
    if os.name == "nt":
        from config import windows

        return os.path.isfile(path) and (windows.readable_by_others(path) or windows.readable_by_others(
            os.path.dirname(os.path.abspath(path)), for_new_files=True))
    try:
        return bool(stat.S_IMODE(os.stat(path).st_mode) & 0o077)
    except OSError:
        return False
