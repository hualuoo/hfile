"""config.toml (config/configfile.py), the command line around it (config/cli.py: manage.py and
hfile.py) and the settings it gives (config/settings.py); the names of unknown codecs Python doesn't
keep (config/codec_cache.py). The server itself: config/test_serve.py."""

import argparse
import ast
import codecs
import collections
import contextlib
import encodings
import io
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc
import types
import unittest
import zoneinfo
from pathlib import Path
from unittest import mock

import certifi

from django.conf import settings
from django.core.management import get_commands, load_command_class
from django.core.management.base import BaseCommand
from django.test import SimpleTestCase
from django.utils import translation

from apps.core.testing import assert_private

from . import admin_path, cli, cli_help, codec_cache, configfile, database, frozen
from .version import VERSION

ROOT = settings.BASE_DIR
KEY = "k" * 50
# What a process on Windows can't do without: Winsock finds itself by SYSTEMROOT (no sockets otherwise),
# tempfile by TEMP and TMP.
WINDOWS_ENV = ("SYSTEMROOT", "TEMP", "TMP") if os.name == "nt" else ()
# What chooses the language of a program (config/i18n.py).
LANGUAGE_ENV = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def child_env(**extra):
    """A clean environment for a new process: not this one's HFILE_CONFIG. In the language the test runner
    chose (LANGUAGE, apps/core/testing.py), unless the test chooses one: without it a program on Windows
    speaks the language Windows shows, English on GitHub's machines."""
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}
    env.update({key: os.environ[key] for key in WINDOWS_ENV if key in os.environ})
    if "LANGUAGE" in os.environ and not any(key in extra for key in LANGUAGE_ENV):
        env["LANGUAGE"] = os.environ["LANGUAGE"]
    env.update({key: str(value) for key, value in extra.items()})
    return env


def run(*args, timeout=120, **env):
    return subprocess.run(
        [sys.executable, "-B", *args], cwd=ROOT, env=child_env(**env), capture_output=True, text=True,
        encoding="utf-8", timeout=timeout,
    )


def argparse_texts():
    """(text, plural or None) of each message argparse.py translates (its _() and ngettext())."""
    found = set()
    for node in ast.walk(ast.parse(Path(argparse.__file__).read_text(encoding="utf-8"))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in ("_", "ngettext"):
            found.add((node.args[0].value, node.args[1].value if node.func.id == "ngettext" else None))
    return found


class TempDirMixin:
    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory(prefix="hfile-config-")
        self.addCleanup(self._remove_tmp)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "config.toml"

    def _remove_tmp(self):
        """On Windows a server the test started (test_serve.py) may hold its files a moment after the test stopped
        it: its site ends after the outer process, by the job object (config/windows.py), and Windows doesn't
        delete a file that is open. Seen once in three runs in parallel."""
        deadline = time.monotonic() + 10
        while True:
            try:
                return self._tmp.cleanup()
            except PermissionError:
                if os.name != "nt" or time.monotonic() > deadline:
                    raise
                time.sleep(0.1)

    def write(self, text):
        self.path.write_text(text, encoding="utf-8")
        return self.path


class TableTests(SimpleTestCase):
    """One table (configfile.OPTIONS) defines the file: its sections, keys, defaults and comments."""

    def test_the_specifications_keys(self):
        self.assertEqual(
            {section: [option.key for option in options] for section, options in configfile.SECTIONS.items()},
            {
                "server": ["host", "port", "threads", "proxies", "log_requests"],
                "database": ["engine", "file", "host", "port", "name", "user", "password", "tls", "ca"],
                "site": ["secret_key", "allowed_hosts", "url", "admin_path", "debug"],
                "paths": ["uploads", "backups", "geoip"],
                "upgrade": ["auto"],
            },
        )
        defaults = configfile.defaults()
        self.assertEqual(
            (defaults["server"]["host"], defaults["server"]["port"], defaults["server"]["threads"],
             defaults["server"]["proxies"], defaults["server"]["log_requests"]),
            ("0.0.0.0", 8080, 8, 0, True),
        )
        self.assertEqual((defaults["database"]["engine"], defaults["database"]["file"], defaults["database"]["name"],
                          defaults["database"]["user"]), ("sqlite", "hfile.sqlite3", "hfile", "hfile"))
        self.assertEqual((defaults["site"]["secret_key"], defaults["site"]["debug"]), ("", False))
        self.assertEqual((defaults["paths"]["uploads"], defaults["paths"]["backups"]), ("media", "backups"))
        self.assertIsNone(defaults["upgrade"]["auto"])  # unset: on in the single executable, off from source

    def test_every_setting_says_what_it_is(self):
        # In English in the table, in the language of a new file when it's written: Chinese here.
        for option in configfile.OPTIONS:
            if option.comment:
                self.assertRegex(configfile._(option.comment), r"[一-鿿]", option.key)
        rendered = configfile.render()
        self.assertTrue(rendered.startswith("# HFile 的设置。"))
        self.assertIn('\nhost = "0.0.0.0"\n', rendered)
        self.assertIn("\n# 端口\nport = 8080\n", rendered)

    def test_the_example_is_what_a_new_file_is(self):
        # config.example.toml is what `hfile` writes when there is no config.toml.
        self.assertEqual((ROOT / "config.example.toml").read_text(encoding="utf-8"), configfile.render())


class FileTests(TempDirMixin, SimpleTestCase):
    def test_a_new_file(self):
        self.assertTrue(configfile.create(self.path))
        self.assertEqual(self.path.read_text(encoding="utf-8"), configfile.render())
        assert_private(self, self.path)  # it will hold passwords
        self.assertFalse(configfile.create(self.path))  # never over an existing one
        self.assertEqual(configfile.load(self.path), configfile.defaults())
        self.assertEqual(configfile.load(self.dir / "missing.toml"), configfile.defaults())
        with self.assertRaisesMessage(configfile.ConfigError, "不存在，没法在那里新建设置文件"):
            configfile.create(self.dir / "no-such-folder" / "config.toml")

    def test_values_read_back_as_they_were(self):
        tricky = "a\"b\\c\nd\te ${HOME} #x 'q' 密码 \x7f \x00 end\\"
        configfile.create(self.path)
        configfile.update(self.path, {
            "database": {"password": tricky, "port": 13306},
            "site": {"allowed_hosts": ["example.com", "[::1]"], "debug": True},
        })
        values = configfile.load(self.path)
        self.assertEqual(values["database"]["password"], tricky)
        self.assertEqual((values["database"]["port"], values["site"]["allowed_hosts"]), (13306, ["example.com", "[::1]"]))
        self.assertIs(values["site"]["debug"], True)
        self.assertIsNone(values["upgrade"]["auto"])  # still left out
        self.assertIn("\n# auto = true\n", self.path.read_text(encoding="utf-8"))
        assert_private(self, self.path)
        self.assertFalse((self.dir / "config.toml.part").exists())
        with self.assertRaisesMessage(configfile.ConfigError, "[server] port"):
            configfile.update(self.path, {"server": {"port": 0}})  # what is written has to be right
        self.assertEqual(configfile.load(self.path), values)

    def test_rewritten_while_a_site_runs_with_it(self):
        """The installer writes config.toml while serve holds its lock on it: on Windows an open file, which
        os.replace() is refused (configfile.write)."""
        configfile.create(self.path)
        serving = configfile.hold_while_serving(self.path)
        self.addCleanup(serving.close)
        self.assertTrue(configfile.in_use(self.path))
        configfile.update(self.path, {"site": {"secret_key": KEY}})
        self.assertEqual(configfile.load(self.path)["site"]["secret_key"], KEY)
        assert_private(self, self.path)

    def test_update_keeps_the_rest(self):
        self.write('[server]\nport = 9000\n\n[site]\nurl = "https://Files.Example.com/"\n[upgrade]\nauto = false\n')
        configfile.update(self.path, {"site": {"secret_key": KEY}})
        values = configfile.load(self.path)
        self.assertEqual(
            (values["server"]["port"], values["site"]["url"], values["site"]["secret_key"], values["upgrade"]["auto"]),
            (9000, "https://files.example.com", KEY, False),
        )

    def test_a_part_file_left_behind_changes_nothing(self):
        configfile.create(self.path)
        elsewhere = self.dir / "elsewhere.txt"
        elsewhere.write_text("not a config")
        part = self.path.with_name("config.toml.part")
        part.symlink_to(elsewhere)  # or one with other modes: both would be written through
        configfile.update(self.path, {"site": {"secret_key": KEY}})
        self.assertEqual(elsewhere.read_text(), "not a config")
        self.assertFalse(self.path.is_symlink())
        assert_private(self, self.path)
        self.assertEqual(configfile.load(self.path)["site"]["secret_key"], KEY)
        self.assertFalse(part.exists())

    def test_mistakes_are_listed_at_once(self):
        cases = [
            ('[server]\nport = "8080"\n', '[server] port：要写 1 到 65535 之间的整数（不加引号），现在是 "8080"。'),
            ("[server]\nport = 0\n", "[server] port：要写 1 到 65535 之间的整数"),
            ("[server]\nport = true\n", "[server] port：要写 1 到 65535 之间的整数（不加引号），现在是 true。"),
            ("[server]\nprot = 8080\n", "[server] prot：没有这个设置，看看是不是写错了。这一节可以有：host、port、threads、proxies、log_requests。"),
            ("[server]\nlog_files = true\n", "[server] log_files：没有这个设置"),
            ("[sever]\nport = 8080\n", "[sever]：没有这一节，看看是不是写错了。"),
            ("[Server]\nPort = 8080\n", "[Server]：没有这一节"),  # HFile's still (not another program's file)
            ('[mail]\nhost = "smtp.example.com"\n', "[mail]：没有这一节"),
            ('[server]\nport = 8080\n[mail]\nfile_dir = "x"\n', "[mail]：没有这一节"),  # half of it HFile's: HFile's still
            ("port = 8080\n", "port：要写在某一节下面"),
            ("", None),
            ('[server]\nhost = " "\n', "[server] host：要写一个 IP"),
            ("[server]\nproxies = 6\n", "[server] proxies：要写 0 到 5 之间的整数"),
            ('[site]\ndebug = "yes"\n', '[site] debug：要写 true 或 false（小写，不加引号），现在是 "yes"。'),
            ('[site]\nallowed_hosts = "a.com,b.com"\n', "[site] allowed_hosts：要写成列表"),
            ('[site]\nallowed_hosts = ["ok.com", "bad host!", 3]\n', "[site] allowed_hosts：「bad host!」、3不是域名或 IP。"),
            ('[site]\nurl = "files.example.com"\n', "[site] url：要以 https:// 或 http:// 开头"),
            ('[site]\nurl = "https://files.example.com:x/"\n', "[site] url：要以 https:// 或 http:// 开头"),
            ('[site]\nurl = "https://files.example.com/sub/"\n', "[site] url：只写到域名（和端口）为止"),
            # OPS-09: a host that isn't one, said as such (it was "要以 https:// 或 http:// 开头").
            ('[site]\nurl = "https://exa mple.com"\n', "[site] url：「exa mple.com」不是域名或 IP。"),
            ('[site]\nurl = "https://例子..中国"\n', "[site] url：「例子..中国」不是域名或 IP。中文域名要能转成 xn-- 开头的形式"),
            ('[site]\nallowed_hosts = ["☃.net"]\n', "[site] allowed_hosts：「☃.net」不是域名或 IP。中文域名要能转成 xn-- 开头的形式"),
            ('[site]\nurl = "https://user@files.example.com"\n', "[site] url：只写到域名（和端口）为止"),
            # SEC-08: TLS to MySQL, and the CA certificate to check the server's with.
            ('[database]\ntls = "on"\n', '[database] tls：要写 "preferred"、"required"、"verify_ca" 或 "verify_identity"，现在是 "on"。'),
            ('[database]\ntls = "verify_ca"\n', '[database] ca：tls 是 "verify_ca" 或 "verify_identity" 时，要写校验证书用的 CA 证书文件。'),
            ('[database]\nca = "ca.pem"\n', '[database] ca：只有 tls 是 "verify_ca" 或 "verify_identity" 时才校验证书'),
            ('[database]\ntls = "required"\n', None),
            ('[database]\ntls = "VERIFY_IDENTITY"\nca = "ca.pem"\n', None),
            ('[site]\nadmin_path = "api"\n', "[site] admin_path：/api/ 是网站自己要用的地址，请换一个。"),
            ('[site]\nadmin_path = "Admin"\n', "[site] admin_path：/admin/ 永远是 404"),
            ('[site]\nsecret_key = "short"\n', "[site] secret_key：太短了，至少要 32 个字符。"),
            ('[database]\nengine = "postgres"\n', '[database] engine：要写 "sqlite" 或 "mysql"，现在是 "postgres"。'),
            ('[database]\nfile = " "\n', "[database] file：不能为空。"),
            ('[database]\nfile = "a\\nb.sqlite3"\n', "[database] file：里面不能有换行之类的控制字符。"),
            ('[database]\nname = "my-db"\n', "[database] name：只能用英文字母、数字和下划线"),
            ('[database]\nuser = ""\n', "[database] user：不能为空"),
            ("[upgrade]\nauto = 1\n", "[upgrade] auto：要写 true 或 false"),
            ('[paths]\nuploads = ""\n', "[paths] uploads：不能为空。"),
            # A password or a key isn't repeated in the message (the console, a log).
            ("[database]\npassword = 123456\n", "[database] password：要写成带英文双引号的文字（这里不显示它现在的值）。"),
            ("server = 1\n[server]\n", "格式不对"),  # the same name twice
            ('[server]\nhost = 0.0.0.0\n', "格式不对：Expected newline or end of document after a statement（第 2 行第 11 个字符）"),
            ("[server]\nport = 8080", None),  # fine: no newline at the end
            # OPS-04: no host at all, and no url to give one: every request was a 400.
            ("[site]\nallowed_hosts = []\n", "[site] allowed_hosts：是空的，url 也空着"),
            ('[site]\nallowed_hosts = []\nurl = "https://files.example.com"\n', None),
        ]
        for text, message in cases:
            with self.subTest(text=text):
                self.write(text)
                if message is None:
                    configfile.load(self.path)
                    continue
                with self.assertRaises(configfile.ConfigError) as caught:
                    configfile.load(self.path)
                self.assertIn(message, str(caught.exception))
                self.assertIn(str(self.path), str(caught.exception))
        # Every mistake at once.
        self.write('[server]\nport = "x"\nthreads = 0\n[site]\ndebug = 1\n')
        with self.assertRaises(configfile.ConfigError) as caught:
            configfile.load(self.path)
        self.assertEqual(len(caught.exception.problems), 3)
        self.assertIn("里有 3 处要改", str(caught.exception))

    def test_another_programs_file(self):
        """A config.toml mostly of other things than HFile's sections and settings, like another program's in the
        downloads folder HFile was started in: said to be one, not its settings listed as HFile's mistakes, which
        would have them changed. A few of its names may be HFile's too."""
        others = [
            # settings at the top, a nested table (the one reported)
            'model = "m"\nmodel_provider = "relay"\n\n[model_providers.relay]\nbase_url = "https://relay.example.com/v1"\n',
            'MusicFolder = "D:/Music"\nPort = 4533\nLogLevel = "info"\n',  # settings at the top only, one named as HFile's
            # an array of tables
            'concurrent = 4\n\n[session_server]\nsession_timeout = 1800\n\n[[runners]]\nname = "build"\nurl = "https://ci.example.com"\n',
            # tables holding settings named as HFile's
            'serverAddr = "203.0.113.10"\nserverPort = 7000\n\n[webServer]\nport = 7400\nuser = "admin"\npassword = "admin"\n\n'
            '[log]\nlevel = "info"\n\n[[proxies]]\nname = "web"\nlocalPort = 8080\n',
            '[server]\nport = 8501\nheadless = true\n\n[browser]\ngatherUsageStats = false\n\n[theme]\nbase = "dark"\n',  # one of HFile's sections
            # a table named as one of HFile's settings (debug)
            'version = 2\nroot = "/var/lib/x"\n\n[grpc]\naddress = "/run/x.sock"\n\n[debug]\nlevel = "info"\n\n[metrics]\naddress = ""\n',
        ]
        for text in others:
            with self.subTest(text=text):
                self.write(text)
                with self.assertRaises(configfile.ConfigError) as caught:
                    configfile.load(self.path)
                self.assertEqual(str(caught.exception), "\n".join([
                    f"设置文件 {self.path} 看起来不是 HFile 的：里面的内容大多不是 HFile 的节（[server]、[database]、[site]、[paths]、[upgrade]）"
                    "或设置，多半是别的程序的同名文件。HFile 不会改动它。",
                    "把 HFile 放进一个单独的文件夹里再运行，它会在那里新建自己的设置文件；或者用 --config 指定另一个设置文件。",
                ]))

    def test_secrets_arent_repeated(self):
        for text, secret in (("[database]\npassword = 987654321\n", "987654321"),
                             ("[site]\nsecret_key = 31415926535897932384626433832795028841\n", "3141592653")):
            with self.assertRaises(configfile.ConfigError) as caught:
                configfile.clean(configfile.tomllib.loads(text))
            self.assertNotIn(secret, str(caught.exception))
            self.assertIn("这里不显示它现在的值", str(caught.exception))

    def test_encodings_and_odd_files(self):
        self.path.write_bytes(b"\xef\xbb\xbf[server]\nport = 8001\n")  # "UTF-8 with BOM", as Windows Notepad saves
        self.assertEqual(configfile.load(self.path)["server"]["port"], 8001)
        self.path.write_bytes('[database]\npassword = "密码"\n'.encode("gbk"))
        with self.assertRaisesMessage(configfile.ConfigError, "要用 UTF-8 编码保存"):
            configfile.load(self.path)
        self.write("[site]\nallowed_hosts = " + "[" * 5000 + "]" * 5000 + "\n")
        with self.assertRaisesMessage(configfile.ConfigError, "嵌套得太深"):
            configfile.read(self.path)

    def test_hosts_urls_and_folders(self):
        backups = "C:/srv/backups" if os.name == "nt" else "/srv/backups"  # absolute: on Windows with its drive
        self.write(
            '[server]\nhost = "[::]"\n'
            '[site]\nallowed_hosts = ["Example.COM:8080", "::1", "*", ".example.org", "10.0.0.1:80", "example.com"]\n'
            'url = "https://Files.example.com:8443"\n'
            f'[paths]\nuploads = "data/my uploads"\nbackups = "{backups}"\n'
        )
        values = configfile.load(self.path)
        self.assertEqual(values["server"]["host"], "::")
        self.assertEqual(values["site"]["allowed_hosts"], ["example.com", "[::1]", "*", ".example.org", "10.0.0.1"])
        self.assertEqual(values["site"]["url"], "https://files.example.com:8443")
        self.assertEqual(configfile.allowed_hosts(values)[-1], "files.example.com")  # the url's host too
        # A space in a folder is fine (Windows has them); relative to the file's own folder.
        self.assertEqual(configfile.folder(self.path, values["paths"]["uploads"]), self.dir / "data" / "my uploads")
        self.assertEqual(configfile.folder(self.path, values["paths"]["backups"]), Path(backups))

    def test_the_site_url_as_a_browser_writes_the_origin(self):
        # CSRF_TRUSTED_ORIGINS is compared with the browser's Origin exactly: :443 would make every form a 403.
        for typed, kept in (
            ("https://files.example.com:443", "https://files.example.com"),
            ("https://files.example.com:", "https://files.example.com"),
            ("https://Files.Example.com.", "https://files.example.com"),
            ("https://files.example.com:08443/", "https://files.example.com:8443"),
            ("http://[0:0:0:0:0:0:0:1]:80", "http://[::1]"),
            ("http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ):
            self.assertEqual(configfile.site_url(typed), kept, typed)
        with self.assertRaises(configfile.Problem):
            configfile.site_url("https://files.example.com:0")

    def test_hosts_in_other_scripts(self):
        # OPS-09: as browsers send them (the Host header, the Origin CSRF compares): punycode.
        self.write('[site]\nallowed_hosts = ["例子.中国", ".Bücher.example:8080"]\nurl = "https://例子。中国:8443"\n')
        values = configfile.load(self.path)
        self.assertEqual(values["site"]["allowed_hosts"], ["xn--fsqu00a.xn--fiqs8s", ".xn--bcher-kva.example"])
        self.assertEqual(values["site"]["url"], "https://xn--fsqu00a.xn--fiqs8s:8443")
        self.assertEqual(configfile.allowed_hosts(values), ["xn--fsqu00a.xn--fiqs8s", ".xn--bcher-kva.example"])

    def test_tls_to_mysql(self):
        # SEC-08: there was no way to ask for TLS, nor to check the server's certificate.
        self.write('[database]\nengine = "mysql"\ntls = "verify_identity"\nca = "certs/mysql-ca.pem"\n')
        options = database.django_settings(configfile.load(self.path)["database"], self.path)["OPTIONS"]
        self.assertEqual(options["ssl_mode"], "VERIFY_IDENTITY")
        self.assertEqual(options["ssl"], {"ca": str(self.dir / "certs" / "mysql-ca.pem")})  # next to config.toml
        self.write('[database]\nengine = "mysql"\ntls = "required"\n')
        options = database.django_settings(configfile.load(self.path)["database"], self.path)["OPTIONS"]
        self.assertEqual((options["ssl_mode"], "ssl" in options), ("REQUIRED", False))
        self.write('[database]\nengine = "mysql"\n')  # MySQL's own default, PREFERRED: nothing is added
        options = database.django_settings(configfile.load(self.path)["database"], self.path)["OPTIONS"]
        self.assertEqual(sorted(options), ["charset", "init_command"])

    def test_the_kind_of_database(self):
        for text, engine in [
            ("", "sqlite"),
            ('[database]\nfile = "data/site.sqlite3"\n', "sqlite"),
            ('[database]\nengine = " MySQL "\n', "mysql"),
            ('[database]\nengine = "sqlite3"\n', "sqlite"),
            # Only engine decides: MySQL's settings without it are SQLite still (no old files to guess at).
            ('[database]\nhost = "127.0.0.1"\nport = 3306\nname = "hfile"\nuser = "hfile"\npassword = "pw"\n', "sqlite"),
        ]:
            with self.subTest(text=text):
                self.write(text)
                self.assertEqual(configfile.load(self.path)["database"]["engine"], engine)


class WhichFileTests(TempDirMixin, SimpleTestCase):
    def test_the_command_line(self):
        self.assertEqual(configfile.split_argv(["--config", "a.toml", "serve"]), ("a.toml", ["serve"]))
        self.assertEqual(configfile.split_argv(["upgrade", "--check", "--config=b.toml"]), ("b.toml", ["upgrade", "--check"]))
        self.assertEqual(configfile.split_argv(["shell", "--", "--config", "x"]), (None, ["shell", "--", "--config", "x"]))
        self.assertEqual(configfile.split_argv([]), (None, []))
        for bad in (["--config"], ["--config="], ["--config", "--version"]):
            with self.subTest(bad=bad), self.assertRaisesMessage(configfile.ConfigError, "--config 后面要写设置文件的路径"):
                configfile.split_argv(bad)

    def test_which_file(self):
        with mock.patch.dict(os.environ, {configfile.ENV_VAR: str(self.dir / "env.toml")}):
            self.assertEqual(configfile.locate("given.toml"), Path.cwd() / "given.toml")  # --config first
            self.assertEqual(configfile.locate(), self.dir / "env.toml")  # then $HFILE_CONFIG
            self.assertEqual(configfile.locate(str(self.dir)), self.dir / "config.toml")  # a folder: the file in it
            self.assertEqual(configfile.locate(f"{self.dir}/new-site/"), self.dir / "new-site" / "config.toml")
        with mock.patch.dict(os.environ):
            os.environ.pop(configfile.ENV_VAR, None)
            self.assertEqual(configfile.locate(), ROOT / "config.toml")  # from the source code: next to manage.py
            program = self.dir / "bin" / "hfile"
            # The program's folder with links resolved, and on Windows 8.3 short names spelled out, as a
            # temporary folder may have them (C:\Users\RUNNER~1\…).
            beside = self.dir.resolve() / "bin" / "config.toml"
            with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", str(program)):
                # PyInstaller: next to the executable, not the current folder nor where it unpacks itself.
                self.assertEqual(configfile.locate(), beside)
                self.assertEqual(cli.own_command("x"), [str(program)])
            # Nuitka: sys.argv[0] is the executable (its sys.executable doesn't exist).
            with mock.patch.object(configfile, "_nuitka", return_value=object()), mock.patch.object(configfile, "_ARGV0", str(program)):
                self.assertEqual(configfile.locate(), beside)
        self.assertEqual(cli.own_command("manage.py"), [sys.executable, str(Path("manage.py").resolve())])
        # Neither stops the program with a traceback:
        configfile.locate("~nosuchuser-hfile/config.toml")
        configfile.locate("/tmp/" + "x" * 300)


class FrozenTests(TempDirMixin, SimpleTestCase):
    """The single executable takes the time zones and CA certificates of the system it runs on
    (frozen.py), not those of the machine it was built on: a server has none of that machine's."""

    def setUp(self):
        super().setUp()
        self.addCleanup(zoneinfo.reset_tzpath)  # back to this Python's own

    def test_only_the_single_executable(self):
        with mock.patch.object(frozen, "use_system_files") as use, contextlib.redirect_stdout(io.StringIO()):
            cli.main(["manage.py", "--version"])
            use.assert_not_called()
            with mock.patch.object(configfile, "FROZEN", True):
                cli.main(["hfile", "--version"])
        use.assert_called_once_with()

    def test_time_zones(self):
        frozen.use_system_files({})
        self.assertEqual(zoneinfo.TZPATH, frozen.TZPATH if os.name == "posix" else ())
        # A system without them: tzdata's, which goes into the program (requirements.txt, scripts/build.sh).
        zoneinfo.reset_tzpath([str(self.dir)])
        self.assertEqual(str(zoneinfo.ZoneInfo.no_cache(settings.TIME_ZONE)), "Asia/Shanghai")
        frozen.use_system_files({"PYTHONTZPATH": str(self.dir)})  # set: as Python took it
        self.assertEqual(zoneinfo.TZPATH, (str(self.dir),))

    @unittest.skipUnless(os.name == "posix", "Windows has no TZDIR")
    def test_the_c_librarys_time_zones(self):
        # With the system's: nothing to do. Without any (a minimal image): tzdata's, for the log's times.
        env = {}
        with mock.patch.object(frozen, "TZPATH", (str(self.dir),)):
            frozen.use_system_files(env)
        self.assertNotIn("TZDIR", env)
        with mock.patch.object(frozen, "TZPATH", (str(self.dir / "missing"),)):
            frozen.use_system_files(env)
            self.assertEqual(env["TZDIR"], frozen.tzdata_folder())
            self.assertTrue(Path(env["TZDIR"], "Asia", "Shanghai").is_file())
            frozen.use_system_files(env)  # serve's child: chooses again, its own
            self.assertEqual(env["TZDIR"], env[frozen.CHOSEN_TZDIR])
            env = {"TZDIR": "/own/zoneinfo"}  # set by hand: as it is
            frozen.use_system_files(env)
            self.assertEqual(env["TZDIR"], "/own/zoneinfo")

    @unittest.skipUnless(os.name == "posix", "Windows: Python reads the system's certificate store itself")
    def test_ca_certificates(self):
        bundle = self.dir / "ca-bundle.crt"
        bundle.write_text("")
        missing = str(self.dir / "missing.pem")
        with mock.patch.object(frozen, "CA_FILES", (missing, str(bundle))):
            env = {}
            frozen.use_system_files(env)
        env = {key: value for key, value in env.items() if "TZDIR" not in key}  # the time zones: above
        self.assertEqual(env, {"SSL_CERT_FILE": str(bundle), frozen.CHOSEN: str(bundle)})
        # The program it starts (serve's child) chooses for itself: here on a system without any,
        # certifi's, inside the program.
        with mock.patch.object(frozen, "CA_FILES", (missing,)):
            frozen.use_system_files(env)
        env = {key: value for key, value in env.items() if "TZDIR" not in key}
        self.assertEqual(env, {"SSL_CERT_FILE": certifi.where(), frozen.CHOSEN: certifi.where()})
        self.assertTrue(Path(certifi.where()).is_file())
        for given in ({"SSL_CERT_FILE": "/own.pem"}, {"SSL_CERT_DIR": "/own/certs"}):  # set by hand: as it is
            with self.subTest(given=given):
                env = dict(given)
                frozen.use_system_files(env)
                self.assertEqual({key: value for key, value in env.items() if "TZDIR" not in key}, given)


class CommandLineTests(TempDirMixin, SimpleTestCase):
    """manage.py and hfile.py in a new process."""

    def test_a_command_error_is_its_message_alone(self):
        # Without Django's "CommandError: " in front; the exit status is the error's (upgrade --check: 4).
        self.write(f'[database]\nfile = "missing/site.sqlite3"\n[site]\nsecret_key = "{KEY}"\n')
        r = run("manage.py", "--config", self.path, "upgrade", "--check")
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertTrue(r.stderr.startswith("读不了数据库文件"), r.stderr)
        self.assertNotIn("CommandError", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_ctrl_c_while_the_site_starts(self):
        """Ctrl+C in the site's process (serve's child) while it starts, here as Django begins loading the apps,
        ends it as one while it serves does: with 0, without KeyboardInterrupt's traceback (and, in the single
        executable, PyInstaller's "Failed to execute script")."""
        self.write('[server]\nhost = "127.0.0.1"\n')
        script = (
            "import signal, sys\n"
            "import django\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)  # as from a terminal, whatever started this\n"
            "setup = django.setup\n"
            "def interrupted(*args, **kwargs):\n"
            "    signal.raise_signal(signal.SIGINT)  # Ctrl+C, now\n"
            "    return setup(*args, **kwargs)\n"
            "django.setup = interrupted\n"
            "from config import cli\n"
            f"sys.exit(cli.main(['hfile.py', '--config', {str(self.path)!r}], default_command='serve'))\n"
        )
        r = run("-c", script, timeout=60, **{cli.CHILD: "1"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertNotIn("KeyboardInterrupt", r.stderr)
        self.assertEqual(r.stdout, "")  # stopped before it served

    def test_a_command_stopped_by_ctrl_c(self):
        """Ctrl+C in a command ends it with a line in the system's language instead of KeyboardInterrupt's
        traceback, and the exit status a shell gives a program that Ctrl+C ended: while it runs, and while
        Django loads the apps, before it runs."""
        self.write(f'[site]\nsecret_key = "{KEY}"\n')
        ctrl_c = "import signal; signal.signal(signal.SIGINT, signal.default_int_handler); signal.raise_signal(signal.SIGINT)"
        for env, line in (({}, "已中断。"), ({"LANG": "en_US.UTF-8"}, "Interrupted.")):
            with self.subTest(**env):
                r = run("manage.py", "--config", self.path, "shell", "-c", ctrl_c, **env)
                self.assertEqual(r.returncode, 130, r.stderr)
                self.assertEqual(r.stderr, f"\n{line}\n")
        script = (
            "import signal, sys\n"
            "import django\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)  # as from a terminal, whatever started this\n"
            "setup = django.setup\n"
            "def interrupted(*args, **kwargs):\n"
            "    signal.raise_signal(signal.SIGINT)\n"
            "    return setup(*args, **kwargs)\n"
            "django.setup = interrupted\n"
            "from config import cli\n"
            f"sys.exit(cli.main(['manage.py', '--config', {str(self.path)!r}, 'check']))\n"
        )
        r = run("-c", script, timeout=60)
        self.assertEqual(r.returncode, 130, r.stderr)
        self.assertEqual(r.stderr, "\n已中断。\n")

    def test_a_ctrl_c_in_a_finalizer_is_not_lost(self):
        """A KeyboardInterrupt in a finalizer, which the garbage collector runs wherever the program is, can't be
        raised there: Python printed "Exception ignored in ..." with a traceback and went on without it. It is
        raised just after."""
        script = (
            "import signal, time\n"
            "from config import cli\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
            "cli._ctrl_c(passed_on=False)\n"
            "class Collected:\n"
            "    def __del__(self):\n"
            "        raise KeyboardInterrupt  # Ctrl+C just then\n"
            "try:\n"
            "    Collected()\n"
            "    time.sleep(10)  # it ends this wait too\n"
            "    print('lost')\n"
            "except KeyboardInterrupt:\n"
            "    print('raised')\n"
        )
        r = run("-c", script, timeout=60)
        self.assertEqual((r.stdout, r.stderr), ("raised\n", ""))

    def test_a_sigint_passed_on_is_the_same_ctrl_c(self):
        """The single executable outside Windows: a Ctrl+C's SIGINT comes twice, from the terminal and passed on
        by PyInstaller's bootloader. One KeyboardInterrupt for both; another Ctrl+C later is one again."""
        script = (
            "import signal, time\n"
            "from config import cli\n"
            "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
            "cli._ctrl_c(passed_on=True)\n"
            "def sigint():\n"
            "    try:\n"
            "        signal.raise_signal(signal.SIGINT)\n"
            "        for _ in range(1000):\n"
            "            pass\n"
            "    except KeyboardInterrupt:\n"
            "        return 'raised'\n"
            "    return 'not'\n"
            "print(sigint(), sigint())  # from the terminal, then passed on\n"
            "time.sleep(cli.SAME_CTRL_C + 0.1)\n"
            "print(sigint())  # another Ctrl+C\n"
        )
        r = run("-c", script)
        self.assertEqual((r.stdout, r.stderr), ("raised not\nraised\n", ""))

    def test_a_missing_file_is_created(self):
        r = run("manage.py", "--config", self.path, "check")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"没有找到设置文件，新建了一个：{self.path}", r.stderr)
        self.assertEqual(self.path.read_text(encoding="utf-8"), configfile.render())
        assert_private(self, self.path)
        r = run("manage.py", "check", HFILE_CONFIG=self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("新建", r.stderr)
        r = run("manage.py", "check", HFILE_CONFIG=self.dir)  # a folder: the config.toml in it
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("manage.py", "check", "--config", self.dir / "elsewhere.toml", HFILE_CONFIG=self.path)  # --config wins
        self.assertTrue((self.dir / "elsewhere.toml").exists())

    def test_output_that_cant_take_chinese(self):
        # OPS-03: stdout in an encoding without Chinese (Windows' cp1252 when it goes to a file or a service
        # wrapper): the first Chinese line was a UnicodeEncodeError, and the program ended with 1 (serve before
        # it listened). Neither is a terminal here: UTF-8, also for stderr, which wrote \u escapes.
        r = run("hfile.py", "--config", self.path, "help", "serve", PYTHONIOENCODING="ascii")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("启动网站", r.stdout)
        r = run("hfile.py", "--config", self.path, "check", PYTHONIOENCODING="ascii")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"没有找到设置文件，新建了一个：{self.path}", r.stderr)

    def test_asking_for_help_creates_nothing(self):
        r = run("manage.py", "--version", "--config", self.path)
        self.assertEqual(r.stdout.split()[:2], ["HFile", VERSION])
        r = run("hfile.py", "-V", HFILE_CONFIG=self.path)
        self.assertEqual(r.stdout.split()[:2], ["HFile", VERSION])
        r = run("manage.py", "help", "--config", self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        for command in ("serve", "upgrade", "backup", "restore"):
            self.assertIn(command, r.stdout)
        self.assertFalse(self.path.exists())

    def test_mistakes_are_plain_messages(self):
        self.write("[server]\nport = 70000\n")
        r = run("hfile.py", "--config", self.path, "check")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"设置文件 {self.path} 里有 1 处要改：\n  - [server] port：要写 1 到 65535 之间的整数", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        r = run("manage.py", "check", "--config")
        self.assertEqual((r.returncode, r.stderr.strip()), (2, "--config 后面要写设置文件的路径，比如 --config /srv/hfile/config.toml。"))
        r = run("manage.py", "--config", self.dir / "no-such-folder" / "config.toml", "check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("不存在，没法在那里新建设置文件", r.stderr)

    def test_other_commands_wait_for_the_installer(self):
        r = run("manage.py", "--config", self.path, "migrate")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"HFile 还没有安装：设置文件 {self.path} 里还没有 secret_key。", r.stderr)
        for command in (["upgrade"], ["backup"], ["createsuperuser"], ["shell", "-c", "1"]):
            with self.subTest(command=command):
                r = run("manage.py", "--config", self.path, *command)
                self.assertEqual(r.returncode, 1)
                self.assertIn("HFile 还没有安装", r.stderr)
        # Before installing there is no database: Django's checks, which runserver runs first, mustn't try one.
        r = run("manage.py", "--config", self.path, "check", "--database", "default")
        self.assertEqual(r.returncode, 0, r.stderr)
        r = run("manage.py", "--config", self.path, "collectstatic", "--help")
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_help_in_the_systems_language(self):
        # argparse writes --help before the command runs, and `help <command>` doesn't run it at all.
        r = run("manage.py", "--config", self.path, "help", "serve", LANG="en_US.UTF-8")
        self.assertIn("Starts the site: serves it on the IP and port", r.stdout)
        self.write(f'[database]\nfile = "missing/site.sqlite3"\n[site]\nsecret_key = "{KEY}"\n')
        r = run("manage.py", "--config", self.path, "backup", "--help", LANG="en_US.UTF-8")
        self.assertIn("Backs up the whole database", r.stdout)
        r = run("manage.py", "--config", self.path, "backup", "--help")
        self.assertIn("把整个数据库备份成", r.stdout)

    def test_help_all_in_one_language(self):
        # argparse's words and Django's (its options and placeholders, the list of commands, an unknown command, a
        # wrong argument) in Chinese too (cli_help.py), and in English as they write them.
        self.write(f'[database]\nfile = "missing/site.sqlite3"\n[site]\nsecret_key = "{KEY}"\n')
        command = ("manage.py", "--config", self.path)
        chinese, english = run(*command, "help", "backup"), run(*command, "help", "backup", LANG="en_US.UTF-8")
        for text in ("用法：manage.py backup [-h] [-o 输出文件]", "\n选项：\n", "\n  -h, --help            显示帮助信息并退出\n",
                     "\n  --settings 设置模块   设置模块的 Python 路径"):
            self.assertIn(text, chinese.stdout)
        for text in ("usage: manage.py backup [-h] [-o OUTPUT]", "\noptions:\n", "\n  -h, --help            show this help message and exit\n",
                     "\n  --settings SETTINGS   The Python path to a settings module"):
            self.assertIn(text, english.stdout)
        r = run(*command, "help")
        self.assertIn("\n可用的子命令：\n", r.stdout)
        self.assertIn("\n[账号]\n    resetadmin\n", r.stdout)
        r = run(*command, "help", LANG="en_US.UTF-8")
        self.assertIn("\nType 'manage.py help <subcommand>' for help on a specific subcommand.\n", r.stdout)
        self.assertIn("\n[accounts]\n    resetadmin\n", r.stdout)
        r = run(*command, "bakup")
        self.assertEqual((r.returncode, r.stderr), (1, "没有「bakup」这个命令。你是不是想用 backup？\n输入「manage.py help」查看用法。\n"))
        r = run(*command, "bakup", LANG="en_US.UTF-8")
        self.assertEqual(r.stderr, "Unknown command: 'bakup'. Did you mean backup?\nType 'manage.py help' for usage.\n")
        r = run(*command, "restore")
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertTrue(r.stderr.startswith("用法：manage.py restore "), r.stderr)
        self.assertTrue(r.stderr.endswith("\nmanage.py restore：错误：缺少必需的参数：文件\n"), r.stderr)
        r = run(*command, "restore", LANG="en_US.UTF-8")
        self.assertTrue(r.stderr.endswith("\nmanage.py restore: error: the following arguments are required: file\n"), r.stderr)

    def test_in_the_systems_language(self):
        # Before Django starts too (config/i18n.py): what `hfile` says, and the comments of a new file.
        r = run("manage.py", "--config", self.path, "check", LANG="en_US.UTF-8")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"No settings file was found, so a new one was created: {self.path}\n", r.stderr)
        self.assertTrue(self.path.read_text(encoding="utf-8").startswith("# HFile's settings."))
        r = run("manage.py", "--config", self.path, "migrate", LANG="en_US.UTF-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"HFile isn't installed yet: the settings file {self.path} has no secret_key.", r.stderr)
        self.write("[server]\nport = 70000\nthreads = 0\n")
        r = run("hfile.py", "--config", self.path, "check", LANG="en_US.UTF-8")
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"The settings file {self.path} has 2 things to fix:\n"
                      "  - [server] port: Must be a whole number from 1 to 65535 (no quotes); it is 70000 now.\n", r.stderr)


class HelpTests(SimpleTestCase):
    """cli_help.py: every command's help in the language it is shown in, and cli_texts.py, the other people's texts
    it translates."""

    CJK = re.compile(r"[㐀-鿿]")

    def setUp(self):
        cli_help.install()  # as cli.main does

    @staticmethod
    def parsers(create_parser=None):
        """(name, parser) of every command, as its help makes it, or with `create_parser`."""
        for name, app in sorted(get_commands().items()):
            command = load_command_class(app, name)
            yield name, (create_parser or BaseCommand.create_parser)(command, "hfile", name)

    @staticmethod
    def texts(parser):
        """The texts of a parser's help, its placeholders aside."""
        texts = [parser.description, parser.epilog, parser.missing_args_message, *(action.help for action in parser._actions)]
        texts += [text for group in parser._action_groups for text in (group.title, group.description)]
        return [text for text in texts if isinstance(text, str) and text and text != argparse.SUPPRESS]

    @staticmethod
    def placeholders(parser):
        """The names of the options' values as the help shows them; N, a number, stays N (the texts say N too)."""
        names = []
        for action in parser._actions:
            if action.nargs == 0 or (action.metavar is None and action.choices is not None):
                continue
            shown = action.metavar if action.metavar is not None else (
                action.dest.upper() if action.option_strings else action.dest)
            names += shown if isinstance(shown, tuple) else [shown]
        return [name for name in names if name != "N"]

    @staticmethod
    def groups():
        return {"django" if app == "django.core" else app.rpartition(".")[-1] for app in get_commands().values()}

    def test_chinese_all_of_it(self):
        with translation.override("zh-hans"):
            for name, parser in self.parsers():
                with self.subTest(name):
                    english = [text for text in [*self.texts(parser), *self.placeholders(parser)] if not self.CJK.search(text)]
                    self.assertEqual(english, [])
                    self.assertTrue(parser.format_help().startswith("用法："))
            for group in self.groups():  # the names of `hfile help`'s groups (Django REST framework is a name)
                self.assertNotEqual(translation.pgettext("command group", group), group)

    def test_english_as_django_writes_it(self):
        with translation.override("en"):
            ours, theirs = self.parsers(), self.parsers(BaseCommand.create_parser.__wrapped__)
            for (name, parser), (_name, django) in zip(ours, theirs, strict=True):
                with self.subTest(name):
                    self.assertEqual(parser.format_help(), django.format_help())
                    self.assertNotRegex(parser.format_help(), self.CJK)

    def test_every_argparse_text_translated(self):
        # its errors too, which no help shows
        with translation.override("zh-hans"):
            for text, plural in argparse_texts():
                translated = translation.ngettext(text, plural, 1) if plural else translation.gettext(text)
                self.assertNotEqual(translated, text)  # "%(heading)s：" has no Chinese character but its colon

    def test_the_list_of_other_peoples_texts(self):
        """cli_texts.py has each text of argparse's and Django's the help shows (as their code writes it, where the
        help doesn't show it in the language already), and no other. After an upgrade: copy in the missing ones,
        take out the others."""
        listed = collections.defaultdict(set)
        for node in ast.walk(ast.parse((ROOT / "config" / "cli_texts.py").read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                args = [arg.value for arg in node.args]
                if node.func.id == "pgettext_lazy":
                    listed[args[0]].add(args[1])
                elif node.func.id in ("gettext_noop", "ngettext_lazy"):
                    listed[None].add(args[0])
        shown = collections.defaultdict(set, {None: {text for text, _plural in argparse_texts()}, "command group": self.groups()})
        with translation.override("zh-hans"):
            for _name, parser in self.parsers(BaseCommand.create_parser.__wrapped__):
                shown[None].update(text for text in self.texts(parser) if not self.CJK.search(text))
                shown["placeholder"].update(name for name in self.placeholders(parser) if not self.CJK.search(name))
        for context in (None, "placeholder", "command group"):
            with self.subTest(context):
                self.assertEqual(sorted(shown[context] - listed[context]), [], "shown, not in cli_texts.py")
                self.assertEqual(sorted(listed[context] - shown[context]), [], "in cli_texts.py, no longer shown")

    def test_laid_out_by_columns(self):
        # A Chinese character takes two columns: the helps start in one column, no line runs past the width, and a
        # mark that ends a clause doesn't start a line.
        parser = argparse.ArgumentParser(
            prog="hfile demo", formatter_class=lambda prog: cli_help.Formatter(prog, width=44),
            description="把整个数据库备份成一个 .sql.gz 文件，默认放在备份文件夹里。上传的文件不在里面。")
        parser.add_argument("-o", metavar="输出文件", help="备份文件的路径，以 .sql.gz 结尾。默认在备份文件夹里按时间起名")
        parser.add_argument("--yes", action="store_true", help="不再询问，直接恢复")
        lines = parser.format_help().splitlines()
        self.assertEqual([line for line in lines if cli_help.columns(line) > 44], [])
        self.assertEqual([line for line in lines if line.strip() and line.lstrip()[0] in "，。、）」"], [])
        starts = {cli_help.columns(line[:line.index(text)]) for line in lines for text in ("备份文件的", "不再询问") if text in line}
        self.assertEqual(len(starts), 1, lines)


class SettingsTests(TempDirMixin, SimpleTestCase):
    """config/settings.py read in a new process from a config.toml."""

    def settings_of(self, text, *names):
        self.write(text)
        script = "from django.conf import settings as s\nimport json\n"
        script += f"print(json.dumps({{n: str(getattr(s, n, None)) for n in {list(names)!r}}}))"
        r = run("-c", script, HFILE_CONFIG=self.path, DJANGO_SETTINGS_MODULE="config.settings")
        if r.returncode:
            return r
        return json.loads(r.stdout)

    def test_before_installing(self):
        values = self.settings_of("", "SETUP_MODE", "ROOT_URLCONF", "ALLOWED_HOSTS", "DATABASES", "AUTO_UPGRADE", "SESSION_ENGINE")
        self.assertEqual((values["SETUP_MODE"], values["ROOT_URLCONF"], values["ALLOWED_HOSTS"]), ("True", "apps.installer.setup_urls", "['*']"))
        self.assertIn("'ENGINE': 'django.db.backends.dummy'", values["DATABASES"])  # nothing to connect to yet
        self.assertEqual(values["AUTO_UPGRADE"], "False")
        self.assertEqual(values["SESSION_ENGINE"], "django.contrib.sessions.backends.signed_cookies")

    def test_an_installed_site(self):
        text = (
            '[server]\nproxies = 1\n'
            '[database]\nengine = "mysql"\nhost = "db.internal"\nport = 3307\nname = "files"\nuser = "app"\npassword = "pw"\n'
            f'[site]\nsecret_key = "{KEY}"\nurl = "https://files.example.com"\nadmin_path = "/my-admin/"\n'
            '[paths]\nuploads = "files"\n'
            '[upgrade]\nauto = true\n'
        )
        names = ["SETUP_MODE", "ALLOWED_HOSTS", "ADMIN_PATH", "SITE_URL", "CSRF_TRUSTED_ORIGINS", "SESSION_COOKIE_SECURE",
                 "CSRF_COOKIE_SECURE", "SECURE_PROXY_SSL_HEADER", "MEDIA_ROOT", "BACKUP_DIR", "AUTO_UPGRADE", "DEBUG",
                 "TIME_ZONE", "LANGUAGE_CODE", "MAX_REQUEST_BYTES"]
        values = self.settings_of(text, *names)
        self.assertEqual(values, {
            "SETUP_MODE": "False",
            "ALLOWED_HOSTS": "['127.0.0.1', 'localhost', 'files.example.com']",
            "ADMIN_PATH": "my-admin",
            "SITE_URL": "https://files.example.com",
            "CSRF_TRUSTED_ORIGINS": "['https://files.example.com']",
            "SESSION_COOKIE_SECURE": "True",
            "CSRF_COOKIE_SECURE": "True",
            "SECURE_PROXY_SSL_HEADER": "('HTTP_X_FORWARDED_PROTO', 'https')",
            "MEDIA_ROOT": str(self.dir / "files"),
            "BACKUP_DIR": str(self.dir / "backups"),
            "AUTO_UPGRADE": "True",
            "DEBUG": "False",
            "TIME_ZONE": "Asia/Shanghai",
            "LANGUAGE_CODE": "zh-hans",
            "MAX_REQUEST_BYTES": str(64 * 1024 * 1024),
        })
        db = self.settings_of(text, "DATABASES")["DATABASES"]
        for part in ("'ENGINE': 'django.db.backends.mysql'", "'HOST': 'db.internal'", "'PORT': '3307'", "'NAME': 'files'",
                     "'USER': 'app'", "'PASSWORD': 'pw'", "'NAME': 'test_files'", "'COLLATION': 'utf8mb4_0900_ai_ci'"):
            self.assertIn(part, db)
        rest = self.settings_of(text, "REST_FRAMEWORK")["REST_FRAMEWORK"]
        self.assertIn("'NUM_PROXIES': 1", rest)

    def test_sqlite(self):
        script = (
            "from django.conf import settings as s\nfrom django.db import connection\nimport json\n"
            "d = s.DATABASES['default']\n"
            "print(json.dumps([d['ENGINE'], d['NAME'], d['OPTIONS'], connection.vendor]))"
        )
        self.write(f'[database]\nengine = "sqlite"\nfile = "data/site.sqlite3"\n[site]\nsecret_key = "{KEY}"\n')
        r = run("-c", script, HFILE_CONFIG=self.path, DJANGO_SETTINGS_MODULE="config.settings")
        self.assertEqual(r.returncode, 0, r.stderr)
        engine, name, options, vendor = json.loads(r.stdout)
        self.assertEqual((engine, name, vendor), ("config.backends.sqlite3", str(self.dir / "data" / "site.sqlite3"), "sqlite"))
        self.assertEqual(options["transaction_mode"], "IMMEDIATE")  # writes wait their turn instead of failing
        self.assertIn("journal_mode = WAL", options["init_command"])

    def test_over_http_without_a_proxy(self):
        # Without an https url, logging in has to work over http: the cookies aren't https-only.
        values = self.settings_of(
            f'[site]\nsecret_key = "{KEY}"\nurl = "http://192.168.1.5:8080"\n',
            "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_PROXY_SSL_HEADER", "ALLOWED_HOSTS", "ADMIN_PATH",
        )
        self.assertEqual(
            (values["SESSION_COOKIE_SECURE"], values["CSRF_COOKIE_SECURE"], values["SECURE_PROXY_SSL_HEADER"]), ("False", "False", "None")
        )
        self.assertEqual(values["ALLOWED_HOSTS"], "['127.0.0.1', 'localhost', '192.168.1.5']")
        self.assertEqual(values["ADMIN_PATH"], admin_path.path_from_key(KEY))  # none set: from the key

    def test_a_mistake_stops_django_too(self):
        r = self.settings_of('[site]\nadmin_path = "static"\n', "ADMIN_PATH")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ImproperlyConfigured", r.stderr)
        self.assertIn("[site] admin_path：/static/ 是网站自己要用的地址，请换一个。", r.stderr)


def not_a_codec(length=0):
    """A new name, which no codec has."""
    return "zz" + secrets.token_hex(8) + "x" * length


class CodecCacheTests(TempDirMixin, SimpleTestCase):
    """Python keeps the codecs it finds, and none of the names it doesn't (codec_cache.py): clients choose those
    (the headers that name one: apps/core/tests.py UnknownCodecNameTests)."""

    def test_on_wherever_the_settings_are_loaded(self):
        # In the tests' process, and in a command's: serve, runserver and the single executable start the same
        # way (scripts/build.sh checks the program). If this fails on a new Python, see what its encodings keeps.
        self.assertIs(type(encodings._cache), codec_cache.FoundOnly)
        self.write(f'[database]\nengine = "sqlite"\n[site]\nsecret_key = "{KEY}"\n')
        script = (
            "import codecs, encodings\n"
            "try:\n    codecs.lookup('hfile-no-such-codec')\nexcept LookupError:\n    pass\n"
            "print(type(encodings._cache).__name__, 'hfile_no_such_codec' in encodings._cache)"
        )
        r = run("manage.py", "--config", str(self.path), "shell", "-c", script)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip().splitlines()[-1], "FoundOnly False")

    def test_names_not_found_are_not_kept(self):
        before = set(encodings._cache)
        for name in (not_a_codec(), "ZZ-" + not_a_codec(), not_a_codec(9000)):
            for _ in range(2):  # the same answer again, not one kept from the first time
                with self.assertRaises(LookupError):
                    codecs.lookup(name)
                with self.assertRaises(LookupError):
                    b"x".decode(name)
        self.assertEqual(set(encodings._cache) - before, set())
        # Nor is the name itself: the registry interns it, and an interned string goes with its last reference.
        name = not_a_codec()
        with self.assertRaises(LookupError):
            codecs.lookup(name)
        twin = "".join(["", name])  # equal, but a string of its own
        self.assertIsNot(twin, name)
        self.assertIs(sys.intern(twin), twin)

    def test_no_memory_kept_for_them(self):
        def kept():
            """Bytes still allocated after looking up 50 new names of 2 KB, and how long the names were."""
            names = [not_a_codec(2000) for _ in range(50)]
            tracing = tracemalloc.is_tracing()
            if not tracing:
                tracemalloc.start()
            try:
                start = tracemalloc.get_traced_memory()[0]
                for name in names:
                    try:
                        codecs.lookup(name)
                    except LookupError:
                        pass
                return tracemalloc.get_traced_memory()[0] - start, sum(map(len, names))
            finally:
                if not tracing:
                    tracemalloc.stop()

        kept()  # what a first lookup sets up once
        with mock.patch.object(encodings, "_cache", dict(encodings._cache)):  # Python's own: a plain dict
            remembered, size = kept()
        self.assertGreater(remembered, size)  # every name, for good
        forgotten, size = kept()
        self.assertLess(forgotten, size // 10)

    def test_codecs_found_are_kept(self):
        for name, key, codec in (
            ("utf-8", "utf_8", "utf-8"), ("GBK", "gbk", "gbk"), ("latin-1", "latin_1", "iso8859-1"), ("cp932", "cp932", "cp932"),
            ("UTF-16", "utf_16", "utf-16"), ("MS.Kanji", "ms.kanji", "cp932"),  # a spelling nothing else asks for: found now
        ):
            with self.subTest(name=name):
                self.assertEqual(codecs.lookup(name).name, codec)
                self.assertEqual(encodings._cache[key].name, codec)
        self.assertEqual("猫".encode("GBK").decode("gbk"), "猫")
        self.assertEqual("猫".encode("cp932").decode("MS.Kanji"), "猫")

    def test_once_and_only_on_the_dict_it_expects(self):
        found = codecs.lookup("utf-8")
        module = types.SimpleNamespace(_cache={"utf_8": found, "zz_not_a_codec": None})
        self.assertTrue(codec_cache.keep_found_only(module))
        cache = module._cache
        self.assertEqual((type(cache), cache), (codec_cache.FoundOnly, {"utf_8": found}))  # names not found so far go
        latin = codecs.lookup("latin-1")
        cache["zz_another"], cache["latin_1"] = None, latin
        self.assertEqual(cache, {"utf_8": found, "latin_1": latin})
        self.assertTrue(codec_cache.keep_found_only(module))
        self.assertIs(module._cache, cache)  # again: nothing changes
        # Another Python's encodings, without that dict: nothing is done (and the tests above fail).
        for attributes in ({}, {"_cache": None}, {"_cache": collections.OrderedDict()}):
            module = types.SimpleNamespace(**attributes)
            self.assertFalse(codec_cache.keep_found_only(module))
            self.assertEqual(vars(module), attributes)


class AdminPathTests(SimpleTestCase):
    def test_paths(self):
        self.assertRegex(admin_path.random_path(), rf"^[{admin_path.ALPHABET}]{{12}}$")
        self.assertEqual(admin_path.path_from_key(KEY), admin_path.path_from_key(KEY))  # the same on every start
        self.assertNotEqual(admin_path.path_from_key(KEY), admin_path.path_from_key("x" * 50))
        self.assertEqual(admin_path.clean(" /my-admin/ "), "my-admin")

    def test_problems(self):
        for path, problem in (
            ("", "不能为空"),
            ("a" * 65, "最多 64 个字符"),
            ("-x", "只能用英文字母"),
            ("a/b", "只能用英文字母"),
            ("admin", "/admin/ 永远是 404"),
            ("API", "是网站自己要用的地址"),
            ("manage", "是网站自己要用的地址"),
            ("media", "是网站自己要用的地址"),
        ):
            with self.subTest(path=path):
                self.assertIn(problem, admin_path.problem(path))
        self.assertEqual(admin_path.problem("k3n9x2-admin_area"), "")
        self.assertTrue(admin_path.guessable("Dashboard"))


class SupervisorTests(TempDirMixin, SimpleTestCase):
    def test_started_again_after_installing(self):
        """The outer process starts the server again when it exits with 3: how the installer's settings are read."""
        runs = self.dir / "runs"
        script = (
            "import os, pathlib, sys\n"
            f"runs = pathlib.Path({str(runs)!r})\n"
            "n = len(runs.read_text()) if runs.exists() else 0\n"
            "runs.write_text('x' * (n + 1))\n"
            f"assert os.environ[{cli.CHILD!r}] == '1'\n"
            f"sys.exit({cli.RESTART} if n == 0 else 7)\n"
        )
        with mock.patch.object(cli, "say") as say:
            self.assertEqual(cli.supervise([sys.executable, "-c", script]), 7)
        self.assertEqual(runs.read_text(), "xx")  # 3 the first time: started again
        say.assert_called_once_with("\n正在用新的设置重新启动……\n")

    @unittest.skipIf(os.name == "nt", "Windows has no SIGKILL: a process killed there ends with the code it was given")
    def test_killed_by_a_signal_as_a_shell_says_it(self):
        self.assertEqual(cli.supervise([sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]), 137)

    @unittest.skipIf(os.name == "nt", "Popen.wait() takes no such lock on Windows")
    def test_waits_without_the_lock_of_popen_wait(self):
        """Popen.wait() takes a lock and lets go of it in a try: a KeyboardInterrupt in between (in the single
        executable a Ctrl+C comes twice, the second passed on by the bootloader) left it held, and the outer
        process then waited for it forever. Its waits for the server don't take that lock."""
        child = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(0.5); sys.exit(7)"])
        self.addCleanup(child.kill)
        child._waitpid_lock.acquire()  # as that KeyboardInterrupt left it
        self.addCleanup(child._waitpid_lock.release)
        with self.assertRaises(subprocess.TimeoutExpired):
            cli._wait(child, 0.1)
        waited = []
        waiting = threading.Thread(target=lambda: waited.append(cli._wait(child)), daemon=True)  # not to hang the tests
        waiting.start()
        waiting.join(10)
        self.assertEqual(waited, [7])

    @unittest.skipIf(os.name == "nt", "Windows has no SIGINT for one process alone")
    def test_a_ctrl_c_that_comes_twice_is_one(self):
        """In the single executable a Ctrl+C reaches the outer process twice: from the terminal, and passed on
        by PyInstaller's bootloader. The second SIGINT isn't another Ctrl+C: the server, which got the first
        one too and is stopping, isn't sent SIGTERM as well."""
        got, ready = self.dir / "got", self.dir / "ready"
        server = (
            "import pathlib, signal, sys, time\n"
            "def stopping(signum, frame):\n"
            f"    with open({str(got)!r}, 'a') as file:\n"
            "        file.write(signal.Signals(signum).name + ' ')\n"
            "    if signum == signal.SIGINT:\n"
            "        time.sleep(0.5)  # the requests in hand\n"
            "        sys.exit(0)\n"
            "signal.signal(signal.SIGINT, stopping)\n"
            "signal.signal(signal.SIGTERM, stopping)\n"
            f"pathlib.Path({str(ready)!r}).touch()\n"
            "time.sleep(30)\n"
        )
        outer = subprocess.Popen(
            [sys.executable, "-c", f"import sys\nsys.path.insert(0, {str(ROOT)!r})\nfrom config import cli\n"
                                   f"sys.exit(cli.supervise([sys.executable, '-c', {server!r}]))"],
            env=child_env(), start_new_session=True, preexec_fn=lambda: signal.signal(signal.SIGINT, signal.SIG_DFL),
        )
        def kill():
            with contextlib.suppress(ProcessLookupError):  # ended already, as it should have
                os.killpg(outer.pid, signal.SIGKILL)

        self.addCleanup(outer.wait)
        self.addCleanup(kill)
        for _ in range(100):
            if ready.exists():
                break
            time.sleep(0.1)
        os.killpg(outer.pid, signal.SIGINT)  # Ctrl+C in the terminal: the outer process and the server
        time.sleep(0.05)
        os.kill(outer.pid, signal.SIGINT)  # passed on by the bootloader
        self.assertEqual(outer.wait(timeout=30), 0)
        self.assertEqual(got.read_text().split(), ["SIGINT"])

    @unittest.skipUnless(sys.platform.startswith("linux"), "PR_SET_PDEATHSIG is Linux's")
    def test_the_single_executable_ends_with_its_bootloader(self):
        """kill -9 of the process that was started (PyInstaller's bootloader) stops the outer process too,
        with SIGTERM, which it passes on to the server. Here a Python parent plays the bootloader: the
        same executable as its child's sys.executable, as the bootloader is the program's."""
        stopped, ready = self.dir / "stopped", self.dir / "ready"
        child = (
            "import os, pathlib, signal, sys, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from config import cli, configfile\n"
            "configfile.FROZEN = True\n"
            f"signal.signal(signal.SIGTERM, lambda *_: (pathlib.Path({str(stopped)!r}).write_text('SIGTERM'), os._exit(0)))\n"
            f"pathlib.Path({str(ready)!r}).write_text(str(cli._end_with_bootloader()))\n"
            "time.sleep(30)\n"
        )
        bootloader = subprocess.Popen(
            [sys.executable, "-c", f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)"],
            env=child_env(),
        )
        self.addCleanup(bootloader.wait)
        self.addCleanup(bootloader.kill)
        for _ in range(100):
            if ready.exists() and ready.read_text():
                break
            time.sleep(0.1)
        self.assertEqual(ready.read_text(), "True")
        bootloader.kill()
        for _ in range(50):
            if stopped.exists():
                break
            time.sleep(0.1)
        self.assertEqual(stopped.read_text(), "SIGTERM")

    @unittest.skipUnless(os.name == "nt", "Windows' way")
    def test_the_single_executable_ends_with_its_bootloader_on_windows(self):
        """Task Manager or taskkill /F of the process that was started (PyInstaller's bootloader) ends the outer
        process too, and so the server (the job). A Python parent plays the bootloader, as above; one that
        runs another program (cmd, as a shell or a service manager would) isn't watched."""
        ready = self.dir / "ready"
        child = (
            "import pathlib, sys, threading, time\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "from config import cli, configfile\n"
            "configfile.FROZEN = True\n"
            "bound = cli._end_with_bootloader()\n"
            "watched = any(thread.name == 'end-with-parent' for thread in threading.enumerate())\n"
            f"pathlib.Path({str(ready)!r}).write_text(f'{{bound}} {{watched}}')\n"
            "time.sleep(30)\n"
        )

        def started(parent):
            # The child writes nothing, but holds the pipe it inherited: it reaches its end once both have ended.
            process = subprocess.Popen(parent, env=child_env(), stdout=subprocess.PIPE)
            self.addCleanup(process.wait)
            self.addCleanup(process.kill)
            for _ in range(100):
                if ready.exists() and ready.read_text():
                    break
                time.sleep(0.1)
            return process

        bootloader = started([sys.executable, "-c", f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, '-c', {child!r}])\ntime.sleep(60)"])
        self.assertEqual(ready.read_text(), "True True")
        bootloader.kill()
        bootloader.communicate(timeout=10)  # TimeoutExpired: the child went on
        ready.unlink()
        script = self.dir / "child.py"
        script.write_text(child, encoding="utf-8")
        shell = started(f'cmd /d /s /c ""{sys.executable}" "{script}""')  # /s: cmd takes off the outer quotes only
        self.assertEqual(ready.read_text(), "True False")
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(shell.pid)], capture_output=True)  # with the child, unbound
        shell.communicate(timeout=10)

    def test_from_the_source_code_it_isnt_bound_to_its_parent(self):
        # A shell or systemd started it, not a bootloader: `nohup python hfile.py &` must outlive the shell.
        real = os.path.realpath
        shell = mock.patch("os.path.realpath", lambda path: "/usr/bin/bash" if str(path).startswith("/proc/") else real(path))
        with mock.patch.object(cli, "_on_parents_death") as bind, shell:
            self.assertTrue(cli._end_with_bootloader())  # not FROZEN
            with mock.patch.object(configfile, "FROZEN", True):
                self.assertTrue(cli._end_with_bootloader())  # FROZEN, but the parent isn't the program
        bind.assert_not_called()
