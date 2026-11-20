"""The text editor's save (PUT /api/v1/uploads/text/): 上传 and 删除 (D17a), folder passwords, the
encodings, the line breaks, `expected` against a file changed meanwhile, the size cap."""

import datetime
import json
import os

from django.test import Client

from apps.core.testing import TO_THE_SECOND
from apps.storages.models import FolderPassword
from apps.uploads import service

from .base import API, EVERYTHING, NO_DELETE, UploadTestCase

URL = f"{API}text/"


class TextTests(UploadTestCase):
    def setUp(self):
        super().setUp()
        self.file = self.root / "notes.txt"
        self.file.write_bytes(b"old\n")

    def save(self, content="新的内容\n", path="/notes.txt", client=None, **more):
        body = {"storage": "disk", "path": path, "content": content, **more}
        return (client or self.client).put(URL, json.dumps(body), content_type="application/json")

    def opened(self):
        info = os.stat(self.file)
        moment = datetime.datetime.fromtimestamp(info.st_mtime, datetime.timezone.utc)
        return {"modified": moment.isoformat(), "size": info.st_size}

    def test_saving_over_the_file(self):
        answer = self.save()
        self.assertEqual(answer.status_code, 200, answer.content)
        body = answer.json()
        self.assertEqual((body["name"], body["path"], body["encoding"]), ("notes.txt", "/notes.txt", "utf-8"))
        self.assertEqual(body["size"], len("新的内容\n".encode()))
        self.assertRegex(body["modified"], TO_THE_SECOND)  # TEXT-01: it was UTC with microseconds
        self.assertEqual(self.file.read_bytes(), "新的内容\n".encode())
        self.assertEqual(self.signals, [("disk", ["/notes.txt"])])

    def test_it_needs_upload_and_delete(self):
        self.grant(self.alice, NO_DELETE)
        body = self.assertCode(self.save(), 403, "permission_denied")
        self.assertIn("删除", body["detail"])
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
        self.assertCode(self.save(), 403, "permission_denied")
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_a_new_file_needs_only_upload(self):
        self.grant(self.alice, NO_DELETE)
        answer = self.save("hello", path="/new.txt")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual((self.root / "new.txt").read_bytes(), b"hello")
        self.assertCode(self.save("x", path="/missing/new.txt"), 404, "not_found")
        self.assertCode(self.save("x", path="/bad\tname.txt"), 400, "invalid_name")

    def test_expected(self):
        seen = self.opened()
        self.assertEqual(self.save("one", expected=seen).status_code, 200)
        # The file changed since: the old expectation no longer holds.
        body = self.assertCode(self.save("two", expected=seen), 409, "changed")
        self.assertEqual(body["current"]["size"], 3)
        self.assertEqual(self.file.read_bytes(), b"one")
        # What the 409 says the file is now (TEXT-01: to the second, in the site's zone) may be sent back as it is.
        self.assertRegex(body["current"]["modified"], TO_THE_SECOND)
        self.assertEqual(self.save("two", expected=body["current"]).status_code, 200)
        # Either part alone, and the time to the second.
        self.assertEqual(self.save("three", expected={"size": 3}).status_code, 200)
        self.assertCode(self.save("four", expected={"size": 3}), 409, "changed")
        modified = self.opened()["modified"]
        whole_seconds = datetime.datetime.fromisoformat(modified).replace(microsecond=0).isoformat()
        self.assertEqual(self.save("five", expected={"modified": whole_seconds}).status_code, 200)
        self.assertCode(self.save("six", expected={"modified": "not a time"}), 400, "bad_expected")
        # Deleted meanwhile.
        self.file.unlink()
        self.assertCode(self.save("seven", expected={"size": 4}), 409, "changed")
        self.assertFalse(self.file.exists())
        # Without an expectation the editor may write it again.
        self.assertEqual(self.save("eight").status_code, 200)

    def test_encodings(self):
        cases = [
            ("utf-8", "中文 ok", "中文 ok".encode()),
            ("utf-8-bom", "中文", b"\xef\xbb\xbf" + "中文".encode()),
            ("gbk", "中文", "中文".encode("gbk")),
            ("GB18030", "中文😀", "中文😀".encode("gb18030")),
            ("big5", "繁體", "繁體".encode("big5hkscs")),
            ("shift_jis", "日本語", "日本語".encode("cp932")),
            ("euc-kr", "한국어", "한국어".encode("cp949")),
            ("utf-16le", "ab中", b"\xff\xfe" + "ab中".encode("utf-16-le")),
            ("utf-16be", "ab中", b"\xfe\xff" + "ab中".encode("utf-16-be")),
            ("windows-1252", "café", "café".encode("cp1252")),
        ]
        for encoding, content, expected in cases:
            with self.subTest(encoding=encoding):
                answer = self.save(content, encoding=encoding)
                self.assertEqual(answer.status_code, 200, answer.content)
                self.assertEqual(self.file.read_bytes(), expected)
        self.assertEqual(self.save("x", encoding="GB2312").json()["encoding"], "gbk")
        self.assertCode(self.save("x", encoding="ebcdic"), 400, "encoding")
        body = self.assertCode(self.save("第一行\n第二行有😀", encoding="gbk"), 400, "unencodable")
        self.assertIn("第 2 行第 5 个字符「😀」", body["detail"])

    def test_a_nul_character(self):
        # FILE-03: refused, as before, but saying what and where (it was Django's 「不允许是用空字符串。」).
        body = self.assertCode(self.save("第一行\n第二\x00行"), 400, "null_character")
        self.assertIn("第 2 行第 3 个字符是 NUL（U+0000）", body["detail"])
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_never_another_text(self):
        """What browsers would read back as another character is refused (apps/uploads/text.py): look-alikes a
        codec writes for characters the encoding lacks, and bytes the browsers' tables read otherwise."""
        cases = [("shift_jis", "価格 ¢5", 4), ("shift_jis", "〜", 1), ("big5", "價格 ¥5", 4), ("big5", "•", 1),
                 ("gb18030", "\ue78d", 1), ("shift_jis", "\uf8f0", 1)]
        for encoding, content, column in cases:
            with self.subTest(encoding=encoding, content=content):
                body = self.assertCode(self.save(content, encoding=encoding), 400, "unencodable")
                self.assertIn(f"第 1 行第 {column} 个字符", body["detail"])
        self.assertEqual(self.file.read_bytes(), b"old\n")
        # The browsers' bytes for the characters Python's big5hkscs writes elsewhere; the rest unchanged.
        self.assertEqual(self.save("a∕b﹨c／d＼", encoding="big5").status_code, 200)
        self.assertEqual(self.file.read_bytes(), b"a\xa2\x41b\xa2\x42c\xa1\xfed\xa2\x40")
        self.assertEqual(self.save("ḿ\ue7c7", encoding="gb18030").status_code, 200)
        self.assertEqual(self.file.read_bytes(), b"\xa8\xbc\x81\x35\xf4\x37")

    def test_line_breaks(self):
        self.save("a\nb\n", newline="crlf")
        self.assertEqual(self.file.read_bytes(), b"a\r\nb\r\n")
        self.save("a\r\nb\n", newline="lf")
        self.assertEqual(self.file.read_bytes(), b"a\nb\n")
        self.save("a\nb", newline="cr")
        self.assertEqual(self.file.read_bytes(), b"a\rb")
        self.assertCode(self.save("a", newline="dos"), 400, "newline")

    def test_the_size_cap(self):
        limit = service.TEXT_MAX
        self.assertEqual(self.save("x" * limit).status_code, 200)
        self.assertEqual(self.save("x" * (limit + 1)).status_code, 400)  # the serializer's max_length
        # Fewer characters than the cap, but more bytes in UTF-16.
        self.assertCode(self.save("x" * (limit // 2 + 1), encoding="utf-16le"), 413, "too_large")
        too_big = self.client.put(URL, b"{" + b" " * (33 * 1024 * 1024) + b"}", content_type="application/json")
        self.assertEqual(too_big.status_code, 413)

    def test_folder_passwords(self):
        (self.root / "secret").mkdir()
        (self.root / "secret" / "a.txt").write_text("x")
        FolderPassword.objects.create(storage=self.storage, expression="/secret", password="pw")
        self.assertCode(self.save("y", path="/secret/a.txt"), 403, "password_required")
        self.assertEqual(self.save("y", path="/secret/a.txt", password="pw").status_code, 200)

    def test_not_a_file(self):
        (self.root / "folder").mkdir()
        self.assertCode(self.save("x", path="/folder"), 409, "folder_exists")
        self.assertCode(self.save("x", path="/"), 400, "invalid_path")
        self.assertCode(self.save("x", path="/../etc/passwd"), 400, "invalid_path")

    def test_no_upload_anywhere_is_refused_before_the_body_is_read(self):
        guest = Client()
        refused = guest.put(URL, b'{"broken json', content_type="application/json")
        self.assertCode(refused, 401, "login_required")
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
        refused = self.client.put(URL, b'{"broken json', content_type="application/json")
        self.assertCode(refused, 403, "permission_denied")
        self.grant(self.alice, EVERYTHING)
        self.assertEqual(self.client.put(URL, b'{"broken json', content_type="application/json").status_code, 400)

    def test_a_raw_newline(self):
        self.save("a\nb", newline="\r\n")
        self.assertEqual(self.file.read_bytes(), b"a\r\nb")

    def test_the_guest(self):
        guest = Client()
        self.assertCode(self.save(client=guest), 401, "login_required")
        self.grant(self.guest, ["upload", "delete"])
        self.assertEqual(self.save("guest", client=guest).status_code, 200)
        self.assertEqual(self.file.read_bytes(), b"guest")
