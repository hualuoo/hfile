"""hfile resetadmin: a superuser's new password, their two-step verification off."""

import io
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.siteconf.models import SiteSettings

from .test_totp import turn_on

User = get_user_model()
NEW = "a-New-admin-pass-2026"
COMMAND = "apps.accounts.management.commands.resetadmin"


def run(*args, passwords=(NEW, NEW), answers=(), tty=True):
    """Runs the command as if typed at a terminal: getpass() gives `passwords`, input() `answers`."""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch(f"{COMMAND}.sys.stdin") as stdin, \
            mock.patch(f"{COMMAND}.getpass.getpass", side_effect=list(passwords)), \
            mock.patch("builtins.input", side_effect=list(answers)):
        stdin.isatty.return_value = tty
        call_command("resetadmin", *args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class ResetAdminTests(TestCase):
    def setUp(self):
        self.boss = User.objects.create_superuser("boss", password="pw-boss-2026", nickname="老板")

    def test_the_only_superuser(self):
        turn_on(self.boss)
        self.boss.is_active = False
        self.boss.save()
        session = APIClient()
        session.force_login(self.boss)
        out, _ = run()
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.check_password(NEW))
        self.assertEqual((self.boss.totp_secret, self.boss.totp_last_step, self.boss.is_active), ("", None, True))
        self.assertIn("已经给 boss 设了新密码", out)
        self.assertIn("两步验证已经关掉了", out)
        self.assertIn("原来是停用的", out)
        self.assertIn("登录地址：/login/", out)
        self.assertNotIn(NEW, out)
        self.assertFalse(session.get("/api/v1/session/").json()["authenticated"])
        r = APIClient().post("/api/v1/session/", {"username": "boss", "password": NEW}, format="json")
        self.assertEqual(r.status_code, 201)  # no two-step verification any more

    def test_several_to_choose_from(self):
        other = User.objects.create_superuser("other", password="pw-other-2026")
        with self.assertRaisesMessage(CommandError, "有 2 个超级管理员，请写上用户名"):
            run(tty=False)
        out, err = run(answers=("9", "other"))
        self.assertIn("1. boss（老板）", out)
        self.assertIn("没有这一个", err)
        other.refresh_from_db()
        self.assertTrue(other.check_password(NEW))
        run(answers=("1",), passwords=("b-New-pass-2026",) * 2)
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.check_password("b-New-pass-2026"))

    def test_by_name(self):
        User.objects.create_superuser("other", password="pw-other-2026")
        run("BOSS")
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.check_password(NEW))
        User.objects.create_user("alice", password="pw-alice-2026")
        with self.assertRaisesMessage(CommandError, "「alice」不是超级管理员。超级管理员有：boss、other"):
            run("alice")
        with self.assertRaisesMessage(CommandError, "没有用户名是「nobody」的账号。"):
            run("nobody")
        with self.assertRaisesMessage(CommandError, "不是超级管理员"):
            run("guest")

    def test_passwords_typed_wrong(self):
        out, err = run(passwords=("x-pass-2026-a", "x-pass-2026-b", "12345678", "12345678", NEW, NEW))
        self.assertIn("两次输入的不一样", err)
        self.assertIn("密码", err)  # Django's own reason for 12345678, in Chinese
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.check_password(NEW))
        with self.assertRaisesMessage(CommandError, "没有设好新密码，什么都没有改。"):
            run(passwords=("", "a", "b", "c", "d"))
        self.boss.refresh_from_db()
        self.assertTrue(self.boss.check_password(NEW))

    def test_not_at_a_terminal(self):
        with self.assertRaisesMessage(CommandError, "要在终端里运行这个命令"):
            run(tty=False)

    def test_no_superuser_yet(self):
        self.boss.delete()
        with self.assertRaisesMessage(CommandError, "还没有超级管理员"):
            run()

    def test_it_tells_where_to_log_in(self):
        site = SiteSettings.load()
        site.secure_login_entry = "door-42"
        site.login_captcha = True
        site.save()
        with override_settings(SITE_URL="https://files.example.com"):
            out, _ = run()
        self.assertIn("登录地址：https://files.example.com/login/door-42/（设了登录安全入口，只能从这里登录）", out)
        self.assertIn("登录要输入图片验证码", out)

    @override_settings(SETUP_MODE=True)
    def test_not_before_installing(self):
        with self.assertRaisesMessage(CommandError, "HFile 还没有安装"):
            run()
