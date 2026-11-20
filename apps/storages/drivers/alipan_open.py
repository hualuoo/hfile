"""Alipan (阿里云盘, https://www.alipan.com), through its open platform API at https://openapi.alipan.com.

Documentation: https://www.yuque.com/aliyundrive/zpfszx (authorization: efabcs, scopes: dspik0, app levels and
limits: mqocg38hlxzc5vcd, errors and rules: rgg2p1qnsfdux61r, FAQ: zscu11; the APIs: mbb50w, zqkqp6,
gogo34oi2gy98w5d, ezlzok, dp9gn443hh8oksgd, gzeh9ecpxihziqrc, get3mkr677pf10ws).

Paths
    Paths are found by listing from the root folder ("root") of the drive (IdDriver): the resource library or
    the backup drive, from getDriveInfo, as `drive` says. Listings are /openFile/list, `page_size` (at most 100)
    a call, while it gives a next_marker. Alipan allows several files or folders of one name in a folder; see
    _ids for which one a path finds.

Login
    The admin's own app on the open platform (https://www.alipan.com/developer): its App ID and secret, this
    site's authorization callback as its callback URL (the host must be the same), and the scopes user:base,
    file:all:read and file:all:write. "Authorize" sends the browser to /oauth/authorize, which also has the user
    allow both drives (drive=backup,resource), whichever `drive` shows: Alipan keeps one choice of drives per app
    and account, and each authorization replaces it (FAQ 47), so a source of the other drive authorized with the
    same app would otherwise take this one's drive away. Its code gives the tokens (/oauth/access_token, JSON).
    The access token lasts expires_in seconds (2 hours) and is sent as "Authorization: Bearer <token>"; it is
    saved with its expiry. The refresh token is valid 90 days and for one refresh only: each refresh gives a new
    one, which is saved at once. 401 AccessTokenExpired or AccessTokenInvalid refreshes; 403 PermissionDenied
    means the authorization was withdrawn in the Alipan app (or the account frozen).

Requests
    Per user, over all apps: /openFile/list 40 calls in 10 seconds, getDownloadUrl 10 in 10 seconds, the others
    15 a second and 150 in 10 seconds. Calls are paced below that (`list_rate`, `link_rate`, `call_rate` a
    second). A 429 TooManyRequests says how long to wait in x-retry-after (milliseconds): the call waits that
    long and is tried again, for at most `throttle_wait` seconds (Alipan treats retrying without waiting as an
    attack).

Changes
    mkdir: /openFile/create (type folder, check_name_mode refuse; the answer's "exist" says the name was taken).
    rename: /openFile/update (refuse). move: /openFile/move (refuse, "exist" again). copy: /openFile/copy, which
    copies a folder in the background. delete: /openFile/recyclebin/trash, into the recycle bin. A move or delete
    done in the background (async_task_id) is asked about (/openFile/async_task/get) for at most `task_wait`
    seconds. A folder copy's task isn't: it can't be asked about yet ("暂时未开放查询task状态的接口，可以先忽略",
    the copy doc), so the copy may still be under way when copy() returns; the target folder is listed again.

Downloads
    Through this site only (can_direct_download False): Alipan bans accounts whose download links are used from
    several IP addresses, and forbids handing one account's links to other people (FAQ 20 and 39). open() asks
    /openFile/getDownloadUrl for a link (valid 15 minutes for most apps; reused for `link_ttl` seconds) and
    fetches it, always with a Range: a download without one holds a place in the concurrency limit for 60
    seconds instead of 1. More downloads at once than the app may have (3 for most apps) are refused with 403
    RequestDeniedByCallback / ExceedMaxConcurrency, and tried again after `busy_wait` seconds (Alipan says at
    least 3), `busy_tries` times. A frozen file (punish_flag 2) can't be downloaded, nor one with punish_flag 103
    from the resource library (from the backup drive it can, FAQ 38): its link gives a notice instead. A Live
    Photo (.livp) has no link at all, only its picture and video apart (streamsUrl; OpenList's driver shows it),
    so it can't be downloaded either.

Uploads
    /openFile/create with check_name_mode ignore (Alipan can't replace a file: IdDriver deletes the old one after,
    upload_conflict "keep") and the parts; each part (`part_size`, larger when 10,000 wouldn't do) is PUT to its
    upload_url without a Content-Type (the URL's signature has none), then /openFile/complete. Upload URLs last
    an hour; they're renewed (/openFile/getUploadUrl) after `upload_url_ttl` seconds. Rapid upload, when the
    data can seek: the first create sends the SHA-1 of the first KiB (pre_hash). Without a match it's an
    ordinary upload; on 409 PreHashMatched a second create sends the SHA-1 of the whole file and the proof code
    (base64 of up to 8 bytes of the file at an offset from the access token's MD5), and when Alipan has the
    file nothing is uploaded.

Names
    Names are at most 1024 bytes of UTF-8.

Limits
    Not tried against the real service: the tests use a fake built from the documentation above.
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http
from ._cloud import (
    RateLimiter,
    TTLCache,
    as_int,
    exact_body,
    json_or_none,
    parse_time,
    read_exact,
    response_stream,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, StaleFolder, now
from .base import AlreadyExists, AuthError, BackendError, DriverError, InvalidPath, NotFound, Param, ReadStream

SERVICE = gettext_lazy("Alipan")
SCOPES = "user:base,file:all:read,file:all:write"
PREHASH_BYTES = 1024  # pre_hash: SHA-1 of the first KiB
FROZEN = 2  # punish_flag: frozen, a notice instead of the file (FAQ 38)
NOT_IN_RESOURCE = 103  # punish_flag: can't be downloaded from the resource library, can from the backup drive (FAQ 38)
_REFRESH_CODES = {"AccessTokenExpired", "AccessTokenInvalid", "I400JD"}
CREATE = "/adrive/v1.0/openFile/create"


@dataclasses.dataclass(frozen=True)
class _Item(Node):
    punish: int | None = None  # punish_flag: FROZEN and NOT_IN_RESOURCE can't be downloaded (there)


def _item_node(item: Any) -> _Item | None:
    if not isinstance(item, dict) or not isinstance(item.get("file_id"), str) or not item["file_id"]:
        return None
    name = item.get("name") or item.get("file_name")
    if not usable_name(name):
        return None
    is_dir = item.get("type") == "folder"
    return _Item(id=item["file_id"], name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("size")),
                 modified=parse_time(item.get("updated_at") or item.get("created_at")),
                 punish=as_int(item.get("punish_flag")))


def _tell(data: BinaryIO) -> int | None:
    """Where a seekable file object is, or None when it can't seek."""
    try:
        if data.seekable():
            return data.tell()
    except (AttributeError, OSError, ValueError):
        pass
    return None


def proof_code(access_token: str, data: BinaryIO, start: int, size: int) -> str:
    """The proof of a rapid upload: base64 of the bytes [r, min(r + 8, size)) of the file, r being the first 16
    hex digits of the access token's MD5, as a number, modulo the size."""
    if size <= 0:
        return ""
    first = int(hashlib.md5(access_token.encode("utf-8")).hexdigest()[:16], 16) % size
    data.seek(start + first)
    return base64.b64encode(read_exact(data, min(first + 8, size) - first)).decode("ascii")


def _sha1(data: BinaryIO, start: int, size: int) -> str:
    data.seek(start)
    digest = hashlib.sha1()
    for chunk in exact_body(data, size):
        digest.update(chunk)
    return digest.hexdigest().upper()


class _Expired(Exception):
    """An upload URL Alipan's storage refused (403): it expired, or was used up."""


class AlipanOpenDriver(IdDriver):
    type = "alipan-open"
    label = gettext_lazy("Alipan (open platform)")
    service = SERVICE
    root_id = "root"
    params = (
        Param("client_id", gettext_lazy("App ID"),
              help=gettext_lazy("Of your app on the Alipan open platform (https://www.alipan.com/developer): add this "
                                "site's authorization callback URL as its callback URL, and the scopes user:base, "
                                "file:all:read and file:all:write.")),
        Param("client_secret", gettext_lazy("App secret"), kind="password", secret=True,
              help=gettext_lazy("The secret of the same app.")),
        Param("drive", gettext_lazy("Drive"), kind="select", default="resource",
              options=(("resource", gettext_lazy("Resource library")), ("backup", gettext_lazy("Backup drive"))),
              help=gettext_lazy("Which of the account's drives to show. Authorizing asks the user to allow both drives, "
                                "so that one app can serve a storage source of each.")),
        Param("access_token", gettext_lazy("Access token"), kind="password", required=False, secret=True,
              help=gettext_lazy('With the App ID and secret filled in, click "Authorize", log in to Alipan and allow '
                                "the app: it's filled in for you.")),
        Param("refresh_token", gettext_lazy("Refresh token"), kind="password", required=False, secret=True,
              help=gettext_lazy("Filled in when authorizing. Alipan gives a new one on every refresh and the old one "
                                "stops working; HFile saves the new one itself.")),
        # Secret only so that the form keeps the saved value (see open115's).
        Param("token_expires_at", gettext_lazy("Access token expiry"), kind="number", required=False, advanced=True,
              minimum=0, secret=True,
              help=gettext_lazy("Unix time (seconds), filled in when authorizing and refreshing the token; no need to change it.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder of the drive that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = False  # see the module's docstring
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    upload_conflict = "keep"
    max_name_bytes = 1024

    oauth = True
    setup_params = ("client_id", "client_secret", "drive")
    auth_params = ("access_token", "refresh_token", "token_expires_at")

    # Endpoints and tuning; tests point them at a fake.
    api_root: ClassVar[str] = "https://openapi.alipan.com"
    page_size: ClassVar[int] = 100
    max_pages: ClassVar[int] = 10_000
    part_size: ClassVar[int] = 20 * 1024 * 1024
    max_parts: ClassVar[int] = 10_000
    link_ttl: ClassVar[float] = 600.0
    upload_url_ttl: ClassVar[float] = 50 * 60.0
    list_rate: ClassVar[float] = 3.5
    link_rate: ClassVar[float] = 0.9
    call_rate: ClassVar[float] = 12.0
    queue_wait: ClassVar[float] = 20.0  # seconds a call may wait for its turn, else BackendError
    throttle_wait: ClassVar[float] = 30.0
    busy_wait: ClassVar[float] = 3.0
    busy_tries: ClassVar[int] = 3
    task_wait: ClassVar[float] = 30.0
    poll_interval: ClassVar[float] = 1.0
    part_tries: ClassVar[int] = 3
    retry_delay: ClassVar[float] = 1.0
    refusal_ttl: ClassVar[float] = 30.0  # seconds a refused refresh is remembered

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._http = http.client()
        self._pace_list = RateLimiter(self.list_rate, longest=self.queue_wait, service=SERVICE)
        self._pace_link = RateLimiter(self.link_rate, longest=self.queue_wait, service=SERVICE)
        self._pace_call = RateLimiter(self.call_rate, longest=self.queue_wait, service=SERVICE)
        self._refused: tuple[float, str] | None = None  # (until, message) of a refused refresh
        self._drive_lock = threading.Lock()
        self._drive_id: str | None = None
        self._links = TTLCache(1024)  # file id -> (link, monotonic time it may be used until)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = cls.base_path_errors(config)
        expires = config.get("token_expires_at")
        if expires is not None and (type(expires) is not int or expires < 0):
            errors["token_expires_at"] = gettext("The access token expiry should be an integer of at least 0 (Unix seconds).")
        return errors

    @staticmethod
    def _drive_kind(config: Mapping[str, Any]) -> str:
        return "backup" if config.get("drive") == "backup" else "resource"

    @classmethod
    def _app(cls, config: Mapping[str, Any]) -> tuple[str, str]:
        client_id = str(config.get("client_id") or "").strip()
        secret = str(config.get("client_secret") or "")
        if not client_id or not secret:
            raise AuthError(gettext("Please fill in the App ID and App secret first."))
        return client_id, secret

    @classmethod
    def authorize_url(cls, config: Mapping[str, Any], *, redirect_uri: str, state: str) -> str:
        client_id, _ = cls._app(config)
        # Both drives: the drives an app may use are the ones the last authorization allowed (FAQ 47).
        query = {"client_id": client_id, "redirect_uri": redirect_uri, "scope": SCOPES, "response_type": "code",
                 "state": state, "drive": "backup,resource"}
        return cls.api_root + "/oauth/authorize?" + urllib.parse.urlencode(query)

    @classmethod
    def exchange_code(cls, config: Mapping[str, Any], *, code: str, redirect_uri: str) -> dict[str, Any]:
        return cls._tokens(cls._token_request(config, {"grant_type": "authorization_code", "code": code}))

    # -- login

    @staticmethod
    def _tokens(answer: Mapping[str, Any]) -> dict[str, Any]:
        return {"access_token": answer["access_token"], "refresh_token": answer["refresh_token"],
                "token_expires_at": int(time.time()) + (as_int(answer.get("expires_in")) or 7200)}

    @classmethod
    def _token_request(cls, config: Mapping[str, Any], fields: Mapping[str, str],
                       client: httpx.Client | None = None) -> dict[str, Any]:
        client_id, secret = cls._app(config)
        body = {"client_id": client_id, "client_secret": secret, **fields}
        try:
            if client is not None:
                response = client.post(cls.api_root + "/oauth/access_token", json=body)
            else:
                with http.client() as own:
                    response = own.post(cls.api_root + "/oauth/access_token", json=body)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        answer = json_or_none(response)
        answer = answer if isinstance(answer, dict) else {}
        if response.status_code == 429 or response.status_code >= 500:
            raise BackendError(gettext("Alipan's authorization service is unavailable for now (HTTP %(status)s). Please try again later.")
                               % {"status": response.status_code})
        access, refresh = answer.get("access_token"), answer.get("refresh_token")
        if response.status_code == 200 and isinstance(access, str) and access and isinstance(refresh, str) and refresh:
            return answer
        code = str(answer.get("code") or response.status_code)
        if code in ("AppNotExists", "InvalidClientId", "InvalidClientSecret"):
            raise AuthError(gettext("Alipan refused the App ID or App secret (%(code)s). Please check them.") % {"code": code})
        if code in ("InvalidCode", "RefreshTokenExpired", "RefreshTokenInvalid", "PermissionDenied"):
            raise AuthError(gettext("Alipan's authorization has expired or been withdrawn (%(code)s). Please authorize again in the storage source's settings.")
                            % {"code": code})
        raise AuthError(gettext("Alipan refused the authorization (%(code)s): %(message)s") % {"code": code, "message": short(answer.get("message"))})

    def _access_token(self) -> str:
        with self.token_lock:
            token = self.config.get("access_token")
            expires = as_int(self.config.get("token_expires_at")) or 0
            if token and (expires == 0 or time.time() < expires - 300):  # unknown expiry: try it
                return token
            return self._refresh_locked()

    def _refresh_after_refusal(self, token: str) -> None:
        """Alipan refused `token`: refresh, unless another thread already did."""
        with self.token_lock:
            if self.config.get("access_token") == token:
                self._refresh_locked()

    def _refresh_locked(self) -> str:
        if self._refused is not None and time.monotonic() < self._refused[0]:
            raise AuthError(self._refused[1])
        refresh = str(self.config.get("refresh_token") or "")
        if not refresh:
            raise AuthError(gettext('Not authorized yet: click "Authorize" in the storage source\'s settings and log in to Alipan.'))
        try:
            answer = self._token_request(self.config, {"grant_type": "refresh_token", "refresh_token": refresh},
                                         client=self._http)
        except AuthError as error:
            self._refused = (time.monotonic() + self.refusal_ttl, error.message)
            raise
        self._refused = None
        tokens = self._tokens(answer)
        self.log.info("refreshed the access token")
        self.update_config(tokens)  # the old refresh token is spent: save the new one before anything else
        return tokens["access_token"]

    # -- requests

    def _post(self, api: str, body: Mapping[str, Any] | Callable[[str], Mapping[str, Any]], *,
              pace: RateLimiter | None = None, accept: tuple[str, ...] = ()) -> dict[str, Any]:
        """One call of the API (a JSON POST): its answer, or a DriverError. `body` may be made from the access
        token (a rapid upload's proof). `accept`: error codes given back as answers (with their "code")."""
        refreshed = False
        deadline = time.monotonic() + self.throttle_wait
        while True:
            token = self._access_token()
            (pace or self._pace_call).wait()
            try:
                response = self._http.post(self.api_root + api, json=body(token) if callable(body) else body,
                                           headers={"Authorization": f"Bearer {token}"})
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            answer = json_or_none(response)
            answer = answer if isinstance(answer, dict) else {}
            if 200 <= response.status_code < 300:
                return answer
            code = str(answer.get("code") or "")
            if code in accept:
                return answer
            if code in _REFRESH_CODES and not refreshed:
                refreshed = True
                self._refresh_after_refusal(token)
                continue
            if response.status_code == 429:
                wait = (as_int(response.headers.get("x-retry-after")) or 1000) / 1000  # milliseconds
                if time.monotonic() + wait <= deadline:
                    time.sleep(wait)
                    continue
            raise self._error(response.status_code, code, answer)

    @staticmethod
    def _error(status: int, code: str, answer: Mapping[str, Any]) -> DriverError:
        message = short(answer.get("message") or "")
        if code == "UserNotAllowedAccessDrive":
            return AuthError(gettext("Alipan doesn't let this app use the chosen drive or folder. Authorize again in the "
                                     "storage source's settings and allow that drive."))
        if status == 401 or code in _REFRESH_CODES or code == "PermissionDenied":
            return AuthError(gettext("Alipan refused the access (%(code)s): the authorization may have been withdrawn in the "
                                     "Alipan app. Please authorize again in the storage source's settings.") % {"code": code or status})
        if code in ("QuotaExhausted.Drive", "ExceedCapacityForbidden"):
            return DriverError(gettext("The Alipan drive is full."))
        if status == 404 or code.startswith("NotFound") or code == "ForbiddenFileInTheRecycleBin":
            return NotFound()
        if status == 409 and "exist" in code.lower():
            return AlreadyExists()
        if code == "ForbiddenDriveLocked":
            return DriverError(gettext("Alipan has locked this drive for now, so this can't be done."))
        if code == "UserNotAllowedAccessResource":
            return DriverError(gettext("Alipan doesn't let this account access the file (it broke Alipan's rules)."))
        if status == 429:
            return BackendError(gettext("Alipan is limiting this account's requests for now. Please try again in a moment."))
        if status >= 500:
            return BackendError(gettext("Alipan failed (HTTP %(status)s). Please try again later.") % {"status": status})
        if "name" in code.lower():
            return InvalidPath(gettext("Alipan doesn't accept this name: %(message)s") % {"message": message or code})
        return DriverError(gettext("Alipan refused this (%(code)s): %(message)s") % {"code": code or status, "message": message})

    def _drive(self) -> str:
        """The id of the drive of `drive` (getDriveInfo, asked once)."""
        with self._drive_lock:
            if self._drive_id is None:
                answer = self._post("/adrive/v1.0/user/getDriveInfo", {})
                drive = answer.get(self._drive_kind(self.config) + "_drive_id")
                if not isinstance(drive, str) or not drive:
                    raise self._error(403, "UserNotAllowedAccessDrive", {})
                self._drive_id = drive
            return self._drive_id

    def _wait_task(self, answer: Mapping[str, Any]) -> None:
        """A change Alipan does in the background (async_task_id): wait until it's done, for at most task_wait
        seconds (after that the listings show it when it's done)."""
        task = answer.get("async_task_id")
        if not isinstance(task, str) or not task:
            return
        deadline = time.monotonic() + self.task_wait
        while True:
            try:
                state = self._post("/adrive/v1.0/openFile/async_task/get", {"async_task_id": task}).get("state")
            except (NotFound, BackendError) as error:  # a task that can't be asked about
                self.log.debug("can't ask about task %s: %s", task, error.message)
                return
            if state == "Succeed":
                return
            if state == "Failed":
                raise BackendError(gettext("Alipan couldn't finish this. Please check the files and try again."))
            if time.monotonic() >= deadline:
                self.log.info("task %s still runs after %s seconds", task, self.task_wait)
                return
            time.sleep(self.poll_interval)

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[_Item]:
        drive = self._drive()
        nodes: list[_Item] = []
        marker = ""
        for _ in range(self.max_pages):
            try:
                answer = self._post("/adrive/v1.0/openFile/list", {
                    "drive_id": drive, "parent_file_id": folder_id, "limit": self.page_size, "marker": marker,
                }, pace=self._pace_list)
            except NotFound:
                raise StaleFolder(folder_id) from None
            items = answer.get("items")
            if not isinstance(items, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            nodes += [node for node in map(_item_node, items) if node is not None]
            marker = answer.get("next_marker") or ""
            if not marker:
                return nodes
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _create_node(self, parent_id: str, name: str) -> _Item:
        answer = self._post(CREATE, {"drive_id": self._drive(), "parent_file_id": parent_id, "name": name,
                                     "type": "folder", "check_name_mode": "refuse"})
        if answer.get("exist"):
            raise AlreadyExists()  # made elsewhere meanwhile
        if not isinstance(answer.get("file_id"), str) or not answer["file_id"]:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        return _Item(id=answer["file_id"], name=name, is_dir=True, size=None, modified=now())

    def _delete_node(self, node: Node, parent_id: str) -> None:
        self._wait_task(self._post("/adrive/v1.0/openFile/recyclebin/trash", {"drive_id": self._drive(), "file_id": node.id}))

    def _rename_node(self, node: Node, name: str) -> None:
        answer = self._post("/adrive/v1.0/openFile/update", {"drive_id": self._drive(), "file_id": node.id,
                                                              "name": name, "check_name_mode": "refuse"})
        if isinstance(answer.get("name"), str) and answer["name"] != name:
            raise AlreadyExists()  # refused: the name was taken meanwhile

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        answer = self._post("/adrive/v1.0/openFile/move", {"drive_id": self._drive(), "file_id": node.id,
                                                            "to_parent_file_id": target_id, "check_name_mode": "refuse"})
        if answer.get("exist"):
            raise AlreadyExists()  # not moved: the name was taken meanwhile
        self._wait_task(answer)

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        # A folder's copy goes on in the background, and its task can't be asked about (the copy doc): not waited for.
        answer = self._post("/adrive/v1.0/openFile/copy", {"drive_id": self._drive(), "file_id": node.id,
                                                            "to_parent_file_id": target_id, "auto_rename": False})
        made = answer.get("file_id")
        if node.is_dir or not isinstance(made, str) or not made or made == node.id:
            return None  # the folder is listed again
        return dataclasses.replace(node, id=made, modified=now())

    def _rapid(self, body: dict[str, Any], data: BinaryIO, start: int, size: int) -> dict[str, Any]:
        """create with a rapid upload's hashes: the answer, rapid or with the parts to upload."""
        data.seek(start)
        head = hashlib.sha1(read_exact(data, min(PREHASH_BYTES, size))).hexdigest().upper()
        answer = self._post(CREATE, {**body, "size": size, "pre_hash": head}, accept=("PreHashMatched",))
        if answer.get("code") != "PreHashMatched":
            return answer  # Alipan has no file that starts so: an ordinary upload
        whole = _sha1(data, start, size)
        try:
            return self._post(CREATE, lambda token: {
                **body, "size": size, "content_hash": whole, "content_hash_name": "sha1", "proof_version": "v1",
                "proof_code": proof_code(token, data, start, size)})
        finally:
            data.seek(start)

    def _part_size(self, size: int) -> int:
        if size <= self.part_size * self.max_parts:
            return self.part_size
        mib = 1024 * 1024
        return mib * -(-size // (self.max_parts * mib))  # whole MiB, so that max_parts do

    @staticmethod
    def _part_urls(parts: Any) -> dict[int, str]:
        urls = {}
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict) and as_int(part.get("part_number")) and isinstance(part.get("upload_url"), str):
                urls[as_int(part["part_number"])] = part["upload_url"]
        return urls

    def _upload_urls(self, drive: str, file_id: str, upload_id: str, count: int) -> dict[int, str]:
        answer = self._post("/adrive/v1.0/openFile/getUploadUrl", {
            "drive_id": drive, "file_id": file_id, "upload_id": upload_id,
            "part_info_list": [{"part_number": n} for n in range(1, count + 1)]})
        return self._part_urls(answer.get("part_info_list"))

    def _put_part(self, url: str, data: BinaryIO, at: int | None, length: int) -> None:
        """PUT one part, from `at` in `data` (None: where the stream is). Tried again on a broken connection or a 5xx
        when `data` can seek."""
        tries = self.part_tries if at is not None else 1
        for attempt in range(tries):
            if at is not None:
                data.seek(at)
            try:
                # No Content-Type: the upload URL is signed without one.
                response = self._http.put(url, content=exact_body(data, length), headers={"Content-Length": str(length)})
            except httpx.HTTPError as error:
                if attempt + 1 < tries:
                    time.sleep(self.retry_delay)
                    continue
                raise transport_error(error, SERVICE) from None
            if response.status_code in (200, 409):  # 409: the part is there already (a repeated PUT)
                return
            if response.status_code == 403:
                raise _Expired()
            if response.status_code >= 500 and attempt + 1 < tries:
                time.sleep(self.retry_delay)
                continue
            raise BackendError(gettext("Alipan's storage refused the upload (HTTP %(status)s). Please try again.") % {"status": response.status_code})

    def _put_parts(self, drive: str, file_id: str, upload_id: str, parts: Any, data: BinaryIO, start: int | None,
                   size: int, part: int) -> None:
        count = -(-size // part)
        urls, given = self._part_urls(parts), time.monotonic()
        for number in range(1, count + 1):
            offset = (number - 1) * part
            length = min(part, size - offset)
            for renewed in (False, True):
                if renewed or number not in urls or time.monotonic() - given > self.upload_url_ttl:
                    urls, given = self._upload_urls(drive, file_id, upload_id, count), time.monotonic()
                if number not in urls:
                    raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
                try:
                    self._put_part(urls[number], data, None if start is None else start + offset, length)
                    break
                except _Expired:
                    if renewed or start is None:  # a stream that was read can't be sent again
                        raise BackendError(gettext("Alipan's storage refused the upload. Please try again.")) from None

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        drive = self._drive()
        part = self._part_size(size)
        count = -(-size // part)
        body = {"drive_id": drive, "parent_file_id": folder_id, "name": name, "type": "file",
                "check_name_mode": "ignore", "part_info_list": [{"part_number": n} for n in range(1, count + 1)]}
        start = _tell(data)
        if start is not None and size:
            answer = self._rapid(body, data, start, size)
        else:
            answer = self._post(CREATE, body)
        file_id, upload_id = answer.get("file_id"), answer.get("upload_id")
        if not isinstance(file_id, str) or not file_id or not isinstance(upload_id, str) or not upload_id:
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        if not answer.get("rapid_upload"):
            self._put_parts(drive, file_id, upload_id, answer.get("part_info_list"), data, start, size, part)
        done = self._post("/adrive/v1.0/openFile/complete", {"drive_id": drive, "file_id": file_id, "upload_id": upload_id})
        node = _item_node({"file_id": file_id, "name": name, "size": size, **done, "type": "file"})
        return node if node is not None and node.id == file_id else None

    def _link(self, node: Node, *, fresh: bool = False) -> str:
        cached = None if fresh else self._links.get(node.id)
        if cached is not None and time.monotonic() < cached[1]:
            return cached[0]
        answer = self._post("/adrive/v1.0/openFile/getDownloadUrl", {"drive_id": self._drive(), "file_id": node.id,
                                                                      "expire_sec": 900}, pace=self._pace_link)
        link = answer.get("url")
        if not link and isinstance(answer.get("streamsUrl"), dict):  # a Live Photo (.livp): its parts only
            raise DriverError(gettext("Alipan gives this Live Photo only as a separate picture and video, which can't "
                                      "be downloaded here as one file."))
        if not isinstance(link, str) or not link.startswith(("https://", "http://")):
            raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
        valid = self.link_ttl
        expiration = parse_time(answer.get("expiration"))
        if expiration is not None:
            valid = min(valid, (expiration - datetime.datetime.now(datetime.timezone.utc)).total_seconds() - 60)
        if valid > 0:
            self._links.put(node.id, (link, time.monotonic() + valid))
        return link

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        punish = node.punish if isinstance(node, _Item) else None
        if punish == FROZEN:
            raise DriverError(gettext("Alipan has frozen this file, so it can't be downloaded."))
        if punish == NOT_IN_RESOURCE and self._drive_kind(self.config) == "resource":
            raise DriverError(gettext("Alipan doesn't let this file be downloaded from the resource library (it can be "
                                      "from the backup drive)."))
        size = node.size
        if length == 0 or size == 0:
            return ReadStream(iter(()), size=0, total=size, offset=offset, modified=node.modified)
        last = offset + length - 1 if length is not None else (size - 1 if size is not None else None)
        # Always a Range: a download without one holds a place in Alipan's concurrency limit for a minute.
        headers = {"Accept-Encoding": "identity", "Range": f"bytes={offset}-{'' if last is None else last}"}
        fresh = False
        busy = 0
        while True:
            try:
                response = self._http.send(self._http.build_request("GET", self._link(node, fresh=fresh), headers=headers),
                                           stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code in (200, 206):
                return response_stream(response, offset=offset, length=length, total=size, service=SERVICE,
                                       modified=node.modified)
            text = http.read_error(response).content
            if response.status_code == 403 and b"ExceedMaxConcurrency" in text:
                busy += 1
                if busy > self.busy_tries:
                    raise BackendError(gettext("Alipan allows this account only a few downloads at once, and they're "
                                               "all in use. Please try again in a moment."))
                time.sleep(self.busy_wait)
                continue
            if response.status_code == 403 and not fresh:  # the link expired: ask for another
                fresh = True
                continue
            if response.status_code in (404, 410):
                raise NotFound()
            raise BackendError(gettext("Alipan failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})
