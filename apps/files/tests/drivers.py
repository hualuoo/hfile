"""Local-storage variants for the files tests, registered as types of their own (helpers.FilesTestCase):
no copy of its own (as FTP and SFTP), links of its own (as a bucket), one that counts its streams, and one that
finds names whatever their case."""

from __future__ import annotations

import dataclasses
import threading
import urllib.parse

from apps.storages.drivers import paths
from apps.storages.drivers.base import NotFound, ReadStream
from apps.storages.drivers.local import LocalDriver


class NoCopyDriver(LocalDriver):
    """A backend without copy (FTP, SFTP): the site copies files itself."""

    type = "test-nocopy"
    label = "测试（不能复制）"
    can_copy = False

    def copy(self, path, folder):  # never called: the site checks can_copy
        raise AssertionError("copy() called on a driver without copy")


class DirectDriver(LocalDriver):
    """A backend that gives its own download links (a bucket's presigned URLs); `scheme` "http" makes them http://
    links (a server on the LAN, 123's direct links)."""

    type = "test-direct"
    label = "测试（直接下载）"
    can_direct_download = True
    scheme = "https"
    calls: list[tuple[str, str | None, str | None]] = []
    lock = threading.Lock()

    def download_url(self, path, *, filename, expires=3600, user_agent=None):
        with DirectDriver.lock:
            DirectDriver.calls.append((path, filename, user_agent))
        return f"{self.scheme}://cdn.example.test{urllib.parse.quote(path)}?expires={expires}&sig=abc"


class CountingDriver(LocalDriver):
    """Remembers the streams it opened, to see that they are closed (a client that goes away)."""

    type = "test-counting"
    label = "测试（数流）"
    streams: list[ReadStream] = []

    def open(self, path, offset=0, length=None):
        stream = super().open(path, offset, length)
        CountingDriver.streams.append(stream)
        return stream


class NoCaseDriver(LocalDriver):
    """A backend that ignores case, and names what it finds as it was asked for (FTP's MLST, SFTP and WebDAV on
    Windows): a stat of "/A.txt" or "/DOCS/a.txt" finds "/a.txt" or "/docs/a.txt", and calls it "A.txt" or "a.txt"."""

    type = "test-nocase"
    label = "测试（不分大小写）"

    def stat(self, path):
        try:
            return super().stat(path)
        except NotFound:
            real = paths.ROOT
            for name in paths.segments(path):
                real = next((entry.path for entry in super().list(real) if entry.name.casefold() == name.casefold()),
                            None)
                if real is None:
                    raise
            return dataclasses.replace(super().stat(real), name=paths.name(path), path=path)


TYPES = {
    "test-nocopy": "apps.files.tests.drivers:NoCopyDriver",
    "test-direct": "apps.files.tests.drivers:DirectDriver",
    "test-counting": "apps.files.tests.drivers:CountingDriver",
    "test-nocase": "apps.files.tests.drivers:NoCaseDriver",
}
