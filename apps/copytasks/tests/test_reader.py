"""SourceReader and Meter (apps/copytasks/reader.py), on a source in memory: what targets do with the file they are
given (read it in pieces, go back, skip ahead) gives the file's bytes, with as few streams opened as can be."""

import io
import threading
from unittest import mock

from django.test import SimpleTestCase

from apps.storages.drivers.base import BackendError, DriverError, ReadStream

from ..reader import PIECE, SKIP_AHEAD, Meter, SourceReader, Stopped

DATA = bytes(range(256)) * 40  # 10 KiB


class MemoryDriver:
    """A source of one file, sent in chunks of `chunk` bytes; remembers where each stream began."""

    def __init__(self, data=DATA, chunk=1000, can_range=True):
        self.data = data
        self.chunk = chunk
        self.can_range = can_range
        self.opens = []
        self.closed = 0

    def open(self, path, offset=0, length=None):
        if offset and not self.can_range:
            raise AssertionError("open() with an offset on a source that can't")
        if not 0 <= offset <= len(self.data):
            raise AssertionError(f"offset {offset} out of the file")
        self.opens.append(offset)
        body = self.data[offset:]
        chunks = [body[at:at + self.chunk] for at in range(0, len(body), self.chunk)]
        return ReadStream(chunks, size=len(body), total=len(self.data), offset=offset, close=self._closed)

    def _closed(self):
        self.closed += 1


def reader(driver, size=None, rate=0, **kwargs):
    meter = Meter(rate, threading.Event())
    return SourceReader(driver, "/f", len(driver.data) if size is None else size, meter, **kwargs), meter


class SourceReaderTests(SimpleTestCase):
    def test_reads_the_file_in_any_pieces(self):
        for piece in (1, 7, 1000, 4096, 100_000):
            with self.subTest(piece=piece):
                driver = MemoryDriver()
                source, meter = reader(driver)
                parts = []
                while chunk := source.read(piece):
                    self.assertLessEqual(len(chunk), piece)
                    parts.append(chunk)
                self.assertEqual(b"".join(parts), DATA)
                self.assertEqual(driver.opens, [0])
                self.assertEqual(meter.total, len(DATA))
                self.assertEqual(source.tell(), len(DATA))

    def test_a_read_gives_all_it_asks_for(self):
        """read(n) gives n bytes until the end, not just what one chunk of the stream had."""
        source, _meter = reader(MemoryDriver(chunk=3))
        self.assertEqual(source.read(1000), DATA[:1000])
        self.assertEqual(source.read(), DATA[1000:])
        self.assertEqual(source.read(10), b"")

    def test_nothing_past_the_size(self):
        """A file that grew since it was listed is cut at the size the target was told."""
        source, _meter = reader(MemoryDriver(), size=100)
        self.assertEqual(source.read(), DATA[:100])
        self.assertEqual(source.read(1), b"")

    def test_a_file_that_shrank(self):
        """A stream that ends before the size is the source's error, not a short read the target would refuse as its
        own."""
        source, _meter = reader(MemoryDriver(data=DATA[:50]), size=100)
        self.assertEqual(source.read(50), DATA[:50])
        with self.assertRaisesMessage(DriverError, "源文件读到的比文件的大小少：文件可能在复制时改了。") as caught:
            source.read(1)
        self.assertIs(source.source_error, caught.exception)

    def test_going_back_opens_the_source_again_there(self):
        driver = MemoryDriver()
        source, _meter = reader(driver)
        self.assertEqual(source.read(5000), DATA[:5000])
        self.assertEqual(source.seek(3000), 3000)
        self.assertEqual(source.read(10), DATA[3000:3010])
        self.assertEqual(driver.opens, [0, 3000])
        self.assertEqual(driver.closed, 1)  # the first stream
        source.seek(0)
        self.assertEqual(source.read(), DATA)
        self.assertEqual(driver.opens, [0, 3000, 0])

    def test_a_target_that_hashes_first(self):
        """Read all of it, back to the start, all of it again (Baidu Netdisk, 115, 123): two streams."""
        driver = MemoryDriver()
        source, meter = reader(driver)
        self.assertEqual(source.read(), DATA)
        source.seek(0)
        parts = [source.read(4096) for _ in range(3)]
        self.assertEqual(b"".join(parts), DATA)
        self.assertEqual(driver.opens, [0, 0])
        self.assertEqual(meter.total, 2 * len(DATA))

    def test_a_little_ahead_reads_on(self):
        """Ahead within what the stream brought, or a little further: the same stream."""
        driver = MemoryDriver()
        source, _meter = reader(driver)
        self.assertEqual(source.read(10), DATA[:10])
        source.seek(500)  # in the chunk it has
        self.assertEqual(source.read(10), DATA[500:510])
        source.seek(4321)  # chunks further on
        self.assertEqual(source.read(10), DATA[4321:4331])
        source.seek(10, io.SEEK_CUR)
        self.assertEqual(source.read(5), DATA[4341:4346])
        self.assertEqual(driver.opens, [0])

    def test_far_ahead_opens_there(self):
        data = bytes(3 * SKIP_AHEAD)
        driver = MemoryDriver(data=data, chunk=64 * 1024)
        source, meter = reader(driver)
        source.read(10)
        source.seek(2 * SKIP_AHEAD + 7)
        self.assertEqual(source.read(3), data[2 * SKIP_AHEAD + 7:2 * SKIP_AHEAD + 10])
        self.assertEqual(driver.opens, [0, 2 * SKIP_AHEAD + 7])
        self.assertLess(meter.total, SKIP_AHEAD)  # the bytes in between weren't read

    def test_a_source_that_cant_start_halfway(self):
        """can_range False: from the start each time, the bytes before dropped (and counted: they came)."""
        driver = MemoryDriver(can_range=False)
        source, meter = reader(driver)
        self.assertEqual(source.read(5000), DATA[:5000])
        source.seek(4000)
        self.assertEqual(source.read(10), DATA[4000:4010])
        source.seek(9000)  # far ahead: on in the same stream
        self.assertEqual(source.read(), DATA[9000:])
        self.assertEqual(driver.opens, [0, 0])
        self.assertEqual(meter.total, 5000 + len(DATA))

    def test_seek_from_the_end_and_past_it(self):
        driver = MemoryDriver()
        source, _meter = reader(driver)
        self.assertEqual(source.seek(-10, io.SEEK_END), len(DATA) - 10)
        self.assertEqual(source.read(), DATA[-10:])
        self.assertEqual(source.seek(len(DATA) + 5), len(DATA) + 5)
        self.assertEqual(source.read(), b"")
        with self.assertRaises(ValueError):
            source.seek(-1)
        self.assertEqual(driver.opens, [len(DATA) - 10])  # never opened at or past the end

    def test_a_file_object_as_targets_expect(self):
        source, _meter = reader(MemoryDriver())
        self.assertTrue(source.readable())
        self.assertTrue(source.seekable())
        self.assertFalse(source.writable())
        with self.assertRaises(io.UnsupportedOperation):
            source.fileno()
        target = bytearray(6)
        self.assertEqual(source.readinto(target), 6)
        self.assertEqual(bytes(target), DATA[:6])
        source.close()
        with self.assertRaises(ValueError):
            source.read(1)

    def test_the_stream_it_was_given(self):
        """The caller opened the file to learn its size: the reader goes on with that stream."""
        driver = MemoryDriver()
        stream = driver.open("/f")
        source, _meter = reader(driver, stream=stream)
        self.assertEqual(source.read(), DATA)
        self.assertEqual(driver.opens, [0])

    def test_hears_of_reads_and_where_it_is(self):
        reads, places = [], []
        source, _meter = reader(MemoryDriver(), on_read=reads.append, on_position=places.append)
        source.read(1500)
        source.seek(100)
        source.read(50)
        self.assertEqual(reads, [1000, 1000, 1000])  # the chunks of the two streams
        self.assertEqual(places, [1000, 1500, 100, 150])  # a piece at a time

    def test_the_sources_error_is_kept(self):
        class Breaks(MemoryDriver):
            def open(self, path, offset=0, length=None):
                stream = super().open(path, offset, length)

                def chunks():
                    yield next(iter(stream))
                    raise BackendError("断了")

                return ReadStream(chunks(), size=stream.size, total=stream.total)

        source, _meter = reader(Breaks())
        with self.assertRaises(BackendError) as caught:
            source.read()
        self.assertIs(source.source_error, caught.exception)

        class WontOpen(MemoryDriver):
            def open(self, path, offset=0, length=None):
                raise BackendError("打不开")

        source, _meter = reader(WontOpen())
        with self.assertRaises(BackendError) as caught:
            source.read(1)
        self.assertIs(source.source_error, caught.exception)


class MeterTests(SimpleTestCase):
    def test_counts_and_stops(self):
        stop = threading.Event()
        meter = Meter(0, stop)
        self.assertIsNone(meter.step)
        meter.take(10)
        meter.take(5)
        self.assertEqual(meter.total, 15)
        stop.set()
        with self.assertRaises(Stopped):
            meter.take(1)

    def test_pieces_wait_for_their_turn(self):
        """At a rate, the reader hands on pieces of at most `step` bytes, each after the wait the speed asks for;
        the waits end at once when the run stops."""
        stop = mock.Mock(spec=threading.Event)
        stop.is_set.return_value = False
        stop.wait.return_value = False
        meter = Meter(100 * 1024, stop)
        self.assertEqual(meter.step, 100 * 1024 // 8)
        self.assertLessEqual(Meter(10 * 1024 ** 3, stop).step, PIECE)
        with mock.patch.object(meter._speed, "wait", return_value=0.5) as wait:
            source = SourceReader(MemoryDriver(chunk=len(DATA)), "/f", len(DATA), meter)
            self.assertEqual(source.read(), DATA)
        sizes = [call.args[1] for call in wait.call_args_list]
        self.assertEqual(sum(sizes), len(DATA))
        self.assertTrue(all(size <= meter.step for size in sizes))
        self.assertEqual(stop.wait.call_count, len(sizes))
        stop.wait.return_value = True  # stopped while waiting
        with mock.patch.object(meter._speed, "wait", return_value=0.5):
            with self.assertRaises(Stopped):
                SourceReader(MemoryDriver(), "/f", len(DATA), meter).read()

    def test_the_rate_over_several_threads(self):
        """All the threads of a run share one limit: real waits, at 64 KiB a second for 40 KiB from two threads."""
        import time

        meter = Meter(64 * 1024, threading.Event())
        started = time.monotonic()

        def read():
            SourceReader(MemoryDriver(data=bytes(20 * 1024)), "/f", 20 * 1024, meter).read()

        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        took = time.monotonic() - started
        self.assertEqual(meter.total, 40 * 1024)
        self.assertGreater(took, 40 / 64 - 0.25 - 0.05)  # less the burst it may be ahead
