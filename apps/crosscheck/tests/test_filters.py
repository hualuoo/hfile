"""Rule 2, the filter rules: each mode (仅隐藏, 不可访问, 不可下载) on a file and on the folder above it,
through the listing and the item, /pd/, the preview sources, uploads, the file operations, links and
shares; 忽略隐藏 bypasses them only in the actor's own view (never in links or shares)."""

from __future__ import annotations

from apps.accounts.permissions import Permission
from apps.links.models import ShortLink
from apps.shares.models import Share
from apps.storages import access as policy

from .base import EVERYTHING, FILE, FOLDER, CrossCase, FilterMode, query_of

HIDDEN, INACCESSIBLE, NO_DOWNLOAD = FilterMode.HIDDEN, FilterMode.INACCESSIBLE, FilterMode.DISABLE_DOWNLOAD


class FilterCase(CrossCase):
    """Links and a share made before the rule, to see that a rule added later stops them."""

    def setUp(self):
        super().setUp()
        self.files({"docs/notes.md": b"# notes\n\nhello"})
        self.old_pd = self.file_url(self.as_alice, "/docs/a.txt")
        self.old_inline = self.inline_url(self.as_alice, "/docs/notes.md")
        self.old_short = self.short_link(self.as_alice, "/docs/a.txt")
        self.old_path = self.path_link(self.as_alice, "/docs/a.txt")
        self.share = self.share_of(self.alice, "/", [("docs", FOLDER), ("top.txt", FILE)])

    def opens(self, client, url):
        response, target, body = self.follow(client, url)
        return response.status_code == 302 and target is not None and target.status_code == 200

    def downloads(self, client, url) -> bool:
        response, body = self.fetch(client, url)
        return response.status_code == 200

    def assertShareRefuses(self, path, status=403, code=None):
        self.assertRefused(self.vitem(self.anon, self.share, path), status, code)
        self.assertRefused(self.vdownload(self.anon, self.share, path), status)
        self.assertRefused(self.vpreview(self.anon, self.share, path), status)


class HiddenTests(FilterCase):
    """仅隐藏: left out of listings, opened by its path anywhere."""

    def test_on_a_file(self):
        self.hide("/docs/a.txt", HIDDEN)
        self.assertNotIn("a.txt", self.names(self.as_alice, "/docs"))
        self.assertOk(self.item(self.as_alice, "/docs/a.txt"))
        self.assertTrue(self.downloads(self.anon, self.old_pd))
        self.assertTrue(self.opens(self.anon, self.old_short))
        self.assertTrue(self.opens(self.as_alice, self.old_path))
        self.assertOk(self.path_links(self.as_alice, ["/docs/a.txt"]))
        self.assertOk(self.short_links(self.as_alice, ["/docs/a.txt"]), 201)
        # Shares: not listed, opened by its path; not shareable itself (its visitors couldn't see it).
        self.assertNotIn("a.txt", self.vnames(self.anon, self.share, "/docs"))
        self.assertOk(self.vitem(self.anon, self.share, "/docs/a.txt"))
        self.assertEqual(self.served(self.anon, self.vdownload(self.anon, self.share, "/docs/a.txt"))[1], b"alpha")
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 400, "hidden")
        # Changing it is fine.
        self.assertOk(self.upload(self.as_alice, "/docs", "a.txt", b"new", overwrite=True), 201)
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/docs", ["a.txt"], "/")))
        self.assertOk(self.rename(self.as_alice, "/docs", "a.txt", "z.txt"))

    def test_on_a_folder(self):
        self.hide("/docs", HIDDEN)
        self.assertNotIn("docs", self.names(self.as_alice))
        self.assertEqual(self.names(self.as_alice, "/docs"), ["a.txt", "b.txt", "notes.md", "sub"])
        self.assertTrue(self.downloads(self.anon, self.file_url(self.as_alice, "/docs/a.txt")))
        self.assertNotIn("docs", self.vnames(self.anon, self.share))
        self.assertEqual(self.vnames(self.anon, self.share, "/docs"), ["a.txt", "b.txt", "notes.md", "sub"])
        self.assertRefused(self.share_create(self.as_alice, "/", [("docs", FOLDER)]), 400, "hidden")
        self.assertOk(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 201)
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/", ["docs"], "/home")))
        self.assertOk(self.rename(self.as_alice, "/", "docs", "papers"))

    def test_for_the_guest_and_staff_too(self):
        """Anybody without 忽略隐藏 is filtered, the guest and a staff admin included."""
        self.hide("/docs/a.txt", HIDDEN)
        self.grant(self.guest, [Permission.PREVIEW, Permission.DOWNLOAD])
        for client in (self.anon, self.as_staff):
            with self.subTest(client=client):
                self.assertNotIn("a.txt", self.names(client, "/docs"))
                self.assertOk(self.item(client, "/docs/a.txt"))


class InaccessibleTests(FilterCase):
    """不可访问: it and everything below it, for every operation, also links made before the rule."""

    def test_on_a_file_reading(self):
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.assertNotIn("a.txt", self.names(self.as_alice, "/docs"))
        self.assertRefused(self.item(self.as_alice, "/docs/a.txt"), 403, "inaccessible")
        self.assertRefused(self.anon.get(self.old_pd), 403)
        self.assertRefused(self.list_(self.as_alice, "/docs/a.txt"), 403, "inaccessible")

    def test_on_a_file_links(self):
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.assertRefused(self.anon.get(self.old_short), 403)
        self.assertRefused(self.as_alice.get(self.old_path), 403)
        self.assertRefused(self.path_links(self.as_alice, ["/docs/a.txt"]), 403, "inaccessible")
        self.assertRefused(self.short_links(self.as_alice, ["/docs/a.txt"]), 403, "inaccessible")
        self.site(path_link_anonymous=True)
        self.assertRefused(self.anon.get(self.old_path), 403)

    def test_on_a_file_shares(self):
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.assertNotIn("a.txt", self.vnames(self.anon, self.share, "/docs"))
        self.assertShareRefuses("/docs/a.txt", 403, "inaccessible")
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 403, "inaccessible")

    def test_on_a_file_writing(self):
        """Nothing answers 目标已存在 about it either: the policy comes before the storage is asked."""
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.assertRefused(self.upload_session(self.as_alice, "/docs", "a.txt", 3), 403, "inaccessible")
        self.assertRefused(self.upload_session(self.as_alice, "/docs", "a.txt", 3, overwrite=True), 403, "inaccessible")
        self.assertRefused(self.save_text(self.as_alice, "/docs/a.txt", "x"), 403, "inaccessible")
        self.assertRefused(self.mkdir(self.as_alice, "/docs", "a.txt"), 403, "inaccessible")
        self.assertRefused(self.rename(self.as_alice, "/docs", "a.txt", "z.txt"), 403, "inaccessible")
        self.assertRefused(self.rename(self.as_alice, "/docs", "b.txt", "a.txt"), 403, "inaccessible")
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/docs", ["a.txt"], "/")), ["inaccessible"])
        self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/docs", ["a.txt"], "/")), ["inaccessible"])
        self.assertEqual(self.batch_codes(self.delete(self.as_alice, "/docs", ["a.txt"])), ["inaccessible"])
        self.files({"elsewhere/a.txt": b"other"})
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/elsewhere", ["a.txt"], "/docs")), ["inaccessible"])
        self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/elsewhere", ["a.txt"], "/docs")), ["inaccessible"])
        self.assertEqual((self.root / "docs/a.txt").read_bytes(), b"alpha")

    def test_the_folder_holding_it(self):
        """Deleting, moving, copying or renaming /docs as a whole would take a.txt with it."""
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.assertEqual(self.batch_codes(self.delete(self.as_alice, "/", ["docs"])), ["inaccessible"])
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/", ["docs"], "/home")), ["inaccessible"])
        self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/", ["docs"], "/home")), ["inaccessible"])
        self.assertRefused(self.rename(self.as_alice, "/", "docs", "papers"), 403, "inaccessible")
        self.assertTrue((self.root / "docs/a.txt").exists())
        # The folder's other files are fine one by one.
        self.assertTrue(self.batch_ok(self.copy(self.as_alice, "/docs", ["b.txt"], "/home")))

    def test_on_a_folder(self):
        self.hide("/docs", INACCESSIBLE)
        self.assertNotIn("docs", self.names(self.as_alice))
        for path in ("/docs", "/docs/sub"):
            with self.subTest(path=path):
                self.assertRefused(self.list_(self.as_alice, path), 403, "inaccessible")
        for path in ("/docs/a.txt", "/docs/sub/c.txt", "/docs/nothing-here.txt"):
            with self.subTest(path=path):
                self.assertRefused(self.item(self.as_alice, path), 403, "inaccessible")
        self.assertRefused(self.anon.get(self.old_pd), 403)
        self.assertRefused(self.preview_markdown(self.anon, self.old_inline), 403, "inaccessible")
        self.assertRefused(self.anon.get(self.old_short), 403)
        self.assertRefused(self.as_alice.get(self.old_path), 403)

    def test_on_a_folder_writing(self):
        self.hide("/docs", INACCESSIBLE)
        self.assertRefused(self.upload_session(self.as_alice, "/docs", "new.txt", 3), 403, "inaccessible")
        self.assertRefused(self.upload_session(self.as_alice, "/", "new.txt", 3, relative_path="docs/deeper"), 403,
                           "inaccessible")
        self.assertRefused(self.make_folders(self.as_alice, "/", ["docs/new"]), 403, "inaccessible")
        self.assertRefused(self.make_folders(self.as_alice, "/docs", ["new"]), 403, "inaccessible")
        self.assertRefused(self.save_text(self.as_alice, "/docs/new.txt", "x"), 403, "inaccessible")
        self.assertRefused(self.mkdir(self.as_alice, "/docs", "new"), 403, "inaccessible")
        self.assertRefused(self.move(self.as_alice, "/", ["top.txt"], "/docs"), 403, "inaccessible")
        self.assertRefused(self.copy(self.as_alice, "/", ["top.txt"], "/docs/sub"), 403, "inaccessible")
        self.assertEqual(self.batch_codes(self.delete(self.as_alice, "/", ["docs"])), ["inaccessible"])
        self.assertRefused(self.rename(self.as_alice, "/", "docs", "papers"), 403, "inaccessible")
        # Making a folder of that name again elsewhere and moving it in: refused at the target.
        self.assertOk(self.mkdir(self.as_alice, "/home", "docs"), 201)
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/home", ["docs"], "/")), ["inaccessible"])
        self.assertEqual(sorted(p.name for p in (self.root / "docs").iterdir()), ["a.txt", "b.txt", "notes.md", "sub"])

    def test_on_a_folder_shares(self):
        self.hide("/docs", INACCESSIBLE)
        self.assertNotIn("docs", self.vnames(self.anon, self.share))
        self.assertRefused(self.vlist(self.anon, self.share, "/docs"), 403, "inaccessible")
        self.assertShareRefuses("/docs/a.txt", 403, "inaccessible")
        self.assertRefused(self.vunlock(self.anon, self.share, "/docs", "x"), 403, "inaccessible")
        self.assertRefused(self.share_create(self.as_alice, "/", [("docs", FOLDER)]), 403, "inaccessible")
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 403, "inaccessible")
        inside = self.share_of(self.alice, "/docs", [("a.txt", FILE)])
        self.assertRefused(self.vlist(self.anon, inside), 403, "inaccessible")

    def test_added_halfway_through_an_upload(self):
        """Every chunk checks the rules again (apps.uploads.service.recheck)."""
        made = self.assertOk(self.upload_session(self.as_alice, "/docs", "big.bin", 2500), 201).json()
        self.assertOk(self.put_chunk(self.as_alice, made, b"x" * 1000, 0, 2500), 202)
        rule = self.hide("/docs/big.bin", INACCESSIBLE)
        self.assertRefused(self.put_chunk(self.as_alice, made, b"x" * 1000, 1000, 2500), 403, "inaccessible")
        rule.delete()
        self.assertOk(self.put_chunk(self.as_alice, made, b"x" * 1000, 1000, 2500), 202)
        self.hide("/docs", INACCESSIBLE)
        self.assertRefused(self.put_chunk(self.as_alice, made, b"x" * 500, 2000, 2500), 403, "inaccessible")
        self.assertFalse((self.root / "docs/big.bin").exists())

    def test_a_rule_by_name_anywhere(self):
        """**/secret: wherever it is, whatever a move does with it."""
        self.files({"docs/sub/secret/s.txt": b"s", "home/alice/secret.txt": b"no"})
        self.hide("**/secret", INACCESSIBLE)
        self.assertNotIn("secret", self.names(self.as_alice, "/docs/sub"))
        self.assertRefused(self.item(self.as_alice, "/docs/sub/secret/s.txt"), 403, "inaccessible")
        self.assertOk(self.item(self.as_alice, "/home/alice/secret.txt"))
        # The folder holding it moves; what is named so stays out of reach where it lands.
        self.assertTrue(self.batch_ok(self.move(self.as_alice, "/docs", ["sub"], "/home")))
        self.assertRefused(self.item(self.as_alice, "/home/sub/secret/s.txt"), 403, "inaccessible")
        self.assertRefused(self.mkdir(self.as_alice, "/home", "secret"), 403, "inaccessible")


class NoDownloadTests(FilterCase):
    """不可下载: its content can't be read (download, preview, links) and it can't be copied, moved or
    renamed (a new name would make it downloadable); deleting and replacing it are fine."""

    def test_on_a_file(self):
        self.hide("/docs/a.txt", NO_DOWNLOAD)
        self.assertNotIn("a.txt", self.names(self.as_alice, "/docs"))
        self.assertRefused(self.item(self.as_alice, "/docs/a.txt"), 403, "download_disabled")
        self.assertRefused(self.anon.get(self.old_pd), 403)
        self.assertRefused(self.anon.get(self.old_short), 403)
        self.assertRefused(self.as_alice.get(self.old_path), 403)
        self.assertRefused(self.path_links(self.as_alice, ["/docs/a.txt"]), 403, "download_disabled")
        self.assertRefused(self.short_links(self.as_alice, ["/docs/a.txt"]), 403, "download_disabled")
        self.assertRefused(self.rename(self.as_alice, "/docs", "a.txt", "z.txt"), 403, "download_disabled")
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/docs", ["a.txt"], "/")), ["download_disabled"])
        self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/docs", ["a.txt"], "/")), ["download_disabled"])
        self.assertNotIn("a.txt", self.vnames(self.anon, self.share, "/docs"))
        self.assertShareRefuses("/docs/a.txt", 403, "download_disabled")
        # Replacing (上传 + 删除, D17a) and deleting don't hand the content out.
        self.assertOk(self.upload(self.as_alice, "/docs", "a.txt", b"replaced", overwrite=True), 201)
        self.assertRefused(self.item(self.as_alice, "/docs/a.txt"), 403, "download_disabled")
        saved = self.assertOk(self.save_text(self.as_alice, "/docs/a.txt", "saved")).json()
        self.assertNotIn("content", saved)
        self.assertRefused(self.item(self.as_alice, "/docs/a.txt"), 403, "download_disabled")
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/docs", ["a.txt"])))

    def test_on_a_folder(self):
        self.hide("/docs", NO_DOWNLOAD)
        self.assertNotIn("docs", self.names(self.as_alice))
        items = self.items(self.as_alice, "/docs")
        self.assertEqual(sorted(items), ["a.txt", "b.txt", "notes.md", "sub"])
        self.assertEqual({name: item["url"] for name, item in items.items()},
                         dict.fromkeys(items))  # no download URL for any of them
        for path in ("/docs/a.txt", "/docs/sub/c.txt"):
            with self.subTest(path=path):
                self.assertRefused(self.item(self.as_alice, path), 403, "download_disabled")
        self.assertRefused(self.preview_markdown(self.anon, self.old_inline), 403, "download_disabled")
        # Share: listed inside (no URLs), never downloaded or previewed.
        items = self.vitems(self.anon, self.share, "/docs")
        self.assertEqual({item["url"] for item in items.values()} | {item["download_url"] for item in items.values()},
                         {None})
        self.assertShareRefuses("/docs/sub/c.txt", 403, "download_disabled")

    def test_the_folder_holding_it(self):
        self.hide("/docs/a.txt", NO_DOWNLOAD)
        self.assertEqual(self.batch_codes(self.move(self.as_alice, "/", ["docs"], "/home")), ["download_disabled"])
        self.assertEqual(self.batch_codes(self.copy(self.as_alice, "/", ["docs"], "/home")), ["download_disabled"])
        self.assertRefused(self.rename(self.as_alice, "/", "docs", "papers"), 403, "download_disabled")
        self.assertTrue(self.batch_ok(self.delete(self.as_alice, "/", ["docs"])))

    def test_by_suffix(self):
        """**.txt anywhere: the README's links and the share's URLs leave them out too."""
        self.hide("**.txt", NO_DOWNLOAD)
        items = self.items(self.as_alice, "/docs")
        self.assertEqual(sorted(items), ["notes.md", "sub"])
        self.assertIsNotNone(items["notes.md"]["url"])
        self.assertRefused(self.item(self.as_alice, "/docs/sub/c.txt"), 403, "download_disabled")
        self.assertRefused(self.anon.get(self.old_pd), 403)
        self.assertShareRefuses("/top.txt", 403, "download_disabled")


class IgnoreHiddenTests(FilterCase):
    """忽略隐藏 is the actor's own view: links and shares never get it."""

    def setUp(self):
        super().setUp()
        self.grant(self.alice, [*EVERYTHING, Permission.IGNORE_HIDDEN])
        self.hide("/docs/a.txt", INACCESSIBLE)
        self.hide("/docs/b.txt", NO_DOWNLOAD)
        self.hide("/top.txt", HIDDEN)

    def test_his_own_view(self):
        self.assertEqual(self.names(self.as_alice, "/docs"), ["a.txt", "b.txt", "notes.md", "sub"])
        self.assertIn("top.txt", self.names(self.as_alice))
        for path, data in (("/docs/a.txt", b"alpha"), ("/docs/b.txt", b"bravo")):
            with self.subTest(path=path):
                url = self.file_url(self.as_alice, path)
                self.assertEqual(query_of(url).get("h"), ["1"])
                self.assertEqual(self.fetch(self.anon, url)[1], data)
                inline = self.inline_url(self.as_alice, path)
                self.assertEqual(query_of(inline).get("h"), ["1"])
        # A file no rule refuses gets a link that the rules still apply to (a rule added later counts).
        url = self.file_url(self.as_alice, "/docs/sub/c.txt")
        self.assertNotIn("h", query_of(url))
        self.hide("/docs/sub", INACCESSIBLE)
        self.assertRefused(self.anon.get(url), 403)
        # He may work where the rules would stop others.
        self.assertOk(self.rename(self.as_alice, "/docs", "b.txt", "bb.txt"))
        self.assertOk(self.upload(self.as_alice, "/docs", "a.txt", b"mine", overwrite=True), 201)

    def test_never_in_links(self):
        for path, code in (("/docs/a.txt", "inaccessible"), ("/docs/b.txt", "download_disabled")):
            with self.subTest(path=path):
                self.assertRefused(self.path_links(self.as_alice, [path]), 403, code)
                self.assertRefused(self.short_links(self.as_alice, [path]), 403, code)
        # A short link he made before the rule: re-checked as him, the rules apply.
        self.assertRefused(self.anon.get(self.old_short), 403)
        self.assertRefused(self.as_alice.get(self.old_path), 403)
        self.assertEqual(ShortLink.objects.count(), 1)

    def test_never_in_shares(self):
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("a.txt", FILE)]), 400, "hidden")
        self.assertRefused(self.share_create(self.as_alice, "/docs", [("b.txt", FILE)]), 400, "hidden")
        self.assertRefused(self.share_create(self.as_alice, "/", [("top.txt", FILE)]), 400, "hidden")
        self.assertEqual(self.vnames(self.anon, self.share, "/docs"), ["notes.md", "sub"])
        self.assertShareRefuses("/docs/a.txt", 403, "inaccessible")
        self.assertShareRefuses("/docs/b.txt", 403, "download_disabled")
        # The share's own links never carry h=1.
        item = self.assertOk(self.vitem(self.anon, self.share, "/docs/notes.md")).json()
        self.assertNotIn("h", query_of(item["inline_url"]))
        self.assertEqual(Share.objects.count(), 1)

    def test_a_staff_admin_without_it(self):
        self.assertEqual(self.names(self.as_staff, "/docs"), ["notes.md", "sub"])
        self.assertRefused(self.item(self.as_staff, "/docs/a.txt"), 403, "inaccessible")
        self.assertRefused(self.item(self.as_staff, "/docs/b.txt"), 403, "download_disabled")


class RuleSpellingTests(CrossCase):
    """A rule can't be passed by spelling the path otherwise: case, trailing dots and spaces, "\\" (as a
    Windows server reads it), Unicode forms (apps/storages/rules.py canonical())."""

    def setUp(self):
        super().setUp()
        self.files({"private/p.txt": b"p", "caf\u00e9/x.txt": b"x"})
        self.hide("/private", INACCESSIBLE)
        self.hide("/caf\u00e9", INACCESSIBLE)  # NFC

    def refused(self, path):
        try:
            access = policy.open_storage(None, self.storage, actor=policy.Actor(self.alice))
            access.check(policy.READ, access.path(path))
        except policy.AccessDenied as error:
            return error.code
        return None

    def test_spellings(self):
        for path in ("/private/p.txt", "/PRIVATE/p.txt", "/Private./p.txt", "/private /p.txt", "/private. ./p.txt",
                     "/private\\p.txt", "/café/x.txt", "/CAFÉ/x.txt", "/cafe\u0301/x.txt"):
            with self.subTest(path=path):
                self.assertIn(self.refused(path), ("inaccessible", "invalid_path"))
