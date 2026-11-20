#!/usr/bin/env python3
"""The translations of the pages, the scripts and the commands (config/locale/, docs/development.md 翻译).

    python scripts/i18n.py extract   # the texts in the code into config/locale/<语言>/LC_MESSAGES/*.po
    python scripts/i18n.py compile   # the .po files into .mo files and the scripts' web/i18n/<语言>.js

extract runs Django's makemessages for every language but English, the language of the texts in the
code: django.po (Python and the templates) and djangojs.po (the scripts). New texts come in untranslated,
texts no longer used are dropped. It needs GNU gettext (xgettext, msgmerge): apt install gettext, brew
install gettext, or conda install -c conda-forge gettext.

compile needs nothing more than Python (apps/web/catalogs.py). Commit the .po, the .mo and the .js files
together; apps/web/test_i18n.py fails while one of them doesn't match the others, or while a text has
no translation.

Both read the settings file as manage.py does (config.toml next to it, or $HFILE_CONFIG).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Not translated: tests, other people's code, what the build makes, the catalogs themselves.
IGNORE = [
    "*/tests/*", "test_*.py", "tests.py", "testing.py", "*/vendor/*", "*.min.js", "node_modules/*",
    "dist/*", "build/*", "staticfiles/*", "docs/*", "scripts/*", "config/locale/*", "apps/web/static/web/i18n/*",
]


def _django():
    sys.path.insert(0, str(ROOT))
    os.chdir(ROOT)
    from config import configfile

    path = configfile.locate()
    configfile.create(path)
    os.environ[configfile.ENV_VAR] = str(path)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    import django

    django.setup()


def extract():
    _django()
    from django.conf import settings
    from django.utils.translation import to_locale

    from config.i18n import ENGLISH

    languages = [to_locale(code) for code, _name in settings.LANGUAGES if code != ENGLISH]
    for domain in ("django", "djangojs"):
        command = [sys.executable, "manage.py", "makemessages", "--domain", domain, "--no-obsolete",
                   "--add-location", "file", *(f"--ignore={pattern}" for pattern in IGNORE)]
        for language in languages:
            command += ["--locale", language]
        subprocess.run(command, cwd=ROOT, check=True)


def compile_():
    _django()
    from apps.web.catalogs import compile_all

    for path in compile_all():
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    actions = {"extract": extract, "compile": compile_}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        sys.exit(__doc__)
    actions[sys.argv[1]]()
