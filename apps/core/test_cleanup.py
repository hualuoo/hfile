"""apps.core.cleanup: the jobs run in the order registered, each in a transaction of its own (a failing one is
rolled back and logged, the others still run), and stop between jobs once the program exits; the thread that
config/wsgi.py starts runs them a few minutes after the start and then every hour."""

import threading
from unittest import mock

from django.db import DatabaseError, transaction
from django.test import SimpleTestCase, TestCase, override_settings

from apps.accounts.models import User
from apps.core import cleanup


class RunTests(TestCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.dict(cleanup._jobs, clear=True), mock.patch.object(cleanup, "_stop", threading.Event())):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.ran = []
        self.committed = []

    def job(self, name, count=0, fail=False, rows=0):
        def run(now):
            self.ran.append((name, now))
            for n in range(rows):
                User.objects.create_user(f"{name}-{n}", password="pw-cleanup-2026x")
            transaction.on_commit(lambda: self.committed.append(name))
            if fail:
                raise RuntimeError(f"{name} broke")
            return count
        cleanup.register(name, run)

    def test_jobs_in_the_order_registered(self):
        self.job("b", 2)
        self.job("a", 0)
        with self.captureOnCommitCallbacks(execute=True), self.assertLogs("hfile.cleanup", "INFO") as logged:
            results = cleanup.run()
        self.assertEqual(results, [("b", 2), ("a", 0)])
        self.assertEqual([name for name, _ in self.ran], ["b", "a"])
        self.assertEqual(len({now for _, now in self.ran}), 1)  # the same moment for every job
        self.assertEqual(self.committed, ["b", "a"])
        self.assertEqual(logged.output, ["INFO:hfile.cleanup:Removed what had expired: b 2"])
        # Registering a name again replaces the job, in its place.
        cleanup.register("b", lambda now: 7)
        with self.assertLogs("hfile.cleanup", "INFO"):
            self.assertEqual(cleanup.run()[0], ("b", 7))

    def test_nothing_removed_says_nothing(self):
        self.job("a", 0)
        with self.assertNoLogs("hfile.cleanup"):
            self.assertEqual(cleanup.run(), [("a", 0)])

    def test_a_failing_job(self):
        self.job("first", 1, rows=1)
        self.job("broken", fail=True, rows=2)
        self.job("last", 3)
        with self.captureOnCommitCallbacks(execute=True), self.assertLogs("hfile.cleanup", "INFO") as logged:
            results = cleanup.run()
        self.assertEqual(results, [("first", 1), ("broken", None), ("last", 3)])
        self.assertTrue(logged.output[0].startswith("ERROR:hfile.cleanup:The cleanup job broken failed\n"))
        self.assertIn("RuntimeError: broken broke", logged.output[0])
        self.assertEqual(logged.output[1], "INFO:hfile.cleanup:Removed what had expired: first 1, last 3")
        # Its own transaction was rolled back, the others' weren't.
        self.assertTrue(User.objects.filter(username="first-0").exists())
        self.assertFalse(User.objects.filter(username__startswith="broken-").exists())
        self.assertEqual(self.committed, ["first", "last"])

    def test_stops_between_jobs_when_the_program_exits(self):
        self.job("first", 1)
        cleanup.register("exit", lambda now: cleanup._stop.set() or 0)
        self.job("last", 1)
        with self.assertLogs("hfile.cleanup", "INFO"):
            self.assertEqual(cleanup.run(), [("first", 1), ("exit", 0)])
        self.assertEqual([name for name, _ in self.ran], ["first"])

    def test_a_job_needs_a_name(self):
        with self.assertRaises(ValueError):
            cleanup.register("", lambda now: 0)


class ThreadTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        for patcher in (mock.patch.object(cleanup, "_thread", None), mock.patch.object(cleanup, "_stop", threading.Event())):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_started_once_per_process(self):
        looped = threading.Event()
        with mock.patch.object(cleanup, "_loop", looped.set), mock.patch.object(cleanup.atexit, "register") as at_exit:
            cleanup.start()
            thread = cleanup._thread
            thread.join(5)
            cleanup.start()
        self.assertTrue(looped.is_set())
        self.assertEqual((thread.name, thread.daemon), ("hfile-cleanup", True))
        self.assertIs(cleanup._thread, thread)
        at_exit.assert_called_once_with(cleanup._stop_at_exit)

    @override_settings(SETUP_MODE=True)
    def test_not_before_the_site_is_installed(self):
        with mock.patch.object(cleanup, "_loop", lambda: None), mock.patch.object(cleanup.atexit, "register") as at_exit:
            cleanup.start()
        self.assertIsNone(cleanup._thread)
        at_exit.assert_not_called()

    def test_the_loop(self):
        """A few minutes after the start, then every hour; the thread's database connections are closed after each
        run, and one that can't be closed doesn't stop it."""
        waits = []
        stop = mock.Mock(spec=threading.Event)
        stop.wait.side_effect = lambda seconds: waits.append(seconds) or len(waits) > 3
        with (mock.patch.object(cleanup, "_stop", stop), mock.patch.object(cleanup, "run") as run,
              mock.patch.object(cleanup.random, "uniform", return_value=42.0) as uniform,
              mock.patch.object(cleanup, "connections") as connections):
            connections.close_all.side_effect = [None, DatabaseError("gone"), None]
            cleanup._loop()
        uniform.assert_called_once_with(0, cleanup.SPREAD)
        self.assertEqual(waits, [cleanup.FIRST_AFTER + 42.0, cleanup.EVERY, cleanup.EVERY, cleanup.EVERY])
        self.assertEqual(run.call_count, 3)
        self.assertEqual(connections.close_all.call_count, 3)

    def test_stopping_at_exit(self):
        thread = threading.Thread(target=cleanup._stop.wait, daemon=True)  # a thread waiting for its next run
        thread.start()
        with mock.patch.object(cleanup, "_thread", thread):
            cleanup._stop_at_exit()
        self.assertTrue(cleanup._stop.is_set())
        self.assertFalse(thread.is_alive())
