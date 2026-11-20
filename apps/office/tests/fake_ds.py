"""A stand-in OnlyOffice Document Server on 127.0.0.1 (no real one is available for tests).

In the unit tests (FakeDocumentServer, port 0) it serves what a test puts in `files` at /cache/<name>,
the way the real one serves an edited document, and the misbehaviours the callback must survive:
redirects (/redirect?to=, /chain/<n>), a Content-Length over the limit (/big), a body without one
(/stream?size=&delay=, starting like a ZIP), one that drips a byte now and then
(/drip?seconds=&every=), one cut short (/short), any status (/status/<code>), a slow one (/slow?seconds=).
Every request is recorded in `requests`.

Run as a program it is the browser checks' Document Server too:

    python -m apps.office.tests.fake_ds --port 18651 --secret-file <file> [--log <file>] [--probe <origin>]

- /web-apps/apps/api/documents/api.js: a DocsAPI.DocEditor that puts a frame of this server
  (/editor.html) in place of the placeholder and hands it the config, as the real api.js does. With
  --probe it also tries what the editor page's CSP must refuse: a script and a frame of another origin
  and a fetch() to this one; window.__officeProbe records what happened.
- /editor.html posts the config to /simulate (same origin, as the real editor talks to its server).
- /simulate checks the config's token (HS256 with the secret), downloads document.url from the site,
  makes an "edited" document of it (the original bytes and a line of its own) at /cache/<key>.<ext>,
  and calls editorConfig.callbackUrl back: status 1, then 6 (when editing) and 2, each with a token
  (in the body, or in Authorization: Bearer wrapped in "payload", alternately). What happened goes to
  --log as JSON lines. The secret is read from a file and never printed.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

API_JS = r"""
(function () {
  'use strict';
  var me = (document.currentScript && new URL(document.currentScript.src).origin) || '';
  var probe = %(probe)s;
  window.__officeProbe = { violations: [], loaded: [], fetched: null, frames: [] };
  document.addEventListener('securitypolicyviolation', function (e) {
    window.__officeProbe.violations.push({ directive: e.violatedDirective, blocked: e.blockedURI });
  });
  function DocEditor(id, config) {
    var target = document.getElementById(id);
    var frame = document.createElement('iframe');
    frame.name = 'frameEditor';
    frame.title = 'Fake Document Server';
    frame.src = me + '/editor.html';
    frame.width = config.width || '100%%';
    frame.height = config.height || '100%%';
    frame.addEventListener('load', function () {
      frame.contentWindow.postMessage(JSON.stringify(config), me);
      window.__officeProbe.frames.push(frame.src);
    });
    target.parentNode.replaceChild(frame, target);
    window.__officeConfig = config;
    if (probe) {
      var other = document.createElement('script');
      other.src = probe + '/probe.js';
      other.onload = function () { window.__officeProbe.loaded.push(other.src); };
      document.head.appendChild(other);
      var otherFrame = document.createElement('iframe');
      otherFrame.src = probe + '/editor.html';
      otherFrame.className = 'probe-frame';
      document.body.appendChild(otherFrame);
      fetch(me + '/ping').then(function () { window.__officeProbe.fetched = 'allowed'; },
                               function () { window.__officeProbe.fetched = 'blocked'; });
    }
    this.destroyEditor = function () { frame.remove(); };
  }
  window.DocsAPI = { DocEditor: DocEditor };
})();
"""

EDITOR_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Fake Document Server</title></head>
<body><h1 id="title">Fake Document Server</h1><p id="state">waiting for the config</p>
<script src="/editor.js"></script></body></html>
"""

EDITOR_JS = """
window.addEventListener('message', function (event) {
  var config = JSON.parse(event.data);
  document.getElementById('title').textContent = config.document.title + ' (' + config.editorConfig.mode + ')';
  document.getElementById('state').textContent = 'simulating';
  fetch('/simulate', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: event.data })
    .then(function (r) { return r.json(); })
    .then(function (answer) { document.getElementById('state').textContent = 'done: ' + JSON.stringify(answer); });
});
"""


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeDocumentServer/1"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, status, body=b"", content_type="application/octet-stream", headers=None, length=True):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        if length:
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def do_GET(self):  # noqa: N802 - http.server's name
        ds = self.server.ds
        parts = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(parts.query)
        with ds.lock:
            ds.requests.append({"method": "GET", "path": parts.path, "query": parts.query,
                                "headers": dict(self.headers)})
        path = parts.path
        if path.startswith("/cache/"):
            name = urllib.parse.unquote(path[len("/cache/"):])
            with ds.lock:
                data = ds.files.get(name)
            if data is None:
                return self._send(404, b"not found")
            return self._send(200, data)
        if path == "/redirect":
            return self._send(302, headers={"Location": query.get("to", ["/"])[0]})
        if path.startswith("/chain/"):
            left = int(path.rsplit("/", 1)[1])
            target = f"/chain/{left - 1}?{parts.query}" if left > 0 else query.get("end", ["/cache/doc"])[0]
            return self._send(302, headers={"Location": target})
        if path == "/big":
            self.send_response(200)
            self.send_header("Content-Length", str(10**12))
            self.end_headers()
            self.wfile.write(b"x" * 1024)
            self.close_connection = True
            return None
        if path == "/stream":
            size = int(query.get("size", ["0"])[0])
            time.sleep(float(query.get("delay", ["0"])[0]))
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            sent = 0
            piece = b"y" * 65536
            try:
                while sent < size:
                    chunk = piece[:min(len(piece), size - sent)]
                    if sent == 0:
                        chunk = (b"PK\x03\x04" + chunk)[:len(chunk)]  # a ZIP's first bytes, like a .docx
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    sent += len(chunk)
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
            return None
        if path == "/drip":
            # A byte every `every` seconds for `seconds` seconds, in one chunked answer.
            self.send_response(200)
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            ends = time.monotonic() + float(query.get("seconds", ["10"])[0])
            every = float(query.get("every", ["0.2"])[0])
            try:
                self.wfile.write(b"4\r\nPK\x03\x04\r\n")
                self.wfile.flush()
                while time.monotonic() < ends:
                    time.sleep(every)
                    self.wfile.write(b"1\r\nx\r\n")
                    self.wfile.flush()
                self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
            return None
        if path == "/short":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"z" * 10)
            self.wfile.flush()
            self.close_connection = True
            return None
        if path.startswith("/status/"):
            return self._send(int(path.rsplit("/", 1)[1]), b"status")
        if path == "/slow":
            time.sleep(float(query.get("seconds", ["1"])[0]))
            with ds.lock:
                data = ds.files.get(query.get("name", ["doc"])[0], b"slow")
            return self._send(200, data)
        if path == "/web-apps/apps/api/documents/api.js":
            body = (API_JS % {"probe": json.dumps(ds.probe or "")}).encode()
            return self._send(200, body, "text/javascript; charset=utf-8")
        if path == "/editor.html":
            return self._send(200, EDITOR_HTML.encode(), "text/html; charset=utf-8")
        if path == "/editor.js":
            return self._send(200, EDITOR_JS.encode(), "text/javascript; charset=utf-8")
        if path == "/probe.js":
            return self._send(200, b"window.__probeRan = true;", "text/javascript; charset=utf-8")
        if path == "/ping":
            return self._send(200, b"pong", "text/plain", {"Access-Control-Allow-Origin": "*"})
        return self._send(404, b"not found")

    def do_HEAD(self):  # noqa: N802
        return self.do_GET()

    def do_POST(self):  # noqa: N802
        ds = self.server.ds
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        with ds.lock:
            ds.requests.append({"method": "POST", "path": self.path, "headers": dict(self.headers)})
        if self.path == "/simulate" and ds.secret is not None:
            try:
                result = ds.simulate(json.loads(body))
            except Exception as error:  # noqa: BLE001 - reported to the page and the log
                result = {"failed": type(error).__name__, "message": str(error)[:300]}
            ds.log(result)
            return self._send(200, json.dumps(result).encode(), "application/json")
        return self._send(404, b"not found")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # a client that hung up on purpose (the size and redirect tests)


class FakeDocumentServer:
    def __init__(self, port: int = 0, *, secret: str | None = None, probe: str = "", log_path: str | None = None):
        self.files: dict[str, bytes] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self.secret = secret
        self.probe = probe
        self.log_path = log_path
        self.rounds = 0
        self.httpd = _Server(("127.0.0.1", port), _Handler)
        self.httpd.ds = self
        self.thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def start(self) -> FakeDocumentServer:
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="fake-document-server", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def paths(self) -> list[str]:
        with self.lock:
            return [request["path"] for request in self.requests]

    def log(self, entry: dict) -> None:
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as out:
                out.write(json.dumps({"at": time.time(), **entry}, ensure_ascii=False) + "\n")

    # -- the browser checks' editing session

    def _post(self, url: str, payload: dict, *, header: bool) -> dict:
        import jwt

        body = dict(payload)
        headers = {"Content-Type": "application/json"}
        expires = int(time.time()) + 300  # the Document Server signs its callbacks for five minutes
        if header:
            headers["Authorization"] = "Bearer " + jwt.encode({"payload": payload, "exp": expires}, self.secret,
                                                              algorithm="HS256")
        else:
            body["token"] = jwt.encode({**payload, "exp": expires}, self.secret, algorithm="HS256")
        request = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=60) as answer:
            return json.loads(answer.read())

    def simulate(self, config: dict) -> dict:
        import jwt

        token = config.pop("token", None)
        claims = jwt.decode(token, self.secret, algorithms=["HS256"]) if token else None
        if claims is None or claims.get("document") != config.get("document"):
            return {"failed": "the config's token doesn't match the config"}
        document, editor = claims["document"], claims["editorConfig"]
        with urllib.request.urlopen(document["url"], timeout=30) as answer:
            original = answer.read()
        key, ext = document["key"], document["fileType"]
        result = {"key": key, "title": document["title"], "mode": editor.get("mode"),
                  "downloaded": len(original), "callbacks": []}
        user = editor.get("user", {}).get("id", "anonymous")
        result["callbacks"].append(self._post(editor["callbackUrl"], {"key": key, "status": 1, "users": [user]},
                                              header=False))
        if editor.get("mode") != "edit":
            result["callbacks"].append(self._post(editor["callbackUrl"], {"key": key, "status": 4}, header=True))
            return result
        self.rounds += 1
        for status, header in ((6, True), (2, False)):
            name = f"{key}-{status}.{ext}"
            edited = original + f"\n[edited by the fake Document Server, round {self.rounds}, status {status}]\n".encode()
            with self.lock:
                self.files[name] = edited
            payload = {"key": key, "status": status, "url": f"{self.url}/cache/{urllib.parse.quote(name)}",
                       "filetype": ext, "users": [user]}
            result["callbacks"].append(self._post(editor["callbackUrl"], payload, header=header))
        result["saved"] = len(edited)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="A fake OnlyOffice Document Server for HFile's checks")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--secret-file", required=True)
    parser.add_argument("--log")
    parser.add_argument("--probe", default="", help="another origin the api.js stub tries (CSP check)")
    args = parser.parse_args()
    with open(args.secret_file, encoding="utf-8") as source:
        secret = source.read().strip()
    server = FakeDocumentServer(args.port, secret=secret, probe=args.probe, log_path=args.log)
    print(f"fake Document Server on {server.url}", flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()


if __name__ == "__main__":
    main()
