"""Where the Django admin lives: /<settings.ADMIN_PATH>/, a random path rather than /admin/,
so that scanners looking for Django's login page don't find it. /admin/ is always a 404.

[site] admin_path in config.toml sets it; the installer writes one, typed in or random. Without it
the path comes from SECRET_KEY (path_from_key): random-looking, different on every site, and the
same on every start. The /manage/ page links to it, and runserver and serve print it.

No Django imports here: settings.py reads this before Django is set up.
"""

import hashlib
import hmac
import re
import secrets

from .i18n import _, ngettext

# 32 characters without look-alikes (no l/1, o/0), so a written-down address can be typed back.
ALPHABET = "abcdefghijkmnpqrstuvwxyz23456789"
LENGTH = 12  # 60 bits
MAX_LENGTH = 64
_PATH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*")
# The first path segments the site uses or will use (config/urls.py, apps/web/urls.py, the installer).
# A new app with an address of its own at the top level adds its first segment here.
TAKEN = frozenset({
    "api", "static", "media", "install", "manage", "components", "favicon.ico", "robots.txt",
    "site",  # the custom CSS and JS of the 显示设置 (siteconf): /site/custom.css, /site/custom.js
    # Short links, proxied downloads, direct links (their default prefix), shares, single sign-on,
    # OnlyOffice, the login page, and a few more names kept free.
    "s", "pd", "directlink", "share", "sso", "onlyoffice", "file", "files", "login", "logout",
    # The storages app: the OAuth callback of OneDrive, SharePoint and Google Drive.
    "storages",
    # The shares app: 我的分享 (/shares/).
    "shares",
})
# Django's usual address: always a 404 here.
DJANGO_DEFAULT = "admin"

# Other addresses scanners try first. The installer refuses them: a random path is the point. A
# config.toml written by hand may still name one (refusing it there would stop a site that uses it).
GUESSABLE = frozenset({
    "administrator", "adm", "wp-admin", "django-admin", "djangoadmin", "backend", "dashboard",
    "console", "cms", "manager", "management", "admin-panel", "adminpanel", "panel",
})


def guessable(path):
    """Whether `path` (already clean()ed) is one of GUESSABLE, whatever its case."""
    return path.lower() in GUESSABLE


def random_path():
    return "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))


def path_from_key(secret_key):
    """The path of a site without an admin_path: a keyed hash of its SECRET_KEY."""
    digest = hmac.new(secret_key.encode(), b"hfile django admin path", hashlib.sha256).digest()
    return "".join(ALPHABET[byte % len(ALPHABET)] for byte in digest[:LENGTH])


def clean(text):
    """As typed: " /my-admin/ " -> "my-admin"."""
    return str(text).strip().strip("/").strip()


def problem(path):
    """What's wrong with `path` (already clean()ed) as the admin's address; "" when nothing is."""
    if not path:
        return _("Can't be empty.")
    if len(path) > MAX_LENGTH:
        return ngettext("At most %(limit)s character.", "At most %(limit)s characters.", MAX_LENGTH) % {"limit": MAX_LENGTH}
    if not _PATH_RE.fullmatch(path):
        return _("Can only have English letters, digits, - and _, and must start with a letter or a digit.")
    if path.lower() == DJANGO_DEFAULT:
        return _("/admin/ is always a 404 (scanners try it first). Please choose another.")
    if path.lower() in TAKEN:
        return _("/%(path)s/ is an address the site uses itself. Please choose another.") % {"path": path}
    return ""
