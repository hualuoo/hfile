"""apps.core.cleanup, the jobs of `hfile cleanup`, and the command: order, --only, a failing job doesn't stop
the others (and is rolled back), --dry-run rolls everything back and runs nothing on commit."""

import io
from unittest import mock

from django.core.management import CommandError, call_command
from django.db import transaction
from django.test import TestCase

from apps.accounts.models import User
from apps.core import cleanup


class CleanupTests(TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(cleanup._jobs, clear=True)
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
        cleanup.register(name, f"{name} 的标签", run)

    def test_jobs_in_the_order_registered(self):
        self.job("b", 2)
        self.job("a", 3)
        with self.captureOnCommitCallbacks(execute=True):
            results = cleanup.run()
        self.assertEqual(results, [("b", "b 的标签", 2), ("a", "a 的标签", 3)])
        self.assertEqual([name for name, _ in self.ran], ["b", "a"])
        self.assertEqual(self.committed, ["b", "a"])
        # Registering a name again replaces the job, in its place.
        cleanup.register("b", "新标签", lambda now: 7)
        self.assertEqual(cleanup.run()[0], ("b", "新标签", 7))

    def test_only(self):
        self.job("a", 1)
        self.job("b", 2)
        self.assertEqual(cleanup.run(only=["b"]), [("b", "b 的标签", 2)])
        with self.assertRaises(ValueError) as caught:
            cleanup.run(only=["b", "nope"])
        self.assertIn("nope", str(caught.exception))

    def test_a_failing_job(self):
        self.job("first", 1, rows=1)
        self.job("broken", fail=True, rows=2)
        self.job("last", 3)
        errors = []
        with self.captureOnCommitCallbacks(execute=True), self.assertLogs("hfile.cleanup", "ERROR"):
            results = cleanup.run(errors=errors)
        self.assertEqual(results, [("first", "first 的标签", 1), ("broken", "broken 的标签", None), ("last", "last 的标签", 3)])
        self.assertEqual([(name, str(error)) for name, _label, error in errors], [("broken", "broken broke")])
        # Its own transaction was rolled back, the others' weren't.
        self.assertTrue(User.objects.filter(username="first-0").exists())
        self.assertFalse(User.objects.filter(username__startswith="broken-").exists())
        self.assertEqual(self.committed, ["first", "last"])

    def test_dry_run(self):
        self.job("a", 4, rows=2)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(cleanup.run(dry_run=True), [("a", "a 的标签", 4)])
        self.assertFalse(User.objects.filter(username__startswith="a-").exists())
        self.assertEqual(self.committed, [])

    def test_a_job_needs_a_name(self):
        with self.assertRaises(ValueError):
            cleanup.register("", "x", lambda now: 0)


class CommandTests(TestCase):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(cleanup._jobs, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        call_command("cleanup", *args, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_one_line_per_job(self):
        cleanup.register("links", "过期的短链", lambda now: 5)
        cleanup.register("shares", "过期的分享", lambda now: 0)
        out, err = self.call()
        self.assertEqual(out.splitlines(), ["过期的短链：删除了 5 个", "过期的分享：删除了 0 个"])
        self.assertEqual(err, "")
        out, _ = self.call("--dry-run")
        self.assertEqual(out.splitlines(), ["过期的短链：可以删除 5 个", "过期的分享：可以删除 0 个", "这是试运行：什么都没有删除。"])
        out, _ = self.call("--list")
        self.assertEqual(out.splitlines(), ["links\t过期的短链", "shares\t过期的分享"])
        out, _ = self.call("--only", "shares")
        self.assertEqual(out.splitlines(), ["过期的分享：删除了 0 个"])

    def test_an_error_gives_a_non_zero_exit(self):
        def broken(now):
            raise RuntimeError("数据库连不上")

        cleanup.register("broken", "坏掉的一项", broken)
        cleanup.register("fine", "好的一项", lambda now: 1)
        out, err = io.StringIO(), io.StringIO()
        with self.assertRaises(CommandError) as caught, self.assertLogs("hfile.cleanup", "ERROR"):
            call_command("cleanup", stdout=out, stderr=err)
        self.assertIn("有 1 项清理出错了", str(caught.exception))
        self.assertIn("坏掉的一项：出错了：数据库连不上", err.getvalue())
        self.assertIn("好的一项：删除了 1 个", out.getvalue())

    def test_an_unknown_job(self):
        with self.assertRaises(CommandError) as caught:
            self.call("--only", "nope")
        self.assertIn("没有这个清理项：nope", str(caught.exception))

    def test_nothing_registered(self):
        out, _ = self.call()
        self.assertEqual(out.strip(), "没有清理项。")
