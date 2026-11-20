"""`hfile.py` (serve) in new processes: waitress on [server] host and port, two processes, the installer
and the restart after it, the signals, a busy port. Each server listens on 127.0.0.1, on a port the
system chose a moment before."""

import contextlib
import hashlib
import http.client
import http.cookiejar
import json
import os
import re
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

from django.test import SimpleTestCase

from apps.core.testing import assert_private, sqlite_file

from . import configfile
from .tests import KEY, ROOT, TempDirMixin, child_env, run
from .version import VERSION

PASSWORD = "a-Strong-pass-42"
# What stops the server as SIGTERM does (systemd, docker stop). Windows has none to send: there Ctrl+Break,
# which the outer process and the server both get (start() gives them a process group of their own).
STOP = signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url, data=None, headers=None, method=None, timeout=30):
    """(status, headers, body) without following redirects."""

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None

    request = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def children(pid):
    """The process ids `pid` started (Linux)."""
    try:
        return [int(child) for child in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]
    except FileNotFoundError:
        return []


def listening(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except OSError:
        return False
    return True


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    stat_line = Path(f"/proc/{pid}/stat").read_text() if Path(f"/proc/{pid}/stat").exists() else ""
    return " Z " not in stat_line  # a zombie has finished


class PortTaken(AssertionError):
    """The server stopped at once: another process (a test running in parallel) took its port between
    free_port() and the server listening on it. The port may stay taken for a minute (TIME_WAIT)."""


class ServerMixin(TempDirMixin):
    def start(self, text="", wait_path="/install/", tries=5):
        """`hfile.py --config` in a process group of its own; the cleanup ends the whole group, the
        server itself too, which would otherwise keep running after its parent is killed. On Windows it
        kills the outer process, and its job object (config/windows.py) the server. When its port was
        taken before it listened on it, it starts again on another one."""
        self.port = free_port()
        self.origin = f"http://127.0.0.1:{self.port}"
        self.write(f'[server]\nhost = "127.0.0.1"\nport = {self.port}\n' + text)
        self.process = subprocess.Popen(
            [sys.executable, "-B", "hfile.py", "--config", str(self.path)],
            cwd=ROOT, env=child_env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            **({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}),
        )

        def stop():
            if os.name == "nt":
                self.process.kill()
            else:
                with contextlib.suppress(ProcessLookupError):  # already stopped, as the test does it
                    os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()

        self.addCleanup(stop)
        if wait_path:
            try:
                self.wait_for(wait_path)
            except PortTaken:
                if tries <= 1:
                    raise
                return self.start(text, wait_path, tries - 1)
        return self.process

    def wait_for(self, path, status=200, seconds=60):
        deadline = time.monotonic() + seconds
        while True:
            try:
                answer = fetch(self.origin + path, timeout=10)
                if answer[0] == status:
                    return answer
            except OSError:  # starting (again)
                pass
            if self.process.poll() is not None:
                try:
                    output = self.process.communicate(timeout=10)[0]
                except subprocess.TimeoutExpired:  # something it started still holds its output
                    output = ""
                if f"端口 {self.port} 已经被别的程序占用了" in output:
                    raise PortTaken(output)
                self.fail(f"the server stopped ({self.process.returncode}): {output}")
            self.assertLess(time.monotonic(), deadline, f"{path} didn't answer {status}")
            time.sleep(0.3)

    def stop_with(self, signum, seconds=60):
        self.process.send_signal(signum)
        output, _ = self.process.communicate(timeout=seconds)
        return output

    def installed(self, text=""):
        """An installed site on SQLite, made by the commands (no installer): its tables and an admin."""
        self.write(f'[site]\nsecret_key = "{KEY}"\n' + text)
        for args in (["upgrade"], ["createsuperuser", "--noinput"]):
            r = run("manage.py", "--config", self.path, *args,
                    DJANGO_SUPERUSER_USERNAME="boss", DJANGO_SUPERUSER_PASSWORD=PASSWORD)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return f'[site]\nsecret_key = "{KEY}"\n' + text


class BeforeInstallingTests(ServerMixin, SimpleTestCase):
    def test_the_installer_is_the_whole_site(self):
        self.start()
        status, headers, page = fetch(f"{self.origin}/install/")
        self.assertEqual(status, 200)
        self.assertIn("安装 HFile", page.decode())
        status, headers, _ = fetch(f"{self.origin}/static/installer/install.css")  # WhiteNoise, with DEBUG off
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/css"))
        for path in ("/", "/manage/", "/admin/", "/media/site/x.png"):
            status, headers, _ = fetch(self.origin + path)
            self.assertEqual((status, headers["Location"]), (302, "/install/"), path)
        status, headers, body = fetch(f"{self.origin}/api/v1/site/")
        self.assertEqual((status, json.loads(body)["detail"]), (503, "网站还没有安装：先打开 /install/ 按安装向导填写。"))
        code = (self.dir / "install-token.txt").read_text().strip()  # next to config.toml
        assert_private(self, self.dir / "install-token.txt")
        fetch(f"{self.origin}/install/?token={code}")

        output = self.stop_with(STOP)  # to the outer process, which passes it on (systemd, docker stop)
        self.assertEqual(self.process.returncode, 0, output)
        self.assertIn(f"HFile {VERSION} 在 127.0.0.1:{self.port} 上提供网页。按 Ctrl+C 停止。", output)
        self.assertIn(f"打开 {self.origin}/install/?token={code} 按安装向导填写", output)
        self.assertIn(f"安装口令：{code}", output)
        self.assertIn("HFile 已经停止。", output)
        # The access log: a line per request, static files too, the token masked.
        self.assertRegex(output, r'INFO access: 200 \| +[\d.]+m?s \| +127\.0\.0\.1 \| GET +"/static/installer/install\.css\?v=|GET +"/static/installer/install\.css"')
        self.assertIn('GET     "/install/?token=<token>"', output)
        self.assertNotIn(f"?token={code}\"", output)
        with self.assertRaises(OSError):  # nothing listens any more
            socket.create_connection(("127.0.0.1", self.port), timeout=2).close()


class InstallTests(ServerMixin, SimpleTestCase):
    def test_installing_on_sqlite_without_javascript(self):
        """The whole form at once, as a browser without JavaScript sends it; then the restart, the admin
        logging in, /admin/ a 404 and the Django admin at its random path."""
        self.start()
        browser = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        page = fetch(f"{self.origin}/install/")[2].decode()
        self.assertIn('value="hfile.sqlite3"', page)  # SQLite, next to config.toml, unless changed
        form = {
            "token": (self.dir / "install-token.txt").read_text().strip(),
            "db_engine": "sqlite", "db_file": "data/site.sqlite3",
            "db_host": "", "db_port": "", "db_name": "", "db_user": "", "db_password": "",  # MySQL's, hidden: not looked at
            "site_name": "小站", "url": "", "hosts": "127.0.0.1", "proxies": "0", "admin_path": "",
            "username": "boss", "password1": PASSWORD, "password2": PASSWORD,
        }
        with browser.open(f"{self.origin}/install/", data=urllib.parse.urlencode(form).encode(), timeout=60) as response:
            done = response.read().decode()
        self.assertIn("程序正在重新启动", done, done[-2000:])
        admin = re.search(r"/([a-z0-9]{12})/</code>", done).group(1)
        self.wait_for("/api/v1/site/")  # started again, installed
        self.assertEqual(json.loads(fetch(f"{self.origin}/api/v1/site/")[2])["site_name"], "小站")

        values = configfile.load(self.path)
        self.assertEqual((values["database"]["engine"], values["database"]["file"]), ("sqlite", "data/site.sqlite3"))
        self.assertEqual((values["site"]["admin_path"], values["site"]["allowed_hosts"]), (admin, ["127.0.0.1", "localhost"]))
        self.assertGreaterEqual(len(values["site"]["secret_key"]), 50)
        self.assertEqual(values["server"]["port"], self.port)  # the rest as it was
        assert_private(self, self.path)
        self.assertFalse((self.dir / "install-token.txt").exists())
        database = self.dir / "data" / "site.sqlite3"
        assert_private(self, database)  # password hashes
        with sqlite_file(database) as db:
            self.assertEqual(db.execute("SELECT username, is_superuser, is_staff FROM accounts_user WHERE kind = 'normal'").fetchall(), [("boss", 1, 1)])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM upgrades_upgradelog").fetchone(), (1,))

        # The installer is gone, /admin/ is a 404, the Django admin is at its path.
        self.assertEqual(fetch(f"{self.origin}/install/")[0], 404)
        self.assertEqual(fetch(f"{self.origin}/admin/")[0], 404)
        status, headers, _ = fetch(f"{self.origin}/{admin}/")
        self.assertEqual((status, headers["Location"]), (302, f"/{admin}/login/?next=/{admin}/"))
        # The admin logs in through the API.
        cookies = {}
        status, headers, body = fetch(f"{self.origin}/api/v1/session/")
        for header in headers.get_all("Set-Cookie") or []:
            cookies.update((key, morsel.value) for key, morsel in SimpleCookie(header).items())
        token = json.loads(body)["csrf_token"]
        login = json.dumps({"username": "BOSS", "password": PASSWORD}).encode()  # any case
        status, headers, body = fetch(f"{self.origin}/api/v1/session/", data=login, method="POST", headers={
            "Content-Type": "application/json", "X-CSRFToken": token,
            "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items())})
        self.assertEqual(status, 201, body)
        self.assertEqual(json.loads(body)["user"]["username"], "boss")

        output = self.stop_with(STOP)
        self.assertEqual(self.process.returncode, 0, output)
        self.assertIn("正在用新的设置重新启动", output)
        self.assertIn(f"数据库：SQLite，文件 {database}\n上传的文件：{self.dir / 'media'}\n", output)
        self.assertIn(f"后台（Django 管理后台）：{self.origin}/{admin}/", output)  # printed at the start, for the operator
        # In the access log the admin's path is masked, the next= in the query too.
        self.assertIn('GET     "/<admin>/" ', output)
        self.assertNotIn(f'"/{admin}/', output)

    def test_installing_behind_an_https_proxy(self):
        """As behind Caddy or nginx ending https: the proxy passes Host on and adds X-Forwarded-For and
        -Proto, the browser's Origin is https://…, and the program gets plain http. The installer works
        there (no CSRF check, the token instead), writes the address and the proxy into config.toml, and
        then the site's own forms work too."""
        site = "https://files.example"
        proxy = {"Host": "files.example", "X-Forwarded-For": "203.0.113.7", "X-Forwarded-Proto": "https"}
        self.start()
        cookies = {}

        def call(method, path, form=None, data=None):
            headers = {**proxy, "Cookie": "; ".join(f"{key}={value}" for key, value in cookies.items())}
            body = None
            if form is not None:
                body = urllib.parse.urlencode(form).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            elif data is not None:
                body = json.dumps(data).encode()
                headers.update({"Content-Type": "application/json", "X-CSRFToken": cookies.get("hfile_csrftoken", "")})
            if method != "GET":
                headers["Origin"] = site  # as the browser sends it
            status, answer, text = fetch(f"{self.origin}{path}", data=body, headers=headers, method=method, timeout=60)
            for header in answer.get_all("Set-Cookie") or []:
                cookies.update((key, morsel.value) for key, morsel in SimpleCookie(header).items())
            return status, text.decode(), answer.get_all("Set-Cookie") or []

        status, page, _ = call("GET", "/install/")
        self.assertIn('<option value="1" selected>', page)  # the proxy's headers: 反向代理 is 1 层
        form = {
            "token": (self.dir / "install-token.txt").read_text().strip(),
            "db_engine": "sqlite", "db_file": "site.sqlite3",
            "site_name": "小站", "url": site, "hosts": "files.example", "proxies": "1", "admin_path": "my-back-office",
            "username": "boss", "password1": PASSWORD, "password2": PASSWORD,
        }
        status, done, _ = call("POST", "/install/", form=form)
        self.assertEqual(status, 200, done[-2000:])
        self.assertIn("程序正在重新启动", done)
        self.assertIn(f"{site}/my-back-office/", done)  # the admin, at the site's address
        self.wait_for("/api/v1/site/")  # started again, installed (127.0.0.1 is allowed too)
        values = configfile.load(self.path)
        self.assertEqual((values["site"]["url"], values["server"]["proxies"]), (site, 1))

        # The site's own forms from https://files.example: logging in.
        _, _, set_cookies = call("GET", "/api/v1/session/")
        self.assertTrue(set_cookies and all("; Secure" in header for header in set_cookies), set_cookies)  # https only
        status, body, _ = call("POST", "/api/v1/session/", data={"username": "boss", "password": PASSWORD})
        self.assertEqual(status, 201, body)
        with sqlite_file(self.dir / "site.sqlite3") as db:  # proxies = 1: the visitor's address, not the proxy's
            self.assertEqual(db.execute("SELECT last_login_ip FROM accounts_user WHERE kind = 'normal'").fetchall(), [("203.0.113.7",)])
        output = self.stop_with(STOP)
        self.assertEqual(self.process.returncode, 0, output)
        self.assertNotIn("Forbidden", output)
        self.assertRegex(output, r"\| +203\.0\.113\.7 \| POST")  # the access log's IP, by the same rule


class PortTests(ServerMixin, SimpleTestCase):
    def test_a_port_in_use_stops_it_before_the_database_is_touched(self):
        self.write(f'[site]\nsecret_key = "{KEY}"\n[upgrade]\nauto = true\n')
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen()
            port = taken.getsockname()[1]
            self.write(f'[server]\nhost = "127.0.0.1"\nport = {port}\n[site]\nsecret_key = "{KEY}"\n[upgrade]\nauto = true\n')
            r = run("hfile.py", "--config", self.path, timeout=60)
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"端口 {port} 已经被别的程序占用了", r.stderr)
        self.assertNotIn("Traceback", r.stderr)
        self.assertFalse((self.dir / "hfile.sqlite3").exists())  # not opened, let alone upgraded
        self.assertFalse((self.dir / "backups").exists())


class SignalTests(ServerMixin, SimpleTestCase):
    @unittest.skipIf(os.name == "nt", "Windows sends Ctrl+C to a whole console, never to one process")
    def test_sigint_to_the_outer_process_alone_is_passed_on(self):
        self.start()
        [child] = children(self.process.pid)
        started = time.monotonic()
        output = self.stop_with(signal.SIGINT)  # kill -INT of the outer process: the server doesn't get it itself
        self.assertGreaterEqual(time.monotonic() - started, 2.5)  # it waited to see whether the server stops by itself
        self.assertIn("HFile 已经停止。", output)
        self.assertFalse(alive(child))

    def test_the_server_stops_when_the_outer_process_is_killed(self):
        self.start()
        if os.name == "nt":  # TerminateProcess: the job object (config/windows.py) ends the server too
            self.process.kill()
            self.process.wait()
            deadline = time.monotonic() + 30
            while listening(self.port):
                self.assertLess(time.monotonic(), deadline, "the server kept running")
                time.sleep(0.2)
            return
        [child] = children(self.process.pid)
        self.process.send_signal(signal.SIGKILL)  # PR_SET_PDEATHSIG: the server gets SIGTERM
        self.process.wait()
        deadline = time.monotonic() + 30
        while alive(child):
            self.assertLess(time.monotonic(), deadline, "the server kept running")
            time.sleep(0.2)
        self.assertFalse(listening(self.port))  # and lets go of the port

    def slow_logins(self, seconds):
        """A login as boss with a wrong password takes about `seconds` from now on: a password hash that
        takes that long to check (timed on this machine)."""
        started = time.perf_counter()
        hashlib.pbkdf2_hmac("sha256", b"password", b"salt", 200_000)
        iterations = int(200_000 * seconds / (time.perf_counter() - started))
        db = sqlite3.connect(self.dir / "hfile.sqlite3")
        with db:
            db.execute("UPDATE accounts_user SET password = ? WHERE username = 'boss'", [f"pbkdf2_sha256${iterations}$salt$hash"])
        db.close()

    def test_sigterm_lets_the_requests_in_hand_finish(self):
        """Logins with a wrong password take a while (the password hash, about 2 seconds here): SIGTERM comes
        once the server has them all, while they are being handled, and each still gets its whole answer
        before the server stops. Not at a fixed moment after they were started: on GitHub's busy Windows
        runner the server hadn't read two of the six yet 0.15 s later, and a request that comes after SIGTERM
        gets no answer, as it should."""
        self.start(self.installed(), wait_path="/api/v1/site/")
        self.slow_logins(2)
        _, headers, body = fetch(f"{self.origin}/api/v1/session/")
        cookie = "; ".join(f"{k}={m.value}" for h in headers.get_all("Set-Cookie") for k, m in SimpleCookie(h).items())
        token = json.loads(body)["csrf_token"]
        answers = []
        sent = threading.Semaphore(0)

        def login(n):
            data = json.dumps({"username": "boss", "password": f"wrong-{n}"}).encode()
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
            try:
                connection.request("POST", "/api/v1/session/", data, {
                    "Content-Type": "application/json", "X-CSRFToken": token, "Cookie": cookie})
                sent.release()
                response = connection.getresponse()
                answers.append((response.status, response.headers, response.read()))
            finally:
                connection.close()

        threads = [threading.Thread(target=login, args=(n,)) for n in range(6)]
        for thread in threads:
            thread.start()
        for _ in threads:
            self.assertTrue(sent.acquire(timeout=60), "a login wasn't sent")
        time.sleep(1)  # read by the server, and being handled
        output = self.stop_with(STOP)
        for thread in threads:
            thread.join()
        self.assertEqual(self.process.returncode, 0, output)
        self.assertEqual([status for status, _, _ in answers], [400] * 6, output)
        self.assertTrue(all(json.loads(body)["non_field_errors"] == ["用户名或密码不对。"] for _, _, body in answers))
        self.assertIn("HFile 已经停止。", output)

    def test_ctrl_c_lets_the_requests_in_hand_finish(self):
        """OPS-01: Ctrl+C in a terminal signals the whole process group, the server too. The outer process
        passed another SIGINT on SIGINT_GRACE seconds later, which cut short the server's waiting for its
        requests: a login still being checked got no answer, and the program ended with 130."""
        self.start(self.installed(), wait_path="/api/v1/site/")
        self.slow_logins(6)  # longer than the grace
        _, headers, body = fetch(f"{self.origin}/api/v1/session/")
        cookie = "; ".join(f"{k}={m.value}" for h in headers.get_all("Set-Cookie") for k, m in SimpleCookie(h).items())
        token = json.loads(body)["csrf_token"]
        answers = []

        def login():
            data = json.dumps({"username": "boss", "password": "wrong"}).encode()
            try:
                answers.append(fetch(f"{self.origin}/api/v1/session/", data=data, method="POST", timeout=60, headers={
                    "Content-Type": "application/json", "X-CSRFToken": token, "Cookie": cookie}))
            except OSError as error:  # cut short: no answer
                answers.append(error)

        thread = threading.Thread(target=login)
        thread.start()
        time.sleep(1)  # sent, and being checked
        if os.name == "nt":  # Ctrl+Break, the Ctrl+C a process group can be sent: both get it
            self.process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            os.killpg(self.process.pid, signal.SIGINT)  # as Ctrl+C in a terminal: the outer process and the server
        output, _ = self.process.communicate(timeout=60)
        thread.join()
        self.assertEqual(self.process.returncode, 0, output)
        [answer] = answers
        self.assertIsInstance(answer, tuple, output)
        self.assertEqual(answer[0], 400, output)
        self.assertIn("HFile 已经停止。", output)


class HeadTests(ServerMixin, SimpleTestCase):
    """HEAD through all of serve (config/wsgi.py, waitress): only the headers go out. A client reads no body
    after HEAD, so the next answer on the connection would start with whatever followed them."""

    def test_the_connection_serves_the_next_request(self):
        self.start(self.installed(), wait_path="/api/v1/site/")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        self.addCleanup(connection.close)
        for path in ("/api/v1/site/", "/no-such-page/"):
            connection.request("HEAD", path)
            head = connection.getresponse()
            head.read()
            sock = connection.sock
            connection.request("GET", path)
            got = connection.getresponse()
            body = got.read()
            self.assertIs(connection.sock, sock, path)  # the same connection, kept open
            self.assertEqual(head.status, got.status, path)
            self.assertEqual(head.getheader("Content-Length"), str(len(body)), path)


class DownloadThreadTests(ServerMixin, SimpleTestCase):
    """Downloads through the site (/pd/) run on threads of their own (apps/files/pool.py), not on [server]
    threads: a download holds its thread for as long as its client takes, and the pages don't wait for it."""

    def storage(self, **fields):
        """A local storage source with a 256 MB file (sparse), and the file's signed link."""
        root = self.dir / "files"
        root.mkdir()
        with open(root / "big.bin", "wb") as f:
            f.truncate(256 * 1024 * 1024)
        code = (
            "from apps.files import signing\n"
            "from apps.storages import config\n"
            "from apps.storages.drivers.local import LocalDriver\n"
            "from apps.storages.models import StorageSource\n"
            "s = StorageSource.objects.create(key='disk', name='Disk', type='local', "
            f"config=config.encode(LocalDriver, {{'root': {str(root)!r}}}), **{fields!r})\n"
            "print(signing.sign(s, '/big.bin', lifetime=3600))\n"
        )
        r = run("manage.py", "--config", self.path, "shell", "-v", "0", "-c", code)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r.stdout.strip().splitlines()[-1]

    def stuck_download(self, link):
        """A download whose client reads the start of the answer and then nothing: the server sends until the
        buffers are full, and the download's thread waits. The answer's status line."""
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.settimeout(30)
        sock.connect(("127.0.0.1", self.port))
        self.addCleanup(sock.close)
        sock.sendall(f"GET {link} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\n\r\n".encode())
        start = b""
        while b"\r\n" not in start:
            piece = sock.recv(64)
            self.assertTrue(piece, "the server closed the connection")
            start += piece
        return start.partition(b"\r\n")[0].decode()

    def test_a_download_doesnt_hold_up_the_pages(self):
        text = self.installed()
        link = self.storage()
        self.start("threads = 1\n" + text, wait_path="/api/v1/site/")
        self.assertEqual(self.stuck_download(link), "HTTP/1.1 200 OK")
        time.sleep(1)  # the buffers fill up, and the download's thread waits for its client
        started = time.monotonic()
        try:
            status, _, _ = fetch(f"{self.origin}/api/v1/site/", timeout=10)
        except OSError as error:
            self.fail(f"a page didn't answer while a download was being sent: {error}")
        self.assertEqual(status, 200)
        self.assertLess(time.monotonic() - started, 5)

    def test_a_storage_at_its_limit_answers_at_once(self):
        """One more download than the storage sends at once gets its 429 from a spare thread of the pool."""
        text = self.installed()
        link = self.storage(proxy_connections=1)
        self.start("threads = 1\n" + text, wait_path="/api/v1/site/")
        self.assertEqual(self.stuck_download(link), "HTTP/1.1 200 OK")
        started = time.monotonic()
        status, headers, _ = fetch(self.origin + link, timeout=10)
        self.assertEqual((status, headers["Retry-After"]), (429, "5"))
        self.assertLess(time.monotonic() - started, 5)
