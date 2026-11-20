"""The checks and re-encoding of uploaded logos and icons (apps/core/images.py)."""

import struct
from io import BytesIO
from unittest import mock

from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from PIL import Image

from apps.core import images
from apps.core.images import FAVICON, LOGO, process_favicon, process_logo

PROCESSORS = [(LOGO, process_logo), (FAVICON, process_favicon)]


def upload(image, fmt, name="x.png", **save_options):
    buffer = BytesIO()
    image.save(buffer, fmt, **save_options)
    return SimpleUploadedFile(name, buffer.getvalue(), content_type="image/png")


def rgb(width, height, fmt="PNG", **options):
    return upload(Image.new("RGB", (width, height), (120, 160, 200)), fmt, **options)


def decoded(result):
    return Image.open(BytesIO(result.file.read()))


class ImageCheckTests(SimpleTestCase):
    def assertRefused(self, process, file, fragment):
        with self.assertRaises(ValidationError) as caught:
            process(file)
        self.assertIn(fragment, caught.exception.messages[0])

    def test_files_that_are_not_images_are_refused(self):
        fakes = {
            "text named .png": b"not an image",
            "svg with a script": b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
            "html": b"<!doctype html><script>alert(1)</script>",
            "zip": b"PK\x03\x04" + b"\x00" * 64,
        }
        for rule, process in PROCESSORS:
            for label, data in fakes.items():
                with self.subTest(rule.what, file=label):
                    self.assertRefused(process, SimpleUploadedFile("x.png", data, content_type="image/png"), "格式")

    def test_size_limits(self):
        for rule, process in PROCESSORS:
            with self.subTest(rule.what):
                self.assertEqual(rule.max_bytes, 1024 * 1024)
                big = SimpleUploadedFile("x.png", b"\x89PNG" + b"0" * rule.max_bytes, content_type="image/png")
                self.assertRefused(process, big, "不能超过 1 MB")
                self.assertRefused(process, SimpleUploadedFile("x.png", b""), "空文件")

    def test_only_the_listed_formats_are_read(self):
        self.assertRefused(process_logo, rgb(64, 64, "BMP"), "PNG、WebP、JPG 或 GIF")
        self.assertRefused(process_logo, rgb(64, 64, "ICO"), "格式")
        self.assertRefused(process_favicon, rgb(64, 64, "TIFF"), "格式")
        self.assertEqual(process_favicon(rgb(64, 64, "ICO", sizes=[(32, 32), (64, 64)])).width, 64)
        for fmt in ("PNG", "JPEG", "WEBP", "GIF"):
            with self.subTest(fmt):
                self.assertEqual(decoded(process_logo(rgb(100, 40, fmt))).format, "PNG")

    def test_resolution_limits(self):
        # 6000×5000 one-bit pixels are a small file but would be 30 megapixels once decoded.
        huge = upload(Image.new("1", (6000, 5000)), "PNG")
        self.assertRefused(process_logo, huge, "分辨率太高")
        self.assertRefused(process_logo, rgb(8, 40), "太小")
        self.assertRefused(process_favicon, rgb(8, 8), "太小")
        self.assertEqual(Image.MAX_IMAGE_PIXELS * 2, max(rule.max_pixels for rule in images.RULES.values()))

    def test_truncated_file_is_refused(self):
        whole = rgb(400, 400, "JPEG").read()
        cut = SimpleUploadedFile("x.jpg", whole[:600], content_type="image/jpeg")
        self.assertRefused(process_logo, cut, "损坏")

    def test_reencoding_applies_rotation_and_drops_exif_and_trailing_bytes(self):
        exif = Image.Exif()
        exif[0x0112] = 6  # Orientation: rotate 90° when shown
        exif[0x010F] = "SecretPhone"  # Make
        data = rgb(400, 100, "JPEG", exif=exif.tobytes()).read() + b"<script>alert(1)</script>"
        result = process_logo(SimpleUploadedFile("x.jpg", data, content_type="image/jpeg"))
        self.assertEqual((result.width, result.height), (40, 160))  # shown as 100×400, fitted into 640×160
        stored = result.file.read()
        self.assertEqual(stored[:8], b"\x89PNG\r\n\x1a\n")
        for marker in (b"SecretPhone", b"Exif", b"EXIF", b"<script>"):
            self.assertNotIn(marker, stored)

    def test_damaged_exif_counts_as_none(self):
        with mock.patch("PIL.Image.Image.getexif", side_effect=struct.error("bad")):
            image = images.open_image(rgb(64, 64), LOGO)
        self.assertEqual(image.size, (64, 64))

    def test_logo_keeps_transparency(self):
        logo = upload(Image.new("RGBA", (2000, 400), (255, 0, 0, 0)), "PNG")
        result = process_logo(logo)
        image = decoded(result)
        self.assertEqual((result.width, result.height, image.format, image.mode), (640, 128, "PNG", "RGBA"))
        self.assertEqual(image.getpixel((0, 0))[3], 0)
        small = process_logo(rgb(100, 30))
        self.assertEqual((small.width, small.height), (100, 30))  # never enlarged

    def test_favicon_is_padded_to_a_square(self):
        result = process_favicon(rgb(300, 150))
        image = decoded(result)
        self.assertEqual((image.size, image.format), ((192, 192), "PNG"))
        self.assertEqual(image.getpixel((96, 2))[3], 0)  # transparent band above the picture
        self.assertEqual(image.getpixel((96, 96))[3], 255)

    def test_favicon_must_be_nearly_square(self):
        self.assertRefused(process_favicon, rgb(2000, 16), "接近正方形")
        self.assertEqual(decoded(process_favicon(rgb(64, 16))).size, (64, 64))  # 4:1 is still an icon

    def test_wide_grey_and_one_colour_transparency(self):
        image = images.open_image(upload(Image.new("I;16", (64, 64), 30000), "PNG"), LOGO)
        self.assertEqual(image.getpixel((1, 1)), (116, 116, 116))  # 30000/65535 of white, not white
        image = images.open_image(upload(Image.new("L", (64, 64), 10), "PNG", transparency=10), LOGO)
        self.assertEqual((image.mode, image.getpixel((1, 1))[3]), ("RGBA", 0))

    def test_rule_texts(self):
        self.assertEqual((LOGO.max_mb, LOGO.formats_text), ("1", "PNG、WebP、JPG 或 GIF"))
        self.assertIn("image/x-icon", FAVICON.accept)
        self.assertNotIn("svg", FAVICON.accept + LOGO.accept)
