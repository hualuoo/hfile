"""Drawing the captcha pictures with the `captcha` package (PyPI captcha 0.7.1, BSD licence), as
rolehomes does.

The library turns, bends and shifts each character and scatters dots and arcs over them. Pictures
are drawn at twice the size they are shown at, so they stay sharp on phones.
"""

import base64
import secrets
from io import BytesIO

from captcha.image import ImageCaptcha, random_color
from PIL import Image
from PIL.ImageDraw import Draw
from PIL.ImageFilter import SMOOTH

SCALE = 2
HEIGHT = 60  # CSS pixels; the PNG is SCALE times larger
FONT_SIZES = (38, 43, 48)  # the library's are 42–56, which cut letters off at the edges
DOTS = 30
ARCS = 2


def width_for(length):
    """160 CSS pixels for 4 characters (the library's default size), 30 more for each one after that."""
    return 40 + 30 * length


class _Picture(ImageCaptcha):
    """The library's picture with its default distortion, dark text on a light background, and arcs
    for noise."""

    character_offset_dx = (0, 4 * SCALE)
    character_offset_dy = (0, 6 * SCALE)

    def __init__(self, length):
        super().__init__(
            width=width_for(length) * SCALE,
            height=HEIGHT * SCALE,
            font_sizes=tuple(size * SCALE for size in FONT_SIZES),
        )

    def create_captcha_image(self, chars, color, background):
        # The library's layout, with one change: it makes each character's mask from the
        # character's brightness, so dark text came out faint. Its alpha channel keeps it solid.
        image = Image.new("RGB", (self._width, self._height), background)
        draw = Draw(image)
        glyphs = []
        for c in chars:
            if secrets.randbits(32) / (2**32) > self.word_space_probability:
                glyphs.append(self._draw_character(" ", draw, color))
            glyphs.append(self._draw_character(c, draw, color))

        text_width = sum(glyph.size[0] for glyph in glyphs)
        width = max(text_width, self._width)
        image = image.resize((width, self._height))
        average = int(text_width / len(chars))
        rand = int(self.word_offset_dx * average)
        offset = int(average * 0.1)
        for glyph in glyphs:
            w, h = glyph.size
            image.paste(glyph, (offset, int((self._height - h) / 2)), glyph.getchannel("A"))
            offset = offset + w - secrets.randbelow(rand + 1)
        if width > self._width:
            image = image.resize((self._width, self._height))
        return image

    def _arc(self, image, color):
        # ImageCaptcha.create_noise_curve, with a line width.
        w, h = image.size
        x1 = secrets.randbelow(int(w / 5) + 1)
        x2 = secrets.randbelow(w - int(w / 5) + 1) + int(w / 5)
        y1 = secrets.randbelow(h - 2 * int(h / 5) + 1) + int(h / 5)
        y2 = secrets.randbelow(h - y1 - int(h / 5) + 1) + y1
        start = secrets.randbelow(21)
        end = secrets.randbelow(41) + 160
        Draw(image).arc([x1, y1, x2, y2], start, end, fill=color, width=SCALE)

    def generate_image(self, chars, bg_color=None, fg_color=None):
        # Dark text on a light background: every channel at most 110 keeps the contrast above 4.5:1.
        background = bg_color or random_color(238, 255)
        color = fg_color or random_color(10, 110, 255)
        image = self.create_captcha_image(chars, color, background)
        self.create_noise_dots(image, color, width=3 * SCALE, number=DOTS)
        for _ in range(ARCS):
            self._arc(image, color)
        return image.filter(SMOOTH)


def render(text):
    """The picture of `text` as PNG bytes, SCALE times the size given by width_for() and HEIGHT."""
    picture = _Picture(len(text))
    # 32 colours look the same and make the file about a third of the size (5 KB instead of 17 KB).
    image = picture.generate_image(text).quantize(32, dither=Image.Dither.NONE)
    buffer = BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def data_uri(png):
    """For <img src>: the API sends the picture inline, so it is never fetched (or drawn again) by URL."""
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
