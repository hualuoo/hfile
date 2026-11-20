"""Binding an account to oneself (账号绑定: /api/v1/sso/links/), unbinding it, and setting a first
password without an old one (PUT /api/v1/users/me/password/ for accounts single sign-on made)."""

from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.accounts.models import LoginLog
from apps.core.testing import TO_THE_SECOND
from apps.sso.models import Identity

from .helpers import FakeIdpMixin

User = get_user_model()
LINKS = "/api/v1/sso/links/"
PASSWORD = "pw-alice-2026"


def query_of(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


class LinkTests(FakeIdpMixin, TestCase):
    client_class = APIClient

    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password=PASSWORD)
        self.client.force_login(self.alice)

    def begin_link(self, password=PASSWORD, next_path="/files/?auth=sso", code="fake", client=None):
        body = {"next": next_path}
        if password is not None:
            body["password"] = password
        return (client or self.client).post(f"{LINKS}{code}/", body, format="json")

    def test_the_list(self):
        self.make_provider(code="off", name="停用的", enabled=False)
        data = self.client.get(LINKS).json()
        self.assertEqual(data["has_password"], True)
        self.assertEqual([(row["code"], row["linked"]) for row in data["providers"]], [("fake", False)])
        self.assertEqual(self.client_class().get(LINKS).status_code, 401)

    def test_binding(self):
        logged = LoginLog.objects.count()  # force_login's
        answer = self.begin_link()
        self.assertEqual(answer.status_code, 200, answer.content)
        address = answer.json()["url"]
        self.assertTrue(address.startswith(f"{self.idp.base}/authorize?"))
        response = self.come_back(address)
        self.assertEqual((response.status_code, response["Location"]), (302, "/files/?auth=sso&sso_linked=fake"))
        identity = Identity.objects.get()
        self.assertEqual((identity.user, identity.external_id), (self.alice, "alice-1"))
        self.assertEqual(LoginLog.objects.count(), logged)  # binding isn't a login
        row = self.client.get(LINKS).json()["providers"][0]
        self.assertEqual((row["linked"], row["external_id"]), (True, "alice-1"))
        # Now it logs in.
        self.client.logout()
        self.assertEqual(self.login_via_sso().status_code, 302)
        self.assertEqual(self.client.get("/api/v1/session/").json()["user"]["username"], "alice")
        row = self.client.get(LINKS).json()["providers"][0]
        for value in (row["created_at"], row["last_login_at"]):  # TEXT-01: they were UTC with microseconds
            self.assertRegex(value, TO_THE_SECOND)

    def test_a_long_address_goes_back_home(self):
        # A page address over NEXT_MAX doesn't stop the binding: the browser comes back to the home page, as
        # /sso/<code>/login does with it (ACCT-04: it was 400 "地址太长了。").
        answer = self.begin_link(next_path="/files/" + "%E4%B8%AD" * 300)
        self.assertEqual(answer.status_code, 200, answer.content)
        response = self.come_back(answer.json()["url"])
        self.assertEqual((response.status_code, response["Location"]), (302, "/?sso_linked=fake"))

    def test_the_password_is_asked_for(self):
        self.assertEqual(self.begin_link(password=None).json(), {"password": ["为了确认是你本人，请输入登录密码。"]})
        self.assertEqual(self.begin_link(password="wrong").json(), {"password": ["密码不对。"]})
        self.assertNotIn("hfile_sso_pending", self.client.session)
        nobody = User.objects.create_user("nopw")  # no usable password: nothing to ask
        client = self.client_class()
        client.force_login(nobody)
        self.assertEqual(self.begin_link(password=None, client=client).status_code, 200)

    def test_a_password_hashed_again_keeps_the_session(self):
        hashers = ["django.contrib.auth.hashers.PBKDF2PasswordHasher", "django.contrib.auth.hashers.MD5PasswordHasher"]
        with override_settings(PASSWORD_HASHERS=hashers):  # alice's MD5 hash gets upgraded when checked
            before = User.objects.get(pk=self.alice.pk).password
            self.assertEqual(self.begin_link().status_code, 200)
            self.assertNotEqual(User.objects.get(pk=self.alice.pk).password, before)
            self.assertTrue(self.client.get("/api/v1/session/").json()["authenticated"])

    def test_checks_count_in_the_password_limit(self):
        for _ in range(10):
            self.begin_link(password="wrong")
        self.assertEqual(self.begin_link().status_code, 429)

    def test_someone_elses_account(self):
        bob = User.objects.create_user("bob")
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=bob)
        response = self.come_back(self.begin_link().json()["url"])
        self.assertContains(response, "这个账号已经绑定了别的用户", status_code=403)
        self.assertEqual(Identity.objects.get().user, bob)
        self.assertContains(response, "/files/?auth=sso", status_code=403)  # 返回

    def test_a_second_account_of_one_provider(self):
        Identity.objects.create(provider=self.provider, external_id="old-id", user=self.alice)
        answer = self.begin_link()
        self.assertEqual(answer.status_code, 409)
        self.assertIn("先解除", answer.json()["detail"])
        # Bound meanwhile (another tab): the callback refuses too.
        Identity.objects.all().delete()
        address = self.begin_link().json()["url"]
        Identity.objects.create(provider=self.provider, external_id="old-id", user=self.alice)
        self.assertContains(self.come_back(address), "你已经绑定了另一个", status_code=403)
        # The same account, bound meanwhile: nothing new to do.
        Identity.objects.all().delete()
        address = self.begin_link().json()["url"]
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)
        self.assertEqual(self.come_back(address).status_code, 302)
        self.assertEqual(Identity.objects.count(), 1)

    def restore(self, pending):
        session = self.client.session
        session["hfile_sso_pending"] = pending
        session.save()

    def test_the_session_changed_meanwhile(self):
        address = self.begin_link().json()["url"]
        pending = self.client.session["hfile_sso_pending"]
        self.client.force_login(User.objects.create_user("mallory"))  # the same browser, someone else now
        self.restore(pending)
        self.assertContains(self.come_back(address), "绑定没有完成", status_code=403)
        self.client.logout()  # nobody now
        from django.core.cache import cache

        cache.clear()  # the state's one-use mark
        self.restore(pending)
        self.assertContains(self.come_back(address), "绑定没有完成", status_code=403)
        self.assertFalse(Identity.objects.exists())
        self.assertEqual(self.idp.token_forms, [])  # refused before asking the provider anything

    def test_a_disabled_provider(self):
        self.provider.enabled = False
        self.provider.save()
        self.assertEqual(self.begin_link().status_code, 404)

    def test_unbinding(self):
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)
        self.assertEqual(self.client.delete(f"{LINKS}fake/").status_code, 204)
        self.assertFalse(Identity.objects.exists())
        self.assertEqual(self.client.delete(f"{LINKS}fake/").status_code, 404)
        self.assertEqual(self.client.delete(f"{LINKS}nope/").status_code, 404)

    def test_not_the_last_way_to_log_in(self):
        sso_only = User.objects.create_user("sso-only")
        self.assertFalse(sso_only.has_usable_password())
        Identity.objects.create(provider=self.provider, external_id="x-1", user=sso_only)
        client = self.client_class()
        client.force_login(sso_only)
        refused = client.delete(f"{LINKS}fake/")
        self.assertEqual(refused.status_code, 409)
        self.assertIn("唯一的登录方式", refused.json()["detail"])
        other = self.make_provider(code="other", name="另一个")
        Identity.objects.create(provider=other, external_id="x-1", user=sso_only)
        self.assertEqual(client.delete(f"{LINKS}fake/").status_code, 204)  # another one is left
        self.assertEqual(client.delete(f"{LINKS}other/").status_code, 409)  # not that one
        # An account at a provider that is switched off isn't a way in.
        other.enabled = False
        other.save()
        self.make_provider(code="third", name="第三个")
        Identity.objects.create(provider=self.provider, external_id="x-1", user=sso_only)
        self.assertEqual(client.delete(f"{LINKS}fake/").status_code, 409)
        other.enabled = True
        other.save()
        # With a password set, the last one may go.
        self.assertEqual(client.put("/api/v1/users/me/password/", {"new_password": "a-New-pass-2027"}, format="json").status_code, 204)
        self.assertEqual(client.delete(f"{LINKS}other/").status_code, 204)


class FirstPasswordTests(TestCase):
    """修改密码 for an account without a usable password: no old password."""

    url = "/api/v1/users/me/password/"
    client_class = APIClient

    def test_without_an_old_password(self):
        user = User.objects.create_user("sso-made")
        self.client.force_login(user)
        self.assertEqual(self.client.put(self.url, {"new_password": "12345678"}, format="json").status_code, 400)  # still checked
        response = self.client.put(self.url, {"new_password": "a-New-pass-2027", "old_password": ""}, format="json")
        self.assertEqual(response.status_code, 204)
        user.refresh_from_db()
        self.assertTrue(user.check_password("a-New-pass-2027"))
        self.assertTrue(self.client.get("/api/v1/session/").json()["authenticated"])  # this session stays
        # From now on the old one is asked for.
        self.assertEqual(self.client.put(self.url, {"new_password": "b-New-pass-2028"}, format="json").json(),
                         {"old_password": ["要填原密码。"]})
        self.assertEqual(self.client.put(self.url, {"old_password": "wrong", "new_password": "b-New-pass-2028"}, format="json").json(),
                         {"old_password": ["原密码不对。"]})
        self.assertEqual(self.client.put(self.url, {"old_password": "a-New-pass-2027", "new_password": "b-New-pass-2028"},
                                         format="json").status_code, 204)

    def test_with_a_password_the_old_one_is_needed(self):
        user = User.objects.create_user("alice", password=PASSWORD)
        self.client.force_login(user)
        self.assertEqual(self.client.put(self.url, {"new_password": "a-New-pass-2027"}, format="json").json(),
                         {"old_password": ["要填原密码。"]})
        self.assertEqual(self.client.put(self.url, {"old_password": "", "new_password": "a-New-pass-2027"}, format="json").json(),
                         {"old_password": ["要填原密码。"]})
        user.refresh_from_db()
        self.assertTrue(user.check_password(PASSWORD))
