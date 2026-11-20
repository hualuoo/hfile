"""The installer (setup mode, apps/installer/), its token and what runserver and serve print. A whole install
in a real process, restart included: config/test_serve.py."""

import errno
import html
import importlib.util
import io
import os
import sqlite3
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest import mock

import certifi
from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client, SimpleTestCase, TestCase, TransactionTestCase, override_settings
from django.urls import reverse

from config import admin_path, cli, configfile
from config.version import VERSION
from apps.core.testing import assert_private, deny, readable_by_all, sqlite_file
from apps.siteconf.models import SiteSettings

from . import service, token, views
from .forms import InstallForm, host_of
from .management.commands.runserver import Command as Runserver
from .management.commands import serve
from .management.commands.serve import Command as Serve

User = get_user_model()
STRONG = "a-Strong-pass-42"


class TempHomeMixin:
    """config.toml, as `hfile` makes it before installing, and install-token.txt next to it, are in a
    temporary folder."""

    def setUp(self):
        super().setUp()
        self._home = tempfile.TemporaryDirectory(prefix="hfile-home-")
        self.home = Path(self._home.name)
        self.config = self.home / "config.toml"
        configfile.create(self.config)
        override = override_settings(CONFIG_FILE=self.config)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(self._home.cleanup)

    def written(self):
        return configfile.load(self.config)


class InstalledSiteTests(TestCase):
    def test_the_admin_is_only_at_its_path(self):
        self.assertEqual(reverse("admin:index"), f"/{settings.ADMIN_PATH}/")
        self.assertNotEqual(settings.ADMIN_PATH, "admin")
        self.assertEqual(self.client.get("/admin/").status_code, 404)
        self.assertEqual(self.client.get("/admin/login/").status_code, 404)
        self.assertEqual(self.client.get(f"/{settings.ADMIN_PATH}/login/").status_code, 200)
        self.assertEqual(self.client.get("/install/").status_code, 404)  # installed: no installer
        staff = User.objects.create_user("boss", password="pw-boss-2026", is_staff=True)
        self.client.force_login(staff)
        self.assertContains(self.client.get("/manage/"), f'href="/{settings.ADMIN_PATH}/"')

    def test_runserver_prints_it(self):
        self.assertIn(f"后台（Django 管理后台）：http://127.0.0.1:8000/{settings.ADMIN_PATH}/", runserver_banner())


def runserver_banner():
    out = io.StringIO()
    command = Runserver(stdout=out)
    command.addr, command.protocol, command._raw_ipv6 = "0.0.0.0", "http", False
    command.on_bind(8000)
    return out.getvalue()


@override_settings(SETUP_MODE=True)
class SetupBannerTests(TempHomeMixin, SimpleTestCase):
    def test_runserver_says_where_the_installer_is(self):
        banner = runserver_banner()
        code = token.get()
        self.assertIn(f"打开 http://127.0.0.1:8000/install/?token={code} 按安装向导填写", banner)
        self.assertIn(f"安装口令：{code}（也保存在 {token.path()}）", banner)
        self.assertEqual(token.path(), self.home / "install-token.txt")  # next to config.toml
        self.assertNotIn("后台", banner)

    def test_listening_on_ipv6(self):
        from . import banner

        self.assertEqual(banner.origin("::", 8080), "http://[::1]:8080")  # waitress takes IPv6 only on ::
        self.assertEqual(banner.origin("0.0.0.0", 8080), "http://127.0.0.1:8080")
        self.assertEqual(banner.origin("2001:db8::1", 8080), "http://[2001:db8::1]:8080")


class ServeBannerTests(TempHomeMixin, SimpleTestCase):
    def test_what_serve_prints(self):
        command = Serve(stdout=io.StringIO(), no_color=True)
        sqlite = {**configfile.defaults()["database"], "file": "data/site.sqlite3"}
        with override_settings(CONFIG={**settings.CONFIG, "database": sqlite}, MEDIA_ROOT=self.home / "uploads"):
            text = command.started("0.0.0.0", 8080)
        self.assertIn(f"HFile {VERSION} 在 0.0.0.0:8080 上提供网页。按 Ctrl+C 停止。\n", text)
        self.assertIn(
            f"设置文件：{self.config}\n数据库：SQLite，文件 {self.home / 'data' / 'site.sqlite3'}\n上传的文件：{self.home / 'uploads'}\n"
            "网站：http://127.0.0.1:8080/\n",
            text,
        )
        mysql = {**sqlite, "engine": "mysql", "host": "db.internal", "port": 3307, "name": "files"}
        with override_settings(CONFIG={**settings.CONFIG, "database": mysql}):
            self.assertIn("数据库：MySQL，db.internal:3307 上的 files\n", command.started("0.0.0.0", 8080))
        self.assertIn(f"后台（Django 管理后台）：http://127.0.0.1:8080/{settings.ADMIN_PATH}/\n", text)
        self.assertIn("从别的电脑打开时，把 127.0.0.1 换成这台机器的 IP 或域名（要写在设置文件的 allowed_hosts 里）。", text)
        self.assertNotIn("调试模式", text)
        with override_settings(SITE_URL="https://files.example.com"):
            text = command.started("127.0.0.1", 8080)
        self.assertIn("网站：https://files.example.com/\n", text)
        self.assertIn(f"后台（Django 管理后台）：https://files.example.com/{settings.ADMIN_PATH}/\n", text)
        text = command.started("::1", 8001)
        self.assertIn("在 [::1]:8001 上提供网页", text)
        self.assertNotIn("从别的电脑", text)
        with override_settings(SETUP_MODE=True, DEBUG=True):
            text = command.started("0.0.0.0", 8080)
        self.assertIn(f"打开 http://127.0.0.1:8080/install/?token={token.get()} 按安装向导填写", text)
        self.assertIn("从别的电脑打开时，把 127.0.0.1 换成这台机器的 IP 或域名。", text)
        self.assertNotIn("网站：", text)
        self.assertNotIn("数据库：", text)  # the installer asks which
        self.assertIn("调试模式开着（设置文件里 debug = true），正式运行前要关掉。", text)
        with override_settings(SERVER={**settings.SERVER, "proxies": 1}):
            self.assertIn("绕过代理直接访问端口 8080 的人可以随意填 X-Forwarded-For", command.started("0.0.0.0", 8080))
            self.assertNotIn("绕过代理", command.started("127.0.0.1", 8080))

    def test_a_sqlite_file_others_can_read(self):
        command = Serve(stdout=io.StringIO(), no_color=True)
        database = self.home / "data" / "site.sqlite3"
        database.parent.mkdir()
        database.touch(mode=0o600)
        sqlite = {**configfile.defaults()["database"], "file": "data/site.sqlite3"}
        with override_settings(CONFIG={**settings.CONFIG, "database": sqlite}):
            self.assertNotIn("别的用户也能读", command.started("127.0.0.1", 8080))
            if os.name == "nt":
                # The folder above lets every user read, as C:\ does, and the database's folder takes that, and so
                # do the files in it (SQLite's own too). The command said makes it the site's user's again.
                readable_by_all(self.home)
                text = command.started("127.0.0.1", 8080)
                self.assertIn(f"数据库文件 {database}，或者 SQLite 在同一个文件夹里写的文件，这台机器上别的用户也能读，"
                              f"里面有密码哈希和存储源的密钥。把这个文件夹改成只有运行网站的用户能读写：icacls \"{database.parent}\"", text)
                subprocess.run(text.split("能读写：", 1)[1].split("\n", 1)[0], check=True, capture_output=True)
                self.assertNotIn("别的用户也能读", command.started("127.0.0.1", 8080))
                return
            database.chmod(0o644)  # made by hand, or by a command of an older version
            self.assertIn(f"数据库文件 {database} 这台机器上别的用户也能读，里面有密码哈希和存储源的密钥。"
                          f"改成只有运行网站的用户能读写：chmod 600 {database}\n", command.started("127.0.0.1", 8080))

    def test_when_it_cant_listen(self):
        problem = Serve.listen_problem
        self.assertIn("端口 8080 已经被别的程序占用了", problem("0.0.0.0", 8080, OSError(errno.EADDRINUSE, "in use")))
        if os.name == "nt":
            self.assertIn("Windows 把这个端口留给了别的用途", problem("0.0.0.0", 80, PermissionError(errno.EACCES, "denied")))
        else:
            self.assertIn("1024 以下的端口一般要管理员权限", problem("0.0.0.0", 80, PermissionError(errno.EACCES, "denied")))
        self.assertIn("这台机器上没有 10.9.9.9 这个地址", problem("10.9.9.9", 8080, OSError(errno.EADDRNOTAVAIL, "no")))
        self.assertIn("这台机器上没有 nowhere.invalid 这个地址", problem("nowhere.invalid", 8080, ValueError("Invalid host/port specified.")))

    def test_waitress_is_set_up_as_the_spec_says(self):
        from waitress import server as waitress

        made = {}

        def create_server(application, **options):
            made.update(options)
            raise OSError(errno.EADDRINUSE, "stop here")

        open_files = mock.Mock(RLIMIT_NOFILE=7, **{"getrlimit.return_value": (1024, 524288)})  # systemd's for a service
        with (
            mock.patch.object(waitress, "create_server", create_server),
            mock.patch.object(serve, "resource", open_files),
            mock.patch.object(serve, "CONNECTION_LIMIT", 1000),  # with poll(), which Windows hasn't (500 there)
            self.assertRaises(Exception),
        ):
            Serve(stdout=io.StringIO()).handle()
        self.assertEqual(
            {key: made[key] for key in ("clear_untrusted_proxy_headers", "connection_limit", "channel_timeout", "max_request_body_size",
                                        "cleanup_interval")},
            {"clear_untrusted_proxy_headers": False, "connection_limit": 1000, "channel_timeout": 30,
             "max_request_body_size": 2 * settings.MAX_REQUEST_BYTES,
             "cleanup_interval": 5},  # how often apps/installer/connections.py checks the deadlines
        )

    def test_connections_and_open_files(self):
        # OPS-05: up to 1000 connections, but systemd lets a service open 1024 files (its soft limit), and the
        # site has others open too: they ran out first, and waitress filled the log with errors. The soft
        # limit is raised, as far as the hard one allows; the connections are fewer when that isn't enough.
        def limit(soft, hard, threads=8):
            open_files = mock.Mock(RLIMIT_NOFILE=7, **{"getrlimit.return_value": (soft, hard)})
            with mock.patch.object(serve, "resource", open_files), mock.patch.object(serve, "CONNECTION_LIMIT", 1000):
                return serve.connection_limit(threads), open_files.setrlimit.call_args_list

        needed = 32 + 6 * 8 + 2 * 1000  # its own files, each thread's, two for each connection
        self.assertEqual(limit(1024, 524288), (1000, [mock.call(7, (needed, 524288))]))
        self.assertEqual(limit(1024, 1500), ((1500 - 32 - 48) // 2, [mock.call(7, (1500, 1500))]))
        self.assertEqual(limit(1024, 1024), ((1024 - 32 - 48) // 2, []))
        self.assertEqual(limit(65536, 65536), (1000, []))  # plenty: left as it is
        self.assertEqual(limit(1024, 1024, threads=64), ((1024 - 32 - 6 * 64) // 2, []))
        with mock.patch.object(serve, "resource", None):  # Windows: no such limit
            self.assertEqual(serve.connection_limit(8), serve.CONNECTION_LIMIT)


class TokenTests(TempHomeMixin, SimpleTestCase):
    def test_the_token(self):
        code = token.get()
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$")
        self.assertEqual(token.get(), code)  # saved
        assert_private(self, token.path())
        self.assertTrue(token.check(code.lower().replace("-", " ")))
        self.assertFalse(token.check(""))
        self.assertFalse(token.check("AAAA-BBBB-CCCC-DDDD"))
        self.assertFalse(token.check("安装口令"))  # letters, but not ASCII ones: no crash, just wrong
        self.assertFalse(token.check("Ｚ" + code[1:]))  # a full-width letter in front
        token.path().write_text("", encoding="ascii")  # e.g. writing it failed half way
        self.assertFalse(token.check("-"))
        self.assertFalse(token.check(" "))
        token.remove()
        self.assertFalse(token.path().exists())

    def test_a_file_that_holds_no_token_gets_a_new_one(self):
        token.path().write_text("编辑过的 nonsense", encoding="utf-8")
        code = token.get()
        self.assertRegex(code, r"^[A-Z2-9]{4}(-[A-Z2-9]{4}){3}$")
        self.assertEqual(token.path().read_text().strip(), code)


class TokenWithoutHardLinksTests(TempHomeMixin, SimpleTestCase):
    def test_a_file_system_without_hard_links(self):
        """OPS-02: the token was written to a temporary file and hard-linked into place; on vfat, exfat or a FUSE
        file system without hard links (rclone's) that failed, and the site couldn't be installed, the message
        saying the folder couldn't be written to, where config.toml had just been made."""
        real_link = os.link
        for number in (errno.EPERM, errno.ENOSYS, errno.EOPNOTSUPP):
            with self.subTest(errno=errno.errorcode[number]):
                token.remove()

                def no_hard_links(source, target, number=number):
                    raise OSError(number, os.strerror(number))

                with mock.patch.object(os, "link", no_hard_links):
                    code = token.get()
                self.assertEqual(token.path().read_text(encoding="ascii").strip(), code)
                assert_private(self, token.path())
                self.assertEqual(sorted(p.name for p in token.path().parent.iterdir() if ".part" in p.name), [])
                self.assertEqual(token.get(), code)
        self.assertIs(os.link, real_link)

    def test_what_the_page_and_the_banner_say(self):
        """OPS-02: every system error was told as a folder that can't be written to."""
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        self.assertNotIn("不能写入", token.problem(full))
        self.assertIn(os.strerror(errno.ENOSPC), token.problem(full))
        self.assertIn("不能写入", token.problem(PermissionError(errno.EACCES, os.strerror(errno.EACCES))))


def form_data(**changes):
    data = {
        "token": "t",
        "db_engine": "mysql",
        "db_file": "hfile.sqlite3",
        "db_host": "127.0.0.1",
        "db_port": "3306",
        "db_name": "hfile",
        "db_user": "hfile",
        "db_password": "secret",
        "site_name": "  我的 网盘 ",
        "url": "",
        "hosts": "Example.com:8080, 192.168.1.5，[::1]:80",
        "proxies": "0",
        "admin_path": "",
        "username": "boss",
        "password1": STRONG,
        "password2": STRONG,
    }
    data.update(changes)
    return {key: value for key, value in data.items() if value is not None}


MYSQL = service.Database(engine="mysql", host="127.0.0.1", port=3306, name="hfile", user="hfile", password="secret")


class FormTests(SimpleTestCase):
    def test_a_good_form(self):
        form = InstallForm(form_data())
        self.assertTrue(form.is_valid(), form.errors)
        data = form.cleaned_data
        self.assertEqual(data["site_name"], "我的 网盘")
        self.assertEqual(data["hosts"], ["example.com", "192.168.1.5", "[::1]", "127.0.0.1", "localhost"])
        self.assertEqual((data["username"], data["admin_path"]), ("boss", ""))
        self.assertEqual(form.database(), MYSQL)
        # Anything can be written into config.toml (TOML escapes it).
        self.assertTrue(InstallForm(form_data(db_password="it's ${HOME} \\ \"")).is_valid())
        self.assertEqual(InstallForm().fields["db_engine"].initial, "sqlite")
        self.assertEqual((data["url"], data["proxies"]), ("", 0))
        form = InstallForm(form_data(username="ｂｏｓｓ"))  # full-width letters, from an input method
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["username"], "boss")

    def test_the_address_and_the_proxies(self):
        form = InstallForm(form_data(url=" HTTPS://Files.Example.com/ ", proxies="1"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data["url"], form.cleaned_data["proxies"]), ("https://files.example.com", 1))
        self.assertTrue(InstallForm(form_data(url="http://10.0.0.5:8080")).is_valid())
        for url, message in (
            ("files.example.com", '要以 https:// 或 http:// 开头，比如 "https://files.example.com"。'),
            ("https://files.example.com/site/", '只写到域名（和端口）为止，后面不要带路径，比如 "https://files.example.com"。'),
        ):
            with self.subTest(url=url):
                form = InstallForm(form_data(url=url))
                self.assertFalse(form.is_valid())
                self.assertEqual(form.errors["url"], [message])  # config.toml's own check
        self.assertFalse(InstallForm(form_data(proxies="6")).is_valid())
        # Not sent at all (a script from before these fields): fine, config.toml keeps its values (views.py).
        form = InstallForm(form_data(url=None, proxies=None))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual((form.cleaned_data["url"], form.cleaned_data["proxies"]), ("", None))

    def test_sqlite(self):
        # MySQL's fields are sent too, hidden, maybe wrong: they aren't looked at.
        form = InstallForm(form_data(db_engine="sqlite", db_file=" data/my site.sqlite3 ", db_port="x", db_name="my-db", db_host=""))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.database(), service.Database(engine="sqlite", file="data/my site.sqlite3"))
        self.assertEqual(form.database().config(), {"engine": "sqlite", "file": "data/my site.sqlite3"})
        for path, message in ((" ", "要写数据库文件的路径"), ("data/", "要写到文件名为止"), ("a\tb", "控制字符"),
                              ("~nosuchuser-hfile/x.sqlite3", "这个用户不存在")):
            with self.subTest(path=path):
                form = InstallForm(form_data(db_engine="sqlite", db_file=path))
                self.assertFalse(form.is_valid())
                self.assertIn(message, form.errors["db_file"][0])
        # And for MySQL, SQLite's file isn't looked at.
        self.assertTrue(InstallForm(form_data(db_file="")).is_valid())
        self.assertFalse(InstallForm(form_data(db_engine="postgres")).is_valid())

    def test_mistakes(self):
        cases = {
            "db_name": ({"db_name": "my-db"}, "数据库名只能用英文字母、数字和下划线，最多 64 个。"),
            "hosts": ({"hosts": "ok.com, bad_host!"}, "「bad_host!」不是域名或 IP。"),
            "admin_path": ({"admin_path": "/static/"}, "/static/ 是网站自己要用的地址，请换一个。"),
            "admin_path ": ({"admin_path": "admin"}, "/admin/ 永远是 404（扫描程序第一个就试它），请换一个。"),
            "admin_path  ": ({"admin_path": "Dashboard"}, "/Dashboard/ 太好猜了，扫描的程序第一个就会试它。留空随机生成一个，或者换一个别人猜不到的。"),
            "username": ({"username": "a b"}, "用户名只能用字母（中文也行）、数字和 @ . + - _。"),
            "username ": ({"username": "boss​"}, None),
            "password2": ({"password2": STRONG + "x"}, "两次输入的密码不一样。"),
            "password1": ({"password1": "12345678", "password2": "12345678"}, None),
            "db_password": ({"db_password": "a\nb"}, "密码里不能有换行。"),
            "db_port": ({"db_port": "3306x"}, "端口是 1 到 65535 之间的整数。"),
            "db_host": ({"db_host": " "}, "要写 MySQL 的地址，里面不能有空格。"),
            "db_user": ({"db_user": ""}, "要写 MySQL 的用户名，里面不能有空格。"),
            # config.toml would refuse it after the install: refused here, before anything is done.
            "db_user ": ({"db_user": "site user"}, "要写 MySQL 的用户名，里面不能有空格。"),
            "site_name": ({"site_name": "名" * 31}, "网站名称最多 30 个字。"),
        }
        for field, (changes, message) in cases.items():
            with self.subTest(field=field):
                form = InstallForm(form_data(**changes))
                self.assertFalse(form.is_valid())
                self.assertIn(field.strip(), form.errors)
                if message:
                    self.assertEqual(form.errors[field.strip()], [message])

    def test_the_steps(self):
        # Every field in one step of the page (install.js, views.CheckView), and only in one.
        names = [name for fields in InstallForm.STEPS.values() for name in fields]
        self.assertEqual(sorted(names), sorted(InstallForm().fields))
        self.assertEqual(len(names), len(set(names)))
        self.assertEqual(list(InstallForm.STEPS), ["token", "database", "site", "admin"])  # the spec's four

    def test_hosts(self):
        self.assertEqual(host_of("Example.COM."), "example.com")
        self.assertEqual(host_of(".example.com"), ".example.com")
        self.assertEqual(host_of("[::1]:8080"), "[::1]")  # Django compares an IPv6 Host with its brackets
        self.assertEqual(host_of("::1"), "[::1]")
        self.assertEqual(host_of("10.0.0.1:80"), "10.0.0.1")
        self.assertEqual(host_of("a b"), "")
        # OPS-09: a host in another script, as browsers send it (punycode); it was "not a host".
        self.assertEqual(host_of("例子.中国"), "xn--fsqu00a.xn--fiqs8s")
        self.assertEqual(host_of(".例子.中国:8080"), ".xn--fsqu00a.xn--fiqs8s")  # and its subdomains
        self.assertEqual(host_of("☃.net"), "")  # IDNA 2008 refuses it

    def test_hosts_in_other_scripts(self):
        # OPS-09: the address was "要以 https:// 或 http:// 开头", the hosts "不是域名或 IP".
        form = InstallForm(form_data(url="https://例子.中国:8443", hosts="例子.中国"))
        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data["url"], "https://xn--fsqu00a.xn--fiqs8s:8443")
        self.assertEqual(form.cleaned_data["hosts"], ["xn--fsqu00a.xn--fiqs8s", "127.0.0.1", "localhost"])
        hint = "中文域名要能转成 xn-- 开头的形式：检查一下写法，或者直接写成 xn-- 开头的形式。"
        form = InstallForm(form_data(url="https://例子..中国", hosts="ok.com, ☃.net"))
        self.assertFalse(form.is_valid())
        self.assertEqual(form.errors["url"], [f"「例子..中国」不是域名或 IP。{hint}"])
        self.assertEqual(form.errors["hosts"], [f"「☃.net」不是域名或 IP。{hint}"])


SETUP = {"ROOT_URLCONF": "apps.installer.setup_urls", "SETUP_MODE": True, "ALLOWED_HOSTS": ["*"]}


@override_settings(**SETUP)
class SetupPageTests(TempHomeMixin, TestCase):
    """The installer's pages; the database part (service.py) has its own tests below."""

    def post(self, **changes):
        return self.client.post("/install/", form_data(token=token.get(), **changes))

    def test_a_token_that_cant_be_saved(self):
        # OPS-02: what went wrong, not always 「设置文件所在的文件夹不能写入」.
        full = OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        with mock.patch.object(token, "get", side_effect=full):
            page = self.client.get("/install/").content.decode()
        self.assertIn(html.escape(token.problem(full)), page)
        self.assertNotIn("不能写入", page)

    def test_everything_leads_to_the_installer(self):
        for path in ("/", "/manage/", "/admin/", f"/{settings.ADMIN_PATH}/", "/media/site/x.png"):
            self.assertRedirects(self.client.get(path), "/install/", fetch_redirect_response=False)
        with self.assertNoLogs("django.request"):  # expected: the last page asks until the program is back
            r = self.client.get("/api/v1/site/")
        self.assertEqual((r.status_code, r.json()["detail"]), (503, "网站还没有安装：先打开 /install/ 按安装向导填写。"))
        self.assertEqual(self.client.post("/api/v1/session/", {}).status_code, 503)
        for path in ["web/css/m3.css", "web/css/app.css", "web/fonts/roboto-400.woff2", "installer/install.js"]:  # without DEBUG too
            self.assertEqual(self.client.get(f"/static/{path}").status_code, 200, path)
        database = {"engine": "mysql", "file": "x.sqlite3", "host": "db.internal", "port": 3307, "name": "files", "user": "app",
                    "password": "pw-in-the-file"}
        with override_settings(CONFIG={**settings.CONFIG, "database": database}):
            page = self.client.get("/install/?token=ABCD", HTTP_HOST="files.example.com:8080").content.decode()
        self.assertLess(page.index("web/css/m3.css"), page.index("web/css/app.css"))  # the tokens, then what uses them
        self.assertIn('value="ABCD"', page)
        self.assertIn('value="files.example.com"', page)  # the hosts field starts with the address in use
        # 网站地址 starts with the address as the program sees it; install.js puts the browser's in.
        self.assertIn('value="http://files.example.com:8080"', page)
        self.assertIn('data-guess="http://files.example.com:8080"', page)
        self.assertIn('data-scheme="http"', page)
        self.assertIn('<option value="0" selected>没有，网站直接对外</option>', page)  # no proxy's headers
        # What config.toml has for the database already, but not its password: anybody can open this page.
        for value in ("db.internal", "3307", "files", "app", "x.sqlite3"):
            self.assertIn(f'value="{value}"', page)
        self.assertRegex(page, r'<input type="radio" name="db_engine" value="mysql"[^>]* checked>')
        self.assertNotIn("pw-in-the-file", page)
        # A new config.toml: SQLite, in a file next to it.
        with override_settings(CONFIG=self.written()):
            page = self.client.get("/install/").content.decode()
        self.assertRegex(page, r'<input type="radio" name="db_engine" value="sqlite"[^>]* checked>')
        self.assertIn('value="hfile.sqlite3"', page)
        self.assertIn(f"相对路径从设置文件所在的文件夹（{self.home}）算起", page)
        self.assertTrue(token.path().exists())  # made for the page to point at

    def test_the_steps_on_the_page(self):
        page = self.client.get("/install/").content.decode()
        for step in InstallForm.STEPS:
            self.assertIn(f'data-step="{step}"', page)
        self.assertIn('data-check="/install/check/"', page)
        self.assertIn('<button class="btn" type="button" data-action="test">测试连接</button>', page)
        self.assertEqual(page.count('data-action="next"'), 4)  # 下一步 three times, then 开始安装
        self.assertIn('id="install-submit" data-action="next">开始安装</button>', page)
        # Without JavaScript all four steps show and the form is sent as it is: no step is hidden by the page.
        self.assertNotRegex(page, r'<section[^>]*data-step="[a-z]+"[^>]*hidden')
        for name in ("token", "db_file", "db_host", "db_port", "db_name", "db_user", "site_name", "hosts", "username", "password2"):
            self.assertRegex(page, rf'<input type="[a-z]+" name="{name}"[^>]* required[ >]')
        self.assertIn('<label for="id_db_host">地址<span class="required-mark" aria-hidden="true">*</span></label>', page)
        self.assertIn('<label for="id_db_password">密码</label>', page)  # MySQL's password may be empty
        self.assertNotIn("data-sent", page)
        self.assertNotIn("csrfmiddlewaretoken", page)  # no CSRF check: the token does that job
        page = self.post(password2="other").content.decode()  # install.js opens the step with the problem
        self.assertIn("data-sent", page)

    def test_the_token_is_needed(self):
        token.get()
        with mock.patch.object(service, "preflight") as preflight, self.assertLogs("installer", "WARNING"):
            r = self.client.post("/install/", form_data(token="AAAA-BBBB-CCCC-DDDD"))
        self.assertEqual(r.status_code, 400)
        self.assertIn("安装口令不对", r.content.decode())
        self.assertEqual(self.written()["site"]["secret_key"], "")
        preflight.assert_not_called()
        # The form has no CSRF check (views.py): a page on another site can send it, but it can't know
        # the token, and without it nothing happens, not even a look at the database.
        with mock.patch.object(service, "preflight") as preflight, self.assertLogs("installer", "WARNING"):
            r = Client(enforce_csrf_checks=True).post(
                "/install/", form_data(token="AAAA-BBBB-CCCC-DDDD"), HTTP_ORIGIN="https://elsewhere.example"
            )
        self.assertEqual(r.status_code, 400)
        preflight.assert_not_called()

    @mock.patch.object(service, "restart_soon", return_value=True)
    @mock.patch.object(service, "install", return_value=User(username="boss"))
    @mock.patch.object(service, "preflight")
    def test_behind_a_proxy_that_ends_https(self, preflight, install, restart):
        """As Caddy or nginx pass it on: Host kept, X-Forwarded-* added, and the browser's Origin is
        https://…, while the program gets plain http. Django's CSRF check would refuse that (403)."""
        for header in ("HTTP_X_FORWARDED_FOR", "HTTP_X_FORWARDED_PROTO", "HTTP_X_FORWARDED_HOST", "HTTP_X_REAL_IP", "HTTP_FORWARDED"):
            with self.subTest(header=header):
                page = self.client.get("/install/", **{header: "1.2.3.4"}).content.decode()
                self.assertIn('<option value="1" selected>1 层：', page)  # a proxy's header: one is in front
        proxy = {"HTTP_HOST": "files.example.com", "HTTP_X_FORWARDED_FOR": "203.0.113.7", "HTTP_X_FORWARDED_PROTO": "https"}
        page = self.client.get("/install/", **proxy).content.decode()
        self.assertIn('data-guess="http://files.example.com"', page)  # what install.js replaces
        browser = Client(enforce_csrf_checks=True)
        r = browser.post(
            "/install/", form_data(token=token.get(), url="https://files.example.com", proxies="1"),
            HTTP_ORIGIN="https://files.example.com", HTTP_REFERER="https://files.example.com/install/", **proxy,
        )
        self.assertEqual(r.status_code, 200, r.content.decode()[:2000])
        page = r.content.decode()
        self.assertIn("程序正在重新启动", page)
        self.assertRegex(page, r"https://files\.example\.com/[a-z0-9]{12}/")  # the admin, at the site's address
        written = self.written()
        self.assertEqual((written["site"]["url"], written["server"]["proxies"]), ("https://files.example.com", 1))
        self.assertIn("files.example.com", configfile.allowed_hosts(written))

    def test_the_address_from_config_toml(self):
        configfile.update(self.config, {"site": {"url": "https://set.by.hand"}, "server": {"proxies": 2}})
        with override_settings(CONFIG=self.written()):
            page = self.client.get("/install/").content.decode()
        self.assertIn('value="https://set.by.hand"', page)
        self.assertIn('<option value="2" selected>', page)
        self.assertIn("https://set.by.hand/</span>", page)  # before the admin's path

    @mock.patch.object(service, "restart_soon", return_value=True)
    @mock.patch.object(service, "install")
    @mock.patch.object(service, "preflight")
    def test_installing(self, preflight, install, restart):
        install.return_value = User(username="boss")
        configfile.update(self.config, {"server": {"port": 9123}, "site": {"debug": True}})  # set by hand before starting
        r = self.post(admin_path="my-admin")
        self.assertEqual(r.status_code, 200, r.content.decode()[:2000])
        page = r.content.decode()
        self.assertIn("http://testserver/my-admin/", page)
        self.assertIn("程序正在重新启动", page)
        preflight.assert_called_once_with(MYSQL)
        install.assert_called_once_with(username="boss", password=STRONG, site_name="我的 网盘", db=MYSQL)
        written = self.written()
        self.assertEqual(written["site"]["secret_key"], settings.SECRET_KEY)
        self.assertEqual(
            {key: written["site"][key] for key in ("allowed_hosts", "admin_path", "url", "debug")},
            {"allowed_hosts": ["example.com", "192.168.1.5", "[::1]", "127.0.0.1", "localhost"], "admin_path": "my-admin",
             "url": "", "debug": True},  # debug: as it was
        )
        self.assertEqual(
            written["database"],
            {"engine": "mysql", "file": "hfile.sqlite3", "host": "127.0.0.1", "port": 3306, "name": "hfile", "user": "hfile",
             "password": "secret", "tls": "preferred", "ca": None},
        )
        self.assertEqual(written["server"]["port"], 9123)  # the rest of the file as it was
        assert_private(self, self.config)
        self.assertFalse(token.path().exists())

        # Until the program has started again: no form, and the admin's address isn't shown again.
        page = self.client.get("/install/").content.decode()
        self.assertIn("正在等程序重新启动", page)
        self.assertNotIn("my-admin", page)
        self.assertNotIn("my-admin", self.post().content.decode())

    @mock.patch.object(service, "restart_soon", return_value=True)
    @mock.patch.object(service, "install", return_value=User(username="boss"))
    @mock.patch.object(service, "preflight")
    def test_installing_on_sqlite(self, preflight, install, restart):
        configfile.update(self.config, {"database": {"port": 13306}})  # MySQL's, by hand: kept
        page = self.post(db_engine="sqlite", db_file="data/site.sqlite3", db_password="").content.decode()
        self.assertIn("程序正在重新启动", page)
        db = service.Database(engine="sqlite", file="data/site.sqlite3")
        preflight.assert_called_once_with(db)
        self.assertEqual(install.call_args.kwargs["db"], db)
        self.assertEqual(db.path(), self.home / "data" / "site.sqlite3")  # next to config.toml
        written = self.written()["database"]
        self.assertEqual((written["engine"], written["file"], written["port"]), ("sqlite", "data/site.sqlite3", 13306))

    @mock.patch.object(service, "restart_soon", return_value=False)
    @mock.patch.object(service, "install", return_value=User(username="boss"))
    @mock.patch.object(service, "preflight")
    def test_a_random_admin_path_and_a_manual_restart(self, preflight, install, restart):
        page = self.post().content.decode()
        path = self.written()["site"]["admin_path"]
        self.assertEqual(admin_path.problem(path), "")
        self.assertEqual(len(path), admin_path.LENGTH)  # 12, random
        self.assertIn(f"/{path}/", page)
        self.assertIn("请重新启动程序", page)

    @mock.patch.object(service, "restart_soon", return_value=True)
    @mock.patch.object(service, "install", return_value=User(username="boss"))
    @mock.patch.object(service, "preflight")
    def test_when_the_settings_cant_be_written(self, preflight, install, restart):
        with mock.patch.object(configfile, "update", side_effect=PermissionError(13, "Permission denied")):
            page = self.post(db_password="it's ${HOME}").content.decode()
        self.assertIn("但设置文件没能写进去：Permission denied", page)
        self.assertIn(f"请把下面的内容存成 <code>{self.config}</code>", page)
        # The whole file to save by hand: what it has, and what the installer would have written.
        text = page.split('readonly spellcheck="false" aria-label="设置文件的内容">', 1)[1].split("</textarea>", 1)[0]
        values = configfile.clean(tomllib.loads(html.unescape(text)))
        self.assertEqual(values["site"]["secret_key"], settings.SECRET_KEY)
        self.assertEqual(values["database"]["password"], "it's ${HOME}")
        restart.assert_not_called()
        self.assertTrue(token.path().exists())  # still not installed as far as the file says
        self.assertEqual(self.written()["site"]["secret_key"], "")

    def test_restart_only_where_something_starts_it_again(self):
        with mock.patch.object(service.threading, "Timer") as timer, mock.patch.dict(os.environ):
            os.environ.pop(cli.CHILD, None)
            os.environ.pop("RUN_MAIN", None)
            self.assertFalse(service.restart_soon())  # e.g. under some other server: a person restarts it
            os.environ[cli.CHILD] = "1"  # `hfile` (serve): its parent process starts it again
            self.assertTrue(service.restart_soon())
            del os.environ[cli.CHILD]
            os.environ["RUN_MAIN"] = "true"  # runserver's autoreloader
            self.assertTrue(service.restart_soon())
        self.assertEqual(timer.call_count, 2)

    @mock.patch.object(service, "preflight", side_effect=service.InstallError("MySQL 拒绝了用户名 hfile 和这个密码。", "db_password"))
    def test_a_database_problem(self, preflight):
        r = self.post()
        self.assertEqual(r.status_code, 400)
        self.assertIn("MySQL 拒绝了用户名 hfile 和这个密码。", r.content.decode())
        self.assertEqual(self.written()["site"]["secret_key"], "")
        self.assertTrue(token.path().exists())


class InstallServiceTests(TestCase):
    """install() on the test database, already migrated, as after a first try that stopped before the admin."""

    def test_the_first_admin(self):
        admin = service.install(username="boss", password=STRONG, site_name="我的网盘")
        self.assertTrue(admin.is_superuser and admin.is_staff and admin.check_password(STRONG))
        self.assertEqual(SiteSettings.load().site_name, "我的网盘")
        with self.assertRaisesMessage(service.InstallError, "已经有管理员"):
            service.install(username="boss2", password=STRONG, site_name="x")

    def test_names_already_taken(self):
        User.objects.create_user("Boss")
        with self.assertRaises(service.InstallError) as caught:
            service.install(username="boss", password=STRONG, site_name="x")
        self.assertEqual(caught.exception.field, "username")


def the_tests_database(**changes):
    db = connection.settings_dict
    values = {"host": db["HOST"], "port": int(db["PORT"]), "name": db["NAME"], "user": db["USER"], "password": db["PASSWORD"]}
    values.update(changes)
    return service.Database(engine="mysql", **values)


@unittest.skipUnless(connection.vendor == "mysql", "只在 MySQL 上")
class PreflightTests(TransactionTestCase):
    """preflight() against the MySQL server the tests run on, through its own connection."""

    def assertProblem(self, db, field, text, **kwargs):
        with self.assertRaises(service.InstallError) as caught:
            service.preflight(db, **kwargs)
        self.assertEqual(caught.exception.field, field)
        self.assertIn(text, caught.exception.message)

    def test_connection_problems(self):
        self.assertProblem(the_tests_database(password="wrong"), "db_password", "MySQL 拒绝了用户名")
        self.assertProblem(the_tests_database(port=1), "db_host", "连不上 127.0.0.1:1 的 MySQL")
        # The tests' MySQL user may only have databases whose names start with hfile or test_hfile.
        self.assertProblem(the_tests_database(name="other_installer_nope"), "db_name", "没有数据库 other_installer_nope 的权限")

    def test_what_is_in_the_database(self):
        User.objects.guest(), User.objects.template()  # the built-in users are no accounts
        service.preflight(the_tests_database())  # HFile's tables and no account: an install that stopped half way
        User.objects.create_superuser("root", password=STRONG)
        self.assertProblem(the_tests_database(), "db_name", "已经装好了 HFile")
        User.objects.all().delete()
        User.objects.create_user("staff", password=STRONG)  # in use, though nobody is an admin
        self.assertProblem(the_tests_database(), "db_name", "已经装好了 HFile")
        User.objects.all().delete()
        with connection.cursor() as cursor:
            cursor.execute("CREATE TABLE someone_elses (id int)")
        try:
            self.assertProblem(the_tests_database(), "db_name", "已经有别的程序的表了（someone_elses）")
        finally:
            with connection.cursor() as cursor:
                cursor.execute("DROP TABLE someone_elses")

    def test_a_dry_run(self):
        db = the_tests_database()
        message = service.preflight(db, dry_run=True)
        self.assertRegex(message, rf"^连上了 MySQL \d+\.\d+\.\d+。数据库 {db.name} 里是一个没装完的 HFile，安装会接着装完。$")
        # A name the user may not have: said without trying to make it.
        self.assertProblem(the_tests_database(name="other_installer_nope"), "db_name", "没有数据库 other_installer_nope 的权限", dry_run=True)
        # One it may have but that isn't there: said without making it.
        name = f"{db.name}_new_one"[:64]
        self.assertIn(f"数据库 {name} 还没有，安装时会建好它。", service.preflight(the_tests_database(name=name), dry_run=True))
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [name])
            self.assertEqual(cursor.fetchone()[0], 0)

    def test_tls_as_config_toml_says(self):
        # SEC-08: the form doesn't ask for [database] tls and ca; preflight() connects with config.toml's,
        # as the site will (use_database).
        def config(**values):
            return override_settings(CONFIG={**settings.CONFIG, "database": {**settings.CONFIG["database"], **values}})

        with config(tls="required"):  # the tests' MySQL has TLS, as MySQL 8.4 does by default
            service.preflight(the_tests_database(), dry_run=True)
        with config(tls="verify_ca", ca=certifi.where()):  # public CAs: none of them signed the server's certificate
            self.assertProblem(the_tests_database(), None, "检查设置文件 config.toml 里 [database] 的 tls 和 ca", dry_run=True)

    def test_a_new_database_is_made(self):
        name = f"{connection.settings_dict['NAME']}_made"[:64]
        self.addCleanup(lambda: connection.cursor().execute(f"DROP DATABASE IF EXISTS `{name}`"))
        message = service.preflight(the_tests_database(name=name))
        self.assertIn(f"新建了数据库 {name}", message)
        with connection.cursor() as cursor:
            cursor.execute("SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = %s", [name])
            self.assertEqual(cursor.fetchone(), ("utf8mb4", "utf8mb4_0900_ai_ci"))


@unittest.skipUnless(importlib.util.find_spec("MySQLdb"), "no mysqlclient")
class MySQLDryRunTests(SimpleTestCase):
    """_check_mysql as a dry run (测试连接), against a made-up server: nothing is made or changed."""

    def run_check(self, *, schema=None, use_error=None, version="8.4.3"):
        import MySQLdb

        statements = []

        class Cursor:
            last = ""

            def execute(self, sql, params=None):
                statements.append(sql.split()[0])
                if sql.startswith("USE") and use_error:
                    raise MySQLdb.OperationalError(*use_error)
                self.last = sql

            def fetchone(self):
                if "VERSION()" in self.last:
                    return (version,)
                return schema if "SCHEMATA" in self.last else (0,)

            def fetchall(self):
                return []

        return service._check_mysql(Cursor(), MYSQL, True), statements

    def test_a_database_that_isnt_there(self):
        message, statements = self.run_check(use_error=(1049, "Unknown database 'hfile'"))
        self.assertEqual(message, "连上了 MySQL 8.4.3。数据库 hfile 还没有，安装时会建好它。")
        self.assertNotIn("CREATE", statements)
        with self.assertRaises(service.InstallError) as caught:
            self.run_check(use_error=(1044, "Access denied for user 'hfile'@'%' to database 'hfile'"))
        self.assertEqual(caught.exception.field, "db_name")
        self.assertIn("CREATE DATABASE IF NOT EXISTS `hfile`", caught.exception.message)

    def test_errors_that_say_what_to_do(self):
        import MySQLdb

        # OPS-07: MySQL without TLS, an account with caching_sha2_password (8.4's default) that nobody has
        # logged in with since MySQL started: it was 数据库出错 and the error's text.
        error = MySQLdb.OperationalError(
            2061, "Authentication plugin 'caching_sha2_password' reported error: Authentication requires secure connection."
        )
        message = service._mysql_error(error, MYSQL).message
        self.assertIn("MySQL 没有开 TLS", message)
        self.assertIn("用 mysql 客户端以这个用户登录一次", message)
        # SEC-08: [database] tls and ca in config.toml, and TLS that can't be had with them.
        error = MySQLdb.OperationalError(2026, "SSL connection error: error:0A000086:SSL routines::certificate verify failed")
        self.assertIn("检查设置文件 config.toml 里 [database] 的 tls 和 ca", service._mysql_error(error, MYSQL).message)

    def test_tls_from_config_toml(self):
        # SEC-08: the form doesn't ask for them; preflight() connects with config.toml's, as the site will.
        ca = "C:\\srv\\mysql-ca.pem" if os.name == "nt" else "/srv/mysql-ca.pem"  # absolute: on Windows with its drive
        values = {**settings.CONFIG["database"], "tls": "verify_identity", "ca": ca}
        with override_settings(CONFIG={**settings.CONFIG, "database": values}):
            params = MYSQL.params()
        self.assertEqual((params["ssl_mode"], params["ssl"]), ("VERIFY_IDENTITY", {"ca": ca}))
        self.assertNotIn("ssl_mode", MYSQL.params())  # tls = "preferred", MySQL's own default

    def test_a_database_to_change(self):
        message, statements = self.run_check(schema=("latin1", "latin1_swedish_ci"))
        self.assertEqual(message, "连上了 MySQL 8.4.3。数据库 hfile 已经有了，是空的。它的字符集是 latin1，安装时会改成 utf8mb4。")
        self.assertNotIn("ALTER", statements)

    def test_servers_it_doesnt_take(self):
        for version, text in (("8.0.36", "MySQL 的版本是 8.0.36，至少要 8.4。"), ("11.4.2-MariaDB-ubu2404", "HFile 只支持 MySQL 8.4 以上")):
            with self.subTest(version=version), self.assertRaises(service.InstallError) as caught:
                self.run_check(version=version)
            self.assertIn(text, caught.exception.message)
            self.assertEqual(caught.exception.field, "db_host")


class SQLitePreflightTests(TempHomeMixin, SimpleTestCase):
    """preflight() with SQLite: files in a temporary folder, whatever database the tests run on."""

    def check(self, file):
        db = service.Database(engine="sqlite", file=file)
        service.preflight(db)
        return db.path()

    def assertProblem(self, file, text):
        with self.assertRaises(service.InstallError) as caught:
            self.check(file)
        self.assertEqual(caught.exception.field, "db_file")
        self.assertIn(text, caught.exception.message)

    def test_a_new_file(self):
        path = self.check("data/new folder/site.sqlite3")  # the folders are made
        assert_private(self, path)  # password hashes
        with sqlite_file(path) as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT name FROM sqlite_master").fetchall(), [])  # the check's table is gone
        self.assertEqual(self.check(str(path)), path)  # an absolute path; an empty database is fine again

    def test_what_is_in_the_file(self):
        path = self.home / "site.sqlite3"
        with sqlite_file(path) as db:  # HFile's tables and no account: an install that stopped half way
            db.execute("CREATE TABLE accounts_user (id integer)")
        self.check("site.sqlite3")
        with sqlite_file(path) as db:
            db.execute("INSERT INTO accounts_user VALUES (1)")
        self.assertProblem("site.sqlite3", "里已经装好了 HFile")
        with sqlite_file(self.home / "other.sqlite3") as db:
            db.execute("CREATE TABLE someone_elses (id integer)")
        self.assertProblem("other.sqlite3", "里已经有别的程序的表了（someone_elses）。请换一个新的文件名")
        with sqlite_file(self.home / "other.sqlite3") as db:  # looked at only: not made WAL
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "delete")

    def test_the_built_in_users_are_no_accounts(self):
        # The migrations make the guest and the template user (accounts/migrations/0002): a
        # database with only those is an install that stopped half way, not a site in use.
        path = self.home / "half.sqlite3"
        with sqlite_file(path) as db:
            db.execute("CREATE TABLE accounts_user (id integer, username text, kind text)")
            db.execute("INSERT INTO accounts_user VALUES (1, 'guest', 'guest'), (2, 'template', 'template')")
        self.check("half.sqlite3")
        with sqlite_file(path) as db:
            db.execute("INSERT INTO accounts_user VALUES (3, 'boss', 'normal')")
        self.assertProblem("half.sqlite3", "里已经装好了 HFile")

    def test_files_it_cant_use(self):
        (self.home / "notes.txt").write_text("not a database, " * 20)
        self.assertProblem("notes.txt", "不是 SQLite 的数据库文件")
        self.assertEqual((self.home / "notes.txt").read_text(), "not a database, " * 20)  # left alone
        (self.home / "a folder").mkdir()
        self.assertProblem("a folder", "是一个文件夹")
        (self.home / "a file").write_text("")
        self.assertProblem("a file/site.sqlite3", "没法建文件夹")
        if getattr(os, "geteuid", lambda: -1)() != 0:  # root may write anywhere
            locked = self.home / "locked"
            if os.name == "nt":  # a folder's read-only attribute keeps nothing out there
                locked.mkdir()
                deny(self, locked, "WD,AD")
            else:
                locked.mkdir(mode=0o500)
                self.addCleanup(locked.chmod, 0o700)
            self.assertProblem("locked/site.sqlite3", "没法在")
            self.assertFalse((locked / "site.sqlite3").exists())

    def dry(self, file):
        return service.preflight(service.Database(engine="sqlite", file=file), dry_run=True)

    def test_a_dry_run_leaves_no_trace(self):
        path = self.home / "data" / "new folder" / "site.sqlite3"
        self.assertEqual(self.dry("data/new folder/site.sqlite3"), f"能用：安装时会新建数据库文件 {path}，还有它所在的文件夹 {path.parent}。")
        self.assertFalse((self.home / "data").exists())  # neither the file nor the folders
        path = self.home / "empty.sqlite3"
        with sqlite_file(path) as db:
            db.execute("CREATE TABLE t (x)")
            db.execute("DROP TABLE t")
        before = path.read_bytes()
        self.assertEqual(self.dry("empty.sqlite3"), f"能用：数据库文件 {path} 已经有了，里面是空的。")
        self.assertEqual(path.read_bytes(), before)  # not even made WAL
        self.assertEqual(sorted(file.name for file in self.home.iterdir()), ["config.toml", "empty.sqlite3"])
        with sqlite_file(path) as db:
            db.execute("CREATE TABLE accounts_user (id integer)")
        self.assertEqual(self.dry("empty.sqlite3"), f"能用：数据库文件 {path} 里是一个没装完的 HFile，安装会接着装完。")
        (self.home / "a file").write_text("")
        with self.assertRaises(service.InstallError):
            self.dry("a file/site.sqlite3")  # nowhere to make it: the same problems as for real

    def test_a_check_that_fails_leaves_no_trace(self):
        with mock.patch.object(service, "_try_sqlite", side_effect=sqlite3.OperationalError("disk I/O error")):
            self.assertProblem("new/site.sqlite3", "（disk I/O error）")
        self.assertFalse((self.home / "new").exists())


@override_settings(**SETUP)
class CheckTests(TempHomeMixin, SimpleTestCase):
    """POST /install/check/: one step of the form, before the page goes on to the next (install.js)."""

    def check(self, step, client=None, headers=None, **changes):
        data = form_data(**{"token": token.get(), "step": step, **changes})
        return (client or self.client).post("/install/check/", data, headers=headers)

    def test_the_token_first(self):
        # Logged as itself: Django leaves 4xx out of the console (django.request is at ERROR).
        with mock.patch.object(service, "preflight") as preflight, self.assertLogs("installer", "WARNING") as logs:
            r = self.check("database", token="AAAA-BBBB-CCCC-DDDD")
        self.assertIn("Wrong install token", logs.output[0])
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json(), {"ok": False, "step": "token", "errors": {"token": [views.WRONG_TOKEN]}})
        preflight.assert_not_called()  # without it, no server it names is connected to
        self.assertEqual(self.check("token").json(), {"ok": True})
        # No CSRF check, as for the form (views.py): the token does that job.
        r = self.check("token", client=Client(enforce_csrf_checks=True), headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(r.json(), {"ok": True})
        with self.assertNoLogs("django.request"):  # a step that isn't right yet is nothing for the log
            r = self.check("nothing")
        self.assertEqual((r.status_code, r.json()["ok"]), (400, False))

    def test_a_step_on_its_own(self):
        # Its own fields only: the steps after it aren't filled in yet.
        r = self.check("site", url="files.example.com", hosts="", username="", password1="", password2="")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(
            r.json()["errors"],
            {"url": ['要以 https:// 或 http:// 开头，比如 "https://files.example.com"。'], "hosts": ["该字段是必填项。"]},
        )
        self.assertEqual(self.check("site", username="", password1="").json(), {"ok": True})
        self.assertEqual(self.check("admin", password2=STRONG + "x").json(), {"ok": False, "errors": {"password2": ["两次输入的密码不一样。"]}})
        self.assertIn("password1", self.check("admin", password1="12345678", password2="12345678").json()["errors"])

    def test_the_database_is_tested(self):
        with mock.patch.object(service, "preflight", return_value="连上了 MySQL 8.4.3。数据库 hfile 已经有了，是空的。") as preflight:
            r = self.check("database", username="")
        self.assertEqual(r.json(), {"ok": True, "message": "连上了 MySQL 8.4.3。数据库 hfile 已经有了，是空的。"})
        preflight.assert_called_once_with(MYSQL, dry_run=True)
        problem = service.InstallError("MySQL 拒绝了用户名 hfile 和这个密码。", "db_password")
        with mock.patch.object(service, "preflight", side_effect=problem), self.assertNoLogs("django.request"):
            r = self.check("database")
        self.assertEqual((r.status_code, r.json()), (400, {"ok": False, "message": problem.message, "field": "db_password"}))
        with mock.patch.object(service, "preflight") as preflight:
            r = self.check("database", db_port="x")
        self.assertEqual(r.json(), {"ok": False, "errors": {"db_port": ["端口是 1 到 65535 之间的整数。"]}})
        preflight.assert_not_called()

    def test_sqlite_without_a_trace(self):
        r = self.check("database", db_engine="sqlite", db_file="data/site.sqlite3")
        path = self.home / "data" / "site.sqlite3"
        self.assertEqual(r.json(), {"ok": True, "message": f"能用：安装时会新建数据库文件 {path}，还有它所在的文件夹 {path.parent}。"})
        self.assertFalse(path.parent.exists())

    def test_once_installed(self):
        configfile.update(self.config, {"site": {"secret_key": "k" * 50}})
        self.assertEqual(self.check("token").status_code, 409)
