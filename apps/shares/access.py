"""What a share's visitor may do: one decision for every request that reaches a share (the share page,
its API, its downloads, and OnlyOffice through share_file(), contract S1). Decision D32, inventory 10.3-10.7.

    share = find(key)                          # it exists and hasn't expired, else ShareError
    visit = Visit(request, share)              # this browser's cookie for it
    require_verified(share, visit)             # the password was given (shares without one pass)
    access = open_share(request, share, visit) # the owner re-checked: a apps.storages.access.Access
    path = resolve(access, share, client_path, want=FILE)
    access.check(DOWNLOAD, path, is_dir=False) # the owner's permission, filters, folder passwords

- Visitors act as the owner (apps.storages.access.share_actor): the owner's grant, rooted at the share's
  folder. open_storage() re-checks the owner on every request (storage enabled, owner active, grant
  enabled, the folder still inside the owner's current root) and open_share() adds create_share_link:
  disabling the owner, revoking the grant or the permission, or narrowing the root stops the share at
  once, and giving it back revives it.
- Inside a share no filter rule and no folder password is bypassed, whatever the owner's ignore_hidden
  and ignore_password (apps.storages.access does so for share actors); the owner's preview and download are
  checked by the operations themselves.
- Paths: "/" is the share's folder, where only the entries show; deeper paths must start with a shared
  folder (a shared file is only itself). ".." never passes (apps.storages.access.Access.path()).
- The password is asked once; then a signed HttpOnly cookie per share (Visit) says so, bound to the
  share's id, nonce and revision, ending after IDLE seconds without use and VERIFIED_FOR after the
  password was given. No password goes into a URL the site makes (?pwd= only fills in the form). Wrong
  passwords are counted per address and per share (password_wait()). Folder passwords opened inside a
  share go into the same cookie: they hold for that share only, without 记住密码.
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any

from django.conf import settings
from django.core import signing
from django.core.cache import cache
from django.utils.translation import gettext, ngettext

from apps.accounts.models import Kind
from apps.accounts.permissions import Permission
from apps.core import net
from apps.core.secrets import UnreadableSecret
from apps.core.throttles import address_key
from apps.core.times import iso
from apps.storages import access as policy
from apps.storages.access import AccessDenied

from .models import FILE, FOLDER, Share

# --- errors ---------------------------------------------------------------------------------------

SHARE_NOT_FOUND = "share_not_found"
SHARE_EXPIRED = "share_expired"
PASSWORD_REQUIRED = "share_password_required"
PASSWORD_WRONG = "share_password_wrong"
PASSWORD_THROTTLED = "share_password_throttled"
PASSWORD_UNREADABLE = "share_password_unreadable"
UNAVAILABLE = "share_unavailable"
NOT_FOUND = "not_found"  # a path outside the shared entries: files' code, so the browser shows 不存在
IS_FOLDER = "is_folder"


class ShareError(AccessDenied):
    """A refusal of the shares: {"detail", "code", ...}, as apps.storages.access.AccessDenied (whose codes a
    share request may answer too: storage_disabled, actor_disabled, no_grant, outside_root, inaccessible,
    password_required ...). Callers catch AccessDenied for both."""


# --- finding a share ------------------------------------------------------------------------------

KEY_RE = re.compile(r"[A-Za-z0-9_-]{1,16}")


def find(key: Any, *, now=None) -> Share:
    """The share of `key` (spelled exactly), with its owner and storage. ShareError when there is none
    (404 share_not_found) or it has expired (404 share_expired, with expires_at)."""
    share = None
    if isinstance(key, str) and KEY_RE.fullmatch(key):
        share = Share.objects.select_related("owner", "storage").filter(key=key).first()
    if share is None:
        raise ShareError(SHARE_NOT_FOUND, gettext("The share link doesn't exist; it may have been deleted."), status=404)
    if share.is_expired(now):
        raise ShareError(SHARE_EXPIRED, gettext("The share link has expired."), status=404,
                         expires_at=iso(share.expires_at))
    return share


# --- this browser's visit: the password given, folder passwords unlocked ---------------------------

COOKIE_PREFIX = "hfile_share_"
COOKIE_SALT = "hfile.shares.visit"
IDLE = 2 * 3600  # a cookie not used for this long ends
REFRESH = 300  # it is written again at most this often (the idle time starts again)
VERIFIED_FOR = 7 * 24 * 3600  # then the password is asked again, however busy the cookie is
UNLOCKED_MAX = 50  # folder passwords kept per share


class Visit:
    """What this browser holds for one share, in a signed HttpOnly cookie of its own (hfile_share_<id>,
    path /): when its password was given, and the folder passwords unlocked inside it as
    {rule id: revision}. The salt holds the share's id, nonce and revision: a cookie of another share
    (or of a deleted one whose id came back) never verifies. It has the interface of
    apps.storages.access.FolderKeys (has, add, forget, write), so an Access of the share uses it for folder
    passwords. write(response) puts it on the answer when it changed or is due to be renewed."""

    def __init__(self, request, share: Share):
        self.share = share
        self.verified_at: int | None = None
        self.unlocked: dict[int, int] = {}
        self._issued: int | None = None
        self._dirty = False
        cookies = getattr(request, "COOKIES", None) or {}
        raw = cookies.get(self.cookie_name)
        if raw:
            self._load(raw)

    @property
    def cookie_name(self) -> str:
        return f"{COOKIE_PREFIX}{self.share.pk}"

    def _salt(self) -> str:
        return f"{COOKIE_SALT}.{self.share.pk}.{self.share.nonce}.{self.share.revision}"

    def _load(self, raw: str) -> None:
        try:
            data = signing.loads(raw, salt=self._salt(), max_age=IDLE)
        except signing.BadSignature:  # also expired: the answer removes it
            self._dirty = True
            return
        if not isinstance(data, dict) or type(data.get("t")) is not int:
            self._dirty = True
            return
        now = int(time.time())
        self._issued = data["t"]
        verified = data.get("v")
        if type(verified) is int and 0 <= now - verified <= VERIFIED_FOR:
            self.verified_at = verified
        elif verified is not None:
            self._dirty = True
        unlocked = data.get("u")
        if isinstance(unlocked, list):
            for pair in unlocked[:UNLOCKED_MAX]:
                if isinstance(pair, list) and len(pair) == 2 and all(type(n) is int for n in pair):
                    self.unlocked[pair[0]] = pair[1]

    @property
    def verified(self) -> bool:
        """The visitor may see the share: its password was given (a share without one: always)."""
        return not self.share.has_password or self.verified_at is not None

    def verify(self) -> None:
        self.verified_at = int(time.time())
        self._dirty = True

    # -- folder passwords inside the share (apps.storages.access.FolderKeys' interface)

    def has(self, storage_id: int, rule_id: int, revision: int) -> bool:
        return storage_id == self.share.storage_id and self.unlocked.get(rule_id) == revision

    def add(self, storage_id: int, rule_id: int, revision: int, *, remember: bool = False) -> None:
        """remember is ignored: unlocks inside a share last as long as its cookie."""
        if storage_id != self.share.storage_id:
            return
        self.unlocked.pop(rule_id, None)
        self.unlocked[rule_id] = revision
        while len(self.unlocked) > UNLOCKED_MAX:
            self.unlocked.pop(next(iter(self.unlocked)))
        self._dirty = True

    def forget(self) -> None:
        if self.unlocked:
            self.unlocked = {}
            self._dirty = True

    def write(self, response) -> None:
        now = int(time.time())
        due = self._issued is not None and now - self._issued >= REFRESH
        if not (self._dirty or due):
            return
        if self.verified_at is not None or self.unlocked:
            payload: dict[str, Any] = {"t": now, "u": [[rule, revision] for rule, revision in self.unlocked.items()]}
            if self.verified_at is not None:
                payload["v"] = self.verified_at
            response.set_cookie(
                self.cookie_name, signing.dumps(payload, salt=self._salt(), compress=True), max_age=IDLE, path="/",
                httponly=True, samesite="Lax", secure=bool(getattr(settings, "SESSION_COOKIE_SECURE", False)),
            )
            self._issued = now
        else:
            response.delete_cookie(self.cookie_name, path="/", samesite="Lax")
        self._dirty = False


def require_verified(share: Share, visit: Visit) -> None:
    if not visit.verified:
        raise ShareError(PASSWORD_REQUIRED, gettext("This share needs an access password."), status=403)


# --- the share's password ---------------------------------------------------------------------------

ADDRESS_FAILURES, ADDRESS_WINDOW = 10, 60  # wrong passwords per address (IPv6: per /64) and seconds
SHARE_FAILURES, SHARE_WINDOW = 30, 15 * 60  # wrong passwords per share, from anywhere
# Wrong folder passwords tried inside one share, from anywhere (storages counts them per address too).
UNLOCK_FAILURES, UNLOCK_WINDOW = 30, 15 * 60

# Checking the wait, comparing and counting a failure happen under this lock, so that answers sent at
# the same moment can't try more than the limit (the counts are in this process's cache).
_attempts = threading.Lock()


def _counters(request, share: Share):
    ip = net.client_ip(request) if request is not None else None
    where = address_key(ip) if ip else "unknown"
    return ((f"hfile:share-password:address:{where}", ADDRESS_FAILURES, ADDRESS_WINDOW),
            (f"hfile:share-password:share:{share.pk}", SHARE_FAILURES, SHARE_WINDOW))


def _wait(counters) -> int:
    now = time.time()
    wait = 0
    for key, limit, window in counters:
        history = [moment for moment in cache.get(key, []) if moment > now - window]
        if len(history) >= limit:
            wait = max(wait, int(history[-limit] + window - now) + 1)
    return wait


def _count(counters) -> None:
    now = time.time()
    for key, limit, window in counters:
        history = [moment for moment in cache.get(key, []) if moment > now - window]
        history.append(now)
        cache.set(key, history[-limit:], window)


def password_wait(request, share: Share) -> int:
    """Seconds until a password may be tried again for this share from this address; 0 for now."""
    return _wait(_counters(request, share))


def _failed(request, share: Share) -> None:
    _count(_counters(request, share))


def check_password(request, share: Share, visit: Visit, password: Any) -> None:
    """The visitor's answer: right, and the visit is verified (the view writes the cookie); else
    ShareError: wrong (403), too many tries (429, with wait), unreadable (the site's secret_key changed)."""
    if not share.has_password:
        return
    if not isinstance(password, str) or not password.strip():
        raise ShareError("share_password_empty", gettext("Please enter the access password."), status=400)
    with _attempts:
        wait = password_wait(request, share)
        if wait:
            raise ShareError(PASSWORD_THROTTLED, ngettext("Too many wrong passwords. Please try again in %(wait)s second.", "Too many wrong passwords. Please try again in %(wait)s seconds.", wait) % {"wait": wait}, status=429, wait=wait)
        try:
            right = share.password_matches(password)
        except UnreadableSecret:
            raise ShareError(PASSWORD_UNREADABLE, gettext("This share's password can't be read any more (the site's secret key has changed). Please ask the person who shared it to create the share again."),
                             status=409) from None
        if not right:
            _failed(request, share)
    if not right:
        raise ShareError(PASSWORD_WRONG, gettext("Wrong password. Please try again."))
    visit.verify()


def unlock_folder(share: Share, access: policy.Access, *, password: str, path: str | None = None,
                  rule_id: int | None = None):
    """A folder password inside the share (its unlock API), kept in the visit (access.keys): the
    storage path of the folder, or the rule's id. apps.storages.access counts wrong passwords per address;
    here they count per share as well, from anywhere, and so do rule ids that aren't there (else a
    share without a password lets anybody try a folder's password from many addresses). A folder a
    filter rule makes inaccessible isn't unlocked: nothing to try a password on. Returns the rule or
    None; raises AccessDenied (password_wrong, rule_not_found, inaccessible, 429 throttled)."""
    counters = ((f"hfile:share-unlock:{share.pk}", UNLOCK_FAILURES, UNLOCK_WINDOW),)
    with _attempts:
        wait = _wait(counters)
        if wait:
            raise ShareError(policy.PASSWORD_THROTTLED, ngettext("Too many wrong passwords. Please try again in %(wait)s second.", "Too many wrong passwords. Please try again in %(wait)s seconds.", wait) % {"wait": wait}, status=429, wait=wait)
        try:
            if rule_id is not None:
                return access.unlock(password=password, rule_id=rule_id)
            if policy.filter_state(access.rules, path)["inaccessible"]:
                raise AccessDenied(policy.INACCESSIBLE, gettext("This location can't be opened."))
            return access.unlock(path, password=password)
        except AccessDenied as error:
            if error.code in (policy.PASSWORD_WRONG, policy.RULE_NOT_FOUND):
                _count(counters)
            raise


# --- the owner, the paths ----------------------------------------------------------------------------

def open_share(request, share: Share, visit: Visit | None = None) -> policy.Access:
    """The share's Access: its owner as the actor, rooted at the share's folder, re-checked now (see the
    top). Folder passwords are those unlocked in this visit. Raises AccessDenied."""
    owner = share.owner
    if owner.kind != Kind.NORMAL:
        raise ShareError(UNAVAILABLE, gettext("This share can't be opened."))
    entries = [(item["name"], item["type"] == FOLDER) for item in share.entries or ()
               if isinstance(item, dict) and isinstance(item.get("name"), str) and item.get("type") in (FILE, FOLDER)]
    access = policy.open_storage(request, share.storage, actor=policy.share_actor(owner, share.folder, entries))
    if not access.has(Permission.CREATE_SHARE_LINK):
        raise ShareError(UNAVAILABLE, gettext("The person who shared this can no longer create shares, so the share can't be opened for now."))
    access.keys = visit if visit is not None else Visit(request, share)
    return access


ANY = "any"


def resolve(access: policy.Access, share: Share, path: Any, *, want: str = ANY) -> str:
    """The storage path of `path` (as the share page sends it: "/" is the share's folder), if it lies
    within the shared entries: "/" itself (a folder), a shared file, or anything at or below a shared
    folder. want: FOLDER (to list it), FILE (to read it: "/" and a shared folder itself are refused) or
    ANY. Raises AccessDenied (invalid_path for "..", NUL and the like) or ShareError."""
    storage_path = access.path(path)
    relative = access.relative(storage_path)
    if relative == "/":
        if want == FILE:
            raise ShareError(NOT_FOUND, gettext("The file doesn't exist."), status=404)
        return storage_path
    first, _, rest = relative[1:].partition("/")
    entry = share.entry(first)
    if entry is None:
        raise ShareError(NOT_FOUND, gettext("The file or folder doesn't exist."), status=404)
    if entry["type"] == FILE:
        if rest or want == FOLDER:
            raise ShareError(NOT_FOUND, gettext("The folder doesn't exist.") if want == FOLDER and not rest else gettext("The file or folder doesn't exist."), status=404)
    elif want == FILE and not rest:
        raise ShareError(IS_FOLDER, gettext("Folders can't be downloaded yet."), status=400)
    return storage_path


# --- contract S1 --------------------------------------------------------------------------------------

def share_file(request, key: Any, path: Any) -> tuple[policy.Access, str]:
    """Contract S1 (OnlyOffice in a share): (access, storage_path) of the file `path` of the share
    `key`, for a visitor of this request.

    `path` is the file's path inside the share, as the share page sends it ("/report.docx",
    "/folder/report.docx"). Checked as every share request is: the share exists and hasn't expired, its
    password was given (the share's cookie on this request), the path lies within the shared entries
    (a file: not "/" nor a shared folder itself), and the owner is re-checked (open_share()). `access`
    is a apps.storages.access.Access with the share's actor (the owner, rooted at the share's folder, no
    bypass of filters or folder passwords); the caller then runs
    access.check(<its operation>, storage_path, is_dir=False) itself.

    Raises apps.storages.access.AccessDenied: ShareError (share_not_found, share_expired 404;
    share_password_required 403; share_unavailable 403; not_found 404; is_folder 400) or the policy's
    own (storage_disabled, actor_disabled, no_grant, outside_root, invalid_path ...), as the share API
    answers them. Nothing is counted and no cookie is written."""
    share = find(key)
    visit = Visit(request, share)
    require_verified(share, visit)
    access = open_share(request, share, visit)
    return access, resolve(access, share, path, want=FILE)
