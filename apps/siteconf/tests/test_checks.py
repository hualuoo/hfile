"""[site] admin_path against the storage keys and the path-link prefix (apps/siteconf/checks.py), and the
names of uploads."""

from io import StringIO
from unittest import mock

from django.conf import settings
from django.core import checks
from django.db import DatabaseError
from django.test import TestCase

from apps.core import reserved
from apps.core.testing import admin_url
from apps.upgrades import startup

from .. import checks as site_checks
from ..models import SiteSettings
from .helpers import users


class AddressCheckTests(TestCase):
    def test_no_warning_normally(self):
        self.assertEqual(site_checks.addresses_check(None), [])

    def test_the_admin_path_against_the_link_prefix(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"direct_link_prefix": settings.ADMIN_PATH.upper()})
        found = site_checks.addresses_check(None)
        self.assertEqual([message.id for message in found], ["siteconf.W001"])
        self.assertIsInstance(found[0], checks.Warning)
        self.assertIn("直链的地址前缀", found[0].msg)
        self.assertIn("[site] admin_path", found[0].msg)

    def test_the_admin_path_against_storage_keys(self):
        def storage_keys(name):
            return f"/{name}/ 是存储源的别名。" if name.lower() == settings.ADMIN_PATH.lower() else ""

        reserved.register_check(storage_keys)
        self.addCleanup(reserved._checks.remove, storage_keys)
        self.assertIn("是存储源的别名", site_checks.address_problem())
        with mock.patch.object(reserved, "_names", reserved._names | {settings.ADMIN_PATH.lower()}):
            self.assertIn("网站自己要用的地址", site_checks.address_problem())

    def test_before_the_tables_exist(self):
        with mock.patch.object(reserved, "admin_path_problem", side_effect=DatabaseError("no such table")):
            self.assertEqual(site_checks.address_problem(), "")

    def test_serve_stops_runserver_warns(self):
        out = StringIO()
        # It closes the connection for the server's threads: not the test's own (MySQL would drop it).
        with mock.patch.object(site_checks, "address_problem", return_value="冲突了。"), \
                mock.patch.object(site_checks, "connection") as connection:
            with self.assertRaises(SystemExit):
                site_checks.stop_on_conflict(out)
            self.assertIn("没有启动。冲突了。", out.getvalue())
            with mock.patch.object(startup, "LENIENT", True):
                site_checks.stop_on_conflict(out)
            self.assertIn("注意：冲突了。", out.getvalue())
        self.assertEqual(connection.close.call_count, 2)

    def test_registered(self):
        self.assertIn(site_checks.addresses_check, checks.registry.registry.get_checks())


class NameTests(TestCase):
    def test_the_names_it_gives_uploads_are_served(self):
        from apps.core import uploads

        from ..models import site_upload_to

        name = site_upload_to(None, "我的 Logo.PNG")
        self.assertRegex(name, r"^site/[0-9a-f]{32}\.png$")  # the uploader's name is never used
        self.assertTrue(uploads.allowed(name))
        self.assertFalse(uploads.allowed("site/logo.png"))

    def test_the_django_admin_only_shows_them(self):
        # Changed on /manage/ only: a form here would skip the checks (and store the secret in clear).
        import re

        from django.test import Client

        from apps.core import secrets

        site = SiteSettings.load()
        site.onlyoffice_secret = secrets.encrypt("jwt-value")
        site.save()
        boss, _, _ = users()
        browser = Client()
        browser.force_login(boss)
        page = browser.get(admin_url("siteconf/sitesettings/")).content.decode()
        self.assertIn("HFile", page)
        self.assertFalse(re.search(r'href="[^"]*/siteconf/sitesettings/add/"', page))
        self.assertEqual(browser.get(admin_url("siteconf/sitesettings/add/")).status_code, 403)
        page = browser.get(admin_url(f"siteconf/sitesettings/{site.pk}/change/")).content.decode()
        self.assertIn("直链和短链", page)  # grouped as /manage/ shows them
        self.assertNotIn('name="site_name"', page)  # nothing to change
        self.assertNotIn("enc:", page)
        r = browser.post(admin_url(f"siteconf/sitesettings/{site.pk}/change/"), {"site_name": "x", "custom_js": "alert(1)"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(SiteSettings.load().custom_js, "")
