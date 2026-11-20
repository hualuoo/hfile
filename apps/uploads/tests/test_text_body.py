"""The text editor's save, its body and its turns (apps/uploads/views.py: TextJSONParser, TEXT_SAVES_AT_ONCE).

The editor's saves work as before; a body that isn't the save's shape is refused at once, without building
what it holds; bad JSON gets the message it got before; at most TEXT_SAVES_AT_ONCE saves are read and
written at a time, the others wait their turn and then get 503; nothing of the text is left behind for
the garbage collector, which may free it only minutes later; the text isn't gone through in Python
character by character, nor copied again to be written (FILE-04)."""

import gc
import io
import json
import threading
import tracemalloc
import types
from unittest import mock

from django.test import Client
from rest_framework.exceptions import ParseError

from apps.core.parsers import JSONParser
from apps.storages.models import FolderPassword
from apps.uploads import text, views

from .base import API, EVERYTHING, UploadTestCase

URL = f"{API}text/"
JUNK = b"[" + b",".join([b"{}"] * 90_000) + b"]"  # 270 KB: an object for every 3 bytes, parsed whole


def editor_body(**fields) -> bytes:
    """The body as the editor's save() sends it (apps/preview/static/preview/js/text.js): JSON.stringify of
    storage, path, content, encoding, newline and, when the editor knows them, expected {modified, size}."""
    return json.dumps(fields, ensure_ascii=False, separators=(",", ":")).encode()


def text_left_behind(length=100_000) -> list[str]:
    """What holds a string of `length` characters or more, among the objects made since the last full
    collection (with the collector off): what only the garbage collector would free. Frames are left out:
    the running test's own (logging makes them objects) hold the test's strings."""
    found = []
    for obj in gc.get_objects(generation=0):
        if isinstance(obj, types.FrameType):
            continue
        for ref in gc.get_referents(obj):
            items = ref.values() if isinstance(ref, dict) and not gc.is_tracked(ref) else (ref,)
            if any(isinstance(item, str) and len(item) >= length for item in items):
                found.append(type(obj).__qualname__)
    return found


class _Turns(threading.BoundedSemaphore):
    """Turns that tell when a save had to wait for one."""

    def __init__(self, value):
        super().__init__(value)
        self.waited = threading.Event()

    def acquire(self, blocking=True, timeout=None):
        if super().acquire(blocking=False):
            return True
        if not blocking:
            return False
        self.waited.set()
        return super().acquire(timeout=timeout)


class TextBodyTests(UploadTestCase):
    def setUp(self):
        super().setUp()
        self.file = self.root / "notes.txt"
        self.file.write_bytes(b"old\n")

    def put(self, raw: bytes, client=None, content_type="application/json", **extra):
        return (client or self.client).put(URL, raw, content_type=content_type, **extra)

    def detail(self, response, status=400):
        self.assertEqual(response.status_code, status, response.content)
        return response.json()["detail"]

    def taken_turns(self) -> _Turns:
        """Every turn taken, as by saves in progress (the view's turns, for this test only)."""
        turns = _Turns(views.TEXT_SAVES_AT_ONCE)
        for _ in range(views.TEXT_SAVES_AT_ONCE):
            turns.acquire(blocking=False)
        patcher = mock.patch.object(views, "_text_saves", turns)
        patcher.start()
        self.addCleanup(patcher.stop)
        return turns

    def assertTurnsFree(self):
        taken = [views._text_saves.acquire(blocking=False) for _ in range(views.TEXT_SAVES_AT_ONCE)]
        for _ in range(sum(taken)):
            views._text_saves.release()
        self.assertEqual(taken, [True] * views.TEXT_SAVES_AT_ONCE)

    # -- what the editor sends

    def test_the_fields_taken(self):
        self.assertEqual(views._TEXT_FIELDS, {"storage": None, "path": None, "content": None, "encoding": None, "newline": None,
                                              "expected": {"modified": None, "size": None}, "password": None})

    def test_the_text_isnt_checked_character_by_character(self):
        # FILE-04: CharField's checks went through the text in Python (0.3 s of CPU for 5 MB). The parser refuses lone
        # surrogates already (test_bad_json_gets_the_message_it_had); save_text looks for NUL itself (FILE-03).
        validators = views._TextSerializer().fields["content"].validators
        self.assertEqual([type(validator).__name__ for validator in validators], ["MaxLengthValidator"])

    def test_writing_holds_the_bytes_once(self):
        # FILE-04: UTF-8 isn't decoded again to see that it reads back, and without a BOM the bytes written are
        # the encoded text itself, not a copy.
        content = "中文" * 50_000 + "a\n" * 1000
        tracemalloc.start()
        try:
            data = text.encode(content, text.lookup("utf-8"))
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual(data, content.encode())
        self.assertLess(peak, 1.2 * len(data))

    def test_the_editors_saves(self):
        answer = self.put(editor_body(storage="disk", path="/notes.txt", content='第一行\n二\\"😀', encoding="utf-8",
                                      newline="lf"))
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(self.file.read_bytes(), '第一行\n二\\"😀'.encode())
        item = answer.json()
        opened = {"modified": item["modified"], "size": item["size"]}
        answer = self.put(editor_body(storage="disk", path="/notes.txt", content="第一行\n二", encoding="gbk",
                                      newline="crlf", expected=opened))
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual((self.file.read_bytes(), answer.json()["encoding"]), ("第一行\r\n二".encode("gbk"), "gbk"))
        # The file isn't what the editor opened any more: 409, as before.
        changed = self.put(editor_body(storage="disk", path="/notes.txt", content="x", encoding="utf-8", newline="lf",
                                       expected=opened))
        self.assertCode(changed, 409, "changed")
        # A folder password, from an API caller; expected null, the rest left to the defaults.
        (self.root / "secret").mkdir()
        FolderPassword.objects.create(storage=self.storage, expression="/secret", password="pw")
        body = {"storage": "disk", "path": "/secret/a.txt", "content": "y", "expected": None}
        self.assertCode(self.put(json.dumps(body).encode()), 403, "password_required")
        answer = self.put(json.dumps({**body, "password": "pw"}, indent=2).encode())
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual((self.root / "secret" / "a.txt").read_bytes(), b"y")

    # -- a body that isn't the save's

    def test_a_body_of_another_shape_is_refused(self):
        start = '{"storage": "disk", "path": "/notes.txt", "content": "new", '
        cases = [
            (start + '"junk": %s}', "有不认识的字段，只能有 storage、path、content、encoding、newline、expected、password。"),
            (start + '"expected": {"size": 4, "junk": %s}}', "expected 里有不认识的字段，只能有 modified、size。"),
            (start + '"content": %s}', "content 出现了不止一次。"),
            ('{"storage": "disk", "path": "/notes.txt", "content": %s}', "content 不能是数组。"),
            (start + '"expected": {"size": {"a": %s}}}', "expected.size 不能是对象。"),
            (start + '"password": {"a": %s}}', "password 不能是对象。"),
            ("%s", "要的是一个对象（{…}）。"),
        ]
        for body, message in cases:
            with self.subTest(message):
                self.assertEqual(self.detail(self.put((body % "[{}]").encode())), f"JSON 格式不对：{message}")
        # After the object, anything but spaces is bad JSON, with the message it had.
        self.assertEqual(self.detail(self.put(b'{"storage": "disk"} [{}]')),
                         "JSON 格式不对：第 1 行第 21 列开始是多余的内容，JSON 在这之前已经完了。")
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_a_form_is_told_that_only_json_is_taken(self):
        """TEXT-02: the 415 said 「要用 JSON 或者表单发送」, but this address takes JSON alone."""
        r = self.put(b"--x--\r\n", content_type="multipart/form-data; boundary=x")
        self.assertEqual(self.detail(r, 415), "请求的内容要用 JSON（application/json）发送。")
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_refused_without_building_the_body(self):
        """The check's measurement (S2-1017 H01): parsed whole, about 24 bytes of memory for every byte of
        such a body; now the decoded text and nothing more."""
        bodies = [b'{"storage": "disk", "junk": %s}', b'{"content": %s}', b'{"expected": {"size": {"a": %s}}}',
                  b'{"content": "a", "content": %s}', b'{"storage": "disk"} %s', b"%s"]
        for body in bodies:
            body = body % JUNK
            with self.subTest(body[:30]):
                tracemalloc.start()
                try:
                    with self.assertRaises(ParseError):
                        views.TextJSONParser().parse(io.BytesIO(body), "application/json", {})
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 1.5 * len(body))  # the decoded text, about 1 time the body
        # The general parser, for comparison: what it takes (up to 50 000 objects and arrays, SEC-09) it builds whole.
        body = b'{"storage": "disk", "junk": [%s]}' % b",".join([b"{}"] * 40_000)
        tracemalloc.start()
        try:
            JSONParser().parse(io.BytesIO(body), "application/json", {})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertGreater(peak, 10 * len(body))

    def test_bad_json_gets_the_message_it_had(self):
        bodies = [b'{"storage": "disk", "path": "/notes.txt", "content": "x"', b'{"storage": "disk",}', b'{"storage" "disk"}',
                  b'{"storage": NaN}', b'{"content": "\xff"}', b"\xef\xbb\xbf{}", b'{"content": "a\tb"}', b'{"broken json',
                  b'{"content": "\\ud800"}', b'{"content": "\\x"}', b"   "]
        for body in bodies:
            with self.subTest(body=body):
                with self.assertRaises(ParseError) as before:
                    JSONParser().parse(io.BytesIO(body), "application/json", {})
                self.assertEqual(self.detail(self.put(body)), str(before.exception.detail))
        # No body at all: no data, as before (DRF doesn't parse an empty body).
        self.assertIn("storage", self.put(b"").json())
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_16_mb_and_one_byte(self):
        too_large = self.client.generic("PUT", URL, b"{}", content_type="application/json",
                                        CONTENT_LENGTH=str(views.TEXT_REQUEST_MAX + 1))
        self.assertEqual(self.detail(too_large, 413), "提交的内容太大了：在线编辑最多保存 5 MB。")
        self.assertTurnsFree()

    # -- the turns

    def test_busy(self):
        self.taken_turns()
        with mock.patch.object(views, "TEXT_SAVE_WAIT", 0):
            with self.assertNoLogs("django.request", "ERROR"):  # expected, not a 5xx for the console
                busy = self.put(editor_body(storage="disk", path="/notes.txt", content="new"))
            self.assertEqual(self.detail(busy, 503), "同时在保存的文件太多了，请过一会儿再保存。")
            # The body is read in the turn: broken JSON is 503 too.
            self.assertEqual(self.put(b'{"broken json').status_code, 503)
            # What is checked before the body still is, busy or not.
            self.assertCode(self.put(b"{}", client=Client()), 401, "login_required")
            too_large = self.client.generic("PUT", URL, b"{}", content_type="application/json",
                                            CONTENT_LENGTH=str(views.TEXT_REQUEST_MAX + 1))
            self.assertEqual(too_large.status_code, 413)
            self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
            self.assertCode(self.put(b"{}"), 403, "permission_denied")
        self.assertEqual(self.file.read_bytes(), b"old\n")

    def test_a_save_waits_for_its_turn(self):
        turns = self.taken_turns()

        def one_ends():  # a save in progress ends once this one waits
            turns.waited.wait(30)
            turns.release()

        ending = threading.Thread(target=one_ends)
        ending.start()
        try:
            with mock.patch.object(views, "TEXT_SAVE_WAIT", 60):
                answer = self.put(editor_body(storage="disk", path="/notes.txt", content="waited"))
        finally:
            ending.join()
        self.assertTrue(turns.waited.is_set())
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(self.file.read_bytes(), b"waited")

    def test_every_answer_gives_the_turn_back(self):
        body = {"storage": "disk", "path": "/notes.txt", "content": "x"}
        answers = [
            self.put(json.dumps(body).encode()),
            self.put(json.dumps({**body, "junk": 1}).encode()),  # the parser
            self.put(b'{"broken json'),
            self.put(json.dumps({**body, "encoding": "e" * 41}).encode()),  # the serializer
            self.put(json.dumps({**body, "newline": "dos"}).encode()),  # the service
            self.put(json.dumps({**body, "path": "/missing/a.txt"}).encode()),
            self.put(json.dumps({**body, "expected": {"size": 99}}).encode()),
            self.put(b"{}", content_type="text/plain"),
        ]
        self.assertEqual([answer.status_code for answer in answers], [200, 400, 400, 400, 400, 404, 409, 415])
        self.assertTurnsFree()

    def test_nothing_of_the_text_is_left_behind(self):
        """The request and the serializer are reference cycles: what they hold is freed only when the garbage
        collector runs, maybe minutes later, and until then the turns wouldn't bound the memory."""
        long = "x" * 200_000
        bodies = [{"content": long}, {"content": long, "newline": "dos"}, {"content": long, "encoding": "e" * 41},
                  {"content": long, "path": "/missing/a.txt"}]
        statuses = []
        for more in bodies:
            raw = json.dumps({"storage": "disk", "path": "/notes.txt", **more}).encode()
            gc.collect()
            gc.disable()
            try:
                statuses.append(self.put(raw).status_code)
                left = text_left_behind()
            finally:
                gc.enable()
            self.assertEqual(left, [], list(more))
        self.assertEqual(statuses, [200, 400, 400, 404])
