"""UpYun (又拍云) cloud storage through its REST API (https://v0.api.upyun.com).

Paths
    "/" of the storage source is the base path inside the service: with base path "/data", "/a/b.txt" is
    "/<service>/data/a/b.txt" on the API. UpYun has real folders (made with "folder: true", deleted only when empty).
    Every name is percent-encoded in the URL, so no name reaches outside the base path. Listings are paged with
    x-list-iter and x-list-limit (JSON answers, the last page's iter is "g2gCZAAEbmV4dGQAA2VvZg").

Requests
    Requests carry a Date header and "Authorization: UPYUN <operator>:<signature>", the signature being
    base64(HMAC-SHA1(MD5(password), "METHOD&URI&Date")) with URI the encoded path; UpYun accepts it for 30 minutes, and
    a "date offset" answer corrects our clock. Transient failures (connection errors, 5xx, 429) are retried when the
    body can be sent again. UpYun limits how often synchronous deletes may come, so a DELETE it refuses with 429 waits
    seconds, longer each time (`delete_waits`): deleting a big folder takes a DELETE per file.

Params
    The service name, the operator and its password, the base path, the download domain and the token key.

Changes
    Files are moved and copied on the server (X-Upyun-Move-Source, X-Upyun-Copy-Source); UpYun can't do that with
    folders, so renaming, moving or copying a folder makes the new folders and moves or copies every file, then deletes
    the old folders bottom up (not atomic). Deleting a folder deletes what's in it first.

Downloads
    open() reads through the API (HEAD, then GET with Range). download_url gives the URL of the bound domain (without
    one, the service's test domain http://<service>.test.upcdn.net) with `_upd=<file name>` so that the browser saves
    the file under its name, and, when the "Token 防盗链" key is set, the `_upt` token (MD5 of key&expiry&path,
    characters 12-20, plus the expiry).

Uploads
    Uploads are one PUT up to 64 MiB, and UpYun's parallel resumable upload above (X-Upyun-Multi-*, parts of 8 to
    50 MiB). upload_target is a FORM API upload (policy and signature) for files up to 100 MiB, which UpYun's form API
    recommends; larger ones, and paths whose save-key (base path and path) has braces (the form API would replace
    {year}, {filename}, ... in it), go through the site.
"""

from __future__ import annotations

import base64
import datetime
import email.utils
import hashlib
import hmac
import json
import logging
import math
import random
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, BinaryIO, ClassVar
from urllib.parse import quote

import httpx

from django.utils.translation import gettext as _, gettext_lazy

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
    UploadTarget,
)
from .s3 import (
    CHUNK,
    MiB,
    Reply,
    UploadBody,
    check_key,
    check_origin,
    content_type_of,
    guarded,
    parse_origin,
    read_limited,
    run_parallel,
    slice_stream,
)

END_MARK = "g2gCZAAEbmV4dGQAA2VvZg"  # x-list-iter of the last page
MAX_JSON = 32 * MiB


# --- signatures -----------------------------------------------------------------------------------

def signature(password_md5: str, method: str, uri: str, date: str, content_md5: str = "") -> str:
    """UpYun's signature: base64(HMAC-SHA1(MD5(password), METHOD&URI&Date[&Content-MD5]))."""
    message = f"{method}&{uri}&{date}" + (f"&{content_md5}" if content_md5 else "")
    return base64.b64encode(hmac.new(password_md5.encode(), message.encode(), hashlib.sha1).digest()).decode()


def form_signature(password_md5: str, bucket: str, date: str, policy: str) -> str:
    """The FORM API's signature: base64(HMAC-SHA1(MD5(password), POST&/<bucket>&Date&Policy))."""
    message = f"POST&/{bucket}&{date}&{policy}"
    return base64.b64encode(hmac.new(password_md5.encode(), message.encode(), hashlib.sha1).digest()).decode()


def anti_leech_token(key: str, expires_at: int, uri: str) -> str:
    """The `_upt` value of UpYun's token anti-leech: MD5(key&expiry&URI)[12:20] + expiry."""
    return hashlib.md5(f"{key}&{expires_at}&{uri}".encode()).hexdigest()[12:20] + str(expires_at)


# --- errors ---------------------------------------------------------------------------------------

def upyun_error(status: int, code: Any = None, message: str = "") -> DriverError:
    """The DriverError for an UpYun error answer; `status` and `code` are kept on it."""
    # ASCII digits only: isdigit() takes "²", which int() refuses (STOR-17).
    digits = isinstance(code, str) and code.isascii() and code.isdigit()
    code = int(code) if isinstance(code, int) or digits else 0
    text = message.strip()[:200]
    detail = _(" (%(code)s %(message)s)") % {"code": code, "message": text} if code else (_(" (%(message)s)") % {"message": text} if text else "")
    if status == 404:
        error: DriverError = NotFound()
    elif code == 40100012:
        error = BackendError(_("The UpYun service doesn't exist%(detail)s. Please check the service name.") % {"detail": detail})
    elif code in (40100006, 40100009, 40100005):
        error = AuthError(_("UpYun refused the request%(detail)s: please check the operator name and password.") % {"detail": detail})
    elif code == 40100017 or code == 40300011:
        error = AuthError(_("The UpYun operator lacks this permission%(detail)s. Please give it read, "
                            "write and delete permissions in the service's operator authorization.") % {"detail": detail})
    elif code in (40100002, 40300006):
        error = AuthError(_("The server's clock is too far from UpYun's%(detail)s. Please correct the server's time.") % {"detail": detail})
    elif status == 401:
        error = AuthError(_("UpYun refused the request%(detail)s.") % {"detail": detail})
    elif code == 40600002 or code == 40300015:
        error = AlreadyExists()
    elif code in (40300001, 40300002, 40300003):
        error = InvalidPath(_("UpYun doesn't accept this name%(detail)s.") % {"detail": detail})
    elif code == 40300004:
        error = DriverError(_("The UpYun storage is full%(detail)s.") % {"detail": detail})
    elif status == 429:
        error = BackendError(_("UpYun is busy%(detail)s. Please try again later.") % {"detail": detail})
    elif status >= 500:
        error = BackendError(_("UpYun failed (HTTP %(status)s)%(detail)s. Please try again later.") % {"status": status, "detail": detail})
    else:
        error = BackendError(_("UpYun refused the request (HTTP %(status)s)%(detail)s.") % {"status": status, "detail": detail})
    error.status = status  # type: ignore[attr-defined]
    error.code = code  # type: ignore[attr-defined]
    return error


# --- small helpers --------------------------------------------------------------------------------

def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _unix_time(value: Any) -> datetime.datetime | None:
    seconds = _int(value)
    if seconds is None:
        return None
    try:
        return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _http_date(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


# --- the driver -----------------------------------------------------------------------------------

class UpYunDriver(StorageDriver):
    type = "upyun"
    label = gettext_lazy("UpYun")
    params = (
        Param("bucket", gettext_lazy("Service name"), help=gettext_lazy("The name of the UpYun storage service. Direct uploads from the browser use UpYun's form API, so no CORS settings are needed.")),
        Param("operator", gettext_lazy("Operator"), help=gettext_lazy("An operator added in the service's \"Operator authorization\", with read, write and delete permissions.")),
        Param("password", gettext_lazy("Operator password"), kind="password", secret=True),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in the service that is this storage source's root, like / or /folder1.")),
        Param("domain", gettext_lazy("Download domain"), required=False, default="", placeholder="https://files.example.com",
              help=gettext_lazy("The CDN domain bound to the service, written as https://domain. When empty, "
                                "UpYun's test domain for the service, http://service-name.test.upcdn.net, is "
                                "used, which is only good for trying it out.")),
        Param("token", gettext_lazy("Token hotlink protection key"), kind="password", secret=True, required=False, default="",
              help=gettext_lazy("After turning it on in UpYun's \"Access control → Token hotlink protection\", "
                                "enter its key here, and download links get a time-limited _upt signature.")),
    )
    can_range = True
    can_direct_download = True
    can_direct_upload = True
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True

    # Endpoint and tuning; tests point them at a fake and make them small.
    api_url: ClassVar[str] = "https://v0.api.upyun.com"
    attempts: ClassVar[int] = 3
    # Waits before a DELETE that UpYun refused with 429 goes again: it limits how often synchronous
    # deletes may come (its REST API docs, without the numbers), and a folder delete sends one per file.
    delete_waits: ClassVar[tuple[float, ...]] = (1.0, 2.0, 4.0, 8.0, 16.0)
    page_size: ClassVar[int] = 1000  # x-list-limit, at most 10000
    single_put_limit: ClassVar[int] = 64 * MiB
    min_part_size: ClassVar[int] = 8 * MiB
    max_part_size: ClassVar[int] = 50 * MiB
    max_parts: ClassVar[int] = 10000
    form_upload_limit: ClassVar[int] = 100 * MiB
    upload_url_seconds: ClassVar[int] = 3600
    workers: ClassVar[int] = 4
    transport: ClassVar[httpx.BaseTransport | None] = None  # tests

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        try:
            self._base: str | None = paths.clean(self.config.get("base_path") or "/")
        except InvalidPath:
            self._base = None  # every operation refuses: never fall back to the whole service
        self._bucket = str(self.config.get("bucket") or "")
        self._password_md5 = hashlib.md5(str(self.config.get("password") or "").encode()).hexdigest()
        self.clock_offset = 0.0
        kwargs: dict[str, Any] = {"transport": self.transport} if self.transport is not None else {}
        self._http = http.client(**kwargs)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        bucket = config.get("bucket")
        if not isinstance(bucket, str) or not bucket.strip():
            errors["bucket"] = _("Please fill in the service name.")
        elif any(ch in bucket for ch in " /\\?#%&\t\r\n"):
            errors["bucket"] = _("The service name can't have spaces or the characters / \\ ? # % &.")
        if (message := check_key(config.get("operator"), _("Operator"))):
            errors["operator"] = message
        if not str(config.get("password") or ""):
            errors["password"] = _("Please fill in the operator password.")
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as exc:
            errors["base_path"] = exc.message
        domain = config.get("domain")
        if domain and (message := check_origin(domain, _("Download domain"), "https://files.example.com")):
            errors["domain"] = message
        return errors

    # -- requests

    def _send(self, method: str, uri: str, *, headers: Mapping[str, str] | None = None,
              body: bytes | UploadBody = b"", expect: Sequence[int] = (200,)) -> httpx.Response:
        """A signed request; the streaming response when its status is expected, else the
        mapped DriverError."""
        url = self.api_url + uri
        skew_fixed = False
        attempt = 0
        busy = 0  # 429s of a DELETE
        while True:
            attempt += 1
            date = email.utils.formatdate(time.time() + self.clock_offset, usegmt=True)
            sent = {"Date": date, **(headers or {})}
            sent["Authorization"] = (f"UPYUN {self.config.get('operator') or ''}:"
                                     f"{signature(self._password_md5, method, uri, date, sent.get('Content-MD5', ''))}")
            if isinstance(body, UploadBody):
                content = body.chunks()
                sent["Content-Length"] = str(body.size)
            else:
                content = body or None
                if method in ("PUT", "POST"):
                    sent["Content-Length"] = str(len(body))
            try:
                request = self._http.build_request(method, url, headers=sent, content=content)
                response = self._http.send(request, stream=True)
            except httpx.TransportError as exc:
                self.log.warning("UpYun %s %s: %s (try %d)", method, uri, type(exc).__name__, attempt)
                if attempt < self.attempts and self._rewind(body):
                    self._pause(attempt)
                    continue
                raise BackendError(_("Connecting to UpYun failed (%(error)s): %(detail)s") % {"error": type(exc).__name__, "detail": str(exc)[:200] or _("no response")}) from exc
            self.log.debug("UpYun %s %s -> %s", method, uri, response.status_code)
            if response.status_code in expect:
                return response
            error = self._error(response, method)
            if response.status_code in (401, 403) and not skew_fixed:
                # A date offset error (HEAD answers carry no code: compare with the server's Date).
                server = _http_date(response.headers.get("date"))
                skewed = server is not None and abs(server - time.time() - self.clock_offset) > 600
                if (error.code == 40100002 or skewed) and server is not None and self._rewind(body):  # type: ignore[attr-defined]
                    self.clock_offset = server - time.time()
                    self.log.warning("UpYun clock skew: our clock is off by %.0f s", -self.clock_offset)
                    skew_fixed = True
                    continue
            if response.status_code == 429 and method == "DELETE" and busy < len(self.delete_waits):
                self.log.warning("UpYun DELETE %s: HTTP 429, waiting (wait %d)", uri, busy + 1)
                time.sleep(self.delete_waits[busy] * random.uniform(0.75, 1.25))  # still longer each time
                busy += 1
                continue
            if (response.status_code in (429, 500, 502, 503, 504) and attempt < self.attempts
                    and self._rewind(body)):
                self.log.warning("UpYun %s %s: HTTP %s (try %d)", method, uri, response.status_code, attempt)
                self._pause(attempt)
                continue
            raise error

    def _request(self, method: str, uri: str, **kwargs: Any) -> Reply:
        """A signed request whose answer is read whole."""
        response = self._send(method, uri, **kwargs)
        try:
            return Reply(response.status_code, response.headers, read_limited(response, MAX_JSON))
        except httpx.HTTPError as exc:
            raise BackendError(_("Reading UpYun's answer failed (%(error)s).") % {"error": type(exc).__name__}) from exc
        finally:
            response.close()

    @staticmethod
    def _rewind(body: bytes | UploadBody) -> bool:
        return body.rewind() if isinstance(body, UploadBody) else True

    @staticmethod
    def _pause(attempt: int) -> None:
        time.sleep(0.2 * 2 ** (attempt - 1) * random.uniform(0.5, 1.5))

    def _error(self, response: httpx.Response, method: str) -> DriverError:
        code, message = None, ""
        try:
            raw = b"" if method == "HEAD" else read_limited(response, 64 * 1024)
        except (httpx.HTTPError, BackendError):
            raw = b""
        finally:
            http.drain(response)  # a HEAD's, not read above
        try:
            answer = json.loads(raw) if raw else None
        except ValueError:
            answer = None
        if isinstance(answer, dict):
            code, message = answer.get("code"), str(answer.get("msg") or "")
        error = upyun_error(response.status_code, code, message)
        self.log.log(logging.DEBUG if response.status_code == 404 else logging.INFO,
                     "UpYun error: HTTP %s %s %s", response.status_code, code, message[:200])
        return error

    def _head(self, path: str) -> Mapping[str, str] | None:
        """The x-upyun-file-* headers of a file or folder, or None."""
        if path == "/" and self._base == "/":
            return {"x-upyun-file-type": "folder"}
        try:
            return self._request("HEAD", self._uri(path)).headers
        except NotFound:
            return None

    # -- paths

    def _remote(self, path: str) -> str:
        """The path inside the service: base path + storage path."""
        if self._base is None:
            raise InvalidPath(_("The storage source's base path is set wrong. Please ask the administrator to check it."))
        if not paths.is_clean(path):
            raise InvalidPath(_("Invalid path."))
        try:
            path.encode("utf-8")
        except UnicodeEncodeError:  # a lone surrogate
            raise InvalidPath(_("The path has characters that can't be encoded.")) from None
        return paths.rebase(path, self._base)

    def _uri(self, path: str, *, folder: bool = False) -> str:
        remote = self._remote(path)
        uri = f"/{quote(self._bucket, safe='')}{quote(remote, safe='/')}"
        if folder and not uri.endswith("/"):
            uri += "/"
        return uri

    # -- reading

    def _children(self, path: str) -> list[dict[str, Any]]:
        """The entries of a folder, every page; [] for a base folder that doesn't exist yet."""
        items: list[dict[str, Any]] = []
        mark = None
        while True:
            headers = {"Accept": "application/json", "x-list-limit": str(self.page_size)}
            if mark:
                headers["x-list-iter"] = mark
            try:
                response = self._request("GET", self._uri(path, folder=True), headers=headers)
            except NotFound:
                if path == "/":
                    return []
                raise
            try:
                answer = json.loads(response.content)
                files = answer.get("files") or []
                for item in files:
                    name = str(item["name"])
                    if name in ("", ".", "..") or "/" in name or "\x00" in name:
                        continue
                    is_dir = item.get("type") == "folder"
                    modified = item.get("last_modified")
                    items.append({"name": name, "is_dir": is_dir, "size": None if is_dir else int(item["length"]),
                                  "modified": _unix_time(modified)})
            except (ValueError, KeyError, TypeError, AttributeError) as exc:
                raise BackendError(_("UpYun's file list couldn't be understood.")) from exc
            next_mark = answer.get("iter") or response.headers.get("x-upyun-list-iter")
            if not files or not next_mark or next_mark == END_MARK:
                return items
            if next_mark == mark:
                raise BackendError(_("UpYun's file list pages are wrong."))
            mark = next_mark

    def list(self, path: str) -> list[Entry]:
        if path != "/":
            info = self._head(path)
            if info is None:
                raise NotFound()
            if info.get("x-upyun-file-type") != "folder":
                raise NotADirectory()
        return [Entry(item["name"], paths.join(path, item["name"]), item["is_dir"], item["size"], item["modified"])
                for item in self._children(path) if not paths.dots_only(item["name"])]  # D46

    def stat(self, path: str) -> Entry:
        if path == "/":
            return Entry("", "/", True)
        info = self._head(path)
        if info is None:
            raise NotFound()
        if info.get("x-upyun-file-type") == "folder":
            return Entry(paths.name(path), path, True, None, _unix_time(info.get("x-upyun-file-date")))
        return Entry(paths.name(path), path, False, _int(info.get("x-upyun-file-size")),
                     _unix_time(info.get("x-upyun-file-date")))

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        info = self._head(path)
        if info is None or info.get("x-upyun-file-type") == "folder" or path == "/":
            raise NotFound()
        total = _int(info.get("x-upyun-file-size"))
        if length == 0:
            return ReadStream(iter(()), size=0, total=total, offset=offset)
        headers = {"Accept-Encoding": "identity"}
        if offset or length is not None:
            headers["Range"] = f"bytes={offset}-{'' if length is None else offset + length - 1}"
        response = self._send("GET", self._uri(path), headers=headers, expect=(200, 206))
        chunks: Iterator[bytes] = response.iter_raw(CHUNK)
        size = _int(response.headers.get("content-length"))
        if response.status_code == 206:
            range_ = response.headers.get("content-range", "")
            if not range_.startswith(f"bytes {offset}-"):
                response.close()
                raise BackendError(_("The range UpYun returned (Content-Range) is wrong."))
        elif offset or length is not None:  # Range ignored: skip and cut here
            size = length if length is not None else (total - offset if total is not None else None)
            chunks = slice_stream(chunks, offset, size)
        return ReadStream(guarded(chunks), size=size, total=total, offset=offset,
                          content_type=response.headers.get("content-type"), etag=response.headers.get("etag"),
                          modified=_unix_time(info.get("x-upyun-file-date")), close=response.close)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        remote = self._remote(path)
        domain = (self.config.get("domain") or "").strip()
        if domain:
            scheme, host = parse_origin(domain)
            origin = f"{scheme}://{host}"
        else:
            origin = f"http://{self._bucket}.test.upcdn.net"
        query = []
        if filename:
            query.append(f"_upd={quote(filename, safe='')}")
        token = str(self.config.get("token") or "")
        if token:
            expires_at = int(time.time() + self.clock_offset) + max(1, int(expires))
            query.append(f"_upt={anti_leech_token(token, expires_at, remote)}")
        return origin + quote(remote, safe="/") + ("?" + "&".join(query) if query else "")

    # -- writing

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        # The FORM API replaces {year}, {filename}, {random32}, ... in save-key, the base path and the
        # path, and the browser chooses the file name it sends: one with braces goes through the site
        # (REST API).
        if size > self.form_upload_limit:
            return None
        save_key = self._remote(path)
        if "{" in save_key or "}" in save_key:
            return None
        info = self._head(path)
        if info is not None and info.get("x-upyun-file-type") == "folder":
            raise AlreadyExists()
        date = email.utils.formatdate(time.time() + self.clock_offset, usegmt=True)
        policy = base64.b64encode(json.dumps({
            "bucket": self._bucket,
            "save-key": save_key,
            "expiration": int(time.time() + self.clock_offset) + self.upload_url_seconds,
            "date": date,
            "content-length-range": f"{size},{size}",
        }, ensure_ascii=False).encode()).decode()
        authorization = f"UPYUN {self.config.get('operator') or ''}:{form_signature(self._password_md5, self._bucket, date, policy)}"
        return UploadTarget(kind="form", url=f"{self.api_url}/{quote(self._bucket, safe='')}", method="POST",
                            fields={"policy": policy, "authorization": authorization}, file_field="file")

    def _make_folders(self, path: str) -> None:
        """Make a folder and the ones above it; fine when they exist."""
        if path == "/" and self._base == "/":
            return
        try:
            self._request("POST", self._uri(path), headers={"folder": "true", "mkdir": "true"})
        except AlreadyExists:
            pass

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == "/":
            raise AlreadyExists()
        info = self._head(path)
        if info is not None and info.get("x-upyun-file-type") == "folder":
            raise AlreadyExists()
        uri = self._uri(path)
        content_type = content_type_of(paths.name(path))
        if size <= self.single_put_limit:
            self._request("PUT", uri, headers={"Content-Type": content_type, "mkdir": "true"},
                          body=UploadBody(data, size), expect=(200, 201, 204))
            return
        self._make_folders(paths.parent(path))
        part_size = max(self.min_part_size, math.ceil(size / self.max_parts / MiB) * MiB)
        if part_size > self.max_part_size:
            raise DriverError(_("The file is too large for UpYun's chunked upload."))
        started = self._request("PUT", uri, expect=(204, 200), headers={
            "X-Upyun-Multi-Disorder": "true", "X-Upyun-Multi-Stage": "initiate", "X-Upyun-Multi-Length": str(size),
            "X-Upyun-Multi-Part-Size": str(part_size), "X-Upyun-Multi-Type": content_type})
        upload = started.headers.get("x-upyun-multi-uuid")
        if not upload:
            raise BackendError(_("UpYun gave no chunked upload ID (X-Upyun-Multi-Uuid)."))
        for number, first in enumerate(range(0, size, part_size)):
            count = min(part_size, size - first)
            self._request("PUT", uri, expect=(204, 200), body=UploadBody(data, count), headers={
                "X-Upyun-Multi-Stage": "upload", "X-Upyun-Multi-Uuid": upload, "X-Upyun-Part-Id": str(number)})
        self._request("PUT", uri, expect=(204, 201, 200),
                      headers={"X-Upyun-Multi-Stage": "complete", "X-Upyun-Multi-Uuid": upload})

    def mkdir(self, path: str) -> None:
        if path == "/" or self._head(path) is not None:
            raise AlreadyExists()
        self._request("POST", self._uri(path), headers={"folder": "true", "mkdir": "true"})

    def _parallel(self, work: Callable[[Any], Any], items: Sequence[Any]) -> None:
        run_parallel(work, items, self.workers)

    def _delete_folder(self, path: str) -> None:
        """The files of each folder, then the folders, deepest first. The folders go on a stack: recursion over
        them hit Python's limit a thousand folders down (STOR-06)."""
        folders, todo = [], [path]
        while todo:
            folder = todo.pop()
            folders.append(folder)
            children = self._children(folder)
            self._parallel(lambda item, folder=folder: self._request(
                "DELETE", self._uri(paths.join(folder, item["name"]))),
                [item for item in children if not item["is_dir"]])
            todo.extend(paths.join(folder, item["name"]) for item in children if item["is_dir"])
        for folder in reversed(folders):
            self._request("DELETE", self._uri(folder))

    def delete(self, path: str) -> None:
        if path == "/":
            raise InvalidPath(_("The storage source's root folder can't be deleted."))
        info = self._head(path)
        if info is None:
            raise NotFound()
        if info.get("x-upyun-file-type") == "folder":
            self._delete_folder(path)
        else:
            self._request("DELETE", self._uri(path))

    def _transfer_file(self, source: str, target: str, move: bool) -> None:
        header = "X-Upyun-Move-Source" if move else "X-Upyun-Copy-Source"
        self._request("PUT", self._uri(target), headers={header: self._uri(source)})

    def _transfer_folder(self, source: str, target: str, move: bool) -> None:
        """Each folder made at the target and its files moved or copied there, from a stack of folders (as
        _delete_folder); moved ones are deleted at the end, deepest first."""
        moved, todo = [], [(source, target)]
        while todo:
            source, target = todo.pop()
            self._request("POST", self._uri(target), headers={"folder": "true", "mkdir": "true"})
            children = self._children(source)
            self._parallel(lambda item, source=source, target=target: self._transfer_file(
                paths.join(source, item["name"]), paths.join(target, item["name"]), move),
                [item for item in children if not item["is_dir"]])
            todo.extend((paths.join(source, item["name"]), paths.join(target, item["name"]))
                        for item in children if item["is_dir"])
            moved.append(source)
        if move:
            for folder in reversed(moved):
                self._request("DELETE", self._uri(folder))

    def _transfer(self, source: str, target: str, *, move: bool) -> None:
        info = self._head(source)
        if info is None:
            raise NotFound()
        if self._head(target) is not None:
            raise AlreadyExists()
        if info.get("x-upyun-file-type") == "folder":
            self._transfer_folder(source, target, move)
        else:
            self._transfer_file(source, target, move)

    def rename(self, path: str, new_name: str) -> None:
        if path == "/":
            raise InvalidPath(_("The root folder can't be renamed."))
        target = paths.join(paths.parent(path), new_name)
        if target != path:
            self._transfer(path, target, move=True)

    def _target_in(self, path: str, folder: str) -> str:
        if path == "/":
            raise InvalidPath(_("The root folder can't be moved or copied."))
        if folder != "/":
            info = self._head(folder)
            if info is None:
                raise NotFound(_("The target folder doesn't exist."))
            if info.get("x-upyun-file-type") != "folder":
                raise NotADirectory(_("The target isn't a folder."))
        if paths.is_inside(folder, path):
            raise DriverError(_("A folder can't be moved or copied into itself."))
        return paths.join(folder, paths.name(path))

    def move(self, path: str, folder: str) -> None:
        target = self._target_in(path, folder)
        if target != path:
            self._transfer(path, target, move=True)

    def copy(self, path: str, folder: str) -> None:
        self._transfer(path, self._target_in(path, folder), move=False)
