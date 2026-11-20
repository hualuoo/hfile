"""apps.core.parsers.parse_object(): a body of a known shape (the text editor's save) parsed by its shape.

What fits the shape comes out as the site's JSONParser gives it, and bad JSON gets JSONParser's message:
compared on generated documents (fixed seed) and every damaged version of them. What doesn't fit is
refused where it starts, without building the rest."""

import io
import json
import random
import tracemalloc

from django.test import SimpleTestCase
from rest_framework.exceptions import ParseError

from .parsers import JSONParser, decode, parse_object

FIELDS = {"storage": None, "path": None, "content": None, "encoding": None, "newline": None,
          "expected": {"modified": None, "size": None}, "password": None}  # the text save's
KNOWN = "storage、path、content、encoding、newline、expected、password"

STRING_PARTS = ["abc", "中文", "😀", "é", " ", "'", "{", "}", "[", "]", ",", ":", '\\"', "\\\\", "\\/", "\\b", "\\f",
                "\\n", "\\r", "\\t", "\\u0041", "\\u4e2d", "\\ud83d\\ude00", "\\u0000", "\\uFFFF", "\\ud800"]
NUMBERS = ["0", "-0", "7", "-12", "3.5", "-0.25", "1e3", "2E-2", "1.5e+10", "123456789012345678901234567890", "1e400",
           "NaN", "-Infinity"]
SPACES = ["", "", "", " ", "\n", "\t", " \r\n "]
# Put in outside the strings, or inside a value's string: neither adds a key, an array or an object.
OUTSIDE = [",", ":", " ", "x", "1", "-", ".", "e", "t", "n", "N", "I", "\\", "/", "\t"]
INSIDE = ["\\", "\x01", "\n", '"', "\\u", "\\ud800", "\\udc00", "\\x", "\\u12"]


def old(raw: bytes):
    """What the site's JSONParser makes of a body: ("ok", repr of the data) or ("error", its message)."""
    try:
        return "ok", repr(JSONParser().parse(io.BytesIO(raw), "application/json", {}))
    except ParseError as error:
        return "error", str(error.detail)


def new(raw: bytes):
    try:
        return "ok", repr(parse_object(decode(raw, {}), FIELDS))
    except ParseError as error:
        return "error", str(error.detail)


class Documents:
    """Random documents of the text save's shape, any order of keys, any spacing."""

    def __init__(self, seed):
        self.rng = random.Random(seed)

    def space(self):
        return self.rng.choice(SPACES)

    def string(self):
        return '"' + "".join(self.rng.choice(STRING_PARTS) for _ in range(self.rng.randrange(6))) + '"'

    def scalar(self):
        return self.rng.choice([self.string, self.string, lambda: self.rng.choice(NUMBERS), lambda: "true",
                                lambda: "false", lambda: "null"])()

    def key(self, name):
        return f'"\\u{ord(name[0]):04x}{name[1:]}"' if self.rng.random() < 0.1 else f'"{name}"'  # "storage"

    def object(self, fields):
        names = self.rng.sample(list(fields), self.rng.randrange(len(fields) + 1))
        pairs = []
        for name in names:
            value = self.object(fields[name]) if fields[name] and self.rng.random() < 0.7 else self.scalar()
            pairs.append(self.space() + self.key(name) + self.space() + ":" + self.space() + value + self.space())
        return "{" + (",".join(pairs) if pairs else self.space()) + "}"

    def document(self):
        return self.space() + self.object(FIELDS) + self.space()


def regions(doc):
    """For each character of a valid document: "key" or "value" in a string (its quotes too), "out" elsewhere."""
    kinds = ["out"] * len(doc)
    index = 0
    while index < len(doc):
        if doc[index] == '"':
            end = index + 1
            while doc[end] != '"':
                end += 2 if doc[end] == "\\" else 1
            kind = "key" if doc[end + 1:].lstrip(" \t\r\n").startswith(":") else "value"
            kinds[index:end + 1] = [kind] * (end + 1 - index)
            index = end
        index += 1
    return kinds


def damaged(doc):
    """doc with one change that can't add a key, an array or an object: cut off anywhere; a character outside
    the strings (not a bracket or a quote) taken out; one put in outside the strings or inside a value's."""
    kinds = regions(doc)
    for end in range(len(doc)):
        yield doc[:end]
    for index, char in enumerate(doc):
        if kinds[index] == "out" and char not in '{}[]"':
            yield doc[:index] + doc[index + 1:]
    for index in range(len(doc) + 1):
        around = (kinds[index - 1] if index else "out", kinds[index] if index < len(doc) else "out")
        for char in OUTSIDE if around == ("out", "out") else INSIDE if around == ("value", "value") else ():
            yield doc[:index] + char + doc[index:]


class SameAsJSONParserTests(SimpleTestCase):
    def assertSame(self, raw):
        self.assertEqual(new(raw), old(raw), raw[:300])

    def test_documents_of_the_shape_come_out_the_same(self):
        documents = Documents(20261110)
        outcomes = set()
        for _ in range(2000):
            raw = documents.document().encode()
            self.assertSame(raw)
            outcomes.add(old(raw)[0])
        self.assertEqual(outcomes, {"ok", "error"})  # NaN, half pairs: refused by both

    def test_bad_json_gets_the_same_message(self):
        documents = Documents(1110)
        fixed = ['{"storage": "disk", "path": "/a\\"b.txt", "content": "第一行\\n😀\\u4e2d\\\\", "encoding": "gbk", '
                 '"newline": "crlf", "expected": {"modified": "2026-11-10T00:00:00+00:00", "size": 12}, "password": null}',
                 '{"expected":{},"storage":-0.5e3,"content":""}', ' { } ', '{"expected": null}']
        count = 0
        for doc in fixed + [documents.document() for _ in range(40)]:
            for version in damaged(doc):
                self.assertSame(version.encode())
                count += 1
        self.assertGreater(count, 10_000)

    def test_bytes_that_arent_utf8_and_the_bom(self):
        doc = '{"storage": "disk", "content": "中文"}'.encode()
        for index in range(len(doc) + 1):
            self.assertSame(doc[:index] + b"\xff" + doc[index:])
        for raw in (b"\xef\xbb\xbf" + doc, doc + "中".encode()[:2], doc[:-4] + doc[-3:]):
            self.assertSame(raw)
        for charset in ("utf-8-sig", "ascii", "latin-1"):
            context = {"encoding": charset}
            self.assertEqual(parse_object(decode(b'\xef\xbb\xbf{"path": "a"}' if charset == "utf-8-sig" else b'{"path": "a"}',
                                                 context), FIELDS), {"path": "a"})
        with self.assertRaisesMessage(ParseError, "JSON 格式不对：不是 ASCII 编码的文字（第 11 个字节）。"):
            decode('{"path": "中"}'.encode(), {"encoding": "ascii"})
        with self.assertRaisesMessage(ParseError, "不支持 Content-Type 里写的字符集 rot13。"):
            decode(b"{}", {"encoding": "rot13"})


class RefusedTests(SimpleTestCase):
    def test_what_doesnt_fit_the_shape(self):
        cases = [
            ('{"storage": "a", "junk": 1}', f"有不认识的字段，只能有 {KNOWN}。"),
            ('{"Storage": "a"}', f"有不认识的字段，只能有 {KNOWN}。"),
            ('{"expected": {"size": 1, "when": 2}}', "expected 里有不认识的字段，只能有 modified、size。"),
            ('{"content": "a", "content": "b"}', "content 出现了不止一次。"),
            ('{"content": "a", "\\u0063ontent": "b"}', "content 出现了不止一次。"),
            ('{"expected": null, "expected": {}}', "expected 出现了不止一次。"),
            ('{"expected": {"size": 1, "size": 1}}', "expected.size 出现了不止一次。"),
            ('{"content": ["a"]}', "content 不能是数组。"),
            ('{"expected": []}', "expected 不能是数组。"),
            ('{"expected": {"size": [1]}}', "expected.size 不能是数组。"),
            ('{"storage": {}}', "storage 不能是对象。"),
            ('{"expected": {"modified": {"a": 1}}}', "expected.modified 不能是对象。"),
            ("[]", "要的是一个对象（{…}）。"),
            ('"text"', "要的是一个对象（{…}）。"),
            ("12", "要的是一个对象（{…}）。"),
            ("null", "要的是一个对象（{…}）。"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                with self.assertRaises(ParseError) as caught:
                    parse_object(body, FIELDS)
                self.assertEqual(str(caught.exception.detail), f"JSON 格式不对：{message}")
        # Bad JSON before the place that doesn't fit: JSONParser's message. Trailing data: its "Extra data".
        for body, message in [('{"storage" 1, "junk": [1]}', "第 1 行第 12 列应该是冒号（:）"),
                              ('{"storage": "a"} [1]', "第 1 行第 18 列开始是多余的内容，JSON 在这之前已经完了"),
                              ('"text" x', "第 1 行第 8 列开始是多余的内容，JSON 在这之前已经完了")]:
            with self.assertRaisesMessage(ParseError, f"JSON 格式不对：{message}。"):
                parse_object(body, FIELDS)

    def test_refused_before_the_rest_is_built(self):
        junk = "[" + ",".join(["{}"] * 60_000) + "]"  # 180 KB, an object for every 3 bytes once parsed
        bodies = {
            "an unknown key": '{"storage": "a", "junk": %s}',
            "an array": '{"content": %s}',
            "a deeper object": '{"expected": {"size": {"a": %s}}}',
            "a repeated key": '{"content": "a", "content": %s}',
            "trailing data": '{"storage": "a"} %s',
            "an array instead of the object": "%s",
        }
        for what, body in bodies.items():
            body = body % junk
            with self.subTest(what):
                tracemalloc.start()
                try:
                    with self.assertRaises(ParseError):
                        parse_object(body, FIELDS)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, len(body) // 2)  # parsed whole: about 20 times the body


class JSONParserTests(SimpleTestCase):
    """The API's JSONParser: what it refuses, and in what words."""

    def parse(self, body: str):
        return JSONParser().parse(io.BytesIO(body.encode()), "application/json", {})

    def assertRefused(self, body: str, message: str):
        with self.assertRaises(ParseError) as caught:
            self.parse(body)
        self.assertEqual(str(caught.exception.detail), message)

    def test_bad_json_is_told_in_chinese(self):
        """TEXT-02: json's English was passed on, 「JSON parse error - Expecting ',' delimiter: line 1 column 9 (char 8)」."""
        cases = [
            ('{"a": 1 "b": 2}', "第 1 行第 9 列应该是逗号（,）"),
            ('{"a":\n [1,\n 2 3]}', "第 3 行第 4 列应该是逗号（,）"),
            ('{"a": }', "第 1 行第 7 列缺少值"),
            ("{a: 1}", "第 1 行第 2 列应该是用双引号括起来的字段名"),
            ('{"a" 1}', "第 1 行第 6 列应该是冒号（:）"),
            ('{"a": 1} x', "第 1 行第 10 列开始是多余的内容，JSON 在这之前已经完了"),
            ('"abc', "第 1 行第 1 列开始的字符串没有结束"),
            ('{"a": "x' + chr(9) + 'y"}', "第 1 行第 9 列有没转义的控制字符"),
            ('{"a": "\\q"}', "第 1 行第 8 列有不认识的转义（\\ 后面）"),
            ('{"a": "\\u12"}', "第 1 行第 9 列有写错的 \\uXXXX 转义"),
        ]
        for body, message in cases:
            with self.subTest(body=body):
                self.assertRefused(body, f"JSON 格式不对：{message}。")
        self.assertRefused(chr(0xFEFF) + "{}", "JSON 格式不对：开头不能有 BOM（U+FEFF）。")
        self.assertRefused('{"a": ' + "1" * 5000 + "}", "JSON 格式不对：整数的位数太多了。")
        with self.assertRaisesMessage(ParseError, "JSON 格式不对：不是 UTF-8 编码的文字（第 8 个字节）。"):
            JSONParser().parse(io.BytesIO(b'{"a": "' + bytes([0xFF]) + b'"}'), "application/json", {})

    def test_numbers_that_are_no_numbers(self):
        """API-08: 1e400 was read as infinity; STRICT_JSON only refused NaN and Infinity written as such."""
        for number in ("1e400", "-1e400", "1.5e309", "1" * 400 + ".0"):
            with self.subTest(number=number[:20]):
                self.assertRefused(f'{{"a": {number}}}', f"JSON 格式不对：数字 {number[:30]} 太大了。")
                with self.assertRaises(ParseError) as caught:
                    parse_object(f'{{"storage": {number}}}', FIELDS)
                self.assertEqual(str(caught.exception.detail), f"JSON 格式不对：数字 {number[:30]} 太大了。")
        for constant in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(constant=constant):
                self.assertRefused(f"[{constant}]", f"JSON 格式不对：不能有 {constant}（JSON 里没有这样的数）。")
        self.assertEqual(self.parse('[1e308, -1e-400, 12345678901234567890, 0.1]'), [1e308, -0.0, 12345678901234567890, 0.1])

    def test_objects_and_arrays_are_counted(self):
        """SEC-09: once parsed, an object or an array takes some 60 bytes for its 2 or 3 bytes of JSON: a 2.5 MB
        body of [{}, {}, ...] took 60 MB. Up to 50 000 of them pass (10 000 grants of a user take 30 001)."""
        self.assertEqual(len(self.parse("[" + ",".join(["{}"] * 49_999) + "]")), 49_999)
        self.assertRefused("[" + ",".join(["{}"] * 50_000) + "]", "JSON 格式不对：对象和数组太多了，最多 50000 个。")
        self.assertRefused("[" + ",".join(["[]"] * 50_000) + "]", "JSON 格式不对：对象和数组太多了，最多 50000 个。")
        # Inside strings, { and [ are text.
        text = '{[\\"' * 100_000
        self.assertEqual(self.parse(json.dumps({"a": text, "b": [{}]})), {"a": text, "b": [{}]})
        body = "[" + ",".join(["{}"] * 800_000) + "]"  # 2.4 MB
        tracemalloc.start()
        try:
            with self.assertRaises(ParseError):
                JSONParser().parse(io.BytesIO(body.encode()), "application/json", {})
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 4 * len(body))  # parsed: some 20 times the body
