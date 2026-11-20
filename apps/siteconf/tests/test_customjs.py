"""Custom JS as files of the site (apps/siteconf/customjs.py)."""

import json

from django.test import SimpleTestCase

from .. import customjs


class CustomJsTests(SimpleTestCase):
    def part(self, number):
        return f"/site/custom.js?part={number}"

    def items(self, text):
        loader = customjs.file_text(text, self.part)
        return json.loads(loader.split("var items = ", 1)[1].split(";\n", 1)[0])

    def test_plain_js_is_the_file(self):
        self.assertEqual(customjs.file_text("console.log('<b>');", self.part), "console.log('<b>');")
        self.assertIsNone(customjs.parse("var scripts = '<scriptless>';"))

    def test_scripts_in_their_order(self):
        text = (
            '<!-- 统计 -->\n<script async src="https://a.example/x.js" data-id="7"></script>\n'
            "<script>var _hmt = _hmt || [];</script>\n<p>ignored</p>\n"
            '<SCRIPT SRC="https://b.example/lib.js" defer></SCRIPT>\n'
            "<script type=\"module\">import x from '/y.js';</script>\n"
            '<script type="application/ld+json">{"@type": "WebSite"}</script>\n'
            "<script></script><script src=''></script>"
        )
        self.assertEqual(self.items(text), [
            {"src": "https://a.example/x.js", "attrs": [["async", ""], ["data-id", "7"]], "wait": False},
            {"src": self.part(1), "attrs": [], "wait": True},
            {"src": "https://b.example/lib.js", "attrs": [["defer", ""]], "wait": True},
            {"src": self.part(2), "attrs": [["type", "module"]], "wait": True},
            {"data": '{"@type": "WebSite"}', "attrs": [["type", "application/ld+json"]]},
        ])
        self.assertEqual(customjs.inline_parts(text), ["var _hmt = _hmt || [];", "import x from '/y.js';", ""])

    def test_odd_markup(self):
        # An unclosed script, entities in attributes, a script inside a comment.
        self.assertEqual(customjs.inline_parts("<script>var a = '</scrip' + 't>';"), ["var a = '</scrip' + 't>';"])
        self.assertEqual(self.items('<script src="https://x.example/a.js?b=1&amp;c=2"></script>')[0]["src"], "https://x.example/a.js?b=1&c=2")
        self.assertEqual(self.items("<!-- <script src='https://no.example'></script> --><script>1</script>"),
                         [{"src": self.part(1), "attrs": [], "wait": True}])

    def test_the_loader_is_plain_ascii(self):
        loader = customjs.file_text('<script src="https://x.example/统计.js"></script>', self.part)
        self.assertTrue(loader.split("*/", 1)[1].isascii())  # the list is JSON with \\u escapes
