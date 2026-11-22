"""Tests of apps.storages.drivers._cloud, the helpers the cloud drive drivers share."""

from __future__ import annotations

import datetime
import gzip
import io
import threading
import time
import unittest
from unittest import mock

import httpx

from apps.storages.drivers import _cloud, http
from apps.storages.drivers.base import AuthError, BackendError, DriverError
from apps.storages.tests.fake_cloud_http import LONG, FakeServer, LongBody, Response, file_response


class SmallHelperTests(unittest.TestCase):
    def test_usable_name(self):
        for name in ["a", "中文", "a b", " lead", ".hidden", "a.", "...a"]:
            self.assertTrue(_cloud.usable_name(name), name)
        for name in ["", ".", "..", "...", ".. ", " . ", "a/b", "a\x00b", "\ud800", None, 3]:
            self.assertFalse(_cloud.usable_name(name), repr(name))

    def test_as_int(self):
        self.assertEqual([_cloud.as_int(v) for v in [5, "7", " -3 ", "+2", 4.0]], [5, 7, -3, 2, 4])
        for value in ["²", "--5", "1.5", "", "x", True, None, 4.5]:
            self.assertIsNone(_cloud.as_int(value), repr(value))

    def test_parse_time(self):
        utc = datetime.timezone.utc
        self.assertEqual(_cloud.parse_time("2024-01-02T03:04:05Z"), datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=utc))
        self.assertEqual(_cloud.parse_time("2024-01-02T03:04:05.1234567Z").microsecond, 123456)
        self.assertEqual(_cloud.parse_time(1700000000), datetime.datetime.fromtimestamp(1700000000, utc))
        self.assertEqual(_cloud.parse_time("1700000000"), datetime.datetime.fromtimestamp(1700000000, utc))
        self.assertIsNotNone(_cloud.parse_time("2024-01-02T03:04:05").tzinfo)
        for value in [None, "", "²", "yesterday", 0, -5, True, [], "9" * 30]:
            self.assertIsNone(_cloud.parse_time(value), repr(value))

    def test_short(self):
        self.assertEqual(_cloud.short("a\nb\r\n c"), "a b c")
        self.assertEqual(len(_cloud.short("x" * 500)), 200)

    def test_origin(self):
        self.assertEqual(_cloud.origin("https://Graph.Microsoft.com/v1.0?x"), ("https", "graph.microsoft.com", 443))
        self.assertEqual(_cloud.origin("http://127.0.0.1:8080/"), ("http", "127.0.0.1", 8080))
        for url in ["/relative", "ftp://h/x", "http://[::1", ""]:
            self.assertEqual(_cloud.origin(url), ("", "", 0), url)

    def test_retry_after(self):
        def answer(value):  # header values as bytes, the way a server sends them
            return httpx.Response(429, headers={} if value is None else {"Retry-After": value.encode("latin-1")})

        self.assertEqual(_cloud.retry_after(answer(None)), 1.0)
        self.assertEqual(_cloud.retry_after(answer("3")), 3.0)
        self.assertIsNone(_cloud.retry_after(answer("3600")))
        self.assertEqual(_cloud.retry_after(answer("²")), 1.0)
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2))
        self.assertLessEqual(_cloud.retry_after(answer(soon.strftime("%a, %d %b %Y %H:%M:%S GMT"))), 2.0)

    def test_range_header(self):
        self.assertIsNone(_cloud.range_header(0, None, 100))
        self.assertIsNone(_cloud.range_header(0, 100, 100))
        self.assertEqual(_cloud.range_header(10, None, 100), "bytes=10-")
        self.assertEqual(_cloud.range_header(10, 5, 100), "bytes=10-14")

    def test_exact_body(self):
        self.assertEqual(b"".join(_cloud.exact_body(io.BytesIO(b"abcdef"), 4, chunk=3)), b"abcd")
        with self.assertRaises(DriverError):
            b"".join(_cloud.exact_body(io.BytesIO(b"ab"), 4))
        self.assertEqual(_cloud.read_exact(io.BytesIO(b"abc"), 10), b"abc")


class TokenCacheTests(unittest.TestCase):
    def test_refresh_once_and_reuse(self):
        calls = []

        def refresh():
            calls.append(1)
            time.sleep(0.05)
            return f"token-{len(calls)}", 3600

        tokens = _cloud.TokenCache(refresh)
        threads = [threading.Thread(target=tokens.get) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(calls), 1)
        tokens.invalidate("token-other")  # not the current one: nothing happens
        self.assertEqual(tokens.get(), "token-1")
        tokens.invalidate("token-1")
        self.assertEqual(tokens.get(), "token-2")

    def test_short_lifetimes_are_renewed_early(self):
        tokens = _cloud.TokenCache(lambda: ("t", 0.05))
        tokens.get()
        time.sleep(0.06)
        self.assertEqual(tokens.get(), "t")  # refreshed, no error

    def test_refusals_are_remembered(self):
        calls = []

        def refresh():
            calls.append(1)
            raise AuthError("授权失效了")

        tokens = _cloud.TokenCache(refresh)
        for _ in range(3):
            with self.assertRaises(AuthError) as caught:
                tokens.get()
            self.assertEqual(caught.exception.message, "授权失效了")
        self.assertEqual(len(calls), 1)

    def test_other_failures_are_not_remembered(self):
        calls = []

        def refresh():
            calls.append(1)
            if len(calls) == 1:
                raise BackendError("暂时不行")
            return "t", 3600

        tokens = _cloud.TokenCache(refresh)
        with self.assertRaises(BackendError):
            tokens.get()
        self.assertEqual(tokens.get(), "t")


class CacheAndLockTests(unittest.TestCase):
    def test_ttl_cache(self):
        cache = _cloud.TTLCache(2, 0.05)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")  # a is now the most recent
        cache.put("c", 3)  # b goes
        self.assertEqual((cache.get("a"), cache.get("b"), cache.get("c")), (1, None, 3))
        cache.discard_if(lambda key: key == "a")
        self.assertIsNone(cache.get("a"))
        time.sleep(0.08)  # past the 0.05 by more than a tick of Windows' clock (15.6 ms)
        self.assertIsNone(cache.get("c"))

    def test_keyed_locks(self):
        locks = _cloud.KeyedLocks()
        inside, most = [0], [0]

        def work(key):
            with locks.hold(key):
                inside[0] += 1
                most[0] = max(most[0], inside[0])
                time.sleep(0.02)
                inside[0] -= 1

        threads = [threading.Thread(target=work, args=("same",)) for _ in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(most[0], 1)
        self.assertEqual(locks._locks, {})  # nothing kept once nobody holds it
        # Different keys don't wait for each other: the five hold theirs at the same time. (Not timed: on a
        # busy machine five threads sleeping 0.02 s at once can take longer than 0.1 s, as if one after another.)
        together = threading.Barrier(5, timeout=10)

        def meet(key):
            with locks.hold(key):
                together.wait()

        threads = [threading.Thread(target=meet, args=(n,)) for n in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(together.broken, "different keys waited for each other")

    def test_rate_limiter(self):
        limiter = _cloud.RateLimiter(20)
        start = time.monotonic()
        for _ in range(5):
            limiter.wait()
        self.assertGreaterEqual(time.monotonic() - start, 0.18)  # 0.2, less a tick of Windows' clock (15.6 ms)
        # Four callers at once at 10 a second, waiting at most 0.15 s: turns 0 and 0.1 s away
        # are taken, the next ones (0.2 s away) refused at once.
        busy = _cloud.RateLimiter(10, longest=0.15, service="测试")
        outcomes = []
        barrier = threading.Barrier(4)

        def call():
            barrier.wait()
            try:
                busy.wait()
                outcomes.append("ok")
            except BackendError as error:
                outcomes.append(error.message)

        threads = [threading.Thread(target=call) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count("ok"), 2)
        self.assertTrue(all("测试" in o for o in outcomes if o != "ok"))


class DigitTests(unittest.TestCase):
    def test_only_ascii_digits_are_numbers(self):
        """STOR-17: isdigit() takes "²", which int() then refuses: a ValueError, not the storage's error."""
        odd = httpx.Response(206, headers=[(b"content-range", "bytes ²-9/10".encode())], content=b"x")
        with self.assertRaises(BackendError):
            _cloud.response_stream(odd, offset=0, length=None, total=10, service="X")


class ResponseStreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = bytes(range(256)) * 400
        cls.mode = "ok"

        def handle(request):
            if cls.mode == "short":  # chunked, and ended early: the HTTP client sees no error
                return Response(200, iter([cls.data[:1000]]), {"Transfer-Encoding": "chunked"})
            if cls.mode == "gzip":
                return Response(200, b"\x1f\x8b...", {"Content-Encoding": "gzip"})
            if cls.mode == "ignore-range":
                return Response(200, cls.data)
            if cls.mode == "long":  # chunked, longer than the listing said, and no length to go by
                return Response(200, iter([cls.data, b"more"]), {"Transfer-Encoding": "chunked"})
            if cls.mode == "resized":  # the download has another size than the listing gave
                return file_response(cls.data[:5000], request.header("Range"))
            return file_response(cls.data, request.header("Range"))

        cls.server = FakeServer(handle).start()
        cls.client = httpx.Client()

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.close()

    def stream(self, offset=0, length=None, total=None, mode="ok"):
        type(self).mode = mode
        headers = {"Accept-Encoding": "identity"}
        span = _cloud.range_header(offset, length, total)
        if span:
            headers["Range"] = span
        response = self.client.send(self.client.build_request("GET", self.server.url + "/f", headers=headers),
                                    stream=True)
        return _cloud.response_stream(response, offset=offset, length=length, total=total, service="测试")

    def test_ranges(self):
        total = len(self.data)
        self.assertEqual(self.stream(100, 50, total).read(), self.data[100:150])
        self.assertEqual(self.stream(total - 10, None, total).read(), self.data[-10:])
        self.assertEqual(self.stream(0, None, total).read(), self.data)

    def test_server_ignoring_the_range(self):
        stream = self.stream(1000, 20, len(self.data), mode="ignore-range")
        self.assertEqual((stream.offset, stream.size), (1000, 20))
        self.assertEqual(stream.read(), self.data[1000:1020])

    def test_short_body_is_an_error(self):
        with self.assertRaises(BackendError):
            self.stream(0, None, len(self.data), mode="short").read()

    def test_the_downloads_own_size_counts(self):
        """A listing can give another size than the download has: the stream promises what the download has."""
        for listed in (1000, 9000):
            with self.subTest(listed=listed):
                stream = self.stream(0, None, listed, mode="resized")
                self.assertEqual((stream.size, stream.total), (5000, 5000))
                self.assertEqual(stream.read(), self.data[:5000])
        stream = self.stream(100, None, 9000, mode="resized")
        self.assertEqual((stream.offset, stream.size, stream.total), (100, 4900, 5000))
        self.assertEqual(stream.read(), self.data[100:5000])

    def test_a_body_longer_than_promised_is_an_error(self):
        """Passed on, the end would be cut off at Content-Length and the file look complete."""
        stream = self.stream(0, None, len(self.data), mode="long")
        self.assertEqual(stream.size, len(self.data))
        with self.assertRaises(BackendError):
            stream.read()

    def test_encoded_body_is_refused(self):
        with self.assertRaises(BackendError):
            self.stream(0, None, None, mode="gzip")

    def test_close_early(self):
        stream = self.stream(0, None, len(self.data))
        next(iter(stream))
        stream.close()
        self.assertTrue(stream.closed)


class DrainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def handle(request):
            if request.path == "/done":
                return Response(204)
            return Response(404, b"<html>not found</html>" * 20, {"Content-Type": "text/html"})

        cls.server = FakeServer(handle).start()
        cls.addClassCleanup(cls.server.close)

    def test_the_connection_is_used_again(self):
        """Closed before the end of its body, even an empty one (a 204's, a HEAD's), an answer took its connection with
        it: the next request made a new one (with TLS, a new handshake)."""
        for method, path in (("GET", "/missing"), ("DELETE", "/done"), ("HEAD", "/missing")):
            with self.subTest(method=method), httpx.Client() as client:
                before = self.server.connections
                for _ in range(3):
                    http.drain(client.send(client.build_request(method, self.server.url + path), stream=True))
                self.assertEqual(self.server.connections - before, 1)

    def test_an_answer_read_already_is_only_closed(self):
        with httpx.Client() as client:
            whole = client.get(self.server.url + "/missing")
            streamed = client.send(client.build_request("GET", self.server.url + "/missing"), stream=True)
            next(streamed.iter_raw(10))  # begun
            for response in (whole, streamed):
                http.drain(response)
                self.assertTrue(response.is_closed)


class ReadErrorTests(unittest.TestCase):
    """http.read_error(): the words of an error answer, from the first DRAIN_BYTES of its body."""

    WORDS = b'{"error": "no such file"}'

    @classmethod
    def setUpClass(cls):
        def slow():
            yield cls.WORDS
            for _ in range(100):
                time.sleep(0.1)
                yield b" "

        def handle(request):
            if request.path == "/gzip":
                return Response(404, gzip.compress(cls.WORDS), {"Content-Type": "application/json", "Content-Encoding": "gzip"})
            if request.path == "/slow":
                return Response(404, slow(), {"Content-Type": "application/json", "Transfer-Encoding": "chunked"})
            return Response(404, cls.WORDS, {"Content-Type": "application/json"})

        cls.server = FakeServer(handle).start()
        cls.addClassCleanup(cls.server.close)

    def read_error(self, client, path):
        streamed = client.send(client.build_request("GET", self.server.url + path), stream=True)
        answer = http.read_error(streamed)
        self.assertTrue(streamed.is_closed)
        return answer

    def test_the_words(self):
        with httpx.Client() as client:
            before = self.server.connections
            for _ in range(3):
                answer = self.read_error(client, "/short")
                self.assertEqual((answer.status_code, answer.json()), (404, {"error": "no such file"}))
                self.assertEqual(answer.headers["content-type"], "application/json")
            self.assertEqual(self.server.connections - before, 1)  # read to its end: the connection is used again

    def test_a_long_body(self):
        """The first DRAIN_BYTES: the site holds no more of it, and stops reading."""
        body = LongBody(self.WORDS)
        with self.server.intercepting(lambda request: Response(404, body, {"Content-Type": "application/json"}, length=LONG)), \
                httpx.Client() as client:
            answer = self.read_error(client, "/long")
        self.assertEqual(len(answer.content), http.DRAIN_BYTES)
        self.assertEqual(answer.json(), {"error": "no such file"})  # spaces after it, which JSON allows
        self.assertLess(body.sent, LONG // 4)

    def test_a_compressed_body(self):
        with httpx.Client() as client:
            answer = self.read_error(client, "/gzip")
        self.assertEqual(answer.json(), {"error": "no such file"})
        self.assertNotIn("content-encoding", answer.headers)

    def test_a_slow_body(self):
        """Read in DRAIN_SECONDS at most: httpx's timeout is for each read, which a server that sends a little
        every now and then never runs into."""
        with mock.patch.object(http, "DRAIN_SECONDS", 0.5), httpx.Client() as client:
            started = time.monotonic()
            answer = self.read_error(client, "/slow")
        self.assertLess(time.monotonic() - started, 5)
        self.assertTrue(answer.content.startswith(self.WORDS))


if __name__ == "__main__":
    unittest.main()
