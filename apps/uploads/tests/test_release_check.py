"""Release check: an upload stores exactly what was sent.

- Requests on one session at the same moment, in threads: two last chunks, a second finish while the
  file is written, a cancel or a complete/ during a chunk. The stored file is the data or nothing, the
  session ends done once, and no spool file is left. TransactionTestCase: the threads use connections
  of their own, which have to see each other's rows.
- The text editor's save writes bytes that the browser (WHATWG's TextDecoder) reads back as the text
  that was sent, and notices a file changed since it was opened.

The tests marked expectedFailure show a problem found by the release check; they pass once it is fixed.
The one whose comment says "accepted" is a known limit and keeps its mark.
"""

import datetime
import json
import os
import shutil
import threading
import unittest
from unittest import mock

from django.db import connections
from django.test import Client, TransactionTestCase, override_settings

from apps.accounts.models import User
from apps.core.testing import mysql_only
from apps.files import signals as file_signals
from apps.storages import instances
from apps.uploads import service, spool
from apps.uploads.models import State, UploadSession

from .base import API, EVERYTHING, UploadTestCase, test_folder

DATA = bytes(range(256)) * 13 + b"tail"  # 3332 bytes: four chunks of 1000 bytes
WAIT = 20  # seconds a test waits for another thread at most


def in_thread(target):
    """Runs target() in a thread with database connections of its own; join() gives what it returned."""
    result = {}

    def run():
        try:
            result["value"] = target()
        except BaseException as error:  # noqa: BLE001 - handed to the test
            result["error"] = error
        finally:
            connections.close_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def join():
        thread.join(WAIT)
        if thread.is_alive():
            raise AssertionError("the other request didn't finish")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    return join


class ConcurrentRequestsTests(TransactionTestCase):
    """UploadTestCase's storage, user and API helpers, without the transaction around each test."""

    chunk = 1000

    make_storage = UploadTestCase.make_storage
    grant = UploadTestCase.grant
    create = UploadTestCase.create
    put = UploadTestCase.put
    assertCode = UploadTestCase.assertCode

    def setUp(self):
        super().setUp()
        self.tmp = test_folder()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "root"
        self.root.mkdir()
        settings_patch = override_settings(UPLOAD_SPOOL_DIR=str(self.tmp / "spool"))
        settings_patch.enable()
        self.addCleanup(settings_patch.disable)
        for patcher in (mock.patch.object(service, "CHUNK_SIZE", self.chunk),
                        mock.patch.object(instances, "GRACE", 0)):
            patcher.start()
            self.addCleanup(patcher.stop)
        instances.close_all()
        self.addCleanup(instances.close_all)
        self.storage = self.make_storage("disk", self.root)
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.grant(self.alice, EVERYTHING)
        self.client.force_login(self.alice)
        self.other = Client()  # alice in another tab: a client of its own for the other thread
        self.other.force_login(self.alice)
        self.signals = []
        receiver = lambda sender, source, paths, **kw: self.signals.append((source.key, list(paths)))  # noqa: E731
        file_signals.changed.connect(receiver, weak=False, dispatch_uid="uploads-release-check")
        self.addCleanup(file_signals.changed.disconnect, dispatch_uid="uploads-release-check")

    def all_but_the_last_chunk(self, name="data.bin"):
        made = self.create(name, len(DATA))
        self.assertEqual(made.status_code, 201, made.content)
        session = made.json()
        for start in (0, 1000, 2000):
            self.assertEqual(self.put(session, DATA[start:start + 1000], start, len(DATA)).status_code, 202)
        return session

    def blocked(self, target, attribute, when):
        """Patches target.attribute so that its first call for which when(*args) is true waits until
        released: (entered, release) events."""
        entered, release = threading.Event(), threading.Event()
        real = getattr(target, attribute)

        def slow(*args, **kwargs):
            if when(*args) and not entered.is_set():
                entered.set()
                release.wait(WAIT)
            return real(*args, **kwargs)

        patcher = mock.patch.object(target, attribute, side_effect=slow)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(release.set)
        return entered, release

    def assertStoredOnce(self, session):
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)
        self.assertEqual(self.signals, [("disk", ["/data.bin"])])
        self.assertEqual(UploadSession.objects.get(pk=session["id"]).state, State.DONE)
        self.assertFalse(spool.path_of(session["id"]).exists())

    def test_two_last_chunks_at_the_same_time(self):
        session = self.all_but_the_last_chunk()
        entered, release = self.blocked(spool, "write_chunk", lambda session_id, offset, *rest: offset == 3000)
        first = in_thread(lambda: self.put(session, DATA[3000:], 3000, len(DATA)))
        self.assertTrue(entered.wait(WAIT))
        # The same chunk from the other tab while the first is being written: busy, nothing written.
        self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA), client=self.other), 409, "busy")
        release.set()
        self.assertEqual(first().status_code, 201)
        # The panel then asks again: done, and the file was written once.
        self.assertEqual(self.other.get(session["url"]).json()["state"], "done")
        self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA), client=self.other), 409, "not_receiving")
        self.assertStoredOnce(session)

    def test_a_second_finish_while_the_file_is_written(self):
        session = self.all_but_the_last_chunk()
        driver = instances.get(self.storage)
        entered, release = self.blocked(driver, "write", lambda path, *rest: path == "/data.bin")
        first = in_thread(lambda: self.put(session, DATA[3000:], 3000, len(DATA)))
        self.assertTrue(entered.wait(WAIT))
        self.assertEqual(self.other.get(session["url"]).json()["state"], "finishing")
        self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA), client=self.other), 409, "not_receiving")
        complete = self.other.post(session["complete_url"], {}, content_type="application/json")
        self.assertCode(complete, 409, "busy")
        self.assertCode(self.other.delete(session["url"]), 409, "busy")  # no cancel while it is written
        release.set()
        self.assertEqual(first().status_code, 201)
        again = self.other.post(session["complete_url"], {}, content_type="application/json")
        self.assertEqual((again.status_code, again.json()["state"]), (200, "done"))
        self.assertEqual(driver.write.call_count, 1)
        self.assertStoredOnce(session)

    def test_a_cancel_while_the_last_chunk_is_written(self):
        session = self.all_but_the_last_chunk()
        entered, release = self.blocked(spool, "write_chunk", lambda session_id, offset, *rest: offset == 3000)
        first = in_thread(lambda: self.put(session, DATA[3000:], 3000, len(DATA)))
        self.assertTrue(entered.wait(WAIT))
        self.assertCode(self.other.delete(session["url"]), 409, "busy")
        release.set()
        self.assertEqual(first().status_code, 201)
        self.assertStoredOnce(session)

    def test_a_cancel_between_the_last_chunk_and_the_write(self):
        """The cancel comes once the last chunk is in the spool, before the write into the storage takes
        the session: the cancel wins, nothing is stored, the spool file goes, and the chunk's answer is
        an error (busy), not a success."""
        session = self.all_but_the_last_chunk()
        real_finish = service.finish
        cancelled = []

        def cancel_first(*args, **kwargs):
            cancelled.append(in_thread(lambda: self.other.delete(session["url"]))())
            return real_finish(*args, **kwargs)

        with mock.patch.object(service, "finish", side_effect=cancel_first):
            answer = self.put(session, DATA[3000:], 3000, len(DATA))
        self.assertEqual(cancelled[0].status_code, 204)
        self.assertCode(answer, 409, "busy")
        self.assertFalse((self.root / "data.bin").exists())
        self.assertFalse(UploadSession.objects.filter(pk=session["id"]).exists())
        self.assertFalse(spool.path_of(session["id"]).exists())
        self.assertEqual(self.signals, [])
        self.assertEqual(sorted(os.listdir(self.root)), [])  # no temporary file either

    def test_complete_while_the_last_chunk_is_written(self):
        session = self.all_but_the_last_chunk()
        entered, release = self.blocked(spool, "write_chunk", lambda session_id, offset, *rest: offset == 3000)
        first = in_thread(lambda: self.put(session, DATA[3000:], 3000, len(DATA)))
        self.assertTrue(entered.wait(WAIT))
        early = self.other.post(session["complete_url"], {}, content_type="application/json")
        self.assertEqual(self.assertCode(early, 409, "offset")["expected"], 3000)
        release.set()
        self.assertEqual(first().status_code, 201)
        self.assertStoredOnce(session)

    # Not on SQLite: the tests' in-memory database (shared cache) answers "database table is locked" to
    # writes at the same moment instead of waiting. The site's file in WAL waits (timeout 20 s); the
    # release check sent the same race to a running server on SQLite instead (20 rounds, all as here).
    @mysql_only
    def test_many_rounds_of_the_last_chunk_from_two_tabs(self):
        """No interleaving arranged: the threads simply send at the same moment. One of them stores the
        file, the other is told busy, offset or not_receiving; never two writes, never an error 5xx."""
        for n in range(15):
            with self.subTest(round=n):
                data = os.urandom(900 + n)
                name = f"round-{n}.bin"
                made = self.create(name, len(data))
                self.assertEqual(made.status_code, 201, made.content)
                session = made.json()
                barrier = threading.Barrier(2)

                def send(client):
                    barrier.wait(WAIT)
                    return self.put(session, data, 0, len(data), client=client)

                joins = [in_thread(lambda client=client: send(client)) for client in (self.client, self.other)]
                answers = [join() for join in joins]
                codes = sorted(answer.status_code for answer in answers)
                self.assertEqual(codes, [201, 409], [answer.content for answer in answers])
                loser = next(answer for answer in answers if answer.status_code == 409)
                self.assertIn(loser.json()["code"], ("busy", "offset", "not_receiving"))
                self.assertEqual((self.root / name).read_bytes(), data)
                self.assertFalse(spool.path_of(session["id"]).exists())
        self.assertEqual(len(self.signals), 15)


class TextRoundTripTests(UploadTestCase):
    """What the editor saves reads back, in the browser, as the text it sent."""

    URL = f"{API}text/"

    def save(self, content, encoding, path="/notes.txt", **more):
        body = {"storage": "disk", "path": path, "content": content, "encoding": encoding, **more}
        return self.client.put(self.URL, json.dumps(body), content_type="application/json")

    # Bytes that browsers (WHATWG's big5 decoder, also Windows' cp950) read as these characters, which
    # Python's big5hkscs alone writes otherwise (read back as others); apps/uploads/text.py writes them as these:
    #   ／ U+FF0F: A1FE (big5hkscs: A241, read back as ∕ U+2215)
    #   ＼ U+FF3C: A240 (big5hkscs: A242, read back as ﹨ U+FE68)
    def test_big5_slash_and_backslash(self):
        text = "日期：2026／11／08　路徑：C：＼資料"
        self.assertEqual(self.save(text, "big5").status_code, 200)
        stored = (self.root / "notes.txt").read_bytes()
        self.assertEqual(stored, text.encode("cp950"))  # the bytes TextDecoder("big5") reads as `text`

    # Typed characters that Big5 has no bytes for, which Python's big5hkscs writes as a look-alike:
    # • U+2022 as A145 (read back ‧ U+2027), also ¢ £ ¥ ‾ ∼ ☉ ♁ ､. Refusing them, like other characters
    # the encoding lacks, keeps the rule: never a different text without an error.
    def test_big5_a_character_it_lacks(self):
        (self.root / "notes.txt").write_bytes(b"old")
        self.assertCode(self.save("要點 • 一", "big5"), 400, "unencodable")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"old")

    # GB18030-2005 (and WHATWG's gb18030, browsers, Windows) swapped two mappings of GB18030-2000, which
    # Python's codec still has: ḿ U+1E3F is A8BC (Python writes 8135F437, read back as U+E7C7).
    def test_gb18030_m_with_acute(self):
        self.assertEqual(self.save("ḿ", "gb18030").status_code, 200)
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"\xa8\xbc")

    def test_the_other_encodings_write_what_browsers_read(self):
        """Text of each kind, written as the bytes WHATWG's decoders read back as it (checked against the
        Encoding Standard's indexes by the release check). Shift_JIS's NEC/IBM duplicates may come out as
        the other copy: the same character in other bytes."""
        cases = [
            ("gbk", "中文，——…·", "中文，——…·".encode("gbk")),
            ("gb18030", "，—…·〇😺", "，—…·〇😺".encode("gb18030")),
            ("shift_jis", "表示①～", "表示①～".encode("cp932")),
            ("euc-kr", "한국어 ㈜", "한국어 ㈜".encode("cp949")),
            ("windows-1252", "café €", "café €".encode("cp1252")),
            ("utf-16le", "a😺", b"\xff\xfe" + "a😺".encode("utf-16-le")),
        ]
        for encoding, text, expected in cases:
            with self.subTest(encoding=encoding):
                self.assertEqual(self.save(text, encoding).status_code, 200)
                self.assertEqual((self.root / "notes.txt").read_bytes(), expected)

    def test_a_character_the_encoding_lacks_is_an_error(self):
        """WHATWG's windows-1252 reads 0x81 as U+0081, which Python's cp1252 can't write: refused with a
        message, not written as something else."""
        (self.root / "notes.txt").write_bytes(b"old")
        answer = self.save("a\x81b", "windows-1252")
        self.assertCode(answer, 400, "unencodable")
        self.assertEqual((self.root / "notes.txt").read_bytes(), b"old")

    # Accepted known limit: `modified` is to the second, so a change in the same
    # second, of the same size, is overwritten silently.
    @unittest.expectedFailure
    def test_a_file_changed_in_the_same_second_with_the_same_size(self):
        """B opened the file; A saved over it within the same second, same length. B's save must not
        replace A's text without a 409: the listing gives `modified` to the second only."""
        file = self.root / "notes.txt"
        file.write_bytes(b"aaaa")
        moment = int(datetime.datetime.now().timestamp()) - 10
        os.utime(file, (moment + 0.1, moment + 0.1))
        opened = {"modified": datetime.datetime.fromtimestamp(moment, datetime.UTC).isoformat(timespec="seconds"),
                  "size": 4}
        file.write_bytes(b"bbbb")  # A's save
        os.utime(file, (moment + 0.6, moment + 0.6))
        self.assertCode(self.save("cccc", "utf-8", expected=opened), 409, "changed")
        self.assertEqual(file.read_bytes(), b"bbbb")
