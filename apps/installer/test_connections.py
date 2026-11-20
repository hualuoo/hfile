"""apps/installer/connections.py: the rules for slow requests on a real waitress server in this process
(their limits made short), and ChunkedReceiver against waitress's own, which it stands in for."""

import collections
import hashlib
import importlib.metadata
import inspect
import random
import socket
import threading
import time
from http.client import HTTPConnection
from unittest import mock

from django.core.handlers.wsgi import WSGIHandler
from django.http import HttpResponse
from django.test import SimpleTestCase, override_settings
from django.urls import path
from waitress import parser as waitress_parser
from waitress import receiver as waitress_receiver
from waitress.adjustments import Adjustments
from waitress.buffers import OverflowableBuffer
from waitress.receiver import FixedStreamReceiver
from waitress.server import create_server
from waitress.utilities import BadRequest, RequestEntityTooLarge

from apps.core.middleware import utf8_only
from apps.installer import connections
from apps.installer.connections import CHUNK_DATA, CHUNK_LINE, FREE_CHUNKS, LINE_BYTES, TRAILER, ChunkedReceiver

WAITRESS = waitress_receiver.ChunkedReceiver
ADJ = Adjustments()  # waitress's defaults, as serve's: reads of 8 KB, bodies in memory up to 512 KB
TOO_LONG_LINE = "块大小那一行（连同扩展）超过了"
TOO_LONG_TRAILER = "trailer 超过了"
TOO_MANY = "分块的请求体里块太多"
# Size lines with a LF before their CRLF: waitress takes them (the $ of its regexes), ours refuses them
# with waitress's own errors.
LF_LINES = [(b"5\n", "Invalid chunk size"), (b"5;a\n", "Invalid chunk extension"), (b"5;a=b\n", "Invalid chunk extension")]


def hello(environ, start_response):
    body = environ["wsgi.input"].read()
    start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", str(len(body) + 2))])
    return [b"ok" + body]


def echo(request):
    """What Django made of a request's body."""
    body = request.body
    response = HttpResponse(
        f"{request.META.get('CONTENT_LENGTH')} {len(body)} {hashlib.sha256(body).hexdigest()} "
        f"{'HTTP_TRANSFER_ENCODING' in request.META}"
    )
    response["Content-Length"] = len(response.content)  # else waitress would send it chunked
    return response


urlpatterns = [path("echo/", echo)]


class ServerTestCase(SimpleTestCase):
    """A waitress server made as serve makes it (create_server, then connections.guard), in a thread."""

    def start(self, app=hello, per_address=0):
        # cleanup_interval 0: the rules are checked every turn of the loop (at least once a second).
        server = create_server(app, host="127.0.0.1", port=0, threads=2, channel_timeout=30, cleanup_interval=0)
        connections.guard(server, per_address=per_address)
        stop = threading.Event()

        def loop():
            while not stop.is_set():
                server.asyncore.loop(timeout=0.1, map=server._map, use_poll=True, count=1)
            server.close()
            server.task_dispatcher.shutdown(timeout=5)

        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 10)
        self.addCleanup(stop.set)
        self.port = server.effective_port
        return server

    def connect(self):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        return sock

    def exchange(self, request, *more):
        """Sends `request` on a new connection, then each of `more` a moment later (the server may read
        them apart or not); the answer, read until the server closes the connection."""
        sock = self.connect()
        sock.sendall(request)
        for part in more:
            time.sleep(0.1)
            sock.sendall(part)
        answer = b""
        while chunk := sock.recv(65536):
            answer += chunk
        return answer

    def get(self, body=b""):
        head = b"POST / HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: %d\r\n\r\n" % len(body)
        return self.exchange(head + body)


class GuardTests(ServerTestCase):
    def start(self, per_address=0):
        for name, value in (("HEADER_SECONDS", 1), ("BODY_GRACE", 1), ("BODY_RATE", 1000), ("PER_ADDRESS", per_address)):
            patcher = mock.patch.object(connections, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        return super().start(per_address=per_address)

    def closed_within(self, sock, seconds, trickle=b""):
        """Whether the server closes `sock` within `seconds`, while it sends `trickle` every 0.2 s."""
        deadline = time.monotonic() + seconds
        sock.settimeout(0.2)
        while time.monotonic() < deadline:
            try:
                if trickle:
                    sock.sendall(trickle)
                if sock.recv(1024) == b"":
                    return True
            except TimeoutError:
                continue
            except OSError:  # reset, or broken pipe on the next send
                return True
        return False

    def reported(self, logs, text):
        """Whether the console got a line with `text` (said from the server's thread, maybe a moment later)."""
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if any(text in record.getMessage() for record in logs.records):
                return True
            time.sleep(0.1)
        return False

    def test_a_header_sent_a_byte_at_a_time_is_closed(self):
        self.start()
        slow = self.connect()
        slow.sendall(b"GET / HTTP/1.1\r\nHost: x\r\nX-Slow: ")
        with self.assertLogs("hfile", "WARNING") as logs:
            self.assertTrue(self.closed_within(slow, 4, trickle=b"a"))  # 1 s allowed, never idle
            self.assertTrue(self.reported(logs, "关掉了 1 个太慢的请求（请求头 1 秒内没有发完"))
        self.assertTrue(self.get().startswith(b"HTTP/1.1 200 OK"))  # the others are answered

    def test_a_body_sent_a_byte_at_a_time_is_closed(self):
        self.start()
        slow = self.connect()
        slow.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100000\r\n\r\n")
        with self.assertLogs("hfile", "WARNING") as logs:
            self.assertTrue(self.closed_within(slow, 5, trickle=b"b"))  # 1 s and 1000 bytes a second allowed
            self.assertTrue(self.reported(logs, "关掉了 1 个太慢的请求"))

    def test_a_body_that_keeps_coming_is_answered(self):
        self.start()
        sock = self.connect()
        sock.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nConnection: close\r\nContent-Length: 6000\r\n\r\n")
        for _ in range(10):  # 3 s for 6000 bytes: above 1000 a second once the first second is over
            sock.sendall(b"x" * 600)
            time.sleep(0.3)
        answer = b""
        while chunk := sock.recv(65536):
            answer += chunk
        self.assertTrue(answer.startswith(b"HTTP/1.1 200 OK"), answer[:100])
        self.assertTrue(answer.endswith(b"ok" + b"x" * 6000))

    def test_an_idle_connection_between_requests_is_left_alone(self):
        self.start()
        sock = self.connect()
        sock.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertTrue(sock.recv(65536).startswith(b"HTTP/1.1 200 OK"))
        self.assertFalse(self.closed_within(sock, 2.5))  # no request on its way: channel_timeout's (30 s)

    def test_one_address_may_hold_only_so_many(self):
        self.start(per_address=3)
        held = [self.connect() for _ in range(3)]
        time.sleep(0.5)  # accepted
        with self.assertLogs("hfile", "WARNING") as logs:  # before it connects: the server may report it at once
            extra = self.connect()
            self.assertTrue(self.closed_within(extra, 2))
            self.assertTrue(self.reported(logs, "拒绝了 1 个连接（同一个地址同时最多 3 个）"))
        held.pop().close()
        time.sleep(0.5)
        self.assertTrue(self.get().startswith(b"HTTP/1.1 200 OK"))

    def test_behind_a_proxy_there_is_no_limit_per_address(self):
        self.start(per_address=0)
        held = [self.connect() for _ in range(5)]
        time.sleep(0.5)
        self.assertTrue(self.get().startswith(b"HTTP/1.1 200 OK"))
        self.assertEqual(len(held), 5)


# Chunked bodies -------------------------------------------------------------------------------------


def outcome(receiver_class, pieces):
    """What a receiver makes of a body arriving in `pieces`, fed as waitress's parser feeds it (until the
    body is complete or has an error). For each read: the count consumed, complete or not, the error, the
    trailer and the length of the body so far; then the body as the site reads it (wsgi.input)."""
    receiver = receiver_class(OverflowableBuffer(ADJ.inbuf_overflow))
    reads = []
    for piece in pieces:
        consumed = receiver.received(piece)
        error = receiver.error and (type(receiver.error), receiver.error.code, receiver.error.body)
        reads.append((consumed, receiver.completed, error, bytes(receiver.trailer), len(receiver)))
        if receiver.completed or receiver.error:
            break
    body = receiver.getfile().read()
    receiver.getbuf().close()  # a body over 512 KB is in a temporary file
    return reads, body


class Strict:
    """One of waitress's regexes, matched with fullmatch as connections.ChunkedReceiver matches it, for
    running waitress's own ChunkedReceiver that way. `refused` gets what waitress's match takes and
    fullmatch doesn't."""

    refused = []

    def __init__(self, regex):
        self.regex = regex

    def match(self, text):
        found = self.regex.fullmatch(text)
        if found is None and self.regex.match(text):
            Strict.refused.append(bytes(text))
        return found


def strict_outcome(pieces):
    """outcome() of waitress's ChunkedReceiver with fullmatch where it has match; and the size lines and
    extensions that took away from what waitress takes."""
    Strict.refused = []
    with (
        mock.patch.object(waitress_receiver, "CHUNK_EXT_RE", Strict(waitress_receiver.CHUNK_EXT_RE)),
        mock.patch.object(waitress_receiver, "ONLY_HEXDIG_RE", Strict(waitress_receiver.ONLY_HEXDIG_RE)),
    ):
        return outcome(WAITRESS, pieces), Strict.refused


def split(body, *cuts):
    cuts = sorted(set(cut for cut in cuts if 0 < cut < len(body)))
    return [body[a:b] for a, b in zip([0, *cuts], [*cuts, len(body)])]


def splits(body, rng, every=64):
    """`body` in one read; a short one also split at every place and a byte at a time; and split at a
    few random places."""
    yield [body]
    if len(body) <= every:
        for at in range(1, len(body)):
            yield [body[:at], body[at:]]
        yield split(body, *range(len(body)))
    for _ in range(3 if len(body) > 2 else 0):
        yield split(body, *rng.sample(range(1, len(body)), min(len(body) - 1, rng.randint(2, 8))))


def ways(body, *around):
    """`body` in one read, in reads of 8 KB as the channel reads, a byte at a time, and split once at
    each place within 3 bytes of each of `around`."""
    yield [body]
    yield split(body, *range(ADJ.recv_bytes, len(body), ADJ.recv_bytes))
    yield split(body, *range(len(body)))
    for place in around:
        for at in range(place - 3, place + 4):
            yield split(body, at)


TCHAR = b"!#$%&'*+-.^_`|~0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
NOISE = b'\r\n\r\n01aF;="\\ x\t'  # what size lines and their ends are made of, CR and LF most


def token(rng):
    return bytes(rng.choice(TCHAR) for _ in range(rng.randint(1, 4)))


def extensions(rng):
    """Chunk extensions as RFC 9112 has them: names, maybe with a value, a token or a quoted string."""
    text = b""
    for _ in range(rng.randint(0, 2)):
        text += b";" + token(rng)
        if rng.random() < 0.5:
            quoted = b'"' + rng.choice([b"", b"a b", b'\\"', b"\\\\", b";x", b"\t", b"\x80\xff"]) + b'"'
            text += b"=" + (token(rng) if rng.random() < 0.5 else quoted)
    return text


def with_lf(digits, extension, rng):
    """A size line; now and then with a LF at the end of the size or of the extensions, which waitress
    takes (the $ of its regexes) and connections.ChunkedReceiver refuses."""
    if rng.random() < 0.05:
        if extension and rng.random() < 0.5:
            extension += b"\n"
        else:
            digits += b"\n"
    return digits + extension


def valid_body(rng):
    """A body waitress takes: chunks of 1 to 40 bytes, sizes in either case and with leading zeros,
    extensions, empty lines between chunks, a trailer or not, maybe the next request after it; now and
    then a size line with a LF before its CRLF (with_lf)."""
    body = b""
    for _ in range(rng.randint(0, 4)):
        if rng.random() < 0.1:
            body += b"\r\n"
        size = rng.choice([1, 2, 3, 5, 10, 16, 17, rng.randint(1, 40)])
        digits = format(size, "X" if rng.random() < 0.3 else "x").encode()
        if rng.random() < 0.2:
            digits = b"0" * rng.randint(1, 3) + digits
        data = bytes(rng.choice(b"ab\r\n0;") for _ in range(size))
        body += with_lf(digits, extensions(rng), rng) + b"\r\n" + data + b"\r\n"
    body += with_lf(rng.choice([b"0", b"00"]), rng.choice([b"", b";x=1"]), rng) + b"\r\n"
    for _ in range(rng.randint(0, 2)):
        body += b"X-T%d: v\r\n" % rng.randint(0, 9)
    body += b"\r\n"
    if rng.random() < 0.3:
        body += rng.choice([b"GET / HTTP/1.1\r\n\r\n", b"\r\n", b"x"])
    return body


def mutated(body, rng):
    """`body` with one to three bytes changed, added or taken away: mostly CR, LF and what size lines are
    made of, sometimes any byte."""
    body = bytearray(body)
    for _ in range(rng.randint(1, 3)):
        at = rng.randrange(len(body) + 1)
        byte = rng.choice(NOISE) if rng.random() < 0.9 else rng.randrange(256)
        what = rng.random()
        if what < 1 / 3 and body:
            body[min(at, len(body) - 1)] = byte
        elif what < 2 / 3:
            body[at:at] = bytes([byte])
        elif body:
            del body[min(at, len(body) - 1)]
    return bytes(body)


def kinds(pieces, result, refused):
    """How a body ended, for counting what the generated bodies reach."""
    reads, _ = result
    consumed, completed, error, trailer, _ = reads[-1]
    yield error[2] if error else ("complete" if completed else "unfinished")
    if error and completed:
        yield "a trailer read after the error"
    if completed and trailer == b"\r":
        yield "the CR and LF that end it in two reads"
    if consumed < len(pieces[len(reads) - 1]):
        yield "bytes left for the next request"
    if refused:
        yield "a size line with a LF, refused"
        if all(len(piece) == 1 for piece in pieces):
            yield "a size line with a LF, refused, a byte a read"  # its LF and CRLF in different reads


HANDPICKED = [
    b"5\r\nhello\r\n0\r\n\r\n",
    b"5\r\nhello\r\n0\r\n\r\nGET / HTTP/1.1\r\n\r\n",  # the next request follows
    b"\r\n\r\n5\r\nhello\r\n\r\n0\r\n\r\n",  # empty lines are skipped
    # A LF ending the size or the extensions: waitress takes it (the $ of its regexes), ours refuses it.
    b"5\n\r\nhello\r\n0\r\n\r\n", b"5;a\n\r\nhello\r\n0\r\n\r\n", b"5;a=b\n\r\nhello\r\n0\r\n\r\n",
    b'5;a="b"\n\r\nhello\r\n0\r\n\r\n', b"5\n;a\r\nhello\r\n0\r\n\r\n", b"5\n;a\n\r\nhello\r\n",
    b"x;a\n\r\n", b"5\r\nhello\r\n0\n\r\n\r\n", b"5\r\nhello\r\n0;x\n\r\nX: 1\r\n\r\n", b"5\n\n\r\n",
    b"5\r\r\nhello\r\n0\r\n\r\n",  # a CR there: not a size
    b"0x5\r\nhello\r\n", b"-5\r\n", b"+5\r\n", b" 5\r\n", b"5 \r\n", b"5\t\r\n", b"\xb5\r\n", b"\x0b5\r\n",
    b"5_0\r\n", b"\r\n\r\n\r\n",
    b"5;\r\nhello\r\n", b"5;a=\r\n", b"5;a b\r\n", b"5 ;a\r\n", b"5;a=\"b\r\n", b"5;=b\r\n",
    b'5;a="b\\"c";d=e\r\nhello\r\n0\r\n\r\n', b'5;a="\x80\xff"\r\nhello\r\n0\r\n\r\n', b'5;a="\x7f"\r\n',
    b"5\r\nhelloX\r\n0\r\n\r\n",  # chunk not properly terminated
    b"5\r\nhello\n0\r\n\r\n", b"5\r\nhello\r0\r\n\r\n", b"5\r\nhello\n\r\n0\r\n\r\n",
    b"5\r\nhelloXX\r\n\r\nGET / HTTP/1.1\r\n\r\n",  # after that error waitress reads on, as a trailer
    b"5\r\nhelloXX" + b"y" * (TRAILER + 100) + b"\r\n\r\nGET",  # longer than TRAILER: that read alone
    b"5\r\nhelloX\r\n\r\n", b"5\r\nhello\r\r\n\r\n",
    b"5\r\nhello\r\n0\r\nX-A: 1\r\nX-B: 2\r\n\r\n",
    b"5\r\nhello\r\n0\r\n\n", b"5\r\nhello\r\n0\r\n\n\n", b"5\r\nhello\r\n0\r\nX: 1\n\n\r\n\r\n",
    b"5\r\nhello\r\n0\r\n\r\r\n\r\n", b"5\r\nhello\r\n0\r\n\r\n\r\n",
    b"ffffffffffffffffffff\r\nabc",  # a huge size: it waits for the data
    b"0000000000000005\r\nhello\r\n0\r\n\r\n",
    b"5\r\nhel", b"5\r\nhello\r", b"5\r\nhello\r\n0\r", b"5\r\nhello\r\n0\r\nX: 1\r\n\r",
]


class ChunkedLikeWaitressTests(SimpleTestCase):
    """ChunkedReceiver against waitress 3.0.2's: for the same bytes, however they are split into reads,
    the same count consumed, end, error, trailer and body after every read; but for a size line with a
    LF before its CRLF, which waitress takes and ours refuses with waitress's own error."""

    def assertLikeWaitress(self, body, pieces):
        """Ours is waitress's code with fullmatch where it has match, read for read; where that refuses
        nothing, it is waitress itself. Returns our outcome and what fullmatch refused."""
        where = f"{body!r} in reads of {[len(p) for p in pieces]}"
        ours = outcome(ChunkedReceiver, pieces)
        strict, refused = strict_outcome(pieces)
        self.assertEqual(ours, strict, where)
        if not refused:
            self.assertEqual(ours, outcome(WAITRESS, pieces), where)
        else:  # only lines that end in a LF, and they end the body with waitress's own error
            self.assertTrue(all(text.endswith(b"\n") for text in refused), where)
            self.assertIn(ours[0][-1][2][2], ("Invalid chunk size", "Invalid chunk extension"), where)
        return ours, refused

    def test_generated_bodies(self):
        rng = random.Random(20261110)
        bodies = []
        for _ in range(1500):
            body = valid_body(rng)
            bodies += [body, mutated(body, rng)]
        bodies += [bytes(rng.choice(NOISE) for _ in range(rng.randint(1, 30))) for _ in range(500)]
        seen = collections.Counter()
        for body in filter(None, bodies):
            for pieces in splits(body, rng):
                seen.update(kinds(pieces, *self.assertLikeWaitress(body, pieces)))
        # The bodies reach every way a chunked body ends, so this test doesn't quietly become weak.
        for kind in ("complete", "unfinished", "Invalid chunk extension", "Invalid chunk size",
                     "Chunk not properly terminated", "a trailer read after the error",
                     "the CR and LF that end it in two reads", "bytes left for the next request",
                     "a size line with a LF, refused"):
            self.assertGreater(seen[kind], 300, kind)
        self.assertGreater(seen["a size line with a LF, refused, a byte a read"], 50)

    def test_handpicked_bodies(self):
        rng = random.Random(1)
        for body in HANDPICKED:
            for pieces in splits(body, rng, every=100):
                self.assertLikeWaitress(body, pieces)

    def test_a_lf_in_a_size_line_is_refused(self):
        for line, message in LF_LINES:
            body = line + b"\r\nhello\r\n0\r\n\r\n"
            # whole, split between the LF and the CRLF, before the LF, a byte at a time
            for pieces in ([body], split(body, len(line)), split(body, len(line) - 1), split(body, *range(len(body)))):
                reads, received = outcome(WAITRESS, pieces)
                self.assertEqual((reads[-1][1:3], received), ((True, None), b"hello"))  # a chunk to waitress
                reads, received = outcome(ChunkedReceiver, pieces)
                self.assertEqual((reads[-1][2], received), ((BadRequest, 400, message), b""))
                self.assertEqual(reads[-1][0], len(pieces[len(reads) - 1]))  # all of that read, as waitress's errors


def keeps_to_the_rule(lines):
    """Whether size lines [(length, data after it)] keep to FREE_CHUNKS as connections.py states it."""
    counted = data = 0
    for length, size in lines:
        counted += 1 + length // LINE_BYTES
        if counted > FREE_CHUNKS + data // CHUNK_DATA:
            return False
        data += size
    return True


def most(fits):
    """The largest n for which fits(n) (true up to some n, then false)."""
    low, high = 0, 1
    while fits(high):
        low, high = high, high * 2
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (middle, high) if fits(middle) else (low, middle)
    return low


def tiny_chunks(n, first=b"", extension=b""):
    """A body of a chunk of `first` (if any), n one-byte chunks with `extension` and the last chunk."""
    head = b"%x\r\n%s\r\n" % (len(first), first) if first else b""
    return head + (b"1" + extension + b"\r\nx\r\n") * n + b"0\r\n\r\n"


def tiny_lines(n, first=b"", extension=b""):
    """The size lines of tiny_chunks(n, first, extension), for keeps_to_the_rule."""
    head = [(len(b"%x" % len(first)), len(first))] if first else []
    return head + [(1 + len(extension), 1)] * n + [(1, 0)]


class ChunkedLimitTests(SimpleTestCase):
    """The limits: just under, ChunkedReceiver does what waitress does; just over, a 400."""

    def taken(self, body, data, *around):
        for pieces in ways(body, *around):
            ours = outcome(ChunkedReceiver, pieces)
            self.assertEqual(ours, outcome(WAITRESS, pieces), [len(p) for p in pieces])
            reads, received = ours
            self.assertEqual(reads[-1][1:3], (True, None))
            self.assertEqual(received, data)

    def refused(self, body, message, *around):
        for pieces in ways(body, *around):
            reads, _ = outcome(ChunkedReceiver, pieces)
            self.assertEqual(reads[-1][2][:2], (BadRequest, 400), [len(p) for p in pieces])
            self.assertIn(message, reads[-1][2][2])

    def test_a_size_line_up_to_chunk_line_bytes(self):
        line = b"5;a=" + b"b" * (CHUNK_LINE - 4)
        self.assertEqual(len(line), CHUNK_LINE)
        self.taken(line + b"\r\nhello\r\n0\r\n\r\n", b"hello", CHUNK_LINE)
        self.refused(line + b"b\r\nhello\r\n0\r\n\r\n", TOO_LONG_LINE, CHUNK_LINE + 1)
        self.refused(b"\r\n" * 3 + line + b"b\r\n", TOO_LONG_LINE, CHUNK_LINE + 7)

    def test_a_size_line_too_long_is_refused_before_it_ends(self):
        # A byte at a time and no end: refused at CHUNK_LINE + 2 bytes (of CHUNK_LINE + 1, the last could
        # be the CR of its end), holding no more than CHUNK_LINE + 1 meanwhile.
        receiver = ChunkedReceiver(OverflowableBuffer(ADJ.inbuf_overflow))
        for count, byte in enumerate(b"5;a=" + b"b" * CHUNK_LINE, 1):
            receiver.received(bytes([byte]))
            self.assertLessEqual(len(receiver.control_line), CHUNK_LINE + 1)
            if receiver.error:
                break
        self.assertEqual(count, CHUNK_LINE + 2)
        self.assertIn(TOO_LONG_LINE, receiver.error.body)
        self.assertEqual(receiver.received(b"\r\n"), 0)  # and nothing more after it
        # CHUNK_LINE bytes and a CR may still be a whole line; any next byte but its LF makes it too long.
        for after, refused in ((b"\n", False), (b"\r", True), (b"5", True)):
            receiver = ChunkedReceiver(OverflowableBuffer(ADJ.inbuf_overflow))
            receiver.received(b"5" * CHUNK_LINE + b"\r")
            self.assertIsNone(receiver.error)
            receiver.received(after)
            self.assertEqual(receiver.error is not None, refused, after)
        self.assertEqual(receiver.chunk_remainder, 0)
        receiver = ChunkedReceiver(OverflowableBuffer(ADJ.inbuf_overflow))
        receiver.received(b"5" * CHUNK_LINE + b"\r\n")
        self.assertEqual(receiver.chunk_remainder, int(b"5" * CHUNK_LINE, 16))  # waits for that much data

    def test_a_trailer_up_to_trailer_bytes(self):
        trailer = b"X-Long: " + b"a" * (TRAILER - 12) + b"\r\n\r\n"
        self.assertEqual(len(trailer), TRAILER)
        start = b"5\r\nhello\r\n0\r\n"
        self.taken(start + trailer, b"hello", len(start + trailer))
        reads, _ = outcome(ChunkedReceiver, [start + trailer + b"GET"])
        self.assertEqual((reads[-1][0], reads[-1][3]), (len(start + trailer), trailer))
        longer = b"X-Long: " + b"a" * (TRAILER - 11) + b"\r\n\r\n"
        self.refused(start + longer, TOO_LONG_TRAILER, len(start + longer))
        self.refused(start + b"X-A: b\r\n" * (TRAILER // 8), TOO_LONG_TRAILER)  # fields that never end

    def test_a_trailer_too_long_is_refused_before_it_ends(self):
        receiver = ChunkedReceiver(OverflowableBuffer(ADJ.inbuf_overflow))
        receiver.received(b"5\r\nhello\r\n0\r\n")
        for count, byte in enumerate(b"X: " + b"a" * TRAILER, 1):
            receiver.received(bytes([byte]))
            self.assertLess(len(receiver.trailer), TRAILER)
            if receiver.error:
                break
        self.assertEqual(count, TRAILER)  # TRAILER bytes and no end yet: one more byte at least
        self.assertIn(TOO_LONG_TRAILER, receiver.error.body)

    def test_tiny_chunks(self):
        n = most(lambda n: keeps_to_the_rule(tiny_lines(n)))
        self.assertEqual(n, 1024)  # today: 1025 size lines, the 1024 bytes before the last earn one
        self.taken(tiny_chunks(n), b"x" * n)
        self.refused(tiny_chunks(n + 1), TOO_MANY, len(tiny_chunks(n + 1)) - 5)

    def test_data_earns_more_chunks(self):
        first = b"y" * (40 * CHUNK_DATA + 7)
        n = most(lambda n: keeps_to_the_rule(tiny_lines(n, first)))
        # 40 KB earn 40 more size lines, the first chunk's own is one of them
        self.assertEqual(n, most(lambda n: keeps_to_the_rule(tiny_lines(n))) + 40 - 1)
        self.taken(tiny_chunks(n, first), first + b"x" * n)
        self.refused(tiny_chunks(n + 1, first), TOO_MANY)

    def test_long_size_lines_count_more(self):
        # A size line counts once more for every LINE_BYTES bytes in it: today a line of 31 bytes once, of
        # 32 twice, of 159 five times, of CHUNK_LINE 129 times. (Fewer than 1024 bytes of data earn nothing.)
        for length, n in (
            (LINE_BYTES - 1, most(lambda n: keeps_to_the_rule(tiny_lines(n)))),
            (LINE_BYTES, (FREE_CHUNKS - 1) // 2),
            (5 * LINE_BYTES - 1, (FREE_CHUNKS - 1) // 5),
            (CHUNK_LINE, (FREE_CHUNKS - 1) // (1 + CHUNK_LINE // LINE_BYTES)),
        ):
            extension = b";" + b"e" * (length - 2)  # after the size "1": a line of `length` bytes
            self.assertEqual(most(lambda n: keeps_to_the_rule(tiny_lines(n, extension=extension))), n)
            self.taken(tiny_chunks(n, extension=extension), b"x" * n)
            self.refused(tiny_chunks(n + 1, extension=extension), TOO_MANY)

    def test_empty_lines_count(self):
        n = most(lambda n: keeps_to_the_rule([(0, 0)] * n + [(1, 0)]))
        self.assertEqual(n, FREE_CHUNKS - 1)
        self.taken(b"\r\n" * n + b"0\r\n\r\n", b"")
        self.refused(b"\r\n" * (n + 1) + b"0\r\n\r\n", TOO_MANY)

    def test_chunks_of_a_kb_or_more_never_meet_the_limit(self):
        rng = random.Random(5)
        for sizes in (
            [CHUNK_DATA] * (FREE_CHUNKS + 10),
            [4096, 8192, 16384, 65536] * 4,
            [rng.randint(CHUNK_DATA, 20000) for _ in range(90)],
        ):
            data = rng.randbytes(sum(sizes))
            body, at = b"", 0
            for size in sizes:
                body += b"%x\r\n%s\r\n" % (size, data[at:at + size])
                at += size
            body += b"0\r\n\r\n"
            pieces = split(body, *range(ADJ.recv_bytes, len(body), ADJ.recv_bytes))  # as the channel reads
            ours = outcome(ChunkedReceiver, pieces)
            self.assertEqual(ours, outcome(WAITRESS, pieces))
            self.assertEqual(ours[1], data)


class ChunkedParserTests(SimpleTestCase):
    """Through connections.Parser, as waitress's channel uses it."""

    HEADER = b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n"

    def parse(self, *pieces, adj=ADJ):
        """A Parser fed `pieces`, each in reads of 8 KB at most, until the request is complete."""
        parser = connections.Parser(adj)
        for request in pieces:
            while request and not parser.completed:
                request = request[parser.received(request[:ADJ.recv_bytes]):]
        return parser

    def test_a_chunked_body_is_read_by_ours_into_waitress_s_buffer(self):
        parser = self.parse(self.HEADER)
        self.assertIs(type(parser.body_rcv), ChunkedReceiver)
        self.assertIs(type(parser.body_rcv.getbuf()), OverflowableBuffer)
        self.assertEqual(parser.body_rcv.getbuf().overflow, ADJ.inbuf_overflow)
        data = bytes(range(256)) * 100
        parser = self.parse(self.HEADER + b"1000;a=b\r\n" + data[:4096] + b"\r\n%x\r\n" % (len(data) - 4096)
                            + data[4096:] + b"\r\n0\r\nX-Sum: 1\r\n\r\n")
        self.assertTrue(parser.completed)
        self.assertIsNone(parser.error)
        self.assertEqual(parser.headers["CONTENT_LENGTH"], str(len(data)))
        self.assertNotIn("TRANSFER_ENCODING", parser.headers)
        self.assertEqual(parser.get_body_stream().read(), data)

    def test_other_bodies_are_waitress_s(self):
        parser = self.parse(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 5\r\n\r\nhello")
        self.assertIs(type(parser.body_rcv), FixedStreamReceiver)
        self.assertEqual(parser.get_body_stream().read(), b"hello")
        parser = self.parse(b"GET / HTTP/1.0\r\nHost: x\r\n\r\n")
        self.assertIsNone(parser.body_rcv)

    def test_a_refused_body_is_a_400_that_ends_the_request(self):
        for body, message in (
            (b"1;" + b"a" * CHUNK_LINE, TOO_LONG_LINE),
            (b"0\r\n" + b"X: y\r\n" * 2000, TOO_LONG_TRAILER),
            (tiny_chunks(FREE_CHUNKS + 10), TOO_MANY),
            (b"5\r\nhello!!", "Chunk not properly terminated"),
        ):
            parser = self.parse(self.HEADER + body)
            self.assertTrue(parser.completed)
            self.assertIsInstance(parser.error, BadRequest)
            self.assertEqual(parser.error.code, 400)
            self.assertIn(message, parser.error.body)

    def test_a_lf_in_a_size_line_is_a_400(self):
        for line, message in LF_LINES:
            for pieces in ([self.HEADER + line + b"\r\n"], [self.HEADER + line, b"\r\n"]):  # split between LF and CRLF
                parser = self.parse(*pieces)
                self.assertTrue(parser.completed)
                self.assertIsInstance(parser.error, BadRequest)
                self.assertEqual((parser.error.code, parser.error.body), (400, message))

    def test_max_request_body_size_still_counts_chunked_bodies(self):
        body = b"c8\r\n" + b"z" * 200 + b"\r\n0\r\n\r\n"
        parser = self.parse(self.HEADER + body, adj=Adjustments(max_request_body_size=100))
        self.assertIsInstance(parser.error, RequestEntityTooLarge)


class ChunkedServerTests(ServerTestCase):
    """Chunked requests to a waitress server made as serve makes it."""

    @override_settings(ROOT_URLCONF=__name__, MIDDLEWARE=["apps.core.middleware.RequestSizeLimitMiddleware"])
    def test_ordinary_chunked_requests_reach_django(self):
        self.start(app=utf8_only(WSGIHandler()))  # what serve runs, without the database
        data = [b"a" * 16384, b"b" * 7, b"c" * 65536]
        whole = b"".join(data)
        expected = f"{len(whole)} {len(whole)} {hashlib.sha256(whole).hexdigest()} False".encode()
        # A trailer, an extension and a chunk of 64 KB, sent in one go...
        body = b"".join(b"%x;name=value\r\n%s\r\n" % (len(chunk), chunk) for chunk in data) + b"0\r\nX-Sum: 1\r\n\r\n"
        head = b"POST /echo/ HTTP/1.1\r\nHost: x\r\nConnection: close\r\nTransfer-Encoding: chunked\r\n\r\n"
        answer = self.exchange(head + body)
        self.assertTrue(answer.startswith(b"HTTP/1.1 200 OK"), answer[:200])
        self.assertTrue(answer.endswith(b"\r\n\r\n" + expected), answer[-200:])
        # ...and as http.client sends an iterable: a chunk for each item.
        client = HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(client.close)
        client.request("POST", "/echo/", body=iter(data), headers={"Content-Type": "application/octet-stream"})
        response = client.getresponse()
        self.assertEqual((response.status, response.read()), (200, expected))

    def assertRefused(self, body, message, *more):
        answer = self.exchange(b"POST / HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + body, *more)
        self.assertTrue(answer.startswith(b"HTTP/1.1 400 Bad Request\r\n"), answer[:200])
        self.assertIn(b"\r\nConnection: close\r\n", answer)  # and it was closed: exchange() read to the end
        self.assertIn(message.encode(), answer)
        self.assertTrue(self.get(b"next").startswith(b"HTTP/1.1 200 OK"))  # the server goes on

    def test_a_size_line_too_long_is_400(self):
        self.start()
        self.assertRefused(b"1;" + b"a" * CHUNK_LINE, TOO_LONG_LINE)  # refused with its last byte

    def test_a_trailer_too_long_is_400(self):
        self.start()
        self.assertRefused(b"5\r\nhello\r\n0\r\nX-A: " + b"a" * (TRAILER - 5), TOO_LONG_TRAILER)

    def test_tiny_chunks_are_400(self):
        self.start()
        n = most(lambda n: keeps_to_the_rule(tiny_lines(n)))
        self.assertRefused(tiny_chunks(n + 1)[:-5] + b"1\r\n", TOO_MANY)  # one line more than the rule takes

    def test_a_lf_in_a_size_line_is_400(self):
        self.start()
        for line, message in LF_LINES:
            self.assertRefused(line + b"\r\n", message)  # refused with its last byte
            self.assertRefused(line, message, b"\r\n")  # the LF and the CRLF sent apart

    def test_waitress_s_own_errors_are_still_400(self):
        self.start()
        self.assertRefused(b"5\r\nhello\r\nz\r\n", "Invalid chunk size")


class Counted(bytes):
    """A read that counts the bytes copied out of it (slices, concatenations) and searched in it (find),
    and passes the count on to what is made from it, so copying a copy counts too."""

    copied = searched = 0

    def __getitem__(self, key):
        part = bytes.__getitem__(self, key)
        if not isinstance(key, slice):
            return part
        Counted.copied += len(part)
        return Counted(part)

    def __add__(self, other):
        whole = bytes.__add__(self, other)
        Counted.copied += len(whole)
        return Counted(whole)

    def __radd__(self, other):
        if not isinstance(other, bytes):
            return NotImplemented  # a bytearray += a read: it grows in place, as without Counted
        whole = bytes(other) + bytes(self)
        Counted.copied += len(whole)
        return Counted(whole)

    def find(self, sub, start=0, end=None):
        at = bytes.find(self, sub, start, len(self) if end is None else end)
        Counted.searched += (at + len(sub) if at >= 0 else len(self)) - start
        return at


class ListBuffer:
    """A body buffer that copies nothing it is given."""

    def __init__(self):
        self.parts = []

    def append(self, data):
        self.parts.append(data)

    def __len__(self):
        return sum(map(len, self.parts))


class ChunkedCostTests(SimpleTestCase):
    """received() copies and searches each byte of a read a few times at most, however many chunks the read
    holds and however long ago a size line or the trailer began; waitress copies the rest of a read again
    for every chunk in it, and an unfinished line or trailer again with every read. Counted with Counted
    reads (besides, ours copies the data of a read once more into one piece, and a line or trailer begun
    in earlier reads once more when it ends: at most CHUNK_LINE and TRAILER bytes)."""

    def count(self, receiver_class, pieces):
        """The bytes copied and searched for each read of `pieces`."""
        receiver = receiver_class(ListBuffer())
        counts = []
        for piece in pieces:
            Counted.copied = Counted.searched = 0
            receiver.received(Counted(piece))
            counts.append(Counted.copied + Counted.searched)
        self.assertTrue(receiver.completed or receiver_class is WAITRESS)
        self.assertIsNone(receiver.error)
        return counts

    def test_a_read_of_one_byte_chunks(self):
        read = b"1\r\nx\r\n" * (ADJ.recv_bytes // 6) + b"0\r\n\r\n"  # 8 KB, 1365 chunks
        with mock.patch.object(connections, "FREE_CHUNKS", 10**6):  # parse it all: the rule is tested above
            [ours] = self.count(ChunkedReceiver, [read])
        [theirs] = self.count(WAITRESS, [read])
        self.assertLess(ours, 2 * len(read))
        self.assertGreater(theirs, 1000 * len(read))

    def test_a_long_size_line_and_trailer_a_few_bytes_at_a_time(self):
        line = b"5;a=" + b"b" * (CHUNK_LINE - 4)
        trailer = b"X-Long: " + b"a" * (TRAILER - 12) + b"\r\n\r\n"
        body = line + b"\r\nhello\r\n0\r\n" + trailer
        for size in (1, 3):
            pieces = split(body, *range(size, len(body), size))
            ours, theirs = self.count(ChunkedReceiver, pieces), self.count(WAITRESS, pieces)
            # A few bytes for each read, but the last, which ends the trailer: it copies the trailer once.
            *reads, last = ours
            self.assertLessEqual(max(reads), 16 + 4 * size)
            self.assertLessEqual(last, TRAILER + 16 + 4 * size)
            self.assertGreater(sum(theirs), (len(line) ** 2 + len(trailer) ** 2) // (2 * size))

    def test_one_big_chunk_in_reads_of_8_kb(self):
        data = b"d" * 300_000
        body = b"%x\r\n%s\r\n0\r\n\r\n" % (len(data), data)
        pieces = split(body, *range(ADJ.recv_bytes, len(body), ADJ.recv_bytes))
        ours = self.count(ChunkedReceiver, pieces)
        self.assertLess(sum(ours), 1.01 * len(body))  # the data, copied out once


class WaitressVersionTests(SimpleTestCase):
    def test_waitress_is_the_version_chunked_receiver_follows(self):
        """connections.ChunkedReceiver repeats what waitress 3.0.2's ChunkedReceiver does, and Parser
        replaces it in parse_header(). After an upgrade, compare it with the new receiver.py and
        parser.py, run these tests, and only then put the new version and digests here."""
        self.assertEqual(
            importlib.metadata.version("waitress"), "3.0.2",
            "waitress changed: check apps/installer/connections.py's ChunkedReceiver against it (see this test)",
        )
        for code, digest in (
            (waitress_receiver.ChunkedReceiver, "7d4fd7a3f8c53daae920ae9a109bdf70e46a39423dbffa6ee70ad94df88b9764"),
            (waitress_parser.HTTPRequestParser.parse_header, "c2c093b901782da6cb06c31208e183f4c6e1ac4d624a0ab4190bc02f3d7a13f3"),
            (waitress_parser.HTTPRequestParser.received, "b466c02de37d4a01f563ab66ad29c6b59c9bfd4ca2ba8a34e5480b941f824ce8"),
        ):
            self.assertEqual(
                hashlib.sha256(inspect.getsource(code).encode()).hexdigest(), digest,
                f"waitress's {code.__qualname__} changed: check apps/installer/connections.py against it (see this test)",
            )
