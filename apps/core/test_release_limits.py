"""Release check: the limits on what a request may make the site read and hold.

- The whole request: settings.MAX_REQUEST_BYTES (64 MiB), apps.core.middleware.RequestSizeLimitMiddleware;
  waitress twice that (apps/installer/management/commands/serve.py). Tested in apps/core/tests.py.
- A body other than uploaded files: Django's DATA_UPLOAD_MAX_MEMORY_SIZE (2.5 MB). DRF 3.18 reads JSON
  through request.body (Request._parse), so it applies to the JSON API too; apps/core/exceptions.py answers
  413. (The text editor's save has its own parser and limit, apps/uploads/views.py TextJSONParser.)
- Form fields and files: DATA_UPLOAD_MAX_NUMBER_FIELDS (1000) and _FILES (100), multipart only.
- Lists of the batch endpoints (max_length 1000): DRF's ListField validates every item before it looks
  at the length, and keeps an error for each bad one. 2.5 MB of [{}, {}, ...] is 870 000 items: about
  12 s of CPU, 1.3 GB of memory and a 34 MB answer for one request, which anybody can send (the
  serializer runs before any permission; a CSRF token comes with any page; files' batch endpoints have
  no rate limit).

The tests marked expectedFailure show a limit that doesn't hold today: take the mark off with the fix.
"""

import json

from django.conf import settings
from django.test import Client, TestCase

from apps.accounts.models import User

LOGIN = "/api/v1/session/"


def anonymous():
    """A visitor's browser, with the CSRF cookie and token any page hands out."""
    client = Client(enforce_csrf_checks=True)
    token = client.get(LOGIN).json()["csrf_token"]
    return client, token


class JsonBodyLimitTests(TestCase):
    def setUp(self):
        super().setUp()
        User.objects.guest()

    def test_a_json_body_over_the_limit_is_refused(self):
        self.assertEqual(settings.DATA_UPLOAD_MAX_MEMORY_SIZE, 2621440)
        client, token = anonymous()
        body = json.dumps({"username": "a" * (3 * 1024 * 1024), "password": "x"})
        r = client.post(LOGIN, body, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 413, r.content[:200])

    def test_a_throttled_login_does_not_read_a_large_body(self):
        # SessionView.throttled() reads request.data for the log: still within the 2.5 MB.
        client, token = anonymous()
        small = json.dumps({"username": "nobody", "password": "wrong"})
        for _ in range(10):  # settings: "login": "10/minute"
            client.post(LOGIN, small, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        body = json.dumps({"username": "a" * (3 * 1024 * 1024), "password": "x"})
        r = client.post(LOGIN, body, content_type="application/json", HTTP_X_CSRFTOKEN=token)
        self.assertIn(r.status_code, (413, 429), r.content[:200])


class ListLengthTests(TestCase):
    """A list over its max_length is refused at once, with one message, whoever sends it."""

    def setUp(self):
        super().setUp()
        User.objects.guest()

    def post(self, url, body):
        client, token = anonymous()
        return client.post(url, json.dumps(body), content_type="application/json", HTTP_X_CSRFTOKEN=token)

    def assertOneMessage(self, r, field):
        self.assertEqual(r.status_code, 400)
        errors = r.json().get(field)
        # Not {"0": [...], "1": [...], ... "4999": [...]}: 64 MiB of them at 2.5 MB of body.
        self.assertIsInstance(errors, list, str(r.json())[:200])
        self.assertEqual(len(errors), 1)

    def test_upload_folders(self):
        self.assertOneMessage(self.post("/api/v1/uploads/folders/", {"storage": "x", "folder": "/", "folders": [{}] * 5000}),
                              "folders")

    def test_files_delete(self):
        self.assertOneMessage(self.post("/api/v1/files/x/delete/", {"path": "/", "names": [{}] * 5000}), "names")

    def test_path_links(self):
        self.assertOneMessage(self.post("/api/v1/links/x/path/", {"paths": [{}] * 5000}), "paths")

    def test_within_the_length_each_item_is_checked(self):
        r = self.post("/api/v1/uploads/folders/", {"storage": "x", "folder": "/", "folders": [{}, "a"]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("0", r.json()["folders"])


class FormLimitTests(TestCase):
    def test_too_many_form_fields(self):
        client, token = anonymous()
        fields = {f"f{i}": "x" for i in range(settings.DATA_UPLOAD_MAX_NUMBER_FIELDS + 1)}
        r = client.post(LOGIN, {**fields, "username": "a", "password": "b"}, HTTP_X_CSRFTOKEN=token)  # multipart
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["detail"], "表单里的项目太多了。")

    def test_form_fields_over_the_memory_limit(self):
        client, token = anonymous()
        r = client.post(LOGIN, {"username": "a" * (3 * 1024 * 1024), "password": "b"}, HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 413)

    def test_urlencoded_is_not_taken_by_the_api(self):
        client, token = anonymous()
        r = client.post(LOGIN, "username=a&password=b", content_type="application/x-www-form-urlencoded",
                        HTTP_X_CSRFTOKEN=token)
        self.assertEqual(r.status_code, 415)
