"""Rule 6, the local driver: a symlink inside the storage's folder that points outside it is not listed and
not readable, through any endpoint, nor written through, unless allow_external_symlinks is on. Links
that stay inside the folder are followed (an accepted limit: they are judged by their own path)."""

from __future__ import annotations

from apps.core.testing import symlink
from apps.files import signing

from .base import EVERYTHING, FILE, FOLDER, CrossCase

OUTSIDE = ("out.txt", "outdir", "rel.txt", "reldir", "out.md")


class SymlinkCase(CrossCase):
    def setUp(self):
        super().setUp()
        (self.outside / "x.md").write_bytes(b"# outside")
        docs = self.root / "docs"
        symlink(self.outside / "secret.txt", docs / "out.txt")
        symlink(self.outside / "folder", docs / "outdir")
        symlink("../../outside/secret.txt", docs / "rel.txt")
        symlink("../../outside/folder", docs / "reldir")
        symlink(self.outside / "x.md", docs / "out.md")
        symlink("../top.txt", docs / "in.txt")
        symlink("../home", docs / "indir")
        symlink("nowhere", docs / "dangling")
        symlink("loop", docs / "loop")
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER)])

    def assertOutsideUnchanged(self):
        self.assertEqual(sorted(p.name for p in self.outside.iterdir()), ["folder", "secret.txt", "x.md"])
        self.assertEqual((self.outside / "secret.txt").read_bytes(), b"outside secret")
        self.assertEqual(sorted(p.name for p in (self.outside / "folder").iterdir()), ["inner.txt"])


class ExternalLinksOffTests(SymlinkCase):
    def test_listing_and_item(self):
        self.assertEqual(self.names(self.as_alice, "/docs"), ["a.txt", "b.txt", "in.txt", "indir", "sub"])
        for name in OUTSIDE + ("dangling",):
            with self.subTest(name=name):
                self.assertRefused(self.item(self.as_alice, f"/docs/{name}"), 404, "not_found")
        self.assertRefused(self.item(self.as_alice, "/docs/loop"))  # ELOOP: refused (400 failed)
        for path in ("/docs/outdir", "/docs/reldir"):
            with self.subTest(path=path):
                self.assertRefused(self.list_(self.as_alice, path), 404)
                self.assertRefused(self.item(self.as_alice, f"{path}/inner.txt"), 404)
        self.assertEqual(self.fetch(self.anon, self.inline_url(self.as_alice, "/docs/in.txt"))[1], b"top")
        self.assertEqual(self.names(self.as_alice, "/docs/indir"), ["alice", "bob"])

    def test_signed_links(self):
        """Even a validly signed /pd/ link (made as if a listing had handed it out) reads nothing."""
        for path in ("/docs/out.txt", "/docs/rel.txt", "/docs/outdir/inner.txt", "/docs/reldir/inner.txt"):
            with self.subTest(path=path):
                url = signing.sign(self.storage, path, lifetime=600, inline=True)
                response, body = self.fetch(self.anon, url)
                self.assertEqual(response.status_code, 404)
                self.assertNotIn(b"outside", body)
                head = self.anon.head(url)
                self.assertEqual(head.status_code, 404)
                ranged, body = self.fetch(self.anon, url, HTTP_RANGE="bytes=0-3")
                self.assertEqual(ranged.status_code, 404)

    def test_the_preview_sources(self):
        url = signing.sign(self.storage, "/docs/out.md", lifetime=600, inline=True)
        self.assertRefused(self.preview_markdown(self.anon, url), 404, "not_found")

    def test_links(self):
        for path in ("/docs/out.txt", "/docs/outdir/inner.txt"):
            with self.subTest(path=path):
                response, target, body = self.follow(self.as_alice, self.path_link(self.as_alice, path))
                self.assertNotIn(b"outside", body)
                self.assertTrue(response.status_code == 404 or (target is not None and target.status_code == 404))
                response, target, body = self.follow(self.anon, self.short_link(self.as_alice, path))
                self.assertNotIn(b"outside", body)
                self.assertTrue(response.status_code == 404 or (target is not None and target.status_code == 404))

    def test_shares(self):
        self.assertEqual(self.vnames(self.anon, self.share, "/docs"), ["a.txt", "b.txt", "in.txt", "indir", "sub"])
        for path in ("/docs/out.txt", "/docs/rel.txt", "/docs/outdir/inner.txt"):
            with self.subTest(path=path):
                self.assertRefused(self.vitem(self.anon, self.share, path), 404)
                served, body = self.served(self.anon, self.vdownload(self.anon, self.share, path))
                self.assertNotIn(b"outside", body)
                self.assertNotEqual(served.status_code, 200)
        self.assertRefused(self.vlist(self.anon, self.share, "/docs/outdir"), 404)
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("out.txt", FILE)]), 404, "not_found")
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("outdir", FOLDER)]), 404, "not_found")
        self.assertRefused(self.share_create(self.as_alice, "/docs/outdir", [("inner.txt", FILE)]), 404)

    def test_nothing_written_through_them(self):
        self.assertRefused(self.upload(self.as_alice, "/docs/outdir", "new.txt", b"in?"))
        self.assertRefused(self.upload(self.as_alice, "/docs", "new.txt", b"in?", relative_path="outdir/deeper"))
        self.assertRefused(self.upload(self.as_alice, "/docs", "out.txt", b"overwritten?"))
        self.assertRefused(self.upload(self.as_alice, "/docs", "out.txt", b"overwritten?", overwrite=True))
        self.assertRefused(self.save_text(self.as_alice, "/docs/out.txt", "overwritten?"))
        self.assertRefused(self.save_text(self.as_alice, "/docs/outdir/new.txt", "in?"))
        self.assertRefused(self.mkdir(self.as_alice, "/docs/outdir", "new"))
        self.assertRefused(self.make_folders(self.as_alice, "/docs/outdir", ["new"]))
        made = self.make_folders(self.as_alice, "/docs", ["outdir/new"])
        self.assertFalse(made.status_code == 200 and made.json()["created"], made.content)
        self.assertOutsideUnchanged()

    def test_no_file_operation_reaches_them(self):
        for name in ("out.txt", "outdir", "rel.txt"):
            with self.subTest(name=name):
                self.assertRefused(self.rename(self.as_alice, "/docs", name, "renamed"), 404)
                self.assertEqual(self.batch_codes(self.delete(self.as_alice, "/docs", [name])), ["not_found"])
                self.assertEqual(self.batch_codes(self.move(self.as_alice, "/docs", [name], "/")), ["not_found"])
                self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/docs", [name], "/")), ["not_found"])
        self.assertRefused(self.move(self.as_alice, "/", ["top.txt"], "/docs/outdir"), 404)
        self.assertRefused(self.copy(self.as_alice, "/", ["top.txt"], "/docs/outdir"), 404)
        # A copy of the folder holding them leaves them out (and never copies what they point to).
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/", ["docs"], "/home")))
        copied = sorted(p.name for p in (self.root / "home/docs").iterdir())
        self.assertNotIn("out.txt", copied)
        self.assertNotIn("outdir", copied)
        self.assertFalse(any(b"outside" in p.read_bytes() for p in (self.root / "home/docs").rglob("*") if p.is_file()))
        # Deleting the folder removes the links, not what they point to.
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/", ["docs"])))
        self.assertOutsideUnchanged()

    def test_a_readme_file_that_leads_outside(self):
        self.storage.readme_from_file = True
        self.storage.save()
        symlink(self.outside / "x.md", self.root / "docs" / "README.md")
        self.assertIsNone(self.assertOk(self.list_(self.as_alice, "/docs")).json()["readme"])

    def test_for_every_actor(self):
        self.grant(self.guest, EVERYTHING)
        for client in (self.anon, self.as_staff):
            with self.subTest(client=client):
                self.assertEqual(self.names(client, "/docs"), ["a.txt", "b.txt", "in.txt", "indir", "sub"])
                self.assertRefused(self.item(client, "/docs/out.txt"), 404)


class ExternalLinksOnTests(SymlinkCase):
    """With allow_external_symlinks (a superuser's choice, D50) they are ordinary files and folders."""

    def setUp(self):
        super().setUp()
        self.ext = self.make_storage("ext", self.root, external=True)
        self.grant(self.alice, EVERYTHING, storage=self.ext)

    def test_listed_and_read(self):
        self.assertEqual(self.names(self.as_alice, "/docs", key="ext"),
                         ["a.txt", "b.txt", "in.txt", "indir", "out.md", "out.txt", "outdir", "rel.txt", "reldir", "sub"])
        self.assertEqual(self.names(self.as_alice, "/docs/outdir", key="ext"), ["inner.txt"])
        url = self.inline_url(self.as_alice, "/docs/out.txt", key="ext")
        self.assertEqual(self.fetch(self.anon, url)[1], b"outside secret")
        url = self.inline_url(self.as_alice, "/docs/reldir/inner.txt", key="ext")
        self.assertEqual(self.fetch(self.anon, url)[1], b"outside inner")
        # The other storage on the same folder, without the switch, still hides them.
        self.assertNotIn("out.txt", self.names(self.as_alice, "/docs"))
