"""A file of the source storage as the file object the target's write() reads (SourceReader), and the speed limit
of a task's threads together (Meter).

Nothing goes to the server's disk, as with rclone: the target reads the bytes as the source's stream brings them,
a piece at a time. Some targets read a file twice, or go back for a part they send again: Baidu Netdisk, 115, 123
and CF ImgBed hash all of it before uploading (they would put a file that can't seek into a temporary file first),
Alipan reads a few bytes in the middle for its proof, and the targets that send parts again after an error seek
back to them. So the reader can seek: seek() only moves the position, and the next read goes on in the stream it
has when that is the same place or a little further, or opens the source again there (open() with an offset; a
source that can't start halfway, can_range False, is read from the start and the bytes before dropped).
"""

from __future__ import annotations

import io
import threading
from collections.abc import Callable

from django.utils.translation import gettext as _

from apps.files.limits import Speed
from apps.storages.drivers.base import DriverError

PIECE = 64 * 1024  # bytes at most between two looks at the speed limit
SKIP_AHEAD = 1024 * 1024  # a seek this far ahead reads on in the stream there is, a longer one opens a new one
_EMPTY = memoryview(b"")


class Stopped(Exception):
    """The task is stopping: a read halfway gives up."""


class Meter:
    """What a run's threads read from the source: counted, and all together at most `rate` bytes a second (0: no
    limit). Each piece waits for its turn after the pieces before it, of any thread (apps.files.limits.Speed), so
    more threads don't make it faster. Waits end at once when `stop` is set: then Stopped."""

    def __init__(self, rate: int, stop: threading.Event):
        self.rate = rate
        self.step = min(PIECE, max(4096, rate // 8)) if rate else None  # several pieces a second at a low rate
        self._stop = stop
        self._speed = Speed()
        self._lock = threading.Lock()
        self.total = 0

    def take(self, size: int) -> None:
        if self._stop.is_set():
            raise Stopped
        if self.rate and size:
            delay = self._speed.wait("task", size, self.rate)
            if delay and self._stop.wait(delay):
                raise Stopped
        with self._lock:
            self.total += size


class SourceReader:
    """`size` bytes of the file at `path` of the source `driver`, read as a file (read, seek, tell).

    read(n) gives n bytes, fewer only at the end, and none past `size`: a file that grew since is cut there, and
    a stream that ends before it (the file shrank, or the source broke off without saying so) is an error, the
    source's. `on_read(n)` hears of every n bytes read from the source, `on_position(at)` of where the reader is.
    The source's errors pass on as they are; the last one is kept in `source_error`, so that the caller knows whose
    an error from the target's write() was.
    """

    def __init__(self, driver, path: str, size: int, meter: Meter, *, stream=None,
                 on_read: Callable[[int], None] | None = None, on_position: Callable[[int], None] | None = None):
        self._driver = driver
        self._path = path
        self.size = size
        self._meter = meter
        self._can_range = bool(getattr(driver, "can_range", True))
        self._on_read = on_read
        self._on_position = on_position
        self._stream = None
        self._chunks = None
        self._pos = 0  # the next byte read() gives
        self._buffer = _EMPTY  # bytes from _pos on, metered
        self._pending = _EMPTY  # the stream's bytes after the buffer, not metered yet
        self._skip = 0  # bytes the stream drops before it is at _pos (with the buffer and pending empty)
        self.source_error: BaseException | None = None
        self.closed = False
        if stream is not None:  # opened at 0 by the caller, to learn the size
            self._use(stream)

    # -- the file object

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def tell(self) -> int:
        self._check_open()
        return self._pos

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        self._check_open()
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._pos + offset
        elif whence == io.SEEK_END:
            position = self.size + offset
        else:
            raise ValueError(f"invalid whence ({whence})")
        if position < 0:
            raise ValueError(f"negative seek position {position}")
        ahead = position - self._pos
        if ahead:
            held = len(self._buffer) + len(self._pending)
            if 0 < ahead <= held:
                self._drop(ahead)
            elif ahead > 0 and self._chunks is not None and (ahead - held <= SKIP_AHEAD or not self._can_range):
                self._skip += ahead - held
                self._buffer = self._pending = _EMPTY
            else:  # back, or far ahead: the next read opens the source there
                self._close_stream()
            self._pos = position
            self._moved()
        return position

    def read(self, size: int | None = -1) -> bytes:
        self._check_open()
        end = self.size if size is None or size < 0 else min(self.size, self._pos + size)
        parts = []
        while self._pos < end:
            if not self._buffer and not self._fill():
                error = DriverError(_("The source sent less than the file's size: the file may have changed while it "
                                      "was being copied."))
                self.source_error = error
                raise error
            piece = self._buffer[: end - self._pos]
            self._buffer = self._buffer[len(piece):]
            parts.append(piece)
            self._pos += len(piece)
            self._moved()  # a piece at a time: a target that reads a MiB at once, at a low speed limit, moves on too
        return b"".join(parts)

    def readinto(self, target) -> int:
        data = self.read(len(target))
        target[: len(data)] = data
        return len(data)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._close_stream()

    def __enter__(self) -> SourceReader:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the source

    def _check_open(self) -> None:
        if self.closed:
            raise ValueError("I/O operation on closed file.")

    def _moved(self) -> None:
        if self._on_position is not None:
            self._on_position(self._pos)

    def _drop(self, count: int) -> None:
        """Forget the next `count` bytes held (at most all of them)."""
        from_buffer = min(count, len(self._buffer))
        self._buffer = self._buffer[from_buffer:]
        self._pending = self._pending[count - from_buffer:]

    def _fill(self) -> bool:
        """The next piece of the file into the buffer, metered; False at the end of the source's stream."""
        if not self._pending and not self._pull():
            return False
        step = self._meter.step
        if step is None or len(self._pending) <= step:
            piece, self._pending = self._pending, _EMPTY
        else:
            piece, self._pending = self._pending[:step], self._pending[step:]
        self._meter.take(len(piece))
        self._buffer = piece
        return True

    def _pull(self) -> bool:
        """The next chunk of the stream into pending, the bytes to drop dropped (and metered: they came all the
        same); False at its end."""
        while True:
            if self._chunks is None:
                self._open()
            try:
                chunk = next(self._chunks)
            except StopIteration:
                return False
            except BaseException as error:
                self.source_error = error
                raise
            if not chunk:
                continue
            if self._skip:
                dropped = min(self._skip, len(chunk))
                self._skip -= dropped
                self._meter.take(dropped)
                chunk = chunk[dropped:]
                if not chunk:
                    continue
            self._pending = memoryview(chunk)
            return True

    def _open(self) -> None:
        """A stream from _pos: from there where the source can start halfway, else from the start, skipping to it."""
        try:
            if self._pos and self._can_range:
                stream = self._driver.open(self._path, self._pos)
                self._skip = 0
            else:
                stream = self._driver.open(self._path)
                self._skip = self._pos
        except BaseException as error:
            self.source_error = error
            raise
        self._use(stream)

    def _use(self, stream) -> None:
        self._stream = stream
        chunks = iter(stream)
        on_read = self._on_read
        if on_read is None:
            self._chunks = chunks
            return

        def counted():
            for chunk in chunks:
                on_read(len(chunk))
                yield chunk

        self._chunks = counted()

    def _close_stream(self) -> None:
        stream, self._stream, self._chunks = self._stream, None, None
        self._buffer = self._pending = _EMPTY
        self._skip = 0
        if stream is not None:
            try:
                stream.close()
            except Exception:  # noqa: BLE001 - closing is best effort; the bytes are not needed any more
                pass
