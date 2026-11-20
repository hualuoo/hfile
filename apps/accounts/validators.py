"""What usernames and nicknames may be, wherever one is set: the installer's first admin, the Django
admin, later the user management on /manage/.

A username logs in and is unique whatever its case (apps/accounts/models.py). It is kept NFKC, so
full-width letters and digits are the ASCII ones (User.normalize_username); then Django's own rule:
letters (Chinese too), digits and @ . + - _. A nickname is only shown.
"""

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _, ngettext

from apps.core.text import reject_invisible, single_line

# The built-in users' names (apps/accounts/models.py, BUILTIN_USERNAMES).
RESERVED_USERNAMES = frozenset({"guest", "template"})

USERNAME_MIN = 1
USERNAME_MAX = 150  # AbstractUser.username
NICKNAME_MAX = 50


def check_username(value: str) -> None:
    """`value` is NFKC already. Raises ValidationError."""
    if not USERNAME_MIN <= len(value) <= USERNAME_MAX:
        raise ValidationError(ngettext("A username must be %(min)s to %(max)s character long.", "A username must be %(min)s to %(max)s characters long.", USERNAME_MAX) % {"min": USERNAME_MIN, "max": USERNAME_MAX})
    reject_invisible(value, _("Username"))  # the Hangul filler "ㅤ" is a letter for \w
    UnicodeUsernameValidator(message=_("A username can only have letters (Chinese too), digits and @ . + - _."))(value)
    if value.casefold() in RESERVED_USERNAMES:
        raise ValidationError(_("\"%(name)s\" is a built-in user's name. Please choose another.") % {"name": value})


def validate_nickname(value: str) -> None:
    if len(value) > NICKNAME_MAX:
        raise ValidationError(ngettext("A nickname is at most %(limit)s character.", "A nickname is at most %(limit)s characters.", NICKNAME_MAX) % {"limit": NICKNAME_MAX})
    reject_invisible(value, _("Nickname"))


def clean_nickname(value: str) -> str:
    """NFC-normalised, trimmed, each run of whitespace one space, then checked."""
    text = single_line(value)
    validate_nickname(text)
    return text
