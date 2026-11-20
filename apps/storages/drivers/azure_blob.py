"""Azure Blob Storage through the Blob service REST API, with the storage account's key.

Microsoft's documentation (learn.microsoft.com):
- Shared Key: https://learn.microsoft.com/rest/api/storageservices/authorize-with-shared-key
- List Blobs: https://learn.microsoft.com/rest/api/storageservices/list-blobs
- Get Blob: https://learn.microsoft.com/rest/api/storageservices/get-blob
- Get Blob Properties: https://learn.microsoft.com/rest/api/storageservices/get-blob-properties
- Put Blob: https://learn.microsoft.com/rest/api/storageservices/put-blob
- Put Block: https://learn.microsoft.com/rest/api/storageservices/put-block
- Put Block List: https://learn.microsoft.com/rest/api/storageservices/put-block-list
- Copy Blob: https://learn.microsoft.com/rest/api/storageservices/copy-blob
- Abort Copy Blob: https://learn.microsoft.com/rest/api/storageservices/abort-copy-blob
- Copy Blob From URL: https://learn.microsoft.com/rest/api/storageservices/copy-blob-from-url
- Delete Blob: https://learn.microsoft.com/rest/api/storageservices/delete-blob
- Service SAS: https://learn.microsoft.com/rest/api/storageservices/create-service-sas
- Names: https://learn.microsoft.com/rest/api/storageservices/naming-and-referencing-containers--blobs--and-metadata
- Errors: https://learn.microsoft.com/rest/api/storageservices/status-and-error-codes2,
  https://learn.microsoft.com/rest/api/storageservices/blob-service-error-codes,
  https://learn.microsoft.com/rest/api/storageservices/common-rest-api-error-codes
- Hierarchical namespace: https://learn.microsoft.com/azure/storage/blobs/data-lake-storage-known-issues,
  https://learn.microsoft.com/azure/storage/blobs/upgrade-to-data-lake-storage-gen2

Paths
    A storage source is a name prefix in one container: base path "/photos" is the prefix "photos/". The file "/a/b.txt"
    is the blob "photos/a/b.txt"; the folder "/a" is every blob below "photos/a/", listed with the delimiter "/".
    mkdir() writes a zero-length marker "photos/a/" with the metadata hdi_isfolder=true (Microsoft's mark of a folder),
    so that empty folders exist. A zero-length blob "photos/a" with that metadata (the "directory stub" of AzCopy and
    Hadoop) is a folder too.

Hierarchical namespace
    Hierarchical namespace (Data Lake Storage Gen2) accounts answer the same Blob API, with these differences, which the
    code allows for: folders are real (an upload makes the folders above it, and they stay when emptied); listings show
    them as zero-length blobs with ResourceType "directory" (and hdi_isfolder) or as prefixes; Delete Blob deletes a
    folder only when it is empty, so folders are deleted deepest first; a listing whose prefix runs through a file
    fails, so a failed listing checks whether the path is a file. A file made through the Data Lake API can't be
    replaced by Put Block (only by Put Blob): such large uploads fail with a message. That mkdir()'s marker makes a
    directory there is what rclone found; Microsoft's docs don't say. The Data Lake endpoint (dfs) and its atomic
    renames aren't used.

Requests
    Our own client on httpx: Shared Key in the Authorization header (HMAC-SHA256 with the account key over the string to
    sign of the docs), x-ms-version API_VERSION. Transient failures (connection errors, 5xx, ServerBusy) are tried again
    when the body can be sent again; a 403 whose Date is more than 15 minutes from our clock (Azure's limit for
    x-ms-date) corrects our clock and goes once more. A call that reads its answer whole has CALL_SECONDS in all,
    retries included (not uploads and Put Block List). A DELETE sent again after its connection failed, that then finds
    nothing (404), was done the first time: its answer was lost.

Params
    The Blob service endpoint ("https://<account>.blob.core.windows.net"; the same with .blob.core.chinacloudapi.cn for
    Azure in China), the account name and key, the container, the base path, whether the container is private, and an
    optional download domain.

Changes
    Renames, moves and copies use Copy Blob inside the account, then Delete Blob for moves: a request per blob, not
    atomic, folders blob by blob. A copy may finish later than its answer (x-ms-copy-status: pending): the target is
    polled with Get Blob Properties for up to copy_seconds, then the copy is aborted and the target deleted. Copy Blob
    From URL (synchronous) isn't used: it takes sources up to 256 MiB only, and they need a SAS of their own. Deleting a
    folder deletes its blobs one by one (Blob Batch isn't used). A blob another program named with a "." or ".." part
    ("a/./b") can't be named in any request: httpx resolves such parts of a URL's path, while Shared Key signs it as
    written (s3.has_dot_segment). A folder that holds one isn't deleted at all (the message names the blob, to delete
    in the Azure portal); a move or a copy leaves such blobs where they are, and their folders with them.

Downloads
    open() reads with Get Blob and a Range header. download_url(): for a private container a service SAS of the blob
    (sr=b, sp=r) signed with the account key, valid for `expires` seconds, with rscd so that the browser saves the file
    under its name; for a container with anonymous read access the plain URL. A download domain (a custom domain or CDN
    whose origin is the Blob endpoint) replaces the scheme and host; a SAS isn't tied to the host. Links are made
    locally, without a request.

Uploads
    Uploads go through the site: Put Blob up to single_put_limit, else Put Block in blocks of min_block_size or more
    (scaled to stay within 50,000 blocks) and Put Block List. Until the list is committed the old blob stays as it was;
    Azure discards the uncommitted blocks of a failed upload after a week (there is no call to abort them). Put Blob's
    Content-MD5 (computed by Azure) is compared with what was sent. No browser uploads.

Names
    Blob names are case-sensitive. Azure trims the dots at the end of a name's parts ("a." is "a"), so such names are
    refused, and left out of listings.

Limits
    Blob names up to 1,024 characters; Put Blob up to 5,000 MiB, blocks up to 4,000 MiB and 50,000 of them (190.7 TiB);
    a copy's source URL up to 2 KiB. Not tried against a real Azure account: written from the documentation above,
    tested against tests/fake_azure_blob.py.
"""

from __future__ import annotations

import base64
import binascii
import dataclasses
import datetime
import email.utils
import hashlib
import hmac
import logging
import math
import random
import re
import secrets
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, BinaryIO, ClassVar
from urllib.parse import quote, unquote
from xml.sax.saxutils import escape as xml_escape

import httpx

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
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
)
from .s3 import (
    CHUNK,
    MAX_ERROR_BODY,
    MAX_XML,
    MiB,
    Reply,
    UploadBody,
    check_base_path_and_domain,
    check_origin,
    content_disposition,
    content_type_of,
    guarded,
    has_dot_segment,
    key_prefix,
    parse_origin,
    parse_xml,
    read_limited,
    run_parallel,
    slice_stream,
)

API_VERSION = "2021-08-06"  # x-ms-version and the SAS's sv: has ResourceType (2020-10-02) and Encoded names (2021-02-12)
MAX_NAME_CHARS = 1024
MAX_COPY_SOURCE = 2048  # x-ms-copy-source: "a URL of up to 2 KiB"
CALL_SECONDS = 60.0  # a call that reads its answer whole, tries again included (BlobClient.request)
SKEW_SECONDS = 15 * 60  # Azure refuses requests whose x-ms-date is older than this
FOLDER_MARK = "hdi_isfolder"  # the metadata of a folder's marker, stub or Data Lake directory

# The standard headers in Shared Key's string to sign, in its order.
SIGNED_HEADERS = ("content-encoding", "content-language", "content-length", "content-md5", "content-type", "date",
                  "if-modified-since", "if-match", "if-none-match", "if-unmodified-since", "range")


# --- signatures -----------------------------------------------------------------------------------

def string_to_sign(method: str, account: str, path: str, query: Sequence[tuple[str, str]],
                   headers: Mapping[str, str]) -> str:
    """Shared Key's string to sign (Blob service, 2009-09-19 and later). `path` is the URI path as sent
    (percent-encoded), `query` the (name, value) pairs not encoded, `headers` the request's headers with
    lowercase names. Content-Length is empty when zero; x-ms-* values are sent without runs of spaces or
    quoted strings, so collapsing their whitespace is all the canonicalising they need."""
    lines = [method.upper()]
    for name in SIGNED_HEADERS:
        value = headers.get(name, "")
        lines.append("" if name == "content-length" and value == "0" else value)
    canonical_headers = "".join(f"{name}:{' '.join(str(value).split())}\n"
                                for name, value in sorted(headers.items()) if name.startswith("x-ms-"))
    values: dict[str, list[str]] = {}
    for name, value in query:
        values.setdefault(name.lower(), []).append(value)
    resource = f"/{account}{path}" + "".join(f"\n{name}:{','.join(sorted(items))}"
                                              for name, items in sorted(values.items()))
    return "\n".join(lines) + "\n" + canonical_headers + resource


def signature(key: bytes, text: str) -> str:
    """Base64(HMAC-SHA256(key, UTF-8 text)), `key` being the account key decoded from Base64."""
    return base64.b64encode(hmac.new(key, text.encode("utf-8"), hashlib.sha256).digest()).decode()


def sas_string_to_sign(*, permissions: str, expiry: str, resource: str, version: str = API_VERSION,
                       resource_type: str = "b", disposition: str = "") -> str:
    """A service SAS's string to sign (version 2020-12-06 and later), with no start, identifier, IP, protocol,
    snapshot or encryption scope, and only the Content-Disposition (rscd) of the response set. `resource` is
    "/blob/<account>/<container>/<blob name>", not encoded."""
    return "\n".join([permissions, "", expiry, resource, "", "", "", version, resource_type, "", "",
                      "", disposition, "", "", ""])


# --- small helpers --------------------------------------------------------------------------------

def _http_date(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _text(element: ET.Element | None, name: str) -> str:
    child = element.find(name) if element is not None else None
    return (child.text or "") if child is not None else ""


def _listable(name: str) -> bool:
    """A listed name a storage path can address: not dots and spaces only (paths refuse those), no NUL or "/",
    and not ending with a dot, which Azure would trim from a request's URL (another blob)."""
    return not paths.dots_only(name) and "\x00" not in name and "/" not in name and not name.endswith(".")


def _ends_with_dot(name: str) -> bool:
    return any(part.endswith(".") for part in name.split("/"))


def _too_slow() -> BackendError:
    return BackendError(ngettext("The storage didn't finish answering in %(seconds)s second. Please try again later.", "The storage didn't finish answering in %(seconds)s seconds. Please try again later.", CALL_SECONDS) % {"seconds": format(CALL_SECONDS, "g")})


@dataclasses.dataclass(frozen=True)
class BlobItem:
    """A blob of a listing. `directory`: a Data Lake directory or a folder's stub (ResourceType, hdi_isfolder)."""
    name: str
    size: int
    modified: datetime.datetime | None
    directory: bool

    @property
    def folder(self) -> bool:
        """A folder's directory, stub or marker ("a/")."""
        return self.directory or self.name.endswith("/")


@dataclasses.dataclass(frozen=True)
class BlobProps:
    """What Get Blob Properties (or Get Blob) says of a blob."""
    size: int
    modified: datetime.datetime | None
    etag: str | None
    content_type: str | None
    directory: bool
    copy_id: str | None = None
    copy_status: str | None = None
    copy_detail: str | None = None


def _is_directory(headers: httpx.Headers) -> bool:
    return (headers.get("x-ms-resource-type", "").lower() == "directory"
            or headers.get(f"x-ms-meta-{FOLDER_MARK}", "").lower() == "true")


def _props(headers: httpx.Headers) -> BlobProps:
    try:
        size = int(headers.get("content-length", ""))
    except ValueError:
        raise BackendError(gettext("The storage gave no file size (Content-Length).")) from None
    return BlobProps(size, _http_date(headers.get("last-modified")), headers.get("etag"), headers.get("content-type"),
                     _is_directory(headers), headers.get("x-ms-copy-id"), headers.get("x-ms-copy-status"),
                     headers.get("x-ms-copy-status-description"))


# --- errors ---------------------------------------------------------------------------------------

_AUTH_HINTS = {
    "AuthenticationFailed": gettext_lazy("the storage account name or the account key is wrong"),
    "AuthorizationFailure": gettext_lazy("the storage account's network rules (firewall, virtual networks) may not let this server in"),
    "AccountIsDisabled": gettext_lazy("the storage account is disabled"),
    "InsufficientAccountPermissions": gettext_lazy("the storage account doesn't allow this operation now"),
}
_RETRY_CODES = ("ServerBusy", "InternalError", "OperationTimedOut")
_RETRY_STATUS = (429, 500, 502, 503, 504)


def azure_error(status: int, code: str = "", message: str = "", *, container: str = "") -> DriverError:
    """The DriverError for an error answer; `status` and `code` are kept on it. Azure's own message (its first
    line, without RequestId and Time) is shown only where it says more than the code; never for refused
    credentials, whose details repeat the signature."""
    lines = message.strip().splitlines()
    detail = lines[0].strip()[:200] if lines else ""
    if code == "ContainerNotFound":
        error: DriverError = BackendError(gettext("The container %(container)s doesn't exist (ContainerNotFound). Please check the container name in the storage source's settings.") % {"container": container})
    elif status == 404:
        error = NotFound()
    elif status in (401, 403) or code in _AUTH_HINTS or code in ("InvalidAuthenticationInfo", "NoAuthenticationInformation"):
        hint = _AUTH_HINTS.get(code) or gettext("please check the storage account name, the account key, and that the account allows access with account keys")
        error = AuthError(gettext("The storage refused the request (%(code)s): %(hint)s.") % {"code": code or status, "hint": hint})
    elif code == "BlobAlreadyExists":
        error = AlreadyExists()
    elif code == "BlobOperationNotSupported":  # Put Block on a file made through the Data Lake API
        error = DriverError(gettext("Azure can't replace this file block by block: it was made through the Data Lake Storage API (BlobOperationNotSupported). Please delete it first, then upload it again."))
    elif code == "InvalidResourceName":
        error = InvalidPath(gettext("Azure Blob Storage doesn't accept this name (%(code)s).") % {"code": code})
    elif code == "ServerBusy" or status == 429:
        error = BackendError(gettext("The storage is busy (%(code)s). Please try again later.") % {"code": code or status})
    elif status == 501:
        error = Unsupported()
    elif status >= 500:
        error = BackendError(gettext("The storage failed (HTTP %(status)s%(code)s). Please try again later.") % {"status": status, "code": " " + code if code else ""})
    elif detail:
        error = BackendError(gettext("The storage refused the request (HTTP %(status)s%(code)s): %(detail)s") % {"status": status, "code": " " + code if code else "", "detail": detail})
    else:
        error = BackendError(gettext("The storage refused the request (HTTP %(status)s%(code)s).") % {"status": status, "code": " " + code if code else ""})
    error.status = status  # type: ignore[attr-defined]
    error.code = code  # type: ignore[attr-defined]
    return error


# --- the client -----------------------------------------------------------------------------------

class BlobClient:
    """A small client for one container: Shared Key requests with retries, listings, blobs, copies."""

    attempts = 3  # tries of a request that may be repeated

    def __init__(self, *, endpoint: str, account: str, key: bytes, container: str, page_size: int = 5000,
                 copy_seconds: float = 600.0, copy_poll: float = 0.5, transport: httpx.BaseTransport | None = None,
                 logger: logging.Logger | None = None):
        self.scheme, self.host = parse_origin(endpoint)
        self.account = account
        self._key = key  # never logged
        self.container = container
        self.page_size = page_size
        self.copy_seconds = copy_seconds
        self.copy_poll = copy_poll
        self.log = logger or logging.getLogger("hfile.storage.azure-blob")
        self.clock_offset = 0.0  # seconds to add to our clock, from a 403 whose Date was far from it
        kwargs: dict[str, Any] = {"transport": transport} if transport is not None else {}
        self._http = http.client(**kwargs)

    def close(self) -> None:
        self._http.close()

    def path(self, name: str | None) -> str:
        """The URI path, percent-encoded: the container's, or a blob's in it."""
        container = "/" + quote(self.container, safe="")
        return container if name is None else f"{container}/{quote(name, safe='/')}"

    def url(self, name: str, origin: str | None = None) -> str:
        return (origin or f"{self.scheme}://{self.host}") + self.path(name)

    def sas_query(self, name: str, *, expires: int, filename: str = "") -> str:
        """The query of a service SAS that reads one blob for `expires` seconds and, with `filename`, has it saved
        under that name (rscd). No start time: a clock behind Azure's can't make the link not valid yet."""
        expiry = datetime.datetime.fromtimestamp(self.now() + max(1, int(expires)), datetime.timezone.utc)
        fields = {"sv": API_VERSION, "se": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"), "sr": "b", "sp": "r"}
        if filename:
            fields["rscd"] = content_disposition(filename)
        text = sas_string_to_sign(permissions="r", expiry=fields["se"],
                                  resource=f"/blob/{self.account}/{self.container}/{name}", disposition=fields.get("rscd", ""))
        fields["sig"] = signature(self._key, text)
        return "&".join(f"{key}={quote(value, safe='')}" for key, value in fields.items())

    def now(self) -> float:
        return time.time() + self.clock_offset

    # -- requests

    def request(self, method: str, name: str | None = None, *, query: Sequence[tuple[str, str]] = (),
                headers: Mapping[str, str] | None = None, body: bytes | UploadBody = b"",
                expect: Sequence[int] = (200,), takes_long: bool = False, ends: float | None = None) -> Reply:
        """A signed request, its answer read whole, in CALL_SECONDS at most, tries again included: httpx only
        limits each read, and a server answering slowly but steadily would hold the call. Not an upload, which
        takes as long as its bytes do, nor a call that `takes_long`, unless `ends` is given."""
        if ends is None and not takes_long and not isinstance(body, UploadBody):
            ends = time.monotonic() + CALL_SECONDS
        response = self.send(method, name, query=query, headers=headers, body=body, expect=expect, ends=ends)
        try:
            with http.cut_off(response, ends):
                return Reply(response.status_code, response.headers, read_limited(response, MAX_XML))
        except httpx.HTTPError as exc:
            if ends is not None and time.monotonic() >= ends:
                raise _too_slow() from None
            raise BackendError(gettext("Reading the storage's answer failed (%(error)s).") % {"error": type(exc).__name__}) from exc
        finally:
            response.close()

    def send(self, method: str, name: str | None = None, *, query: Sequence[tuple[str, str]] = (),
             headers: Mapping[str, str] | None = None, body: bytes | UploadBody = b"",
             expect: Sequence[int] = (200,), ends: float | None = None,
             unsigned: Mapping[str, str] | None = None) -> httpx.Response:
        """Send a signed request; the streaming response when its status is expected, else the mapped
        DriverError. With `ends` (a time.monotonic()), no try starts after it and none waits beyond it.
        `unsigned`: headers outside the string to sign (Accept-Encoding)."""
        path = self.path(name)
        url = f"{self.scheme}://{self.host}{path}"
        if query:
            url += "?" + "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in query)
        skew_fixed = False
        resent = False  # sent again after its connection failed: it may have been done the first time
        attempt = 0
        while True:
            attempt += 1
            left = None if ends is None else ends - time.monotonic()
            if left is not None and left <= 0:
                raise _too_slow()
            sent = {key.lower(): str(value) for key, value in (headers or {}).items()}
            sent["x-ms-date"] = email.utils.formatdate(self.now(), usegmt=True)
            sent["x-ms-version"] = API_VERSION
            if isinstance(body, UploadBody):
                content: Any = body.chunks()
                sent["content-length"] = str(body.size)
            else:
                content = body or None
                if body or method in ("PUT", "POST"):
                    sent["content-length"] = str(len(body))
            sent["authorization"] = (f"SharedKey {self.account}:"
                                     f"{signature(self._key, string_to_sign(method, self.account, path, query, sent))}")
            sent.update(unsigned or {})
            timeout = {} if left is None else {
                "timeout": httpx.Timeout(min(http.TIMEOUT, left), connect=min(http.CONNECT_TIMEOUT, left))}
            started = time.monotonic()
            try:
                request = self._http.build_request(method, url, headers=sent, content=content, **timeout)
                response = self._http.send(request, stream=True)
            except httpx.TransportError as exc:
                self.log.warning("Azure %s %s: %s (try %d)", method, self._what(name), type(exc).__name__, attempt)
                if ends is not None and time.monotonic() >= ends:
                    raise _too_slow() from None
                if attempt < self.attempts and self._rewind(body):
                    self._pause(attempt)
                    resent = True
                    continue
                raise BackendError(gettext("Connecting to the storage failed (%(error)s): %(detail)s") % {"error": type(exc).__name__, "detail": str(exc)[:200] or gettext("no response")}) from exc
            self.log.debug("Azure %s %s -> %s (%.0f ms)", method, self._what(name), response.status_code,
                           (time.monotonic() - started) * 1000)
            if response.status_code in expect or (resent and method == "DELETE" and response.status_code == 404):
                return response  # a DELETE sent again that finds nothing: the first one was done, its answer lost
            with http.cut_off(response, ends):
                error = self._error(response, method)
            server = _http_date(response.headers.get("date"))
            if (response.status_code == 403 and not skew_fixed and server is not None
                    and abs(server.timestamp() - self.now()) > SKEW_SECONDS and self._rewind(body)):
                self.clock_offset = server.timestamp() - time.time()
                self.log.warning("Azure clock skew: our clock is off by %.0f s", -self.clock_offset)
                skew_fixed = True
                continue
            if ((response.status_code in _RETRY_STATUS or getattr(error, "code", "") in _RETRY_CODES)
                    and attempt < self.attempts and self._rewind(body)):
                self.log.warning("Azure %s %s: HTTP %s %s (try %d)", method, self._what(name), response.status_code,
                                 getattr(error, "code", ""), attempt)
                self._pause(attempt)
                continue
            raise error

    def _what(self, name: str | None) -> str:
        return f"{self.container}/{name}" if name is not None else self.container

    @staticmethod
    def _rewind(body: bytes | UploadBody) -> bool:
        return body.rewind() if isinstance(body, UploadBody) else True

    @staticmethod
    def _pause(attempt: int) -> None:
        time.sleep(0.2 * 2 ** (attempt - 1) * random.uniform(0.5, 1.5))

    def _error(self, response: httpx.Response, method: str) -> DriverError:
        """The error of an answer: the code from x-ms-error-code (HEAD answers have no body) or the XML body."""
        body = b""
        try:
            if method != "HEAD":
                body = read_limited(response, MAX_ERROR_BODY)
        except (httpx.HTTPError, BackendError):
            body = b""
        finally:
            http.drain(response)  # a HEAD's, not read above
        code, message = response.headers.get("x-ms-error-code", ""), ""
        if body.lstrip().startswith(b"<"):
            try:
                root = parse_xml(body)
            except BackendError:
                root = None
            if root is not None and root.tag == "Error":
                code, message = _text(root, "Code") or code, _text(root, "Message")
        error = azure_error(response.status_code, code, message, container=self.container)
        level = logging.DEBUG if response.status_code == 404 else logging.INFO
        self.log.log(level, "Azure error: HTTP %s %s", response.status_code, code)
        return error

    # -- blobs

    def head(self, name: str) -> BlobProps | None:
        """Get Blob Properties; None when there is no such blob."""
        try:
            return _props(self.request("HEAD", name).headers)
        except NotFound:
            return None

    def get(self, name: str, first: int = 0, last: int | None = None) -> httpx.Response:
        """A streaming Get Blob from byte `first` to `last` (inclusive; None: to the end)."""
        headers = {"range": f"bytes={first}-{'' if last is None else last}"} if first or last is not None else {}
        return self.send("GET", name, headers=headers, expect=(200, 206), unsigned={"accept-encoding": "identity"})

    def put_blob(self, name: str, body: bytes | UploadBody, *, content_type: str | None = None,
                 metadata: Mapping[str, str] | None = None) -> None:
        """Put Blob (a block blob); the Content-MD5 Azure computed is compared with what was sent."""
        headers = {"x-ms-blob-type": "BlockBlob", **{f"x-ms-meta-{key}": value for key, value in (metadata or {}).items()}}
        if content_type:
            headers["content-type"] = content_type
        reply = self.request("PUT", name, headers=headers, body=body, expect=(201,))
        returned = reply.headers.get("content-md5")
        if returned:
            sent = body.md5.digest() if isinstance(body, UploadBody) else hashlib.md5(body).digest()
            if base64.b64encode(sent).decode() != returned.strip():
                raise BackendError(gettext("The check after uploading failed: what the storage got differs from what was sent (MD5 mismatch). Please try again."))

    def put_marker(self, name: str) -> None:
        """A folder's marker: "name/", zero-length, hdi_isfolder=true (a directory in a Data Lake account)."""
        self.put_blob(name + "/", b"", metadata={FOLDER_MARK: "true"})

    def put_block(self, name: str, block_id: str, body: UploadBody) -> None:
        self.request("PUT", name, query=[("comp", "block"), ("blockid", block_id)], body=body, expect=(201,))

    def put_block_list(self, name: str, block_ids: Sequence[str], *, content_type: str | None = None) -> None:
        body = ('<?xml version="1.0" encoding="utf-8"?><BlockList>'
                + "".join(f"<Latest>{xml_escape(block_id)}</Latest>" for block_id in block_ids)
                + "</BlockList>").encode()
        headers = {"x-ms-blob-content-type": content_type} if content_type else {}
        self.request("PUT", name, query=[("comp", "blocklist")], headers=headers, body=body, expect=(201,),
                     takes_long=True)

    def delete(self, name: str, *, missing_ok: bool = False) -> None:
        """Delete Blob; a blob with snapshots goes with them (x-ms-delete-snapshots, which Azure wants then)."""
        try:
            try:
                self.request("DELETE", name, expect=(202,))
            except DriverError as error:
                if getattr(error, "code", "") != "SnapshotsPresent":
                    raise
                self.request("DELETE", name, headers={"x-ms-delete-snapshots": "include"}, expect=(202,))
        except NotFound:
            if not missing_ok:
                raise

    def copy(self, source: str, target: str) -> None:
        """Copy Blob inside the account. A pending copy is polled until it ends, for copy_seconds at most;
        then it is aborted. A copy that failed, or was given up, leaves no target behind."""
        source_url = self.url(source)
        if len(source_url) > MAX_COPY_SOURCE:
            raise DriverError(gettext("The path is too long for Azure to copy the file: the address of its source can have at most 2 KiB."))
        deadline = time.monotonic() + self.copy_seconds
        reply = self.request("PUT", target, headers={"x-ms-copy-source": source_url}, expect=(202,), ends=deadline)
        status, copy_id = reply.headers.get("x-ms-copy-status"), reply.headers.get("x-ms-copy-id")
        detail = None
        name = target.rpartition("/")[2]
        delay = self.copy_poll
        while status == "pending":
            if time.monotonic() + delay > deadline:
                self._give_up(target, copy_id)
                minutes = max(1, round(self.copy_seconds / 60))
                raise BackendError(ngettext("Azure didn't finish copying %(name)s in %(minutes)s minute, so the copy was cancelled. Please try again later.", "Azure didn't finish copying %(name)s in %(minutes)s minutes, so the copy was cancelled. Please try again later.", minutes) % {"name": name, "minutes": minutes})
            time.sleep(delay)
            delay = min(delay * 2, 5.0)
            props = self.head(target)
            if props is None or props.copy_id != copy_id:  # written, deleted or copied over meanwhile
                raise BackendError(gettext("%(name)s was changed by something else while Azure was copying it. Please check it and try again.") % {"name": name})
            status, detail = props.copy_status, props.copy_detail
        if status != "success":  # failed or aborted: Azure leaves an empty blob
            self.delete(target, missing_ok=True)
            shown = " ".join((detail or "").split())[:200] or status or "-"
            raise BackendError(gettext("Azure couldn't copy %(name)s: %(detail)s") % {"name": name, "detail": shown})

    def _give_up(self, target: str, copy_id: str | None) -> None:
        """Abort Copy Blob, then delete the empty target it leaves; failures are only logged."""
        try:
            self.request("PUT", target, query=[("comp", "copy"), ("copyid", copy_id or "")],
                         headers={"x-ms-copy-action": "abort"}, expect=(204,))
        except DriverError as exc:
            self.log.warning("Azure abort of copy %s failed: %s", copy_id, exc)
        try:
            self.delete(target, missing_ok=True)
        except DriverError as exc:
            self.log.warning("Azure delete of the cancelled copy %s failed: %s", target, exc)

    # -- listings

    def list_pages(self, prefix: str, *, delimiter: str | None = "/", limit: int | None = None
                   ) -> Iterator[tuple[list[BlobItem], list[str]]]:
        """Pages of List Blobs (blobs with their metadata, and BlobPrefix names), names decoded, until NextMarker
        is empty: a page may hold fewer than maxresults, or none, and still have a NextMarker."""
        marker = None
        while True:
            query = [("restype", "container"), ("comp", "list"), ("include", "metadata"),
                     ("maxresults", str(limit or self.page_size))]
            if prefix:
                query.append(("prefix", prefix))
            if delimiter:
                query.append(("delimiter", delimiter))
            if marker:
                query.append(("marker", marker))
            root = parse_xml(self.request("GET", None, query=query).content)
            blobs = root.find("Blobs")
            items: list[BlobItem] = []
            prefixes: list[str] = []
            for blob in blobs.findall("Blob") if blobs is not None else ():
                name = self._name(blob)
                if name is None or not name.startswith(prefix):
                    continue
                properties = blob.find("Properties")
                try:
                    size = int(_text(properties, "Content-Length") or 0)
                except ValueError:
                    raise BackendError(gettext("The storage's file list has a wrong file size.")) from None
                metadata = blob.find("Metadata")
                marked = metadata is not None and any(
                    isinstance(item.tag, str) and item.tag.lower() == FOLDER_MARK and (item.text or "").strip().lower() == "true"
                    for item in metadata)
                directory = _text(properties, "ResourceType").lower() == "directory" or marked
                items.append(BlobItem(name, size, _http_date(_text(properties, "Last-Modified")), directory))
            for common in blobs.findall("BlobPrefix") if blobs is not None else ():
                name = self._name(common)
                if name is not None and name.startswith(prefix):
                    prefixes.append(name)
            yield items, prefixes
            next_marker = _text(root, "NextMarker")
            if not next_marker:
                return
            if next_marker == marker:
                raise BackendError(gettext("The storage's file list pages are wrong (NextMarker didn't change)."))
            marker = next_marker

    def _name(self, element: ET.Element) -> str | None:
        """A Name, decoded when Azure percent-encoded it (Encoded="true": characters XML can't carry)."""
        node = element.find("Name")
        if node is None:
            return None
        text = node.text or ""
        if node.get("Encoded", "").lower() != "true":
            return text
        try:
            return unquote(text, errors="strict")
        except UnicodeDecodeError:
            self.log.debug("Azure blob name that isn't UTF-8 left out: %r", text)
            return None

    def has_prefix(self, prefix: str) -> bool:
        """Is any blob below prefix (a folder's marker counts)?"""
        for items, prefixes in self.list_pages(prefix, limit=1):
            if items or prefixes:
                return True
        return False


# --- the driver -----------------------------------------------------------------------------------

_ACCOUNT = re.compile(r"[a-z0-9]{3,24}")
_CONTAINER = re.compile(r"[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){2,62}")
_AZURE_HOST = re.compile(r"([a-z0-9]+)(?:-secondary)?\.blob\.core\..+")
ENDPOINT_EXAMPLE = "https://mystorageaccount.blob.core.windows.net"


class AzureBlobDriver(StorageDriver):
    type = "azure-blob"
    label = gettext_lazy("Azure Blob Storage")
    params = (
        Param("endpoint", "Endpoint", placeholder=ENDPOINT_EXAMPLE,
              help=gettext_lazy("The storage account's Blob service endpoint, as \"Endpoints\" in the account's page "
                                "in the Azure portal shows it, like https://mystorageaccount.blob.core.windows.net "
                                "(Azure operated by 21Vianet in China: "
                                "https://mystorageaccount.blob.core.chinacloudapi.cn).")),
        Param("account_name", gettext_lazy("Storage account name"),
              help=gettext_lazy("The storage account's name, the first part of the endpoint's host name: 3 to 24 "
                                "lowercase letters and digits.")),
        Param("account_key", gettext_lazy("Account key"), kind="password", secret=True,
              help=gettext_lazy("key1 or key2 under \"Security + networking → Access keys\" in the storage "
                                "account's page in the Azure portal. The account's \"Allow storage account key "
                                "access\" (under Configuration) must be on.")),
        Param("container", gettext_lazy("Container"),
              help=gettext_lazy("A container of the account (\"Data storage → Containers\" in the Azure portal): 3 "
                                "to 63 lowercase letters, digits and hyphens.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in the container that is this storage source's root, like / or /folder1.")),
        Param("is_private", gettext_lazy("Private container"), kind="bool", required=False, default=True,
              help=gettext_lazy("Turn off only when the container can be read anonymously (the account's \"Allow "
                                "Blob anonymous access\" is on and the container's access level is Blob or "
                                "Container): download links are then plain addresses that don't expire. Otherwise "
                                "they carry a SAS signed with the account key, and expire.")),
        Param("domain", gettext_lazy("Download domain"), required=False, default="", placeholder="https://cdn.example.com",
              help=gettext_lazy("Optional, written as https://domain: a custom domain or a CDN (Azure Front Door, "
                                "say) whose origin is this storage account's Blob service endpoint. Download links "
                                "then use it, as https://domain/container/path; those of a private container keep "
                                "their SAS, so the CDN must pass the query string on to the origin.")),
    )
    can_range = True
    can_direct_download = True
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    # Sizes and waits (the tests make them small).
    page_size: ClassVar[int] = 5000  # List Blobs' maxresults, at most 5000
    single_put_limit: ClassVar[int] = 64 * MiB  # larger files go in blocks
    min_block_size: ClassVar[int] = 8 * MiB
    max_block_size: ClassVar[int] = 4000 * MiB
    max_blocks: ClassVar[int] = 50000
    copy_seconds: ClassVar[float] = 600.0  # how long a pending copy is waited for
    copy_poll: ClassVar[float] = 0.5  # the first wait between polls of a pending copy (doubled up to 5 s)
    workers: ClassVar[int] = 8  # parallel copies and deletes of a folder's blobs
    transport: ClassVar[httpx.BaseTransport | None] = None  # tests route requests to a mock

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        try:
            prefix: str | None = key_prefix(self.config.get("base_path"))
        except InvalidPath:
            prefix = None
        # Every operation refuses a broken base path: never fall back to the whole container.
        self._prefix = None if prefix is None or _ends_with_dot(prefix.rstrip("/")) else prefix
        self._client: BlobClient | None = None
        self._client_lock = threading.Lock()

    def close(self) -> None:
        with self._client_lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        if (message := check_origin(config.get("endpoint"), "Endpoint", ENDPOINT_EXAMPLE)):
            errors["endpoint"] = message
        account = config.get("account_name")
        if not isinstance(account, str) or not account.strip():
            errors["account_name"] = gettext("Please fill in %(label)s.") % {"label": cls._label("account_name")}
        elif not _ACCOUNT.fullmatch(account):
            errors["account_name"] = gettext("The storage account name has 3 to 24 lowercase letters and digits.")
        elif "endpoint" not in errors:
            host = parse_origin(config["endpoint"])[1].lower()
            match = _AZURE_HOST.fullmatch(host.rsplit(":", 1)[0] if not host.startswith("[") else host)
            if match and match.group(1) != account:
                errors["endpoint"] = gettext("The Endpoint belongs to the storage account %(other)s, not %(account)s.") % {"other": match.group(1), "account": account}
        key = config.get("account_key")
        if not isinstance(key, str) or not key.strip():
            errors["account_key"] = gettext("Please fill in %(label)s.") % {"label": cls._label("account_key")}
        elif not cls._decode_key(key):
            errors["account_key"] = gettext("The account key isn't valid Base64. Please copy key1 or key2 again from the storage account's \"Access keys\".")
        container = config.get("container")
        if not isinstance(container, str) or not container.strip():
            errors["container"] = gettext("Please fill in %(label)s.") % {"label": cls._label("container")}
        elif not (_CONTAINER.fullmatch(container) or container == "$web"):  # $web: the static website's container
            errors["container"] = gettext("Invalid container name: it has 3 to 63 lowercase letters, digits and hyphens, and starts and ends with a letter or digit.")
        errors.update(check_base_path_and_domain(config))
        if "base_path" not in errors and _ends_with_dot(paths.clean(config.get("base_path") or "/")):
            errors["base_path"] = gettext("Azure Blob Storage can't have names that end with a dot.")
        return errors

    @classmethod
    def _label(cls, key: str) -> str:
        return next(str(param.label) for param in cls.params if param.key == key)

    @staticmethod
    def _decode_key(value: str) -> bytes | None:
        try:
            return base64.b64decode(value.strip(), validate=True) or None
        except (binascii.Error, ValueError):
            return None

    # -- requests

    def _blob(self) -> BlobClient:
        client = self._client
        if client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._make_client()
                client = self._client
        return client

    def _make_client(self) -> BlobClient:
        endpoint = str(self.config.get("endpoint") or "")
        try:
            parse_origin(endpoint)
        except ValueError:
            raise BackendError(gettext("The storage source's Endpoint is set wrong. Please ask the administrator to check it.")) from None
        key = self._decode_key(str(self.config.get("account_key") or ""))
        if key is None:
            raise AuthError(gettext("The account key isn't valid Base64. Please copy key1 or key2 again from the storage account's \"Access keys\"."))
        return BlobClient(endpoint=endpoint, account=str(self.config.get("account_name") or ""), key=key,
                          container=str(self.config.get("container") or ""), page_size=self.page_size,
                          copy_seconds=self.copy_seconds, copy_poll=self.copy_poll, transport=self.transport,
                          logger=self.log)

    # -- paths

    def _base_prefix(self) -> str:
        if self._prefix is None:
            raise InvalidPath(gettext("The storage source's base path is set wrong. Please ask the administrator to check it."))
        return self._prefix

    def _key(self, path: str) -> str:
        if path == "/" or not paths.is_clean(path):
            raise InvalidPath(gettext("Invalid path."))
        key = self._base_prefix() + path[1:]
        try:
            key.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate
            raise InvalidPath(gettext("The path has characters that can't be encoded.")) from None
        if len(key) > MAX_NAME_CHARS:
            raise InvalidPath(ngettext("The path is too long: a full blob name is at most %(limit)s character.", "The path is too long: a full blob name is at most %(limit)s characters.", MAX_NAME_CHARS) % {"limit": MAX_NAME_CHARS})
        if _ends_with_dot(key):
            raise InvalidPath(gettext("Azure Blob Storage can't have names that end with a dot."))
        return key

    def _folder_prefix(self, path: str) -> str:
        return self._base_prefix() if path == "/" else self._key(path) + "/"

    def _exists(self, path: str) -> bool:
        return self._blob().head(self._key(path)) is not None or self._blob().has_prefix(self._key(path) + "/")

    # -- reading

    def list(self, path: str) -> list[Entry]:
        prefix = self._folder_prefix(path)
        client = self._blob()
        entries: list[Entry] = []
        folders: set[str] = set()
        anything = False  # blobs there, a marker or names no storage path can address: the folder exists
        try:
            for items, prefixes in client.list_pages(prefix):
                anything = anything or bool(items or prefixes)
                for item in items:
                    name = item.name[len(prefix):]
                    if name == "" or not _listable(name):  # "" is this folder's own marker
                        continue
                    if item.directory:
                        if name not in folders:
                            folders.add(name)
                            entries.append(Entry(name, paths.join(path, name), True))
                    else:
                        entries.append(Entry(name, paths.join(path, name), False, item.size, item.modified))
                for common in prefixes:
                    name = common[len(prefix):-1]
                    if _listable(name) and name not in folders:
                        folders.add(name)
                        entries.append(Entry(name, paths.join(path, name), True))
        except DriverError as error:
            # A Data Lake account refuses a prefix that runs through a file: the path may be one.
            if path != "/" and getattr(error, "status", 0) in (400, 409):
                props = client.head(self._key(path))
                if props is not None and not props.directory:
                    raise NotADirectory() from None
            raise
        if not anything and path != "/":
            props = client.head(self._key(path))  # a Data Lake directory or a stub lists nothing when empty
            if props is None:
                raise NotFound()
            if not props.directory:
                raise NotADirectory()
        return entries

    def stat(self, path: str) -> Entry:
        if path == "/":
            return Entry("", "/", True)
        key = self._key(path)
        props = self._blob().head(key)
        if props is not None:
            if props.directory:
                return Entry(paths.name(path), path, True)
            return Entry(paths.name(path), path, False, props.size, props.modified)
        if self._blob().has_prefix(key + "/"):
            return Entry(paths.name(path), path, True)
        raise NotFound()

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        if path == "/":
            raise NotFound()
        key = self._key(path)
        if length == 0:
            props = self._blob().head(key)
            if props is None or props.directory:
                raise NotFound()
            return ReadStream(iter(()), size=0, total=props.size, offset=offset, content_type=props.content_type,
                              etag=props.etag, modified=props.modified)
        last = None if length is None else offset + length - 1
        response = self._blob().get(key, offset, last)
        headers = response.headers
        if _is_directory(headers):
            http.drain(response)
            raise NotFound()
        try:
            size: int | None = int(headers.get("content-length", ""))
        except ValueError:
            size = None
        total = size
        chunks: Iterator[bytes] = response.iter_raw(CHUNK)
        if response.status_code == 206:
            match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+|\*)", headers.get("content-range", "").strip())
            if not match or int(match.group(1)) != offset or (last is not None and int(match.group(2)) != last):
                response.close()
                raise BackendError(gettext("The range the storage returned (Content-Range) is wrong."))
            total = int(match.group(3)) if match.group(3) != "*" else None
        elif offset or length is not None:  # the server ignored Range: skip and cut here
            size = length if length is not None else (total - offset if total is not None else None)
            chunks = slice_stream(chunks, offset, size)
        return ReadStream(guarded(chunks), size=size, total=total, offset=offset,
                          content_type=headers.get("content-type"), etag=headers.get("etag"),
                          modified=_http_date(headers.get("last-modified")), close=response.close)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        key = self._key(path)
        client = self._blob()
        domain = (self.config.get("domain") or "").strip()
        url = client.url(key, "{}://{}".format(*parse_origin(domain)) if domain else None)
        if not self.config.get("is_private", True):
            return url
        return url + "?" + client.sas_query(key, expires=expires, filename=filename)

    # -- writing

    def _block_size(self, size: int) -> int:
        block = max(self.min_block_size, math.ceil(size / self.max_blocks))
        block = math.ceil(block / MiB) * MiB
        if block > self.max_block_size:
            raise DriverError(gettext("The file is too large: Azure Blob Storage takes at most 190.7 TiB per file."))
        return block

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        key = self._key(path)
        client = self._blob()
        # The blob first: a Data Lake account refuses a listing whose prefix runs through a file.
        props = client.head(key)
        if (props is not None and props.directory) or (props is None and client.has_prefix(key + "/")):
            raise AlreadyExists()
        content_type = content_type_of(paths.name(path))
        if size <= self.single_put_limit:
            client.put_blob(key, UploadBody(data, size), content_type=content_type)
            return
        block_size = self._block_size(size)
        token = secrets.token_hex(8)  # this upload's own block ids: two uploads to one path don't mix blocks
        block_ids: list[str] = []
        left = size
        while left > 0:
            count = min(block_size, left)
            block_id = base64.b64encode(f"{token}-{len(block_ids):06d}".encode()).decode()  # one length for all
            client.put_block(key, block_id, UploadBody(data, count))
            block_ids.append(block_id)
            left -= count
        client.put_block_list(key, block_ids, content_type=content_type)

    def mkdir(self, path: str) -> None:
        if path == "/":
            raise AlreadyExists()
        key = self._key(path)
        if self._exists(path):
            raise AlreadyExists()
        self._blob().put_marker(key)

    def _parallel(self, work: Callable[[Any], None], items: Sequence[Any]) -> None:
        run_parallel(work, items, self.workers)

    def _delete_folders(self, names: Sequence[str], *, keep: Sequence[str] = ()) -> None:
        """Delete markers, stubs and Data Lake directories, deepest first: a Data Lake directory goes only
        when it is empty. Not those above a blob of `keep`, which stays where it is."""
        depths: dict[int, list[str]] = {}
        for name in names:
            if not any(kept.startswith(name.rstrip("/") + "/") for kept in keep):
                depths.setdefault(name.rstrip("/").count("/"), []).append(name)
        client = self._blob()
        for depth in sorted(depths, reverse=True):
            self._parallel(lambda name: client.delete(name, missing_ok=True), depths[depth])

    @staticmethod
    def _stuck(client: BlobClient, prefix: str) -> str | None:
        """The first blob below `prefix` with a "." or ".." part, which another program made: httpx resolves those in
        a URL's path, while Shared Key signs it as written, so no request can name it (as s3.has_dot_segment)."""
        for items, _ in client.list_pages(prefix, delimiter=None):
            for item in items:
                if has_dot_segment(item.name[len(prefix):]):
                    return item.name
        return None

    def delete(self, path: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The storage source's root folder can't be deleted."))
        key = self._key(path)
        client = self._blob()
        props = client.head(key)
        if props is not None and not props.directory:
            client.delete(key)
            return
        stuck = self._stuck(client, key + "/")
        if stuck is not None:  # refused before anything is deleted
            raise DriverError(gettext("%(name)s has a . or .. part in its name, which can't be sent to Azure in an "
                                      "address, so this folder isn't deleted. Please delete that blob in the Azure "
                                      "portal first.") % {"name": stuck})
        found = props is not None
        folders: list[str] = []
        for items, _ in client.list_pages(key + "/", delimiter=None):
            found = found or bool(items)
            self._parallel(lambda item: client.delete(item.name, missing_ok=True), [item for item in items if not item.folder])
            folders.extend(item.name for item in items if item.folder)
        if not found:
            raise NotFound()
        self._delete_folders(folders + ([key] if props is not None else []))

    def _transfer(self, source: str, target: str, *, move: bool) -> None:
        client = self._blob()
        source_key, target_key = self._key(source), self._key(target)
        props = client.head(source_key)
        if props is not None and not props.directory:
            if self._exists(target):
                raise AlreadyExists()
            client.copy(source_key, target_key)
            if move:
                client.delete(source_key)
            return
        source_prefix, target_prefix = source_key + "/", target_key + "/"
        pages = (items for items, _ in client.list_pages(source_prefix, delimiter=None) if items)  # skip empty pages
        items = next(pages, [])
        if props is None and not items:
            raise NotFound()
        if self._exists(target):
            raise AlreadyExists()
        if props is not None:  # the folder's own directory or stub
            client.put_marker(target_key)
        folders: list[str] = []
        kept: list[str] = []
        while items:
            # Names with a "." or ".." part can't be asked for (_stuck): they stay where they are.
            stuck = [item for item in items if has_dot_segment(item.name[len(source_prefix):])]
            if stuck:
                self.log.warning("Azure: %d blobs below %r with a . or .. part are left where they are, e.g. %r",
                                 len(stuck), source_prefix, stuck[0].name)
                kept.extend(item.name for item in stuck)
                items = [item for item in items if item not in stuck]
            # Folders come before what is in them in a listing: their markers first (Data Lake directories).
            for item in items:
                if item.folder:
                    client.put_marker((target_prefix + item.name[len(source_prefix):]).rstrip("/"))
            files = [item for item in items if not item.folder]
            self._parallel(lambda item: client.copy(item.name, target_prefix + item.name[len(source_prefix):]), files)
            if move:
                self._parallel(lambda item: client.delete(item.name, missing_ok=True), files)
            folders.extend(item.name for item in items if item.folder)
            items = next(pages, [])
        if move:
            self._delete_folders(folders + ([source_key] if props is not None else []), keep=kept)

    def rename(self, path: str, new_name: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)
        if target != path:
            self._transfer(path, target, move=True)

    def _target_in(self, path: str, folder: str) -> str:
        if path == "/":
            raise InvalidPath(gettext("The root folder can't be moved or copied."))
        if folder != "/":
            key = self._key(folder)
            props = self._blob().head(key)
            if props is not None and not props.directory:
                raise NotADirectory(gettext("The target isn't a folder."))
            if props is None and not self._blob().has_prefix(key + "/"):
                raise NotFound(gettext("The target folder doesn't exist."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved or copied into itself."))
        return paths.join(folder, paths.name(path))

    def move(self, path: str, folder: str) -> None:
        target = self._target_in(path, folder)
        if target != path:
            self._transfer(path, target, move=True)

    def copy(self, path: str, folder: str) -> None:
        self._transfer(path, self._target_in(path, folder), move=False)
