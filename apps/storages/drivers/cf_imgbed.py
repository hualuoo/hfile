"""CloudFlare ImgBed (https://github.com/MarSeventh/CloudFlare-ImgBed), a self-hosted image and file host that runs on
Cloudflare Pages or Workers or in Docker and keeps its files in Telegram, Cloudflare R2, S3, Discord, Hugging Face or
WebDAV ("channels"): the files of one ImgBed site, through its HTTP API. Documentation: https://cfbed.sanyue.de/en/api/
(its markdown: https://github.com/MarSeventh/CloudFlare-ImgBed-Docs, src/en/api/ index.md, list.md, upload.md,
delete.md, file.md, token.md, webdav.md, and src/en/deployment/configuration.md). Where it is silent, the facts are from
ImgBed's source, release v2.7.8 (its main branch of 2026-11-03 differs only where said); the paths below are in it.
Each fact is marked [docs] or [source]. OpenList's driver (drivers/cloudflare_imgbed) gave the first leads; each of
them is checked against the documentation or the source.

Paths
    A file is known by its id, its path without the leading "/" ("album/2026/a.jpg") [docs: file.md]. A folder is only
    the start of the ids of the files in it, so there are no empty folders (has_folders False) [source: readIndex in
    functions/utils/indexManager.js, which finds a folder's subfolders in the ids of the files below it]. The storage
    root is the folder `base_path`. A listing is GET /api/manage/list with dir and count=-1, all the folder's files at
    once [docs: list.md], as ImgBed's own WebDAV lists [source: functions/dav/[[path]].js]: start and count page the
    files only, and each page reads the whole index again [source: indexManager.js]. One path is found in the listing
    of its folder narrowed with search=<name>, which keeps the files whose id or name has it, ignoring case [docs;
    source]. A file and a folder may have one name (ids "a" and "a/b"): the folder is shown. Ids with "," are left
    out: the routes that take an id in their path read "," as "/" [source: params.path.split(",") in
    functions/api/manage/delete/[[path]].js, rename/[[path]].js and functions/file/[[path]].js]. A folder whose id has
    ".." can't be listed: list.js makes "_" of it in dir [source: list.js:57], so its listing would be another folder's;
    it is refused with a message (such names are from ImgBed before 2.6, which didn't clean folder names [source:
    uploadTools.js of 2.3.0 to 2.5.0]; ImgBed's own folder move lists a folder through the same kind of URL as its
    folder delete, so it can't move one either [source: functions/api/manage/move/[[path]].js:42]).

Login
    An API token made in the admin panel (System settings → Security settings → API Token Management), shown in full
    once, sent as "Authorization: Bearer <token>" [docs: index.md], never in a URL. Its permissions [docs]: list for
    listings, upload, delete; rename is under /api/manage, whose paths other than delete and list need manage [source:
    functions/api/manage/_middleware.js], and manage also lets a token manage ImgBed's tokens and settings [docs]. A
    wrong, deleted or expired token, or one without the permission, is 401 [source: the middleware,
    functions/utils/auth/authCore.js; docs: token.md].

Changes
    Renames and moves: POST /api/manage/rename/<id> with {"newFileId"}: 404 when the file is gone, 409 when the new id
    is taken; ImgBed moves the file in its channel too, and refuses files of its old Telegram channel and of Telegraph
    [source: functions/api/manage/rename/[[path]].js]. A folder is renamed or moved file by file, as ImgBed's WebDAV
    does [source: dav/[[path]].js]; one that fails halfway is left split, and one whose listing has no file below its
    id (the index lagging) is an error, not a rename that moved nothing. /api/manage/move isn't used: it overwrites a
    taken id [source]. delete: GET /api/manage/delete/<id> [docs: delete.md]; ImgBed deletes for good [source]. A
    folder is listed first (recursive=true). ImgBed's own folder delete (folder=true; "failed" lists the files it
    couldn't delete [docs]) walks the folder by listing each folder with its id pasted into a URL unencoded
    [source: delete/[[path]].js:46, the same on main], where "#" ends the query, "&" the value, "+" is a space, "%XX" is
    decoded again and control characters are dropped, so it would list, and delete, another folder (with a "#" in the
    first segment, the whole root). It is used when the folder's id and those of the folders below it have only names
    _kept() takes, without control characters; else the files listed are deleted one by one, those whose id has ","
    (which the single-file route reads as "/") counted as failed. ImgBed's batch delete isn't used: on a version without
    it, its POST goes to delete/[[path]].js, which deletes a file with the id "batch" and answers success.
    mkdir makes nothing: the folder shows once a file is in it, as ImgBed's WebDAV answers MKCOL [source]. Nothing is
    copied by ImgBed (can_copy False: the site copies files itself). ImgBed updates its listing index after it has
    answered a change [source: waitUntil in the routes above], so a listing right after one may not show it yet.

Downloads
    GET <address>/file/<id> needs no token [docs: file.md]: download_url() gives that link, built here, and ImgBed's
    links are public and permanent: whoever has one can download the file. ImgBed sends it inline under the name it
    was uploaded with [docs], whatever its name is now. Its allowed domains (hotlink protection) don't stop a browser
    that sends no Referer [source: isDomainAllowed in functions/file/fileTools.js], as this site's pages
    (Referrer-Policy same-origin) and redirects (no-referrer) send none. open() asks for the same with Range: 206 where
    the channel supports it [docs], else the 200 is cut down here; open_known() takes the size and TimeStamp from the
    Entry stat() gave, and so lists nothing. Both add ?v=<the file's TimeStamp>, which ImgBed ignores [source:
    functions/file/imageTransform.js reads width, height, fit and fallback]: ImgBed marks files public for 30 days
    [source: FILE_CACHE_CONTROL in fileTools.js], on every deployment, its Worker deployment keeps them in Cloudflare's
    cache by URL [source: deploy/worker/index.js], and a cached file is purged only when ImgBed's CloudFlare API Token
    setting is filled in [docs: configuration.md], so a file replaced under its name could be read as it was. So does
    download_url(), with the TimeStamps of the files listed in the last hour (else the plain link): browsers get a new
    link once a file is replaced. A file ImgBed blocks (on its block list, labelled adult, or not whitelisted in
    whitelist mode) is a 403 with a picture, or a redirect to /blockimg or /whiteliston [docs: file.md; source:
    fileTools.js]: it can't be read. A file of its External channel (a link it keeps) redirects to that link [source:
    functions/file/[[path]].js], which is followed, without the token. Its size is unknown: ImgBed's upload page sends
    an empty file with the link, and the record keeps that size, 0 [source: frontend-dist/js/178.*.js, new File([],
    ...); functions/upload/index.js keeps file.size]. The client is guarded (http.client(guard=True), every request
    with configured(url)): when ImgBed's address is on the Internet, neither the read of /file/<id> nor the link it
    redirects to may lead into the site's own network (http.py's docstring), and such a read is refused with
    InsideNetwork, nothing read there; anyone may give an External file any link when ImgBed has no user auth code
    [source: checkUser in functions/utils/auth/authCore.js]. An ImgBed on the site's own network (its address on the
    LAN) is trusted: its links are followed where they go.

Uploads
    POST /upload [docs: upload.md] with uploadChannel (`channel`; ImgBed's default is telegram), channelName when set
    (else the first channel of that type, or one at random with load balancing on [source: functions/upload/index.js]),
    uploadFolder, uploadNameType=origin (the file's own name: the default puts a number before it) [docs],
    serverCompress=false (else Telegram recompresses images [docs; source: sendPhoto in upload/index.js]) and
    autoRetry=false (else a failed upload is tried again on every other channel type [docs; source: tryRetry]). As
    ImgBed's own upload page does [source: its compiled frontend, frontend-dist/js/178.*.js], a file of more than 20 MiB
    (9 MiB for Discord) goes in chunks of 16 MiB (9 MiB) through initChunked, chunked (chunkIndex from 0) and merge
    [docs]; when a step fails, cleanup drops what ImgBed kept of it [docs]. Each step has the same query: R2 and S3 fix
    the file's id at chunk 0 [source: functions/upload/chunkUpload.js]. ImgBed keeps a session for an hour [source].
    Hugging Face takes no chunks: a file of 20 MiB or more goes straight to it [docs]: getUploadUrl with its SHA-256 and
    its first 512 bytes (data that can't seek is spooled to a temporary file for that), a PUT to the upload URL with its
    headers, or a PUT per part to the part URLs and their ETags posted to the completion URL, then commitUpload [docs].
    Hugging Face's URLs get no token; the completion URL gets it only when it is ImgBed's own completeMultipart, which
    relays the completion to Hugging Face [source: functions/upload/huggingface/getUploadUrl.js and
    completeMultipart.js]. They are checked as the downloads are (Downloads): an ImgBed on the Internet can't have the
    site send a file into its own network. WebDAV channels take no chunks [source]: a file goes in one request, on
    Cloudflare at most 100 MB [docs]. Without an expire parameter, ImgBed's default file expiry applies (main branch)
    [docs]. ImgBed never replaces a file: a taken name makes "name(1).ext" [source: buildUniqueFileId in
    functions/upload/uploadTools.js]. So a file is replaced here as IdDriver replaces one: the upload, then the old file
    deleted and the new one renamed to the name. A rename is tried first, which ImgBed refuses with 409 while the old
    file is there when the token may rename (manage): without that permission, the upload is deleted and the old file
    stays as it was. An upload stored under another name in its folder replaces the file there, though the listing
    didn't show it (ImgBed adds "(1)" only for an id its database has); one stored in another folder, or whose old file
    can't be deleted, is deleted again. A file that replaces one and goes straight to Hugging Face is asked for under a
    stand-in name ImgBed keeps, hfile-<hex>.<extension>, and committed with its own name as fileName: getUploadUrl would
    make it "name(1).ext", which commitUpload refuses, as sanitizeUploadFolder changes "(" and ")" [source:
    getUploadUrl.js:91, commitUpload.js:37-46, 100].

Names
    ImgBed makes "_" of \\ : * ? " ' < > | ( ) [ ] { } # % ^ ` ~ ; @ & = + $ , and spaces in a new name, and of ".."
    in a folder's name and in a rename's new id; a rename drops spaces at either end [source: sanitizeFileName,
    sanitizeUploadFolder in uploadTools.js; rename/[[path]].js]. Such names are refused (forbidden_chars, _kept): the
    file wouldn't be where it was put. Ids are case-sensitive.

Limits
    Not tried against a real ImgBed site: the tests use a fake built from the documentation and the source above.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import itertools
import json
import logging
import secrets
import tempfile
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import filetypes, http, paths
from ._cloud import (
    KeyedLocks,
    TTLCache,
    as_int,
    exact_body,
    json_or_none,
    origin,
    position,
    range_header,
    redirected,
    response_stream,
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
)

SERVICE = gettext_lazy("ImgBed")
MIB = 1024 * 1024
SAMPLE = 512  # bytes of the start of a file that a direct upload to Hugging Face sends first
# What ImgBed makes "_" of in a new name (sanitizeFileName, sanitizeUploadFolder).
FORBIDDEN = '\\:*?"\'<>| ()[]{}#%^`~;@&=+$,'
BLOCKED = ("/blockimg", "/whiteliston")  # ImgBed's pages for a file it won't serve
OLD_CHANNELS = ("Telegram", "")  # files ImgBed can't rename: of its old Telegram channel, and of Telegraph (none)
CHANNELS = (  # uploadChannel's values (the types of ImgBed's channels), with their names
    ("telegram", "Telegram"),
    ("cfr2", "Cloudflare R2"),
    ("s3", "S3"),
    ("discord", "Discord"),
    ("huggingface", "Hugging Face"),
    ("webdav", "WebDAV"),
)


def _kept(name: str) -> bool:
    """Whether ImgBed stores `name` as it is in a new id."""
    return not any(ch in FORBIDDEN for ch in name) and ".." not in name and name == name.strip()


def _walkable(name: str) -> bool:
    """Whether ImgBed's folder delete lists the folder of the segment `name` as it is: it pastes the folder's id into a
    URL unencoded (delete/[[path]].js:46), where FORBIDDEN's "#", "&", "+" and "%" change it, control characters are
    dropped, and list.js makes "_" of "..". The names _kept() takes, without control characters; and with "," (only the
    routes that take an id in their path read it as "/", and the folder delete deletes the files it lists by id)."""
    return _kept(name.replace(",", "_")) and not any(unicodedata.category(ch) == "Cc" for ch in name)


def _stand_in(name: str) -> str:
    """A name ImgBed keeps as it is, for an upload that is to replace the file `name`: hfile-<hex>, with the extension
    of `name` when it is a short one."""
    stem, dot, extension = name.rpartition(".")
    return f"hfile-{secrets.token_hex(8)}" + (dot + extension if stem and len(extension.encode()) <= 16 else "")


def _changed_name(name: str) -> InvalidPath:
    return InvalidPath(gettext('ImgBed would change the name "%(name)s": it doesn\'t keep spaces, ".." or these characters '
                               "in names: %(chars)s") % {"name": name, "chars": " ".join(FORBIDDEN.replace(" ", ""))})


def _quote(file_id: str) -> str:
    """An id as the path of a URL, each segment percent-encoded."""
    return "/".join(urllib.parse.quote(part, safe="") for part in file_id.split("/"))


def _time(stamp: int | None) -> datetime.datetime | None:
    """A file's TimeStamp: milliseconds since 1970."""
    if stamp is None or stamp <= 0:
        return None
    try:
        return datetime.datetime.fromtimestamp(stamp / 1000, tz=datetime.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _child(value: Any, prefix: str) -> str | None:
    """The name of an id directly in the folder whose ids start with `prefix` ("" or "folder/"), when it is one usable
    here."""
    if not isinstance(value, str) or not value.startswith(prefix):
        return None
    name = value[len(prefix):]
    return name if usable_name(name) and "," not in name else None


def _mime(name: str) -> str:
    return filetypes.guess_type(name, strict=False)[0] or "application/octet-stream"


def _unreadable() -> BackendError:
    return BackendError(gettext("The answer from %(service)s couldn't be understood: is the address that of an ImgBed site?")
                        % {"service": SERVICE})


def _form(fields: Mapping[str, str], file: tuple[str, str, BinaryIO, int] | None = None) -> tuple[Iterator[bytes], dict[str, str]]:
    """A multipart/form-data body, as ImgBed's routes read with request.formData(): the fields, then `file` (name, type,
    data, length) as the part "file", streamed from its data."""
    boundary = "hfile-" + secrets.token_hex(16)
    head = "".join(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n' for key, value in fields.items())
    length = 0
    chunks: Iterator[bytes] = iter(())
    if file is not None:
        name, mime, data, length = file
        head += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{name}"\r\nContent-Type: {mime}\r\n\r\n'
        chunks = exact_body(data, length)
    head_bytes = head.encode("utf-8")
    tail = (("\r\n" if file is not None else "") + f"--{boundary}--\r\n").encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}",
               "Content-Length": str(len(head_bytes) + length + len(tail))}
    return itertools.chain([head_bytes], chunks, [tail]), headers


@dataclasses.dataclass(frozen=True)
class _Item:
    """A file or folder of a listing: its Entry, its id, and of a file the TimeStamp and channel ImgBed keeps."""

    entry: Entry
    id: str
    stamp: int | None = None
    channel: str = ""


class _Spool:
    """The bytes of a direct upload to Hugging Face: seekable (spooled to a temporary file when `data` can't seek),
    with their SHA-256 and first 512 bytes, which ImgBed asks for before the upload."""

    def __init__(self, data: BinaryIO, size: int):
        self.size = size
        start = position(data)
        self._own = start is None
        self._file: BinaryIO = tempfile.TemporaryFile(prefix="hfile-imgbed-") if self._own else data
        self._start = 0 if start is None else start
        digest, head = hashlib.sha256(), b""
        try:
            for chunk in exact_body(data, size):
                digest.update(chunk)
                if len(head) < SAMPLE:
                    head += chunk[: SAMPLE - len(head)]
                if self._own:
                    self._file.write(chunk)
        except BaseException:
            self.close()
            raise
        self.sha256, self.head = digest.hexdigest(), head

    def chunks(self, offset: int, length: int) -> Iterator[bytes]:
        self._file.seek(self._start + offset)
        return exact_body(self._file, length)

    def close(self) -> None:
        if self._own:
            self._file.close()

    def __enter__(self) -> _Spool:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ImgBedDriver(StorageDriver):
    type = "cf-imgbed"
    label = gettext_lazy("CloudFlare ImgBed")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://img.example.com",
              help=gettext_lazy("The address of the ImgBed site, as it is opened in a browser. Browsers get its file links "
                                "(/file/...) as they are, so it must be one they can open.")),
        Param("token", gettext_lazy("API token"), kind="password", secret=True,
              help=gettext_lazy("In ImgBed's admin panel, open System settings → Security settings → API Token "
                                "Management and create a token (ImgBed shows it only once) with the list, upload and "
                                "delete permissions, and manage for renaming, moving and replacing files. Manage also "
                                "lets the token manage ImgBed's tokens and settings.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in ImgBed that is this storage source's root, like / or /folder1.")),
        Param("channel", gettext_lazy("Upload channel"), kind="select", default="telegram", options=CHANNELS,
              help=gettext_lazy("The kind of ImgBed channel that keeps the files uploaded here; ImgBed must have one "
                                "(System settings → Upload settings). Files over 20 MiB (9 MiB for Discord) are uploaded "
                                "in chunks, or straight to Hugging Face. WebDAV channels take each file in one request, "
                                "on Cloudflare at most 100 MB.")),
        Param("channel_name", gettext_lazy("Channel name"), required=False, default="", advanced=True,
              help=gettext_lazy("The name of one channel of that kind, when ImgBed has several. Empty: the first one, or "
                                "one at random when ImgBed balances the load.")),
    )

    can_range = True
    can_direct_download = True  # public links, see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = False
    can_search = False
    has_folders = False
    forbidden_chars = FORBIDDEN

    # Tuning; tests make them small.
    chunk_above: ClassVar[int] = 20 * MIB  # a file larger goes in chunks (telegram, cfr2, s3)
    chunk_size: ClassVar[int] = 16 * MIB
    discord_chunk: ClassVar[int] = 9 * MIB  # Discord's chunks, and the size above which a file goes in them
    direct_from: ClassVar[int] = 20 * MIB  # a file of this size or more goes straight to Hugging Face
    upload_timeout: ClassVar[float] = 300.0  # seconds to wait for ImgBed's answer to an upload, which it gives once stored

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._root = str(self.config.get("url") or "").strip().rstrip("/")
        self._token = str(self.config.get("token") or "").strip()
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._channel = str(self.config.get("channel") or "telegram")
        self._channel_name = str(self.config.get("channel_name") or "").strip()
        self._http = http.client(guard=True)  # every request with configured(self._root): see Downloads
        self._writing = KeyedLocks()  # ids being uploaded to
        self._stamps = TTLCache(10_000, 3600.0)  # storage path -> TimeStamp of a file listed lately, for download_url()

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        raw = str(config.get("url") or "").strip()
        try:
            url = httpx.URL(raw) if raw else None
        except httpx.InvalidURL:
            url = None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            errors["url"] = gettext("The address should start with http:// or https://, like https://img.example.com.")
        if not str(config.get("token") or "").isascii():  # it goes in a header, which takes ASCII only
            errors["token"] = gettext("An ImgBed API token has only ASCII letters, digits and symbols. Please copy it again from ImgBed.")
        try:
            base = paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        else:
            changed = next((part for part in paths.segments(base) if not _kept(part)), None)
            if changed is not None:  # uploads would go to the folder of the name ImgBed makes of it
                errors["base_path"] = _changed_name(changed).message
        return errors

    # -- requests

    def _id(self, path: str) -> str:
        """The id of the storage path: a file's id, or the start of the ids of a folder's files ("" for ImgBed's root)."""
        return "/".join(paths.segments(self._base) + paths.segments(path))

    def _call(self, method: str, url: str, *, token: bool, timeout: float | None = None, **kwargs: Any) -> httpx.Response:
        headers = dict(kwargs.pop("headers", {}))
        if token and self._token:  # none at all without one: ImgBed answers 401
            headers["Authorization"] = "Bearer " + self._token
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=http.CONNECT_TIMEOUT)
        try:
            return self._http.send(self._http.build_request(method, url, headers=headers,
                                                            extensions=http.configured(self._root), **kwargs))
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None

    def _send(self, method: str, path: str, permission: str, **kwargs: Any) -> httpx.Response:
        """A call of the API (`path` below the address) with the token: the answer when it succeeded, else _failure()'s
        DriverError. `permission`: the token's permission the call needs, for the message of a 401."""
        response = self._call(method, self._root + path, token=True, **kwargs)
        if response.is_success:
            return response
        if 300 <= response.status_code < 400:
            raise redirected(response, SERVICE)
        raise self._failure(response, permission)

    def _json(self, method: str, path: str, permission: str, **kwargs: Any) -> Any:
        answer = json_or_none(self._send(method, path, permission, **kwargs))
        if answer is None:
            raise _unreadable()
        return answer

    @staticmethod
    def _message(response: httpx.Response) -> str:
        """ImgBed's message in a refusal: "error" or "message" of a JSON answer, or the text ("Error: ...")."""
        answer = json_or_none(response)
        if isinstance(answer, dict):
            text = next((answer[key] for key in ("error", "message") if isinstance(answer.get(key), str) and answer[key]), "")
        else:
            text = response.text if "html" not in response.headers.get("content-type", "") else ""
        return short(text) or "-"

    @classmethod
    def _failure(cls, response: httpx.Response, permission: str) -> DriverError:
        status = response.status_code
        if status == 401:
            if permission == "manage":
                return AuthError(gettext("ImgBed refused the API token for this: renaming, moving and replacing files need a "
                                         "token with the manage permission (System settings → Security settings → API "
                                         "Token Management)."))
            return AuthError(gettext("ImgBed refused the API token: it may be wrong, deleted or expired, or lack the "
                                     "%(permission)s permission. Please check it in ImgBed (System settings → Security "
                                     "settings → API Token Management).") % {"permission": permission})
        if status == 404:
            return NotFound()
        if status == 409:
            return AlreadyExists()
        if status == 413:
            return DriverError(gettext("ImgBed, or Cloudflare or a proxy in front of it, refused an upload request as too "
                                       "large (HTTP 413). Cloudflare takes requests of at most 100 MB, so larger files "
                                       "can't be uploaded to a WebDAV channel."))
        values = {"status_code": status, "message": cls._message(response)}
        if status == 429 or status >= 500:
            return BackendError(gettext("ImgBed failed (HTTP %(status_code)s): %(message)s") % values)
        return DriverError(gettext("ImgBed refused this (HTTP %(status_code)s): %(message)s") % values)

    def _listing(self, folder_id: str, *, search: str | None = None, recursive: bool = False) -> tuple[list[Any], list[Any], bool]:
        """The files of the folder whose id is `folder_id` (all of them below it with `recursive`), its subfolders, and
        whether the answer came from ImgBed's index (when it can't read it, it lists the folder's own files only).
        DriverError for an id with "..": ImgBed makes "_" of it in dir, and would answer for another folder."""
        if ".." in folder_id:
            raise DriverError(gettext('ImgBed can\'t list the folder "%(name)s": it reads ".." in a folder\'s name as "_", '
                                      "so the folder and the files in it can't be opened, renamed, moved or deleted "
                                      "here.") % {"name": folder_id})
        query = {"dir": folder_id, "count": "-1"}
        if recursive:
            query["recursive"] = "true"
        if search:
            query["search"] = search
        try:
            answer = self._json("GET", "/api/manage/list", "list", params=query)
        except NotFound:
            raise BackendError(gettext("No ImgBed API answered at this address (HTTP 404). Please check the address: it "
                                       "must be that of the ImgBed site.")) from None
        files = answer.get("files") if isinstance(answer, dict) else None
        folders = answer.get("directories") if isinstance(answer, dict) else None
        if not isinstance(files, list) or not isinstance(folders, list):
            raise _unreadable()
        return files, folders, answer.get("isIndexedResponse") is not False

    def _items(self, folder: str, folder_id: str, files: list[Any], folders: list[Any]) -> dict[str, _Item]:
        """The files and subfolders of the folder at storage path `folder` by name; a folder hides a file of its name.
        The files' TimeStamps are kept for download_url()."""
        prefix = folder_id + "/" if folder_id else ""
        items: dict[str, _Item] = {}
        for record in files:
            name = _child(record.get("name"), prefix) if isinstance(record, dict) else None
            if name is None:
                continue
            meta = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            stamp = as_int(meta.get("TimeStamp"))
            size = as_int(meta.get("FileSizeBytes"))
            if size is None:
                size = as_int(meta.get("File-Size"))
            channel = meta.get("Channel") if isinstance(meta.get("Channel"), str) else ""
            if channel == "External" and not (size and size > 0):
                size = None  # a link: ImgBed's upload page sends an empty file with it, whose size the record keeps
            entry = Entry(name=name, path=paths.join(folder, name), is_dir=False, size=size, modified=_time(stamp))
            items[name] = _Item(entry, prefix + name, stamp, channel)
            if stamp and stamp > 0:
                self._stamps.put(entry.path, stamp)
        for value in folders:
            name = _child(value[:-1] if isinstance(value, str) and value.endswith("/") else value, prefix)
            if name is not None:
                items[name] = _Item(Entry(name=name, path=paths.join(folder, name), is_dir=True), prefix + name)
        return items

    def _lookup(self, path: str) -> _Item | None:
        """The file or folder at `path` (not the root), or None."""
        parent, name = paths.split(path)
        folder_id = self._id(parent)
        # ImgBed decodes the search term again: a "%" in it could make it fail.
        files, folders, _ = self._listing(folder_id, search=name if "%" not in name else None)
        return self._items(parent, folder_id, files, folders).get(name)

    def _find(self, path: str) -> _Item:
        item = self._lookup(path)
        if item is None:
            raise NotFound()
        return item

    def _file(self, path: str) -> _Item:
        """The file at `path`. NotFound, a folder too."""
        if path == paths.ROOT:
            raise NotFound()
        item = self._find(path)
        if item.entry.is_dir:
            raise NotFound()
        return item

    def _check_names(self, path: str) -> None:
        """InvalidPath when ImgBed would change a name of `path`, which a new id has: a new file or name, and the folders
        it is in."""
        for part in paths.segments(path):
            if not _kept(part):
                raise _changed_name(part)

    def _rename(self, old: str, new: str) -> None:
        answer = self._json("POST", f"/api/manage/rename/{_quote(old)}", "manage", json={"newFileId": new})
        stored = answer.get("newFileId") if isinstance(answer, dict) else None
        if isinstance(stored, str) and stored != new:
            raise DriverError(gettext('ImgBed stored the file as "%(stored)s", not as "%(name)s".') % {"stored": stored, "name": new})

    def _delete(self, file_id: str, *, folder: bool = False) -> dict[str, Any]:
        answer = self._json("GET", f"/api/manage/delete/{_quote(file_id)}", "delete", params={"folder": "true"} if folder else None)
        if not isinstance(answer, dict) or answer.get("success") is not True:
            raise DriverError(gettext("ImgBed refused this: %(message)s")
                              % {"message": short(answer.get("error") if isinstance(answer, dict) else None) or "-"})
        return answer

    def _discard(self, file_id: str) -> None:
        """Delete what an upload left; a failure is only logged."""
        try:
            self._delete(file_id)
        except DriverError as error:
            self.log.warning("couldn't delete %s, left by an upload: %s", file_id, error.message)

    def _files_below(self, item: _Item) -> list[tuple[str, dict[str, Any]]]:
        """(id, metadata) of every file below the folder `item`, from ImgBed's index. BackendError when ImgBed can't
        read its index: it would list the folder's own files only."""
        files, _, indexed = self._listing(item.id, recursive=True)
        if not indexed:
            raise BackendError(gettext("ImgBed can't read its file index right now, so not all of the folder's files can be "
                                       "found. Please try again later."))
        below = []
        for record in files:
            file_id = record.get("name") if isinstance(record, dict) else None
            if isinstance(file_id, str) and file_id.startswith(item.id + "/"):
                meta = record.get("metadata")
                below.append((file_id, meta if isinstance(meta, dict) else {}))
        return below

    # -- reading

    def list(self, path: str) -> list[Entry]:
        folder_id = self._id(path)
        files, folders, _ = self._listing(folder_id)
        items = self._items(path, folder_id, files, folders)
        if not items and path != paths.ROOT:
            item = self._lookup(path)  # nothing in it: no such folder (ImgBed has no empty ones), or a file
            if item is None:
                raise NotFound()
            if not item.entry.is_dir:
                raise NotADirectory()
        return [item.entry for item in items.values()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._find(path).entry

    def _file_url(self, file_id: str, stamp: int | None = None) -> str:
        return f"{self._root}/file/{_quote(file_id)}" + (f"?v={stamp}" if stamp else "")

    @staticmethod
    def _blocked() -> DriverError:
        return DriverError(gettext("ImgBed won't serve this file: it is on ImgBed's block list or labelled adult content, "
                                   "or ImgBed serves whitelisted files only (System settings → Security settings → Access "
                                   "management)."))

    def _get_file(self, url: str, headers: dict[str, str]) -> httpx.Response:
        """A file's link, without the token: a redirect is followed to the link of a file of the External channel, not
        to ImgBed's page for a file it blocks. InsideNetwork when ImgBed is on the Internet and a hop would go to an
        address of the site's own network."""
        def get(target: str | httpx.URL, follow: bool) -> httpx.Response:
            try:
                return self._http.send(self._http.build_request("GET", target, headers=headers,
                                                                extensions=http.configured(self._root)),
                                       stream=True, follow_redirects=follow)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None

        response = get(url, False)
        if response.is_redirect:
            target = response.url.join(response.headers.get("location", ""))
            http.drain(response)
            if origin(target) == origin(self._root) and target.path in BLOCKED:
                raise self._blocked()
            response = get(target, True)
        status = response.status_code
        if status in (403, 404):
            picture = response.headers.get("content-type", "").startswith("image/")  # ImgBed's, not Cloudflare's page
            http.drain(response)
            if status == 404:
                raise NotFound()
            if picture:
                raise self._blocked()
            raise DriverError(gettext("ImgBed refused this (HTTP %(status_code)s): %(message)s") % {"status_code": status, "message": "-"})
        return response

    def _read(self, file_id: str, entry: Entry, stamp: int | None, offset: int, length: int | None) -> ReadStream:
        """The bytes of the file `file_id`, which `entry` describes, from `offset`: its link, with ?v=<stamp>."""
        total = entry.size
        if length == 0 or total == 0:
            return ReadStream(iter(()), size=0, total=total, offset=offset, modified=entry.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        response = self._get_file(self._file_url(file_id, stamp), headers)
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE, modified=entry.modified)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(path)
        return self._read(item.id, item.entry, item.stamp, offset, length)

    def open_known(self, path: str, entry: Entry, offset: int = 0, length: int | None = None) -> ReadStream:
        if path == paths.ROOT or entry.is_dir:
            raise NotFound()
        # The TimeStamp back from the Entry's time, which _time() made of it to the millisecond.
        stamp = round(entry.modified.timestamp() * 1000) if entry.modified is not None else None
        return self._read(self._id(path), entry, stamp, offset, length)

    def download_url(self, path: str, *, filename: str, expires: int = 3600, user_agent: str | None = None) -> str | None:
        return self._file_url(self._id(path), self._stamps.get(path))

    # -- writing

    def _upload_query(self, folder_id: str) -> dict[str, str]:
        query = {"uploadChannel": self._channel, "uploadNameType": "origin", "serverCompress": "false", "autoRetry": "false"}
        if self._channel_name:
            query["channelName"] = self._channel_name
        if folder_id:
            query["uploadFolder"] = folder_id
        return query

    def _upload_call(self, query: Mapping[str, str], fields: Mapping[str, str],
                     file: tuple[str, str, BinaryIO, int] | None = None) -> Any:
        body, headers = _form(fields, file)
        return self._json("POST", "/upload", "upload", params=query, content=body, headers=headers, timeout=self.upload_timeout)

    @staticmethod
    def _stored(answer: Any) -> str:
        """The id an upload is stored as, from ImgBed's answer: [{"src": "/file/<id>", ...}], or the object alone."""
        first = answer[0] if isinstance(answer, list) and answer else answer
        src = first.get("src") if isinstance(first, dict) else None
        if not isinstance(src, str) or "/file/" not in src:
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": SERVICE})
        return urllib.parse.unquote(src.partition("/file/")[2])

    def _cleanup(self, upload_id: str, count: int) -> None:
        """Drop what ImgBed kept of an unfinished chunked upload."""
        try:
            self._send("POST", "/upload", "upload", params={"cleanup": "true", "uploadId": upload_id, "totalChunks": str(count)},
                       content=b"")
        except DriverError as error:
            self.log.warning("couldn't clean up the upload %s: %s", upload_id, error.message)

    def _upload_chunks(self, folder_id: str, name: str, data: BinaryIO, size: int, chunk: int) -> str:
        query = self._upload_query(folder_id)
        count = -(-size // chunk)
        about = {"originalFileName": name, "originalFileType": _mime(name), "totalChunks": str(count)}
        session = self._upload_call({**query, "initChunked": "true"}, about)
        upload_id = session.get("uploadId") if isinstance(session, dict) else None
        if not isinstance(upload_id, str) or not upload_id:
            raise _unreadable()
        try:
            for index in range(count):
                part = (name, "application/octet-stream", data, min(chunk, size - index * chunk))
                self._upload_call({**query, "chunked": "true"}, {**about, "uploadId": upload_id, "chunkIndex": str(index)}, part)
            answer = self._upload_call({**query, "chunked": "true", "merge": "true"}, {**about, "uploadId": upload_id})
        except BaseException:
            self._cleanup(upload_id, count)
            raise
        return self._stored(answer)

    @classmethod
    def _refused_upload(cls, response: httpx.Response) -> DriverError:
        values = {"status_code": response.status_code, "message": cls._message(response)}
        if response.status_code == 429 or response.status_code >= 500:
            return BackendError(gettext("Hugging Face's storage failed (HTTP %(status_code)s): %(message)s") % values)
        return DriverError(gettext("Hugging Face's storage refused the upload (HTTP %(status_code)s): %(message)s") % values)

    def _put(self, url: str, source: _Spool, offset: int, length: int, headers: dict[str, str]) -> httpx.Response:
        """PUT bytes of the file to a URL of Hugging Face's storage, which has its signature in it: no token."""
        response = self._call("PUT", url, token=False, content=source.chunks(offset, length),
                              headers={**headers, "Content-Length": str(length)})
        if not response.is_success:
            raise self._refused_upload(response)
        return response

    def _upload_lfs(self, action: Any, source: _Spool) -> None:
        """The bytes to Hugging Face's storage: to the upload URL, or in parts to the part URLs and then the completion."""
        if not isinstance(action, dict):
            raise _unreadable()
        href, header = action.get("href"), action.get("header") or {}
        if not isinstance(href, str) or not isinstance(header, dict):
            raise _unreadable()
        if "chunk_size" not in header:
            self._put(href, source, 0, source.size, {str(key): str(value) for key, value in header.items()})
            return
        chunk = as_int(header.get("chunk_size")) or 0
        urls = sorted((int(key), value) for key, value in header.items() if key.isascii() and key.isdigit() and isinstance(value, str))
        if chunk <= 0 or [number for number, _ in urls] != list(range(1, -(-source.size // chunk) + 1)):
            raise _unreadable()
        parts = []
        for number, url in urls:
            offset = (number - 1) * chunk
            etag = self._put(url, source, offset, min(chunk, source.size - offset), {}).headers.get("etag")
            if not etag:
                raise _unreadable()
            parts.append({"partNumber": number, "etag": etag})
        headers = {"Accept": "application/vnd.git-lfs+json", "Content-Type": "application/vnd.git-lfs+json"}
        own = origin(href) == origin(self._root)  # ImgBed's completeMultipart, which passes it on to Hugging Face
        response = self._call("POST", href, token=own, content=json.dumps({"oid": source.sha256, "parts": parts}).encode(),
                              headers=headers, timeout=self.upload_timeout)
        if not response.is_success:
            raise self._failure(response, "upload") if own else self._refused_upload(response)

    def _upload_direct(self, folder_id: str, name: str, data: BinaryIO, size: int, *, replacing: bool) -> str:
        """`replacing`: a file `name` is there, which getUploadUrl would make "name(1).ext", and commitUpload then
        refuse (sanitizeUploadFolder changes "(" and ")"): the upload goes up under a stand-in name, its own as
        fileName."""
        with _Spool(data, size) as source:
            mime = _mime(name)
            asked = {"fileName": _stand_in(name) if replacing else name, "fileType": mime, "fileSize": size,
                     "sha256": source.sha256,
                     "fileSample": base64.b64encode(source.head).decode(), "uploadNameType": "origin", "uploadFolder": folder_id}
            if self._channel_name:
                asked["channelName"] = self._channel_name
            info = self._json("POST", "/upload/huggingface/getUploadUrl", "upload", json=asked)
            if not isinstance(info, dict) or info.get("success") is not True \
                    or not isinstance(info.get("fullId"), str) or not isinstance(info.get("filePath"), str):
                raise _unreadable()
            # Else Hugging Face has the bytes already (alreadyExists), or takes the file without LFS (needsLfs false):
            # straight to the commit [docs].
            if info.get("needsLfs") and not info.get("alreadyExists"):
                self._upload_lfs(info.get("uploadAction"), source)
            commit = {"fullId": info["fullId"], "filePath": info["filePath"], "sha256": source.sha256, "fileSize": size,
                      "fileName": name, "fileType": mime}
            if isinstance(info.get("channelName"), str):
                commit["channelName"] = info["channelName"]
            return self._stored(self._json("POST", "/upload/huggingface/commitUpload", "upload", json=commit,
                                           timeout=self.upload_timeout))

    def _upload(self, folder_id: str, name: str, data: BinaryIO, size: int, *, replacing: bool) -> str:
        """Upload a file into the folder with the id `folder_id`: the id ImgBed stored it as. `replacing`: the listing
        shows a file of that name."""
        if self._channel == "huggingface" and size >= self.direct_from:
            return self._upload_direct(folder_id, name, data, size, replacing=replacing)
        if self._channel == "discord":
            above, chunk = self.discord_chunk, self.discord_chunk
        elif self._channel in ("telegram", "cfr2", "s3"):
            above, chunk = self.chunk_above, self.chunk_size
        else:
            above = chunk = 0  # no chunks
        if chunk and size > above:
            return self._upload_chunks(folder_id, name, data, size, chunk)
        return self._stored(self._upload_call(self._upload_query(folder_id), {}, (name, _mime(name), data, size)))

    def _replace(self, stored: str, target: str) -> None:
        """Put the file just uploaded as `stored` in the place of the file `target`: ImgBed never replaces one."""
        try:
            self._rename(stored, target)  # ImgBed answers 409 while `target` is there, if the token may rename
            return  # it was gone meanwhile
        except AlreadyExists:
            pass
        except DriverError:
            self._discard(stored)  # the old file stays as it was
            raise
        try:
            self._delete(target)
        except DriverError:
            self._discard(stored)
            raise
        try:
            self._rename(stored, target)
        except DriverError as error:
            raise DriverError(gettext('The file was uploaded as "%(stored)s", but ImgBed didn\'t rename it to "%(name)s" in '
                                      'place of the old one: %(message)s') % {"stored": stored, "name": target, "message": error.message}) from None

    def _forget(self, path: str) -> None:
        """Drop the TimeStamps kept of `path` and of what is below it: they changed."""
        self._stamps.discard_if(lambda key: paths.is_inside(key, path))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_names(path)
        parent, name = paths.split(path)
        target = self._id(path)
        with self._writing.hold(target):
            old = self._lookup(path)
            if old is not None and old.entry.is_dir:
                raise AlreadyExists()
            stored = self._upload(self._id(parent), name, data, size, replacing=old is not None)
            self._forget(path)
            if stored == target:
                return
            if stored.rpartition("/")[0] != target.rpartition("/")[0]:
                self._discard(stored)
                raise DriverError(gettext('ImgBed stored the file as "%(stored)s", not as "%(name)s".') % {"stored": stored, "name": target})
            # ImgBed makes another name when its database has `target`, which the listing may not show yet.
            self._replace(stored, target)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_names(path)
        if self._lookup(path) is not None:
            raise AlreadyExists()
        # Nothing to make: ImgBed has no empty folders (see the module's docstring).

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        item = self._find(path)
        self._forget(path)
        if not item.entry.is_dir:
            self._delete(item.id)
            return
        files = self._files_below(item)
        folders = {item.id} | {file_id.rpartition("/")[0] for file_id, _ in files}
        if all(_walkable(part) for folder in folders for part in folder.split("/")):
            failed = self._delete(item.id, folder=True).get("failed")
            count = len(failed) if isinstance(failed, list) else 0
        else:  # ImgBed's folder delete would list, and delete, another folder (see the module's docstring)
            count = 0
            for file_id, _ in files:
                if "," in file_id:  # the single-file route reads "," as "/": another file
                    count += 1
                    continue
                try:
                    self._delete(file_id)
                except (AuthError, BackendError):
                    raise
                except DriverError:
                    count += 1
        if count:
            raise DriverError(gettext("ImgBed couldn't delete %(count)s of the folder's files. Please try again.") % {"count": count})

    @staticmethod
    def _check_channel(channel: Any) -> None:
        if not isinstance(channel, str) or channel in OLD_CHANNELS:
            raise DriverError(gettext("ImgBed can't rename or move files kept in its old Telegram channel or on Telegraph."))

    def _relocate(self, item: _Item, new_path: str) -> None:
        """Rename or move `item` to `new_path` through ImgBed's rename: a folder file by file."""
        new_id = self._id(new_path)
        self._forget(item.entry.path)
        if not item.entry.is_dir:
            self._check_channel(item.channel)
            self._rename(item.id, new_id)
            return
        moves = []
        for file_id, meta in self._files_below(item):
            self._check_channel(meta.get("Channel"))
            rest = file_id[len(item.id):]
            self._check_names(rest)  # the new id has them again: ImgBed would change them
            moves.append((file_id, new_id + rest))
        if not moves:  # its index shows the folder, but no file below its id yet
            raise DriverError(gettext('ImgBed listed no file in "%(name)s", so nothing was moved. Please try again later.')
                              % {"name": item.entry.path})
        for done, (old, new) in enumerate(moves):
            try:
                self._rename(old, new)
            except DriverError as error:
                if not done:
                    raise
                raise DriverError(gettext('Only %(done)s of the folder\'s %(total)s files were moved before ImgBed refused one '
                                          '(%(message)s); the others are still in "%(name)s".')
                                  % {"done": done, "total": len(moves), "message": error.message, "name": item.entry.path}) from None

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        item = self._find(path)
        if new_name == item.entry.name:
            return
        target = paths.join(paths.parent(path), new_name)
        self._check_names(target)
        if self._lookup(target) is not None:
            raise AlreadyExists()
        self._relocate(item, target)

    def move(self, path: str, folder: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be moved into itself."))
        item = self._find(path)
        if paths.parent(path) == folder:
            return  # already there
        if folder != paths.ROOT:
            place = self._lookup(folder)
            if place is None:
                raise NotFound()
            if not place.entry.is_dir:
                raise NotADirectory()
        target = paths.join(folder, item.entry.name)
        self._check_names(target)
        if self._lookup(target) is not None:
            raise AlreadyExists()
        self._relocate(item, target)
