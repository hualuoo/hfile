"""What waitress doesn't do for a site that faces its visitors directly (no Caddy or Nginx in front:
README 运行 says to put one there on the internet; this is for a LAN, and for the moments without).

waitress closes a connection only when it has sent nothing for channel_timeout seconds. One that sends
a byte every few seconds stays open, and connection_limit (1000) of them shut everybody else out. So
serve (apps/installer/management/commands/serve.py) adds, through guard():

- the header of a request must be complete HEADER_SECONDS after its first byte;
- its body must then come at BODY_RATE bytes a second on average, after BODY_GRACE seconds: the
  slowest phone line sends far more, a body sent a byte at a time does not;
- with [server] proxies = 0 (every address a visitor), one address may hold at most PER_ADDRESS
  connections, an IPv6 address counted by its /64 as the rate limits do (apps/core/throttles.py). Behind
  a proxy every connection comes from the proxy, so there it would shut out the whole site.

A connection that breaks a rule is closed without an answer, as waitress closes an idle one, and the
console says how many once a minute at most. The rules are checked every CHECK_EVERY seconds (waitress's
maintenance), so a connection may last that much longer.

A chunked body (Transfer-Encoding: chunked) costs waitress 3.0.2 far more than its bytes, on the one
thread that reads every connection, before anyone has logged in: its ChunkedReceiver has no limit on a
chunk-size line or the trailer and joins what it has of them to every new read, so one long line up to
max_request_body_size (128 MB) means ~1e12 bytes copied; and it copies the rest of a read for every
chunk in it, so a read of one-byte chunks costs nearly a thousand times a plain one. Parser reads chunked
bodies with ChunkedReceiver below: waitress's rules (but a size line ending in a LF, which waitress lets
through), in time linear in the bytes, and a 400 (as for waitress's own errors) for a size line over
CHUNK_LINE bytes, a trailer over TRAILER, or more size lines than FREE_CHUNKS plus one for every
CHUNK_DATA bytes of data (a long line counting as several).
"""

import functools
import logging
import time

from waitress.channel import HTTPChannel
from waitress.parser import HTTPRequestParser
from waitress.rfc7230 import CHUNK_EXT_RE, ONLY_HEXDIG_RE
from waitress.server import BaseWSGIServer, MultiSocketServer
from waitress.utilities import BadRequest

from django.utils.translation import gettext as _, ngettext

from apps.core.throttles import address_key

HEADER_SECONDS = 30
BODY_GRACE = 30
BODY_RATE = 1024  # bytes a second
PER_ADDRESS = 100  # a tenth of serve's connection_limit
CHECK_EVERY = 5  # seconds; waitress's cleanup_interval
REPORT_EVERY = 60  # seconds between two lines about closed connections

# Chunked bodies. A chunk-size line is a hex number and maybe extensions, which nobody sends long (curl
# and requests send none); the trailer is a few header lines at most.
CHUNK_LINE = 4 * 1024  # bytes in a size line, its extensions included (its CRLF not)
TRAILER = 8 * 1024  # bytes of trailer, the empty line that ends it included
# A size line costs Python about what a KB of a plain body does, whatever the size of its chunk, and the
# check of its extensions as much again for every 32 bytes. So a body may have FREE_CHUNKS size lines,
# and one more for every CHUNK_DATA bytes of data in the chunks before it, a line counting once more for
# every LINE_BYTES bytes in it. Tiny chunks or long extensions then cost a few times a plain body at most
# (a large one, which waitress writes to a file, about 1.3 times), and chunks of a KB or more with short
# extensions never meet the limit: curl and requests send several KB and no extensions. Go's net/http
# refuses chunked bodies with "too much non-data" alike.
FREE_CHUNKS = 1024
CHUNK_DATA = 1024
LINE_BYTES = 32

logger = logging.getLogger("hfile")


class Parser(HTTPRequestParser):
    """waitress's request parser, noting when the request began and when its header was complete, and
    reading a chunked body with ChunkedReceiver below."""

    def __init__(self, adj):
        super().__init__(adj)
        self.begun = time.monotonic()  # the channel makes one when the request's first bytes arrive
        self.header_done = None

    def received(self, data):
        consumed = super().received(data)
        if self.headers_finished and self.header_done is None:
            self.header_done = time.monotonic()
        return consumed

    def parse_header(self, header_plus):
        super().parse_header(header_plus)
        if self.chunked:  # waitress made its ChunkedReceiver: ours instead, on the buffer it made
            self.body_rcv = ChunkedReceiver(self.body_rcv.getbuf())


def _find(kept, s, i, mark):
    """Where `mark` ends in kept + s[i:], as an index into s, or -1. `kept`, a line or trailer begun in
    earlier reads, doesn't hold `mark`: only its last bytes are looked at again (`mark` may begin there)."""
    if kept:
        tail = bytes(kept[1 - len(mark):])
        at = (tail + s[i:i + len(mark) - 1]).find(mark)
        if at >= 0:
            return i + at + len(mark) - len(tail)
    at = s.find(mark, i)
    return -1 if at < 0 else at + len(mark)


class ChunkedReceiver:
    """A chunked body, read as waitress 3.0.2's waitress.receiver.ChunkedReceiver reads it: the same
    bytes accepted, the same errors, the same counts consumed, body and trailer, however the bytes are
    split across reads (test_connections.py compares the two). The differences:

    - received() takes time linear in what it's given: an index moves along the read instead of the
      rest being copied for every chunk, a size line or trailer begun in an earlier read waits in a
      bytearray instead of being joined to every read, and a read's data goes to the buffer at once;
    - the limits above, refused with a BadRequest (400, connection closed) as waitress's errors are;
    - a size line with a LF in it is refused, with waitress's own "Invalid chunk size" or "Invalid chunk
      extension": waitress checks the size and the extensions with match, and the $ of its regexes lets
      a LF through at their end (5, a LF, then the CRLF is a chunk of 5 bytes to it), where a proxy in
      front may end the line and read the rest of the body otherwise. fullmatch only refuses more;
    - once it has an error, received() answers 0, as at the end of the body: the parser asks no more then.

    The branches of received() are waitress's, in its order, with its names.
    """

    chunk_remainder = 0  # bytes of the current chunk's data still to come
    validate_chunk_end = False  # the CRLF after a chunk's data comes next
    chunk_end = b""  # its first byte, when a read ended there
    all_chunks_received = False  # the last chunk (size 0) has come: the trailer is next
    completed = False
    error = None

    def __init__(self, buf):
        self.buf = buf  # waitress's OverflowableBuffer, which becomes wsgi.input
        self.control_line = bytearray()  # a size line a read ended in
        self.trailer = bytearray()  # the trailer so far; once complete, what waitress keeps of it
        self.lines = 0  # size lines so far as FREE_CHUNKS counts them, the empty ones waitress skips included
        self.data = 0  # bytes of chunk data so far

    def __len__(self):
        return self.buf.__len__()

    def getfile(self):
        return self.buf.getfile()

    def getbuf(self):
        return self.buf

    def _refuse(self, message):
        self.error = BadRequest(message)

    def received(self, s):
        """Reads `s`, what the connection received next; returns how many of its bytes are this body's."""
        if self.completed or self.error:
            return 0
        n = len(s)
        i = 0  # s[:i] is read
        consumed = n
        data = []  # chunk data in s
        while i < n:
            if self.chunk_remainder > 0:
                end = min(n, i + self.chunk_remainder)
                data.append(s[i:end])
                self.chunk_remainder -= end - i
                self.data += end - i
                i = end
                if self.chunk_remainder == 0:
                    self.validate_chunk_end = True
            elif self.validate_chunk_end:
                kept = self.chunk_end
                head = kept + s[i:i + 2 - len(kept)]
                if len(head) < 2:  # the read ended after the first byte
                    self.chunk_end = head
                    i = n
                    continue
                self.chunk_end = b""
                self.validate_chunk_end = False
                if head == b"\r\n":
                    i += 2 - len(kept)
                else:
                    self.error = BadRequest("Chunk not properly terminated")
                    self.all_chunks_received = True
                    # waitress reads on, what follows as the trailer (the kept byte included): it
                    # decides how many bytes were consumed.
                    self.trailer += kept
            elif not self.all_chunks_received:
                end = _find(self.control_line, s, i, b"\r\n")
                # The line's length; before its CRLF has come, at least all that is here but the last
                # byte (a CR that may begin the CRLF).
                length = len(self.control_line) + (end - 2 if end >= 0 else n - 1) - i
                if length > CHUNK_LINE:
                    self._refuse(ngettext("A chunk-size line of the chunked request body (extensions included) is over %(limit)s byte.",
                                          "A chunk-size line of the chunked request body (extensions included) is over %(limit)s bytes.", CHUNK_LINE) % {"limit": CHUNK_LINE})
                    break
                if end < 0:  # the line goes on in the next read
                    self.control_line += s[i:]
                    i = n
                    continue
                if self.control_line:
                    self.control_line += s[i:end]
                    line = bytes(self.control_line[:-2])
                    self.control_line = bytearray()
                else:
                    line = s[i:end - 2]
                i = end
                self.lines += 1 + len(line) // LINE_BYTES
                if self.lines > FREE_CHUNKS + self.data // CHUNK_DATA:
                    self._refuse(
                        _("Too many chunks in the chunked request body: after the first "
                          "%(free)s, a chunk must carry %(data)s bytes of data on average (a "
                          "size line counts as one more chunk for every %(line)s bytes in it).") % {"free": FREE_CHUNKS, "data": CHUNK_DATA, "line": LINE_BYTES}
                    )
                    break
                if not line:  # waitress skips empty lines
                    continue
                # fullmatch where waitress has match, whose $ lets a LF through at the end (see above)
                semi = line.find(b";")
                if semi >= 0:
                    if not CHUNK_EXT_RE.fullmatch(line[semi:]):
                        self.error = BadRequest("Invalid chunk extension")
                        self.all_chunks_received = True
                        break
                    line = line[:semi]
                if not ONLY_HEXDIG_RE.fullmatch(line):
                    self.error = BadRequest("Invalid chunk size")
                    self.all_chunks_received = True
                    break
                size = int(line, 16)
                if size > 0:
                    self.chunk_remainder = size
                else:
                    self.all_chunks_received = True
            else:
                held = len(self.trailer)
                if (bytes(self.trailer[:2]) + s[i:i + 2]).startswith(b"\r\n"):  # no trailer
                    self.completed = True  # waitress keeps self.trailer as it was (b"" or a CR)
                    consumed = i + 2 - held
                    break
                end = _find(self.trailer, s, i, b"\r\n\r\n")
                # Its length; before its end has come, at least a byte more than what is here. After
                # "Chunk not properly terminated" waitress reads the rest of that read as a trailer: no
                # limit there, it's that one read (the parser asks no more).
                length = held + (end if end >= 0 else n + 1) - i
                if length > TRAILER and self.error is None:
                    self._refuse(ngettext("The trailer of the chunked request body is over %(limit)s byte.",
                                          "The trailer of the chunked request body is over %(limit)s bytes.", TRAILER) % {"limit": TRAILER})
                    break
                if end < 0:
                    self.trailer += s[i:]
                    i = n
                else:
                    self.trailer = bytes(self.trailer) + s[i:end]
                    self.completed = True
                    consumed = end
                    break
        if data:
            self.buf.append(b"".join(data))
        return consumed


class Channel(HTTPChannel):
    parser_class = Parser
    answered = 0.0  # when this connection's last request was answered: no deadline runs meanwhile

    def service(self):
        try:
            super().service()
        finally:
            self.answered = time.monotonic()

    def too_slow(self, now):
        """Whether the request being received is behind its deadline. None being received: False."""
        request = self.request
        if request is None or request.completed:
            return False
        if not request.headers_finished:
            return now - max(request.begun, self.answered) > HEADER_SECONDS
        since = max(request.header_done or request.begun, self.answered)
        return now - since > BODY_GRACE + request.body_bytes_received / BODY_RATE


class _Report:
    """Counts the connections closed for being slow and those refused for their address, and says so
    in the console at most once every REPORT_EVERY seconds."""

    def __init__(self):
        self.slow = self.crowded = 0
        self.next = 0.0

    def flush(self, now):
        if not (self.slow or self.crowded) or now < self.next:
            return
        # A line each, the advice in both: two sentences in one line would need a separator of their own.
        if self.slow:
            logger.warning(ngettext(
                "Closed %(count)s request that was too slow (headers not complete within "
                "%(seconds)s seconds, or a body slower than %(rate)s KB a second). A site "
                "that faces the internet directly is better off with Caddy or Nginx in front.",
                "Closed %(count)s requests that were too slow (headers not complete within "
                "%(seconds)s seconds, or a body slower than %(rate)s KB a second). A site "
                "that faces the internet directly is better off with Caddy or Nginx in front.",
                self.slow,
            ) % {"count": self.slow, "seconds": HEADER_SECONDS, "rate": f"{BODY_RATE / 1024:g}"})
        if self.crowded:
            logger.warning(ngettext(
                "Refused %(count)s connection (one address may hold %(limit)s at a time). A site "
                "that faces the internet directly is better off with Caddy or Nginx in front.",
                "Refused %(count)s connections (one address may hold %(limit)s at a time). A site "
                "that faces the internet directly is better off with Caddy or Nginx in front.",
                self.crowded,
            ) % {"count": self.crowded, "limit": PER_ADDRESS})
        self.slow = self.crowded = 0
        self.next = now + REPORT_EVERY


def _key(addr):
    """What PER_ADDRESS counts for a connection from `addr`; None for a Unix socket's."""
    if isinstance(addr, tuple) and addr:
        return address_key(addr[0])
    return None


def _channel_maker(per_address, report):
    """The server's channel_class: a Channel, unless the address has PER_ADDRESS connections already."""

    def make(server, sock, addr, adj, map=None):
        if per_address and (key := _key(addr)) is not None:
            held = sum(1 for channel in server.active_channels.values() if _key(channel.addr) == key)
            if held >= per_address:
                sock.close()
                report.crowded += 1
                return None
        return Channel(server, sock, addr, adj, map=map)

    return make


def _maintenance(server, report, now):
    """waitress's maintenance (connections idle for channel_timeout), and the deadlines above."""
    cutoff = now - server.adj.channel_timeout
    clock = time.monotonic()
    for channel in list(server.active_channels.values()):
        if channel.requests:  # being answered
            continue
        if channel.last_activity < cutoff:
            channel.will_close = True
        elif isinstance(channel, Channel) and channel.too_slow(clock):
            channel.will_close = True
            report.slow += 1
    report.flush(clock)


def guard(server, per_address):
    """Puts the rules above into `server`, made by waitress's create_server (with cleanup_interval =
    CHECK_EVERY). `per_address`: PER_ADDRESS, or 0 for no limit per address (behind a proxy)."""
    if isinstance(server, MultiSocketServer):  # a host name with several addresses: one server each
        servers = [each for each in server.map.values() if isinstance(each, BaseWSGIServer)]
    else:
        servers = [server]
    report = _Report()
    for each in servers:
        each.channel_class = _channel_maker(per_address, report)
        each.maintenance = functools.partial(_maintenance, each, report)
