"""The Markdown renderer (apps/core/markdown.py): what Markdown becomes, and that neither mode lets a script
through. Each XSS vector is checked by reading the output back with an HTML parser."""

import time
from html.parser import HTMLParser

from django.test import SimpleTestCase

from apps.core import markdown

# Elements that run or load something, and must never come out of either mode.
ACTIVE = {"script", "style", "iframe", "frame", "frameset", "object", "embed", "applet", "base", "meta", "link",
          "form", "button", "select", "textarea", "svg", "math", "template", "noscript", "portal", "param"}
URL_ATTRIBUTES = {"href", "src", "srcset", "poster", "cite", "action", "formaction", "data", "xlink:href", "background"}


class Parsed(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.tags = []  # (tag, {attr: value})
        self.feed(html)
        self.close()

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))

    handle_startendtag = handle_starttag


def dangerous_urls(value):
    """The URLs in an attribute value that a browser could run, as it reads them (control
    characters and spaces dropped, any case)."""
    found = []
    for candidate in value.split(","):
        url = "".join(ch for ch in candidate.strip().split(" ")[0] if ord(ch) > 0x20).lower()
        if url.startswith(("javascript:", "vbscript:", "livescript:", "data:text", "data:application", "data:image/svg")):
            found.append(url)
    return found


# The OWASP filter-evasion cheat sheet and friends: each one must come out harmless in both modes.
VECTORS = [
    "<script>alert(1)</script>",
    "<SCRIPT SRC=https://xss.example/xss.js></SCRIPT>",
    "<script/xss src=https://xss.example/a.js></script>",
    "<<script>alert(1);//<</script>",
    "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "<img src=x onerror=alert(1)>",
    "<img src=\"x\" ONERROR=\"alert(1)\">",
    "<img src=x onerror=alert`1`>",
    "<img/src=x/onerror=alert(1)>",
    "<img src=\"javascript:alert(1)\">",
    "<img src=\" &#14;  javascript:alert(1)\">",
    "<img srcset=\"javascript:alert(1) 1x\">",
    "<img src=x:alert(alt) onerror=eval(src) alt=0>",
    "<img dynsrc=\"javascript:alert(1)\"><img lowsrc=\"javascript:alert(1)\">",
    "<a href=\"javascript:alert(1)\">x</a>",
    "<a href=\"JaVaScRiPt:alert(1)\">x</a>",
    "<a href=\"jav&#x09;ascript:alert(1)\">x</a>",
    "<a href=\"jav\tascript:alert(1)\">x</a>",
    "<a href=\"&#106;&#97;&#118;&#97;&#115;&#99;&#114;&#105;&#112;&#116;&#58;alert(1)\">x</a>",
    "<a href=\"&#x6A&#x61&#x76&#x61&#x73&#x63&#x72&#x69&#x70&#x74&#x3A;alert(1)\">x</a>",
    "<a href=\" javascript:alert(1)\">x</a>",
    "<a href=\"vbscript:msgbox(1)\">x</a>",
    "<a href=\"data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==\">x</a>",
    "<a href=\"data:image/svg+xml;base64,PHN2Zz4=\">x</a>",
    "[x](javascript:alert(1))",
    "[x](  javascript:alert(1)  )",
    "[x](JAVASCRIPT:alert(1))",
    "[x](javascript&#58;alert(1))",
    "[x](data:text/html,<script>alert(1)</script>)",
    "![x](javascript:alert(1))",
    "![x](data:image/svg+xml,<svg onload=alert(1)>)",
    "<javascript:alert(1)>",
    "[a]: javascript:alert(1)\n\n[x][a]",
    "<svg onload=alert(1)>",
    "<svg><script>alert(1)</script></svg>",
    "<svg><a xlink:href=\"javascript:alert(1)\"><text>x</text></a></svg>",
    "<math><mtext><table><mglyph><style><img src=x onerror=alert(1)>",
    "<math href=\"javascript:alert(1)\">x</math>",
    "<iframe src=\"javascript:alert(1)\"></iframe>",
    "<iframe srcdoc=\"<script>alert(1)</script>\"></iframe>",
    "<object data=\"javascript:alert(1)\"></object>",
    "<embed src=\"javascript:alert(1)\">",
    "<form action=\"javascript:alert(1)\"><button>x</button></form>",
    "<button formaction=\"javascript:alert(1)\">x</button>",
    "<input onfocus=alert(1) autofocus>",
    "<input type=\"image\" src=\"javascript:alert(1)\">",
    "<details open ontoggle=alert(1)>",
    "<body onload=alert(1)>",
    "<video><source onerror=\"alert(1)\"></video>",
    "<video poster=javascript:alert(1)></video>",
    "<audio src=x onerror=alert(1)></audio>",
    "<marquee onstart=alert(1)>x</marquee>",
    "<div style=\"background:url(javascript:alert(1))\">x</div>",
    "<div style=\"width: expression(alert(1))\">x</div>",
    "<table background=\"javascript:alert(1)\"><tr><td>x</td></tr></table>",
    "<meta http-equiv=\"refresh\" content=\"0;url=javascript:alert(1)\">",
    "<base href=\"javascript:alert(1)//\">",
    "<link rel=\"import\" href=\"https://xss.example/x.html\">",
    "<style>@import 'https://xss.example/x.css';</style>",
    "<noscript><p title=\"</noscript><img src=x onerror=alert(1)>\"></noscript>",
    "<textarea><img src=x onerror=alert(1)></textarea>",
    "<title><img src=x onerror=alert(1)></title>",
    "<xmp><img src=x onerror=alert(1)></xmp>",
    "<template><img src=x onerror=alert(1)></template>",
    "<!--<img src=x onerror=alert(1)>-->",
    "<![CDATA[<img src=x onerror=alert(1)>]]>",
    "<a href=\"https://ok.example/\" onclick=\"alert(1)\">x</a>",
    "<a href=\"https://ok.example/\"\nonmouseover=alert(1)>x</a>",
    "<p onmouseover=\"alert(1)\">x</p>",
    "`<script>` and ```\n<script>alert(1)</script>\n```",
    "<div><<img src=x onerror=alert(1)>></div>",
    "<img src=\"x\" alt=\"\" \" onerror=alert(1)>",
    "<a href=\"https://ok.example/\" style=\"position:fixed;inset:0\">x</a>",
    "<isindex type=image src=1 onerror=alert(1)>",
    "<keygen autofocus onfocus=alert(1)>",
]


class SafetyTests(SimpleTestCase):
    def assertHarmless(self, html, *, trusted):
        for tag, attrs in Parsed(html).tags:
            self.assertNotIn(tag, ACTIVE, html)
            for name, value in attrs.items():
                self.assertFalse(name.startswith("on"), html)
                if name in URL_ATTRIBUTES:
                    self.assertEqual(dangerous_urls(value or ""), [], html)
                if not trusted:
                    self.assertNotIn(name, {"style", "id", "name"}, html)
                    if name == "class":
                        self.assertEqual((tag, value[:9]), ("code", "language-"), html)
        self.assertNotRegex(html.lower(), r"<\s*script")

    def test_the_vectors(self):
        for vector in VECTORS:
            for trusted in (False, True):
                with self.subTest(vector, trusted=trusted):
                    html = markdown.render_trusted(vector) if trusted else markdown.render(vector)
                    self.assertHarmless(html, trusted=trusted)

    def test_the_vectors_inside_markdown(self):
        # In a list, a quote, a table cell, a link title and a heading too.
        for vector in VECTORS:
            text = f"# {vector}\n\n- {vector}\n\n> {vector}\n\n| a |\n|---|\n| {vector} |\n\n[t](https://ok.example/ \"{vector}\")"
            for trusted in (False, True):
                with self.subTest(vector, trusted=trusted):
                    html = markdown.render_trusted(text) if trusted else markdown.render(text)
                    self.assertHarmless(html, trusted=trusted)

    def test_scripts_go_with_their_content(self):
        for trusted in (False, True):
            html = (markdown.render_trusted if trusted else markdown.render)("a<script>stolen()</script>b<style>p{}</style>c")
            self.assertNotIn("stolen", html)
            self.assertNotIn("p{}", html)

    def test_no_ids_names_or_borrowed_classes_from_untrusted_text(self):
        html = markdown.render('<p id="snackbar" name="App" class="dialog dialog-full">x</p><img name="getElementById" src="a.png">')
        self.assertEqual(Parsed(html).tags, [("p", {}), ("img", {"src": "a.png", "loading": "lazy"})])

    def test_task_list_boxes_are_disabled_checkboxes_only(self):
        html = markdown.render("- [x] done\n- [ ] open\n\n<input type=password name=pw value=x>")
        inputs = [attrs for tag, attrs in Parsed(html).tags if tag == "input"]
        self.assertEqual(len(inputs), 3)
        for attrs in inputs:
            self.assertEqual((attrs.get("type"), "disabled" in attrs, "name" in attrs, "value" in attrs), ("checkbox", True, False, False))
        self.assertEqual(sum("checked" in attrs for attrs in inputs), 1)


class MarkdownTests(SimpleTestCase):
    def test_common_markdown(self):
        html = markdown.render(
            "# 标题\n\n**粗** *斜* ~~删~~ `code`\n\n1. 一\n2. 二\n\n> 引用\n\n---\n\n"
            "[链接](https://example.com/a?b=1&c=2 \"标题\") ![图](pic.png)\n\nhttps://example.com/auto"
        )
        for part in ("<h1>标题</h1>", "<strong>粗</strong>", "<em>斜</em>", "<del>删</del>", "<code>code</code>", "<ol>",
                     "<blockquote>", "<hr>", 'href="https://example.com/a?b=1&amp;c=2"', 'title="标题"',
                     'rel="noopener noreferrer"', '<img src="pic.png" alt="图" loading="lazy">',
                     '<a href="https://example.com/auto" rel="noopener noreferrer">https://example.com/auto</a>'):
            self.assertIn(part, html)

    def test_code_blocks_keep_their_language_and_escape(self):
        html = markdown.render("```python\nprint('<b>&')\n```")
        self.assertIn('<pre><code class="language-python">print(\'&lt;b&gt;&amp;\')\n</code></pre>', html)
        self.assertIn('<code class="language-c++">', markdown.render("```c++\nint x;\n```"))

    def test_tables_align_without_style(self):
        html = markdown.render("| 左 | 中 | 右 |\n|:--|:-:|--:|\n| 1 | 2 | 3 |")
        self.assertIn('<th align="left">左</th>', html)
        self.assertIn('<td align="center">2</td>', html)
        self.assertIn('<td align="right">3</td>', html)
        self.assertNotIn("style", html)

    def test_readme_html(self):
        html = markdown.render(
            '<p align="center"><img src="logo.png" width="120" alt="logo"></p>\n\n'
            '<details><summary>更多</summary>\n\n内容 <kbd>Ctrl</kbd> H<sub>2</sub>O x<sup>2</sup>\n\n</details>\n\n'
            '<picture><source media="(prefers-color-scheme: dark)" srcset="dark.png"><img src="light.png" alt=""></picture>'
        )
        for part in ('<p align="center"><img src="logo.png" width="120" alt="logo" loading="lazy"></p>', "<details>",
                     "<summary>更多</summary>", "<kbd>Ctrl</kbd>", "<sub>2</sub>", "<sup>2</sup>",
                     '<source media="(prefers-color-scheme: dark)" srcset="dark.png">'):
            self.assertIn(part, html)

    def test_image_data_urls(self):
        html = markdown.render('![a](data:image/png;base64,iVBORw0K) <img src="data:image/gif;base64,R0lG">')
        self.assertIn('src="data:image/png;base64,iVBORw0K"', html)
        self.assertIn('src="data:image/gif;base64,R0lG"', html)
        for bad in ('<img src="data:text/html,x">', '<img src="data:image/svg+xml,<svg>">', '<a href="data:image/png;base64,x">a</a>'):
            self.assertNotIn("data:", markdown.render(bad), bad)

    def test_relative_links_can_be_rewritten(self):
        seen = []

        def resolve(url):
            seen.append(url)
            return None if url.startswith("..") else f"/pd/local/docs/{url}"

        html = markdown.render("[a](other.md) ![b](img/p.png) [c](https://x.example/) [d](../up.md)", relative=resolve)
        self.assertIn('href="/pd/local/docs/other.md"', html)
        self.assertIn('src="/pd/local/docs/img/p.png"', html)
        self.assertIn('href="https://x.example/"', html)  # absolute: left alone
        self.assertNotIn("up.md", html)  # None removes it
        self.assertEqual(seen, ["other.md", "img/p.png", "../up.md"])
        self.assertIn('href="other.md"', markdown.render("[a](other.md)"))  # without: kept as written

    def test_relative_srcset_goes_through_the_rewrite_too(self):
        # nh3 hands only href and src to url_relative: srcset candidates are rewritten (or dropped) here.
        def resolve(url):
            return None if url.startswith("..") else f"/pd/local/docs/{url}"

        html = markdown.render('<img src="a.png" srcset="a.png 1x, ../up.png 2x, https://x.example/c.png 3x, //cdn.example/d.png 4x">'
                               '<picture><source srcset="../only.png"><img src="b.png"></picture>', relative=resolve)
        self.assertIn('srcset="/pd/local/docs/a.png 1x, https://x.example/c.png 3x, //cdn.example/d.png 4x"', html)
        self.assertNotIn("up.png", html)
        self.assertNotIn("only.png", html)  # nothing left: the attribute goes
        self.assertIn('srcset="a.png 1x, b.png 2x"', markdown.render('<img src="a.png" srcset="a.png 1x, b.png 2x">'))

    def test_empty_and_odd_input(self):
        self.assertEqual(markdown.render(""), "")
        self.assertEqual(markdown.render(None), "")
        self.assertEqual(markdown.render_trusted(""), "")
        self.assertIn("&lt;", markdown.render("a < b"))
        self.assertIn("不间断&nbsp;空格", markdown.render("不间断\u00a0空格"))

    def test_pathological_input_stays_fast(self):
        # Deep nesting and long runs of the characters the parsers look at: each in well under a second.
        texts = [
            "> " * 5000 + "x", "- " * 5000 + "x", "[" * 20000, "*" * 50000, "_" * 50000, "`" * 20000,
            "<" * 20000, "![" * 10000, "<a " * 10000, "|" * 20000 + "\n" + "|-" * 10000, "\\" * 50000,
            "*a " * 10000, "[a](" * 10000, "&#" * 20000, "<!--" * 10000, ("x" * 100 + "\n") * 20000,
        ]
        for text in texts:
            for render in (markdown.render, markdown.render_trusted):
                with self.subTest(text[:12], render=render.__name__):
                    start = time.process_time()
                    render(text)
                    self.assertLess(time.process_time() - start, 3.0)

    def test_too_long_text_is_cut(self):
        html = markdown.render("a" * (markdown.MAX_LENGTH + 10))
        self.assertEqual(html.count("a"), markdown.MAX_LENGTH)


class TrustedTests(SimpleTestCase):
    def test_admins_keep_their_html(self):
        html = markdown.render_trusted(
            '<div class="notice notice-info" style="color:red" data-x="1" aria-label="公告">'
            '<a href="https://example.com/" target="_blank">链接</a> <font color="red">红</font> '
            '<marquee>滚动</marquee> <video src="/media/a.mp4" controls></video> <a href="tel:+8610">电话</a></div>'
        )
        for part in ('class="notice notice-info"', 'style="color:red"', 'aria-label="公告"',
                     'target="_blank"', 'rel="noopener noreferrer"', '<font color="red">', "<marquee>",
                     '<video src="/media/a.mp4" controls="">', 'href="tel:+8610"'):
            self.assertIn(part, html)

    def test_no_hooks_of_the_sites_own_scripts(self):
        # data-open opens a dialog, data-login the login, data-require-login asks for it (shell.js).
        html = markdown.render_trusted('<a data-open="password-dialog" data-login data-require-login data-x="1" href="/x">a</a>')
        self.assertEqual(html.strip(), '<p><a href="/x" rel="noopener noreferrer">a</a></p>')

    def test_but_not_what_the_csp_blocks(self):
        html = markdown.render_trusted(
            '<script src="https://stats.example/s.js"></script><style>body{}</style>'
            '<iframe src="https://video.example/"></iframe><a href="javascript:x()" onclick="y()">a</a>'
        )
        self.assertEqual(html.strip(), '<a rel="noopener noreferrer">a</a>')

    def test_cached(self):
        markdown.render_trusted.cache_clear()
        markdown.render_trusted("公告")
        markdown.render_trusted("公告")
        self.assertEqual(markdown.render_trusted.cache_info().hits, 1)
