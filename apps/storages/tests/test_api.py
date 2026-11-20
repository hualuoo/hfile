"""The storages API: who may call it, saving storages with their connection test and write-only secrets,
the form's buttons (测试连接, helpers, OAuth), rules, grants (contracts 1-3) and the public endpoints."""

import re
import tempfile
from unittest import mock

from django.conf import settings
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import User
from apps.accounts.permissions import ADMIN_DEFAULTS, ALL
from apps.accounts.signals import user_copied
from apps.core import reserved
from apps.core.testing import TO_THE_SECOND
from apps.storages import access, config, instances
from apps.storages.drivers import GROUPS
from apps.storages.drivers.base import Param
from apps.storages.models import FilterRule, FolderPassword, Grant, ReadmeRule, StorageSource

from .fakes import FakeDriver, FakeTypeMixin

API = "/api/v1/"


def fake_config(**values):
    return {"endpoint": "https://fake.example", "port": 21, "passive": True, "mode": "a", **values}


class ApiTestCase(FakeTypeMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.admin = User.objects.create_superuser("boss", password="pw-boss-2026", default_permissions=list(ADMIN_DEFAULTS))
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.client = APIClient(enforce_csrf_checks=False)
        self.client.force_authenticate(user=None)
        self.client.force_login(self.admin)

    def create(self, key="docs", **data):
        body = {"key": key, "name": data.pop("name", "文件"), "type": "fake", "config": fake_config(**data.pop("config", {}))}
        body.update(data)
        response = self.client.post(f"{API}manage/storages/", body, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        return StorageSource.objects.get(pk=response.json()["id"])

    def stored(self, storage, key):
        storage.refresh_from_db()
        return config.decode(FakeDriver, storage.config)[0].get(key)


class PermissionTests(ApiTestCase):
    ADMIN_ONLY = [
        ("get", "manage/storage-types/"), ("get", "manage/storages/"), ("post", "manage/storages/"),
        ("get", "manage/storages/{id}/"), ("patch", "manage/storages/{id}/"), ("delete", "manage/storages/{id}/"),
        ("post", "manage/storages/{id}/enable/"), ("post", "manage/storages/{id}/disable/"),
        ("post", "manage/storages/{id}/check/"), ("post", "manage/storages/{id}/copy/"),
        ("post", "manage/storages/{id}/rotate-link-secret/"), ("post", "manage/storages/{id}/check-path/"),
        ("get", "manage/storages/{id}/filters/"), ("put", "manage/storages/{id}/filters/"),
        ("get", "manage/storages/{id}/passwords/"), ("put", "manage/storages/{id}/passwords/"),
        ("get", "manage/storages/{id}/readmes/"), ("put", "manage/storages/{id}/readmes/"),
        ("post", "manage/storages/{id}/rules/test/"), ("post", "manage/storages/sort/"),
        ("get", "manage/storages/key-check/"), ("post", "manage/storages/test/"), ("post", "manage/storages/helpers/"),
        ("post", "manage/storages/oauth/"), ("get", "manage/storages/drafts/x/"), ("delete", "manage/storages/drafts/x/"),
        ("get", "manage/users/{user}/grants/"), ("put", "manage/users/{user}/grants/"), ("get", "manage/grant-options/"),
    ]

    def test_admins_only(self):
        storage = self.create()
        for user, expected in ((None, 401), (self.alice, 403)):
            client = APIClient()
            if user:
                client.force_login(user)
            for method, path in self.ADMIN_ONLY:
                url = API + path.format(id=storage.pk, user=self.alice.pk)
                with self.subTest(user=user, method=method, url=url):
                    response = getattr(client, method)(url, {}, format="json")
                    self.assertEqual(response.status_code, expected)
        self.assertTrue(StorageSource.objects.filter(pk=storage.pk).exists())

    def test_staff_who_is_not_active_is_refused(self):
        self.admin.is_active = False
        self.admin.save()
        self.assertIn(self.client.get(f"{API}manage/storages/").status_code, (401, 403))

    def test_the_url_forms_of_java_bugs(self):
        client = APIClient()
        for path in ("//api/v1/manage/storages/", "/api/v1//manage/storages/", "/api/v1/manage/storages/;x=1"):
            with self.subTest(path=path):
                self.assertIn(client.get(path).status_code, (401, 403, 404))


class TypesTests(ApiTestCase):
    def test_every_type_with_its_form(self):
        data = self.client.get(f"{API}manage/storage-types/").json()
        types = {item["type"]: item for item in data["types"]}
        self.assertTrue({"local", "s3", "onedrive", "open115", "fake"} <= set(types))
        self.assertEqual(data["oauth_redirect_uri"], "http://testserver/storages/oauth/callback/")
        fake = types["fake"]
        self.assertTrue(fake["oauth"])
        self.assertEqual(fake["helpers"], [{"name": "list", "label": "列出"}, {"name": "qr", "label": "扫码"}])
        token = next(p for p in fake["params"] if p["key"] == "token")
        self.assertTrue(token["secret"])
        self.assertIsNone(token["default"])
        self.assertEqual(types["local"]["options"], {"download_domain": True, "proxy_download": False, "proxy_upload": False})
        self.assertEqual((types["local"]["group"], types["local"]["group_label"]), ("servers", "本地和服务器"))
        self.assertEqual((types["fake"]["group"], types["fake"]["group_label"]), ("", ""))
        groups = [item["group"] for item in data["types"]]
        self.assertEqual(groups, sorted(groups, key=lambda group: list(GROUPS).index(group) if group else len(GROUPS)))
        self.assertEqual(data["types"][-1]["type"], "fake")
        self.assertEqual(types["s3"]["options"]["proxy_download"], True)

    def test_the_add_wizards_steps(self):
        """setup: the params of the wizard's second step (keys, what connecting needs); auth: what its third
        step fills, and how."""
        types = {item["type"]: item for item in self.client.get(f"{API}manage/storage-types/").json()["types"]}
        self.assertEqual(types["onedrive"]["setup"], ["client_id", "client_secret", "tenant"])
        self.assertEqual(types["onedrive"]["auth"], {"kind": "oauth", "params": ["refresh_token"]})
        self.assertEqual(types["sharepoint"]["setup"], ["client_id", "client_secret", "tenant"])  # the site after 授权
        self.assertEqual(types["google-drive"]["setup"], ["client_id", "client_secret"])
        self.assertEqual(types["google-drive"]["auth"], {"kind": "oauth", "params": ["refresh_token"]})
        self.assertEqual(types["open115"]["setup"], ["app_id"])
        self.assertEqual(types["open115"]["auth"], {"kind": "qrcode", "params": ["access_token", "refresh_token", "token_expires_at"]})
        self.assertEqual(types["local"]["setup"], ["root"])
        self.assertEqual(types["local"]["auth"], {"kind": "", "params": []})
        self.assertEqual(types["s3"]["setup"], ["endpoint", "region", "access_key", "secret_key", "bucket", "path_style"])
        self.assertEqual(types["aliyun"]["setup"], ["endpoint", "access_key", "secret_key", "bucket"])
        self.assertEqual(types["ftp"]["setup"], ["host", "port", "username", "password", "encoding", "passive", "tls"])
        self.assertEqual(types["webdav"]["setup"], ["url", "username", "password"])
        self.assertEqual(types["fake"]["setup"], ["endpoint", "port", "mode"])  # no setup_params: the required ones

    @override_settings(SITE_URL="https://files.example.com/")
    def test_the_callback_comes_from_the_site_url(self):
        self.assertEqual(self.client.get(f"{API}manage/storage-types/").json()["oauth_redirect_uri"],
                         "https://files.example.com/storages/oauth/callback/")


class CreateTests(ApiTestCase):
    def test_the_add_wizard_saves_its_keys_off_and_untested(self):
        """"setup" (the wizard's second step): off whatever "enabled" says, no connection test, and the required
        params of its later steps may still be empty; turning it on later tests the connection."""
        FakeDriver.fail_check = "连不上"
        body = {"key": "wiz", "name": "向导", "type": "fake", "enabled": True, "config": {"token": "t-1"}}
        complete = {**body, "config": {"token": "t-1", "endpoint": "https://fake.example"}}
        refused = self.client.post(f"{API}manage/storages/", complete, format="json")
        self.assertEqual(refused.status_code, 400)  # without "setup": the connection is tested first
        self.assertIn("连不上", refused.json()["detail"])
        with mock.patch.object(FakeDriver, "setup_params", ("token",)):
            response = self.client.post(f"{API}manage/storages/", {**body, "setup": True}, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.assertFalse(response.json()["enabled"])
        self.assertIn("grants", response.json())
        storage = StorageSource.objects.get(key="wiz")
        self.assertEqual(self.stored(storage, "token"), "t-1")
        self.assertIsNone(self.stored(storage, "endpoint"))  # required, for a later step
        self.assertEqual(self.stored(storage, "port"), 21)  # the default
        # Without "setup" a required param left empty is asked for, as always.
        missing = self.client.post(f"{API}manage/storages/", {**body, "key": "wiz2", "enabled": False}, format="json")
        self.assertEqual(missing.status_code, 400)
        self.assertIn("endpoint", missing.json()["config"])
        # 其他设置 fills it; 完成 turns it on, testing the connection.
        FakeDriver.fail_check = ""
        filled = self.client.patch(f"{API}manage/storages/{storage.pk}/", {"config": {"endpoint": "https://fake.example"}},
                                   format="json")
        self.assertEqual(filled.status_code, 200, filled.json())
        self.assertFalse(filled.json()["enabled"])
        self.assertEqual(self.client.post(f"{API}manage/storages/{storage.pk}/enable/").status_code, 200)
        storage.refresh_from_db()
        self.assertTrue(storage.enabled)
        self.assertEqual(self.stored(storage, "token"), "t-1")

    def test_setup_still_asks_for_its_own_params(self):
        """"setup" lets only the later steps' required params wait: the second step's own can't be emptied."""
        storage = self.create(enabled=False)
        response = self.client.patch(f"{API}manage/storages/{storage.pk}/", {"setup": True, "config": {"endpoint": ""}},
                                     format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("endpoint", response.json()["config"])

    def test_the_wizard_goes_on_with_its_storage_while_it_is_off(self):
        """保存并继续 (and 授权's tokens) on the storage the wizard saved: the later steps' required params may
        still be empty while it is off (a SharePoint site); once it is on, "setup" means nothing."""
        with mock.patch.object(FakeDriver, "setup_params", ("token",)):
            created = self.client.post(f"{API}manage/storages/", {"key": "wiz", "name": "向导", "type": "fake", "setup": True,
                                                                   "config": {"token": "t-1"}}, format="json")
            self.assertEqual(created.status_code, 201, created.json())
            storage = StorageSource.objects.get(key="wiz")
            url = f"{API}manage/storages/{storage.pk}/"
            again = self.client.patch(url, {"setup": True, "enabled": True, "config": {"token": "t-2"}}, format="json")
            self.assertEqual(again.status_code, 200, again.json())
            self.assertFalse(again.json()["enabled"])  # "setup" keeps it off
            self.assertEqual(self.stored(storage, "token"), "t-2")
            self.assertIsNone(self.stored(storage, "endpoint"))
            refused = self.client.patch(url, {"config": {"token": "t-3"}}, format="json")  # without "setup": asked for
            self.assertEqual(refused.status_code, 400)
            self.assertIn("endpoint", refused.json()["config"])
            on = self.create(key="on")
            emptied = self.client.patch(f"{API}manage/storages/{on.pk}/", {"setup": True, "config": {"endpoint": ""}},
                                        format="json")
            self.assertEqual(emptied.status_code, 400)
            self.assertIn("endpoint", emptied.json()["config"])
            self.assertEqual(self.stored(on, "endpoint"), "https://fake.example")

    def test_create_tests_saves_and_grants(self):
        guest, template = User.objects.guest(), User.objects.template()
        template.default_permissions = ["download"]
        template.save()
        response = self.client.post(f"{API}manage/storages/", {
            "key": " Docs ", "name": "  我的  文件 ", "type": "fake", "remark": "备注\r\n第二行",
            "config": fake_config(token="s3cret"),
        }, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()
        self.assertEqual((data["key"], data["name"], data["remark"]), ("docs", "我的 文件", "备注\n第二行"))
        self.assertNotIn("s3cret", response.content.decode())
        self.assertEqual(data["secrets"]["token"], "set")
        self.assertEqual(data["grants"], {"users": 4, "enabled": 2})  # boss and the template have defaults
        storage = StorageSource.objects.get(key="docs")
        self.assertNotIn("s3cret", str(storage.config))
        self.assertEqual(self.stored(storage, "token"), "s3cret")
        # Contract 3: every user, the guest and the template included, from their default permissions.
        grants = {grant.user_id: grant for grant in Grant.objects.filter(storage=storage)}
        self.assertEqual(set(grants), {self.admin.pk, self.alice.pk, guest.pk, template.pk})
        self.assertEqual((grants[self.admin.pk].enabled, grants[self.admin.pk].permissions), (True, list(ADMIN_DEFAULTS)))
        self.assertEqual((grants[self.alice.pk].enabled, grants[self.alice.pk].permissions), (False, []))
        self.assertEqual((grants[template.pk].enabled, grants[template.pk].root_path), (True, "/"))
        # Tested before saving; the tested instance became the live one.
        self.assertEqual(len(FakeDriver.instances), 1)
        self.assertIs(instances.get(storage), FakeDriver.instances[0])
        self.assertTrue(instances.status(storage.pk)["ok"])

    def test_a_failed_test_saves_nothing(self):
        FakeDriver.fail_check = "连不上 fake.example"
        response = self.client.post(f"{API}manage/storages/", {"key": "docs", "name": "x", "type": "fake",
                                                               "config": fake_config()}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"detail": "连接失败：连不上 fake.example", "code": "check_failed"})
        self.assertFalse(StorageSource.objects.exists())
        self.assertTrue(FakeDriver.instances[0].closed)

    def test_disabled_storages_are_not_tested(self):
        FakeDriver.fail_check = "连不上"
        storage = self.create(enabled=False)
        self.assertFalse(storage.enabled)
        self.assertEqual(FakeDriver.instances, [])

    def test_field_errors(self):
        response = self.client.post(f"{API}manage/storages/", {
            "key": "a b", "name": "", "type": "fake", "link_lifetime": 5, "download_domain": "ftp://x",
            "config": {"endpoint": "", "port": "x", "nope": 1},
        }, format="json")
        self.assertEqual(response.status_code, 400)
        errors = response.json()
        self.assertEqual(errors["key"], ["别名只能用小写字母、数字、- 和 _。"])
        self.assertEqual(errors["name"], ["请填写名称。"])
        self.assertIn("60", errors["link_lifetime"][0])
        response = self.client.post(f"{API}manage/storages/", {"key": "ok", "name": "x", "type": "fake",
                                                               "config": {"endpoint": "", "port": "x", "nope": 1}}, format="json")
        self.assertEqual(response.json()["config"], {"endpoint": ["请填写「地址」。"], "port": ["请填一个整数。"],
                                                     "nope": ["这种存储没有这个参数。"]})
        response = self.client.post(f"{API}manage/storages/", {"key": "ok", "name": "x", "type": "nope"}, format="json")
        self.assertEqual(response.json()["type"], ["不认识这种存储类型。"])

    def test_keys(self):
        self.create("taken")
        for key, message in [("api", "网站自己要用"), ("manage", "网站自己要用"), ("storages", "网站自己要用"),
                             ("admin", "网站自己要用"), (settings.ADMIN_PATH, "后台"), ("TAKEN", "已经有存储源"),
                             ("x" * 33, "32"), ("", "请填写别名"), ("中文", "小写字母")]:
            with self.subTest(key=key):
                response = self.client.post(f"{API}manage/storages/", {"key": key, "name": "x", "type": "fake",
                                                                       "config": fake_config()}, format="json")
                self.assertEqual(response.status_code, 400)
                self.assertIn(message, response.json()["key"][0])
                check = self.client.get(f"{API}manage/storages/key-check/", {"key": key}).json()
                self.assertFalse(check["ok"])
        self.assertTrue(self.client.get(f"{API}manage/storages/key-check/", {"key": "free-1"}).json()["ok"])
        taken = StorageSource.objects.get(key="taken")
        self.assertTrue(self.client.get(f"{API}manage/storages/key-check/", {"key": "taken", "id": taken.pk}).json()["ok"])

    def test_keys_are_reserved_for_the_others(self):
        storage = self.create("photos")
        self.assertIn("存储源", reserved.problem("Photos"))
        self.assertEqual(reserved.problem("photos", own=__import__("apps.storages.serializers", fromlist=["key_taken"]).key_taken), "")
        storage.delete()
        self.assertEqual(reserved.problem("photos"), "")

    def test_a_local_storage(self):
        with tempfile.TemporaryDirectory() as folder:
            response = self.client.post(f"{API}manage/storages/", {"key": "disk", "name": "本地", "type": "local",
                                                                   "config": {"root": folder}, "download_domain": "https://dl.example.com/"},
                                        format="json")
            self.assertEqual(response.status_code, 201, response.json())
            self.assertEqual(response.json()["download_domain"], "https://dl.example.com")
            response = self.client.post(f"{API}manage/storages/", {"key": "disk2", "name": "本地", "type": "local",
                                                                   "config": {"root": folder + "/missing"}}, format="json")
            self.assertEqual(response.status_code, 400)
            self.assertIn("root", response.json()["config"])
        response = self.client.post(f"{API}manage/storages/", {"key": "disk3", "name": "x", "type": "fake",
                                                               "config": fake_config(), "download_domain": "https://x"},
                                    format="json")
        self.assertIn("不用另外设置下载域名", response.json()["download_domain"][0])


class EditTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.storage = self.create(config={"token": "t1", "refresh_token": "r1"})
        self.url = f"{API}manage/storages/{self.storage.pk}/"

    def patch(self, data, status=200):
        response = self.client.patch(self.url, data, format="json")
        self.assertEqual(response.status_code, status, response.json())
        return response.json()

    def test_the_detail_never_has_secrets(self):
        data = self.client.get(self.url).json()
        self.assertEqual(data["secrets"], {"token": "set", "refresh_token": "set", "expires_at": "unset"})
        self.assertNotIn("token", data["config"])
        self.assertNotIn("t1", str(data))
        self.assertNotIn("r1", str(data))
        self.assertNotIn("link_secret", data)
        self.assertEqual(self.client.get(f"{API}manage/storages/99999/").status_code, 404)

    def test_empty_secrets_are_kept(self):
        self.patch({"config": fake_config(token="", port=2121)})
        self.assertEqual((self.stored(self.storage, "token"), self.stored(self.storage, "port")), ("t1", 2121))
        self.patch({"config": fake_config(token="t2")})
        self.assertEqual(self.stored(self.storage, "token"), "t2")
        self.patch({"config": fake_config(), "clear_secrets": ["token"]})
        self.assertIsNone(self.stored(self.storage, "token"))
        self.assertEqual(self.stored(self.storage, "refresh_token"), "r1")

    def test_the_type_is_fixed(self):
        response = self.client.patch(self.url, {"type": "local"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("不能改", response.json()["type"][0])

    def test_new_params_are_tested_and_replace_the_instance(self):
        old = instances.get(self.storage)
        revision = self.storage.config_revision
        self.patch({"config": fake_config(port=2121)})
        self.storage.refresh_from_db()
        self.assertEqual(self.storage.config_revision, revision + 1)
        self.assertIsNot(instances.get(self.storage), old)
        self.assertTrue(old.closed)
        FakeDriver.fail_check = "坏了"
        data = self.patch({"config": fake_config(port=2222)}, status=400)
        self.assertEqual(data["code"], "check_failed")
        self.assertEqual(self.stored(self.storage, "port"), 2121)  # nothing changed

    def test_other_fields_save_without_a_test(self):
        FakeDriver.fail_check = "坏了"
        revision = self.storage.config_revision
        data = self.patch({"name": "新名字", "gallery_default": True, "link_lifetime": 600, "permanent_links": True})
        self.assertEqual((data["name"], data["gallery_default"], data["link_lifetime"]), ("新名字", True, 600))
        self.storage.refresh_from_db()
        self.assertEqual(self.storage.config_revision, revision)
        self.patch({"key": "renamed"})
        self.assertEqual(StorageSource.objects.get(pk=self.storage.pk).key, "renamed")

    def test_downloads_through_the_site_at_once(self):
        data = self.client.get(self.url).json()
        self.assertEqual((data["proxy_connections"], data["proxy_when_full"]), (16, "refuse"))
        data = self.patch({"proxy_download": True, "proxy_connections": 4, "proxy_when_full": "link"})
        self.assertEqual((data["proxy_download"], data["proxy_connections"], data["proxy_when_full"]), (True, 4, "link"))
        for bad, fragment in ((0, "至少 1 个"), (1001, "最多 1000 个"), ("x", "整数")):
            with self.subTest(bad):
                self.assertIn(fragment, self.patch({"proxy_connections": bad}, status=400)["proxy_connections"][0])
        self.assertIn("proxy_when_full", self.patch({"proxy_when_full": "maybe"}, status=400))
        with mock.patch.object(FakeDriver, "can_direct_download", False):  # no link of its own to send to
            self.assertEqual(self.patch({"proxy_when_full": "link"})["proxy_when_full"], "refuse")

    def test_a_token_refreshed_by_the_test_is_saved(self):
        FakeDriver.rotate_on_check = True
        self.patch({"config": fake_config(port=2121)})
        self.assertRegex(self.stored(self.storage, "refresh_token"), r"^refresh-\d$")
        self.assertEqual(self.stored(self.storage, "port"), 2121)

    def test_a_token_refreshed_by_a_failing_save_is_saved_too(self):
        """115: the check refreshes (the stored refresh token is spent), then something else fails."""
        FakeDriver.rotate_on_check = True

        def check_then_fail(driver):
            driver.refresh()
            raise __import__("apps.storages.drivers.base", fromlist=["DriverError"]).DriverError("后来坏了")

        original = FakeDriver.check
        FakeDriver.check = check_then_fail
        try:
            self.patch({"config": fake_config(port=2121)}, status=400)
        finally:
            FakeDriver.check = original
        self.assertEqual(self.stored(self.storage, "refresh_token"), "refresh-1")
        self.assertEqual(self.stored(self.storage, "port"), 21)

    def test_a_token_refreshed_before_a_failing_transaction_is_saved(self):
        """115 again: the test refreshed (the stored token is spent), then the save itself fails (another
        storage took the key a moment ago): the new token is stored, not lost."""
        from django.db import IntegrityError

        FakeDriver.rotate_on_check = True
        with mock.patch.object(StorageSource, "save", side_effect=IntegrityError("taken")):
            data = self.patch({"config": fake_config(port=2121)}, status=400)
        self.assertIn("key", data)
        self.assertEqual(self.stored(self.storage, "refresh_token"), "refresh-1")
        self.assertEqual(self.stored(self.storage, "port"), 21)

    def test_the_new_instance_gets_what_the_old_one_refreshed_meanwhile(self):
        old = instances.get(self.storage)

        def check_while_the_old_one_refreshes(driver):
            if driver is not old:
                old.refresh()  # a request on the old instance, during the candidate's test

        original = FakeDriver.check
        FakeDriver.check = check_while_the_old_one_refreshes
        try:
            self.patch({"config": fake_config(port=2121)})
        finally:
            FakeDriver.check = original
        self.assertEqual(self.stored(self.storage, "refresh_token"), "refresh-1")
        new = instances.get(StorageSource.objects.get(pk=self.storage.pk))
        self.assertIsNot(new, old)
        self.assertEqual(new.config["refresh_token"], "refresh-1")
        self.assertEqual(new.config["port"], 2121)

    def test_a_failed_test_names_the_draft_that_keeps_a_form_token(self):
        FakeDriver.rotate_on_check = True

        def check_then_fail(driver):
            driver.refresh()
            raise __import__("apps.storages.drivers.base", fromlist=["DriverError"]).DriverError("后来坏了")

        original = FakeDriver.check
        FakeDriver.check = check_then_fail
        try:
            data = self.patch({"config": fake_config(port=2121, refresh_token="typed-in")}, status=400)
        finally:
            FakeDriver.check = original
        self.assertEqual(data["code"], "check_failed")
        draft = self.client.get(f"{API}manage/storages/drafts/{data['draft']}/").json()
        self.assertIn("refresh_token", draft["secrets"])  # the typed-in token was spent: the new one is here
        self.assertEqual(self.stored(self.storage, "refresh_token"), "r1")  # the stored one wasn't used

    def test_odd_ids_in_queries(self):
        for url in (f"{API}manage/storages/key-check/?key=abc&id=²", f"{API}manage/grant-options/?user=²"):
            with self.subTest(url=url):
                self.assertLess(self.client.get(url).status_code, 500)

    def test_enable_and_disable(self):
        driver = instances.get(self.storage)
        response = self.client.post(f"{self.url}disable/")
        self.assertFalse(response.json()["enabled"])
        self.assertTrue(driver.closed)
        with self.assertRaises(instances.StorageUnavailable):
            instances.get(StorageSource.objects.get(pk=self.storage.pk))
        FakeDriver.fail_check = "坏了"
        response = self.client.post(f"{self.url}enable/")
        self.assertEqual(response.status_code, 400)
        self.assertIn("启用失败", response.json()["detail"])
        self.assertFalse(StorageSource.objects.get(pk=self.storage.pk).enabled)
        FakeDriver.fail_check = ""
        self.assertTrue(self.client.post(f"{self.url}enable/").json()["enabled"])
        self.assertTrue(StorageSource.objects.get(pk=self.storage.pk).enabled)
        self.assertEqual(self.client.post(f"{API}manage/storages/9999/enable/").status_code, 404)

    def test_recheck(self):
        data = self.client.post(f"{self.url}check/").json()
        self.assertTrue(data["status"]["ok"])
        FakeDriver.fail_check = "令牌过期"
        data = self.client.post(f"{self.url}check/").json()
        self.assertEqual((data["status"]["ok"], data["status"]["message"]), (False, "令牌过期"))
        self.assertFalse(self.client.get(f"{API}manage/storages/").json()[0]["status"]["ok"])

    def test_times_to_the_second(self):
        """TEXT-01: they were UTC with microseconds, 2026-11-13T02:03:04.567890+00:00."""
        StorageSource.objects.filter(pk=self.storage.pk).update(tokens_saved_at=timezone.now())
        self.client.post(f"{self.url}check/")
        data = self.client.get(self.url).json()
        for value in (data["created_at"], data["updated_at"], data["tokens_saved_at"], data["status"]["at"]):
            self.assertRegex(value, TO_THE_SECOND)

    def test_delete_cascades(self):
        FilterRule.objects.create(storage=self.storage, expression="/x")
        FolderPassword.objects.create(storage=self.storage, expression="/y", password="p")
        ReadmeRule.objects.create(storage=self.storage, expression="/z", text="t")
        driver = instances.get(self.storage)
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertFalse(StorageSource.objects.exists())
        self.assertFalse(Grant.objects.exists() or FilterRule.objects.exists() or FolderPassword.objects.exists()
                         or ReadmeRule.objects.exists())
        self.assertTrue(driver.closed)

    def test_copy(self):
        FilterRule.objects.create(storage=self.storage, expression="/x", mode="inaccessible")
        FolderPassword.objects.create(storage=self.storage, expression="/y", password="p")
        ReadmeRule.objects.create(storage=self.storage, expression="/z", text="t", display="dialog")
        # D55: a permission saved before without its prerequisite (custom_share_key) isn't copied.
        Grant.objects.filter(storage=self.storage, user=self.alice).update(root_path="/alice", enabled=True,
                                                                         permissions=["download", "custom_share_key"])
        StorageSource.objects.filter(pk=self.storage.pk).update(proxy_connections=3, proxy_when_full="link")
        response = self.client.post(f"{self.url}copy/", {"key": "Copy-1", "name": "副本"}, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        copy = StorageSource.objects.get(key="copy-1")
        self.assertEqual(self.stored(copy, "token"), "t1")
        self.assertEqual((copy.proxy_connections, copy.proxy_when_full), (3, "link"))
        self.assertNotEqual(copy.link_secret, StorageSource.objects.get(pk=self.storage.pk).link_secret)
        self.assertEqual(list(copy.filter_rules.values_list("expression", "mode")), [("/x", "inaccessible")])
        self.assertEqual(copy.folder_passwords.get().password, "p")
        self.assertEqual(copy.readme_rules.get().display, "dialog")
        alice = Grant.objects.get(storage=copy, user=self.alice)
        self.assertEqual((alice.root_path, alice.enabled, alice.permissions), ("/alice", True, ["download"]))
        response = self.client.post(f"{self.url}copy/", {"key": "copy-1", "name": "副本"}, format="json")
        self.assertIn("已经有存储源", response.json()["key"][0])

    def test_a_115_copy_does_not_share_the_tokens(self):
        from apps.storages.drivers import driver_class

        cls = driver_class("open115")
        source = StorageSource.objects.create(key="p115", name="115", type="open115", config=config.encode(cls, {
            "app_id": "100", "qps": 1, "access_token": "a", "refresh_token": "r", "token_expires_at": 9}))
        response = self.client.post(f"{API}manage/storages/{source.pk}/copy/", {"key": "p115b", "name": "副本"}, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        data = response.json()
        self.assertFalse(data["enabled"])
        self.assertEqual(data["secrets"], {"access_token": "unset", "refresh_token": "unset", "token_expires_at": "unset"})
        self.assertEqual(data["config"]["app_id"], "100")
        plain, _ = config.decode(cls, StorageSource.objects.get(pk=source.pk).config)
        self.assertEqual(plain["refresh_token"], "r")  # the source keeps its own

    def test_sort(self):
        second = self.create("second")
        third = self.create("third")
        ids = [third.pk, self.storage.pk, second.pk]
        data = self.client.post(f"{API}manage/storages/sort/", {"ids": ids}, format="json").json()
        self.assertEqual([item["id"] for item in data], ids)
        self.assertEqual([item["id"] for item in self.client.get(f"{API}manage/storages/").json()], ids)
        response = self.client.post(f"{API}manage/storages/sort/", {"ids": ids[:2]}, format="json")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.post(f"{API}manage/storages/sort/", {"ids": "x"}, format="json").status_code, 400)

    def test_rotating_the_link_secret(self):
        before = self.storage.link_secret
        self.assertEqual(self.client.post(f"{self.url}rotate-link-secret/").status_code, 204)
        self.storage.refresh_from_db()
        self.assertNotEqual(self.storage.link_secret, before)

    def test_check_path(self):
        FakeDriver.tree = {"/docs": None, "/docs/a.txt": b"x"}
        url = f"{self.url}check-path/"
        self.assertTrue(self.client.post(url, {"path": "docs/"}, format="json").json()["is_dir"])
        data = self.client.post(url, {"path": "/nope"}, format="json").json()
        self.assertEqual((data["checked"], data["exists"]), (True, False))
        self.assertFalse(self.client.post(url, {"path": "/docs/a.txt"}, format="json").json()["is_dir"])
        self.assertEqual(self.client.post(url, {"path": "/../x"}, format="json").status_code, 400)


class FormButtonTests(ApiTestCase):
    def test_testing_an_unsaved_form(self):
        response = self.client.post(f"{API}manage/storages/test/", {"type": "fake", "config": fake_config()}, format="json")
        self.assertEqual(response.json()["ok"], True)
        self.assertFalse(StorageSource.objects.exists())
        FakeDriver.fail_check = "坏了"
        self.assertEqual(self.client.post(f"{API}manage/storages/test/", {"type": "fake", "config": fake_config()},
                                          format="json").json()["message"], "坏了")
        response = self.client.post(f"{API}manage/storages/test/", {"type": "fake", "config": {"endpoint": "bad"}}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["config"], {"endpoint": ["地址不对。"]})
        self.assertTrue(all(driver.closed for driver in FakeDriver.instances))

    def test_testing_an_existing_storage_uses_its_secrets(self):
        storage = self.create(config={"refresh_token": "stored-refresh"})
        FakeDriver.rotate_on_check = True
        live = instances.get(storage)
        data = self.client.post(f"{API}manage/storages/test/", {"storage": storage.pk, "config": fake_config(port=99)},
                                format="json").json()
        self.assertTrue(data["ok"])
        # The stored refresh token was spent by the test: the new one is saved and the live instance has it.
        self.assertRegex(self.stored(storage, "refresh_token"), r"^refresh-\d+$")
        self.assertEqual(live.config["refresh_token"], self.stored(storage, "refresh_token"))
        self.assertEqual(self.stored(storage, "port"), 21)  # the form itself is not saved

    def test_what_a_test_learns_goes_back_to_the_form(self):
        FakeDriver.learn_host_key = True
        data = self.client.post(f"{API}manage/storages/test/", {"type": "fake", "config": fake_config()}, format="json").json()
        self.assertEqual(data["changes"], {"host_key": "ssh-ed25519 SHA256:fake"})

    def test_helpers_get_the_origin(self):
        data = self.client.post(f"{API}manage/storages/helpers/", {"type": "fake", "name": "list",
                                                                   "config": {"endpoint": "x"}}, format="json").json()
        self.assertEqual(data["param"], "mode")
        self.assertEqual(data["choices"][1], {"value": "b", "label": "乙"})
        self.assertEqual(data["message"], "来自 http://testserver")
        with override_settings(SITE_URL="https://files.example.com/sub"):
            data = self.client.post(f"{API}manage/storages/helpers/", {"type": "fake", "name": "list",
                                                                       "config": {"endpoint": "x"}}, format="json").json()
        self.assertEqual(data["message"], "来自 https://files.example.com")
        response = self.client.post(f"{API}manage/storages/helpers/", {"type": "fake", "name": "list", "config": {}}, format="json")
        self.assertEqual(response.json()["detail"], "先填地址。")
        response = self.client.post(f"{API}manage/storages/helpers/", {"type": "fake", "name": "rm -rf", "config": {}}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_a_qr_login_keeps_its_secrets_on_the_server(self):
        url = f"{API}manage/storages/helpers/"
        first = self.client.post(url, {"type": "fake", "name": "qr", "config": fake_config()}, format="json")
        data = first.json()
        self.assertEqual((data["poll"], data["image"]), ("qr", "data:image/png;base64,AAAA"))
        self.assertNotIn("pkce-secret", first.content.decode())  # the PKCE verifier stays here
        self.assertNotIn("args", data)
        second = self.client.post(url, {"type": "fake", "name": "qr", "config": fake_config(), "draft": data["draft"],
                                        "poll_token": data["poll_token"]}, format="json")
        done = second.json()
        self.assertEqual(done["secrets"], ["token", "refresh_token", "expires_at"])
        self.assertEqual(done["changes"], {"host_key": "learnt"})
        self.assertNotIn("qr-refresh", second.content.decode())
        again = self.client.post(url, {"type": "fake", "name": "qr", "config": {}, "draft": data["draft"],
                                       "poll_token": data["poll_token"]}, format="json")
        self.assertEqual(again.json()["code"], "poll_expired")  # a poll token works once
        # Saving the form with the draft takes the secrets from it.
        response = self.client.post(f"{API}manage/storages/", {"key": "q", "name": "q", "type": "fake",
                                                               "config": fake_config(), "draft": data["draft"]}, format="json")
        storage = StorageSource.objects.get(pk=response.json()["id"])
        self.assertEqual((self.stored(storage, "refresh_token"), self.stored(storage, "expires_at")), ("qr-refresh", 4242))
        self.assertEqual(self.client.get(f"{API}manage/storages/drafts/{data['draft']}/").status_code, 404)  # dropped

    def test_drafts_are_bound_to_their_form(self):
        url = f"{API}manage/storages/helpers/"
        data = self.client.post(url, {"type": "fake", "name": "qr", "config": fake_config()}, format="json").json()
        done = self.client.post(url, {"type": "fake", "name": "qr", "config": {}, "draft": data["draft"],
                                      "poll_token": data["poll_token"]}, format="json").json()
        shown = self.client.get(f"{API}manage/storages/drafts/{done['draft']}/").json()
        self.assertEqual(shown["secrets"], ["expires_at", "refresh_token", "token"])
        self.assertNotIn("qr-", str(shown))
        other = self.create("other")
        response = self.client.patch(f"{API}manage/storages/{other.pk}/", {"config": fake_config(), "draft": done["draft"]},
                                     format="json")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.stored(other, "refresh_token"))  # a new storage's draft isn't another one's
        self.assertEqual(self.client.delete(f"{API}manage/storages/drafts/{done['draft']}/").status_code, 204)


class OAuthTests(ApiTestCase):
    def start(self, **data):
        body = {"type": "fake", "config": fake_config(), **data}
        response = self.client.post(f"{API}manage/storages/oauth/", body, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        return response.json()

    def test_the_flow(self):
        started = self.start(config=fake_config(token="client-secret-typed"), form={"name": "x"})
        self.assertEqual(started["redirect_uri"], "http://testserver/storages/oauth/callback/")
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        self.assertNotIn("client-secret-typed", started["url"])
        session = self.client.session
        self.assertNotIn("client-secret-typed", str(dict(session)))  # encrypted in the session
        page = self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state})
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "授权成功")
        self.assertNotContains(page, "oauth-refresh")
        self.assertContains(page, f"#storages/new?draft={started['draft']}")
        shown = self.client.get(f"{API}manage/storages/drafts/{started['draft']}/").json()
        self.assertEqual(shown["secrets"], ["refresh_token"])
        self.assertEqual(shown["form"], {"name": "x"})
        # The state works once.
        self.assertEqual(self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state}).status_code, 400)
        response = self.client.post(f"{API}manage/storages/", {"key": "od", "name": "od", "type": "fake",
                                                               "config": fake_config(), "draft": started["draft"]}, format="json")
        storage = StorageSource.objects.get(pk=response.json()["id"])
        self.assertEqual(self.stored(storage, "refresh_token"), "oauth-refresh-for-https://fake.example")

    def test_a_draft_that_expired_meanwhile(self):
        """The tokens go into a new draft, and the page is told its id (and the one it asked for)."""
        started = self.start()
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        session = self.client.session
        session["hfile.storage_drafts"] = {}
        session.save()
        page = self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state})
        self.assertContains(page, "授权成功")
        new = re.search(r'data-draft="([^"]+)"', page.content.decode()).group(1)
        self.assertNotEqual(new, started["draft"])
        self.assertContains(page, f'data-asked="{started["draft"]}"')
        self.assertContains(page, f"#storages/new?draft={new}")
        shown = self.client.get(f"{API}manage/storages/drafts/{new}/").json()
        self.assertEqual(shown["secrets"], ["refresh_token"])

    def test_the_form_kept_meanwhile_has_no_secrets(self):
        started = self.start(form={"name": "x", "config": {"endpoint": "e", "token": "leaked?"}})
        shown = self.client.get(f"{API}manage/storages/drafts/{started['draft']}/").json()
        self.assertEqual(shown["form"], {"name": "x", "config": {"endpoint": "e"}})

    def test_for_an_existing_storage(self):
        storage = self.create()
        started = self.start(storage=storage.pk)
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        page = self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state})
        self.assertContains(page, f"#storages/{storage.pk}?draft={started['draft']}")
        self.client.patch(f"{API}manage/storages/{storage.pk}/", {"config": fake_config(), "draft": started["draft"]},
                          format="json")
        self.assertEqual(self.stored(storage, "refresh_token"), "oauth-refresh-for-https://fake.example")

    def test_from_the_add_wizard(self):
        """Started by the wizard for its saved storage: the callback leads back to the wizard (which saves the
        draft's tokens into the storage; it stays off)."""
        storage = self.create(enabled=False)
        started = self.start(storage=storage.pk, setup=True, config={}, form=None)
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        page = self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state})
        self.assertContains(page, f"#storages/{storage.pk}/setup?draft={started['draft']}")
        self.assertContains(page, "令牌会自动保存")
        self.assertContains(page, "回到添加存储源")  # where the message says it goes
        response = self.client.patch(f"{API}manage/storages/{storage.pk}/", {"draft": started["draft"]}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(response.json()["secrets"]["refresh_token"], "set")
        self.assertFalse(response.json()["enabled"])
        self.assertEqual(self.stored(storage, "refresh_token"), "oauth-refresh-for-https://fake.example")
        self.assertEqual(self.stored(storage, "endpoint"), "https://fake.example")  # the rest as it was
        # Without a saved storage "setup" means nothing: back to the form.
        started = self.start(setup=True)
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        page = self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state})
        self.assertContains(page, f"#storages/new?draft={started['draft']}")
        self.assertContains(page, "点「保存」就生效了")
        self.assertContains(page, "回到存储源设置")

    def test_from_the_add_wizard_before_a_later_step(self):
        """The tokens are saved while a required param of a later step is still empty (a SharePoint site, chosen
        from a list that needs them)."""
        params = (*FakeDriver.params, Param("site", "网站"))
        with mock.patch.object(FakeDriver, "params", params), mock.patch.object(FakeDriver, "setup_params", ("endpoint",)):
            created = self.client.post(f"{API}manage/storages/", {"key": "sp", "name": "sp", "type": "fake", "setup": True,
                                                                   "config": {"endpoint": "https://fake.example"}}, format="json")
            self.assertEqual(created.status_code, 201, created.json())
            storage = StorageSource.objects.get(pk=created.json()["id"])
            started = self.start(storage=storage.pk, setup=True, config={}, form=None)
            state = re.search(r"state=([^&]+)", started["url"]).group(1)
            self.assertContains(self.client.get("/storages/oauth/callback/", {"code": "good-code", "state": state}), "授权成功")
            url = f"{API}manage/storages/{storage.pk}/"
            response = self.client.patch(url, {"draft": started["draft"], "setup": True}, format="json")
            self.assertEqual(response.status_code, 200, response.json())
            self.assertEqual(response.json()["secrets"]["refresh_token"], "set")
            self.assertFalse(response.json()["enabled"])
            # 其他设置 fills the site; 完成 turns it on.
            self.assertEqual(self.client.patch(url, {"config": {"site": "team"}}, format="json").status_code, 200)
            self.assertEqual(self.client.post(f"{url}enable/").status_code, 200)
            self.assertEqual(self.stored(storage, "refresh_token"), "oauth-refresh-for-https://fake.example")

    def test_failures(self):
        started = self.start()
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        page = self.client.get("/storages/oauth/callback/", {"error": "access_denied", "error_description": "用户<拒绝>了",
                                                             "state": state})
        self.assertEqual(page.status_code, 400)
        self.assertContains(page, "用户&lt;拒绝&gt;了", status_code=400)
        started = self.start()
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        page = self.client.get("/storages/oauth/callback/", {"code": "bad-code", "state": state})
        self.assertContains(page, "授权码不对", status_code=400)
        self.assertEqual(self.client.get("/storages/oauth/callback/", {"code": "x", "state": "forged"}).status_code, 400)
        self.assertEqual(self.client.post(f"{API}manage/storages/oauth/", {"type": "fake", "config": {}},
                                          format="json").json()["detail"], "先填地址。")
        self.assertEqual(self.client.post(f"{API}manage/storages/oauth/", {"type": "local", "config": {}},
                                          format="json").json()["detail"], "这种存储不用授权。")

    def test_only_the_admin_who_started_it(self):
        started = self.start()
        state = re.search(r"state=([^&]+)", started["url"]).group(1)
        other = APIClient()
        self.assertEqual(other.get("/storages/oauth/callback/", {"code": "good-code", "state": state}).status_code, 403)
        other.force_login(User.objects.create_superuser("boss2", password="pw-boss-2026"))
        self.assertEqual(other.get("/storages/oauth/callback/", {"code": "good-code", "state": state}).status_code, 400)

    def test_the_code_and_state_are_masked_in_logs(self):
        from apps.core.masking import mask_address

        self.assertEqual(mask_address("/storages/oauth/callback/", "code=abc&state=def"),
                         "/storages/oauth/callback/?code=<code>&state=<state>")


class UnusableSettingsTests(ApiTestCase):
    """STOR-01: a saved storage whose settings can't make a driver any more (its type gone from the program
    or the database edited by hand, the library it needs missing, a value emptied by hand) answers what is
    wrong wherever it is used, never a 500."""

    def setUp(self):
        super().setUp()
        self.storage = self.create()
        self.url = f"{API}manage/storages/{self.storage.pk}/"

    def break_type(self, enabled):
        StorageSource.objects.filter(pk=self.storage.pk).update(type="gone", enabled=enabled)
        instances.close_all()

    def test_a_type_that_is_gone(self):
        self.break_type(enabled=False)
        for method, path, body in (("post", "enable/", {}), ("post", "check/", {}), ("patch", "", {"readme_from_file": True}),
                                   ("post", "copy/", {"key": "docs2", "name": "副本"})):
            with self.subTest(path=path or "PATCH"):
                r = getattr(self.client, method)(self.url + path, body, format="json")
                self.assertEqual((r.status_code, r.json().get("code")), (400, "type_unavailable"), r.json())
                self.assertIn("不认识这种存储类型", r.json()["detail"])
        r = self.client.post(f"{API}manage/storages/test/", {"storage": self.storage.pk, "config": {}}, format="json")
        self.assertEqual((r.status_code, r.json().get("code")), (400, "type_unavailable"))
        self.assertEqual(StorageSource.objects.count(), 1)  # nothing copied
        self.assertEqual(self.client.get(self.url).status_code, 200)  # the admin can still see and delete it

    def test_an_enabled_one_is_unavailable(self):
        self.break_type(enabled=True)
        r = self.client.get(f"{API}files/docs/list/")
        self.assertEqual((r.status_code, r.json()["code"]), (403, "storage_disabled"))
        r = self.client.post(self.url + "check-path/", {"path": "/"}, format="json")
        self.assertEqual((r.status_code, r.json()["checked"]), (200, False))
        self.assertIn("现在用不了", r.json()["message"])
        r = self.client.post(self.url + "check/", {}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"]["ok"], False)

    def test_a_library_that_is_missing(self):
        """Run from the source code without the library the driver needs."""
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        missing = ModuleNotFoundError("No module named 'paramiko'", name="paramiko")
        with mock.patch("apps.storages.drivers.importlib.import_module", side_effect=missing):
            r = self.client.post(self.url + "enable/", {}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (400, "type_unavailable"))
        self.assertIn("缺少它要用的库（paramiko）", r.json()["detail"])

    def test_values_the_driver_cant_start_with(self):
        """A value emptied in the database by hand: the driver fails on it."""
        StorageSource.objects.filter(pk=self.storage.pk).update(enabled=False)
        with mock.patch.object(FakeDriver, "__init__", side_effect=TypeError("'<' not supported")), \
                self.assertLogs("hfile.storages", "ERROR"):
            r = self.client.post(self.url + "enable/", {}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("启动不了", r.json()["detail"])
        self.assertFalse(StorageSource.objects.get(pk=self.storage.pk).enabled)


class RuleApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.storage = self.create()
        self.base = f"{API}manage/storages/{self.storage.pk}/"

    def test_filters(self):
        url = self.base + "filters/"
        rules = [{"expression": " secret/ ", "description": " 秘密 ", "mode": "inaccessible"},
                 {"expression": "*.tmp", "mode": "hidden"}]
        data = self.client.put(url, rules, format="json").json()
        self.assertEqual([(r["expression"], r["description"], r["mode"]) for r in data],
                         [("/secret", "秘密", "inaccessible"), ("/*.tmp", "", "hidden")])
        self.assertEqual(self.client.get(url).json(), data)
        bad = self.client.put(url, [{"expression": "/a/../b"}, {"expression": ""}, {"expression": "/x", "mode": "nope"}],
                              format="json")
        self.assertEqual(bad.status_code, 400)
        errors = bad.json()
        self.assertIn("..", errors["0"]["expression"][0])  # DRF keys a list's errors by position
        self.assertEqual(errors["1"]["expression"], ["表达式不能是空的。"])
        self.assertIn("模式", errors["2"]["mode"][0])
        self.assertEqual(self.client.put(url, [{"expression": "/x"}] * 201, format="json").status_code, 400)
        self.assertEqual(self.client.put(url, [], format="json").json(), [])

    def test_only_a_json_list_replaces_the_rules(self):
        """API-03: DRF reads any form as the empty list, which deletes every rule of the kind (with the
        folder passwords, their folders are open). A body that isn't a JSON list changes nothing."""
        FolderPassword.objects.create(storage=self.storage, expression="/private", password="p")
        refused = {"non_field_errors": ["要用 JSON 发一个列表，每项一个对象。"]}
        for kind in ("filters", "passwords", "readmes"):
            for body, how in (({}, "multipart"), ({"expression": "/x", "password": "p"}, "multipart"),
                              ({"expression": "/x", "password": "p"}, "json"), ("x", "json")):
                with self.subTest(kind=kind, body=body, format=how):
                    r = self.client.put(self.base + kind + "/", body, format=how)
                    self.assertEqual((r.status_code, r.json()), (400, refused))
        self.assertEqual(list(FolderPassword.objects.filter(storage=self.storage).values_list("expression", flat=True)),
                         ["/private"])

    def test_passwords_keep_their_revision_unless_changed(self):
        url = self.base + "passwords/"
        first = self.client.put(url, [{"expression": "/a", "password": " p1 "}, {"expression": "/b", "password": "p2"}],
                                format="json").json()
        self.assertEqual(first[0]["password"], "p1")  # readable for the admin (D44)
        a, b = FolderPassword.objects.get(pk=first[0]["id"]), FolderPassword.objects.get(pk=first[1]["id"])
        self.assertEqual((a.revision, b.revision), (1, 1))
        # Reordered, b's password changed, a's description changed: only b gets a new revision.
        second = self.client.put(url, [{"id": b.pk, "expression": "/b", "password": "p2-new"},
                                       {"id": a.pk, "expression": "/a", "password": "p1", "description": "改了"}],
                                 format="json").json()
        self.assertEqual([r["id"] for r in second], [b.pk, a.pk])
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.revision, b.revision, b.position, a.position), (1, 2, 1, 2))
        third = self.client.put(url, [{"id": a.pk, "expression": "/a/**", "password": "p1"}], format="json").json()
        self.assertEqual(len(third), 1)
        a.refresh_from_db()
        self.assertEqual(a.revision, 2)
        self.assertFalse(FolderPassword.objects.filter(pk=b.pk).exists())
        bad = self.client.put(url, [{"expression": "/a", "password": "  "}], format="json")
        self.assertEqual(bad.json()["0"]["password"], ["请填写密码。"])
        dup = self.client.put(url, [{"id": a.pk, "expression": "/a", "password": "x"}] * 2, format="json")
        self.assertEqual(dup.status_code, 400)

    def test_a_password_sent_as_a_number(self):
        """API-07: 98765432.10 was saved as "98765432.1", and the visitor typing 98765432.10 was refused."""
        r = self.client.put(self.base + "passwords/", [{"expression": "/a", "password": 98765432.10}], format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"0": {"password": ["应该是文字（JSON 里的字符串）。"]}}))
        self.assertFalse(FolderPassword.objects.filter(storage=self.storage).exists())

    def test_another_storages_rule_ids_make_new_rules(self):
        other = self.create("other")
        rule = FolderPassword.objects.create(storage=other, expression="/x", password="p")
        data = self.client.put(self.base + "passwords/", [{"id": rule.pk, "expression": "/mine", "password": "q"}],
                               format="json").json()
        self.assertNotEqual(data[0]["id"], rule.pk)
        rule.refresh_from_db()
        self.assertEqual((rule.storage, rule.expression), (other, "/x"))

    def test_readmes(self):
        url = self.base + "readmes/"
        data = self.client.put(url, [{"expression": "/", "text": "# 你好\r\n", "path_mode": "absolute", "display": "bottom"}],
                               format="json").json()
        self.assertEqual((data[0]["text"], data[0]["path_mode"], data[0]["display"]), ("# 你好\n", "absolute", "bottom"))
        bad = self.client.put(url, [{"expression": "/", "text": ""}], format="json")
        self.assertEqual(bad.json()["0"]["text"], ["请填写文档的内容。"])
        self.client.patch(self.base, {"readme_from_file": True}, format="json")
        self.assertTrue(StorageSource.objects.get(pk=self.storage.pk).readme_from_file)

    def test_the_tester(self):
        url = self.base + "rules/test/"
        data = self.client.post(url, {"kind": "filters", "path": "/Secret./x", "rules": [
            {"expression": "/secret", "mode": "inaccessible"}, {"expression": "**/x", "mode": "hidden"},
            {"expression": "/other", "mode": "disable_download"}]}, format="json").json()
        self.assertEqual(data["canonical"], "/secret/x")
        self.assertEqual(data["matches"], [{"index": 0, "how": "below"}, {"index": 1, "how": "self"}])
        self.assertEqual((data["hidden"], data["inaccessible"], data["download_disabled"]), (True, True, False))
        data = self.client.post(url, {"kind": "passwords", "path": "/a/b/c", "rules": [
            {"expression": "/a/*", "password": "1"}, {"expression": "/a", "password": "2"}]}, format="json").json()
        self.assertEqual(data["decides"], 0)
        data = self.client.post(url, {"kind": "readmes", "path": "/music/rock", "root": "/music", "rules": [
            {"expression": "/music/**", "text": "a", "path_mode": "absolute"}, {"expression": "/rock", "text": "b"}]},
            format="json").json()
        self.assertEqual((data["decides"], data["relative"]), (0, "/rock"))
        FilterRule.objects.create(storage=self.storage, expression="/saved", mode="hidden")
        data = self.client.post(url, {"kind": "filters", "path": "/saved"}, format="json").json()
        self.assertTrue(data["hidden"])
        bad = self.client.post(url, {"kind": "filters", "path": "/a/../b"}, format="json")
        self.assertEqual(bad.status_code, 400)
        bad = self.client.post(url, {"kind": "filters", "path": "/a", "rules": [{"expression": ""}]}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_the_testers_root_has_its_own_errors(self):
        """API-15: a wrong root was answered as an error of the path, which the page shows as the test path's."""
        url = self.base + "rules/test/"
        dots = "路径里不能有 . 或 .. 这样只有点和空格的一段。"
        r = self.client.post(url, {"kind": "readmes", "path": "/music", "root": "/a/../b"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"root": [dots]}))
        r = self.client.post(url, {"kind": "readmes", "path": "/a/../b", "root": "/a/../b"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"path": [dots], "root": [dots]}))
        r = self.client.post(url, {"kind": "readmes", "path": "/a/../b", "root": "/music"}, format="json")
        self.assertEqual((r.status_code, r.json()), (400, {"path": [dots]}))


class GrantApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.one = self.create("one", name="一号")
        self.two = self.create("two", name="二号", enabled=False)
        self.url = f"{API}manage/users/{self.alice.pk}/grants/"

    def test_the_list_has_a_row_per_storage(self):
        Grant.objects.filter(user=self.alice, storage=self.two).delete()
        rows = self.client.get(self.url).json()
        self.assertEqual([row["storage"] for row in rows], [
            {"id": self.one.pk, "key": "one", "name": "一号", "type": "fake", "enabled": True},
            {"id": self.two.pk, "key": "two", "name": "二号", "type": "fake", "enabled": False},
        ])
        self.assertEqual(rows[0], {"storage": rows[0]["storage"], "granted": True, "enabled": False, "root_path": "/",
                                   "permissions": []})
        self.assertEqual((rows[1]["granted"], rows[1]["enabled"], rows[1]["root_path"]), (False, False, "/"))
        self.assertEqual(self.client.get(f"{API}manage/users/99999/grants/").status_code, 404)

    def test_put_replaces(self):
        rows = self.client.get(self.url).json()
        rows[0].update(enabled=True, root_path=" docs// ", permissions=["upload", "download", "upload"])
        refused = self.client.put(self.url, rows, format="json")  # API-12: spaces aren't cut off silently
        self.assertIn("只有空格", refused.json()["0"]["root_path"][0])
        rows[0].update(root_path="docs//")
        rows[1].update(granted=False)
        data = self.client.put(self.url, rows, format="json").json()
        self.assertEqual((data[0]["enabled"], data[0]["root_path"], data[0]["permissions"]), (True, "/docs", ["upload", "download"]))
        self.assertFalse(data[1]["granted"])
        self.assertFalse(Grant.objects.filter(user=self.alice, storage=self.two).exists())
        # The storage may be given by its id; storages left out keep their grants.
        data = self.client.put(self.url, [{"storage": self.two.pk, "enabled": True, "permissions": ["preview", "download"]}],
                               format="json").json()
        self.assertEqual((data[0]["enabled"], data[1]["enabled"], data[1]["permissions"]), (True, True, ["preview", "download"]))

    def test_validation(self):
        bad = self.client.put(self.url, [{"storage": self.one.pk, "root_path": "/a/../b", "permissions": ["fly"]}],
                              format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("根目录不对", bad.json()["0"]["root_path"][0])
        self.assertIn("不认识的权限", bad.json()["0"]["permissions"][0])
        dup = self.client.put(self.url, [{"storage": self.one.pk}, {"storage": {"id": self.one.pk}}], format="json")
        self.assertIn("不止一次", str(dup.json()))
        gone = self.client.put(self.url, [{"storage": 99999}], format="json")
        self.assertIn("不存在", str(gone.json()))
        self.assertIn("应该是", str(self.client.put(self.url, [{"storage": "x"}], format="json").json()))
        self.assertIn("控制字符", str(self.client.put(self.url, [{"storage": self.one.pk, "root_path": "/a\tb"}], format="json").json()))

    def test_an_id_too_large_for_the_database(self):
        """API-09: from 2^63 up an id doesn't fit in the id column: SQLite answered 500, MySQL that the storage
        is gone."""
        for storage in (2**63, {"id": 2**63}, 10**30):
            with self.subTest(storage=storage):
                r = self.client.put(self.url, [{"storage": storage}], format="json")
                self.assertEqual((r.status_code, r.json()), (400, {"0": {"storage": ["应该是存储源的 id。"]}}))
        r = self.client.put(self.url, [{"storage": 2**63 - 1}], format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("已经不存在了", str(r.json()))

    def test_a_permission_needs_what_it_needs_first(self):
        # D55: 预览, 复制下载链接, 获取直链 and 生成短链 need 下载, 自定义分享链接 key 创建分享链接.
        rows = self.client.get(self.url).json()
        rows[0].update(enabled=True, permissions=["preview", "copy_download_link", "generate_link", "custom_share_key"])
        bad = self.client.put(self.url, rows, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(bad.json()["0"]["permissions"], ["「预览」要先有「下载」的权限。", "「复制下载链接」要先有「下载」的权限。",
                                                          "「获取直链」要先有「下载」的权限。",
                                                          "「自定义分享链接 key」要先有「创建分享链接」的权限。"])
        self.assertEqual(Grant.objects.get(user=self.alice, storage=self.one).permissions, [])
        rows[0]["permissions"] = ["custom_share_key", "create_share_link", "generate_link", "download", "copy_download_link"]
        data = self.client.put(self.url, rows, format="json").json()
        self.assertEqual(data[0]["permissions"], ["download", "copy_download_link", "generate_link", "create_share_link",
                                                  "custom_share_key"])

    def test_saved_before_the_prerequisites(self):
        # A grant saved before D55 is listed without what misses its prerequisite (alone it did nothing, or 预览
        # since it needs 下载 too), and saving the list as given keeps the rest.
        Grant.objects.filter(user=self.alice, storage=self.one).update(
            enabled=True, permissions=["upload", "preview", "copy_download_link", "generate_link"])
        rows = self.client.get(self.url).json()
        self.assertEqual(rows[0]["permissions"], ["upload"])
        self.assertEqual(self.client.put(self.url, rows, format="json").status_code, 200)
        self.assertEqual(Grant.objects.get(user=self.alice, storage=self.one).permissions, ["upload"])
        # New storages: default permissions saved before give what is usable of them.
        User.objects.filter(pk=self.alice.pk).update(default_permissions=["upload", "preview", "generate_link"])
        bob = User.objects.create_user("bob", password=None)
        User.objects.filter(pk=bob.pk).update(default_permissions=["generate_short_link"])
        three = self.create("three")
        mine, bobs = (Grant.objects.get(user=user, storage=three) for user in (self.alice, bob))
        self.assertEqual((mine.enabled, mine.permissions), (True, ["upload"]))
        self.assertEqual((bobs.enabled, bobs.permissions), (False, []))

    def test_the_guest_and_the_template_have_grants_too(self):
        for user in (User.objects.guest(), User.objects.template()):
            rows = self.client.get(f"{API}manage/users/{user.pk}/grants/").json()
            self.assertEqual(len(rows), 2)
            rows[0].update(enabled=True, permissions=["download"])
            self.assertTrue(self.client.put(f"{API}manage/users/{user.pk}/grants/", rows, format="json").json()[0]["enabled"])

    def test_options(self):
        data = self.client.get(f"{API}manage/grant-options/", {"user": self.admin.pk}).json()
        self.assertEqual([p["code"] for p in data["permissions"]], list(ALL))
        self.assertEqual(data["permissions"][0]["label"], "新建文件夹")
        self.assertEqual(data["default_permissions"], list(ADMIN_DEFAULTS))
        self.assertNotIn("default_permissions", self.client.get(f"{API}manage/grant-options/").json())
        # D55: what each needs first (one of them), said in its help too.
        by_code = {p["code"]: p for p in data["permissions"]}
        self.assertEqual((by_code["preview"]["requires"], by_code["copy_download_link"]["requires"],
                          by_code["generate_link"]["requires"], by_code["custom_share_key"]["requires"],
                          by_code["upload"]["requires"]),
                         (["download"], ["download"], ["download"], ["create_share_link"], []))
        self.assertEqual(by_code["generate_short_link"]["help"], "要先勾选「下载」。")
        User.objects.filter(pk=self.admin.pk).update(default_permissions=["upload", "preview", "generate_link"])
        data = self.client.get(f"{API}manage/grant-options/", {"user": self.admin.pk}).json()
        self.assertEqual(data["default_permissions"], ["upload"])

    def test_copying_a_user_copies_his_grants(self):
        """Contract 2: accounts sends user_copied inside the transaction that makes the copy."""
        Grant.objects.filter(user=self.alice, storage=self.one).update(enabled=True, root_path="/a",
                                                                       permissions=["download", "custom_share_key"])
        copy = User.objects.create_user("alice2", password=None)
        user_copied.send(sender=User, source=self.alice, target=copy)
        grant = Grant.objects.get(user=copy, storage=self.one)
        self.assertEqual((grant.enabled, grant.root_path, grant.permissions), (True, "/a", ["download"]))
        self.assertEqual(Grant.objects.filter(user=copy).count(), 2)
        user_copied.send(sender=User, source=self.alice, target=copy)  # again: still one per storage
        self.assertEqual(Grant.objects.filter(user=copy).count(), 2)

    def test_deleting_a_user_deletes_his_grants(self):
        self.alice.delete()
        self.assertFalse(Grant.objects.filter(user_id=self.alice.pk).exists())


class PublicTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.storage = self.create("docs", name="文件")
        self.hidden = self.create("hidden", name="隐藏")
        self.visitor = APIClient()

    def test_the_list_for_each_caller(self):
        self.assertEqual(self.visitor.get(f"{API}storages/").json(), [])
        guest = User.objects.guest()
        Grant.objects.filter(user=guest, storage=self.storage).update(enabled=True, permissions=["download"])
        data = self.visitor.get(f"{API}storages/").json()
        self.assertEqual([item["key"] for item in data], ["docs"])
        self.assertEqual(data[0]["permissions"], ["download"])
        self.assertEqual(data[0]["capabilities"]["range"], True)
        self.assertEqual(set(data[0]), {"key", "name", "type", "type_label", "gallery_default", "capabilities", "permissions"})
        admin_list = self.client.get(f"{API}storages/").json()
        self.assertEqual([item["key"] for item in admin_list], ["docs", "hidden"])  # the admin's own grants
        self.assertNotIn("config", str(admin_list))

    def test_unlock(self):
        FolderPassword.objects.create(storage=self.storage, expression="/locked", password="pw")
        guest = User.objects.guest()
        Grant.objects.filter(user=guest, storage=self.storage).update(enabled=True, permissions=["download"])
        url = f"{API}storages/docs/unlock/"
        wrong = self.visitor.post(url, {"path": "/locked", "password": "no"}, format="json")
        self.assertEqual((wrong.status_code, wrong.json()["code"]), (403, "password_wrong"))
        right = self.visitor.post(url, {"path": "/locked/x", "password": "pw", "remember": True}, format="json")
        self.assertEqual(right.status_code, 200)
        self.assertTrue(right.json()["unlocked"])
        self.assertIn(access.COOKIE_NAME, right.cookies)
        self.assertEqual(self.visitor.post(url, {"path": "/open", "password": "pw"}, format="json").json()["unlocked"], False)
        self.assertEqual(self.visitor.post(url, {"path": "/locked", "password": ""}, format="json").status_code, 400)
        forget = self.visitor.delete(f"{API}storages/unlocks/")
        self.assertEqual(forget.status_code, 204)
        self.assertEqual(forget.cookies[access.COOKIE_NAME].value, "")
        missing = self.visitor.post(f"{API}storages/nope/unlock/", {"path": "/", "password": "x"}, format="json")
        self.assertEqual((missing.status_code, missing.json()["code"]), (404, "storage_not_found"))
        nobody = self.visitor.post(f"{API}storages/hidden/unlock/", {"path": "/", "password": "x"}, format="json")
        self.assertEqual((nobody.status_code, nobody.json()["code"]), (401, "login_required"))

    def test_unlock_by_rule_id(self):
        rule = FolderPassword.objects.create(storage=self.storage, expression="**/private", password="pp")
        url = f"{API}storages/docs/unlock/"
        response = self.client.post(url, {"rule": rule.pk, "password": "pp"}, format="json")
        self.assertEqual(response.json(), {"unlocked": True, "rule": rule.pk})
        response = self.client.post(url, {"rule": 99999, "password": "pp"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (404, "rule_not_found"))

    def test_unlock_is_throttled(self):
        FolderPassword.objects.create(storage=self.storage, expression="/locked", password="pw")
        url = f"{API}storages/docs/unlock/"
        for _ in range(access.THROTTLE_FAILURES):
            self.client.post(url, {"path": "/locked", "password": "no"}, format="json")
        response = self.client.post(url, {"path": "/locked", "password": "pw"}, format="json")
        self.assertEqual((response.status_code, response.json()["code"]), (429, "password_throttled"))
        self.assertIn("Retry-After", response)

    def test_unlock_checks_csrf(self):
        FolderPassword.objects.create(storage=self.storage, expression="/locked", password="pw")
        strict = APIClient(enforce_csrf_checks=True)
        response = strict.post(f"{API}storages/docs/unlock/", {"path": "/locked", "password": "pw"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["code"], "csrf_failed")


class LocalFolderTests(ApiTestCase):
    """D50: a local storage reaches any folder the program can read and write. Creating one, changing
    its folder and turning on links out of it are for superusers; other admins may do the rest."""

    def setUp(self):
        super().setUp()
        self.folder = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.folder, True)
        self.other = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, self.other, True)
        response = self.client.post(f"{API}manage/storages/", {"key": "disk", "name": "本地", "type": "local",
                                                               "config": {"root": self.folder}}, format="json")
        self.assertEqual(response.status_code, 201, response.json())
        self.disk = StorageSource.objects.get(pk=response.json()["id"])
        self.staff = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        self.as_staff = APIClient()
        self.as_staff.force_login(self.staff)

    def refused(self, response):
        self.assertEqual(response.status_code, 403, response.json())
        self.assertEqual(response.json()["code"], "superuser_required")
        self.assertIn("超级管理员", response.json()["detail"])
        return response.json()["detail"]

    def test_only_superusers_create_one(self):
        body = {"key": "mine", "name": "我的", "type": "local", "config": {"root": self.other}}
        self.refused(self.as_staff.post(f"{API}manage/storages/", body, format="json"))
        self.assertFalse(StorageSource.objects.filter(key="mine").exists())
        # Testing one first says nothing about the server's folders either.
        detail = self.refused(self.as_staff.post(f"{API}manage/storages/test/", {"type": "local", "config": {"root": "/nope"}},
                                                 format="json"))
        self.assertNotIn("不存在", detail)
        self.assertEqual(self.client.post(f"{API}manage/storages/", body, format="json").status_code, 201)
        # Other types stay open to them.
        self.assertEqual(self.as_staff.post(f"{API}manage/storages/", {"key": "f", "name": "f", "type": "fake",
                                                                       "config": fake_config()}, format="json").status_code, 201)

    def test_only_superusers_change_its_folder_or_let_links_out(self):
        url = f"{API}manage/storages/{self.disk.pk}/"
        self.refused(self.as_staff.patch(url, {"config": {"root": self.other}}, format="json"))
        self.refused(self.as_staff.patch(url, {"config": {"root": self.folder + "/"}}, format="json"))  # another spelling
        self.refused(self.as_staff.patch(url, {"config": {"root": self.folder, "allow_external_symlinks": True}}, format="json"))
        self.refused(self.as_staff.post(f"{API}manage/storages/test/",
                                        {"storage": self.disk.pk, "config": {"root": self.other}}, format="json"))
        self.disk.refresh_from_db()
        self.assertEqual(config.decode(config.driver("local"), self.disk.config)[0]["root"], self.folder)
        # The rest they may: the same folder, its name, a test of it as saved, switching links off, a copy.
        response = self.as_staff.patch(url, {"name": "新名字", "config": {"root": self.folder, "allow_external_symlinks": False}},
                                       format="json")
        self.assertEqual(response.status_code, 200, response.json())
        response = self.as_staff.post(f"{API}manage/storages/test/", {"storage": self.disk.pk, "config": {"root": self.folder}},
                                      format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.assertEqual(self.client.patch(url, {"config": {"root": self.folder, "allow_external_symlinks": True}},
                                           format="json").status_code, 200)
        response = self.as_staff.patch(url, {"config": {"root": self.folder, "allow_external_symlinks": False}}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        copy = self.as_staff.post(f"{API}manage/storages/{self.disk.pk}/copy/", {"key": "disk-2", "name": "副本"}, format="json")
        self.assertEqual(copy.status_code, 201, copy.json())
        self.assertEqual(self.client.patch(url, {"config": {"root": self.other}}, format="json").status_code, 200)

    def test_the_form_knows(self):
        types = self.as_staff.get(f"{API}manage/storage-types/").json()
        self.assertFalse(types["superuser"])
        local = next(t for t in types["types"] if t["type"] == "local")
        self.assertEqual((local["superuser_only"], local["superuser_params"]), (True, ["root", "allow_external_symlinks"]))
        fake = next(t for t in types["types"] if t["type"] == "fake")
        self.assertEqual((fake["superuser_only"], fake["superuser_params"]), (False, []))
        self.assertTrue(self.client.get(f"{API}manage/storage-types/").json()["superuser"])


class GrantRootTests(TestCase):
    def test_spaces_are_part_of_the_name(self):
        """API-12: "/team " is the folder "team ", not "/team"; spaces that make a segment of their own
        are refused rather than cut off."""
        from apps.storages.grants import clean_root
        from django.core.exceptions import ValidationError

        self.assertEqual(clean_root("/team "), "/team ")
        self.assertEqual(clean_root("team/a b"), "/team/a b")
        self.assertEqual((clean_root(""), clean_root(None), clean_root("/")), ("/", "/", "/"))
        for bad in (" /team", "/team/ ", "/a/ /b", "a\nb"):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                clean_root(bad)

    def test_the_length_as_stored(self):
        """API-11: the "/" put in front counts; 1024 characters typed without it were 1025 stored."""
        from apps.storages.grants import clean_root
        from django.core.exceptions import ValidationError

        self.assertEqual(len(clean_root("/" + "a" * 1023)), 1024)
        with self.assertRaises(ValidationError):
            clean_root("a" * 1024)


class OtherAdminsGrantsTests(ApiTestCase):
    """Only superusers change another admin's grants, as with the rest of that admin's account."""

    def setUp(self):
        super().setUp()
        self.one = self.create("one", name="一号")
        self.staff = User.objects.create_user("helper", password="pw-helper-2026", is_staff=True)
        self.colleague = User.objects.create_user("colleague", password="pw-colleague-2026", is_staff=True)
        self.as_staff = APIClient()
        self.as_staff.force_login(self.staff)

    def put(self, client, user, **row):
        body = [{"storage": self.one.pk, "granted": True, "enabled": True, "root_path": "/", "permissions": [], **row}]
        return client.put(f"{API}manage/users/{user.pk}/grants/", body, format="json")

    def test_staff_cant_change_other_admins(self):
        for target in (self.admin, self.colleague):
            with self.subTest(target=target.username):
                before = list(Grant.objects.filter(user=target).values_list("enabled", "root_path", "permissions"))
                response = self.put(self.as_staff, target, granted=False)
                self.assertEqual(response.status_code, 403)
                self.assertIn("超级管理员", response.json()["detail"])
                self.assertEqual(self.put(self.as_staff, target, root_path="/x").status_code, 403)
                self.assertEqual(list(Grant.objects.filter(user=target).values_list("enabled", "root_path", "permissions")), before)
                self.assertEqual(self.as_staff.get(f"{API}manage/users/{target.pk}/grants/").status_code, 200)

    def test_a_form_is_refused(self):
        """Like the rules (API-03): DRF would read a form as the empty list, and answer 200 for nothing."""
        r = self.client.put(f"{API}manage/users/{self.alice.pk}/grants/", {"storage": self.one.pk}, format="multipart")
        self.assertEqual((r.status_code, r.json()), (400, {"non_field_errors": ["要用 JSON 发一个列表，每项一个对象。"]}))

    def test_what_stays_allowed(self):
        self.assertEqual(self.put(self.as_staff, self.staff, root_path="/own").status_code, 200)  # their own
        self.assertEqual(self.put(self.as_staff, self.alice, root_path="/a").status_code, 200)  # not an admin
        self.assertEqual(self.put(self.client, self.colleague, root_path="/c").status_code, 200)  # a superuser
        self.assertEqual(Grant.objects.get(user=self.colleague, storage=self.one).root_path, "/c")
