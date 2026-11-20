"""apps.files.kinds (W3): the kind of a name, by the site's suffix lists and switches (D23)."""

from django.test import SimpleTestCase

from apps.files.kinds import KINDS, extension, kind_of
from apps.siteconf.models import SiteSettings


def site(**fields):
    return SiteSettings(**fields)  # not saved: kind_of only reads it


class KindTests(SimpleTestCase):
    def test_the_built_in_lists(self):
        default = site()
        cases = {
            "a.JPG": "image", "x.webp": "image", "film.mp4": "video", "s.m3u8": "video", "song.Mp3": "audio",
            "x.py": "text", "notes.txt": "text", "README.md": "markdown", "guide.markdown": "markdown",
            "setup.exe": "executable", "a.zip": "archive", "a.tar.gz": "archive", "paper.pdf": "pdf",
            "sheet.xlsx": "office", "deck.pptx": "office", "data.csv": "office", "book.epub": "document",
            "draw.dwg": "kkfileview", "pic.svg": "kkfileview", "model.stl": "kkfileview", "doc.rtf": "kkfileview",
            "README": "other", ".bashrc": "other", "a.": "other", "x.unknownext": "other",
        }
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(kind_of(name, default), kind)
                self.assertIn(kind, KINDS)

    def test_markdown_by_the_extension_only(self):
        default = site()
        self.assertEqual(kind_of("a.md.txt", default), "text")
        self.assertEqual(kind_of("x.mdx", default), "other")
        self.assertEqual(kind_of("x.MD", default), "markdown")

    def test_the_admins_lists(self):
        mine = site(image_suffixes="png,heic", text_suffixes="txt,log")
        self.assertEqual(kind_of("a.heic", mine), "image")
        self.assertEqual(kind_of("a.jpg", mine), "other")  # his list replaces the default one
        self.assertEqual(kind_of("a.log", mine), "text")
        self.assertEqual(kind_of("a.md", mine), "other")  # md is not in his text list
        self.assertEqual(kind_of("a.markdown", mine), "other")
        self.assertEqual(kind_of("a.py", mine), "other")

    def test_a_switch_turns_a_kind_off(self):
        self.assertEqual(kind_of("a.jpg", site(image_preview=False)), "other")
        self.assertEqual(kind_of("a.md", site(text_preview=False)), "other")
        self.assertEqual(kind_of("a.docx", site(office_preview=False)), "other")
        self.assertEqual(kind_of("a.dwg", site(kkfileview_preview=False)), "other")
        # 3D models kkFileView doesn't take are three_d (downloaded: HFile has no 3D viewer, D20).
        self.assertEqual(kind_of("model.stl", site(kkfileview_preview=False)), "three_d")
        # The fixed kinds have no switch.
        self.assertEqual(kind_of("a.pdf", site(text_preview=False, kkfileview_preview=False)), "pdf")

    def test_kkfileview_follows_its_list(self):
        self.assertEqual(kind_of("a.xyz", site(kkfileview_suffixes="xyz")), "kkfileview")
        self.assertEqual(kind_of("a.dwg", site(kkfileview_suffixes="xyz")), "other")

    def test_extension(self):
        self.assertEqual(extension("photo.JPG"), "jpg")
        self.assertEqual(extension("a.b.c"), "c")
        self.assertEqual(extension("noext"), "")
        self.assertEqual(extension(".hidden"), "")
        self.assertEqual(extension("dir/a.txt"), "txt")
