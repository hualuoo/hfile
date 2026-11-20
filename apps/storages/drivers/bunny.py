"""Bunny Storage (bunny.net Edge Storage): one storage zone through its HTTP API, downloads from a pull zone.

Bunny's documentation, marked [docs] below:
- HTTP API and the regions' endpoints: https://bunny.net/docs/storage/http
- API reference: https://bunny.net/docs/storage/api-reference (OpenAPI:
  https://bunny.net/docs/api-reference/storage/openapi.json)
- Limits (requests, names, renames): https://bunny.net/docs/storage/limits
- Folders: https://bunny.net/docs/storage/dashboard; pull zones: https://bunny.net/docs/storage/quickstart
- Pull zones and metadata: https://bunny.net/docs/storage/s3 ("Object metadata")
- Token authentication: https://bunny.net/docs/cdn/security/token-authentication, and its /advanced
- CDN cache: https://bunny.net/docs/cdn/frequently-asked-questions/updating-files
Where those say nothing, Bunny's own code (MIT), marked [SDK]: its TypeScript storage SDK
(https://github.com/BunnyWay/edge-script-sdk, libs/bunny-storage: src/file.ts, and tests/integration.test.ts, which
runs against Bunny) and its token signers (https://github.com/BunnyWay/BunnyCDN.TokenAuthentication). OpenList's
driver was read for leads; nothing here rests on it alone.

Paths
    A zone's files are at https://<endpoint>/<zone>/<path>, the endpoint of the zone's main region [docs]. "/" of the
    storage source is the base path in the zone; every name is percent-encoded, so none reaches outside it. Folders
    are real: an upload makes the folders above its file [docs], the dashboard makes empty ones [docs], and so does
    PUT <folder>/ without a body [SDK: createDirectory, which Bunny's integration test runs]: mkdir() is that PUT.
    A listing is GET <folder>/, a JSON array of the folder's objects, all at once (ObjectName, IsDirectory, Length,
    LastChanged) [docs]. LastChanged has no offset: taken as UTC.

Finding one file or folder
    DESCRIBE <path> answers its object, as listings have them [SDK: file.get]; the API reference hasn't it. When it
    finds nothing (404), a listing of <path>/ tells whether a folder is there; when that is empty, or DESCRIBE is
    refused (400, 405, 501; after a 405 or 501 it isn't asked again), the name is looked for in its folder's listing.
    So what Bunny answers to DESCRIBE for a folder, or for the listing of a missing folder (404 or an empty one),
    doesn't matter: the documentation doesn't say.

Login
    AccessKey: the zone's password [docs]; a wrong password, zone name or region is 401 [docs].

Requests
    Errors carry {"HttpCode", "Message"} (an array of them in the reference; one alone is read too). Bunny limits how
    many requests run at once, answering 429, or 400 "Unable to process request", above that [docs: limits]: those, 5xx
    answers and connection errors are tried again (`attempts`), when the request body can be sent again. A DELETE sent
    again after its connection failed, that then finds nothing (404), was done the first time: its answer was lost.

Changes
    Bunny has no renames, moves or copies: "Renaming files or folders directly on Bunny Storage is not currently
    supported by design" [docs: limits], and the API has no such call. can_move and can_copy are False (the site copies
    files itself and refuses folder copies). rename() of a file downloads it, uploads it under the new name, then
    deletes it, as the docs advise: not atomic, and the file goes through this server. Renaming a folder raises
    Unsupported. Whether Bunny ignores case in names isn't documented: a rename that only changes the case goes through
    a third name (base.rename_through), which is right either way. Bunny's integration test waits a second between an
    upload and a delete of the same file, which may otherwise not delete it [SDK]: that third name may be left behind.

Downloads
    open() reads GET <path> with a Range header; an answer of the whole file (200) is cut here, as the documentation
    doesn't say whether the API takes Range. Without a pull zone, downloads go through this site (the instance's
    can_direct_download is False). With one, download_url() is the pull zone's address + the path in the zone [docs:
    quickstart], made here without a request; with its token authentication key, the link carries an Advanced token
    [docs]: token=HS256-<Base64URL(HMAC-SHA256(key, path + expires))>&expires=<Unix time>, valid `expires` seconds.
    The path is signed as written, not percent-encoded, as the docs' examples sign theirs; Bunny's own signers differ
    for a path that needs encoding (Go's signs it decoded, those for C#, Node.js and Rust encoded) [SDK], and which
    one the pull zone checks isn't documented. Pull zones send no Content-Disposition [docs: S3], so a link can't name
    the file the browser saves: it takes the link's last part, the file's name. A pull zone may serve a cached copy
    of a file replaced or deleted since, until the copy expires or is purged [docs: CDN cache]. Without token
    authentication the pull zone serves every file of the storage zone to whoever has its address ("Token
    authentication blocks requests to your Pull Zone unless a valid signed token is included" [docs: token
    authentication]), also those outside the base path and those the site hides: the param's help says so.
    open_known() reads with what the site's stat() just gave, without asking DESCRIBE again.

Uploads
    One PUT of the whole file, Content-Type by its name [docs]. When `data` can seek, its SHA-256 is computed first and
    sent as Checksum (uppercase hex), and Bunny refuses an upload that doesn't match it [docs]; a stream that can't
    seek goes without, as hashing it first would mean spooling it to disk. No browser uploads: they'd need the password.

Names
    No name may start or end with a space, and a path has at most 6,000 characters [docs: limits]: such new names are
    refused before asking Bunny. Bunny documents no limit per name: HFile's 255 bytes stays.

Limits
    Not tried against a real Bunny account: the tests use a fake built from the documentation and the SDK above.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import random
import re
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Any, BinaryIO, ClassVar
from urllib.parse import quote

import httpx

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
from ._cloud import as_int, parse_time, range_header, redirected, response_stream, retry_after, short, transport_error, usable_name
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
    only_case_differs,
    rename_through,
)
from .s3 import MiB, Reply, UploadBody, check_key, check_origin, content_type_of, parse_origin, read_limited

SERVICE = gettext_lazy("Bunny Storage")
MAX_PATH = 6000  # characters of a path in the zone [docs: limits]
MAX_LISTING = 64 * MiB  # a listing has the whole folder, a few hundred bytes an object
MAX_ANSWER = 64 * 1024
PULL_ZONE_EXAMPLE = "https://myzone.b-cdn.net"
_ZONE = re.compile(r"[A-Za-z0-9-]{1,63}")  # letters, digits and dashes [docs: S3], at most 63 [docs: core API]
_BUSY = "unable to process request"  # a 400 for too many requests of one kind at once [docs: limits]
_RETRY_STATUS = (500, 502, 503, 504)

# The main regions' endpoints, by Bunny's list [docs: http]; the value is the host.
ENDPOINTS = (
    ("storage.bunnycdn.com", gettext_lazy("Frankfurt, Germany (storage.bunnycdn.com)")),
    ("uk.storage.bunnycdn.com", gettext_lazy("London, United Kingdom (uk.storage.bunnycdn.com)")),
    ("ny.storage.bunnycdn.com", gettext_lazy("New York, United States (ny.storage.bunnycdn.com)")),
    ("la.storage.bunnycdn.com", gettext_lazy("Los Angeles, United States (la.storage.bunnycdn.com)")),
    ("sg.storage.bunnycdn.com", gettext_lazy("Singapore (sg.storage.bunnycdn.com)")),
    ("se.storage.bunnycdn.com", gettext_lazy("Stockholm, Sweden (se.storage.bunnycdn.com)")),
    ("br.storage.bunnycdn.com", gettext_lazy("São Paulo, Brazil (br.storage.bunnycdn.com)")),
    ("jh.storage.bunnycdn.com", gettext_lazy("Johannesburg, South Africa (jh.storage.bunnycdn.com)")),
    ("syd.storage.bunnycdn.com", gettext_lazy("Sydney, Australia (syd.storage.bunnycdn.com)")),
)
_HOSTS = frozenset(host for host, _ in ENDPOINTS)


def advanced_token(key: str, path: str, expires: int) -> str:
    """The token of Bunny's Advanced token authentication for `path` (as written, not percent-encoded) until the Unix
    time `expires`, without an IP or other parameters: "HS256-" + Base64URL(HMAC-SHA256(key, path + expires)), the
    padding left out."""
    digest = hmac.new(key.encode(), f"{path}{expires}".encode(), hashlib.sha256).digest()
    return "HS256-" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def bunny_error(status: int, message: str = "", reason: str = "") -> DriverError:
    """The DriverError for an error answer of the storage API. `status` is kept on it, and `busy`: Bunny's limit on
    requests at once (429, or 400 "Unable to process request", which may be the message or the reason phrase)."""
    busy = status == 429 or (status == 400 and _BUSY in f"{reason} {message}".lower())
    detail = gettext(" (HTTP %(status)s)") % {"status": status}
    if status in (401, 403):
        error: DriverError = AuthError(gettext("Bunny Storage refused the password%(detail)s. Please check the storage zone's name, its password (not the read-only one) and its region.") % {"detail": detail})
    elif status == 404:
        error = NotFound()
    elif busy:
        error = BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.") % {"service": SERVICE})
    elif status >= 500:
        error = BackendError(gettext("%(service)s failed for now%(detail)s. Please try again later.") % {"service": SERVICE, "detail": detail})
    elif message:
        error = DriverError(gettext("%(service)s refused the request%(detail)s: %(message)s") % {"service": SERVICE, "detail": detail, "message": message})
    else:
        error = DriverError(gettext("%(service)s refused the request%(detail)s.") % {"service": SERVICE, "detail": detail})
    error.status = status  # type: ignore[attr-defined]
    error.busy = busy  # type: ignore[attr-defined]
    return error


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _message(raw: bytes) -> str:
    """The Message of an error body: {"HttpCode", "Message"}, an array of them, or text."""
    answer = _json(raw)
    if isinstance(answer, list) and answer:
        answer = answer[0]
    if isinstance(answer, dict):
        return short(answer.get("Message") or "")
    return short(raw.decode("utf-8", "replace")) if answer is None else ""


def _spaced(path: str) -> bool:
    """Has a name in `path` a space at its start or end, which Bunny refuses [docs: limits]?"""
    return any(name != name.strip(" ") for name in paths.segments(path))


def _sha256(data: BinaryIO, size: int, start: int | None) -> str | None:
    """The SHA-256 of the next `size` bytes of `data`, as Bunny's Checksum (uppercase hex), `data` sought back to
    `start` afterwards; None when it can't seek (start None)."""
    if start is None:
        return None
    digest = hashlib.sha256()
    left = size
    while left > 0:
        chunk = data.read(min(left, MiB))
        if not chunk:
            break
        chunk = chunk[:left]
        digest.update(chunk)
        left -= len(chunk)
    data.seek(start)
    return digest.hexdigest().upper()


class _Reader:
    """A ReadStream as the file an upload reads: rename() sends on what it downloads."""

    def __init__(self, stream: ReadStream):
        self._chunks = iter(stream)
        self._rest = b""

    def read(self, size: int = -1) -> bytes:
        while size < 0 or len(self._rest) < size:
            chunk = next(self._chunks, b"")
            if not chunk:
                break
            self._rest += chunk
        if size < 0:
            data, self._rest = self._rest, b""
        else:
            data, self._rest = self._rest[:size], self._rest[size:]
        return data


class BunnyStorageDriver(StorageDriver):
    type = "bunny-storage"
    label = gettext_lazy("Bunny Storage")
    params = (
        Param("storage_zone", gettext_lazy("Storage zone name"),
              help=gettext_lazy("The storage zone's name, as the zone's Access tab (API / HTTP) in the bunny.net dashboard shows it.")),
        Param("password", gettext_lazy("Password"), kind="password", secret=True,
              help=gettext_lazy("The storage zone's password, from the same tab: not its read-only password, nor the account's API key.")),
        Param("endpoint", gettext_lazy("Region"), kind="select", default=ENDPOINTS[0][0], options=ENDPOINTS,
              help=gettext_lazy("The storage zone's main region: the same tab shows its endpoint, given in brackets here.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in the storage zone that is this storage source's root, like / or /folder1.")),
        Param("pull_zone", gettext_lazy("Pull zone address"), required=False, default="", placeholder=PULL_ZONE_EXAMPLE,
              help=gettext_lazy("Optional: a pull zone whose origin is this storage zone, written as https://hostname (its b-cdn.net hostname, or a custom one). Downloads then come from it; when empty, they go through this site. Without Token Authentication on the pull zone, anyone can download any file of the zone by its address, also files this site hides.")),
        Param("token_key", gettext_lazy("Token authentication key"), kind="password", secret=True, required=False, default="",
              help=gettext_lazy("When Token Authentication is on in the pull zone's Security settings: its URL Token Authentication Key. Download links then carry a token that expires.")),
    )
    can_range = True
    can_direct_download = True  # with a pull zone: the instance's follows the config
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True

    # Endpoint and tuning; tests point them at a fake.
    api_url: ClassVar[str] = ""  # tests: the fake's address, instead of https://<endpoint>
    attempts: ClassVar[int] = 3
    transport: ClassVar[httpx.BaseTransport | None] = None  # tests

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        try:
            base: str | None = paths.clean(self.config.get("base_path") or "/")
        except InvalidPath:
            base = None
        # A broken base path makes every operation refuse: never fall back to the whole zone.
        self._base = base if base is not None and not _spaced(base) else None
        self._zone = str(self.config.get("storage_zone") or "")
        self._password = str(self.config.get("password") or "")
        endpoint = str(self.config.get("endpoint") or "")
        self._api = self.api_url or (f"https://{endpoint}" if endpoint in _HOSTS else "")
        pull = str(self.config.get("pull_zone") or "").strip()
        try:
            self._pull = "{}://{}".format(*parse_origin(pull)) if pull else ""
        except ValueError:
            self._pull = ""  # validate_config refuses it: downloads go through the site
        self._token_key = str(self.config.get("token_key") or "")
        self._no_describe = False  # set when Bunny refuses DESCRIBE (_describe)
        self.can_direct_download = bool(self._pull)  # type: ignore[misc]
        kwargs: dict[str, Any] = {"transport": self.transport} if self.transport is not None else {}
        self._http = http.client(**kwargs)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        zone = config.get("storage_zone")
        if not isinstance(zone, str) or not zone.strip():
            errors["storage_zone"] = gettext("Please fill in %(label)s.") % {"label": cls._label("storage_zone")}
        elif not _ZONE.fullmatch(zone):
            errors["storage_zone"] = gettext("A storage zone's name has only English letters, digits and hyphens.")
        if (message := check_key(config.get("password"), cls._label("password"))):
            errors["password"] = message
        try:
            base = paths.clean(config.get("base_path") or "/")
        except InvalidPath as exc:
            errors["base_path"] = exc.message
        else:
            if _spaced(base):
                errors["base_path"] = gettext("Names in %(service)s can't start or end with a space.") % {"service": SERVICE}
        pull = config.get("pull_zone")
        if pull and (message := check_origin(pull, cls._label("pull_zone"), PULL_ZONE_EXAMPLE)):
            errors["pull_zone"] = message
        if config.get("token_key") and not pull:
            errors["token_key"] = gettext("The key signs the pull zone's links: please fill in the pull zone address too.")
        return errors

    @classmethod
    def _label(cls, key: str) -> str:
        return next(str(param.label) for param in cls.params if param.key == key)

    # -- requests

    def _send(self, method: str, remote: str, *, folder: bool = False, headers: Mapping[str, str] | None = None,
              body: bytes | UploadBody = b"", expect: Sequence[int] = (200,)) -> httpx.Response:
        """A request about `remote` (a path in the zone; `folder`: with the trailing "/"); the streaming response when
        its status is expected, else the mapped DriverError."""
        if not self._api:
            raise BackendError(gettext("The storage source's Endpoint is set wrong. Please ask the administrator to check it."))
        url = f"{self._api}/{quote(self._zone, safe='')}{quote(remote, safe='/')}"
        if folder and not url.endswith("/"):
            url += "/"
        resent = False  # sent again after its connection failed: it may have been done the first time
        attempt = 0
        while True:
            attempt += 1
            sent = {"AccessKey": self._password, **(headers or {})}
            if isinstance(body, UploadBody):
                content: Any = body.chunks()
                sent["Content-Length"] = str(body.size)
            else:
                content = body or None
                if method == "PUT":
                    sent["Content-Length"] = str(len(body))
            backoff = 0.2 * 2 ** (attempt - 1) * random.uniform(0.5, 1.5)
            try:
                request = self._http.build_request(method, url, headers=sent, content=content)
                response = self._http.send(request, stream=True)
            except httpx.TransportError as exc:
                self.log.warning("Bunny %s %s: %s (try %d)", method, remote, type(exc).__name__, attempt)
                if attempt < self.attempts and self._rewind(body):
                    self._pause(backoff)
                    resent = True
                    continue
                raise transport_error(exc, SERVICE) from None
            self.log.debug("Bunny %s %s -> %s", method, remote, response.status_code)
            if response.status_code in expect or (resent and method == "DELETE" and response.status_code == 404):
                return response  # a DELETE sent again that finds nothing: the first one was done, its answer lost
            if 300 <= response.status_code < 400:
                raise redirected(response, SERVICE)
            error = self._error(response)
            if ((error.busy or response.status_code in _RETRY_STATUS)  # type: ignore[attr-defined]
                    and attempt < self.attempts and self._rewind(body)):
                wait = retry_after(response, default=backoff)
                if wait is not None:  # None: Bunny asks for a longer wait than a request should hold a thread
                    self.log.warning("Bunny %s %s: HTTP %s (try %d)", method, remote, response.status_code, attempt)
                    self._pause(wait)
                    continue
            raise error

    def _request(self, method: str, remote: str, *, limit: int = MAX_ANSWER, **kwargs: Any) -> Reply:
        """A request whose answer is read whole."""
        response = self._send(method, remote, **kwargs)
        try:
            return Reply(response.status_code, response.headers, read_limited(response, limit))
        except httpx.HTTPError as exc:
            raise BackendError(gettext("Reading the storage's answer failed (%(error)s).") % {"error": type(exc).__name__}) from exc
        finally:
            response.close()

    def _error(self, response: httpx.Response) -> DriverError:
        try:
            raw = read_limited(response, MAX_ANSWER)
        except (httpx.HTTPError, BackendError):
            raw = b""
        finally:
            response.close()
        message = _message(raw)
        self.log.log(logging.DEBUG if response.status_code == 404 else logging.INFO,
                     "Bunny error: HTTP %s %s", response.status_code, message)
        return bunny_error(response.status_code, message, response.reason_phrase)

    @staticmethod
    def _rewind(body: bytes | UploadBody) -> bool:
        return body.rewind() if isinstance(body, UploadBody) else True

    @staticmethod
    def _pause(seconds: float) -> None:
        time.sleep(seconds)

    # -- paths

    def _remote(self, path: str) -> str:
        """The path in the zone: base path + storage path."""
        if self._base is None:
            raise InvalidPath(gettext("The storage source's base path is set wrong. Please ask the administrator to check it."))
        if not paths.is_clean(path):
            raise InvalidPath(gettext("Invalid path."))
        return paths.rebase(path, self._base)

    def _check_new(self, path: str) -> None:
        """Refuse what Bunny refuses in a new path [docs: limits], before asking it."""
        if _spaced(path):
            raise InvalidPath(gettext("Names in %(service)s can't start or end with a space.") % {"service": SERVICE})
        if len(self._remote(path)) > MAX_PATH:
            raise InvalidPath(ngettext("The path is too long: at most %(limit)s character.", "The path is too long: at most %(limit)s characters.", MAX_PATH) % {"limit": MAX_PATH})

    # -- objects

    def _children(self, remote: str) -> list[dict[str, Any]] | None:
        """The objects of the folder `remote` (a path in the zone); None when Bunny has none there (404)."""
        try:
            reply = self._request("GET", remote, folder=True, headers={"Accept": "application/json"}, limit=MAX_LISTING)
        except NotFound:
            return None
        answer = _json(reply.content)
        if not isinstance(answer, list):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return [item for item in answer if isinstance(item, dict)]

    def _describe(self, remote: str) -> tuple[dict[str, Any] | None, bool]:
        """DESCRIBE: (the object at `remote`, True), (None, True) when there is nothing (404), (None, False) when
        Bunny refused the call or answered something else. A Bunny that doesn't take the call (405, 501) isn't asked
        again: listings tell from then on."""
        if self._no_describe:
            return None, False
        try:
            reply = self._request("DESCRIBE", remote, headers={"Accept": "application/json"})
        except NotFound:
            return None, True
        except DriverError as error:
            status = getattr(error, "status", 0)
            if status in (405, 501):
                self._no_describe = True
            if status in (400, 405, 501) and not getattr(error, "busy", False):
                return None, False
            raise
        answer = _json(reply.content)
        return (answer, True) if isinstance(answer, dict) else (None, False)

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        modified = parse_time(item.get("LastChanged"))
        if item.get("IsDirectory") is True:
            return Entry(paths.name(path), path, True, None, modified)
        size = as_int(item.get("Length"))
        return Entry(paths.name(path), path, False, size if size is not None and size >= 0 else None, modified)

    def _in_parent(self, path: str) -> Entry | None:
        """`path` as its folder's listing has it, the name spelled exactly so."""
        name = paths.name(path)
        for item in self._children(self._remote(paths.parent(path))) or ():
            if item.get("ObjectName") == name:
                return self._entry(path, item)
        return None

    def _lookup(self, path: str) -> Entry | None:
        """The file or folder at `path` (not "/"), or None."""
        remote = self._remote(path)
        item, told = self._describe(remote)
        if item is not None:
            return self._entry(path, item)
        if told:  # no file there; a folder?
            children = self._children(remote)
            if children is None:
                return None
            if children:
                return Entry(paths.name(path), path, True)
        # An empty listing (an empty folder DESCRIBE didn't describe, or none), or DESCRIBE refused: the folder above tells.
        return self._in_parent(path)

    # -- reading

    def list(self, path: str) -> list[Entry]:
        remote = self._remote(path)
        children = self._children(remote)
        if children:
            return [self._entry(paths.join(path, item["ObjectName"]), item)
                    for item in children if usable_name(item.get("ObjectName"))]
        if path == "/":
            return []  # an empty zone, or a base folder that isn't there yet (an upload makes it)
        item, told = self._describe(remote)
        if item is not None:
            if item.get("IsDirectory") is True:
                return []
            raise NotADirectory()
        if children is None and told:
            raise NotFound()
        entry = self._in_parent(path)
        if entry is None:
            raise NotFound()
        if not entry.is_dir:
            raise NotADirectory()
        return []

    def stat(self, path: str) -> Entry:
        if path == "/":
            self._remote(path)  # a broken base path refuses
            return Entry("", "/", True)
        entry = self._lookup(path)
        if entry is None:
            raise NotFound()
        return entry

    def _read(self, path: str, entry: Entry | None, offset: int, length: int | None) -> ReadStream:
        """The bytes of the file `entry` describes, from `offset`."""
        if entry is None or entry.is_dir:
            raise NotFound()
        if length == 0:
            return ReadStream(iter(()), size=0, total=entry.size, offset=offset, modified=entry.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, entry.size)
        if span:
            headers["Range"] = span
        response = self._send("GET", self._remote(path), headers=headers, expect=(200, 206))
        return response_stream(response, offset=offset, length=length, total=entry.size, service=SERVICE,
                               etag=response.headers.get("etag"), modified=entry.modified)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        return self._read(path, self._lookup(path) if path != "/" else None, offset, length)

    def open_known(self, path: str, entry: Entry, offset: int = 0, length: int | None = None) -> ReadStream:
        # The site has just looked it up (stat(), for a ranged download): its size and time are known.
        return self._read(path, entry if path != "/" else None, offset, length)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        remote = self._remote(path)
        if not self._pull:
            return None
        url = self._pull + quote(remote, safe="/")
        if self._token_key:
            expires_at = int(time.time()) + max(1, int(expires))
            url += f"?token={advanced_token(self._token_key, remote, expires_at)}&expires={expires_at}"
        return url

    # -- writing

    def _put(self, path: str, data: BinaryIO, size: int) -> None:
        body = UploadBody(data, size)
        headers = {"Content-Type": content_type_of(paths.name(path))}
        checksum = _sha256(data, size, body.start)
        if checksum is not None:
            headers["Checksum"] = checksum
        self._request("PUT", self._remote(path), headers=headers, body=body, expect=(200, 201))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == "/":
            raise AlreadyExists()
        self._check_new(path)
        entry = self._lookup(path)
        if entry is not None and entry.is_dir:
            raise AlreadyExists()
        self._put(path, data, size)

    def mkdir(self, path: str) -> None:
        if path == "/":
            raise AlreadyExists()
        self._check_new(path)
        if self._lookup(path) is not None:
            raise AlreadyExists()
        self._request("PUT", self._remote(path), folder=True, expect=(200, 201))

    def delete(self, path: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The storage source's root folder can't be deleted."))
        entry = self.stat(path)
        self._request("DELETE", self._remote(path), folder=entry.is_dir, expect=(200, 204))

    def _move_file(self, source: str, target: str) -> None:
        """A file renamed by hand: downloaded, uploaded as `target`, then deleted."""
        if self._lookup(target) is not None:
            raise AlreadyExists()
        stream = self.open(source)
        try:
            if stream.size is None:
                raise Unsupported(gettext("This file's size is unknown, so it can't be copied."))
            self._put(target, _Reader(stream), stream.size)  # type: ignore[arg-type]
        finally:
            stream.close()
        self._request("DELETE", self._remote(source), expect=(200, 204))

    def rename(self, path: str, new_name: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)
        if target == path:
            return
        if self.stat(path).is_dir:
            raise Unsupported(gettext("Bunny Storage can't rename folders. Please make a folder with the new name, copy the files into it, then delete the old folder."))
        self._check_new(target)
        if only_case_differs(paths.name(path), new_name):
            # Another file of exactly that name is refused; else the file goes through a third name, so that a Bunny
            # ignoring case doesn't take the new name for the file itself (and delete it after the upload).
            if any(item.get("ObjectName") == new_name for item in self._children(self._remote(paths.parent(path))) or ()):
                raise AlreadyExists()
            rename_through(self._move_file, path, new_name)
        else:
            self._move_file(path, target)
