import itertools
import threading
import time
import unittest

from apps.storages.drivers.base import BackendError
from apps.storages.drivers.pool import Pool


class Conn:
    ids = itertools.count(1)

    def __init__(self):
        self.id = next(self.ids)
        self.closed = False
        self.alive = True


class PoolTests(unittest.TestCase):
    def make(self, **kwargs):
        self.made = []
        self.lock = threading.Lock()

        def create():
            conn = Conn()
            with self.lock:
                self.made.append(conn)
            return conn

        def close(conn):
            conn.closed = True

        kwargs.setdefault("check", lambda conn: conn.alive)
        return Pool(create, close, **kwargs)

    def test_reuse_and_limit_under_threads(self):
        pool = self.make(size=3, wait=5)
        in_use = []
        peak = []
        lent_twice = []
        guard = threading.Lock()

        def work():
            for _ in range(50):
                conn = pool.acquire()
                with guard:
                    in_use.append(conn)
                    peak.append(len(in_use))
                    if len({c.id for c in in_use}) != len(in_use):
                        lent_twice.append(conn.id)
                time.sleep(0.0005)
                with guard:
                    in_use.remove(conn)
                pool.release(conn)

        threads = [threading.Thread(target=work) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(lent_twice, [])
        self.assertLessEqual(max(peak), 3)
        self.assertLessEqual(len(self.made), 3)
        pool.close()
        self.assertTrue(all(conn.closed for conn in self.made))

    def test_broken_connections_are_closed_and_replaced(self):
        pool = self.make(size=1)
        first = pool.acquire()
        pool.release(first, broken=True)
        self.assertTrue(first.closed)
        second = pool.acquire()
        self.assertIsNot(first, second)
        pool.release(second)
        self.assertIs(pool.acquire(), second)

    def test_busy(self):
        pool = self.make(size=1, wait=0.1, busy="都在忙")
        conn = pool.acquire()
        started = time.monotonic()
        with self.assertRaises(BackendError) as caught:
            pool.acquire()
        self.assertEqual(str(caught.exception), "都在忙")
        self.assertGreaterEqual(time.monotonic() - started, 0.09)
        threading.Timer(0.05, pool.release, args=(conn,)).start()
        pool._wait = 5
        self.assertIs(pool.acquire(), conn)

    def test_idle_check_and_expiry(self):
        pool = self.make(size=2, check_after=-1, max_idle=60)  # -1: checked even in the clock's same tick (Windows)
        conn = pool.acquire()
        pool.release(conn)
        conn.alive = False
        replacement = pool.acquire()
        self.assertIsNot(replacement, conn)
        self.assertTrue(conn.closed)
        pool.release(replacement)
        pool._max_idle = -1
        newest = pool.acquire()
        self.assertIsNot(newest, replacement)
        self.assertTrue(replacement.closed)

    def test_failed_create_frees_its_place(self):
        calls = []

        def create():
            calls.append(1)
            if len(calls) == 1:
                raise BackendError("down")
            return Conn()

        pool = Pool(create, lambda conn: None, size=1, wait=0.1)
        with self.assertRaises(BackendError):
            pool.acquire()
        self.assertIsInstance(pool.acquire(), Conn)

    def test_closed_pool(self):
        pool = self.make(size=1)
        conn = pool.acquire()
        pool.close()
        with self.assertRaises(BackendError):
            pool.acquire()
        pool.release(conn)
        self.assertTrue(conn.closed)


if __name__ == "__main__":
    unittest.main()
