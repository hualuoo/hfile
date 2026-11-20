"""Rule 1, the actor's root: nothing outside the grant's root (or a share's folder) can be reached, through
any endpoint that takes a path, whatever the path looks like (.., encoded separators, backslashes, segments
of dots and spaces only, D46), and links and shares made inside a root stop when the root moves."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from apps.accounts.permissions import Permission

from .base import EVERYTHING, FILE, FOLDER, CrossCase

# Paths that would leave bob's root (/home/bob) if they were taken as they are, or that Windows-like
# servers may read as "..": every one must be refused.
TRAVERSALS = [
    "..", "/..", "../docs", "/../../docs", "/x/../..", "/./..",
    "..\\docs", "\\..\\docs", "/x\\..\\..\\docs", "/bob.txt\\..\\..",
    "...", "/...", ".. ", "/. .", "/x/.../y", " ..", "/ .. /docs",
]
# New names that are dots and spaces only (D46), or carry a separator.
BAD_NAMES = ["..", "...", ".. ", ". .", " ..", "a/..", "..\\x", "\\..", "a\\b"]


class RootPathTests(CrossCase):
    """bob (root /home/bob) tries to get out through every endpoint; nothing outside changes."""

    def setUp(self):
        super().setUp()
        self.before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def assertNothingChanged(self):
        after = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        self.assertEqual(after, self.before)
        self.assertEqual((self.root / "top.txt").read_bytes(), b"top")

    def test_reading(self):
        for path in TRAVERSALS:
            with self.subTest(path=path):
                self.assertRefused(self.list_(self.as_bob, path), 400, "invalid_path")
                self.assertRefused(self.item(self.as_bob, path), 400, "invalid_path")
                self.assertRefused(self.unlock(self.as_bob, path), 400, "invalid_path")

    def test_file_operations(self):
        for path in TRAVERSALS:
            with self.subTest(path=path):
                self.assertRefused(self.mkdir(self.as_bob, path, "new"))
                self.assertRefused(self.rename(self.as_bob, path, "bob.txt", "x.txt"))
                self.assertBatchRefused(self.move(self.as_bob, path, ["bob.txt"], "/"))
                self.assertBatchRefused(self.move(self.as_bob, "/", ["bob.txt"], path))
                self.assertBatchRefused(self.copy(self.as_bob, path, ["top.txt"], "/"))
                self.assertBatchRefused(self.copy(self.as_bob, "/", ["bob.txt"], path))
                self.assertBatchRefused(self.delete(self.as_bob, path, ["top.txt"]))
        for name in BAD_NAMES:
            with self.subTest(name=name):
                self.assertRefused(self.mkdir(self.as_bob, "/", name))
                self.assertRefused(self.rename(self.as_bob, "/", "bob.txt", name))
                self.assertRefused(self.rename(self.as_bob, "/", name, "x.txt"))
                self.assertBatchRefused(self.move(self.as_bob, "/", [name], "/"))
                self.assertBatchRefused(self.copy(self.as_bob, "/", [name], "/"))
                self.assertBatchRefused(self.delete(self.as_bob, "/", [name]))
        self.assertNothingChanged()

    def test_uploads(self):
        for path in TRAVERSALS:
            with self.subTest(path=path):
                self.assertRefused(self.upload_session(self.as_bob, path, "x.txt", 3))
                self.assertRefused(self.upload_session(self.as_bob, "/", "x.txt", 3, relative_path=path))
                self.assertRefused(self.make_folders(self.as_bob, path, ["new"]))
                self.assertRefused(self.make_folders(self.as_bob, "/", [path]))
                self.assertRefused(self.save_text(self.as_bob, path, "x"))
                self.assertRefused(self.save_text(self.as_bob, f"{path}/x.txt", "x"))
        for name in BAD_NAMES:
            with self.subTest(name=name):
                self.assertRefused(self.upload_session(self.as_bob, "/", name, 3))
                self.assertRefused(self.upload(self.as_bob, "/", name, b"abc"))
                self.assertRefused(self.make_folders(self.as_bob, "/", [f"ok/{name}"]))
        self.assertNothingChanged()

    def test_links(self):
        for path in TRAVERSALS:
            with self.subTest(path=path):
                self.assertRefused(self.path_links(self.as_bob, [path]))
                self.assertRefused(self.path_links(self.as_bob, [f"{path}/top.txt"]))
                self.assertRefused(self.short_links(self.as_bob, [f"{path}/top.txt"]))
        from apps.links.models import ShortLink

        self.assertEqual(ShortLink.objects.count(), 0)

    def test_share_creation(self):
        for path in TRAVERSALS:
            with self.subTest(path=path):
                self.assertRefused(self.share_create(self.as_bob, path, [("docs", FOLDER)]))
        for name in BAD_NAMES:
            with self.subTest(name=name):
                self.assertRefused(self.share_create(self.as_bob, "/", [(name, FOLDER)]))
                self.assertRefused(self.share_create(self.as_bob, "/", [(name, FILE)]))
        self.assertEqual(self.as_bob.get("/api/v1/shares/").json()["count"], 0)

    def test_share_visits(self):
        """A visitor of bob's share in /home/bob (his root) never gets out of the shared entries."""
        self.files({"home/bob/pics/p.txt": b"p", "home/bob/private.txt": b"not shared"})
        self.before = sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))
        share = self.share_of(self.bob, "/home/bob", [("pics", FOLDER), ("bob.txt", FILE)])
        self.assertEqual(self.vnames(self.anon, share, "/pics"), ["p.txt"])
        for path in TRAVERSALS + ["/pics/..", "/pics/../..", "/pics/..\\..\\docs", "/pics/...", "/pics\\..\\.."]:
            with self.subTest(path=path):
                self.assertRefused(self.vlist(self.anon, share, path))
                self.assertRefused(self.vitem(self.anon, share, path))
                self.assertNotEqual(self.vdownload(self.anon, share, f"{path}/top.txt").status_code, 302)
                self.assertNotEqual(self.vpreview(self.anon, share, f"{path}/top.txt").status_code, 302)
                self.assertRefused(self.vunlock(self.anon, share, path))
        # Only the shared entries at "/": not the folder's other files.
        self.assertEqual(self.vnames(self.anon, share), ["bob.txt", "pics"])
        for path in ("/private.txt", "/bob.txt/x", "/Pics/p.txt", "/pics./p.txt"):
            with self.subTest(path=path):
                self.assertRefused(self.vitem(self.anon, share, path), 404)
                self.assertRefused(self.vdownload(self.anon, share, path), 404)
        self.assertNothingChanged()


class EncodedSeparatorTests(CrossCase):
    """Separators and dots percent-encoded in the address itself, which Django decodes before the views
    see the path."""

    def test_files_api_queries(self):
        for query in ("..%2F..%2Fdocs", "%2E%2E%2F%2E%2E", "%5C..%5C..%5Cdocs", "x%5C..%5C..", "%2E%2E%2E",
                      "..%00", "/home%2F..%2F.."):
            with self.subTest(query=query):
                self.assertRefused(self.as_bob.get(f"/api/v1/files/disk/list/?path={query}"), 400)
                self.assertRefused(self.as_bob.get(f"/api/v1/files/disk/item/?path={query}"), 400)

    def test_browser_pages(self):
        for address in ("/disk/..%2F..%2Fdocs", "/disk/%2E%2E/%2E%2E/docs", "/disk/x%5C..%5C..%5Cdocs",
                        "/disk/%2E%2E%2E/", "/disk/..%20/docs"):
            with self.subTest(address=address):
                response = self.as_bob.get(address)
                self.assertEqual(response.status_code, 400, address)

    def test_pd_links(self):
        from apps.files import signing

        good = signing.sign(self.storage, "/home/bob/bob.txt", lifetime=600)
        self.assertOk(self.as_bob.get(good))
        query = good.split("?", 1)[1]
        for path in ("/pd/disk/home/bob/%2E%2E/%2E%2E/top.txt", "/pd/disk/home/bob%2F..%2F..%2Ftop.txt",
                     "/pd/disk/home/bob/..%5C..%5Ctop.txt", "/pd/disk/home/bob/%2E%2E%2E/bob.txt"):
            with self.subTest(path=path):
                response = self.anon.get(f"{path}?{query}")
                self.assertIn(response.status_code, (400, 403, 404), path)

    def test_path_links(self):
        link = self.path_link(self.as_bob, "/bob.txt")
        self.assertEqual(link, "/directlink/disk/home/bob/bob.txt")
        self.assertEqual(self.as_bob.get(link).status_code, 302)
        for address in ("/directlink/disk/home/bob/..%2F..%2Ftop.txt", "/directlink/disk/home/bob/%2E%2E/%2E%2E/top.txt",
                        "/directlink/disk/home/bob/..%5C..%5Ctop.txt", "/directlink/disk/home/bob/%2E%2E%2E/top.txt"):
            with self.subTest(address=address):
                self.assertIn(self.as_bob.get(address).status_code, (400, 403, 404), address)

    def test_share_pages(self):
        share = self.share_of(self.bob, "/home", [("bob", FOLDER)])
        for query in ("..%2Ftop.txt", "bob%2F..%2F..%2Ftop.txt", "bob%5C..%5C..%5Ctop.txt", "%2E%2E%2E%2Ftop.txt"):
            with self.subTest(query=query):
                for what in ("download", "preview"):
                    response = self.anon.get(f"/share/{share.key}/{what}?path={query}")
                    self.assertNotEqual(response.status_code, 302, query)


class RootMovedTests(CrossCase):
    """A share, a short link, a path link and an upload made inside alice's root, then the root moved."""

    def setUp(self):
        super().setUp()
        self.grant(self.alice, EVERYTHING, root="/home/alice")

    def move_root(self, root):
        self.grant(self.alice, EVERYTHING, root=root)

    def test_a_share(self):
        share = self.share_of(self.alice, "/home/alice", [("docs", FOLDER), ("mine.txt", FILE)])
        self.assertEqual(self.vnames(self.anon, share), ["docs", "mine.txt"])
        self.assertEqual(self.vdownload(self.anon, share, "/mine.txt").status_code, 302)
        for root in ("/home/bob", "/home/alice/docs", "/docs"):
            with self.subTest(root=root):
                self.move_root(root)
                self.assertRefused(self.vlist(self.anon, share), 403, "outside_root")
                self.assertRefused(self.vitem(self.anon, share, "/mine.txt"), 403, "outside_root")
                self.assertRefused(self.vdownload(self.anon, share, "/mine.txt"), 403)
                self.assertRefused(self.vpreview(self.anon, share, "/docs/note.txt"), 403)
                self.assertNotIn("entries", self.vinfo(self.anon, share).json())
        self.move_root("/home")  # wider: the share's folder is inside again
        self.assertEqual(self.vnames(self.anon, share, "/docs"), ["note.txt"])

    def test_a_share_through_the_api(self):
        made = self.assertOk(self.share_create(self.as_alice, "/", [("docs", FOLDER)]), 201)
        from apps.shares.models import Share

        share = Share.objects.get(key=made.json()["key"])
        self.assertEqual(share.folder, "/home/alice")
        self.move_root("/home/bob")
        self.assertRefused(self.vlist(self.anon, share, "/docs"), 403)

    def test_a_shared_folder_renamed(self):
        share = self.share_of(self.alice, "/home/alice", [("docs", FOLDER)])
        self.assertOk(self.rename(self.as_alice, "/", "docs", "papers"))
        self.assertRefused(self.vlist(self.anon, share, "/docs"), 404)
        self.assertEqual(self.vnames(self.anon, share), [])

    def test_a_short_link(self):
        link = self.short_link(self.as_alice, "/mine.txt")
        response, target, body = self.follow(self.anon, link)
        self.assertEqual((response.status_code, body), (302, b"alice's"))
        self.move_root("/home/bob")
        response, _, _ = self.follow(self.anon, link)
        self.assertRefused(response, 403)
        self.move_root("/home/alice")
        self.assertEqual(self.follow(self.anon, link)[2], b"alice's")

    def test_a_path_link(self):
        link = self.path_link(self.as_alice, "/mine.txt")
        self.assertEqual(link, "/directlink/disk/home/alice/mine.txt")
        self.assertEqual(self.follow(self.as_alice, link)[2], b"alice's")
        self.move_root("/home/bob")
        self.assertRefused(self.as_alice.get(link), 403, None)
        # Opened as the visitor: bob's root doesn't hold it either...
        self.assertRefused(self.as_bob.get(link), 403)
        # ...and a guest with download at "/" may (the visitor's own permission, D40a).
        self.grant(self.guest, [Permission.DOWNLOAD])
        self.assertEqual(self.follow(self.anon, link)[2], b"alice's")

    def test_an_upload(self):
        made = self.assertOk(self.upload_session(self.as_alice, "/docs", "big.bin", 2500), 201).json()
        self.assertOk(self.put_chunk(self.as_alice, made, b"x" * 1000, 0, 2500), 202)
        self.move_root("/home/bob")
        self.assertRefused(self.put_chunk(self.as_alice, made, b"x" * 1000, 1000, 2500), 403, "outside_root")
        self.move_root("/home/alice")
        self.assertOk(self.put_chunk(self.as_alice, made, b"x" * 1000, 1000, 2500), 202)
        self.move_root("/home")
        # Still inside (wider): the upload goes on, into the same storage path.
        self.assertOk(self.put_chunk(self.as_alice, made, b"x" * 500, 2000, 2500), 201)
        self.assertEqual((self.root / "home/alice/docs/big.bin").stat().st_size, 2500)


class ActorRootTests(CrossCase):
    """The guest's and a staff admin's roots hold as a normal user's: no bypass for staff."""

    def test_the_guest(self):
        self.grant(self.guest, EVERYTHING, root="/docs")
        self.assertEqual(self.names(self.anon), ["a.txt", "b.txt", "sub"])
        self.assertRefused(self.list_(self.anon, ".."), 400)
        self.assertRefused(self.item(self.anon, "/top.txt"), 404)  # /docs/top.txt doesn't exist
        self.assertRefused(self.path_links(self.anon, ["/../top.txt"]), 400)
        self.assertRefused(self.anon.get("/directlink/disk/top.txt"), 403, None)
        self.assertEqual(self.follow(self.anon, "/directlink/disk/docs/a.txt")[2], b"alpha")

    def test_a_staff_admin(self):
        self.grant(self.staff, EVERYTHING, root="/home/alice")
        self.assertEqual(self.names(self.as_staff), ["docs", "mine.txt"])
        self.assertRefused(self.list_(self.as_staff, "/.."), 400)
        self.assertRefused(self.as_staff.get("/directlink/disk/top.txt"), 403)
        self.assertRefused(self.as_staff.get("/directlink/disk/home/bob/bob.txt"), 403)
        self.assertRefused(self.share_create(self.as_staff, "/..", [("top.txt", FILE)]), 400)
        # Without a grant a staff admin has nothing at all.
        from apps.storages.models import Grant

        Grant.objects.filter(user=self.staff).delete()
        self.assertRefused(self.list_(self.as_staff), 403, "no_grant")
        self.assertRefused(self.as_staff.get("/directlink/disk/docs/a.txt"), 403, None)

    def test_upload_sessions_are_their_makers(self):
        """Nobody else (another user, the guest, a staff admin) can see, feed, finish or cancel one."""
        self.grant(self.guest, EVERYTHING)
        made = self.assertOk(self.upload_session(self.as_alice, "/docs", "big.bin", 2500), 201).json()
        for client in (self.as_bob, self.as_staff, self.anon, self.client_for(self.boss)):
            with self.subTest(client=client):
                self.assertRefused(client.get(made["url"]), 404, "not_found")
                self.assertRefused(self.put_chunk(client, made, b"x" * 1000, 0, 2500), 404, "not_found")
                self.assertRefused(client.post(made["complete_url"]), 404, "not_found")
                self.assertRefused(client.delete(made["url"]), 404, "not_found")
        guest_made = self.assertOk(self.upload_session(self.anon, "/docs", "g.bin", 2500), 201).json()
        self.assertRefused(self.client_for().get(guest_made["url"]), 404, "not_found")  # another browser
        self.assertOk(self.anon.get(guest_made["url"]))

    def test_a_signed_in_user_never_uses_the_guests_grant(self):
        self.grant(self.guest, EVERYTHING)
        from apps.storages.models import Grant

        Grant.objects.filter(user=self.bob).delete()
        self.assertEqual(self.names(self.anon, "/docs"), ["a.txt", "b.txt", "sub"])
        self.assertRefused(self.list_(self.as_bob, "/docs"), 403, "no_grant")
        self.assertRefused(self.as_bob.get("/directlink/disk/docs/a.txt"), 403)


class DotsOnlyNamesOnDiskTests(CrossCase):
    """D46: names of dots and spaces only can't be made or opened through HFile (an accepted limit). One
    made outside HFile (a shell, an SFTP client, another program on the bucket) is left alone: the folder
    holding it still lists, for the owner and a share's visitor."""

    def setUp(self):
        super().setUp()
        docs = self.root / "docs"
        if os.name == "nt":  # Windows drops dots and spaces at a name's end, except after \\?\ (as other programs make them)
            docs = Path("\\\\?\\" + str(docs))
            self.addCleanup(shutil.rmtree, "\\\\?\\" + str(self.tmp), True)
        (docs / "...").write_bytes(b"dots")
        (docs / ".. ").mkdir()
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER)])

    def test_they_cant_be_opened(self):
        for path in ("/docs/...", "/docs/.. ", "/docs/.. /x"):
            with self.subTest(path=path):
                self.assertRefused(self.item(self.as_alice, path), 400, "invalid_path")
                self.assertRefused(self.vitem(self.anon, self.share, path), 400, "invalid_path")

    def test_the_folder_still_lists(self):
        self.assertEqual(self.names(self.as_alice, "/docs"), ["a.txt", "b.txt", "sub"])

    def test_the_folder_still_lists_in_a_share(self):
        self.assertEqual(self.vnames(self.anon, self.share, "/docs"), ["a.txt", "b.txt", "sub"])

    def test_the_folder_can_still_be_shared(self):
        self.assertOk(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 201)

    def test_the_folder_as_a_whole(self):
        """Copying, moving and deleting the folder holding it (the local driver walks it by name)."""
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/", ["docs"], "/home")))
        self.assertTrue((self.root / "home/docs/...").exists())
        self.assertTrue(self.batch_ok(self.move(self.as_alice, "/home", ["docs"], "/home/bob")))
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/home/bob", ["docs"])))
        self.assertFalse((self.root / "home/bob/docs").exists())
