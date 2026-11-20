"""Image captchas: the pictures, handing them out, checking answers (one try per picture), and the login
asking for one while SiteSettings.login_captcha is on (the session API and the Django admin's page)."""

import base64
import io
import re
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone
from PIL import Image
from rest_framework.test import APIClient, APITestCase

from apps.activity.models import ActivityLog
from apps.activity.testing import logins, only_login
from apps.accounts.serializers import LOGIN_FAILED
from apps.core.testing import admin_url
from apps.siteconf.models import SiteSettings

from . import images, service
from .models import Challenge, Scene

User = get_user_model()
ISSUE_URL = "/api/v1/captcha/"
SESSION = "/api/v1/session/"


def decode(data_uri):
    prefix = "data:image/png;base64,"
    assert data_uri.startswith(prefix), data_uri[:40]
    return Image.open(io.BytesIO(base64.b64decode(data_uri[len(prefix):])))


def captcha_on(on=True):
    site = SiteSettings.load()
    site.login_captcha = on
    site.save()


class PictureTests(SimpleTestCase):
    def test_pictures_are_png_at_twice_their_size(self):
        picture = Image.open(io.BytesIO(images.render("AB3D")))
        self.assertEqual(picture.format, "PNG")
        self.assertEqual(picture.size, (images.width_for(4) * 2, images.HEIGHT * 2))
        self.assertEqual(images.width_for(4), 160)

    def test_texts_leave_out_look_alike_characters(self):
        seen = set("".join(service.random_text() for _ in range(300)))
        self.assertLessEqual(seen, set(service.CHARACTERS))
        self.assertTrue(set("01IO").isdisjoint(service.CHARACTERS))
        self.assertEqual(len(service.random_text()), 4)

    def test_answers_are_compared_loosely(self):
        self.assertEqual(service.normalize(" ab 3d\t"), "AB3D")
        self.assertEqual(service.normalize("ＡＢ３ｄ"), "AB3D")  # typed with a Chinese input method


class IssueTests(APITestCase):
    def issue(self, scene="login", client=None):
        r = (client or self.client).post(ISSUE_URL, {"scene": scene})
        self.assertEqual(r.status_code, 200, r.json())
        self.assertEqual(r["Cache-Control"], "no-store")
        return r.json()

    def test_none_while_the_login_asks_for_none(self):
        self.assertEqual(self.issue(), {"required": False})
        self.assertFalse(Challenge.objects.exists())

    def test_a_picture(self):
        captcha_on()
        before = timezone.now()
        data = self.issue()
        self.assertEqual(set(data), {"required", "key", "image", "width", "height", "length", "expires_in"})  # no answer
        self.assertRegex(data["key"], r"^[0-9a-f]{32}$")
        self.assertEqual((data["width"], data["height"], data["length"], data["expires_in"]), (160, 60, 4, 600))
        self.assertEqual(decode(data["image"]).size, (320, 120))
        challenge = Challenge.objects.get(key=data["key"])
        self.assertEqual((challenge.scene, len(challenge.answer)), ("login", 4))
        self.assertGreaterEqual(challenge.expires_at, before + timedelta(minutes=10))

    def test_unknown_form(self):
        self.assertEqual(self.client.post(ISSUE_URL, {"scene": "nope"}).json(), {"scene": ["没有这种表单。"]})
        self.assertEqual(self.client.post(ISSUE_URL, {}).status_code, 400)
        self.assertEqual(self.client.get(ISSUE_URL).status_code, 405)

    def test_expired_pictures_are_cleaned_up(self):
        captcha_on()
        old = Challenge.objects.create(key="0" * 32, scene="login", answer="ABCD", expires_at=timezone.now())
        self.issue()
        self.assertFalse(Challenge.objects.filter(pk=old.pk).exists())
        self.assertEqual(Challenge.objects.count(), 1)

    def test_rate_limit(self):
        for _ in range(30):
            self.issue()
        self.assertEqual(self.client.post(ISSUE_URL, {"scene": "login"}).status_code, 429)


class VerifyTests(TestCase):
    def setUp(self):
        captcha_on()

    def fresh(self, scene="login"):
        picture = service.issue(scene)
        return picture.key, Challenge.objects.get(key=picture.key).answer

    def assertFails(self, message, scene, key, answer):
        with self.assertRaises(service.CaptchaError) as caught:
            service.verify(scene, key, answer)
        self.assertEqual(caught.exception.message, message)

    def test_right_answers(self):
        key, answer = self.fresh()
        service.verify("login", key, answer)
        key, answer = self.fresh()
        service.verify("login", key, f" {answer.lower()} ")
        key, answer = self.fresh()
        service.verify("login", key, "".join(chr(ord(c) + 0xFEE0) for c in answer))  # full-width

    def test_one_try_per_picture(self):
        key, answer = self.fresh()
        self.assertFails(service.WRONG, "login", key, "ZZZZ" if answer != "ZZZZ" else "YYYY")
        self.assertFails(service.EXPIRED, "login", key, answer)  # the right answer comes too late
        key, answer = self.fresh()
        service.verify("login", key, answer)
        self.assertFails(service.EXPIRED, "login", key, answer)  # and can't be used twice
        self.assertFalse(Challenge.objects.exists())

    def test_expired_or_unknown(self):
        key, answer = self.fresh()
        Challenge.objects.filter(key=key).update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFails(service.EXPIRED, "login", key, answer)
        key, answer = self.fresh()
        Challenge.objects.filter(key=key).update(scene="other")
        self.assertFails(service.EXPIRED, "login", key, answer)  # another form's picture
        self.assertFails(service.EXPIRED, "login", "f" * 32, "ABCD")
        self.assertFails(service.EXPIRED, "login", "x" * 5000, "ABCD")
        key, answer = self.fresh()
        self.assertFails(service.EXPIRED, "login", key + "junk", answer)  # only the exact key
        self.assertFails(service.EXPIRED, "login", key.upper(), answer)
        service.verify("login", key, answer)  # and those tries didn't use it up

    def test_nothing_typed_uses_nothing_up(self):
        key, answer = self.fresh()
        for typed in ("", "   ", None, 1234, ["ABCD"]):
            self.assertFails(service.MISSING, "login", key, typed)
        self.assertFails(service.MISSING, "login", None, answer)
        service.verify("login", key, answer)  # still good

    def test_odd_answers_are_just_wrong(self):
        for typed in ("验证", "A" * 10000, "\x00\x01\x02\x03"):
            key, _ = self.fresh()
            self.assertFails(service.WRONG, "login", key, typed)


class LoginTests(APITestCase):
    """POST /api/v1/session/ with the captcha on: the step "captcha", before the password."""

    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        captcha_on()

    def solved(self):
        key = self.client.post(ISSUE_URL, {"scene": "login"}).json()["key"]
        return {"captcha_key": key, "captcha": Challenge.objects.get(key=key).answer}

    def wrong(self):
        fields = self.solved()
        fields["captcha"] = "2222" if fields["captcha"] != "2222" else "3333"
        return fields

    def login(self, password="pw-alice-2026", **captcha):
        return self.client.post(SESSION, {"username": "alice", "password": password, **captcha})

    def test_without_one_the_step_is_asked_for(self):
        r = self.login()
        self.assertEqual((r.status_code, r.json()), (200, {
            "authenticated": False, "step": "captcha", "before_password": True, "detail": "还要图片验证码才能登录。"}))
        self.assertFalse(ActivityLog.objects.exists())  # nothing was checked yet
        self.assertFalse(self.client.get(SESSION).json()["authenticated"])

    def test_checked_before_the_password(self):
        r = self.login("wrong-password", **self.wrong())
        self.assertEqual((r.status_code, r.json()), (400, {"captcha": [service.WRONG]}))
        entry = only_login(self)
        self.assertEqual((entry.username, entry.reason, entry.user), ("alice", "captcha", None))
        r = self.login(captcha_key="", captcha="")  # sent but empty: not a try
        self.assertEqual(r.json(), {"captcha": [service.MISSING]})
        self.assertEqual(len(logins()), 1)

    def test_a_wrong_password_uses_the_picture_up(self):
        fields = self.solved()
        r = self.login("wrong-password", **fields)
        self.assertEqual(r.json(), {"non_field_errors": [LOGIN_FAILED]})
        self.assertEqual(self.login(**fields).json(), {"captcha": [service.EXPIRED]})
        r = self.login(**self.solved())
        self.assertEqual(r.status_code, 201)
        self.assertTrue(self.client.get(SESSION).json()["authenticated"])

    def test_a_body_that_isnt_an_object(self):
        r = self.client.post(SESSION, ["alice", "pw-alice-2026"], format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["step"], "captcha")

    def test_turned_off_again(self):
        captcha_on(False)
        self.assertEqual(self.login().status_code, 201)


class AdminLoginTests(TestCase):
    """The Django admin's login page (at its random address): otherwise a way round the login captcha."""

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.browser = Client()
        captcha_on()

    def page_key(self):
        page = self.browser.get(admin_url(f"login/?next={admin_url()}")).content.decode()
        self.assertIn('<img src="data:image/png;base64,', page)
        key = re.search(r'name="captcha_key" value="([0-9a-f]{32})"', page).group(1)
        self.assertTrue(Challenge.objects.filter(key=key).exists())
        return key

    def post(self, password, key, answer):
        form = {"username": "boss", "password": password, "next": admin_url(), "captcha_key": key, "captcha": answer}
        return self.browser.post(admin_url(f"login/?next={admin_url()}"), form)

    def test_wrong_and_missing_never_reach_the_password(self):
        key = self.page_key()
        self.assertContains(self.post("pw-boss-2026", key, ""), service.MISSING)
        answer = Challenge.objects.get(key=key).answer
        r = self.post("pw-boss-2026", key, "ZZZZ" if answer != "ZZZZ" else "YYYY")
        self.assertContains(r, service.WRONG)
        self.assertFalse(Challenge.objects.filter(key=key).exists())
        self.assertNotIn("_auth_user_id", self.browser.session)
        self.assertEqual([(entry.reason, entry.source) for entry in logins()], [("captcha", "admin")])
        # The page that says so has a new picture.
        new_key = re.search(r'name="captcha_key" value="([0-9a-f]{32})"', r.content.decode()).group(1)
        self.assertTrue(Challenge.objects.filter(key=new_key).exists())

    def test_right_answer_then_password(self):
        key = self.page_key()
        r = self.post("pw-boss-2026", key, Challenge.objects.get(key=key).answer.lower())
        self.assertRedirects(r, admin_url(), fetch_redirect_response=False)
        self.assertEqual(self.browser.session["_auth_user_id"], str(self.boss.pk))

    def test_its_pictures_count_in_the_captcha_limit(self):
        # Each showing of the page draws a picture and stores a Challenge: as many as POST /api/v1/captcha/
        # hands out (30 a minute per address), and in the same budget.
        for _ in range(29):
            self.assertIn('name="captcha_key"', self.browser.get(admin_url("login/")).content.decode())
        self.assertEqual(APIClient().post(ISSUE_URL, {"scene": "login"}).status_code, 200)  # the 30th
        page = self.browser.get(admin_url("login/")).content.decode()
        self.assertNotIn('name="captcha_key"', page)
        self.assertNotIn("<img", page)
        self.assertRegex(page, r"验证码图片要得太频繁了，请 \d+ 秒后刷新这个页面再登录。")
        self.assertEqual(Challenge.objects.count(), 30)
        self.assertEqual(APIClient().post(ISSUE_URL, {"scene": "login"}).status_code, 429)
        # Sending the form then: no picture to answer, so no password is tried.
        r = self.browser.post(admin_url("login/"), {"username": "boss", "password": "pw-boss-2026"})
        self.assertContains(r, service.MISSING)
        self.assertNotIn("_auth_user_id", self.browser.session)
        # Another address has its own budget.
        other = Client(REMOTE_ADDR="203.0.113.9")
        self.assertIn('name="captcha_key"', other.get(admin_url("login/")).content.decode())

    def test_without_the_login_captcha(self):
        captcha_on(False)
        page = self.browser.get(admin_url("login/")).content.decode()
        self.assertNotIn('name="captcha', page)
        self.assertFalse(Challenge.objects.exists())
        r = self.browser.post(admin_url(f"login/?next={admin_url()}"), {"username": "boss", "password": "pw-boss-2026", "next": admin_url()})
        self.assertRedirects(r, admin_url(), fetch_redirect_response=False)


class PageTests(TestCase):
    def test_visitors_get_the_scripts_and_the_form_knows(self):
        page = self.client.get("/").content.decode()
        self.assertRegex(page, r'<script src="/static/captchas/js/captcha\.js\?v=[0-9a-f]{12}"></script>')
        self.assertRegex(page, r'<script src="/static/accounts/js/login_steps\.js\?v=[0-9a-f]{12}"></script>')
        self.assertLess(page.index("web/js/shell.js"), page.index("captchas/js/captcha.js"))
        self.assertIn('data-captcha="0"', page)
        captcha_on()
        self.assertIn('data-captcha="1"', self.client.get("/").content.decode())
        self.client.force_login(User.objects.create_user("alice", password="pw-alice-2026"))
        self.assertNotIn("captchas/js/captcha.js", self.client.get("/").content.decode())

    def test_scene_values(self):
        self.assertEqual(Scene.values, ["login"])
