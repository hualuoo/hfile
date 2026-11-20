"""The admins' side of single sign-on: /api/v1/manage/sso/ (providers, their order, discovery), a user's
bound accounts (/api/v1/manage/users/{id}/identities/), and the public list of login buttons."""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from apps.activity.models import ActivityLog
from apps.sso.models import Identity, Provider

from .helpers import FakeIdpMixin

User = get_user_model()
PROVIDERS = "/api/v1/manage/sso/providers/"
DISCOVER = "/api/v1/manage/sso/discover/"


def new_provider(**values):
    body = {
        "code": "gitlab", "name": "GitLab", "client_id": "cid", "client_secret": "s3cret-value",
        "auth_url": "https://gitlab.example.com/oauth/authorize", "token_url": "https://gitlab.example.com/oauth/token",
        "userinfo_url": "https://gitlab.example.com/oauth/userinfo", "scope": "openid profile", "token_auth": "post",
        "id_field": "sub", "username_field": "preferred_username", "nickname_field": "name",
    }
    body.update(values)
    return body


class AccessTests(TestCase):
    client_class = APIClient

    def test_admins_only(self):
        provider = Provider.objects.create(code="x", name="X", client_id="c", auth_url="https://a.example/a",
                                           token_url="https://a.example/t", userinfo_url="https://a.example/u")
        user = User.objects.create_user("alice")
        endpoints = [("get", PROVIDERS), ("post", PROVIDERS), ("get", f"{PROVIDERS}{provider.pk}/"),
                     ("patch", f"{PROVIDERS}{provider.pk}/"), ("delete", f"{PROVIDERS}{provider.pk}/"),
                     ("post", f"{PROVIDERS}order/"), ("post", DISCOVER), ("get", f"/api/v1/manage/users/{user.pk}/identities/"),
                     ("post", f"/api/v1/manage/users/{user.pk}/identities/"),
                     ("delete", f"/api/v1/manage/users/{user.pk}/identities/1/")]
        for method, url in endpoints:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 401)
        self.client.force_login(user)
        for method, url in endpoints:
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(self.client, method)(url, {}, format="json").status_code, 403)
        self.assertTrue(Provider.objects.filter(pk=provider.pk).exists())


class ProviderApiTests(TestCase):
    client_class = APIClient

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client.force_login(self.boss)

    def test_clearing_the_secret(self):
        # null clears a saved secret, a public client from then on (ACCT-05: it was 400 "Client Secret不能为空。").
        url = f"{PROVIDERS}{self.client.post(PROVIDERS, new_provider(), format='json').json()['id']}/"
        cleared = self.client.patch(url, {"client_secret": None}, format="json")
        self.assertEqual(cleared.status_code, 200, cleared.content)
        self.assertFalse(cleared.json()["client_secret_set"])
        self.assertEqual(Provider.objects.get().secret(), "")

    def test_a_form_without_enabled(self):
        # As a user's is_active (API-06): a provider created with a form that left the switch out was disabled.
        response = self.client.post(PROVIDERS, new_provider(), format="multipart")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(Provider.objects.get().enabled)

    def test_create_read_change_delete(self):
        response = self.client.post(PROVIDERS, new_provider(), format="json")
        self.assertEqual(response.status_code, 201, response.content)
        data = response.json()
        self.assertNotIn("client_secret", data)
        self.assertEqual((data["client_secret_set"], data["client_secret_unreadable"], data["identities"]), (True, False, 0))
        self.assertEqual(data["callback_url"], "http://testserver/sso/gitlab/login/callback")
        stored = Provider.objects.get().client_secret
        self.assertTrue(stored.startswith("enc:"))
        self.assertNotIn("s3cret-value", stored)
        self.assertEqual(Provider.objects.get().secret(), "s3cret-value")
        url = f"{PROVIDERS}{data['id']}/"
        self.assertNotIn("s3cret", self.client.get(url).content.decode())
        self.assertNotIn("s3cret", self.client.get(PROVIDERS).content.decode())
        # A blank secret leaves it as it is; a new one replaces it.
        changed = self.client.patch(url, {"client_secret": "", "name": "公司 GitLab", "enabled": False}, format="json").json()
        self.assertEqual((changed["name"], changed["enabled"], changed["client_secret_set"]), ("公司 GitLab", False, True))
        self.assertEqual(Provider.objects.get().secret(), "s3cret-value")
        self.client.patch(url, {"client_secret": "  another-one\n"}, format="json")
        self.assertEqual(Provider.objects.get().secret(), "another-one")
        # Deleting deletes the accounts bound of it, not the users.
        alice = User.objects.create_user("alice")
        Identity.objects.create(provider=Provider.objects.get(), external_id="a-1", user=alice)
        self.assertEqual(self.client.get(url).json()["identities"], 1)
        self.assertEqual(self.client.delete(url).status_code, 204)
        self.assertFalse(Identity.objects.exists())
        self.assertTrue(User.objects.filter(pk=alice.pk).exists())

    def test_what_is_checked(self):
        self.client.post(PROVIDERS, new_provider(), format="json")
        response = self.client.post(PROVIDERS, new_provider(
            code="GitLab", name="  ", client_id="a b", auth_url="ftp://x", token_url="https://u:p@x.example/t",
            userinfo_url="https://x.example/#u", scope='openid "x"', token_auth="jwt", id_field="a..b", username_field="[x]",
            nickname_field="", icon="javascript:alert(1)", order="first",
        ), format="json")
        self.assertEqual(response.status_code, 400)
        errors = response.json()
        self.assertEqual(set(errors), {"code", "name", "client_id", "auth_url", "token_url", "userinfo_url", "scope",
                                       "token_auth", "id_field", "username_field", "icon", "order"})
        self.assertEqual(errors["code"], ["这个简称已经有了，换一个。"])  # lower-cased first: gitlab is taken
        self.assertIn("http://", errors["auth_url"][0])
        self.assertIn("用户名和密码", errors["token_url"][0])
        self.assertEqual(errors["token_auth"], ["Client Secret 的发送方式只能是 post 或 basic。"])
        self.assertEqual(errors["order"], ["排序要写成整数。"])
        missing = self.client.post(PROVIDERS, {}, format="json").json()
        self.assertEqual(set(missing), {"code", "name", "client_id", "auth_url", "token_url", "userinfo_url", "id_field"})
        self.assertEqual(missing["code"], ["要填简称。"])

    def test_a_public_client_and_an_order(self):
        first = self.client.post(PROVIDERS, new_provider(client_secret="", code="b", order=5), format="json").json()
        self.assertFalse(first["client_secret_set"])
        second = self.client.post(PROVIDERS, new_provider(code="a", order=1), format="json").json()
        self.assertEqual([row["code"] for row in self.client.get(PROVIDERS).json()], ["a", "b"])
        answer = self.client.post(f"{PROVIDERS}order/", {"ids": [first["id"], second["id"]]}, format="json")
        self.assertEqual([row["code"] for row in answer.json()], ["b", "a"])
        self.assertEqual(self.client.post(f"{PROVIDERS}order/", {"ids": [first["id"]]}, format="json").status_code, 409)
        self.assertEqual(self.client.post(f"{PROVIDERS}order/", {"ids": [first["id"], first["id"]]}, format="json").status_code, 400)

    def test_staff_who_arent_superusers_only_look(self):
        # The settings decide which account at the provider logs in as whom: staff could otherwise log in
        # as a superuser by pointing id_field (or userinfo_url) at a value they choose.
        made = self.client.post(PROVIDERS, new_provider(), format="json").json()
        helper = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        client = self.client_class()
        client.force_login(helper)
        url = f"{PROVIDERS}{made['id']}/"
        self.assertEqual(client.get(PROVIDERS).status_code, 200)
        self.assertEqual(client.get(url).status_code, 200)
        for method, address, body in (("post", PROVIDERS, new_provider(code="x")), ("patch", url, {"id_field": "bio"}),
                                      ("delete", url, {}), ("post", f"{PROVIDERS}order/", {"ids": [made["id"]]}),
                                      ("post", DISCOVER, {"url": "https://gitlab.example.com/.well-known/openid-configuration"})):
            with self.subTest(method=method, address=address):
                answer = getattr(client, method)(address, body, format="json")
                self.assertEqual(answer.status_code, 403)
                self.assertIn("只有超级管理员", answer.json()["detail"])
        self.assertEqual(Provider.objects.get().id_field, "sub")

    def test_the_public_list(self):
        Provider.objects.create(code="b", name="B", client_id="c", auth_url="https://a.example/a", token_url="https://a.example/t",
                                userinfo_url="https://a.example/u", order=2, icon="https://img.example/b.png")
        Provider.objects.create(code="a", name="A", client_id="c", auth_url="https://a.example/a", token_url="https://a.example/t",
                                userinfo_url="https://a.example/u", order=1)
        Provider.objects.create(code="off", name="Off", client_id="c", auth_url="https://a.example/a", token_url="https://a.example/t",
                                userinfo_url="https://a.example/u", enabled=False)
        answer = self.client_class().get("/api/v1/sso/providers/")
        self.assertEqual(answer.json(), [{"code": "a", "name": "A", "icon": ""}, {"code": "b", "name": "B", "icon": "https://img.example/b.png"}])

    def test_login_log_labels(self):
        ActivityLog.objects.create(kind="login_failed", target_name="fake:a-1", detail={"reason": "sso_not_bound", "source": "sso"})
        entry = self.client.get("/api/v1/manage/activity/").json()["results"][0]
        self.assertEqual((entry["target"]["username"], entry["detail_text"]), ("fake:a-1", "单点登录：账号还没有绑定"))


class DiscoveryTests(FakeIdpMixin, TestCase):
    client_class = APIClient

    def setUp(self):
        super().setUp()
        self.client.force_login(User.objects.create_superuser("boss", password="pw-boss-2026"))

    def test_reading_a_configuration(self):
        answer = self.client.post(DISCOVER, {"url": f"{self.idp.base}/.well-known/openid-configuration"}, format="json")
        self.assertEqual(answer.status_code, 200, answer.content)
        self.assertEqual(answer.json(), {
            "auth_url": f"{self.idp.base}/authorize", "token_url": f"{self.idp.base}/token", "userinfo_url": f"{self.idp.base}/userinfo",
            "scopes": ["openid", "profile", "email"], "token_auth": "post", "issuer": self.idp.base,
        })

    def test_what_goes_wrong(self):
        self.assertIn("http://", self.client.post(DISCOVER, {"url": "ftp://example.com/x"}, format="json").json()["url"][0])
        for url, text in ((f"{self.idp.base}/nothing-here", "服务商返回 404"),
                          ("http://127.0.0.1:9/.well-known/openid-configuration", "连不上")):
            with self.subTest(url=url), self.assertLogs("hfile.sso", "WARNING"):
                answer = self.client.post(DISCOVER, {"url": url}, format="json")
                self.assertEqual(answer.status_code, 400)
                self.assertIn(text, answer.json()["url"][0])
        self.idp.behave(not_json=True)
        with self.assertLogs("hfile.sso", "WARNING"):
            answer = self.client.post(DISCOVER, {"url": f"{self.idp.base}/.well-known/openid-configuration"}, format="json")
        self.assertEqual(answer.json(), {"url": ["读取 OpenID 配置时服务商的回答不是 JSON。"]})


class IdentityApiTests(TestCase):
    client_class = APIClient

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client.force_login(self.boss)
        self.alice = User.objects.create_user("alice")
        self.provider = Provider.objects.create(code="gh", name="GitHub", client_id="c", auth_url="https://a.example/a",
                                                token_url="https://a.example/t", userinfo_url="https://a.example/u")
        self.url = f"/api/v1/manage/users/{self.alice.pk}/identities/"

    def test_binding_and_unbinding(self):
        made = self.client.post(self.url, {"provider": self.provider.pk, "external_id": " 4711 "}, format="json")
        self.assertEqual(made.status_code, 201, made.content)
        self.assertEqual((made.json()["external_id"], made.json()["provider"]["code"]), ("4711", "gh"))
        self.assertEqual([row["external_id"] for row in self.client.get(self.url).json()], ["4711"])
        again = self.client.post(self.url, {"provider": self.provider.pk, "external_id": "4711"}, format="json")
        self.assertEqual(again.json(), {"external_id": ["这个 GitHub 账号已经绑定了他。"]})
        second = self.client.post(self.url, {"provider": self.provider.pk, "external_id": "4712"}, format="json")
        self.assertIn("已经绑定了一个 GitHub 账号", second.json()["provider"][0])
        bob = User.objects.create_user("bob")
        taken = self.client.post(f"/api/v1/manage/users/{bob.pk}/identities/", {"provider": self.provider.pk, "external_id": "4711"}, format="json")
        self.assertEqual(taken.json(), {"external_id": ["这个 GitHub 账号已经绑定了用户「alice」。"]})
        identity = Identity.objects.get()
        self.assertEqual(self.client.delete(f"/api/v1/manage/users/{bob.pk}/identities/{identity.pk}/").status_code, 404)
        self.assertEqual(self.client.delete(f"{self.url}{identity.pk}/").status_code, 204)
        self.assertFalse(Identity.objects.exists())

    def test_the_provider_is_an_integer_id(self):
        # 5.9 was bound to the provider of id 5, and 1e400 (infinity) answered 500 (API-08).
        for given, answer in ((f"{self.provider.pk}.9", {"provider": ["服务商要写成 ID。"]}),
                              (str(10**30), {"provider": ["没有这个服务商。"]}),
                              ("1e400", {"detail": "JSON 格式不对：数字 1e400 太大了。"})):  # the parser's, now
            with self.subTest(provider=given):
                r = self.client.post(self.url, f'{{"provider": {given}, "external_id": "4711"}}', content_type="application/json")
                self.assertEqual((r.status_code, r.json()), (400, answer))
        self.assertFalse(Identity.objects.exists())

    def test_whose_it_is_only_for_admins_who_may_look(self):
        Identity.objects.create(provider=self.provider, external_id="4711", user=self.boss)  # a superuser's
        bob = User.objects.create_user("bob")
        Identity.objects.create(provider=self.provider, external_id="4712", user=bob)
        helper = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        client = self.client_class()
        client.force_login(helper)
        answer = client.post(self.url, {"provider": self.provider.pk, "external_id": "4711"}, format="json")
        self.assertEqual(answer.json(), {"external_id": ["这个账号已经绑定了别的用户。"]})
        answer = client.post(self.url, {"provider": self.provider.pk, "external_id": "4712"}, format="json")
        self.assertEqual(answer.json(), {"external_id": ["这个 GitHub 账号已经绑定了用户「bob」。"]})  # he may look at bob
        answer = self.client.post(self.url, {"provider": self.provider.pk, "external_id": "4711"}, format="json")
        self.assertEqual(answer.json(), {"external_id": ["这个 GitHub 账号已经绑定了用户「boss」。"]})  # a superuser may

    def test_what_is_checked(self):
        for body, field in (({"external_id": "x"}, "provider"), ({"provider": 999, "external_id": "x"}, "provider"),
                            ({"provider": self.provider.pk}, "external_id"), ({"provider": self.provider.pk, "external_id": "a\x00"}, "external_id"),
                            ({"provider": self.provider.pk, "external_id": "x" * 300}, "external_id")):
            with self.subTest(body=body):
                answer = self.client.post(self.url, body, format="json")
                self.assertEqual(answer.status_code, 400)
                self.assertIn(field, answer.json())
        self.assertEqual(self.client.get("/api/v1/manage/users/999999/identities/").status_code, 404)

    def test_not_for_builtin_users(self):
        for user in (User.objects.guest(), User.objects.template()):
            answer = self.client.post(f"/api/v1/manage/users/{user.pk}/identities/", {"provider": self.provider.pk, "external_id": "x"}, format="json")
            self.assertEqual(answer.status_code, 403)
            self.assertIn("内置用户", answer.json()["detail"])
        self.assertFalse(Identity.objects.exists())

    def test_who_may_change_whom(self):
        helper = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        client = self.client_class()
        client.force_login(helper)
        self.assertEqual(client.post(self.url, {"provider": self.provider.pk, "external_id": "1"}, format="json").status_code, 201)
        boss_url = f"/api/v1/manage/users/{self.boss.pk}/identities/"
        refused = client.post(boss_url, {"provider": self.provider.pk, "external_id": "2"}, format="json")
        self.assertEqual(refused.status_code, 403)
        self.assertEqual(refused.json()["detail"], "只有超级管理员可以修改其他管理员的账号。")
        identity = Identity.objects.create(provider=self.provider, external_id="3", user=self.boss)
        self.assertEqual(client.delete(f"{boss_url}{identity.pk}/").status_code, 403)
        self.assertEqual(client.get(boss_url).status_code, 403)  # nor read another admin's ids
        self.assertEqual(client.get(self.url).status_code, 200)  # a normal user's, yes
        # A superuser may bind one to another superuser (D35: never automatically, by an admin yes).
        other_boss = User.objects.create_superuser("boss2", password="pw-boss2-2026")
        answer = self.client.post(f"/api/v1/manage/users/{other_boss.pk}/identities/", {"provider": self.provider.pk, "external_id": "5"}, format="json")
        self.assertEqual(answer.status_code, 201)

    def test_not_ones_own(self):
        # An open admin session alone mustn't add a lasting way in: 账号绑定 asks for the password.
        own = f"/api/v1/manage/users/{self.boss.pk}/identities/"
        answer = self.client.post(own, {"provider": self.provider.pk, "external_id": "4"}, format="json")
        self.assertEqual(answer.status_code, 403)
        self.assertIn("账号绑定", answer.json()["detail"])
        identity = Identity.objects.create(provider=self.provider, external_id="4", user=self.boss)
        self.assertEqual(self.client.get(own).status_code, 200)
        self.assertEqual(self.client.delete(f"{own}{identity.pk}/").status_code, 403)
        self.assertTrue(Identity.objects.filter(pk=identity.pk).exists())
