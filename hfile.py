#!/usr/bin/env python
"""HFile's single executable is built from this file (scripts/build.sh).

    hfile                        starts the site, with config.toml next to the program
    hfile --config 路径          the same with that settings file (created when it's missing)
    hfile upgrade, backup, …     any manage.py command

See config/cli.py. From the source code, `python hfile.py` does the same.
"""

import sys

from config.cli import main

if __name__ == "__main__":
    sys.exit(main(default_command="serve"))
