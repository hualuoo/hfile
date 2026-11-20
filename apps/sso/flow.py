"""Logging in, and binding an account, with a provider: what /sso/<code>/login and its callback do
(views.py), the authorization-code flow of OAuth 2 (RFC 6749 4.1) with PKCE (RFC 7636).

1. begin(): a fresh state (32 hex characters) and PKCE verifier, kept in this session with the
   provider, where to go afterwards (a path of this site) and the intent ("login", or "link" with the
   user's id), for PENDING_SECONDS; at most PENDING_MAX at once, the oldest goes. The browser is sent to
   the provider's authorization endpoint.
2. complete(): the provider sent the browser back with ?code=&state=. The state is taken out of the
   session before anything else (it works once), and must be this provider's and fresh. The code is
   exchanged for a token (outbound.py), the token for the userinfo, the userinfo's id field gives the
   account's id at the provider (jsonpath.py).
3. That id, through an Identity, is a user: logged in with apps.accounts.login.after_password(), so two-step
   verification still applies. With no Identity: a new user from the template user while
   SiteSettings.sso_auto_register is on, else a page saying how to bind the account. Never a user found
   by name. With the intent "link", the account is bound to the user who began (if it is still them).

Every way this ends without logging in raises Failure, which views.py shows as an error page
(apps.web.errors.error_page) and, for a login, writes to the login log. What a provider sent is never put on
the page: only the short error code of ?error=, checked.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import time
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from django.conf import settings
from django.contrib.auth import logout
from django.core.exceptions import ValidationError
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext as _

from apps.accounts import entry as login_entry
from apps.accounts import login as login_steps
from apps.accounts.models import Kind, User
from apps.accounts.permissions import usable
from apps.accounts.signals import user_copied
from apps.accounts.validators import NICKNAME_MAX, USERNAME_MAX, check_username, validate_nickname
from apps.core import origin
from apps.core.text import is_invisible, single_line
from apps.siteconf.models import SiteSettings

from . import jsonpath, outbound
from .models import Identity, Provider, key_of
from .validation import clean_external_id

logger = logging.getLogger("hfile.sso")

PENDING_KEY = "hfile_sso_pending"
PENDING_MAX = 5
PENDING_SECONDS = 600
# The identity a login began with while it waits for a step (two-step verification): its last_login_at
# is set once the login is done (apps.py, on user_logged_in).
LOGIN_IDENTITY_KEY = "hfile_sso_identity"
# A session that holds nothing but logins begun here (a visitor not logged in; a scanner fetching
# /sso/<code>/login without a cookie makes a new session row each time) lasts SHORT_SECONDS, not
# SESSION_COOKIE_AGE; logging in gives it the normal lifetime back (signals.on_login: cycle_key keeps a
# custom expiry).
SHORT_KEY = "hfile_sso_short"
SHORT_SECONDS = PENDING_SECONDS + 60

LOGIN = "login"
LINK = "link"

NEXT_MAX = 2000
CODE_MAX = 4096  # an authorization code; real ones are far shorter
_STATE = re.compile(r"[0-9a-f]{32}")
_CODE = re.compile(r"[\x21-\x7e]+")


class Failure(Exception):
    """A login or binding that ends on an error page: `status`, `heading` and `message` (translated, never a
    provider's text), and for the login log `reason` (a loginlog.REASONS code), `username` and `user`.
    `retry`: offer to start again with the same provider."""

    def __init__(self, status, heading, message, *, reason="", username="", user=None, retry=True):
        super().__init__(message)
        self.status = status
        self.heading = heading
        self.message = message
        self.reason = reason
        self.username = username
        self.user = user
        self.retry = retry
        # What the page offers next (complete() fills them in): the provider, where the attempt was
        # to go on to, and whether it was a login or a binding.
        self.provider = None
        self.next = "/"
        self.intent = LOGIN


# ------------------------------------------------------------------ addresses


def callback_path(provider):
    return f"/sso/{provider.code}/login/callback"


def callback_url(provider, request=None):
    """The address to register at the provider ([site] url, else the one this request came to)."""
    return origin.absolute(callback_path(provider), request)


def safe_next(value):
    """`value` when it is a path of this site ("/manage/#users"), else "/"."""
    if not isinstance(value, str) or not value.startswith("/") or len(value) > NEXT_MAX:
        return "/"
    if value.startswith("//") or "\\" in value or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        return "/"
    if not url_has_allowed_host_and_scheme(value, allowed_hosts=None):
        return "/"
    return value


def with_query(path, **params):
    """`path` (a path of this site, maybe with a query and a #fragment) with `params` added; a path in
    them keeps its slashes (?next=/manage/). What isn't a path of this site becomes "/"."""
    parts = urlsplit(path)
    target = parts.path if parts.path.startswith("/") and not parts.path.startswith("//") else "/"
    if parts.scheme or parts.netloc:
        target = "/"
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in params]
    query += list(params.items())
    encoded = urlencode(query, quote_via=quote, safe="/")
    return target + (f"?{encoded}" if encoded else "") + (f"#{parts.fragment}" if parts.fragment else "")


def login_form(next_path):
    """Where a login waiting for a step (two-step verification) is finished: the login dialog on the
    home page, or the secure login entry's page while one is set (the session API wants the entry)."""
    entry = login_entry.current()
    params = {"next": next_path} if next_path and next_path != "/" else {}
    if entry:
        return with_query(f"/login/{quote(entry, safe='')}/", **params)
    return with_query("/", auth="login", **params)


# ------------------------------------------------------------------ step 1: to the provider


def pkce_challenge(verifier):
    """RFC 7636 4.2, S256: BASE64URL(SHA256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorize_url(provider, *, state, challenge, redirect_uri):
    """provider.auth_url with the request's parameters (RFC 6749 4.1.1, RFC 7636 4.3) merged into the
    query it may have already: its own others stay, ours replace any of the same name."""
    ours = {
        "response_type": "code",
        "client_id": provider.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if provider.scope:
        ours["scope"] = provider.scope
    parts = urlsplit(provider.auth_url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in ours]
    query += list(ours.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, quote_via=quote), ""))


def _pending(request):
    """The logins begun in this session that haven't expired."""
    now = time.time()
    entries = request.session.get(PENDING_KEY)
    if not isinstance(entries, list):
        return []
    return [entry for entry in entries if isinstance(entry, dict) and entry.get("until", 0) >= now]


def begin(request, provider, *, intent=LOGIN, next_path="/", user=None):
    """Keeps a new login (or binding, for `user`) in the session; returns the provider's authorization
    address to send the browser to."""
    state = secrets.token_hex(16)
    verifier = secrets.token_urlsafe(48)  # 64 characters, RFC 7636 4.1 allows 43 to 128
    redirect_uri = callback_url(provider, request)
    entries = _pending(request)
    entries.append({
        "state": state,
        "provider": provider.pk,
        "verifier": verifier,
        # The token request must send the same redirect_uri as the authorization request did.
        "redirect_uri": redirect_uri,
        "next": safe_next(next_path),
        "intent": intent,
        "user": user.pk if user is not None else None,
        "until": time.time() + PENDING_SECONDS,
    })
    session = request.session
    keys = set(session.keys())
    only_ours = keys <= {PENDING_KEY} or (SHORT_KEY in keys and keys <= {PENDING_KEY, SHORT_KEY, "_session_expiry"})
    if not request.user.is_authenticated and only_ours:
        session[SHORT_KEY] = True
        session.set_expiry(SHORT_SECONDS)
    session[PENDING_KEY] = entries[-PENDING_MAX:]
    return authorize_url(provider, state=state, challenge=pkce_challenge(verifier), redirect_uri=redirect_uri)


def take(request, state):
    """The pending login of `state`, taken out of the session whatever happens next; None when there is
    none (also when it has expired: expired ones are dropped)."""
    entries = request.session.get(PENDING_KEY)
    if not isinstance(entries, list):
        return None
    found = None
    kept = []
    now = time.time()
    wanted = state.encode("ascii") if isinstance(state, str) and _STATE.fullmatch(state) else None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        given = entry.get("state")
        if (found is None and wanted is not None and isinstance(given, str)
                and hmac.compare_digest(given.encode("ascii", "replace"), wanted)):
            found = entry
        elif entry.get("until", 0) >= now:
            kept.append(entry)
    if kept:
        request.session[PENDING_KEY] = kept
    else:
        request.session.pop(PENDING_KEY, None)
    if found is None:
        return None
    # Saved at once, before anything is asked of the provider: Django's SessionMiddleware doesn't save a
    # session on a 5xx answer (the 502 of a provider that failed), and a crash on the way mustn't bring
    # the state back either.
    request.session.save()
    # Two requests with the same state at once may both have read it before this save: the first to
    # claim it here wins (this process's cache; the provider's one-use code and PKCE cover the rest).
    if not cache.add(f"hfile-sso-state:{wanted.decode()}", 1, timeout=PENDING_SECONDS + 60):
        return None
    return found


# ------------------------------------------------------------------ step 2: back from the provider


def complete(request, code, params):
    """The callback of provider `code` with the query `params`. Returns where to send the browser;
    raises Failure."""
    entry = take(request, params.get("state"))  # first: a state works once, whatever happens below
    provider = Provider.objects.filter(code=code, enabled=True).first()
    try:
        return _complete(request, provider, entry, params)
    except Failure as failure:
        failure.provider = provider
        if entry is not None and entry.get("provider") == getattr(provider, "pk", None):
            failure.next = entry.get("next") or "/"
            failure.intent = entry.get("intent") or LOGIN
        raise


def _complete(request, provider, entry, params):
    if provider is None:
        raise Failure(404, _("Single sign-on not found"), _("This single sign-on doesn't exist or isn't enabled."), retry=False)
    if entry is None or entry.get("provider") != provider.pk:
        raise Failure(400, _("Single sign-on didn't finish"), _("This login's check code (state) doesn't match: the login "
                      "page may have been open too long, this address opened in "
                      "another browser, or this page reloaded. Please log in again."))
    if entry.get("until", 0) < time.time():
        raise Failure(400, _("Single sign-on expired"), _("More than 10 minutes passed between clicking to log in and coming back. Please log in again."))
    logging_in = entry.get("intent") != LINK
    error = params.get("error")
    if error is not None:
        named = outbound.short_code(error)  # only a short code of a-z and _ is shown, never other text
        code = _(" (%(code)s)") % {"code": named} if named else ""
        if logging_in:
            raise Failure(400, _("The provider didn't allow the login"), _("The provider refused this login%(code)s. If you canceled it yourself, just log in again.") % {"code": code},
                          reason="sso_denied")
        raise Failure(400, _("The provider didn't allow the linking"), _("The provider refused this linking%(code)s. If you canceled it yourself, just link again.") % {"code": code})
    auth_code = params.get("code")
    if not isinstance(auth_code, str) or len(auth_code) > CODE_MAX or not _CODE.fullmatch(auth_code):
        if logging_in:
            raise Failure(400, _("Single sign-on didn't finish"), _("The authorization code the provider sent back is wrong. Please log in again."))
        raise Failure(400, _("Linking didn't finish"), _("The authorization code the provider sent back is wrong. Please link again."))
    if not logging_in:
        _check_linking_user(request, entry)  # before asking the provider anything
    try:
        token = outbound.exchange_code(provider, auth_code, entry.get("redirect_uri", ""), entry.get("verifier", ""))
        info = outbound.userinfo(provider, token)
    except outbound.ProviderError as exc:
        raise Failure(502, _("Single sign-on failed"), _("Single sign-on failed: %(message)s Please try again later; if it keeps happening, ask the administrator to check this provider's settings.") % {"message": exc.message},
                      reason="sso_provider" if logging_in else "") from None
    external_id = external_id_of(provider, info, logging_in)
    if not logging_in:
        return _link(request, provider, entry, external_id)
    return _log_in(request, provider, entry, external_id, info)


def external_id_of(provider, info, logging_in=True):
    """The account's id at the provider: what provider.id_field finds in the userinfo. Raises Failure."""
    try:
        value, why = jsonpath.text_at(info, jsonpath.parse(provider.id_field))
        if value is None:
            raise ValidationError(why)
        return clean_external_id(value)
    except (jsonpath.PathError, ValidationError) as exc:
        why = exc.messages[0] if isinstance(exc, ValidationError) else _("is malformed: %(error)s") % {"error": exc}
        logger.warning("SSO userinfo of provider %s has no usable id at %s", provider.code, provider.id_field)
        raise Failure(502, _("Single sign-on failed"), _("The user info from the provider has no usable user ID: \"%(id_field)s\" %(why)s. Please ask the administrator to check this provider's \"User ID field\" and Scope.") % {"id_field": provider.id_field, "why": why.rstrip("。.")},
                      reason="sso_userinfo" if logging_in else "") from None


def find_identity(provider, external_id):
    return Identity.objects.select_related("user").filter(provider=provider, external_key=key_of(external_id)).first()


def _log_in(request, provider, entry, external_id, info):
    identity = find_identity(provider, external_id)
    if identity is None:
        if not SiteSettings.load().sso_auto_register:
            # The id goes into the login log (for the admin, who may bind it), not on the page.
            raise Failure(
                403, _("This %(name)s account isn't linked to a user") % {"name": provider.name},
                _("No user on this site is linked to this %(name)s account, so it can't log in. If you have an "
                  'account, log in with your username and password first, then link %(name)s in "Linked accounts" '
                  "in the menu at the top right; if you don't, ask the administrator to create and link an account "
                  'for you, or to turn on "Let single sign-on register new users" in "Security settings".') % {"name": provider.name},
                reason="sso_not_bound", username=f"{provider.code}:{external_id}", retry=False,
            )
        identity = register(provider, external_id, info)
    user = identity.user
    if user.is_builtin:
        raise Failure(403, _("Can't log in"), _("Built-in users can't log in."), reason="builtin", user=user, retry=False)
    if not user.is_active:
        raise Failure(403, _("Account disabled"), _("The user linked to this %(name)s account is disabled and can't log in. Please contact the administrator with any questions.") % {"name": provider.name},
                      reason="inactive", user=user, retry=False)
    if request.user.is_authenticated:
        if request.user.pk == user.pk:  # logged in meanwhile (another tab): nothing more to do
            Identity.objects.filter(pk=identity.pk).update(last_login_at=timezone.now())
            return entry.get("next") or "/"
        logout(request)  # someone else: a login waiting for a step starts from an empty session
    user.backend = settings.AUTHENTICATION_BACKENDS[0]
    _answer, logged_in = login_steps.after_password(request, user, source="sso")
    if logged_in:
        Identity.objects.filter(pk=identity.pk).update(last_login_at=timezone.now())
        return entry.get("next") or "/"
    # Two-step verification: the login form asks for the code (it sees the pending step on load).
    request.session[LOGIN_IDENTITY_KEY] = identity.pk
    return login_form(entry.get("next") or "/")


def _check_linking_user(request, entry):
    """A binding goes to the user who began it, if this session is still theirs."""
    user = request.user
    if not user.is_authenticated or user.pk != entry.get("user") or user.is_builtin:
        raise Failure(403, _("Linking didn't finish"), _('The account logged in when linking started has logged out or changed. Please log in again, then link in "Linked accounts".'), retry=False)


def _link(request, provider, entry, external_id):
    user = request.user
    back = entry.get("next") or "/"
    _check_linking_user(request, entry)
    taken = _("This %(name)s account is linked to another user: a provider account can be linked to one user only.") % {"name": provider.name}
    with transaction.atomic():
        existing = find_identity(provider, external_id)
        if existing is not None and existing.user_id == user.pk:
            return with_query(back, sso_linked=provider.code)  # bound already: nothing to do
        if existing is not None:
            raise Failure(403, _("This account is linked to another user"), taken, retry=False)
        if Identity.objects.filter(provider=provider, user=user).exists():
            raise Failure(403, _("You have already linked another %(name)s account") % {"name": provider.name},
                          _("A user can link only one %(name)s account. To use another, "
                            'unlink the current one in "Linked accounts" first.') % {"name": provider.name}, retry=False)
        try:
            with transaction.atomic():
                Identity.objects.create(provider=provider, external_id=external_id, user=user)
        except IntegrityError:  # bound at the same moment by another request
            existing = find_identity(provider, external_id)
            if existing is None or existing.user_id != user.pk:
                raise Failure(403, _("Linking didn't finish"), taken if existing else _("You have just linked another %(name)s account.") % {"name": provider.name},
                              retry=False) from None
    return with_query(back, sso_linked=provider.code)


# ------------------------------------------------------------------ new users


def _proposal(provider, info, path):
    if not path:
        return None
    try:
        value, _why = jsonpath.text_at(info, jsonpath.parse(path))
    except jsonpath.PathError:
        return None
    return value


def proposed_username(provider, info):
    """The username provider.username_field proposes, normalised as usernames are, when it is a valid
    one; None otherwise."""
    value = _proposal(provider, info, provider.username_field)
    if value is None:
        return None
    name = User.normalize_username(value.strip()[: USERNAME_MAX * 2])  # cut first: NFKC can be slow on a lot
    try:
        check_username(name)
    except ValidationError:
        return None
    return name


def fallback_username(provider, external_id):
    """<code>_<external id>, the id's characters that a username can't have made "_"; a digest of the
    id when even that isn't a valid username."""
    name = User.normalize_username(f"{provider.code}_{external_id}")
    name = re.sub(r"[^\w.@+-]", "_", name)[: USERNAME_MAX - 8]
    try:
        check_username(name)
    except ValidationError:
        name = f"{provider.code}_{key_of(external_id)[:16]}"
    return name


def username_candidates(provider, external_id, info):
    proposed = proposed_username(provider, info)
    if proposed:
        yield proposed
    base = fallback_username(provider, external_id)
    yield base
    for number in range(2, 100):
        yield f"{base}-{number}"
    yield f"{base[:USERNAME_MAX - 13]}-{secrets.token_hex(6)}"


def proposed_nickname(provider, info):
    """The nickname provider.nickname_field gives, cleaned up as nicknames are (one line, without
    invisible characters) and cut to length; "" when there is none."""
    value = _proposal(provider, info, provider.nickname_field)
    if not value:
        return ""
    text = single_line(value[: NICKNAME_MAX * 8])  # cut first: a provider may send a megabyte
    text = "".join(char for i, char in enumerate(text) if not is_invisible(char, text[i - 1] if i else ""))
    text = single_line(text[:NICKNAME_MAX])
    try:
        validate_nickname(text)
    except ValidationError:
        return ""
    return text


def _save_with_free_username(user, candidates):
    for name in candidates:
        if User.objects.username_taken(name):
            continue
        user.username = name
        try:
            with transaction.atomic():
                user.save()
            return
        except IntegrityError:  # taken at the same moment
            continue
    raise IntegrityError("no free username")


def register(provider, external_id, info):
    """A new user for this account (SiteSettings.sso_auto_register is on): of the normal kind, never
    staff, without a usable password (修改密码 sets one without an old one), with the template user's
    default permissions; then its Identity, and user_copied, so that the storages app copies the
    template's grants. One transaction. Returns the Identity."""
    template = User.objects.template()
    try:
        with transaction.atomic():
            user = User(
                kind=Kind.NORMAL, is_active=True, is_staff=False, is_superuser=False,
                nickname=proposed_nickname(provider, info),
                default_permissions=usable(template.default_permissions or []),  # D55
            )
            user.set_unusable_password()
            _save_with_free_username(user, username_candidates(provider, external_id, info))
            identity = Identity.objects.create(provider=provider, external_id=external_id, user=user)
            user_copied.send(sender=User, source=template, target=user)
    except IntegrityError:
        identity = find_identity(provider, external_id)  # registered by a request at the same moment
        if identity is None:
            raise
    return identity
