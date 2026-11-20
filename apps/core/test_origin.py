from django.core.exceptions import DisallowedHost
from django.test import RequestFactory, SimpleTestCase, override_settings

from apps.core import origin


@override_settings(ALLOWED_HOSTS=["files.example.com", "127.0.0.1"])
class OriginTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(SITE_URL="https://files.example.com")
    def test_site_url_wins(self):
        request = self.factory.get("/", HTTP_HOST="127.0.0.1")
        self.assertEqual(origin.site_origin(request), "https://files.example.com")
        self.assertEqual(origin.site_origin(), "https://files.example.com")
        self.assertEqual(origin.absolute("/sso/github/login/callback", request),
                         "https://files.example.com/sso/github/login/callback")
        self.assertTrue(origin.from_site_url())

    @override_settings(SITE_URL="")
    def test_the_request_without_site_url(self):
        request = self.factory.get("/", HTTP_HOST="127.0.0.1:8000")
        self.assertEqual(origin.site_origin(request), "http://127.0.0.1:8000")
        self.assertEqual(origin.absolute("/a?b=1", request), "http://127.0.0.1:8000/a?b=1")
        self.assertFalse(origin.from_site_url())
        self.assertEqual(origin.site_origin(), "")
        with self.assertRaises(ValueError):
            origin.absolute("/a")

    @override_settings(SITE_URL="")
    def test_a_host_that_is_not_allowed(self):
        request = self.factory.get("/", HTTP_HOST="evil.example.net")
        with self.assertRaises(DisallowedHost):
            origin.site_origin(request)

    @override_settings(SITE_URL="https://files.example.com")
    def test_only_paths_of_the_site(self):
        for path in ["https://evil.example.net/", "//evil.example.net/", "relative", "", None]:
            with self.assertRaises(ValueError, msg=path):
                origin.absolute(path)
