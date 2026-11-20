"""S3 and S3-compatible object storage: AWS S3 and other S3 services, MinIO, Aliyun OSS, Tencent COS and Huawei OBS.
Qiniu (qiniu.py) and DogeCloud (dogecloud.py) build on S3Driver.

Paths
    A storage source is a key prefix in one bucket: base path "/photos" is the prefix "photos/", base path "/" the empty
    prefix. The file "/a/b.txt" is the object "photos/a/b.txt"; the folder "/a" is every key below "photos/a/". mkdir()
    writes a zero-byte marker object "photos/a/" so that empty folders exist; a folder whose marker is missing still
    shows while something is in it, and disappears with its last object (S3 has no real folders). Names never contain
    "/", so no storage path reaches a key outside the prefix; listed keys whose names can't be storage paths ("", ".",
    "..", NUL) are left out. Listings use ListObjectsV2 with the delimiter "/" and encoding-type=url (Tencent and
    Huawei: ListObjects with markers, as rclone does; any service answering V2 without a token is paged that way too).
    Keys that other programs made with "." or ".." segments are hidden, and folder copies leave them in place: a URL
    can't carry them, since httpx resolves such segments.

Requests
    Our own small client on httpx (no boto3): AWS Signature Version 4 in the Authorization header for API calls and in
    the query for presigned URLs. Upload bodies are sent as UNSIGNED-PAYLOAD with a Content-Length and checked against
    the returned ETag (MD5) where the service gives one; small XML bodies are hashed. Accept-Encoding goes unsigned:
    Google Cloud Storage's front end adds to it. Transient failures (connection errors, 5xx, SlowDown) are retried when
    the request body can be sent again; a RequestTimeTooSkewed answer corrects our clock. A call that reads its answer
    whole has CALL_SECONDS in all, retries included (not uploads, copies and the end of a multipart upload, which take
    as long as they take).

Addressing
    Virtual-hosted style ("bucket.endpoint/key") unless the type or the param says path style ("endpoint/bucket/key"),
    or the bucket name or endpoint can't be a host name (an IP address, "localhost", a bucket with "_", capitals, or
    dots over https): those use path style.

Params
    All types: endpoint ("https?://host[:port]"), access key and secret key, bucket, base path, public or private
    bucket, and an optional download domain (CDN or custom domain). S3 adds region, addressing style and skipping TLS
    verification; MinIO the region and TLS.

Changes
    Copies use CopyObject up to 5 GiB and UploadPartCopy above. Renaming, moving and copying a folder copies every
    object and then deletes the old ones: it takes a request per object and isn't atomic. Folder deletion uses
    DeleteObjects in batches of 1000.

Downloads
    Download links (download_url). Private bucket: a presigned GET, valid for `expires` seconds (at most 7 days), with
    response-content-disposition so that the browser saves the file under its name. Public bucket: the plain object URL.
    A download domain replaces the scheme and host of the link: with virtual-hosted style it is a CDN or custom domain
    of the bucket and the link stays signed for the bucket's own host (the CDN must pull with that Host header and must
    not sign its own requests to the bucket); with path style it is the public address of the S3 server (a reverse proxy
    that keeps the Host header), and links and browser uploads are signed for it. Aliyun signs download links with OSS's
    own V4 query signature (V1 only where the endpoint doesn't name the region: OSS no longer takes V1 from accounts
    made since March 2025), which doesn't cover the host, so a custom domain bound to the bucket works for private
    buckets too. With Aliyun's internal endpoint (oss-<region>-internal), links and browser uploads use the public one.

Uploads
    One PUT up to 64 MiB, multipart uploads above (parts of 8 MiB or more, scaled to stay below 10,000 parts; objects up
    to 10,000 parts of 5 GiB, 48.8 TiB). Browser uploads (upload_target): a presigned PUT valid for an hour, with
    Content-Type and Content-Length signed, up to 5 GiB. The bucket must allow the site's origin (CORS); the helper
    "set_cors" adds such a rule (MinIO has no CORS per bucket: its server has one setting).

Limits
    There is no server-side search.
"""

from __future__ import annotations

import base64
import concurrent.futures
import dataclasses
import datetime
import email.utils
import hashlib
import hmac
import ipaddress
import logging
import math
import random
import re
import threading
import time
import xml.etree.ElementTree as ET
from xml.parsers import expat
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from typing import Any, BinaryIO, ClassVar
from urllib.parse import quote, unquote, unquote_plus, urlsplit
from xml.sax.saxutils import escape as xml_escape

import httpx

from django.utils.text import format_lazy
from django.utils.translation import gettext, gettext_lazy, ngettext, pgettext_lazy

from . import filetypes, http, paths
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

KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
MAX_PRESIGN_SECONDS = 7 * 24 * 3600  # SigV4 query signatures last at most a week
MAX_KEY_BYTES = 1024

CHUNK = 256 * KiB  # downloads are streamed in pieces of this size
MAX_XML = 16 * MiB  # largest XML answer we read (a page of 1000 long keys is ~1.5 MB)
CALL_SECONDS = 60.0  # a call that reads its answer whole, tries again included (S3Client.request)
MAX_ERROR_BODY = 64 * KiB


# --- Signature Version 4 ------------------------------------------------------------------------

def uri_encode(text: str, *, keep_slash: bool = False) -> str:
    """SigV4 UriEncode: every byte but A-Z a-z 0-9 - . _ ~ as %XX (and "/" kept for keys)."""
    return quote(text, safe="/" if keep_slash else "")


def canonical_query(params: Iterable[tuple[str, str]]) -> str:
    """Names and values URI-encoded, sorted by name and then value."""
    pairs = sorted((uri_encode(name), uri_encode(value)) for name, value in params)
    return "&".join(f"{name}={value}" for name, value in pairs)


def signing_key(secret_key: str, date: str, region: str, service: str = "s3") -> bytes:
    key = hmac.new(("AWS4" + secret_key).encode(), date.encode(), hashlib.sha256).digest()
    for part in (region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return key


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """(canonical header lines, signed header names) of headers whose names are lowercase."""
    names = sorted(headers)
    lines = "".join(f"{name}:{' '.join(str(headers[name]).split())}\n" for name in names)
    return lines, ";".join(names)


def canonical_request(method: str, path: str, query: str, headers: Mapping[str, str], payload_hash: str) -> str:
    lines, signed = _canonical_headers(headers)
    return "\n".join([method, path, query, lines, signed, payload_hash])


def string_to_sign(amz_date: str, scope: str, request: str) -> str:
    return "\n".join([ALGORITHM, amz_date, scope, hashlib.sha256(request.encode()).hexdigest()])


@dataclasses.dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    token: str | None = None  # temporary credentials (DogeCloud)
    expires_at: float | None = None  # Unix time, for temporary credentials

    def __repr__(self) -> str:  # never show the secret
        return f"Credentials(access_key={self.access_key!r})"


def sign_headers(method: str, host: str, path: str, query: Sequence[tuple[str, str]],
                 headers: Mapping[str, str], payload_hash: str, credentials: Credentials,
                 region: str, now: datetime.datetime) -> dict[str, str]:
    """The headers of a request signed in the Authorization header.

    `path` is the URI-encoded path as sent; `query` the (name, value) pairs not yet encoded;
    every header in `headers` is signed. The answer has lowercase names and includes host,
    x-amz-date, x-amz-content-sha256 (and x-amz-security-token) and authorization."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    signed = {name.lower(): str(value) for name, value in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    if credentials.token:
        signed["x-amz-security-token"] = credentials.token
    request = canonical_request(method, path, canonical_query(query), signed, payload_hash)
    key = signing_key(credentials.secret_key, amz_date[:8], region)
    signature = hmac.new(key, string_to_sign(amz_date, scope, request).encode(), hashlib.sha256).hexdigest()
    signed["authorization"] = (f"{ALGORITHM} Credential={credentials.access_key}/{scope}, "
                               f"SignedHeaders={_canonical_headers(signed)[1]}, Signature={signature}")
    return signed


def presign(method: str, scheme: str, host: str, path: str, query: Sequence[tuple[str, str]],
            credentials: Credentials, region: str, now: datetime.datetime, expires: int,
            headers: Mapping[str, str] | None = None) -> str:
    """A presigned URL (SigV4 in the query). `headers` are signed too: the client must send
    them exactly (e.g. Content-Type and Content-Length of an upload)."""
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{amz_date[:8]}/{region}/s3/aws4_request"
    signed = {name.lower(): str(value) for name, value in (headers or {}).items()}
    signed["host"] = host
    params = list(query) + [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{credentials.access_key}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires)),
        ("X-Amz-SignedHeaders", _canonical_headers(signed)[1]),
    ]
    if credentials.token:
        params.append(("X-Amz-Security-Token", credentials.token))
    query_string = canonical_query(params)
    request = canonical_request(method, path, query_string, signed, UNSIGNED_PAYLOAD)
    key = signing_key(credentials.secret_key, amz_date[:8], region)
    signature = hmac.new(key, string_to_sign(amz_date, scope, request).encode(), hashlib.sha256).hexdigest()
    return f"{scheme}://{host}{path}?{query_string}&X-Amz-Signature={signature}"


# --- Aliyun OSS's own signatures ------------------------------------------------------------------

_OSS_REGION = re.compile(r"(?:^|\.)oss-([a-z0-9-]+?)(?:-internal)?\.aliyuncs\.com$")


def oss_region(endpoint: str) -> str | None:
    """The region ID an OSS endpoint names (https://oss-cn-hangzhou.aliyuncs.com, its -internal form, or
    s3.oss-cn-hangzhou...), for V4 signatures; None for one that doesn't (oss-accelerate)."""
    try:
        host = (urlsplit(endpoint.strip()).hostname or "").lower()
    except ValueError:
        return None
    match = _OSS_REGION.search(host)
    if match is None or match.group(1).startswith("accelerate"):
        return None
    return match.group(1)


def oss_v4_query(access_key: str, secret_key: str, method: str, bucket: str, key: str, when: datetime.datetime,
                 expires: int, region: str, query: Sequence[tuple[str, str]] = (),
                 headers: Mapping[str, str] | None = None) -> list[tuple[str, str]]:
    """The query of a link signed with OSS's V4 signature (OSS4-HMAC-SHA256, "在URL中包含V4签名"): `query` and
    the x-oss-* parameters with x-oss-signature, each encoded as it goes into the URL. The host isn't signed, so
    a custom domain bound to the bucket serves the link too. `headers`: the request's own signed headers
    (content-type, content-md5, x-oss-*), lower case; a browser's GET has none."""
    stamp = when.astimezone(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    date = stamp[:8]
    scope = f"{date}/{region}/oss/aliyun_v4_request"
    pairs = [*query, ("x-oss-signature-version", "OSS4-HMAC-SHA256"), ("x-oss-credential", f"{access_key}/{scope}"),
             ("x-oss-date", stamp), ("x-oss-expires", str(expires))]
    encoded = sorted((quote(name, safe=""), quote(value, safe="")) for name, value in pairs)
    canonical_query = "&".join(f"{name}={value}" if value else name for name, value in encoded)
    canonical_headers = "".join(f"{name}:{value.strip()}\n" for name, value in sorted((headers or {}).items()))
    canonical = (f"{method}\n{quote(f'/{bucket}/{key}', safe='/')}\n{canonical_query}\n{canonical_headers}\n\n"
                 "UNSIGNED-PAYLOAD")
    to_sign = f"OSS4-HMAC-SHA256\n{stamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}"
    signing_key = ("aliyun_v4" + secret_key).encode()
    for part in (date, region, "oss", "aliyun_v4_request"):
        signing_key = hmac.new(signing_key, part.encode(), hashlib.sha256).digest()
    return encoded + [("x-oss-signature", hmac.new(signing_key, to_sign.encode(), hashlib.sha256).hexdigest())]


def oss_v1_signature(secret_key: str, method: str, bucket: str, key: str, expires_at: int,
                     subresources: Sequence[tuple[str, str]] = ()) -> str:
    """OSS's V1 query signature: base64(HMAC-SHA1(secret, method\\n\\n\\nexpires\\n/bucket/key[?sub])).
    Sub-resources (response-* overrides) are sorted and not encoded in the string to sign."""
    resource = f"/{bucket}/{key}"
    if subresources:
        resource += "?" + "&".join(f"{name}={value}" if value else name for name, value in sorted(subresources))
    message = f"{method}\n\n\n{expires_at}\n{resource}"
    return base64.b64encode(hmac.new(secret_key.encode(), message.encode(), hashlib.sha1).digest()).decode()


# --- small helpers --------------------------------------------------------------------------------

def parse_origin(value: str) -> tuple[str, str]:
    """(scheme, host[:port]) of "https?://host[:port]", the default port left out, as in a Host
    header. ValueError when it isn't one."""
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("scheme")
    if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
        raise ValueError("extra")
    if parts.path not in ("", "/"):
        raise ValueError("path")
    host = parts.hostname
    if ":" in host:  # IPv6
        ipaddress.IPv6Address(host)
        host = f"[{host}]"
    elif not re.fullmatch(r"[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?(\.[a-z0-9_]([a-z0-9_-]*[a-z0-9_])?)*", host):
        # Underscores: Docker's DNS resolves service names like "my_minio".
        raise ValueError("host")
    port = parts.port  # ValueError when malformed
    if port is not None and port != (443 if parts.scheme == "https" else 80):
        host = f"{host}:{port}"
    return parts.scheme, host


def check_origin(value: Any, what: str, example: str) -> str | None:
    """None when value is "https?://host[:port]", else a message for the form."""
    if not isinstance(value, str) or not value.strip():
        return gettext("Please fill in %(what)s.") % {"what": what}
    try:
        parse_origin(value)
    except ValueError:
        return gettext("%(what)s should be http(s)://host[:port], without a path or query, like %(example)s.") % {"what": what, "example": example}
    return None


def check_key(value: Any, label: str) -> str | None:
    """A key or name sent in a header (AccessKey, operator): printable ASCII without spaces."""
    if not isinstance(value, str) or not value.strip():
        return gettext("Please fill in %(label)s.") % {"label": label}
    if not value.isascii() or not value.isprintable() or " " in value:
        return gettext("%(label)s can only have English letters, digits and symbols, without spaces.") % {"label": label}
    return None


def check_base_path_and_domain(config: Mapping[str, Any]) -> dict[str, str]:
    errors = {}
    try:
        paths.clean(config.get("base_path") or "/")
    except InvalidPath as exc:
        errors["base_path"] = exc.message
    domain = config.get("domain")
    if domain and (message := check_origin(domain, gettext("Download domain"), "https://cdn.example.com")):
        errors["domain"] = message
    return errors


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return False
    return True


def _virtual_hostable(bucket: str, scheme: str, host: str) -> bool:
    """Can the bucket be the first label of the host name (bucket.endpoint)?"""
    hostname = host[1:host.index("]")] if host.startswith("[") else host.rsplit(":", 1)[0]
    if _is_ip(hostname) or hostname == "localhost" or hostname.endswith(".localhost"):
        return False
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket) or ".." in bucket:
        return False
    # A bucket with dots doesn't match the provider's wildcard TLS certificate.
    return not ("." in bucket and scheme == "https")


def content_type_of(name: str) -> str:
    return filetypes.guess_type(name, strict=False)[0] or "application/octet-stream"


def content_disposition(filename: str) -> str:
    """attachment with an ASCII fallback and the UTF-8 name (RFC 6266)."""
    fallback = "".join(ch if 0x20 <= ord(ch) < 0x7F and ch not in '"\\%' else "_" for ch in filename)
    return f"attachment; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{quote(filename, safe='')}"


def replace_origin(url: str, domain: str) -> str:
    """url with its scheme and host taken from domain ("https://cdn.example.com")."""
    scheme, host = parse_origin(domain)
    parts = urlsplit(url)
    rest = url[len(f"{parts.scheme}://{parts.netloc}"):]
    return f"{scheme}://{host}{rest}"


def _http_date(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def _iso_date(value: str | None) -> datetime.datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=datetime.timezone.utc)


def has_dot_segment(key: str) -> bool:
    """Has the key a "." or ".." segment? httpx (like browsers) resolves those in a URL's path,
    so such a key, made by another program, can't be sent in a URL as it is: it would reach a
    different key, even outside the prefix."""
    return any(part in (".", "..") for part in key.split("/"))


def _listable(name: str) -> bool:
    """A listed name that a storage path can address."""
    return not paths.dots_only(name) and "\x00" not in name and "/" not in name  # D46: "", ".", "...", ".. "


_XML_INVALID = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")  # not allowed in XML 1.0


def _refuse_dtd(*args: Any) -> None:
    raise BackendError(gettext("The XML from the storage has a DTD, so it isn't parsed."))


def parse_xml(body: bytes) -> ET.Element:
    """An S3 XML answer, namespaces removed. No DTDs: S3 never sends one, and refusing them
    rules out entity expansion; expat finds them in whatever encoding the document declares."""
    checker = expat.ParserCreate()
    checker.StartDoctypeDeclHandler = _refuse_dtd
    checker.EntityDeclHandler = _refuse_dtd
    try:
        checker.Parse(body, True)
        root = ET.fromstring(body)
    # LookupError: an encoding Python doesn't know, which was a 500 (STOR-04).
    except (expat.ExpatError, ET.ParseError, ValueError, LookupError):
        raise BackendError(gettext("What the storage returned isn't valid XML.")) from None
    for element in root.iter():
        if isinstance(element.tag, str):
            element.tag = element.tag.rpartition("}")[2]
    return root


def _text(element: ET.Element, name: str) -> str:
    child = element.find(name)
    return (child.text or "") if child is not None else ""


def _too_slow() -> BackendError:
    return BackendError(ngettext("The storage didn't finish answering in %(seconds)s second. Please try again later.", "The storage didn't finish answering in %(seconds)s seconds. Please try again later.", CALL_SECONDS) % {"seconds": format(CALL_SECONDS, "g")})


def read_limited(response: httpx.Response, limit: int) -> bytes:
    data = bytearray()
    for chunk in response.iter_bytes():
        data += chunk
        if len(data) > limit:
            raise BackendError(gettext("What the storage returned is too large."))
    return bytes(data)


class UploadBody:
    """`size` bytes of a file object as a request body; sent again after a failure when the file
    can seek back. Keeps the MD5 of what was sent (S3 compares it with the ETag)."""

    def __init__(self, data: BinaryIO, size: int):
        self.data = data
        self.size = size
        self.md5 = hashlib.md5()
        try:
            self.start = data.tell() if data.seekable() else None
        except (AttributeError, OSError, ValueError):
            self.start = None

    def chunks(self) -> Iterator[bytes]:
        self.md5 = hashlib.md5()
        left = self.size
        while left > 0:
            chunk = self.data.read(min(left, MiB))
            if not chunk:
                raise DriverError(ngettext("The uploaded data is incomplete: %(missing)s byte short of the file size.", "The uploaded data is incomplete: %(missing)s bytes short of the file size.", left) % {"missing": left})
            chunk = bytes(chunk[:left])
            self.md5.update(chunk)
            left -= len(chunk)
            yield chunk

    def rewind(self) -> bool:
        if self.start is None:
            return False
        try:
            self.data.seek(self.start)
        except (OSError, ValueError):
            return False
        return True


@dataclasses.dataclass(frozen=True)
class S3Object:
    key: str
    size: int | None
    modified: datetime.datetime | None


@dataclasses.dataclass(frozen=True)
class S3Head:
    size: int
    modified: datetime.datetime | None
    etag: str | None
    content_type: str | None
    metadata: Mapping[str, str] = dataclasses.field(default_factory=dict)  # kept by copies


# Object headers a multipart copy has to set again (CopyObject keeps them itself).
_KEPT_HEADERS = ("content-type", "content-disposition", "content-encoding", "content-language",
                 "cache-control", "expires")


@dataclasses.dataclass(frozen=True)
class Reply:
    """A response that was read whole."""
    status: int
    headers: httpx.Headers
    content: bytes


def url_query(params: Iterable[tuple[str, str]]) -> str:
    """The query string as sent: the canonical one, with "name" alone for an empty value
    ("?uploads"), which servers canonicalise back to "uploads="."""
    pairs = sorted((uri_encode(name), uri_encode(value)) for name, value in params)
    return "&".join(f"{name}={value}" if value else name for name, value in pairs)


def guarded(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """A response body stream whose transport errors become BackendError."""
    try:
        yield from chunks
    except httpx.HTTPError as exc:
        raise BackendError(gettext("The connection broke while reading the storage's data (%(error)s).") % {"error": type(exc).__name__}) from exc


def run_parallel(work: Callable[[Any], Any], items: Sequence[Any], workers: int) -> None:
    """work(item) for every item in up to `workers` threads; the first error stops the rest
    (those not started yet) and is raised."""
    if len(items) <= 1 or workers <= 1:
        for item in items:
            work(item)
        return
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(workers, len(items))) as pool:
        futures = [pool.submit(work, item) for item in items]
        concurrent.futures.wait(futures, return_when=concurrent.futures.FIRST_EXCEPTION)
        for future in futures:
            future.cancel()  # only those not started yet
        for future in futures:
            if not future.cancelled() and future.done() and future.exception() is not None:
                raise future.exception()  # type: ignore[misc]


def key_prefix(base_path: Any) -> str:
    """The key prefix of a base path: "/" → "", "/a/b" → "a/b/". InvalidPath when it isn't one."""
    clean = paths.clean(base_path if base_path not in (None, "") else "/")
    return "" if clean == "/" else clean[1:] + "/"


def slice_stream(chunks: Iterator[bytes], skip: int, size: int | None) -> Iterator[bytes]:
    """chunks without the first `skip` bytes and cut after `size` bytes (None: all)."""
    for chunk in chunks:
        if skip:
            if len(chunk) <= skip:
                skip -= len(chunk)
                continue
            chunk, skip = chunk[skip:], 0
        if size is not None:
            if size <= 0:
                return
            chunk = chunk[:size]
            size -= len(chunk)
        yield chunk


# --- errors ---------------------------------------------------------------------------------------

_AUTH_HINTS = {
    "InvalidAccessKeyId": gettext_lazy("the AccessKey doesn't exist"),
    "SignatureDoesNotMatch": gettext_lazy("the signature is wrong; please check the SecretKey and the region"),
    "AccessDenied": gettext_lazy("these keys have no permission for this"),
    "AllAccessDisabled": gettext_lazy("access to this bucket has been blocked"),
    "AccountProblem": gettext_lazy("something is wrong with the account; please look in the provider's console"),
    "InvalidToken": gettext_lazy("the temporary key is invalid"),
    "ExpiredToken": gettext_lazy("the temporary key has expired"),
    "TokenRefreshRequired": gettext_lazy("the temporary key needs refreshing"),
    "RequestTimeTooSkewed": gettext_lazy("the server's clock is too far from the storage's; please correct the server's time"),
    "RequestExpired": gettext_lazy("the request has expired; please correct the server's time"),
}
_REGION_CODES = ("AuthorizationHeaderMalformed", "InvalidRegion", "IllegalLocationConstraintException")
_RETRY_CODES = ("SlowDown", "InternalError", "ServiceUnavailable", "RequestTimeout", "Throttling",
                "ThrottlingException", "RequestLimitExceeded", "TooManyRequests")
_RETRY_STATUS = (429, 500, 502, 503, 504)
# Temporary credentials the service no longer takes.
_EXPIRED_CODES = ("ExpiredToken", "InvalidToken", "TokenRefreshRequired", "InvalidAccessKeyId")
# .apk and .ipa files through the bucket's default domain: COS refuses them (403) for buckets created since 2024, OBS
# (400) for those created after 2023-12-30.
_DOWNLOAD_BANS = ("DownloadForbidden", "InsecureDownloadForbidden")


def s3_error(status: int, code: str = "", message: str = "", *, bucket: str = "",
             endpoint: str = "", region: str = "") -> DriverError:
    """The DriverError for an S3 error answer; `status` and `code` are kept on it."""
    detail = message.strip()[:200]
    if code == "NoSuchBucket":
        error: DriverError = BackendError(gettext("The bucket %(bucket)s doesn't exist (NoSuchBucket). Please check the bucket name in the storage source's settings.") % {"bucket": bucket})
    elif status == 404 and code in ("", "NoSuchKey", "NotFound", "404"):
        error = NotFound()
    elif status in (301, 307) or code in ("PermanentRedirect", "TemporaryRedirect"):
        where = endpoint or (gettext("region %(region)s") % {"region": region} if region else gettext("the Endpoint of the bucket's region"))
        error = BackendError(gettext("The bucket isn't at this Endpoint (%(code)s). Please use %(where)s instead.") % {"code": code or status, "where": where})
    elif code in _REGION_CODES:
        error = AuthError(gettext("Wrong region (%(code)s): %(detail)s") % {"code": code, "detail": detail or gettext("please check the region in the storage source's settings")})
    elif code in _DOWNLOAD_BANS:  # not a problem of the keys, which COS's 403 said
        error = DriverError(gettext("The storage doesn't allow downloading .apk and .ipa files through the bucket's default domain (%(code)s); a custom or CDN domain bound to the bucket is needed for them.") % {"code": code})
    elif status in (401, 403) or code in _AUTH_HINTS:
        hint = _AUTH_HINTS.get(code) or detail or gettext("please check the AccessKey, the SecretKey and the bucket's permissions")
        error = AuthError(gettext("The storage refused the request (%(code)s): %(hint)s.") % {"code": code or status, "hint": hint})
    elif code in ("SlowDown", "TooManyRequests") or status == 429:
        error = BackendError(gettext("The storage is busy (%(code)s). Please try again later.") % {"code": code or status})
    elif status == 501 or code == "NotImplemented":  # trying again won't help (MinIO's PutBucketCors, say)
        error = Unsupported()
    elif status >= 500:
        error = BackendError(gettext("The storage failed (HTTP %(status)s%(code)s). Please try again later.") % {"status": status, "code": " " + code if code else ""})
    elif code == "EntityTooLarge":
        error = DriverError(gettext("The file is too large for the storage (EntityTooLarge)."))
    elif code == "KeyTooLongError":
        error = InvalidPath(gettext("The path is too long for the storage (KeyTooLongError)."))
    else:
        shown = " " + code if code else ""
        if detail:
            error = BackendError(gettext("The storage refused the request (HTTP %(status)s%(code)s): %(detail)s") % {"status": status, "code": shown, "detail": detail})
        else:
            error = BackendError(gettext("The storage refused the request (HTTP %(status)s%(code)s).") % {"status": status, "code": shown})
    error.status = status  # type: ignore[attr-defined]
    error.code = code  # type: ignore[attr-defined]
    return error


# --- the client -----------------------------------------------------------------------------------

class S3Client:
    """A small S3 client for one bucket: signed requests with retries, and presigned URLs."""

    attempts = 3  # tries of a request that may be repeated

    def __init__(self, *, endpoint: str, bucket: str, region: str, credentials: Callable[[], Credentials],
                 path_style: bool = False, verify: bool = True, url_encoded_listing: bool = True,
                 url_encoded_tokens: bool = False, list_version: int = 2, page_size: int = 1000,
                 renew_credentials: Callable[[], None] | None = None,
                 transport: httpx.BaseTransport | None = None, logger: logging.Logger | None = None):
        self.scheme, self.host = parse_origin(endpoint)
        self.bucket = bucket
        self.region = region
        self.credentials = credentials
        # Temporary credentials: called when the service says they expired, before one more try.
        self.renew_credentials = renew_credentials
        self.path_style = path_style or not _virtual_hostable(bucket, self.scheme, self.host)
        self.url_encoded_listing = url_encoded_listing
        # The URL-encoded listing's NextContinuationToken is encoded too (OSS); AWS's isn't, nor Ceph's.
        self.url_encoded_tokens = url_encoded_tokens
        self.list_version = list_version  # 2: ListObjectsV2; 1: ListObjects with markers
        self.page_size = page_size
        self.log = logger or logging.getLogger("hfile.storage.s3")
        self.clock_offset = 0.0  # seconds to add to our clock, from a RequestTimeTooSkewed answer
        kwargs: dict[str, Any] = {"transport": transport} if transport is not None else {}
        self._http = http.client(verify=verify, **kwargs)

    def close(self) -> None:
        self._http.close()

    def now(self) -> datetime.datetime:
        return datetime.datetime.fromtimestamp(time.time() + self.clock_offset, datetime.timezone.utc)

    # -- addressing

    def target(self, key: str | None, *, service: bool = False, host: str | None = None) -> tuple[str, str]:
        """(host, URI-encoded path) of the service (ListBuckets), the bucket (key None) or an
        object. `host` replaces the endpoint's host (signing for a public address)."""
        endpoint = host or self.host
        if service:
            return endpoint, "/"
        if self.path_style:
            path = "/" + uri_encode(self.bucket)
            return endpoint, path + ("/" + uri_encode(key, keep_slash=True) if key is not None else "")
        if host is not None:  # a domain of the bucket itself
            return endpoint, "/" + (uri_encode(key, keep_slash=True) if key is not None else "")
        return f"{self.bucket}.{endpoint}", "/" + (uri_encode(key, keep_slash=True) if key is not None else "")

    def object_url(self, key: str) -> str:
        host, path = self.target(key)
        return f"{self.scheme}://{host}{path}"

    # -- requests

    def request(self, method: str, key: str | None = None, *, query: Sequence[tuple[str, str]] = (),
                headers: Mapping[str, str] | None = None, body: bytes | UploadBody = b"",
                expect: Sequence[int] = (200,), service: bool = False, takes_long: bool = False) -> Reply:
        """A signed request, its answer read whole (at most 16 MiB), in CALL_SECONDS at most, tries again included:
        httpx only limits each read and write, so a server answering slowly but steadily held a call (and the admin
        waiting on 测试连接 or a helper button) as long as it liked (STOR-07). Not an upload, which takes as long as its
        bytes do, nor a call that `takes_long`: a copy or the end of a multipart upload, which S3 may work on for
        minutes, sending spaces meanwhile."""
        ends = None if takes_long or isinstance(body, UploadBody) else time.monotonic() + CALL_SECONDS
        response = self.send(method, key, query=query, headers=headers, body=body, expect=expect, service=service,
                             ends=ends)
        try:
            with http.cut_off(response, ends):
                return Reply(response.status_code, response.headers, read_limited(response, MAX_XML))
        except httpx.HTTPError as exc:
            if ends is not None and time.monotonic() >= ends:
                raise _too_slow() from None
            raise BackendError(gettext("Reading the storage's answer failed (%(error)s).") % {"error": type(exc).__name__}) from exc
        finally:
            response.close()

    def send(self, method: str, key: str | None = None, *, query: Sequence[tuple[str, str]] = (),
             headers: Mapping[str, str] | None = None, body: bytes | UploadBody = b"",
             expect: Sequence[int] = (200,), service: bool = False, ends: float | None = None,
             unsigned: Mapping[str, str] | None = None) -> httpx.Response:
        """Send a signed request; the streaming response when its status is expected, else the
        mapped DriverError. Retries transient failures when the body can be sent again. With `ends`
        (a time.monotonic()), no try starts after it and none waits for its answer beyond it.
        `unsigned`: headers sent but not signed, for those that something on the way may change."""
        host, path = self.target(key, service=service)
        url = f"{self.scheme}://{host}{path}" + (f"?{url_query(query)}" if query else "")
        skew_fixed = renewed = False
        attempt = 0
        while True:
            attempt += 1
            left = None if ends is None else ends - time.monotonic()
            if left is not None and left <= 0:
                raise _too_slow()
            if isinstance(body, UploadBody):
                payload_hash, content = UNSIGNED_PAYLOAD, body.chunks()
                extra = {"content-length": str(body.size)}
            else:
                payload_hash = hashlib.sha256(body).hexdigest() if body else EMPTY_SHA256
                content, extra = (body or None), {}
            signed = sign_headers(method, host, path, query, headers or {}, payload_hash,
                                  self.credentials(), self.region, self.now())
            signed.update(extra)
            signed.update(unsigned or {})
            started = time.monotonic()
            timeout = {} if left is None else {
                "timeout": httpx.Timeout(min(http.TIMEOUT, left), connect=min(http.CONNECT_TIMEOUT, left))}
            try:
                request = self._http.build_request(method, url, headers=signed, content=content, **timeout)
                response = self._http.send(request, stream=True)
            except httpx.TransportError as exc:
                self.log.warning("S3 %s %s: %s (try %d)", method, self._what(key), type(exc).__name__, attempt)
                if ends is not None and time.monotonic() >= ends:
                    raise _too_slow() from None
                if attempt < self.attempts and self._rewind(body):
                    self._pause(attempt)
                    continue
                raise BackendError(gettext("Connecting to the storage failed (%(error)s): %(detail)s") % {"error": type(exc).__name__, "detail": str(exc)[:200] or gettext("no response")}) from exc
            self.log.debug("S3 %s %s -> %s (%.0f ms)", method, self._what(key), response.status_code,
                           (time.monotonic() - started) * 1000)
            if response.status_code in expect:
                return response
            with http.cut_off(response, ends):
                error = self._error(response, method)
            code = getattr(error, "code", "")
            server = _http_date(response.headers.get("date"))
            skewed = (server is not None and response.status_code == 403  # HEAD answers carry no code
                      and abs(server.timestamp() - time.time() - self.clock_offset) > 900)
            if (code in ("RequestTimeTooSkewed", "RequestExpired") or skewed) and not skew_fixed:
                if server is not None and self._rewind(body):
                    self.clock_offset = server.timestamp() - time.time()
                    self.log.warning("S3 clock skew: our clock is off by %.0f s", -self.clock_offset)
                    skew_fixed = True
                    continue
            if (code in _EXPIRED_CODES and self.renew_credentials is not None and not renewed
                    and self._rewind(body)):
                self.log.warning("S3 %s %s: %s, fetching new credentials", method, self._what(key), code)
                self.renew_credentials()
                renewed = True
                continue
            if ((response.status_code in _RETRY_STATUS or code in _RETRY_CODES)
                    and attempt < self.attempts and self._rewind(body)):
                self.log.warning("S3 %s %s: HTTP %s %s (try %d)", method, self._what(key),
                                 response.status_code, code, attempt)
                self._pause(attempt)
                continue
            raise error

    def _what(self, key: str | None) -> str:
        return f"{self.bucket}/{key}" if key is not None else self.bucket

    @staticmethod
    def _rewind(body: bytes | UploadBody) -> bool:
        return body.rewind() if isinstance(body, UploadBody) else True

    @staticmethod
    def _pause(attempt: int) -> None:
        time.sleep(0.2 * 2 ** (attempt - 1) * random.uniform(0.5, 1.5))

    def _error(self, response: httpx.Response, method: str) -> DriverError:
        body = b""
        try:
            if method != "HEAD":
                body = read_limited(response, MAX_ERROR_BODY)
        except (httpx.HTTPError, BackendError):
            body = b""
        finally:
            http.drain(response)  # a HEAD's, not read above
        code = message = endpoint = ""
        if body.lstrip().startswith(b"<"):
            try:
                root = parse_xml(body)
            except BackendError:
                root = None
            if root is not None and root.tag == "Error":
                code, message, endpoint = _text(root, "Code"), _text(root, "Message"), _text(root, "Endpoint")
        if endpoint and "://" not in endpoint:
            # AWS names the bucket's own host (bucket.s3.eu-central-1.amazonaws.com); the Endpoint setting is the
            # service's address, with the scheme.
            endpoint = f"{self.scheme}://{endpoint.removeprefix(self.bucket + '.')}"
        error = s3_error(response.status_code, code, message, bucket=self.bucket, endpoint=endpoint,
                         region=response.headers.get("x-amz-bucket-region", ""))
        level = logging.DEBUG if response.status_code == 404 else logging.INFO
        self.log.log(level, "S3 error: HTTP %s %s %s", response.status_code, code, message[:200])
        return error

    def xml(self, method: str, key: str | None = None, **kwargs: Any) -> ET.Element:
        response = self.request(method, key, **kwargs)
        root = parse_xml(response.content)
        if root.tag == "Error":  # CopyObject and CompleteMultipartUpload can fail with a 200
            raise s3_error(500 if _text(root, "Code") in _RETRY_CODES else 400, _text(root, "Code"),
                           _text(root, "Message"), bucket=self.bucket)
        return root

    # -- objects

    def head(self, key: str) -> S3Head | None:
        try:
            response = self.request("HEAD", key)
        except NotFound:
            return None
        try:
            size = int(response.headers.get("content-length", ""))
        except ValueError:
            raise BackendError(gettext("The storage gave no file size (Content-Length).")) from None
        metadata = {name: value for name, value in response.headers.items()
                    if name in _KEPT_HEADERS or name.startswith("x-amz-meta-")}
        return S3Head(size, _http_date(response.headers.get("last-modified")),
                      response.headers.get("etag"), response.headers.get("content-type"), metadata)

    def get(self, key: str, first: int = 0, last: int | None = None) -> httpx.Response:
        """A streaming GET from byte `first` to `last` (inclusive; None: to the end)."""
        headers: dict[str, str] = {}
        if first or last is not None:
            headers["range"] = f"bytes={first}-{'' if last is None else last}"
        # Google Cloud Storage's front end adds "gzip(gfe)" to Accept-Encoding: signed, it failed every GET there.
        return self.send("GET", key, headers=headers, expect=(200, 206), unsigned={"accept-encoding": "identity"})

    def put(self, key: str, body: bytes | UploadBody, *, content_type: str | None = None) -> None:
        headers = {"content-type": content_type} if content_type else {}
        response = self.request("PUT", key, headers=headers, body=body)
        self._check_etag(response, body)

    def _check_etag(self, response: Reply, body: bytes | UploadBody) -> None:
        """A plain ETag is the MD5 of the body: compare it, except with KMS or customer keys."""
        etag = (response.headers.get("etag") or "").strip('"').lower()
        encryption = response.headers.get("x-amz-server-side-encryption") or ""
        if (not re.fullmatch(r"[0-9a-f]{32}", etag) or encryption.startswith("aws:kms")
                or response.headers.get("x-amz-server-side-encryption-customer-algorithm")):
            return
        sent = body.md5.hexdigest() if isinstance(body, UploadBody) else hashlib.md5(body).hexdigest()
        if etag != sent:
            raise BackendError(gettext("The check after uploading failed: what the storage got differs from what was sent (MD5 mismatch). Please try again."))

    def delete(self, key: str) -> None:
        self.request("DELETE", key, expect=(200, 204))

    def delete_many(self, keys: Sequence[str]) -> None:
        """Delete up to 1000 keys in one request (DeleteObjects)."""
        plain = [key for key in keys if not _XML_INVALID.search(key)]
        stuck = [key for key in keys if _XML_INVALID.search(key) and has_dot_segment(key)]
        for key in keys:
            if _XML_INVALID.search(key) and key not in stuck:  # not expressible in XML 1.0: one DELETE each
                self.delete(key)
        if plain:
            self._delete_objects(plain)
        if stuck:  # neither XML nor a URL can carry them
            raise DriverError(ngettext("%(count)s object can't be deleted: its name has both a . "
                                       "or .. part and control characters (like %(example)s). "
                                       "Please delete it in the provider's console.", "%(count)s objects can't be deleted: their names have both a . or .. part and control characters (like %(example)s). Please delete them in the provider's console.", len(stuck)) % {"count": len(stuck), "example": repr(stuck[0])})

    def _delete_objects(self, plain: Sequence[str]) -> None:
        body = ("<Delete><Quiet>true</Quiet>"
                + "".join(f"<Object><Key>{xml_escape(key, {chr(13): '&#13;'})}</Key></Object>" for key in plain)
                + "</Delete>").encode()
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        root = self.xml("POST", None, query=[("delete", "")], body=body,
                        headers={"content-md5": md5, "content-type": "application/xml"})
        errors = root.findall("Error")
        if errors:
            first = errors[0]
            code = _text(first, "Code")
            error = s3_error(500 if code in _RETRY_CODES else 400, code, _text(first, "Message"), bucket=self.bucket)
            error.message = ngettext("%(count)s object wasn't deleted (like %(example)s): %(message)s", "%(count)s objects weren't deleted (like %(example)s): %(message)s", len(errors)) % {"count": len(errors), "example": _text(first, 'Key'), "message": error.message}
            error.args = (error.message,)
            raise error

    def copy(self, source_key: str, key: str) -> None:
        """CopyObject inside the bucket (up to 5 GiB), metadata copied."""
        source = "/" + uri_encode(self.bucket) + "/" + uri_encode(source_key, keep_slash=True)
        self.xml("PUT", key, headers={"x-amz-copy-source": source}, takes_long=True)

    def create_multipart(self, key: str, content_type: str | None, metadata: Mapping[str, str] | None = None) -> str:
        headers = dict(metadata or {})
        if content_type:
            headers["content-type"] = content_type
        upload_id = _text(self.xml("POST", key, query=[("uploads", "")], headers=headers), "UploadId")
        if not upload_id:
            raise BackendError(gettext("The storage gave no UploadId for the multipart upload."))
        return upload_id

    def upload_part(self, key: str, upload_id: str, number: int, body: UploadBody) -> str:
        response = self.request("PUT", key, query=[("partNumber", str(number)), ("uploadId", upload_id)], body=body)
        self._check_etag(response, body)
        etag = response.headers.get("etag")
        if not etag:
            raise BackendError(gettext("The storage gave no ETag for a part."))
        return etag

    def upload_part_copy(self, key: str, upload_id: str, number: int, source_key: str, first: int, last: int,
                         etag: str | None = None) -> str:
        """A part copied from a range of source_key; with `etag` only while the source is unchanged."""
        source = "/" + uri_encode(self.bucket) + "/" + uri_encode(source_key, keep_slash=True)
        headers = {"x-amz-copy-source": source, "x-amz-copy-source-range": f"bytes={first}-{last}"}
        if etag:
            headers["x-amz-copy-source-if-match"] = etag
        root = self.xml("PUT", key, query=[("partNumber", str(number)), ("uploadId", upload_id)], headers=headers,
                        takes_long=True)
        etag = _text(root, "ETag")
        if not etag:
            raise BackendError(gettext("The storage gave no ETag for a part."))
        return etag

    def complete_multipart(self, key: str, upload_id: str, parts: Sequence[tuple[int, str]]) -> None:
        body = ("<CompleteMultipartUpload>"
                + "".join(f"<Part><PartNumber>{number}</PartNumber><ETag>{xml_escape(etag)}</ETag></Part>"
                          for number, etag in parts)
                + "</CompleteMultipartUpload>").encode()
        self.xml("POST", key, query=[("uploadId", upload_id)], body=body, headers={"content-type": "application/xml"},
                 takes_long=True)

    def abort_multipart(self, key: str, upload_id: str) -> None:
        self.request("DELETE", key, query=[("uploadId", upload_id)], expect=(200, 204))

    # -- listings

    def list_pages(self, prefix: str, *, delimiter: str | None = "/", limit: int | None = None
                   ) -> Iterator[tuple[list[S3Object], list[str]]]:
        """Pages of ListObjectsV2 (or ListObjects with markers): (objects, common prefixes),
        keys decoded. A service that answers V2 requests the V1 way is paged with markers."""
        version = self.list_version
        token = marker = None
        while True:
            query = [("max-keys", str(limit or self.page_size))]
            if version == 2:
                query.append(("list-type", "2"))
            if prefix:
                query.append(("prefix", prefix))
            if delimiter:
                query.append(("delimiter", delimiter))
            if self.url_encoded_listing:
                query.append(("encoding-type", "url"))
            if token:
                query.append(("continuation-token", token))
            if marker:
                query.append(("marker", marker))
            root = self.xml("GET", None, query=query)
            encoded = _text(root, "EncodingType").lower() == "url"
            objects = []
            for item in root.findall("Contents"):
                key = self._decode(_text(item, "Key"), encoded)
                if key is None or not key.startswith(prefix):
                    continue
                try:
                    size = int(_text(item, "Size"))
                except ValueError:
                    raise BackendError(gettext("The storage's file list has a wrong file size.")) from None
                objects.append(S3Object(key, size, _iso_date(_text(item, "LastModified"))))
            prefixes = []
            for item in root.findall("CommonPrefixes"):
                common = self._decode(_text(item, "Prefix"), encoded)
                if common is not None and common.startswith(prefix):
                    prefixes.append(common)
            yield objects, prefixes
            if _text(root, "IsTruncated").lower() != "true" or limit:
                return
            if version == 2:
                next_token = _text(root, "NextContinuationToken")
                if encoded and self.url_encoded_tokens:
                    next_token = unquote(next_token)  # not unquote_plus: a token has no spaces, a "+" is a "+"
                if next_token and next_token != token:
                    token = next_token
                    continue
                if next_token:
                    raise BackendError(gettext("The storage's file list pages are wrong (NextContinuationToken didn't change)."))
                self.log.info("S3 %s answers ListObjectsV2 without a token: paging with markers", self.bucket)
                version = self.list_version = 1
                token = None
            # ListObjects: NextMarker, or the last key or prefix of the page.
            next_marker = (self._decode(_text(root, "NextMarker"), encoded) if root.find("NextMarker") is not None
                           else None) or max([item.key for item in objects] + prefixes, default=None)
            if not next_marker or next_marker == marker:
                raise BackendError(gettext("The storage's file list pages are wrong (no position for the next page)."))
            marker = next_marker

    def _decode(self, value: str, encoded: bool) -> str | None:
        if not encoded:
            return value
        try:
            return unquote_plus(value, errors="strict")
        except UnicodeDecodeError:
            self.log.debug("S3 key that isn't UTF-8 left out: %r", value)
            return None

    def has_prefix(self, prefix: str) -> bool:
        """Is any key below prefix (a folder marker counts)?"""
        for objects, prefixes in self.list_pages(prefix, delimiter="/", limit=1):
            return bool(objects or prefixes)
        return False

    # -- bucket and service

    def list_buckets(self) -> list[tuple[str, datetime.datetime | None]]:
        root = self.xml("GET", None, service=True)
        buckets = root.find("Buckets")
        return [(_text(item, "Name"), _iso_date(_text(item, "CreationDate")))
                for item in (buckets.findall("Bucket") if buckets is not None else [])]

    def get_cors(self) -> tuple[list[dict[str, Any]], str | None]:
        """The bucket's CORS rules, and its ResponseVary (COS, OSS: Vary: Origin on the answers), None without one."""
        try:
            root = self.xml("GET", None, query=[("cors", "")])
        except DriverError as error:
            if getattr(error, "code", "") == "NoSuchCORSConfiguration":
                return [], None
            raise
        rules = []
        for item in root.findall("CORSRule"):
            rules.append({
                "id": _text(item, "ID"),
                "origins": [e.text or "" for e in item.findall("AllowedOrigin")],
                "methods": [e.text or "" for e in item.findall("AllowedMethod")],
                "headers": [e.text or "" for e in item.findall("AllowedHeader")],
                "expose": [e.text or "" for e in item.findall("ExposeHeader")],
                "max_age": _text(item, "MaxAgeSeconds"),
            })
        return rules, _text(root, "ResponseVary").strip() or None

    def put_cors(self, rules: Sequence[Mapping[str, Any]], response_vary: str | None = None) -> None:
        """Replaces the whole configuration: `response_vary` is the one get_cors found, written back."""
        parts = ["<CORSConfiguration>"]
        for rule in rules:
            parts.append("<CORSRule>")
            if rule.get("id"):
                parts.append(f"<ID>{xml_escape(rule['id'])}</ID>")
            for tag, name in (("AllowedOrigin", "origins"), ("AllowedMethod", "methods"),
                              ("AllowedHeader", "headers"), ("ExposeHeader", "expose")):
                parts.extend(f"<{tag}>{xml_escape(value)}</{tag}>" for value in rule.get(name) or ())
            if rule.get("max_age"):
                parts.append(f"<MaxAgeSeconds>{int(rule['max_age'])}</MaxAgeSeconds>")
            parts.append("</CORSRule>")
        if response_vary:
            parts.append(f"<ResponseVary>{xml_escape(response_vary)}</ResponseVary>")
        parts.append("</CORSConfiguration>")
        body = "".join(parts).encode()
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        self.request("PUT", None, query=[("cors", "")], body=body,
                     headers={"content-md5": md5, "content-type": "application/xml"})

    # -- presigned URLs

    def presign(self, method: str, key: str, *, expires: int, query: Sequence[tuple[str, str]] = (),
                headers: Mapping[str, str] | None = None, domain: str | None = None) -> str:
        """A presigned URL of an object; with `domain` ("https://host") signed for that host."""
        scheme = self.scheme
        host = None
        if domain:
            scheme, host = parse_origin(domain)
        host, path = self.target(key, host=host)
        expires = max(1, min(int(expires), MAX_PRESIGN_SECONDS))
        return presign(method, scheme, host, path, query, self.credentials(), self.region, self.now(), expires, headers)


# --- params ---------------------------------------------------------------------------------------

def _endpoint_param(help_text: str, example: str, label: str = "Endpoint") -> Param:
    return Param("endpoint", label, help=help_text, placeholder=example)


ACCESS_KEY = Param("access_key", "AccessKey", help=gettext_lazy("The AccessKey ID from \"Access keys\" in the provider's console."))
SECRET_KEY = Param("secret_key", "SecretKey", kind="password", secret=True,
                   help=gettext_lazy("The SecretKey (AccessKey Secret) that goes with the AccessKey."))
BASE_PATH = Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
                  help=gettext_lazy("The folder in the bucket that is this storage source's root, like / or /folder1."))
IS_PRIVATE = Param("is_private", gettext_lazy("Private bucket"), kind="bool", required=False, default=True,
                   help=gettext_lazy("A private bucket's download links are signed and expire; a "
                                     "public bucket's (public read) are plain addresses that don't."))
CORS_HELP = (gettext_lazy("For browsers to upload straight to the bucket, the bucket must allow this "
             "site's cross-origin requests (CORS): origin this site's address, methods PUT, "
             'GET and HEAD, headers *, exposed header ETag. Click "Allow direct uploads '
             "from this site (set CORS)\" to add it, or set it in the provider's console."))


# These two are called while the driver classes are made (at import): their texts stay lazy.
def bucket_param(help_text: str = "", label: str = gettext_lazy("Bucket name")) -> Param:
    return Param("bucket", label, help=format_lazy("{} {}", help_text, CORS_HELP) if help_text else CORS_HELP)


def domain_param(help_text: str) -> Param:
    return Param("domain", gettext_lazy("Download domain"), required=False, default="", placeholder="https://cdn.example.com",
                 help=help_text)


DOMAIN_HELP = (gettext_lazy("Optional. A CDN domain or custom domain bound to the bucket, written as "
               "https://domain. When set, download links use it. Links of a private bucket are "
               "still signed for the bucket's own address, so the CDN must send the bucket's "
               "domain as Host when fetching from the origin, without origin authentication."))


# --- the drivers ----------------------------------------------------------------------------------

class S3Driver(StorageDriver):
    type = "s3"
    label = gettext_lazy("S3-compatible storage")
    params = (
        _endpoint_param(gettext_lazy("The S3 API's address, with http:// or https://, like "
                        "https://s3.us-east-1.amazonaws.com, https://<account "
                        "ID>.r2.cloudflarestorage.com or http://192.168.1.10:9000."),
                        "https://s3.us-east-1.amazonaws.com"),
        Param("region", gettext_lazy("Region"), required=False, default="",
              help=gettext_lazy("The region for signing, like us-east-1 or auto (Cloudflare R2). "
                                "When empty, it's taken from an AWS Endpoint, or us-east-1 otherwise.")),
        ACCESS_KEY,
        SECRET_KEY,
        bucket_param(),
        BASE_PATH,
        Param("path_style", gettext_lazy("Addressing style"), kind="select", required=False, default="virtual",
              options=(("virtual", gettext_lazy("Virtual-hosted style (bucket.Endpoint)")), ("path", gettext_lazy("Path style (Endpoint/bucket)"))),
              help=gettext_lazy("AWS and most cloud services use virtual-hosted style; self-hosted services "
                   "(MinIO, Ceph and so on) usually use path style. Path style is used anyway when "
                   "the Endpoint is an IP address or the bucket name can't be part of a domain.")),
        IS_PRIVATE,
        domain_param(gettext_lazy("Optional, written as https://domain. When set, download links use it. "
                     "With virtual-hosted style it is the bucket's CDN or custom domain, and "
                     "private links are still signed for the bucket's own address, so the CDN "
                     "must send the bucket's domain as Host when fetching from the origin, "
                     "without origin authentication; with path style it is the S3 service's "
                     "public address (MinIO behind an Nginx reverse proxy, say, which must keep "
                     "the Host header), and download links and direct uploads are signed for it.")),
        Param("skip_tls_verify", gettext_lazy("Don't verify the TLS certificate"), kind="bool", required=False, default=False, advanced=True,
              help=gettext_lazy("Only for a self-hosted S3 service with a self-signed certificate; don't turn it on for cloud services.")),
    )
    can_range = True
    can_direct_download = True
    can_direct_upload = True
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    helpers = {"buckets": gettext_lazy("List buckets"), "cors": gettext_lazy("Show CORS settings"), "set_cors": gettext_lazy("Allow direct uploads from this site (set CORS)")}
    # Subclasses without region or path_style simply have fewer (config.setup_keys keeps the params they have).
    setup_params = ("endpoint", "region", "access_key", "secret_key", "bucket", "path_style")

    # Per type: the region to sign with (None: the "region" param), and the addressing style
    # (None: the "path_style" param).
    signing_region: ClassVar[str | None] = None
    force_path_style: ClassVar[bool | None] = None
    url_encoded_listing: ClassVar[bool] = True
    url_encoded_tokens: ClassVar[bool] = False  # the URL-encoded listing's NextContinuationToken is encoded too (OSS)
    list_version: ClassVar[int] = 2  # 1: ListObjects with markers, for services without V2
    endpoint_example: ClassVar[str] = "https://s3.us-east-1.amazonaws.com"

    # Sizes (the tests make them small).
    single_put_limit: ClassVar[int] = 64 * MiB
    min_part_size: ClassVar[int] = 8 * MiB
    max_parts: ClassVar[int] = 10000
    copy_limit: ClassVar[int] = 5 * GiB
    min_copy_part_size: ClassVar[int] = 512 * MiB
    direct_upload_limit: ClassVar[int] = 5 * GiB
    upload_url_seconds: ClassVar[int] = 3600
    page_size: ClassVar[int] = 1000
    workers: ClassVar[int] = 8  # parallel copies when moving or copying a folder
    transport: ClassVar[httpx.BaseTransport | None] = None  # tests route requests to a local server

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        try:
            self._prefix: str | None = key_prefix(self.config.get("base_path"))
        except InvalidPath:
            self._prefix = None  # every operation refuses: never fall back to the whole bucket
        self._client: S3Client | None = None
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
        if (message := check_origin(config.get("endpoint"), "Endpoint", cls.endpoint_example)):
            errors["endpoint"] = message
        bucket = config.get("bucket")
        if (message := cls._check_bucket(bucket)):
            errors["bucket"] = message
        elif "endpoint" not in errors and cls.force_path_style is not True and config.get("path_style") != "path":
            host = parse_origin(config["endpoint"])[1]
            if host.startswith(f"{bucket}."):
                errors["endpoint"] = gettext("Leave the bucket name out of the Endpoint: enter the service's address, and the bucket name on its own.")
        access_label = next((p.label for p in cls.params if p.key == "access_key"), "AccessKey")
        if (message := check_key(config.get("access_key"), access_label)):
            errors["access_key"] = message
        if not str(config.get("secret_key") or "").strip():
            errors["secret_key"] = gettext("Please fill in the SecretKey.")
        errors.update(check_base_path_and_domain(config))
        region = config.get("region")
        if region and not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(region)):
            errors["region"] = gettext("The region can only have letters, digits, - and _, like us-east-1.")
        return errors

    @classmethod
    def _check_bucket(cls, bucket: Any) -> str | None:
        if not isinstance(bucket, str) or not bucket.strip():
            return gettext("Please fill in the bucket name.")
        if len(bucket) > 255 or not re.fullmatch(r"[^\s/\\?#%\x00-\x1f\x7f]+", bucket):
            return gettext("Invalid bucket name: it can't have spaces or the characters /, \\, ?, # and %.")
        return None

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        if name not in cls.helpers:
            raise Unsupported()
        needed = ("endpoint", "access_key", "secret_key") + (() if name == "buckets" else ("bucket",))
        missing = [key for key in needed if not config.get(key) and any(p.key == key for p in cls.params)]
        if missing:
            labels = gettext(", ").join(p.label for p in cls.params if p.key in missing)
            raise DriverError(gettext("Please fill in %(labels)s first.") % {"labels": labels})
        errors = {key: message for key, message in cls.validate_config(config).items() if key in needed}
        if errors:
            raise DriverError(next(iter(errors.values())))
        driver = cls(config)
        try:
            return driver._helper(name, args)
        finally:
            driver.close()

    def _helper(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        s3 = self._s3()
        if name == "buckets":
            buckets = s3.list_buckets()
            if not buckets:
                return {"message": gettext("These keys have no buckets.")}
            return {"param": "bucket", "choices": [
                {"value": bucket, "label": gettext("%(bucket)s (created %(created)s)") % {"bucket": bucket, "created": format(created, "%Y-%m-%d")} if created else bucket}
                for bucket, created in buckets]}
        rules, response_vary = s3.get_cors()
        if name == "cors":
            if not rules:
                return {"message": gettext("This bucket has no CORS rules yet, so browsers can't upload to it directly.")}
            lines = [gettext("%(index)s. Origins %(origins)s; methods %(methods)s; "
                             "headers %(headers)s; exposed headers %(exposed)s") % {"index": index, "origins": ' '.join(rule['origins']) or '-', "methods": ' '.join(rule['methods']) or '-', "headers": ' '.join(rule['headers']) or '-', "exposed": ' '.join(rule['expose']) or '-'}
                     for index, rule in enumerate(rules, 1)]
            return {"message": gettext("The bucket's CORS rules now:\n") + "\n".join(lines)}
        origin = str(args.get("origin") or "").strip()
        try:
            scheme, host = parse_origin(origin)
        except ValueError:
            raise DriverError(gettext("This site's address (origin) is missing, so CORS can't be set.")) from None
        origin = f"{scheme}://{host}"
        for rule in rules:
            if (origin in rule["origins"] or "*" in rule["origins"]) and "PUT" in rule["methods"]:
                return {"message": gettext("The bucket already allows cross-origin uploads from %(origin)s.") % {"origin": origin}}
        rules.append({"origins": [origin], "methods": ["GET", "PUT", "HEAD"], "headers": ["*"],
                      "expose": ["ETag"], "max_age": 3600})
        s3.put_cors(rules, response_vary)
        return {"message": gettext("Cross-origin uploads and downloads from %(origin)s are allowed now.") % {"origin": origin}}

    # -- requests

    def _region(self) -> str:
        if self.signing_region:
            return self.signing_region
        region = (self.config.get("region") or "").strip()
        if region:
            return region
        host = parse_origin(self.config["endpoint"])[1]
        if (match := re.fullmatch(r"(?:.+\.)?s3[.-]([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?(?::\d+)?", host)):
            if match.group(1) not in ("external-1", "dualstack"):
                return match.group(1)
        if host.endswith(".r2.cloudflarestorage.com"):
            return "auto"
        return "us-east-1"

    def _path_style(self) -> bool:
        if self.force_path_style is not None:
            return self.force_path_style
        return self.config.get("path_style") == "path"

    def _make_client(self) -> S3Client:
        endpoint = self.config.get("endpoint") or ""
        try:
            parse_origin(endpoint)
        except ValueError:
            raise BackendError(gettext("The storage source's Endpoint is set wrong. Please ask the administrator to check it.")) from None
        credentials = Credentials(str(self.config.get("access_key") or ""), str(self.config.get("secret_key") or ""))
        return S3Client(endpoint=endpoint, bucket=str(self.config.get("bucket") or ""), region=self._region(),
                        credentials=lambda: credentials, path_style=self._path_style(),
                        verify=not self.config.get("skip_tls_verify"), url_encoded_listing=self.url_encoded_listing,
                        url_encoded_tokens=self.url_encoded_tokens, list_version=self.list_version,
                        page_size=self.page_size, transport=self.transport, logger=self.log)

    def _s3(self) -> S3Client:
        client = self._client
        if client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self._make_client()
                client = self._client
        return client

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
            size = len(key.encode("utf-8"))
        except UnicodeEncodeError:  # a lone surrogate
            raise InvalidPath(gettext("The path has characters that can't be encoded.")) from None
        if size > MAX_KEY_BYTES:
            raise InvalidPath(ngettext("The path is too long: a full object storage path is at most %(limit)s byte.", "The path is too long: a full object storage path is at most %(limit)s bytes.", MAX_KEY_BYTES) % {"limit": MAX_KEY_BYTES})
        return key

    def _folder_prefix(self, path: str) -> str:
        if path == "/":
            return self._base_prefix()
        return self._key(path) + "/"

    def _is_folder(self, path: str) -> bool:
        return path == "/" or self._s3().has_prefix(self._folder_prefix(path))

    def _exists(self, path: str) -> bool:
        return self._s3().head(self._key(path)) is not None or self._is_folder(path)

    # -- reading

    def list(self, path: str) -> list[Entry]:
        prefix = self._folder_prefix(path)
        entries: list[Entry] = []
        marker = False
        hidden = False  # keys there that no storage path can address
        folders: set[str] = set()  # some services (and moto) repeat common prefixes on every page
        for objects, prefixes in self._s3().list_pages(prefix):
            hidden = hidden or bool(objects or prefixes)
            for item in objects:
                name = item.key[len(prefix):]
                if name == "":
                    marker = True
                elif _listable(name):
                    entries.append(Entry(name, paths.join(path, name), False, item.size, item.modified))
            for common in prefixes:
                name = common[len(prefix):-1]
                if _listable(name) and name not in folders:
                    folders.add(name)
                    entries.append(Entry(name, paths.join(path, name), True))
        if not entries and not marker and not hidden and path != "/":
            if self._s3().head(self._key(path)) is not None:
                raise NotADirectory()
            raise NotFound()
        return entries

    def stat(self, path: str) -> Entry:
        if path == "/":
            return Entry("", "/", True)
        head = self._s3().head(self._key(path))
        if head is not None:
            return Entry(paths.name(path), path, False, head.size, head.modified)
        if self._is_folder(path):
            return Entry(paths.name(path), path, True)
        raise NotFound()

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        if path == "/":
            raise NotFound()
        key = self._key(path)
        if length == 0:
            head = self._s3().head(key)
            if head is None:
                raise NotFound()
            return ReadStream(iter(()), size=0, total=head.size, offset=offset, content_type=head.content_type,
                              etag=head.etag, modified=head.modified)
        last = None if length is None else offset + length - 1
        response = self._s3().get(key, offset, last)
        headers = response.headers
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
        s3 = self._s3()
        domain = (self.config.get("domain") or "").strip()
        if not self.config.get("is_private", True):
            url = s3.object_url(key)
            return replace_origin(url, domain) if domain else url
        query = [("response-content-disposition", content_disposition(filename))] if filename else []
        if domain and s3.path_style:
            return s3.presign("GET", key, expires=expires, query=query, domain=domain)
        url = s3.presign("GET", key, expires=expires, query=query)
        return replace_origin(url, domain) if domain else url

    # -- writing

    def _upload_domain(self, s3: S3Client) -> str | None:
        """The address ("https://host") browser uploads are signed for; None: the bucket's own. With path
        style, the download domain: the S3 service's public address."""
        domain = (self.config.get("domain") or "").strip()
        return domain if domain and s3.path_style else None

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        if size > self.direct_upload_limit:
            return None
        key = self._key(path)
        s3 = self._s3()
        if s3.has_prefix(key + "/"):
            raise AlreadyExists()
        content_type = content_type_of(paths.name(path))
        url = s3.presign("PUT", key, expires=self.upload_url_seconds,
                         headers={"content-type": content_type, "content-length": str(size)},
                         domain=self._upload_domain(s3))
        return UploadTarget(kind="single", url=url, method="PUT", headers={"Content-Type": content_type})

    def _abort(self, key: str, upload_id: str) -> None:
        try:
            self._s3().abort_multipart(key, upload_id)
        except DriverError as exc:
            self.log.warning("S3 abort of multipart upload %s failed: %s", upload_id, exc)

    def _part_size(self, size: int, minimum: int) -> int:
        if size > self.max_parts * 5 * GiB:  # 10,000 parts of 5 GiB: 48.8 TiB, AWS's limit (OBS's too)
            raise DriverError(gettext("The file is too large: object storage takes at most 48.8 TiB per file."))
        part = max(minimum, math.ceil(size / self.max_parts))
        return min(math.ceil(part / MiB) * MiB, 5 * GiB)

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        key = self._key(path)
        s3 = self._s3()
        if s3.has_prefix(key + "/"):
            raise AlreadyExists()
        content_type = content_type_of(paths.name(path))
        if size <= self.single_put_limit:
            s3.put(key, UploadBody(data, size), content_type=content_type)
            return
        part_size = self._part_size(size, self.min_part_size)
        upload_id = s3.create_multipart(key, content_type)
        try:
            parts = []
            left = size
            while left > 0:
                count = min(part_size, left)
                number = len(parts) + 1
                parts.append((number, s3.upload_part(key, upload_id, number, UploadBody(data, count))))
                left -= count
            s3.complete_multipart(key, upload_id, parts)
        except BaseException:
            self._abort(key, upload_id)
            raise

    def mkdir(self, path: str) -> None:
        if path == "/":
            raise AlreadyExists()
        key = self._key(path)
        if self._exists(path):
            raise AlreadyExists()
        self._s3().put(key + "/", b"")

    def delete(self, path: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The storage source's root folder can't be deleted."))
        key = self._key(path)
        s3 = self._s3()
        if s3.head(key) is not None:
            s3.delete(key)
            return
        found = False
        for objects, _ in s3.list_pages(key + "/", delimiter=None):
            if objects:
                found = True
                s3.delete_many([item.key for item in objects])
        if not found:
            raise NotFound()

    def _parallel(self, work: Callable[[Any], None], items: Sequence[Any]) -> None:
        run_parallel(work, items, self.workers)

    def _copy_object(self, source_key: str, key: str, size: int | None, head: S3Head | None) -> None:
        s3 = self._s3()
        if size is None or size <= self.copy_limit:
            s3.copy(source_key, key)
            return
        head = head or s3.head(source_key)
        if head is None:
            raise NotFound()
        part_size = self._part_size(size, self.min_copy_part_size)
        upload_id = s3.create_multipart(key, None, head.metadata)
        try:
            ranges = [(number + 1, first, min(first + part_size, size) - 1)
                      for number, first in enumerate(range(0, size, part_size))]
            etags: dict[int, str] = {}

            def copy_part(item: tuple[int, int, int]) -> None:
                number, first, last = item
                etags[number] = s3.upload_part_copy(key, upload_id, number, source_key, first, last, head.etag)

            self._parallel(copy_part, ranges)
            s3.complete_multipart(key, upload_id, [(number, etags[number]) for number, _, _ in ranges])
        except BaseException:
            self._abort(key, upload_id)
            raise

    def _transfer(self, source: str, target: str, *, move: bool) -> None:
        s3 = self._s3()
        source_key, target_key = self._key(source), self._key(target)
        head = s3.head(source_key)
        if head is not None:
            if self._exists(target):
                raise AlreadyExists()
            self._copy_object(source_key, target_key, head.size, head)
            if move:
                s3.delete(source_key)
            return
        source_prefix, target_prefix = source_key + "/", target_key + "/"
        pages = s3.list_pages(source_prefix, delimiter=None)
        first = next(pages, ([], []))[0]
        if not first:
            raise NotFound()
        if self._exists(target):
            raise AlreadyExists()
        objects = first
        while True:
            # Keys made by other programs with "." or ".." segments can't be sent in a URL (see
            # has_dot_segment); they stay where they are.
            stuck = [item.key for item in objects if has_dot_segment(item.key[len(source_prefix):])]
            if stuck:
                self.log.warning("S3 %d keys under %r with . or .. segments left in place, e.g. %r",
                                 len(stuck), source_prefix, stuck[0])
                objects = [item for item in objects if not has_dot_segment(item.key[len(source_prefix):])]
            self._parallel(lambda item: self._copy_object(
                item.key, target_prefix + item.key[len(source_prefix):], item.size, None), objects)
            if move and objects:
                s3.delete_many([item.key for item in objects])
            objects = next(pages, ([], []))[0]
            if not objects:
                break

    def rename(self, path: str, new_name: str) -> None:
        if path == "/":
            raise InvalidPath(gettext("The root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)
        if target != path:
            self._transfer(path, target, move=True)

    def _target_in(self, path: str, folder: str) -> str:
        if path == "/":
            raise InvalidPath(gettext("The root folder can't be moved or copied."))
        if not self._is_folder(folder):
            if folder != "/" and self._s3().head(self._key(folder)) is not None:
                raise NotADirectory(gettext("The target isn't a folder."))
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


class MinIODriver(S3Driver):
    type = "minio"
    label = "MinIO"
    params = (
        _endpoint_param(gettext_lazy("MinIO's S3 API address (not the web console's), with the scheme, like http://192.168.1.10:9000."),
                        "http://192.168.1.10:9000", gettext_lazy("Service address")),
        Param("region", gettext_lazy("Region"), required=False, default="us-east-1",
              help=gettext_lazy("MINIO_SITE_REGION when MinIO has it set; otherwise keep us-east-1.")),
        ACCESS_KEY,
        SECRET_KEY,
        Param("bucket", gettext_lazy("Bucket name"),
              help=gettext_lazy("MinIO has no CORS settings per bucket: it takes cross-origin requests from every "
                                "origin, unless MINIO_API_CORS_ALLOW_ORIGIN (or the setting api cors_allow_origin) "
                                "names some; then this site's address must be among them for browsers to upload "
                                "straight to the bucket.")),
        BASE_PATH,
        IS_PRIVATE,
        domain_param(gettext_lazy("Optional. MinIO's public address, like https://minio.example.com behind an Nginx "
                     "reverse proxy (which must keep the Host header). Fill it in when the service "
                     "address is internal: download links and direct uploads are signed for it.")),
        Param("skip_tls_verify", gettext_lazy("Don't verify the TLS certificate"), kind="bool", required=False, default=False, advanced=True,
              help=gettext_lazy("Turn on when MinIO uses a self-signed certificate.")),
    )
    # MinIO's bucket CORS calls are stubs (GetBucketCors: always NoSuchCORSConfiguration, PutBucketCors: 501).
    helpers = {"buckets": gettext_lazy("List buckets")}
    force_path_style = True
    endpoint_example = "http://192.168.1.10:9000"

    def _region(self) -> str:
        return (self.config.get("region") or "").strip() or "us-east-1"


class AliyunDriver(S3Driver):
    """Aliyun OSS through its S3-compatible API; download links use OSS's V4 signature. With the internal endpoint
    (oss-<region>-internal), which only Alibaba Cloud's network reaches, links and browser uploads go to the
    public one, as OSS's docs say to make public URLs, and only the site's own requests are internal."""

    type = "aliyun"
    label = gettext_lazy("Alibaba Cloud OSS")
    params = (
        _endpoint_param(gettext_lazy("The OSS region endpoint, with the scheme, like "
                        "https://oss-cn-hangzhou.aliyuncs.com; when the server is an Alibaba "
                        "Cloud ECS in the same region, the internal endpoint "
                        "https://oss-cn-hangzhou-internal.aliyuncs.com works too: the site then reads and "
                        "writes over the internal network, while download links and direct uploads use the "
                        "public endpoint."),
                        "https://oss-cn-hangzhou.aliyuncs.com"),
        ACCESS_KEY,
        SECRET_KEY,
        bucket_param(pgettext_lazy("Aliyun, DogeCloud", "The bucket's name."), pgettext_lazy("Aliyun, DogeCloud", "Bucket name")),
        BASE_PATH,
        IS_PRIVATE,
        domain_param(gettext_lazy("Optional. A custom or CDN domain bound to the bucket, written as "
                     "https://domain. When set, download links use it; private links use OSS's own "
                     "signatures, which aren't tied to a host, so custom domains work too.")),
    )
    signing_region = "oss"
    force_path_style = False
    url_encoded_tokens = True  # "会对返回结果中的…NextContinuationToken…进行编码", and OSS's SDKs decode it
    endpoint_example = "https://oss-cn-hangzhou.aliyuncs.com"

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        key = self._key(path)
        s3 = self._s3()
        # A domain bound to the bucket serves its root, and so does the bucket's public host (the V4 signature
        # doesn't cover the host).
        origin = (self.config.get("domain") or "").strip() or self._public_origin(s3)
        if origin:
            scheme, host = parse_origin(origin)
            url = f"{scheme}://{host}/{uri_encode(key, keep_slash=True)}"
        else:
            url = s3.object_url(key)
        if not self.config.get("is_private", True):
            return url
        expires = max(1, min(int(expires), MAX_PRESIGN_SECONDS))
        overrides = [("response-content-disposition", content_disposition(filename))] if filename else []
        access_key, secret_key = str(self.config.get("access_key") or ""), str(self.config.get("secret_key") or "")
        region = oss_region(str(self.config.get("endpoint") or ""))
        if region is None:
            # An endpoint that doesn't name the region (oss-accelerate): V1, which OSS stops taking for accounts made
            # since March 2025 and buckets made since September 2025.
            expires_at = int(time.time() + s3.clock_offset) + expires
            signature = oss_v1_signature(secret_key, "GET", s3.bucket, key, expires_at, overrides)
            query = [("OSSAccessKeyId", access_key), ("Expires", str(expires_at)), ("Signature", signature)] + overrides
            return url + "?" + "&".join(f"{name}={quote(value, safe='')}" for name, value in query)
        now = datetime.datetime.fromtimestamp(time.time() + s3.clock_offset, datetime.timezone.utc)
        query = oss_v4_query(access_key, secret_key, "GET", s3.bucket, key, now, expires, region, overrides)
        return url + "?" + "&".join(f"{name}={value}" if value else name for name, value in query)

    def _upload_domain(self, s3: S3Client) -> str | None:
        return self._public_origin(s3)  # a presigned PUT signs the host: the public one, when the endpoint is internal

    @staticmethod
    def _public_origin(s3: S3Client) -> str | None:
        """The bucket's public address, "https://bucket.host", when the endpoint is an internal one
        (oss-<region>-internal or s3.oss-<region>-internal); else None. Always https: an http:// internal
        endpoint is the server's choice, and browsers refuse plain-http uploads and downloads from an https page
        (the upload page's CSP only allows https: besides the addresses the admin typed)."""
        host = re.sub(r"-internal(?=\.aliyuncs\.com(?::\d+)?$)", "", s3.host)
        return f"https://{s3.bucket}.{host.partition(':')[0]}" if host != s3.host else None


class TencentDriver(S3Driver):
    type = "tencent"
    label = gettext_lazy("Tencent Cloud COS")
    params = (
        _endpoint_param(gettext_lazy("The COS region endpoint, with the scheme, like https://cos.ap-guangzhou.myqcloud.com."),
                        "https://cos.ap-guangzhou.myqcloud.com"),
        Param("access_key", "SecretId", help=gettext_lazy("The SecretId from Tencent Cloud's \"Access management → API keys\".")),
        Param("secret_key", "SecretKey", kind="password", secret=True, help=gettext_lazy("The SecretKey that goes with the SecretId.")),
        bucket_param(gettext_lazy("The bucket's full name with the APPID, like examplebucket-1250000000.")),
        BASE_PATH,
        Param("is_private", gettext_lazy("Private bucket"), kind="bool", required=False, default=True,
              help=gettext_lazy("Turn on for private-read buckets: download links are signed and expire. Turn it off "
                                "when you use a custom CDN domain with origin authentication on in Tencent Cloud.")),
        domain_param(gettext_lazy("Optional. A CDN domain or custom domain bound to the bucket, written as "
                                  "https://domain. When set, download links use it. Links of a private bucket "
                                  "are still signed for the bucket's own address, so the CDN must send the "
                                  "bucket's domain as Host when fetching from the origin, without origin "
                                  "authentication. Tencent Cloud CDN's \"origin authentication\" conflicts "
                                  "with private bucket signatures here: only one of them can be used. Buckets "
                                  "created since 2024 don't allow downloading .apk and .ipa files through their "
                                  "default domain, except over Tencent Cloud's internal network: such files need "
                                  "this domain, and downloads through the site fail for them.")),
    )
    signing_region = "cos"
    force_path_style = False
    list_version = 1  # as rclone: COS pages ListObjects with markers
    endpoint_example = "https://cos.ap-guangzhou.myqcloud.com"

    @classmethod
    def _check_bucket(cls, bucket: Any) -> str | None:
        message = super()._check_bucket(bucket)
        if message is None and not re.fullmatch(r"[a-z0-9-]+-\d+", bucket):
            return gettext("A COS bucket name must include the APPID, written as name-APPID, like examplebucket-1250000000.")
        return message


class HuaweiDriver(S3Driver):
    type = "huawei"
    label = gettext_lazy("Huawei Cloud OBS")
    params = (
        _endpoint_param(gettext_lazy("The OBS endpoint, with the scheme, like https://obs.cn-north-4.myhuaweicloud.com."),
                        "https://obs.cn-north-4.myhuaweicloud.com"),
        ACCESS_KEY,
        SECRET_KEY,
        bucket_param(pgettext_lazy("Huawei Cloud", "The bucket's name."), pgettext_lazy("Huawei Cloud", "Bucket name")),
        BASE_PATH,
        IS_PRIVATE,
        domain_param(format_lazy("{} {}", DOMAIN_HELP, gettext_lazy(
            "Buckets created after December 30, 2023 don't allow downloading .apk and .ipa files through their "
            "default domain over the internet: such files need a custom domain here, and downloads through the site "
            "fail for them."))),
    )
    signing_region = "obs"
    force_path_style = False
    # As rclone: OBS (its parallel file system buckets) has no ListObjectsV2, and with
    # encoding-type=url its markers don't move on.
    list_version = 1
    url_encoded_listing = False
    endpoint_example = "https://obs.cn-north-4.myhuaweicloud.com"
