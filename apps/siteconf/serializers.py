"""The checks of every setting, shared by the settings API (views.py) and the /manage/ pages, whose
forms are drawn from these fields (labels, help, choices and limits: form_fields()).

One serializer per page (GROUPS): 站点设置 site, 显示设置 view, 直链和短链 link, 安全设置 security,
访问控制 access. A PATCH changes only the fields it sends; every problem is answered at once, by
field, in Chinese. What each check does:

- choices and ranges as the fields say;
- texts: trimmed, one line where they are one line, no control or invisible characters, limits;
- addresses: http(s) only; the logo's link may also be a path of the site;
- logo and icon: an uploaded file (site/<random>.png, uploaded by views.py; only the current one can
  be kept) or an http(s) URL;
- suffix lists: trimmed, lower case, without dots and repeats; empty, or the default list, is saved
  as "" and means the default; each kind has its own switch;
- the path-link prefix and the secure login entry: their characters, and the prefix against the
  site's other first path segments (apps.core.reserved);
- short-link lifetimes: positive, at most one "forever", seconds as value × unit;
- referer rules, IP and UA blocklists (apps/siteconf/blocklist.py), one per line, every bad line named;
  a blocklist that would block the admin saving it is refused;
- only superusers change the fields whose holder could act through other people's pages or get the
  site's files (superuser_fields): the custom CSS and JS, the announcement and the custom guest page
  (raw HTML and JS on the public pages, D33), the OnlyOffice address (its api.js runs on the editor
  page with the visitor's session) and secret (it signs the callbacks that save files), and the
  kkFileView address (it gets the signed URLs of the files it previews) (D49).

Secrets are write-only: onlyoffice_secret is sent to change it ("" removes it) and never sent back;
onlyoffice_secret_set says whether there is one.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import idna
from django.core.files.storage import default_storage
from django.core.validators import URLValidator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.translation import gettext as _, gettext_lazy, ngettext, pgettext_lazy
from rest_framework import serializers
from rest_framework.exceptions import PermissionDenied

from apps.core import net, reserved, secrets
from apps.core.fields import STRING_FIELD_MAPPING, BoundedListField, StringField
from apps.core.text import reject_invisible, single_line

from . import blocklist
from .models import (
    ANNOUNCEMENT_MAX,
    CUSTOM_CODE_MAX,
    DEFAULT_DIRECT_LINK_PREFIX,
    DEFAULT_SUFFIXES,
    GUEST_HTML_MAX,
    HOME_NAME_DEFAULT,
    HOME_NAME_MAX,
    ICP_MAX,
    PREVIEW_KINDS,
    SECRET_MAX,
    SITE_NAME_MAX,
    UPLOAD_PATTERN,
    URL_MAX,
    GuestHome,
    SiteSettings,
    image_url,
    is_upload,
)

_RAW_MAX = 1_000  # characters of a one-line text as sent, before it is cleaned and its limit checked

# ------------------------------------------------------------------ helpers


def _text(value, what, limit, *, required=False):
    """One line of text: NFC, runs of whitespace as one space, trimmed; no invisible characters."""
    text = single_line(value)
    if required and not text:
        raise serializers.ValidationError(_("%(what)s can't be empty.") % {"what": what})
    if len(text) > limit:
        raise serializers.ValidationError(ngettext("%(what)s is at most %(limit)s character; it has %(count)s now.", "%(what)s is at most %(limit)s characters; it has %(count)s now.", limit) % {"what": what, "limit": limit, "count": len(text)})
    try:
        reject_invisible(text, what)
    except DjangoValidationError as error:
        raise serializers.ValidationError(error.messages) from None
    return text


def _block(value, what, limit):
    """Text of several lines (HTML, CSS, JS, Markdown): LF line ends; no control characters other
    than tab and line feed. Spaces at line ends stay (two of them break a line in Markdown)."""
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if len(text) > limit:
        raise serializers.ValidationError(ngettext("%(what)s is at most %(limit)s character; it has %(count)s now.", "%(what)s is at most %(limit)s characters; it has %(count)s now.", limit) % {"what": what, "limit": limit, "count": len(text)})
    for char in text:
        if char not in "\t\n" and unicodedata.category(char) == "Cc":
            raise serializers.ValidationError(_("%(what)s can't contain control characters; found U+%(code)s.") % {"what": what, "code": format(ord(char), "04X")})
    return text.strip("\n") if text.strip() else ""


_HTTP_URL = URLValidator(schemes=["http", "https"])


def _http_url(value, what):
    """"" or an http(s) URL, as it is saved: the scheme and the host in lower case, a host in another
    script as punycode (例子.中国 as xn--fsqu00a.xn--fiqs8s). The pages' CSP names the logo's and
    the icon's origins, and a header with other characters than ASCII is no policy at all."""
    url = value.strip()
    if not url:
        return ""
    if len(url) > URL_MAX:
        raise serializers.ValidationError(ngettext("%(what)s is at most %(limit)s character.", "%(what)s is at most %(limit)s characters.", URL_MAX) % {"what": what, "limit": URL_MAX})
    try:
        _HTTP_URL(url)
    except DjangoValidationError:
        raise serializers.ValidationError(_("%(what)s must be a whole http:// or https:// address.") % {"what": what}) from None
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise serializers.ValidationError(_("%(what)s can't contain a username and password.") % {"what": what})
    host = parts.hostname or ""
    if not host.isascii():
        try:  # UTS #46, as browsers: Python's own codec is IDNA 2003, which made faß.de fass.de (API-13)
            host = idna.encode(host, uts46=True).decode("ascii")
        except UnicodeError:  # idna's errors are UnicodeErrors; IDNA 2008 refuses some that browsers take (☃.net)
            raise serializers.ValidationError(_("The domain in %(what)s is wrong, or has to be written in its xn-- form.") % {"what": what}) from None
    netloc = f"[{host}]" if ":" in host else host
    try:
        port = parts.port
    except ValueError:
        raise serializers.ValidationError(_("The port in %(what)s is wrong.") % {"what": what}) from None
    if port is not None:
        netloc += f":{port}"
    url = urlunsplit((parts.scheme.lower(), netloc.lower(), parts.path, parts.query, parts.fragment))
    if len(url) > URL_MAX:  # as stored: a host in another script is longer as punycode (API-11)
        raise serializers.ValidationError(ngettext("%(what)s is at most %(limit)s character (a non-ASCII domain counts in its xn-- form).", "%(what)s is at most %(limit)s characters (a non-ASCII domain counts in its xn-- form).", URL_MAX) % {"what": what, "limit": URL_MAX})
    return url


def _site_path(value, what):
    """A path of this site: /, /folder/file?x=1. Not //host (another site), nor \\ (browsers read it
    as /), nor control characters."""
    if (
        not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(char) <= 0x20 or ord(char) == 0x7F for char in value)
        or urlsplit(value).netloc
    ):
        raise serializers.ValidationError(_("%(what)s must be an http(s) address or a path on this site starting with /.") % {"what": what})
    if len(value) > URL_MAX:
        raise serializers.ValidationError(ngettext("%(what)s is at most %(limit)s character.", "%(what)s is at most %(limit)s characters.", URL_MAX) % {"what": what, "limit": URL_MAX})
    return value


_SUFFIX = re.compile(r"[0-9a-z][0-9a-z_+~-]{0,19}")
_SUFFIX_SPLIT = re.compile(r"[\s,，、;；]+")
MAX_SUFFIXES = 500


def clean_suffixes(value, kind, what):
    """The list as it is saved: "mp4,webm" (lower case, without dots or repeats), or "" for the
    default list (empty, or the same suffixes as the default)."""
    suffixes = []
    for item in _SUFFIX_SPLIT.split(unicodedata.normalize("NFKC", value).lower()):
        item = item.strip().lstrip("*").lstrip(".")
        if not item:
            continue
        if not _SUFFIX.fullmatch(item):
            raise serializers.ValidationError(
                _("\"%(suffix)s\" in %(what)s isn't a file suffix: only English letters, "
                  "digits and _ + ~ -, at most 20 characters, without the dot.") % {"what": what, "suffix": item[:30]}
            )
        if item not in suffixes:
            suffixes.append(item)
    if len(suffixes) > MAX_SUFFIXES:
        raise serializers.ValidationError(ngettext("%(what)s has at most %(limit)s.", "%(what)s has at most %(limit)s.", MAX_SUFFIXES) % {"what": what, "limit": MAX_SUFFIXES})
    if not suffixes or set(suffixes) == set(DEFAULT_SUFFIXES[kind].split(",")):
        return ""
    return ",".join(suffixes)


def _lines(value, what, *, pattern, explain, most, length=URL_MAX):
    """Rules one per line: trimmed, blank lines and repeats dropped; the first bad lines named (blocklist.shown)."""
    kept, problems = [], []
    for number, line in enumerate(value.replace("\r\n", "\n").replace("\r", "\n").split("\n"), start=1):
        rule = line.strip()
        if not rule:
            continue
        if len(rule) > length or not pattern.fullmatch(rule):
            problems.append(_('Line %(number)s, "%(rule)s": %(error)s') % {"number": number, "rule": rule[:60], "error": explain})
        elif rule not in kept:
            kept.append(rule)
    problems = blocklist.shown(problems)
    if len(kept) > most:
        problems.append(ngettext("%(what)s is at most %(limit)s line; it has %(count)s now.", "%(what)s is at most %(limit)s lines; it has %(count)s now.", most) % {"what": what, "limit": most, "count": len(kept)})
    if problems:
        raise serializers.ValidationError(problems)
    return "\n".join(kept)


class TextField(StringField):
    """A string as it was sent: the validate_<name> methods clean it."""

    def __init__(self, raw_max, **kwargs):
        kwargs.setdefault("allow_blank", True)
        kwargs.setdefault("trim_whitespace", False)
        super().__init__(max_length=raw_max, **kwargs)


# ------------------------------------------------------------------ short-link lifetimes

UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800, "M": 2592000, "y": 31536000}
UNIT_LABELS = {"s": gettext_lazy("seconds"), "m": gettext_lazy("minutes"), "h": gettext_lazy("hours"), "d": gettext_lazy("days"), "w": gettext_lazy("weeks"), "M": gettext_lazy("months"), "y": gettext_lazy("years"), "forever": gettext_lazy("Never")}
FOREVER = "forever"
FOREVER_SECONDS = -1  # a short link made with it has no expiry (expires_at NULL)
MAX_EXPIRIES = 20
MAX_EXPIRY_SECONDS = 100 * UNIT_SECONDS["y"]


class ExpirySerializer(serializers.Serializer):
    value = serializers.IntegerField(min_value=1, max_value=100_000, error_messages={"min_value": gettext_lazy("Must be more than 0.")})
    unit = serializers.ChoiceField(choices=list(UNIT_LABELS.items()))
    seconds = serializers.IntegerField(required=False)

    def validate(self, attrs):
        unit = attrs["unit"]
        seconds = FOREVER_SECONDS if unit == FOREVER else attrs["value"] * UNIT_SECONDS[unit]
        if unit == FOREVER:
            attrs["value"] = 1
        elif seconds > MAX_EXPIRY_SECONDS:
            raise serializers.ValidationError(_('A lifetime is at most 100 years; for longer, choose "Forever".'))
        if "seconds" in attrs and attrs["seconds"] != seconds:
            raise serializers.ValidationError(_("seconds should be %(seconds)s (%(duration)s).") % {"seconds": seconds, "duration": expiry_label({"value": attrs["value"], "unit": unit})})
        return {"value": attrs["value"], "unit": unit, "seconds": seconds}


# "7 天": a number of units, in the request's language with its plural (1 day, 7 days)
_UNIT_COUNTS = {
    "s": lambda n: ngettext("%(n)s second", "%(n)s seconds", n), "m": lambda n: ngettext("%(n)s minute", "%(n)s minutes", n),
    "h": lambda n: ngettext("%(n)s hour", "%(n)s hours", n), "d": lambda n: ngettext("%(n)s day", "%(n)s days", n),
    "w": lambda n: ngettext("%(n)s week", "%(n)s weeks", n), "M": lambda n: ngettext("%(n)s month", "%(n)s months", n),
    "y": lambda n: ngettext("%(n)s year", "%(n)s years", n),
}


def expiry_label(item):
    return _("Never") if item["unit"] == FOREVER else _UNIT_COUNTS[item["unit"]](item["value"]) % {"n": item["value"]}


class ExpiryListField(BoundedListField):
    child = ExpirySerializer()

    def __init__(self, **kwargs):
        messages = {"empty": _("There must be at least one lifetime."), "max_length": ngettext("At most %(limit)s lifetime.", "At most %(limit)s lifetimes.", MAX_EXPIRIES) % {"limit": MAX_EXPIRIES}}
        super().__init__(allow_empty=False, max_length=MAX_EXPIRIES, error_messages=messages, **kwargs)

    def to_internal_value(self, data):
        items = super().to_internal_value(data)
        if sum(item["unit"] == FOREVER for item in items) > 1:
            raise serializers.ValidationError(_('There can be only one "Forever".'))
        seen = {}
        for item in items:
            if item["seconds"] in seen:
                raise serializers.ValidationError(_('"%(first)s" and "%(second)s" are the same length; keep one.') % {"first": expiry_label(item), "second": expiry_label(seen[item["seconds"]])})
            seen[item["seconds"]] = item
        return items


# ------------------------------------------------------------------ the groups


class SettingsSerializer(serializers.ModelSerializer):
    """One page of settings. Subclasses list their fields; changing those in `superuser_fields` needs
    a superuser (D33, D49: see the module's docstring). Sending one unchanged is fine, but a write-only
    one (the OnlyOffice secret) is stored encrypted and can't be compared: sending it at all, "" too,
    counts as a change."""

    superuser_fields: tuple = ()
    label = ""
    serializer_field_mapping = STRING_FIELD_MAPPING  # text columns take JSON strings only (API-07)

    class Meta:
        model = SiteSettings
        fields = []

    def to_internal_value(self, data):
        # A name that isn't one of the page's was ignored, and the answer, 200 with the settings as they were,
        # looked as if it had been saved: ip_blacklist for ip_blocklist blocked nobody (API-05). The read-only
        # ones GET answers are known: the page may be sent back as it was.
        unknown = [name for name in data if name not in self.fields] if isinstance(data, Mapping) else []
        errors = {name[:100]: [_("This page of settings has no such item.")] for name in unknown[:20]}
        try:
            values = super().to_internal_value(data)
        except serializers.ValidationError as error:
            raise serializers.ValidationError({**error.detail, **errors}) from None
        if errors:
            raise serializers.ValidationError(errors)
        return values

    def validate(self, attrs):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if self.instance is not None and not getattr(user, "is_superuser", False):
            changed = [name for name in self.superuser_fields if name in attrs and (
                self.fields[name].write_only or attrs[name] != getattr(self.instance, name))]
            if changed:
                labels = _(", ").join(str(self.fields[name].label) for name in changed)
                raise PermissionDenied(ngettext(
                    'Only superusers can change "%(labels)s": whoever can change it can put their own '
                    "code or content into pages other people open, or get and rewrite files on the site.",
                    'Only superusers can change "%(labels)s": whoever can change them can put their own '
                    "code or content into pages other people open, or get and rewrite files on the site.",
                    len(changed)) % {"labels": labels})
        return attrs


class SiteGroupSerializer(SettingsSerializer):
    """站点设置."""

    label = gettext_lazy("Site settings")
    site_name = TextField(_RAW_MAX, label=gettext_lazy("Site name"), help_text=gettext_lazy("Shown in the browser tab's title and at the top left of the page."))
    favicon = TextField(
        _RAW_MAX, label=gettext_lazy("site icon"),
        help_text=gettext_lazy("The little icon on the browser tab. Upload a PNG, ICO, WebP, JPG or GIF (at most 1 "
        "MB; saved as a 192×192 PNG), or enter an http(s) picture address; with neither, "
        "HFile's own icon is used. After a change, browsers may need a few reloads to show it."),
    )
    home_name = TextField(_RAW_MAX, label=gettext_lazy("Root name"), help_text=gettext_lazy("The first part of the path above the file list. The default, 首页, shows in each visitor's language (Home in English)."))
    home_logo = TextField(
        _RAW_MAX, label="Logo",
        help_text=gettext_lazy("The picture before the site name at the top left of the page. Upload a PNG, "
        "WebP, JPG or GIF (at most 1 MB; scaled to fit 640×160 and saved as a PNG), "
        "or enter an http(s) picture address; with neither, HFile's own icon is used."),
    )
    home_logo_link = TextField(
        _RAW_MAX, label=gettext_lazy("Address the logo opens"),
        help_text=gettext_lazy("What clicking the logo and site name at the top left of the home and "
        "file pages opens: an http(s) address or a path on this site starting "
        "with /; when empty, nothing. On the Manage page it always goes home."),
    )
    icp = TextField(
        _RAW_MAX, label=gettext_lazy("ICP number"), help_text=gettext_lazy('When filled in, shown at the bottom of the home and file pages, linking to the MIIT filing site, like "京ICP备12345678号".'),
    )
    favicon_preview = serializers.SerializerMethodField()
    home_logo_preview = serializers.SerializerMethodField()

    class Meta:
        model = SiteSettings
        fields = ["site_name", "favicon", "home_name", "home_logo", "home_logo_link", "home_logo_target", "icp",
                  "max_file_uploads", "favicon_preview", "home_logo_preview"]
        extra_kwargs = {
            "home_logo_target": {"label": gettext_lazy("Opens in")},
            "max_file_uploads": {
                "min_value": 1, "max_value": 20,
                "help_text": gettext_lazy("With many files chosen, how many upload at once; the rest wait. 1 to 20."),
            },
        }

    def get_favicon_preview(self, obj):
        return obj.favicon_url

    def get_home_logo_preview(self, obj):
        return obj.logo_url

    def validate_site_name(self, value):
        return _text(value, _("Site name"), SITE_NAME_MAX, required=True)

    def validate_home_name(self, value):
        return _text(value, _("Root name"), HOME_NAME_MAX, required=True)

    def validate_icp(self, value):
        return _text(value, _("ICP number"), ICP_MAX)

    def _image(self, value, field, what):
        value = value.strip()
        current = getattr(self.instance, field, "") if self.instance is not None else ""
        if is_upload(value):
            # Uploads are made by PUT .../site/<field>/; here the current one can only be kept.
            if value != current:
                raise serializers.ValidationError(_('%(what)s can only keep the picture uploaded now; to change it, use "Upload picture".') % {"what": what})
            return value
        return _http_url(value, _("%(what)s address") % {"what": what})

    def validate_favicon(self, value):
        return self._image(value, "favicon", _("site icon"))

    def validate_home_logo(self, value):
        return self._image(value, "home_logo", "Logo")

    def validate_home_logo_link(self, value):
        value = value.strip()
        if not value or value.lower().startswith(("http://", "https://")):
            return _http_url(value, _("Address the logo opens"))
        return _site_path(value, _("Address the logo opens"))

    def update(self, instance, validated_data):
        replaced = [getattr(instance, field) for field in ("favicon", "home_logo")
                    if field in validated_data and validated_data[field] != getattr(instance, field)]
        instance = super().update(instance, validated_data)
        for name in replaced:
            forget_upload(name)
        return instance


def forget_upload(name):
    """Deletes an upload that a setting no longer names, once the change is committed. Only a name of
    the uploads' own pattern (site/<32 hex>.png...), whatever the row holds: nothing else is deleted."""
    from django.db import transaction

    if re.fullmatch(UPLOAD_PATTERN, name or "", re.ASCII):
        def delete():
            try:
                default_storage.delete(name)
            except OSError:
                pass  # gone already, or a folder that can't be written: nothing else names it

        transaction.on_commit(delete)


_SUFFIX_HELP = gettext_lazy("When empty, the default is used.")
_PREVIEW_LABELS = {"image": gettext_lazy("Preview image files"), "video": gettext_lazy("Preview video files"), "audio": gettext_lazy("Preview audio files"), "text": gettext_lazy("Preview text files"),
                   "office": gettext_lazy("Preview Office files"), "kkfileview": gettext_lazy("Preview with kkFileView")}


class ViewGroupSerializer(SettingsSerializer):
    """显示设置: 使用习惯, 预览类型, OnlyOffice, kkFileView, 公告, 自定义 CSS/JS."""

    label = gettext_lazy("Display settings")
    superuser_fields = ("announcement", "custom_css", "custom_js", "onlyoffice_url", "onlyoffice_secret", "kkfileview_url")
    image_suffixes = TextField(20_000, label=gettext_lazy("Image suffixes"), help_text=_SUFFIX_HELP)
    video_suffixes = TextField(
        20_000, label=gettext_lazy("Video suffixes"),
        help_text=gettext_lazy("Whether a video plays depends on the browser supporting its codec; modern browsers generally play H.264 mp4 only."),
    )
    audio_suffixes = TextField(20_000, label=gettext_lazy("Audio suffixes"), help_text=_SUFFIX_HELP)
    text_suffixes = TextField(20_000, label=gettext_lazy("Text suffixes"), help_text=gettext_lazy("md and markdown are shown as Markdown."))
    office_suffixes = TextField(20_000, label=gettext_lazy("Office suffixes"), help_text=gettext_lazy('Preview and edit with OnlyOffice; fill in its address in "OnlyOffice" first.'))
    kkfileview_suffixes = TextField(20_000, label=gettext_lazy("kkFileView suffixes"), help_text=gettext_lazy('Preview with kkFileView; fill in its address in "kkFileView" first.'))
    onlyoffice_url = TextField(
        _RAW_MAX, label=gettext_lazy("OnlyOffice server address"),
        help_text=gettext_lazy("Like https://office.example.com; when empty, OnlyOffice isn't "
        "used. To preview, OnlyOffice must be able to download files from "
        "this site, and to save, reach this site back (the settings file's "
        "[site] url); with an https site, OnlyOffice must be https too."),
    )
    onlyoffice_secret = StringField(
        label="OnlyOffice Secret", max_length=SECRET_MAX, required=False, allow_blank=True, trim_whitespace=False,
        write_only=True,
        help_text=gettext_lazy("OnlyOffice's secret (the JWT key) when it has JWT authentication on; otherwise "
                               "leave it empty. Saved encrypted and not shown again; empty means unchanged."),
    )
    onlyoffice_secret_set = serializers.SerializerMethodField()
    kkfileview_url = TextField(
        _RAW_MAX, label=gettext_lazy("kkFileView server address"),
        help_text=gettext_lazy("Like https://kk.example.com; when empty, kkFileView isn't used. "
                               "kkFileView must be able to download the files to preview from this site."),
    )
    announcement = TextField(
        ANNOUNCEMENT_MAX * 2, label=pgettext_lazy("its text", "Announcement"),
        help_text=gettext_lazy("Shown above the home and file pages; visitors can close it (it shows again "
        "when it changes). Markdown works, and HTML too; the page doesn't run "
        "scripts or use inline styles in it (<script>, onclick, style attributes): "
        'put analytics code in "Custom JS" and styles in "Custom CSS".'),
    )
    custom_css = TextField(
        CUSTOM_CODE_MAX * 2, label=gettext_lazy("Custom CSS"),
        help_text=gettext_lazy("Added to the home, file and share pages, without <style></style>. The Manage, login and setup pages don't have it."),
    )
    custom_js = TextField(
        CUSTOM_CODE_MAX * 2, label=gettext_lazy("Custom JS"),
        help_text=gettext_lazy("Analytics code, say, added to the home, file and share pages. <script></script> "
        "is optional: scripts run in their order, and external scripts need https "
        "addresses; inline event attributes (onclick and the like) don't run. With custom "
        "JS, these pages may load scripts, pictures and APIs over https. The Manage, "
        "login and setup pages don't have it, so if a script breaks, you can still log in "
        "and fix it. It can do anything the site's own code can (including reading what's "
        "typed on the page): only put code you trust here."),
    )

    class Meta:
        model = SiteSettings
        fields = [
            "layout", "mobile_layout", "mobile_show_size", "root_show_storage", "default_save_password",
            "download_confirm", "file_click_mode", "mobile_file_click_mode", "max_show_size", "load_more_size",
            "default_sort_field", "default_sort_order", "show_document",
            *[name for kind in PREVIEW_KINDS for name in (f"{kind}_preview", f"{kind}_suffixes")],
            "text_preview_max_kb", "onlyoffice_url", "onlyoffice_secret", "onlyoffice_secret_set",
            "kkfileview_url", "kkfileview_open_mode", "show_announcement", "announcement", "custom_css", "custom_js",
        ]
        extra_kwargs = {
            "layout": {"help_text": gettext_lazy("How the file list looks on computers: Full width fills the window, Centered is a column in the middle, Card puts it on a card.")},
            "mobile_show_size": {"label": gettext_lazy("Show file sizes on phones"), "help_text": gettext_lazy("Only for the list view.")},
            "download_confirm": {"help_text": gettext_lazy("When off, downloads start right away without asking.")},
            "file_click_mode": {"help_text": gettext_lazy("Whether a single or double click opens folders and previews files.")},
            "max_show_size": {
                "min_value": 1, "max_value": 100_000,
                "help_text": gettext_lazy("With many files in a folder, this many are shown first, then \"Load more\", so the page doesn't stall."),
            },
            "load_more_size": {"min_value": 1, "max_value": 10_000},
            "default_sort_order": {"help_text": gettext_lazy("Files and folders sort separately; folders always come first.")},
            "text_preview_max_kb": {
                "min_value": 1, "max_value": 10_240, "help_text": gettext_lazy("Text files larger than this aren't previewed but downloaded. 1 to 10240."),
            },
            "kkfileview_open_mode": {"label": gettext_lazy("kkFileView opens")},  # DRF would capitalise the model's name
            **{f"{kind}_preview": {"label": _PREVIEW_LABELS[kind]} for kind in PREVIEW_KINDS},
        }

    def to_representation(self, instance):
        data = super().to_representation(instance)
        for kind in PREVIEW_KINDS:
            data[f"{kind}_suffixes"] = instance.suffix_text(kind)  # the default one shows when "" is saved
        return data

    def get_onlyoffice_secret_set(self, obj):
        return secrets.is_set(obj.onlyoffice_secret)

    def validate_onlyoffice_url(self, value):
        return _http_url(value, _("OnlyOffice server address")).rstrip("/")

    def validate_kkfileview_url(self, value):
        return _http_url(value, _("kkFileView server address")).rstrip("/")

    def validate_onlyoffice_secret(self, value):
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
            raise serializers.ValidationError(_("The secret can't contain line breaks or control characters."))
        return value

    def validate_announcement(self, value):
        return _block(value, _("Announcement"), ANNOUNCEMENT_MAX)

    def validate_custom_css(self, value):
        return _block(value, _("Custom CSS"), CUSTOM_CODE_MAX)

    def validate_custom_js(self, value):
        return _block(value, _("Custom JS"), CUSTOM_CODE_MAX)

    def update(self, instance, validated_data):
        secret = validated_data.pop("onlyoffice_secret", None)
        if secret is not None:
            validated_data["onlyoffice_secret"] = secrets.encrypt(secret)
        return super().update(instance, validated_data)


for _kind in PREVIEW_KINDS:
    def _validate_suffixes(self, value, kind=_kind):
        return clean_suffixes(value, kind, self.fields[f"{kind}_suffixes"].label)

    setattr(ViewGroupSerializer, f"validate_{_kind}_suffixes", _validate_suffixes)


_REFERER = re.compile(r"(?:https?|\*)://[^\s]+", re.IGNORECASE)
_PREFIX = re.compile(r"[A-Za-z0-9_-]{1,32}")


class LinkGroupSerializer(SettingsSerializer):
    """直链和短链: 通用设置, 直链设置, 短链设置."""

    label = gettext_lazy("Direct and short links")
    referer_rules = TextField(
        100_000, label=gettext_lazy("Allowlist or blocklist"),
        help_text=gettext_lazy("One per line, with the scheme; * is a wildcard: https://*.example.com is "
        "example.com and all its subdomains, *://example.com matches http and https, "
        "https://example.com/** is every page of that site (the same as no path). When a "
        "link is opened from another site, browsers usually send only that site's address "
        "without the path, so rules with paths mostly matter for this site's own pages."),
    )
    direct_link_prefix = TextField(
        _RAW_MAX, label=gettext_lazy("Direct link prefix"),
        help_text=gettext_lazy("A direct link is: site address/prefix/storage-alias/file-path. English "
        "letters, digits, - and _ only, at most 32 characters, and it can't be "
        "one of the site's own addresses, a storage source's alias or the Django "
        "admin address. Once changed, old direct links stop working right away."),
    )
    short_link_expiries = ExpiryListField(label=gettext_lazy("Short link lifetimes"), help_text=gettext_lazy("The lifetimes to choose from when creating a short link, listed in this order; the first is the default."))

    class Meta:
        model = SiteSettings
        fields = [
            "record_download_log", "referer_type", "referer_allow_empty", "referer_rules", "link_limit_seconds",
            "link_limit_downloads", "show_path_link", "direct_link_prefix", "path_link_anonymous", "show_short_link",
            "short_link_expiries",
        ]
        extra_kwargs = {
            "record_download_log": {"help_text": gettext_lazy('Logs every download through direct and short links, and downloads through this site after clicking "Download" in the file list or a preview; see them in "Download log".')},
            "referer_type": {"help_text": gettext_lazy("Checks the referring site (Referer) when direct and short links are opened. Once a download is sent on to the storage source's own address, it can't be checked.")},
            "referer_allow_empty": {"help_text": gettext_lazy("Opening a link straight in the browser, and some download tools, send no Referer.")},
            "link_limit_seconds": {"label": gettext_lazy("Seconds"), "min_value": 0, "max_value": 30 * 86400},
            "link_limit_downloads": {
                "label": gettext_lazy("Downloads"), "min_value": 0, "max_value": 10_000_000,
                "help_text": gettext_lazy("Each IP can download at most this many times in this many seconds, direct and short links together; either one 0 means no limit. The storage source's own addresses that downloads are sent on to don't go through HFile and can't be limited."),
            },
            "show_path_link": {"help_text": gettext_lazy("A switch for the whole site: when off, \"Get direct links\" isn't shown and existing direct links don't download, whatever the storage source permissions.")},
            "path_link_anonymous": {
                "label": gettext_lazy("People who aren't logged in can download with direct links too"),
                "help_text": gettext_lazy("When off, opening a direct link needs the download permission for the "
                "file (the anonymous user's when not logged in), the file in your root "
                "folder, and its folder's password entered in this browser; when on, "
                "anyone who knows the address can download files from any enabled "
                "storage source (including those the anonymous user has no permission "
                "on), and only filter rules still apply (folder passwords don't)."),
            },
            "show_short_link": {"help_text": gettext_lazy("A switch for the whole site: when off, \"Create short links\" isn't shown and existing short links don't download, whatever the storage source permissions.")},
        }

    def validate_referer_rules(self, value):
        return _lines(
            value, _("Allowlist or blocklist"), pattern=_REFERER, most=200,
            explain=_("Must start with http://, https:// or *://, with no spaces."),
        )

    def validate_direct_link_prefix(self, value):
        prefix = value.strip().strip("/").strip()
        if not _PREFIX.fullmatch(prefix):
            raise serializers.ValidationError(_("The direct link prefix can only have English letters, digits, - and _, 1 to 32 characters."))
        from .apps import link_prefix_taken

        # The default is one of the site's own names (config/admin_path.TAKEN), kept for it.
        reason = reserved.problem(prefix, own=link_prefix_taken, static=prefix.lower() != DEFAULT_DIRECT_LINK_PREFIX)
        if reason:
            raise serializers.ValidationError(reason)
        return prefix


class ProxyGroupSerializer(SettingsSerializer):
    """代理下载: the limits per IP of downloads through the site (apps/files/limits.py). Each storage
    source's own limit is in its settings."""

    label = gettext_lazy("Proxy download")

    class Meta:
        model = SiteSettings
        fields = ["proxy_ip_connections", "proxy_ip_speed"]
        extra_kwargs = {
            "proxy_ip_connections": {
                "min_value": 0, "max_value": 1000,
                "help_text": gettext_lazy("How many downloads through this site one IP can have going at once; "
                "each connection of a download tool counts, and so does each picture a gallery page loads. "
                "One more gets 429 (Too Many Requests). 0 means no limit."),
            },
            "proxy_ip_speed": {
                "min_value": 0, "max_value": 10_000_000,
                "help_text": gettext_lazy("How fast all of one IP's downloads through this site go together, "
                "however many connections it opens; 1024 KB/s is 1 MB/s. 0 means no limit."),
            },
        }


_ENTRY = re.compile(r"[A-Za-z0-9_-]{0,32}")


class SecurityGroupSerializer(SettingsSerializer):
    """安全设置. Their behaviour is the accounts app's (login, captcha, secure entry)."""

    label = gettext_lazy("Security settings")
    superuser_fields = ("guest_home_html",)
    secure_login_entry = TextField(
        _RAW_MAX, label=gettext_lazy("Secure login entrance"),
        help_text=gettext_lazy('When set, logging in only works at "site address/login/entrance/" and there is '
        "no login box anywhere else, so the login can't be found by scanning. English "
        "letters, digits, - and _ only, at most 32 characters; empty means none. Note the "
        'address down (it also shows in "Site settings" in the Django admin).'),
    )
    guest_home_html = TextField(
        GUEST_HTML_MAX * 2, label=gettext_lazy("Custom page"),
        help_text=gettext_lazy("With \"Custom page\", the whole page people who aren't logged in see on the home and "
        "file pages. It can be just the body, which gets the site's name, icon and styles, and "
        "HFile's style classes work, like <a class=\"btn btn-filled\" href=\"/login/\">Log "
        "in</a>; or a whole HTML page. Inline scripts don't run (to send people to the login "
        'page, choose "Go to the login page"), and external scripts need https addresses.'),
    )

    class Meta:
        model = SiteSettings
        fields = ["secure_login_entry", "show_login", "guest_home", "guest_home_html", "login_captcha",
                  "sso_auto_register"]
        extra_kwargs = {
            "show_login": {
                "help_text": gettext_lazy('Shows a "Log in" button at the top right of the page. Only the button: opening the login page "site address/login/" works either way. Never shown when a secure login entrance is set.'),
            },
            "guest_home": {"help_text": gettext_lazy("What people who aren't logged in see on the home and file pages. Shares, direct and short links aren't affected.")},
            "login_captcha": {"help_text": gettext_lazy("Logging in with a password also asks for the characters in a picture, against mass password guessing. A strong password for every account still works best.")},
        }

    def validate_secure_login_entry(self, value):
        entry = value.strip().strip("/").strip()
        if not _ENTRY.fullmatch(entry):
            raise serializers.ValidationError(_("The secure login entrance can only have English letters, digits, - and _, at most 32 characters."))
        return entry

    def validate_guest_home_html(self, value):
        return _block(value, _("Custom page"), GUEST_HTML_MAX)

    def validate(self, attrs):
        attrs = super().validate(attrs)
        home = attrs.get("guest_home", self.instance.guest_home if self.instance else GuestHome.NORMAL)
        html = attrs.get("guest_home_html", self.instance.guest_home_html if self.instance else "")
        if home == GuestHome.CUSTOM and not html.strip():
            raise serializers.ValidationError({"guest_home_html": [_("With \"Custom page\" chosen, the page's HTML is needed.")]})
        return attrs


class AccessGroupSerializer(SettingsSerializer):
    """访问控制."""

    label = gettext_lazy("Access control")
    ip_blocklist = TextField(
        200_000, label=gettext_lazy("IP blocklist"),
        help_text=gettext_lazy("One IP or range per line, like 203.0.113.7, 192.168.0.0/24 or "
        "2001:db8::/32 (IPv6 works); lines starting with # are comments. "
        "IPs on the list get 403 for every address of this site."),
    )
    ua_blocklist = TextField(
        200_000, label=gettext_lazy("UA blocklist"),
        help_text=gettext_lazy("One browser identifier (User-Agent) per line; * is any characters, the rule must match "
                               "the whole UA, and case matters, like *Thunder*; lines starting with # are comments."),
    )
    your_ip = serializers.SerializerMethodField()
    your_user_agent = serializers.SerializerMethodField()

    class Meta:
        model = SiteSettings
        fields = ["ip_blocklist", "ua_blocklist", "your_ip", "your_user_agent"]

    def get_your_ip(self, obj):
        request = self.context.get("request")
        return net.client_ip(request) if request else None

    def get_your_user_agent(self, obj):
        request = self.context.get("request")
        return blocklist.user_agent(request.META) if request else ""

    def validate_ip_blocklist(self, value):
        try:
            return blocklist.normalized_ip_text(value)
        except blocklist.RuleError as error:
            raise serializers.ValidationError(str(error).split("\n")) from None

    def validate_ua_blocklist(self, value):
        try:
            return blocklist.normalized_ua_text(value)
        except blocklist.RuleError as error:
            raise serializers.ValidationError(str(error).split("\n")) from None

    def validate(self, attrs):
        attrs = super().validate(attrs)
        request = self.context.get("request")
        if request is not None:
            ip_text = attrs.get("ip_blocklist", self.instance.ip_blocklist if self.instance else "")
            ua_text = attrs.get("ua_blocklist", self.instance.ua_blocklist if self.instance else "")
            yourself = blocking_yourself(blocklist.parse(ip_text, ua_text), request)
            if yourself:
                raise serializers.ValidationError(yourself)
        return attrs


def blocking_yourself(rules, request):
    """{field: [message]} when the rules block the request (the admin saving them), else {}."""
    ip = net.client_ip(request)
    user_agent = blocklist.user_agent(request.META)
    problems = {}
    rule = rules.ip_rule(ip)
    if rule is not None:
        problems["ip_blocklist"] = [
            _('"%(rule)s" would block you: the site sees your IP as %(ip)s. After saving, '
              "you couldn't open this site. If that isn't your IP (a reverse proxy's, say), "
              "check the settings file's [server] proxies in \"Diagnostics\" first.") % {"rule": rule, "ip": ip}
        ]
    rule = rules.ua_rule(user_agent)
    if rule is not None:
        problems["ua_blocklist"] = [_("\"%(rule)s\" would block the browser you're using; after saving, you couldn't open this site.") % {"rule": rule}]
    return problems


GROUPS = {
    "site": SiteGroupSerializer,
    "view": ViewGroupSerializer,
    "link": LinkGroupSerializer,
    "proxy": ProxyGroupSerializer,
    "security": SecurityGroupSerializer,
    "access": AccessGroupSerializer,
}


class AccessTestSerializer(serializers.Serializer):
    """POST manage/settings/access/test/: would these lists block this IP or User-Agent?"""

    ip_blocklist = TextField(200_000, required=False)
    ua_blocklist = TextField(200_000, required=False)
    ip = StringField(max_length=100, required=False, allow_blank=True)
    user_agent = StringField(max_length=2000, required=False, allow_blank=True, trim_whitespace=False)

    def validate_ip_blocklist(self, value):
        return AccessGroupSerializer.validate_ip_blocklist(self, value)

    def validate_ua_blocklist(self, value):
        return AccessGroupSerializer.validate_ua_blocklist(self, value)

    def validate_ip(self, value):
        value = value.strip()
        if value and net.address_of(value) is None:
            raise serializers.ValidationError(_("Not an IP address."))
        return net.address_of(value) if value else ""


# ------------------------------------------------------------------ GET /api/v1/site/


class PublicSiteSerializer(serializers.ModelSerializer):
    """What everybody may know: what the pages' scripts need (inventory section 2 "Pub"). Never
    secrets or internal values (the OnlyOffice secret, the login entry, the blocklists, referer
    rules...)."""

    home_name = serializers.SerializerMethodField()
    home_logo = serializers.SerializerMethodField()
    preview_suffixes = serializers.SerializerMethodField()
    show_login = serializers.BooleanField(source="login_visible")

    class Meta:
        model = SiteSettings
        fields = [
            "site_name", "home_name", "home_logo", "home_logo_link", "home_logo_target", "icp", "max_file_uploads",
            "layout", "mobile_layout", "mobile_show_size", "root_show_storage", "default_save_password",
            "download_confirm", "file_click_mode", "mobile_file_click_mode", "max_show_size", "load_more_size",
            "default_sort_field", "default_sort_order", "show_document", "preview_suffixes", "text_preview_max_kb",
            "onlyoffice_url", "kkfileview_url", "kkfileview_open_mode", "show_path_link", "direct_link_prefix",
            "show_short_link", "short_link_expiries", "show_login",
        ]
        read_only_fields = fields

    def get_home_name(self, obj):
        # The default (首页) in the page's language; a name the admin gave stays as it is.
        return _("Home") if obj.home_name == HOME_NAME_DEFAULT else obj.home_name

    def get_home_logo(self, obj):
        return image_url(obj.home_logo)

    def get_preview_suffixes(self, obj):
        return obj.preview_suffixes()


# ------------------------------------------------------------------ the pages' forms


@dataclass(frozen=True)
class FieldSpec:
    """What a /manage/ page needs to draw one field (apps/siteconf/templates/siteconf/manage/_field.html)."""

    name: str
    label: str
    help: str
    kind: str  # switch, select, number, text, textarea, code, image, secret, suffixes, expiries
    choices: tuple = ()
    max_length: int | None = None
    min_value: int | None = None
    max_value: int | None = None
    superuser_only: bool = False
    default: str = ""  # the default suffix list ("恢复默认")
    rows: int = 4

    @property
    def counted(self):
        """A short text with a counter (12 / 30), and its help beside it."""
        return self.kind == "text" and self.max_length is not None and self.max_length <= 100


_KINDS = {
    "favicon": "image", "home_logo": "image", "onlyoffice_secret": "secret", "short_link_expiries": "expiries",
    "announcement": "code", "custom_css": "code", "custom_js": "code", "guest_home_html": "code",
    "referer_rules": "textarea", "ip_blocklist": "textarea", "ua_blocklist": "textarea",
    **{f"{kind}_suffixes": "suffixes" for kind in PREVIEW_KINDS},
}
_LIMITS = {
    "site_name": SITE_NAME_MAX, "home_name": HOME_NAME_MAX, "icp": ICP_MAX, "home_logo_link": URL_MAX,
    "onlyoffice_url": URL_MAX, "kkfileview_url": URL_MAX, "direct_link_prefix": 32, "secure_login_entry": 32,
    "announcement": ANNOUNCEMENT_MAX, "custom_css": CUSTOM_CODE_MAX, "custom_js": CUSTOM_CODE_MAX,
    "guest_home_html": GUEST_HTML_MAX, "onlyoffice_secret": SECRET_MAX,
}
_ROWS = {"announcement": 8, "custom_css": 8, "custom_js": 8, "guest_home_html": 10, "ip_blocklist": 8, "ua_blocklist": 6,
         "referer_rules": 5, "kkfileview_suffixes": 4}


def form_fields(group):
    """{name: FieldSpec} of a group's writable fields, from its serializer."""
    serializer_class = GROUPS[group]
    specs = {}
    for name, field in serializer_class().fields.items():
        if field.read_only:
            continue
        if isinstance(field, serializers.BooleanField):
            kind = "switch"
        elif isinstance(field, serializers.ChoiceField):
            kind = "select"
        elif isinstance(field, serializers.IntegerField):
            kind = "number"
        else:
            kind = _KINDS.get(name, "text")
        default = DEFAULT_SUFFIXES[name.removesuffix("_suffixes")].replace(",", ", ") if kind == "suffixes" else ""
        specs[name] = FieldSpec(
            name=name,
            label=str(field.label or name),
            help=str(field.help_text or ""),
            kind=kind,
            choices=tuple((str(value), str(label)) for value, label in getattr(field, "choices", {}).items()),
            max_length=_LIMITS.get(name),
            min_value=getattr(field, "min_value", None),
            max_value=getattr(field, "max_value", None),
            superuser_only=name in serializer_class.superuser_fields,
            default=default,
            rows=_ROWS.get(name, 2 if kind == "suffixes" else 4),
        )
    return specs
