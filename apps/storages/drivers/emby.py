"""Emby and Jellyfin (https://emby.media, https://jellyfin.org): the media libraries of a server, as a read-only
storage, through their REST API (Jellyfin keeps Emby's).

Paths
    The root holds the user's libraries (/Users/<id>/Views); a folder's items are /Users/<id>/Items with its
    ParentId. A file is named after its file on the server (the last segment of its Path, "Movie (2020).mkv"),
    a folder after its name (series, seasons and other folders the server makes have no file). Paths are
    found by id (IdDriver): of two items of one name in a folder one is shown, the newer.

Login
    `username` and `password` (POST /Users/AuthenticateByName) give an access token and the user's id; or an
    API key of the server's settings with `user_id`. Every request says who the client is, and carries the
    token, in "Authorization: MediaBrowser Client=..., Device=..., DeviceId=..., Version=..., Token=...", the
    only header Jellyfin 12 reads by default (its legacy authorization is off: AuthorizationContext.cs); and in
    X-Emby-Authorization and X-Emby-Token, which Emby reads. Jellyfin ends a user's other sessions of the same
    DeviceId at each login (SessionManager.GetAuthorizationToken), so the DeviceId is one per server, user and
    base path: two storage sources of one account don't log each other out. A token the server refuses (401) is
    asked for again once, for downloads too. A 400 or 404 for the user's libraries means a wrong user ID, or an
    address that isn't the server's.

Downloads
    Through this site only: the server's links carry the token, which would let anyone who has one use the
    account. open() fetches /Items/<id>/Download (the original file; the user needs the "allow media
    download" permission, else a 403) or, with `link_method` "stream", /Videos/<id>/stream?static=true, with
    the Range.
"""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import as_int, json_or_none, parse_time, range_header, response_stream, transport_error, usable_name
from ._ids import IdDriver, Node, StaleFolder
from .base import AuthError, BackendError, DriverError, Param, ReadStream

SERVICE = gettext_lazy("Emby")
VIEWS = "views"  # the root's id: the user's libraries
VIEWS_API = "/Users/{user}/Views"
AUTHORIZATION = 'MediaBrowser Client="HFile", Device="HFile", DeviceId="hfile-{key}", Version="1.0"'


@dataclasses.dataclass(frozen=True)
class _Item(Node):
    pass


class EmbyDriver(IdDriver):
    type = "emby"
    label = gettext_lazy("Emby / Jellyfin")
    service = SERVICE
    root_id = VIEWS
    params = (
        Param("url", gettext_lazy("Address"), placeholder="http://192.168.1.10:8096",
              help=gettext_lazy("The address of the Emby or Jellyfin server, as it is opened in a browser.")),
        Param("username", gettext_lazy("Username"), required=False),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True),
        Param("api_key", gettext_lazy("API key"), kind="password", required=False, secret=True, advanced=True,
              help=gettext_lazy("Instead of a username and password: an API key from the server's settings, with the user's ID below.")),
        Param("user_id", gettext_lazy("User ID"), required=False, advanced=True,
              help=gettext_lazy("With an API key: the ID of the user whose libraries to show (in the address of the user's page in the server's settings).")),
        Param("link_method", gettext_lazy("Fetch files as"), kind="select", required=False, default="download",
              options=(("download", gettext_lazy("Original files (needs the user's download permission)")),
                       ("stream", gettext_lazy("Static streams (the same bytes, without the download permission)"))),
              help=gettext_lazy("How files are fetched from the server.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The library or folder that is this storage source's root, like / or /Movies.")),
    )

    can_range = True
    can_direct_download = False  # the server's links carry the token
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    setup_params = ("url", "username", "password", "api_key", "user_id")

    # Tuning: items asked for per listing call.
    page_size: ClassVar[int] = 500

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._root_url = str(self.config.get("url") or "").strip().rstrip("/")
        self._http = http.client()
        self._lock = threading.Lock()
        key = str(self.config.get("api_key") or "").strip()
        self._token: str | None = key or None
        self._user: str | None = str(self.config.get("user_id") or "").strip() or None if key else None
        self._fixed = bool(key)
        # The same device each time (the server lists the devices that logged in), one per server, user and base
        # path: see Login in the module's docstring.
        device = hashlib.sha256(f"{self._root_url}\n{self.config.get('username') or ''}\n{self._base}".encode()).hexdigest()[:16]
        self._client = AUTHORIZATION.format(key=device)

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        errors = cls.base_path_errors(config)
        raw = str(config.get("url") or "").strip()
        url = httpx.URL(raw) if raw else None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            errors["url"] = gettext("The address should start with http:// or https://, like http://192.168.1.10:8096.")
        if str(config.get("api_key") or "").strip():
            if not str(config.get("user_id") or "").strip():
                errors["user_id"] = gettext("An API key needs the user's ID.")
        elif not str(config.get("username") or "").strip():
            errors["username"] = gettext("Give the username and password, or an API key with the user's ID.")
        return errors

    # -- login

    def _log_in(self) -> tuple[str, str]:
        try:
            response = self._http.post(self._root_url + "/Users/AuthenticateByName",
                                       headers={"Authorization": self._client, "X-Emby-Authorization": self._client},
                                       json={"Username": str(self.config.get("username") or ""), "Pw": str(self.config.get("password") or "")})
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        answer = json_or_none(response)
        token = answer.get("AccessToken") if isinstance(answer, dict) else None
        user = (answer.get("User") or {}).get("Id") if isinstance(answer, dict) and isinstance(answer.get("User"), dict) else None
        if response.status_code in (400, 401, 403):
            raise AuthError(gettext("The media server refused the username or password."))
        if response.status_code != 200 or not isinstance(token, str) or not isinstance(user, str):
            raise BackendError(gettext("The media server failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})
        return token, user

    def _session(self) -> tuple[str, str]:
        with self._lock:
            if self._token is None or self._user is None:
                self._token, self._user = self._log_in()
            return self._token, self._user

    def _expire(self, token: str) -> bool:
        """The server refused `token` (401): whether to log in again (not with an API key)."""
        if self._fixed:
            return False
        with self._lock:
            if self._token == token:
                self._token = None  # logged out on the server: again
        return True

    def _headers(self, token: str) -> dict[str, str]:
        """The token where Jellyfin and Emby read it: see Login in the module's docstring."""
        return {"Authorization": f'{self._client}, Token="{token}"', "X-Emby-Token": token}

    # -- requests

    def _get(self, api: str, params: Mapping[str, Any] | None = None) -> Any:
        for attempt in range(2):
            token, user = self._session()
            try:
                response = self._http.get(self._root_url + api.format(user=user), params=params, headers=self._headers(token))
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code == 401 and not attempt and self._expire(token):
                continue
            if response.status_code in (401, 403):
                raise AuthError(gettext("The media server refused the access token or API key. Please check the storage source's settings."))
            if response.status_code in (400, 404) and api == VIEWS_API:
                raise AuthError(gettext("The media server doesn't know this user (HTTP %(status)s): the user ID, or the "
                                        "address, is wrong. Please check the storage source's settings.") % {"status": response.status_code})
            if response.status_code in (400, 404):
                raise StaleFolder(api)
            if response.status_code != 200:
                raise BackendError(gettext("The media server failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})
            answer = json_or_none(response)
            if answer is None:
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            return answer
        raise AssertionError("unreachable")

    @staticmethod
    def _item_node(item: Any) -> _Item | None:
        if not isinstance(item, dict) or not isinstance(item.get("Id"), str):
            return None
        is_dir = bool(item.get("IsFolder"))
        name = item.get("Name")
        source = item.get("Path")
        if not is_dir and isinstance(source, str) and source:
            name = source.replace("\\", "/").rstrip("/").rpartition("/")[2] or name
        if not usable_name(name):
            return None
        size = None
        if not is_dir:
            size = as_int(item.get("Size"))
            sources = item.get("MediaSources")
            if size is None and isinstance(sources, list) and sources and isinstance(sources[0], dict):
                size = as_int(sources[0].get("Size"))
        return _Item(id=item["Id"], name=name, is_dir=is_dir, size=size,
                     modified=parse_time(item.get("DateModified") or item.get("DateCreated")))

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[_Item]:
        if folder_id == VIEWS:
            answer = self._get(VIEWS_API)
            items = answer.get("Items") if isinstance(answer, dict) else None
            return [node for node in map(self._item_node, items or []) if node is not None]
        nodes: list[_Item] = []
        for start in range(0, 10_000_000, self.page_size):
            answer = self._get("/Users/{user}/Items", {
                "ParentId": folder_id, "Recursive": "false", "StartIndex": start, "Limit": self.page_size,
                "Fields": "Path,Size,DateCreated,DateModified,MediaSources"})
            items = answer.get("Items") if isinstance(answer, dict) else None
            if not isinstance(items, list):
                raise BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.") % {"service": SERVICE})
            nodes += [node for node in map(self._item_node, items) if node is not None]
            if len(items) < self.page_size or start + len(items) >= (as_int(answer.get("TotalRecordCount")) or 0):
                return nodes
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        if self.config.get("link_method") == "stream":
            url, params = f"{self._root_url}/Videos/{node.id}/stream", {"static": "true"}
        else:
            url, params = f"{self._root_url}/Items/{node.id}/Download", {}
        span = range_header(offset, length, node.size)
        for attempt in range(2):
            token, _ = self._session()
            headers = {**self._headers(token), "Accept-Encoding": "identity"}
            if span:
                headers["Range"] = span
            try:
                response = self._http.send(self._http.build_request("GET", url, params=params, headers=headers), stream=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code == 401:
                http.drain(response)
                if not attempt and self._expire(token):
                    continue  # a GET: nothing to send again
                raise AuthError(gettext("The media server refused the access token or API key. Please check the storage source's settings."))
            if response.status_code == 403:
                http.drain(response)
                raise DriverError(gettext("The media server refused the download: the user may lack the permission to download media (or choose static streams in the settings)."))
            if response.status_code not in (200, 206):
                http.drain(response)
                raise BackendError(gettext("The media server failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})
            return response_stream(response, offset=offset, length=length, total=node.size, service=SERVICE, modified=node.modified)
        raise AssertionError("unreachable")
