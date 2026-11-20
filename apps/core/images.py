"""Uploaded images: the site's logo and its icon (站点设置). Ported from rolehomes' apps/core/images.py.

Every upload goes through the same checks before anything is stored:

* the file size, checked before the image is read;
* the real format, which Pillow reads from the file's bytes (the file name and the browser's content
  type are ignored). Pillow only tries the formats each kind allows, so its parsers for rare formats
  never run on uploads. No SVG: it can hold scripts, and /media/ would serve them from this site;
* the pixel count, so a small file that decodes to a huge image is refused;
* a full decode of the first frame, which catches truncated or corrupt files.

The stored file is then re-encoded from the decoded pixels, as PNG. That drops EXIF (GPS position and
so on) and anything appended to the file, and the stored file's extension always matches its format.
Animated GIF and WebP keep only their first frame.

A big picture takes a few hundred MB for a moment while it is decoded, so at most DECODING_AT_ONCE
uploads are decoded at the same time; the others wait their turn.
"""

from __future__ import annotations

import threading
import warnings
from dataclasses import dataclass
from io import BytesIO

from django.core.exceptions import ValidationError
from django.core.files.base import ContentFile
from django.utils.translation import gettext as _, gettext_lazy, pgettext
from PIL import Image, UnidentifiedImageError

MB = 1024 * 1024

_FORMAT_NAMES = {"JPEG": "JPG", "PNG": "PNG", "WEBP": "WebP", "GIF": "GIF", "ICO": "ICO"}
_MIME_TYPES = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
    "GIF": "image/gif",
    "ICO": "image/x-icon,image/vnd.microsoft.icon,.ico",
}


@dataclass(frozen=True)
class ImageRule:
    what: str  # the name used in messages, e.g. "网站图标"
    max_bytes: int
    formats: tuple[str, ...]  # Pillow format names
    min_side: int = 16
    max_pixels: int = 25_000_000

    @property
    def max_mb(self) -> str:
        return f"{self.max_bytes / MB:g}"

    @property
    def formats_text(self) -> str:
        names = [_FORMAT_NAMES[f] for f in self.formats]
        return _(", ").join(names[:-1]) + pgettext("between Latin words", " or ") + names[-1] if len(names) > 1 else names[0]

    @property
    def accept(self) -> str:
        """For <input type="file" accept="...">."""
        return ",".join(_MIME_TYPES[f] for f in self.formats)


# At most 1 MB each.
LOGO = ImageRule(gettext_lazy("logo"), 1 * MB, ("PNG", "WEBP", "JPEG", "GIF"))
FAVICON = ImageRule(gettext_lazy("site icon"), 1 * MB, ("PNG", "ICO", "WEBP", "JPEG", "GIF"), max_pixels=4_000_000)
RULES = {"logo": LOGO, "favicon": FAVICON}

# Pillow's own limit, for every picture this program decodes: the largest of the rules. An .ico file
# has its largest picture decoded inside Image.open(), before open_image() can look at the size.
# Pillow refuses a picture (DecompressionBombError) with more than twice MAX_IMAGE_PIXELS, and only
# warns between once and twice. Turning that warning into an error needs warnings.catch_warnings(),
# which isn't safe with two uploads decoding at once, so the limit is set to half and the warning
# (for pictures the rules allow) is ignored.
_LARGEST = max(rule.max_pixels for rule in RULES.values())
Image.MAX_IMAGE_PIXELS = _LARGEST // 2
warnings.filterwarnings("ignore", category=Image.DecompressionBombWarning)

# 16-bit grey pictures (PNG): Pillow keeps them as 32-bit numbers, and converting them to RGB would
# cut every value above 255 (nearly everything comes out white).
_WIDE_GREY = {"I", "I;16", "I;16L", "I;16B", "I;16N"}

DECODING_AT_ONCE = 2
_decoding = threading.BoundedSemaphore(DECODING_AT_ONCE)

# EXIF orientation -> the turn that makes the picture upright (what ImageOps.exif_transpose does).
_ORIENTATION = 0x0112
_UPRIGHT = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _upright(source):
    """The turn the picture's EXIF orientation asks for, or None. Damaged EXIF counts as none:
    ImageOps.exif_transpose() writes the EXIF out again, and fails on some (a 500)."""
    try:
        return _UPRIGHT.get(int(source.getexif().get(_ORIENTATION, 1)))
    except Exception:
        return None


@dataclass(frozen=True)
class ProcessedImage:
    file: ContentFile
    width: int
    height: int


def open_image(uploaded, rule: ImageRule) -> Image.Image:
    """Run the checks above and return the decoded first frame, EXIF rotation applied."""
    size = getattr(uploaded, "size", None)
    if size == 0:
        raise ValidationError(_("The %(what)s is an empty file.") % {"what": rule.what})
    if size is not None and size > rule.max_bytes:
        raise ValidationError(_("The %(what)s can't be larger than %(max_mb)s MB; this file is %(size)s MB.") % {"what": rule.what, "max_mb": rule.max_mb, "size": format(size / MB, ".1f")})
    uploaded.seek(0)
    try:
        with _decoding:
            with Image.open(uploaded, formats=list(rule.formats)) as source:
                if source.width * source.height > rule.max_pixels:
                    raise ValidationError(_("The resolution of the %(what)s is too high (%(width)s×%(height)s).") % {"what": rule.what, "width": source.width, "height": source.height})
                if min(source.size) < rule.min_side:
                    raise ValidationError(_("The %(what)s is too small; it must be at least %(min_side)s×%(min_side)s.") % {"what": rule.what, "min_side": rule.min_side})
                turn = _upright(source)
                source.load()
                # Transparency also comes as one colour marked transparent (PNG tRNS), in grey and RGB pictures.
                has_alpha = source.has_transparency_data
                try:
                    if source.mode in _WIDE_GREY:
                        source = source.convert("I").point(lambda v: v * (1 / 257)).convert("L")
                    image = source.convert("RGBA" if has_alpha else "RGB")  # a copy that outlives the file
                except (OSError, ValueError) as exc:
                    raise ValidationError(_("Can't handle the color format of this %(what)s. Please use a plain PNG or JPG.") % {"what": rule.what}) from exc
            return image.transpose(turn) if turn is not None else image
    except ValidationError:
        raise
    except UnidentifiedImageError as exc:
        raise ValidationError(_("The %(what)s must be a %(formats_text)s image, and this file isn't.") % {"what": rule.what, "formats_text": rule.formats_text}) from exc
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ValidationError(_("The resolution of the %(what)s is too high.") % {"what": rule.what}) from exc
    except Exception as exc:  # a damaged file can make a decoder raise nearly anything
        raise ValidationError(_("Can't read this %(what)s; the file may be damaged.") % {"what": rule.what}) from exc


def _encode(image: Image.Image, fmt: str, name: str, **options) -> ProcessedImage:
    buffer = BytesIO()
    image.save(buffer, format=fmt, **options)
    return ProcessedImage(ContentFile(buffer.getvalue(), name=name), image.width, image.height)


LOGO_MAX_WIDTH = 640
LOGO_MAX_HEIGHT = 160  # shown 32 px high, so this is sharp on 4x screens too
FAVICON_SIZE = 192
FAVICON_MAX_RATIO = 4  # the long side at most 4 times the short one: a thinner strip isn't an icon


def process_logo(uploaded) -> ProcessedImage:
    """Shrink to fit 640×160 and save as PNG (lossless: logos have sharp edges)."""
    image = open_image(uploaded, LOGO)
    image.thumbnail((LOGO_MAX_WIDTH, LOGO_MAX_HEIGHT), Image.Resampling.LANCZOS)
    return _encode(image, "PNG", "logo.png", optimize=True)


def process_favicon(uploaded) -> ProcessedImage:
    """Shrink to fit 192×192, pad to a square with transparency, save as PNG.

    Shrinking comes first: padding a long, thin picture first (250000×16 is within the pixel limit)
    would make a square of billions of pixels. From an .ico file Pillow reads the largest size it
    contains.
    """
    image = open_image(uploaded, FAVICON).convert("RGBA")
    if max(image.size) > FAVICON_MAX_RATIO * min(image.size):
        raise ValidationError(
            _("The site icon should be close to square, its long side at most "
              "%(ratio)s times the short one. This one is %(width)s×%(height)s.") % {"ratio": FAVICON_MAX_RATIO, "width": image.width, "height": image.height}
        )
    image.thumbnail((FAVICON_SIZE, FAVICON_SIZE), Image.Resampling.LANCZOS)
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return _encode(square, "PNG", "favicon.png", optimize=True)
