"""The site's settings that admins change on the site (not in config.toml): one row, SiteSettings.

Every setting has a typed field here, grouped as the admin pages show them: 站点设置, 显示设置,
直链和短链, 安全设置, 访问控制 (inventory/features.md section 2). The defaults are
the values of a new site. Validation, the settings API and the admin pages are the siteconf app's
(serializers.py, views.py); other apps only read the row, through SiteSettings.load(), which is
cached (see load()).

Secrets (onlyoffice_secret) are stored encrypted (apps/core/secrets.py) and never sent back.

The suffix lists of the previews are "" for the built-in default (DEFAULT_SUFFIXES), so a site keeps
getting a newer version's defaults until an admin writes a list of his own; a switch per kind
(<kind>_preview) turns the kind off. preview_suffixes() gives the lists in effect.
"""

import functools
import hashlib
import random
import re
import uuid
from urllib.parse import urlsplit

from django.conf import settings
from django.core.cache import cache
from django.db import connection, models, transaction
from django.templatetags.static import static
from django.utils.translation import gettext_lazy, pgettext_lazy

SITE_NAME_MAX = 30
DEFAULT_SITE_NAME = "HFile"
HOME_NAME_MAX = 30
# Stored as it is, as every site has stored it: a value, not a text; the pages show it in their language
# ("Home", PublicSiteSerializer), any other name as it is.
HOME_NAME_DEFAULT = "首页"  # i18n: data
ICP_MAX = 100
URL_MAX = 500
ANNOUNCEMENT_MAX = 20_000
CUSTOM_CODE_MAX = 100_000  # custom_css, custom_js
GUEST_HTML_MAX = 100_000
SECRET_MAX = 1_000
DEFAULT_DIRECT_LINK_PREFIX = "directlink"

# The suffixes each kind of preview opens, a new site's defaults.
# Three-D models have no viewer in HFile: their suffixes go to kkFileView.
DEFAULT_IMAGE_SUFFIXES = "gif,jpg,jpeg,png,bmp,webp,ico"
DEFAULT_VIDEO_SUFFIXES = "mp4,webm,m3u8,rmvb,avi,swf,3gp,mkv,flv"
DEFAULT_AUDIO_SUFFIXES = "mp3,wav,wma,ogg,aac,flac,m4a"
DEFAULT_TEXT_SUFFIXES = (
    "scss,sass,kt,gitignore,bat,properties,yml,css,js,md,xml,txt,py,go,html,less,php,rb,rust,script,java,sh,sql"
)
DEFAULT_OFFICE_SUFFIXES = "doc,docx,csv,xls,xlsx,ppt,pptx,xlsm"
DEFAULT_KKFILEVIEW_SUFFIXES = (
    "tsv,dotm,xlt,xltm,dot,dotx,xlam,xla,wps,dps,et,ett,wpt,odt,ods,ots,odp,otp,six,ott,fodt,fods,vsd,vsdx,"
    "wmf,emf,psd,eps,ofd,rtf,xmind,bpmn,eml,3ds,off,3dm,wrl,3mf,ifc,brep,step,iges,fcstd,bim,dwg,dxf,dwf,igs,"
    "dwt,dng,dwfx,cf2,plt,jar,gzip,jfif,tif,tiff,tga,svg,mov,rm,ts,mpeg,mpg,wmv,dcm,drawio,"
    "dae,fbx,gltf,glb,obj,ply,stl"
)
# The kinds of preview whose suffixes admins set, in the order a file's suffix is looked up.
PREVIEW_KINDS = ("image", "video", "audio", "text", "office", "kkfileview")
DEFAULT_SUFFIXES = {
    "image": DEFAULT_IMAGE_SUFFIXES,
    "video": DEFAULT_VIDEO_SUFFIXES,
    "audio": DEFAULT_AUDIO_SUFFIXES,
    "text": DEFAULT_TEXT_SUFFIXES,
    "office": DEFAULT_OFFICE_SUFFIXES,
    "kkfileview": DEFAULT_KKFILEVIEW_SUFFIXES,
}

# What uploads of the logo and the icon are named (apps.core.uploads serves only these, apps.py).
UPLOAD_PATTERN = r"site/[0-9a-f]{32}\.(?:png|ico|webp|jpe?g|gif)"


def default_short_link_expiries():
    """The lifetimes offered when making a short link on a new site: one day."""
    return [{"value": 1, "unit": "d", "seconds": 86400}]


def site_upload_to(instance, filename):
    """site/<random>.<ext>, the names apps.core.uploads serves for this app (apps.py): the uploader's own
    file name is never used."""
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else "png"
    return f"site/{uuid.uuid4().hex}.{extension}"


def is_upload(value):
    """Whether a favicon or home_logo value names an uploaded file (rather than an http(s) URL)."""
    return bool(value) and value.startswith("site/")


def image_url(value):
    """The address of a favicon or home_logo value: an upload under MEDIA_URL, or the URL itself."""
    if not value:
        return ""
    if is_upload(value):
        return settings.MEDIA_URL + value
    return value


class Layout(models.TextChoices):
    FULL = "full", gettext_lazy("Full width")
    CENTER = "center", gettext_lazy("Centered")
    CARD = "card", gettext_lazy("Card")


class ClickMode(models.TextChoices):
    CLICK = "click", gettext_lazy("Single click")
    DOUBLE = "dbclick", gettext_lazy("Double click")


class SortField(models.TextChoices):
    NAME = "name", gettext_lazy("File name")
    SIZE = "size", gettext_lazy("File size")
    TIME = "time", gettext_lazy("Modified")


class SortOrder(models.TextChoices):
    ASC = "asc", gettext_lazy("Ascending")
    DESC = "desc", gettext_lazy("Descending")


class LinkTarget(models.TextChoices):
    BLANK = "_blank", pgettext_lazy("link target", "New tab")
    SELF = "_self", gettext_lazy("Same tab")


class KkFileViewMode(models.TextChoices):
    IFRAME = "iframe", gettext_lazy("Embedded")
    NEW_TAB = "new_tab", gettext_lazy("New tab")


class RefererType(models.TextChoices):
    OFF = "off", gettext_lazy("No Referer hotlink protection")
    ALLOWLIST = "allowlist", gettext_lazy("Allowlist")
    BLOCKLIST = "blocklist", gettext_lazy("Blocklist")


class GuestHome(models.TextChoices):
    NORMAL = "normal", gettext_lazy("Show it as usual")
    LOGIN = "login", gettext_lazy("Go to the login page")
    CUSTOM = "custom", gettext_lazy("Custom page")


# SiteSettings.load()'s cache (Django's cache: this process's memory). A copy of the row is kept with
# the generation it was read under; saving bumps the generation, so a copy read before the save, or
# while it was being saved, is never used again. Other processes (several workers, a restore run
# from the command line) don't see this process's saves: their copies go after CACHE_SECONDS.
_ROW_KEY = "siteconf:settings:row"
_GENERATION_KEY = "siteconf:settings:generation"
CACHE_SECONDS = 30


def _generation():
    generation = cache.get(_GENERATION_KEY)
    if generation is None:
        cache.add(_GENERATION_KEY, random.getrandbits(62), None)
        generation = cache.get(_GENERATION_KEY)
    return generation


def forget_cached():
    """Drops what load() keeps: after a change it didn't see (raw SQL, a restore, a migration)."""
    try:
        cache.incr(_GENERATION_KEY)
    except ValueError:  # not there (yet, or any more): a new one, which no copy has
        cache.set(_GENERATION_KEY, random.getrandbits(62), None)


# What a CSP host-source with a scheme can be (and nothing that would end the header or a directive).
_CSP_ORIGIN = re.compile(r"https?://[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?(?::[0-9]{1,5})?")


@functools.lru_cache(maxsize=4)
def _fingerprint(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


class SiteSettings(models.Model):
    """A single row (pk=1). Read it with SiteSettings.load()."""

    # --- 站点设置
    site_name = models.CharField(gettext_lazy("Site name"), max_length=SITE_NAME_MAX, default=DEFAULT_SITE_NAME)
    favicon = models.CharField(
        gettext_lazy("site icon"), max_length=URL_MAX, blank=True,
        help_text=gettext_lazy("An uploaded picture (a file name under site/) or an http(s) address; when empty, HFile's own icon."),
    )
    home_name = models.CharField(
        gettext_lazy("Root name"), max_length=HOME_NAME_MAX, default=HOME_NAME_DEFAULT, help_text=gettext_lazy("The first part of the path above the file list."),
    )
    home_logo = models.CharField(
        gettext_lazy("Home logo"), max_length=URL_MAX, blank=True, help_text=gettext_lazy("An uploaded picture (a file name under site/) or an http(s) address."),
    )
    home_logo_link = models.CharField(
        gettext_lazy("Home logo link"), max_length=URL_MAX, blank=True, default="/", help_text=gettext_lazy("The address the logo opens; when empty, it opens nothing."),
    )
    home_logo_target = models.CharField(
        gettext_lazy("Home logo link opens in"), max_length=10, choices=LinkTarget.choices, default=LinkTarget.SELF,
    )
    icp = models.CharField(gettext_lazy("ICP number"), max_length=ICP_MAX, blank=True)
    max_file_uploads = models.PositiveSmallIntegerField(gettext_lazy("Uploads at once"), default=5)

    # --- 显示设置
    layout = models.CharField(gettext_lazy("Page layout"), max_length=10, choices=Layout.choices, default=Layout.FULL)
    mobile_layout = models.CharField(gettext_lazy("Phone page layout"), max_length=10, choices=Layout.choices, default=Layout.FULL)
    mobile_show_size = models.BooleanField(gettext_lazy("Show file sizes on phones"), default=True)
    root_show_storage = models.BooleanField(
        gettext_lazy("List all storage sources at the root"), default=True, help_text=gettext_lazy("When off, the home page shows the first storage source's content right away."),
    )
    default_save_password = models.BooleanField(gettext_lazy('Check "Remember password" by default for folder passwords'), default=False)
    download_confirm = models.BooleanField(gettext_lazy("Confirm before downloading"), default=True, help_text=gettext_lazy("When off, downloads start right away."))
    file_click_mode = models.CharField(gettext_lazy("Opening files"), max_length=10, choices=ClickMode.choices, default=ClickMode.DOUBLE)
    mobile_file_click_mode = models.CharField(
        gettext_lazy("Opening files on phones"), max_length=10, choices=ClickMode.choices, default=ClickMode.CLICK,
    )
    max_show_size = models.PositiveIntegerField(
        gettext_lazy("Files shown at first"), default=1000, help_text=gettext_lazy("With many files in a folder, this many are shown first so the page doesn't stall."),
    )
    load_more_size = models.PositiveIntegerField(gettext_lazy('Files per "Load more"'), default=50)
    default_sort_field = models.CharField(gettext_lazy("Default sort field"), max_length=10, choices=SortField.choices, default=SortField.NAME)
    default_sort_order = models.CharField(gettext_lazy("Default sort order"), max_length=10, choices=SortOrder.choices, default=SortOrder.ASC)
    show_document = models.BooleanField(gettext_lazy("Show folder readmes"), default=True, help_text=gettext_lazy("Whether the folder readmes (README) set on storage sources are shown, for the whole site."))
    image_preview = models.BooleanField(gettext_lazy("Preview images"), default=True)
    image_suffixes = models.TextField(gettext_lazy("Image suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    video_preview = models.BooleanField(gettext_lazy("Preview video"), default=True)
    video_suffixes = models.TextField(gettext_lazy("Video suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    audio_preview = models.BooleanField(gettext_lazy("Preview audio"), default=True)
    audio_suffixes = models.TextField(gettext_lazy("Audio suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    text_preview = models.BooleanField(gettext_lazy("Preview text"), default=True)
    text_suffixes = models.TextField(gettext_lazy("Text suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    office_preview = models.BooleanField(gettext_lazy("Preview Office files"), default=True)
    office_suffixes = models.TextField(gettext_lazy("Office suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    kkfileview_preview = models.BooleanField(gettext_lazy("Preview with kkFileView"), default=True)
    kkfileview_suffixes = models.TextField(gettext_lazy("kkFileView suffixes"), blank=True, help_text=gettext_lazy("When empty, the default."))
    text_preview_max_kb = models.PositiveIntegerField(gettext_lazy("Largest text file to preview (KB)"), default=512)
    onlyoffice_url = models.CharField(
        gettext_lazy("OnlyOffice server address"), max_length=URL_MAX, blank=True, help_text=gettext_lazy("When empty, OnlyOffice isn't used to preview and edit Office files."),
    )
    onlyoffice_secret = models.TextField(
        "OnlyOffice Secret", blank=True, help_text=gettext_lazy("OnlyOffice's secret, when it has JWT authentication on. Saved encrypted and not shown again."),
    )
    kkfileview_url = models.CharField(gettext_lazy("kkFileView server address"), max_length=URL_MAX, blank=True)
    kkfileview_open_mode = models.CharField(
        gettext_lazy("kkFileView opens"), max_length=10, choices=KkFileViewMode.choices, default=KkFileViewMode.IFRAME,
    )
    show_announcement = models.BooleanField(gettext_lazy("Show the announcement"), default=True)
    announcement = models.TextField(pgettext_lazy("its text", "Announcement"), blank=True)
    custom_css = models.TextField(gettext_lazy("Custom CSS"), blank=True)
    custom_js = models.TextField(gettext_lazy("Custom JS"), blank=True)

    # --- 直链和短链
    record_download_log = models.BooleanField(gettext_lazy("Log downloads"), default=True, help_text=gettext_lazy("Logs the downloads of direct and short links."))
    referer_type = models.CharField(gettext_lazy("Referer hotlink protection"), max_length=10, choices=RefererType.choices, default=RefererType.OFF)
    referer_allow_empty = models.BooleanField(gettext_lazy("Allow an empty Referer"), default=True)
    referer_rules = models.TextField(gettext_lazy("Allowlist or blocklist"), blank=True, help_text=gettext_lazy("One per line, with the scheme, * as a wildcard, like https://*.example.com"))
    link_limit_seconds = models.PositiveIntegerField(gettext_lazy("Download rate limit: seconds"), default=3600)
    link_limit_downloads = models.PositiveIntegerField(
        gettext_lazy("Download rate limit: downloads"), default=10000, help_text=gettext_lazy("Each IP can download direct links at most this many times in this many seconds; either one 0 means no limit."),
    )
    show_path_link = models.BooleanField(gettext_lazy("Allow direct links (paths)"), default=True)
    direct_link_prefix = models.CharField(gettext_lazy("Direct link prefix"), max_length=32, default=DEFAULT_DIRECT_LINK_PREFIX)
    path_link_anonymous = models.BooleanField(
        gettext_lazy("People who aren't logged in can download with direct links too"), default=False,
        help_text=gettext_lazy("When off, opening a direct link needs the download permission for the file (the "
                               "anonymous user's when not logged in); when on, anyone who knows the address can "
                               "download files from any enabled storage source, and only filter rules still apply."),
    )
    show_short_link = models.BooleanField(gettext_lazy("Allow short links"), default=True)
    short_link_expiries = models.JSONField(gettext_lazy("Short link lifetimes"), default=default_short_link_expiries)

    # --- 代理下载: downloads through the site, per client IP (apps/files/limits.py); 0 is no limit
    proxy_ip_connections = models.PositiveIntegerField(gettext_lazy("Connections per IP"), default=0)
    proxy_ip_speed = models.PositiveIntegerField(gettext_lazy("Total speed per IP (KB/s)"), default=0)

    # --- 安全设置
    secure_login_entry = models.CharField(
        gettext_lazy("Secure login entrance"), max_length=32, blank=True, help_text=gettext_lazy("When set, logging in only works at /login/<entrance>/, so the login can't be found by scanning."),
    )
    show_login = models.BooleanField(gettext_lazy("Show the login button"), default=True)
    guest_home = models.CharField(gettext_lazy("Home page for visitors"), max_length=10, choices=GuestHome.choices, default=GuestHome.NORMAL)
    guest_home_html = models.TextField(gettext_lazy("Custom page"), blank=True)
    login_captcha = models.BooleanField(gettext_lazy("Captcha at login"), default=False)
    sso_auto_register = models.BooleanField(
        gettext_lazy("Let single sign-on register new users"), default=False,
        help_text=gettext_lazy("When on, someone using single sign-on for the first time gets an "
                               'account made for them, with the permissions of the "New user template".'),
    )

    # --- 访问控制
    ip_blocklist = models.TextField(gettext_lazy("IP blocklist"), blank=True, help_text=gettext_lazy("One IP or range per line, like 192.168.0.0/24; IPv6 works too."))
    ua_blocklist = models.TextField(gettext_lazy("UA blocklist"), blank=True, help_text=gettext_lazy("One User-Agent per line, with * as a wildcard."))

    updated_at = models.DateTimeField(gettext_lazy("Modified"), auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = gettext_lazy("Site settings")

    def __str__(self):
        return self.site_name

    @classmethod
    def load(cls):
        """The row, made when it is missing. Outside transactions it comes from the cache: every
        request reads it (the access control middleware, the pages), and saving it (save(), the
        admin's pages, the Django admin) makes the next load() read it again. Each call returns a
        copy of its own, so changing it changes nobody else's.

        Inside a transaction it is read from the database: a row changed by a transaction that is
        then rolled back must not be kept. QuerySet.update() and raw SQL bypass save(): call
        forget_cached() after them.
        """
        if connection.in_atomic_block:
            return cls.objects.get_or_create(pk=1)[0]
        generation = _generation()
        kept = cache.get(_ROW_KEY)
        if kept is not None and kept[0] == generation:
            return kept[1]
        site = cls.objects.get_or_create(pk=1)[0]
        cache.set(_ROW_KEY, (generation, site), CACHE_SECONDS)
        return site

    # ------------------------------------------------------------------ what the rest of the site reads

    def preview_suffixes(self):
        """{kind: [suffix, ...]} in effect for each of PREVIEW_KINDS: the admin's list, the default
        one when it is empty, nothing when the kind's switch is off. Lower case, no dots."""
        lists = {}
        for kind in PREVIEW_KINDS:
            if not getattr(self, f"{kind}_preview"):
                lists[kind] = []
                continue
            text = getattr(self, f"{kind}_suffixes") or DEFAULT_SUFFIXES[kind]
            lists[kind] = [suffix for suffix in text.split(",") if suffix]
        return lists

    def suffix_text(self, kind):
        """The list a kind's field shows: the admin's own, or the default one ("gif, jpg, ...")."""
        return (getattr(self, f"{kind}_suffixes") or DEFAULT_SUFFIXES[kind]).replace(",", ", ")

    @property
    def favicon_url(self):
        """The tab icon: the uploaded or linked one, else HFile's own."""
        return image_url(self.favicon) or static("web/favicon.svg")

    @property
    def logo_url(self):
        """The top bar's logo: the uploaded or linked one, else HFile's own icon."""
        return image_url(self.home_logo) or static("web/favicon.svg")

    @property
    def image_origins(self):
        """The origins of the logo and the icon when they are links: the pages' CSP lets them in. Only
        what a CSP source can say (scheme, an ASCII host name, a port): the settings save hosts in
        another script as punycode, and an IPv6 literal can't be named there (no logo from it then)."""
        origins = []
        for value in (self.favicon, self.home_logo):
            if value and not is_upload(value):
                parts = urlsplit(value)
                origin = f"{parts.scheme}://{parts.netloc}".lower()
                if _CSP_ORIGIN.fullmatch(origin) and origin not in origins:
                    origins.append(origin)
        return origins

    @property
    def login_visible(self):
        """Whether pages show a way to log in: not with a secure login entry, whose address only the
        people who know it may find (accounts), nor when an admin turned the button off."""
        return self.show_login and not self.secure_login_entry

    @property
    def announcement_html(self):
        """The announcement as HTML (Markdown with the admin's HTML in it, apps/core/markdown.py); "" when
        it isn't shown."""
        if not self.show_announcement or not self.announcement.strip():
            return ""
        from apps.core import markdown

        return markdown.render_trusted(self.announcement)

    @property
    def announcement_version(self):
        """Changes with the text: a visitor who closed the announcement sees the next one."""
        return _fingerprint(self.announcement)

    @property
    def custom_css_version(self):
        return _fingerprint(self.custom_css) if self.custom_css.strip() else ""

    @property
    def custom_js_version(self):
        return _fingerprint(self.custom_js) if self.custom_js.strip() else ""


def _forget_after_change(**kwargs):
    # At once (this transaction's later reads), and after the commit: a copy another thread read in
    # between, of the row as it was before, has an older generation then.
    forget_cached()
    transaction.on_commit(forget_cached, robust=True)
