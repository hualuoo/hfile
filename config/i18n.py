"""The language of what HFile says outside a page: the `hfile` commands, the errors before the site
starts, a new config.toml.

It follows the system, as other programs do: the first of LANGUAGE, LC_ALL, LC_MESSAGES and LANG that
is set (LANG=en_US.UTF-8: English); on Windows, where none usually is, the language Windows shows. A
language HFile doesn't have, or none (C, POSIX), is Chinese, the site's default language
(settings.LANGUAGE_CODE). Pages have their own (settings.LANGUAGES, LocaleMiddleware).
The texts in the code are English; config/locale/zh_Hans/ has them in Chinese.

_() and ngettext() are for code that also runs before Django has started (configfile.py, cli.py): once
the apps are loaded they are Django's, in the language that is active (a page's, or this one in a command,
cli.py); before, the same catalog (config/locale/) read with Python's gettext, in this language.
"""

import gettext as _gettext
import os
from functools import cache
from pathlib import Path

LOCALE_DIR = Path(__file__).resolve().parent / "locale"
DEFAULT = "zh-hans"
ENGLISH = "en"  # the language of the texts in the code: it has no catalog


def system_language() -> str:
    """"en" or "zh-hans", from the environment."""
    for name in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(name, "")
        if value:
            # LANGUAGE is a list ("en_GB:en"); the others one locale ("en_US.UTF-8").
            first = value.split(":", 1)[0].lower()
            return ENGLISH if first.startswith("en") else DEFAULT
    if os.name == "nt":
        from .windows import ui_language

        return ENGLISH if ui_language().lower().startswith("en") else DEFAULT
    return DEFAULT


@cache
def _catalog(language: str) -> _gettext.NullTranslations:
    if language == ENGLISH:
        return _gettext.NullTranslations()
    return _gettext.translation("django", localedir=LOCALE_DIR, languages=["zh_Hans"], fallback=True)


def _(message: str) -> str:
    from django.apps import apps

    if apps.ready:
        from django.utils.translation import gettext

        return gettext(message)
    return _catalog(system_language()).gettext(message)


def ngettext(singular: str, plural: str, number: int) -> str:
    """_() for a text with a count in it: the form for `number`."""
    from django.apps import apps

    if apps.ready:
        from django.utils import translation

        return translation.ngettext(singular, plural, number)
    return _catalog(system_language()).ngettext(singular, plural, number)


def gettext_noop(message: str) -> str:
    """Marks a text for the catalog where it is written (at import, when the language isn't known yet);
    _() translates it where it is used."""
    return message
