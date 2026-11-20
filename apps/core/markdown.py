"""The site's one Markdown renderer (decision D22): mistune turns Markdown into HTML, nh3 keeps what
may be shown. Two modes:

- render(text): untrusted text. A README.md in a storage, a Markdown file being previewed: anyone
  who can upload a file writes it. HTML in it is kept as far as READMEs use it (details/summary,
  <p align="center">, <img width>, <picture> with a dark-mode <source>, <kbd>, <sup>...), through a
  fixed allow-list. Never scripts, styles, frames, forms, event attributes or `style`; links only
  http(s), mailto and relative ones, with rel="noopener noreferrer"; images also data:image/png,
  gif, jpeg and webp; no id or name (they could take the page's own element ids), and no class but
  code's language-xxx (for highlighting), so the text can't borrow the site's own styles to imitate
  its dialogs. `relative`, a function (url) -> url or None, rewrites the relative links and images
  (a README's own pictures, which sit next to it in the storage).
- render_trusted(text): what admins write in /manage/ (the announcement, readme rules typed there).
  Their HTML is kept, class and style included, except what the pages' CSP blocks anyway: <script>
  and <style> (with their content), frames, objects, forms, <base>, <meta>, <link>, event
  attributes, javascript: links; and data-* attributes, to which the site's own scripts react
  (data-open opens a dialog, data-login the login). The result thus looks the same under the strict
  policy and under the one public pages get while custom JS is set (D33), and a <script
  src="https://..."> in an announcement can't use that policy's https: scripts.

Both give HTML to put into a page as it is ({{ html|safe }} or element.innerHTML).

The callers limit the size: rendering is pure Python, a few MB a second (MAX_LENGTH is a sanity
limit, not a budget). Pathological input (deep nesting, long runs of brackets) stays linear.
"""

from __future__ import annotations

import functools
import re
import threading
from collections.abc import Callable

import mistune
import nh3

# Longer text is cut here before rendering: callers should refuse far less (D21's size guards).
MAX_LENGTH = 5_000_000

_PLUGINS = ["strikethrough", "table", "url", "task_lists"]

# ------------------------------------------------------------------ untrusted

_TAGS = {
    "a", "abbr", "b", "bdi", "bdo", "blockquote", "br", "caption", "center", "cite", "code", "col", "colgroup",
    "dd", "del", "details", "dfn", "div", "dl", "dt", "em", "figcaption", "figure", "h1", "h2", "h3", "h4", "h5",
    "h6", "hr", "i", "img", "input", "ins", "kbd", "li", "mark", "ol", "p", "picture", "pre", "q", "rp", "rt",
    "ruby", "s", "samp", "small", "source", "span", "strike", "strong", "sub", "summary", "sup", "table", "tbody",
    "td", "tfoot", "th", "thead", "time", "tr", "tt", "u", "ul", "var", "wbr",
}
_ALIGNED = ("div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "img", "table", "td", "th", "tr", "figure", "center")
_ATTRIBUTES = {
    "*": {"title", "lang", "dir"},
    "a": {"href"},
    "img": {"src", "srcset", "alt", "width", "height"},
    "source": {"srcset", "media", "type", "width", "height"},
    "input": {"checked"},  # task lists; always a disabled checkbox (below)
    "code": {"class"},
    "ol": {"start", "type"},
    "li": {"value"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "details": {"open"},
    "time": {"datetime"},
}
for _tag in _ALIGNED:
    _ATTRIBUTES.setdefault(_tag, set()).add("align")
_FORCED = {"input": {"type": "checkbox", "disabled": ""}, "img": {"loading": "lazy"}}
_TRUSTED_FORCED = {"input": {"type": "checkbox", "disabled": ""}}

# ------------------------------------------------------------------ trusted

_TRUSTED_TAGS = _TAGS | {
    "address", "article", "aside", "audio", "big", "blink", "data", "font", "footer", "header", "hgroup", "main",
    "marquee", "meter", "nav", "progress", "section", "track", "video",
}
_TRUSTED_ATTRIBUTES = {
    "*": {"title", "lang", "dir", "class", "style", "align", "valign", "width", "height", "hidden", "role",
          "color", "bgcolor", "border"},
    "a": {"href", "target", "download", "hreflang"},
    "img": {"src", "srcset", "sizes", "alt", "loading", "decoding"},
    "source": {"src", "srcset", "type", "media", "sizes"},
    "video": {"src", "poster", "controls", "autoplay", "loop", "muted", "playsinline", "preload"},
    "audio": {"src", "controls", "autoplay", "loop", "muted", "preload"},
    "track": {"src", "kind", "srclang", "label", "default"},
    "input": {"checked"},
    "font": {"face", "size"},
    "table": {"cellpadding", "cellspacing", "summary"},
    "td": {"colspan", "rowspan", "nowrap", "headers"},
    "th": {"colspan", "rowspan", "nowrap", "headers", "scope"},
    "col": {"span"},
    "colgroup": {"span"},
    "ol": {"start", "type", "reversed"},
    "ul": {"type"},
    "li": {"value"},
    "details": {"open"},
    "time": {"datetime"},
    "meter": {"value", "min", "max", "low", "high", "optimum"},
    "progress": {"value", "max"},
    "marquee": {"behavior", "direction", "scrollamount", "scrolldelay", "loop"},
    "blockquote": {"cite"},
    "q": {"cite"},
}

# ------------------------------------------------------------------ both

_SCHEMES = {"http", "https", "mailto", "data"}  # data: only where _url_value allows it
_TRUSTED_SCHEMES = _SCHEMES | {"tel"}
_IMAGE_DATA = re.compile(r"data:image/(?:png|gif|jpeg|webp)[;,]", re.IGNORECASE)
_MEDIA_URLS = {("img", "src"), ("source", "src"), ("video", "poster")}
_LANGUAGE = re.compile(r"language-[A-Za-z0-9_+#.-]{1,40}")
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_URL_NOISE = re.compile(r"[\x00-\x20]")  # what browsers drop from a URL (or around it) before reading it


def _plain_url(url):
    """http(s), or relative: for the URLs nh3 doesn't check (srcset, poster, cite)."""
    scheme = _SCHEME.match(_URL_NOISE.sub("", url))
    return not scheme or scheme.group(0).lower() in ("http:", "https:")


def _url_value(tag, attr, value):
    """data: only for images (nh3 has checked the schemes of href and src already)."""
    if _URL_NOISE.sub("", value)[:5].lower() == "data:":
        return value if (tag, attr) in _MEDIA_URLS and _IMAGE_DATA.match(value.strip()) else None
    if attr in ("poster", "cite") and not _plain_url(value):
        return None
    return value


def _srcset(value, relative=None):
    """A srcset ("a.png 1x, https://x/b.png 2x") whose every URL is http(s) or relative, else None.
    With `relative` (render(relative=...)), relative URLs go through it as nh3 sends href and src:
    rewritten, or that candidate dropped (None when none is left)."""
    kept = []
    for candidate in value.split(","):
        parts = candidate.split()
        if not parts or not _plain_url(parts[0]) or _URL_NOISE.sub("", parts[0])[:5].lower() == "data:":
            return None
        if relative is not None and not _SCHEME.match(_URL_NOISE.sub("", parts[0])) and not parts[0].startswith("//"):
            rewritten = relative(parts[0])
            if not rewritten:
                continue
            parts[0] = rewritten
        kept.append(" ".join(parts))
    if relative is None:
        return value
    return ", ".join(kept) or None


def _filter(tag, attr, value):
    if attr in ("href", "src", "poster", "cite"):
        return _url_value(tag, attr, value)
    if attr == "srcset":
        return _srcset(value)
    return value


def _untrusted_filter(tag, attr, value):
    if attr == "class":
        if tag != "code":
            return None
        kept = [name for name in value.split() if _LANGUAGE.fullmatch(name)]
        return kept[0] if kept else None
    return _filter(tag, attr, value)


def _cleaner(trusted, relative=None):
    attribute_filter = _filter if trusted else _untrusted_filter
    if relative is not None and not trusted:
        def attribute_filter(tag, attr, value):  # noqa: F811 - srcset goes through `relative` too
            if attr == "srcset":
                return _srcset(value, relative)
            return _untrusted_filter(tag, attr, value)

    return nh3.Cleaner(
        tags=_TRUSTED_TAGS if trusted else _TAGS,
        clean_content_tags={"script", "style", "noscript", "template", "textarea", "title", "xmp", "iframe",
                            "noembed", "noframes", "object"},
        attributes=_TRUSTED_ATTRIBUTES if trusted else _ATTRIBUTES,
        attribute_filter=attribute_filter,
        url_schemes=_TRUSTED_SCHEMES if trusted else _SCHEMES,
        set_tag_attribute_values=_TRUSTED_FORCED if trusted else _FORCED,
        generic_attribute_prefixes={"aria-"} if trusted else None,
        link_rel="noopener noreferrer",
        strip_comments=True,
        url_relative=relative,
    )


_UNTRUSTED = _cleaner(trusted=False)
_TRUSTED = _cleaner(trusted=True)

# ------------------------------------------------------------------ Markdown


def _table_cell(renderer, text, align=None, head=False):
    """mistune's table cell with align="..." instead of a style attribute, which the CSP blocks."""
    tag = "th" if head else "td"
    aligned = f' align="{align}"' if align in ("left", "center", "right") else ""
    return f"  <{tag}{aligned}>{text}</{tag}>\n"


def _new_markdown():
    # escape=False: HTML in the text reaches nh3, which decides what of it stays. mistune's own check
    # of link schemes runs too (javascript: becomes "#harmful-link").
    md = mistune.create_markdown(escape=False, hard_wrap=False, plugins=_PLUGINS)
    md.renderer.register("table_cell", _table_cell)
    return md


_local = threading.local()


def _markdown():
    """One mistune instance per thread: it keeps no state between calls, but doesn't promise it."""
    md = getattr(_local, "md", None)
    if md is None:
        md = _local.md = _new_markdown()
    return md


def _to_html(text):
    text = str(text or "")
    if len(text) > MAX_LENGTH:
        text = text[:MAX_LENGTH]
    return _markdown()(text)


def render(text: str, *, relative: Callable[[str], str | None] | None = None) -> str:
    """Untrusted Markdown (a README in a storage, a previewed .md file) as safe HTML."""
    html = _to_html(text)
    cleaner = _UNTRUSTED if relative is None else _cleaner(trusted=False, relative=relative)
    return cleaner.clean(html)


@functools.lru_cache(maxsize=16)
def render_trusted(text: str) -> str:
    """Markdown written by an admin (the announcement, readme rules typed in /manage/) as HTML: their
    HTML stays, without what the CSP would block anyway (see the top). Cached: the announcement is
    on every public page."""
    return _TRUSTED.clean(_to_html(text))
