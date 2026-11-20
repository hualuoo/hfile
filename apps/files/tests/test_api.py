"""The files API through the access policy: who may list and change what, roots and escapes, the filter
rules, folder passwords on every operation, D17, per-item results, capabilities, sorting, readmes, the
item endpoint and the signal of W5."""

import datetime
import os
import urllib.parse
from unittest import mock

from django.test import SimpleTestCase
from django.utils import timezone

from apps.files import signals
from apps.files.service import iso, natural_key, sort_entries
from apps.siteconf.models import SiteSettings
from apps.storages.drivers.base import Entry
from apps.storages.models import FilterMode

from .helpers import API, EVERYTHING, NOT_DISK, FilesTestCase


class WhoTests(FilesTestCase):
    def test_the_guest_needs_a_grant(self):
        response = self.get("list", path="/")
        self.assertEqual((response.status_code, self.code(response)), (401, "login_required"))
        self.grant(self.guest, ["preview"])
        self.assertEqual(self.names(), ["docs", "top.txt"])

    def test_a_user_with_and_without_a_grant(self):
        self.login()
        self.assertEqual(self.names("/docs"), ["sub", "a.txt", "b.txt"])
        self.grant(self.alice, EVERYTHING, enabled=False)
        response = self.get("list", path="/")
        self.assertEqual((response.status_code, self.code(response)), (403, "no_grant"))

    def test_a_logged_in_user_never_gets_the_guests_grant(self):
        self.grant(self.guest, EVERYTHING)
        from apps.accounts.models import User

        bob = User.objects.create_user("bob", password="pw-bob-2026x")
        self.login(bob)
        self.assertEqual(self.code(self.get("list", path="/")), "no_grant")

    def test_a_disabled_storage_and_an_unknown_one(self):
        self.login()
        self.storage.enabled = False
        self.storage.save()
        response = self.get("list", path="/")
        self.assertEqual((response.status_code, self.code(response)), (403, "storage_disabled"))
        response = self.get("list", key="nope", path="/")
        self.assertEqual((response.status_code, self.code(response)), (404, "storage_not_found"))

    def test_only_the_key_finds_the_storage(self):
        # API-16: the key in any case, nothing that only compares equal to it on MySQL.
        self.login()
        self.assertEqual(self.get("list", key="DISK", path="/").status_code, 200)
        for key in NOT_DISK:
            with self.subTest(key=key):
                response = self.get("list", key=urllib.parse.quote(key), path="/")
                self.assertEqual((response.status_code, self.code(response)), (404, "storage_not_found"))
                response = self.unlock("/docs", key=urllib.parse.quote(key))
                self.assertEqual((response.status_code, self.code(response)), (404, "storage_not_found"))

    def test_a_disabled_user_is_a_visitor(self):
        self.login()
        self.alice.is_active = False
        self.alice.save()
        self.assertEqual(self.code(self.get("list", path="/")), "login_required")

    def test_the_permissions_as_the_policy_applies_them(self):
        self.grant(self.alice, ["preview", "generate_link", "generate_short_link", "ignore_hidden"])
        self.login()
        permissions = self.listing()["permissions"]
        self.assertTrue(permissions["preview"])
        self.assertFalse(permissions["download"])
        self.assertFalse(permissions["generate_link"])  # D40a: links need download too
        self.assertFalse(permissions["generate_short_link"])
        self.assertTrue(permissions["ignore_hidden"])
        self.assertEqual(len(permissions), 15)


class PathTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.login()

    def test_the_root_is_the_grants_folder(self):
        answer = self.listing("/")
        self.assertEqual(answer["root"], "/docs")
        self.assertEqual(answer["path"], "/")
        self.assertEqual([(i["name"], i["path"]) for i in answer["items"]],
                         [("sub", "/sub"), ("a.txt", "/a.txt"), ("b.txt", "/b.txt")])
        self.assertEqual(self.names("/sub"), ["c.txt"])

    def test_escapes(self):
        for path in ("..", "/..", "../top.txt", "/sub/../..", "sub/./c.txt", "a\\..\\..\\top"):
            with self.subTest(path=path):
                response = self.get("list", path=path)
                self.assertEqual((response.status_code, self.code(response)), (400, "invalid_path"))
        for path in ("\x00", "a\x00b"):  # refused when the query is read
            with self.subTest(path=path):
                self.assertEqual(self.get("list", path=path).status_code, 400)
        # An encoded slash is only a slash; a folder that isn't there is a 404, not the folder above.
        response = self.get("list", path="sub%2F..%2F..")
        self.assertIn(response.status_code, (400, 404))
        response = self.client.post("/api/v1/files/disk/mkdir/", b'{"path": "/", "name": "x\\ud800"}',
                                    content_type="application/json")  # a lone surrogate, as JSON escapes it
        self.assertEqual(response.status_code, 400)
        response = self.client.post("/api/v1/files/disk/delete/", b'{"path": "/\\udc00", "names": ["a"]}',
                                    content_type="application/json")
        self.assertEqual(response.status_code, 400)
        response = self.post("delete", {"path": "/..", "names": ["top.txt"]})
        self.assertEqual(self.code(response), "invalid_path")
        response = self.post("delete", {"path": "/", "names": ["../top.txt"]})
        self.assertEqual(self.code(response), "invalid_name")
        response = self.post("move", {"path": "/", "names": ["a.txt"], "target": "/../"})
        self.assertEqual(self.code(response), "invalid_path")
        self.assertTrue((self.root / "top.txt").exists())
        self.assertTrue((self.root / "docs/a.txt").exists())

    def test_a_path_too_long(self):
        response = self.get("list", path="/a" * 300)
        self.assertEqual(self.code(response), "invalid_path")


class FilterTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.files({"docs/hidden.txt": b"h", "docs/nodl.txt": b"n", "docs/secret/x.txt": b"x", "docs/nodl-dir/y.txt": b"y"})
        self.hide("/docs/hidden.txt")
        self.hide("/docs/nodl.txt", FilterMode.DISABLE_DOWNLOAD)
        self.hide("/docs/nodl-dir", FilterMode.DISABLE_DOWNLOAD)
        self.hide("/docs/secret", FilterMode.INACCESSIBLE)
        self.login()

    def test_every_mode_hides(self):
        self.assertEqual(self.names("/docs"), ["sub", "a.txt", "b.txt"])

    def test_inaccessible_at_and_below(self):
        for path in ("/docs/secret", "/docs/secret/inner"):
            with self.subTest(path):
                self.assertEqual(self.code(self.get("list", path=path)), "inaccessible")
        self.assertEqual(self.code(self.get("item", path="/docs/secret/x.txt")), "inaccessible")
        response = self.post("delete", {"path": "/docs", "names": ["secret"]})
        self.assertEqual(response.json()["results"][0]["code"], "inaccessible")
        response = self.post("mkdir", {"path": "/docs/secret", "name": "n"})
        self.assertEqual(self.code(response), "inaccessible")
        response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": "/docs/secret"})
        self.assertEqual(self.code(response), "inaccessible")
        self.assertTrue((self.root / "docs/secret/x.txt").exists())

    def test_hidden_is_reachable_by_its_path(self):
        item = self.get("item", path="/docs/hidden.txt").json()
        self.assertEqual(item["name"], "hidden.txt")
        self.assertTrue(item["url"].startswith("/pd/disk/docs/hidden.txt?"))

    def test_disable_download_at_and_below(self):
        for path in ("/docs/nodl.txt", "/docs/nodl-dir/y.txt"):
            with self.subTest(path):
                response = self.get("item", path=path)
                self.assertEqual((response.status_code, self.code(response)), (403, "download_disabled"))
        # Not copied, moved or renamed either (a new name would make it downloadable).
        response = self.post("copy", {"path": "/docs", "names": ["nodl.txt"], "target": "/"})
        self.assertEqual(response.json()["results"][0]["code"], "download_disabled")
        response = self.post("rename", {"path": "/docs", "name": "nodl.txt", "new_name": "ok.txt"})
        self.assertEqual(self.code(response), "download_disabled")
        self.assertTrue((self.root / "docs/nodl.txt").exists())

    def test_no_url_below_a_disable_download_folder(self):
        # The folder is hidden, but can be listed by its path: its files come without a link.
        items = self.listing("/docs/nodl-dir")["items"]
        self.assertEqual([(i["name"], i["url"]) for i in items], [("y.txt", None)])

    def test_ignore_hidden_sees_everything(self):
        self.grant(self.alice, [*EVERYTHING, "ignore_hidden"])
        self.assertEqual(self.names("/docs"), ["nodl-dir", "secret", "sub", "a.txt", "b.txt", "hidden.txt", "nodl.txt"])
        url = self.get("item", path="/docs/nodl.txt").json()["url"]
        self.assertIn("&h=1", url)  # served without the filters, as the policy did for him
        self.assertEqual(b"".join(self.client.get(url).streaming_content), b"n")
        # Only the files that needed it: a rule added later still stops the link of an ordinary file.
        plain = self.get("item", path="/docs/a.txt").json()
        self.assertNotIn("h=1", plain["url"] + plain["inline_url"])
        self.hide("/docs/a.txt", FilterMode.INACCESSIBLE)
        self.assertEqual(self.client.get(plain["url"]).status_code, 403)


class PasswordTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.files({"locked/in.txt": b"in", "locked/deep/d.txt": b"d"})
        self.rule_ = self.lock("/locked", "pw1")
        self.login()

    def test_listing_asks_then_holds(self):
        response = self.get("list", path="/locked")
        self.assertEqual((response.status_code, self.code(response)), (403, "password_required"))
        self.assertEqual(response.json()["rule"], self.rule_.pk)
        self.assertEqual(response.json()["path"], "/locked")
        self.assertEqual(self.code(self.unlock("/locked", "nope")), "password_wrong")
        self.assertEqual(self.unlock("/locked").status_code, 200)
        self.assertEqual(self.names("/locked"), ["deep", "in.txt"])
        self.assertEqual(self.names("/locked/deep"), ["d.txt"])

    def test_a_locked_folder_shows_its_lock(self):
        item = next(i for i in self.listing("/")["items"] if i["name"] == "locked")
        self.assertTrue(item["locked"])
        self.unlock("/locked")
        item = next(i for i in self.listing("/")["items"] if i["name"] == "locked")
        self.assertFalse(item["locked"])
        self.assertNotIn("locked", next(i for i in self.listing("/")["items"] if i["name"] == "docs"))

    def test_a_password_sent_with_the_request(self):
        response = self.client.get("/api/v1/files/disk/list/", {"path": "/locked"}, HTTP_X_FOLDER_PASSWORD="pw1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([i["name"] for i in response.json()["items"]], ["deep", "in.txt"])
        # ...for that request only.
        self.assertEqual(self.code(self.get("list", path="/locked")), "password_required")
        response = self.client.get("/api/v1/files/disk/list/", {"path": "/locked"}, HTTP_X_FOLDER_PASSWORD="bad")
        self.assertEqual(self.code(response), "password_wrong")

    def test_a_header_password_that_isnt_ascii(self):
        """API-14: WSGI gives a header's UTF-8 bytes as latin-1 text; compared as such, it was never right."""
        self.rule_.password = "密码"
        self.rule_.save()
        sent = "密码".encode("utf-8").decode("latin-1")  # what the header looks like in request.META
        response = self.client.get("/api/v1/files/disk/list/", {"path": "/locked"}, HTTP_X_FOLDER_PASSWORD=sent)
        self.assertEqual(response.status_code, 200, response.content)

    def test_every_operation_asks(self):
        calls = [
            ("mkdir", {"path": "/locked", "name": "n"}),
            ("rename", {"path": "/locked", "name": "in.txt", "new_name": "x.txt"}),
            ("delete", {"path": "/locked", "names": ["in.txt"]}),
            ("delete", {"path": "/", "names": ["locked"]}),  # the folder with its protected content
            ("rename", {"path": "/", "name": "locked", "new_name": "open"}),
            ("move", {"path": "/locked", "names": ["in.txt"], "target": "/"}),
            ("move", {"path": "/", "names": ["top.txt"], "target": "/locked"}),  # the target
            ("copy", {"path": "/", "names": ["top.txt"], "target": "/locked/deep"}),
            ("copy", {"path": "/", "names": ["locked"], "target": "/docs"}),
        ]
        for what, body in calls:
            with self.subTest(what=what, body=body):
                response = self.post(what, body)
                self.assertEqual((response.status_code, self.code(response)), (403, "password_required"), response.json())
        self.assertEqual(sorted(os.listdir(self.root / "locked")), ["deep", "in.txt"])
        self.assertTrue((self.root / "top.txt").exists())

    def test_operations_after_unlocking(self):
        self.unlock("/locked")
        self.assertEqual(self.post("mkdir", {"path": "/locked", "name": "new"}).status_code, 201)
        response = self.post("move", {"path": "/", "names": ["top.txt"], "target": "/locked/new"})
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertTrue((self.root / "locked/new/top.txt").exists())

    def test_the_whole_request_stops_before_anything_runs(self):
        # One item needs a password: nothing is deleted, the others neither.
        response = self.post("delete", {"path": "/", "names": ["top.txt", "locked"]})
        self.assertEqual(self.code(response), "password_required")
        self.assertTrue((self.root / "top.txt").exists())

    def test_a_password_in_the_body_for_this_request(self):
        response = self.post("delete", {"path": "/locked", "names": ["in.txt"], "password": "pw1"})
        self.assertEqual(response.json()["succeeded"], 1)
        response = self.post("move", {"path": "/", "names": ["top.txt"], "target": "/locked", "target_password": "pw1"})
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertTrue((self.root / "locked/top.txt").exists())

    def test_ignore_password(self):
        self.grant(self.alice, [*EVERYTHING, "ignore_password"])
        self.assertEqual(self.names("/locked"), ["deep", "in.txt"])

    def test_rules_on_the_files_of_an_open_folder(self):
        # `/dir/**` and `**/*.pdf` cover files, not their folder: the folder lists without a password,
        # its files come locked and without a link until it is entered (the review's H1).
        self.files({"dir/a.txt": b"a", "dir/sub/b.txt": b"b", "docs/paper.pdf": b"%PDF"})
        self.lock("/dir/**", "pw2", position=1)
        self.lock("**/*.pdf", "pw3", position=2)
        items = {i["name"]: i for i in self.listing("/dir")["items"]}
        self.assertEqual((items["a.txt"]["locked"], items["a.txt"]["url"]), (True, None))
        self.assertTrue(items["sub"]["locked"])
        pdf = next(i for i in self.listing("/docs")["items"] if i["name"] == "paper.pdf")
        self.assertEqual((pdf["locked"], pdf["url"]), (True, None))
        self.assertIsNotNone(next(i for i in self.listing("/docs")["items"] if i["name"] == "a.txt")["url"])
        self.assertEqual(self.code(self.get("item", path="/dir/a.txt")), "password_required")
        self.assertEqual(self.unlock("/dir/a.txt", "pw2").status_code, 200)
        items = {i["name"]: i for i in self.listing("/dir")["items"]}
        self.assertFalse(items["a.txt"]["locked"])
        self.assertTrue(items["a.txt"]["url"].startswith("/pd/disk/dir/a.txt?"))
        # A password sent with a request opens what that request's own check needs: the file's item.
        response = self.client.get("/api/v1/files/disk/item/", {"path": "/docs/paper.pdf"}, HTTP_X_FOLDER_PASSWORD="pw3")
        self.assertEqual(response.status_code, 200)
        self.assertIsNotNone(response.json()["url"])
        self.assertTrue(next(i for i in self.listing("/docs")["items"] if i["name"] == "paper.pdf")["locked"])


class OperationTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.login()
        self.changed = []
        handler = lambda sender, source, paths, **kwargs: self.changed.append((source.key, paths))  # noqa: E731
        signals.changed.connect(handler, dispatch_uid="test-files-changed", weak=False)
        self.addCleanup(signals.changed.disconnect, dispatch_uid="test-files-changed")

    def test_mkdir(self):
        response = self.post("mkdir", {"path": "/docs", "name": "新建 文件夹"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.json(), {"name": "新建 文件夹", "path": "/docs/新建 文件夹", "type": "folder"})
        self.assertTrue((self.root / "docs/新建 文件夹").is_dir())
        self.assertEqual(self.changed, [("disk", ["/docs/新建 文件夹"])])

    def test_mkdir_names(self):
        for name, code in (("a/b", "invalid_name"), ("", "invalid_name"), ("  ", "invalid_name"), (".", "invalid_name"),
                           ("..", "invalid_name"), ("a\\b", "invalid_name"), ("x\ny", "invalid_name"), ("é" * 200, "invalid_name")):
            with self.subTest(name=name):
                self.assertEqual(self.code(self.post("mkdir", {"path": "/", "name": name})), code)
        self.assertEqual(self.post("mkdir", {"path": "/docs/missing", "name": "x"}).status_code, 404)
        self.assertEqual(self.code(self.post("mkdir", {"path": "/top.txt", "name": "x"})), "not_a_folder")
        self.assertEqual(self.changed, [])

    def test_d17_mkdir_and_rename(self):
        response = self.post("mkdir", {"path": "/docs", "name": "sub"})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "目标已存在"))
        response = self.post("mkdir", {"path": "/docs", "name": "a.txt"})
        self.assertEqual(self.code(response), "exists")
        response = self.post("rename", {"path": "/docs", "name": "a.txt", "new_name": "b.txt"})
        self.assertEqual((response.status_code, self.code(response)), (409, "exists"))
        self.assertEqual((self.root / "docs/b.txt").read_bytes(), b"world!")
        response = self.post("rename", {"path": "/docs", "name": "a.txt", "new_name": "sub"})
        self.assertEqual(self.code(response), "exists")
        self.assertEqual(self.changed, [])

    def test_rename(self):
        response = self.post("rename", {"path": "/docs", "name": "a.txt", "new_name": "甲.txt"})
        self.assertEqual(response.json(), {"name": "甲.txt", "path": "/docs/甲.txt", "type": "file"})
        self.assertEqual((self.root / "docs/甲.txt").read_bytes(), b"hello")
        self.assertEqual(self.changed, [("disk", ["/docs/a.txt", "/docs/甲.txt"])])
        # The same name: nothing happens.
        response = self.post("rename", {"path": "/docs", "name": "sub", "new_name": "sub"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.changed), 1)
        self.assertEqual(self.post("rename", {"path": "/docs", "name": "gone", "new_name": "x"}).status_code, 404)

    def test_move_and_copy_with_results_per_item(self):
        self.files({"dest/a.txt": b"old"})
        response = self.post("move", {"path": "/docs", "names": ["a.txt", "b.txt", "sub", "gone"], "target": "/dest"})
        self.assertEqual(response.status_code, 200)
        results = {r["name"]: r for r in response.json()["results"]}
        self.assertEqual((results["a.txt"]["ok"], results["a.txt"]["code"], results["a.txt"]["type"]), (False, "exists", "file"))
        self.assertEqual((results["b.txt"]["ok"], results["b.txt"]["type"]), (True, "file"))
        self.assertEqual((results["sub"]["ok"], results["sub"]["type"]), (True, "folder"))
        self.assertEqual((results["gone"]["ok"], results["gone"]["code"], results["gone"]["type"]), (False, "not_found", None))
        self.assertEqual((response.json()["succeeded"], response.json()["failed"]), (2, 2))
        self.assertEqual((self.root / "dest/a.txt").read_bytes(), b"old")  # D17: not replaced
        self.assertTrue((self.root / "dest/sub/c.txt").exists())
        self.assertEqual(self.changed, [("disk", ["/docs/b.txt", "/dest/b.txt", "/docs/sub", "/dest/sub"])])
        response = self.post("copy", {"path": "/dest", "names": ["sub", "a.txt"], "target": "/docs"})
        self.assertEqual(response.json()["succeeded"], 1)  # a.txt is still in /docs
        self.assertTrue((self.root / "dest/sub/c.txt").exists())
        self.assertTrue((self.root / "docs/sub/c.txt").exists())

    def test_the_same_folder(self):
        response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": "/docs"})
        self.assertEqual(response.json()["results"][0], {"name": "a.txt", "type": "file", "ok": True, "code": "same_folder",
                                                         "message": "已经在这个文件夹里了。"})
        response = self.post("copy", {"path": "/docs", "names": ["a.txt"], "target": "/docs"})
        self.assertEqual(response.json()["results"][0]["code"], "exists")
        self.assertEqual(self.changed, [])

    def test_into_itself_by_segments(self):
        self.files({"ab/x.txt": b"x", "a/y.txt": b"y"})
        response = self.post("move", {"path": "/", "names": ["a"], "target": "/a"})
        self.assertEqual(response.json()["results"][0]["code"], "into_itself")
        self.post("mkdir", {"path": "/a", "name": "inner"})
        response = self.post("copy", {"path": "/", "names": ["a"], "target": "/a/inner"})
        self.assertEqual(response.json()["results"][0]["code"], "into_itself")
        # /a into /ab is fine: a prefix of the name isn't a folder above.
        response = self.post("move", {"path": "/", "names": ["a"], "target": "/ab"})
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertTrue((self.root / "ab/a/y.txt").exists())

    def test_a_missing_target(self):
        response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": "/nowhere"})
        self.assertEqual(response.status_code, 404)
        response = self.post("copy", {"path": "/docs", "names": ["a.txt"], "target": "/top.txt"})
        self.assertEqual(self.code(response), "not_a_folder")

    def test_delete(self):
        response = self.post("delete", {"path": "/docs", "names": ["a.txt", "sub", "gone"]})
        results = {r["name"]: r for r in response.json()["results"]}
        self.assertTrue(results["a.txt"]["ok"] and results["sub"]["ok"])
        self.assertEqual(results["gone"]["code"], "not_found")
        self.assertEqual(sorted(os.listdir(self.root / "docs")), ["b.txt"])
        self.assertEqual(self.changed, [("disk", ["/docs/a.txt", "/docs/sub"])])

    def test_delete_needs_names_never_the_root(self):
        for body in ({"path": "/", "names": []}, {"path": "/", "names": [""]}, {"path": "/docs", "names": ["."]},
                     {"path": "/", "names": ["a/b"]}, {"path": "/", "names": "top.txt"}):
            with self.subTest(body=body):
                self.assertEqual(self.post("delete", body).status_code, 400)
        self.assertTrue((self.root / "docs").is_dir())

    def test_the_writes_need_their_path_and_target(self):
        """API-01: a `path` or `target` left out or misspelt is refused, never taken as the root."""
        self.files({"a.txt": b"root a"})
        for what, body, field in (
            ("delete", {"pth": "/docs", "names": ["a.txt"]}, "path"),
            ("delete", {"names": ["a.txt"]}, "path"),
            ("move", {"path": "/docs", "names": ["a.txt"], "to": "/"}, "target"),
            ("copy", {"path": "/docs", "names": ["a.txt"]}, "target"),
            ("rename", {"name": "a.txt", "new_name": "x.txt"}, "path"),
            ("mkdir", {"name": "new"}, "path"),
        ):
            with self.subTest(what=what, body=body):
                response = self.post(what, body)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json())
        # A form without it, too.
        response = self.client.post(f"{API}disk/delete/", {"names": ["a.txt"]}, format="multipart")
        self.assertEqual((response.status_code, list(response.json())), (400, ["path"]))
        self.assertEqual((self.root / "a.txt").read_bytes(), b"root a")
        self.assertEqual((self.root / "docs/a.txt").read_bytes(), b"hello")
        self.assertFalse((self.root / "new").exists())
        self.assertEqual(self.changed, [])

    def test_names_are_text(self):
        """API-07: a number is refused, not written Python's way ({"name": 1e2} made 「100.0」, and
        names: [1.10] deleted 「1.1」)."""
        self.files({"1.1": b"one point one"})
        self.assertEqual(self.post("mkdir", {"path": "/", "name": 1e2}).json(), {"name": ["应该是文字（JSON 里的字符串）。"]})
        response = self.post("delete", {"path": "/", "names": [1.10]})
        self.assertEqual((response.status_code, list(response.json())), (400, ["names"]))
        self.assertEqual((self.root / "1.1").read_bytes(), b"one point one")
        self.assertFalse((self.root / "100.0").exists())

    def test_the_root_by_name(self):
        """"" and "/" are the root for the writes; list/ shows it without a path."""
        self.assertEqual(self.post("mkdir", {"path": "", "name": "n1"}).status_code, 201)
        self.assertEqual(self.post("mkdir", {"path": "/", "name": "n2"}).status_code, 201)
        response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": ""})
        self.assertEqual(response.json()["succeeded"], 1)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"hello")
        self.assertEqual(sorted(item["name"] for item in self.get("list").json()["items"]),
                         ["a.txt", "docs", "n1", "n2", "top.txt"])

    def test_each_permission(self):
        self.grant(self.alice, ["preview", "download"])
        for what, body in (("mkdir", {"path": "/", "name": "n"}), ("rename", {"path": "/", "name": "top.txt", "new_name": "x"}),
                           ("move", {"path": "/", "names": ["top.txt"], "target": "/docs"}),
                           ("copy", {"path": "/", "names": ["top.txt"], "target": "/docs"}),
                           ("delete", {"path": "/", "names": ["top.txt"]})):
            with self.subTest(what=what):
                response = self.post(what, body)
                self.assertEqual((response.status_code, self.code(response)), (403, "permission_denied"))
        self.assertTrue((self.root / "top.txt").exists())

    def test_csrf_is_checked_for_visitors_too(self):
        self.client.logout()
        self.grant(self.guest, EVERYTHING)
        self.client.credentials()
        response = self.client.post("/api/v1/files/disk/delete/", {"path": "/", "names": ["top.txt"]}, format="json")
        self.assertEqual((response.status_code, self.code(response)), (403, "csrf_failed"))
        self.assertTrue((self.root / "top.txt").exists())

    def test_a_receiver_that_fails_doesnt_fail_the_operation(self):
        def broken(**kwargs):
            raise RuntimeError("boom")

        signals.changed.connect(broken, dispatch_uid="test-broken", weak=False)
        self.addCleanup(signals.changed.disconnect, dispatch_uid="test-broken")
        with self.assertLogs("django.dispatch", "ERROR"):
            self.assertEqual(self.post("mkdir", {"path": "/", "name": "n"}).status_code, 201)


class CapabilityTests(FilesTestCase):
    storage_type = "test-nocopy"

    def setUp(self):
        super().setUp()
        self.login()

    def test_files_are_copied_through_the_site(self):
        self.files({"big.bin": os.urandom(3 * 1024 * 1024 + 7)})
        response = self.post("copy", {"path": "/", "names": ["big.bin", "top.txt"], "target": "/docs"})
        self.assertEqual(response.json()["succeeded"], 2)
        self.assertEqual((self.root / "docs/big.bin").read_bytes(), (self.root / "big.bin").read_bytes())
        self.assertEqual((self.root / "docs/top.txt").read_bytes(), b"top")

    def test_folders_are_not(self):
        caps = self.listing()["capabilities"]
        self.assertEqual((caps["copy"], caps["copy_files"], caps["copy_folders"]), (False, True, False))
        response = self.post("copy", {"path": "/", "names": ["docs"], "target": "/"})
        self.assertEqual(response.json()["results"][0]["code"], "unsupported")
        self.post("mkdir", {"path": "/", "name": "dest"})
        response = self.post("copy", {"path": "/", "names": ["docs"], "target": "/dest"})
        self.assertEqual(response.json()["results"][0]["code"], "unsupported")
        self.assertFalse((self.root / "dest/docs").exists())


class NoCaseTests(FilesTestCase):
    storage_type = "test-nocase"

    def setUp(self):
        super().setUp()
        self.login()

    def test_a_rename_that_changes_only_the_case(self):
        # The new spelling finds the entry itself, under the name asked for: the folder's listing tells.
        response = self.post("rename", {"path": "/docs", "name": "a.txt", "new_name": "A.txt"})
        self.assertEqual(response.json(), {"name": "A.txt", "path": "/docs/A.txt", "type": "file"})
        self.assertEqual(sorted(path.name for path in (self.root / "docs").iterdir()), ["A.txt", "b.txt", "sub"])
        # Where both spellings are there, the other one is kept.
        self.files({"docs/B.txt": b"other"})
        if len(list((self.root / "docs").iterdir())) == 3:
            return  # the disk underneath ignores case too (Windows, macOS): that was b.txt again
        response = self.post("rename", {"path": "/docs", "name": "b.txt", "new_name": "B.txt"})
        self.assertEqual((response.status_code, self.code(response)), (409, "exists"))
        self.assertEqual((self.root / "docs/B.txt").read_bytes(), b"other")

    def test_a_move_into_its_own_folder_in_other_case(self):
        # The target's name finds the item itself: already there, as with the folder's own spelling (was 目标已存在).
        response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": "/DOCS"})
        self.assertEqual(response.json()["results"][0], {"name": "a.txt", "type": "file", "ok": True, "code": "same_folder",
                                                         "message": "已经在这个文件夹里了。"})
        self.assertEqual((self.root / "docs/a.txt").read_bytes(), b"hello")
        response = self.post("copy", {"path": "/docs", "names": ["a.txt"], "target": "/DOCS"})
        self.assertEqual(response.json()["results"][0]["code"], "exists")
        # Where both spellings are folders, the other one is another folder.
        self.files({"DOCS/a.txt": b"other"})
        if not (self.root / "DOCS").samefile(self.root / "docs"):  # unless the disk underneath ignores case too
            response = self.post("move", {"path": "/docs", "names": ["a.txt"], "target": "/DOCS"})
            self.assertEqual(response.json()["results"][0]["code"], "exists")
            self.assertEqual((self.root / "DOCS/a.txt").read_bytes(), b"other")


class TimeTests(SimpleTestCase):
    def test_times_without_a_zone_are_utc(self):
        # A time a driver gives without a zone is taken as UTC, and shown in the site's zone.
        naive = datetime.datetime(2026, 1, 2, 3, 4, 5)
        with timezone.override("Asia/Shanghai"):
            self.assertEqual(iso(naive), "2026-01-02T11:04:05+08:00")
            self.assertEqual(iso(naive.replace(tzinfo=datetime.timezone.utc)), "2026-01-02T11:04:05+08:00")
        self.assertIsNone(iso(None))

    def test_times_the_sites_zone_cant_write(self):
        utc = datetime.timezone.utc
        with timezone.override("Asia/Shanghai"):
            # STOR-02: past the year 9999 in the site's zone: unknown.
            self.assertEqual(iso(datetime.datetime(9999, 12, 31, 15, 59, 59, tzinfo=utc)), "9999-12-31T23:59:59+08:00")
            self.assertIsNone(iso(datetime.datetime(9999, 12, 31, 16, tzinfo=utc)))
            self.assertIsNone(iso(datetime.datetime.max))
            # STOR-03: Shanghai's offset before 1901, +08:05:43, isn't one browsers read: in UTC.
            self.assertEqual(iso(datetime.datetime(1800, 6, 1, 12, tzinfo=utc)), "1800-06-01T12:00:00+00:00")
            self.assertEqual(iso(datetime.datetime.min), "0001-01-01T00:00:00+00:00")
            self.assertEqual(iso(datetime.datetime(1901, 6, 1, 12, tzinfo=utc)), "1901-06-01T20:00:00+08:00")


class SortTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.files({f"n/{name}": data for name, data in (
            ("file10.txt", b"1"), ("File2.txt", b"22"), ("file1.txt", b"333"), ("b", None), ("A", None),
            ("x01.txt", b""), ("x1.txt", b""), ("报告.txt", b"4444"))})
        os.utime(self.root / "n/file10.txt", (1, 1_000_000))
        os.utime(self.root / "n/File2.txt", (1, 3_000_000))
        os.utime(self.root / "n/file1.txt", (1, 2_000_000))
        self.login()

    def test_case_insensitive_natural_order_folders_first(self):
        self.assertEqual(self.names("/n"), ["A", "b", "file1.txt", "File2.txt", "file10.txt", "x01.txt", "x1.txt", "报告.txt"])
        self.assertEqual(self.names("/n", order="desc"), ["b", "A", "报告.txt", "x1.txt", "x01.txt", "file10.txt", "File2.txt", "file1.txt"])

    def test_by_size_and_time(self):
        self.assertEqual(self.names("/n", sort="size")[2:5], ["x01.txt", "x1.txt", "file10.txt"])
        self.assertEqual(self.names("/n", sort="size", order="desc")[:3], ["b", "A", "报告.txt"])
        times = self.names("/n", sort="time")
        self.assertLess(times.index("file10.txt"), times.index("file1.txt"))
        self.assertLess(times.index("file1.txt"), times.index("File2.txt"))

    def test_the_sites_defaults(self):
        site = SiteSettings.load()
        site.default_sort_field, site.default_sort_order = "size", "desc"
        site.save()
        answer = self.listing("/n")
        self.assertEqual(answer["sort"], {"field": "size", "order": "desc"})
        self.assertEqual([i["name"] for i in answer["items"]][:3], ["b", "A", "报告.txt"])

    def test_bad_values(self):
        self.assertEqual(self.get("list", path="/", sort="owner").status_code, 400)
        self.assertEqual(self.get("list", path="/", order="up").status_code, 400)

    def test_the_key(self):
        long_digits = "a" + "9" * 5000
        self.assertLess(natural_key("a" + "1" * 10), natural_key(long_digits))
        when = timezone.now()
        entries = [Entry("b", "/b", False, None, None), Entry("a", "/a", False, 5, when), Entry("d", "/d", True)]
        self.assertEqual([e.name for e in sort_entries(entries, "size")], ["d", "b", "a"])
        self.assertEqual([e.name for e in sort_entries(entries, "time", "desc")], ["d", "a", "b"])


class ListingTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_an_item(self):
        answer = self.listing("/docs")
        a = next(i for i in answer["items"] if i["name"] == "a.txt")
        self.assertEqual(set(a), {"name", "path", "type", "size", "modified", "kind", "url"})
        self.assertEqual((a["path"], a["type"], a["size"], a["kind"]), ("/docs/a.txt", "file", 5, "text"))
        moment = datetime.datetime.fromisoformat(a["modified"])
        self.assertIsNotNone(moment.utcoffset())
        self.assertTrue(a["url"].startswith("/pd/disk/docs/a.txt?e="))
        sub = next(i for i in answer["items"] if i["name"] == "sub")
        self.assertEqual((sub["type"], sub["kind"], sub["url"]), ("folder", None, None))
        self.assertEqual(answer["storage"], {"key": "disk", "name": "存储 disk", "type": "local", "type_label": "本地存储"})
        self.assertEqual(answer["capabilities"]["copy_folders"], True)
        self.assertFalse(answer["gallery_default"])

    def test_odd_modified_times(self):
        # STOR-02: one file's time doesn't make the folder's listing a 500; STOR-03: browsers read them all.
        utc = datetime.timezone.utc
        for moment, expected in ((datetime.datetime(9999, 12, 31, 20, tzinfo=utc), None),
                                 (datetime.datetime(1800, 6, 1, 12, tzinfo=utc), "1800-06-01T12:00:00+00:00")):
            with self.subTest(moment=moment), mock.patch("apps.storages.drivers.local._mtime", return_value=moment):
                self.assertEqual({item["modified"] for item in self.listing("/docs")["items"]}, {expected})
                self.assertEqual(self.get("item", path="/docs/a.txt").json()["modified"], expected)

    def test_no_url_without_preview_or_download(self):
        self.grant(self.alice, ["rename"])
        self.assertEqual({i["url"] for i in self.listing("/docs")["items"]}, {None})
        item = self.get("item", path="/docs/a.txt").json()
        self.assertEqual((item["url"], item["inline_url"]), (None, None))
        self.grant(self.alice, ["preview"])
        self.assertTrue(all(i["url"] for i in self.listing("/docs")["items"] if i["type"] == "file"))

    def test_a_file_is_not_a_folder(self):
        self.assertEqual(self.code(self.get("list", path="/top.txt")), "not_a_folder")
        self.assertEqual(self.get("list", path="/none").status_code, 404)

    def test_the_item_endpoint(self):
        item = self.get("item", path="/docs/a.txt").json()
        self.assertEqual((item["name"], item["kind"]), ("a.txt", "text"))
        self.assertIn("&d=i&", item["inline_url"])
        self.assertTrue(item["inline_url"].startswith("/pd/disk/docs/a.txt?"))
        response = self.client.get(item["inline_url"])
        self.assertEqual(response["Content-Disposition"], 'inline; filename="a.txt"')
        folder = self.get("item", path="/docs").json()
        self.assertEqual((folder["type"], folder["inline_url"]), ("folder", None))
        self.assertEqual(self.get("item", path="/docs/none").status_code, 404)

    def test_permanent_links_and_same_origin_ones(self):
        self.storage.permanent_links = True
        self.storage.save()
        item = self.get("item", path="/docs/a.txt").json()
        self.assertNotIn("e=", item["url"].split("?")[1].split("&")[0])
        self.assertIn("?e=", item["inline_url"])  # same-origin links always expire (an hour)

    def test_a_download_domain(self):
        self.storage.download_domain = "https://dl.example.com/files"
        self.storage.save()
        url = next(i["url"] for i in self.listing("/docs")["items"] if i["name"] == "a.txt")
        self.assertEqual(url, "https://dl.example.com/files/docs/a.txt")


class ReadmeTests(FilesTestCase):
    def setUp(self):
        super().setUp()
        self.login()

    def test_a_rule_rendered_as_untrusted(self):
        self.readme_rule("/docs", "# 说明\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[a](a.txt) ![p](../top.txt)",
                         display="dialog")
        readme = self.listing("/docs")["readme"]
        self.assertEqual(readme["display"], "dialog")
        self.assertIn("<h1>说明</h1>", readme["html"])
        self.assertNotIn("<script", readme["html"])
        self.assertNotIn("onerror", readme["html"])
        self.assertIn('href="/pd/disk/docs/a.txt?', readme["html"])
        self.assertIn('src="/pd/disk/top.txt?', readme["html"])
        self.assertEqual(len(readme["hash"]), 16)
        self.assertIsNone(self.listing("/")["readme"])

    def test_a_readme_file(self):
        self.storage.readme_from_file = True
        self.storage.save()
        self.grant(self.alice, EVERYTHING, root="/docs")
        self.files({"docs/ReadMe.md": "# 你好\n[up](../../top.txt) [sub](sub/) [x](x.txt#y) [#](#top)".encode("gbk")})
        readme = self.listing("/")["readme"]
        self.assertEqual(readme["display"], "bottom")
        self.assertIn("<h1>你好</h1>", readme["html"])  # GBK read too
        self.assertNotIn("top.txt", readme["html"])  # outside his root: dropped
        self.assertIn('href="/disk/sub"', readme["html"])
        self.assertIn('href="#top"', readme["html"])

    def test_a_utf16_readme_file(self):
        # FILE-06: by its byte order mark, as the Markdown preview reads it.
        self.storage.readme_from_file = True
        self.storage.save()
        self.files({"docs/README.md": "\ufeff# 小端".encode("utf-16-le"), "docs/sub/README.md": "\ufeff# 大端".encode("utf-16-be")})
        self.assertIn("<h1>小端</h1>", self.listing("/docs")["readme"]["html"])
        self.assertIn("<h1>大端</h1>", self.listing("/docs/sub")["readme"]["html"])

    def test_a_readme_file_too_big_or_unreadable(self):
        self.storage.readme_from_file = True
        self.storage.save()
        self.files({"docs/README.md": b"# x\n" + b"a" * 600 * 1024})
        self.assertIsNone(self.listing("/docs")["readme"])
        self.files({"docs/README.md": b"# small"})
        self.assertIsNotNone(self.listing("/docs")["readme"])
        self.grant(self.alice, ["rename"])  # neither preview nor download: he can't read it
        self.assertIsNone(self.listing("/docs")["readme"])

    def test_rendered_once_per_text_and_visitor(self):
        self.storage.readme_from_file = True
        self.storage.save()
        self.files({"docs/README.md": b"# one\n![p](a.txt)"})
        from apps.core import markdown

        with mock.patch("apps.files.readme.markdown.render", wraps=markdown.render) as render:
            first = self.listing("/docs")["readme"]
            self.assertEqual(self.listing("/docs")["readme"], first)
            self.assertEqual(render.call_count, 1)
            self.grant(self.alice, [*EVERYTHING, "ignore_hidden"])  # another visitor state: rendered again
            self.listing("/docs")
            self.assertEqual(render.call_count, 2)
            self.files({"docs/README.md": b"# two, longer"})  # another text
            self.assertIn("two", self.listing("/docs")["readme"]["html"])
            self.assertEqual(render.call_count, 3)

    def test_links_are_capped(self):
        from apps.files import readme

        self.readme_rule("/docs", " ".join(f"[{i}](a.txt?{i})" for i in range(30)))
        with mock.patch.object(readme, "LINKS_MAX", 10):
            html = self.listing("/docs")["readme"]["html"]
        self.assertEqual(html.count('href="/pd/'), 10)

    def test_show_document_off(self):
        self.readme_rule("/docs", "# x")
        site = SiteSettings.load()
        site.show_document = False
        site.save()
        self.assertIsNone(self.listing("/docs")["readme"])
