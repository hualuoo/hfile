"""OneDrive share links: a folder of OneDrive for work or school or of a SharePoint Online site, shared with
"Anyone with the link", as a read-only storage, read the way a browser without an account reads it. Microsoft
documents that such links open "without signing in", while "People in your organization" and "Specific people" links
need a sign-in:
    https://learn.microsoft.com/sharepoint/shareable-links-anyone-specific-people-organization
and that its API for shared items doesn't take them: "For OneDrive for Business and SharePoint, the Shares API always
requires authentication and can't be used to access anonymously shared content without a user context":
    https://learn.microsoft.com/graph/api/shares-get
So the driver opens the link as a browser does, and asks SharePoint's REST API with the cookie that gives.

Paths
    The storage root is the shared folder; /a/b is <folder>/a/b, sent as it is in FolderServerRelativeUrl.

    Listing (documented): POST <web>/_api/web/GetListUsingPath(DecodedUrl=@a1)/RenderListDataAsStream?@a1='<library>'
    with the parameters FolderServerRelativeUrl, a ViewXml naming the fields FileLeafRef, FSObjType, File_x0020_Size,
    Modified and UniqueId with a RowLimit of page_size, RenderOptions ListData (2) and EnableMediaTAUrls (4096),
    DatesInUtc and AddRequiredFields. The answer's Row holds the items; its NextHref, while there are more, is the
    next page's query.
        https://learn.microsoft.com/sharepoint/dev/sp-add-ins/working-with-lists-and-list-items-with-rest
        https://learn.microsoft.com/sharepoint/dev/solution-guidance/supporting-and-in-file-and-folder-with-the-resourcepath-api
    From OpenList: FSObjType "1" is a folder, File_x0020_Size the bytes as text, "Modified." the time in ISO 8601,
    UniqueId "{guid}", and rows asked for that way carry .spItemUrl. A path is looked up in its folder's listing, kept
    cache_ttl seconds.

Login
    Opening the link (from OpenList's driver; not documented): its GET, not following redirects, answers 302 with a
    FedAuth cookie (the visitor's session) to the folder's page: <web>/_layouts/15/onedrive.aspx?id=<folder> on
    OneDrive, <web>/<library>/Forms/AllItems.aspx?id=<folder> on a site. id is the shared folder's server-relative
    path; the document library is the first folder below the site <web> in it. A link with a password answers an
    ASP.NET form instead (__VIEWSTATE, __EVENTVALIDATION and the password field txtPassword), which is posted back as
    a browser does: the right password gets the 302 and the cookie, a wrong one the form again. Redirects are followed
    on the link's host only; one to another host is a sign-in (AuthError). The cookies go to that host only, in each
    request's Cookie header (the HTTP client keeps none). Then POST <web>/_api/contextinfo gives the form digest that
    POST requests without OAuth need in X-RequestDigest, valid FormDigestTimeoutSeconds:
        https://learn.microsoft.com/sharepoint/dev/sp-add-ins/complete-basic-operations-using-sharepoint-rest-endpoints
    Cookie and digest are kept that long as one session (TokenCache). When SharePoint refuses one (401, 403, a
    redirect away), the link is opened again, once.

Requests
    SharePoint throttles with 429 or 503 and a Retry-After it asks clients to honour: a short one (10 s at most)
    is waited out once, opening the link, listing and downloading; a longer one is an error at once.
        https://learn.microsoft.com/sharepoint/dev/general-development/how-to-avoid-getting-throttled-or-blocked-in-sharepoint-online
    A download's answer that isn't the file is read to its end when it is short (http.drain()), so that its
    connection can be used again; a longer one is cut off.

Links
    Folder links (/:f:/) on <tenant>-my.sharepoint.com and <tenant>.sharepoint.com, or sharepoint.cn (21Vianet),
    with or without a password. validate_config refuses links to a single file, and personal OneDrive (1drv.ms,
    onedrive.live.com): its /shares API on api.onedrive.com answers 401 without a sign-in
    (https://learn.microsoft.com/answers/questions/2138092), and Microsoft's own website reads such links with a
    token asked for with its built-in app ids, which HFile doesn't use.

Downloads
    open(): <web>/_layouts/15/download.aspx?UniqueId=<guid> with the cookie and the Range, following redirects on the
    host (from OpenList). 401, 403 or an HTML page without Content-Disposition instead of the file is a refusal: the
    link is opened again once, then DriverError (the link may not allow downloads).
    download_url(): a row's .spItemUrl is the item in SharePoint's own OneDrive API (<host>/_api/v2.0/drives/...);
    asked with the cookie (from OpenList), it gives @content.downloadUrl, which on that "direct endpoint" is Graph's
    pre-authenticated download URL: no authorization needed, Range taken, and it "might expire within minutes".
        https://learn.microsoft.com/onedrive/developer/rest-api/concepts/direct-endpoint-differences
        https://learn.microsoft.com/graph/api/driveitem-get-content
    Without .spItemUrl, or when that API doesn't answer 200, it gives None and the site sends the file. A request per
    link.

Limits
    Read-only. A folder whose name has % or # may not be read by SharePoint as meant in FolderServerRelativeUrl. The
    files of a link that doesn't allow downloads are listed but can't be read. It hasn't been tried against a real
    SharePoint.
"""

from __future__ import annotations

import dataclasses
import datetime
import html.parser
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from http.cookiejar import CookieJar, DefaultCookiePolicy
from typing import Any, ClassVar, TypeVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    TokenCache,
    TTLCache,
    as_int,
    json_or_none,
    origin,
    parse_time,
    range_header,
    response_stream,
    retry_after,
    short,
    transport_error,
    usable_name,
)
from .base import AuthError, BackendError, DriverError, Entry, NotADirectory, NotFound, Param, ReadStream, StorageDriver

SERVICE = "SharePoint"
JSON = "application/json;odata=nometadata"
RENDER_OPTIONS = 2 | 4096  # ListData, EnableMediaTAUrls
FIELDS = ("FileLeafRef", "FSObjType", "File_x0020_Size", "Modified", "UniqueId")
DIGEST_LIFETIME = 1800  # seconds, SharePoint's default FormDigestTimeoutSeconds

_HOSTS = (".sharepoint.com", ".sharepoint.cn")
_PERSONAL = re.compile(r"(?:.+\.)?(?:1drv\.ms|onedrive\.live\.com|onedrive\.com|microsoftpersonalcontent\.com)")
_KIND = re.compile(r"/:([A-Za-z]):/")  # /:f:/ a folder, /:u:/, /:w:/, /:x:/, /:b:/, ... a file
_GUID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}")
# The folder's page a link leads to, and the site's path in it.
_FOLDER_PAGES = (
    re.compile(r"(.*?)/_layouts/15/onedrive\.aspx", re.IGNORECASE),  # OneDrive for work or school
    re.compile(r"(.*)/[^/]+/Forms/[^/]+\.aspx", re.IGNORECASE),  # a site's document library
)

_T = TypeVar("_T")


@dataclasses.dataclass(frozen=True)
class _Session:
    """What opening the link gave: the cookies for its host, the site, its library and the shared folder (both
    server-relative paths), and the form digest."""

    cookies: str
    web: str  # absolute URL of the site, no "/" at the end
    library: str
    folder: str
    digest: str


@dataclasses.dataclass(frozen=True)
class _Item:
    name: str
    is_dir: bool
    size: int | None
    modified: datetime.datetime | None
    guid: str  # UniqueId without its braces; "" for a folder
    item_url: str  # .spItemUrl, or ""


class _Refused(Exception):
    """SharePoint refused the session: the caller opens the link again, once, and else raises `error`."""

    def __init__(self, error: DriverError):
        super().__init__(error.message)
        self.error = error


class _Forms(html.parser.HTMLParser):
    """The forms of a page, each with its action and the attributes of its inputs."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms: list[tuple[str, list[dict[str, str]]]] = []
        self._inputs: list[dict[str, str]] | None = None

    def handle_starttag(self, tag, attrs):
        values = {name: value or "" for name, value in attrs}
        if tag == "form":
            self._inputs = []
            self.forms.append((values.get("action", ""), self._inputs))
        elif tag == "input" and self._inputs is not None:
            self._inputs.append(values)

    def handle_endtag(self, tag):
        if tag == "form":
            self._inputs = None


def _password_form(page: str) -> tuple[str, list[dict[str, str]]] | None:
    """The page's form that asks for a password: (action, inputs), or None."""
    parser = _Forms()
    parser.feed(page)
    parser.close()
    return next(((action, inputs) for action, inputs in parser.forms
                 if any(attrs.get("type", "").lower() == "password" for attrs in inputs)), None)


def _form_fields(inputs: list[dict[str, str]], password: str) -> list[tuple[str, str]]:
    """What a browser sends when Enter is pressed in the password field: the hidden and text inputs as they are,
    the password, and the form's first submit button."""
    fields: list[tuple[str, str]] = []
    submit = False
    for attrs in inputs:
        name, kind = attrs.get("name"), (attrs.get("type") or "text").lower()
        if not name:
            continue
        if kind == "password":
            fields.append((name, password))
        elif kind in ("hidden", "text"):
            fields.append((name, attrs.get("value", "")))
        elif kind == "submit" and not submit:
            fields.append((name, attrs.get("value", "")))
            submit = True
    return fields


def _folder_page(url: httpx.URL) -> tuple[str, str] | None:
    """(site path, shared folder) when `url` is the shared folder's page, else None."""
    query = dict(urllib.parse.parse_qsl(url.query.decode("ascii", "replace")))
    folder = (query.get("id") or query.get("RootFolder") or "").rstrip("/")
    if not folder.startswith("/") or "\x00" in folder:
        return None
    for page in _FOLDER_PAGES:
        found = page.fullmatch(url.path)
        if found:
            return found.group(1), folder
    return None


def _send(client: httpx.Client, method: str, url: str | httpx.URL, *, stream: bool = False, **kwargs: Any) -> httpx.Response:
    """client's request, sent once more after a short Retry-After when SharePoint throttles it (429, 503)."""
    for attempt in range(2):
        response = client.send(client.build_request(method, url, **kwargs), stream=stream)
        wait = retry_after(response) if response.status_code in (429, 503) and not attempt else None
        if wait is None:
            break
        http.drain(response)
        time.sleep(wait)
    return response


def _literal(value: str) -> str:
    """An OData string literal."""
    return "'" + value.replace("'", "''") + "'"


def _item(row: Any) -> _Item | None:
    """An item of a RenderListDataAsStream row; None for one that can't be shown."""
    if not isinstance(row, dict) or not usable_name(row.get("FileLeafRef")):
        return None
    kind = str(row.get("FSObjType", "")).strip()
    if kind not in ("0", "1"):
        return None
    is_dir = kind == "1"
    guid = str(row.get("UniqueId") or "").strip().strip("{}")
    if not _GUID.fullmatch(guid):
        if not is_dir:
            return None  # a file that can't be downloaded
        guid = ""
    item_url = row.get(".spItemUrl")
    return _Item(
        name=row["FileLeafRef"],
        is_dir=is_dir,
        size=None if is_dir else as_int(row.get("File_x0020_Size")),
        modified=parse_time(row.get("Modified.")) or parse_time(row.get("Modified")),
        guid="" if is_dir else guid,
        item_url=item_url if isinstance(item_url, str) and item_url.startswith(("https://", "http://")) else "",
    )


def _error(response: httpx.Response) -> DriverError:
    """The error for a SharePoint answer of 400 or more (not 401 or 403, which are refusals)."""
    answer = json_or_none(response)
    error = (answer.get("odata.error") or answer.get("error")) if isinstance(answer, dict) else None
    message = ""
    if isinstance(error, dict):
        text = error.get("message")
        message = short(text.get("value") if isinstance(text, dict) else text)
    status = response.status_code
    detail = gettext(" (HTTP %(status)s)") % {"status": status}
    if status in (404, 410):
        return NotFound()
    if status in (429, 503):
        return BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": SERVICE})
    if status >= 500:
        return BackendError(gettext("%(service)s failed for now%(detail)s. Please try again later.") % {"service": SERVICE, "detail": detail})
    if message:
        return DriverError(gettext("%(service)s refused the request%(detail)s: %(message)s") % {"service": SERVICE, "detail": detail, "message": message})
    return DriverError(gettext("%(service)s refused the request%(detail)s.") % {"service": SERVICE, "detail": detail})


def _not_understood() -> BackendError:
    return BackendError(gettext("%(service)s returned content that couldn't be understood.") % {"service": SERVICE})


def _sign_in() -> AuthError:
    return AuthError(gettext('This link only opens for people who sign in. Share the folder with "Anyone with the '
                             'link" and use that link; the link may also have expired or been turned off.'))


def _no_folder() -> DriverError:
    return DriverError(gettext("SharePoint didn't show the shared folder: the link may have expired or been turned "
                               "off, or not be a link to a folder."))


def _too_many_redirects() -> BackendError:
    return BackendError(gettext("SharePoint redirected too many times."))


class OneDriveShareDriver(StorageDriver):
    type = "onedrive-share"
    label = gettext_lazy("OneDrive share link")
    params = (
        Param("url", gettext_lazy("Share link"), placeholder="https://contoso-my.sharepoint.com/:f:/g/personal/...",
              help=gettext_lazy('A link to a folder in OneDrive for work or school or in SharePoint, shared with '
                                '"Anyone with the link" so that it opens without signing in (Share, then Copy link). '
                                "Links to a personal OneDrive and links that need signing in don't work.")),
        Param("password", gettext_lazy("Share password"), kind="password", required=False, secret=True,
              help=gettext_lazy("When the share has one.")),
    )

    can_range = True
    can_direct_download = True  # when SharePoint gives a link, see download_url()
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    setup_params = ("url", "password")

    # Tuning; the tests make page_size small.
    page_size: ClassVar[int] = 500  # rows a listing request
    cache_ttl: ClassVar[float] = 60.0  # seconds a folder's listing is kept
    max_redirects: ClassVar[int] = 5

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._link = str(self.config.get("url") or "").strip()
        # Keeps no cookies: the share's go to its host only, in the Cookie header of each request.
        self._http = http.client(cookies=CookieJar(DefaultCookiePolicy(allowed_domains=[])))
        self._sessions = TokenCache(self._open_link)
        self._listings = TTLCache(256, self.cache_ttl)  # storage path of a folder -> {name: _Item}

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        link = str(config.get("url") or "").strip()
        try:
            split = urllib.parse.urlsplit(link)
            host = (split.hostname or "").lower()
        except ValueError:
            split, host = None, ""
        if _PERSONAL.fullmatch(host):
            return {"url": gettext("Links to a personal OneDrive (1drv.ms, onedrive.live.com) can't be used: besides "
                                   "Microsoft's own OneDrive website, only apps someone has signed in to can read "
                                   "them. Links from OneDrive for work or school and SharePoint work.")}
        if split is None or split.scheme != "https" or not host.endswith(_HOSTS):
            return {"url": gettext("The share link should start with https:// and be on sharepoint.com (sharepoint.cn "
                                   "for 21Vianet), as the links of OneDrive for work or school and SharePoint are.")}
        kind = _KIND.match(split.path)
        if kind and kind.group(1).lower() != "f":
            return {"url": gettext("This is a link to a file. Share the folder it is in and use that link.")}
        return {}

    # -- login

    def _open_link(self) -> tuple[_Session, float]:
        """Open the share link as a browser without an account does (see the module's docstring): the session and
        how many seconds it lasts."""
        home = origin(self._link)
        if home == ("", "", 0):
            raise _no_folder()
        password = str(self.config.get("password") or "")
        with http.client() as browser:  # keeps the cookies the link's pages set, as a browser does
            try:
                response = _send(browser, "GET", self._link)
                posted = False
                for _ in range(self.max_redirects + 2):
                    if response.has_redirect_location:
                        target = response.url.join(response.headers["location"])
                        if origin(target) != home:
                            raise _sign_in()  # sent to sign in
                        found = _folder_page(target)
                        if found is not None:
                            return self._session(browser, target, *found)
                        response = _send(browser, "GET", target)
                    elif response.status_code == 200:
                        form = _password_form(response.text)
                        if form is None:
                            raise _no_folder()
                        if posted:
                            raise AuthError(gettext("The share password is wrong."))
                        if not password:
                            raise AuthError(gettext("The share link has a password: please fill in the share password."))
                        action = response.url.join(form[0])
                        if origin(action) != home:
                            raise _no_folder()
                        response = _send(browser, "POST", action, content=urllib.parse.urlencode(_form_fields(form[1], password)),
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
                        posted = True
                    elif response.status_code in (404, 410):
                        raise NotFound(gettext("The share link doesn't exist; it may have been deleted."))
                    elif response.status_code in (401, 403):
                        raise _sign_in()
                    else:
                        raise _error(response)
                raise _too_many_redirects()
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None

    def _session(self, browser: httpx.Client, page: httpx.URL, web_path: str, folder: str) -> tuple[_Session, float]:
        """The session of the folder's page `page`: the library from the paths, the digest from the site."""
        if not folder.lower().startswith(web_path.lower() + "/"):
            raise _no_folder()
        library = web_path + "/" + folder[len(web_path) + 1:].split("/")[0]
        web = f"{page.scheme}://{page.netloc.decode('ascii')}{urllib.parse.quote(web_path, safe='/')}"
        cookies = "; ".join(f"{cookie.name}={cookie.value}" for cookie in browser.cookies.jar)
        response = _send(browser, "POST", web + "/_api/contextinfo", headers={"Accept": JSON})
        if response.status_code in (401, 403) or response.is_redirect:
            raise self._refused(response.status_code)
        if response.status_code >= 400:
            raise _error(response)
        answer = json_or_none(response)
        if isinstance(answer, dict) and isinstance(answer.get("d"), dict):  # odata=verbose
            answer = answer["d"].get("GetContextWebInformation")
        digest = answer.get("FormDigestValue") if isinstance(answer, dict) else None
        if not isinstance(digest, str) or not digest:
            raise _not_understood()
        lifetime = as_int(answer.get("FormDigestTimeoutSeconds")) or DIGEST_LIFETIME
        return _Session(cookies=cookies, web=web, library=library, folder=folder, digest=digest), float(lifetime)

    @staticmethod
    def _refused(status: int) -> AuthError:
        return AuthError(gettext("SharePoint refused access through the share link (HTTP %(status)s): it may have "
                                 "expired, been turned off, or had its password changed.") % {"status": status})

    def _with_session(self, work: Callable[[_Session], _T]) -> _T:
        """work(session); after a refusal, once more with the link opened again (its cookie or digest ran out)."""
        session = self._sessions.get()
        try:
            return work(session)
        except _Refused:
            self._sessions.invalidate(session)
        try:
            return work(self._sessions.get())
        except _Refused as refused:
            raise refused.error from None

    # -- requests

    def _post(self, session: _Session, url: str, body: Mapping[str, Any]) -> dict[str, Any]:
        headers = {"Accept": JSON, "Content-Type": JSON, "Cookie": session.cookies, "X-RequestDigest": session.digest}
        try:
            response = _send(self._http, "POST", url, content=json.dumps(body).encode(), headers=headers)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code in (401, 403) or response.is_redirect:
            raise _Refused(self._refused(response.status_code))
        if response.status_code >= 400:
            raise _error(response)
        answer = json_or_none(response)
        if not isinstance(answer, dict):
            raise _not_understood()
        return answer

    def _render(self, session: _Session, path: str) -> dict[str, _Item]:
        """The items of a folder, all pages."""
        base = (f"{session.web}/_api/web/GetListUsingPath(DecodedUrl=@a1)/RenderListDataAsStream"
                f"?@a1={urllib.parse.quote(_literal(session.library), safe='')}")
        view = ("<View><ViewFields>" + "".join(f'<FieldRef Name="{name}"/>' for name in FIELDS)
                + f'</ViewFields><RowLimit Paged="TRUE">{self.page_size}</RowLimit></View>')
        body = {"parameters": {
            "RenderOptions": RENDER_OPTIONS,
            "FolderServerRelativeUrl": session.folder + ("" if path == paths.ROOT else path),
            "ViewXml": view,
            "DatesInUtc": True,
            "AddRequiredFields": True,
        }}
        items: dict[str, _Item] = {}
        url, seen = base, set()
        while True:
            answer = self._post(session, url, body)
            data = answer["ListData"] if isinstance(answer.get("ListData"), dict) else answer
            rows = data.get("Row")
            if not isinstance(rows, list):
                raise _not_understood()
            for row in rows:
                item = _item(row)
                if item is not None:
                    items.setdefault(item.name, item)
            following = data.get("NextHref")
            if not following:
                return items
            if not isinstance(following, str) or not following.startswith("?"):
                raise _not_understood()
            if following in seen:
                raise BackendError(gettext("%(service)s repeated a page address.") % {"service": SERVICE})
            seen.add(following)
            url = base + "&" + following[1:]

    def _listing(self, path: str) -> dict[str, _Item]:
        cached = self._listings.get(path)
        if cached is None:
            cached = self._with_session(lambda session: self._render(session, path))
            self._listings.put(path, cached)
        return cached

    def _find(self, path: str) -> _Item:
        """The item at `path` (not the root), from its folder's listing. NotFound."""
        parent, name = paths.split(path)
        if parent != paths.ROOT and not self._find(parent).is_dir:
            raise NotFound()
        item = self._listing(parent).get(name)
        if item is None:
            raise NotFound()
        return item

    def _file(self, path: str) -> _Item:
        item = self._find(path) if path != paths.ROOT else None
        if item is None or item.is_dir:
            raise NotFound()
        return item

    @staticmethod
    def _entry(path: str, item: _Item) -> Entry:
        return Entry(name=item.name, path=path, is_dir=item.is_dir, size=item.size, modified=item.modified)

    # -- files

    def list(self, path: str) -> list[Entry]:
        if path != paths.ROOT and not self._find(path).is_dir:
            raise NotADirectory()
        return [self._entry(paths.join(path, item.name), item) for item in self._listing(path).values()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._listing(path)
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._find(path))

    @staticmethod
    def _download_refused(status: int) -> DriverError:
        return DriverError(gettext("SharePoint didn't send the file (HTTP %(status)s): the share link may not allow "
                                   "downloads.") % {"status": status})

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(path)
        span = range_header(offset, length, item.size)

        def download(session: _Session) -> ReadStream:
            url = f"{session.web}/_layouts/15/download.aspx?UniqueId={item.guid}"
            headers = {"Accept-Encoding": "identity", "Cookie": session.cookies, **({"Range": span} if span else {})}
            try:
                for _ in range(self.max_redirects + 1):
                    response = _send(self._http, "GET", url, headers=headers, stream=True)
                    if not response.has_redirect_location:
                        break
                    target = response.url.join(response.headers["location"])
                    http.drain(response)
                    if origin(target) != origin(session.web):  # sent to sign in
                        raise _Refused(self._download_refused(response.status_code))
                    url = str(target)
                else:
                    raise _too_many_redirects()
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            status = response.status_code
            page = ("text/html" in response.headers.get("content-type", "").lower()
                    and "content-disposition" not in response.headers)
            if status in (404, 410):
                http.drain(response)
                raise NotFound()
            if status in (401, 403) or (status in (200, 206) and page):
                http.drain(response)
                raise _Refused(self._download_refused(status))
            if status not in (200, 206):
                raise _error(http.read_error(response))
            return response_stream(response, offset=offset, length=length, total=item.size, service=SERVICE,
                                   modified=item.modified)

        return self._with_session(download)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        item = self._file(path)
        if not item.item_url:
            return None
        session = self._sessions.get()
        if origin(item.item_url) != origin(session.web):
            return None  # the cookie goes to the share's host only
        try:
            response = self._http.get(item.item_url, headers={"Accept": "application/json", "Cookie": session.cookies})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        answer = json_or_none(response) if response.status_code == 200 else None
        link = answer.get("@content.downloadUrl") if isinstance(answer, dict) else None
        return link if isinstance(link, str) and link.startswith(("https://", "http://")) else None
