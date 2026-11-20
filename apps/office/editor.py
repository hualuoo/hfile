"""The editor config: what the editor page hands to OnlyOffice's DocsAPI.DocEditor (inventory 7.3).

    config, access = for_file(request, "disk", "/docs/a.docx", password=None)
    config = for_share(request, "Ab3dE7fG", "/a.docx")

Checks, through apps.storages.access (D8), before anything is handed out:
- 显示设置 has an OnlyOffice 服务地址 (else onlyoffice_off);
- the storage, the actor, the grant, the root; the file is of the kind office (W3, the admin's list);
- download (READ, which previewing needs too), the filter rules and the folder password, as for any read:
  the config holds a link the Document Server downloads the file by;
- the file exists and is a file.

Editing is offered only with an OnlyOffice secret (the config and the callbacks are signed with it),
to an actor who may overwrite the file (上传 and 删除, D17a, with the filter rules and folder passwords
of both), never in a share, and only for the types the Document Server saves in their own format (a
.doc would come back as .docx). The editor that may edit becomes the identity that saves.

The answer is the Document Server's config: document {fileType, key, title, url, permissions},
documentType, type "mobile" on phones, editorConfig {callbackUrl, lang, mode, user, customization}, and
its JWT "token" (HS256 over the whole object, with iat and an exp ten minutes on) when a secret is set. document.url is a signed /pd/ link of
ten minutes, absolute from [site] url (D14): the Document Server fetches it when it first opens the key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext as _

from apps.accounts.models import Kind
from apps.accounts.permissions import Permission
from apps.core import origin
from apps.files import kinds, signing
from apps.files.service import driver_problem, needs_bypass
from apps.siteconf.models import SiteSettings
from apps.storages import access as policy
from apps.storages.access import READ, UPLOAD, AccessDenied
from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError
from apps.uploads.service import overwrite_allowed

from . import sessions, shares, tokens
from .sessions import OfficeError

logger = logging.getLogger("hfile.office")

URL_LIFETIME = 600  # seconds of document.url (W2)
# editorConfig.lang: the page's language, in the Document Server's codes (a language, or a language and a
# region: en, pt-BR). Django's zh-hans is its zh-CN; en is the same in both.
EDITOR_LANGUAGES = {"zh-hans": "zh-CN"}

# documentType by extension; the editor works it out itself for the others.
DOCUMENT_TYPES = {
    **dict.fromkeys("doc docm docx dot dotm dotx epub fb2 fodt htm html mht mhtml odt ott rtf txt wps wpt xml"
                    .split(), "word"),
    **dict.fromkeys("csv et ett fods ods ots xls xlsb xlsm xlsx xlt xltm xltx".split(), "cell"),
    **dict.fromkeys("dps dpt fodp odp otp pot potm potx pps ppsm ppsx ppt pptm pptx".split(), "slide"),
}
# What the Document Server saves back in the file's own format: only these may be edited. Others (the
# old binary .doc, .xls, .ppt) would come back in another format under the old name, so they open to view.
EDITABLE = frozenset(
    "docx docm dotx dotm xlsx xlsm xltx xltm pptx pptm potx potm ppsx ppsm odt ott ods ots odp otp rtf txt csv"
    .split())


def _enabled(site: SiteSettings) -> None:
    if not site.onlyoffice_url:
        raise OfficeError("onlyoffice_off", _("The site has no OnlyOffice server address set, so Office files can't be opened online."), status_code=404)


def _office(storage_path: str, site: SiteSettings) -> str:
    """The file's extension; OfficeError unless it is of the kind office."""
    name = paths.name(storage_path)
    if not name or kinds.kind_of(name, site) != "office":
        raise OfficeError("not_office", _("This isn't an Office file OnlyOffice can open."))
    return kinds.extension(name)


def _stat(access, storage_path: str):
    try:
        entry = access.driver.stat(storage_path)
    except DriverError as error:
        user = access.actor.user
        code, message, status_code = driver_problem(error, staff=bool(user.is_staff) and not access.actor.via_share)
        if code in ("storage_auth", "storage_error", "failed"):
            logger.warning("Storage %s: stat for OnlyOffice failed: %s", access.storage.key, error.message)
        raise OfficeError(code, message, status_code=status_code) from None
    if entry.is_dir:
        raise OfficeError("not_a_file", _("This is a folder, not a file."))
    return entry


def _may_edit(access, storage_path: str, extension: str, secret: str) -> bool:
    if not secret or access.actor.via_share or extension not in EDITABLE:
        return False
    try:
        # The rules READ just passed (a folder password sent with the request opened them for it).
        access.check(UPLOAD, storage_path, is_dir=False, keys=access.opened)
        overwrite_allowed(access, storage_path, keys=access.opened)
    except AccessDenied:
        return False
    return True


def _user(user) -> dict:
    """editorConfig.user: who the co-editors see. Visitors who aren't logged in get no id: the Document
    Server gives each of them one of their own."""
    if user is None or not user.is_authenticated or user.kind == Kind.GUEST:
        return {"name": _("Anonymous user")}
    return {"id": f"hfile-{user.pk}", "name": user.display_name}


@dataclass(frozen=True)
class Display:
    """How the browser asks to show the editor: OnlyOffice's phone layout, its dark theme."""

    mobile: bool = False
    dark: bool = False


PLAIN = Display()


def build(request, access, storage_path: str, *, session, can_edit: bool, viewer, secret: str,
          display: Display = PLAIN) -> dict:
    """The Document Server's config of a checked file and its session."""
    name = paths.name(storage_path)
    extension = kinds.extension(name)
    link = signing.sign(access.storage, storage_path, lifetime=URL_LIFETIME,
                        ignore_hidden=needs_bypass(access, storage_path))
    downloads = access.has(Permission.DOWNLOAD)
    language = translation.get_language()
    editor_config = {
        "callbackUrl": origin.absolute(reverse("office:callback"), request),
        "lang": EDITOR_LANGUAGES.get(language, language),
        "mode": "edit" if can_edit else "view",
        "user": _user(viewer),
    }
    customization = {}
    if can_edit:
        # The editor's 保存 (Ctrl+S) saves into the storage at once (status 6), not only once everybody
        # has closed the document (status 2).
        customization["forcesave"] = True
    if display.dark:
        customization["uiTheme"] = "theme-dark"  # the site's dark theme (left out: the editor's own choice)
    if customization:
        editor_config["customization"] = customization
    config = {
        "width": "100%",
        "height": "100%",
        "document": {
            "fileType": extension,
            "key": session.key,
            "title": name,
            "url": origin.absolute(link, request),
            # What the editor offers besides reading: downloading and printing follow 下载 (D37).
            "permissions": {"edit": can_edit, "download": downloads, "print": downloads},
        },
        "editorConfig": editor_config,
    }
    if extension in DOCUMENT_TYPES:
        config["documentType"] = DOCUMENT_TYPES[extension]
    if display.mobile:
        config["type"] = "mobile"  # the editor's layout for phones (it isn't chosen by the editor itself)
    if secret:
        config["token"] = tokens.sign_config(config, secret)
    return config


def for_file(request, storage_key: str, path: str, *, password: str | None = None, display: Display = PLAIN):
    """(config, access) for a file of a storage, as the browser opens it."""
    site = SiteSettings.load()
    _enabled(site)
    access = policy.open_storage(request, storage_key)
    storage_path = access.path(path)
    extension = _office(storage_path, site)
    access.check(READ, storage_path, is_dir=False, password=password)
    entry = _stat(access, storage_path)
    secret = tokens.secret(site)
    can_edit = _may_edit(access, storage_path, extension, secret)
    session = sessions.open_session(
        access.storage, storage_path, user=access.actor.user, can_edit=can_edit,
        keys=access.unlocked_keys(storage_path) if can_edit else (), entry=entry)
    config = build(request, access, storage_path, session=session, can_edit=can_edit, viewer=access.actor.user,
                   secret=secret, display=display)
    return config, access


def for_share(request, share_key: str, path: str, *, display: Display = PLAIN) -> dict:
    """The config for a file inside a share (10.5): read only, the share's owner as the identity."""
    site = SiteSettings.load()
    _enabled(site)
    access, storage_path = shares.share_file(request, share_key, path)
    _office(storage_path, site)
    # Folder passwords: what the visitor unlocked in this share, as share_file() left it in the access.
    access.check(READ, storage_path, is_dir=False, keys=access.opened)
    entry = _stat(access, storage_path)
    session = sessions.open_session(access.storage, storage_path, user=access.actor.user, can_edit=False,
                                    entry=entry)
    return build(request, access, storage_path, session=session, can_edit=False,
                 viewer=getattr(request, "user", None), secret=tokens.secret(site), display=display)
