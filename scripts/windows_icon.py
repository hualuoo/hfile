#!/usr/bin/env python3
"""Builds scripts/hfile.ico, the icon of the Windows program (scripts/build.sh), from HFile's logo, the site's
icon apps/web/static/web/favicon.svg: Chrome draws it at each size Windows shows, so that the small ones are
drawn for their size rather than scaled down from a large one.

    python scripts/windows_icon.py

Run it again when favicon.svg changes, and commit the new hfile.ico with it. It drives the system's Chrome with
Playwright (requirements-dev.txt), as the browser tests do; $HFILE_TEST_CHROME names another one.
"""

import base64
import io
import os
import sys
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LOGO = ROOT / "apps" / "web" / "static" / "web" / "favicon.svg"
ICON = ROOT / "scripts" / "hfile.ico"
# 16, 32 and 48 at 100 % to 250 % of the screen's scale (the window, the taskbar, Explorer's lists), and 256 for
# Explorer's large icons.
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def main():
    logo = "data:image/svg+xml;base64," + base64.b64encode(LOGO.read_bytes()).decode()
    chrome = os.environ.get("HFILE_TEST_CHROME")
    images = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**({"executable_path": chrome} if chrome else {"channel": "chrome"}))
        try:
            page = browser.new_page(viewport={"width": max(SIZES), "height": max(SIZES)}, device_scale_factor=1)
            for size in SIZES:
                page.set_content(f'<body style="margin:0"><img src="{logo}" width="{size}" height="{size}" style="display:block">')
                page.wait_for_function("document.images[0].complete && document.images[0].naturalWidth > 0")
                png = page.screenshot(clip={"x": 0, "y": 0, "width": size, "height": size}, omit_background=True)
                images.append(Image.open(io.BytesIO(png)).convert("RGBA"))
        finally:
            browser.close()
    # Pillow takes the largest as the image and the others for their own sizes (else it would scale it down).
    images[-1].save(ICON, sizes=[(size, size) for size in SIZES], append_images=images[:-1])
    print(f"{ICON}: {', '.join(map(str, SIZES))} px, {ICON.stat().st_size // 1024} KB")


if __name__ == "__main__":
    sys.exit(main())
