"""FebBox (https://www.febbox.com), a cloud drive, through its open platform: the files of the FebBox account that
generated the key (a client ID and secret).

Documentation: https://www.febbox.com/open/docs (read 2026-11-18), whose pages are https://www.febbox.com/open/docs?id=
followed by: local-oauth-authorize (OAuth 2.0), api-93205891 (get token), local-api-limits (rate limits and hosts),
api-146718079 (file list), api-file-get-play-url-user (play URL, user traffic), api-146674774 (download URLs),
api-155822083 (create a directory), api-155962854 (rename), api-157707193 (move), api-157705957 (copy), api-156009938
(delete), api-144790863 (add a file). Each fact below is marked [docs] (those pages), [FAQ]
(https://www.febbox.com/faq) or [OpenList] (OpenList's FebBox driver, drivers/febbox, and its page
https://doc.oplist.org/guide/drivers/febbox).

Paths
    IdDriver: the account's root is folder 0, and a path is found by listing each folder from there. Listings are
    file_list, `page_size` (at most 500) entries a page, pages from 1 while has_more is 1 [docs]; an answer without
    has_more (OpenList's driver doesn't read it) goes on while its pages are full. The root also lists the folders
    the account subscribed to from others' shares (is_share 1) [docs]; they are left out: they are another
    account's, listed with other parameters. The docs don't say how file_list answers for a folder that is gone, so a
    cached folder id is trusted until it expires (folder_ttl).

Login
    The admin's own key: on the open platform's Secret Key page (https://www.febbox.com/open/clients) an account
    generates a client_id and client_secret, giving an app name and a redirect URI [docs], which this driver doesn't
    use; the page lists client_secret above client_id [OpenList]. POST /oauth/token with an
    application/x-www-form-urlencoded body of grant_type client_credentials, client_id and client_secret gives an
    access token for the files of that account and nobody else's, and is also how the token is renewed
    [docs]: nothing is saved. The token is kept in memory until shortly before its expires_in (TokenCache), and got
    again when the API answers code -10001, "invalid token" [docs]. FebBox's authorization-code flow, for apps that
    act for other accounts, isn't used.

Requests
    POST /oauth with "Authorization: Bearer <token>" and a multipart/form-data body of text fields whose `module`
    names the call [docs]. Answers are {"code", "msg", "data"}: code 1 is success, below 1 a failure [docs]. An
    account may make 120 calls a minute, code -429 beyond [docs]: calls are paced at `rate` a second, and a
    throttled one is tried again after 1, 2, 4... seconds for at most `throttle_wait` seconds. Fast callers may get a
    Cloudflare page [docs]: such a page (HTML), or an HTTP 403, is backed off the same way ("Back off on -429 or
    HTTP 403/challenge pages" [docs]), and is a BackendError when it doesn't go away; another answer that isn't JSON
    is a BackendError at once.

Changes
    mkdir: create_dir with parent_id and name [docs] (the module's name [OpenList]: its page leaves it out); its answer
    isn't documented, so the new folder is found in its parent's listing. rename: file_rename (fid, name); copy:
    file_copy (fids[], to); delete: file_delete (fids[]) [docs]. move: file_move with fids[] and to [OpenList], as
    copy takes them: the docs' move page repeats the delete page. The docs don't say whether a delete goes to a
    trash, nor what a name taken in the target folder does: HFile looks there first (IdDriver).

Downloads
    file_get_play_url_user gives one file's original.download_url, which takes Range and lasts 14400 seconds [docs];
    it is reused for `link_ttl` seconds. Its traffic counts against the account's own quota, as on the website [docs]
    (free accounts: 100 GB of high-speed traffic a month, then large files at 100 KB/s; premium ones: a daily quota
    [FAQ]); past the hard cap the module answers code -1, "Traffic exceeded" [docs]. file_get_download_url isn't
    used: its traffic is billed to the open platform channel's balance [docs]. The links are for players to fetch as
    they are [docs], so browsers get them too (download_url); they can't be given a name, so `filename` isn't used.
    FebBox picks the download server by the ip of the call, by default the caller's [docs]: this site's.

Uploads
    None: the open platform only adds files from an http(s) URL that FebBox fetches itself (file_add [docs]), so
    can_upload is False (nobody gets the "Upload" permission) and write() raises Unsupported. Files are uploaded in
    FebBox's apps and website, or through its WebDAV [FAQ].

Names
    The docs don't say which names FebBox refuses or whether a folder may hold two of a name: none are refused here,
    and names are compared as they are spelled.

Limits
    Not tried against the real FebBox: the tests use a fake built from the documentation above.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http
from ._cloud import (
    RateLimiter,
    TokenCache,
    TTLCache,
    as_int,
    json_or_none,
    origin,
    parse_time,
    range_header,
    redirected,
    response_stream,
    retry_after,
    short,
    transport_error,
    usable_name,
)
from ._ids import IdDriver, Node, rank
from .base import AuthError, BackendError, DriverError, Param, ReadStream, Unsupported

SERVICE = gettext_lazy("FebBox")
OK = 1  # an answer's code for success; failures are below 1
BAD_TOKEN = -10001  # "invalid token": missing, expired or not FebBox's
THROTTLED = -429  # "too many requests, max 120 per minute"


def _unreadable() -> BackendError:
    return BackendError(gettext("The answer from %(service)s couldn't be understood. Please try again later.")
                        % {"service": SERVICE})


def _detail(code: Any, message: str) -> str:
    return (gettext(" (%(code)s): %(message)s") % {"code": code, "message": message} if message
            else gettext(" (%(code)s)") % {"code": code})


def _to_node(item: Any) -> Node | None:
    """A Node of a file_list entry; None for a folder subscribed from someone's share, or an entry without an id or a
    name HFile can show."""
    if not isinstance(item, dict) or as_int(item.get("is_share")) == 1:
        return None
    fid, name = as_int(item.get("fid")), item.get("file_name")
    if fid is None or fid <= 0 or not usable_name(name):
        return None
    is_dir = as_int(item.get("is_dir")) == 1
    return Node(id=str(fid), name=name, is_dir=is_dir, size=None if is_dir else as_int(item.get("file_size")),
                modified=parse_time(item.get("update_time")) or parse_time(item.get("add_time")))


class FebBoxDriver(IdDriver):
    type = "febbox"
    label = gettext_lazy("FebBox")
    service = SERVICE
    root_id = "0"
    params = (
        Param("client_id", gettext_lazy("Client ID"),
              help=gettext_lazy("On FebBox's \"Secret Key\" page (https://www.febbox.com/open/clients), generate a key: "
                                "any app name will do, and http://localhost as its redirect URI, which HFile doesn't "
                                "use. Enter the key's client_id here; the page shows it below client_secret. HFile "
                                "shows the files of the FebBox account that generated the key.")),
        Param("client_secret", gettext_lazy("Client secret"), kind="password", secret=True,
              help=gettext_lazy("The client_secret of the same key.")),
        Param("base_path", gettext_lazy("Base path"), kind="path", required=False, default="/",
              help=gettext_lazy("The folder in FebBox that is this storage source's root, like / or /folder1.")),
    )

    can_range = True
    can_direct_download = True  # the links carry no token
    can_direct_upload = False
    can_move = True
    can_copy = True
    can_search = False
    has_folders = True
    can_upload = False  # see Uploads above

    # Endpoints and tuning; tests point them at a fake and make them small.
    api_root: ClassVar[str] = "https://api.febbox.com"
    page_size: ClassVar[int] = 500  # entries a listing page, file_list's most
    max_pages: ClassVar[int] = 10_000
    rate: ClassVar[float] = 1.5  # calls a second: three quarters of the 120 a minute FebBox allows an account
    queue_wait: ClassVar[float] = 10.0  # seconds a call may wait for its turn, else BackendError
    throttle_delay: ClassVar[float] = 1.0  # the first wait after a -429; it doubles
    throttle_wait: ClassVar[float] = 15.0  # seconds a throttled call is tried again for
    link_ttl: ClassVar[float] = 3600.0  # seconds a download link is reused (FebBox's last 14400)

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)  # listing_ttl, guard_ttl, folder_ttl: IdDriver's
        self._http = http.client()
        self._tokens = TokenCache(self._new_token)
        self._pace = RateLimiter(self.rate, longest=self.queue_wait, service=SERVICE)
        self._links = TTLCache(1024, self.link_ttl)  # (fid, size, modified) -> download link

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        return cls.base_path_errors(config)

    # -- login

    def _new_token(self) -> tuple[str, float]:
        """TokenCache's refresh: an access token of the key (client_credentials), and its lifetime in seconds."""
        client_id = str(self.config.get("client_id") or "").strip()
        secret = str(self.config.get("client_secret") or "").strip()
        if not client_id or not secret:
            raise AuthError(gettext("Please fill in the client ID and client secret first."))
        status, answer = self._exchange(self.api_root + "/oauth/token", None, data={
            "grant_type": "client_credentials", "client_id": client_id, "client_secret": secret})
        code, data = as_int(answer.get("code")), answer.get("data")
        if code != OK:
            detail = _detail(code if code is not None else f"HTTP {status}", short(answer.get("msg") or ""))
            raise AuthError(gettext("FebBox refused the client ID or secret%(detail)s. Please check them on FebBox's "
                                    "\"Secret Key\" page (https://www.febbox.com/open/clients), which lists "
                                    "client_secret above client_id.") % {"detail": detail})
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token.strip() or not token.strip().isprintable():
            raise _unreadable()
        self.log.debug("got a new access token")
        return token.strip(), float(as_int(data.get("expires_in")) or 3600)

    # -- requests

    def _exchange(self, url: str, token: str | None, **body: Any) -> tuple[int, dict[str, Any]]:
        """One POST of `body` (httpx's data= or files=), paced, and tried again while FebBox says it's throttling or
        Cloudflare's page (or a 403) is in the way: the HTTP status and the answer, a JSON object. BackendError when
        FebBox failed (5xx) or answered with something else (a Cloudflare page that didn't go away)."""
        deadline = time.monotonic() + self.throttle_wait
        delay = self.throttle_delay
        while True:
            self._pace.wait()
            headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
            try:
                response = self._http.post(url, headers=headers, **body)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            status = response.status_code
            if 300 <= status < 400:
                raise redirected(response, SERVICE)
            answer = json_or_none(response)
            code = as_int(answer.get("code")) if isinstance(answer, dict) else None
            throttled = status == 429 or code == THROTTLED
            # Not a refused token: waiting wouldn't help it, a new token does (_call).
            challenged = (status == 403 and code != BAD_TOKEN) or (
                not isinstance(answer, dict) and "html" in response.headers.get("content-type", "").lower())
            if throttled or challenged:
                wait = retry_after(response, default=delay, longest=deadline - time.monotonic())
                if wait is not None:
                    self.log.debug("FebBox is throttling; trying again in %.1f s", wait)
                    time.sleep(wait)
                    delay *= 2
                    continue
                if throttled:
                    raise BackendError(gettext("%(service)s is rate-limiting: too many requests. Please try again later.")
                                       % {"service": SERVICE})
            if status >= 500 or (status >= 400 and not isinstance(answer, dict)):
                raise BackendError(gettext("%(service)s failed (HTTP %(status_code)s). Please try again later.")
                                   % {"service": SERVICE, "status_code": status})
            if not isinstance(answer, dict):
                raise _unreadable()
            return status, answer

    def _call(self, module: str, fields: Mapping[str, Any]) -> dict[str, Any]:
        """A call of the API: its answer when its code is 1, else the DriverError. When FebBox refuses the token
        (-10001), another one is got and the call made once more."""
        for attempt in range(2):
            token = self._tokens.get()
            form = {"module": module, **fields}  # as text fields of a multipart/form-data body, (None, value)
            _status, answer = self._exchange(self.api_root + "/oauth", token,
                                             files={name: (None, str(value)) for name, value in form.items()})
            code = as_int(answer.get("code"))
            if code == OK:
                return answer
            if code == BAD_TOKEN:
                self._tokens.invalidate(token)
                if not attempt:
                    self.log.info("the access token was refused; getting another")
                    continue
                raise AuthError()
            if code is None:
                raise _unreadable()
            raise DriverError(gettext("%(service)s refused this (%(code)s): %(message)s")
                              % {"service": SERVICE, "code": code, "message": short(answer.get("msg") or "") or "-"})
        raise AssertionError("unreachable")

    # -- files

    def _link(self, node: Node, *, fresh: bool = False) -> str:
        """The file's original.download_url, reused while its id, size and time stay the same."""
        key = (node.id, node.size, node.modified)
        if not fresh:
            cached = self._links.get(key)
            if cached is not None:
                return cached
        data = self._call("file_get_play_url_user", {"fid": node.id}).get("data")
        original = data.get("original") if isinstance(data, dict) else None
        link = original.get("download_url") if isinstance(original, dict) else None
        if not isinstance(link, str) or origin(link) == ("", "", 0):
            raise BackendError(gettext("%(service)s gave no download address for this file.") % {"service": SERVICE})
        self._links.put(key, link)
        return link

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        return self._link(self._file(path))

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        raise Unsupported(gettext("FebBox's open platform can't upload files: upload them in FebBox's apps or on its website."))

    # -- IdDriver's hooks

    def _fetch(self, folder_id: str) -> list[Node]:
        nodes: dict[str, Node] = {}  # by id: an entry that moves between pages comes twice
        for page in range(1, self.max_pages + 1):
            data = self._call("file_list", {"parent_id": folder_id, "page": str(page), "pagelimit": str(self.page_size),
                                            "order": "name_asc"}).get("data")
            items = data.get("file_list") if isinstance(data, dict) else None
            if items is None and isinstance(data, dict):
                items = []  # an empty folder may say null
            if not isinstance(items, list):
                raise _unreadable()
            for item in items:
                node = _to_node(item)
                if node is not None:
                    nodes[node.id] = node
            more = as_int(data.get("has_more"))
            if not (more == 1 if more is not None else len(items) >= self.page_size):
                return list(nodes.values())
        raise BackendError(gettext("The file list from %(service)s never ends. Please try again later.") % {"service": SERVICE})

    def _create_node(self, parent_id: str, name: str) -> Node:
        self._call("create_dir", {"parent_id": parent_id, "name": name})
        made = [node for node in self._fetch(parent_id) if node.is_dir and node.name == name]
        if not made:
            raise BackendError(gettext("%(service)s didn't confirm the new folder. Please refresh to see whether it is there.")
                               % {"service": SERVICE})
        return max(made, key=rank)

    def _delete_node(self, node: Node, parent_id: str) -> None:
        self._call("file_delete", {"fids[]": node.id})

    def _rename_node(self, node: Node, name: str) -> None:
        self._call("file_rename", {"fid": node.id, "name": name})

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        self._call("file_move", {"fids[]": node.id, "to": target_id})

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        self._call("file_copy", {"fids[]": node.id, "to": target_id})
        return None  # the answer isn't documented: the folder is listed again

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        if length == 0 or node.size == 0:  # nothing to fetch
            return ReadStream(iter(()), size=0, total=node.size, offset=offset, modified=node.modified)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, node.size)
        if span:
            headers["Range"] = span
        for attempt in range(2):
            request = self._http.build_request("GET", self._link(node, fresh=attempt > 0), headers=headers)
            try:  # no token: the link needs none
                response = self._http.send(request, stream=True, follow_redirects=True)
            except httpx.HTTPError as error:
                raise transport_error(error, SERVICE) from None
            if response.status_code in (403, 404, 410) and not attempt:
                http.drain(response)  # an expired link: ask for another
                continue
            return response_stream(response, offset=offset, length=length, total=node.size, service=SERVICE,
                                    modified=node.modified)
        raise AssertionError("unreachable")
