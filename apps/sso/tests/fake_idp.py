"""A fake OAuth 2 / OpenID Connect provider on 127.0.0.1, for the tests and for trying single sign-on in a
browser (no real provider account needed):

    python -m apps.sso.tests.fake_idp --port 18351 --client-id hfile --client-secret s3cret [--basic]
        [--sub alice-1 --login alice --name 爱丽丝]

    GET  /authorize    checks client_id, response_type, redirect_uri and the PKCE challenge (S256), then
                       sends the browser back to redirect_uri with ?code=&state= (as if the person agreed),
                       or with ?error= when the query has deny=1
    POST /token        checks the code (once only, 10 minutes), the redirect_uri it was given for, the
                       PKCE verifier, and the client's credentials: in the form (post) or HTTP Basic
                       (basic), never both; answers {"access_token", "token_type": "Bearer"}
    GET  /userinfo     Bearer token -> the user's claims
    GET  /.well-known/openid-configuration

Tests change `behaviour` (FakeIdp.behave()) to make it fail in the ways a real one can: a status, a
token_type, an error in a 200, a body over 1 MiB, a redirect, a slow answer, gzip, not JSON.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlencode, urlsplit


class FakeIdp:
    """The provider's state; serve() runs it in a thread of its own."""

    def __init__(self, *, client_id="hfile-client", client_secret="hfile-secret", basic=False, claims=None):
        self.client_id = client_id
        self.client_secret = client_secret
        self.basic = basic  # which client authentication the provider accepts (one only, as Logto's)
        self.claims = claims if claims is not None else {"sub": "alice-1", "preferred_username": "alice", "name": "爱丽丝"}
        self.codes = {}  # code -> {redirect_uri, challenge, until, claims}
        self.tokens = {}  # access token -> claims
        self.behaviour = {}
        self.requests = []  # (method, path) of every request, to see what the site asked
        self.token_forms = []  # the token requests' forms (to check what was sent)
        self.lock = threading.Lock()
        self.server = None

    # --- running

    def serve(self, port=0):
        idp = self

        class Handler(_Handler):
            fake = idp

        self.server = _Server(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def behave(self, **behaviour):
        """How the next requests are answered: status_token, status_userinfo (an HTTP status), token_type,
        token_error (an error code in a 200), big (the token answer over 1 MiB), redirect_userinfo,
        slow (seconds before answering), gzip, not_json, not_object, claims=..."""
        with self.lock:
            self.behaviour = behaviour

    def reset(self):
        with self.lock:
            self.codes.clear()
            self.tokens.clear()
            self.behaviour = {}
            self.requests.clear()
            self.token_forms.clear()

    # --- the authorization step, as a browser would go through it

    def approve(self, authorize_url, claims=None):
        """What the provider's page does when the person agrees: a code for this request, and the address
        the browser is sent back to (redirect_uri?code=&state=). Checks the request as /authorize does."""
        query = parse_qs(urlsplit(authorize_url).query)
        one = {key: values[-1] for key, values in query.items()}
        problem = self.check_authorization(one)
        if problem:
            raise AssertionError(problem)
        code = secrets.token_urlsafe(24)
        with self.lock:
            self.codes[code] = {
                "redirect_uri": one["redirect_uri"], "challenge": one["code_challenge"], "until": time.time() + 600,
                "claims": dict(claims if claims is not None else self.claims),
            }
        return f"{one['redirect_uri']}?{urlencode({'code': code, 'state': one['state']})}"

    def check_authorization(self, one):
        if one.get("response_type") != "code":
            return "response_type is not code"
        if one.get("client_id") != self.client_id:
            return "unknown client_id"
        if not one.get("redirect_uri", "").startswith(("http://", "https://")):
            return "no redirect_uri"
        if one.get("code_challenge_method") != "S256" or not one.get("code_challenge"):
            return "no PKCE S256 challenge"
        if not one.get("state"):
            return "no state"
        return ""

    # --- the token endpoint

    def token(self, form, authorization):
        """(status, answer) for a token request."""
        self.token_forms.append(dict(form))
        basic = None
        if authorization.startswith("Basic "):
            try:
                user, _, password = base64.b64decode(authorization[6:]).decode("utf-8").partition(":")
                basic = (unquote(user), unquote(password))
            except (ValueError, UnicodeError):
                return 401, {"error": "invalid_client"}
        if basic and "client_secret" in form:
            return 400, {"error": "invalid_request", "error_description": "client authentication twice"}
        if self.basic:
            if basic != (self.client_id, self.client_secret):
                return 401, {"error": "invalid_client"}
        elif self.client_secret:
            if basic is not None or form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret:
                return 401, {"error": "invalid_client"}
        elif form.get("client_id") != self.client_id:  # a public client: the id only
            return 401, {"error": "invalid_client"}
        if form.get("grant_type") != "authorization_code":
            return 400, {"error": "unsupported_grant_type"}
        with self.lock:
            grant = self.codes.pop(form.get("code", ""), None)
        if grant is None or grant["until"] < time.time():
            return 400, {"error": "invalid_grant"}
        if form.get("redirect_uri") != grant["redirect_uri"]:
            return 400, {"error": "invalid_grant", "error_description": "redirect_uri mismatch"}
        verifier = form.get("code_verifier", "")
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()
        if not secrets.compare_digest(challenge, grant["challenge"]):
            return 400, {"error": "invalid_grant", "error_description": "PKCE verification failed"}
        access = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[access] = grant["claims"]
        return 200, {"access_token": access, "token_type": "Bearer", "expires_in": 3600, "scope": "openid profile"}

    def configuration(self):
        return {
            "issuer": self.base,
            "authorization_endpoint": f"{self.base}/authorize",
            "token_endpoint": f"{self.base}/token",
            "userinfo_endpoint": f"{self.base}/userinfo",
            "scopes_supported": ["openid", "profile", "email", "bad scope \"x\""],
            "token_endpoint_auth_methods_supported": ["client_secret_basic" if self.basic else "client_secret_post"],
        }


class _Server(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        pass  # the site hangs up on purpose (an answer over 1 MiB, a timeout): not worth a traceback


class _Handler(BaseHTTPRequestHandler):
    fake: FakeIdp = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # quiet
        pass

    def _send(self, status, body=b"", content_type="application/json", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, data, headers=None):
        behaviour = self.fake.behaviour
        if behaviour.get("not_json"):
            return self._send(status, b"<html>maintenance</html>", "text/html")
        if behaviour.get("not_object"):
            data = [data]
        body = json.dumps(data).encode("utf-8")
        if behaviour.get("gzip"):
            return self._send(status, gzip.compress(body), headers={"Content-Encoding": "gzip", **(headers or {})})
        return self._send(status, body, headers=headers)

    def do_GET(self):
        fake = self.fake
        parts = urlsplit(self.path)
        fake.requests.append(("GET", parts.path))
        behaviour = fake.behaviour
        if parts.path == "/authorize":
            one = {key: values[-1] for key, values in parse_qs(parts.query).items()}
            problem = fake.check_authorization(one)
            if problem:
                return self._send(400, problem.encode(), "text/plain")
            if one.get("deny"):
                target = f"{one['redirect_uri']}?{urlencode({'error': 'access_denied', 'state': one['state']})}"
            else:
                target = fake.approve(f"/authorize?{parts.query}")
            return self._send(302, b"", "text/plain", {"Location": target})
        if parts.path == "/.well-known/openid-configuration":
            return self._json(behaviour.get("status_discovery", 200), fake.configuration())
        if parts.path == "/userinfo":
            if behaviour.get("slow"):
                time.sleep(behaviour["slow"])
            if behaviour.get("redirect_userinfo"):
                return self._send(302, b"", "text/plain", {"Location": f"{fake.base}/elsewhere"})
            if "status_userinfo" in behaviour:
                return self._json(behaviour["status_userinfo"], {"message": "token=secret-in-body"})
            token = self.headers.get("Authorization", "")
            claims = fake.tokens.get(token[7:]) if token.startswith("Bearer ") else None
            if claims is None:
                return self._json(401, {"error": "invalid_token"})
            return self._json(200, behaviour.get("claims", claims))
        if parts.path == "/elsewhere":
            return self._json(200, {"sub": "redirected"})
        if parts.path in ("/drip-headers", "/drip-body"):  # a byte at a time, never done: for the deadline
            self.close_connection = True
            try:
                if parts.path == "/drip-body":
                    self.wfile.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n")
                for _ in range(300):
                    self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n" if parts.path == "/drip-headers" else b" ")
                    self.wfile.flush()
                    time.sleep(0.2)
            except OSError:
                pass  # the site hung up: what the test wants
            return None
        return self._send(404, b"{}")

    def do_POST(self):
        fake = self.fake
        parts = urlsplit(self.path)
        fake.requests.append(("POST", parts.path))
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        if parts.path != "/token":
            return self._send(404, b"{}")
        behaviour = fake.behaviour
        if behaviour.get("slow"):
            time.sleep(behaviour["slow"])
        if behaviour.get("big"):
            return self._send(200, b'{"padding": "' + b"x" * (1024 * 1024 + 10) + b'"}')
        if "status_token" in behaviour:
            return self._json(behaviour["status_token"], {"error": "server_error", "error_description": "<b>secret details</b>"})
        form = {key: values[-1] for key, values in parse_qs(raw, keep_blank_values=True).items()}
        status, answer = fake.token(form, self.headers.get("Authorization", ""))
        if behaviour.get("token_error"):
            status, answer = 200, {"error": behaviour["token_error"], "error_description": "bad <script>"}
        if "token_type" in behaviour and status == 200:
            if behaviour["token_type"] is None:
                answer.pop("token_type", None)
            else:
                answer["token_type"] = behaviour["token_type"]
        return self._json(status, answer)


def main():
    parser = argparse.ArgumentParser(description="A fake OAuth 2 / OpenID Connect provider on 127.0.0.1.")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--client-id", default="hfile-client")
    parser.add_argument("--client-secret", default="hfile-secret")
    parser.add_argument("--basic", action="store_true", help="accept client credentials only by HTTP Basic")
    parser.add_argument("--sub", default="alice-1")
    parser.add_argument("--login", default="alice")
    parser.add_argument("--name", default="爱丽丝")
    args = parser.parse_args()
    claims = {"sub": args.sub, "preferred_username": args.login, "name": args.name}
    idp = FakeIdp(client_id=args.client_id, client_secret=args.client_secret, basic=args.basic, claims=claims).serve(args.port)
    print(f"fake provider at {idp.base} (client {args.client_id}, {'basic' if args.basic else 'post'})", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        idp.stop()


if __name__ == "__main__":
    main()
