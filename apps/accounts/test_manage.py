"""User management on /manage/ (apps/accounts/managing.py, manage_views.py), the Django admin's rules for
users, and what disabling or a new password does to sessions."""

import re
from pathlib import Path

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, TestCase
from rest_framework.test import APIClient, APITestCase

from rest_framework.exceptions import PermissionDenied, ValidationError

from apps.core.testing import admin_url, mysql_only

from . import managing
from .models import Kind
from .permissions import ADMIN_DEFAULTS, ALL
from .signals import user_copied

User = get_user_model()
URL = "/api/v1/manage/users/"
STRONG = "a-Strong-pass-2026"


def logged_in(user):
    """A client with a real session of `user` (not force_authenticate: sessions are what is tested)."""
    client = APIClient()
    client.force_login(user)
    return client


class ManageTestBase(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.alice = User.objects.create_user("alice", password="pw-alice-2026", nickname="小艾")
        self.guest = User.objects.guest()
        self.template = User.objects.template()
        self.client.force_login(self.boss)

    def detail(self, user):
        return f"{URL}{user.pk}/"

    def is_logged_in(self, client):
        return client.get("/api/v1/session/").json()["authenticated"]


class AccessTests(ManageTestBase):
    def test_admins_only(self):
        anonymous = APIClient()
        self.assertEqual(anonymous.get(URL).status_code, 401)
        self.assertEqual(anonymous.post(URL, {"username": "x"}).status_code, 401)
        user = logged_in(self.alice)
        for method, url in (("get", URL), ("post", URL), ("get", self.detail(self.alice)), ("patch", self.detail(self.alice)),
                            ("delete", self.detail(self.guest)), ("post", self.detail(self.alice) + "copy/")):
            with self.subTest(method=method, url=url):
                self.assertEqual(getattr(user, method)(url, {}, format="json").status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.guest.pk).exists())

    def test_the_django_admin_path_forms(self):
        # Permissions are on the view, whatever the address looks like (doubled slashes, ;parameters).
        anonymous = APIClient()
        for url in ("/api/v1//manage/users/", "/api/v1/manage//users/", "/api/v1/manage/users/;x=1/"):
            self.assertIn(anonymous.get(url).status_code, (401, 404), url)

    def test_put_is_not_offered(self):
        self.assertEqual(self.client.put(self.detail(self.alice), {"username": "x"}).status_code, 405)


class ListTests(ManageTestBase):
    def test_everyone_with_the_builtins_marked(self):
        data = self.client.get(URL).json()
        self.assertEqual(data["count"], 4)
        by_name = {user["username"]: user for user in data["results"]}
        self.assertEqual({name: (u["kind"], u["role"], u["role_label"]) for name, u in by_name.items()}, {
            "guest": ("guest", "guest", "匿名用户"),
            "template": ("template", "template", "虚拟新用户"),
            "boss": ("normal", "superuser", "超级管理员"),
            "alice": ("normal", "user", "普通用户"),
        })
        alice = by_name["alice"]
        self.assertEqual((alice["nickname"], alice["has_password"], alice["is_active"]), ("小艾", True, True))
        self.assertNotIn("password", alice)
        self.assertEqual(by_name["boss"]["default_permissions"], list(ADMIN_DEFAULTS))
        self.assertFalse(by_name["guest"]["has_password"])

    def test_what_the_page_may_offer(self):
        by_name = {user["username"]: user for user in self.client.get(URL).json()["results"]}
        self.assertEqual(by_name["alice"]["can"], {"edit": True, "rename": True, "password": True, "toggle": True, "delete": True, "copy": True})
        self.assertEqual(by_name["alice"]["why"], {})
        boss = by_name["boss"]  # the admin looking, and the only superuser
        self.assertEqual(boss["can"], {"edit": True, "rename": True, "password": False, "toggle": False, "delete": False, "copy": True})
        self.assertEqual(boss["why"]["delete"], "不能删除自己的账号。")
        self.assertEqual(boss["why"]["toggle"], "不能停用自己的账号。")
        guest, template = by_name["guest"], by_name["template"]
        self.assertEqual((guest["can"]["rename"], guest["can"]["password"], guest["can"]["toggle"], guest["can"]["delete"]),
                         (False, False, True, False))
        self.assertFalse(template["can"]["toggle"])
        self.assertEqual(template["why"]["toggle"], "虚拟新用户不登录，没有启用和停用。")

    def test_search_filter_and_order(self):
        User.objects.create_user("bob", password=STRONG, nickname="阿宝", is_active=False)
        names = lambda query: [u["username"] for u in self.client.get(URL + query).json()["results"]]  # noqa: E731
        self.assertEqual(names("?search=AL"), ["alice"])
        self.assertEqual(names("?search=阿宝"), ["bob"])
        self.assertEqual(names(f"?search=%23{self.alice.pk}"), ["alice"])
        self.assertEqual(names("?is_active=false"), ["bob"])
        self.assertEqual(names("?kind=guest"), ["guest"])
        self.assertEqual(names("?ordering=-username&kind=normal"), ["boss", "bob", "alice"])
        r = self.client.get(URL + "?ordering=password")
        self.assertEqual(r.status_code, 400)
        self.assertIn("不能按「password」排序", r.json()["ordering"][0])
        self.assertEqual(self.client.get(URL + "?is_active=maybe").status_code, 400)
        self.assertEqual(self.client.get(URL + "?page_size=500").status_code, 400)


class CreateTests(ManageTestBase):
    def test_a_user(self):
        r = self.client.post(URL, {"username": " Ｄｏｒａ ", "nickname": "  朵拉\n ", "password": STRONG,
                                   "default_permissions": ["upload", "new_folder", "upload"]})
        self.assertEqual(r.status_code, 201, r.json())
        dora = User.objects.get(pk=r.json()["id"])
        self.assertEqual((dora.username, dora.nickname, dora.kind, dora.is_active), ("Dora", "朵拉", Kind.NORMAL, True))
        self.assertEqual(dora.default_permissions, ["new_folder", "upload"])
        self.assertTrue(dora.check_password(STRONG))
        self.assertFalse(dora.is_staff or dora.is_superuser)

    def test_a_form_without_is_active(self):
        # DRF takes a switch left out of a form for an unticked checkbox: the user was created disabled (API-06).
        r = self.client.post(URL, {"username": "fiona"}, format="multipart")
        self.assertEqual(r.status_code, 201, r.json())
        self.assertTrue(User.objects.get(username="fiona").is_active)

    def test_unknown_permissions_are_echoed_short(self):
        # The first three, each cut at 40 characters, not whatever was sent (SEC-05).
        r = self.client.post(URL, {"username": "zed", "default_permissions": ["x" * 5000, "fly", "swim", "dive"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["default_permissions"], [f"不认识的权限：{'x' * 40}…、fly、swim 等 4 个"])

    def test_without_a_password_it_cant_log_in(self):
        r = self.client.post(URL, {"username": "erin", "password": ""})
        self.assertEqual(r.status_code, 201)
        erin = User.objects.get(username="erin")
        self.assertFalse(erin.has_usable_password())
        self.assertFalse(r.json()["has_password"])
        self.assertEqual(r.json()["default_permissions"], [])

    def test_mistakes(self):
        for body, field, message in (
            ({}, "username", "要填用户名。"),
            ({"username": "ALICE"}, "username", "这个用户名已经被别人用了。"),
            ({"username": "Guest"}, "username", "「Guest」是内置用户的名字，请换一个。"),
            ({"username": "a b"}, "username", "用户名只能用字母（中文也行）、数字和 @ . + - _。"),
            ({"username": "x" * 151}, "username", "用户名要 1 到 150 个字符。"),
            ({"username": "zed", "nickname": "a​b"}, "nickname", None),
            ({"username": "zed", "password": "12345678"}, "password", None),
            ({"username": "zedekiah", "password": "Zedekiah1"}, "password", None),  # too like the username
            ({"username": "zed", "default_permissions": ["fly"]}, "default_permissions", "不认识的权限：fly"),
            ({"username": "zed", "default_permissions": "upload"}, "default_permissions", "权限应该是一个列表。"),
            ({"username": "zed", "default_permissions": ["preview"]}, "default_permissions",
             "「预览」要先有「下载」的权限。"),  # D55
            ({"username": "zed", "is_active": "sometimes"}, "is_active", None),
        ):
            with self.subTest(body=body):
                r = self.client.post(URL, body)
                self.assertEqual(r.status_code, 400, r.json())
                self.assertIn(field, r.json())
                if message:
                    self.assertEqual(r.json()[field], [message])
        self.assertFalse(User.objects.filter(username__in=["zed", "zedekiah"]).exists())

    def test_never_an_admin(self):
        r = self.client.post(URL, {"username": "mallory", "is_staff": True, "is_superuser": True, "kind": "guest"})
        self.assertEqual(r.status_code, 201)
        mallory = User.objects.get(username="mallory")
        self.assertEqual((mallory.is_staff, mallory.is_superuser, mallory.kind), (False, False, Kind.NORMAL))


class UpdateTests(ManageTestBase):
    def test_names_permissions_and_switch(self):
        r = self.client.patch(self.detail(self.alice), {"username": "alicia", "nickname": "艾丽", "default_permissions": ["download"]})
        self.assertEqual(r.status_code, 200, r.json())
        self.alice.refresh_from_db()
        self.assertEqual((self.alice.username, self.alice.nickname, self.alice.default_permissions), ("alicia", "艾丽", ["download"]))
        self.assertTrue(self.alice.check_password("pw-alice-2026"))  # no password sent: unchanged
        self.client.patch(self.detail(self.alice), {"password": ""})
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password("pw-alice-2026"))
        self.assertEqual(self.client.patch(self.detail(self.alice), {"username": "BOSS"}).json(), {"username": ["这个用户名已经被别人用了。"]})

    def test_permissions_that_need_another_first(self):
        # D55: refused alone; a list saved before comes without them (alone they did nothing, or 预览 since it
        # needs 下载 too).
        r = self.client.patch(self.detail(self.alice), {"default_permissions": ["generate_short_link"]})
        self.assertEqual((r.status_code, r.json()["default_permissions"]), (400, ["「生成短链」要先有「下载」的权限。"]))
        User.objects.filter(pk=self.alice.pk).update(default_permissions=["upload", "preview", "copy_download_link", "generate_link"])
        self.assertEqual(self.client.get(self.detail(self.alice)).json()["default_permissions"], ["upload"])
        r = self.client.patch(self.detail(self.alice), {"default_permissions": ["download", "generate_link", "generate_short_link"]})
        self.assertEqual((r.status_code, r.json()["default_permissions"]), (200, ["download", "generate_link", "generate_short_link"]))

    def test_a_new_password_ends_their_sessions_not_mine(self):
        alices = logged_in(self.alice)
        self.assertTrue(self.is_logged_in(alices))
        r = self.client.patch(self.detail(self.alice), {"password": STRONG})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(self.is_logged_in(alices))
        self.assertTrue(self.is_logged_in(self.client))
        self.alice.refresh_from_db()
        self.assertTrue(self.alice.check_password(STRONG))

    def test_disabling_ends_their_sessions_for_good(self):
        alices = logged_in(self.alice)
        self.assertEqual(self.client.patch(self.detail(self.alice), {"is_active": False}).status_code, 200)
        self.assertFalse(self.is_logged_in(alices))
        self.assertEqual(self.client.patch(self.detail(self.alice), {"is_active": True}).status_code, 200)
        self.assertFalse(self.is_logged_in(alices))  # enabled again: the old session stays gone
        self.assertEqual(APIClient().post("/api/v1/session/", {"username": "alice", "password": "pw-alice-2026"}).status_code, 201)

    def test_my_own_account(self):
        r = self.client.patch(self.detail(self.boss), {"is_active": False})
        self.assertEqual((r.status_code, r.json()), (400, {"is_active": ["不能停用自己的账号。"]}))
        r = self.client.patch(self.detail(self.boss), {"password": STRONG})
        self.assertEqual(r.json(), {"password": ["改自己的密码请用右上角菜单里的「修改密码」。"]})
        r = self.client.patch(self.detail(self.boss), {"nickname": "老板", "default_permissions": list(ALL)})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.is_logged_in(self.client))

    def test_the_builtins(self):
        for user in (self.guest, self.template):
            with self.subTest(user=user.username):
                r = self.client.patch(self.detail(user), {"username": "visitor", "nickname": "访客", "password": STRONG})
                self.assertEqual(r.status_code, 400)
                self.assertEqual(set(r.json()), {"username", "nickname", "password"})
                r = self.client.patch(self.detail(user), {"username": user.username, "default_permissions": ["preview", "download"]})
                self.assertEqual(r.status_code, 200, r.json())  # an unchanged name is fine
                user.refresh_from_db()
                self.assertEqual(user.default_permissions, ["preview", "download"])
                self.assertFalse(user.has_usable_password())
        r = self.client.patch(self.detail(self.template), {"is_active": False})
        self.assertEqual(r.json(), {"is_active": ["虚拟新用户不登录，没有启用和停用。"]})
        self.assertEqual(self.client.patch(self.detail(self.guest), {"is_active": False}).status_code, 200)
        self.guest.refresh_from_db()
        self.assertFalse(self.guest.is_active)  # visitors may do nothing now

    def test_the_last_superuser(self):
        other = User.objects.create_superuser("other", password="pw-other-2026")
        self.assertEqual(self.client.patch(self.detail(other), {"is_active": False}).status_code, 200)
        # Through the API the admin acting is always an active superuser himself, so the last one
        # is his own account; the rule holds for any caller all the same (a superuser who isn't staff).
        actor = User.objects.create_user("root", password=STRONG, is_superuser=True)
        self.assertEqual(managing.rights(actor, self.boss)["toggle"], managing.LAST_DISABLE)
        self.assertEqual(managing.rights(actor, self.boss)["delete"], managing.LAST_DELETE)
        with self.assertRaises(ValidationError) as caught:
            managing.update_user(actor, self.boss.pk, {"is_active": False})
        self.assertEqual(caught.exception.detail, {"is_active": [managing.LAST_DISABLE]})
        with self.assertRaises(PermissionDenied):
            managing.delete_user(actor, self.boss.pk)
        self.assertTrue(User.objects.get(pk=self.boss.pk).is_active)
        managing.update_user(actor, other.pk, {"is_active": True})
        managing.update_user(actor, self.boss.pk, {"is_active": False})  # other is active again
        self.assertFalse(User.objects.get(pk=self.boss.pk).is_active)

    def test_only_superusers_change_other_admins(self):
        staff = User.objects.create_user("staff", password=STRONG, is_staff=True)
        other_staff = User.objects.create_user("staff2", password=STRONG, is_staff=True)
        client = logged_in(staff)
        for target in (self.boss, other_staff):
            with self.subTest(target=target.username):
                r = client.patch(self.detail(target), {"password": "a-Takeover-pass-1"})
                self.assertEqual((r.status_code, r.json()["detail"]), (403, "只有超级管理员可以修改其他管理员的账号。"))
                self.assertEqual(client.delete(self.detail(target)).status_code, 403)
        self.assertTrue(User.objects.get(pk=self.boss.pk).check_password("pw-boss-2026"))
        self.assertEqual(client.patch(self.detail(self.alice), {"nickname": "爱丽丝"}).status_code, 200)
        row = next(u for u in client.get(URL).json()["results"] if u["username"] == "boss")
        self.assertEqual(row["can"]["edit"], False)
        self.assertEqual(row["why"]["edit"], "只有超级管理员可以修改其他管理员的账号。")
        # Nor copy one, as with every other action on another admin's account.
        r = client.post(self.detail(self.boss) + "copy/", {"username": "boss2"})
        self.assertEqual((r.status_code, r.json()["detail"]), (403, "只有超级管理员可以修改其他管理员的账号。"))
        self.assertFalse(User.objects.filter(username="boss2").exists())
        self.assertEqual((row["can"]["copy"], row["why"]["copy"]), (False, "只有超级管理员可以修改其他管理员的账号。"))
        self.assertEqual(client.post(self.detail(self.alice) + "copy/", {"username": "alice2"}).status_code, 201)

    def test_a_user_gone_meanwhile(self):
        url = self.detail(self.alice)
        self.alice.delete()
        self.assertEqual(self.client.patch(url, {"nickname": "x"}).status_code, 404)
        self.assertEqual(self.client.get(f"{URL}abc/").status_code, 404)


class DeleteTests(ManageTestBase):
    def test_a_user(self):
        alices = logged_in(self.alice)
        self.assertEqual(self.client.delete(self.detail(self.alice)).status_code, 204)
        self.assertFalse(User.objects.filter(username="alice").exists())
        self.assertFalse(self.is_logged_in(alices))

    def test_what_cant_be_deleted(self):
        for user, message in ((self.guest, "内置用户不能删除。"), (self.template, "内置用户不能删除。"), (self.boss, "不能删除自己的账号。")):
            with self.subTest(user=user.username):
                r = self.client.delete(self.detail(user))
                self.assertEqual((r.status_code, r.json()["detail"]), (403, message))
        helper = User.objects.create_user("helper", password=STRONG, is_staff=True, is_superuser=True)
        self.boss.is_active = False
        self.boss.save()
        r = logged_in(helper).delete(self.detail(self.boss))
        self.assertEqual(r.status_code, 204)  # a disabled superuser isn't the last active one
        self.assertEqual(User.objects.filter(is_superuser=True).count(), 1)


class CopyTests(ManageTestBase):
    def test_a_copy(self):
        self.alice.default_permissions = ["preview", "download", "custom_share_key"]  # the last saved before D55
        self.alice.is_active = False
        self.alice.save()
        seen = []

        def receiver(sender, source, target, **kwargs):
            seen.append((sender, source.pk, target.pk, connection.in_atomic_block, target.pk is not None))

        user_copied.connect(receiver)
        self.addCleanup(user_copied.disconnect, receiver)
        r = self.client.post(self.detail(self.alice) + "copy/", {"username": "alice2", "nickname": "小艾二号"})
        self.assertEqual(r.status_code, 201, r.json())
        copy = User.objects.get(username="alice2")
        self.assertEqual((copy.nickname, copy.default_permissions, copy.is_active, copy.kind), ("小艾二号", ["preview", "download"], False, Kind.NORMAL))
        self.assertFalse(copy.has_usable_password())  # never the source's password
        self.assertEqual(seen, [(User, self.alice.pk, copy.pk, True, True)])

    def test_with_a_password_and_from_the_builtins(self):
        self.guest.is_active = False
        self.guest.default_permissions = ["preview", "download"]
        self.guest.save()
        r = self.client.post(self.detail(self.guest) + "copy/", {"username": "visitor", "password": STRONG})
        self.assertEqual(r.status_code, 201)
        visitor = User.objects.get(username="visitor")
        self.assertEqual((visitor.kind, visitor.is_active, visitor.default_permissions), (Kind.NORMAL, True, ["preview", "download"]))
        self.assertTrue(visitor.check_password(STRONG))
        r = self.client.post(self.detail(self.boss) + "copy/", {"username": "boss-copy"})
        copy = User.objects.get(username="boss-copy")
        self.assertEqual((copy.is_staff, copy.is_superuser, copy.default_permissions), (False, False, list(ADMIN_DEFAULTS)))

    def test_mistakes(self):
        url = self.detail(self.alice) + "copy/"
        self.assertEqual(self.client.post(url, {}).json(), {"username": ["要填用户名。"]})
        self.assertEqual(self.client.post(url, {"username": "Alice"}).json(), {"username": ["这个用户名已经被别人用了。"]})
        self.assertIn("password", self.client.post(url, {"username": "alice3", "password": "123"}).json())
        self.assertEqual(self.client.post(f"{URL}999999/copy/", {"username": "x1"}).status_code, 404)
        self.assertFalse(User.objects.filter(username__startswith="alice3").exists())


class CollationTests(ManageTestBase):
    """ACCT-01: a username is taken as the unique indexes compare. On MySQL they take ß for ss, æ for
    ae, œ for oe (utf8mb4_0900_ai_ci), where iexact (LIKE, a character at a time) doesn't: making,
    renaming or copying to such a name is 「这个用户名已经被别人用了。」, never a 500."""

    TAKEN = {"username": ["这个用户名已经被别人用了。"]}

    def test_without_case_on_every_database(self):
        self.assertTrue(User.objects.username_taken("ALICE"))
        self.assertFalse(User.objects.username_taken("ALICE", exclude=self.alice.pk))
        self.assertFalse(User.objects.username_taken("alicia"))

    @mysql_only
    def test_names_the_index_takes_as_one(self):
        for existing, new in (("strasse", "straße"), ("strasse", "Straße"), ("aesir", "æsir"), ("oeuvre", "œuvre")):
            with self.subTest(new=new):
                other = User.objects.create_user(existing, password="pw-other-2026")
                try:
                    r = self.client.post(URL, {"username": new})
                    self.assertEqual((r.status_code, r.json()), (400, self.TAKEN))
                    r = self.client.patch(self.detail(self.alice), {"username": new})
                    self.assertEqual((r.status_code, r.json()), (400, self.TAKEN))
                    r = self.client.post(self.detail(self.alice) + "copy/", {"username": new})
                    self.assertEqual((r.status_code, r.json()), (400, self.TAKEN))
                    self.assertTrue(User.objects.username_taken(new))
                finally:
                    other.delete()
        self.alice.refresh_from_db()
        self.assertEqual(self.alice.username, "alice")
        self.assertEqual(User.objects.count(), 4)  # boss, alice, the guest, the template: nobody was made

    @mysql_only
    def test_the_index_has_the_last_word(self):
        """Another request took the name between the check and the save: the index refuses it, and
        that is still 「已经被别人用了」."""
        User.objects.create_user("strasse", password="pw-other-2026")
        with self.assertRaises(ValidationError) as caught:
            managing._save(User(username="straße"))
        self.assertEqual(caught.exception.detail, {"username": ["这个用户名已经被别人用了。"]})


class SuperuserDefaultsTests(TestCase):
    def test_new_superusers_get_the_admin_defaults(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.assertEqual(boss.default_permissions, list(ADMIN_DEFAULTS))
        self.assertEqual(User.objects.create_user("alice").default_permissions, [])


class SessionEpochTests(TestCase):
    def test_a_disabled_account_never_gets_its_sessions_back(self):
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        before = alice.get_session_auth_hash()
        alice.nickname = "小艾"
        alice.save()
        self.assertEqual(alice.get_session_auth_hash(), before)  # other changes keep them
        alice.is_active = False
        alice.save(update_fields=["is_active"])
        alice.refresh_from_db()
        self.assertEqual(alice.session_epoch, 1)
        alice.save()  # still disabled: no second step
        alice.is_active = True
        alice.save()
        self.assertEqual(User.objects.get(pk=alice.pk).session_epoch, 1)
        self.assertNotEqual(alice.get_session_auth_hash(), before)

    def test_epoch_zero_is_djangos_hash(self):
        from django.contrib.auth.base_user import AbstractBaseUser

        alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.assertEqual(alice.get_session_auth_hash(), AbstractBaseUser.get_session_auth_hash(alice))


class DjangoAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.browser = Client()
        self.browser.force_login(self.boss)

    def change(self, user, **changes):
        url = admin_url(f"accounts/user/{user.pk}/change/")
        page = self.browser.get(url)
        form = page.context["adminform"].form
        data = {name: form[name].value() for name in form.fields if form[name].value() is not None}
        for name in ("is_active", "is_staff", "is_superuser"):
            data[name] = "on" if changes.get(name, getattr(user, name)) else ""
        data = {key: value for key, value in data.items() if value != ""}
        data.update({k: v for k, v in changes.items() if k not in ("is_active", "is_staff", "is_superuser")})
        data["date_joined_0"], data["date_joined_1"] = "2026-11-08", "00:00:00"
        return self.browser.post(url, data)

    def test_the_last_superuser_stays(self):
        for change in ({"is_active": False}, {"is_staff": False}, {"is_superuser": False}):
            with self.subTest(change=change):
                r = self.change(self.boss, **change)
                self.assertEqual(r.status_code, 200)  # the form again, with the error
                self.assertContains(r, "不能停用自己的账号，也不能去掉自己的管理员身份。")
        other = User.objects.create_superuser("other", password="pw-other-2026")
        r = self.change(other, is_superuser=False)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(User.objects.get(pk=other.pk).is_superuser)
        self.assertFalse(self.browser.post(admin_url(f"accounts/user/{self.boss.pk}/delete/"), {"post": "yes"}).status_code == 302)
        self.assertTrue(User.objects.filter(pk=self.boss.pk).exists())

    def test_disabling_there_ends_sessions_too(self):
        alice = User.objects.create_user("alice", password="pw-alice-2026")
        alices = logged_in(alice)
        r = self.change(alice, is_active=False)
        self.assertEqual(r.status_code, 302)
        self.assertFalse(alices.get("/api/v1/session/").json()["authenticated"])

    def test_builtins_get_no_password(self):
        guest = User.objects.guest()
        r = self.browser.get(admin_url(f"accounts/user/{guest.pk}/password/"))
        self.assertEqual(r.status_code, 403)
        page = self.browser.get(admin_url(f"accounts/user/{guest.pk}/change/")).content.decode()
        self.assertNotIn('name="username"', page)  # read-only

    def test_staff_who_arent_superusers(self):
        staff = User.objects.create_user("staff", password=STRONG, is_staff=True)
        from django.contrib.auth.models import Permission

        staff.user_permissions.add(*Permission.objects.filter(codename__in=["change_user", "view_user", "delete_user"]))
        browser = Client()
        browser.force_login(staff)
        url = admin_url(f"accounts/user/{self.boss.pk}/change/")
        page = browser.get(url)
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="is_superuser"')  # read-only for them
        self.assertEqual(browser.post(admin_url(f"accounts/user/{self.boss.pk}/delete/"), {"post": "yes"}).status_code, 403)


class PageTests(ManageTestBase):
    def test_the_section(self):
        browser = Client()
        browser.force_login(self.boss)
        page = browser.get("/manage/").content.decode()
        self.assertIn('id="section-users"', page)
        self.assertRegex(page, r'<script src="/static/accounts/js/manage_users\.js\?v=[0-9a-f]{12}"></script>')
        self.assertIn('id="user-dialog"', page)
        for code in ALL:
            self.assertIn(f'<input type="checkbox" name="default_permissions" value="{code}"', page)
        self.assertIn("忽略这个存储源的所有过滤规则", page)  # the permissions' help
        # D55: what a permission needs first, for App.applyRequires, and said under it.
        self.assertIn('value="preview" data-requires="download"', page)
        self.assertIn('value="copy_download_link" data-requires="download"', page)
        self.assertIn('value="generate_link" data-requires="download"', page)
        self.assertIn('value="custom_share_key" data-requires="create_share_link"', page)
        self.assertNotIn('value="download" data-requires', page)
        self.assertIn("要先勾选「下载」。在网页上预览", page)
        for tab in ("基本信息", "默认权限", "存储源权限"):
            self.assertIn(tab, page)


class IconTests(TestCase):
    """The icons accounts' templates and scripts use are in the sprite (apps/web/tests.py checks web's own)."""

    def test_they_exist(self):
        base = Path(settings.BASE_DIR) / "apps"
        sprite = (base / "web" / "static" / "web" / "icons.svg").read_text(encoding="utf-8")
        ids = set(re.findall(r'<symbol id="i-([a-z0-9_]+(?:-fill)?)"', sprite))
        used = set()
        for app in ("accounts", "captchas"):
            for path in (base / app).rglob("*.html"):
                used.update(re.findall(r'\{% icon "([a-z0-9_-]+)"', path.read_text(encoding="utf-8")))
            for path in (base / app).rglob("*.js"):
                for first in re.findall(r"\bicon\(\s*([^,()]*)", path.read_text(encoding="utf-8")):
                    used.update(re.findall(r"'([a-z0-9_]+)'", first))
        self.assertGreater(len(used), 5)
        self.assertEqual(used - ids, set())
