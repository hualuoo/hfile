"""Fake HTTP APIs for driver tests: a ThreadingHTTPServer on 127.0.0.1, a random port."""

from __future__ import annotations

import http.server
import json
import threading
from typing import Any


class Handler(http.server.BaseHTTPRequestHandler):
    """Base request handler: quiet, HTTP/1.1, with helpers to answer."""

    protocol_version = "HTTP/1.1"
    # Headers and body go out in separate writes; without TCP_NODELAY each answer waits for the
    # client's delayed ACK (40 ms).
    disable_nagle_algorithm = True
    fake: Any = None  # the fake this server belongs to, set by Server()

    def log_message(self, format, *args):  # noqa: A002 - keep the test output clean
        pass

    def body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""

    def answer(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if status != 204 and (self.command != "HEAD" or "Content-Length" not in (headers or {})):
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def answer_json(self, status: int, value: Any, headers: dict[str, str] | None = None) -> None:
        self.answer(status, json.dumps(value, ensure_ascii=False).encode(),
                    {"Content-Type": "application/json", **(headers or {})})


class _QuietServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    connections = 0

    def process_request(self, request, client_address):
        self.connections += 1  # in the one thread that accepts them
        super().process_request(request, client_address)

    def handle_error(self, request, client_address):
        pass  # clients that hang up halfway (tests of early closes) are expected


class Server:
    """A fake API running in a thread; `url` is http://127.0.0.1:<port>."""

    def __init__(self, handler: type[Handler], fake: Any):
        handler_class = type(handler.__name__, (handler,), {"fake": fake})
        self.httpd = _QuietServer(("127.0.0.1", 0), handler_class)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def connections(self) -> int:
        """The TCP connections accepted so far: whether a client used one again."""
        return self.httpd.connections

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join()
