"""123 Cloud Drive's direct links (123云盘直链) with their URL authentication, for the tests of
apps.storages.drivers.pan123_link (tests only, no Django).

    fake = FakePan123Link(files, uid=1800000000, key="289ds32418bxdba").start()   # files: {"/a/b.txt": b"..."}
    fake.link("/a/b.txt")   # http://127.0.0.1:<port>/1800000000/a/b.txt, percent-encoded

The files of a direct-link space answer GET with Range, and HEAD, at /<uid>/<path>. With a `key` (直链鉴权 on), a
request needs auth_key=<timestamp>-<rand>-<uid>-<md5> as https://www.123pan.com/faq/?problem=dev_guide has it: the
timestamp is when it expires, ten digits, not past; rand has no "-"; uid is the account's; md5 is the lowercase hex
MD5 of "<path, percent-decoded>-<timestamp>-<rand>-<uid>-<key>". Anything else is refused with 403. `targets` keeps
what each request asked for, `heads` counts the HEAD requests.
"""

from __future__ import annotations

import hashlib
import threading
import time
import urllib.parse

from .fake_cloud_http import FakeServer, HTTPError, file_response


class FakePan123Link:
    def __init__(self, files: dict[str, bytes], *, uid: int = 1800000000, key: str | None = None):
        self.files = dict(files)
        self.uid = uid
        self.key = key
        self.lock = threading.Lock()
        self.targets: list[str] = []
        self.heads = 0
        self.server = FakeServer(self.handle)

    def start(self) -> FakePan123Link:
        self.server.start()
        return self

    def close(self) -> None:
        self.server.close()

    def link(self, path: str) -> str:
        """The direct link of the file at `path` of the space, as 123 gives it."""
        return f"{self.server.url}/{self.uid}{urllib.parse.quote(path)}"

    def handle(self, req):
        with self.lock:
            self.targets.append(req.target)
            if req.method == "HEAD":
                self.heads += 1
        if req.method not in ("GET", "HEAD"):
            raise HTTPError(405, b"Method Not Allowed")
        path = urllib.parse.unquote(req.path)
        if self.key is not None:
            self._check(path, req.query.get("auth_key", []))
        own, _, inside = path.partition(f"/{self.uid}/")
        data = self.files.get("/" + inside) if own == "" and inside else None
        if data is None:
            raise HTTPError(404, b"Not Found")
        return file_response(data, req.header("Range"), {"Last-Modified": "Tue, 17 Nov 2026 12:00:00 GMT"})

    def _check(self, path: str, values: list[str]) -> None:
        parts = values[0].split("-") if len(values) == 1 else []
        if len(parts) != 4:
            raise HTTPError(403, b"Forbidden")
        timestamp, rand, uid, digest = parts
        if not (len(timestamp) == 10 and timestamp.isascii() and timestamp.isdigit()) or int(timestamp) < time.time():
            raise HTTPError(403, b"Forbidden")
        if not rand or uid != str(self.uid):
            raise HTTPError(403, b"Forbidden")
        if digest != hashlib.md5(f"{path}-{timestamp}-{rand}-{uid}-{self.key}".encode()).hexdigest():
            raise HTTPError(403, b"Forbidden")
