from django.conf import settings
from django.test import TestCase

from apps.core import reserved


class ReservedTests(TestCase):  # the registered checks read the database
    def test_static_and_admin_path(self):
        for name in ["api", "API", "static", "admin", "s", "pd", "site", "install", settings.ADMIN_PATH.upper()]:
            self.assertNotEqual(reserved.problem(name), "", name)
        self.assertEqual(reserved.problem("my-files"), "")

    def test_checks(self):
        def taken_by_test(name):
            return "测试占用了。" if name.lower() == "busy" else ""

        reserved.register_check(taken_by_test)
        self.addCleanup(reserved._checks.remove, taken_by_test)
        self.assertEqual(reserved.problem("Busy"), "测试占用了。")
        self.assertEqual(reserved.problem("busy", own=taken_by_test), "")

    def test_register(self):
        reserved.register("Extra-App")
        self.addCleanup(reserved._names.discard, "extra-app")
        self.assertNotEqual(reserved.problem("extra-app"), "")
