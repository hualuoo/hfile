"""The README screenshots of the demo site run.sh makes, in one language: NAME.png in Chinese, NAME.en.png in
English.

    PYTHON_WITH_PLAYWRIGHT -I shots.py SITE_URL PASSWORD_FILE zh|en OUT_DIR [files gallery-dark preview storage-wizard mobile]

Logs in as admin through the login page; the language comes from the hfile_language cookie, the theme from
localStorage. The browser is the system's Chrome ($CHROME, default /usr/bin/google-chrome).
"""

import os
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1].rstrip("/")
PASSWORD = Path(sys.argv[2]).read_text(encoding="utf-8").strip()
LANG = sys.argv[3]
OUT = Path(sys.argv[4])
WANTED = sys.argv[5:] or ["files", "gallery-dark", "preview", "storage-wizard", "mobile"]
SUFFIX = {"zh": ".png", "en": ".en.png"}[LANG]
T = {
    "zh": {"cookie": "zh-hans", "locale": "zh-CN", "photos": "照片", "code": "代码", "next": "下一步",
           "storage_name": "阿里云 OSS"},
    "en": {"cookie": "en", "locale": "en-US", "photos": "Photos", "code": "Code", "next": "Next",
           "storage_name": "Aliyun OSS"},
}[LANG]

IMAGES_LOADED = """() => [...document.images].filter(i => i.offsetParent !== null)
    .every(i => i.complete && i.naturalWidth > 0)"""


def page_for(browser, *, width, height, scale=1, mobile=False, theme="light"):
    context = browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale, is_mobile=mobile,
        has_touch=mobile, locale=T["locale"], color_scheme=theme, timezone_id="Asia/Shanghai")
    context.add_cookies([{"name": "hfile_language", "value": T["cookie"], "url": BASE}])
    context.add_init_script(f"try {{ localStorage.setItem('hfile-theme', '{theme}'); }} catch (e) {{}}")
    # No scrollbar, as with overlay scrollbars (macOS, phones): app.css keeps a gutter for one
    # (scrollbar-gutter: stable), which headless Chrome makes 15px wide. Through CSSOM: the site's CSP
    # (style-src 'self') blocks a <style>.
    context.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
        document.documentElement.style.setProperty('scrollbar-width', 'none', 'important');
    });""")
    page = context.new_page()
    page.goto(BASE + "/login/")
    page.fill("#login-username", "admin")
    page.fill("#login-password", PASSWORD)
    page.click("#login-form button[type=submit]")
    page.wait_for_url(lambda url: "/login" not in url)
    return page


def settle(page, rest=(5, 5)):
    """Everything loaded and drawn, and the mouse somewhere nothing reacts to it."""
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts.ready")
    page.wait_for_function(IMAGES_LOADED)
    page.mouse.move(*rest)
    page.wait_for_timeout(600)  # transitions (ripples, the dialog's fade-in)


def files(browser):
    page = page_for(browser, width=1440, height=800)
    page.goto(BASE + "/demo/")
    page.wait_for_selector(".files-row.files-item")
    settle(page, rest=(720, 440))
    return page


def gallery_dark(browser):
    page = page_for(browser, width=1440, height=900, theme="dark")
    page.goto(f"{BASE}/demo/{T['photos']}")
    page.wait_for_selector(".files-row.files-item")
    page.click("button.icon-button[aria-pressed]")  # the gallery view
    page.wait_for_function("() => document.querySelector('button.icon-button[aria-pressed]').getAttribute('aria-pressed') === 'true'")
    settle(page, rest=(40, 600))
    return page


def preview(browser):
    page = page_for(browser, width=1440, height=900)
    page.goto(f"{BASE}/demo/{T['code']}")
    row = page.locator(".files-row.files-item").filter(has=page.locator(".files-label", has_text="sync.py"))
    row.dblclick()
    page.wait_for_selector("dialog[open]")
    settle(page, rest=(20, 880))
    return page


def storage_wizard(browser):
    page = page_for(browser, width=1440, height=900)
    page.goto(BASE + "/manage/#storages/new?type=aliyun")
    page.wait_for_selector("[id^=sw-name]")
    page.fill("[id^=sw-name]", T["storage_name"])
    page.fill("[id^=sw-key]", "oss")
    page.get_by_role("button", name=T["next"]).click()
    page.wait_for_selector("text=Endpoint")
    settle(page, rest=(1300, 860))
    return page


def mobile(browser):
    page = page_for(browser, width=390, height=844, scale=2, mobile=True)
    page.goto(BASE + "/demo/")
    page.wait_for_selector(".files-row.files-item")
    settle(page, rest=(380, 120))
    return page


SHOTS = {"files": files, "gallery-dark": gallery_dark, "preview": preview, "storage-wizard": storage_wizard,
         "mobile": mobile}

OUT.mkdir(parents=True, exist_ok=True)
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(executable_path=os.environ.get("CHROME", "/usr/bin/google-chrome"))
    for name in WANTED:
        path = OUT / f"{name}{SUFFIX}"
        SHOTS[name](browser).screenshot(path=path)
        print(path)
    browser.close()
