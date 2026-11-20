"""URL tree: files that are web addresses, arranged in folders by the admin, as a read-only storage.

Paths
    `tree` has one line per file or folder; two spaces of indent go one folder deeper:

        Linux ISOs:
          debian.iso:2147483648:https://mirror.example.com/debian.iso
          https://mirror.example.com/ubuntu.iso
        notes.txt:https://example.com/notes.txt

    A line ending in ":" is a folder; else it is [name:][size:]address, the address starting with http://
    or https:// (and one httpx can use: a port that isn't a number is refused). Without a name, the file is
    named after the address's last segment; without a size (in bytes) it is asked for with HEAD when the file
    is looked at by itself, and in listings too with `head_sizes` (`head_threads` at a time, at most
    `head_limit` a listing). HEAD asks for no compression (a compressed answer's length isn't the file's: no
    size then); what it says is kept `cache_ttl` seconds, that it failed `failure_ttl` seconds.

Requests
    The client is guarded (http.client(guard=True)): a file whose address is on the Internet may not lead into
    the site's own network, by its name or a redirect (http.py's docstring); one whose address is on the LAN
    is fetched wherever it redirects.

Downloads
    The address itself, with the Range: download_url() is the address (the browser fetches it from its
    server), unless it has a user name or password in it (they would go to every browser: the site sends the
    file then); open() fetches it from here, following redirects, without HEAD (the answer gives the file's
    length). An answer that is an error is read to its end when it is short (http.drain()), so that its
    connection can be used again; a longer one is cut off. (A HEAD's answer has no body.)
"""

from __future__ import annotations

import dataclasses
import email.utils
import re
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any, ClassVar

import httpx

from django.utils.translation import gettext, gettext_lazy

from . import http, paths
from ._cloud import TTLCache, as_int, range_header, response_stream, transport_error, usable_name
from .base import BackendError, DriverError, Entry, NotADirectory, NotFound, Param, ReadStream, StorageDriver

SERVICE = gettext_lazy("the web server")
_ADDRESS = re.compile(r"https?://\S+$")


def _userinfo(url: str) -> bool:
    """Whether an address has a user name or password in it (https://user:password@host/)."""
    try:
        return bool(httpx.URL(url).userinfo)
    except httpx.InvalidURL:
        return False


def _usable_address(url: str) -> bool:
    """Whether httpx can send a request to `url` (it refuses a port that isn't a number, control characters)."""
    try:
        return bool(httpx.URL(url).host)
    except httpx.InvalidURL:
        return False


def _invalid_address() -> DriverError:
    return DriverError(gettext("The file's address isn't a valid web address. Please correct it in the storage "
                               "source's settings."))


@dataclasses.dataclass
class _Folder:
    children: dict[str, _Folder | _File] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class _File:
    url: str
    size: int | None


def parse_tree(text: Any) -> tuple[_Folder, list[str]]:
    """The tree of the `tree` param, and what is wrong with it (messages naming the lines)."""
    root = _Folder()
    stack: list[_Folder] = [root]  # the folders of the levels so far
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines() if isinstance(text, str) else [], start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        spaces = len(raw) - len(raw.lstrip(" "))
        level, odd = divmod(spaces, 2)
        line = raw.strip()
        if odd or raw[spaces:spaces + 1] == "\t" or level > len(stack) - 1:
            errors.append(gettext("Line %(line)s: indent with two spaces a level, one level deeper than the folder above at most.") % {"line": number})
            continue
        del stack[level + 1:]
        folder = stack[level]
        address = _ADDRESS.search(line)
        if address is None:
            if not line.endswith(":"):
                errors.append(gettext("Line %(line)s: a folder ends with \":\", a file has an http:// or https:// address.") % {"line": number})
                continue
            name, node = line[:-1].strip(), _Folder()
        else:
            prefix = line[:address.start()].rstrip(":").strip()
            name, _, size = prefix.rpartition(":")
            if not (size.isascii() and size.isdigit()):
                name, size = prefix, ""
            url = address.group(0)
            if not _usable_address(url):
                errors.append(gettext("Line %(line)s: the address isn't a valid web address.") % {"line": number})
                continue
            if not name:
                name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rstrip("/").rpartition("/")[2])
            node = _File(url=url, size=int(size) if size else None)
        if not usable_name(name) or "\\" in name:
            errors.append(gettext("Line %(line)s: the name is missing or can't be used.") % {"line": number})
            continue
        if name in folder.children:
            errors.append(gettext("Line %(line)s: %(name)s is in this folder already.") % {"line": number, "name": name})
            continue
        folder.children[name] = node
        if isinstance(node, _Folder):
            stack.append(node)
    if not root.children and not errors:
        errors.append(gettext("Give at least one file or folder."))
    return root, errors


class URLTreeDriver(StorageDriver):
    type = "url-tree"
    label = gettext_lazy("URL tree")
    service: ClassVar[Any] = SERVICE  # where the links lead, for messages
    params = (
        Param("tree", gettext_lazy("Files and folders"), kind="textarea",
              placeholder="Linux ISOs:\n  debian.iso:2147483648:https://mirror.example.com/debian.iso\nhttps://example.com/notes.txt",
              help=gettext_lazy('One per line, two spaces of indent a folder deeper. A folder: name: (with the ":"). '
                                "A file: its address, or name:address, or name:size in bytes:address.")),
        Param("head_sizes", gettext_lazy("Ask for sizes in listings"), kind="bool", required=False, default=False, advanced=True,
              help=gettext_lazy("Ask the servers for the size of the files without one (HEAD) when a folder is listed, not only when a file is opened.")),
    )

    can_range = True
    can_direct_download = True
    can_direct_upload = False
    can_move = False
    can_copy = False
    can_search = False
    has_folders = True
    can_write = False

    # Tuning: the HEADs of a listing (see the module's docstring), the seconds a size from HEAD is kept, and that a
    # HEAD failed.
    head_limit: ClassVar[int] = 64
    head_threads: ClassVar[int] = 8
    cache_ttl: ClassVar[float] = 300.0
    failure_ttl: ClassVar[float] = 60.0

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self._tree, _ = parse_tree(self.config.get("tree"))
        self._head_sizes = bool(self.config.get("head_sizes"))
        self._http = http.client(guard=True)
        self._sizes = TTLCache(4096, self.cache_ttl)  # address -> (size, modified) from HEAD
        self._failed = TTLCache(4096, self.failure_ttl)  # address -> the DriverError its HEAD gave

    def close(self) -> None:
        self._http.close()

    # -- the admin form

    @classmethod
    def validate_config(cls, config: Mapping[str, Any]) -> dict[str, str]:
        _, errors = parse_tree(config.get("tree"))
        return {"tree": " ".join(errors)} if errors else {}

    # -- requests

    def _node(self, path: str) -> _Folder | _File:
        node: _Folder | _File = self._tree
        for name in paths.segments(path):
            if not isinstance(node, _Folder) or name not in node.children:
                raise NotFound()
            node = node.children[name]
        return node

    def _address(self, file: _File, expires: int | None = None) -> str:
        """Where `file` is fetched from: by a browser for `expires` seconds (download_url), or by this site (None).
        The tree's link; a subclass may sign it (123's direct links)."""
        return file.url

    def _head(self, file: _File) -> tuple[int | None, Any]:
        cached = self._sizes.get(file.url)
        if cached is not None:
            return cached
        failed = self._failed.get(file.url)
        if failed is not None:
            raise type(failed)(failed.message)
        try:
            try:
                response = self._http.head(self._address(file), headers={"Accept-Encoding": "identity"},
                                           follow_redirects=True, extensions=http.configured(file.url))
            except httpx.InvalidURL:
                raise _invalid_address() from None
            except httpx.HTTPError as error:
                raise transport_error(error, self.service) from None
            if response.status_code != 200:
                raise self._failure(response)
        except DriverError as error:
            self._failed.put(file.url, error)
            raise
        modified = None
        if response.headers.get("last-modified"):
            try:
                modified = email.utils.parsedate_to_datetime(response.headers["last-modified"])
            except (TypeError, ValueError):
                modified = None
        encoding = response.headers.get("content-encoding", "").strip().lower()
        size = as_int(response.headers.get("content-length")) if encoding in ("", "identity") else None  # else the .gz's
        found = (size, modified)
        self._sizes.put(file.url, found)
        return found

    def _failure(self, response: httpx.Response) -> DriverError:
        if response.status_code in (404, 410):
            return NotFound()
        if response.status_code in (401, 403):
            return DriverError(gettext("The web server refused access (HTTP %(status)s); the link may have expired.") % {"status": response.status_code})
        return BackendError(gettext("The web server failed (HTTP %(status)s). Please try again later.") % {"status": response.status_code})

    def _entry(self, path: str, node: _Folder | _File, *, ask: bool) -> Entry:
        name = paths.name(path) if path != paths.ROOT else ""
        if isinstance(node, _Folder):
            return Entry(name=name, path=path, is_dir=True)
        size, modified = node.size, None
        if ask and size is None:
            try:
                size, modified = self._head(node)
            except DriverError:
                pass  # shown without a size
        return Entry(name=name, path=path, is_dir=False, size=size, modified=modified)

    # -- files

    def list(self, path: str) -> list[Entry]:
        node = self._node(path)
        if not isinstance(node, _Folder):
            raise NotADirectory()
        items = [(paths.join(path, name), child) for name, child in node.children.items()]
        if not self._head_sizes:
            return [self._entry(child_path, child, ask=False) for child_path, child in items]
        unknown = [child_path for child_path, child in items if isinstance(child, _File) and child.size is None][:self.head_limit]
        if unknown:
            from concurrent.futures import ThreadPoolExecutor

            with ThreadPoolExecutor(max_workers=min(self.head_threads, len(unknown))) as pool:
                asked = dict(zip(unknown, pool.map(lambda p: self._entry(p, self._node(p), ask=True), unknown)))
        else:
            asked = {}
        return [asked.get(child_path) or self._entry(child_path, child, ask=False) for child_path, child in items]

    def stat(self, path: str) -> Entry:
        return self._entry(path, self._node(path), ask=True)

    def _file(self, path: str) -> _File:
        node = self._node(path)
        if not isinstance(node, _File):
            raise NotFound()
        return node

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        file = self._file(path)
        headers = {"Accept-Encoding": "identity"}
        span = range_header(offset, length, file.size)
        if span:
            headers["Range"] = span
        try:
            response = self._http.send(self._http.build_request("GET", self._address(file), headers=headers,
                                                                extensions=http.configured(file.url)),
                                       stream=True, follow_redirects=True)
        except httpx.InvalidURL:
            raise _invalid_address() from None
        except httpx.HTTPError as error:
            raise transport_error(error, self.service) from None
        if response.status_code not in (200, 206):
            http.drain(response)
            raise self._failure(response)
        return response_stream(response, offset=offset, length=length, total=file.size, service=self.service)

    def download_url(self, path: str, *, filename: str, expires: int = 3600,
                     user_agent: str | None = None) -> str | None:
        file = self._file(path)
        if _userinfo(file.url):
            return None  # the user name and password would go to the browser: the site sends the file
        return self._address(file, expires)
