#!/usr/bin/env bash
# Takes the README's screenshots again: docs/images/NAME.png in Chinese (README.md), NAME.en.png in English
# (docs/README.en.md). They come from a demo site made for them, anew each time: the files of make_content.py
# on a local storage source "demo" (its README.md shown under the list), its photos and software folders on
# two more, all three in the sidebar with icons of their own, an admin who may do everything with them, the
# site on 127.0.0.1, and the system's Chrome driven by Playwright (shots.py).
#
#   scripts/screenshots/run.sh [输出文件夹]      默认是项目里的 docs/images/
#
# Run it in the hfile environment (conda activate hfile): the site and make_content.py (Pillow) run in it;
# the site is the working tree, not the last commit. Playwright runs in a Python of its own:
# $PLAYWRIGHT_PYTHON, else a venv in the work folder, made anew (pip install playwright==PLAYWRIGHT_VERSION)
# whenever it doesn't have that version: the first time, after an install that broke off. The browser is
# $CHROME (default /usr/bin/google-chrome). The work folder, $HFILE_SHOTS_WORK (default
# $TMPDIR/hfile-screenshots), keeps that venv; the demo files and the site in it are made anew.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HERE="$ROOT/scripts/screenshots"
PYTHON="${PYTHON:-python}"
PLAYWRIGHT_VERSION=1.65.2
mkdir -p "${1:-$ROOT/docs/images}"
OUT="$(cd "${1:-$ROOT/docs/images}" && pwd)"
WORK="${HFILE_SHOTS_WORK:-${TMPDIR:-/tmp}/hfile-screenshots}"
SITE="$WORK/site"
mkdir -p "$WORK"

if [ -z "${PLAYWRIGHT_PYTHON:-}" ]; then
  PLAYWRIGHT_PYTHON="$WORK/venv/bin/python"
  if ! "$PLAYWRIGHT_PYTHON" -I -c 'import importlib.metadata as m, sys; sys.exit(m.version("playwright") != sys.argv[1])' \
      "$PLAYWRIGHT_VERSION" 2>/dev/null; then
    echo "在 $WORK/venv 里装 Playwright $PLAYWRIGHT_VERSION……"
    python3 -m venv --clear "$WORK/venv"
    "$WORK/venv/bin/pip" install -q "playwright==$PLAYWRIGHT_VERSION"
  fi
fi

# The demo files, then the site on SQLite and its admin (a random password, in a file only this user can
# read); install() makes the tables too. The storage sources come with each language, below.
"$PYTHON" "$HERE/make_content.py" "$WORK" >/dev/null
rm -rf "$SITE"
mkdir -p "$SITE"
(umask 077; "$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))' >"$SITE/admin-password")
port="$("$PYTHON" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])')"
export HFILE_CONFIG="$SITE/config.toml" PYTHONDONTWRITEBYTECODE=1
cd "$ROOT"
"$PYTHON" - "$HFILE_CONFIG" "$port" <<'PY'
import secrets, sys
from config import configfile
configfile.create(sys.argv[1])
configfile.update(sys.argv[1], {"server": {"host": "127.0.0.1", "port": int(sys.argv[2])},
                                "site": {"secret_key": secrets.token_urlsafe(50)}})
PY
PASSWORD_FILE="$SITE/admin-password" "$PYTHON" manage.py shell -v 0 -c '
import os
from apps.installer.service import install
install(username="admin", password=open(os.environ["PASSWORD_FILE"]).read().strip(), site_name="HFile")
'

server=""
stop() {
  if [ -n "$server" ]; then
    kill -TERM "$server" 2>/dev/null || true
    wait "$server" 2>/dev/null || true
    server=""
  fi
}
trap stop EXIT

# One language at a time: the storage sources show that language's files, under that language's names (the
# photos and software storages' names are their folders' names). The site starts again for each: it keeps a
# storage's driver, and so its folder, while it runs. Its time zone is the one the demo files' times are
# written in.
for lang in zh en; do
  DEMO_ROOT="$WORK/content-$lang" DEMO_LANG="$lang" "$PYTHON" manage.py shell -v 0 -c '
import os
from apps.accounts.models import User
from apps.accounts.permissions import ALL
from apps.storages import config, drivers
from apps.storages.models import Grant, StorageSource
zh = os.environ["DEMO_LANG"] == "zh"
admin = User.objects.get(username="admin")
for key, name, icon in [("demo", "演示存储" if zh else "Demo storage", "folder_shared"),
                        ("photos", "照片" if zh else "Photos", "photo_library"),
                        ("software", "软件" if zh else "Software", "download")]:
    root = os.environ["DEMO_ROOT"] if key == "demo" else os.path.join(os.environ["DEMO_ROOT"], name)
    storage, _ = StorageSource.objects.update_or_create(key=key, defaults={
        "name": name, "type": "local", "readme_from_file": key == "demo", "icon": icon, "show_in_nav": True,
        "config": config.encode(drivers.driver_class("local"), {"root": root})})
    Grant.objects.update_or_create(user=admin, storage=storage, defaults={
        "permissions": [code for code in ALL if not code.startswith("ignore_")], "root_path": "/", "enabled": True})
'
  TZ=Asia/Shanghai "$PYTHON" hfile.py --config "$HFILE_CONFIG" >"$SITE/server-$lang.log" 2>&1 &
  server=$!
  if ! curl -fs -o /dev/null --retry 60 --retry-connrefused --retry-delay 1 "http://127.0.0.1:$port/login/"; then
    echo "演示站点没有启动起来，看看 $SITE/server-$lang.log" >&2
    exit 1
  fi
  "$PLAYWRIGHT_PYTHON" -I "$HERE/shots.py" "http://127.0.0.1:$port" "$SITE/admin-password" "$lang" "$OUT"
  stop
done
echo "截图都保存在 $OUT 里了。"
