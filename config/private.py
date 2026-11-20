"""Files that only the user running the site may read: config.toml, the SQLite database, backups, the
installer's token, uploads on their way in, and the folders made for backups and uploads. On Linux the mode
they are made with does that (600, 700); Windows ignores the mode, and there each gets an access list of its
own (config/windows.py make_private)."""

import os
from pathlib import Path


def made(path, *, folder=False):
    """For a file just made with mode 600 (os.open(path, ..., 0o600)), before anything is written to it; or a
    folder just made with mode 700 (folder), whose files and folders then take that too. On Windows a file
    system without access lists (FAT) leaves it as it is: nothing could keep others out there."""
    if os.name == "nt":
        from . import windows

        try:
            windows.make_private(path, folder=folder)
        except OSError:
            pass


def mkdir(path):
    """Path(path).mkdir(mode=0o700, parents=True, exist_ok=True); on Windows the folder, when this makes it,
    gets an access list of its own for that (made())."""
    path = Path(path)
    existed = path.is_dir()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not existed:
        made(path, folder=True)
