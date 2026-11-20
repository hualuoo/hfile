import unittest

from apps.storages.drivers import paths
from apps.storages.drivers.base import DriverError, InvalidPath, only_case_differs, rename_through


class CleanTests(unittest.TestCase):
    def test_clean(self):
        for given, expected in [(None, "/"), ("", "/"), ("/", "/"), ("a", "/a"), ("/a/", "/a"),
                                ("//a//b/", "/a/b"), ("/a\\b", "/a\\b"), ("/ a /", "/ a ")]:
            self.assertEqual(paths.clean(given), expected, given)

    def test_refused(self):
        for given in ["/a/../b", "..", "/./a", "/a/.", "a\x00b", "/a\ud800b", "\udfff", 3]:
            with self.assertRaises(InvalidPath, msg=repr(given)):
                paths.clean(given)

    def test_dots_and_spaces_only(self):
        # A server on Windows drops the dots and spaces at the end of a name: ".. " or "..." may be "..".
        for given in ["/a/.. /b", "/... ", "/. ./x", "/ ", "/a/  /b", "/.. ."]:
            with self.assertRaises(InvalidPath, msg=repr(given)):
                paths.clean(given)
        for fine in ["/a./b", "/.a", "/a ", "/..a", "/a..", "/ a"]:
            self.assertEqual(paths.clean(fine), fine)
        for bad in ["...", ".. ", " .", ". ."]:
            with self.assertRaises(InvalidPath, msg=repr(bad)):
                paths.check_name(bad)
            with self.assertRaises(InvalidPath, msg=repr(bad)):
                paths.join("/", bad)

    def test_join_split(self):
        self.assertEqual(paths.join("/", "a"), "/a")
        self.assertEqual(paths.join("/a", "b", "c"), "/a/b/c")
        self.assertEqual(paths.split("/a/b"), ("/a", "b"))
        self.assertEqual(paths.split("/a"), ("/", "a"))
        self.assertEqual(paths.split("/"), ("/", ""))
        for bad in ["", "a/b", "..", "."]:
            with self.assertRaises(InvalidPath):
                paths.join("/", bad)

    def test_inside(self):
        self.assertTrue(paths.is_inside("/a/b", "/a"))
        self.assertTrue(paths.is_inside("/a", "/a"))
        self.assertFalse(paths.is_inside("/ab", "/a"))
        self.assertTrue(paths.is_inside("/x", "/"))
        self.assertEqual(paths.relative("/a/b/c", "/a"), "/b/c")
        self.assertEqual(paths.relative("/a", "/a"), "/")
        self.assertEqual(paths.rebase("/b/c", "/a"), "/a/b/c")
        self.assertEqual(paths.rebase("/", "/a"), "/a")
        with self.assertRaises(InvalidPath):
            paths.relative("/ab", "/a")

    def test_check_name(self):
        for good in ["a", "中文.txt", "a b", "x" * 255, "汉" * 85]:
            self.assertEqual(paths.check_name(good), good)
        for bad in ["", "  ", ".", "..", "a/b", "a\\b", "a\nb", "a\tb", "a\x7fb", "x" * 256, "汉" * 86, "\ud800"]:
            with self.assertRaises(InvalidPath, msg=repr(bad)):
                paths.check_name(bad)
        with self.assertRaises(InvalidPath):
            paths.check_name("a?b", forbidden='"*:<>?|')

    def test_check_name_text_direction(self):
        """SEC-07: "invoice‮txt.exe" would be listed as "invoiceexe.txt". Every Bidi_Control
        character is refused; the other format characters stay (emoji's ZWJ, Persian's ZWNJ)."""
        for char in "؜‎‏‪‫‬‭‮⁦⁧⁨⁩":
            with self.subTest(char=f"U+{ord(char):04X}"), self.assertRaises(InvalidPath) as caught:
                paths.check_name(f"invoice{char}txt.exe")
            self.assertIn(f"U+{ord(char):04X}", caught.exception.message)
        for good in ["👨‍👩‍👧", "می‌خواهم", "a­b", "﻿bom"]:
            self.assertEqual(paths.check_name(good), good)


class RenameThroughTests(unittest.TestCase):
    """base.rename_through: a rename that changes only the case, through a third name."""

    def renames(self, failing=()):
        done = []

        def rename(old, new):
            if len(done) in failing:
                done.append(None)
                raise OSError("refused")
            done.append((old, new))
        return done, rename

    def test_two_renames(self):
        done, rename = self.renames()
        rename_through(rename, "/d/readme.txt", "README.txt")
        (first, middle), (again, last) = done
        self.assertEqual((first, again, last), ("/d/readme.txt", middle, "/d/README.txt"))
        self.assertRegex(middle, r"^/d/\.hfile-rename-[0-9a-f]{12}$")

    def test_the_name_back_when_the_second_fails(self):
        done, rename = self.renames(failing={1})
        with self.assertRaises(OSError):
            rename_through(rename, "/readme.txt", "README.txt")
        self.assertEqual(done[2], (done[0][1], "/readme.txt"))

    def test_the_name_it_has_when_that_fails_too(self):
        done, rename = self.renames(failing={1, 2})
        with self.assertRaises(DriverError) as caught:
            rename_through(rename, "/readme.txt", "README.txt")
        self.assertIn(done[0][1].lstrip("/"), caught.exception.message)

    def test_only_case_differs(self):
        self.assertTrue(only_case_differs("a.txt", "A.TXT"))
        self.assertFalse(only_case_differs("a.txt", "a.txt"))
        self.assertFalse(only_case_differs("a.txt", "b.txt"))
