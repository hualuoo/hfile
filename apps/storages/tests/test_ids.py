"""The base of the drivers of id-addressed backends (apps/storages/drivers/_ids.py), over an in-memory
backend that, like most cloud drives, allows several files and folders of one name in a folder.

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_ids
"""

from __future__ import annotations

import datetime
import io
import itertools
import threading
import unittest

from apps.storages.drivers import _ids
from apps.storages.drivers._ids import IdDriver, Node, StaleFolder
from apps.storages.drivers.base import AlreadyExists, NotFound, ReadStream

from apps.storages.drivers import paths

from .contract import EMPTY_FOLDER, READ_ONLY_TREE, DriverContract, ReadOnlyContract



class MemoryBackend:
    """Files and folders by id. `conflict`: what an upload under a taken name does: "keep" both, the new
    one named "a(1).txt", or "replace" the old one."""

    def __init__(self, conflict: str = "keep"):
        self.conflict = conflict
        self.items: dict[str, dict] = {"0": {"name": "", "parent": None, "dir": True, "time": 0}}
        self.start = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
        self.data: dict[str, bytes] = {}
        self.ids = itertools.count(1)
        self.clock = itertools.count(1)
        self.calls: list[str] = []
        self.lock = threading.Lock()

    def add(self, parent: str, name: str, *, data: bytes | None = None) -> str:
        """Makes a file (or a folder: data None) as the backend's own apps would, duplicates allowed."""
        with self.lock:
            item_id = str(next(self.ids))
            self.items[item_id] = {"name": name, "parent": parent, "dir": data is None, "time": next(self.clock)}
            if data is not None:
                self.data[item_id] = data
            return item_id

    def children(self, folder_id: str) -> list[str]:
        return [item_id for item_id, item in self.items.items() if item["parent"] == folder_id]

    def node(self, item_id: str) -> Node:
        item = self.items[item_id]
        return Node(id=item_id, name=item["name"], is_dir=item["dir"],
                    size=None if item["dir"] else len(self.data[item_id]),
                    modified=self.start + datetime.timedelta(seconds=item["time"]))

    def taken(self, folder_id: str, name: str) -> bool:
        return any(self.items[child]["name"] == name for child in self.children(folder_id))

    def remove(self, item_id: str) -> None:
        for child in self.children(item_id):
            self.remove(child)
        self.items.pop(item_id)
        self.data.pop(item_id, None)

    def clone(self, item_id: str, parent: str) -> str:
        item = self.items[item_id]
        new_id = self.add(parent, item["name"], data=self.data.get(item_id) if not item["dir"] else None)
        for child in self.children(item_id):
            self.clone(child, new_id)
        return new_id


class MemoryDriver(IdDriver):
    type = "memory-ids"
    label = "memory"
    service = "Memory"
    params = ()
    can_copy = True
    can_move = True

    def __init__(self, config, backend: MemoryBackend):
        super().__init__(config)
        self.backend = backend
        self.upload_conflict = backend.conflict

    def _fetch(self, folder_id):
        b = self.backend
        with b.lock:
            b.calls.append("list")
            item = b.items.get(folder_id)
            if item is None or not item["dir"]:
                raise StaleFolder(folder_id)
            return [b.node(child) for child in b.children(folder_id)]

    def _create_node(self, parent_id, name):
        b = self.backend
        b.calls.append("mkdir")
        return b.node(b.add(parent_id, name))

    def _delete_node(self, node, parent_id):
        b = self.backend
        with b.lock:
            b.calls.append("delete")
            if node.id not in b.items:
                raise NotFound()
            b.remove(node.id)

    def _rename_node(self, node, name):
        b = self.backend
        with b.lock:
            b.calls.append("rename")
            b.items[node.id]["name"] = name

    def _move_node(self, node, source_id, target_id):
        b = self.backend
        with b.lock:
            b.calls.append("move")
            b.items[node.id]["parent"] = target_id

    def _copy_node(self, node, target_id):
        b = self.backend
        b.calls.append("copy")
        with b.lock:
            pass
        b.clone(node.id, target_id)
        return None  # like 115: the copy's id isn't said

    def _upload_node(self, folder_id, name, data, size):
        b = self.backend
        body = data.read(size)
        with b.lock:
            b.calls.append("upload")
            if b.conflict == "replace":
                for child in b.children(folder_id):
                    if b.items[child]["name"] == name and not b.items[child]["dir"]:
                        b.data[child] = body
                        b.items[child]["time"] = next(b.clock)
                        return b.node(child)
            elif b.taken(folder_id, name):
                stem, dot, ext = name.rpartition(".")
                name = f"{stem}(1).{ext}" if dot and stem else f"{name}(1)"
        return b.node(b.add(folder_id, name, data=body))

    def _read_node(self, node, offset, length):
        body = self.backend.data[node.id]
        part = body[offset:] if length is None else body[offset:offset + length]
        return ReadStream([part], size=len(part), total=len(body), offset=offset)


class KeepContractTests(DriverContract, unittest.TestCase):
    def make_driver(self):
        return MemoryDriver({}, MemoryBackend("keep"))


class ReplaceContractTests(DriverContract, unittest.TestCase):
    def make_driver(self):
        return MemoryDriver({}, MemoryBackend("replace"))


class BasePathContractTests(DriverContract, unittest.TestCase):
    def make_driver(self):
        backend = MemoryBackend("keep")
        top = backend.add("0", "top")
        backend.add(top, "base")
        backend.add("0", "beside.txt", data=b"x")
        return MemoryDriver({"base_path": "/top/base"}, backend)


class ReadOnlyMemoryDriver(MemoryDriver):
    can_write = False
    can_move = False
    can_copy = False


class ReadOnlyContractTests(ReadOnlyContract, unittest.TestCase):
    """The read-only contract itself, over the memory backend."""

    def make_driver(self):
        backend = MemoryBackend()
        folders = {paths.ROOT: "0"}

        def folder(path):
            if path not in folders:
                folders[path] = backend.add(folder(paths.parent(path)), paths.name(path))
            return folders[path]

        for path, data in READ_ONLY_TREE.items():
            backend.add(folder(paths.parent(path)), paths.name(path), data=data)
        folder(EMPTY_FOLDER)
        return ReadOnlyMemoryDriver({}, backend)


class IdDriverTests(unittest.TestCase):
    def setUp(self):
        self.backend = MemoryBackend("keep")
        self.d = MemoryDriver({}, self.backend)

    def put(self, path, data):
        self.d.write(path, io.BytesIO(data), len(data))

    def names(self, folder_id):
        return sorted(self.backend.items[child]["name"] for child in self.backend.children(folder_id))

    def test_of_duplicates_a_path_finds_folders_then_the_newest(self):
        b = self.backend
        b.add("0", "a.txt", data=b"old")
        b.add("0", "a.txt", data=b"new")
        b.add("0", "x", data=b"file")
        b.add("0", "x")
        entries = {entry.name: entry for entry in self.d.list("/")}
        self.assertEqual(sorted(entries), ["a.txt", "x"])
        self.assertEqual(entries["a.txt"].size, 3)
        self.assertEqual(self.d.open("/a.txt").read(), b"new")
        self.assertTrue(entries["x"].is_dir)
        self.assertEqual(sorted(["12", "²", "3"], key=_ids.id_key), ["3", "12", "²"])

    def test_writing_over_a_file_leaves_one_of_that_name(self):
        self.put("/a.txt", b"1")
        self.put("/a.txt", b"22")  # the backend names the new one a(1).txt: deleted the old one, renamed back
        self.assertEqual(self.names("0"), ["a.txt"])
        self.assertEqual(self.d.open("/a.txt").read(), b"22")
        self.assertIn("rename", self.backend.calls)

    def test_names_taken_behind_our_back(self):
        self.d.list("/")
        self.backend.add("0", "late.txt", data=b"theirs")
        # The listing is older than that, so the guard lists the folder again. (-1, not 0: Windows' monotonic
        # clock moves in steps of about 16 ms, and a listing of this very step is 0 seconds old.)
        self.d.guard_ttl = -1
        with self.assertRaises(AlreadyExists):
            self.d.mkdir("/late.txt")
        self.assertEqual(self.names("0"), ["late.txt"])

    def test_our_changes_update_the_cached_listing(self):
        self.d.mkdir("/d")
        calls = len(self.backend.calls)
        for n in range(5):
            self.put(f"/d/{n}.txt", b"x")
        self.assertEqual(self.backend.calls[calls:].count("list"), 1)  # once, for the first guard
        self.assertEqual(sorted(e.name for e in self.d.list("/d")), [f"{n}.txt" for n in range(5)])

    def test_a_folder_gone_behind_our_back(self):
        self.d.mkdir("/d/e")
        self.d.list("/d/e")
        d_id = next(i for i, item in self.backend.items.items() if item["name"] == "d")
        self.backend.remove(d_id)
        with self.assertRaises(NotFound):
            self.d.list("/d/e")
        new_d = self.backend.add("0", "d")  # made again elsewhere: the path finds the new one...
        self.backend.add(new_d, "e")
        self.d.listing_ttl = -1  # ...once the listing that didn't have it is too old to believe its miss (-1: above)
        self.assertEqual(self.d.list("/d"), [self.d.stat("/d/e")])

    def test_a_missing_base_path(self):
        d = MemoryDriver({"base_path": "/nope"}, self.backend)
        with self.assertRaises(NotFound) as caught:
            d.list("/")
        self.assertIn("/nope", caught.exception.message)
        self.assertIn("Memory", caught.exception.message)
        self.assertEqual(MemoryDriver.base_path_errors({"base_path": "/a/../b"}).keys(), {"base_path"})
        self.assertEqual(MemoryDriver.base_path_errors({"base_path": "/a"}), {})


if __name__ == "__main__":
    unittest.main()
