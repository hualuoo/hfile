"""What kind of file a name is (contract W3, decision D23): the browser opens, and draws, a file by it.

    kind_of("song.MP3", SiteSettings.load())  ->  "audio"

The kinds: image, video, audio, text, markdown, pdf, office, kkfileview, archive, executable, document,
three_d and other. The extension is what follows the last "." of the name, in lower case; a name
without a dot (or ".bashrc") has none. The first kind whose suffixes hold the extension wins, in this
order (inventory section 7.1):

    image, video, audio, text (markdown), executable, archive, pdf, office, document, kkfileview, three_d

- image, video, audio, text, office and kkfileview follow the site's lists (显示设置 → 预览类型):
  SiteSettings.preview_suffixes() gives the admin's list, the default one when it is empty, and
  nothing when the kind's switch is off. The rest are fixed.
- markdown is the text kind's md (and markdown, which goes with md): text files the Markdown
  viewer opens. Decided by the extension, not by ".md" anywhere in the name.
- three_d comes after kkfileview: HFile has no 3D viewer (D20), so 3D models that kkFileView takes
  (its default list has them) go there, and only the others are three_d (downloaded).
"""

from __future__ import annotations

KINDS = ("image", "video", "audio", "text", "markdown", "pdf", "office", "kkfileview", "archive",
         "executable", "document", "three_d", "other")

EXECUTABLE = frozenset("exe dll com vbs msi apk deb rpm".split())
ARCHIVE = frozenset("7z zip rar tar gz tgz bz2 xz zst".split())
PDF = frozenset({"pdf"})
DOCUMENT = frozenset("pages epub numbers keynote".split())
THREE_D = frozenset("dae fbx gltf glb obj ply stl 3ds 3mf".split())
MARKDOWN = frozenset({"md", "markdown"})


def extension(name: str) -> str:
    """"Photo.JPG" -> "jpg"; "" for "README", ".bashrc" and "a." ."""
    base = name.rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    if not dot or not stem or not ext:
        return ""
    return ext.lower()


class Kinds:
    """The suffix lists of one SiteSettings row, ready to look names up (one per listing)."""

    def __init__(self, site_settings):
        lists = site_settings.preview_suffixes()
        self.image = frozenset(lists.get("image", ()))
        self.video = frozenset(lists.get("video", ()))
        self.audio = frozenset(lists.get("audio", ()))
        self.text = frozenset(lists.get("text", ()))
        self.office = frozenset(lists.get("office", ()))
        self.kkfileview = frozenset(lists.get("kkfileview", ()))

    def of(self, name: str) -> str:
        ext = extension(name)
        if not ext:
            return "other"
        if ext in self.image:
            return "image"
        if ext in self.video:
            return "video"
        if ext in self.audio:
            return "audio"
        if ext in self.text or (ext == "markdown" and "md" in self.text):
            return "markdown" if ext in MARKDOWN else "text"
        if ext in EXECUTABLE:
            return "executable"
        if ext in ARCHIVE:
            return "archive"
        if ext in PDF:
            return "pdf"
        if ext in self.office:
            return "office"
        if ext in DOCUMENT:
            return "document"
        if ext in self.kkfileview:
            return "kkfileview"
        if ext in THREE_D:
            return "three_d"
        return "other"


def kind_of(name: str, site_settings) -> str:
    """The kind of a file name, by the site's settings (a SiteSettings row)."""
    return Kinds(site_settings).of(name)
