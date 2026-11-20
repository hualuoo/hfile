"""Tests in a real browser: Playwright drives the system's Chrome against the site that
StaticLiveServerTestCase serves from this process, to see what the pages' scripts do.

BrowserTestCase starts one browser for a class. new_context() is a browser profile of its own (cookies,
localStorage) in a language and theme, at a fixed size, in the site's time zone, with animations reduced,
and logged in when given a user: by a session cookie, not through the login page. open() loads an address
in it and returns once the page has stopped asking the site for things. Each page records what goes wrong
in it: uncaught errors, errors in the console (a script's, what the CSP refused, a file that didn't load),
requests that failed and the site's answers of 400 and up; assertClean() fails with what it recorded.
local_storage() makes a local storage with files; row() and file_action() find and work a file browser's
rows. `expect` is Playwright's, waiting as long as the pages do (None without Playwright, whose tests are
skipped).

The tests are tagged "browser": `manage.py test --exclude-tag browser` leaves them out, `--tag browser`
runs only them. Without Playwright (requirements-dev.txt) or a Chrome they are skipped, unless
HFILE_BROWSER_TESTS=required (CI), which makes that an error. The browser is the installed Google Chrome
(Playwright's "chrome" channel) or $HFILE_TEST_CHROME. With $HFILE_BROWSER_ARTIFACTS set, a test that
fails leaves screenshots of its pages in that folder.

The module's name keeps it out of the program: scripts/build.sh leaves test modules out.
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from django.conf import settings
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.core.servers.basehttp import WSGIServer
from django.test import Client, override_settings, tag
from django.test.testcases import LiveServerThread

from apps.accounts.models import User
from apps.files.tests.helpers import EVERYTHING
from apps.storages import config, drivers, instances
from apps.storages.models import Grant, StorageSource

from .navigation import SECTIONS

LOCALES = {"zh-hans": "zh-CN", "en": "en-US"}
THEME_KEY = "hfile-theme"  # apps/web/static/web/js/theme.js
TIMEOUT_MS = 15_000

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import expect, sync_playwright
except ImportError:  # requirements-dev.txt
    sync_playwright = expect = None
else:
    expect.set_options(timeout=TIMEOUT_MS)


class _OneRequestAtATime(WSGIServer):
    """The live server without a thread per request. SQLite's test database is in memory: one connection,
    which Django's live server shares with all its threads, and a page asking for several things at once
    then had a session read halfway (a 401, or a 500 of an IndexError). Django closes the connection after
    every answer of a server without threads, so the browser doesn't wait on one it keeps open."""

    def __init__(self, *args, connections_override=None, **kwargs):  # the thread itself has them
        super().__init__(*args, **kwargs)


class _ServerThread(LiveServerThread):
    server_class = _OneRequestAtATime


@tag("browser")
class BrowserTestCase(StaticLiveServerTestCase):
    serialized_rollback = True  # the built-in users (accounts.0002), which the flush after a test takes
    server_thread_class = _ServerThread

    @classmethod
    def setUpClass(cls):
        reason = cls._start_browser()
        if reason:
            if os.environ.get("HFILE_BROWSER_TESTS") == "required":
                raise RuntimeError(f"HFILE_BROWSER_TESTS=required, but {reason}")
            raise unittest.SkipTest(reason)
        try:
            super().setUpClass()
        except BaseException:
            cls._stop_browser()
            raise

    @classmethod
    def tearDownClass(cls):
        try:
            super().tearDownClass()
        finally:
            cls._stop_browser()

    @classmethod
    def _start_browser(cls) -> str | None:
        """None once the browser runs, else why it doesn't."""
        cls._playwright = cls.browser = None
        if sync_playwright is None:
            return "Playwright isn't installed (requirements-dev.txt)"
        # Playwright's synchronous API keeps an event loop in this thread, so Django would take every query
        # the test makes for one made from async code and refuse it. There is no async code here.
        cls._async_unsafe = os.environ.get("DJANGO_ALLOW_ASYNC_UNSAFE")
        os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = "true"
        cls._playwright = sync_playwright().start()
        chrome = os.environ.get("HFILE_TEST_CHROME")
        try:
            cls.browser = cls._playwright.chromium.launch(**({"executable_path": chrome} if chrome else {"channel": "chrome"}))
        except PlaywrightError as error:
            cls._stop_browser()
            return f"Chrome didn't start: {str(error).strip().splitlines()[0]}"
        return None

    @classmethod
    def _stop_browser(cls):
        if cls.browser is not None:
            cls.browser.close()
        if cls._playwright is not None:
            cls._playwright.stop()
            if cls._async_unsafe is None:
                os.environ.pop("DJANGO_ALLOW_ASYNC_UNSAFE", None)
            else:
                os.environ["DJANGO_ALLOW_ASYNC_UNSAFE"] = cls._async_unsafe
        cls._playwright = cls.browser = None

    def setUp(self):
        super().setUp()
        self._problems = {}  # page -> what went wrong in it, not yet checked
        self._failed = False
        self._shots = 0

    def _callTestMethod(self, method):
        try:
            super()._callTestMethod(method)
        except BaseException:
            self._failed = True  # for _keep(), which runs after it
            raise

    # -- data

    def local_storage(self, files: dict[str, bytes], *, users=(), key: str = "disk") -> Path:
        """A local storage `key` on a new folder holding `files` ({name: bytes}), where each of `users` may do
        everything. Returns the folder."""
        instances.close_all()  # a storage of an earlier test may have had this one's id
        self.addCleanup(instances.close_all)
        root = Path(tempfile.mkdtemp(prefix="hfile-browser-"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        for name, data in files.items():
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(data)
        storage = StorageSource.objects.create(key=key, name=key.title(), type="local",
                                               config=config.encode(drivers.driver_class("local"), {"root": str(root)}))
        for user in users:
            Grant.objects.create(user=user, storage=storage, permissions=EVERYTHING, root_path="/", enabled=True)
        return root

    # -- pages

    def new_context(self, *, user=None, language="zh-hans", theme="light", width=1280, height=800):
        """A browser profile of its own (see the module's docstring); `user` is logged in."""
        context = self.browser.new_context(
            viewport={"width": width, "height": height}, locale=LOCALES[language], timezone_id=settings.TIME_ZONE,
            color_scheme=theme, reduced_motion="reduce")
        self.addCleanup(context.close)
        context.set_default_timeout(TIMEOUT_MS)
        context.set_default_navigation_timeout(TIMEOUT_MS)
        cookies = [{"name": settings.LANGUAGE_COOKIE_NAME, "value": language, "url": self.live_server_url}]
        if user is not None:
            client = Client()
            client.force_login(user)
            session = client.cookies[settings.SESSION_COOKIE_NAME].value
            cookies.append({"name": settings.SESSION_COOKIE_NAME, "value": session, "url": self.live_server_url})
        context.add_cookies(cookies)
        context.add_init_script(f"try {{ localStorage.setItem('{THEME_KEY}', '{theme}'); }} catch (e) {{}}")
        return context

    def open(self, context, path: str):
        """A new page of `context` at `path` (the site's), once it has stopped asking the site for things."""
        page = context.new_page()
        problems = self._problems[page] = []
        site = self.live_server_url

        def response(answer):
            if answer.status >= 400 and answer.url.startswith(site):
                problems.append(f"{answer.status} {answer.request.method} {answer.url}")

        def failed(request):
            if request.failure != "net::ERR_ABORTED":  # a navigation or fetch the page itself stopped
                problems.append(f"failed: {request.method} {request.url} ({request.failure})")

        page.on("pageerror", lambda error: problems.append(f"uncaught: {error}"))
        page.on("console", lambda message: message.type == "error" and problems.append(f"console: {message.text}"))
        page.on("response", response)
        page.on("requestfailed", failed)
        self.addCleanup(self._keep, page)  # before the context closes: cleanups run last first
        page.goto(site + path, wait_until="networkidle")
        return page

    def assertClean(self, page, allow=()):
        """Nothing went wrong in `page` since it opened or was last checked, but what the regular
        expressions `allow` find."""
        problems = [text for text in self._problems.pop(page, []) if not any(re.search(pattern, text) for pattern in allow)]
        self._problems[page] = []
        if problems:
            self._screenshot(page)
            self.fail(f"{page.url}:\n  " + "\n  ".join(problems))

    # -- the file browser (static/files/js/files.js), as the Chinese pages say it

    def row(self, page, name: str):
        """The row of `name` in the file browser on `page`."""
        return page.locator(".files-item").filter(has=page.locator(".files-label", has_text=re.compile(rf"^{re.escape(name)}$")))

    def file_action(self, page, name: str, action: str):
        """Runs the action labelled `action` on `name` in the file browser on `page`, from its row's 更多操作."""
        page.get_by_role("button", name=f"{name} 的更多操作", exact=True).click()
        page.get_by_role("menu", name="操作").get_by_role("menuitem", name=action, exact=True).click()

    def _keep(self, page):
        """After a failed test: a screenshot of each page still open, with $HFILE_BROWSER_ARTIFACTS."""
        if self._failed and not page.is_closed():
            self._screenshot(page)

    def _screenshot(self, page):
        folder = os.environ.get("HFILE_BROWSER_ARTIFACTS")
        if not folder:
            return
        self._shots += 1
        path = Path(folder) / f"{self.id()}-{self._shots}.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            page.screenshot(path=path, full_page=True)
        except PlaywrightError:  # a page that broke: the failure says why, the screenshot is extra
            pass


class PagesTests(BrowserTestCase):
    """Every page opens without anything going wrong in it, in both languages and themes."""

    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.local_storage({"docs/README.md": b"# Docs\n\nSome *text*.\n", "docs/a.txt": b"hello\n", "top.txt": b"top\n"},
                           users=[self.admin])

    def test_every_page(self):
        visitor = ["/login/", "/"]
        admin = ["/", "/disk/", "/disk/docs/", "/shares/"] + [f"/manage/#{section.key}" for section in SECTIONS]
        for language, theme in (("zh-hans", "light"), ("en", "dark")):
            for user, paths in ((None, visitor), (self.admin, admin)):
                context = self.new_context(user=user, language=language, theme=theme)
                for path in paths:
                    with self.subTest(path, language=language, theme=theme, logged_in=user is not None):
                        page = self.open(context, path)
                        self.assertClean(page)
                        page.close()

    def test_an_error_page(self):
        page = self.open(self.new_context(user=self.admin), "/no-such-storage/")
        self.assertClean(page, allow=[r"^404 GET .*/no-such-storage/$", r"^console: Failed to load resource: .* 404"])

    def test_the_check_sees_what_goes_wrong(self):
        page = self.open(self.new_context(user=self.admin), "/disk/")
        with page.expect_event("pageerror"):
            page.evaluate("setTimeout(() => { throw new Error('thrown on purpose'); })")
        with page.expect_event("console"):
            page.evaluate("console.error('logged on purpose')")
        with page.expect_response(lambda answer: answer.url.endswith("/api/v1/no-such-thing/")):
            page.evaluate("fetch('/api/v1/no-such-thing/')")
        with page.expect_event("console"):  # the CSP's refusal
            page.evaluate("document.body.append(Object.assign(document.createElement('script'), {textContent: 'window.x = 1'}))")
        with self.assertRaises(AssertionError) as failed:
            self.assertClean(page)
        for found in ("uncaught: thrown on purpose", "console: logged on purpose", "404 GET ", "Content Security Policy"):
            self.assertIn(found, str(failed.exception))
        self.assertClean(page)  # what was reported is gone

    @override_settings(DEBUG=True)  # the page is there with DEBUG only (apps/web/views.py)
    def test_the_components_page(self):
        self.assertClean(self.open(self.new_context(user=self.admin), "/components/"))
