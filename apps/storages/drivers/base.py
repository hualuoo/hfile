"""Storage drivers: one class per kind of storage (local folder, S3, OneDrive, ...).

A driver knows how to talk to one backend and nothing else. It knows nothing about users,
permissions, folder passwords, filters or links: the site (the storages and files apps) does all
of that and calls the driver with clean storage paths. Drivers use only the standard library,
the packages in requirements.txt and Django's translations (for their messages). Their tests are
plain unittest tests that need no database, run with manage.py test so that the messages can be
translated (`python -B manage.py test apps.storages.tests.test_driver_local`).

Paths. A storage path starts with "/", has no empty, "." or ".." segments and no trailing "/".
"/" is the root of the storage source, i.e. the folder, bucket prefix or drive folder the admin
configured; the driver maps storage paths onto it. apps.storages.drivers.paths cleans and joins
paths, and the site only ever passes clean ones. Names are not Unicode-normalised: a name is
used exactly as the backend has it.

Threads. One driver instance serves many requests at once (waitress runs `[server] threads`
threads) and lives as long as its storage source is unchanged, so every method must be
thread-safe. Keep per-connection state (FTP, SFTP) in a pool, or open a connection per call.

Errors. Raise DriverError or a subclass with a message in Chinese for the user; the site turns
them into answers (NotFound 404, AlreadyExists 409, AuthError and BackendError 502, ...). Any
other exception is a bug: a 500, with the traceback in the log.
"""

from __future__ import annotations

import datetime
import secrets
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, BinaryIO, ClassVar

from django.utils.translation import gettext, gettext_lazy


# --- errors -------------------------------------------------------------------------------------

class DriverError(Exception):
    """A failure to show to the user; str(error) is the message, in Chinese."""

    def __init__(self, message: str = gettext_lazy("The storage operation failed.")):
        super().__init__(message)
        self.message = message


class InvalidPath(DriverError):
    """A path or name the site or the backend doesn't accept (".." segments, "/" in a name, ...)."""


class NotFound(DriverError):
    def __init__(self, message: str = gettext_lazy("The file or folder doesn't exist.")):
        super().__init__(message)


class AlreadyExists(DriverError):
    def __init__(self, message: str = gettext_lazy("A file or folder with that name already exists.")):
        super().__init__(message)


class NotADirectory(DriverError):
    def __init__(self, message: str = gettext_lazy("This isn't a folder.")):
        super().__init__(message)


class Unsupported(DriverError):
    def __init__(self, message: str = gettext_lazy("This kind of storage doesn't support this.")):
        super().__init__(message)


class AuthError(DriverError):
    """The backend refused the credentials: wrong keys, or an expired authorisation."""

    def __init__(self, message: str = gettext_lazy("The storage's account or key is wrong, or its authorization has expired. Please ask the administrator to check the storage source's settings.")):
        super().__init__(message)


class BackendError(DriverError):
    """The backend failed or couldn't be reached (timeouts, 5xx, protocol errors)."""

    def __init__(self, message: str = gettext_lazy("Couldn't connect to the storage. Please try again later.")):
        super().__init__(message)


# --- what drivers return --------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    """One setting of a storage source, shown on the admin form and stored in its config.

    The site checks the type before the driver sees it: text, password, textarea and path give
    str; number gives int; bool gives bool; select gives one of the option values. Cross-field
    rules go in StorageDriver.validate_config.
    """

    key: str
    label: str
    kind: str = "text"  # text | password | textarea | number | bool | select | path
    required: bool = True
    default: Any = None
    help: str = ""
    placeholder: str = ""
    options: tuple[tuple[str, str], ...] = ()  # select: (value, label)
    # Stored encrypted and never sent back to the browser: the form shows "已设置" and keeps the
    # old value when the field is left empty.
    secret: bool = False
    advanced: bool = False  # under "更多设置" on the form
    minimum: int | None = None  # number
    maximum: int | None = None  # number


def usable_time(moment: datetime.datetime | None) -> datetime.datetime | None:
    """`moment`, or None when it is no time in UTC: a storage may send 31 Dec 9999 23:59:59 -2359, which is in the
    year 10000 there, and a download then couldn't write its Last-Modified header, a 500 (STOR-05)."""
    if moment is not None:
        try:
            moment.astimezone(datetime.timezone.utc)
        except (OverflowError, ValueError):
            return None
    return moment


@dataclass(frozen=True)
class Entry:
    """A file or folder."""

    name: str
    path: str  # storage path, e.g. "/docs/a.txt"
    is_dir: bool
    size: int | None = None  # bytes; None when unknown (folders usually; also files without a
    # stored size, e.g. Google Docs, which are exported on download)
    modified: datetime.datetime | None = None  # timezone-aware; None when unknown (usable_time())

    def __post_init__(self):
        object.__setattr__(self, "modified", usable_time(self.modified))


class ReadStream:
    """The bytes of a file, or of a part of it, as an iterator of chunks.

    size: bytes in this stream; total: bytes in the whole file; offset: where the stream starts
    in the file. The site always calls close(), also when the client goes away halfway.
    """

    def __init__(self, chunks: Iterable[bytes], *, size: int | None, total: int | None,
                 offset: int = 0, content_type: str | None = None, etag: str | None = None,
                 modified: datetime.datetime | None = None, close: Callable[[], None] | None = None):
        self._chunks = iter(chunks)
        self.size = size
        self.total = total
        self.offset = offset
        self.content_type = content_type
        self.etag = etag
        self.modified = usable_time(modified)
        self._close = close
        self.closed = False

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._chunks:
            if chunk:
                yield chunk

    def read(self) -> bytes:
        """All of it at once; for small files and tests."""
        try:
            return b"".join(self)
        finally:
            self.close()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        close = getattr(self._chunks, "close", None)
        try:
            if close is not None:
                close()
        finally:
            if self._close is not None:
                self._close()

    def __enter__(self) -> ReadStream:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


@dataclass(frozen=True)
class UploadTarget:
    """How a browser uploads one file straight to the backend, bypassing the site.

    kind "single": one request, `method` to `url` with `headers`, the file as the body.
    kind "form": a multipart POST to `url` with `fields`, the file in the field `file_field`.
    kind "chunks": PUT the file to `url` in pieces of `chunk_size` bytes, each with
    `Content-Range: bytes a-b/total` (OneDrive upload sessions, Google resumable uploads).

    The backend must allow the site's origin (CORS); drivers that need it set up say so in
    their params' help text.
    """

    kind: str
    url: str
    method: str = "PUT"
    headers: Mapping[str, str] = field(default_factory=dict)
    fields: Mapping[str, str] = field(default_factory=dict)
    file_field: str = "file"
    chunk_size: int | None = None
    expires_in: int | None = None  # seconds the URL stays valid, when it expires


# --- the driver -----------------------------------------------------------------------------------

def only_case_differs(name: str, new_name: str) -> bool:
    """Whether `new_name` is `name` spelled in other case ("a.txt", "A.txt")."""
    return new_name != name and new_name.casefold() == name.casefold()


def rename_through(rename: Callable[[str, str], None], path: str, new_name: str) -> None:
    """Rename `path` to `new_name`, its name in other case, on a backend that ignores case, through a third name:
    straight to the new spelling the rename does nothing there (Linux) or is refused as taken (OpenSSH's link(),
    WebDAV's "Overwrite: F"). `rename(old, new)` renames storage paths. When the second step fails, the entry gets
    its name back; when that fails too, the DriverError says what it is called now."""
    folder = path.rpartition("/")[0]
    middle = f"{folder}/.hfile-rename-{secrets.token_hex(6)}"
    rename(path, middle)
    try:
        rename(middle, f"{folder}/{new_name}")
    except Exception:
        try:
            rename(middle, path)
        except Exception:
            raise DriverError(gettext('The rename stopped halfway: it is called "%(name)s" now. Please rename it '
                                      'again.') % {"name": middle.rpartition("/")[2]}) from None
        raise


class StorageDriver:
    """Base class of the drivers. Subclasses set the class attributes and implement the methods.

    `config` is the storage source's settings (keys of `params`), secrets decrypted. When the
    driver changes them itself (a refreshed OAuth token), it calls update_config(), and the site
    saves the change.
    """

    type: ClassVar[str] = ""  # the storage source's type, e.g. "local"
    label: ClassVar[str] = ""  # e.g. "本地存储"
    params: ClassVar[tuple[Param, ...]] = ()

    # What the backend can do. The site hides what a storage can't do and never calls those
    # methods; when it does by mistake they raise Unsupported. A driver may set these on the
    # instance from its config (FTP's can_range follows its "supports REST" param), so the site
    # reads them from the instance, never from the class.
    can_range: ClassVar[bool] = True  # open() honours offset and length
    can_direct_download: ClassVar[bool] = False  # download_url() gives a URL of the backend
    can_direct_upload: ClassVar[bool] = False  # upload_target() gives a way to upload to it
    can_move: ClassVar[bool] = True
    can_copy: ClassVar[bool] = True
    # False: the backend can only be read (a share link, a release page). Nobody gets the permissions that
    # change a storage of this type (apps/storages/access.py CHANGES), so write, mkdir, delete, rename, move and
    # copy are never called. Read from the class: it can't depend on the params.
    can_write: ClassVar[bool] = True
    # False: files can't be uploaded (FebBox's open platform), though the rest of can_write works. Nobody gets the
    # "Upload" permission on a storage of this type, so write() is never called; such a driver copies itself
    # (can_copy). Read from the class, like can_write.
    can_upload: ClassVar[bool] = True
    # False: a link download_url() gives may work once only (Cloudreve v3's "one-time download"), so the site asks
    # for another each time a file is opened, where it would hand out the one it got minutes ago
    # (apps/files/download.py). Read from the class, like can_write.
    reusable_links: ClassVar[bool] = True
    can_search: ClassVar[bool] = False  # search() asks the backend; else the site walks folders
    # False: the backend has no empty folders (an object store without folder markers); a folder
    # exists while something is in it.
    has_folders: ClassVar[bool] = True
    # Characters the backend refuses in names (OneDrive: '"*:<>?\\|'). The site refuses them
    # in new names with a clear message before asking the backend.
    forbidden_chars: ClassVar[str] = ""
    # Longest name in UTF-8 bytes.
    max_name_bytes: ClassVar[int] = 255
    # How many transfers (an open() stream being read, a write()) the instance can have going at once, when it has a
    # limit of its own: FTP's and SFTP's pools of connections (max_connections), where a download holds its connection
    # until its stream is closed. None: no limit. Copy tasks copy at most that many files at once (apps/copytasks).
    max_transfers: int | None = None

    # OAuth drivers (OneDrive, SharePoint, Google Drive): the admin fills in the app's id and
    # secret and presses the button; the browser goes to authorize_url(), the provider sends it
    # back to the site's callback with a code, and exchange_code() gives the params to save
    # (the refresh token, ...).
    oauth: ClassVar[bool] = False

    # Buttons on the admin form that ask the backend something with the values typed so far,
    # e.g. {"buckets": "列出存储桶"}. See helper().
    helpers: ClassVar[Mapping[str, str]] = {}

    # The add wizard (manage page, #storages/new): its second step asks for `setup_params` (the keys and
    # what connecting needs; None: the required params) and saves; its third step fills `auth_params`
    # by 授权 (oauth) or the "qrcode" helper; the other params wait for its fourth step, 其他设置.
    setup_params: ClassVar[tuple[str, ...] | None] = None
    auth_params: ClassVar[tuple[str, ...]] = ()

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        self.config = dict(config)
        self._on_update = on_update
        # Held while the driver reads or changes the tokens it keeps in config (an access token with its expiry and
        # refresh token: they go together). The site gives every instance of a storage the same one right after
        # making it, and holds it to put in tokens another instance refreshed (apps/storages/instances.py): so a
        # driver never reads half of them, and two instances don't refresh with the same refresh token at once
        # (Baidu's and 115's work once). Re-entrant: the site takes it again to hand on what a driver changed under it.
        self.token_lock = threading.RLock()

    def update_config(self, changes: Mapping[str, Any]) -> None:
        """Keep and save settings the driver changed itself (refreshed tokens)."""
        self.config.update(changes)
        if self._on_update is not None:
            self._on_update(dict(changes))

    # -- the admin form

    @classmethod
    def defaults(cls) -> dict[str, Any]:
        return {p.key: p.default for p in cls.params}

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        """Rules across params, after the site checked each one's type: {param key: message}."""
        return {}

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        raise Unsupported()

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        raise Unsupported()

    @classmethod
    def helper(cls, name: str, config: Mapping[str, Any], args: Mapping[str, Any]) -> dict[str, Any]:
        """Run one of `helpers` with the form's current values; the answer is JSON for the form.

        Helpers make the server connect to what the form says (an endpoint, a tenant), so the site
        runs them for admins only. `args` holds what the site adds for every helper, e.g.
        {"origin": "https://files.example.com"} (the site's own origin, for CORS rules), plus the
        `args` of a poll answer below. Answers:

        {"param": "bucket", "choices": [{"value": "b1", "label": "b1（华东）"}, ...]}
            offers choices for a param;
        {"message": "..."} shows a message;
        {"changes": {...}} fills params in (a finished QR-code login);
        {"image": "data:image/png;base64,...", "poll": "<helper>", "args": {...}, "message": "..."}
            shows a picture (a QR code) and calls helper `poll` with `args` every few seconds.
        """
        raise Unsupported()

    # -- files

    def check(self) -> None:
        """Test the settings by asking the backend for the root; raises DriverError if wrong."""
        self.list("/")

    def list(self, path: str) -> list[Entry]:
        """The files and folders in a folder, in any order. NotFound, NotADirectory."""
        raise NotImplementedError

    def stat(self, path: str) -> Entry:
        """One file or folder; "/" is the root folder. NotFound."""
        raise NotImplementedError

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        """The bytes of a file from `offset`, `length` of them or to the end.

        The site checks the range against the file's size first: 0 <= offset < size (or 0 for
        an empty file), and offset + length <= size. Drivers with can_range = False only ever
        get offset 0 and no length. NotFound; a folder gives NotFound too.
        """
        raise NotImplementedError

    def open_known(self, path: str, entry: Entry, offset: int = 0, length: int | None = None) -> ReadStream:
        """open(), for a file the site has just looked up: `entry` is what stat(path) gave (a ranged download
        asks first). A driver whose open() would ask the backend the same (WebDAV's PROPFIND) uses it instead."""
        return self.open(path, offset, length)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        """A URL of the backend the browser can download the file from, valid for about
        `expires` seconds, that saves it as `filename` (Content-Disposition attachment) where
        the backend can do that; None when there isn't one (the site then sends the file).

        `user_agent` is the browser's User-Agent. Backends whose links only work for the
        User-Agent that asked for them (115) need it, and give None without it."""
        return None

    def timed_download_url(self, path: str, *, filename: str, expires: int = 3600,
                           user_agent: str | None = None) -> tuple[str | None, float | None]:
        """download_url(), and for how many more seconds that link may be handed out again, where the backend
        decides how long its links last and says so (Cloudreve's "File Blob URL TTL", which may be shorter than
        `expires`); None when the driver knows no better than `expires`. The site keeps the links it hands out
        again (apps/files/download.py) no longer than that."""
        return self.download_url(path, filename=filename, expires=expires, user_agent=user_agent), None

    def upload_target(self, path: str, size: int) -> UploadTarget | None:
        """How the browser can upload `size` bytes to `path` itself, or None (the site then
        receives the file and calls write()). Missing folders above `path` are created."""
        return None

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        """Store `size` bytes read from `data` at `path`, replacing a file that is there.
        Missing folders above it are created. A folder at `path` gives AlreadyExists."""
        raise NotImplementedError

    def mkdir(self, path: str) -> None:
        """Create a folder, and the missing ones above it. AlreadyExists if `path` exists."""
        raise NotImplementedError

    def delete(self, path: str) -> None:
        """Delete a file, or a folder with everything in it. NotFound."""
        raise NotImplementedError

    def rename(self, path: str, new_name: str) -> None:
        """Rename a file or folder in its folder. NotFound; AlreadyExists if the new name is taken."""
        raise NotImplementedError

    def move(self, path: str, folder: str) -> None:
        """Move a file or folder into another folder, keeping its name.

        NotFound when either is missing; AlreadyExists when the folder has that name already;
        DriverError when moving a folder into itself or below itself."""
        raise Unsupported()

    def copy(self, path: str, folder: str) -> None:
        """Copy a file or folder (with everything in it) into another folder; errors as move()."""
        raise Unsupported()

    def search(self, folder: str, keyword: str, limit: int) -> list[Entry]:
        """Files and folders at or below `folder` whose names contain `keyword`, ignoring case;
        at most `limit`. Only for can_search drivers."""
        raise Unsupported()

    def close(self) -> None:
        """Release connections; the instance isn't used afterwards."""
