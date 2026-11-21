"""
HFile settings.

Per-machine values (listen address and port, database, secret key, debug flag, folders) come
from config.toml (config/configfile.py): the file given with --config, else $HFILE_CONFIG,
else config.toml next to the program, which `hfile` and manage.py create when it's missing
(config/cli.py). config.example.toml is what a new one looks like. Nothing is read from .env
files or other environment variables.

With an empty secret_key (a new install) the program starts in setup mode: the whole site is
the installer (apps/installer/), which writes the settings into config.toml and then has the program
start again.
"""

import secrets
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.utils.csp import CSP

from . import admin_path, codec_cache, configfile, database

# Python stops keeping the names of codecs it doesn't know, which clients choose (config/codec_cache.py).
# Here because whatever runs HFile (serve, runserver, manage.py, the tests, the single executable) loads
# the settings before it reads a request.
codec_cache.keep_found_only()

BASE_DIR = Path(__file__).resolve().parent.parent
# Built as a single executable, the code is unpacked into a new temporary folder on every start.
# What has to stay (config.toml, uploads, backups) is next to the executable, or next to the
# config.toml given with --config: folders in it are relative to its own folder.
FROZEN = configfile.FROZEN
CONFIG_FILE = configfile.locate()
try:
    CONFIG = configfile.load(CONFIG_FILE)
except configfile.ConfigError as error:
    raise ImproperlyConfigured(str(error)) from None
# [server]: where `hfile serve` listens (apps/installer/management/commands/serve.py).
SERVER = CONFIG["server"]
_site, _db = CONFIG["site"], CONFIG["database"]

SECRET_KEY = _site["secret_key"]
# Not installed yet: only the installer runs, under a key of this process alone. It writes
# the real one into config.toml.
SETUP_MODE = not SECRET_KEY
if SETUP_MODE:
    SECRET_KEY = secrets.token_urlsafe(50)

DEBUG = _site["debug"]
ALLOWED_HOSTS = configfile.allowed_hosts(CONFIG)
if SETUP_MODE:
    # The installer has to open at whatever address the server is reached by; it then writes
    # the site's own addresses into allowed_hosts.
    ALLOWED_HOSTS = ["*"]

# The Django admin is at /<ADMIN_PATH>/, never /admin/ (config/admin_path.py).
ADMIN_PATH = _site["admin_path"] or admin_path.path_from_key(SECRET_KEY)

# HFile's own apps, in apps/. Their labels are still the folder names (accounts, storages, …), which
# the tables, the migrations' records and AUTH_USER_MODEL go by. A new app is added here: its models,
# migrations, templates, static files and tests are then found, the single executable takes it in
# (scripts/build.sh reads this list), and the upgrade counts its tables as HFile's
# (apps/upgrades/service.py own_table).
HFILE_APPS = [
    "apps.accounts",
    "apps.activity",
    "apps.captchas",
    "apps.copytasks",
    "apps.files",
    "apps.ipgeo",
    "apps.links",
    "apps.office",
    "apps.preview",
    "apps.siteconf",
    "apps.sso",
    "apps.shares",
    "apps.storages",
    "apps.upgrades",
    "apps.uploads",
    "apps.web",
]

INSTALLED_APPS = [
    # Before staticfiles: its runserver command replaces theirs (it prints the admin's address,
    # and the installer's in setup mode).
    "apps.installer",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    *HFILE_APPS,
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.staticfiles.StaticFilesMiddleware",  # WhiteNoise: /static/, also with DEBUG off
    "apps.core.middleware.CurrentRequestMiddleware",  # code below a view reads the caller's IP through it
    # The page's language (LANGUAGES below): before every middleware that answers by itself (413, 403).
    "django.middleware.locale.LocaleMiddleware",
    "apps.core.middleware.RequestSizeLimitMiddleware",  # 413 with an explanation over MAX_REQUEST_BYTES
    "apps.siteconf.middleware.AccessControlMiddleware",  # 403 for the IP and UA blocklists (访问控制)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.middleware.PrivateAnswersMiddleware",  # no-store for logged-in users (the back/forward cache)
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    # Inside Django's: the origins of http(s) single sign-on icons added to the page's policy. Outside the
    # settings' one below, which leaves a response alone once it has a policy: that one sets it, this one
    # adds to it.
    "apps.sso.middleware.SsoIconMiddleware",
    # Inside Django's: what the settings add to a page's policy (linked logo, custom JS on public pages).
    "apps.siteconf.middleware.SiteCSPMiddleware",
]

ROOT_URLCONF = "apps.installer.setup_urls" if SETUP_MODE else "config.urls"
# A form sent without its CSRF token (the Django admin's): the site's 403 page, in Chinese.
CSRF_FAILURE_VIEW = "apps.web.errors.csrf_failure"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "django.template.context_processors.i18n",
                "apps.siteconf.context_processors.site_settings",
                "apps.web.context_processors.shell",
            ],
            # {% translate %} and {% blocktranslate %} in every template, without {% load i18n %}.
            "builtins": ["django.templatetags.i18n"],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite (a file) or MySQL 8.4+, as [database] engine says (config/database.py). The MySQL for
# development and the tests runs in Docker, see scripts/dev-db.sh; SQLite needs nothing.
DATABASES = {"default": database.django_settings(_db, CONFIG_FILE)}
if SETUP_MODE:
    # Not chosen yet: the installer asks. Django's checks would ask the database for its version, so
    # until the installer switches it over (apps/installer/service.py use_database), it's the backend that
    # never connects. Sessions are signed cookies meanwhile: a cookie left from an earlier site at
    # this address would otherwise make Django ask the database for it.
    DATABASES = {"default": {"ENGINE": "django.db.backends.dummy"}}
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

AUTH_USER_MODEL = "accounts.User"
# ModelBackend that finds the username whatever its case (apps/accounts/backends.py).
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.HFileBackend"]
# There is no login page: this opens the login dialog on the home page, then goes to ?next=.
LOGIN_URL = "/?auth=login"
# Cookies of their own names: other Django sites on the same host (another port, the same
# domain) would otherwise log each other out, browsers don't keep cookies apart by port.
SESSION_COOKIE_NAME = "hfile_session"
CSRF_COOKIE_NAME = "hfile_csrftoken"
# A login lasts 30 days (Django's default is 14, ACCT-06); disabling the account or changing the password
# ends it sooner (apps/accounts/models.py session_epoch).
SESSION_COOKIE_AGE = 30 * 24 * 3600

AUTH_PASSWORD_VALIDATORS = [
    # The nickname is shown to everybody, like the username.
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
        "OPTIONS": {"user_attributes": ("username", "nickname", "email")},
    },
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# The languages of the pages. The texts in the code are the Chinese ones; config/locale/ has their
# translations (docs/development.md, 翻译). A visitor gets what the language menu chose (a cookie),
# else what the browser asks for, else LANGUAGE_CODE. The names are each language's own.
LANGUAGE_CODE = "zh-hans"
LANGUAGES = [("zh-hans", "简体中文"), ("en", "English")]
LOCALE_PATHS = [BASE_DIR / "config" / "locale"]
LANGUAGE_COOKIE_NAME = "hfile_language"
LANGUAGE_COOKIE_AGE = 365 * 24 * 3600
LANGUAGE_COOKIE_SAMESITE = "Lax"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise (apps/core/staticfiles.py) serves the apps' own files: the single executable has them
# unpacked, and nothing runs collectstatic.
WHITENOISE_USE_FINDERS = True
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Static URLs carry a fingerprint of the file, so browsers fetch changed files at once.
    "staticfiles": {"BACKEND": "apps.core.staticfiles.VersionedStaticFilesStorage"},
}
MEDIA_URL = "/media/"
# What the site saves of what is uploaded to it ([paths] uploads): the site's logo and icon, say.
# Only names the site gave them are served (apps/core/uploads.py).
MEDIA_ROOT = configfile.folder(CONFIG_FILE, CONFIG["paths"]["uploads"])
# Where the chunks of an upload that goes through the site wait until the last one has come
# (apps/uploads/spool.py): next to config.toml, never inside MEDIA_ROOT, which is served. Made with mode 700.
UPLOAD_SPOOL_DIR = configfile.folder(CONFIG_FILE, "upload-spool")

# Largest request body the site accepts (apps.core.middleware.RequestSizeLimitMiddleware answers 413 with
# an explanation above it); waitress takes twice as much before it cuts a request off itself
# (apps/installer/management/commands/serve.py). Files are uploaded in chunks below it. A reverse proxy in
# front should allow the same: nginx client_max_body_size 64m.
MAX_REQUEST_BYTES = 64 * 1024 * 1024

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# The site's address ([site] url), for full links (direct links, shares), e.g.
# https://files.example.com. Without it they use the address each request came to, which behind a
# reverse proxy needs the proxy to pass Host on (and X-Forwarded-Proto for https, see below).
SITE_URL = _site["url"]
# A form sent from the site's own address is never a CSRF failure, also when a reverse proxy
# changes the Host it passes on.
CSRF_TRUSTED_ORIGINS = [SITE_URL] if SITE_URL else []

# The cache holds the rate limits' counters (DRF throttles, apps/core/throttles.py), in this process's
# memory. Django's default keeps 300 entries and then drops a third of them: a few hundred
# addresses (the /64s of one IPv6 network, say) would give everybody a fresh budget. An entry is
# a short list of times, so 100 000 of them take a few tens of MB at most.
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "OPTIONS": {"MAX_ENTRIES": 100_000},
    }
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.core.auth.SessionAuthentication"],
    # Closed unless a view says otherwise: a view that forgets to is refused, not open.
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "apps.core.pagination.StandardPagination",
    "DEFAULT_RENDERER_CLASSES": ["apps.core.renderers.JSONRenderer"]
    + (["rest_framework.renderers.BrowsableAPIRenderer"] if DEBUG else []),
    # JSON and multipart (files) only: the site's pages send nothing else.
    "DEFAULT_PARSER_CLASSES": [
        "apps.core.parsers.JSONParser",  # DRF's, with bad JSON it lets through answered 400 as well
        "rest_framework.parsers.MultiPartParser",
    ],
    "EXCEPTION_HANDLER": "apps.core.exceptions.api_exception_handler",
    # No answers to OPTIONS (405): DRF's would give anybody each view's docstring, notes for the developers
    # with module paths and how the checks work (SEC-04). The site's pages never ask.
    "DEFAULT_METADATA_CLASS": None,
    "DEFAULT_THROTTLE_RATES": {
        "login": "10/minute",  # POST /api/v1/session/, per address (apps.core.throttles.LoginThrottle)
        "password": "10/hour",  # checks of one's password: PUT /api/v1/users/me/password/, POST /api/v1/sso/links/…/
        "captcha": "30/minute",  # POST /api/v1/captcha/, a new picture
        "totp": "20/hour",  # POST /api/v1/users/me/totp/…: setting up, confirming, turning off
        # Single sign-on, per address (per user when logged in): /sso/<code>/login and its callback, which
        # makes requests to the provider; POST /api/v1/manage/sso/discover/.
        "sso": "30/minute",
        "upload": "1200/minute",  # POST /api/v1/uploads/ and uploads/folders/, per address (a folder of small files)
        "text_save": "60/minute",  # PUT /api/v1/uploads/text/, the text editor's save
        "links": "60/minute",  # POST /api/v1/links/<key>/path/ and short/: making path and short links
        "preview": "60/minute",  # POST /api/v1/preview/…: Markdown rendered on the server, kkFileView addresses
        "office": "60/minute",  # POST /api/v1/office/config/, opening a file in OnlyOffice
    },
    # Reverse proxies in front of Django ([server] proxies; nginx alone: 1). Rate limits, the access
    # log and the login IP (apps/core/net.py) then read the client address from X-Forwarded-For; with 0
    # the header is ignored, because without a proxy anybody can send it.
    "NUM_PROXIES": SERVER["proxies"],
    "COERCE_DECIMAL_TO_STRING": False,
    # Times in the site's zone to the second, as apps.core.times.iso() writes those of answers made by hand
    # (TEXT-01); DRF's default writes the microseconds.
    "DATETIME_FORMAT": "%Y-%m-%dT%H:%M:%S%:z",
    "TEST_REQUEST_DEFAULT_FORMAT": "json",
}

# Upgrading (upgrades app). A new version upgrades the database when it starts, before it
# serves anything (config/wsgi.py): it backs the whole database up into BACKUP_DIR, applies
# the migrations, and puts the backup back if one fails. On by default only in the single
# executable; from the source code run `manage.py upgrade` instead, or set [upgrade] auto = true.
_auto_upgrade = CONFIG["upgrade"]["auto"]
AUTO_UPGRADE = (FROZEN if _auto_upgrade is None else _auto_upgrade) and not SETUP_MODE
BACKUP_DIR = configfile.folder(CONFIG_FILE, CONFIG["paths"]["backups"])

# IP 归属地 (ipgeo app): the database downloaded on /manage/#ip-geo is kept here ([paths] geoip).
GEOIP_DIR = configfile.folder(CONFIG_FILE, CONFIG["paths"]["geoip"])

# `manage.py test` (apps/core/testing.py): every test starts with an empty cache (the rate limits).
TEST_RUNNER = "apps.core.testing.TestRunner"

# Every script and stylesheet is served from this site: no CDNs, and nothing inline, so a strict
# policy works. Previews of files a storage serves from elsewhere (a presigned S3 URL) and an
# OnlyOffice server will need their origins added to img-src, media-src or frame-src.
# The viewers (preview app): workers only from this site (pdf.js and hls.js run theirs from
# /static/), also where custom JS widens script-src. blob: media (hls.js's MediaSource, subtitles made
# in the page) and kkFileView's frame are added on the pages that show the viewers (apps/preview/csp.py).
SECURE_CSP = {
    "default-src": [CSP.SELF],
    "script-src": [CSP.SELF],
    "style-src": [CSP.SELF],
    "img-src": [CSP.SELF, "data:", "blob:"],
    "worker-src": [CSP.SELF],
    "object-src": [CSP.NONE],
    "base-uri": [CSP.NONE],
    "frame-ancestors": [CSP.NONE],
    "form-action": [CSP.SELF],
}

# Cookies only over https when the site is reached over https: its url starts with https://.
# Otherwise (an address without a certificate yet, a test on the local network) logging in has to
# work over http. The installer is usually opened over plain http, before there is a certificate.
if SITE_URL.startswith("https://") and not SETUP_MODE:
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
# Behind a reverse proxy that ends https, Django sees http: the proxy says it was https in
# X-Forwarded-Proto (Caddy does by default; nginx: proxy_set_header X-Forwarded-Proto $scheme).
# Links Django makes (the API's next page) are then https too. Without a proxy nobody's
# X-Forwarded-Proto is believed.
if SERVER["proxies"]:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Warnings and errors (with the traceback of a 500) are written to the console, also with debug
# off, when Django would only email them to ADMINS (there are none). Answers in the 4xx aren't:
# bots trying addresses would fill the console.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "plain": {"format": "[{asctime}] {levelname} {name}: {message}", "style": "{", "datefmt": "%Y-%m-%d %H:%M:%S"},
        "django.server": {"()": "django.utils.log.ServerFormatter", "format": "[{server_time}] {message}", "style": "{"},
    },
    "filters": {"one_line": {"()": "apps.core.logs.OneLine"}, "without_secrets": {"()": "apps.core.logs.WithoutSecrets"}},
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "plain", "filters": ["without_secrets"]},
        "console_one_line": {
            "class": "logging.StreamHandler", "formatter": "plain", "filters": ["without_secrets", "one_line"],
        },
        "django.server": {"class": "logging.StreamHandler", "formatter": "django.server", "filters": ["without_secrets"]},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO" if DEBUG else "WARNING", "propagate": False},
        "django.request": {"level": "ERROR"},
        # Requests made up to probe the site (another site's Host, /media/../): a line is enough.
        "django.security": {"handlers": ["console_one_line"], "propagate": False},
        # runserver's line per request, as Django has it.
        "django.server": {"handlers": ["django.server"], "level": "INFO", "propagate": False},
        # serve's line per request ([server] log_requests, apps/core/access_log.py).
        "access": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # HFile's own loggers (hfile.storage.<type> for the drivers, ...).
        "hfile": {"handlers": ["console"], "level": "INFO", "propagate": False},
        # httpx and httpcore log the full URL of every request at INFO and DEBUG, and the URLs of
        # some storages are credentials (OneDrive upload sessions, 115 and CDN links).
        "httpx": {"level": "WARNING"},
        "httpcore": {"level": "WARNING"},
    },
}
