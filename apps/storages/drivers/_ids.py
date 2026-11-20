"""A base for the drivers of backends that address files and folders by id, not by path (115, most cloud
drives); not a driver itself. 115's driver (open115.py) is where this comes from.

Paths
    The storage root ("/") is the folder `base_path` of the account; "/a/b" is base_path + "/a/b". The
    backend knows files and folders by id only: a path is found by listing each folder from the account's
    root (root_id). Folder ids are cached for `folder_ttl` seconds, and a listing forgets the cached folders
    below it that are gone or are another folder now; a folder moved or renamed in the backend's own apps
    can still be reached through its old path until one of these happens. Our own renames, moves and
    deletes drop the cached ids at and below the old path at once, and an id the backend no longer knows
    (_fetch raises StaleFolder) is dropped and looked up again. Files are found in the listing of their
    folder, which is cached for `listing_ttl` seconds from when the backend gave it; list() itself always
    asks the backend.

    Our own changes update the cached listings in place: a delete takes the node out, a move takes it out
    of one listing and into the other, a rename renames it, and a new folder or an upload the backend
    describes is added (with our clock's time until a listing gives the backend's). A batch of changes in
    one big folder so lists it once, not once per item. An updated listing still expires `listing_ttl`
    seconds after the backend gave it, so changes made elsewhere show up as soon as before. When we don't
    know the new node (a copy, an upload the backend doesn't describe) or a call failed (the backend may
    have done it or not), the listing is dropped and asked for again.

    Many such backends allow several files or folders with the same name in one folder. HFile never makes
    such duplicates: write, mkdir, rename, move and copy look at the target folder first, under a lock per
    folder and name, so two requests can't both create a name. That look trusts a listing the backend gave
    at most `guard_ttl` seconds ago (a few), else it asks again; a name made in the backend's own apps
    during those seconds can be missed (as one made while a listing is being fetched always could). Of
    existing duplicates a listing shows one, the same one a path finds: folders before files, then the most
    recently modified, then the larger id.

What a subclass gives
    root_id, or _account_root()   the id of the account's root folder;
    _fetch(folder_id)             everything in the folder (all pages) as Nodes; a subclass of Node may
                                  carry more of what the backend gave (115's pick code). StaleFolder when
                                  the backend doesn't know the id, or answers with another folder;
    _create_node(parent_id, name) makes a folder and gives its Node, with its id;
    _delete_node(node, parent_id), _rename_node(node, name), _move_node(node, source_id, target_id);
    _copy_node(node, target_id)   the copy's Node, or None when the backend doesn't say (the folder is
                                  then listed again);
    _upload_node(folder_id, name, data, size)
                                  stores a file, gives its Node, or None when the backend doesn't say;
    _read_node(node, offset, length)
                                  the file's bytes, as open();
    upload_conflict               what the backend does with an upload whose name is taken: "replace" (it
                                  replaces that file: its overwrite mode), or "keep" (it keeps both, or
                                  names the new one otherwise, like "a(1).txt"); with "keep" the new file is
                                  found, then the old one deleted and the new one renamed back.
Errors of the backend come as DriverError from the hooks: AlreadyExists when a name is taken, NotFound
when a node is gone.
"""

from __future__ import annotations

import contextlib
import dataclasses
import datetime
import logging
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from typing import Any, BinaryIO, ClassVar

from django.utils.translation import gettext

from . import paths
from ._cloud import KeyedLocks, TTLCache
from .base import (
    AlreadyExists,
    BackendError,
    DriverError,
    Entry,
    InvalidPath,
    NotADirectory,
    NotFound,
    ReadStream,
    StorageDriver,
)


@dataclasses.dataclass(frozen=True)
class Node:
    """A file or folder of the backend, as a listing gave it."""

    id: str
    name: str
    is_dir: bool
    size: int | None
    modified: datetime.datetime | None


def id_key(value: str) -> tuple[int, int | str]:
    """Orders ids: numbers by value, before anything else. Not "²", which isdigit() takes (STOR-17)."""
    return (0, int(value)) if value.isascii() and value.isdigit() else (1, value)


def rank(node: Node) -> tuple:
    """Which of several same-named nodes a path finds: folders, then the newest, then the larger id."""
    stamp = node.modified.timestamp() if node.modified is not None else 0.0
    return node.is_dir, stamp, id_key(node.id)


@dataclasses.dataclass(frozen=True)
class Listing:
    folder_id: str
    nodes: tuple[Node, ...]  # all of them, duplicates included
    by_name: Mapping[str, Node]  # the one node per name that a path finds
    fetched: float  # time.monotonic() when the backend gave it; kept by our in-place updates


def make_listing(folder_id: str, nodes: Iterable[Node], fetched: float) -> Listing:
    nodes = tuple(nodes)
    by_name: dict[str, Node] = {}
    for node in nodes:
        other = by_name.get(node.name)
        if other is None or rank(node) > rank(other):
            by_name[node.name] = node
    return Listing(folder_id, nodes, by_name, fetched)


def now() -> datetime.datetime:
    """The time of something we just made, until a listing gives the backend's own (a moment later)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)


class StaleFolder(Exception):
    """The backend doesn't know the folder id (any more), or answered a listing with another folder."""


class IdDriver(StorageDriver):
    """The base of drivers of backends that address files and folders by id (see the module's docstring)."""

    root_id: ClassVar[str] = "0"
    upload_conflict: ClassVar[str] = "keep"  # "keep" | "replace"
    listing_ttl: ClassVar[float] = 30.0  # seconds a folder listing serves lookups, from its fetch
    guard_ttl: ClassVar[float] = 5.0  # seconds it can also tell that a name is free (_guard)
    folder_ttl: ClassVar[float] = 300.0  # seconds a folder id is trusted without listing its parent
    service: ClassVar[Any] = ""  # the backend's name in messages, e.g. gettext_lazy("115")

    def __init__(self, config: Mapping[str, Any], *, on_update: Callable[[dict[str, Any]], None] | None = None):
        super().__init__(config, on_update=on_update)
        self.log = logging.getLogger(f"hfile.storage.{self.type}")
        self._base = paths.clean(self.config.get("base_path") or "/")
        self._folders = TTLCache(10000, self.folder_ttl)  # storage path -> folder id ("/": the base)
        self._names = KeyedLocks()  # (folder id, name) of what is being created
        self._listings = TTLCache(256, self.listing_ttl)  # storage path -> Listing
        # Held while _changes, the cached listings and the cached folder ids change, so that two
        # changes of one folder don't lose each other's in-place updates and a listing fetched
        # during a change isn't cached; reentrant: a move updates two folders in one step.
        self._changes_lock = threading.RLock()
        self._changes = 0  # counts our changes, so a listing fetched meanwhile isn't cached

    @staticmethod
    def base_path_errors(config: Mapping[str, Any]) -> dict[str, str]:
        """validate_config()'s check of `base_path`, for subclasses."""
        try:
            paths.clean(config.get("base_path") or "/")
        except InvalidPath as error:
            return {"base_path": gettext("Invalid base path: %(message)s") % {"message": error.message}}
        return {}

    # -- what a subclass gives

    def _account_root(self) -> str:
        return self.root_id

    def _fetch(self, folder_id: str) -> list[Node]:
        raise NotImplementedError

    def _create_node(self, parent_id: str, name: str) -> Node:
        raise NotImplementedError

    def _delete_node(self, node: Node, parent_id: str) -> None:
        raise NotImplementedError

    def _rename_node(self, node: Node, name: str) -> None:
        raise NotImplementedError

    def _move_node(self, node: Node, source_id: str, target_id: str) -> None:
        raise NotImplementedError

    def _copy_node(self, node: Node, target_id: str) -> Node | None:
        raise NotImplementedError

    def _upload_node(self, folder_id: str, name: str, data: BinaryIO, size: int) -> Node | None:
        raise NotImplementedError

    def _read_node(self, node: Node, offset: int, length: int | None) -> ReadStream:
        raise NotImplementedError

    def _cacheable(self, node: Node) -> bool:
        """Whether a node an upload gave can go into the cached listing as it is; else the folder is listed again
        (115 may leave out the pick code that downloads need)."""
        return True

    # -- what is cached

    def _change_count(self) -> int:
        with self._changes_lock:
            return self._changes

    def _forget(self, path: str) -> None:
        """Drop what we know about `path` and everything below it."""
        with self._changes_lock:
            self._changes += 1
            self._folders.discard_if(lambda key: paths.is_inside(key, path))
            self._listings.discard_if(lambda key: paths.is_inside(key, path))

    def _touched(self, folder: str) -> None:
        """Something in the folder `folder` changed and we don't know what: drop its listing."""
        with self._changes_lock:
            self._changes += 1
            self._listings.pop(folder)

    def _unsure(self, *changed: str) -> None:
        """The backend may have changed these paths in a way we don't know: forget what is cached at and
        below them, and the listings of their folders."""
        for path in changed:
            self._forget(path)
            if path != paths.ROOT:
                self._touched(paths.parent(path))

    @contextlib.contextmanager
    def _changing(self, *changed: str) -> Iterator[None]:
        """Around a call that changes the paths `changed`: when it fails, the backend may have done it or
        not (a timeout, a broken connection), so what is cached about them isn't trusted any more."""
        try:
            yield
        except BaseException:
            self._unsure(*changed)
            raise

    def _cached(self, path: str, within: float) -> Listing | None:
        """The cached listing of the folder at `path`, if the backend gave it at most `within` seconds ago."""
        listing = self._listings.get(path)
        if listing is None or time.monotonic() - listing.fetched > within:
            return None
        return listing

    def _edit(self, folder: str, folder_id: str, *, gone: Iterable[Node] = (), made: Iterable[Node] = ()) -> None:
        """A change of ours in the folder went through: the nodes `gone` left it, the nodes `made` are in
        it now (each replacing the node with its id, if any).

        The cached listing is updated in place, by make_listing's rules, rather than dropped, so that the
        next change in this folder doesn't list it again. It keeps its fetch time and still expires
        listing_ttl seconds after the backend gave it. Taking out a node that isn't there or putting in
        one that is changes nothing, so it doesn't matter whether a listing fetched meanwhile already
        shows this change; one whose fetch overlapped it isn't cached anyway (_listing). Without a cached
        listing only the folder ids at the names are put right.
        """
        gone, made = list(gone), list(made)
        with self._changes_lock:
            self._changes += 1
            listing = self._cached(folder, self.listing_ttl)
            if listing is None or listing.folder_id != folder_id:
                self._listings.pop(folder)
                listing = None
            else:
                ids = {node.id for node in gone + made}
                nodes = [node for node in listing.nodes if node.id not in ids] + made
                listing = make_listing(folder_id, nodes, listing.fetched)
                self._listings.put(folder, listing)
            for name in {node.name for node in gone + made}:
                if listing is not None:
                    found = listing.by_name.get(name)
                else:  # what we made is there; anything else shows when the folder is listed
                    found = next((node for node in made if node.name == name), None)
                self._settle(folder, name, found)

    def _settle(self, folder: str, name: str, node: Node | None) -> None:
        """The path folder/name finds `node` now (None: nothing, or not known): make the cached folder ids
        and listings at and below that path agree. Called under _changes_lock."""
        child = paths.join(folder, name)
        folder_id = node.id if node is not None and node.is_dir else None
        if folder_id is None or self._folders.get(child) != folder_id:
            self._folders.discard_if(lambda key: paths.is_inside(key, child))
            self._listings.discard_if(lambda key: paths.is_inside(key, child))
        if folder_id is not None:
            self._folders.put(child, folder_id)

    def _base_missing(self) -> NotFound:
        return NotFound(gettext("The base path %(base)s doesn't exist in %(service)s.")
                        % {"base": self._base, "service": self.service})

    def _root_id(self) -> str:
        """The id of the folder base_path."""
        cached = self._folders.get("/")
        if cached is not None:
            return cached
        folder_id = self._account_root()
        for name in paths.segments(self._base):
            try:
                listing = make_listing(folder_id, self._fetch(folder_id), time.monotonic())
            except StaleFolder:
                raise self._base_missing() from None
            node = listing.by_name.get(name)
            if node is None or not node.is_dir:
                raise self._base_missing()
            folder_id = node.id
        self._folders.put("/", folder_id)
        return folder_id

    def _folder_id(self, path: str) -> str:
        """The id of the folder at storage path `path`. NotFound, NotADirectory."""
        if path == paths.ROOT:
            return self._root_id()
        cached = self._folders.get(path)
        if cached is not None:
            return cached
        changes = self._change_count()
        node = self._node(path)
        if not node.is_dir:
            raise NotADirectory()
        with self._changes_lock:
            if self._changes == changes:  # else a change of ours may have moved it meanwhile
                self._folders.put(path, node.id)
        return node.id

    def _listing(self, path: str, *, fresh: bool = False, folder_id: str | None = None) -> tuple[Listing, bool]:
        """The listing of the folder at `path`, and whether it came from the cache.

        `folder_id`: the folder's id, when the caller has just found it to put something in it. It is
        listed as it is: looking the path up again can go through a cached listing that doesn't show the
        folder yet, when a request of ours made it and the backend's answer hasn't come back (the listing
        is updated then, _edit). That listing is cached only when the path is known to lead to the id, as
        when _folder_id gives it from the cache.
        """
        if not fresh:
            cached = self._cached(path, self.listing_ttl)
            if cached is not None:
                return cached, True
        known = folder_id
        for attempt in range(2):
            changes = self._change_count()  # before the id: a move or rename may take it from `path`
            if known is not None and not attempt:
                folder_id, trusted = known, self._folders.get(path) == known
            else:
                folder_id, trusted = self._folder_id(path), True
            try:
                nodes = self._fetch(folder_id)
            except StaleFolder:
                # Deleted or moved behind our back: find the path again from its parent.
                self._forget(path)
                if path != paths.ROOT:
                    self._touched(paths.parent(path))
                if attempt:
                    raise (self._base_missing() if path == paths.ROOT else NotFound()) from None
                continue
            listing = make_listing(folder_id, nodes, time.monotonic())
            if len(listing.by_name) < len(listing.nodes):
                self.log.debug("folder %s has entries with the same name; showing one of each", folder_id)
            folders = {node.name: node.id for node in listing.by_name.values() if node.is_dir}
            depth = len(paths.segments(path))

            def gone(key: str) -> bool:  # a cached path below one of the children that isn't there now
                return key != path and paths.is_inside(key, path) and paths.segments(key)[depth] not in folders

            with self._changes_lock:
                # Kept only when no change of ours went through while the backend was asked: the answer
                # may be from before it, and its in-place update went to what was cached then.
                current = trusted and self._changes == changes
                if current:
                    self._listings.put(path, listing)
                self._folders.discard_if(gone)
                self._listings.discard_if(gone)
                for name, node_id in folders.items():
                    child = paths.join(path, name)
                    if self._folders.get(child) not in (None, node_id):  # another folder has the name now
                        self._folders.discard_if(lambda key, child=child: paths.is_inside(key, child))
                        self._listings.discard_if(lambda key, child=child: paths.is_inside(key, child))
                    if current:
                        self._folders.put(child, node_id)
            return listing, False
        raise AssertionError("unreachable")

    def _node(self, path: str) -> Node:
        """The file or folder at `path` (not the root). NotFound."""
        parent, name = paths.split(path)
        try:
            listing, cached = self._listing(parent)
        except NotADirectory:
            raise NotFound() from None
        # A miss in a cached listing (at most listing_ttl old) is believed: asking the backend again for
        # every missing name (a site looking for readme files) would cost a listing each time.
        node = listing.by_name.get(name)
        if node is None:
            raise NotFound()
        return node

    def _ensure_folder(self, path: str) -> str:
        """The id of the folder at `path`, created with the missing ones above it."""
        try:
            return self._folder_id(path)
        except NotFound:
            if path == paths.ROOT:
                raise
        parent, name = paths.split(path)
        parent_id = self._ensure_folder(parent)
        try:
            return self._create_folder(parent, parent_id, name)
        except AlreadyExists:  # made meanwhile, or a file has the name
            node = self._listing(parent, fresh=True, folder_id=parent_id)[0].by_name.get(name)
            if node is not None and node.is_dir:
                return node.id
            raise NotADirectory(gettext("%(path)s is a file, not a folder.") % {"path": path}) from None

    def _create_folder(self, parent: str, parent_id: str, name: str) -> str:
        path = paths.join(parent, name)
        with self._names.hold((parent_id, name)):
            self._check_free(parent, parent_id, name)
            with self._changing(path):
                node = self._create_node(parent_id, name)
            self._edit(parent, parent_id, made=[node])
            return node.id

    def _guard(self, folder: str, folder_id: str) -> Listing:
        """The listing that tells whether a name is free in the folder at `folder`, whose id `folder_id`
        the caller has just found, before we put something there under that name.

        A cached listing is what the backend gave plus our own changes since (anything else drops it),
        and it is trusted here when the backend gave it at most guard_ttl seconds ago; else the backend is
        asked. So a batch of moves or uploads into a big folder lists it every few seconds rather than
        once per item. The price: a name made in the backend's own apps during those seconds isn't seen.
        Our own requests can't race like this: the lock per folder and name keeps them apart, and each
        sees the others' in-place updates.
        """
        cached = self._cached(folder, self.guard_ttl)
        if cached is not None and cached.folder_id == folder_id:
            return cached
        return self._listing(folder, fresh=True, folder_id=folder_id)[0]

    def _check_free(self, folder: str, folder_id: str, name: str) -> None:
        """AlreadyExists when the folder has something called `name` (see _guard)."""
        if name in self._guard(folder, folder_id).by_name:
            raise AlreadyExists()

    @staticmethod
    def _entry(path: str, node: Node) -> Entry:
        return Entry(name=node.name, path=path, is_dir=node.is_dir,
                     size=None if node.is_dir else node.size, modified=node.modified)

    def _file(self, path: str) -> Node:
        """The file at `path`. NotFound, a folder too."""
        if path == paths.ROOT:
            raise NotFound()
        node = self._node(path)
        if node.is_dir:
            raise NotFound()
        return node

    # -- files

    def check(self) -> None:
        self.list(paths.ROOT)

    def list(self, path: str) -> list[Entry]:
        if path != paths.ROOT and not self._node(path).is_dir:
            raise NotADirectory()
        listing, _ = self._listing(path, fresh=True)
        return [self._entry(paths.join(path, node.name), node) for node in listing.by_name.values()]

    def stat(self, path: str) -> Entry:
        if path == paths.ROOT:
            self._root_id()
            return Entry(name="", path=paths.ROOT, is_dir=True)
        return self._entry(path, self._node(path))

    def open(self, path: str, offset: int = 0, length: int | None = None) -> ReadStream:
        return self._read_node(self._file(path), offset, length)

    def write(self, path: str, data: BinaryIO, size: int) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent, name = paths.split(path)
        folder_id = self._ensure_folder(parent)
        with self._names.hold((folder_id, name)):
            self._write(path, parent, name, folder_id, data, size)

    def _write(self, path: str, parent: str, name: str, folder_id: str, data: BinaryIO, size: int) -> None:
        listing = self._guard(parent, folder_id)
        old = listing.by_name.get(name)
        if old is not None and old.is_dir:
            raise AlreadyExists()
        before = {node.id for node in listing.nodes}
        with self._changing(path):
            uploaded = self._upload_node(folder_id, name, data, size)
        if uploaded is not None and (old is None or self.upload_conflict == "replace"):
            if uploaded.name != name:
                # The name was taken by a file we didn't know of (made in the backend's own apps meanwhile), so
                # the backend gave the upload another; it isn't where the user put it (STOR-19).
                self._touched(parent)
                raise DriverError(gettext('The file was uploaded, but %(service)s already had a "%(name)s" (not uploaded through this site), so it named the new one "%(renamed)s".')
                                  % {"service": self.service, "name": name, "renamed": uploaded.name})
            if self._cacheable(uploaded):
                self._edit(parent, folder_id, gone=[old] if old is not None and old.id != uploaded.id else [], made=[uploaded])
            else:
                self._touched(parent)
            return
        self._touched(parent)
        if self.upload_conflict == "replace":
            return  # the backend doesn't say what it made: the next listing shows it
        # Find the file just made before touching the old one: by its id, or as the new entry of that size
        # (the backend may have given it another name, like "a(1).txt", as the name was taken).
        listing, _ = self._listing(parent, fresh=True, folder_id=folder_id)
        new_id = uploaded.id if uploaded is not None else None
        if new_id is not None:
            made = [node for node in listing.nodes if node.id == new_id]
        else:
            made = [node for node in listing.nodes if node.id not in before and not node.is_dir and node.size == size]
            made.sort(key=lambda node: (node.name == name, rank(node)))
        if not made:
            if new_id is not None:  # the backend says it's there; keep the old one rather than guess
                self.log.warning("uploaded %s but %s doesn't list the new file yet", path, self.service)
                return
            raise BackendError(gettext("%(service)s didn't confirm the upload finished. Please refresh to see whether the file is there.")
                               % {"service": self.service})
        new = made[-1]
        try:
            if old is not None and old.id != new.id:
                with self._changing(path):
                    self._delete_node(old, folder_id)
                self._edit(parent, folder_id, gone=[old])
            if new.name != name:
                with self._changing(path, paths.join(parent, new.name)):
                    self._rename_node(new, name)
                self._edit(parent, folder_id, gone=[new], made=[dataclasses.replace(new, name=name)])
        except DriverError as error:
            # The new file is there; at worst next to the old one, or under the name the backend gave it.
            self.log.warning("uploaded %s but couldn't tidy up after it: %s", path, error.message)

    def mkdir(self, path: str) -> None:
        if path == paths.ROOT:
            raise AlreadyExists()
        parent, name = paths.split(path)
        self._create_folder(parent, self._ensure_folder(parent), name)

    def delete(self, path: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be deleted."))
        node = self._node(path)
        parent = paths.parent(path)
        parent_id = self._folder_id(parent)
        with self._changing(path):
            self._delete_node(node, parent_id)
        self._edit(parent, parent_id, gone=[node])  # a folder: with what is cached below it

    def rename(self, path: str, new_name: str) -> None:
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be renamed."))
        node = self._node(path)
        if new_name == node.name:
            return
        parent = paths.parent(path)
        parent_id = self._folder_id(parent)
        with self._names.hold((parent_id, new_name)):
            other = self._guard(parent, parent_id).by_name.get(new_name)
            if other is not None and other.id != node.id:
                raise AlreadyExists()
            with self._changing(path, paths.join(parent, new_name)):
                self._rename_node(node, new_name)
            self._edit(parent, parent_id, gone=[node], made=[dataclasses.replace(node, name=new_name)])

    def _target(self, path: str, folder: str, copy: bool) -> tuple[Node, str]:
        """Checks of move() and copy(): the node at `path` and the id of `folder`."""
        if path == paths.ROOT:
            raise DriverError(gettext("The storage source's root folder can't be copied.") if copy else gettext("The storage source's root folder can't be moved."))
        if paths.is_inside(folder, path):
            raise DriverError(gettext("A folder can't be copied into itself.") if copy else gettext("A folder can't be moved into itself."))
        return self._node(path), self._folder_id(folder)

    def move(self, path: str, folder: str) -> None:
        if path != paths.ROOT and paths.parent(path) == folder:
            self._node(path)
            return  # already there
        node, folder_id = self._target(path, folder, copy=False)
        source = paths.parent(path)
        source_id = self._folder_id(source)
        with self._names.hold((folder_id, node.name)):
            self._check_free(folder, folder_id, node.name)
            with self._changing(path, paths.join(folder, node.name)):
                self._move_node(node, source_id, folder_id)
            with self._changes_lock:  # one step: no lookup finds the node in both folders or in neither
                self._edit(source, source_id, gone=[node])
                self._edit(folder, folder_id, made=[node])

    def copy(self, path: str, folder: str) -> None:
        node, folder_id = self._target(path, folder, copy=True)
        with self._names.hold((folder_id, node.name)):
            self._check_free(folder, folder_id, node.name)
            with self._changing(paths.join(folder, node.name)):
                made = self._copy_node(node, folder_id)
            if made is not None and made.name == node.name:
                self._edit(folder, folder_id, made=[made])
            else:
                self._touched(folder)  # the backend doesn't say what it made: the folder is listed again
