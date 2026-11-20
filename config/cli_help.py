"""The commands' help in the system's language, all of it (cli.py). argparse writes its own texts (the usage
line, the headings, its errors) and Django its own (the options every command has, its commands' descriptions
and options, the list of commands, the answer to an unknown one) in English: argparse's go through the catalog
(config/i18n.py), Django's are looked up in it where they're shown (translate, Utility), and cli_texts.py lists
both for it. HFile's commands translate their own where they're written.

The layout counts the columns of a terminal (columns): a Chinese character takes two, and argparse counts one,
so the help after a Chinese placeholder started further right than the others', and a Chinese text, which has
no spaces to wrap at, ran past the edge.
"""

import argparse
import sys
import unicodedata
from collections import defaultdict
from difflib import get_close_matches

from django.conf import settings
from django.core.management import ManagementUtility, get_commands
from django.core.management.base import BaseCommand, DjangoHelpFormatter
from django.core.management.color import color_style
from django.utils import translation
from django.utils.translation import gettext as _, pgettext

from . import i18n


def install():
    """argparse's texts through the catalog, and Django's in every command's parser (translate)."""
    argparse._, argparse.ngettext = i18n._, i18n.ngettext
    create_parser = BaseCommand.create_parser
    if getattr(create_parser, "translated", False):
        return

    def translated(self, prog_name, subcommand, **kwargs):
        kwargs.setdefault("formatter_class", Formatter)
        parser = create_parser(self, prog_name, subcommand, **kwargs)
        translate(parser)
        return parser

    translated.translated = True
    translated.__wrapped__ = create_parser  # Django's own, for the tests
    BaseCommand.create_parser = translated


def translate(parser):
    """The texts of a command's parser in the active language: Django's descriptions, helps and messages, and the
    placeholders of the options' values (argparse makes OUTPUT of --output). Those already in the language
    (argparse's, HFile's) stay as they are: the catalog has nothing for them."""

    def text(value):
        return _(value) if isinstance(value, str) and value != argparse.SUPPRESS else value

    parser.description, parser.epilog = text(parser.description), text(parser.epilog)
    parser.missing_args_message = text(parser.missing_args_message)
    for group in parser._action_groups:
        group.title, group.description = text(group.title), text(group.description)
    for action in parser._actions:
        action.help = text(action.help)
        if action.nargs == 0 or (action.metavar is None and action.choices is not None):
            continue  # it takes no value, or shows its choices ({0,1,2,3})
        names = action.metavar
        if names is None:
            names = action.dest.upper() if action.option_strings else action.dest
        if isinstance(names, tuple):
            shown = tuple(pgettext("placeholder", name) for name in names)
        else:
            shown = pgettext("placeholder", names)
        if shown != names:
            action.metavar = shown


def _wide(character):
    return unicodedata.east_asian_width(character) in "WF"


def columns(text):
    """The columns of a terminal `text` takes: two for a wide character (Chinese), one for the others."""
    return len(text) + sum(map(_wide, text))


def _pieces(text):
    """What a line of `text` may break between: a word, a wide character, a space. A wide punctuation mark that
    can't start a line (，。）」) is taken with the piece before it, one that can't end a line (（「) with the
    piece after it."""
    pieces = []
    for character in text:
        before = pieces[-1][-1] if pieces and pieces[-1] != " " else None
        if character != " " and before is not None and (
                _wide(character) and unicodedata.category(character) in ("Pe", "Pf", "Po")
                or _wide(before) and unicodedata.category(before) in ("Ps", "Pi")
                or not _wide(character) and not _wide(before)):
            pieces[-1] += character
        else:
            pieces.append(character)
    return pieces


def wrap(text, width):
    """The lines of `text`, of at most `width` columns, broken between its pieces; a piece wider than that is a
    line of its own."""
    lines, line = [], ""
    for piece in _pieces(text):
        if piece == " ":
            line += " " if line else ""
        elif line.strip() and columns(line) + columns(piece) > width:
            lines.append(line.rstrip())
            line = piece
        else:
            line += piece
    if line.strip():
        lines.append(line.rstrip())
    return lines


class Formatter(DjangoHelpFormatter):
    """Django's, laid out by the columns of a terminal. What has no wide character comes out as argparse writes it."""

    def _split_lines(self, text, width):
        if not any(map(_wide, text)):
            return super()._split_lines(text, width)
        return wrap(self._whitespace_matcher.sub(" ", text).strip(), width)

    def _fill_text(self, text, width, indent):
        if not any(map(_wide, text)):
            return super()._fill_text(text, width, indent)
        lines = wrap(self._whitespace_matcher.sub(" ", text).strip(), width - len(indent))
        return "\n".join(indent + line for line in lines)

    def add_argument(self, action):
        super().add_argument(action)
        if action.help is not argparse.SUPPRESS:  # the helps' column follows the widest invocation, as shown
            invocation = self._format_action_invocation(action)
            self._action_max_length = max(self._action_max_length, columns(invocation) + self._current_indent)

    def _format_action(self, action):
        text = super()._format_action(action)
        start = " " * self._current_indent + self._format_action_invocation(action)
        first, newline, rest = text.partition("\n")
        help_text = first[len(start):].lstrip(" ")
        if columns(start) == len(start) or not first.startswith(start) or not help_text:
            return text  # nothing wide, or its help starts on the next line already
        position = min(self._action_max_length + 2, self._max_help_position)
        padding = position - columns(start)
        if padding >= 2:
            first = start + " " * padding + help_text
        else:  # too wide for the column: the help on the next line, where the others' start
            first = start + "\n" + " " * position + help_text
        return first + newline + rest

    def _format_usage(self, usage, actions, groups, prefix):
        """argparse's, its lines after the first indented as far as the prefix shows (用法：), and wrapped
        narrower until the widest fits."""
        width, text = self._width, super()._format_usage(usage, actions, groups, prefix)
        if not any(map(_wide, text)):
            return text
        prefix = argparse._("usage: ") if prefix is None else prefix
        extra = columns(prefix) - len(prefix)
        try:
            for _attempt in range(10):
                first, *rest = text.rstrip("\n").split("\n")
                lines = [first, *(" " * extra + line if line.startswith(" ") else line for line in rest)]
                over = max(map(columns, lines)) - (width - self._current_indent)
                if over <= 0 or self._width - over < 20:
                    break
                self._width -= over
                text = super()._format_usage(usage, actions, groups, prefix)
        finally:
            self._width = width
        return "\n".join(lines) + "\n\n"


class Utility(ManagementUtility):
    """Django's, with the list of commands (hfile help) and the answer to an unknown command in the system's
    language: Django writes them in English."""

    def main_help_text(self, commands_only=False):
        if commands_only:
            return super().main_help_text(commands_only)
        with translation.override(i18n.system_language()):
            lines = ["", _("Type '%(program)s help <subcommand>' for help on a specific subcommand.") % {"program": self.prog_name},
                     "", _("Available subcommands:")]
            groups = defaultdict(list)
            for name, app in get_commands().items():
                groups["django" if app == "django.core" else app.rpartition(".")[-1]].append(name)
            style = color_style()
            for group in sorted(groups):  # by the app's label, as Django sorts them; shown by its name
                lines += ["", style.NOTICE("[%s]" % pgettext("command group", group)),
                          *("    %s" % name for name in sorted(groups[group]))]
            if self.settings_exception is not None:
                lines.append(style.NOTICE(
                    _("Note that only Django core commands are listed as settings are not properly configured (error: %(error)s).")
                    % {"error": self.settings_exception}))
            return "\n".join(lines)

    def fetch_command(self, subcommand):
        commands = get_commands()
        if subcommand in commands:
            return super().fetch_command(subcommand)
        settings.INSTALLED_APPS  # as Django: a mistake in the settings is raised, not taken for an unknown command
        with translation.override(i18n.system_language()):
            matches = get_close_matches(subcommand, commands)
            if matches:
                text = _("Unknown command: '%(command)s'. Did you mean %(match)s?") % {"command": subcommand, "match": matches[0]}
            else:
                text = _("Unknown command: '%(command)s'") % {"command": subcommand}
            sys.stderr.write(text + "\n" + _("Type '%(program)s help' for usage.") % {"program": self.prog_name} + "\n")
        sys.exit(1)
