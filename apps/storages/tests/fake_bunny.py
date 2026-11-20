"""An in-memory Bunny Storage for driver tests: the HTTP API of one storage zone (fake.url) and a pull zone in front of
it (fake.cdn_url).

Written from Bunny's documentation and its TypeScript SDK (their URLs are in apps/storages/drivers/bunny.py), without
any of the driver's code: it checks what Bunny checks, and signs nothing itself.
- The API: AccessKey must be the zone's password and the path's first part the zone's name, else 401. GET <folder>/
  lists a folder as a JSON array of objects (Guid, Path, ObjectName, Length, LastChanged without an offset,
  IsDirectory, DateCreated, Checksum, ContentType, ...); GET <file> answers the file, a Range with 206; DESCRIBE <path>
  answers one object; PUT <file> stores the body and makes the folders above it, refusing (400) a Checksum that isn't
  the body's SHA-256 in uppercase hex, a name that starts or ends with a space and a path of more than 6,000
  characters; PUT <folder>/ without a body makes the folder and those above; DELETE removes a file, or a folder with
  everything in it, but not the zone's root without allowRootDelete. Errors are [{"HttpCode": ..., "Message": ...}],
  the reference's array; their messages are guesses (Bunny documents none but "Unable to process request").
- The pull zone: GET <path> answers the zone's files; with token_key set, only with a valid Advanced token (HMAC-SHA256
  over the path, not percent-encoded, the expiry and the other query parameters sorted) that hasn't expired, else 403.
Stricter than Bunny on purpose: a chunked request body is refused (Bunny's SDK sends them; the driver doesn't), and a
request whose body ends before its Content-Length (the client went away) is dropped unanswered, storing nothing.
Folders stay when emptied, which the documentation doesn't say either way.

What Bunny's documentation leaves open is a knob, so that the tests try the driver with each answer: ignore_case
(names found whatever their case, kept as first written), describe_folders (DESCRIBE answers folders), describe_status
(DESCRIBE refused with that status), missing_lists_empty (a missing folder lists as [] instead of a 404), error_object
(errors as one object, not an array). Also token_key, clock_skew (seconds added to the fake's clock), fail(status,
message, method=, times=, headers=), and `requests` (what the API was asked).
"""

from __future__ import annotations

import base64
import dataclasses
import datetime
import hashlib
import hmac
import threading
import time
import types
import uuid
from urllib.parse import parse_qsl, unquote, urlsplit

from apps.storages.tests.fake_cloud_http import HTTPError, byte_range
from apps.storages.tests.fake_http import Handler, Server

ZONE = "hfile-test-zone"
PASSWORD = "fake-zone-password-0123"
MAX_PATH = 6000


def stamp(seconds: float) -> str:
    """A time as Bunny writes LastChanged: UTC, milliseconds, no offset ("2017-03-10T03:06:48.203")."""
    return datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]


@dataclasses.dataclass
class Item:
    path: str  # as first written, e.g. "/a/b.txt"; "/" is the zone's root
    is_dir: bool
    data: bytes = b""
    content_type: str = "application/octet-stream"
    created: float = 0.0
    changed: float = 0.0
    guid: str = dataclasses.field(default_factory=lambda: str(uuid.uuid4()))


class FakeBunny:
    def __init__(self, zone: str = ZONE, password: str = PASSWORD):
        self.zone, self.password = zone, password
        self.lock = threading.RLock()
        self.reset()
        self.api = Server(ApiHandler, self)
        self.cdn = Server(CdnHandler, self)

    @property
    def url(self) -> str:
        return self.api.url

    @property
    def cdn_url(self) -> str:
        return self.cdn.url

    def stop(self) -> None:
        self.api.stop()
        self.cdn.stop()

    def reset(self) -> None:
        with self.lock:
            self.items: dict[str, Item] = {"/": Item("/", True)}  # by key(path)
            self.requests: list[types.SimpleNamespace] = []
            self.failures: list[list] = []
            self.token_key = ""
            self.clock_skew = 0.0
            self.ignore_case = False
            self.describe_folders = True
            self.describe_status: int | None = None
            self.missing_lists_empty = False
            self.error_object = False

    # -- the store

    def now(self) -> float:
        return time.time() + self.clock_skew

    def key(self, path: str) -> str:
        return path.casefold() if self.ignore_case else path

    def get(self, path: str) -> Item | None:
        return self.items.get(self.key(path))

    def children(self, folder: Item) -> list[Item]:
        prefix = self.key(folder.path).rstrip("/") + "/"
        return sorted((item for key, item in self.items.items()
                       if key != "/" and key.startswith(prefix) and "/" not in key[len(prefix):]), key=lambda item: item.path)

    def make_folders(self, path: str, *, itself: bool) -> str | None:
        """The folders above `path`, and `path` itself with `itself`, made when missing; a message when a file is in
        the way."""
        names = [name for name in path.split("/") if name]
        for count in range(1, len(names) + (1 if itself else 0)):
            folder = "/" + "/".join(names[:count])
            item = self.get(folder)
            if item is None:
                self.items[self.key(folder)] = Item(folder, True, created=self.now(), changed=self.now())
            elif not item.is_dir:
                return f"{item.path} is a file."
        return None

    def add(self, path: str, data: bytes = b"") -> None:
        """A file as another program would have put it, without a request."""
        with self.lock:
            self.make_folders(path, itself=False)
            self.items[self.key(path)] = Item(path, False, data, created=self.now(), changed=self.now())

    def files(self) -> dict[str, bytes]:
        with self.lock:
            return {item.path: item.data for item in self.items.values() if not item.is_dir}

    def folders(self) -> set[str]:
        with self.lock:
            return {item.path for item in self.items.values() if item.is_dir}

    def describe(self, item: Item) -> dict:
        parent, _, name = item.path.rpartition("/")
        return {"Guid": item.guid, "StorageZoneName": self.zone, "Path": f"/{self.zone}{parent}/", "ObjectName": name,
                "Length": 0 if item.is_dir else len(item.data), "LastChanged": stamp(item.changed),
                "IsDirectory": item.is_dir, "ServerId": 7, "UserId": "00000000-0000-0000-0000-000000000000",
                "DateCreated": stamp(item.created), "StorageZoneId": 1,
                "Checksum": None if item.is_dir else hashlib.sha256(item.data).hexdigest().upper(),
                "ReplicatedZones": None, "ContentType": "" if item.is_dir else item.content_type}

    # -- failures and tokens

    def fail(self, status: int, message: str = "", *, method: str | None = None, times: int = 1,
             headers: dict[str, str] | None = None) -> None:
        """The next `times` API requests (of `method`) get this error."""
        with self.lock:
            self.failures.append([method, status, message, times, headers or {}])

    def take_failure(self, method: str) -> tuple[int, str, dict[str, str]] | None:
        for failure in self.failures:
            if failure[3] > 0 and failure[0] in (None, method):
                failure[3] -= 1
                return failure[1], failure[2], failure[4]
        return None

    def token_valid(self, path: str, query: dict[str, str]) -> bool:
        """The docs' Advanced token: "HS256-" + Base64URL(HMAC-SHA256(key, path + expires + parameters)), without an IP."""
        token, expires = query.get("token", ""), query.get("expires", "")
        if not (expires.isascii() and expires.isdigit()) or int(expires) <= self.now():
            return False
        signing = "&".join(f"{name}={value}" for name, value in sorted(query.items()) if name not in ("token", "expires"))
        digest = hmac.new(self.token_key.encode(), f"{path}{expires}{signing}".encode(), hashlib.sha256).digest()
        return hmac.compare_digest(token, "HS256-" + base64.urlsafe_b64encode(digest).decode().rstrip("="))


class _Files(Handler):
    def send_file(self, item: Item) -> None:
        data = item.data
        try:
            span = byte_range(self.headers.get("Range"), len(data))
        except HTTPError:
            return self.answer(416, b"", {"Content-Range": f"bytes */{len(data)}"})
        headers = {"Content-Type": item.content_type, "Accept-Ranges": "bytes"}
        if span is None:
            return self.answer(200, data, headers)
        start, end = span
        return self.answer(206, data[start:end + 1], {**headers, "Content-Range": f"bytes {start}-{end}/{len(data)}"})


class ApiHandler(_Files):
    def do_GET(self):
        self.dispatch()

    do_PUT = do_DELETE = do_DESCRIBE = do_GET

    def error(self, status: int, message: str, headers: dict[str, str] | None = None) -> None:
        body = {"HttpCode": status, "Message": message}
        self.answer_json(status, body if self.fake.error_object else [body], headers)

    def dispatch(self):
        fake = self.fake
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            return self.error(411, "Length Required")
        body = self.body()
        if len(body) != int(self.headers.get("Content-Length") or 0):
            self.close_connection = True  # the client went away halfway through the body: nothing is stored
            return
        raw_path, _, raw_query = self.path.partition("?")
        with fake.lock:
            fake.requests.append(types.SimpleNamespace(
                method=self.command, path=raw_path, query=raw_query, size=len(body),
                headers={name.lower(): value for name, value in self.headers.items()}))
            failure = fake.take_failure(self.command)
        if failure is not None:
            return self.error(*failure)
        zone, slash, rest = raw_path[1:].partition("/")
        if self.headers.get("AccessKey") != fake.password or zone != fake.zone or not slash:
            return self.error(401, "Unauthorized")
        try:
            decoded = unquote(rest, errors="strict")
        except UnicodeDecodeError:
            return self.error(400, "The path isn't UTF-8.")
        if decoded.startswith("/") or "//" in decoded or "\x00" in decoded:
            return self.error(400, "Invalid path.")
        query = dict(parse_qsl(raw_query, keep_blank_values=True))
        with fake.lock:
            return getattr(self, "op_" + self.command)("/" + decoded.rstrip("/"), raw_path.endswith("/"), body, query)

    def op_GET(self, path, folder, body, query):
        fake = self.fake
        item = fake.get(path)
        if folder:
            if item is None or not item.is_dir:
                return self.answer_json(200, []) if fake.missing_lists_empty else self.error(404, "Object Not Found")
            return self.answer_json(200, [fake.describe(child) for child in fake.children(item)])
        if item is None or item.is_dir:
            return self.error(404, "Object Not Found")
        return self.send_file(item)

    def op_DESCRIBE(self, path, folder, body, query):
        fake = self.fake
        if fake.describe_status:
            return self.error(fake.describe_status, "Method Not Allowed")
        item = fake.get(path)
        if item is None or path == "/" or (item.is_dir and not fake.describe_folders):
            return self.error(404, "Object Not Found")
        return self.answer_json(200, fake.describe(item))

    def op_PUT(self, path, folder, body, query):
        fake = self.fake
        names = [name for name in path.split("/") if name]
        if not names:
            return self.error(400, "Invalid path.")
        if any(name != name.strip(" ") for name in names):
            return self.error(400, "File names cannot start or end with a space.")
        if len(path) > MAX_PATH:
            return self.error(400, "The path is too long.")
        if folder:
            if body:
                return self.error(400, "A folder has no content.")
            problem = fake.make_folders(path, itself=True)
            return self.error(400, problem) if problem else self.answer_json(201, {"HttpCode": 201, "Message": "Directory created."})
        checksum = self.headers.get("Checksum")
        if checksum is not None and checksum != hashlib.sha256(body).hexdigest().upper():
            return self.error(400, "The checksum doesn't match.")
        item = fake.get(path)
        if item is not None and item.is_dir:
            return self.error(400, "A folder has this name.")
        problem = fake.make_folders(path, itself=False)
        if problem:
            return self.error(400, problem)
        content_type = self.headers.get("Content-Type") or "application/octet-stream"
        if item is None:
            fake.items[fake.key(path)] = Item(path, False, body, content_type, fake.now(), fake.now())
        else:  # replaced, keeping the name as first written
            item.data, item.content_type, item.changed = body, content_type, fake.now()
        return self.answer_json(201, {"HttpCode": 201, "Message": "File uploaded."})

    def op_DELETE(self, path, folder, body, query):
        fake = self.fake
        if path == "/" and "true" not in (query.get("allowRootDelete"), self.headers.get("allowRootDelete")):
            return self.error(400, "Deleting the root directory isn't allowed.")
        item = fake.get(path)
        if item is None:
            return self.error(404, "Object Not Found")
        key = fake.key(path)
        if item.is_dir:
            prefix = key.rstrip("/") + "/"
            for other in [other for other in fake.items if other.startswith(prefix)]:
                del fake.items[other]
        if key != "/":
            del fake.items[key]
        return self.answer_json(200, {"HttpCode": 200, "Message": "File deleted successfuly."})


class CdnHandler(_Files):
    """The pull zone: the zone's files at their paths, behind token authentication when token_key is set."""

    def do_GET(self):
        fake = self.fake
        parts = urlsplit(self.path)
        try:
            path = unquote(parts.path, errors="strict")
        except UnicodeDecodeError:
            return self.answer(400, b"400 Bad Request")
        query = dict(parse_qsl(parts.query, keep_blank_values=True))
        with fake.lock:
            if fake.token_key and not fake.token_valid(path, query):
                return self.answer(403, b"403 Forbidden")
            item = fake.get(path)
            if item is None or item.is_dir:
                return self.answer(404, b"404 Not Found")
            return self.send_file(item)
