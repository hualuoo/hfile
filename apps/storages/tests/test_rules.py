"""The glob matcher of storage rules (apps/storages/rules.py, decision D9). Plain unittest: no Django."""

import unittest

from apps.storages.rules import Glob, GlobError, canonical, compile_glob, normalize_expression, prefixes, problem


def matches(expression, path):
    return compile_glob(expression).matches(canonical(path))


def covers(expression, path):
    return compile_glob(expression).covers(canonical(path))


class ExamplesOfD9(unittest.TestCase):
    """The examples the admin pages and the README give."""

    TABLE = [
        # expression, matches, doesn't match
        ("/", ["/"], ["/a"]),
        ("/dir", ["/dir"], ["/dir/a", "/dirx", "/"]),
        ("/dir/*", ["/dir/a", "/dir/b.txt"], ["/dir", "/dir/a/b", "/dirx/a"]),
        ("/dir/**", ["/dir/a", "/dir/a/b", "/dir/a/b/c.txt"], ["/dir", "/dirx", "/dirx/a"]),
        ("*.txt", ["/a.txt", "/.txt"], ["/d/a.txt", "/a.txt.bak"]),
        ("/*.txt", ["/a.txt"], ["/d/a.txt"]),
        ("**/*.txt", ["/a.txt", "/d/a.txt", "/d/e/a.txt"], ["/a.txt/x", "/atxt"]),
        ("**.txt", ["/a.txt", "/d/a.txt", "/d/e/a.txt"], ["/a.txtx"]),
        ("/a/**/b", ["/a/b", "/a/x/b", "/a/x/y/b"], ["/a/xb", "/a/b/c", "/b"]),
        ("**/private/**", ["/private/x", "/a/private/x", "/a/b/private/x/y"], ["/private", "/a/private", "/a/privatex/y"]),
        ("**/private", ["/private", "/a/private"], ["/private/x", "/aprivate"]),
        ("/**", ["/a", "/a/b"], ["/"]),
        ("/a?c", ["/abc", "/a.c"], ["/ac", "/a/c", "/abbc"]),
        ("/a*", ["/a", "/abc"], ["/b", "/a/b"]),
        ("/a**", ["/a", "/abc", "/a/b", "/ab/c"], ["/b"]),
        ("/x/*/y", ["/x/a/y"], ["/x/y", "/x/a/b/y"]),
    ]

    def test_the_table(self):
        for expression, yes, no in self.TABLE:
            for path in yes:
                with self.subTest(expression=expression, path=path):
                    self.assertTrue(matches(expression, path))
            for path in no:
                with self.subTest(expression=expression, path=path, expected=False):
                    self.assertFalse(matches(expression, path))

    def test_covering_is_the_entry_and_everything_below(self):
        self.assertTrue(covers("/dir", "/dir"))
        self.assertTrue(covers("/dir", "/dir/a/b"))
        self.assertFalse(covers("/dir", "/dirx/a"))
        self.assertFalse(covers("/dir", "/"))
        self.assertTrue(covers("/", "/anything/at/all"))
        self.assertTrue(covers("/dir/*", "/dir/a/deeper"))
        self.assertFalse(covers("/dir/*", "/dir"))
        self.assertFalse(covers("/dir/**", "/dir"))
        self.assertTrue(covers("*.txt", "/a.txt"))
        self.assertTrue(covers("**/private", "/a/private/x/y"))

    def test_other_characters_are_themselves(self):
        # No character classes, braces or escapes: a name with them is matched as it is.
        for name in ["[x](y)", "{a,b}", "a\\b", "100%", "a+b", "a.b", "(1)", "$x", "^x"]:
            with self.subTest(name=name):
                self.assertTrue(matches(f"/{name}", f"/{name}"))
        self.assertFalse(matches("/[ab]", "/a"))
        self.assertFalse(matches("/{a,b}", "/a"))
        self.assertFalse(matches("/a.c", "/abc"))


class Canonical(unittest.TestCase):
    """NFC, case folding and the trailing dots and spaces of each segment (aliases on Windows-like backends)."""

    def test_the_form(self):
        self.assertEqual(canonical("/"), "/")
        self.assertEqual(canonical("/Secret./B "), "/secret/b")
        self.assertEqual(canonical("/A/..."), "/a/...")  # a name of dots only stays itself
        self.assertEqual(canonical("/Straße"), "/strasse")
        self.assertEqual(canonical("/café"), "/café")

    def test_aliases_match_the_rule(self):
        for alias in ["/SECRET", "/Secret", "/secret.", "/secret ", "/secret. .", "/sEcReT/x"]:
            with self.subTest(alias=alias):
                self.assertTrue(covers("/secret", alias))
        self.assertTrue(covers("/café", "/café/x"))  # decomposed é
        self.assertTrue(covers("/CAFÉ", "/café"))
        self.assertTrue(covers("/strasse", "/Straße"))  # full case folding: the safe side
        self.assertTrue(covers("/ﬁles", "/files"))  # the ligature folds to fi

    def test_the_rule_is_folded_too(self):
        self.assertTrue(matches("/Photos/*.JPG", "/photos/a.jpg"))
        self.assertTrue(matches("/secret.", "/secret"))
        self.assertTrue(matches("/*.", "/a"))

    def test_prefixes(self):
        self.assertEqual(list(prefixes("/")), ["/"])
        self.assertEqual(list(prefixes("/a")), ["/", "/a"])
        self.assertEqual(list(prefixes("/a/b/c")), ["/", "/a", "/a/b", "/a/b/c"])


class Expressions(unittest.TestCase):
    def test_normalized(self):
        self.assertEqual(normalize_expression(" /a/b/ "), "/a/b")
        self.assertEqual(normalize_expression("a//b"), "/a/b")
        self.assertEqual(normalize_expression("*.txt"), "/*.txt")
        self.assertEqual(normalize_expression("/"), "/")
        self.assertEqual(normalize_expression("//"), "/")

    def test_refused(self):
        for bad, why in [("", "空"), ("   ", "空"), ("/a/../b", ".."), ("/./a", "."), ("/a\nb", "控制字符"),
                         ("/a\x00", "控制字符"), ("/a***", "*"), ("x" * 513, "512"), (None, "文字"), (5, "文字")]:
            with self.subTest(bad=bad):
                with self.assertRaises(GlobError) as caught:
                    compile_glob(bad) if isinstance(bad, str) else normalize_expression(bad)
                self.assertIn(why, str(caught.exception))
        self.assertEqual(problem("/fine/**"), "")
        self.assertIn("..", problem("../x"))

    def test_literal_prefix(self):
        self.assertEqual(compile_glob("/a/b*c").literal, "/a/b")
        self.assertEqual(compile_glob("/a/b").literal, "/a/b")
        self.assertEqual(compile_glob("**/x").literal, "/")
        self.assertEqual(compile_glob("/A/?x").literal, "/a/")


class ExpressionLength(unittest.TestCase):
    def test_the_length_as_stored(self):
        """API-11: the "/" put in front counts; 512 characters typed without it were 513 stored."""
        self.assertEqual(len(normalize_expression("/" + "a" * 511)), 512)
        with self.assertRaises(GlobError):
            normalize_expression("a" * 512)


class StaticFolderCheck(unittest.TestCase):
    """may_cover_within: could a rule cover the folder, something above or something below it (D12)."""

    def test_rules_inside_the_folder(self):
        self.assertTrue(compile_glob("/a/b/secret").may_cover_within("/a"))
        self.assertTrue(compile_glob("/a/b/secret/**").may_cover_within("/a/b"))
        self.assertTrue(compile_glob("/a/*/x").may_cover_within("/a"))

    def test_rules_above_or_at_the_folder(self):
        self.assertTrue(compile_glob("/a").may_cover_within("/a/b/c"))
        self.assertTrue(compile_glob("/a/**").may_cover_within("/a"))
        self.assertTrue(compile_glob("/").may_cover_within("/x"))

    def test_leading_wildcards_cover_every_folder(self):
        for folder in ["/", "/x", "/x/y"]:
            self.assertTrue(compile_glob("**/private/**").may_cover_within(folder))
            self.assertTrue(compile_glob("*.txt").may_cover_within(folder) or folder != "/")

    def test_unrelated_rules(self):
        self.assertFalse(compile_glob("/ab").may_cover_within("/a"))
        self.assertFalse(compile_glob("/a/b").may_cover_within("/a/c"))
        self.assertFalse(compile_glob("/a*").may_cover_within("/b"))
        self.assertFalse(compile_glob("/abc/**").may_cover_within("/ab"))
        self.assertTrue(compile_glob("/ab*").may_cover_within("/abc"))

    def test_folded(self):
        self.assertTrue(compile_glob("/Music/Private").may_cover_within(canonical("/MUSIC")))

    def test_segment_by_segment(self):
        self.assertFalse(compile_glob("*.bak").may_cover_within("/deep/a"))
        self.assertTrue(compile_glob("*.bak").may_cover_within("/"))
        self.assertTrue(compile_glob("/*/x").may_cover_within("/a"))
        self.assertFalse(compile_glob("/*/x").may_cover_within("/a/b"))
        self.assertFalse(compile_glob("/a/*.mp4").may_cover_within("/a/b"))

    def test_what_a_folder_operation_could_free(self):
        cases = {
            "**/private": {"/x": False, "/x/private": True, "/": False},
            "**.mp4": {"/x": False, "/x/a.mp4": True},
            "**/private/**": {"/x": False, "/x/private": True},
            "**/videos/*.mp4": {"/a": False, "/a/videos": True, "/a/videos/b": False},
            "**/x/a/secret": {"/r": False, "/r/x": True, "/r/x/a": True, "/r/a": False},
            "**/a/**/b": {"/q": True},
            "/a/private": {"/a": True, "/b": False},
        }
        for expression, folders in cases.items():
            glob = compile_glob(expression)
            for folder, expected in folders.items():
                with self.subTest(expression=expression, folder=folder):
                    self.assertEqual(glob.protects_within(folder), expected)

    def test_backslashes(self):
        self.assertEqual(canonical("/Secret\\File.TXT"), "/secret/file.txt")
        self.assertEqual(normalize_expression("a\\b\\"), "/a/b")
        with self.assertRaises(GlobError):
            normalize_expression("/a\\..\\b")


class EngineTests(unittest.TestCase):
    """The automaton (for expressions re could be slow with) agrees with the regular expression."""

    def test_the_automaton_agrees_with_the_regex(self):
        import itertools
        import re

        from apps.storages.rules import _regex_source

        names = ["a", "b", "ab", "a.b"]
        paths = ["/"] + ["/" + "/".join(combo) for depth in (1, 2, 3) for combo in itertools.product(names, repeat=depth)]
        for expression in ("/a", "*", "/*/b", "**", "/a/**", "**/b", "**/a/**", "/a/**/b/**/a", "*a*b*", "**.b",
                           "/a**b", "/**/?/**", "/*b*/**/a*", "**/a/b", "/a/*/**/b*", "**/**/b"):
            glob = Glob(expression)
            regex = re.compile(_regex_source([part for part in glob.pattern.split("/") if part]), re.DOTALL)
            automaton = glob.automaton
            for path in paths:
                with self.subTest(expression=expression, path=path):
                    expected = regex.fullmatch(path) is not None
                    self.assertEqual(bool(automaton.run(path) & automaton.final), expected)
                    self.assertEqual(glob.matches(path), expected)
                    covered = any(regex.fullmatch(prefix) for prefix in prefixes(path))
                    self.assertEqual(glob.covers(path), covered)

    def test_slow_expressions_use_the_automaton(self):
        self.assertIsNotNone(Glob("**/*.bak").regex)
        self.assertIsNotNone(Glob("**/private/**").regex)
        for expression in ("*-*-*.bak", "/a/**/b/**/c", "**/**/**/x", "/**a**b"):
            with self.subTest(expression=expression):
                self.assertIsNone(Glob(expression).regex)
