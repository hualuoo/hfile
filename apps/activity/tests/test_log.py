"""Writing entries (apps/activity/log.py), what they say (describe.py) and the login log's rows moved in."""

import importlib
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.db import DatabaseError
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone, translation

from apps.core import net
from apps.storages.models import StorageSource

from .. import describe, log
from ..models import ActivityLog

User = get_user_model()
Kind = ActivityLog.Kind


def during(request):
    """The request as the one being handled (apps.core.middleware.CurrentRequestMiddleware)."""
    token = net.bind_request(request)
    return lambda: net.unbind_request(token)


class RecordTests(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user("alice", password="pw-alice-2026", nickname="爱丽丝")
        request = RequestFactory().get("/", REMOTE_ADDR="203.0.113.9", HTTP_USER_AGENT="Browser/1.0")
        request.user = self.alice
        self.addCleanup(during(request))

    def test_from_the_request_being_handled(self):
        entry = log.record(Kind.TOTP_ON)
        entry.refresh_from_db()
        self.assertEqual((entry.actor, entry.actor_name, entry.ip, entry.user_agent, entry.ip_location),
                         (self.alice, "alice", "203.0.113.9", "Browser/1.0", ""))

    def test_given_and_nobody(self):
        entry = log.record(Kind.RESET_ADMIN, actor=None, target=self.alice, ip=None, user_agent="")
        self.assertEqual((entry.actor, entry.target_name, entry.ip, entry.user_agent), (None, "alice", None, ""))
        self.assertIsNone(log.record(Kind.LOGIN_FAILED, actor=AnonymousUser()).actor)

    def test_outside_a_request(self):
        token = net.bind_request(None)
        self.addCleanup(net.unbind_request, token)
        entry = log.record(Kind.SORT_STORAGES)
        self.assertEqual((entry.actor, entry.ip, entry.user_agent), (None, None, ""))

    def test_names_are_copied(self):
        storage = StorageSource.objects.create(key="disk", name="网盘", type="local")
        entry = log.record(Kind.DELETE, storage=storage, path="/a\x00.txt", target_name="x" * 500, key="k" * 50)
        storage.delete()
        entry.refresh_from_db()
        self.assertEqual((entry.storage, entry.storage_name, entry.path, len(entry.target_name), len(entry.key)),
                         (None, "网盘", "/a.txt", 150, 32))

    def test_many_at_once(self):
        with mock.patch.object(log, "locate", return_value="US") as locate:
            with self.assertNumQueries(1):
                rows = log.record_many(Kind.DELETE, [{"path": "/a"}, {"path": "/b", "detail": {"folder": True}}])
        self.assertEqual(locate.call_count, 1)
        self.assertEqual([(row.path, row.detail, row.ip_location, row.actor) for row in ActivityLog.objects.order_by("id")],
                         [("/a", {}, "US", self.alice), ("/b", {"folder": True}, "US", self.alice)])
        self.assertEqual(log.record_many(Kind.DELETE, []), [])
        self.assertEqual(len(rows), 2)

    def test_quietly(self):
        with mock.patch.object(ActivityLog, "save", side_effect=DatabaseError("full")):
            with self.assertLogs("apps.activity.log", "WARNING"):
                self.assertIsNone(log.record(Kind.MKDIR, path="/x", quiet=True))
            with self.assertRaises(DatabaseError):
                log.record(Kind.MKDIR, path="/x")
        with mock.patch.object(ActivityLog.objects, "bulk_create", side_effect=DatabaseError("full")):
            with self.assertLogs("apps.activity.log", "WARNING"):
                self.assertEqual(log.record_many(Kind.DELETE, [{"path": "/a"}], quiet=True), [])
        self.assertFalse(ActivityLog.objects.exists())

    def test_every_kind_has_a_group(self):
        self.assertEqual(sorted(log.CATEGORY_OF), sorted(Kind.values))


class DescribeTests(TestCase):
    def text(self, kind, detail=None, **fields):
        return describe.describe(ActivityLog(kind=kind, detail=detail or {}, **fields))

    def test_in_both_languages(self):
        cases = [
            (Kind.LOGIN, {"source": "admin"}, {}, "Django admin", "后台"),
            (Kind.LOGIN, {"source": "site"}, {}, "", ""),
            (Kind.LOGIN_FAILED, {"source": "site", "reason": "wrong_password", "username": "Alice"}, {},
             'Wrong password; typed "Alice"', "密码不对；输入的是「Alice」"),
            (Kind.LOGIN_FAILED, {"source": "sso", "reason": "sso_not_bound"}, {}, "Single sign-on: account not linked", "单点登录：账号还没有绑定"),
            (Kind.CHANGE_PASSWORD, {"first": True}, {}, "Set a password (there was none)", "设置了密码（以前没有）"),
            (Kind.RENAME, {}, {"path": "/docs/a.txt", "to_path": "/docs/b.txt"}, "→ b.txt", "→ b.txt"),
            (Kind.MOVE, {"folder": True}, {"path": "/docs/sub", "to_path": "/archive/sub"}, "→ /archive", "→ /archive"),
            (Kind.DELETE, {"folder": True}, {}, "The folder and everything in it", "文件夹，连同里面的东西"),
            (Kind.UPLOAD, {"size": 2048, "replaced": True}, {}, "2.0\xa0KB; Replaced the file that was there", "2.0\xa0KB；替换了原来的文件"),
            (Kind.EDIT, {"size": 5, "via": "onlyoffice"}, {}, "5\xa0bytes; OnlyOffice", "5\xa0字节；OnlyOffice"),
            (Kind.SHORT_LINK, {"expires_at": None}, {}, "Never expires", "永久有效"),
            (Kind.CREATE_SHARE, {"items": {"names": ["a", "b", "c", "d"], "count": 5}, "password": True, "expires_at": None}, {},
             "a, b, c, … (5 in all); With a password; Never expires", "a、b、c 等 5 项；有访问密码；永久有效"),
            (Kind.CLEAR_SHARES, {"count": 2, "everyone": True}, {}, "2 shares, everybody's", "所有人的 2 个分享"),
            (Kind.UPDATE_USER, {"username": ["al", "alice"], "nickname": ["", "爱丽丝"], "password": True, "enabled": False,
                                "permissions": {"added": ["upload"], "removed": ["delete", "nope"]}}, {},
             "Username: al → alice; Nickname: (empty) → 爱丽丝; New password; Disabled; Default permissions: added Upload, removed Delete, nope",
             "用户名：al → alice；昵称：（空） → 爱丽丝；新密码；已停用；默认权限：加上 上传，去掉 删除、nope"),
            (Kind.SET_GRANTS, {"storages": [{"name": "网盘", "change": "added"}, {"name": "S3", "change": "removed"},
                                            {"name": "FTP", "change": "changed", "enabled": False, "root": ["/", "/a"]}]}, {},
             "网盘: allowed; S3: not allowed any more; FTP: Disabled, Root folder: / → /a",
             "网盘：可以用；S3：不能用了；FTP：已停用，根目录：/ → /a"),
            (Kind.UPDATE_STORAGE, {"type": "local", "name": ["a", "b"], "fields": ["proxy_download"], "params": ["root", "gone"]}, {},
             "Name: a → b; Changed: Download through the site, settings (Folder, gone)",
             "名称：a → b；改了：代理下载、参数（文件夹、gone）"),
            (Kind.CREATE_STORAGE, {"type": "no-such-type"}, {}, "no-such-type", "no-such-type"),
            (Kind.UPDATE_FILTERS, {"added": 1, "changed": 0, "removed": 2}, {}, "1 added, 2 removed", "加了 1 条、删了 2 条"),
            (Kind.UPDATE_SETTINGS, {"group": "site", "fields": ["site_name", "favicon"]}, {}, "Site name, Site icon", "站点名称、网站图标"),
            (Kind.UPDATE_PROVIDER, {"provider": "GitHub", "fields": ["client_secret", "enabled"]}, {},
             "GitHub; Changed: Client Secret, Enabled", "GitHub；改了：Client Secret、启用"),
            (Kind.DELETE_SHORT_LINKS, {"count": 3, "expired": True, "logs": 1}, {},
             "3 short links, the expired ones, with 1 download log entry", "3 个短链，过期的，连同 1 条下载日志"),
            (Kind.UPDATE_IP_GEO, {"enabled": False}, {}, "Turned off", "停用"),
            (Kind.DOWNLOAD_IP_GEO, {"built_at": "2026-11-18T07:00:00+00:00"}, {}, "Data of 2026-11-18", "数据日期 2026-11-18"),
            (Kind.DOWNLOAD_IP_GEO, {"built_at": "2026-11-17T16:20:58+00:00"}, {}, "Data of 2026-11-18", "数据日期 2026-11-18"),  # the 18th in Shanghai
            (Kind.FILL_IP_GEO, {"changed": 3, "redo": True, "addresses": 9}, {},
             "Looked up 9 IPs again; the locations of 3 records changed", "重新查了 9 个 IP；3 条记录的归属地有变化"),
            (Kind.RESET_ADMIN, {"totp": True, "enabled": False, "staff": True}, {},
             "New password, two-step verification turned off, can open the Manage page again",
             "新密码，关掉了两步验证，又能打开管理页了"),
        ]
        for kind, detail, fields, english, chinese in cases:
            with self.subTest(kind=kind, detail=detail):
                with translation.override("en"):
                    self.assertEqual(self.text(kind, detail, **fields), english)
                with translation.override("zh-hans"):
                    self.assertEqual(self.text(kind, detail, **fields), chinese)

    @override_settings(TIME_ZONE="Asia/Shanghai")
    def test_times_in_the_sites_zone(self):
        with translation.override("en"):
            self.assertEqual(self.text(Kind.SHORT_LINK, {"expires_at": "2026-11-20T16:30:00+00:00"}), "Expires 2026-11-21 00:30")

    def test_what_isnt_there(self):
        for kind in Kind.values:
            with self.subTest(kind=kind):
                self.assertIsInstance(self.text(kind, {}), str)
                self.assertIsInstance(describe.describe(ActivityLog(kind=kind, detail=["not", "a", "dict"])), str)


class LoginLogMoveTests(TestCase):
    """migrations/0002_login_log: a row of the old accounts.LoginLog, as an entry."""

    def entry(self, **row):
        moving = importlib.import_module("apps.activity.migrations.0002_login_log")
        defaults = {"created_at": timezone.now(), "username": "", "user_id": None, "user__username": None, "ip": "203.0.113.1",
                    "ip_location": "US", "user_agent": "UA", "result": "failure", "reason": "", "source": "site"}
        return moving.entry(ActivityLog, {**defaults, **row})

    def test_a_login(self):
        entry = self.entry(username="alice", user_id=7, user__username="alice", result="success")
        self.assertEqual((entry.kind, entry.actor_id, entry.actor_name, entry.detail, entry.ip, entry.ip_location, entry.user_agent),
                         ("login", 7, "alice", {"source": "site"}, "203.0.113.1", "US", "UA"))

    def test_failed_ones(self):
        typo = self.entry(username="Alice", user_id=7, user__username="alice", reason="wrong_password")
        self.assertEqual((typo.kind, typo.target_id, typo.target_name, typo.detail),
                         ("login_failed", 7, "alice", {"source": "site", "reason": "wrong_password", "username": "Alice"}))
        nobody = self.entry(username="root", reason="unknown_user", source="admin")
        self.assertEqual((nobody.target_id, nobody.target_name, nobody.detail),
                         (None, "root", {"source": "admin", "reason": "unknown_user"}))
        gone = self.entry(username="bob", user_id=None, user__username=None, result="success")
        self.assertEqual((gone.actor_id, gone.actor_name), (None, "bob"))
