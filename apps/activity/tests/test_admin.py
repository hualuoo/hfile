"""Admins' changes and account changes in the activity log: what changed, never a password or a secret, in
the same transaction as the change (refused or failed: no entry)."""

from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from apps.accounts.test_totp import turn_on
from apps.links.models import DownloadLog
from apps.links.tests.helpers import LinksTestCase
from apps.shares.tests.helpers import SharesTestCase
from apps.siteconf.models import SiteSettings
from apps.sso.models import Identity, Provider
from apps.sso.tests.test_manage import PROVIDERS, new_provider
from apps.storages.models import Grant
from apps.storages.tests.test_api import API, ApiTestCase

from ..models import ActivityLog
from ..testing import entries

User = get_user_model()


def changes(kind):
    return [entry.detail for entry in entries(kind)]


class StorageTests(ApiTestCase):
    def test_new_changed_and_deleted(self):
        storage = self.create("docs", name="文件")
        entry = entries("create_storage")[0]
        self.assertEqual((entry.actor, entry.storage, entry.storage_name, entry.detail), (self.admin, storage, "文件", {"type": "fake"}))
        url = f"{API}manage/storages/{storage.pk}/"
        saved = self.client.patch(url, {"name": "资料", "config": {"token": "tok-secret-1", "port": 2121}}, format="json")
        self.assertEqual(saved.status_code, 200, saved.json())
        self.assertEqual(changes("update_storage"), [{"type": "fake", "name": ["文件", "资料"], "params": ["token", "port"]}])
        self.client.patch(url, {"name": "资料", "remark": "", "config": {"port": 2121}}, format="json")  # nothing new
        self.client.patch(url, {"key": "docs2", "proxy_download": True}, format="json")
        self.assertEqual(changes("update_storage")[1:], [{"type": "fake", "key": ["docs", "docs2"], "fields": ["proxy_download"]}])
        self.assertNotIn("tok-secret-1", repr(list(ActivityLog.objects.values())))
        self.assertEqual(self.client.delete(url).status_code, 204)
        gone = entries("delete_storage")[0]
        self.assertEqual((gone.storage, gone.storage_name, gone.detail), (None, "资料", {"type": "fake"}))
        self.assertEqual({entry.storage for entry in entries()}, {None})  # the link goes, the name stays

    def test_a_failed_connection_test_logs_nothing(self):
        storage = self.create("docs")
        from apps.storages.tests.fakes import FakeDriver

        FakeDriver.fail_check = "refused"
        response = self.client.patch(f"{API}manage/storages/{storage.pk}/", {"config": {"port": 99}}, format="json")
        self.assertEqual(response.status_code, 400, response.json())
        self.assertEqual(entries("update_storage"), [])

    def test_switches_copies_order_and_the_link_secret(self):
        docs, music = self.create("docs"), self.create("music")
        for action in ("disable", "disable", "enable"):
            self.assertEqual(self.client.post(f"{API}manage/storages/{docs.pk}/{action}/").status_code, 200)
        self.assertEqual([entry.kind for entry in entries() if entry.kind.endswith("_storage")][2:],
                         ["disable_storage", "enable_storage"])
        copy = self.client.post(f"{API}manage/storages/{docs.pk}/copy/", {"key": "docs-copy", "name": "副本"}, format="json")
        self.assertEqual(copy.status_code, 201, copy.json())
        entry = entries("copy_storage")[0]
        self.assertEqual((entry.storage.key, entry.detail), ("docs-copy", {"from": "文件", "type": "fake"}))
        order = [music.pk, docs.pk, copy.json()["id"]]
        self.client.post(f"{API}manage/storages/sort/", {"ids": order}, format="json")
        self.client.post(f"{API}manage/storages/sort/", {"ids": order}, format="json")  # the same order: nothing to log
        self.assertEqual(len(entries("sort_storages")), 1)
        self.assertEqual(self.client.post(f"{API}manage/storages/{docs.pk}/rotate-link-secret/").status_code, 204)
        self.assertEqual(entries("rotate_link_secret")[0].storage, docs)

    def test_rules(self):
        storage = self.create("docs")
        url = f"{API}manage/storages/{storage.pk}/passwords/"
        rule = {"expression": "/secret", "description": "", "password": "folder-pw-1"}
        saved = self.client.put(url, [rule, {**rule, "expression": "/other"}], format="json").json()
        self.client.put(url, [{**saved[0], "password": "folder-pw-2"}], format="json")
        self.client.put(url, [{**saved[0], "password": "folder-pw-2"}], format="json")  # the same: nothing changes
        self.assertEqual(changes("update_passwords"), [{"added": 2, "changed": 0, "removed": 0},
                                                       {"added": 0, "changed": 1, "removed": 1}])
        self.assertNotIn("folder-pw", repr(list(ActivityLog.objects.values())))

    def test_a_users_grants(self):
        docs, music = self.create("docs"), self.create("music")
        Grant.objects.filter(user=self.alice).delete()  # the ones new storages gave everybody
        url = f"{API}manage/users/{self.alice.pk}/grants/"
        row = lambda storage, **more: {"storage": storage.pk, "granted": True, "enabled": True, "root_path": "/",  # noqa: E731
                                       "permissions": ["download"], **more}
        self.assertEqual(self.client.put(url, [row(docs)], format="json").status_code, 200)
        self.client.put(url, [row(docs, root_path="/a", permissions=["download", "preview"]), row(music, granted=False)], format="json")
        self.client.put(url, [row(docs, granted=False)], format="json")
        found = entries("set_grants")
        self.assertEqual([(entry.target, entry.detail) for entry in found], [
            (self.alice, {"storages": [{"storage": docs.pk, "change": "added", "name": "文件"}]}),
            (self.alice, {"storages": [{"storage": docs.pk, "change": "changed", "root": ["/", "/a"],
                                        "permissions": {"added": ["preview"], "removed": []}, "name": "文件"}]}),
            (self.alice, {"storages": [{"storage": docs.pk, "change": "removed", "name": "文件"}]}),
        ])


class UserTests(APITestCase):
    URL = "/api/v1/manage/users/"

    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client.force_authenticate(self.boss)

    def test_new_changed_copied_deleted(self):
        made = self.client.post(self.URL, {"username": "carol", "is_active": False}, format="json")
        self.assertEqual(made.status_code, 201, made.json())
        carol = User.objects.get(username="carol")
        self.assertEqual([(entry.actor, entry.target, entry.detail) for entry in entries("create_user")],
                         [(self.boss, carol, {"enabled": False, "password": False})])
        url = f"{self.URL}{carol.pk}/"
        response = self.client.patch(url, {"username": "carol2", "nickname": "卡萝", "is_active": True, "password": "New-pass-2026x",
                                           "default_permissions": ["download", "preview"]}, format="json")
        self.assertEqual(response.status_code, 200, response.json())
        self.client.patch(url, {"username": "carol2", "nickname": "卡萝"}, format="json")  # nothing changes
        self.client.patch(url, {"default_permissions": ["download"]}, format="json")
        self.assertEqual(changes("update_user"), [
            {"username": ["carol", "carol2"], "nickname": ["", "卡萝"], "enabled": True,
             "permissions": {"added": ["preview", "download"], "removed": []}, "password": True},
            {"permissions": {"added": [], "removed": ["preview"]}},
        ])
        self.assertEqual(entries("update_user")[0].target_name, "carol2")
        self.assertNotIn("New-pass-2026x", repr(list(ActivityLog.objects.values())))
        copied = self.client.post(f"{url}copy/", {"username": "dave"}, format="json")
        self.assertEqual(copied.status_code, 201, copied.json())
        self.assertEqual([(entry.target.username, entry.detail) for entry in entries("copy_user")], [("dave", {"from": "carol2"})])
        self.assertEqual(self.client.delete(url).status_code, 204)
        gone = entries("delete_user")[0]
        self.assertEqual((gone.target, gone.target_name), (None, "carol2"))

    def test_a_refused_change_logs_nothing(self):
        self.assertEqual(self.client.patch(f"{self.URL}{self.boss.pk}/", {"is_active": False}, format="json").status_code, 400)
        self.assertEqual(self.client.delete(f"{self.URL}{self.boss.pk}/").status_code, 403)
        self.assertFalse(ActivityLog.objects.exists())


class AccountTests(APITestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.client.force_authenticate(self.alice)

    def test_passwords(self):
        r = self.client.put("/api/v1/users/me/password/", {"old_password": "pw-alice-2026", "new_password": "Fresh-pass-2026"}, format="json")
        self.assertEqual(r.status_code, 204, r.content)
        self.alice.set_unusable_password()
        self.alice.save()
        r = self.client.put("/api/v1/users/me/password/", {"new_password": "First-pass-2026"}, format="json")
        self.assertEqual(r.status_code, 204, r.content)
        self.client.put("/api/v1/users/me/password/", {"old_password": "wrong", "new_password": "Other-pass-2026"}, format="json")
        self.assertEqual([(entry.actor, entry.detail) for entry in entries("change_password")], [(self.alice, {}), (self.alice, {"first": True})])

    def test_two_step_verification_off(self):
        turn_on(self.alice)
        r = self.client.post("/api/v1/users/me/totp/disable/", {"password": "pw-alice-2026"}, format="json")
        self.assertEqual(r.status_code, 200, r.content)
        self.client.post("/api/v1/users/me/totp/disable/", {"password": "pw-alice-2026"}, format="json")  # off already
        self.assertEqual([entry.actor for entry in entries("totp_off")], [self.alice])

    def test_single_sign_on(self):
        provider = Provider.objects.create(code="fake", name="Fake", client_id="c", auth_url="https://idp.example/a",
                                           token_url="https://idp.example/t", userinfo_url="https://idp.example/u")
        Identity.objects.create(provider=provider, user=self.alice, external_id="alice-1")
        self.assertEqual(self.client.delete("/api/v1/sso/links/fake/").status_code, 204)
        self.assertEqual(changes("unlink_sso"), [{"provider": "Fake", "code": "fake"}])


class SsoAdminTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.alice = User.objects.create_user("alice", password="pw-alice-2026")
        self.client.force_authenticate(self.boss)

    def test_providers(self):
        made = self.client.post(PROVIDERS, new_provider(), format="json").json()
        url = f"{PROVIDERS}{made['id']}/"
        self.client.patch(url, {"name": "GitLab", "client_secret": "new-s3cret-value", "scope": "openid email"}, format="json")
        self.client.patch(url, {"name": "GitLab"}, format="json")  # nothing changes
        other = self.client.post(PROVIDERS, new_provider(code="other", name="Other"), format="json").json()
        self.client.post(f"{PROVIDERS}order/", {"ids": [other["id"], made["id"]]}, format="json")
        self.client.delete(url)
        self.assertEqual([(entry.kind, entry.detail) for entry in entries()], [
            ("create_provider", {"provider": "GitLab", "code": "gitlab"}),
            ("update_provider", {"provider": "GitLab", "code": "gitlab", "fields": ["client_secret", "scope"]}),
            ("create_provider", {"provider": "Other", "code": "other"}),
            ("sort_providers", {}),
            ("delete_provider", {"provider": "GitLab", "code": "gitlab"}),
        ])
        self.assertNotIn("s3cret", repr(list(ActivityLog.objects.values())))

    def test_linking_for_a_user(self):
        provider = Provider.objects.create(code="fake", name="Fake", client_id="c", auth_url="https://idp.example/a",
                                           token_url="https://idp.example/t", userinfo_url="https://idp.example/u")
        url = f"/api/v1/manage/users/{self.alice.pk}/identities/"
        made = self.client.post(url, {"provider": provider.pk, "external_id": "alice-9"}, format="json")
        self.assertEqual(made.status_code, 201, made.json())
        self.client.delete(f"{url}{made.json()['id']}/")
        self.assertEqual([(entry.kind, entry.target, entry.detail) for entry in entries()], [
            ("link_identity", self.alice, {"provider": "Fake", "code": "fake", "external_id": "alice-9"}),
            ("unlink_identity", self.alice, {"provider": "Fake", "code": "fake", "external_id": "alice-9"}),
        ])


class SettingsTests(APITestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.client.force_authenticate(self.boss)

    def test_pages_of_settings(self):
        url = "/api/v1/manage/settings/"
        current = SiteSettings.load().site_name
        self.assertEqual(self.client.patch(f"{url}site/", {"site_name": current}, format="json").status_code, 200)  # the same
        self.client.patch(f"{url}site/", {"site_name": "我的网盘", "icp": "京ICP备1号"}, format="json")
        self.client.patch(f"{url}view/", {"onlyoffice_secret": "oo-secret-1"}, format="json")
        self.client.patch(f"{url}security/", {"login_captcha": "maybe"}, format="json")  # refused
        self.assertEqual(changes("update_settings"), [{"group": "site", "fields": ["site_name", "icp"]},
                                                      {"group": "view", "fields": ["onlyoffice_secret"]}])
        self.assertNotIn("oo-secret-1", repr(list(ActivityLog.objects.values())))

    def test_ip_location(self):
        url = "/api/v1/manage/ip-geo/"
        for enabled in (True, True, False):
            self.client.patch(url, {"enabled": enabled}, format="json")
        self.assertEqual(changes("update_ip_geo"), [{"enabled": True}, {"enabled": False}])


class LinkAdminTests(LinksTestCase):
    def setUp(self):
        super().setUp()
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        self.admin = APIClient()
        self.admin.force_authenticate(self.boss)

    def test_short_links_and_the_download_log(self):
        mine = self.short_link_of(self.alice, "/docs/a.txt", key="Mine0001")
        self.short_link_of(self.alice, "/top.txt", key="Old00001", expires_at=timezone.now() - timezone.timedelta(days=1))
        others = [self.short_link_of(self.alice, key=f"Many{n:04d}") for n in range(2)]
        self.assertEqual(self.admin.delete(f"/api/v1/manage/short-links/{mine.pk}/").status_code, 200)
        self.admin.post("/api/v1/manage/short-links/delete/", {"ids": [link.pk for link in others]}, format="json")
        self.admin.post("/api/v1/manage/short-links/delete-expired/")
        self.admin.post("/api/v1/manage/short-links/delete-expired/")  # nothing left to delete
        DownloadLog.objects.create(kind="page", storage=self.storage, path="/top.txt")
        self.admin.post("/api/v1/manage/download-logs/delete/", {"ids": list(DownloadLog.objects.values_list("pk", flat=True))}, format="json")
        found = [(entry.kind, entry.target, entry.key, entry.path, entry.detail) for entry in entries() if entry.actor == self.boss]
        self.assertEqual(found, [
            ("delete_short_links", self.alice, "Mine0001", "/docs/a.txt", {"count": 1}),
            ("delete_short_links", None, "", "", {"count": 2}),
            ("delete_short_links", None, "", "", {"count": 1, "expired": True, "logs": 0}),
            ("delete_download_logs", None, "", "", {"count": 1}),
        ])


class ShareAdminTests(SharesTestCase):
    def test_others_shares(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026")
        admin = APIClient()
        admin.force_authenticate(boss)
        share = self.share()
        self.share(expires_at=timezone.now() - timezone.timedelta(days=1))
        self.assertEqual(admin.delete(f"/api/v1/manage/shares/{share.key}/").status_code, 204)
        self.assertEqual(admin.post("/api/v1/manage/shares/clear-expired/").json(), {"deleted": 1})
        self.assertEqual([(entry.kind, entry.actor, entry.target, entry.key) for entry in entries()],
                         [("delete_share", boss, self.alice, share.key), ("clear_shares", boss, None, "")])
        self.assertEqual(entries("clear_shares")[0].detail, {"count": 1, "everyone": True})


class CommandTests(TestCase):
    def test_resetadmin(self):
        boss = User.objects.create_superuser("boss", password="pw-boss-2026", is_active=False)
        turn_on(boss)
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("getpass.getpass", return_value="Brand-new-pass-2026"):
            call_command("resetadmin", "boss", stdout=StringIO())
        entry = ActivityLog.objects.get()
        self.assertEqual((entry.kind, entry.actor, entry.target, entry.ip, entry.detail),
                         ("reset_admin", None, boss, None, {"totp": True, "enabled": True, "staff": False}))

    def test_installing(self):
        from apps.installer import service

        with mock.patch.object(service.upgrades, "upgrade"):
            admin = service.install(username="first", password="First-admin-2026", site_name="站")
        entry = ActivityLog.objects.get()
        self.assertEqual((entry.kind, entry.actor), ("install", admin))
