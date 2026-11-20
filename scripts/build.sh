#!/usr/bin/env bash
# Builds HFile as a single executable with PyInstaller: dist/hfile (hfile.exe on Windows).
#
#   scripts/build.sh [输出文件夹]      默认是项目里的 dist/
#
# Run it in a Python environment with requirements.txt, requirements-dev.txt (PyInstaller), mysqlclient
# and Pillow, e.g. conda's `hfile`, at the versions of requirements.lock (it checks them first). The program is for the system it is built on: built on an older
# Linux it also runs on newer ones, not the other way round (glibc), so build on the oldest Linux you
# want to run it on. On Windows it runs in Git for Windows' bash. Temporary files go to $TMPDIR.
#
# What goes in is the last commit (git archive HEAD), not the working tree: every file in an app's
# folder goes in with its data files, so one left there (an .env, an editor's swap file, a key)
# would too. Changes not committed yet stay out; the script says so.
set -euo pipefail

# The program: hfile, or hfile.exe on Windows.
exe=""
if [ "${OS:-}" = Windows_NT ]; then
  exe=".exe"
fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python}"
if ! commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)"; then
  echo "scripts/build.sh 要在 HFile 的 git 仓库里运行：打包的是最后一次提交里的代码。" >&2
  exit 1
fi
mkdir -p "${1:-$ROOT/dist}"
OUT="$(cd "${1:-$ROOT/dist}" && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
SRC="$WORK/src"
mkdir "$SRC"
git -C "$ROOT" archive --format=tar HEAD | tar -x -C "$SRC"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  echo "注意：工作区里有没提交的改动，它们不会打包进去（打包的是提交 $commit）。" >&2
fi
cd "$SRC"
# PYTHONIOENCODING: what Python prints into a pipe is UTF-8 on Windows too, where it is in the system's code page,
# which may have no Chinese (cp1252).
export PYTHONPATH="$SRC" PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8
# The environment is the one the tests passed in (BUILD-04): every package, direct and indirect, at its version
# in the commit's requirements.lock. scripts/lock.py is a tool like this one, next to it.
"$PYTHON" "$ROOT/scripts/lock.py" --check "$SRC"

# PyInstaller's Django hook loads the settings while it looks for what to put in. It gets a
# config.toml of its own, with a secret key, so that it sees the installed site, not the
# installer. Nothing of it goes into the program.
"$PYTHON" - "$WORK/config.toml" <<'PY'
import secrets, sys
from config import configfile
configfile.create(sys.argv[1])
configfile.update(sys.argv[1], {"site": {"secret_key": secrets.token_urlsafe(50)}})
PY
export HFILE_CONFIG="$WORK/config.toml" DJANGO_SETTINGS_MODULE=config.settings

# Every app with its migrations, templates and static files (settings.HFILE_APPS, plus the parts
# that aren't apps of their own), and the libraries that load modules or files by name. Missing
# migrations would make the program miscount what to upgrade.
# (Printed without a line end, here and below: on Windows that is \r\n, and $(...) keeps the \r.)
apps="$("$PYTHON" -c 'import django; django.setup(); from django.conf import settings; print(" ".join(settings.HFILE_APPS), end="")')"
args=()
for package in $apps apps.installer apps.core config apps.storages; do
  args+=(--collect-submodules "$package" --collect-data "$package")
done
# But not their tests: test modules import libraries only the tests use (moto and boto3, a WebDAV and
# an FTP server, …: about 40 MB). PyInstaller's Django hook collects every submodule of the installed
# apps, tests included, so they are left out by name.
tests="$("$PYTHON" - $apps apps.installer apps.core config apps.storages <<'PY'
import pkgutil, sys
from pathlib import Path


def is_test(name):
    last = name.rsplit(".", 1)[-1]
    return last in ("tests", "test") or last.startswith("test_")


def walk(package, folder):
    for info in pkgutil.iter_modules([str(folder)]):
        name = f"{package}.{info.name}"
        if is_test(name):
            yield name  # a test package goes with everything in it
        elif info.ispkg:
            yield from walk(name, folder / info.name)


found = []
for package in dict.fromkeys(sys.argv[1:]):
    found.extend(walk(package, Path(*package.split("."))))
print(" ".join(found), end="")
PY
)"
for module in $tests; do
  args+=(--exclude-module "$module")
done
for library in rest_framework whitenoise waitress; do
  args+=(--collect-submodules "$library")
done
args+=(--collect-data rest_framework)
# DRF's test client, which only tests use, imports requests, and requests urllib3 and the pyOpenSSL of
# the FTP driver's tests: 60 modules HFile never runs.
args+=(--exclude-module rest_framework.test --exclude-module requests --exclude-module urllib3 --exclude-module OpenSSL)
# And what the tests' Flask and WsgiDAV bring along, and tblib, which optional imports of libraries HFile does
# use would put in (Django's Jinja2 template backend, YAML serializer and test runner, httpx's command line):
# HFile uses none of it.
args+=(--exclude-module jinja2 --exclude-module markupsafe --exclude-module yaml --exclude-module click --exclude-module tblib)
# The font the captcha pictures are drawn with (apps/captchas/images.py) is a data file of the captcha package.
args+=(--collect-data captcha)
# Time zones and CA certificates for systems without them (config/frozen.py). zoneinfo reads
# tzdata's files by importing its packages, so both the packages and the files go in.
args+=(--collect-submodules tzdata --collect-data tzdata --collect-data certifi --hidden-import certifi)
# Pillow from conda-forge links libxcb (for ImageGrab, which the site doesn't use), and PyInstaller
# never puts libxcb in: it expects it next to a desktop. A server without one doesn't have it, and a
# program with Pillow in it doesn't even start there (libxcb.so.1: cannot open shared object file).
# So it goes in, with the two libraries it needs. Pillow from PyPI brings copies of its own. (On Windows it
# links no libxcb.)
if [ "$(uname -s)" = Linux ] && imaging="$("$PYTHON" -c 'import PIL._imaging as m; print(m.__file__)' 2>/dev/null)"; then
  while read -r library; do
    args+=(--add-binary "$library:.")
  done < <(ldd "$imaging" | awk '$1 ~ /^lib(xcb|Xau|Xdmcp)\.so/ && $3 ~ /^\// {print $3}')
fi
# Both databases: PyInstaller's Django hook only takes the one the settings above name.
args+=(--collect-submodules django.db.backends.sqlite3 --collect-submodules django.db.backends.mysql --hidden-import MySQLdb)

"$PYTHON" -m PyInstaller --noconfirm --clean --onefile --name hfile \
  --distpath "$WORK/dist" --workpath "$WORK/build" --specpath "$WORK" --paths "$SRC" \
  "${args[@]}" "$SRC/hfile.py"
# Checked where it was built: the program in $OUT is replaced only once every check below has passed.
BIN="$WORK/dist/hfile$exe"

# The program on its own, as on a server that has nothing of this machine's: the time zones only
# from the tzdata inside it (PYTHONTZPATH names an empty folder), CA certificates from the system
# or from the certifi inside it, and both database drivers. And its Python keeps no names of codecs
# it doesn't know (config/codec_cache.py).
mkdir "$WORK/no-zoneinfo"
PYTHONTZPATH="$WORK/no-zoneinfo" "$BIN" --config "$WORK/config.toml" shell -c "
import os, ssl, sqlite3, zoneinfo, certifi, MySQLdb
zoneinfo.ZoneInfo('Asia/Shanghai')
assert os.path.isfile(certifi.where()), certifi.where()
assert ssl.create_default_context().cert_store_stats()['x509_ca'], 'no CA certificates'
from django.db.backends.mysql import base
import importlib.util
for name in ('moto', 'boto3', 'botocore', 'wsgidav', 'pyftpdlib', 'flask', 'responses', 'rest_framework.test',
             'requests', 'urllib3', 'OpenSSL', 'jinja2', 'markupsafe', 'yaml', 'click', 'tblib', 'playwright', 'greenlet'):
    assert importlib.util.find_spec(name) is None, name + ' (only the tests use it) went into the program'
import codecs, encodings
try:
    codecs.lookup('hfile-no-such-codec')
except LookupError:
    pass
assert 'hfile_no_such_codec' not in encodings._cache, 'the names of unknown codecs are kept (config/codec_cache.py)'
"
# Every migration went in: on an empty database the program lists the ones the source code has. One
# missing would make it miscount what to upgrade (a database "newer" than the program, or tables
# never made).
rm -f "$WORK"/hfile.sqlite3*
expected="$( { "$PYTHON" manage.py --config "$WORK/config.toml" upgrade --check || true; } | grep '^  ' || true)"
rm -f "$WORK"/hfile.sqlite3*
found="$( { "$BIN" --config "$WORK/config.toml" upgrade --check || true; } | grep '^  ' || true)"
if [ -z "$expected" ] || [ "$found" != "$expected" ]; then
  echo "打包出的程序列出的迁移和源码的不一样：" >&2
  diff <(echo "$expected") <(echo "$found") >&2 || true
  exit 1
fi
echo "迁移都在：$(echo "$found" | wc -l) 个。"

# The program serving, as on a server (BUILD-03): installed with its own commands (docs/install.md), started,
# and asked for what only a whole program answers: the home page and a style sheet it names, the site's
# settings, logging in, then SIGTERM (Ctrl+Break on Windows, as in config/test_serve.py). A module or a data
# file left out shows here, not on a server.
mkdir "$WORK/smoke"
"$PYTHON" - "$BIN" "$WORK/smoke" <<'PY'
import json, os, re, secrets, signal, socket, subprocess, sys, time, urllib.error, urllib.request
from http.cookies import SimpleCookie
from pathlib import Path

from config import configfile

program, folder = sys.argv[1], Path(sys.argv[2])
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
config = folder / "config.toml"
configfile.create(config)
configfile.update(config, {"server": {"host": "127.0.0.1", "port": port}, "site": {"secret_key": secrets.token_urlsafe(50)}})
password = secrets.token_urlsafe(16)
# As on a server: nothing of this script's environment (PYTHONPATH names the source code).
env = {key: value for key, value in os.environ.items() if key not in ("PYTHONPATH", "HFILE_CONFIG", "DJANGO_SETTINGS_MODULE")}
env.update(DJANGO_SUPERUSER_USERNAME="boss", DJANGO_SUPERUSER_PASSWORD=password)
for args in (["upgrade"], ["createsuperuser", "--noinput"]):
    # What it prints is UTF-8 (config/cli.py), not in the system's code page as text=True alone would read it on Windows.
    done = subprocess.run([program, "--config", str(config), *args], env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=300)
    if done.returncode:
        sys.exit(f"打包出的程序 {' '.join(args)} 失败：\n{done.stdout}{done.stderr}")

origin = f"http://127.0.0.1:{port}"
windows = os.name == "nt"
STOP, stop_name = (signal.CTRL_BREAK_EVENT, "Ctrl+Break") if windows else (signal.SIGTERM, "SIGTERM")


def fetch(path, data=None, headers=None):
    request = urllib.request.Request(origin + path, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


# In a group of its own, which the stop signal goes to (and on Linux SIGKILL, at the end, if it has to).
group = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if windows else {"start_new_session": True}
server = subprocess.Popen([program, "--config", str(config)], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace", **group)


def started():
    deadline = time.monotonic() + 120
    while server.poll() is None and time.monotonic() < deadline:
        try:
            if fetch("/api/v1/site/")[0] == 200:
                return True
        except OSError:  # not listening yet
            pass
        time.sleep(0.5)
    return False


def check():
    """What is wrong with the program serving: [] when nothing. Ends with stopping it."""
    problems = []
    status, _, page = fetch("/")
    page = page.decode("utf-8", "replace")
    if status != 200 or "<html" not in page:
        problems.append(f"首页回答 {status}")
    style = re.search(r'href="(/static/[^"?]+\.css(?:\?[^"]*)?)"', page)  # with its ?v=, as the page names it
    if style is None:
        problems.append("首页里没有样式表")
    else:
        status, headers, _ = fetch(style.group(1))
        if status != 200 or headers.get_content_type() != "text/css":
            problems.append(f"样式表 {style.group(1)} 回答 {status}（{headers.get_content_type()}）")
    status, _, body = fetch("/api/v1/site/")
    if status != 200 or "site_name" not in json.loads(body):
        problems.append(f"/api/v1/site/ 回答 {status}")
    cookies = {}

    def keep(headers):
        cookies.update((key, morsel.value) for header in headers.get_all("Set-Cookie") or [] for key, morsel in SimpleCookie(header).items())
        return "; ".join(f"{key}={value}" for key, value in cookies.items())

    status, headers, body = fetch("/api/v1/session/")
    token, cookie = json.loads(body)["csrf_token"], keep(headers)
    login = json.dumps({"username": "boss", "password": password}).encode()
    status, headers, body = fetch("/api/v1/session/", data=login, headers={
        "Content-Type": "application/json", "X-CSRFToken": token, "Cookie": cookie})
    if status != 201:
        problems.append(f"登录回答 {status}：{body[:200]!r}")
    status, _, body = fetch("/api/v1/session/", headers={"Cookie": keep(headers)})
    if status != 200 or not json.loads(body)["authenticated"]:
        problems.append("登录以后还不是登录状态")
    server.send_signal(STOP)
    if server.wait(timeout=60) != 0:
        problems.append(f"{stop_name} 以后退出码是 {server.returncode}")
    return problems


try:
    problems = check() if started() else ["打包出的程序没有开始提供网页"]
except Exception as error:  # an answer that isn't what it should be: not JSON, say
    problems = [f"{type(error).__name__}: {error}"]
finally:
    if server.poll() is None:
        if windows:  # the program and every process under it
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(server.pid)], capture_output=True)
        else:
            os.killpg(server.pid, signal.SIGKILL)
    output = server.communicate()[0]
if problems:
    sys.exit("打包出的程序提供网页时有问题：\n  " + "\n  ".join(problems) + "\n程序的输出：\n" + output)
print(f"打包出的程序能提供网页：首页和样式表、接口、登录都正常，{stop_name} 以后正常退出。")
PY

# The license texts of everything that went in (BUILD-05), to publish with the program. The script is a tool
# like this one, next to it; HFile's own LICENSE and NOTICE are those of the commit.
"$PYTHON" "$ROOT/scripts/licenses.py" "$WORK/build/hfile" "$SRC" "$WORK/dist/hfile-licenses.txt"

# Every check passed: now the program and its licenses take the place of those in $OUT (BUILD-02), each
# copied next to its old one first and then renamed over it.
for file in "hfile$exe" hfile-licenses.txt; do
  cp "$WORK/dist/$file" "$OUT/.$file.new"
  mv -f "$OUT/.$file.new" "$OUT/$file"
done
echo "已生成：$OUT/hfile$exe（提交 $commit），许可证在 $OUT/hfile-licenses.txt"
