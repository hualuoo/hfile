#!/usr/bin/env python
"""Django's command-line utility for administrative tasks.

Like the single executable (hfile.py), it reads config.toml next to it, or the one given with
--config, and creates it when it's missing (config/cli.py).
"""

import sys

from config.cli import main

if __name__ == "__main__":
    sys.exit(main())
