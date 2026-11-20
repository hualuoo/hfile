"""Logging in with single sign-on (apps/sso/flow.py, apps/sso/views.py) against the fake provider: the state, PKCE
and the redirect_uri, what the provider answers, the binding of its accounts to users, new users, two-step
verification, the secure login entry and the login log."""

import time
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.sessions.models import Session
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import Kind, LoginLog
from apps.accounts.signals import user_copied
from apps.accounts.test_totp import code_for, turn_on
from apps.core import masking
from apps.siteconf.models import SiteSettings
from apps.sso import flow, outbound
from apps.sso.models import Identity, key_of

from .helpers import FakeIdpMixin

User = get_user_model()


def query_of(url):
    return {key: values[-1] for key, values in parse_qs(urlsplit(url).query).items()}


def site(**values):
    settings = SiteSettings.load()
    for name, value in values.items():
        setattr(settings, name, value)
    settings.save()


class LoginTests(FakeIdpMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026", nickname="小艾")
        self.identity = Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)

    def whoami(self, client=None):
        return (client or self.client).get("/api/v1/session/").json()

    def test_from_start_to_end(self):
        address = self.begin(next_path="/manage/%23users")
        parts = urlsplit(address)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", f"{self.idp.base}/authorize")
        query = parse_qs(parts.query)
        self.assertEqual({key: len(values) for key, values in query.items()}, {key: 1 for key in query})  # merged, once each
        query = query_of(address)
        self.assertEqual(query["response_type"], "code")
        self.assertEqual(query["client_id"], self.idp.client_id)
        self.assertEqual(query["redirect_uri"], "http://testserver/sso/fake/login/callback")
        self.assertRegex(query["state"], r"^[0-9a-f]{32}$")
        self.assertEqual(query["code_challenge_method"], "S256")
        self.assertRegex(query["code_challenge"], r"^[A-Za-z0-9_-]{43}$")
        self.assertEqual(query["scope"], "openid profile")  # ours, instead of the auth_url's own
        self.assertEqual(query["prompt"], "consent")  # the auth_url's other parameters stay
        response = self.come_back(address)
        self.assertEqual((response.status_code, response["Location"]), (302, "/manage/#users"))
        self.assertEqual(self.whoami()["user"]["username"], "alice")
        self.identity.refresh_from_db()
        self.assertIsNotNone(self.identity.last_login_at)
        form = self.idp.token_forms[-1]
        self.assertEqual(form["redirect_uri"], query["redirect_uri"])
        self.assertEqual((form["client_id"], form["client_secret"], form["grant_type"]), (self.idp.client_id, "hfile-secret", "authorization_code"))
        self.assertEqual(len(form["code_verifier"]), 64)
        entry = LoginLog.objects.get()
        self.assertEqual((entry.result, entry.source, entry.user, entry.username), ("success", "sso", self.alice, "alice"))
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.last_login_ip, "127.0.0.1")

    def test_the_provider_checks_pkce(self):
        address = self.begin()
        pending = self.client.session[flow.PENDING_KEY]
        pending[0]["verifier"] = "x" * 64  # not the one the challenge was made from
        session = self.client.session
        session[flow.PENDING_KEY] = pending
        session.save()
        with self.assertLogs("hfile.sso", "WARNING"):
            response = self.come_back(address)
        self.assertContains(response, "获取 Token 时服务商返回 400", status_code=502)
        self.assertFalse(self.whoami()["authenticated"])

    def test_where_it_goes_afterwards(self):
        for asked, went in (("/files/a?b=1", "/files/a?b=1"), ("//evil.example", "/"), ("https://evil.example/", "/"),
                            ("/\\evil.example", "/"), ("", "/"), ("javascript:alert(1)", "/")):
            with self.subTest(asked=asked):
                self.client.logout()
                address = self.begin(next_path=asked.replace("#", "%23").replace("?", "%3F").replace("&", "%26"))
                self.assertEqual(self.come_back(address)["Location"], went)

    def test_someone_logged_in_already_goes_on(self):
        self.client.force_login(self.alice)
        response = self.client.get("/sso/fake/login?next=/x")
        self.assertEqual((response.status_code, response["Location"]), (302, "/x"))
        self.assertEqual(self.idp.requests, [])

    def test_unknown_and_disabled_providers(self):
        response = self.client.get("/sso/nope/login")
        self.assertContains(response, "这个单点登录不存在或者没有启用", status_code=404)
        address = self.begin()
        self.provider.enabled = False
        self.provider.save()
        self.assertContains(self.client.get("/sso/fake/login"), "没有启用", status_code=404)
        self.assertContains(self.come_back(address), "没有启用", status_code=404)
        self.assertEqual(self.idp.token_forms, [])
        self.assertFalse(self.whoami()["authenticated"])


class StateTests(FakeIdpMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)

    def assertRefused(self, response, text="state"):
        self.assertContains(response, text, status_code=400)
        self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])

    def test_missing_wrong_and_reused(self):
        address = self.begin()
        back = self.idp.approve(address)
        query = query_of(back)
        self.assertRefused(self.client.get("/sso/fake/login/callback", {"code": query["code"]}))
        self.assertRefused(self.client.get("/sso/fake/login/callback", {"code": query["code"], "state": "0" * 32}))
        self.assertRefused(self.client.get("/sso/fake/login/callback", {"code": query["code"], "state": "nonsense"}))
        self.assertEqual(self.idp.token_forms, [])
        self.assertEqual(self.client.get("/sso/fake/login/callback", query).status_code, 302)  # the right one, once
        self.client.logout()
        self.assertRefused(self.client.get("/sso/fake/login/callback", query))  # used once only

    def test_the_same_state_twice_at_once(self):
        address = self.begin()
        pending = self.client.session[flow.PENDING_KEY]
        twin = self.client_class()  # a second request that read the session before the first saved it
        session = twin.session
        session[flow.PENDING_KEY] = pending
        session.save()
        self.assertEqual(self.come_back(address).status_code, 302)
        self.assertContains(self.come_back(address, client=twin), "state", status_code=400)
        self.assertFalse(twin.get("/api/v1/session/").json()["authenticated"])

    def test_a_failed_provider_still_uses_up_the_state(self):
        address = self.begin()
        self.idp.behave(status_token=500)
        with self.assertLogs("hfile.sso", "WARNING"):
            self.assertEqual(self.come_back(address).status_code, 502)
        # Django's SessionMiddleware doesn't save a session on a 5xx: the removal was saved before.
        self.assertNotIn(flow.PENDING_KEY, self.client.session)
        cache.clear()  # as in another process, where the one-use mark isn't
        self.idp.behave()
        self.assertRefused(self.come_back(address))

    def test_another_browser_has_no_state(self):
        address = self.begin()
        other = self.client_class()
        self.assertRefused(self.come_back(address, client=other))

    def test_expired(self):
        address = self.begin()
        with mock.patch("apps.sso.flow.time.time", return_value=time.time() + flow.PENDING_SECONDS + 5):
            self.assertRefused(self.come_back(address), "过期")
        self.assertNotIn(flow.PENDING_KEY, self.client.session)  # taken out all the same

    def test_another_providers_state(self):
        other = self.make_provider(code="other", name="另一个")
        address = self.begin(code="other")
        back = query_of(self.idp.approve(address))
        self.assertRefused(self.client.get("/sso/fake/login/callback", back))
        # The state is gone now, for its own provider too.
        self.assertRefused(self.client.get(f"/sso/{other.code}/login/callback", back))

    def test_at_most_five_pending(self):
        addresses = [self.begin() for _ in range(flow.PENDING_MAX + 1)]
        self.assertEqual(len(self.client.session[flow.PENDING_KEY]), flow.PENDING_MAX)
        self.assertRefused(self.come_back(addresses[0]))  # the oldest went
        self.assertEqual(self.come_back(addresses[-1]).status_code, 302)

    def test_the_provider_said_no(self):
        address = self.begin()
        response = self.come_back(address, error="access_denied", state=query_of(address)["state"])
        self.assertContains(response, "服务商拒绝了这次登录（access_denied）", status_code=400)
        entry = LoginLog.objects.get()
        self.assertEqual((entry.result, entry.reason, entry.source), ("failure", "sso_denied", "sso"))
        address = self.begin()
        response = self.come_back(address, error="<b>Evil</b> text", state=query_of(address)["state"])
        self.assertContains(response, "服务商拒绝了这次登录。", status_code=400)
        self.assertNotIn("Evil", response.content.decode())

    def test_a_bad_code(self):
        address = self.begin()
        response = self.come_back(address, code="a b", state=query_of(address)["state"])
        self.assertContains(response, "授权码", status_code=400)

    def test_secrets_are_masked_in_the_access_log(self):
        self.assertEqual(masking.mask_address("/sso/fake/login/callback", "code=abc123&state=0123456789abcdef"),
                         "/sso/fake/login/callback?code=<code>&state=<state>")


class ProviderAnswerTests(FakeIdpMixin, TestCase):
    """What a provider may answer, and what the page says then: the stage and the status, never what the
    provider wrote."""

    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)

    def fails(self, text, **behaviour):
        self.idp.behave(**behaviour)
        with self.assertLogs("hfile.sso", "WARNING") as logged:
            response = self.login_via_sso()
        self.assertContains(response, text, status_code=502)
        page = response.content.decode()
        for secret in ("secret-in-body", "secret details", "<script>", "hfile-secret", *self.idp.tokens):
            self.assertNotIn(secret, page)
            self.assertNotIn(secret, "\n".join(logged.output))
        self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])
        return response

    def test_statuses(self):
        self.fails("单点登录失败：获取 Token 时服务商返回 500", status_token=500)
        self.fails("单点登录失败：获取 Token 时服务商返回 401", status_token=401)
        self.fails("单点登录失败：获取用户信息时服务商返回 403", status_userinfo=403)
        entry = LoginLog.objects.order_by("id").last()
        self.assertEqual((entry.reason, entry.source), ("sso_provider", "sso"))

    def test_token_types_and_errors(self):
        self.fails("不是 Bearer 类型", token_type="mac")
        self.fails("服务商拒绝了（bad_verification_code）", token_error="bad_verification_code")
        self.fails("服务商拒绝了", token_error="Bad Code With Spaces")
        self.idp.behave(token_type="BEARER")
        self.assertEqual(self.login_via_sso().status_code, 302)  # any case
        self.client.logout()
        self.idp.behave(token_type=None)
        self.assertEqual(self.login_via_sso().status_code, 302)  # left out: fine

    def test_limits(self):
        self.fails("超过 1 MB", big=True)
        self.fails("单点登录不跟随跳转", redirect_userinfo=True)
        self.assertNotIn(("GET", "/elsewhere"), self.idp.requests)
        self.fails("压缩过的", gzip=True)
        self.fails("不是 JSON", not_json=True)
        self.fails("不是 JSON 对象", not_object=True)
        with mock.patch.object(outbound, "TIMEOUT", 0.3):
            self.fails("太久没有回答", slow=1)

    def test_the_whole_exchange_has_a_deadline(self):
        # httpx's timeouts are per read: a "100 Continue", or a byte of the body, every 0.2 s would never end.
        import time as clock

        for path in ("/drip-headers", "/drip-body"):
            with self.subTest(path=path):
                self.provider.userinfo_url = f"{self.idp.base}{path}"
                self.provider.save()
                started = clock.monotonic()
                with mock.patch.object(outbound, "TIMEOUT", 1.0):
                    self.fails("太久没有回答完")
                self.assertLess(clock.monotonic() - started, 4)
                self.client.logout()

    def test_a_provider_that_isnt_there(self):
        self.provider.token_url = "http://127.0.0.1:9/token"  # the discard port: nothing listens
        self.provider.save()
        self.fails("连不上服务商")

    def test_the_id_field(self):
        for claims, text in (
            ({"name": "x"}, "「sub」没有这个字段"), ({"sub": "   "}, "「sub」是空的"), ({"sub": {"id": 1}}, "「sub」是一个对象"),
            ({"sub": True}, "「sub」是 true 或 false"), ({"sub": 1.5}, "「sub」是小数"), ({"sub": None}, "「sub」是 null"),
            ({"sub": ["a"]}, "「sub」是一个列表"), ({"sub": "x" * 300}, "太长了"), ({"sub": "a\x00b"}, "控制字符"),
        ):
            with self.subTest(claims=claims):
                self.idp.behave(claims=claims)
                with self.assertLogs("hfile.sso", "WARNING"):
                    self.assertContains(self.login_via_sso(), text, status_code=502)
                self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])
        self.assertEqual(LoginLog.objects.order_by("id").last().reason, "sso_userinfo")

    def test_invisible_characters_in_ids(self):
        for sub in ("a\u202eb", "a\u200bb", "a\x00b"):
            with self.subTest(sub=sub):
                self.idp.behave(claims={"sub": sub})
                with self.assertLogs("hfile.sso", "WARNING"):
                    self.assertContains(self.login_via_sso(), "不可见字符", status_code=502)

    def test_a_numeric_id(self):
        self.provider.id_field = "$.user['id']"
        self.provider.save()
        Identity.objects.create(provider=self.provider, external_id="12345", user=User.objects.create_user("bob"))
        self.idp.behave(claims={"user": {"id": 12345}})
        self.assertEqual(self.login_via_sso().status_code, 302)
        self.assertEqual(self.client.get("/api/v1/session/").json()["user"]["username"], "bob")

    def test_ids_are_compared_exactly(self):
        # On MySQL text compares without case; identities are found by the id's digest.
        self.idp.behave(claims={"sub": "ALICE-1"})
        self.assertContains(self.login_via_sso(), "还没有绑定用户", status_code=403)
        self.assertEqual(key_of("alice-1"), Identity.objects.get().external_key)

    def test_basic_and_public_clients(self):
        self.idp.basic = True
        try:
            self.provider.token_auth = "basic"
            self.provider.save()
            self.assertEqual(self.login_via_sso().status_code, 302)
            form = self.idp.token_forms[-1]
            self.assertNotIn("client_secret", form)
            self.assertNotIn("client_id", form)
        finally:
            self.idp.basic = False
        self.client.logout()
        self.provider.token_auth = "post"
        self.provider.client_secret = ""
        self.provider.save()
        self.idp.client_secret = ""
        try:
            self.assertEqual(self.login_via_sso().status_code, 302)
            self.assertEqual(set(self.idp.token_forms[-1]), {"grant_type", "code", "redirect_uri", "code_verifier", "client_id"})
        finally:
            self.idp.client_secret = "hfile-secret"

    def test_an_unreadable_secret(self):
        self.provider.client_secret = "enc:not-a-token"
        self.provider.save()
        response = self.login_via_sso()
        self.assertContains(response, "Client Secret 读不出来了", status_code=502)
        self.assertEqual(self.idp.token_forms, [])


class RefusedTests(FakeIdpMixin, TestCase):
    def test_a_disabled_user(self):
        alice = User.objects.create_user("alice", password="pw-alice-2026", is_active=False)
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=alice)
        response = self.login_via_sso()
        self.assertContains(response, "已经停用", status_code=403)
        entry = LoginLog.objects.get()
        self.assertEqual((entry.reason, entry.user, entry.source), ("inactive", alice, "sso"))

    def test_a_builtin_user(self):
        template = User.objects.template()
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=template)  # never made by the site
        self.assertContains(self.login_via_sso(), "内置用户不能登录", status_code=403)
        self.assertEqual(LoginLog.objects.get().reason, "builtin")
        self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])

    def test_not_bound_while_auto_register_is_off(self):
        User.objects.create_user("alice", password="pw-alice-2026")  # the same name: never logged in by it
        response = self.login_via_sso()
        self.assertContains(response, "这个 测试服务商 账号还没有绑定用户", status_code=403)
        self.assertContains(response, "账号绑定", status_code=403)
        self.assertContains(response, "/login/?next=", status_code=403)  # 用密码登录
        self.assertNotContains(response, "alice-1", status_code=403)  # the provider's id isn't shown
        self.assertEqual(User.objects.count(), 3)  # the guest, the template, alice
        entry = LoginLog.objects.get()
        self.assertEqual((entry.reason, entry.username, entry.user), ("sso_not_bound", "fake:alice-1", None))
        self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])

    def test_not_bound_with_a_secure_entry_or_no_login_button(self):
        for values in ({"secure_login_entry": "door-42"}, {"show_login": False}):
            with self.subTest(values=values):
                site(**{"secure_login_entry": "", "show_login": True, **values})
                self.client.logout()
                response = self.login_via_sso()
                self.assertContains(response, "还没有绑定用户", status_code=403)
                self.assertNotContains(response, "door-42", status_code=403)
                self.assertNotContains(response, "用密码登录", status_code=403)


class AutoRegisterTests(FakeIdpMixin, TestCase):
    def setUp(self):
        super().setUp()
        site(sso_auto_register=True)
        self.template = User.objects.template()
        self.template.default_permissions = ["preview", "download"]
        self.template.save()
        self.copies = []
        user_copied.connect(self.copied, dispatch_uid="sso-test")
        self.addCleanup(user_copied.disconnect, dispatch_uid="sso-test")

    def copied(self, sender, source, target, **kwargs):
        self.copies.append((source, target))

    def test_a_new_user(self):
        response = self.login_via_sso()
        self.assertEqual(response.status_code, 302)
        user = User.objects.get(username="alice")
        self.assertEqual((user.kind, user.nickname, user.is_active, user.is_staff, user.is_superuser),
                         (Kind.NORMAL, "爱丽丝", True, False, False))
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.default_permissions, ["preview", "download"])
        self.assertEqual(Identity.objects.get(user=user).external_id, "alice-1")
        self.assertEqual(self.copies, [(self.template, user)])
        self.assertEqual(self.client.get("/api/v1/session/").json()["user"]["username"], "alice")
        # The next time: the same user, nobody new.
        self.client.logout()
        self.assertEqual(self.login_via_sso().status_code, 302)
        self.assertEqual(User.objects.filter(kind=Kind.NORMAL).count(), 1)
        self.assertEqual(len(self.copies), 1)

    def test_the_template_s_grants_are_copied(self):
        # The storages app answers user_copied: the new user can use what the template can.
        from apps.storages.models import Grant, StorageSource

        storage = StorageSource.objects.create(key="docs", name="文档", type="local", config={})
        Grant.objects.update_or_create(user=self.template, storage=storage,
                                       defaults={"root_path": "/public", "permissions": ["preview", "download"], "enabled": True})
        self.assertEqual(self.login_via_sso().status_code, 302)
        grant = Grant.objects.get(user__username="alice", storage=storage)
        self.assertEqual((grant.root_path, grant.permissions, grant.enabled), ("/public", ["preview", "download"], True))

    def test_without_what_the_template_saved_without_its_prerequisite(self):
        # D55: lists saved before, with a permission that misses what it needs first (预览 or 获取直链 without 下载),
        # are copied without it, the default permissions as the grants.
        from apps.storages.models import Grant, StorageSource

        User.objects.filter(pk=self.template.pk).update(default_permissions=["upload", "preview", "generate_link"])
        storage = StorageSource.objects.create(key="docs", name="文档", type="local", config={})
        Grant.objects.update_or_create(user=self.template, storage=storage, defaults={
            "root_path": "/", "permissions": ["upload", "preview", "copy_download_link", "generate_link"], "enabled": True})
        self.assertEqual(self.login_via_sso().status_code, 302)
        user = User.objects.get(username="alice")
        self.assertEqual(user.default_permissions, ["upload"])
        self.assertEqual(Grant.objects.get(user=user, storage=storage).permissions, ["upload"])

    def test_never_by_name_and_names_taken(self):
        alice = User.objects.create_user("Alice", password="pw-alice-2026", is_staff=True)
        self.login_via_sso()
        user = User.objects.get(sso_identities__external_id="alice-1")
        self.assertNotEqual(user, alice)
        self.assertEqual(user.username, "fake_alice-1")
        self.assertFalse(user.is_staff)
        self.client.logout()
        self.login_via_sso(claims={"sub": "alice-2", "preferred_username": "fake_alice-1"})
        self.assertTrue(User.objects.filter(username="fake_alice-2").exists())
        User.objects.create_user("fake_bob")
        User.objects.create_user("fake_bob-2")
        self.client.logout()
        self.login_via_sso(claims={"sub": "bob", "preferred_username": "Alice"})
        self.assertEqual(User.objects.get(sso_identities__external_id="bob").username, "fake_bob-3")

    def test_proposals_that_arent_usernames(self):
        for number, (proposal, expected) in enumerate((
            ("guest", "fake_u0"), ("a b", "fake_u1"), ("x" * 200, "fake_u2"), ("ad​min", "fake_u3"), (42, "42"),
            ("ｂｏｂ", "bob"), ({"x": 1}, "fake_u6"),
        )):
            with self.subTest(proposal=proposal):
                self.client.logout()
                response = self.login_via_sso(claims={"sub": f"u{number}", "preferred_username": proposal})
                self.assertEqual(response.status_code, 302)
                self.assertEqual(User.objects.get(sso_identities__external_id=f"u{number}").username, expected)

    def test_a_megabyte_of_name(self):
        import time as clock

        started = clock.process_time()  # just under the 1 MiB an answer may have (6 bytes each, escaped)
        self.login_via_sso(claims={"sub": "big", "preferred_username": "\ufdfa" * 85_000, "name": "\ufdfa" * 85_000})
        self.assertLess(clock.process_time() - started, 2)
        user = User.objects.get(sso_identities__external_id="big")
        self.assertEqual(user.username, "fake_big")
        self.assertEqual(len(user.nickname), 50)

    def test_ids_that_arent_usernames_and_nicknames(self):
        self.provider.username_field = ""
        self.provider.save()
        self.login_via_sso(claims={"sub": "auth0|5f/7c", "name": " 很长的\n名字​" + "啊" * 80})
        user = User.objects.get(sso_identities__external_id="auth0|5f/7c")
        self.assertEqual(user.username, "fake_auth0_5f_7c")
        self.assertEqual(user.nickname, "很长的 名字" + "啊" * 44)
        # An id that makes no username even then (invisible ones are refused before): its digest.
        self.assertRegex(flow.fallback_username(self.provider, "\u3164"), r"^fake_[0-9a-f]{16}$")

    def test_never_staff_whatever_the_template(self):
        User.objects.filter(pk=self.template.pk).update(is_staff=True, is_superuser=True)  # not something the site allows
        self.login_via_sso()
        user = User.objects.get(username="alice")
        self.assertFalse(user.is_staff or user.is_superuser)


class TwoStepTests(FakeIdpMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        turn_on(self.boss)
        self.identity = Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.boss)

    def finish(self, entry=None):
        self.assertEqual(self.client.get("/api/v1/session/").json()["step"], "totp")
        body = {"step": "totp", "code": code_for(self.boss)}
        if entry:
            body["entry"] = entry
        response = self.client.post("/api/v1/session/", body, content_type="application/json")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertTrue(self.client.get("/api/v1/session/").json()["authenticated"])

    def test_the_login_dialog_asks_for_the_code(self):
        response = self.login_via_sso(next_path="/manage/")
        self.assertEqual((response.status_code, response["Location"]), (302, "/?auth=login&next=/manage/"))
        self.assertFalse(self.client.get("/api/v1/session/").json()["authenticated"])
        self.identity.refresh_from_db()
        self.assertIsNone(self.identity.last_login_at)
        self.finish()
        self.identity.refresh_from_db()
        self.assertIsNotNone(self.identity.last_login_at)
        entry = LoginLog.objects.get()
        self.assertEqual((entry.result, entry.source, entry.user), ("success", "sso", self.boss))

    def test_with_a_secure_entry(self):
        site(secure_login_entry="door-42")
        self.assertEqual(self.begin()[:len(self.idp.base)], self.idp.base)  # single sign-on works with an entry
        response = self.login_via_sso()
        self.assertEqual(response["Location"], "/login/door-42/")
        self.assertEqual(self.client.get("/login/door-42/").status_code, 200)
        wrong = self.client.post("/api/v1/session/", {"step": "totp", "code": code_for(self.boss)}, content_type="application/json")
        self.assertEqual(wrong.status_code, 404)  # the session API wants the entry
        self.finish(entry="door-42")

    def test_a_wrong_code_is_logged_as_single_sign_on(self):
        self.login_via_sso()
        response = self.client.post("/api/v1/session/", {"step": "totp", "code": "000000"}, content_type="application/json")
        self.assertEqual(response.status_code, 400)
        entry = LoginLog.objects.get()
        self.assertEqual((entry.reason, entry.source), ("totp", "sso"))

    def test_someone_else_logged_in_meanwhile(self):
        other = User.objects.create_user("other", password="pw-other-2026")
        address = self.begin()
        self.client.force_login(other)
        response = self.come_back(address)
        self.assertEqual(response["Location"], "/?auth=login")
        data = self.client.get("/api/v1/session/").json()
        self.assertEqual((data["authenticated"], data["step"]), (False, "totp"))


class SessionLifetimeTests(FakeIdpMixin, TestCase):
    """Each GET /sso/<code>/login without a cookie makes a session row: one that holds nothing but the
    pending login lives as long as that (a minute more than the 10 its states have), not
    SESSION_COOKIE_AGE; logging in gives the session the normal lifetime back."""

    SHORT = flow.PENDING_SECONDS + 60

    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        Identity.objects.create(provider=self.provider, external_id="alice-1", user=self.alice)

    def lifetime(self):
        key = self.client.cookies[settings.SESSION_COOKIE_NAME].value
        return (Session.objects.get(session_key=key).expire_date - timezone.now()).total_seconds()

    def cookie_age(self):
        return int(self.client.cookies[settings.SESSION_COOKIE_NAME]["max-age"])

    def test_short_until_logged_in(self):
        address = self.begin()
        self.assertAlmostEqual(self.lifetime(), self.SHORT, delta=30)
        self.assertEqual(self.cookie_age(), self.SHORT)
        self.assertEqual(self.come_back(address).status_code, 302)
        self.assertAlmostEqual(self.lifetime(), settings.SESSION_COOKIE_AGE, delta=30)
        self.assertEqual(self.cookie_age(), settings.SESSION_COOKIE_AGE)

    def test_a_password_login_in_the_same_browser(self):
        self.begin()  # abandoned
        response = self.client.post("/api/v1/session/", {"username": "alice", "password": "pw-alice-2026"},
                                    content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertAlmostEqual(self.lifetime(), settings.SESSION_COOKIE_AGE, delta=30)

    def test_through_two_step_verification(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        turn_on(boss)
        Identity.objects.filter(user=self.alice).update(user=boss)
        self.login_via_sso()
        self.assertAlmostEqual(self.lifetime(), self.SHORT, delta=30)  # still nobody's
        response = self.client.post("/api/v1/session/", {"step": "totp", "code": code_for(boss)}, content_type="application/json")
        self.assertEqual(response.status_code, 201)
        self.assertAlmostEqual(self.lifetime(), settings.SESSION_COOKIE_AGE, delta=30)

    def test_a_session_with_other_things_keeps_its_lifetime(self):
        session = self.client.session
        session["remembered"] = "a folder password, say"
        session.save()
        self.begin()
        self.assertAlmostEqual(self.lifetime(), settings.SESSION_COOKIE_AGE, delta=30)

    def test_a_logged_in_user_binding(self):
        self.client.force_login(User.objects.create_user("nopw"))
        answer = self.client.post("/api/v1/sso/links/fake/", {}, content_type="application/json")
        self.assertEqual(answer.status_code, 200)
        self.assertAlmostEqual(self.lifetime(), settings.SESSION_COOKIE_AGE, delta=30)


class ThrottleTests(FakeIdpMixin, TestCase):
    def test_per_address(self):
        from apps.sso import views

        with mock.patch.object(views.SsoThrottle, "THROTTLE_RATES", {"sso": "3/minute"}):
            for _ in range(3):
                self.assertEqual(self.client.get("/sso/fake/login").status_code, 303)
            response = self.client.get("/sso/fake/login")
            self.assertContains(response, "太频繁了", status_code=429)
            self.assertTrue(response["Retry-After"].isdigit())
            self.assertEqual(self.client.get("/sso/fake/login/callback").status_code, 429)
