"""A small threaded HTTP server for the in-memory fakes of cloud APIs (tests only, no Django).

    server = FakeServer(handle)      # handle(Request) -> Response, called in the server's threads
    server.start(); server.url       # "http://127.0.0.1:<port>"
    server.connections               # the TCP connections accepted so far
    with server.intercepting(answer): ...  # answer(Request) -> Response, or None for the fake's own
    server.close()

The handler raises HTTPError(status, body) or returns a Response; any other exception becomes a
500 with the traceback in the body, so a test failure shows where the fake broke.
"""

from __future__ import annotations

import contextlib
import json
import threading
import traceback
import urllib.parse
from collections.abc import Callable, Iterable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Request:
    def __init__(self, method: str, target: str, headers: Any, body: bytes, server_url: str):
        self.method = method
        self.target = target
        split = urllib.parse.urlsplit(target)
        self.path = split.path  # still percent-encoded
        self.raw_query = split.query
        self.query = urllib.parse.parse_qs(split.query, keep_blank_values=True)
        self.headers = headers  # email.message.Message: get() ignores case
        self.body = body
        self.server_url = server_url

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name, default)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8")) if self.body else None

    def form(self) -> dict[str, str]:
        """An application/x-www-form-urlencoded body (one value per name)."""
        pairs = urllib.parse.parse_qsl(self.body.decode("utf-8"), keep_blank_values=True)
        return dict(pairs)

    def bearer(self) -> str | None:
        value = self.header("Authorization") or ""
        return value[7:] if value.startswith("Bearer ") else None


class Response:
    # status 0: the server closes the connection without answering (a lost answer).
    def __init__(self, status: int = 200, body: bytes | Iterable[bytes] = b"",
                 headers: dict[str, str] | list[tuple[str, str]] | None = None,
                 length: int | None = None):
        self.status = status
        self.body = body
        self.headers = list(headers.items()) if isinstance(headers, dict) else list(headers or [])
        self.length = len(body) if isinstance(body, (bytes, bytearray)) else length


class HTTPError(Exception):
    def __init__(self, status: int, body: Any = b"", headers: dict[str, str] | None = None):
        super().__init__(status)
        self.response = body if isinstance(body, Response) else (
            json_response(body, status, headers) if isinstance(body, (dict, list)) else
            Response(status, body if isinstance(body, bytes) else str(body).encode(), headers))


def json_response(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    all_headers = {"Content-Type": "application/json; charset=utf-8"}
    all_headers.update(headers or {})
    return Response(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), all_headers)


LONG = 64 * 1024 * 1024  # bytes of a LongBody


class LongBody:
    """An answer's body of `words`, then spaces, LONG bytes in all (length=LONG), which counts in `sent` what the
    server got to send, every time it is sent: the server stops when the client goes away. For a test that an
    error's body isn't read whole (http.read_error())."""

    def __init__(self, words: bytes):
        self.words = words
        self.sent = 0

    def __iter__(self) -> Iterator[bytes]:
        piece, left = self.words.ljust(64 * 1024), LONG
        while left > 0:
            self.sent += len(piece)
            left -= len(piece)
            yield piece
            piece = b" " * (64 * 1024)


def byte_range(header: str | None, total: int) -> tuple[int, int] | None:
    """(start, end inclusive) of a "bytes=a-b" Range header, None for none; HTTPError 416 if bad."""
    if not header:
        return None
    unit, _, spec = header.partition("=")
    if unit.strip() != "bytes" or "," in spec:
        raise HTTPError(416, b"bad range")
    first, _, last = spec.strip().partition("-")
    if first == "":
        if not last.isdigit():
            raise HTTPError(416, b"bad range")
        start, end = max(total - int(last), 0), total - 1
    else:
        if not first.isdigit() or (last and not last.isdigit()):
            raise HTTPError(416, b"bad range")
        start = int(first)
        end = min(int(last), total - 1) if last else total - 1
    if start >= total or start > end:
        raise HTTPError(416, b"range not satisfiable", {"Content-Range": f"bytes */{total}"})
    return start, end


def file_response(data: bytes, range_header: str | None, headers: dict[str, str] | None = None) -> Response:
    """200 with the whole file or 206 with the asked range; streamed in pieces like a CDN."""
    all_headers = {"Content-Type": "application/octet-stream", "Accept-Ranges": "bytes"}
    all_headers.update(headers or {})
    span = byte_range(range_header, len(data))
    status = 200
    if span is not None:
        start, end = span
        all_headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
        data = data[start:end + 1]
        status = 206

    def pieces():
        for i in range(0, len(data), 64 * 1024):
            yield data[i:i + 64 * 1024]

    return Response(status, pieces(), all_headers, length=len(data))


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    connections = 0

    def process_request(self, request, client_address):
        self.connections += 1  # in the one thread that accepts them
        super().process_request(request, client_address)


class FakeServer:
    def __init__(self, handle: Callable[[Request], Response]):
        self.handle = handle
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            # Headers and body are written separately: without TCP_NODELAY, Nagle and delayed ACKs
            # cost about 40 ms a request.
            disable_nagle_algorithm = True

            def log_message(self, *args):  # quiet
                pass

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
                    # The client went away while this waited for its next request, with an answer it had stopped
                    # reading (a download closed early) still unread: a server sees the connection go, says nothing.
                    self.close_connection = True

            def _read_body(self) -> bytes:
                if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
                    parts = []
                    while True:
                        line = self.rfile.readline().strip()
                        size = int(line.split(b";")[0], 16)
                        if size == 0:
                            while self.rfile.readline().strip():
                                pass
                            break
                        parts.append(self.rfile.read(size))
                        self.rfile.readline()
                    return b"".join(parts)
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def _serve(self):
                try:
                    body = self._read_body()
                except (OSError, ValueError):
                    self.close_connection = True
                    return
                request = Request(self.command, self.path, self.headers, body, server.url)
                try:
                    response = server.handle(request)
                except HTTPError as error:
                    response = error.response
                except Exception:  # a bug in the fake: show it in the test's failure
                    response = Response(500, traceback.format_exc().encode(), {"Content-Type": "text/plain"})
                if response.status == 0:  # a connection that breaks before the answer
                    self.close_connection = True
                    return
                try:
                    self.send_response(response.status)
                    names = {name.lower() for name, _ in response.headers}
                    chunked = any(name.lower() == "transfer-encoding" and "chunked" in value.lower()
                                  for name, value in response.headers)
                    for name, value in response.headers:
                        self.send_header(name, value)
                    if "content-length" not in names and not chunked:
                        self.send_header("Content-Length", str(response.length or 0))
                    self.end_headers()
                    if self.command != "HEAD":
                        pieces = [response.body] if isinstance(response.body, (bytes, bytearray)) else response.body
                        for piece in pieces:
                            if chunked:
                                if piece:
                                    self.wfile.write(b"%x\r\n%s\r\n" % (len(piece), piece))
                            else:
                                self.wfile.write(piece)
                        if chunked:
                            self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    self.close_connection = True

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _serve

        self._httpd = _Server(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._httpd.server_port}"
        self._thread: threading.Thread | None = None

    @property
    def connections(self) -> int:
        """The TCP connections accepted so far: whether a client used one again."""
        return self._httpd.connections

    @contextlib.contextmanager
    def intercepting(self, answer: Callable[[Request], Response | None]) -> Iterator[None]:
        """In the block, a request answer() returns a Response for gets that one instead of the fake's."""
        handle = self.handle
        self.handle = lambda request: answer(request) or handle(request)
        try:
            yield
        finally:
            self.handle = handle

    def start(self) -> FakeServer:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="fake-cloud-http", daemon=True)
        self._thread.start()
        return self

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(5)
