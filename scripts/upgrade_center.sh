#!/usr/bin/env bash
# 在中心机上把一套新代码升级进现有安装目录。
# 不要手写 cp / git archive / systemctl。工作站请用 scripts/push_upgrade.sh，
# 它会把本脚本从新包里带上中心再执行，而不是调用已经装在中心上的旧副本。
#
# 用法（在中心上）:
#   bash scripts/upgrade_center.sh <包目录或 tar.gz> --sha <git-sha> --dir /opt/monitor
#
# 保留 config/center.json、data/、vendor/python 里的 tar.gz。
# 失败时用升级前的一致备份回滚代码、数据库和 systemd 单元。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/sync_tree.sh
. "$SCRIPT_DIR/lib/sync_tree.sh"
# shellcheck source=lib/unit_env.sh
. "$SCRIPT_DIR/lib/unit_env.sh"
# shellcheck source=lib/python_min.sh
. "$SCRIPT_DIR/lib/python_min.sh"

INSTALL_DIR="${MONITOR_UPGRADE_DIR:-/opt/monitor}"
SHA=""
KEEP_BACKUPS=5
PKG_INPUT=""
WORK=""
BACKUP_DIR=""
UPGRADE_MUTATED=0
UPGRADE_DONE=0

usage() {
  cat <<EOF
用法: $0 <包目录或 tar.gz> [--dir /opt/monitor] [--sha SHA] [--keep-backups N]

在中心机执行。升级前用 Python sqlite3 backup 做一致的数据库副本并 quick_check，
同步时不覆盖 config/center.json、data/ 和已放置的便携 Python 包。
健康检查、主机/报告接口、历史上报条数或本机测试失败时自动回滚。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --sha) SHA="$2"; shift 2 ;;
    --keep-backups) KEEP_BACKUPS="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --) shift; break ;;
    -*) echo "未知参数: $1"; usage; exit 1 ;;
    *)
      if [[ -z "$PKG_INPUT" ]]; then
        PKG_INPUT="$1"
        shift
      else
        echo "多余参数: $1"
        usage
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$PKG_INPUT" ]]; then
  usage
  exit 1
fi

UNIT_PATH="${MONITOR_UPGRADE_UNIT:-/etc/systemd/system/monitor-center.service}"
SYSTEMCTL_CMD="${MONITOR_UPGRADE_SYSTEMCTL_CMD:-systemctl}"
CURL_CMD="${MONITOR_UPGRADE_CURL_CMD:-}"

cleanup() {
  if [[ -n "${WORK:-}" && -d "$WORK" ]]; then
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

upgrade_systemctl() {
  if [[ ! -x "$SYSTEMCTL_CMD" && "$SYSTEMCTL_CMD" != "systemctl" ]]; then
    if [[ -x "$SYSTEMCTL_CMD" ]]; then
      "$SYSTEMCTL_CMD" "$@"
      return
    fi
  fi
  if [[ "$SYSTEMCTL_CMD" != "systemctl" ]]; then
    "$SYSTEMCTL_CMD" "$@"
    return
  fi
  command systemctl "$@"
}

upgrade_curl() {
  local url="$1"
  if [[ -n "$CURL_CMD" ]]; then
    "$CURL_CMD" "$url"
    return
  fi
  curl -sf --max-time 10 "$url"
}

upgrade_python() {
  local line py
  if [[ -n "${MONITOR_UPGRADE_PYTHON:-}" && -x "${MONITOR_UPGRADE_PYTHON}" ]]; then
    printf '%s\n' "$MONITOR_UPGRADE_PYTHON"
    return 0
  fi
  if [[ -f "$UNIT_PATH" ]]; then
    line="$(grep -E '^ExecStart=' "$UNIT_PATH" | head -n 1 || true)"
    py="${line#ExecStart=}"
    py="${py%% *}"
    if [[ -n "$py" && -x "$py" ]]; then
      printf '%s\n' "$py"
      return 0
    fi
    line="$(grep -E '^Environment=LD_LIBRARY_PATH=' "$UNIT_PATH" | head -n 1 || true)"
    if [[ -n "$line" ]]; then
      export LD_LIBRARY_PATH="${line#Environment=LD_LIBRARY_PATH=}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    fi
  fi
  command -v python3
}

backup_sqlite() {
  local src="$1"
  local dst="$2"
  local py="$3"
  "$py" - "$src" "$dst" <<'PY'
import os, sqlite3, sys
src, dst = sys.argv[1], sys.argv[2]
os.makedirs(os.path.dirname(dst), exist_ok=True)
src_conn = sqlite3.connect(src)
dst_conn = sqlite3.connect(dst)
try:
    src_conn.backup(dst_conn)
finally:
    dst_conn.close()
    src_conn.close()
check = sqlite3.connect(dst)
try:
    row = check.execute("PRAGMA quick_check").fetchone()
finally:
    check.close()
if not row or str(row[0]).lower() != "ok":
    raise SystemExit("quick_check 失败: %s" % (row,))
PY
}

db_counts() {
  local db="$1"
  local py="$2"
  "$py" - "$db" <<'PY'
import os, sqlite3, sys
path = sys.argv[1]
if not os.path.isfile(path):
    print("0 0")
    raise SystemExit(0)
conn = sqlite3.connect(path)
def one(sql):
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return 0
print("%d %d" % (
    one("SELECT COUNT(*) FROM hosts"),
    one("SELECT COUNT(*) FROM metrics_history"),
))
conn.close()
PY
}

upgrade_port() {
  local cfg="$INSTALL_DIR/config/center.json"
  local py="$1"
  if [[ ! -f "$cfg" ]]; then
    echo 8080
    return 0
  fi
  "$py" - "$cfg" <<'PY'
import json, sys
print(int(json.load(open(sys.argv[1], encoding="utf-8")).get("port") or 8080))
PY
}

do_rollback() {
  echo "==> 回滚到 ${BACKUP_DIR}"
  upgrade_systemctl stop monitor-center >/dev/null 2>&1 || true
  local failed_dir="${INSTALL_DIR}.failed-$$"
  rm -rf "$failed_dir"
  if [[ -d "$INSTALL_DIR" ]]; then
    mv "$INSTALL_DIR" "$failed_dir"
  fi
  cp -a "$BACKUP_DIR/code" "$INSTALL_DIR"
  if [[ -f "$BACKUP_DIR/monitor-center.service" ]]; then
    mkdir -p "$(dirname "$UNIT_PATH")"
    cp -a "$BACKUP_DIR/monitor-center.service" "$UNIT_PATH"
  fi
  upgrade_systemctl daemon-reload >/dev/null 2>&1 || true
  upgrade_systemctl restart monitor-center >/dev/null 2>&1 || true
  rm -rf "$failed_dir"
  echo "[rollback] 已恢复代码、数据库和 systemd 单元"
}

fail_upgrade() {
  local reason="$1"
  echo "升级失败: $reason"
  if [[ "$UPGRADE_MUTATED" == "1" && -n "$BACKUP_DIR" && -d "$BACKUP_DIR/code" ]]; then
    do_rollback || echo "[rollback] 回滚命令没有全部成功，请检查 $BACKUP_DIR"
  fi
  exit 1
}

prune_backups() {
  local parent base d
  local -a dirs=()
  parent="$(dirname "$INSTALL_DIR")"
  base="$(basename "$INSTALL_DIR")"
  shopt -s nullglob
  for d in "$parent/${base}-backup-"*; do
    [[ -d "$d" ]] || continue
    dirs+=("$d")
  done
  shopt -u nullglob
  if [[ "${#dirs[@]}" -le "$KEEP_BACKUPS" ]]; then
    return 0
  fi
  local sorted remove_count i=0
  sorted="$(printf '%s\n' "${dirs[@]}" | sort)"
  remove_count=$(( ${#dirs[@]} - KEEP_BACKUPS ))
  while IFS= read -r d; do
    [[ -n "$d" ]] || continue
    if [[ "$i" -lt "$remove_count" ]]; then
      rm -rf "$d"
      echo "[backup] 删除旧备份 $d"
    fi
    i=$((i + 1))
  done <<<"$sorted"
}

if [[ -f "$PKG_INPUT" ]]; then
  WORK="$(mktemp -d /tmp/monitor-upgrade-pkg.XXXXXX)"
  tar -xzf "$PKG_INPUT" -C "$WORK"
  PKG="$WORK"
elif [[ -d "$PKG_INPUT" ]]; then
  PKG="$(cd "$PKG_INPUT" && pwd)"
else
  echo "安装包不存在: $PKG_INPUT"
  exit 1
fi

if ! assert_package_tree "$PKG" "升级包"; then
  exit 1
fi
if [[ ! -d "$INSTALL_DIR" ]]; then
  echo "安装目录不存在: $INSTALL_DIR （首次安装请用 scripts/deploy_center.sh）"
  exit 1
fi
if [[ ! -f "$INSTALL_DIR/config/center.json" ]]; then
  echo "缺少 $INSTALL_DIR/config/center.json，拒绝升级以免覆盖出一份空配置"
  exit 1
fi

PY="$(upgrade_python)"
if [[ -z "$PY" || ! -x "$PY" ]]; then
  echo "找不到可用于备份数据库的 Python >= ${MONITOR_CENTER_PY_MIN_MAJOR}.${MONITOR_CENTER_PY_MIN_MINOR}"
  exit 1
fi

DB="$INSTALL_DIR/data/monitor.db"
db_bytes=0
if [[ -f "$DB" ]]; then
  db_bytes="$(stat -c%s "$DB" 2>/dev/null || echo 0)"
fi
need_bytes=$(( db_bytes * 3 + 20 * 1024 * 1024 ))
free_kb="$(df -Pk "$INSTALL_DIR" | awk 'NR==2 {print $4}')"
free_bytes=$(( ${free_kb:-0} * 1024 ))
if [[ "$free_bytes" -lt "$need_bytes" ]]; then
  echo "磁盘空间不足: 可用 ${free_bytes} 字节，至少需要 ${need_bytes}（约 3 倍数据库再加 20MB）"
  exit 1
fi

TS="$(date +%Y%m%d%H%M%S)"
BACKUP_DIR="$(dirname "$INSTALL_DIR")/$(basename "$INSTALL_DIR")-backup-${TS}"
if [[ -e "$BACKUP_DIR" ]]; then
  BACKUP_DIR="${BACKUP_DIR}-$$"
fi
echo "==> 备份到 $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp -a "$INSTALL_DIR" "$BACKUP_DIR/code"
if [[ -f "$DB" ]]; then
  rm -f "$BACKUP_DIR/code/data/monitor.db" \
    "$BACKUP_DIR/code/data/monitor.db-wal" \
    "$BACKUP_DIR/code/data/monitor.db-shm"
  if ! backup_sqlite "$DB" "$BACKUP_DIR/code/data/monitor.db" "$PY"; then
    rm -rf "$BACKUP_DIR"
    echo "数据库一致备份失败，未改动安装目录"
    exit 1
  fi
  echo "[ok] 数据库 quick_check 通过"
fi
if [[ -f "$UNIT_PATH" ]]; then
  cp -a "$UNIT_PATH" "$BACKUP_DIR/monitor-center.service"
fi

read -r HOSTS_BEFORE HIST_BEFORE <<<"$(db_counts "$DB" "$PY")"
echo "[ok] 升级前 主机=${HOSTS_BEFORE} 历史上报=${HIST_BEFORE}"

echo "==> 同步代码到 $INSTALL_DIR"
UPGRADE_MUTATED=1
if ! sync_tree "$PKG" "$INSTALL_DIR"; then
  fail_upgrade "同步代码失败"
fi

exec_line=""
if [[ -f "$UNIT_PATH" ]]; then
  exec_line="$(grep -E '^ExecStart=' "$UNIT_PATH" | head -n 1 || true)"
fi
ENV_BLOCK="$(collect_preserved_environment "$UNIT_PATH" "$INSTALL_DIR" "")"
if [[ -z "$exec_line" ]]; then
  exec_line="ExecStart=${PY} -m center --config ${INSTALL_DIR}/config/center.json"
fi
mkdir -p "$(dirname "$UNIT_PATH")"
cat > "$UNIT_PATH" <<EOF
[Unit]
Description=简易多机监控中心端
After=network.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
${ENV_BLOCK}
${exec_line}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

if [[ -n "$SHA" ]]; then
  printf '%s\n' "$SHA" > "$INSTALL_DIR/.merge_sha"
fi
if [[ -f "$PKG/center/__init__.py" ]]; then
  ver="$(grep -E '__version__' "$PKG/center/__init__.py" | head -n 1 || true)"
  if [[ -n "$ver" ]]; then
    printf '%s\n' "$ver" > "$INSTALL_DIR/version"
  fi
fi

echo "==> 重启 monitor-center"
upgrade_systemctl daemon-reload >/dev/null 2>&1 || true
if ! upgrade_systemctl restart monitor-center; then
  fail_upgrade "systemctl restart monitor-center 失败"
fi
if ! upgrade_systemctl is-active --quiet monitor-center; then
  fail_upgrade "monitor-center 未处于 active"
fi

PORT="$(upgrade_port "$PY")"
BASE="http://127.0.0.1:${PORT}"
echo "==> 检查 ${BASE}"
health_ok=0
health_body=""
for _try in 1 2 3 4 5 6 7 8 9 10; do
  if health_body="$(upgrade_curl "${BASE}/api/v1/health" 2>/dev/null || true)" && printf '%s' "$health_body" | grep -q '"ok"'; then
    health_ok=1
    break
  fi
  sleep 1
done
if [[ "$health_ok" != "1" ]]; then
  fail_upgrade "健康检查失败: ${BASE}/api/v1/health"
fi
if ! upgrade_curl "${BASE}/api/v1/hosts" >/dev/null; then
  fail_upgrade "GET /api/v1/hosts 失败"
fi
if ! upgrade_curl "${BASE}/api/v1/reports" >/dev/null; then
  fail_upgrade "GET /api/v1/reports 失败"
fi

read -r HOSTS_AFTER HIST_AFTER <<<"$(db_counts "$DB" "$PY")"
if [[ "$HOSTS_AFTER" -lt "$HOSTS_BEFORE" || "$HIST_AFTER" -lt "$HIST_BEFORE" ]]; then
  fail_upgrade "主机或历史上报条数减少（主机 ${HOSTS_BEFORE}->${HOSTS_AFTER}，历史上报 ${HIST_BEFORE}->${HIST_AFTER}）"
fi

echo "==> 在安装树上跑测试"
if [[ ! -d "$INSTALL_DIR/tests" ]]; then
  fail_upgrade "安装树没有 tests/"
fi
# 不传 -t：tests/ 不是包（没有 __init__.py）。cwd 与 PYTHONPATH 指向安装树，center 才能被导入。
if ! (cd "$INSTALL_DIR" && PYTHONPATH="$INSTALL_DIR" "$PY" -m unittest discover -s tests); then
  fail_upgrade "安装树上的测试未通过"
fi

prune_backups
UPGRADE_DONE=1
echo
echo "升级完成"
echo "  目录: $INSTALL_DIR"
echo "  sha: ${SHA:-未提供}"
echo "  备份: $BACKUP_DIR"
echo "  主机: ${HOSTS_BEFORE} -> ${HOSTS_AFTER}"
echo "  历史上报: ${HIST_BEFORE} -> ${HIST_AFTER}"
echo "  健康检查: ${health_body}"
echo "  测试: 通过"
