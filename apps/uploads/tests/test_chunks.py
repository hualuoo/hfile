"""The chunks of a proxied session (PUT /api/v1/uploads/<id>/): order, overlap, resending, the last
chunk's length, finishing into the driver, failures kept for a retry, cancelling, the owner."""

import io
import os
import time
from datetime import timedelta
from unittest import mock

from django.test import Client
from django.utils import timezone

from apps.storages import instances
from apps.storages.drivers.base import BackendError
from apps.uploads import service, spool
from apps.uploads.models import State, UploadSession

from .base import API, EVERYTHING, NO_DELETE, UploadTestCase

DATA = bytes(range(256)) * 13 + b"tail"  # 3332 bytes: four chunks of 1000 bytes


class ChunkTests(UploadTestCase):
    def session(self, name="data.bin", data=DATA, folder="/", **more):
        made = self.create(name, len(data), folder, **more)
        self.assertEqual(made.status_code, 201, made.content)
        return made.json()

    def test_a_file_in_four_chunks(self):
        session = self.session()
        answers = []
        for start in range(0, len(DATA), 1000):
            answers.append(self.put(session, DATA[start:start + 1000], start, len(DATA)))
        self.assertEqual([answer.status_code for answer in answers], [202, 202, 202, 201])
        self.assertEqual([answer.json()["received"] for answer in answers], [1000, 2000, 3000, 3332])
        last = answers[-1].json()
        self.assertEqual(last["state"], "done")
        self.assertEqual((last["item"]["name"], last["item"]["path"], last["item"]["size"]), ("data.bin", "/data.bin", 3332))
        self.assertIsNotNone(last["item"]["modified"])
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)
        self.assertFalse(spool.path_of(session["id"]).exists())
        self.assertEqual(self.signals, [("disk", ["/data.bin"])])
        # The panel may still ask, for an hour.
        again = self.client.get(session["url"]).json()
        self.assertEqual((again["state"], again["item"]["size"]), ("done", 3332))
        expires = UploadSession.objects.get(pk=session["id"]).expires_at
        self.assertLess(expires, timezone.now() + service.DONE_KEPT + timedelta(seconds=5))

    def test_new_folders_are_made_by_the_last_chunk(self):
        session = self.session(folder="/", relative_path="a/b")
        self.assertFalse((self.root / "a").exists())
        for start in range(0, len(DATA), 1000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        self.assertEqual((self.root / "a" / "b" / "data.bin").read_bytes(), DATA)
        self.assertEqual(self.signals, [("disk", ["/a", "/a/b", "/a/b/data.bin"])])
        self.assertEqual(UploadSession.objects.get(pk=session["id"]).item["path"], "/a/b/data.bin")

    def test_chunks_out_of_order(self):
        session = self.session()
        body = self.assertCode(self.put(session, DATA[1000:2000], 1000, len(DATA)), 409, "offset")
        self.assertEqual(body["expected"], 0)
        self.assertEqual(self.put(session, DATA[:1000], 0, len(DATA)).status_code, 202)
        # Overlapping: the next chunk must start at 1000.
        body = self.assertCode(self.put(session, DATA[500:1500], 500, len(DATA)), 409, "offset")
        self.assertEqual(body["expected"], 1000)
        # The same chunk again (its answer got lost): where to go on.
        body = self.assertCode(self.put(session, DATA[:1000], 0, len(DATA)), 409, "offset")
        self.assertEqual(body["expected"], 1000)
        self.assertEqual(os.path.getsize(spool.path_of(session["id"])), 1000)

    def test_how_much_arrived(self):
        session = self.session()
        self.put(session, DATA[:1000], 0, len(DATA))
        body = self.client.get(session["url"]).json()
        self.assertEqual((body["state"], body["received"], body["size"], body["chunk_size"]), ("receiving", 1000, 3332, 1000))
        self.assertIsNone(body["error"])
        self.assertNotIn("target", body)

    def test_ranges_and_lengths(self):
        session = self.session()
        url = session["url"]
        for header in ["", "bytes 0-999", "bytes=0-999/3332", "bytes 0-999/*", "items 0-999/3332", "bytes -1-999/3332"]:
            with self.subTest(header=header):
                answer = self.client.put(url, data=DATA[:1000], content_type="application/octet-stream", HTTP_CONTENT_RANGE=header)
                self.assertCode(answer, 400, "bad_range")
        self.assertCode(self.put(session, DATA[:1000], 0, 9999), 400, "bad_range")  # another total
        too_long = self.client.put(url, data=DATA[:1001], content_type="application/octet-stream",
                                   HTTP_CONTENT_RANGE="bytes 0-1000/3332")
        self.assertCode(too_long, 413, "chunk_too_large")
        past_end = self.client.put(url, data=b"x", content_type="application/octet-stream", HTTP_CONTENT_RANGE="bytes 3332-3332/3332")
        self.assertCode(past_end, 400, "bad_range")
        # The body must be as long as the range says, the last chunk too.
        short = self.client.put(url, data=DATA[:999], content_type="application/octet-stream", HTTP_CONTENT_RANGE="bytes 0-999/3332")
        self.assertCode(short, 400, "bad_length")
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        last = self.client.put(url, data=DATA[3000:3300], content_type="application/octet-stream",
                               HTTP_CONTENT_RANGE="bytes 3000-3331/3332")
        self.assertCode(last, 400, "bad_length")
        self.assertEqual(self.put(session, DATA[3000:], 3000, len(DATA)).status_code, 201)

    def test_a_body_that_ends_early_is_sent_again(self):
        session = UploadSession.objects.get(pk=self.session()["id"])
        request = self.client.get("/").wsgi_request
        request.user = self.alice
        with self.assertRaises(service.UploadError) as caught:
            service.receive(request, session, content_range="bytes 0-999/3332", content_length=1000,
                            body=io.BytesIO(DATA[:600]))
        self.assertEqual(caught.exception.code, "short_body")
        session.refresh_from_db()
        self.assertEqual((session.received, session.locked_until), (0, None))
        self.assertEqual(os.path.getsize(spool.path_of(session.id)), 0)
        self.assertEqual(self.put(str(session.id), DATA[:1000], 0, len(DATA)).status_code, 202)

    def test_a_failed_write_is_kept_for_a_retry(self):
        session = self.session()
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        driver = instances.get(self.storage)
        with mock.patch.object(driver, "write", side_effect=BackendError("存储暂时连不上。")):
            failed = self.put(session, DATA[3000:], 3000, len(DATA))
        body = self.assertCode(failed, 502, "storage_error")
        # What the storage said is for the log and the admins; alice gets the file API's words (SEC-01).
        self.assertEqual(body["detail"], "连接存储失败，请稍后再试。")
        state = self.client.get(session["url"]).json()
        self.assertEqual((state["state"], state["received"], state["error"]["code"]), ("failed", 3332, "storage_error"))
        self.assertEqual(state["error"]["detail"], "连接存储失败，请稍后再试。")
        self.assertTrue(spool.path_of(session["id"]).exists())
        self.assertFalse((self.root / "data.bin").exists())
        self.assertEqual(self.signals, [])
        done = self.client.post(session["complete_url"], {}, content_type="application/json")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual(done.json()["state"], "done")
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)
        self.assertFalse(spool.path_of(session["id"]).exists())

    def test_a_backend_that_cannot_say_after_the_write(self):
        """The file is written: a stat that fails afterwards doesn't turn that into a failure."""
        (self.root / "data.bin").write_bytes(b"old")
        session = self.session(overwrite=True)
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        driver = instances.get(self.storage)
        real_stat = driver.stat

        def stat(path):
            if path == "/data.bin":
                raise BackendError("稍后再试。")
            return real_stat(path)

        with mock.patch.object(driver, "stat", side_effect=stat):
            done = self.put(session, DATA[3000:], 3000, len(DATA))
        self.assertEqual(done.status_code, 201, done.content)
        self.assertEqual((done.json()["item"]["size"], done.json()["item"]["modified"]), (3332, None))
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)

    def test_a_file_that_turned_up_meanwhile(self):
        """D17 at the end too: a file put there while the chunks came isn't replaced silently."""
        session = self.session()
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        (self.root / "data.bin").write_bytes(b"theirs")
        body = self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA)), 409, "exists")
        self.assertTrue(body["can_overwrite"])
        self.assertEqual((self.root / "data.bin").read_bytes(), b"theirs")
        self.assertEqual(self.client.get(session["url"]).json()["error"]["code"], "exists")
        # Again without overwrite: still refused; with it (and 删除), the file is replaced.
        self.assertCode(self.client.post(session["complete_url"], {}, content_type="application/json"), 409, "exists")
        self.grant(self.alice, NO_DELETE)
        self.assertCode(self.client.post(session["complete_url"], {"overwrite": True}, content_type="application/json"),
                        403, "permission_denied")
        self.grant(self.alice, EVERYTHING)
        done = self.client.post(session["complete_url"], {"overwrite": True}, content_type="application/json")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)

    def test_overwrite(self):
        (self.root / "data.bin").write_bytes(b"old")
        self.assertEqual(self.upload("data.bin", DATA, overwrite=True).status_code, 201)
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)

    def test_a_grant_revoked_halfway(self):
        session = self.session()
        self.assertEqual(self.put(session, DATA[:1000], 0, len(DATA)).status_code, 202)
        self.grant(self.alice, [code for code in EVERYTHING if code != "upload"])
        self.assertCode(self.put(session, DATA[1000:2000], 1000, len(DATA)), 403, "permission_denied")
        self.grant(self.alice, EVERYTHING, enabled=False)
        self.assertCode(self.put(session, DATA[1000:2000], 1000, len(DATA)), 403, "no_grant")
        self.grant(self.alice, EVERYTHING)
        self.assertEqual(self.put(session, DATA[1000:2000], 1000, len(DATA)).status_code, 202)
        self.storage.enabled = False
        self.storage.save()
        self.assertCode(self.put(session, DATA[2000:3000], 2000, len(DATA)), 403, "storage_disabled")

    def test_new_folder_revoked_halfway(self):
        session = self.session(relative_path="new")
        self.grant(self.alice, [code for code in EVERYTHING if code != "new_folder"])
        self.assertCode(self.put(session, DATA[:1000], 0, len(DATA)), 403, "permission_denied")

    def test_delete_revoked_before_an_overwrite_lands(self):
        (self.root / "data.bin").write_bytes(b"old")
        session = self.session(overwrite=True)
        self.grant(self.alice, NO_DELETE)
        self.assertCode(self.put(session, DATA[:1000], 0, len(DATA)), 403, "permission_denied")
        self.assertEqual((self.root / "data.bin").read_bytes(), b"old")

    def test_a_session_busy_with_another_request(self):
        session = self.session()
        UploadSession.objects.filter(pk=session["id"]).update(locked_until=timezone.now() + timedelta(minutes=1))
        body = self.assertCode(self.put(session, DATA[:1000], 0, len(DATA)), 409, "busy")
        self.assertTrue(55 <= body["wait"] <= 60, body)  # the seconds until that lease runs out
        self.assertTrue(55 <= self.assertCode(self.client.delete(session["url"]), 409, "busy")["wait"] <= 60)
        # A lease that ran out (its process died) doesn't hold it.
        UploadSession.objects.filter(pk=session["id"]).update(locked_until=timezone.now() - timedelta(seconds=1))
        self.assertEqual(self.put(session, DATA[:1000], 0, len(DATA)).status_code, 202)

    def test_a_finish_that_was_cut_off(self):
        session = self.session()
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        spool.write_chunk(session["id"], 3000, io.BytesIO(DATA[3000:]), 332)
        UploadSession.objects.filter(pk=session["id"]).update(
            received=3332, state=State.FINISHING, locked_until=timezone.now() - timedelta(seconds=1))
        state = self.client.get(session["url"]).json()
        self.assertEqual((state["state"], state["error"]["code"]), ("failed", "interrupted"))
        done = self.client.post(session["complete_url"], {}, content_type="application/json")
        self.assertEqual(done.status_code, 200, done.content)
        self.assertEqual((self.root / "data.bin").read_bytes(), DATA)

    def test_complete_before_every_byte(self):
        session = self.session()
        self.put(session, DATA[:1000], 0, len(DATA))
        body = self.assertCode(self.client.post(session["complete_url"], {}, content_type="application/json"), 409, "offset")
        self.assertEqual(body["expected"], 1000)

    def test_a_spool_that_lost_bytes(self):
        session = self.session()
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        os.truncate(spool.path_of(session["id"]), 10)
        self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA)), 500, "spool")
        self.assertFalse((self.root / "data.bin").exists())

    def test_cancel(self):
        session = self.session()
        self.put(session, DATA[:1000], 0, len(DATA))
        self.assertEqual(self.client.delete(session["url"]).status_code, 204)
        self.assertFalse(UploadSession.objects.filter(pk=session["id"]).exists())
        self.assertFalse(spool.path_of(session["id"]).exists())
        self.assertCode(self.put(session, DATA[1000:2000], 1000, len(DATA)), 404, "not_found")
        self.assertEqual(self.client.delete(session["url"]).status_code, 404)

    def test_no_cancel_while_writing_into_the_storage(self):
        session = self.session()
        UploadSession.objects.filter(pk=session["id"]).update(
            state=State.FINISHING, locked_until=timezone.now() + timedelta(minutes=1))
        self.assertCode(self.client.delete(session["url"]), 409, "busy")

    def test_no_cancel_while_a_chunk_is_written(self):
        session = self.session()
        UploadSession.objects.filter(pk=session["id"]).update(locked_until=timezone.now() + timedelta(minutes=1))
        self.assertCode(self.client.delete(session["url"]), 409, "busy")
        self.assertTrue(spool.path_of(session["id"]).exists())

    def test_a_folder_that_went_away_meanwhile(self):
        """write() would make it again: that needs 新建文件夹, like any folder an upload makes."""
        (self.root / "a").mkdir()
        self.grant(self.alice, [code for code in EVERYTHING if code != "new_folder"])
        session = self.session(folder="/a")
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        (self.root / "a").rmdir()
        self.assertCode(self.put(session, DATA[3000:], 3000, len(DATA)), 403, "permission_denied")
        self.assertFalse((self.root / "a").exists())
        self.grant(self.alice, EVERYTHING)
        self.assertEqual(self.client.post(session["complete_url"], {}, content_type="application/json").status_code, 200)
        self.assertEqual((self.root / "a" / "data.bin").read_bytes(), DATA)

    def test_a_finish_that_lost_its_lease(self):
        """If the lease ran out while the driver wrote and another request took the session, this one
        leaves finishing the session to that one."""
        session = self.session()
        for start in (0, 1000, 2000):
            self.put(session, DATA[start:start + 1000], start, len(DATA))
        driver = instances.get(self.storage)
        real_write = driver.write

        def write_and_lose(path, data, size):
            real_write(path, data, size)
            UploadSession.objects.filter(pk=session["id"]).update(locked_until=timezone.now() + timedelta(minutes=9))

        with mock.patch.object(driver, "write", side_effect=write_and_lose):
            answer = self.put(session, DATA[3000:], 3000, len(DATA))
        self.assertEqual(answer.status_code, 202, answer.content)
        self.assertEqual(answer.json()["state"], "finishing")
        self.assertTrue(spool.path_of(session["id"]).exists())  # the other request still needs it
        self.assertEqual(self.signals, [])

    def test_a_chunk_for_a_finished_upload(self):
        session = self.session(data=b"0123")
        self.assertEqual(self.put(session, b"0123", 0).status_code, 201)
        self.assertCode(self.put(session, b"0123", 0), 409, "not_receiving")


class OwnerTests(UploadTestCase):
    """A session is its maker's: another user, and another visitor's browser, get 404."""

    def test_another_user(self):
        session = self.create("a.txt", 10).json()
        bob = self.make_user("bob")
        other = Client()
        other.force_login(bob)
        self.assertCode(other.get(session["url"]), 404, "not_found")
        self.assertCode(self.put(session, b"0123456789", 0, client=other), 404, "not_found")
        self.assertCode(other.delete(session["url"]), 404, "not_found")
        self.assertCode(other.post(session["complete_url"], {}, content_type="application/json"), 404, "not_found")
        self.assertTrue(UploadSession.objects.filter(pk=session["id"]).exists())

    def test_another_visitors_browser(self):
        self.grant(self.guest, ["upload"])
        mine, theirs = Client(), Client()
        session = self.create("a.txt", 10, client=mine).json()
        self.assertCode(theirs.get(session["url"]), 404, "not_found")
        self.assertCode(self.put(session, b"0123456789", 0, client=theirs), 404, "not_found")
        self.assertCode(theirs.delete(session["url"]), 404, "not_found")
        # Not alice either, though logged in: it is the guest's.
        self.assertCode(self.client.get(session["url"]), 404, "not_found")
        self.assertEqual(self.put(session, b"0123456789", 0, client=mine).status_code, 201)
        self.assertEqual((self.root / "a.txt").read_bytes(), b"0123456789")

    def test_a_visitor_whose_session_ended(self):
        self.grant(self.guest, ["upload"])
        mine = Client()
        session = self.create("a.txt", 10, client=mine).json()
        mine.cookies.clear()
        self.assertCode(mine.get(session["url"]), 404, "not_found")

    def test_the_guest_cannot_use_a_users_session(self):
        session = self.create("a.txt", 10).json()
        self.grant(self.guest, ["upload"])
        self.assertCode(Client().get(session["url"]), 404, "not_found")

    def make_user(self, name):
        from apps.accounts.models import User

        user = User.objects.create_user(name, password=f"pw-{name}-2026x")
        self.grant(user, EVERYTHING)
        return user

    def test_a_session_id_that_is_not_one(self):
        self.assertEqual(self.client.get(f"{API}not-a-uuid/").status_code, 404)
        self.assertCode(self.client.get(f"{API}00000000-0000-4000-8000-000000000000/"), 404, "not_found")


class LeaseTests(UploadTestCase):
    def test_a_short_lease_renewed_while_the_work_goes_on(self):
        """After a crash the session is free within LEASE; while a request works, a thread renews it."""
        self.assertLessEqual(service.LEASE, timedelta(seconds=90))
        self.assertLess(service.HEARTBEAT * 2, service.LEASE.total_seconds())  # two renewals may fail
        made = self.create("a.bin", len(DATA)).json()
        with mock.patch.object(service, "_Renewer", wraps=service._Renewer) as renewer:
            self.put(made, DATA[:1000], 0, len(DATA))  # a chunk is written under the renewer too
            self.assertEqual(renewer.call_count, 1)
            for start in (1000, 2000, 3000):
                self.put(made, DATA[start:start + 1000], start, len(DATA))
            self.assertEqual(renewer.call_count, 5)  # three chunks, and writing the file into the storage


class RenewerTests(UploadTestCase):
    def test_it_renews_until_stopped_or_lost(self):
        class FakeLease:
            session_id = "x"

            def __init__(self, answers):
                self.answers = list(answers)
                self.calls = 0

            def renew(self):
                self.calls += 1
                return self.answers.pop(0) if self.answers else True

        with mock.patch.object(service, "HEARTBEAT", 0.01):
            lease = FakeLease([True, True, False])
            with service._Renewer(lease) as renewer:
                deadline = time.monotonic() + 5
                while not renewer.lost and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertTrue(renewer.lost)
            self.assertEqual(lease.calls, 3)
            steady = FakeLease([])
            with service._Renewer(steady) as renewer:
                # Until three renewals, not for a fixed time: on a busy machine (GitHub's Intel Mac) the thread
                # can wake late and renew only twice in 0.1 s.
                deadline = time.monotonic() + 5
                while steady.calls < 3 and time.monotonic() < deadline:
                    time.sleep(0.01)
            self.assertFalse(renewer.lost)
            self.assertGreater(steady.calls, 2)
