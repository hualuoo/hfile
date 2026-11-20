"""OneDrive and SharePoint through Microsoft Graph, on the global cloud and on 21Vianet's (china).
Types: onedrive and onedrive-china use the signed-in user's own drive (/me/drive); sharepoint and
sharepoint-china use a document library of a SharePoint site (/drives/<id>, or the site's default
library /sites/<id>/drive).

Paths
    The storage root "/" is `base_path` in the drive ("/" is the drive's root). A storage path /a/b
    is the drive path <base_path>/a/b, addressed as ".../root:/<base>/a/b:" with every segment
    percent-encoded (quote(segment, safe="")), so a name can't become a separator, a query or a
    Graph function on our side; segments no OneDrive item can have (with " * : < > ? \\ | or only
    dots and spaces) give NotFound before any request, so Graph never has to decide what they mean.
    Files are replaced by their item id, new ones created by path. OneDrive compares names without
    regard to case. Items shared from other drives (remoteItem) are left out: they aren't below the
    base path.

    list: children, $top=1000, following @odata.nextLink (only when it stays on the Graph host).

Login
    The admin registers an app in the Azure portal (21Vianet: portal.azure.cn) with the site's OAuth
    callback as a "Web" redirect URI and the delegated Graph permissions offline_access, User.Read,
    Files.ReadWrite.All, Sites.Read.All and Sites.ReadWrite.All, then enters its client id and
    secret; "授权" fills in the refresh token (refreshed tokens are saved through update_config).
    `tenant` is "common" for apps that accept any organisation or personal accounts, else the tenant
    id or domain.

Params
    SharePoint also needs the site (helper "sites" lists them; the site's URL works too) and
    optionally a document library (helper "drives"; empty: the site's default library).
    `download_domain` swaps the host of download links for a reverse proxy (a Cloudflare
    worker, ...).

Requests
    Graph throttles busy apps (429 with Retry-After): short waits are retried, longer ones give
    BackendError; so are download links'.

Changes
    mkdir with conflictBehavior fail; rename and move with PATCH, after checking that the target
    name is free (Graph documents no conflictBehavior for PATCH); copy is asynchronous in Graph: its
    monitor URL is polled for up to `copy_timeout` seconds (Graph finishes it later if that runs
    out); its default conflict behaviour is fail (OneDrive personal takes no other). delete moves
    the item to the drive's recycle bin.

Downloads
    open: the item's @microsoft.graph.downloadUrl (pre-authenticated, so fetched without our token)
    with a Range header. download_url gives that link; it lasts about an hour and OneDrive names the
    download after the file itself. Some accounts leave the link out of the item although $select
    names it: then it comes from GET .../items/<id>/content, which answers 302 to the same link
    (asked with our token and not followed, so the token stays on Graph). Should /content send the
    bytes itself, open() passes them on and download_url gives None (the site sends the file).

Uploads
    write: files up to `small_upload_limit` (4 MiB) in one PUT; larger ones through an upload
    session in chunks of `upload_chunk` (10 MiB, a multiple of 320 KiB) read one at a time, a chunk
    retried up to three times (after Retry-After when throttled). A session Graph lost (404) starts
    the upload over once, when the data can be read again. upload_target gives the browser such a
    session (kind "chunks"); Graph allows its upload URLs from any origin, so no CORS setup is
    needed. Empty files always go through write() (sessions can't take 0 bytes). Existing files are
    replaced, a folder in the way gives AlreadyExists; missing folders above are created.

Names
    Names can't contain " * : < > ? / \\ | (forbidden_chars), start or end with a space, start with
    ~$, contain _vti_, or be CON, PRN, AUX, NUL, COM0-COM9, LPT0-LPT9, .lock or desktop.ini.

Limits
    The whole path is limited to 400 characters and a file to 250 GB.

    search: Graph's search of the whole drive (names and contents, matching words, not any
    substring), kept to items whose names contain the keyword and that lie below the folder.
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.text import format_lazy
from django.utils.translation import gettext as _, gettext_lazy, ngettext, pgettext_lazy

from . import http, paths
from ._cloud import (
    TokenCache,
    as_int,
    json_or_none,
    origin,
    parse_time,
    position,
    range_header,
    read_exact,
    redirected,
    response_stream,
    retry_after,
    short,
    transport_error,
    usable_name,
)
from .base import (
    AlreadyExists,
    AuthError,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
    Unsupported,
    UploadTarget,
)

GLOBAL_GRAPH = "https://graph.microsoft.com"
GLOBAL_LOGIN = "https://login.microsoftonline.com"
CHINA_GRAPH = "https://microsoftgraph.chinacloudapi.cn"
CHINA_LOGIN = "https://login.partner.microsoftonline.cn"

# The delegated permissions asked for (offline_access gives the refresh token).
SCOPE = "offline_access User.Read Files.ReadWrite.All Sites.Read.All Sites.ReadWrite.All"

# Properties of a driveItem we read.
SELECT = "id,name,size,lastModifiedDateTime,eTag,file,folder,package,root,parentReference,remoteItem"
CONFLICT = "@microsoft.graph.conflictBehavior"

_TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9.-]{0,252}")
_ORIGIN_RE = re.compile(r"https?://[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?/?")
_HOST_RE = re.compile(r"[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?")
_SITE_ID_RE = re.compile(r"[A-Za-z0-9.-]+,[0-9A-Fa-f-]{36},[0-9A-Fa-f-]{36}")
_DRIVE_ID_RE = re.compile(r"[A-Za-z0-9!_.-]{1,256}")
_USER_RE = re.compile(r"[^\s/?#]{1,256}")
_RESERVED_NAMES = frozenset(
    ["con", "prn", "aux", "nul", ".lock", "desktop.ini"]
    + [f"com{i}" for i in range(10)] + [f"lpt{i}" for i in range(10)]
)


def _quote_path(path: str) -> str:
    """A drive path with each segment percent-encoded: "/a b/c%" -> "/a%20b/c%25"."""
    return "".join("/" + urllib.parse.quote(part, safe="") for part in paths.segments(path))


def _query(pairs: Mapping[str, str]) -> str:
    """A query string keeping OData's "$" and "@" names readable (httpx would encode them)."""
    return "&".join(f"{name}={urllib.parse.quote(value, safe=',:@')}" for name, value in pairs.items())


def _with_query(url: str, pairs: Mapping[str, str]) -> str:
    return url + ("&" if "?" in url else "?") + _query(pairs) if pairs else url


def _given_link(item: Mapping[str, Any]) -> str | None:
    """The item's @microsoft.graph.downloadUrl; None when Graph left it out."""
    url = item.get("@microsoft.graph.downloadUrl")
    return url if isinstance(url, str) and url else None


def _below(full: str, top: str) -> list[str] | None:
    """The segments of drive path `full` below `top`, comparing names as OneDrive does (without
    regard to case); None when `full` isn't below `top`."""
    full_parts, top_parts = paths.segments(full), paths.segments(top)
    if len(full_parts) <= len(top_parts):
        return None
    if any(a.casefold() != b.casefold() for a, b in zip(full_parts, top_parts)):
        return None
    return full_parts[len(top_parts):]


class _CommitUncertain(BackendError):
    """The answer to an upload's last chunk was lost and the session is gone: the file may be
    complete; the caller looks."""


class _SessionGone(BackendError):
    """Graph no longer knows the upload session (404) before its last chunk: the upload starts over."""


# Called while the driver classes are made (at import): lazy texts, made in each request's language.
def _params(*, portal: str, service: str, sharepoint: bool) -> tuple[Param, ...]:
    params = [
        Param(
            "client_id", gettext_lazy("Application ID (Client ID)"),
            help=format_lazy(gettext_lazy("Register an app in \"App registrations\" in the Azure portal ({portal}): choose \"Web\" for the redirect URI and enter this site's authorization callback URL; in \"API permissions\" add the Microsoft Graph delegated permissions offline_access, User.Read, Files.ReadWrite.All, Sites.Read.All and Sites.ReadWrite.All. Enter the \"Application (client) ID\" from the app's \"Overview\" page here."), portal=portal),
            placeholder="00000000-0000-0000-0000-000000000000",
        ),
        Param(
            "client_secret", gettext_lazy("Client secret (Client Secret)"), kind="password", secret=True,
            help=gettext_lazy("Create a client secret in the app's \"Certificates & secrets\" "
                              'and enter its "Value" (not the "Secret ID"). When it '
                              "expires, create a new one, enter it and authorize again."),
        ),
        Param(
            "tenant", gettext_lazy("Tenant"), required=False, default="common", advanced=True,
            help=(gettext_lazy("common when the app supports accounts of any organization or personal Microsoft "
                  "accounts; for a single-tenant app that only supports your organization's "
                  "accounts, the tenant ID or domain (like contoso.onmicrosoft.com).")),
        ),
        Param(
            "refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
            help=(gettext_lazy('After saving the settings above, click "Authorize", log in to the Microsoft '
                  "account and agree: it's filled in for you. A changed password, a revoked "
                  "authorization or a long time unused ends the token; just authorize again.")),
        ),
    ]
    if sharepoint:
        params += [
            Param(
                "site", gettext_lazy("Site"),
                help=(gettext_lazy("Choose one with \"List sites\", or enter the site's "
                      "address, like https://contoso.sharepoint.com/sites/team "
                      "(.sharepoint.cn for 21Vianet). Needs authorizing first.")),
            ),
            Param(
                "drive_id", gettext_lazy("Document library"), required=False,
                help=gettext_lazy("Choose one with \"List document libraries\"; leave empty for the site's default library (\"Documents\")."),
            ),
        ]
    params += [
        Param(
            "base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
            help=format_lazy(gettext_lazy("The folder in {service} that is this storage source's root, like / or /folder1. It must already exist."), service=service),
        ),
        Param(
            "download_domain", pgettext_lazy("OneDrive, SharePoint", "Download domain"), required=False, advanced=True,
            placeholder="https://dl.example.com",
            help=(gettext_lazy("Fill in when a Cloudflare Worker or your own reverse proxy passes on "
                  "Microsoft's download addresses: direct links replace the domain of Microsoft's "
                  "download address with it. Leave empty to use Microsoft's addresses.")),
        ),
    ]
    return tuple(params)


def _app_params(*, portal: str) -> tuple[Param, ...]:
    return (
        Param("client_id", gettext_lazy("Application ID (Client ID)"),
              help=format_lazy(gettext_lazy("Register an app in \"App registrations\" in the Azure portal ({portal}); in \"API permissions\" add the Microsoft Graph application permission Files.ReadWrite.All (not a delegated one) and click \"Grant admin consent\". Enter the \"Application (client) ID\" from the app's \"Overview\" page here. It can then reach every user's OneDrive in the organization, without anyone signing in."), portal=portal),
              placeholder="00000000-0000-0000-0000-000000000000"),
        Param("client_secret", gettext_lazy("Client secret (Client Secret)"), kind="password", secret=True,
              help=gettext_lazy("Create a client secret in the app's \"Certificates & secrets\" "
                                'and enter its "Value" (not the "Secret ID"). When it expires, create a new one and enter it.')),
        Param("tenant", gettext_lazy("Tenant"),
              help=gettext_lazy("The organization's tenant ID or domain, like contoso.onmicrosoft.com (an app's own access needs it).")),
        Param("user", gettext_lazy("User"),
              help=gettext_lazy("The user whose OneDrive to show: their email address (user principal name) or object ID.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=format_lazy(gettext_lazy("The folder in {service} that is this storage source's root, like / or /folder1. It must already exist."), service="OneDrive")),
        Param("download_domain", pgettext_lazy("OneDrive, SharePoint", "Download domain"), required=False, advanced=True,
              placeholder="https://dl.example.com",
              help=(gettext_lazy("Fill in when a Cloudflare Worker or your own reverse proxy passes on "
                    "Microsoft's download addresses: direct links replace the domain of Microsoft's "
                    "download address with it. Leave empty to use Microsoft's addresses."))),
    )


def _missing_token() -> str:
    raise AuthError(_('Click "Authorize" and log in to the Microsoft account before listing sites and document libraries.'))


def _site_lookup(site: str) -> str | None:
    """The Graph path that finds a site: "/sites/<id>" for an id (or "root"), "/sites/<host>:/<path>"
    for a site URL; None when the text is neither."""
    if site == "root" or _SITE_ID_RE.fullmatch(site):
        return f"/sites/{site}"
    if not site.startswith("https://"):
        return None
    try:
        split = urllib.parse.urlsplit(site)
        odd = bool(split.query or split.fragment or split.username or split.port)
    except ValueError:
        return None
    host = split.hostname or ""
    if odd or not _HOST_RE.fullmatch(host):
        return None
    try:
        relative = paths.clean(urllib.parse.unquote(split.path))
    except InvalidPath:
        return None
    if relative == paths.ROOT:
        return f"/sites/{host}"
    return f"/sites/{host}:{_quote_path(relative)}"


class _GraphDriver(StorageDriver):
    """What OneDrive and SharePoint share; subclasses set the cloud and the drive."""

    service: ClassVar[str] = "OneDrive"  # the name in messages

    can_range = True
    can_direct_download = True
    can_direct_upload = True
    can_move = True
    can_copy = True
    can_search = True
    has_folders = True
    forbidden_chars = '"*:<>?\\|'

    oauth = True
    # A SharePoint site is chosen after 授权 (helper "sites" needs the token): the wizard's 其他设置.
    setup_params = ("client_id", "client_secret", "tenant")
    auth_params = ("refresh_token",)

    # Endpoints and tuning; the tests point the endpoints at the fake server and make the numbers small.
    graph_root: ClassVar[str] = GLOBAL_GRAPH
    login_root: ClassVar[str] = GLOBAL_LOGIN
    #: Bytes up to which write() uses one PUT; above it an upload session.
    small_upload_limit: ClassVar[int] = 4 * 1024 * 1024
    #: Bytes per upload session request: a multiple of 320 KiB, at most 60 MiB.
    upload_chunk: ClassVar[int] = 32 * 320 * 1024
    #: Seconds copy() waits for Graph's asynchronous copy, and the first pause between polls.
    copy_timeout: ClassVar[float] = 300.0
    copy_poll: ClassVar[float] = 0.2
    #: Seconds before the first retry of a failed upload chunk (doubling each time).
    retry_delay: ClassVar[float] = 0.5
    throttle_wait: ClassVar[float] = 120.0  # seconds a throttled upload chunk waits at most before trying again
    #: check()'s message when even the drive's root can't be found.
    missing_drive: ClassVar[str] = (gettext_lazy("This Microsoft account has no usable OneDrive: a "
                                    "personal account must have opened OneDrive once, and "
                                    "a work or school account needs a OneDrive license."))

    def __init__(self, config: Mapping[str, Any], *, on_update=None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._http = http.client()
        self._tokens = TokenCache(self._refresh)
        self._drive_lock = threading.Lock()
        self._drive_path: str | None = None

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = error.message
        tenant = str(config.get("tenant") or "common").strip()
        if not _TENANT_RE.fullmatch(tenant):
            errors["tenant"] = _("The tenant should be common, organizations, a tenant ID or a domain.")
        domain = str(config.get("download_domain") or "").strip()
        if domain and not _ORIGIN_RE.fullmatch(domain):
            errors["download_domain"] = _("The download domain should look like https://dl.example.com (a port is fine, no path).")
        return errors

    @classmethod
    def _tenant(cls, config: Mapping[str, Any]) -> str:
        tenant = str(config.get("tenant") or "common").strip()
        if not _TENANT_RE.fullmatch(tenant):
            raise DriverError(_("The tenant is set wrong."))
        return tenant

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        client_id = str(config.get("client_id") or "").strip()
        if not client_id:
            raise DriverError(_("Fill in the application ID (Client ID) first."))
        query = urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "response_mode": "query",
            "scope": SCOPE,
            "state": state,
            "prompt": "select_account",
        }, quote_via=urllib.parse.quote)
        return f"{cls.login_root}/{cls._tenant(config)}/oauth2/v2.0/authorize?{query}"

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        with http.client() as client:
            answer = cls._token_request(client, config, {
                "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            })
        refresh_token = answer.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token:
            raise AuthError(_("Microsoft gave no refresh token: make sure the app's API permissions include offline_access, then authorize again."))
        return {"refresh_token": refresh_token}

    # -- login

    @classmethod
    def _token_request(cls, client: httpx.Client, config: Mapping[str, Any], fields: dict[str, str]) -> dict[str, Any]:
        """POST to the token endpoint; the answer has a usable access_token."""
        client_id = str(config.get("client_id") or "").strip()
        secret = str(config.get("client_secret") or "")
        if not client_id or not secret:
            raise AuthError(_("Fill in the application ID and client secret first."))
        form = {"client_id": client_id, "client_secret": secret, "scope": SCOPE, **fields}
        url = f"{cls.login_root}/{cls._tenant(config)}/oauth2/v2.0/token"
        try:
            response = client.post(url, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as error:
            raise transport_error(error, _("the Microsoft login service")) from error
        answer = json_or_none(response)
        if response.status_code >= 500 or response.status_code == 429:
            raise BackendError(_("The Microsoft login service is unavailable for now "
                                 "(HTTP %(status_code)s). Please try again later.") % {"status_code": response.status_code})
        if response.status_code != 200 or not isinstance(answer, dict):
            reason = ""
            if isinstance(answer, dict):
                # "AADSTS7000215: Invalid client secret provided. ...\r\nTrace ID: ..." -> first line
                description = str(answer.get("error_description") or "").splitlines()
                reason = short(description[0] if description else answer.get("error"), 160)
            raise AuthError(_("Microsoft refused the authorization: %(reason)s. Please check the "
                              "application ID, client secret and tenant, and authorize again if needed.") % {"reason": reason or 'HTTP ' + str(response.status_code)})
        if not isinstance(answer.get("access_token"), str) or not answer["access_token"]:
            raise BackendError(_("The Microsoft login service's answer has no access token."))
        return answer

    def _refresh(self) -> tuple[str, float]:
        refresh_token = str(self.config.get("refresh_token") or "").strip()
        if not refresh_token:
            raise AuthError(_("Not authorized yet: click \"Authorize\" in the storage source's settings and log in to the Microsoft account."))
        self.log.debug("refreshing the access token")
        answer = self._token_request(self._http, self.config, {
            "grant_type": "refresh_token", "refresh_token": refresh_token,
        })
        new_token = answer.get("refresh_token")
        if isinstance(new_token, str) and new_token and new_token != refresh_token:
            self.update_config({"refresh_token": new_token})
        return answer["access_token"], float(as_int(answer.get("expires_in")) or 3600)

    # -- requests

    def _url(self, api_path: str) -> str:
        return f"{self.graph_root}/v1.0{api_path}"

    def _request(self, method: str, url: str, *, json: Any = None, content: bytes | None = None,
                 headers: Mapping[str, str] | None = None, stream: bool = False,
                 redirect_ok: bool = False) -> httpx.Response:
        """A Graph call with our token; once more after a 401 (new token) or a short 429/503.
        Redirects aren't followed, and are errors unless `redirect_ok` (/content's). stream=True: the
        body of an answer below 400 is left unread (the caller closes it); an error's is read (http.read_error()),
        for the message."""
        if origin(url) != origin(self.graph_root):
            raise BackendError(_("%(service)s gave an address that isn't on Graph.") % {"service": self.service})
        for attempt in range(3):
            token = self._tokens.get()
            all_headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
            all_headers.update(headers or {})
            try:
                if stream:
                    response = self._http.send(self._http.build_request(method, url, json=json, content=content,
                                                                        headers=all_headers),
                                               stream=True, follow_redirects=False)
                    if response.status_code >= 400:
                        response = http.read_error(response)
                else:
                    response = self._http.request(method, url, json=json, content=content, headers=all_headers)
            except httpx.HTTPError as error:
                raise transport_error(error, self.service) from error
            if response.status_code == 401 and attempt == 0:
                self._tokens.invalidate(token)
                continue
            if response.status_code in (429, 503) and attempt < 2:
                wait = retry_after(response)
                if wait is not None:
                    self.log.info("%s throttled, waiting %.1f s", self.service, wait)
                    time.sleep(wait)
                    continue
            if response.status_code >= 400:
                raise self._error(response)
            if response.status_code >= 300 and not redirect_ok:
                raise redirected(response, self.service)
            return response
        raise self._error(response)

    def _json(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        response = self._request(method, url, **kwargs)
        if response.status_code == 204 or not response.content:
            return {}
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": self.service})
        return answer

    @classmethod
    def _error(cls, response: httpx.Response) -> DriverError:
        answer = json_or_none(response)
        error = answer.get("error") if isinstance(answer, dict) else None
        code = message = ""
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            message = short(error.get("message"))
        status = response.status_code
        detail = _(" (%(code)s)") % {"code": code} if code else _(" (HTTP %(status)s)") % {"status": status}
        if status == 404 or code == "itemNotFound":
            return NotFound()
        if status == 409 or code == "nameAlreadyExists":
            return AlreadyExists()
        if status == 401:
            return AuthError(_("%(service)s doesn't accept the current authorization%(detail)s. "
                               "Please authorize again in the storage source's settings.") % {"service": cls.service, "detail": detail})
        if status == 507 or code in ("quotaLimitReached", "insufficientStorage"):
            return DriverError(_("%(service)s is out of space.") % {"service": cls.service})
        if status == 429 or code == "activityLimitReached":
            return BackendError(_("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": cls.service})
        if status == 423 or code == "resourceLocked":
            return DriverError(_("The file is locked (it may be open for editing in Office)%(detail)s. Please try again later.") % {"detail": detail})
        if status >= 500:
            return BackendError(_("%(service)s failed for now%(detail)s. Please try again later.") % {"service": cls.service, "detail": detail})
        if status == 403:
            return DriverError(_("%(service)s refused this%(detail)s: %(message)s") % {"service": cls.service, "detail": detail, "message": message or _("Permission denied.")})
        if message:
            return DriverError(_("%(service)s refused the request%(detail)s: %(message)s") % {"service": cls.service, "detail": detail, "message": message})
        return DriverError(_("%(service)s refused the request%(detail)s.") % {"service": cls.service, "detail": detail})

    # -- where things are

    def _drive(self) -> str:
        """The API path of the drive, e.g. "/me/drive"."""
        return "/me/drive"

    def _item(self, path: str, suffix: str = "") -> str:
        """The API path of a storage path's item, with a sub-resource like "/children"."""
        full = paths.rebase(path, self._base)
        if full == paths.ROOT:
            return f"{self._drive()}/root{suffix}"
        for part in paths.segments(full):
            # No OneDrive item has such a name, and how Graph would read one ("\" as "/", a
            # decoded ":" ending the path, "..." or ". ." trimmed to "..") isn't ours to test.
            if part.strip(". ") == "" or any(ch in self.forbidden_chars for ch in part):
                raise NotFound()
        return f"{self._drive()}/root:{_quote_path(full)}" + (f":{suffix}" if suffix else "")

    def _item_by_id(self, item_id: Any, suffix: str = "") -> str:
        if not isinstance(item_id, str) or not item_id:
            raise BackendError(_("%(service)s gave no ID for the file.") % {"service": self.service})
        return f"{self._drive()}/items/{urllib.parse.quote(item_id, safe='!')}{suffix}"

    def _get_item(self, path: str, select: str = SELECT) -> dict[str, Any]:
        return self._json("GET", _with_query(self._url(self._item(path)), {"$select": select}))

    def _existing(self, path: str, select: str = SELECT) -> dict[str, Any] | None:
        try:
            return self._get_item(path, select)
        except NotFound:
            return None

    def _entry(self, item: Mapping[str, Any], path: str) -> Entry:
        is_dir = "file" not in item
        name = item.get("name")
        if path == paths.ROOT:
            name = ""
        elif not usable_name(name):
            name = paths.name(path)
        return Entry(
            name=name,
            path=path,
            is_dir=is_dir,
            size=None if is_dir else as_int(item.get("size")),
            modified=parse_time(item.get("lastModifiedDateTime")),
        )

    def _check_new_name(self, name: str) -> None:
        """Graph's rules for names on top of forbidden_chars (only for names we create)."""
        paths.check_name(name, forbidden=self.forbidden_chars, max_bytes=self.max_name_bytes)
        if name != name.strip(" "):
            raise InvalidPath(_("Names in %(service)s can't start or end with a space.") % {"service": self.service})
        if name.lower() in _RESERVED_NAMES:
            raise InvalidPath(_("%(service)s doesn't allow %(name)s as a name.") % {"service": self.service, "name": name})
        if name.startswith("~$"):
            raise InvalidPath(_("Names in %(service)s can't start with ~$.") % {"service": self.service})
        if "_vti_" in name.lower():
            raise InvalidPath(_("Names in %(service)s can't contain _vti_.") % {"service": self.service})

    # -- files

    def check(self) -> None:
        try:
            root = self.stat(paths.ROOT)
        except NotFound:
            if self._base == paths.ROOT:
                raise NotFound(self.missing_drive) from None
            raise NotFound(_("The base path %(base)s doesn't exist in %(service)s.") % {"base": self._base, "service": self.service}) from None
        if not root.is_dir:
            raise NotADirectory(_("The base path %(base)s is a file, not a folder.") % {"base": self._base})
        self.list(paths.ROOT)

    def list(self, path: str) -> list[Entry]:
        url: str | None = _with_query(self._url(self._item(path, "/children")),
                                      {"$top": "1000", "$select": SELECT})
        entries: list[Entry] = []
        listed_any = False
        seen: set[str] = set()
        try:
            while url:
                if url in seen:
                    raise BackendError(_("%(service)s repeated a page address.") % {"service": self.service})
                seen.add(url)
                answer = self._json("GET", url)
                for item in answer.get("value") or []:
                    listed_any = True
                    name = item.get("name") if isinstance(item, dict) else None
                    if not usable_name(name):
                        self.log.debug("leaving out an item with an unusable name")
                        continue
                    if "remoteItem" in item:
                        # Shared with this account from another drive: not below the base path,
                        # and not reachable through it by path.
                        self.log.debug("leaving out an item of another drive")
                        continue
                    entries.append(self._entry(item, paths.join(path, name)))
                next_link = answer.get("@odata.nextLink")
                url = next_link if isinstance(next_link, str) and next_link else None
        except (NotFound, DriverError) as error:
            if isinstance(error, (AuthError, BackendError)):
                raise
            # Children of a file: Graph may answer 4xx; tell the two apart.
            item = self._existing(path, "id,file,folder")
            if item is not None and "file" in item:
                raise NotADirectory() from None
            raise
        if not listed_any:
            # An empty answer is also what a file's children look like.
            if "file" in self._get_item(path, "id,file,folder"):
                raise NotADirectory()
        return entries

    def stat(self, path: str) -> Entry:
        return self._entry(self._get_item(path), path)

    def _content(self, item: Mapping[str, Any], headers: Mapping[str, str]) -> httpx.Response:
        """GET the item's /content, for an item without @microsoft.graph.downloadUrl: a streamed answer
        that redirects to the item's pre-authenticated link (_location()) or carries the bytes."""
        self.log.debug("no download link in the item, asking /content")
        response = self._request("GET", self._url(self._item_by_id(item.get("id"), "/content")),
                                 headers={"Accept": "*/*", **headers}, stream=True, redirect_ok=True)
        if response.status_code in (200, 206) or response.has_redirect_location:
            return response
        http.drain(response)
        raise BackendError(_("Downloading from %(service)s failed (HTTP %(status_code)s).") % {"service": self.service, "status_code": response.status_code})

    def _location(self, response: httpx.Response) -> str:
        """The link a /content answer redirects to, made absolute; closes the answer."""
        try:
            target = response.url.join(response.headers["location"])
        except (KeyError, TypeError, ValueError, httpx.InvalidURL):
            target = None
        finally:
            http.drain(response)
        if target is None or origin(target) == ("", "", 0):
            raise BackendError(_("The download address from %(service)s is wrong.") % {"service": self.service})
        return str(target)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._get_item(path, SELECT + ",@microsoft.graph.downloadUrl")
        if "file" not in item:
            raise NotFound()
        total = as_int(item.get("size"))
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total) if total else None
        if span:
            headers["Range"] = span
        url = _given_link(item)
        response = None
        if url is None:
            # Not with the Range: Graph wants it on the link /content redirects to, "not to the request for /content"
            # (driveitem-get-content). A 200 with the whole file is cut down by response_stream().
            response = self._content(item, {name: value for name, value in headers.items() if name != "Range"})
            if response.has_redirect_location:
                url, response = self._location(response), None
        if response is None:
            for attempt in range(2):
                try:
                    # The link carries its own authorisation: no token, and redirects are safe to follow.
                    response = self._http.send(self._http.build_request("GET", url, headers=headers),
                                               stream=True, follow_redirects=True)
                except httpx.HTTPError as error:
                    raise transport_error(error, self.service) from error
                # Throttled (429, 503): a short Retry-After is waited out once, as Graph's own calls do.
                wait = retry_after(response) if response.status_code in (429, 503) and not attempt else None
                if wait is None:
                    break
                http.drain(response)
                self.log.info("%s throttled a download, waiting %.1f s", self.service, wait)
                time.sleep(wait)
        if response.status_code == 404:
            http.drain(response)
            raise NotFound()
        return response_stream(response, offset=offset, length=length, total=total,
                               service=self.service, etag=item.get("eTag"),
                               modified=parse_time(item.get("lastModifiedDateTime")))

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        item = self._get_item(path, "id,file,folder,@microsoft.graph.downloadUrl")
        if "file" not in item:
            raise NotFound()
        url = _given_link(item)
        if url is None:
            response = self._content(item, {"Accept-Encoding": "identity"})
            if not response.has_redirect_location:
                response.close()  # /content sends the bytes itself: the site passes them on (open())
                return None
            url = self._location(response)
        domain = str(self.config.get("download_domain") or "").strip()
        if domain:
            proxy = httpx.URL(domain)
            url = str(httpx.URL(url).copy_with(scheme=proxy.scheme, host=proxy.host, port=proxy.port))
        return url

    def _create_folder(self, parent: str, name: str) -> None:
        self._json("POST", self._url(self._item(parent, "/children")),
                   json={"name": name, "folder": {}, CONFLICT: "fail"})

    def _make_dirs(self, folder: str) -> None:
        """Create `folder` and the folders above it that are missing."""
        if folder == paths.ROOT:
            return
        parent, name = paths.split(folder)
        try:
            self._create_folder(parent, name)
        except NotFound:
            self._make_dirs(parent)
            self._create_folder(parent, name)
        except AlreadyExists:
            if not self.stat(folder).is_dir:
                raise NotADirectory(_("%(folder)s is a file, not a folder.") % {"folder": folder}) from None

    def _upload_session(self, path: str, existing: Mapping[str, Any] | None) -> str:
        """An upload session for a new file at `path` (failing if the name is taken meanwhile),
        or for new content of the existing file (by its id)."""
        if existing is not None:
            answer = self._json("POST", self._url(self._item_by_id(existing.get("id"), "/createUploadSession")),
                                json={})
        else:
            url = self._url(self._item(path, "/createUploadSession"))
            body = {"item": {CONFLICT: "fail"}}

            def start() -> dict[str, Any]:
                try:
                    return self._json("POST", url, json=body)
                except AlreadyExists:
                    # Nothing had the name a moment ago (the caller looked). On OneDrive for Business and SharePoint an
                    # upload session holds the name from its start until it is finished, deleted or expired, so this
                    # is most likely an earlier upload of the same file that never finished.
                    raise DriverError(_("%(service)s already has a file of that name, or an unfinished upload of it. Please try again later.") % {"service": self.service}) from None

            try:
                answer = start()
            except NotFound:
                self._make_dirs(paths.parent(path))
                answer = start()
        upload_url = answer.get("uploadUrl")
        if not isinstance(upload_url, str) or not upload_url.startswith(("https://", "http://")):
            raise BackendError(_("%(service)s gave no upload address.") % {"service": self.service})
        return upload_url

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        if size <= 0 or path == paths.ROOT:
            return None
        self._check_new_name(paths.name(path))
        existing = self._existing(path, "id,file,folder")
        if existing is not None and "file" not in existing:
            raise AlreadyExists()
        url = self._upload_session(path, existing)
        return UploadTarget(kind="chunks", url=url, method="PUT", chunk_size=self.upload_chunk)

    def _cancel_session(self, upload_url: str) -> None:
        try:
            self._http.delete(upload_url)
        except httpx.HTTPError:
            pass

    def _session_ranges(self, upload_url: str) -> list[Any] | None:
        """The byte ranges an upload session still waits for."""
        try:
            response = self._http.get(upload_url)
        except httpx.HTTPError:
            return None
        answer = json_or_none(response) if response.status_code == 200 else None
        ranges = answer.get("nextExpectedRanges") if isinstance(answer, dict) else None
        return ranges if isinstance(ranges, list) else None

    def _put_chunk(self, upload_url: str, chunk: bytes, start: int, end: int,
                   size: int) -> tuple[bool, list[Any] | None]:
        """PUT one chunk: (finished, Graph's nextExpectedRanges), retrying 5xx and lost answers."""
        # No Authorization header here: the URL is the credential, and Graph refuses a token on it.
        headers = {"Content-Range": f"bytes {start}-{end}/{size}"}
        lost = False  # an earlier try may have reached Graph
        for attempt in range(4):
            try:
                response = self._http.put(upload_url, content=chunk, headers=headers)
            except httpx.HTTPError as error:
                lost = True
                if attempt == 3:
                    raise transport_error(error, self.service) from error
            else:
                if response.status_code in (200, 201):
                    return True, None
                if response.status_code == 202:
                    answer = json_or_none(response)
                    return False, answer.get("nextExpectedRanges") if isinstance(answer, dict) else None
                if response.status_code == 416 and end + 1 < size:
                    # Graph has this chunk already: an earlier try got through but its answer was lost.
                    expected = self._session_ranges(upload_url)
                    if expected and str(expected[0]).startswith(f"{end + 1}-"):
                        return False, expected
                if response.status_code < 500 and response.status_code != 429:
                    if response.status_code in (404, 409, 410, 416) and lost and end + 1 == size:
                        raise _CommitUncertain(_("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.") % {"service": self.service})
                    if response.status_code == 404:
                        raise _SessionGone(_("The %(service)s upload session has expired. Please upload again.") % {"service": self.service})
                    raise self._error(response)
                if attempt == 3:
                    raise self._error(response)
                if response.status_code in (429, 503):
                    # Throttled: Microsoft requires waiting as long as Retry-After says, and more throttling follows
                    # for those who don't. An upload has its thread to itself, so it can wait that long.
                    wait = retry_after(response, default=self.retry_delay * 2 ** attempt, longest=self.throttle_wait)
                    if wait is None:
                        raise self._error(response)
                    time.sleep(wait)
                    continue
            time.sleep(self.retry_delay * 2 ** attempt)
        raise BackendError(_("Uploading to %(service)s failed.") % {"service": self.service})  # not reached

    def _send_chunks(self, upload_url: str, data: BinaryIO, size: int) -> None:
        """PUT the data to an upload session, one chunk in memory at a time."""
        offset = 0
        try:
            while offset < size:
                want = min(self.upload_chunk, size - offset)
                chunk = read_exact(data, want)
                if len(chunk) != want:
                    raise DriverError(_("The uploaded data is incomplete: less than the size given."))
                end = offset + len(chunk) - 1
                finished, expected = self._put_chunk(upload_url, chunk, offset, end, size)
                if finished:
                    if end + 1 != size:
                        raise BackendError(_("%(service)s ended the upload early.") % {"service": self.service})
                    return
                if expected and not str(expected[0]).startswith(f"{end + 1}-"):
                    raise BackendError(_("The upload range %(service)s wants doesn't match (%(range)s).") % {"service": self.service, "range": short(expected[0], 40)})
                offset = end + 1
            raise BackendError(_("%(service)s didn't confirm the upload finished.") % {"service": self.service})
        except BaseException:
            self._cancel_session(upload_url)
            raise

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        existing = self._existing(path, "id,file,folder,eTag")
        if existing is not None and "file" not in existing:
            raise AlreadyExists()
        if size <= self.small_upload_limit:
            body = read_exact(data, size)
            if len(body) != size:
                raise DriverError(_("The uploaded data is incomplete: less than the size given."))
            headers = {"Content-Type": "application/octet-stream"}
            if existing is not None:  # new content for that file, addressed by its id
                self._request("PUT", self._url(self._item_by_id(existing.get("id"), "/content")),
                              content=body, headers=headers)
                return
            url = self._url(self._item(path, "/content"))
            try:
                self._request("PUT", url, content=body, headers=headers)
            except NotFound:
                self._make_dirs(paths.parent(path))
                self._request("PUT", url, content=body, headers=headers)
            return
        begin = position(data)
        for attempt in range(2):
            upload_url = self._upload_session(path, existing)
            try:
                self._send_chunks(upload_url, data, size)
                return
            except _SessionGone:
                # Graph lost the session (SharePoint does, even a second after making it): "start the entire upload
                # over" (createUploadSession's best practices), once, when the data can be read again.
                if attempt or begin is None:
                    raise
                self.log.info("%s lost an upload session; starting the upload over", self.service)
                data.seek(begin)
            except _CommitUncertain:
                # Done if the file is there now, new or changed, with all the bytes.
                item = self._existing(path, "id,file,size,eTag")
                if (item is None or "file" not in item or as_int(item.get("size")) != size
                        or (existing is not None and item.get("eTag") == existing.get("eTag"))):
                    raise
                return

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent, name = paths.split(path)
        self._check_new_name(name)
        try:
            self._create_folder(parent, name)
        except NotFound:
            self._make_dirs(parent)
            self._create_folder(parent, name)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be deleted."))
        self._request("DELETE", self._url(self._item(path)))

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be renamed."))
        self._check_new_name(new_name)
        item = self._get_item(path, "id")
        if new_name != paths.name(path):
            other = self._existing(paths.join(paths.parent(path), new_name), "id")
            if other is not None and other.get("id") != item.get("id"):
                raise AlreadyExists()
        self._request("PATCH", self._url(self._item(path)), json={"name": new_name})

    def _destination(self, path: str, folder: str, copy: bool) -> tuple[dict[str, Any], dict[str, Any]]:
        """The source item and the target folder of a move or copy, checked."""
        if path == paths.ROOT:
            raise DriverError(_("The storage source's root folder can't be copied.") if copy else _("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(_("A folder can't be copied into itself.") if copy else _("A folder can't be moved into itself."))
        source = self._get_item(path, "id,file,folder")
        target = self._get_item(folder, "id,file,folder,root,parentReference")
        if "file" in target:
            raise NotADirectory()
        if not isinstance(target.get("id"), str) or not target["id"]:
            raise BackendError(_("%(service)s gave no ID for the target folder.") % {"service": self.service})
        return source, target

    def move(self, path: str, folder: str) -> None:
        source, target = self._destination(path, folder, copy=False)
        if paths.parent(path) == folder:
            return
        other = self._existing(paths.join(folder, paths.name(path)), "id")
        if other is not None:
            raise AlreadyExists()
        self._request("PATCH", self._url(self._item(path)), json={"parentReference": {"id": target["id"]}})

    def _wait_for_copy(self, monitor: str) -> None:
        """Poll the monitor of an asynchronous copy (it needs no token) until it is done."""
        deadline = time.monotonic() + self.copy_timeout
        delay = self.copy_poll
        while True:
            try:
                response = self._http.get(monitor, headers={"Accept": "application/json"})
            except httpx.HTTPError as error:
                response = None
                self.log.debug("copy monitor unreachable: %s", type(error).__name__)
            if response is not None:
                if response.status_code == 303:
                    return
                answer = json_or_none(response)
                if not isinstance(answer, dict):
                    answer = {}
                status = answer.get("status")
                if response.status_code in (200, 201, 202) and status == "completed":
                    return
                if response.status_code in (200, 201) and status is None and "id" in answer:
                    return  # some monitors answer with the new item itself
                if status == "failed":
                    error = answer.get("error")
                    code = str(error.get("code") or "") if isinstance(error, dict) else ""
                    if code == "nameAlreadyExists":
                        raise AlreadyExists()
                    message = short(error.get("message")) if isinstance(error, dict) else ""
                    if message:
                        raise DriverError(_("Copying in %(service)s failed (%(code)s): %(message)s") % {"service": self.service, "code": code or "failed", "message": message})
                    raise DriverError(_("Copying in %(service)s failed (%(code)s).") % {"service": self.service, "code": code or "failed"})
                if response.status_code == 404:
                    raise BackendError(_("The %(service)s copy job is gone. Please refresh to see whether the copy is there.") % {"service": self.service})
                if 400 <= response.status_code < 500 and response.status_code != 429:
                    raise self._error(response)
            if time.monotonic() + delay > deadline:
                raise BackendError(ngettext("%(service)s is still copying (after waiting %(seconds)s second); it goes on in the background, so refresh later to see it.", "%(service)s is still copying (after waiting %(seconds)s seconds); it goes on in the background, so refresh later to see it.", int(self.copy_timeout)) % {"service": self.service, "seconds": int(self.copy_timeout)})
            time.sleep(delay)
            delay = min(delay * 2, 3.0)

    def copy(self, path: str, folder: str) -> None:
        source, target = self._destination(path, folder, copy=True)
        name = paths.name(path)
        if self._existing(paths.join(folder, name), "id") is not None:
            raise AlreadyExists()
        reference = {"id": target["id"]}
        drive_id = (target.get("parentReference") or {}).get("driveId")
        if isinstance(drive_id, str) and drive_id:
            reference["driveId"] = drive_id
        response = self._request("POST", self._url(self._item(path, "/copy")),
                                 json={"parentReference": reference, "name": name})
        monitor = response.headers.get("location")
        if response.status_code == 202 and monitor:
            if not origin(monitor)[0]:
                raise BackendError(_("The copy progress address from %(service)s is wrong.") % {"service": self.service})
            self._wait_for_copy(monitor)
        elif response.status_code not in (200, 201):
            raise BackendError(_("%(service)s didn't start copying (HTTP %(status_code)s).") % {"service": self.service, "status_code": response.status_code})

    def _parent_path(self, item: Mapping[str, Any], known: dict[str, str | None]) -> str | None:
        """The drive path of an item's folder: from parentReference.path, else by walking up."""
        reference = item.get("parentReference")
        if not isinstance(reference, dict) or not (reference.get("path") or reference.get("id")):
            # Search results may come without it: ask for the item itself.
            item_id = item.get("id")
            if not isinstance(item_id, str) or not item_id or item.get("_fetched"):
                return None
            try:
                fetched = self._json("GET", _with_query(
                    self._url(f"{self._drive()}/items/{urllib.parse.quote(item_id, safe='!')}"),
                    {"$select": "id,name,parentReference"}))
            except NotFound:
                return None
            return self._parent_path({**fetched, "_fetched": True}, known)
        ref_path = reference.get("path")
        if isinstance(ref_path, str) and "root:" in ref_path:
            rest = urllib.parse.unquote(ref_path.split("root:", 1)[1])
            try:
                return paths.clean(rest) if rest else paths.ROOT
            except InvalidPath:
                return None
        parent_id = reference.get("id")
        if not isinstance(parent_id, str) or not parent_id:
            return None
        if parent_id in known:
            return known[parent_id]
        known[parent_id] = None  # a cycle or a failure leaves it unknown
        try:
            parent = self._json("GET", _with_query(
                self._url(f"{self._drive()}/items/{urllib.parse.quote(parent_id, safe='!')}"),
                {"$select": "id,name,root,parentReference"}))
        except NotFound:
            return None
        if "root" in parent:
            result: str | None = paths.ROOT
        else:
            above = self._parent_path(parent, known)
            name = parent.get("name")
            result = paths.join(above, name) if above is not None and usable_name(name) else None
        known[parent_id] = result
        return result

    def search(self, folder: str, keyword: str, limit: int) -> list[Entry]:
        if not keyword or limit <= 0:
            return []
        scope = paths.rebase(folder, self._base)
        needle = keyword.casefold()
        text = urllib.parse.quote(keyword.replace("'", "''"), safe="")
        url: str | None = _with_query(self._url(f"{self._drive()}/root/search(q='{text}')"),
                                      {"$top": "200", "$select": SELECT})
        parents: dict[str, str | None] = {}
        results: list[Entry] = []
        pages = 0
        while url and len(results) < limit and pages < 20:
            answer = self._json("GET", url)
            pages += 1
            for item in answer.get("value") or []:
                name = item.get("name") if isinstance(item, dict) else None
                if not usable_name(name) or needle not in name.casefold() or "remoteItem" in item:
                    continue
                parent = self._parent_path(item, parents)
                if parent is None:
                    continue
                rest = _below(paths.join(parent, name), scope)
                if rest is None:  # outside the folder (or the base path)
                    continue
                results.append(self._entry(item, "/" + "/".join(paths.segments(folder) + rest)))
                if len(results) >= limit:
                    break
            next_link = answer.get("@odata.nextLink")
            url = next_link if isinstance(next_link, str) and next_link else None
        return results


# --- OneDrive ----------------------------------------------------------------------------------------

class OneDriveDriver(_GraphDriver):
    type = "onedrive"
    label = "OneDrive"
    params = _params(portal="https://portal.azure.com", service="OneDrive", sharepoint=False)


class OneDriveChinaDriver(_GraphDriver):
    type = "onedrive-china"
    label = gettext_lazy("OneDrive (21Vianet)")
    params = _params(portal="https://portal.azure.cn", service="OneDrive", sharepoint=False)
    graph_root = CHINA_GRAPH
    login_root = CHINA_LOGIN


# --- OneDrive with the app's own access (no one signs in) ---------------------------------------------

class OneDriveAppDriver(_GraphDriver):
    """A user's OneDrive through the app's own access (OAuth 2.0 client credentials): the admin gives the app
    the application permission Files.ReadWrite.All with the organization's consent, and no one signs in; the
    token is asked for again whenever it runs out. Works for work and school accounts only (Microsoft 365)."""

    type = "onedrive-app"
    label = gettext_lazy("OneDrive (app access)")
    params = _app_params(portal="https://portal.azure.com")
    oauth = False
    setup_params = ("client_id", "client_secret", "tenant", "user")
    auth_params = ()
    missing_drive = gettext_lazy("This user's OneDrive wasn't found: check the user, whether the user has a "
                                 "OneDrive (it is made when they first open it), and whether the app's "
                                 "Files.ReadWrite.All application permission has admin consent.")

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = super().validate_config(config)
        tenant = str(config.get("tenant") or "").strip()
        if tenant.lower() in ("", "common", "organizations", "consumers"):
            errors["tenant"] = _("An app's own access needs the organization's tenant ID or domain, not common.")
        if not _USER_RE.fullmatch(str(config.get("user") or "").strip()):
            errors["user"] = _("The user is their email address (user principal name) or object ID.")
        return errors

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        raise Unsupported()

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        raise Unsupported()

    def _refresh(self) -> tuple[str, float]:
        answer = self._token_request(self._http, self.config, {
            "grant_type": "client_credentials", "scope": f"{self.graph_root}/.default",
        })
        return answer["access_token"], float(as_int(answer.get("expires_in")) or 3600)

    def _drive(self) -> str:
        user = str(self.config.get("user") or "").strip()
        if not _USER_RE.fullmatch(user):
            raise DriverError(_("The user is their email address (user principal name) or object ID."))
        return f"/users/{urllib.parse.quote(user, safe='@')}/drive"


class OneDriveAppChinaDriver(OneDriveAppDriver):
    type = "onedrive-app-china"
    label = gettext_lazy("OneDrive (app access, 21Vianet)")
    params = _app_params(portal="https://portal.azure.cn")
    graph_root = CHINA_GRAPH
    login_root = CHINA_LOGIN


# --- SharePoint --------------------------------------------------------------------------------------

class SharePointDriver(_GraphDriver):
    type = "sharepoint"
    label = "SharePoint"
    service = "SharePoint"
    params = _params(portal="https://portal.azure.com", service="SharePoint", sharepoint=True)
    helpers = {"sites": gettext_lazy("List sites"), "drives": gettext_lazy("List document libraries")}
    missing_drive = gettext_lazy("The site's document library wasn't found: please check \"Site\" "
                                 'and "Document library", and whether the account has access.')

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = super().validate_config(config)
        site = str(config.get("site") or "").strip()
        if site and _site_lookup(site) is None:
            errors["site"] = _("The site should be an ID chosen in \"List sites\", or the site's address, like https://contoso.sharepoint.com/sites/team.")
        drive_id = str(config.get("drive_id") or "").strip()
        if drive_id and not _DRIVE_ID_RE.fullmatch(drive_id):
            errors["drive_id"] = _('The document library ID is wrong. Please choose again with "List document libraries".')
        return errors

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if name not in cls.helpers:
            raise Unsupported()
        with http.client() as client:
            token = cls._token_request(client, config, {
                "grant_type": "refresh_token",
                "refresh_token": str(config.get("refresh_token") or "").strip() or _missing_token(),
            })["access_token"]
            if name == "sites":
                return {"param": "site", "choices": cls._site_choices(client, token)}
            site = str(config.get("site") or "").strip()
            lookup = _site_lookup(site)
            if lookup is None:
                return {"message": _("Choose the site (or enter its address) before listing document libraries.")}
            site = cls._get(client, token, _with_query(f"{cls.graph_root}/v1.0{lookup}", {"$select": "id"}))
            site_id = site.get("id")
            if not isinstance(site_id, str) or not _SITE_ID_RE.fullmatch(site_id):
                raise BackendError(_("SharePoint gave no ID for the site."))
            url = _with_query(f"{cls.graph_root}/v1.0/sites/{urllib.parse.quote(site_id, safe=',')}/drives",
                              {"$select": "id,name,driveType,webUrl"})
            choices = [{"value": "", "label": _("The site's default library")}]
            for drive in cls._get_all(client, token, url):
                drive_id, label = drive.get("id"), drive.get("name")
                if isinstance(drive_id, str) and _DRIVE_ID_RE.fullmatch(drive_id):
                    choices.append({"value": drive_id, "label": str(label or drive_id)})
            return {"param": "drive_id", "choices": choices}

    @classmethod
    def _site_choices(cls, client: httpx.Client, token: str) -> list[dict[str, str]]:
        select = {"$select": "id,displayName,webUrl"}
        sites: list[dict[str, Any]] = []
        try:
            sites.append(cls._get(client, token, _with_query(f"{cls.graph_root}/v1.0/sites/root", select)))
        except NotFound:
            pass
        sites += cls._get_all(client, token, _with_query(f"{cls.graph_root}/v1.0/sites", {"search": "*", **select}))
        choices, seen = [], set()
        for site in sites:
            site_id = site.get("id")
            if not isinstance(site_id, str) or not _SITE_ID_RE.fullmatch(site_id) or site_id in seen:
                continue
            seen.add(site_id)
            title = str(site.get("displayName") or site_id)
            web_url = site.get("webUrl")
            choices.append({"value": site_id, "label": _("%(title)s (%(web_url)s)") % {"title": title, "web_url": web_url} if web_url else title})
        return choices

    @classmethod
    def _get(cls, client: httpx.Client, token: str, url: str) -> dict[str, Any]:
        try:
            response = client.get(url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})
        except httpx.HTTPError as error:
            raise transport_error(error, cls.service) from error
        if response.status_code >= 400:
            raise cls._error(response)
        if response.status_code >= 300:
            raise redirected(response, cls.service)
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            raise BackendError(_("%(service)s returned content that couldn't be understood.") % {"service": cls.service})
        return answer

    @classmethod
    def _get_all(cls, client: httpx.Client, token: str, url: str, pages: int = 10) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        next_url: str | None = url
        while next_url and pages > 0:
            if origin(next_url) != origin(cls.graph_root):
                break
            answer = cls._get(client, token, next_url)
            values += [value for value in answer.get("value") or [] if isinstance(value, dict)]
            link = answer.get("@odata.nextLink")
            next_url = link if isinstance(link, str) and link else None
            pages -= 1
        return values

    def _drive(self) -> str:
        drive_id = str(self.config.get("drive_id") or "").strip()
        if drive_id:
            if not _DRIVE_ID_RE.fullmatch(drive_id):
                raise DriverError(_('The document library ID is wrong. Please choose again with "List document libraries".'))
            return f"/drives/{urllib.parse.quote(drive_id, safe='!')}"
        with self._drive_lock:
            if self._drive_path is None:
                self._drive_path = f"/sites/{urllib.parse.quote(self._site_id(), safe=',')}/drive"
            return self._drive_path

    def _site_id(self) -> str:
        site = str(self.config.get("site") or "").strip()
        lookup = _site_lookup(site)
        if lookup is None:
            raise DriverError(_("The site isn't set, or is set wrong."))
        if lookup.startswith("/sites/") and "," in lookup:
            return lookup[len("/sites/"):]
        answer = self._json("GET", _with_query(self._url(lookup), {"$select": "id"}))
        site_id = answer.get("id")
        if not isinstance(site_id, str) or not _SITE_ID_RE.fullmatch(site_id):
            raise BackendError(_("SharePoint gave no ID for the site."))
        return site_id


class SharePointChinaDriver(SharePointDriver):
    type = "sharepoint-china"
    label = gettext_lazy("SharePoint (21Vianet)")
    params = _params(portal="https://portal.azure.cn", service="SharePoint", sharepoint=True)
    graph_root = CHINA_GRAPH
    login_root = CHINA_LOGIN
