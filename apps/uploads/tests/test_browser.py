"""The upload panel (static/uploads/js/uploads.js) in a real browser, in the file browser, uploading to a local
storage. The browser tests' base: apps/web/test_browser.py."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from apps.accounts.models import User
from apps.web.test_browser import BrowserTestCase

from ..service import FOLDERS_MAX


class FolderUploadTests(BrowserTestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.chosen = Path(tempfile.mkdtemp(prefix="hfile-chosen-"))  # the folders the browser uploads from
        self.addCleanup(shutil.rmtree, self.chosen, ignore_errors=True)

    def choose_folder(self, page, folder: Path):
        """Uploads `folder` with 上传文件夹 to the folder `page` shows."""
        page.click("#files-upload-button")
        with page.expect_file_chooser() as chooser:
            page.click("#files-upload-menu [data-files-action=upload_folder]")
        chooser.value.set_files(folder)

    def uploaded(self, page, count: int) -> list[list[str]]:
        """The panel's `count` rows, [state, text], once none is waiting or uploading."""
        page.wait_for_function("n => document.querySelectorAll('.upload-row').length === n && !App.uploads.count", arg=count)
        return page.locator(".upload-row").evaluate_all("rows => rows.map((row) => [row.dataset.state, row.textContent])")

    @unittest.skipIf(os.name == "nt", "Windows: a path 100 folders deep in %TEMP% is longer than MAX_PATH (260 characters)")
    def test_a_deep_folder_tree(self):
        # FILE-01: each file's folder has 100 more above it in the upload, so they name more folders than one
        # request may (FOLDERS_MAX, with those above). The panel sent 500 of them at a time, counting
        # only those: one request for all, refused, and every file failed with it.
        chain = "/".join(["d"] * 99)
        names = [f"tree/b{i}/{chain}/f{i}.txt" for i in range(FOLDERS_MAX // 100 + 1)]
        for name in names:
            (self.chosen / name).parent.mkdir(parents=True)
            (self.chosen / name).write_text(name)
        root = self.local_storage({}, users=[self.admin])
        page = self.open(self.new_context(user=self.admin), "/disk/")
        asked = []
        page.on("request", lambda request: request.url.endswith("/api/v1/uploads/folders/") and asked.append(request))
        self.choose_folder(page, self.chosen / "tree")
        rows = self.uploaded(page, len(names))
        self.assertEqual([state for state, _ in rows], ["done"] * len(names), rows)
        for name in names:
            self.assertEqual((root / name).read_text(), name)
        self.assertGreater(len(asked), 1)  # what the test is about
        self.assertClean(page)

    @unittest.skipIf(os.name == "nt", "Windows has no names with \\ in them")
    def test_a_backslash_in_names(self):
        # FILE-02: "\" is a character of a name. Files go into the folder "a\b" (made outside HFile), where the
        # panel took it for a separator and sent them to a/b. A dropped folder "c\d" is one name, which a
        # new folder can't have (check_name): refused, saying so, where the panel made c and d. Chrome's
        # folder chooser itself gives "/" for a "\" in the names it finds (webkitRelativePath), so "c\d"
        # comes as from a drop: start() gets what drop() makes of the entries.
        (self.chosen / "up").mkdir()
        (self.chosen / "up" / "f.txt").write_bytes(b"f")
        root = self.local_storage({"a\\b/old.txt": b"old"}, users=[self.admin])
        page = self.open(self.new_context(user=self.admin), "/disk/a%5Cb/")
        self.choose_folder(page, self.chosen / "up")
        self.assertEqual([state for state, _ in self.uploaded(page, 1)], ["done"])
        page.evaluate("path => App.uploads.start(App.files.current.ctx, [{file: new File(['g'], 'g.txt'), path}])", "drop/c\\d/g.txt")
        rows = sorted(self.uploaded(page, 2))
        self.assertEqual([state for state, _ in rows], ["done", "failed"], rows)
        self.assertIn("名字里不能有 \\。", rows[1][1])
        self.assertEqual(sorted(str(path.relative_to(root)) for path in root.rglob("*")),
                         ["a\\b", "a\\b/old.txt", "a\\b/up", "a\\b/up/f.txt"])
        self.assertClean(page, allow=[r"^400 POST .*/api/v1/uploads/folders/$", r"^console: Failed to load resource: .* 400"])
