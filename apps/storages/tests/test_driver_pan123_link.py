"""Tests of the 123 Cloud Drive direct links driver (apps.storages.drivers.pan123_link) against a fake of 123's direct
links and their URL authentication (tests/fake_pan123_link.py).

HFILE_CONFIG=... python -B manage.py test apps.storages.tests.test_driver_pan123_link
"""

from __future__ import annotations

import hashlib
import time
import unittest
import urllib.parse

from apps.storages.drivers.base import AuthError, NotFound
from apps.storages.drivers.pan123_link import Pan123LinkDriver, sign
from apps.storages.tests.contract import EMPTY_FOLDER, READ_ONLY_TREE, ReadOnlyContract
from apps.storages.tests.fake_pan123_link import FakePan123Link

UID = 1800000000
KEY = "289ds32418bxdba"  # the key of 123's documented example


def contract_tree(fake: FakePan123Link) -> str:
    """READ_ONLY_TREE as a tree: some files named and sized, some named after their link."""
    return "\n".join([
        f"a.txt:{fake.link('/a.txt')}",
        "d:",
        f"  b.bin:{len(READ_ONLY_TREE['/d/b.bin'])}:{fake.link('/d/b.bin')}",
        "  sub:",
        f"    {fake.link('/d/sub/c.txt')}",
        f"  {fake.link('/d/中文 名字.txt')}",
        "  a b:",
        f"    100%.txt:{fake.link('/d/a b/100%.txt')}",
        EMPTY_FOLDER[1:] + ":",
    ])


def auth_key(url: str) -> list[str]:
    """The parts of a link's auth_key: timestamp, rand, uid, md5."""
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["auth_key"]
    return values[0].split("-")


class SignedContractTests(ReadOnlyContract, unittest.TestCase):
    """123's URL authentication on: whatever the driver asks for, and the links it gives, are signed."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakePan123Link(READ_ONLY_TREE, uid=UID, key=KEY).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def make_driver(self):
        return Pan123LinkDriver({"tree": contract_tree(self.fake), "head_sizes": True, "uid": UID, "private_key": KEY})


class UnsignedContractTests(ReadOnlyContract, unittest.TestCase):
    """URL authentication off: the links as they are."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.fake = FakePan123Link(READ_ONLY_TREE, uid=UID).start()

    @classmethod
    def tearDownClass(cls):
        cls.fake.close()
        super().tearDownClass()

    def make_driver(self):
        return Pan123LinkDriver({"tree": contract_tree(self.fake), "head_sizes": True})


class Pan123LinkTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakePan123Link({"/x.bin": b"x" * 100, "/音乐/02 a+b.wma": b"wma"}, uid=UID, key=KEY).start()
        self.addCleanup(self.fake.close)
        self.tree = f"{self.fake.link('/x.bin')}\nmusic:\n  {self.fake.link('/音乐/02 a+b.wma')}"

    def driver(self, tree=None, **config):
        d = Pan123LinkDriver({"tree": tree or self.tree, **config})
        self.addCleanup(d.close)
        return d

    def test_the_documented_example(self):
        # 直链鉴权开发指引's example: key 289ds32418bxdba, expiring at 1689220731, UID 13, rand 123.
        self.assertEqual(sign("http://13.cdn.123clouddisk.com/13/files/1.txt", KEY, 13, 1689220731, "123"),
                         "http://13.cdn.123clouddisk.com/13/files/1.txt?auth_key=1689220731-123-13-3bdacc0e031fd67fe829152f37c8fbad")

    def test_the_path_is_signed_decoded(self):
        encoded = sign("https://vip.123pan.com/29/%E9%9F%B3%E4%B9%90/02%20a+b.wma", "k", 29, 1700000000, "r")
        written = sign("https://vip.123pan.com/29/音乐/02 a+b.wma", "k", 29, 1700000000, "r")
        digest = hashlib.md5("/29/音乐/02 a+b.wma-1700000000-r-29-k".encode()).hexdigest()  # "+" stays "+"
        self.assertEqual(auth_key(encoded), ["1700000000", "r", "29", digest])
        self.assertEqual(auth_key(written), auth_key(encoded))
        self.assertTrue(encoded.startswith("https://vip.123pan.com/29/%E9%9F%B3%E4%B9%90/02%20a+b.wma?auth_key="))

    def test_the_rest_of_the_query_is_kept(self):
        url = sign("https://h.example/29/a.txt?x=1&auth_key=1-2-3-old&y=%20#top", "k", 29, 1700000000, "r")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(parts.query.split("&")[:2], ["x=1", "y=%20"])
        self.assertEqual(len(urllib.parse.parse_qs(parts.query)["auth_key"]), 1)  # the old one replaced
        self.assertEqual(auth_key(url)[:3], ["1700000000", "r", "29"])
        self.assertEqual(parts.fragment, "top")

    def test_download_url(self):
        d = self.driver(uid=UID, private_key=KEY)
        url = d.download_url("/music/02 a+b.wma", filename="02 a+b.wma", expires=900)
        timestamp, rand, uid, _ = auth_key(url)
        self.assertAlmostEqual(int(timestamp), time.time() + 900, delta=5)  # the expiry, not the time it was signed
        self.assertEqual((len(rand), uid), (32, str(UID)))
        self.assertNotEqual(auth_key(d.download_url("/x.bin", filename="x.bin"))[1], rand)  # a new rand each time
        self.assertNotIn(KEY, url)
        with self.assertRaises(NotFound):
            d.download_url("/music", filename="music")
        unsigned = self.driver(uid=UID)  # no key: the link as the tree has it
        self.assertEqual(unsigned.download_url("/x.bin", filename="x.bin"), self.fake.link("/x.bin"))

    def test_requests_are_signed_and_sizes_kept(self):
        d = self.driver(uid=UID, private_key=KEY)
        self.assertEqual({e.name: e.size for e in d.list("/")}, {"x.bin": None, "music": None})
        self.assertEqual(d.stat("/x.bin").size, 100)
        self.assertEqual(d.stat("/x.bin").size, 100)
        self.assertEqual(d.stat("/music/02 a+b.wma").size, 3)
        self.assertEqual(d.open("/music/02 a+b.wma", 1, 2).read(), b"ma")  # open() asks no HEAD
        self.assertEqual(d.open("/x.bin").read(), b"x" * 100)
        self.assertEqual(self.fake.heads, 2)  # one for each file: kept under the link, not the signed one
        self.assertEqual(len(self.fake.targets), 4)
        self.assertTrue(all("auth_key=" in target and KEY not in target for target in self.fake.targets))

    def test_refused_links(self):
        for config in ({"uid": UID, "private_key": "wrong"}, {"uid": 13, "private_key": KEY}, {}):
            with self.subTest(config=config):
                d = self.driver(**config)
                self.assertIsNone(d.stat("/x.bin").size)  # shown without a size
                with self.assertRaises(AuthError) as caught:
                    d.open("/x.bin")
                self.assertIn("403", str(caught.exception))
        with self.assertRaises(NotFound):
            self.driver(self.fake.link("/gone.txt"), uid=UID, private_key=KEY).open("/gone.txt")

    def test_validate_config(self):
        self.assertEqual(Pan123LinkDriver.validate_config({"tree": self.tree}), {})
        self.assertEqual(Pan123LinkDriver.validate_config({"tree": self.tree, "uid": UID, "private_key": KEY}), {})
        self.assertEqual(list(Pan123LinkDriver.validate_config({"tree": self.tree, "private_key": KEY})), ["uid"])
        self.assertEqual(sorted(Pan123LinkDriver.validate_config({"tree": "a:\n    b:", "private_key": KEY})), ["tree", "uid"])


if __name__ == "__main__":
    unittest.main()
