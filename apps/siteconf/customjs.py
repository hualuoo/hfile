"""自定义 JS (显示设置) as a file of this site, /site/custom.js (decision D33).

Admins paste what a statistics service gives them, script tags and all, or just the JavaScript. The
pages' CSP runs no inline script, so the text is turned into files:

- Text without a <script> tag is JavaScript as it is: /site/custom.js is that text.
- Otherwise /site/custom.js is a small loader that adds the scripts to the page in their order, as
  the HTML would have run them: <script src="..."> with all its attributes (async ones without
  waiting for them), an inline <script> as /site/custom.js?part=<n> (the same text, served as a file
  of its own, so its top-level names are global, as in the HTML). Each one starts when the one
  before it has loaded, so "load the library, then call it" works. Scripts of a type that isn't
  JavaScript (application/ld+json, templates) are added as data, not run. Anything outside the
  script tags is left out.

The page then needs script-src https: for external scripts: the public pages get it while custom
JS is set (apps/siteconf/middleware.py). What browsers don't allow in an added script still doesn't run:
document.write() in an external script, inline event handlers.
"""

from __future__ import annotations

import functools
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser

_SCRIPT_TAG = re.compile(r"<script[\s>/]", re.IGNORECASE)
# The MIME types (and "module") that make a <script> run; anything else is a data block.
_RUNNABLE = {
    "", "module", "text/javascript", "application/javascript", "application/ecmascript", "text/ecmascript",
    "application/x-javascript", "application/x-ecmascript", "text/jscript", "text/livescript",
    "text/x-javascript", "text/x-ecmascript", "text/javascript1.0", "text/javascript1.1",
    "text/javascript1.2", "text/javascript1.3", "text/javascript1.4", "text/javascript1.5",
}


@dataclass
class Script:
    attrs: list = field(default_factory=list)  # [(name, value)], src among them for an external one
    code: str = ""  # an inline script's text

    @property
    def src(self):
        return next((value for name, value in self.attrs if name == "src"), None)

    @property
    def runnable(self):
        kind = next((value for name, value in self.attrs if name == "type"), "") or ""
        return kind.strip().lower() in _RUNNABLE


class _Scripts(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.found = []
        self._open = None

    def handle_starttag(self, tag, attrs):
        if tag == "script":
            self._open = Script(attrs=[(name, value if value is not None else "") for name, value in attrs])
            self.found.append(self._open)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self._open = None

    def handle_endtag(self, tag):
        if tag == "script":
            self._open = None

    def handle_data(self, data):
        if self._open is not None:
            self._open.code += data


@functools.lru_cache(maxsize=4)
def parse(text):
    """The <script> elements of the admin's text, or None when it has none (then it is all JS)."""
    if not _SCRIPT_TAG.search(text):
        return None
    parser = _Scripts()
    parser.feed(text)
    parser.close()
    return tuple(parser.found)


def inline_parts(text):
    """The texts of the inline scripts, numbered from 1 as /site/custom.js?part=<n> serves them."""
    scripts = parse(text) or ()
    return [script.code for script in scripts if script.src is None and script.runnable]


def file_text(text, part_url):
    """/site/custom.js: the admin's JS, or the loader (see the top). part_url(n) is the address of
    the n-th inline script."""
    scripts = parse(text)
    if scripts is None:
        return text
    items, part = [], 0
    for script in scripts:
        attrs = [[name, value] for name, value in script.attrs if name != "src"]
        if script.src is not None:
            if not script.src.strip():
                continue
            items.append({"src": script.src, "attrs": attrs, "wait": not any(name == "async" for name, _ in attrs)})
        elif script.runnable:
            part += 1
            if script.code.strip():
                items.append({"src": part_url(part), "attrs": attrs, "wait": True})
        elif script.code.strip():
            items.append({"data": script.code, "attrs": attrs})
    listed = json.dumps(items, ensure_ascii=True, indent=1)
    return _LOADER.replace("/*ITEMS*/[]", listed)


_LOADER = """/* The custom JS of the display settings: HFile adds these scripts to the page in their order (/site/custom.js). */
(function () {
  'use strict';
  var items = /*ITEMS*/[];
  function add(index) {
    if (index >= items.length) return;
    var item = items[index];
    var script = document.createElement('script');
    item.attrs.forEach(function (pair) { script.setAttribute(pair[0], pair[1]); });
    if (item.data !== undefined) {
      script.textContent = item.data;
      document.head.appendChild(script);
      add(index + 1);
      return;
    }
    if (item.wait) {
      script.async = false;
      var next = function () { add(index + 1); };
      script.addEventListener('load', next);
      script.addEventListener('error', next);
    }
    script.src = item.src;
    document.head.appendChild(script);
    if (!item.wait) add(index + 1);
  }
  add(0);
})();
"""
