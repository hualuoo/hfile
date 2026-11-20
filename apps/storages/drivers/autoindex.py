"""AutoIndex: the directory listing pages of a web server (nginx's autoindex, Apache's mod_autoindex, lighttpd,
Caddy's browse, Python's http.server), as a read-only storage.

Paths
    A folder is the page at its URL, ending in "/": `url` is the root's. Its entries are the links to the
    names right below it ("a.txt", "sub/"), taken from the link's href (the text may be cut short, as nginx
    cuts long names), the href and the folder's address compared percent-decoded (servers escape other
    characters than quote() does); a trailing "/" makes a folder. Links elsewhere (sorting links "?C=N;O=D", the
    parent "../", other sites) and the names in `ignore` are left out. JSON gives exact names, kinds, sizes and
    times: nginx's "autoindex_format json" (name, type, mtime, size), and Caddy's browse, which answers JSON to
    an Accept with application/json (name, a folder's with "/" after it, is_dir, mod_time, size). In HTML
    pages the size and time are read from the text after the link on its line, or the rest of its table row:
    the size is the column right after the time when it is in bytes (a rounded one, "1.2M", isn't used), or
    the exact one in a cell's data-value (lighttpd) or data-size (Caddy); of a <time> element only its
    datetime is read; a time without a time zone is taken as UTC. The files whose size isn't known that way
    are asked about with HEAD, without compression (a compressed answer's length isn't the file's: no size
    then), `head_threads` at a time, at most `head_limit` of them a listing (the others show no size until
    looked at by themselves). What HEAD says is kept `head_ttl` seconds; a HEAD that fails leaves the size
    unknown. Listings are cached for `cache_ttl` seconds.

Requests
    The client is guarded (http.client(guard=True)): when `url` is on the Internet, neither it nor a redirect
    may lead into the site's own network (http.py's docstring); a server there (`url` on the LAN) redirects
    where it likes. A listing page is asked for without compression, so that MAX_PAGE bounds what the server
    sends, and read in `page_seconds` at most. An answer that is an error is read to its end when it is short (http.drain()), so
    that its connection can be used again; a longer one is cut off.

Downloads
    The file's URL, with the Range: download_url() is that URL (looked up in the cached listing, without a
    request) when the server needs no password; with `username` (HTTP Basic), or a user name and password in
    the address (the settings refuse them: they would go to every browser), the site sends the file.
"""

from __future__ import annotations

import codecs
import datetime
import email.utils
import html.parser
import json
import re
import time
import urllib.parse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy, ngettext

from . import http, paths
from ._cloud import TTLCache, as_int, json_or_none, parse_time, range_header, response_stream, transport_error, usable_name
from .base import (
    AuthError,
    BackendError,
    DriverError,
    Entry,
    NotADirectory,
    NotFound,
    Param,
    ReadStream,
    StorageDriver,
)

SERVICE = gettext_lazy("the web server")
MAX_PAGE = 16 * 1024 * 1024  # bytes of a listing page read at most
_DATES = [  # (regular expression, strptime format) of the times listings show
    (r"\d{1,2}-[A-Za-z]{3}-\d{4} \d{2}:\d{2}(?::\d{2})?", ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S")),
    (r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(?::\d{2})?", ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S")),
    (r"\d{4}-[A-Za-z]{3}-\d{2} \d{2}:\d{2}:\d{2}", ("%Y-%b-%d %H:%M:%S",)),
]
_DATE = re.compile("|".join(f"(?:{pattern})" for pattern, _ in _DATES))


def _time(text: str) -> datetime.datetime | None:
    found = _DATE.search(text)
    if not found:
        return None
    for _, formats in _DATES:
        for form in formats:
            try:
                return datetime.datetime.strptime(found.group(0), form).replace(tzinfo=datetime.timezone.utc)
            except ValueError:
                continue
    return None


def _size(text: str) -> int | None:
    """The size in bytes in the text after a link: the column right after its time, when that is a plain number
    (not "1.2M", "-"); None without a time. A description column further on isn't read."""
    found = _DATE.search(text)
    following = text[found.end():].split(None, 1) if found else []
    return int(following[0]) if following and following[0].isascii() and following[0].isdigit() else None


def _userinfo(url: str) -> bool:
    """Whether an address has a user name or password in it (https://user:password@host/)."""
    try:
        return bool(httpx.URL(url).userinfo)
    except httpx.InvalidURL:
        return False


def _too_slow(seconds: float) -> BackendError:
    return BackendError(ngettext("The storage didn't finish answering in %(seconds)s second. Please try again later.",
                                 "The storage didn't finish answering in %(seconds)s seconds. Please try again later.",
                                 seconds) % {"seconds": format(seconds, "g")})


class _Links(html.parser.HTMLParser):
    """The links of a listing page, each with the text after it on its line or in its table row, and the exact
    size a cell of that row gives (lighttpd's data-value, Caddy's data-size)."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[list[Any]] = []  # [href, text after, exact size or None]
        self._current: list[Any] | None = None
        self._in_link = False
        self._in_row = False  # in a table's row: its cells are the link's details, on whatever lines
        self._in_time = False

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a":
            href = values.get("href")
            self._in_link = True
            if href is not None:
                self._current = [href, "", None]
                self.links.append(self._current)
        elif tag == "tr":
            self._current, self._in_row = None, True
        elif self._current is not None:
            if tag == "time":  # Caddy: <time datetime="2026-11-17T12:00:00Z">11/17/2026 12:00:00 PM +00:00</time>
                self._in_time = True
                stamp = values.get("datetime")
                if stamp:
                    self._current[1] += " " + stamp[:19]
            exact = values.get("data-size")  # Caddy's size cell
            if exact is None and "s" in (values.get("class") or "").split():
                exact = values.get("data-value")  # lighttpd's
            if exact is not None and exact.isascii() and exact.isdigit():
                self._current[2] = int(exact)

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False
        elif tag == "tr":
            self._current, self._in_row = None, False
        elif tag == "time":
            self._in_time = False

    def handle_data(self, data):
        if self._current is None or self._in_link or self._in_time:
            return
        if self._in_row:
            self._current[1] += " " + data
            return
        text, newline, _ = data.partition("\n")
        self._current[1] += " " + text
        if newline:
            self._current = None


class AutoIndexDriver(StorageDriver):
    type = "autoindex"
    label = gettext_lazy("Web server directory listing")
    params = (
        Param("url", gettext_lazy("Address"), placeholder="https://mirror.example.com/pub/",
              help=gettext_lazy("The address of the listing page of the folder to show (nginx autoindex, Apache, Caddy, lighttpd...).")),
        Param("username", gettext_lazy("Username"), required=False, advanced=True,
              help=gettext_lazy("For a server that asks for a password (HTTP Basic). Downloads then go through this site.")),
        Param("password", gettext_lazy("Password"), kind="password", required=False, secret=True, advanced=True),
        Param("ignore", gettext_lazy("Names to leave out"), kind="textarea", required=False, default="Parent Directory\nUp", advanced=True,
              help=gettext_lazy("One per line: entries of the listing that aren't files or folders.")),
    )

    can_range = True
    can_direct_download = True  # without a password, see download_url()
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    setup_params = ("url",)

    # Tuning, described in the module's docstring; tests make them small.
    cache_ttl: ClassVar[float] = 30.0
    head_limit: ClassVar[int] = 64
    head_threads: ClassVar[int] = 8
    head_ttl: ClassVar[float] = 300.0
    page_seconds: ClassVar[float] = 60.0

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        url = str(self.config.get("url") or "").strip()
        self._root = url if url.endswith("/") else url + "/"
        username = str(self.config.get("username") or "")
        auth = httpx.BasicAuth(username, str(self.config.get("password") or "")) if username else None
        # A user name and password in the address (saved past the form's check) log in too: never give them out.
        self._private = auth is not None or _userinfo(url)
        self._http = http.client(auth=auth, guard=True)
        self._ignore = {line.strip() for line in str(self.config.get("ignore") or "").splitlines() if line.strip()}
        self._listings = TTLCache(256, self.cache_ttl)
        self._heads = TTLCache(4096, self.head_ttl)  # (path, its time in the listing) -> the Entry HEAD gave

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        raw = str(config.get("url") or "").strip()
        url = httpx.URL(raw) if raw else None
        if url is None or url.scheme not in ("http", "https") or not url.host:
            return {"url": gettext("The address should start with http:// or https://, like https://mirror.example.com/pub/.")}
        if "?" in raw or "#" in raw:
            return {"url": gettext("The address should be the folder's page without a ? or # part, like "
                                   "https://mirror.example.com/pub/.")}
        if url.userinfo:
            return {"url": gettext('The address can\'t contain a user name or password: fill them in "Username" and '
                                   '"Password" under "More settings".')}
        return {}

    # -- requests

    def _url(self, path: str, *, folder: bool) -> str:
        quoted = "/".join(urllib.parse.quote(part, safe="") for part in paths.segments(path))
        return self._root + quoted + ("/" if folder and quoted else "")

    def _failure(self, response: httpx.Response) -> DriverError:
        if response.status_code == 404:
            return NotFound()
        if response.status_code in (401, 403):
            if self._private:
                return AuthError(gettext("The web server refused access (HTTP %(status)s). Check the username and password.") % {"status": response.status_code})
            if response.status_code == 401:  # it wants a password: the settings have none
                return AuthError(gettext('The web server asks for a username and password (HTTP 401): fill in "Username" '
                                         'and "Password" under "More settings".'))
            return DriverError(gettext("The web server refused access (HTTP %(status)s).") % {"status": response.status_code})
        return BackendError(gettext("The web server failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})

    def _listing(self, path: str) -> dict[str, Entry]:
        cached = self._listings.get(path)
        if cached is not None:
            return cached
        ends = time.monotonic() + self.page_seconds
        try:
            with self._http.stream("GET", self._url(path, folder=True), follow_redirects=True,
                                   headers={"Accept": "text/html, application/json", "Accept-Encoding": "identity"},
                                   extensions=http.configured(self._root)) as response:
                if response.status_code != 200:
                    http.drain(response)
                    raise self._failure(response)
                pieces: list[bytes] = []
                size = 0
                with http.cut_off(response, ends):
                    for piece in response.iter_bytes():
                        pieces.append(piece)
                        size += len(piece)
                        if size > MAX_PAGE:
                            raise BackendError(gettext("The listing page is too big."))
                kind = response.headers.get("content-type", "").lower()
                charset = response.charset_encoding or "utf-8"
        except httpx.HTTPError as error:
            if time.monotonic() >= ends:
                raise _too_slow(self.page_seconds) from None
            raise transport_error(error, SERVICE) from None
        try:
            codecs.lookup(charset)
        except LookupError:  # a name Python doesn't know ("utf8mb4")
            charset = "utf-8"
        page = b"".join(pieces)
        entries = self._parse_json(path, page) if "json" in kind else self._parse_html(path, page.decode(charset, errors="replace"))
        self._fill_sizes(entries)
        self._listings.put(path, entries)
        return entries

    def _fill_sizes(self, entries: dict[str, Entry]) -> None:
        """The sizes the listing didn't give: those HEAD gave before, and by HEAD (see the module's docstring)."""
        asking = []
        for entry in [entry for entry in entries.values() if not entry.is_dir and entry.size is None]:
            known = self._heads.get((entry.path, entry.modified))
            if known is not None:
                entries[entry.name] = known
            elif len(asking) < self.head_limit:
                asking.append(entry)
        if not asking:
            return
        with ThreadPoolExecutor(max_workers=min(self.head_threads, len(asking))) as pool:
            for entry in pool.map(self._head, asking):
                entries[entry.name] = entry

    def _parse_json(self, path: str, page: bytes) -> dict[str, Entry]:
        """nginx's "autoindex_format json": [{"name", "type": "file" | "directory", "mtime", "size"}]; Caddy's browse:
        [{"name" (a folder's with "/" after it), "is_dir", "mod_time", "size", ...}]."""
        try:
            items = json.loads(page)
        except ValueError:
            raise BackendError(gettext("The listing page couldn't be understood.")) from None
        entries: dict[str, Entry] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            is_dir = item.get("type") == "directory" or item.get("is_dir") is True
            if is_dir and isinstance(name, str) and name.endswith("/"):  # Caddy's
                name = name[:-1]
            if not usable_name(name) or name in self._ignore:
                continue
            modified = None
            stamp = item.get("mtime", item.get("mod_time"))
            if isinstance(stamp, str):
                try:
                    modified = datetime.datetime.strptime(stamp, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=datetime.timezone.utc)
                except ValueError:
                    modified = parse_time(stamp)
            entries[name] = Entry(name=name, path=paths.join(path, name), is_dir=is_dir,
                                  size=None if is_dir else as_int(item.get("size")), modified=modified)
        return entries

    def _parse_html(self, path: str, page: str) -> dict[str, Entry]:
        parser = _Links()
        parser.feed(page)
        base = urllib.parse.urlsplit(self._url(path, folder=True))
        folder = urllib.parse.unquote(base.path)
        entries: dict[str, Entry] = {}
        for href, after, exact in parser.links:
            target = urllib.parse.urlsplit(urllib.parse.urljoin(base.geturl(), href.strip()))
            if (target.scheme, target.netloc) != (base.scheme, base.netloc) or target.query or target.fragment:
                continue
            where = urllib.parse.unquote(target.path)
            if not where.startswith(folder):
                continue
            rest = where[len(folder):]
            is_dir = rest.endswith("/")
            name = rest[:-1] if is_dir else rest
            if not name or "/" in name or not usable_name(name) or name in self._ignore or name in entries:
                continue
            entries[name] = Entry(name=name, path=paths.join(path, name), is_dir=is_dir,
                                  size=None if is_dir else (exact if exact is not None else _size(after)),
                                  modified=_time(after))
        return entries

    def _head(self, entry: Entry) -> Entry:
        """`entry` with the size and time the server says for the file (HEAD), kept head_ttl seconds; `entry` as it
        is when HEAD fails."""
        key = (entry.path, entry.modified)
        known = self._heads.get(key)
        if known is not None:
            return known
        try:
            response = self._http.head(self._url(entry.path, folder=False), headers={"Accept-Encoding": "identity"},
                                       follow_redirects=True, extensions=http.configured(self._root))
        except (httpx.HTTPError, http.InsideNetwork):
            return entry
        if response.status_code != 200:
            return entry
        encoding = response.headers.get("content-encoding", "").strip().lower()
        size = as_int(response.headers.get("content-length")) if encoding in ("", "identity") else None  # else the .gz's
        modified = entry.modified
        if response.headers.get("last-modified"):
            try:
                modified = email.utils.parsedate_to_datetime(response.headers["last-modified"])
            except (TypeError, ValueError):
                pass
        found = Entry(name=entry.name, path=entry.path, is_dir=False, size=size, modified=modified)
        self._heads.put(key, found)
        return found

    def _find(self, path: str) -> Entry:
        parent, name = paths.split(path)
        try:
            entry = self._listing(parent).get(name)
        except NotFound:
            entry = None
        if entry is None:
            raise NotFound()
        if not entry.is_dir and entry.size is None:
            entry = self._head(entry)
        return entry

    # -- files

    def list(self, path: str) -> list[Entry]:
        if path != paths.ROOT and not self._find(path).is_dir:
            raise NotADirectory()
        return list(self._listing(path).values())

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._listing(path)
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._find(path)

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        entry = self._find(path) if path != paths.ROOT else None
        if entry is None or entry.is_dir:
            raise NotFound()
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, entry.size)
        if span:
            headers["Range"] = span
        try:
            response = self._http.send(self._http.build_request("GET", self._url(path, folder=False), headers=headers,
                                                                extensions=http.configured(self._root)),
                                       stream=True, follow_redirects=True)
        except httpx.HTTPError as error:
            raise transport_error(error, SERVICE) from None
        if response.status_code not in (200, 206):
            http.drain(response)
            raise self._failure(response)
        return response_stream(response, offset=offset, length=length, total=entry.size, service=SERVICE,
                               modified=entry.modified)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        if self._private:
            return None
        parent, name = paths.split(path)
        entry = self._listing(parent).get(name) if path != paths.ROOT else None  # no HEAD: the size isn't needed
        if entry is None or entry.is_dir:
            raise NotFound()
        return self._url(path, folder=False)
