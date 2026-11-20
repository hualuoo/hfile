"""What people do with files, links and shares, in the activity log: one entry for each item done, none for
what was refused or failed. Downloads aren't logged here (the download log has them)."""

from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from apps.files.tests.helpers import EVERYTHING, FilesTestCase
from apps.links.tests.helpers import LinksTestCase
from apps.shares.tests.helpers import SharesTestCase
from apps.uploads.tests.base import API as UPLOADS, UploadTestCase

from ..testing import entries

FILE_KINDS = ("mkdir", "rename", "move", "copy", "delete", "upload", "edit", "short_link", "create_share",
              "delete_share", "clear_shares")


def done(kind=None):
    """(kind, path, to_path, detail) of the entries about files, links and shares."""
    return [(entry.kind, entry.path, entry.to_path, entry.detail) for entry in entries(kind) if entry.kind in FILE_KINDS]


class FileOperationTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_new_folder_rename_move_copy_delete(self):
        self.assertEqual(self.post("mkdir", {"path": "/docs", "name": "new"}).status_code, 201)
        self.assertEqual(self.post("rename", {"path": "/docs", "name": "a.txt", "new_name": "z.txt"}).status_code, 200)
        self.assertEqual(self.post("rename", {"path": "/docs", "name": "sub", "new_name": "inner"}).status_code, 200)
        self.assertEqual(self.post("move", {"path": "/docs", "names": ["z.txt", "inner"], "target": "/docs/new"}).status_code, 200)
        self.assertEqual(self.post("copy", {"path": "/", "names": ["top.txt"], "target": "/docs"}).status_code, 200)
        self.assertEqual(self.post("delete", {"path": "/docs", "names": ["new", "b.txt"]}).status_code, 200)
        self.assertEqual(done(), [
            ("mkdir", "/docs/new", "", {}),
            ("rename", "/docs/a.txt", "/docs/z.txt", {}),
            ("rename", "/docs/sub", "/docs/inner", {"folder": True}),
            ("move", "/docs/z.txt", "/docs/new/z.txt", {}),
            ("move", "/docs/inner", "/docs/new/inner", {"folder": True}),
            ("copy", "/top.txt", "/docs/top.txt", {}),
            ("delete", "/docs/new", "", {"folder": True}),
            ("delete", "/docs/b.txt", "", {}),
        ])
        entry = entries("delete")[0]
        self.assertEqual((entry.actor, entry.storage, entry.storage_name, entry.ip), (self.alice, self.storage, "存储 disk", "127.0.0.1"))

    def test_storage_paths_whatever_the_root(self):
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.assertEqual(self.post("delete", {"path": "/", "names": ["a.txt"]}).status_code, 200)
        self.assertEqual(done(), [("delete", "/docs/a.txt", "", {})])

    def test_only_what_was_done(self):
        response = self.post("move", {"path": "/docs", "names": ["a.txt", "nothing.txt", "sub"], "target": "/docs/sub"})
        self.assertEqual(response.json()["succeeded"], 1, response.json())
        self.assertEqual(self.post("copy", {"path": "/", "names": ["top.txt"], "target": "/"}).json()["failed"], 1)  # 目标已存在
        self.assertEqual(self.post("mkdir", {"path": "/", "name": "docs"}).status_code, 409)
        self.grant(self.alice, [code for code in EVERYTHING if code != "delete"])
        self.assertEqual(self.post("delete", {"path": "/", "names": ["top.txt"]}).status_code, 403)
        self.assertEqual(done(), [("move", "/docs/a.txt", "/docs/sub/a.txt", {})])

    def test_visitors_as_the_guest(self):
        self.client = APIClient(enforce_csrf_checks=True, REMOTE_ADDR="198.51.100.4")
        self.grant(self.guest, EVERYTHING)
        self.assertEqual(self.post("mkdir", {"path": "/", "name": "drop"}).status_code, 201)
        entry = entries("mkdir")[0]
        self.assertEqual((entry.actor, entry.actor_name, entry.ip), (self.guest, "guest", "198.51.100.4"))

    def test_reading_isnt_logged(self):
        self.listing("/docs")
        self.get("item", path="/docs/a.txt")
        self.assertEqual(done(), [])


class UploadTests(UploadTestCase):
    def test_a_file_through_the_site(self):
        answer = self.upload("data.bin", b"x" * 2500, "/")
        self.assertEqual(answer.status_code, 201, answer.content)
        self.assertEqual(done(), [("upload", "/data.bin", "", {"size": 2500})])
        entry = entries("upload")[0]
        self.assertEqual((entry.actor, entry.storage), (self.alice, self.storage))
        # Replacing it says so.
        self.assertEqual(self.upload("data.bin", b"y" * 10, "/", overwrite=True).status_code, 201)
        self.assertEqual(done()[-1], ("upload", "/data.bin", "", {"size": 10, "replaced": True}))

    def test_an_empty_file_and_one_that_never_arrived(self):
        self.assertEqual(self.create("empty.txt", 0).json()["state"], "done")
        session = self.create("half.bin", 2500).json()
        self.put(session, b"x" * 1000, 0, 2500)
        self.client.delete(session["url"])
        self.assertEqual(done(), [("upload", "/empty.txt", "", {"size": 0})])

    def test_a_folder_upload(self):
        (self.root / "album").mkdir()
        body = {"storage": "disk", "folder": "/", "folders": ["album", "album/2026", "album/2026/11"]}
        response = self.client.post(f"{UPLOADS}folders/", body, content_type="application/json")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(done(), [("mkdir", "/album/2026", "", {"upload": True}), ("mkdir", "/album/2026/11", "", {"upload": True})])

    def test_text_saves(self):
        save = lambda path, content: self.client.put(f"{UPLOADS}text/", {"storage": "disk", "path": path, "content": content},  # noqa: E731
                                                     content_type="application/json")
        self.assertEqual(save("/notes.txt", "hello").status_code, 200)
        self.assertEqual(save("/notes.txt", "hello again").status_code, 200)
        self.assertEqual(done(), [("edit", "/notes.txt", "", {"size": 5, "new": True}), ("edit", "/notes.txt", "", {"size": 11})])


class LinkTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_short_links(self):
        response = self.make("short", ["/docs/a.txt", "/top.txt"], expires=86400)
        self.assertEqual(response.status_code, 201, response.content)
        links = response.json()["links"]
        self.assertEqual([(entry.path, entry.key, entry.detail) for entry in entries("short_link")],
                         [("/docs/a.txt", links[0]["key"], {"expires_at": links[0]["expires_at"]}),
                          ("/top.txt", links[1]["key"], {"expires_at": links[1]["expires_at"]})])
        self.assertIsNotNone(links[0]["expires_at"])

    def test_direct_links_and_downloads_arent_logged(self):
        self.assertEqual(self.make("path", ["/docs/a.txt"]).status_code, 200)
        link = self.short()
        self.assertEqual(self.visitor().get(f"/s/{link.key}").status_code, 302)
        self.assertEqual(self.client.get(self.path_link("/docs/a.txt")).status_code, 302)
        self.assertEqual([entry.kind for entry in entries() if entry.kind != "login"], ["short_link"])


class ShareTests(SharesTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_making_and_deleting_one(self):
        response = self.create(self.body(entries=[{"name": "docs", "type": "folder"}, {"name": "top.txt", "type": "file"}],
                                         password="pass-2026", expiry="7d"))
        self.assertEqual(response.status_code, 201, response.content)
        key = response.json()["key"]
        entry = entries("create_share")[0]
        self.assertEqual((entry.actor, entry.storage, entry.path, entry.key, entry.detail["items"], entry.detail["password"]),
                         (self.alice, self.storage, "/", key, {"names": ["docs", "top.txt"], "count": 2}, True))
        self.assertIsNotNone(entry.detail["expires_at"])
        self.assertNotIn("pass-2026", repr(entry.detail))
        self.assertEqual(self.client.delete(f"/api/v1/shares/{key}/").status_code, 204)
        gone = entries("delete_share")[0]
        self.assertEqual((gone.actor, gone.target, gone.key, gone.detail), (self.alice, None, key, {"items": {"names": ["docs", "top.txt"], "count": 2}}))

    def test_clearing_expired_ones(self):
        self.share(expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(self.client.post("/api/v1/shares/clear-expired/").json(), {"deleted": 1})
        self.assertEqual(self.client.post("/api/v1/shares/clear-expired/").json(), {"deleted": 0})
        self.assertEqual([entry.detail for entry in entries("clear_shares")], [{"count": 1}])

    def test_visiting_isnt_logged(self):
        share = self.share()
        self.visitor.get(f"/api/v1/shares/{share.key}/")
        self.assertEqual(entries("create_share"), [])
        self.assertEqual([entry.kind for entry in entries()], ["login"])
