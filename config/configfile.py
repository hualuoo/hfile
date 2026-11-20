"""config.toml: HFile's settings file (listen address and port, database, site, folders).

Which file: `--config <path>` on the command line (config/cli.py), else the HFILE_CONFIG
environment variable (cli.py sets it for what it starts), else config.toml next to the program:
next to the executable when HFile is built as one, next to manage.py when it runs from the
source code. cli.py creates the file when it's missing; config.example.toml is a copy of a new one.
Folders in it are relative to the file's own folder.

Every setting is one row of OPTIONS: its section and name, its default, the function that checks
it and the lines written above it in a new file, translated then (config/i18n.py: the system's language
before Django starts, a page's in the installer). render() writes the file from the table
(config.example.toml too, in Chinese, a test keeps them equal), clean() checks a file against it,
listing every mistake at once, so a typo stops the program with a message instead of being ignored; a file
mostly of other things than HFile's sections and settings is said to be another program's instead.
The installer writes the file again with what it was given (update): first config.toml.part, read
back and compared, then renamed over the old one, mode 600. Comments written by hand are lost.

No Django imports here: settings.py reads this before Django is set up.
"""

import ipaddress
import os
import re
import sys
import tomllib
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import admin_path, private
from .i18n import _, gettext_noop, ngettext

FILE_NAME = "config.toml"
ENV_VAR = "HFILE_CONFIG"
BASE_DIR = Path(__file__).resolve().parent.parent  # the source code's root, where manage.py is


def _nuitka():
    """Nuitka's __compiled__ (a global of every module it compiled), or None."""
    return globals().get("__compiled__") or getattr(sys.modules.get("__main__"), "__compiled__", None)


# Built as a single executable: PyInstaller sets sys.frozen, Nuitka gives __compiled__ instead.
FROZEN = bool(getattr(sys, "frozen", False) or _nuitka())
# Nuitka makes sys.argv[0] the executable's absolute path, symlinks resolved, in onefile and
# standalone builds alike; its sys.executable names a file that doesn't exist. Kept from the start,
# before anything could change the current directory.
_ARGV0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""


def executable():
    """The executable the user started, when HFile is built as one; else None."""
    if getattr(sys, "frozen", False):  # PyInstaller: sys.executable is the executable itself
        return sys.executable
    if _nuitka() is not None:
        return _ARGV0
    return None


def program_dir():
    """The folder of the program the user started: the executable's when built as one (not the
    temporary folder it unpacks into, nor the current directory), else the source code's."""
    program = executable()
    return Path(program).resolve().parent if program else BASE_DIR


def locate(given=None):
    """The settings file's path: `given` (--config), else $HFILE_CONFIG, else next to the program.
    A folder means the config.toml in it."""
    raw = given or os.environ.get(ENV_VAR) or ""
    path = _expanded(raw) if raw else program_dir() / FILE_NAME
    path = Path(os.path.abspath(path))
    if raw.endswith(("/", os.sep)):  # a folder, even one that isn't there yet (abspath drops the /)
        return path / FILE_NAME
    try:
        is_dir = path.is_dir()
    except OSError:  # a name too long, say: then it isn't one, and reading it says what is wrong
        is_dir = False
    return path / FILE_NAME if is_dir else path


def _expanded(value):
    """~ and ~user as the home folder. ~ of a user that doesn't exist stays as it is."""
    try:
        path = Path(value).expanduser()
    except RuntimeError:
        return Path(value)
    head = re.split(r"[\\/]", value, maxsplit=1)[0]
    # Python on Windows takes ~user for the folder beside this user's home, whether there is one or not.
    if os.name == "nt" and len(head) > 1 and head[0] == "~" and not os.path.isdir(os.path.expanduser(head)):
        return Path(value)
    return path


def split_argv(args):
    """Takes --config out of a command line, before or after the command.

    Returns (the path or None, the other arguments). Raises ConfigError when no path follows it.
    """
    given, rest = None, []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":  # the rest belongs to the command
            rest.extend(args[i:])
            break
        if arg == "--config" or arg.startswith("--config="):
            value = arg.partition("=")[2] if "=" in arg else (args[i + 1] if i + 1 < len(args) else "")
            if not value or (arg == "--config" and value.startswith("-")):
                raise ConfigError(_("--config needs the path of a settings file after it, like --config /srv/hfile/config.toml."))
            given = value
            i += 1 if "=" in arg else 2
            continue
        rest.append(arg)
        i += 1
    return given, rest


def folder(config_path, value):
    """A folder or file named in the file: relative to the file's own folder."""
    path = _expanded(value)
    return path if path.is_absolute() else Path(config_path).parent / path


# ---- The checks of the table ----


class ConfigError(Exception):
    """The file can't be used. str() is the whole message, with every problem on its own line."""

    def __init__(self, message, problems=()):
        super().__init__(message)
        self.message, self.problems = message, list(problems)

    def __str__(self):
        return "\n".join([self.message, *(f"  - {problem}" for problem in self.problems)])


class Problem(ValueError):
    """What is wrong with one value."""


def toml_value(value):
    """A value as TOML writes it: a string in double quotes, true/false, a number, a list."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    raise TypeError(f"not a config value: {value!r}")


def _toml_string(text):
    out = ['"']
    for ch in text:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\t":
            out.append("\\t")
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:  # control characters must be escaped
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _shown(value):
    """A value as it was written, for messages: 现在是 "8080" (it is now "8080")."""
    try:
        return toml_value(value)
    except TypeError:
        return str(value)


def _text(value):
    if not isinstance(value, str):
        raise Problem(_("Must be text in double quotes; it is %(value)s now.") % {"value": _shown(value)})
    return value


def _nonblank(value):
    value = _text(value).strip()
    if not value or any(ch.isspace() for ch in value):
        raise Problem(_("Can't be empty or contain spaces."))
    return value


def _integer(low, high):
    def check(value):
        if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
            raise Problem(_("Must be a whole number from %(low)s to %(high)s (no quotes); it is %(value)s now.") % {
                "low": low, "high": high, "value": _shown(value)})
        return value

    return check


def _boolean(value):
    if not isinstance(value, bool):
        raise Problem(_("Must be true or false (lower case, no quotes); it is %(value)s now.") % {"value": _shown(value)})
    return value


def _listen_host(value):
    value = _text(value).strip()
    if value.startswith("[") and value.endswith("]"):  # [::]
        value = value[1:-1]
    if not value or any(ch.isspace() for ch in value):
        raise Problem(_('Must be an IP, like "0.0.0.0" (all addresses) or "127.0.0.1" (this machine only).'))
    return value


def _path(value):
    """A file or folder: anything on one line (spaces too, as in Windows folders)."""
    value = _text(value).strip()
    if not value:
        raise Problem(_("Can't be empty."))
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        raise Problem(_("Can't contain control characters such as line breaks."))
    if value.startswith("~") and _expanded(value) == Path(value):
        user = re.split(r"[\\/]", value, maxsplit=1)[0]
        raise Problem(_("The user %(user)s doesn't exist, so it has no home folder.") % {"user": user})
    if any(len(part.encode("utf-8", "surrogatepass")) > 255 for part in Path(value).parts):
        raise Problem(_("A part of the path is too long: each name in it is at most 255 bytes."))
    return value


ENGINES = ("sqlite", "mysql")


def _engine(value):
    engine = _text(value).strip().lower()
    engine = "sqlite" if engine == "sqlite3" else engine
    if engine not in ENGINES:
        raise Problem(_('Must be "sqlite" or "mysql"; it is %(value)s now.') % {"value": _shown(value)})
    return engine


# [database] tls: MySQL's --ssl-mode, without DISABLED. The last two check the server's certificate
# with [database] ca (SEC-08).
TLS_MODES = ("preferred", "required", "verify_ca", "verify_identity")
TLS_CHECKED = ("verify_ca", "verify_identity")


def _tls(value):
    mode = _text(value).strip().lower()
    if mode not in TLS_MODES:
        raise Problem(_('Must be "preferred", "required", "verify_ca" or "verify_identity"; it is %(value)s now.') % {
            "value": _shown(value)})
    return mode


_DB_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")


def _db_name(value):
    value = _text(value).strip()
    if not _DB_NAME_RE.fullmatch(value):
        raise Problem(_("Can only have English letters, digits and underscores, 64 at most."))
    return value


SECRET_KEY_MIN = 32


def _secret_key(value):
    value = _text(value)
    if value and len(value) < SECRET_KEY_MIN:
        raise Problem(ngettext("Too short: at least %(limit)s character. Left empty, the setup wizard generates one.",
                               "Too short: at least %(limit)s characters. Left empty, the setup wizard generates one.", SECRET_KEY_MIN)
                      % {"limit": SECRET_KEY_MIN})
    return value


_HOST_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME_RE = re.compile(rf"\.?(?:{_HOST_LABEL}\.)*{_HOST_LABEL}")
HOSTS_MAX = 50


def host_of(value):
    """As ALLOWED_HOSTS wants it: "Example.com:8080" -> "example.com", "[::1]:8080" or "::1" -> "[::1]"
    (Django compares an IPv6 Host with its brackets), "例子.中国" -> "xn--fsqu00a.xn--fiqs8s" (as
    browsers send it); "" when it isn't a host."""
    text = value.strip().lower().rstrip(".")
    if text.startswith("["):  # [IPv6]:port
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        pass
    else:
        return f"[{ip}]" if ip.version == 6 else str(ip)
    if not text.isascii():
        text = _punycode(text)
    return text if len(text) <= 253 and _HOSTNAME_RE.fullmatch(text) else ""


def _punycode(host):
    """A host in another script as browsers send it, "" when it can't be (OPS-09). A leading dot
    (.example.com: and its subdomains) stays."""
    import idna  # httpx's; only here, as every start reads this file

    dot = "." if host.startswith(".") else ""
    try:  # UTS #46, as browsers: Python's own codec is IDNA 2003, which made faß.de fass.de
        return dot + idna.encode(host[len(dot):], uts46=True).decode("ascii")
    except UnicodeError:  # idna's errors are UnicodeErrors; IDNA 2008 refuses some that browsers take (☃.net)
        return ""


def not_hosts(items):
    """What to say about items of a list of hosts that aren't one (the first three, as written). The
    installer's form says it too."""
    shown = _(", ").join(_('"%(host)s"') % {"host": item} if isinstance(item, str) else _shown(item) for item in items[:3])
    count = len(items[:3])
    if any(isinstance(item, str) and not item.isascii() for item in items[:3]):
        return ngettext("%(hosts)s isn't a domain name or IP. A domain name in another script must "
                        "convert to the xn-- form: check how it is written, or write it in the xn-- form.",
                        "%(hosts)s aren't domain names or IPs. A domain name in another script must convert "
                        "to the xn-- form: check how they are written, or write them in the xn-- form.",
                        count) % {"hosts": shown}
    return ngettext("%(hosts)s isn't a domain name or IP.", "%(hosts)s aren't domain names or IPs.", count) % {"hosts": shown}


def _hosts(value):
    if not isinstance(value, list):
        raise Problem(_('Must be a list, like ["files.example.com", "127.0.0.1"]; it is %(value)s now.') % {"value": _shown(value)})
    hosts, bad = [], []
    for item in value:
        if not isinstance(item, str):
            bad.append(item)
            continue
        host = "*" if item.strip() == "*" else host_of(item)
        if not host:
            bad.append(item)
        elif host not in hosts:
            hosts.append(host)
    if bad:
        raise Problem(not_hosts(bad))
    if len(hosts) > HOSTS_MAX:
        raise Problem(ngettext("%(limit)s at most.", "%(limit)s at most.", HOSTS_MAX) % {"limit": HOSTS_MAX})
    return hosts


def _site_url(value):
    value = _text(value).strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018  (raises ValueError for a port that isn't a number)
    except ValueError:
        parts = None
    if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
        raise Problem(_('Must start with https:// or http://, like "https://files.example.com".'))
    if parts.path not in ("", "/") or parts.query or parts.fragment or "@" in parts.netloc:
        raise Problem(_('Ends with the domain name (and port), with no path after it, like "https://files.example.com".'))
    if not host_of(parts.netloc):  # it said 要以 https:// 开头 (OPS-09)
        raise Problem(not_hosts([parts.hostname]))
    # As a browser writes the page's origin (CSRF_TRUSTED_ORIGINS is compared with it exactly, and full
    # links start with it): lower case, no dot at the end, IPv6 shortened, the port only when it isn't
    # the scheme's own (https://files.example.com:443 is https://files.example.com), punycode.
    host = parts.hostname.rstrip(".")
    if not host.isascii():
        host = host_of(host)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        host = f"[{ip.compressed}]" if ip.version == 6 else str(ip)
    port = parts.port
    if port == 0:
        raise Problem(_("The port can't be 0."))
    if port is not None and port != {"http": 80, "https": 443}[parts.scheme]:
        host = f"{host}:{port}"
    return f"{parts.scheme}://{host}"


def site_url(value):
    """[site] url as the file keeps it: "https://files.example.com", or "" for none. Raises Problem
    (a ValueError) saying what is wrong. The installer's form checks its field with it."""
    return _site_url(value)


def file_path(value):
    """A file's path as [database] file and [paths] keep it. Raises Problem (a ValueError) saying what is
    wrong (~nosuchuser/x, control characters, a name too long). The installer checks its field with it."""
    return _path(value)


def _admin_path(value):
    path = admin_path.clean(_text(value))
    if path and (problem := admin_path.problem(path)):
        raise Problem(problem)
    return path


# ---- The table ----


@dataclass(frozen=True)
class Option:
    section: str
    key: str
    default: object  # None: may be left out (a new file has the line commented out, with `example`)
    check: object  # value -> the value to use; raises Problem
    comment: str = ""  # the lines above it in a new file (gettext_noop: translated by render())
    example: object = None


OPTIONS = (
    Option("server", "host", "0.0.0.0", _listen_host,
           gettext_noop('The IP to listen on. "0.0.0.0" is every IPv4 address of this machine, "::" every IPv6 address.\n'
           'To let in only a reverse proxy on this machine (Nginx, Caddy), write "127.0.0.1".\n'
           "python manage.py runserver doesn't use this section.")),
    Option("server", "port", 8080, _integer(1, 65535), gettext_noop("Port")),
    Option("server", "threads", 8, _integer(1, 64), gettext_noop("How many requests are handled at the same time; downloads through the site have threads of their own")),
    Option("server", "proxies", 0, _integer(0, 5),
           gettext_noop("How many reverse proxies are in front: 0 when the site faces the internet itself, 1 behind a single Nginx or Caddy.\n"
           "When it isn't 0, the visitor's IP is the one that many from the right in X-Forwarded-For, and X-Forwarded-Proto\n"
           "says whether it is https; the proxies must pass on these two headers and Host. At 0 these headers are ignored,\n"
           "as anyone can forge them.")),
    Option("server", "log_requests", True, _boolean,
           gettext_noop("One line in the log for each request (as Go's Gin does): status, time taken, the visitor's IP, method and address,\n"
           "size, browser. The admin's address and the tokens and signatures in links are not printed.\n"
           "python manage.py runserver doesn't use this; it has its own.")),
    Option("database", "engine", "sqlite", _engine,
           gettext_noop('The kind of database: "sqlite" or "mysql".\n'
           'sqlite: the data is kept in one file (file below), with no database server to install; for a site without much traffic.\n'
           'mysql: MySQL 8.4 or later, with host, port, name, user and password below.')),
    Option("database", "file", "hfile.sqlite3", _path,
           gettext_noop("The SQLite database file. Keep it on a disk of this machine, not in a folder shared over the network.\n"
           "The -wal and -shm files of the same name next to it are part of the database too: while the program runs,\n"
           "don't copy this file alone as a backup; use the backup command.")),
    Option("database", "host", "127.0.0.1", _nonblank, gettext_noop("MySQL's address, or the path of a Unix socket (starting with /).")),
    Option("database", "port", 3306, _integer(1, 65535)),
    Option("database", "name", "hfile", _db_name),
    Option("database", "user", "hfile", _nonblank),
    Option("database", "password", "", _text),
    Option("database", "tls", "preferred", _tls,
           gettext_noop('Encryption (TLS) of the connection to MySQL: "preferred" encrypts when the server has TLS on (MySQL\'s own default);\n'
           '"required" always encrypts, or doesn\'t connect; "verify_ca" also checks the server\'s certificate with ca below;\n'
           '"verify_identity" also wants the name on the certificate to be host.\n'
           'When MySQL is on another machine, use verify_ca or verify_identity: encrypting without checking the certificate\n'
           "doesn't stop a machine posing as the database.")),
    Option("database", "ca", None, _path,
           gettext_noop("The CA certificate file (PEM) that checks the MySQL server's certificate; needed when tls is verify_ca or verify_identity."),
           example="mysql-ca.pem"),
    Option("site", "secret_key", "", _secret_key,
           gettext_noop("The site's secret key: logins and the storage passwords kept in the database depend on it.\n"
           "Left empty, the program starts in setup mode (the whole site is the setup page); the setup wizard writes one here.\n"
           "Don't change it once installed: everyone would have to log in again, and the encrypted passwords couldn't be read\n"
           "any more.")),
    Option("site", "allowed_hosts", ["127.0.0.1", "localhost"], _hosts,
           gettext_noop("The site's domain names or IPs: it answers only when it is opened at one of them. The domain of url below is added by itself.")),
    Option("site", "url", "", _site_url,
           gettext_noop('The site\'s public address, like "https://files.example.com". Full links (direct links, shares) use it,\n'
           'and it is an origin CSRF trusts; when it starts with https://, the login cookies go over https only.\n'
           'Empty: links use the address of each request.')),
    Option("site", "admin_path", "", _admin_path,
           gettext_noop("The address of the Django admin (Django's own admin site): /<this part>/.\n"
           "Empty: a random one made from the secret key, printed when the program starts.\n"
           "Only English letters, digits, - and _. /admin/ is always a 404.")),
    Option("site", "debug", False, _boolean,
           gettext_noop("Debug mode: an error shows its details on the page, and the API can be browsed in a browser.\n"
                        "Turn it on only for development on your own computer.")),
    Option("paths", "uploads", "media", _path,
           gettext_noop("The files uploaded to the site itself (such as its logo and icon) are kept in this folder. Their addresses are /media/….\n"
           "When you change it, move the files of the old folder there too.")),
    Option("paths", "backups", "backups", _path, gettext_noop("Database backups: those made before an upgrade, and those of the backup command")),
    Option("upgrade", "auto", None, _boolean,
           gettext_noop('When the database needs an upgrade at start: back it up, then upgrade it by itself (README, "Upgrading").\n'
           "Left out: true for the single-file program; false when running "
           "from source, which upgrades with python manage.py upgrade."),
           example=True),
)

# The sections in their order, each with its rows: {"server": [Option, ...], ...}
SECTIONS = {}
for _option in OPTIONS:
    SECTIONS.setdefault(_option.section, []).append(_option)

HEADER = gettext_noop(
    "HFile's settings. A change takes effect when the program is started again.\n"
    "It holds the database password and the site's secret key: don't send it to anyone, and keep it out of code repositories.\n"
    "The format is TOML: text in double quotes, numbers and true/false without.\n"
    "Files and folders may be relative paths, from the folder of this file."
)

SECRET_KEYS = {("database", "password"), ("site", "secret_key")}  # never shown in a message


def defaults():
    return {section: {o.key: o.default for o in options} for section, options in SECTIONS.items()}


def _looks_like_hfiles(raw):
    """Whether at least half of the entries at the top of a parsed file are HFile's: one of its sections, one of its
    settings written outside a section, or a table holding one of its settings (a misspelled section, as [sever]
    with port in it), in any case. Another program's config.toml may share a few of these names, as port or [server]."""
    ours = {name.lower() for name in SECTIONS} | {option.key.lower() for option in OPTIONS}
    hfile = sum(
        name.lower() in ours or (isinstance(value, dict) and any(key.lower() in ours for key in value))
        for name, value in raw.items()
    )
    return 2 * hfile >= len(raw)


def clean(raw, path="config.toml"):
    """The settings from a parsed file (a dict): checked, with defaults for what is left out.
    Raises ConfigError listing every problem."""
    if not _looks_like_hfiles(raw):
        # Another program's config.toml, like one in the downloads folder HFile was started in: its settings
        # listed as HFile's mistakes would have it changed for HFile, which would break it.
        raise ConfigError("\n".join([
            _("The settings file %(path)s doesn't seem to be HFile's: most of what is in it isn't one of HFile's "
              "sections (%(sections)s) or settings, so it is probably another program's file of the same name. "
              "HFile leaves it as it is.")
            % {"path": path, "sections": _(", ").join(f"[{s}]" for s in SECTIONS)},
            _("Put HFile in a folder of its own and start it there, where it creates a settings file of its own; "
              "or choose another settings file with --config."),
        ]))
    problems = []
    for name, value in raw.items():
        if name not in SECTIONS:
            if isinstance(value, dict):
                problems.append(_("[%(section)s]: There is no such section; check the spelling. The sections are: %(sections)s.") % {
                    "section": name, "sections": _(", ").join(f"[{s}]" for s in SECTIONS)})
            else:
                problems.append(_("%(name)s: Must be in a section, after a line such as [server].") % {"name": name})
    values = {}
    for section, options in SECTIONS.items():
        given = raw.get(section, {})
        if not isinstance(given, dict):
            problems.append(_("[%(section)s]: Must be a section (a line [%(section)s] on its own, with the settings below it).") % {"section": section})
            given = {}
        known = [o.key for o in options]
        for key in given:
            if key not in known:
                problems.append(_("[%(section)s] %(key)s: There is no such setting; check the spelling. This section can have: %(keys)s.") % {
                    "section": section, "key": key, "keys": _(", ").join(known)})
        values[section] = {}
        for option in options:
            if option.key not in given:
                values[section][option.key] = option.default
                continue
            try:
                values[section][option.key] = option.check(given[option.key])
            except Problem as problem:
                if (section, option.key) in SECRET_KEYS and not isinstance(given[option.key], str):
                    problem = _("Must be text in double quotes (its value isn't shown here).")  # not echoed, as 现在是 12345
                problems.append(_("[%(section)s] %(key)s: %(problem)s") % {
                    "section": section, "key": option.key, "problem": problem})
    db = values["database"]
    if "tls" in db and "ca" in db:  # both right on their own
        if db["tls"] in TLS_CHECKED and db["ca"] is None:
            problems.append(_('[database] ca: With tls "verify_ca" or "verify_identity", name the CA certificate file that checks the certificate.'))
        elif db["tls"] not in TLS_CHECKED and db["ca"] is not None:
            problems.append(_('[database] ca: The certificate is checked only when tls is "verify_ca" '
                              'or "verify_identity"; tls is "%(tls)s" now, so ca isn\'t used.') % {"tls": db["tls"]})
    site = values["site"]
    if site.get("allowed_hosts") == [] and site.get("url") == "":
        # Every request was refused (400) by a site that started as if all was well (OPS-04).
        problems.append(_('[site] allowed_hosts: It is empty and so is url, so the site answers 400 at any address. '
                          'Write at least one domain name or IP (like ["127.0.0.1", "localhost"]), or fill in url.'))
    if problems:
        raise ConfigError(ngettext("The settings file %(path)s has %(count)s thing to fix:", "The settings file %(path)s has %(count)s things to fix:",
                                   len(problems)) % {"path": path, "count": len(problems)}, problems)
    return values


def _where(message):
    """tomllib's "(at line 3, column 7)" in the language of the messages."""
    message = re.sub(r"\s*\(at line (\d+), column (\d+)\)",
                     lambda at: _(" (line %(line)s, column %(column)s)") % {"line": at[1], "column": at[2]}, message)
    return re.sub(r"\s*\(at end of document\)", lambda at: _(" (at the end of the file)"), message)


def read(path):
    """The file as TOML: a dict, empty when there is no file. Raises ConfigError."""
    path = Path(path)
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as error:
        raise ConfigError(_("Can't read the settings file %(path)s: %(error)s.") % {"path": path, "error": error.strerror or error}) from None
    if data.startswith(b"\xef\xbb\xbf"):  # saved by Windows Notepad as "UTF-8 with BOM"
        data = data[3:]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ConfigError(_("The settings file %(path)s must be saved in UTF-8.") % {"path": path}) from None
    try:
        return tomllib.loads(text)
    except RecursionError:  # [[[[[... thousands deep: tomllib reads it by recursion
        raise ConfigError(_("The settings file %(path)s isn't valid: a list or table is nested too deep.") % {"path": path}) from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError("\n  ".join([
            _("The settings file %(path)s isn't valid: %(error)s.") % {"path": path, "error": _where(str(error))},
            _('The format is TOML: one "name = value" on each line, text in double quotes, numbers and true/false without.'),
        ])) from None


def load(path):
    """The settings in the file at `path`, or the defaults when there is no file. Raises ConfigError."""
    return clean(read(path), path)


def _comment(text):
    """A text of the table as the comment lines of the file, translated. Nothing for none: _("") is the
    catalog's own header."""
    return [f"# {line}" for line in _(text).splitlines()] if text else []


def render(values=None):
    """The text of a settings file with `values` ({section: {key: value}}) and defaults for the rest."""
    values = values or {}
    lines = _comment(HEADER)
    for section, options in SECTIONS.items():
        lines += ["", f"[{section}]"]
        for option in options:
            lines += _comment(option.comment)
            value = values.get(section, {}).get(option.key, option.default)
            if value is None:
                lines.append(f"# {option.key} = {toml_value(option.example)}")
            else:
                lines.append(f"{option.key} = {toml_value(value)}")
    return "\n".join(lines) + "\n"


def _write_file(path, text, *, exclusive):
    """A new file only for this user (600), on the disk before this returns. Never through a link or
    into a file that is there already (exclusive), else into one made anew."""
    # O_BINARY: Windows would add a \r before each \n on its own.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    if not exclusive:
        # Left behind by a write that was cut short: maybe with other modes, or even a link to elsewhere.
        with suppress(FileNotFoundError):
            os.unlink(path)
    fd = os.open(path, flags, 0o600)
    private.made(path)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())  # else a power cut right after the rename may leave an empty config.toml


def _sync_folder(path):
    """After a rename: the folder's entry on the disk too (Linux). Not possible everywhere (Windows)."""
    with suppress(OSError):
        fd = os.open(Path(path).parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def create(path):
    """Writes a new file with the defaults when there is none at `path`. Returns whether it did.
    Raises ConfigError when it can't."""
    path = Path(path)
    if path.exists():
        return False
    if not path.parent.is_dir():
        raise ConfigError(_("The folder %(folder)s doesn't exist, so the settings file can't be created "
                            "there. Create the folder first, or choose another place with --config.")
                          % {"folder": path.parent})
    try:
        _write_file(path, render(), exclusive=True)
    except FileExistsError:  # another process made it meanwhile
        return False
    except OSError as error:
        raise ConfigError(_("Can't create the settings file %(path)s: %(error)s. Choose a place that can be written to with --config.")
                          % {"path": path, "error": error.strerror or error}) from None
    return True


def write(path, values):
    """Writes the whole file (mode 600) with `values`, after checking that it reads back as them.
    Raises ConfigError, or OSError when the file can't be written."""
    path = Path(path)
    full = defaults()
    for section, changes in values.items():
        full[section].update(changes)
    text = render(full)
    parsed = tomllib.loads(text)
    intended = {
        section: {key: list(value) if isinstance(value, tuple) else value for key, value in items.items() if value is not None}
        for section, items in full.items()
    }
    if parsed != intended:
        raise ConfigError(_("Some values can't be written into the settings file %(path)s as they are.") % {"path": path})
    clean(parsed, path)  # and they have to be right
    temporary = path.with_name(path.name + ".part")
    _write_file(temporary, text, exclusive=False)
    if os.name == "nt":  # serve's lock keeps the file open (hold_while_serving), which os.replace() is refused
        from . import windows

        windows.replace(temporary, path)
    else:
        os.replace(temporary, path)
    _sync_folder(path)
    return text


def update(path, changes):
    """Writes `changes` ({section: {key: value}}) into the file, keeping its other values."""
    values = load(path)
    for section, items in changes.items():
        values[section].update(items)
    return write(path, values)


def allowed_hosts(values):
    """ALLOWED_HOSTS: [site] allowed_hosts, plus the host of [site] url."""
    hosts = list(values["site"]["allowed_hosts"])
    url = values["site"]["url"]
    if url and (host := host_of(urlsplit(url).netloc)) not in hosts:
        hosts.append(host)
    return hosts


# -------------------------------------------------- a site that runs with this file

def hold_while_serving(path):
    """serve keeps a shared lock on its settings file while it runs (the system lets go of it when the
    process ends), so that `restore` and `upgrade` can see the site is running: in_use(). Returns the
    open file (on Windows a lock, config/windows.py), to be kept as long as the site runs; None where that
    can't be done."""
    if os.name == "nt":
        from . import windows

        try:
            return windows.lock(path, exclusive=False)
        except OSError:
            return None
    try:
        import fcntl
    except ImportError:
        return None
    try:
        file = open(path, "rb")
    except OSError:
        return None
    try:
        fcntl.flock(file, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:  # a filesystem without locks
        file.close()
        return None
    return file


def in_use(path):
    """Whether a site (serve) runs with the settings file at `path`, on this machine. False when it
    can't tell: a site served some other way, with another settings file or elsewhere."""
    if os.name == "nt":
        from . import windows

        try:
            lock = windows.lock(path, exclusive=True)
        except OSError:
            return False
        if lock is None:
            return True
        lock.close()
        return False
    try:
        import fcntl
    except ImportError:
        return False
    try:
        with open(path, "rb") as file:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    return False
