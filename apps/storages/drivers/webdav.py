"""WebDAV storage (RFC 4918), with httpx.

Paths
    The storage path "/a/b" is <url>/a/b: the configured URL, with its path, is the root (e.g.
    https://cloud.example.com/remote.php/dav/files/me/), and every name is percent-encoded as one
    path segment, so "/", "?", "#" or "%" in a name can't change the URL; backslashes are refused,
    because IIS takes them for separators. The URL's host is kept as httpx sends it (lowercase,
    IDNA, no default port): MOVE's and COPY's Destination must name the request's Host, and servers
    compare the two as text. Hrefs in answers, with or without scheme and host, in any
    percent-encoding, are decoded and matched against the path asked for; one that starts with "/"
    is a path to its end, "?" and "#" too (servers that don't encode them in names). The folder
    itself, which servers list with its contents, is told apart from them. A listing that comes in
    pages (Jianguoyun's: 750 entries each, the next one named in a Link header with rel="next") is
    read to its end, from the storage's origin only.

Login
    HTTP Basic, sent with every request; a server that answers with a Digest challenge (itself, not
    a host a redirect led to) gets Digest from then on. NTLM isn't supported. Only GET and PROPFIND
    follow redirects. GET's to another origin get no credentials. PROPFIND follows its own, each as
    a new request with the same method, body and credentials (httpx would send the Digest answer for
    the old URL, which Apache refuses, and make a 302 or 303 a GET): one to the path asked for with
    "/" goes to that URL, on the storage's origin whatever the Location says (behind a TLS proxy
    Apache writes http://); others are followed on that origin only, and one elsewhere is an error.

Params
    url, username, password, verify_tls.

Requests
    PROPFIND (Depth 0 for stat, 1 for a listing; the XML is read with expat, DOCTYPEs are refused,
    so no entity can expand), GET with Range (a server that ignores Range still works), PUT streamed
    with a Content-Length, MKCOL, MOVE and COPY with Destination and "Overwrite: F", DELETE. A
    folder is asked for with "/" at the end, as RFC 4918 wants (Apache redirects it there
    otherwise); a file asked for so, which Apache refuses with 400, is asked for again without. GET
    and PROPFIND go again, once, when the connection drops before an answer. Of a redirect's or a
    401's body at most 64 KiB is read. A time in CST is China Standard Time (UTC+8).

Changes
    Missing folders above a new file or folder are made with MKCOL.

Downloads
    GET passes the bytes on as they come, also under a Content-Encoding (a stored .gz that Apache
    labels gzip).

Uploads
    A PUT's answer is waited for a minute and a second more for each MiB sent: a proxy that buffered
    the body passes it on first, and the server may store it slowly.

Names
    Names that aren't UTF-8 are left out.

Limits
    No direct links (the server needs our credentials). PUT is one request; whether an interrupted
    upload leaves something depends on the server (most write to a temporary file). Locks (LOCK,
    UNLOCK) aren't used. The largest file is what the server takes.
"""

from __future__ import annotations

import base64
import contextlib
import datetime
import email.utils
import logging
import re
import threading
import urllib.parse
import xml.parsers.expat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import BinaryIO

import httpx

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
from ._cloud import position
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
    only_case_differs,
    rename_through,
)

log = logging.getLogger("hfile.storage.webdav")

CHUNK = 256 * 1024
WRITE_CHUNK = 256 * 1024
MAX_LISTING = 256 * 1024 * 1024  # bytes of the PROPFIND answers of one listing, all its pages together
MAX_PAGES = 1000  # pages of one listing
UNUSED_BODY_MAX = 64 * 1024  # bytes httpx reads of a redirect's or a 401's body

_PROPFIND = (b'<?xml version="1.0" encoding="utf-8"?>\n'
             b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:getcontentlength/><d:getlastmodified/>'
             b'<d:getetag/><d:getcontenttype/></d:prop></d:propfind>')
_PROPS = ("getcontentlength", "getlastmodified", "getetag", "getcontenttype")

_LINK = re.compile(r"<([^>]*)>([^<]*)")  # a link of a Link header (RFC 8288), and its parameters
_REL = re.compile(r';\s*rel\s*=\s*(?:"([^"]*)"|([^\s;,"]+))', re.IGNORECASE)


# --- addresses -----------------------------------------------------------------------------------

def normalize_url(url: str) -> str:
    """The WebDAV URL with its host as httpx sends it, its path percent-encoded once and ending with "/";
    ValueError with a message for the admin when it isn't usable."""
    url = (url or "").strip()
    if not url:
        raise ValueError(gettext("Please fill in the WebDAV address."))
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError(gettext("The address can't contain control characters such as line breaks."))
    parts = urllib.parse.urlsplit(url)
    if any(ch.isspace() for ch in parts.netloc):
        raise ValueError(gettext("The server address can't contain spaces."))
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError(gettext("The address must start with http:// or https://."))
    if not parts.hostname:
        raise ValueError(gettext("The address has no server."))
    if parts.username is not None or parts.password is not None:
        raise ValueError(gettext("Put the username and password in the fields below, not in the address."))
    if parts.query or parts.fragment or "?" in url or "#" in url:
        raise ValueError(gettext("The address can't have a part after ? or #."))
    try:
        parts.port
    except ValueError:
        raise ValueError(gettext("The port in the address is wrong.")) from None
    segments = [urllib.parse.unquote(part) for part in parts.path.split("/") if part]
    if any(part in (".", "..") for part in segments):
        raise ValueError(gettext("The address's path can't have . or .."))
    path = "/" + "".join(urllib.parse.quote(part, safe="") + "/" for part in segments)
    try:
        # Lowercase, IDNA, no default port: the Host httpx sends, which a Destination must name as it is.
        origin = httpx.URL(f"{parts.scheme.lower()}://{parts.netloc}/")
    except httpx.InvalidURL:
        raise ValueError(gettext("The server's name in the address is wrong.")) from None
    return f"{origin.scheme}://{origin.netloc.decode('ascii')}{path}"


def _segments(raw_path: str, errors: str = "strict") -> list[str]:
    """The decoded names of an encoded URL path; UnicodeDecodeError when one isn't UTF-8."""
    return [urllib.parse.unquote(part, errors=errors) for part in raw_path.split("/") if part]


# --- listings ------------------------------------------------------------------------------------

def _parse_time(text: str | None) -> datetime.datetime | None:
    if not text:
        return None
    text = text.strip()
    if text[-4:].upper() == " CST":
        # China Standard Time on the servers that write it (Java's name for Asia/Shanghai); email.utils takes it
        # for US Central Time, 14 hours off (STOR-11).
        text = text[:-4] + " +0800"
    try:
        value = email.utils.parsedate_to_datetime(text)  # RFC 1123, what getlastmodified is
    except (TypeError, ValueError, OverflowError, IndexError):
        try:
            value = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (ValueError, OverflowError):
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value


class _BadXML(Exception):
    pass


@dataclass
class _Resource:
    href: str
    is_dir: bool
    size: int | None
    modified: datetime.datetime | None
    etag: str | None
    content_type: str | None


class _MultiStatus:
    """The <response> elements of a multistatus answer, read with expat (no DTDs, no entities)."""

    def __init__(self):
        self.resources: list[_Resource] = []
        self._parser = xml.parsers.expat.ParserCreate(namespace_separator=" ")
        parser = self._parser
        parser.buffer_text = True
        parser.StartElementHandler = self._start
        parser.EndElementHandler = self._end
        parser.CharacterDataHandler = self._text
        parser.StartDoctypeDeclHandler = self._refuse
        parser.EntityDeclHandler = self._refuse
        parser.SetParamEntityParsing(xml.parsers.expat.XML_PARAM_ENTITY_PARSING_NEVER)
        self._stack: list[str] = []
        self._chars: list[str] = []
        self._response: dict | None = None
        self._propstat: dict | None = None

    def feed(self, data: bytes, final: bool = False) -> None:
        try:
            self._parser.Parse(data, final)
        # LookupError: an encoding Python doesn't know, which was a 500 (STOR-04).
        except (xml.parsers.expat.ExpatError, LookupError) as e:
            raise _BadXML(str(e)) from None

    @staticmethod
    def _refuse(*args):
        raise _BadXML("DOCTYPE or entity declaration")

    @staticmethod
    def _local(name: str) -> str | None:
        """The name of an element of the DAV: namespace (or of none, for sloppy servers)."""
        namespace, _, local = name.rpartition(" ")
        return local if namespace in ("DAV:", "") else None

    def _text(self, data: str) -> None:
        self._chars.append(data)

    def _start(self, name, attrs):
        local = self._local(name)
        parent = self._stack[-1] if self._stack else None
        self._stack.append(local or "")
        self._chars = []
        if local == "response":
            self._response = {"href": None, "props": {}}
        elif local == "propstat" and self._response is not None:
            self._propstat = {"status": None, "props": {}}
        elif local == "collection" and parent == "resourcetype" and self._propstat is not None:
            self._propstat["props"]["collection"] = True

    def _end(self, name):
        local = self._stack.pop() if self._stack else ""
        parent = self._stack[-1] if self._stack else None
        text = "".join(self._chars).strip()
        self._chars = []
        if not local:
            return
        if local == "href" and parent == "response" and self._response is not None:
            self._response["href"] = text
        elif local == "status" and parent == "propstat" and self._propstat is not None:
            self._propstat["status"] = text
        elif parent == "prop" and local in _PROPS and self._propstat is not None:
            self._propstat["props"][local] = text
        elif local == "propstat" and self._propstat is not None and self._response is not None:
            status = (self._propstat["status"] or "HTTP/1.1 200").split()
            if len(status) < 2 or status[1].startswith("2"):
                self._response["props"].update(self._propstat["props"])
            self._propstat = None
        elif local == "response" and self._response is not None:
            if self._response["href"]:
                props = self._response["props"]
                size = None
                if not props.get("collection"):
                    try:
                        size = int(props.get("getcontentlength", ""))
                    except ValueError:
                        size = None
                self.resources.append(_Resource(
                    href=self._response["href"], is_dir=bool(props.get("collection")), size=size,
                    modified=_parse_time(props.get("getlastmodified")), etag=props.get("getetag") or None,
                    content_type=props.get("getcontenttype") or None))
            self._response = None


def parse_multistatus(chunks: Iterable[bytes], limit: int = MAX_LISTING) -> list[_Resource]:
    """The resources of a multistatus answer; BackendError when it has more than `limit` bytes."""
    parser = _MultiStatus()
    total = 0
    for chunk in chunks:
        total += len(chunk)
        if total > limit:
            raise BackendError(gettext("This folder's listing is too large."))
        parser.feed(chunk)
    parser.feed(b"", True)
    return parser.resources


class _Counted:
    """The chunks of a body, counting their bytes (`size`)."""

    def __init__(self, chunks: Iterable[bytes]):
        self._chunks, self.size = chunks, 0

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            self.size += len(chunk)
            yield chunk


def split_listing(resources: list[_Resource], url: str) -> tuple[_Resource | None, dict[str, _Resource]]:
    """(the resource asked for, {name: child}) of a PROPFIND answer from `url`.

    Hrefs are resolved against that URL and compared by their decoded names, so hosts and
    percent-encodings don't matter. When none of them is under the path asked for (a proxy that
    rewrites paths), the shortest one is the resource asked for."""
    # Paths are compared decoded leniently; only the names of children must be UTF-8.
    asked = _segments(urllib.parse.urlsplit(url).path, "replace")
    found = []
    for resource in resources:
        href = resource.href
        if href.startswith("/"):
            # A path to its end: old nginx-dav-ext writes "?" and "#" of names as they are ("C# notes.txt").
            href = href.replace("?", "%3F").replace("#", "%23")
        raw = urllib.parse.urlsplit(urllib.parse.urljoin(url, href)).path
        found.append((_segments(raw, "replace"), raw, resource))
    folder = asked
    if not any(segments == asked or segments[:-1] == asked for segments, _raw, _r in found) and found:
        folder = min((segments for segments, _raw, _r in found), key=len)
    itself = None
    children: dict[str, _Resource] = {}
    for segments, raw, resource in found:
        if segments == folder:
            itself = resource
        elif len(segments) == len(folder) + 1 and segments[:-1] == folder:
            try:
                name = urllib.parse.unquote(raw.rstrip("/").rsplit("/", 1)[-1], errors="strict")
            except UnicodeDecodeError:
                log.debug("left out a name that isn't UTF-8: %s", resource.href)
                continue
            if name in (".", "..") or "/" in name or "\\" in name or "\x00" in name:
                log.debug("left out a name that can't be a storage path: %r", name)
                continue
            children[name] = resource
        else:
            log.debug("ignored an href outside the folder asked for: %s", resource.href)
    return itself, children


def _next_link(response: httpx.Response) -> str | None:
    """The target of the answer's "Link: <url>; rel="next"", if it has one."""
    for value in response.headers.get_list("link"):
        for target, params in _LINK.findall(value):
            if any("next" in (quoted or bare).lower().split() for quoted, bare in _REL.findall(params)):
                return target.strip()
    return None


def _next_page(response: httpx.Response, folder: str) -> str | None:
    """The URL of the next page of the listing of `folder` (the URL its first page came from); None after the last.

    It must be on the folder's origin, so that the credentials go nowhere else. A path that names the folder,
    encoded once or twice (the clients made for Jianguoyun decode it once before they use it), gives the
    folder's own URL with the link's query."""
    target = _next_link(response)
    if target is None:
        return None
    try:
        url = response.url.join(target)
    except httpx.InvalidURL:
        url = None
    if url is None or _origin(url) != _origin(response.url):
        raise BackendError(gettext("The WebDAV server puts the rest of this folder's listing at an address HFile doesn't follow."))
    asked = _segments(urllib.parse.urlsplit(folder).path, "replace")
    named = _segments(url.raw_path.partition(b"?")[0].decode("ascii"), "replace")
    if named == asked or [urllib.parse.unquote(part) for part in named] == asked:
        return str(httpx.URL(folder).copy_with(query=url.query))
    return str(url)


# --- the HTTP client -----------------------------------------------------------------------------

class _Capped(httpx.SyncByteStream):
    """The first `left` bytes of a stream."""

    def __init__(self, stream, left: int):
        self._stream, self._left = stream, left

    def __iter__(self):
        if self._left <= 0:
            return
        for chunk in self._stream:
            yield chunk[:self._left]
            self._left -= len(chunk)
            if self._left <= 0:
                return  # before asking the stream for more

    def close(self):
        self._stream.close()


def _cap_unused_body(response: httpx.Response) -> None:
    """A response hook: httpx reads the whole body of a redirect it follows and of the 401 a Digest login answers,
    with no limit, and the server may send any amount there (SEC-10). Nothing reads those bodies."""
    if response.status_code == 401 or 300 <= response.status_code < 400:
        response.stream = _Capped(response.stream, UNUSED_BODY_MAX)


def _origin(url: httpx.URL) -> tuple[str, str, int | None]:
    default = {"http": 80, "https": 443}.get(url.scheme)
    return url.scheme, url.host, url.port or default


class _Auth(httpx.Auth):
    """Basic with every request; Digest from the first time the server asks for it. The server's challenge
    goes to httpx's DigestAuth as it is, so that every later request carries the answer from the start:
    a streamed body (an upload) can't be sent a second time just to learn it. On a server that wants
    Digest for writes only (Apache's usual <LimitExcept GET PROPFIND>), the first two uploads failed
    (STOR-08); now the first one goes again (WebDAVDriver.write) and the next ones go through."""

    def __init__(self, username: str, password: str):
        token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        self._basic = f"Basic {token}"
        self._digest = httpx.DigestAuth(username, password)
        self._use_digest = False
        self._lock = threading.Lock()

    @property
    def digest(self) -> bool:
        return self._use_digest

    def auth_flow(self, request):
        if not self._use_digest:
            request.headers["Authorization"] = self._basic
            response = yield request
            if response.status_code != 401 or _origin(response.url) != _origin(request.url):
                return  # a 401 from where a redirect led says nothing about our server
            challenges = [value.split(None, 1)[0].lower()
                          for value in response.headers.get_list("www-authenticate") if value.strip()]
            if "digest" not in challenges or "basic" in challenges:
                return
            with self._lock:
                self._use_digest = True
            del request.headers["Authorization"]
            # The Digest flow takes this challenge (not asking for another one) and remembers it; it gives
            # the request back with its answer.
            flow = self._digest.auth_flow(request)
            next(flow)
            try:
                answered = flow.send(response)
            except StopIteration:
                return
            if not isinstance(request.stream, httpx.ByteStream):
                return  # a streamed body can't be sent twice: write() sends its data again
            response = yield answered
            try:
                flow.send(response)
            except StopIteration:
                pass
            return
        yield from self._digest.auth_flow(request)


# --- uploads and downloads -----------------------------------------------------------------------

class _ReadFailed(Exception):
    """Reading the data to upload failed: the caller's error, passed on as it is."""

    def __init__(self, error: BaseException):
        super().__init__(error)
        self.error = error


def _upload_chunks(data: BinaryIO, size: int) -> Iterator[bytes]:
    left = size
    while left > 0:
        try:
            chunk = data.read(min(WRITE_CHUNK, left))
        except Exception as e:
            raise _ReadFailed(e) from e
        if not chunk:
            raise DriverError(ngettext("The uploaded data is incomplete: %(missing)s byte missing.", "The uploaded data is incomplete: %(missing)s bytes missing.", left) % {"missing": left})
        chunk = chunk[:left]
        left -= len(chunk)
        yield chunk


def _content_range(value: str) -> tuple[int | None, int | None, int | None]:
    """'bytes 100-149/10000' -> (100, 149, 10000); None for what's missing or '*'."""
    try:
        unit, _, rest = value.strip().partition(" ")
        span, _, full = rest.partition("/")
        start, _, end = span.partition("-")
        if unit.lower() != "bytes":
            return None, None, None
        return int(start), int(end), None if full in ("*", "") else int(full)
    except ValueError:
        return None, None, None


def _body(response: httpx.Response, skip: int, size: int | None) -> Iterator[bytes]:
    """The bytes of a GET's answer as they come: a Content-Encoding there is the stored file's own (a .gz that
    Apache labels gzip), as its size is, not one httpx should undo (we ask for identity)."""
    try:
        for chunk in response.iter_raw(CHUNK):
            if skip:
                if len(chunk) <= skip:
                    skip -= len(chunk)
                    continue
                chunk, skip = chunk[skip:], 0
            if size is not None:
                if len(chunk) >= size:
                    if size:
                        yield chunk[:size]
                    return
                size -= len(chunk)
            yield chunk
        if size:
            raise BackendError(gettext("The WebDAV server ended the transfer early."))
    except httpx.HTTPError as e:
        raise BackendError(gettext("Reading the file from the WebDAV server failed: %(error)s") % {"error": type(e).__name__}) from e
    finally:
        response.close()


# --- the driver ----------------------------------------------------------------------------------

class WebDAVDriver(StorageDriver):
    type = "webdav"
    label = "WebDAV"
    params = (
        Param("url", gettext_lazy("WebDAV address"), placeholder="https://dav.example.com/dav/",
              help=gettext_lazy("With the path on the server: the folder at this address is the storage's "
                   "root. For Nextcloud, say, https://domain/remote.php/dav/files/username/; "
                   "for Synology, https://domain:5006/shared-folder/.")),
        Param("username", gettext_lazy("Username"), required=False),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True,
              help=gettext_lazy('For services with two-step verification on, such as Nextcloud, enter an "app password" here.')),
        Param("verify_tls", gettext_lazy("Verify the server certificate"), kind="bool", required=False, default=True, advanced=True,
              help=gettext_lazy("Verifies the server's certificate over https. It can be turned off for servers with "
                                "self-signed certificates such as NASes, but then someone could pose as the server.")),
    )

    can_range = True
    can_direct_download = False
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    setup_params = ("url", "username", "password")

    def __init__(self, config, *, on_update=None):
        super().__init__(config, on_update=on_update)
        try:
            self._base_url = normalize_url(self.config.get("url") or "")
        except ValueError:
            self._base_url = None
        username = self.config.get("username") or ""
        password = self.config.get("password") or ""
        self._auth = _Auth(username, password) if username or password else None
        self._client = self._make_client()

    def close(self) -> None:
        self._client.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config):
        errors = {}
        try:
            normalize_url(config.get("url") or "")
        except ValueError as e:
            errors["url"] = str(e)
        for key in ("username", "password"):
            if any(ch in str(config.get(key) or "") for ch in "\r\n\x00"):
                errors[key] = gettext("It can't contain line breaks.")
        if ":" in str(config.get("username") or ""):
            errors["username"] = gettext("The username can't contain a colon.")
        return errors

    # -- requests

    def _make_client(self, **extra) -> httpx.Client:
        return http.client(verify=self.config.get("verify_tls", True) is not False, auth=self._auth,
                           event_hooks={"response": [_cap_unused_body]}, **extra)

    def _url(self, path: str, *, folder: bool = False) -> str:
        if not isinstance(path, str) or not paths.is_clean(path):
            raise InvalidPath(gettext("Invalid path."))
        if self._base_url is None:
            raise BackendError(gettext("The WebDAV address is set wrong. Please ask the administrator to check the storage source's settings."))
        segments = paths.segments(path)
        if any("\\" in part for part in segments):
            # Windows servers (IIS) take a backslash for a separator: "a\\..\\.." could leave the URL's folder.
            raise InvalidPath(gettext("Names in WebDAV storage can't contain \\."))
        try:
            encoded = "/".join(urllib.parse.quote(part, safe="") for part in segments)
        except UnicodeEncodeError:
            raise InvalidPath(gettext("The name has characters that can't be used.")) from None
        return self._base_url + encoded + ("/" if folder and segments else "")

    def _send(self, method: str, url: str, *, headers: dict | None = None, content=None,
              follow: bool = False, timeout=httpx.USE_CLIENT_DEFAULT) -> httpx.Response:
        # A read goes again, once, when the connection dropped before an answer came: a kept-alive connection the
        # server had closed meanwhile does that (STOR-09). A change may have been made all the same: not that.
        again = method in ("GET", "PROPFIND")
        while True:
            try:
                request = self._client.build_request(method, url, headers=headers, content=content, timeout=timeout)
                response = self._client.send(request, stream=True, follow_redirects=follow)
                break
            except _ReadFailed as e:
                raise e.error from None
            except DriverError:
                raise
            except httpx.TimeoutException as e:
                raise BackendError(gettext("The WebDAV server didn't answer in time. Please try again later.")) from e
            except httpx.TooManyRedirects as e:
                raise BackendError(gettext("The WebDAV server redirected too many times.")) from e
            except httpx.StreamConsumed as e:
                raise BackendError(gettext("The WebDAV server wants a new login. Please upload again.")) from e
            except (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError) as e:
                if not again:
                    raise BackendError(gettext("Couldn't connect to the WebDAV server: %(error)s: %(detail)s") % {"error": type(e).__name__, "detail": e}) from e
                again = False
                log.info("WebDAV %s: the connection dropped before the answer (%s); sending it again", method,
                         type(e).__name__)
            except httpx.HTTPError as e:
                raise BackendError(gettext("Couldn't connect to the WebDAV server: %(error)s: %(detail)s") % {"error": type(e).__name__, "detail": e}) from e
        if response.status_code == 401:
            schemes = {value.split(None, 1)[0].lower() for value in response.headers.get_list("www-authenticate")
                       if value.strip()}
            http.drain(response)
            if schemes and schemes <= {"ntlm", "negotiate"}:  # STOR-10: not a wrong password
                raise AuthError(gettext("This WebDAV server only accepts Windows NTLM / Negotiate logins, which HFile doesn't support."))
            raise AuthError(gettext("The WebDAV server doesn't accept this username and password."))
        return response

    def _call(self, method: str, url: str, *, ok: tuple[int, ...], headers: dict | None = None,
              content=None) -> httpx.Response:
        """A request without a body to read; the response if its status is in `ok`, else an error."""
        response = self._send(method, url, headers=headers, content=content)
        http.drain(response)
        if response.status_code not in ok:
            raise self._status_error(response)
        return response

    @staticmethod
    def _status_error(response: httpx.Response) -> DriverError:
        code = response.status_code
        what = f"{code} {response.reason_phrase}".strip()
        if code == 404:
            return NotFound()
        if code == 403:
            return DriverError(gettext("The WebDAV server refused this (%(what)s).") % {"what": what})
        if code == 423:
            return DriverError(gettext("The file is locked. Please try again later (%(what)s).") % {"what": what})
        if code == 507:
            return DriverError(gettext("The WebDAV server is out of space (%(what)s).") % {"what": what})
        if code == 413:
            return DriverError(gettext("The file is too large for the WebDAV server (%(what)s).") % {"what": what})
        if 300 <= code < 400:
            location = response.headers.get("location", "")
            if location:  # which may be the right address (https:// for the http:// typed)
                what = f"{what}, {location[:200]}"
            return BackendError(gettext("The WebDAV server redirects to another address (%(what)s). "
                                        "Please check the storage source's WebDAV address.") % {"what": what})
        if code >= 500:
            return BackendError(gettext("The WebDAV server failed (%(what)s).") % {"what": what})
        return DriverError(gettext("The WebDAV server refused this (%(what)s).") % {"what": what})

    def _propfind(self, path: str, depth: int, *, folder: bool = False) -> tuple[_Resource | None, dict[str, _Resource]]:
        """split_listing() of a PROPFIND of `path`. `folder` asks for it with "/" at the end, which Apache answers
        without a redirect; a 400 to that (Apache's for a file asked for so) asks again without. Every page of a
        listing is read (Jianguoyun's have 750 entries), at most MAX_PAGES of them and MAX_LISTING bytes."""
        plain = self._url(path)
        url = self._url(path, folder=folder)
        response = self._ask(url, depth)
        if url != plain and response.status_code == 400:
            http.drain(response)
            response = self._ask(plain, depth)
        here = str(response.url)  # where the folder is, after redirects: every page is matched against it
        itself, children, left = None, {}, MAX_LISTING
        for page in range(MAX_PAGES):
            if page:
                response = self._ask(following, depth)
            resources, left = self._read_multistatus(response, left)
            found, more = split_listing(resources, here)
            itself = found if itself is None else itself
            children.update(more)
            following = _next_page(response, here) if depth else None
            if following is None:
                return itself, children
        raise BackendError(gettext("This folder's listing is too large."))

    def _ask(self, url: str, depth: int) -> httpx.Response:
        """The answer to a PROPFIND of `url`, after the redirects it follows itself, each a new request with the
        same method and body, Digest answering for its URL (httpx sends the answer for the first one, which Apache
        refuses with 400, and makes a 302 or 303 a GET). A redirect to the path asked for with "/" goes there on
        the URL's origin, whatever its Location names: behind a proxy that does TLS, Apache's says http://. Other
        redirects are followed on the origin only; one that leaves it is given back, for the caller to report."""
        for _hop in range(self._client.max_redirects + 1):
            response = self._send("PROPFIND", url, content=_PROPFIND,
                                  headers={"Depth": str(depth), "Content-Type": "application/xml; charset=utf-8"})
            location = response.headers.get("location") if response.status_code in (301, 302, 303, 307, 308) else None
            if not location:
                return response
            asked = response.url
            try:
                target = asked.join(location)
            except httpx.InvalidURL:
                return response
            with contextlib.suppress(httpx.HTTPError):
                response.read()  # at most UNUSED_BODY_MAX: the connection can serve the next request
            response.close()
            if target.path == asked.path + "/" and not asked.query:
                url = str(asked) + "/"
            elif _origin(target) == _origin(asked):
                url = str(target)
            else:
                return response
        raise BackendError(gettext("The WebDAV server redirected too many times."))

    def _read_multistatus(self, response: httpx.Response, left: int) -> tuple[list[_Resource], int]:
        """The resources of a PROPFIND's answer, and what is left of the `left` bytes a listing may still have;
        closes the answer."""
        try:
            if response.status_code != 207:
                http.drain(response)
                raise self._status_error(response)
            body = _Counted(response.iter_bytes())
            try:
                resources = parse_multistatus(body, left)
            except _BadXML as e:
                raise BackendError(gettext("The WebDAV server's answer isn't valid XML: %(detail)s") % {"detail": e}) from None
            except httpx.HTTPError as e:
                raise BackendError(gettext("Reading the WebDAV server's answer failed: %(detail)s") % {"detail": e}) from e
        finally:
            response.close()
        return resources, left - body.size

    def _stat_or_none(self, path: str, *, folder: bool = False) -> _Resource | None:
        try:
            itself, children = self._propfind(path, 0, folder=folder)
        except NotFound:
            return None
        if itself is None:
            # Depth 0 answers hold only the resource asked for, whatever its href says.
            if len(children) == 1:
                return next(iter(children.values()))
            raise BackendError(gettext("The WebDAV server's answer doesn't have the file asked for."))
        return itself

    def _entry(self, path: str, resource: _Resource) -> Entry:
        return Entry(name=paths.name(path), path=path, is_dir=resource.is_dir,
                     size=None if resource.is_dir else resource.size, modified=resource.modified)

    # -- reading

    def check(self) -> None:
        if not self.stat("/").is_dir:
            raise DriverError(gettext("This WebDAV address isn't a folder."))
        self.list("/")

    def list(self, path: str) -> list[Entry]:
        try:
            itself, children = self._propfind(path, 1, folder=True)
        except NotFound:
            if path == paths.ROOT:
                raise BackendError(gettext("The folder at the WebDAV address doesn't exist. Please ask the administrator to check the storage source's settings.")) from None
            raise
        if itself is not None and not itself.is_dir:
            raise NotADirectory()
        return [self._entry(paths.join(path, name), resource) for name, resource in children.items()
                if not paths.dots_only(name)]  # D46: such a name can't be addressed

    def stat(self, path: str) -> Entry:
        resource = self._stat_or_none(path)
        if resource is None:
            if path == paths.ROOT:
                raise BackendError(gettext("The folder at the WebDAV address doesn't exist. Please ask the administrator to check the storage source's settings."))
            raise NotFound()
        if path == paths.ROOT and not resource.is_dir:
            raise BackendError(gettext("This WebDAV address isn't a folder. Please ask the administrator to check the storage source's settings."))
        return self._entry(path, resource)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        return self.open_known(path, self.stat(path), offset, length)

    def open_known(self, path: str, entry: Entry, offset: int = 0, length: int | None = None) -> ReadStream:
        if entry.is_dir:
            raise NotFound()
        total = entry.size
        offset = max(offset, 0)
        if total is not None:
            offset = min(offset, total)
            size = total - offset if length is None else max(0, min(length, total - offset))
            if size == 0:
                return ReadStream([], size=0, total=total, offset=offset, modified=entry.modified)
        else:
            size = length
        headers = {"Accept-Encoding": "identity"}
        if offset > 0 or (size is not None and total is not None and offset + size < total):
            last = "" if size is None else str(offset + size - 1)
            headers["Range"] = f"bytes={offset}-{last}"
        response = self._send("GET", self._url(path), headers=headers, follow=True)
        try:
            skip = 0
            if response.status_code == 206:
                start, end, full = _content_range(response.headers.get("content-range", ""))
                if start != offset:
                    raise BackendError(gettext("The range the WebDAV server sent is wrong."))
                total = full if full is not None else total
            elif response.status_code == 200:
                skip = offset  # the server ignored Range: drop what comes before
            else:
                http.drain(response)
                raise self._status_error(response)
        except BaseException:
            response.close()
            raise
        return ReadStream(_body(response, skip, size), size=size, total=total, offset=offset,
                          content_type=response.headers.get("content-type"), etag=response.headers.get("etag"),
                          modified=entry.modified, close=response.close)

    # -- writing

    def _makedirs(self, folder: str) -> None:
        """Make `folder` and the missing ones above it."""
        if folder == paths.ROOT:
            return
        response = self._call("MKCOL", self._url(folder, folder=True), ok=(200, 201, 204, 405, 409))
        if response.status_code in (200, 201, 204):
            return
        if response.status_code == 409:  # the folder above is missing
            self._makedirs(paths.parent(folder))
            response = self._call("MKCOL", self._url(folder, folder=True), ok=(200, 201, 204, 405))
            if response.status_code != 405:
                return
        existing = self._stat_or_none(folder)  # 405: something is there already
        if existing is None:
            raise DriverError(gettext("The WebDAV server doesn't allow creating this folder (405)."))
        if not existing.is_dir:
            raise NotADirectory(gettext('"%(name)s" is a file, not a folder.') % {"name": paths.name(folder)})

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists(gettext("This is the storage's root folder."))
        url = self._url(path)
        existing = self._stat_or_none(path)
        if existing is not None and existing.is_dir:
            raise AlreadyExists(gettext("There is already a folder with that name there."))
        if existing is None:
            parent = self._stat_or_none(paths.parent(path), folder=True)
            if parent is None:
                self._makedirs(paths.parent(path))
            elif not parent.is_dir:
                raise NotADirectory()
        headers = {"Content-Length": str(size), "Content-Type": "application/octet-stream"}
        # The answer may come long after the last byte: a proxy that buffered the body (nginx does so by default)
        # passes it on only then, and the server may copy or index it. A minute, and a second more for each MiB.
        timeout = httpx.Timeout(http.TIMEOUT, connect=http.CONNECT_TIMEOUT, read=http.TIMEOUT + size / (1024 * 1024))
        start = position(data)
        was_digest = self._auth is not None and self._auth.digest
        try:
            response = self._send("PUT", url, content=_upload_chunks(data, size) if size else b"", headers=headers,
                                  timeout=timeout)
        except AuthError:
            if self._auth is None or was_digest or not self._auth.digest:
                raise
            # This upload made the server ask for Digest (one that wants it for writes only, STOR-08): the
            # answer is known now, the data goes again.
            if start is None:
                raise BackendError(gettext("The WebDAV server asked for Digest login, which is now used. Please upload again.")) from None
            data.seek(start)
            response = self._send("PUT", url, content=_upload_chunks(data, size), headers=headers, timeout=timeout)
        http.drain(response)
        if response.status_code in (200, 201, 204):
            return
        what = f"{response.status_code} {response.reason_phrase}".strip()
        # Asked above: the folder is there, and no folder has the file's name. These two mean something else here.
        if response.status_code == 405:  # nginx without PUT in its dav_methods
            raise DriverError(gettext("The WebDAV server doesn't allow uploading files here (%(what)s).") % {"what": what})
        if response.status_code == 404:  # IIS, for a body over its limit
            raise DriverError(gettext("The WebDAV server refused the upload (%(what)s). IIS answers so to a file over its "
                                      "size limit (maxAllowedContentLength, about 28.6 MB unless set higher).") % {"what": what})
        raise self._status_error(response)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        url = self._url(path, folder=True)
        response = self._call("MKCOL", url, ok=(200, 201, 204, 405, 409))
        if response.status_code == 409:
            self._makedirs(paths.parent(path))
            response = self._call("MKCOL", url, ok=(200, 201, 204, 405))
        if response.status_code == 405:
            if self._stat_or_none(path) is not None:
                raise AlreadyExists()
            raise DriverError(gettext("The WebDAV server doesn't allow creating this folder (405)."))

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be deleted."))
        entry = self.stat(path)
        response = self._call("DELETE", self._url(path, folder=entry.is_dir), ok=(200, 202, 204, 207))
        if response.status_code == 207:  # a multistatus: something in the folder wasn't deleted
            raise DriverError(gettext("Some files in the folder couldn't be deleted."))

    def _transfer(self, method: str, path: str, target: str, entry: Entry) -> None:
        """MOVE or COPY `path` (`entry`, which the caller asked the server for) to `target`, never replacing anything."""
        headers = {"Destination": self._url(target, folder=entry.is_dir), "Overwrite": "F"}
        if method == "COPY":
            headers["Depth"] = "infinity"
        response = self._call(method, self._url(path, folder=entry.is_dir), headers=headers,
                              ok=(200, 201, 204, 409, 412))
        if response.status_code == 412:
            raise AlreadyExists()
        if response.status_code == 409:  # the folder to put it in is missing
            raise NotFound(gettext("The target folder doesn't exist."))

    def _check_target(self, folder: str, target: str) -> None:
        dest = self._stat_or_none(folder, folder=True)
        if dest is None:
            raise NotFound(gettext("The target folder doesn't exist."))
        if not dest.is_dir:
            raise NotADirectory()
        if self._stat_or_none(target) is not None:
            raise AlreadyExists()

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)  # InvalidPath for "/" and ".."
        entry = self.stat(path)
        if new_name == paths.name(path):
            return
        if self._stat_or_none(target) is not None:  # also for servers that ignore "Overwrite: F"
            # A server that ignores case (IIS, or rclone on Windows) finds the entry itself under its new spelling;
            # its folder's listing has the names as they are. A MOVE there is refused (412), so two are made.
            if (not only_case_differs(paths.name(path), new_name)
                    or new_name in self._propfind(paths.parent(path), 1, folder=True)[1]):
                raise AlreadyExists()
            rename_through(lambda old, new: self._transfer("MOVE", old, new, entry), path, new_name)
            return
        self._transfer("MOVE", path, target, entry)

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved into itself or a folder inside it."))
        if paths.parent(path) == folder:
            self.stat(path)
            return
        target = paths.join(folder, paths.name(path))
        entry = self.stat(path)
        self._check_target(folder, target)
        self._transfer("MOVE", path, target, entry)

    def copy(self, path: str, folder: str) -> None:
        if path == paths.ROOT or paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself or a folder inside it."))
        target = paths.join(folder, paths.name(path))
        entry = self.stat(path)
        self._check_target(folder, target)
        self._transfer("COPY", path, target, entry)
