"""What the site asks a provider, server to server: the token, the userinfo, the OpenID configuration
(discovery, for admins).

Every request goes through apps.storages.drivers.http.client(): TLS checked against the system's CA
certificates, User-Agent "HFile" (GitHub's API refuses requests without one). Redirects aren't
followed, the whole exchange has TIMEOUT seconds, at most MAX_BYTES of an answer are read (and
uncompressed answers are asked for, so that the limit is what ends up in memory), and only http(s)
addresses are fetched. The admin chooses the addresses, but the server fetches them.

httpx's timeouts are per operation (connecting, each read): a server that answers a byte, or a
"100 Continue", every few seconds would hold a request (and the site's thread) for as long as it likes.
So a timer shuts the connection's socket down when TIMEOUT is up, whatever the request is doing then
(httpcore's trace extension hands over the socket as soon as it is connected). Only resolving the
host name isn't covered.

Errors name the stage and what went wrong ("获取 Token 时服务商返回 401。"; the login's page puts
"单点登录失败：" before it). What a provider
answered is never shown, logged or kept: an answer can hold tokens, and its text is whatever the
provider (or whoever sits in between) wrote.
"""

from __future__ import annotations

import json
import logging
import re
import socket
import ssl
import threading
import time
from urllib.parse import quote, urlsplit

import httpx

from django.utils.translation import gettext as _, gettext_lazy, pgettext

from apps.storages.drivers import http

logger = logging.getLogger("hfile.sso")

TIMEOUT = 15.0  # seconds, for the whole request
MAX_BYTES = 1024 * 1024

# A provider's error code (RFC 6749 5.2: "invalid_grant"), shown as it is; other text never is.
ERROR_CODE = re.compile(r"[a-z_]{1,64}")
# An access token: visible ASCII, as an Authorization header can carry it.
_TOKEN = re.compile(r"[\x21-\x7e]{1,16384}")
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,100}")  # RFC 6749 3.3

TOKEN = gettext_lazy("getting the token")
USERINFO = gettext_lazy("getting the user info")
DISCOVERY = gettext_lazy("reading the OpenID configuration")
LATE = gettext_lazy("the provider took too long to finish answering (over 15 seconds)")


class ProviderError(Exception):
    """Something a provider answered (or didn't) that ends the login. `message` is for the page:
    the stage and what happened, never what the provider wrote."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.message = message
        self.status = status


def short_code(value) -> str:
    """An error code as a provider sent it, when it is short and plain; "" otherwise."""
    return value if isinstance(value, str) and ERROR_CODE.fullmatch(value) else ""


def _fail(stage: str, what: str, *, status: int | None = None, provider: str = "") -> ProviderError:
    logger.warning("SSO %s failed for provider %s: %s", stage, provider or "?", what if status is None else f"HTTP {status}")
    # "获取 Token 时", "获取用户信息时": a space after a Latin letter
    message = pgettext("after a Latin word", "While %(stage)s, %(what)s.") if str(stage)[-1:].isascii() else _("While %(stage)s, %(what)s.")
    return ProviderError(message % {"stage": stage, "what": what}, status=status)


def _transport_problem(exc: Exception) -> str:
    """What a failed connection was, without the exception's text (it may hold the address)."""
    if isinstance(exc, httpx.TimeoutException):
        return _("the provider took too long to answer (over 15 seconds)")
    cause = exc
    while cause is not None:
        if isinstance(cause, ssl.SSLCertVerificationError):
            return _("the provider's TLS certificate didn't pass verification")
        if isinstance(cause, ssl.SSLError):
            return _("the TLS connection to the provider failed")
        cause = cause.__cause__ or cause.__context__
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return _("the provider couldn't be reached")
    return _("the connection to the provider failed")


def request_json(method: str, url: str, *, stage: str, provider: str = "", headers: dict | None = None,
                 data: dict | None = None, auth: httpx.Auth | None = None) -> dict:
    """The JSON object a provider answers `method url` with. Raises ProviderError."""
    try:
        parts = urlsplit(url)
    except ValueError:
        parts = None
    if parts is None or parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise _fail(stage, _("the address isn't an http(s) address"), provider=provider)
    deadline = time.monotonic() + TIMEOUT
    sent = {"Accept": "application/json", "Accept-Encoding": "identity", **(headers or {})}
    sockets = []

    def trace(event, info):
        # The sockets the request opens (to the provider or a proxy, and its TLS layer): the watchdog's.
        if event in ("connection.connect_tcp.complete", "connection.start_tls.complete"):
            stream = info.get("return_value")
            sock = stream.get_extra_info("socket") if stream is not None else None
            if sock is not None:
                sockets.append(sock)

    def cut_off():
        for sock in list(sockets):
            try:
                socket.socket.shutdown(sock, socket.SHUT_RDWR)  # the TCP connection, under any TLS
            except OSError:
                pass

    watchdog = threading.Timer(TIMEOUT, cut_off)
    watchdog.daemon = True
    watchdog.start()
    try:
        with http.client(timeout=TIMEOUT, headers=sent) as client:
            with client.stream(method, url, data=data, auth=auth, extensions={"trace": trace}) as response:
                status = response.status_code
                if 300 <= status < 400:
                    raise _fail(stage, _("the provider answered %(status)s (a redirect, which single sign-on doesn't follow)") % {"status": status}, status=status, provider=provider)
                if not 200 <= status < 300:
                    raise _fail(stage, _("the provider answered %(status)s") % {"status": status}, status=status, provider=provider)
                encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
                if encoding not in ("", "identity"):
                    raise _fail(stage, _("the provider's answer was compressed (no compression was asked for)"), provider=provider)
                body = bytearray()
                for chunk in response.iter_raw():
                    body += chunk
                    if len(body) > MAX_BYTES:
                        raise _fail(stage, _("the provider's answer is too large (over 1 MB)"), provider=provider)
                    if time.monotonic() > deadline:
                        raise _fail(stage, LATE, provider=provider)
    except (httpx.HTTPError, httpx.InvalidURL, httpx.StreamError, ValueError) as exc:
        late = time.monotonic() >= deadline  # the watchdog cut it off
        raise _fail(stage, LATE if late else _transport_problem(exc), provider=provider) from None
    finally:
        watchdog.cancel()
    if time.monotonic() >= deadline:  # an answer without a length looks complete when it is cut off
        raise _fail(stage, LATE, provider=provider)
    try:
        answer = json.loads(bytes(body))
    except (ValueError, RecursionError):
        raise _fail(stage, _("the provider's answer isn't JSON"), provider=provider) from None
    if not isinstance(answer, dict):
        raise _fail(stage, _("the provider's answer isn't a JSON object"), provider=provider)
    return answer


def _form_encoded(value: str) -> str:
    """RFC 6749 2.3.1: the client id and secret are form-encoded before they go into HTTP Basic."""
    return quote(value, safe="")


def exchange_code(provider, code: str, redirect_uri: str, verifier: str) -> str:
    """The access token for the authorization `code` (RFC 6749 4.1.3, with PKCE's verifier). The client
    secret goes in the form or in HTTP Basic, as provider.token_auth says; without one (a public client)
    only the client id is sent. Raises ProviderError."""
    from apps.core.secrets import UnreadableSecret

    from .models import TokenAuth

    try:
        secret = provider.secret()
    except UnreadableSecret:
        raise ProviderError(
            _("This provider's Client Secret can't be read any more (secret_key in the settings "
              "file has changed). Please ask the administrator to enter the Client Secret again.")
        ) from None
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri, "code_verifier": verifier}
    auth = None
    if secret and provider.token_auth == TokenAuth.BASIC:
        auth = httpx.BasicAuth(_form_encoded(provider.client_id), _form_encoded(secret))
    else:
        form["client_id"] = provider.client_id
        if secret:
            form["client_secret"] = secret
    answer = request_json("POST", provider.token_url, stage=TOKEN, provider=provider.code, data=form, auth=auth)
    if "error" in answer:  # GitHub answers 200 with {"error": "bad_verification_code", ...}
        code_text = short_code(answer.get("error"))
        raise _fail(TOKEN, _("the provider refused (%(code)s)") % {"code": code_text} if code_text else _("the provider refused"), provider=provider.code)
    token = answer.get("access_token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise _fail(TOKEN, _("the provider's answer has no usable access_token"), provider=provider.code)
    token_type = answer.get("token_type")
    if token_type is not None and not (isinstance(token_type, str) and token_type.lower() == "bearer"):
        raise _fail(TOKEN, _("the token from the provider isn't a Bearer token"), provider=provider.code)
    return token


def userinfo(provider, token: str) -> dict:
    """The provider's userinfo answer for `token`. Raises ProviderError."""
    return request_json("GET", provider.userinfo_url, stage=USERINFO, provider=provider.code,
                        headers={"Authorization": f"Bearer {token}"})


def discover(url: str) -> dict:
    """An OpenID configuration (.well-known/openid-configuration): the endpoints, scopes and client
    authentication methods it lists, each checked; what is missing or unusable is None. Raises
    ProviderError."""
    from django.core.exceptions import ValidationError

    from .validation import clean_url

    answer = request_json("GET", url, stage=DISCOVERY)
    found = {}
    for key, name in (("authorization_endpoint", "auth_url"), ("token_endpoint", "token_url"),
                      ("userinfo_endpoint", "userinfo_url")):
        value = answer.get(key)
        try:
            found[name] = clean_url(value) if isinstance(value, str) else None
        except ValidationError:
            found[name] = None
    scopes = answer.get("scopes_supported")
    found["scopes"] = [
        scope for scope in (scopes if isinstance(scopes, list) else [])
        if isinstance(scope, str) and _SCOPE.fullmatch(scope)
    ][:100]
    methods = answer.get("token_endpoint_auth_methods_supported")
    if not isinstance(methods, list):
        methods = ["client_secret_basic"]  # OpenID Connect Discovery 1.0, section 3: the default
    if "client_secret_basic" in methods:
        found["token_auth"] = "basic"
    elif "client_secret_post" in methods:
        found["token_auth"] = "post"
    else:
        found["token_auth"] = None
    issuer = answer.get("issuer")
    found["issuer"] = issuer if isinstance(issuer, str) and len(issuer) <= 1000 and issuer.isprintable() else None
    return found
