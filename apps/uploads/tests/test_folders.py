"""The folders of a folder upload (POST /api/v1/uploads/folders/): parents first, empty ones too,
新建文件夹 and folder passwords checked for all of them before any is made, a file in the way."""

import json

from apps.storages.models import FilterMode, FilterRule, FolderPassword

from .base import API, EVERYTHING, UploadTestCase

URL = f"{API}folders/"


class FolderTests(UploadTestCase):
    def make(self, folders, folder="/", client=None, **more):
        body = {"storage": "disk", "folder": folder, "folders": folders, **more}
        return (client or self.client).post(URL, json.dumps(body), content_type="application/json")

    def test_parents_first_and_empty_ones(self):
        (self.root / "photos").mkdir()
        answer = self.make(["photos/2024/summer", "photos/empty", "docs"])
        self.assertEqual(answer.status_code, 200, answer.content)
        body = answer.json()
        self.assertEqual(body["created"], ["/docs", "/photos/2024", "/photos/empty", "/photos/2024/summer"])
        self.assertEqual(body["existing"], ["/photos"])
        self.assertEqual(body["failed"], [])
        for name in ("docs", "photos/2024/summer", "photos/empty"):
            self.assertTrue((self.root / name).is_dir(), name)
        self.assertEqual(self.signals, [("disk", ["/docs", "/photos/2024", "/photos/empty", "/photos/2024/summer"])])
        # Again: all of them exist, nothing is made.
        again = self.make(["photos/2024/summer", "photos/empty", "docs"]).json()
        self.assertEqual(again["created"], [])
        self.assertEqual(len(again["existing"]), 5)

    def test_inside_a_folder_below_the_root(self):
        (self.root / "home" / "up").mkdir(parents=True)
        self.grant(self.alice, EVERYTHING, root="/home")
        body = self.make(["a/b"], folder="/up").json()
        self.assertEqual(body["created"], ["/up/a", "/up/a/b"])
        self.assertTrue((self.root / "home" / "up" / "a" / "b").is_dir())

    def test_a_file_in_the_way(self):
        (self.root / "taken").write_text("x")
        body = self.make(["taken/inner/deeper", "fine"]).json()
        self.assertEqual(body["created"], ["/fine"])
        self.assertEqual([(item["path"], item["code"]) for item in body["failed"]],
                         [("/taken", "file_exists"), ("/taken/inner", "parent_failed"), ("/taken/inner/deeper", "parent_failed")])
        self.assertTrue((self.root / "taken").is_file())

    def test_new_folder_is_needed(self):
        self.grant(self.alice, [code for code in EVERYTHING if code != "new_folder"])
        self.assertCode(self.make(["a"]), 403, "permission_denied")
        # Also when everything exists: the answer would tell what is there.
        (self.root / "a").mkdir()
        self.assertCode(self.make(["a"]), 403, "permission_denied")

    def test_no_answer_about_places_the_actor_may_not_go(self):
        """The policy comes before any lookup: inside a locked folder, existing and missing names get
        the same answer; an inaccessible folder is refused even when it exists."""
        (self.root / "locked" / "there").mkdir(parents=True)
        FolderPassword.objects.create(storage=self.storage, expression="/locked", password="pw")
        one = self.assertCode(self.make(["locked/there"]), 403, "password_required")
        two = self.assertCode(self.make(["locked/nothing"]), 403, "password_required")
        self.assertEqual((one["detail"], one["rule"]), (two["detail"], two["rule"]))
        FilterRule.objects.create(storage=self.storage, expression="/hidden-away", mode=FilterMode.INACCESSIBLE)
        (self.root / "hidden-away").mkdir()
        self.assertCode(self.make(["hidden-away"]), 403, "inaccessible")

    def test_the_base_folder_must_exist(self):
        self.assertCode(self.make(["a"], folder="/nowhere"), 404, "not_found")
        self.assertFalse((self.root / "nowhere").exists())
        (self.root / "file.txt").write_text("x")
        self.assertCode(self.make(["a"], folder="/file.txt"), 409, "parent_is_file")

    def test_deep_and_many(self):
        self.assertCode(self.make(["/".join(["d"] * 257)]), 400, "invalid_path")
        # Long paths that share their folders are cheap and fine...
        shared = "/".join(f"s{n}" for n in range(200))
        answer = self.make([f"{shared}/leaf{n}" for n in range(5)])
        self.assertEqual(answer.status_code, 200, answer.content[:300])
        self.assertEqual(len(answer.json()["created"]), 205)
        # ...too many different ones are refused before anything is made.
        self.assertCode(self.make([f"x{n}/y" for n in range(501)]), 400, "too_many_folders")
        self.assertFalse((self.root / "x0").exists())

    def test_every_folder_is_checked_before_any_is_made(self):
        FolderPassword.objects.create(storage=self.storage, expression="/up/*/locked", password="pw")
        (self.root / "up").mkdir()
        body = self.assertCode(self.make(["one", "x/locked"], folder="/up"), 403, "password_required")
        self.assertEqual(body["folder"], "/up/x/locked")
        self.assertFalse((self.root / "up" / "one").exists())
        self.assertFalse((self.root / "up" / "x").exists())
        made = self.make(["one", "x/locked"], folder="/up", password="pw")
        self.assertEqual(made.status_code, 200, made.content)
        self.assertTrue((self.root / "up" / "x" / "locked").is_dir())

    def test_filter_rules(self):
        FilterRule.objects.create(storage=self.storage, expression="/**/private", mode=FilterMode.INACCESSIBLE)
        self.assertCode(self.make(["a/private"]), 403, "inaccessible")
        self.assertFalse((self.root / "a").exists())

    def test_names_and_paths(self):
        self.assertCode(self.make(["a/../../x"]), 400, "invalid_path")
        self.assertCode(self.make(["ok"], folder="/.."), 400, "invalid_path")
        self.assertCode(self.make(["bad\tname"]), 400, "invalid_name")
        self.assertEqual(self.make([]).status_code, 400)
        self.assertEqual(self.make(["x" * 5000]).status_code, 400)

    def test_too_many(self):
        self.assertEqual(self.make([f"f{n}" for n in range(1001)]).status_code, 400)
        answer = self.make([f"f{n}/g" for n in range(600)])  # 1200 folders with their parents
        self.assertCode(answer, 400, "too_many_folders")

    def test_the_guest(self):
        from django.test import Client

        guest = Client()
        self.assertCode(self.make(["a"], client=guest), 401, "login_required")
        self.grant(self.guest, ["upload", "new_folder"])
        self.assertEqual(self.make(["a"], client=guest).json()["created"], ["/a"])
