#!/usr/bin/env bash
# Local MySQL 8.4 in Docker, for developing and testing HFile on MySQL (SQLite needs nothing of this).
# (This machine has no `docker compose` plugin, so this uses plain `docker run`.)
#
#   scripts/dev-db.sh up                     create or start the container, wait until it accepts connections
#   scripts/dev-db.sh down                   stop it (the data stays in the volume)
#   scripts/dev-db.sh status
#   scripts/dev-db.sh logs
#   scripts/dev-db.sh shell [库名]            mysql client as the app user (库名 default hfile)
#   scripts/dev-db.sh use 设置文件 [库名]      point that config.toml's [database] at the container
#   scripts/dev-db.sh destroy                remove the container AND its data volume (asks first)
#
# The account is in an env file: $HFILE_DEV_DB_ENV, else dev-db.env in the project's root (not in
# git). The first `up` writes it (chmod 600) with a random password:
#   HFILE_DEV_DB_HOST=127.0.0.1  HFILE_DEV_DB_PORT=23306  HFILE_DEV_DB_USER=hfile  HFILE_DEV_DB_PASSWORD=…
# The user may use every database whose name starts with hfile (hfile, hfile_platform, ...) and
# Django's test databases of them (test_hfile, test_hfile_platform, ...).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENV_FILE="${HFILE_DEV_DB_ENV:-$ROOT/dev-db.env}"
CONTAINER=hfile-mysql
VOLUME=hfile-mysql-data
IMAGE=mysql:8.4
PYTHON="${PYTHON:-python3}"

usage() { sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; }

DB_HOST=127.0.0.1 DB_PORT=23306 DB_USER=hfile DB_PASSWORD=""

read_env() {
  [ -f "$ENV_FILE" ] || return 1
  local key value
  while IFS='=' read -r key value; do
    case "$key" in
      HFILE_DEV_DB_HOST) DB_HOST="$value" ;;
      HFILE_DEV_DB_PORT) DB_PORT="$value" ;;
      HFILE_DEV_DB_USER) DB_USER="$value" ;;
      HFILE_DEV_DB_PASSWORD) DB_PASSWORD="$value" ;;
    esac
  done <"$ENV_FILE"
  [ -n "$DB_PASSWORD" ]
}

write_env() {
  DB_PASSWORD="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  (
    umask 077
    printf 'HFILE_DEV_DB_HOST=%s\nHFILE_DEV_DB_PORT=%s\nHFILE_DEV_DB_USER=%s\nHFILE_DEV_DB_PASSWORD=%s\n' \
      "$DB_HOST" "$DB_PORT" "$DB_USER" "$DB_PASSWORD" >"$ENV_FILE"
  )
  chmod 600 "$ENV_FILE"
  echo "新建了 $ENV_FILE（账号和密码，只有你能读）。"
}

need_env() {
  if ! read_env; then
    echo "缺少 $ENV_FILE（或者里面没有 HFILE_DEV_DB_PASSWORD）：先运行 $0 up。" >&2
    exit 1
  fi
}

exists() { docker container inspect "$CONTAINER" >/dev/null 2>&1; }

# A command in the container as the app user. The password goes in on stdin, not on a command line.
as_user() {
  local database="$1"
  shift
  printf '%s' "$DB_PASSWORD" | docker exec -i "$CONTAINER" sh -c \
    'MYSQL_PWD="$(cat)" exec mysql -h127.0.0.1 -u"$1" "$2" -e "$3"' sh "$DB_USER" "$database" "$*"
}

wait_ready() {
  # TCP only works once the real server is up; the first-run init server has networking off.
  printf '等待 MySQL 就绪'
  for _ in $(seq 1 90); do
    if as_user hfile 'SELECT 1' >/dev/null 2>&1; then
      echo ' ✓'
      return 0
    fi
    printf '.'
    sleep 2
  done
  echo
  echo "MySQL 没有按时就绪，看看日志：$0 logs" >&2
  return 1
}

case "${1:-}" in
  up)
    if exists; then
      need_env
      docker start "$CONTAINER" >/dev/null
    else
      read_env || write_env
      # The MYSQL_* settings go in through a file, so no password is on a command line. root's password
      # is random; the container keeps it: docker exec hfile-mysql printenv MYSQL_ROOT_PASSWORD
      settings="$(mktemp)"
      trap 'rm -f "$settings"' EXIT
      printf 'MYSQL_ROOT_PASSWORD=%s\nMYSQL_DATABASE=hfile\nMYSQL_USER=%s\nMYSQL_PASSWORD=%s\n' \
        "$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(18))')" "$DB_USER" "$DB_PASSWORD" >"$settings"
      docker create --name "$CONTAINER" --env-file "$settings" \
        -p "127.0.0.1:$DB_PORT:3306" -v "$VOLUME:/var/lib/mysql" "$IMAGE" >/dev/null
      # Copied in rather than mounted: the container keeps working when this folder moves.
      docker cp "$ROOT/docker/mysql/init/01-grants.sh" "$CONTAINER:/docker-entrypoint-initdb.d/01-grants.sh" >/dev/null
      docker start "$CONTAINER" >/dev/null
    fi
    wait_ready
    echo "MySQL $IMAGE 在 $DB_HOST:$DB_PORT，用户 $DB_USER，可以用名字以 hfile 开头的库和它们的测试库 test_hfile…"
    echo "让一个设置文件用它：$0 use 设置文件 [库名]"
    ;;
  down)
    docker stop "$CONTAINER" >/dev/null
    echo "已停止，数据保留在卷 $VOLUME 里。"
    ;;
  status)
    docker ps -a --filter "name=^${CONTAINER}\$" --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
    ;;
  logs)
    docker logs --tail 100 "$CONTAINER"
    ;;
  shell)
    need_env
    settings="$(mktemp)"
    trap 'rm -f "$settings"' EXIT
    (umask 077; printf 'MYSQL_PWD=%s\n' "$DB_PASSWORD" >"$settings")
    docker exec -it --env-file "$settings" "$CONTAINER" mysql -h127.0.0.1 -u"$DB_USER" "${2:-hfile}"
    ;;
  use)
    need_env
    config="${2:-}"
    name="${3:-hfile}"
    if [ -z "$config" ]; then
      echo "要写设置文件的路径：$0 use 设置文件 [库名]" >&2
      exit 1
    fi
    case "$name" in
      hfile*) ;;
      *) echo "库名要以 hfile 开头（这个用户只能用这些库），比如 hfile_dev。" >&2; exit 1 ;;
    esac
    # configfile.update keeps the rest of the file and writes it with mode 600; the password never
    # reaches the screen.
    printf '%s' "$DB_PASSWORD" | PYTHONPATH="$ROOT" PYTHONDONTWRITEBYTECODE=1 "$PYTHON" -c '
import sys
from config import configfile
path, host, port, user, name = sys.argv[1:]
configfile.create(path)
configfile.update(path, {"database": {"engine": "mysql", "host": host, "port": int(port), "user": user,
                                      "name": name, "password": sys.stdin.read()}})
' "$config" "$DB_HOST" "$DB_PORT" "$DB_USER" "$name"
    echo "$config 的 [database] 现在是 $DB_HOST:$DB_PORT 上的 MySQL 库 $name（测试库 test_$name）。"
    ;;
  destroy)
    read -r -p "删除容器 $CONTAINER 和数据卷 $VOLUME（里面的数据全部丢失）？输入 yes 确认：" answer
    if [ "$answer" != yes ]; then
      echo "已取消。"
      exit 1
    fi
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
    docker volume rm "$VOLUME" >/dev/null 2>&1 || true
    echo "已删除。账号文件 $ENV_FILE 还在，下次 up 接着用它。"
    ;;
  *)
    usage
    exit 1
    ;;
esac
