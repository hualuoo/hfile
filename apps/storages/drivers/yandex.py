"""Yandex Disk (https://disk.yandex.com), through its REST API (https://yandex.com/dev/disk-api/doc/en/).

Paths
    The storage root ("/") is the disk's folder `base_path`: "/a/b" is disk:/base/a/b. The API addresses
    everything by path. Folders are listed `page_size` entries a call.

Login
    The admin's own app on Yandex's OAuth service (https://oauth.yandex.com/client/new), with the Yandex
    Disk REST API's permissions to read and write anywhere on the disk, and this site's callback as its
    Redirect URI: "Authorize" sends the browser to Yandex, whose code gives the tokens. The access token is
    valid for about a year; it is saved with its expiry and sent as "Authorization: OAuth <token>". The
    refresh token renews it before then, and when the API refuses it (401); each refresh gives a new refresh
    token too ("Яндекс OAuth возвращает токен и новый refresh-токен", https://yandex.ru/dev/id/doc/ru/
    tokens/refresh-client), saved with it. Tokens are read and refreshed under token_lock, which the
    storage's instances share: they don't refresh with the same refresh token at once, and one that finds
    the tokens renewed by another uses them.

Changes
    mkdir: PUT /resources (one folder at a time, so the missing ones above are made first). move (and
    rename, a move in the same folder) and copy: POST /resources/move and /resources/copy, with
    overwrite=false, so a taken name is a 409; delete: DELETE /resources (into the disk's trash). Big
    folders are moved, copied or deleted in the background: the API answers 202 with a link to the
    operation, which is asked about until it is over, for at most `operation_wait` seconds. The link is
    asked as it is, in either form (.../operations/<id>, or .../operations?id=<id> as the docs' examples
    write it), when it is on the API's own host: it goes with the token. A link elsewhere isn't followed;
    the paths tell instead when the change is done (the target there, the source or the deleted path gone).

Downloads
    GET /resources/download gives a link of Yandex's download servers, which needs no token: download_url()
    is that link, which saves the file under its name on the disk; open() fetches it, with the Range.

Uploads
    GET /resources/upload (overwrite=true) gives a link that the file is PUT to, streamed, without the token;
    Yandex then needs a moment to process it (202). Missing folders above it are made first.

Names
    Any name but "/" (names of dots only are the site's to refuse), of at most 255 characters: Yandex counts
    a name's characters, not its bytes ("Максимальная длина имени загружаемого файла — 255 символов",
    reference/upload). New names are checked for that here; the site checks the bytes, up to 1020 (255
    characters of 4 bytes).
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import (
    as_int,
    exact_body,
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

SERVICE = gettext_lazy("Yandex Disk")
FIELDS = "name,path,type,size,modified"
MAX_NAME_CHARS = 255  # Yandex counts a name's characters (reference/upload)


class YandexDiskDriver(StorageDriver):
    type = "yandex-disk"
    label = gettext_lazy("Yandex Disk")
    params = (
        Param("client_id", gettext_lazy("Client ID"),
              help=gettext_lazy("Of your app on Yandex OAuth (https://oauth.yandex.com/client/new): choose the "
                                "Yandex Disk REST API's permissions to read and write anywhere on the disk, and add "
                                "this site's authorization callback URL as a Redirect URI.")),
        Param("client_secret", gettext_lazy("Client secret"), kind="password", secret=True,
              help=gettext_lazy("The client secret of the same app.")),
        Param("access_token", gettext_lazy("Access token"), kind="password", required=False, secret=True,
              help=gettext_lazy('With the client ID and secret filled in, click "Authorize", log in to Yandex and allow '
                                "the app: it's filled in for you.")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Filled in when authorizing. Yandex gives a new one with every new access token; HFile "
                                "saves it itself.")),
        # Secret only so that the form keeps the saved value (see open115's).
        Param("token_expires_at", gettext_lazy("Access token expiry"), kind="number", required=False, advanced=True,
              minimum=0, secret=True,
              help=gettext_lazy("Unix time (seconds), filled in when authorizing and refreshing the token; no need to change it.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the disk that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    # Yandex Disk takes any name but "/"; names of dots only are the site's to refuse.
    forbidden_chars = ""
    max_name_bytes = 1020  # 255 characters of up to 4 bytes; _check_new_name() counts the characters

    oauth = True
    auth_params = ("access_token", "refresh_token", "token_expires_at")

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://cloud-api.yandex.net/v1/disk"
    auth_endpoint: ClassVar[str] = "https://oauth.yandex.com/authorize"
    token_endpoint: ClassVar[str] = "https://oauth.yandex.com/token"
    page_size: ClassVar[int] = 1000
    operation_wait: ClassVar[float] = 60.0
    poll_interval: ClassVar[float] = 0.5
    refusal_ttl: ClassVar[float] = 30.0  # seconds a refused refresh is remembered

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._http = http.client()
        self._refused: tuple[float, str] | None = None  # (until, message) of a refused refresh

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors: dict[str, str] = {}
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            errors["base_path"] = gettext("Invalid base path: %(message)s") % {"message": error.message}
        expires = config.get("token_expires_at")
        if expires is not None and (type(expires) is not int or expires < 0):
            errors["token_expires_at"] = gettext("The access token expiry should be an integer of at least 0 (Unix seconds).")
        return errors

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        client_id = str(config.get("client_id") or "").strip()
        if not client_id:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        query = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri, "state": state,
                 "force_confirm": "yes"}
        return cls.auth_endpoint + "?" + urllib.parse.urlencode(query)

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        answer = cls._token_request(config, {"grant_type": "authorization_code", "code": code})
        refresh = answer.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AuthError(gettext("Yandex gave no refresh token. Please authorize again."))
        return cls._tokens(answer, refresh)

    # -- login

    @staticmethod
    def _tokens(answer: Mapping[str, Any], refresh: str) -> dict[str, Any]:
        """The params to save from a token answer: the access token, its expiry and the refresh token (Yandex's new
        one, or `refresh` when it gives none)."""
        new = answer.get("refresh_token")
        return {"access_token": answer["access_token"],
                "refresh_token": new if isinstance(new, str) and new else refresh,
                "token_expires_at": int(time.time()) + (as_int(answer.get("expires_in")) or 3600)}

    @classmethod
    def _token_request(cls, config: Mapping[str, Any], fields: dict[str, str],
                       client: httpx.Client | None = None) -> dict[str, Any]:
        client_id = str(config.get("client_id") or "").strip()
        secret = str(config.get("client_secret") or "")
        if not client_id or not secret:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        data = {"client_id": client_id, "client_secret": secret, **fields}
        try:
            if client is not None:
                response = client.post(cls.token_endpoint, data=data)
            else:
                with http.client() as own:
                    response = own.post(cls.token_endpoint, data=data)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from error
        body = json_or_none(response)
        if response.status_code >= 500 or response.status_code == 429:
            raise BackendError(gettext("Yandex's authorization service is unavailable for now (HTTP %(status_code)s). Please try again later.")
                               % {"status_code": response.status_code})
        token = body.get("access_token") if isinstance(body, dict) else None
        if response.status_code != 200 or not isinstance(token, str) or not token:
            reason = body.get("error") if isinstance(body, dict) else None
            if reason == "invalid_grant":
                raise AuthError(gettext("Yandex's authorization has expired or been revoked (invalid_grant). Please authorize again in the storage source's settings."))
            raise AuthError(gettext("Yandex refused the authorization (%(code)s). Please check the client ID and secret.")
                            % {"code": reason or response.status_code})
        return body

    def _access_token(self) -> str:
        with self.token_lock:
            token = self.config.get("access_token")
            expires = as_int(self.config.get("token_expires_at")) or 0
            if token and (expires == 0 or time.time() < expires - 300):  # unknown expiry: try it
                return token
            return self._refresh_locked()

    def _refresh_after_refusal(self, token: str) -> None:
        """Yandex refused `token`: refresh, unless another thread or instance already did."""
        with self.token_lock:
            if self.config.get("access_token") == token:
                self._refresh_locked()

    def _refresh_locked(self) -> str:
        """New tokens from the refresh token, saved before token_lock is let go: the storage's other instances, which
        share the lock, then find them instead of refreshing with the refresh token this one has just used."""
        if self._refused is not None and time.monotonic() < self._refused[0]:
            raise AuthError(self._refused[1])
        refresh = str(self.config.get("refresh_token") or "")
        if not refresh:
            raise AuthError(gettext('Not authorized yet: click "Authorize" in the storage source\'s settings and log in to Yandex.'))
        try:
            answer = self._token_request(self.config, {"grant_type": "refresh_token", "refresh_token": refresh},
                                         client=self._http)
        except AuthError as error:
            self._refused = (time.monotonic() + self.refusal_ttl, error.message)
            raise
        self._refused = None
        tokens = self._tokens(answer, refresh)
        self.update_config(tokens)
        return tokens["access_token"]

    # -- requests

    def _disk(self, path: str) -> str:
        return "disk:" + paths.rebase(path, self._base)

    def _call(self, method: str, api: str, *, params: Mapping[str, Any] | None = None,
              ok: tuple[int, ...] = (200,)) -> httpx.Response:
        """A call with the token: `api` is a path below api_root, or an operation's link on the API's own origin
        (_wait checks that it is), asked with its own query (httpx drops it for any `params`, {} too). Sent again
        after a refused token, refreshed once, and a short throttle."""
        url = api if api.startswith(("https://", "http://")) else self.api_root + api
        for attempt in range(3):
            token = self._access_token()
            try:
                response = self._http.request(method, url, params=params,
                                              headers={"Authorization": f"OAuth {token}", "Accept": "application/json"})
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from error
            if response.status_code == 401 and attempt == 0:
                self._refresh_after_refusal(token)
                continue
            if response.status_code in (429, 503) and attempt < 2:
                wait = retry_after(response, default=1.0, longest=10.0)
                if wait is not None:
                    time.sleep(wait)
                    continue
            if response.status_code in ok:
                return response
            raise self._error(response)
        raise AssertionError("unreachable")

    def _error(self, response: httpx.Response) -> DriverError:
        body = json_or_none(response)
        code = str(body.get("error") or "") if isinstance(body, dict) else ""
        message = short(body.get("description") or body.get("message") or "") if isinstance(body, dict) else ""
        status = response.status_code
        if status == 404 or code == "DiskNotFoundError":
            return NotFound()
        if code in ("DiskResourceAlreadyExistsError", "DiskPathPointsToExistentDirectoryError"):
            return AlreadyExists()
        if status == 401:
            return AuthError(gettext("Yandex Disk refused the authorization. Please authorize again in the storage source's settings."))
        if status == 507 or code == "DiskInsufficientStorageError":
            return DriverError(gettext("The Yandex Disk is out of space."))
        if status == 429:
            return BackendError(gettext("Too many requests to Yandex Disk: it is rate-limiting them. Please try again later."))
        if status >= 500:
            return BackendError(gettext("Yandex Disk failed (HTTP %(status)s). Please try again later.") % {"status": status})
        return DriverError(gettext("Yandex Disk refused this (%(code)s): %(message)s") % {"code": code or status, "message": message})

    def _resource(self, path: str, **params: Any) -> dict[str, Any]:
        body = json_or_none(self._call("GET", "/resources", params={"path": self._disk(path), **params}))
        if not isinstance(body, dict):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return body

    def _existing(self, path: str) -> dict[str, Any] | None:
        try:
            return self._resource(path, fields=FIELDS)
        except NotFound:
            return None

    def _wait(self, response: httpx.Response, done: Callable[[], bool]) -> None:
        """For a 202: wait until the operation it started is over, for at most operation_wait seconds. Its link is
        asked as it is (either form) when it is on the API's own origin, since it goes with the token; else
        `done()` tells from the paths whether the change has happened. DriverError when Yandex says the operation
        failed, BackendError when it isn't over in time."""
        if response.status_code != 202:
            return
        body = json_or_none(response)
        href = body.get("href") if isinstance(body, dict) else None
        asked = isinstance(href, str) and origin(href) == origin(self.api_root)
        deadline = time.monotonic() + self.operation_wait
        while True:
            if asked:
                answer = json_or_none(self._call("GET", href))
                status = answer.get("status") if isinstance(answer, dict) else None
                if status == "success":
                    return
                if status == "failed":
                    raise DriverError(gettext("Yandex Disk couldn't finish this."))
            elif done():
                return
            if time.monotonic() >= deadline:
                raise BackendError(gettext("Yandex Disk is still working on this. Please refresh in a while to see the result."))
            time.sleep(self.poll_interval)

    @staticmethod
    def _entry(path: str, item: Mapping[str, Any]) -> Entry:
        is_dir = item.get("type") == "dir"
        return Entry(name=paths.name(path) if path != paths.ROOT else "", path=path, is_dir=is_dir,
                     size=None if is_dir else as_int(item.get("size")), modified=parse_time(item.get("modified")))

    # -- files

    def list(self, path: str) -> list[Entry]:
        entries: list[Entry] = []
        offset = 0
        for _ in range(10000):
            body = self._resource(path, limit=self.page_size, offset=offset,
                                  fields=",".join(["type", "_embedded.total", *(f"_embedded.items.{f}" for f in FIELDS.split(","))]))
            if body.get("type") != "dir":
                raise NotADirectory()
            embedded = body.get("_embedded") if isinstance(body.get("_embedded"), dict) else {}
            items = embedded.get("items") if isinstance(embedded.get("items"), list) else []
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if usable_name(name):
                    entries.append(self._entry(paths.join(path, name), item))
            offset += len(items)
            if not items or offset >= (as_int(embedded.get("total")) or 0):
                return entries
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def stat(self, path: str) -> Entry:
        item = self._resource(path, fields=FIELDS)
        if path == paths.ROOT and item.get("type") != "dir":
            raise NotFound()
        return self._entry(path, item)

    def _file(self, path: str) -> dict[str, Any]:
        item = self._resource(path, fields=FIELDS)
        if item.get("type") != "file":
            raise NotFound()
        return item

    def _link(self, path: str) -> str:
        body = json_or_none(self._call("GET", "/resources/download", params={"path": self._disk(path)}))
        href = body.get("href") if isinstance(body, dict) else None
        if not isinstance(href, str) or not href.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        return href

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        item = self._file(path)
        total = as_int(item.get("size"))
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, total)
        if span:
            headers["Range"] = span
        try:  # the link needs no token, and may redirect to another download server
            response = self._http.send(self._http.build_request("GET", self._link(path), headers=headers),
                                       stream=True, follow_redirects=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from error
        if response.status_code == 404:
            http.drain(response)
            raise NotFound()
        return response_stream(response, offset=offset, length=length, total=total, service=SERVICE,
                               modified=parse_time(item.get("modified")))

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        self._file(path)
        return self._link(path)

    @staticmethod
    def _check_new_name(name: str) -> None:
        """InvalidPath for a new name longer than Yandex takes: it counts characters, not bytes."""
        if len(name) > MAX_NAME_CHARS:
            raise InvalidPath(gettext("The name is too long: Yandex Disk takes names of at most %(limit)s characters.")
                              % {"limit": MAX_NAME_CHARS})

    def _make_folders(self, path: str) -> None:
        item = self._existing(path) if path != paths.ROOT else {"type": "dir"}
        if item is not None:
            if item.get("type") != "dir":
                raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": path})
            return
        self._check_new_name(paths.name(path))
        self._make_folders(paths.parent(path))
        try:
            self._call("PUT", "/resources", params={"path": self._disk(path)}, ok=(201,))
        except AlreadyExists:  # made meanwhile
            pass

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        existing = self._existing(path)
        if existing is not None and existing.get("type") == "dir":
            raise AlreadyExists()
        self._make_folders(paths.parent(path))
        body = json_or_none(self._call("GET", "/resources/upload", params={"path": self._disk(path), "overwrite": "true"}))
        href = body.get("href") if isinstance(body, dict) else None
        if not isinstance(href, str) or not href.startswith(("https://", "http://")):
            raise BackendError(gettext("%(service)s gave no upload address.") % {"service": SERVICE})
        try:  # the link is its own permission: no token
            response = self._http.put(href, content=exact_body(data, size),
                                      headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from error
        if response.status_code not in (200, 201, 202):
            raise self._error(response)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        self._check_new_name(paths.name(path))
        if self._existing(path) is not None:
            raise AlreadyExists()
        self._make_folders(path)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        self._wait(self._call("DELETE", "/resources", params={"path": self._disk(path)}, ok=(202, 204)),
                   lambda: self._existing(path) is None)

    def _relocate(self, path: str, target: str, *, copy: bool) -> None:
        response = self._call("POST", "/resources/copy" if copy else "/resources/move",
                              params={"from": self._disk(path), "path": self._disk(target), "overwrite": "false"},
                              ok=(201, 202))
        self._wait(response, lambda: self._existing(target) is not None and (copy or self._existing(path) is None))

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        self._resource(path, fields="name")
        if new_name == paths.name(path):
            return
        self._check_new_name(new_name)
        self._relocate(path, paths.join(paths.parent(path), new_name), copy=False)

    def _check_target(self, path: str, folder: str, *, copy: bool) -> str:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        self._resource(path, fields="name")
        target = self._resource(folder, fields="type")
        if target.get("type") != "dir":
            raise NotADirectory()
        return paths.join(folder, paths.name(path))

    def move(self, path: str, folder: str) -> None:
        if path != paths.ROOT and paths.parent(path) == folder:
            self._resource(path, fields="name")
            return  # already there
        self._relocate(path, self._check_target(path, folder, copy=False), copy=False)

    def copy(self, path: str, folder: str) -> None:
        self._relocate(path, self._check_target(path, folder, copy=True), copy=True)
