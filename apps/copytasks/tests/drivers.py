"""Local-storage variants for the copy task tests, registered as types of their own (TYPES): a target that hashes a
file before storing it (as Baidu Netdisk, 115 and 123 do), a source that can't start halfway, a source that fails a
few times, a source that lists folders' times as NTFS does, targets that refuse, a source that doesn't know sizes, and
a source that waits for the test."""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import timedelta

from apps.storages.drivers.base import AuthError, BackendError, ReadStream
from apps.storages.drivers.local import LocalDriver


class HashFirstDriver(LocalDriver):
    """Reads all of a file first, for its MD5, goes back to where it began and only then stores it."""

    type = "test-copy-hashfirst"
    label = "测试（先算哈希）"
    digests: list[str] = []

    def write(self, path, data, size):
        start = data.tell() if data.seekable() else None
        digest = hashlib.md5()
        left = size
        while left > 0:
            chunk = data.read(min(left, 7000))
            if not chunk:
                break
            digest.update(chunk)
            left -= len(chunk)
        HashFirstDriver.digests.append(digest.hexdigest())
        if start is None:
            raise AssertionError("HashFirstDriver needs data that can seek")
        data.seek(start)
        super().write(path, data, size)


class NoRangeDriver(LocalDriver):
    """A source that can't start halfway (FTP without REST): open() only from the start."""

    type = "test-copy-norange"
    label = "测试（不能从中间读）"
    can_range = False
    opens: list[str] = []

    def open(self, path, offset=0, length=None):
        if offset or length is not None:
            raise AssertionError("open() with a range on a driver without can_range")
        NoRangeDriver.opens.append(path)
        return super().open(path)


class FlakyDriver(LocalDriver):
    """A source whose files fail to open `failures` times each (a storage that is down for a moment)."""

    type = "test-copy-flaky"
    label = "测试（时好时坏）"
    failures = 2
    tries: dict[str, int] = {}

    def open(self, path, offset=0, length=None):
        tried = FlakyDriver.tries.get(path, 0) + 1
        FlakyDriver.tries[path] = tried
        if tried <= FlakyDriver.failures:
            raise BackendError("连接断了")
        return super().open(path, offset, length)


class FlakyListDriver(LocalDriver):
    """A source whose folders fail to list `failures` times each."""

    type = "test-copy-flakylist"
    label = "测试（列文件夹时好时坏）"
    failures = 1
    tries: dict[str, int] = {}

    def list(self, path):
        tried = FlakyListDriver.tries.get(path, 0) + 1
        FlakyListDriver.tries[path] = tried
        if tried <= FlakyListDriver.failures:
            raise BackendError("列表超时了")
        return super().list(path)


class StaleTimesDriver(LocalDriver):
    """A source that lists folders as NTFS does: a folder shows in its parent's listing with an older time (the one
    before something changed in it) until it has been listed itself. Through a link it is the same folder."""

    type = "test-copy-stale"
    label = "测试（像 NTFS 那样列出旧的时间）"
    listed: set[str] = set()
    lists: list[str] = []

    def _real(self, path):
        return os.path.realpath(os.path.join(self._root, *[part for part in path.split("/") if part]))

    def list(self, path):
        import dataclasses

        StaleTimesDriver.lists.append(path)
        StaleTimesDriver.listed.add(self._real(path))
        return [dataclasses.replace(entry, modified=entry.modified - timedelta(seconds=1))
                if entry.is_dir and self._real(entry.path) not in StaleTimesDriver.listed else entry
                for entry in super().list(path)]


class BrokenStreamDriver(LocalDriver):
    """A source whose streams break after the first chunk, every time."""

    type = "test-copy-broken"
    label = "测试（读到一半断开）"

    def open(self, path, offset=0, length=None):
        stream = super().open(path, offset, length)

        def chunks():
            for chunk in stream:
                yield chunk[:1]
                raise BackendError("读到一半断开了")

        return ReadStream(chunks(), size=stream.size, total=stream.total, offset=stream.offset, close=stream.close)


class ExpiredDriver(LocalDriver):
    """A target whose authorization has expired: every write is refused."""

    type = "test-copy-expired"
    label = "测试（授权过期）"
    writes = 0

    def write(self, path, data, size):
        ExpiredDriver.writes += 1
        raise AuthError()


class StrictNamesDriver(LocalDriver):
    """A target that refuses # in names (OneDrive refuses ":" and others)."""

    type = "test-copy-strict"
    label = "测试（名字里不能有 #）"
    forbidden_chars = "#"


class ReadOnlyDriver(LocalDriver):
    type = "test-copy-readonly"
    label = "测试（只读）"
    can_write = False


class NoSizeDriver(LocalDriver):
    """A source that knows no file's size, neither in its listings nor in its streams (Google Docs)."""

    type = "test-copy-nosize"
    label = "测试（不知道大小）"

    def list(self, path):
        import dataclasses

        return [dataclasses.replace(entry, size=None) if not entry.is_dir else entry for entry in super().list(path)]

    def open(self, path, offset=0, length=None):
        stream = super().open(path, offset, length)
        return ReadStream(stream, size=None, total=None, close=stream.close)


class WaitingDriver(LocalDriver):
    """A source whose streams of `paths` wait halfway, after the first byte, until the test lets them go on:
    `reached` is set once one does."""

    type = "test-copy-waiting"
    label = "测试（读到一半等着）"
    paths: set[str] = set()
    reached = threading.Event()
    go_on = threading.Event()

    def open(self, path, offset=0, length=None):
        stream = super().open(path, offset, length)
        if path not in WaitingDriver.paths:
            return stream

        def chunks():
            for chunk in stream:
                yield chunk[:1]
                WaitingDriver.reached.set()
                WaitingDriver.go_on.wait(10)
                yield chunk[1:]

        return ReadStream(chunks(), size=stream.size, total=stream.total, offset=stream.offset, close=stream.close)


TYPES = {
    "test-copy-hashfirst": "apps.copytasks.tests.drivers:HashFirstDriver",
    "test-copy-norange": "apps.copytasks.tests.drivers:NoRangeDriver",
    "test-copy-flaky": "apps.copytasks.tests.drivers:FlakyDriver",
    "test-copy-flakylist": "apps.copytasks.tests.drivers:FlakyListDriver",
    "test-copy-stale": "apps.copytasks.tests.drivers:StaleTimesDriver",
    "test-copy-broken": "apps.copytasks.tests.drivers:BrokenStreamDriver",
    "test-copy-expired": "apps.copytasks.tests.drivers:ExpiredDriver",
    "test-copy-strict": "apps.copytasks.tests.drivers:StrictNamesDriver",
    "test-copy-readonly": "apps.copytasks.tests.drivers:ReadOnlyDriver",
    "test-copy-nosize": "apps.copytasks.tests.drivers:NoSizeDriver",
    "test-copy-waiting": "apps.copytasks.tests.drivers:WaitingDriver",
}


def reset():
    HashFirstDriver.digests = []
    NoRangeDriver.opens = []
    FlakyDriver.tries = {}
    FlakyDriver.failures = 2
    FlakyListDriver.tries = {}
    FlakyListDriver.failures = 1
    StaleTimesDriver.listed = set()
    StaleTimesDriver.lists = []
    ExpiredDriver.writes = 0
    WaitingDriver.paths = set()
    WaitingDriver.reached = threading.Event()
    WaitingDriver.go_on = threading.Event()
