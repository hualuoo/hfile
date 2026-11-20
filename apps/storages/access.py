"""The access policy: what a request may do with the files of a storage source (decisions D8, D10-D12).

Everything that touches a storage's files (browsing, transfers, links, shares, OnlyOffice) asks here and
nowhere else:

    access = open_storage(request, "photos")          # the storage, the actor and the actor's grant
    folder = access.path(request_path)                # a path the client sent -> the storage path
    access.check(LIST, folder, password=sent)         # permission, filters, folder passwords
    entries = access.visible(access.driver.list(folder))

The checks come in this order, and the first that fails decides (D8):

 1. the storage exists and is enabled;
 2. the actor is active: the logged-in user, else the guest; for a share, its owner (Actor);
 3. the actor has a grant for the storage and the grant is enabled;
 4. the operation's permission (apps/accounts/permissions.py);
 5. the path is inside the actor's root: the grant's root folder, or the share's folder;
 6. filter rules (bypassed with ignore_hidden, never inside a share nor for links):
    - hidden: the entry is left out of listings, but can be opened by its path;
    - inaccessible: the entry and everything below it is refused, for every operation;
    - disable_download: the entry and everything below it can't be read (download, preview, links)
      and, so that a copy or a new name doesn't make it downloadable, not copied, moved or renamed;
    every mode hides the entry it matches. Deleting, moving, copying or renaming a folder is refused
    when a rule tied to a place inside it (not one that starts with **) could make something in it
    inaccessible, or not downloadable when it is moved or copied;
 7. folder passwords (bypassed with ignore_password, never inside a share): the first rule in order that
    covers a path decides; the visitor must have unlocked that rule (or send its password). For an
    operation on a whole folder, every rule that could cover the folder or anything in it is needed, up
    to the first one that covers the folder itself (D12: decided from the expressions alone, no walk).

Rules match storage-absolute paths in the canonical form of apps/storages/rules.py. Errors are AccessDenied,
an API error with a stable `code` (D38) the pages branch on.

Unlocked folder passwords (D11) are kept per rule and revision: in the session, and with 记住密码 also
in a signed cookie for 30 days (FolderKeys). Changing a rule's expression or password gives it a new
revision, which ends what was unlocked with the old one. Wrong passwords are counted per address: after
10 in a minute every try is refused for the rest of the minute, right ones too.
"""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass
from functools import cached_property
from typing import Any, Iterable

from django.core import signing
from django.core.cache import cache
from django.conf import settings
from django.utils.translation import gettext as _, ngettext
from rest_framework.exceptions import APIException, ErrorDetail

from apps.accounts.models import Kind, User
from apps.accounts.permissions import Permission
from apps.core import net
from apps.core.throttles import address_key

from . import config as storage_config, instances
from .drivers import paths
from .drivers.base import InvalidPath
from .models import FilterMode, FilterRule, FolderPassword, Grant, ReadmePathMode, ReadmeRule, StorageSource
from .rules import GlobError, canonical, compile_glob

# --- errors ---------------------------------------------------------------------------------------------

STORAGE_NOT_FOUND = "storage_not_found"
STORAGE_DISABLED = "storage_disabled"
LOGIN_REQUIRED = "login_required"
ACTOR_DISABLED = "actor_disabled"
NO_GRANT = "no_grant"
PERMISSION_DENIED = "permission_denied"
INVALID_PATH = "invalid_path"
OUTSIDE_ROOT = "outside_root"
INACCESSIBLE = "inaccessible"
DOWNLOAD_DISABLED = "download_disabled"
PASSWORD_REQUIRED = "password_required"
PASSWORD_WRONG = "password_wrong"
PASSWORD_THROTTLED = "password_throttled"
RULE_NOT_FOUND = "rule_not_found"


class AccessDenied(APIException):
    """Refused, with a stable code: {"detail": "...", "code": "...", ...more}. A view of the API lets it
    through (DRF answers with its status); a page renders its message."""

    status_code = 403

    def __init__(self, code: str, message: str, *, status: int = 403, **extra: Any):
        super().__init__(message, code)
        self.status_code = status
        self.code = code
        self.message = message
        self.extra = extra
        self.detail = {"detail": ErrorDetail(message, code), "code": code, **extra}
        if "wait" in extra:
            self.wait = extra["wait"]

    def __str__(self):
        return self.message


# --- operations -----------------------------------------------------------------------------------------

LIST = "list"  # list a folder
STAT = "stat"  # see one file or folder (no content)
READ = "read"  # the content, to preview or download (preview or download permission)
PREVIEW = "preview"
DOWNLOAD = "download"
UPLOAD = "upload"
MKDIR = "mkdir"
DELETE = "delete"
RENAME = "rename"
MOVE = "move"
COPY = "copy"
LINK = "link"  # make a path direct link
SHORT_LINK = "short_link"  # make a short link, and open one (its creator is checked again, D31)
OPEN_LINK = "open_link"  # open a path direct link: the visitor's download permission (D40a)
SHARE = "share"

_NEEDS: dict[str, tuple[str, ...]] = {
    LIST: (),
    STAT: (),
    READ: (Permission.PREVIEW, Permission.DOWNLOAD),
    PREVIEW: (Permission.PREVIEW,),
    DOWNLOAD: (Permission.DOWNLOAD,),
    UPLOAD: (Permission.UPLOAD,),
    MKDIR: (Permission.NEW_FOLDER,),
    DELETE: (Permission.DELETE,),
    RENAME: (Permission.RENAME,),
    MOVE: (Permission.MOVE,),
    COPY: (Permission.COPY,),
    LINK: (Permission.GENERATE_LINK,),
    SHORT_LINK: (Permission.GENERATE_SHORT_LINK,),
    OPEN_LINK: (Permission.DOWNLOAD,),
    SHARE: (Permission.CREATE_SHARE_LINK,),
}
OPERATIONS = frozenset(_NEEDS)
# ...and also these: a direct or short link hands out the file's content, like a download.
_ALSO_NEEDS: dict[str, tuple[str, ...]] = {LINK: (Permission.DOWNLOAD,), SHORT_LINK: (Permission.DOWNLOAD,)}
_READS = frozenset({READ, PREVIEW, DOWNLOAD, LINK, SHORT_LINK, OPEN_LINK})  # disable_download applies
# Links are handed to others: the filter rules apply to them whatever the actor's 忽略隐藏 says (it is his
# own view of the storage), as they do when the signed /pd/ link a link redirects to is served (W1: links
# never sign one that skips them).
_LINKS = frozenset({LINK, SHORT_LINK, OPEN_LINK})
_RELOCATES = frozenset({RENAME, MOVE, COPY})  # the content ends up at another path
_WHOLE_FOLDER = frozenset({DELETE, RENAME, MOVE, COPY})  # on a folder: everything in it
# The permissions that change a storage. Nobody has them on a storage whose backend can only be read (a share
# link, a release page: StorageDriver.can_write), whatever the grant says.
CHANGES = frozenset({Permission.NEW_FOLDER, Permission.UPLOAD, Permission.RENAME, Permission.MOVE, Permission.COPY,
                     Permission.DELETE})

PATH_MAX = 4096  # characters of a storage path a client may name (Linux's PATH_MAX)
DEPTH_MAX = 256  # and its segments


def clean_path(path: str | None) -> str:
    """A path a client sent, clean (paths.clean(); "" and None are "/"), or AccessDenied (invalid_path, 400).

    Besides what paths.clean() refuses, dots and spaces only (paths.dots_only(): ".", "..", ".. ")
    between backslashes are refused too: a server on Windows takes "\\" for a separator.
    Access.path() rebases it on the actor's root;
    a link's path is storage-absolute already (the root is in it)."""
    try:
        cleaned = paths.clean(path)
    except InvalidPath as error:
        raise AccessDenied(INVALID_PATH, _("Invalid path: %(message)s") % {"message": error.message}, status=400) from None
    for segment in paths.segments(cleaned):
        if "\\" in segment and any(part and paths.dots_only(part) for part in segment.split("\\")):
            raise AccessDenied(INVALID_PATH, _("Invalid path: it can't have a . or .. part."), status=400)
    return _within_limits(cleaned)


def _within_limits(storage_path: str) -> str:
    if len(storage_path) > PATH_MAX or storage_path.count("/") > DEPTH_MAX:
        raise AccessDenied(INVALID_PATH, ngettext("The path is too long: at most %(length)s character and %(depth)s levels.", "The path is too long: at most %(length)s characters and %(depth)s levels.", PATH_MAX) % {"length": PATH_MAX, "depth": DEPTH_MAX}, status=400)
    return storage_path


# --- who asks -------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Actor:
    """Whose grant a request uses. `share_root`: the request is inside a share of `user`'s, whose folder
    (a storage path, inside the owner's root) is then the root; ignore_hidden and ignore_password don't
    apply there (D32). `share_entries`: (storage path, is a folder) of what the share holds in that
    folder, when it holds only some of it (None: all of it)."""

    user: Any
    share_root: str | None = None
    share_entries: tuple[tuple[str, bool], ...] | None = None

    @property
    def is_guest(self) -> bool:
        return self.user.kind == Kind.GUEST

    @property
    def via_share(self) -> bool:
        return self.share_root is not None


def actor_for(request) -> Actor:
    """The logged-in user, else the guest."""
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        return Actor(user)
    return Actor(User.objects.guest())


def share_actor(owner, folder: str, entries: Iterable[tuple[str, bool]] | None = None) -> Actor:
    """A share's owner, with the share's folder as the root. entries: (name, is a folder) of what the
    share holds in its folder (a name that isn't a single clean segment is left out), or None when it
    holds the whole folder."""
    held = None
    if entries is not None:
        base = "" if folder == "/" else folder
        held = tuple((f"{base}/{name}", bool(is_dir)) for name, is_dir in entries
                     if isinstance(name, str) and name not in ("", ".", "..") and "/" not in name
                     and paths.is_clean(f"{base}/{name}"))
    return Actor(owner, share_root=folder, share_entries=held)


# --- the storage's rules --------------------------------------------------------------------------------

class StorageRules:
    """A storage's rules, read once per request: (rule, compiled glob) in order. A stored expression
    that doesn't compile any more is left out (expressions are checked when they are saved)."""

    def __init__(self, filters=(), passwords=(), readmes=()):
        self.filters = self._compiled(filters)
        self.passwords = self._compiled(passwords)
        self.readmes = self._compiled(readmes)
        self.inaccessible = [(rule, glob) for rule, glob in self.filters if rule.mode == FilterMode.INACCESSIBLE]
        self.no_download = [(rule, glob) for rule, glob in self.filters if rule.mode == FilterMode.DISABLE_DOWNLOAD]

    @classmethod
    def load(cls, storage: StorageSource) -> StorageRules:
        def ordered(model):
            return model.objects.filter(storage=storage).order_by("position", "id")

        return cls(ordered(FilterRule), ordered(FolderPassword), ordered(ReadmeRule))

    @staticmethod
    def _compiled(rules):
        loaded = []
        for rule in rules:
            try:
                loaded.append((rule, compile_glob(rule.expression)))
            except GlobError:
                continue
        return loaded


def password_decider(rules: StorageRules, path: str):
    """The folder-password rule that decides for a storage path: the first that covers it, or None."""
    folded = canonical(path)
    for rule, glob in rules.passwords:
        if glob.covers(folded):
            return rule
    return None


def filter_state(rules: StorageRules, path: str) -> dict[str, bool]:
    """What the filter rules say of one storage path (also for the admin's rule tester)."""
    folded = canonical(path)
    return {
        "hidden": any(glob.matches(folded) for _rule, glob in rules.filters)
        or any(glob.covers(folded) for _rule, glob in rules.inaccessible),
        "inaccessible": any(glob.covers(folded) for _rule, glob in rules.inaccessible),
        "download_disabled": any(glob.covers(folded) for _rule, glob in rules.no_download),
    }


# --- unlocked folder passwords --------------------------------------------------------------------------

SESSION_KEY = "hfile.folder_keys"
SESSION_MAX = 200
COOKIE_NAME = "hfile_folder_keys"
COOKIE_SALT = "hfile.storages.folder-keys"
COOKIE_AGE = 30 * 24 * 3600
COOKIE_MAX = 30

THROTTLE_FAILURES = 10
THROTTLE_WINDOW = 60


class FolderKeys:
    """The folder passwords this browser has unlocked: {"<storage id>:<rule id>": revision} in the
    session, and the remembered ones in a signed cookie, each bound to whoever is logged in (0 for
    nobody) and to an expiry. write() puts a changed cookie on the response."""

    def __init__(self, request):
        self.request = request
        self._remembered: list[list[int]] | None = None
        self._dirty = False

    @property
    def _session(self):
        return getattr(self.request, "session", None)

    def _who(self) -> int:
        user = getattr(self.request, "user", None)
        return int(user.pk) if user is not None and user.is_authenticated else 0

    def _cookie(self) -> list[list[int]]:
        if self._remembered is None:
            raw = getattr(self.request, "COOKIES", {}).get(COOKIE_NAME, "")
            entries: list[list[int]] = []
            if raw:
                try:
                    loaded = signing.loads(raw, salt=COOKIE_SALT, max_age=COOKIE_AGE)
                except signing.BadSignature:
                    loaded = []
                    self._dirty = True  # replace a bad one
                if isinstance(loaded, list):
                    now = int(time.time())
                    for item in loaded:
                        if (isinstance(item, list) and len(item) == 5 and all(type(n) is int for n in item)
                                and item[4] > now):
                            entries.append(item)
            self._remembered = entries
        return self._remembered

    def has(self, storage_id: int, rule_id: int, revision: int) -> bool:
        session = self._session
        if session is not None and session.get(SESSION_KEY, {}).get(f"{storage_id}:{rule_id}") == revision:
            return True
        who = self._who()
        return any(entry[:4] == [storage_id, rule_id, revision, who] for entry in self._cookie())

    def add(self, storage_id: int, rule_id: int, revision: int, *, remember: bool = False) -> None:
        session = self._session
        if session is not None:
            keys = dict(session.get(SESSION_KEY, {}))
            keys.pop(f"{storage_id}:{rule_id}", None)
            keys[f"{storage_id}:{rule_id}"] = revision
            while len(keys) > SESSION_MAX:
                keys.pop(next(iter(keys)))
            session[SESSION_KEY] = keys
        if remember:
            who = self._who()
            entries = [entry for entry in self._cookie() if entry[:2] != [storage_id, rule_id] or entry[3] != who]
            entries.append([storage_id, rule_id, revision, who, int(time.time()) + COOKIE_AGE])
            self._remembered = entries[-COOKIE_MAX:]
            self._dirty = True

    def forget(self) -> None:
        """清理密码文件夹缓存: everything unlocked in this browser."""
        session = self._session
        if session is not None:
            session.pop(SESSION_KEY, None)
        self._remembered = []
        self._dirty = True

    def write(self, response) -> None:
        if not self._dirty:
            return
        entries = self._cookie()
        if entries:
            response.set_cookie(
                COOKIE_NAME, signing.dumps(entries, salt=COOKIE_SALT, compress=True), max_age=COOKIE_AGE,
                httponly=True, samesite="Lax", secure=bool(getattr(settings, "SESSION_COOKIE_SECURE", False)),
            )
        else:
            response.delete_cookie(COOKIE_NAME, samesite="Lax")
        self._dirty = False


# Checking the wait, comparing and counting a failure happen under this lock, so requests sent at the
# same moment can't try more passwords than the limit (the count is in this process's cache).
_attempts = threading.Lock()


def _throttle_key(request) -> str:
    ip = net.client_ip(request) if request is not None else None
    return f"hfile:folder-password:{address_key(ip) if ip else 'unknown'}"


def password_wait(request) -> int:
    """Seconds until this address may try a folder password again; 0 when it may now."""
    now = time.time()
    history = [moment for moment in cache.get(_throttle_key(request), []) if moment > now - THROTTLE_WINDOW]
    if len(history) < THROTTLE_FAILURES:
        return 0
    return max(1, int(history[0] + THROTTLE_WINDOW - now) + 1)


def _password_failed(request) -> None:
    key = _throttle_key(request)
    now = time.time()
    history = [moment for moment in cache.get(key, []) if moment > now - THROTTLE_WINDOW]
    history.append(now)
    cache.set(key, history, THROTTLE_WINDOW)


def _key_pairs(keys: Iterable[Any]) -> frozenset[tuple[int, int]]:
    """(rule id, revision) pairs from tuples or two-int lists (a link's stored keys); others are ignored."""
    pairs = set()
    for key in keys or ():
        if isinstance(key, (list, tuple)) and len(key) == 2 and all(type(n) is int for n in key):
            pairs.add((key[0], key[1]))
    return frozenset(pairs)


def _same_password(given: str, expected: str) -> bool:
    """Compared without line breaks and spaces at either end, in constant time."""
    def tidy(text: str) -> bytes:
        return text.replace("\r", "").replace("\n", "").strip().encode("utf-8")

    return hmac.compare_digest(tidy(given), tidy(expected))


# --- the policy -----------------------------------------------------------------------------------------

def _storage(storage) -> StorageSource:
    if isinstance(storage, StorageSource):
        return storage
    found = None
    if isinstance(storage, str) and storage:
        found = StorageSource.by_key(storage)
    if found is None:
        raise AccessDenied(STORAGE_NOT_FOUND, _("The storage source doesn't exist."), status=404)
    return found


def open_storage(request, storage, *, actor: Actor | None = None) -> Access:
    """Checks 1 to 3 for a storage (a StorageSource or its key); the Access for the rest.

    `request` may be None (a background job): then nothing is unlocked and wrong passwords aren't
    counted per address."""
    storage = _storage(storage)
    if not storage.enabled:
        raise AccessDenied(STORAGE_DISABLED, _("This storage source is disabled."))
    actor = actor or actor_for(request)
    user = actor.user
    if not user.is_active:
        if actor.is_guest:
            raise AccessDenied(LOGIN_REQUIRED, _("Please log in first: this site doesn't allow anonymous access."), status=401)
        raise AccessDenied(ACTOR_DISABLED, _("The account of the person who shared this is disabled.") if actor.via_share else _("This account is disabled."))
    grant = Grant.objects.filter(user=user, storage=storage).first()
    if grant is None or not grant.enabled:
        if actor.is_guest:
            raise AccessDenied(LOGIN_REQUIRED, _("Please log in first: this storage source can't be used without logging in."), status=401)
        raise AccessDenied(NO_GRANT, _("The person who shared this can no longer use this storage source.") if actor.via_share else _("You don't have permission to use this storage source."))
    root = grant.root_path if paths.is_clean(grant.root_path) else None
    if root is None:
        raise AccessDenied(NO_GRANT, _("The root folder given on this storage source is set up wrong. Please contact the administrator."))
    if actor.via_share:
        folder = actor.share_root
        if not paths.is_clean(folder) or not paths.is_inside(folder, root):
            raise AccessDenied(OUTSIDE_ROOT, _("The shared folder is no longer within what the person who shared it can access."))
        root = folder
    return Access(request, storage, actor, grant, root)


def visible_storages(actor: Actor):
    """The storages the actor may use, in order: [(storage, grant)] (checks 1 to 3)."""
    if not actor.user.is_active:
        return []
    grants = (Grant.objects.filter(user=actor.user, enabled=True, storage__enabled=True)
              .select_related("storage").order_by("storage__position", "storage__id"))
    return [(grant.storage, grant) for grant in grants if paths.is_clean(grant.root_path)]


def _read_only(storage: StorageSource) -> bool:
    """Whether the storage's backend can only be read (its driver class's can_write)."""
    try:
        return not storage_config.driver(storage.type).can_write
    except storage_config.ConfigError:  # a type that can't be used now: nothing on it works anyway
        return False


def _no_uploads(storage: StorageSource) -> bool:
    """Whether files can't be uploaded to the storage's backend (its driver class's can_upload)."""
    try:
        return not storage_config.driver(storage.type).can_upload
    except storage_config.ConfigError:
        return False


class Access:
    """What one actor may do on one storage, for one request (open_storage)."""

    def __init__(self, request, storage: StorageSource, actor: Actor, grant: Grant, root: str):
        self.request = request
        self.storage = storage
        self.actor = actor
        self.grant = grant
        self.root = root
        self.read_only = _read_only(storage)
        self.no_uploads = _no_uploads(storage)
        refused = CHANGES if self.read_only else frozenset({Permission.UPLOAD}) if self.no_uploads else frozenset()
        self.permissions = frozenset(grant.permissions or ()) - refused
        self.bypass_filters = Permission.IGNORE_HIDDEN in self.permissions and not actor.via_share
        self.bypass_passwords = Permission.IGNORE_PASSWORD in self.permissions and not actor.via_share
        self.keys = FolderKeys(request) if request is not None else None
        # (rule id, revision) that a password sent with a check() opened: for that check only, but
        # unlocked_keys() counts them (the link's creator knew the password).
        self.opened: set[tuple[int, int]] = set()

    @cached_property
    def rules(self) -> StorageRules:
        return StorageRules.load(self.storage)

    @property
    def driver(self):
        try:
            return instances.get(self.storage)
        except instances.StorageUnavailable as error:
            raise AccessDenied(STORAGE_DISABLED, error.message) from None

    # -- paths

    def path(self, relative: str | None) -> str:
        """A path the client sent (relative to its root; "" and None are the root) -> the storage path,
        checked by clean_path()."""
        return _within_limits(paths.rebase(clean_path(relative), self.root))

    def relative(self, storage_path: str) -> str:
        """The client's path of a storage path (inside the root)."""
        if not self.contains(storage_path):
            raise AccessDenied(OUTSIDE_ROOT, _("This location can't be opened."))
        return paths.relative(storage_path, self.root)

    def contains(self, storage_path: str) -> bool:
        return isinstance(storage_path, str) and paths.is_clean(storage_path) and paths.is_inside(storage_path, self.root)

    # -- permissions

    def has(self, permission: str) -> bool:
        return permission in self.permissions

    def may(self, operation: str) -> bool:
        needs = _NEEDS[operation]
        if needs and not any(code in self.permissions for code in needs):
            return False
        return all(code in self.permissions for code in _ALSO_NEEDS.get(operation, ()))

    def _permission(self, operation: str, created: tuple[str, ...] = ()) -> None:
        if operation not in _NEEDS:
            raise ValueError(f"unknown operation {operation!r}")
        if not self.may(operation):
            needs = _NEEDS[operation]
            if not any(code in self.permissions for code in needs):
                missing = needs[0]
                if self.read_only and missing in CHANGES:
                    message = _("This storage source is read-only.")
                elif self.no_uploads and missing == Permission.UPLOAD:
                    message = _("Files can't be uploaded to this storage source.")
                elif operation == READ:
                    message = _("You don't have permission to preview or download.")
                else:
                    message = _("You don't have the \"%(label)s\" permission.") % {"label": Permission(missing).label}
            else:
                missing = next(code for code in _ALSO_NEEDS[operation] if code not in self.permissions)
                message = _('"%(label)s" also needs the "%(needed)s" permission.') % {"label": Permission(needs[0]).label, "needed": Permission(missing).label}
            raise AccessDenied(PERMISSION_DENIED, message, permission=missing)
        if operation == UPLOAD and created and not self.has(Permission.NEW_FOLDER):
            raise AccessDenied(PERMISSION_DENIED, _('Uploading folders also needs the "New folder" permission.'), permission=Permission.NEW_FOLDER)

    def _confined(self, *storage_paths: str) -> None:
        for storage_path in storage_paths:
            if not self.contains(storage_path):
                raise AccessDenied(OUTSIDE_ROOT, _("This location can't be opened."))

    # -- the checks

    def check(self, operation: str, path: str, *, is_dir: bool | None = None, password: str | None = None,
              created: Iterable[str] = (), keys: Iterable[Any] = ()) -> None:
        """May the actor do `operation` on the storage path `path`? Raises AccessDenied.

        is_dir: `path` is a folder (for DELETE, RENAME, MOVE and COPY that means everything in it); those
            four must say it (TypeError otherwise), a forgotten True would skip the whole-folder checks.
        password: a folder password sent with the request (API clients); it unlocks the rules it opens
            for this request only.
        created: for UPLOAD and MKDIR, the folders the operation creates on the way (when unsure, every
            level below the deepest folder known to exist); each must pass on its own.
        keys: (rule id, revision) pairs to count as unlocked, e.g. what a short link's creator unlocked
            (unlocked_keys(); lists of two ints, as JSON gives them back, are fine).
        """
        if is_dir is None and operation in _WHOLE_FOLDER:
            raise TypeError(f"check({operation!r}) needs is_dir")
        created = tuple(created)
        self._permission(operation, created)
        self._confined(path, *created)
        if not self.bypass_filters or operation in _LINKS:
            self._filters(operation, path, is_dir=is_dir, created=created)
        if not self.bypass_passwords:
            self._passwords([path, *created], whole_folder=is_dir and operation in _WHOLE_FOLDER,
                            password=password, keys=keys)

    def check_target(self, operation: str, path: str, *, is_dir: bool | None = None, password: str | None = None,
                     keys: Iterable[Any] = ()) -> None:
        """Where RENAME, MOVE or COPY puts something: `path` is its new storage path. Raises AccessDenied."""
        if operation not in _RELOCATES:
            raise ValueError(f"{operation!r} has no target")
        if is_dir is None:
            raise TypeError(f"check_target({operation!r}) needs is_dir")
        self._permission(operation)
        self._confined(path)
        if not self.bypass_filters:
            folded = canonical(path)
            if any(glob.covers(folded) for _rule, glob in self.rules.inaccessible):
                raise AccessDenied(INACCESSIBLE, _("It can't go there: that location can't be opened."))
        if not self.bypass_passwords:
            self._passwords([path], whole_folder=is_dir, password=password, keys=keys)

    def _filters(self, operation: str, path: str, *, is_dir: bool, created: tuple[str, ...]) -> None:
        rules = self.rules
        for target in (path, *created):
            folded = canonical(target)
            if any(glob.covers(folded) for _rule, glob in rules.inaccessible):
                raise AccessDenied(INACCESSIBLE, _("This location can't be opened."))
        folded = canonical(path)
        if operation in _READS and any(glob.covers(folded) for _rule, glob in rules.no_download):
            raise AccessDenied(DOWNLOAD_DISABLED, _("This file can't be downloaded."))
        if operation in _RELOCATES:
            if any(glob.covers(folded) for _rule, glob in rules.no_download):
                raise AccessDenied(DOWNLOAD_DISABLED, _("Files that can't be downloaded can't be copied, moved or renamed."))
            if is_dir and any(glob.protects_within(folded) for _rule, glob in rules.no_download):
                raise AccessDenied(DOWNLOAD_DISABLED, _("This folder has files that can't be downloaded, so it can't be copied, moved or renamed as a whole."))
        if is_dir and operation in _WHOLE_FOLDER:
            if any(glob.protects_within(folded) for _rule, glob in rules.inaccessible):
                raise AccessDenied(INACCESSIBLE, _("This folder has content that can't be opened, so it can't be deleted, moved, copied or renamed as a whole."))

    def _needed_rules(self, path: str, *, whole_folder: bool) -> list[tuple[Any, Any]]:
        folded = canonical(path)
        if not whole_folder:
            for rule, glob in self.rules.passwords:
                if glob.covers(folded):
                    return [(rule, glob)]
            return []
        needed = []
        for rule, glob in self.rules.passwords:
            if glob.may_cover_within(folded):
                needed.append((rule, glob))
                if glob.covers(folded):
                    break  # it decides for everything at and below the folder: later rules never do
        return needed

    def _unlocked(self, rule, keys: frozenset[tuple[int, int]] = frozenset()) -> bool:
        if (rule.pk, rule.revision) in keys:
            return True
        return self.keys is not None and self.keys.has(self.storage.pk, rule.pk, rule.revision)

    def _hint(self, rule, glob, path: str, covers: bool) -> str | None:
        """Where the client can unlock `rule` by a path: the path itself, or the rule's folder (in the
        admin's spelling), if that is inside the actor's root and the rule decides there. None for
        rules found nowhere in particular (**/private): the client unlocks those by their id."""
        if covers:
            candidate = path
        else:
            expression = glob.expression
            cut = min((expression.find(ch) for ch in "*?" if ch in expression), default=len(expression))
            candidate = expression if cut == len(expression) else expression[:cut].rpartition("/")[0] or "/"
            try:
                candidate = paths.clean(candidate)
            except InvalidPath:
                return None
        if not self.contains(candidate):
            return None
        if not covers and not self.bypass_filters:
            state = filter_state(self.rules, candidate)
            if state["hidden"] or state["inaccessible"]:
                return None  # below the asked path and hidden: don't name it
        decider = password_decider(self.rules, candidate)
        if decider is None or decider.pk != rule.pk:
            return None
        return self.relative(candidate)

    def _passwords(self, targets: list[str], *, whole_folder: bool, password: str | None,
                   keys: Iterable[tuple[int, int]]) -> None:
        if not self.rules.passwords:
            return
        keys = _key_pairs(keys)
        locked = []
        seen = set()
        for index, target in enumerate(targets):
            for rule, glob in self._needed_rules(target, whole_folder=whole_folder and index == 0):
                if rule.pk in seen or self._unlocked(rule, keys):
                    continue
                seen.add(rule.pk)
                locked.append((rule, glob, target))
        if not locked:
            return
        if password:
            with _attempts:
                wait = password_wait(self.request)
                if wait:
                    raise AccessDenied(PASSWORD_THROTTLED, ngettext("Too many wrong passwords. Please try again in %(wait)s second.", "Too many wrong passwords. Please try again in %(wait)s seconds.", wait) % {"wait": wait}, status=429, wait=wait)
                opened = [item for item in locked if _same_password(password, item[0].password)]
                if not opened:
                    _password_failed(self.request)
            self.opened.update((item[0].pk, item[0].revision) for item in opened)
            if not opened:
                rule, glob, target = locked[0]
                raise AccessDenied(PASSWORD_WRONG, _("Wrong password."), rule=rule.pk,
                                   path=self._hint(rule, glob, target, glob.covers(canonical(target))))
            locked = [item for item in locked if item not in opened]
            if not locked:
                return
        rule, glob, target = locked[0]
        covers = glob.covers(canonical(target))
        hint = self._hint(rule, glob, target, covers)
        if len(locked) == 1 and covers:
            message = _("This folder needs a password.")
        elif hint:
            message = _('This also needs the password of "%(hint)s" first.') % {"hint": hint}
        else:
            message = _('This involves folders that need a password: enter it first (or ask the administrator for the "Ignore passwords" permission).')
        raise AccessDenied(PASSWORD_REQUIRED, message, rule=rule.pk, path=hint)

    # -- listings, folders, readmes

    def visible(self, entries: Iterable[Any], *, keys: Iterable[Any] = ()) -> list[Any]:
        """Entries (with a storage `path`) as a listing or a search shows them: without those a filter
        rule hides, nor those inside a folder whose password this browser hasn't entered (a password
        folder itself is shown, with its lock). keys: (rule id, revision) pairs to count as unlocked
        too, as for check() (access.opened: the rules a password sent with this request opened)."""
        entries = list(entries)
        keys = _key_pairs(keys)
        filters = self.rules.filters if not self.bypass_filters else []
        passwords = bool(self.rules.passwords) and not self.bypass_passwords
        if not filters and not passwords:
            return entries
        locked: dict[str, bool] = {}
        shown = []
        for entry in entries:
            folded = canonical(entry.path)
            if filters and (any(glob.matches(folded) for _rule, glob in filters)
                            or any(glob.covers(folded) for _rule, glob in self.rules.inaccessible)):
                continue
            if passwords:
                parent = entry.path.rpartition("/")[0] or "/"
                if parent not in locked:
                    locked[parent] = not self.is_unlocked(parent, keys=keys)
                if locked[parent]:
                    continue
            shown.append(entry)
        return shown

    def password_rule(self, path: str):
        """The folder-password rule that decides for a storage path, unlocked or not; None without one
        (or with ignore_password)."""
        if self.bypass_passwords:
            return None
        return password_decider(self.rules, path)

    def is_unlocked(self, path: str, *, keys: Iterable[Any] = ()) -> bool:
        rule = self.password_rule(path)
        return rule is None or self._unlocked(rule, _key_pairs(keys))

    def unlocked_keys(self, path: str, *, is_dir: bool = False, operation: str = READ) -> list[tuple[int, int]]:
        """(rule id, revision) of the rules `path` needs that this browser has unlocked, for a link to
        remember what its creator opened (check() first: a rule still locked is not in the list)."""
        if self.bypass_passwords:
            return []
        whole = is_dir and operation in _WHOLE_FOLDER
        return [(rule.pk, rule.revision) for rule, _glob in self._needed_rules(path, whole_folder=whole)
                if self._unlocked(rule) or (rule.pk, rule.revision) in self.opened]

    def unlock(self, path: str | None = None, *, password: str, rule_id: int | None = None,
               remember: bool = False):
        """Unlock the rule that decides for a storage path (or the rule with this id) with its password.
        Returns the rule, or None when there is nothing to unlock. Raises AccessDenied (wrong, throttled).
        The view then calls keys.write(response) for 记住密码."""
        if self.bypass_passwords:
            return None
        if rule_id is not None:
            # Only a rule that may decide somewhere the actor can reach: inside his root, or in a share
            # inside the entries it holds. Not a way to try the passwords of other folders.
            rule = next((rule for rule, glob in self.rules.passwords
                         if rule.pk == rule_id and self._reaches(glob)), None)
            if rule is None:
                raise AccessDenied(RULE_NOT_FOUND, _("There is no such password rule; the administrator may have just changed it. Please refresh and try again."), status=404)
        else:
            self._confined(path)
            # A folder that can't be opened has no password to try (unless the actor sees through filters).
            if not self.bypass_filters and filter_state(self.rules, path)["inaccessible"]:
                raise AccessDenied(INACCESSIBLE, _("This location can't be opened."))
            rule = password_decider(self.rules, path)
            if rule is None:
                return None
        with _attempts:
            wait = password_wait(self.request)
            if wait:
                raise AccessDenied(PASSWORD_THROTTLED, ngettext("Too many wrong passwords. Please try again in %(wait)s second.", "Too many wrong passwords. Please try again in %(wait)s seconds.", wait) % {"wait": wait}, status=429, wait=wait)
            right = isinstance(password, str) and _same_password(password, rule.password)
            if not right:
                _password_failed(self.request)
        if not right:
            raise AccessDenied(PASSWORD_WRONG, _("Wrong password."), rule=rule.pk)
        if self.keys is not None:
            self.keys.add(self.storage.pk, rule.pk, rule.revision, remember=remember)
        return rule

    def _reaches(self, glob) -> bool:
        """Could the rule decide anywhere the actor can reach?"""
        entries = self.actor.share_entries
        if entries is None:
            return glob.may_cover_within(canonical(self.root))
        return any(glob.may_cover_within(canonical(path)) if is_dir else glob.covers(canonical(path))
                   for path, is_dir in entries)

    def readme(self, folder: str):
        """The readme rule for a folder (storage path): the first whose expression matches the folder,
        relative to the actor's root or absolute, as the rule says; None without one. With the storage's
        readme_from_file on, no rule applies (None): the caller shows the folder's README.md instead."""
        if self.storage.readme_from_file:
            return None
        absolute = canonical(folder)
        relative = canonical(self.relative(folder))
        for rule, glob in self.rules.readmes:
            if glob.matches(absolute if rule.path_mode == ReadmePathMode.ABSOLUTE else relative):
                return rule
        return None


def check_served(storage: StorageSource, path: str, *, operation: str = DOWNLOAD, ignore_hidden: bool = False) -> None:
    """For a signed link being served (/pd/, D10): the storage is still enabled and the filter rules
    still let the file be read. The signature stands for the rest (who asked, the folder password)."""
    if not storage.enabled:
        raise AccessDenied(STORAGE_DISABLED, _("This storage source is disabled."))
    if not paths.is_clean(path):
        raise AccessDenied(INVALID_PATH, _("Invalid path."), status=400)
    if ignore_hidden:
        return
    rules = StorageRules.load(storage)
    state = filter_state(rules, path)
    if state["inaccessible"]:
        raise AccessDenied(INACCESSIBLE, _("This location can't be opened."))
    if operation in _READS and state["download_disabled"]:
        raise AccessDenied(DOWNLOAD_DISABLED, _("This file can't be downloaded."))
