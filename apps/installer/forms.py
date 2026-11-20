"""The installer's form. Nothing here reads the database: in setup mode there is none yet."""

import re

from django import forms
from django.contrib.auth import get_user_model, password_validation
from django.core.exceptions import ValidationError
from django.utils.text import format_lazy
from django.utils.translation import gettext as _, gettext_lazy, ngettext, ngettext_lazy

from apps.accounts.serializers import PASSWORD_MAX
from apps.accounts.validators import USERNAME_MAX, check_username
from config import admin_path
from config.configfile import Problem, file_path, host_of, not_hosts, site_url
from apps.core.text import reject_invisible, single_line
from apps.siteconf.models import DEFAULT_SITE_NAME, SITE_NAME_MAX

from .service import Database

_DB_NAME_RE = re.compile(r"[A-Za-z0-9_]{1,64}")
HOSTS_MAX = 20
LOCAL_HOSTS = ["127.0.0.1", "localhost"]
ENGINES = [("sqlite", "SQLite"), ("mysql", "MySQL")]
# [server] proxies: how many reverse proxies are in front (config.toml allows 0–5).
PROXIES = [
    (0, gettext_lazy("None: the site faces visitors directly")),
    (1, gettext_lazy("1 proxy: Nginx, Caddy, aaPanel, 1Panel, etc.")),
    (2, gettext_lazy("2 proxies: like a CDN with Nginx behind it")),
    # format_lazy: % on a lazy text would translate it now, in the language of the import
    *((n, format_lazy(ngettext_lazy("{count} proxy", "{count} proxies", n), count=n)) for n in range(3, 6)),
]


class InstallForm(forms.Form):
    """The database's fields are checked for the engine that was chosen only: the page sends the
    other engine's too, hidden, as they were."""

    # The page's steps and their fields: install.js shows one step at a time, and asks
    # views.CheckView whether it is right before it goes on to the next.
    STEPS = {
        "token": ("token",),
        "database": ("db_engine", "db_file", "db_host", "db_port", "db_name", "db_user", "db_password"),
        "site": ("site_name", "url", "hosts", "proxies", "admin_path"),
        "admin": ("username", "password1", "password2"),
    }

    token = forms.CharField(label=gettext_lazy("Install token"), max_length=64)

    db_engine = forms.ChoiceField(label=gettext_lazy("Database"), choices=ENGINES, initial="sqlite", widget=forms.RadioSelect)
    db_file = forms.CharField(label=gettext_lazy("Database file"), max_length=1000, required=False, initial="hfile.sqlite3")
    db_host = forms.CharField(label=gettext_lazy("Address"), max_length=255, required=False, initial="127.0.0.1")
    db_port = forms.CharField(label=gettext_lazy("Port"), max_length=10, required=False, initial="3306")
    db_name = forms.CharField(label=gettext_lazy("Database name"), max_length=64, required=False, initial="hfile")
    db_user = forms.CharField(label=gettext_lazy("Username"), max_length=80, required=False, initial="hfile")
    db_password = forms.CharField(
        label=gettext_lazy("Password"), max_length=256, required=False, strip=False, widget=forms.PasswordInput(render_value=True)
    )

    site_name = forms.CharField(label=gettext_lazy("Site name"), max_length=SITE_NAME_MAX * 2, initial=DEFAULT_SITE_NAME)
    url = forms.CharField(label=gettext_lazy("Site address"), max_length=300, required=False)
    hosts = forms.CharField(label=gettext_lazy("Allowed domains or IPs"), max_length=1000)
    # Not sent (a page or a script from before this field): config.toml keeps its value (views.py).
    proxies = forms.TypedChoiceField(label=gettext_lazy("Reverse proxies"), choices=PROXIES, coerce=int, required=False, empty_value=None, initial=0)
    admin_path = forms.CharField(label=gettext_lazy("Django admin address"), max_length=admin_path.MAX_LENGTH + 10, required=False)

    username = forms.CharField(label=gettext_lazy("Username"), max_length=USERNAME_MAX)
    password1 = forms.CharField(label=gettext_lazy("Password"), strip=False, max_length=PASSWORD_MAX, widget=forms.PasswordInput(render_value=True))
    password2 = forms.CharField(
        label=gettext_lazy("Password again"), strip=False, max_length=PASSWORD_MAX, widget=forms.PasswordInput(render_value=True)
    )

    ATTRS = {
        "token": {"class": "mono", "autocomplete": "off", "spellcheck": "false", "autocapitalize": "characters"},
        # required: marked with * on the page, which waits for them (the shown engine's only);
        # which ones the form needs depends on the engine (clean()).
        "db_file": {"class": "mono", "autocomplete": "off", "spellcheck": "false", "required": True},
        "db_host": {"autocomplete": "off", "spellcheck": "false", "required": True},
        "db_port": {"inputmode": "numeric", "required": True},
        "db_name": {"autocomplete": "off", "spellcheck": "false", "required": True},
        "db_user": {"autocomplete": "off", "spellcheck": "false", "required": True},
        "db_password": {"autocomplete": "off"},
        "site_name": {"autocomplete": "off"},
        "url": {"autocomplete": "off", "spellcheck": "false", "autocapitalize": "off", "inputmode": "url",
                "placeholder": "https://files.example.com"},
        "hosts": {"autocomplete": "off", "spellcheck": "false"},
        "admin_path": {"autocomplete": "off", "spellcheck": "false", "placeholder": gettext_lazy("Leave empty for a random one")},
        "username": {"autocomplete": "username", "spellcheck": "false", "autocapitalize": "off"},
        "password1": {"autocomplete": "new-password"},
        "password2": {"autocomplete": "new-password"},
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name, attrs in self.ATTRS.items():
            self.fields[name].widget.attrs.update(attrs)

    # add_error() takes a field out of cleaned_data: the cleaned values go in first.

    def _clean_sqlite(self, data):
        data["db_file"] = path = data.get("db_file", "").strip()
        if not path:
            self.add_error("db_file", _("Enter the path of the database file, like hfile.sqlite3."))
        elif any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in path):
            self.add_error("db_file", _("The path can't contain control characters such as line breaks."))
        elif path.endswith(("/", "\\")):
            self.add_error("db_file", _("Enter it down to the file name, like data/hfile.sqlite3."))
        else:
            try:
                file_path(path)  # what config.toml will accept, before anything is made
            except Problem as problem:
                self.add_error("db_file", str(problem))

    def _clean_mysql(self, data):
        port = data.get("db_port", "").strip()
        good_port = port.isascii() and port.isdigit() and 1 <= int(port) <= 65535
        data.update(
            db_host=data.get("db_host", "").strip(),
            db_port=int(port) if good_port else port,
            db_name=data.get("db_name", "").strip(),
            db_user=data.get("db_user", "").strip(),
        )
        if not data["db_host"] or any(ch.isspace() for ch in data["db_host"]):
            self.add_error("db_host", _("Enter MySQL's address, without spaces."))
        if not good_port:
            self.add_error("db_port", _("The port is a whole number from 1 to 65535."))
        if not _DB_NAME_RE.fullmatch(data["db_name"]):
            self.add_error("db_name", _("The database name may only use English letters, digits and underscores, 64 at most."))
        if not data["db_user"] or any(ch.isspace() for ch in data["db_user"]):
            # config.toml takes no spaces in it either (configfile._nonblank).
            self.add_error("db_user", _("Enter the MySQL username, without spaces."))
        if any(ch in data.get("db_password", "") for ch in "\r\n\0"):
            self.add_error("db_password", _("The password can't contain line breaks."))

    def clean_site_name(self):
        name = single_line(self.cleaned_data["site_name"])
        if not name:
            raise ValidationError(_("The site name can't be empty."))
        if len(name) > SITE_NAME_MAX:
            raise ValidationError(ngettext("The site name can have %(limit)s character at most.", "The site name can have %(limit)s characters at most.", SITE_NAME_MAX) % {"limit": SITE_NAME_MAX})
        reject_invisible(name, _("Site name"))
        return name

    def clean_url(self):
        try:
            return site_url(self.cleaned_data["url"])
        except Problem as problem:
            raise ValidationError(str(problem)) from None

    def clean_hosts(self):
        hosts, bad = [], []
        for item in re.split(r"[,，;；\s]+", self.cleaned_data["hosts"]):
            if not item:
                continue
            host = "*" if item == "*" else host_of(item)
            if not host:
                bad.append(item)
            elif host not in hosts:
                hosts.append(host)
        if bad:
            raise ValidationError(not_hosts(bad))
        for host in LOCAL_HOSTS:
            if host not in hosts:
                hosts.append(host)
        if len(hosts) > HOSTS_MAX:
            raise ValidationError(ngettext("%(limit)s at most.", "%(limit)s at most.", HOSTS_MAX) % {"limit": HOSTS_MAX})
        return hosts

    def clean_admin_path(self):
        path = admin_path.clean(self.cleaned_data["admin_path"])
        if path and (problem := admin_path.problem(path)):
            raise ValidationError(problem)
        if path and admin_path.guessable(path):
            raise ValidationError(_("/%(path)s/ is too easy to guess: scanners try it first. Leave "
                                    "it empty for a random one, or pick one nobody can guess.") % {"path": path})
        return path  # "" means random

    def clean_username(self):
        # As the site keeps usernames: NFKC first (full-width letters become ASCII ones), then the rules.
        username = get_user_model().normalize_username(self.cleaned_data["username"].strip())
        check_username(username)
        return username

    def clean(self):
        data = super().clean()
        if data.get("db_engine") == "sqlite":
            self._clean_sqlite(data)
        elif data.get("db_engine") == "mysql":
            self._clean_mysql(data)
        first, second = data.get("password1"), data.get("password2")
        if first and second and first != second:
            self.add_error("password2", _("The two passwords don't match."))
        elif first and "username" in data:
            candidate = get_user_model()(username=data["username"])
            try:
                password_validation.validate_password(first, candidate)
            except ValidationError as error:
                self.add_error("password1", error)
        return data

    def database(self):
        data = self.cleaned_data
        if data["db_engine"] == "sqlite":
            return Database(engine="sqlite", file=data["db_file"])
        return Database(
            engine="mysql",
            host=data["db_host"],
            port=data["db_port"],
            name=data["db_name"],
            user=data["db_user"],
            password=data["db_password"],
        )
