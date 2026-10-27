#!/usr/bin/env bash
# 在中心机上把一套新代码升级进现有安装目录。
# 不要手写 cp / git archive / systemctl。工作站请用 scripts/push_upgrade.sh，
# 它会把本脚本从新包里带上中心再执行，而不是调用已经装在中心上的旧副本。
#
# 用法（在中心上）:
#   bash scripts/upgrade_center.sh <包目录或 tar.gz> --sha <git-sha> --dir /opt/monitor
#   bash scripts/upgrade_center.sh <包目录> --sha <git-sha> --dir /opt/monitor --check-only
#
# 保留 config/center.json、data/、vendor/python 里的 tar.gz。
# 先在新包上跑测试，失败则直接退出，不备份、不同步、不重启。
# 同步之前的测试把标准输入接到 /dev/null，避免继承一直不关的 SSH stdin。
# --check-only 跑完测试和磁盘预检就退出，不备份、不同步、不重启。
# 同步之后只做健康检查。回滚只恢复代码和 systemd 单元，不碰 data/，
# 失败的代码留在同级的 <安装目录名>-failed-<时间> 目录里。
# 记基线之前，用已经装上的 center.storage 按 config 的 retention_days 清一次过期历史上报。
# 新进程启动时会立刻再清一次；不清掉这部分，重启后条数变少会被当成数据丢失而回滚。
# 主机变少、库文件不见、库读不出来、历史上报被清空，仍然失败。
# 只少了「备份里已经过保留期」的行，不算失败。
# 跑 Python 之前，在脚本顶层从 systemd 单元读出 LD_LIBRARY_PATH 和 PATH 并 export。
# 不能放进 PY="$(upgrade_python)" 的子 shell，否则现场的 libpython 加载不到。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/sync_tree.sh
. "$SCRIPT_DIR/lib/sync_tree.sh"
# shellcheck source=lib/unit_env.sh
. "$SCRIPT_DIR/lib/unit_env.sh"
# shellcheck source=lib/python_min.sh
. "$SCRIPT_DIR/lib/python_min.sh"
# shellcheck source=lib/backup_prune.sh
. "$SCRIPT_DIR/lib/backup_prune.sh"

INSTALL_DIR="${MONITOR_UPGRADE_DIR:-/opt/monitor}"
SHA=""
KEEP_BACKUPS=5
CHECK_ONLY=0
PKG_INPUT=""
WORK=""
BACKUP_DIR=""
UPGRADE_MUTATED=0
UPGRADE_DONE=0

usage() {
  cat <<EOF
用法: $0 <包目录或 tar.gz> [--dir /opt/monitor] [--sha SHA] [--keep-backups N] [--check-only]

在中心机执行。先在新包上跑测试（标准输入接到 /dev/null），失败则不改安装目录。
--check-only 在测试和磁盘预检之后打印结果并退出，不备份、不同步、不重启。
通过后再用 Python sqlite3 backup 做一致的数据库副本并 quick_check。
同步时不覆盖 config/center.json、data/ 和已放置的便携 Python 包。
记基线前用现场 Storage 按 retention_days 清一次过期历史上报（--check-only 不清）。
重启后检查 health / hosts / reports。主机变少、数据库不见或读不出来、
历史上报被清空，都回滚代码。过期行被保留期清掉不回滚。
健康检查失败时只回滚代码和 systemd 单元，不从备份恢复 data/。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --sha) SHA="$2"; shift 2 ;;
    --keep-backups) KEEP_BACKUPS="$2"; shift 2 ;;
    --check-only) CHECK_ONLY=1; shift ;;
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

_upgrade_assert_not_live_data() {
  local target="${1:-}"
  local live="${INSTALL_DIR}/data"
  [[ -n "$target" ]] || return 1
  case "$target" in
    "$INSTALL_DIR"|"$live"|"$live"/*)
      echo "拒绝删除现场数据: $target" >&2
      return 1
      ;;
  esac
  return 0
}

cleanup() {
  if [[ -n "${SANDBOX_DIR:-}" && -d "$SANDBOX_DIR" ]]; then
    rm -rf "$SANDBOX_DIR"
  fi
  if [[ -n "${WORK:-}" && -d "$WORK" ]]; then
    if declare -F _upgrade_assert_not_live_data >/dev/null 2>&1; then
      _upgrade_assert_not_live_data "$WORK" || return 0
    fi
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

_unit_active() {
  local unit="$1"
  if upgrade_systemctl is-active --quiet "$unit"; then
    echo active
  else
    echo inactive
  fi
}

# 预同步测试用的假命令。只在测试子 shell 的 PATH 最前面，升级本身仍用原来的 systemctl。
_upgrade_prepare_test_tools() {
  local tool
  SANDBOX_DIR="$(mktemp -d /tmp/monitor-upgrade-sandbox.XXXXXX)"
  SANDBOX_BIN="${SANDBOX_DIR}/bin"
  SANDBOX_HOME="${SANDBOX_DIR}/home"
  mkdir -p "$SANDBOX_BIN" "$SANDBOX_HOME"
  if [[ -z "${MONITOR_TEST_FAKE_LOG:-}" ]]; then
    MONITOR_TEST_FAKE_LOG="${SANDBOX_DIR}/commands.log"
  fi
  : >> "$MONITOR_TEST_FAKE_LOG"
  for tool in systemctl pkill kill ssh sshpass scp; do
    cat > "${SANDBOX_BIN}/${tool}" <<EOF
#!/bin/bash
printf '%s %s\n' "${tool}" "\$*" >> "\$MONITOR_TEST_FAKE_LOG"
exit 0
EOF
    chmod 755 "${SANDBOX_BIN}/${tool}"
  done
}

# 测试前是 active、现在不是，就拉起来并告警。结果写到 UNIT_NOW。
_ensure_recorded_unit() {
  local unit="$1"
  local before="$2"
  local now
  now="$(_unit_active "$unit")"
  if [[ "$before" == "active" && "$now" != "active" ]]; then
    echo "[warn] ${unit} 在测试前是 active，现在是 ${now}，尝试重新启动"
    upgrade_systemctl start "$unit" >/dev/null 2>&1 || true
    now="$(_unit_active "$unit")"
    if [[ "$now" == "active" ]]; then
      echo "[ok] 已重新启动 ${unit}"
    else
      echo "[warn] ${unit} 未能恢复为 active"
    fi
  elif [[ "$before" == "active" && "$now" == "active" ]]; then
    echo "[ok] ${unit} 仍为 active"
  else
    echo "[info] ${unit} 测试前=${before} 现在=${now}"
  fi
  UNIT_NOW="$now"
}

_print_unit_summary() {
  echo "  服务: monitor-agent ${AGENT_BEFORE:-未知}->${AGENT_AFTER:-未知}；monitor-center ${CENTER_BEFORE:-未知}->${CENTER_AFTER:-未知}"
}

upgrade_curl() {
  local url="$1"
  if [[ -n "$CURL_CMD" ]]; then
    "$CURL_CMD" "$url"
    return
  fi
  curl -sf --max-time 10 "$url"
}

# 从单元里取某个 Environment= 的值。一行里可以有多个 KEY=VAL。不要在命令替换的子 shell 里 export。
_upgrade_unit_env_value() {
  local key="$1" line rest tok k v found=""
  [[ -f "$UNIT_PATH" ]] || return 0
  while IFS= read -r line || [[ -n "$line" ]]; do
    case "$line" in
      Environment=*) rest="${line#Environment=}" ;;
      *) continue ;;
    esac
    if [[ "$rest" == \"*\" && "$rest" != *" "* ]]; then
      rest="${rest#\"}"
      rest="${rest%\"}"
    fi
    # shellcheck disable=SC2086
    for tok in $rest; do
      tok="${tok#\"}"
      tok="${tok%\"}"
      k="${tok%%=*}"
      [[ "$tok" == *=* ]] || continue
      v="${tok#*=}"
      if [[ "$k" == "$key" ]]; then
        found="$v"
      fi
    done
  done < "$UNIT_PATH"
  printf '%s\n' "$found"
}

# 顶层调用。LD_LIBRARY_PATH / PATH 来自单元；PYTHONPATH 固定为正在测试的新包，不用现场树。
_upgrade_apply_unit_runtime_env() {
  local unit_ld unit_path
  unit_ld="$(_upgrade_unit_env_value LD_LIBRARY_PATH)"
  unit_path="$(_upgrade_unit_env_value PATH)"
  if [[ -n "$unit_ld" ]]; then
    case ":${LD_LIBRARY_PATH:-}:" in
      *":${unit_ld}:"*) ;;
      *) export LD_LIBRARY_PATH="${unit_ld}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" ;;
    esac
  fi
  if [[ -n "$unit_path" ]]; then
    export PATH="${unit_path}${PATH:+:$PATH}"
  fi
  if [[ -n "${PKG:-}" ]]; then
    export PYTHONPATH="$PKG"
  fi
}

_py_imports_sqlite() {
  local bin="$1"
  [[ -n "$bin" && -x "$bin" ]] || return 1
  "$bin" -c 'import sqlite3' >/dev/null 2>&1
}

_upgrade_execstart_python() {
  local line py
  [[ -f "$UNIT_PATH" ]] || return 0
  line="$(grep -E '^ExecStart=' "$UNIT_PATH" | head -n 1 || true)"
  py="${line#ExecStart=}"
  py="${py%% *}"
  py="${py#\"}"
  py="${py%\"}"
  printf '%s\n' "$py"
}

# 只往 stdout 打解释器路径。环境变量必须已经在父 shell export 过。
upgrade_python() {
  local cand
  if [[ -n "${MONITOR_UPGRADE_PYTHON:-}" ]]; then
    if _py_imports_sqlite "$MONITOR_UPGRADE_PYTHON"; then
      printf '%s\n' "$MONITOR_UPGRADE_PYTHON"
      return 0
    fi
    echo "[warn] MONITOR_UPGRADE_PYTHON 无法 import sqlite3: $MONITOR_UPGRADE_PYTHON" >&2
  fi
  cand="$(_upgrade_execstart_python)"
  if [[ -n "$cand" ]]; then
    if _py_imports_sqlite "$cand"; then
      printf '%s\n' "$cand"
      return 0
    fi
    echo "[warn] 单元 ExecStart 的解释器无法 import sqlite3: $cand" >&2
  fi
  cand="$(command -v python3 2>/dev/null || true)"
  if _py_imports_sqlite "$cand"; then
    printf '%s\n' "$cand"
    return 0
  fi
  cand="$(command -v python 2>/dev/null || true)"
  if _py_imports_sqlite "$cand"; then
    printf '%s\n' "$cand"
    return 0
  fi
  return 1
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
try:
    conn = sqlite3.connect(path, timeout=30)
    try:
        try:
            conn.execute("PRAGMA busy_timeout=30000")
        except sqlite3.Error:
            pass
        def one(sql):
            return int(conn.execute(sql).fetchone()[0])
        print("%d %d" % (
            one("SELECT COUNT(*) FROM hosts"),
            one("SELECT COUNT(*) FROM metrics_history"),
        ))
    finally:
        conn.close()
except sqlite3.Error as exc:
    sys.stderr.write("读取数据库失败: %s\n" % exc)
    raise SystemExit(1)
PY
}

# mode=cleanup：用安装目录里的 Storage 删过期 metrics_history，stdout 打「天数 条数」。
# mode=floor：现场历史上报不得少于备份里 ts 仍在保留期内的行。stdout 打这个下限。
# 保留天数与 center/server.py 一致：int(config.get("retention_days") or 7)。
_upgrade_retention_py() {
  local mode="$1"
  PYTHONPATH="${INSTALL_DIR}${PYTHONPATH:+:$PYTHONPATH}" \
    "$PY" - "$mode" "$INSTALL_DIR" "$DB" "${BACKUP_DIR}/code/data/monitor.db" <<'PY'
import json, os, sqlite3, sys

mode, install, live_db, backup_db = sys.argv[1:5]
cfg_path = os.path.join(install, "config", "center.json")

def fail(msg):
    sys.stderr.write(str(msg).rstrip() + "\n")
    raise SystemExit(1)

def load_cfg():
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError) as exc:
        fail("无法读取中心配置: %s" % exc)
    if not isinstance(cfg, dict):
        fail("中心配置不是对象")
    return cfg

def retention_days(cfg):
    raw = cfg.get("retention_days", 7)
    try:
        return int(raw or 7)
    except (TypeError, ValueError):
        fail("retention_days 无法解析: %r" % (raw,))

def open_db(path):
    conn = sqlite3.connect(path, timeout=30)
    try:
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    return conn

if mode == "cleanup":
    if not os.path.isfile(live_db):
        sys.stdout.write("0 0\n")
        raise SystemExit(0)
    storage_py = os.path.join(install, "center", "storage.py")
    if not os.path.isfile(storage_py):
        fail("安装目录没有 center/storage.py: %s" % install)
    sys.path.insert(0, install)
    try:
        import center.storage as storage_mod
    except Exception as exc:
        fail("无法导入安装目录里的 center.storage: %s" % exc)
    loaded = os.path.realpath(storage_mod.__file__)
    expect = os.path.realpath(storage_py)
    if loaded != expect:
        fail("center.storage 不是安装目录里的那份: %s" % loaded)
    days = retention_days(load_cfg())
    storage = storage_mod.Storage(db_path=live_db, retention_days=days)
    try:
        deleted = storage.cleanup_old_metrics()
    except sqlite3.Error as exc:
        fail("保留期清理失败: %s" % exc)
    if deleted is None:
        deleted = 0
    try:
        deleted = int(deleted)
    except (TypeError, ValueError):
        fail("保留期清理没有返回删除条数")
    if deleted < 0:
        fail("保留期清理没有返回删除条数")
    sys.stdout.write("%d %d\n" % (days, deleted))
    raise SystemExit(0)

if mode == "floor":
    import time
    if not os.path.isfile(backup_db):
        fail("没有升级前的数据库备份，不能把历史上报减少解释成保留期清理")
    if not os.path.isfile(live_db):
        fail("现场数据库不见了: %s" % live_db)
    days = retention_days(load_cfg())
    if days <= 0:
        fail("retention_days=%s 时进程不会删除历史上报" % days)
    cutoff = int(time.time()) - days * 86400
    try:
        backup = open_db(backup_db)
        try:
            try:
                backup.execute("PRAGMA query_only=ON")
            except sqlite3.Error:
                pass
            floor = int(backup.execute(
                "SELECT COUNT(*) FROM metrics_history WHERE ts >= ?",
                (cutoff,),
            ).fetchone()[0])
        finally:
            backup.close()
        live = open_db(live_db)
        try:
            live_hist = int(live.execute(
                "SELECT COUNT(*) FROM metrics_history"
            ).fetchone()[0])
        finally:
            live.close()
    except sqlite3.Error as exc:
        fail("核对保留期条数失败: %s" % exc)
    if live_hist < floor:
        fail(
            "保留期内历史上报不足: 现场 %d，备份中仍应保留 %d（retention_days=%d cutoff=%d）"
            % (live_hist, floor, days, cutoff)
        )
    sys.stdout.write("%d\n" % floor)
    raise SystemExit(0)

fail("未知保留期子命令: %s" % mode)
PY
}

# 现场库上的一次保留期清理。失败时调用方还没同步代码，不能当成升级已开始。
_run_retention_cleanup() {
  local attempt out rc days deleted
  for attempt in 1 2 3; do
    set +e
    out="$(_upgrade_retention_py cleanup)"
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      read -r days deleted <<<"$out"
      if [[ "$days" =~ ^-?[0-9]+$ && "$deleted" =~ ^[0-9]+$ ]]; then
        echo "[ok] 升级前按 retention_days=${days} 清理过期历史上报 ${deleted} 条（现场 Storage）"
        return 0
      fi
      echo "升级前保留期清理没有返回条数: ${out}" >&2
      return 1
    fi
    if [[ "$attempt" -lt 3 ]]; then
      echo "[warn] 保留期清理第 ${attempt} 次失败，1 秒后重试" >&2
      sleep 1
    fi
  done
  return 1
}

_load_after_counts() {
  local line
  if [[ "$DB_WAS_PRESENT" == "1" && ! -f "$DB" ]]; then
    fail_upgrade "数据库文件不见了: $DB"
  fi
  if ! line="$(db_counts "$DB" "$PY")"; then
    fail_upgrade "数据库无法读取: $DB"
  fi
  read -r HOSTS_AFTER HIST_AFTER <<<"$line"
  if [[ ! "$HOSTS_AFTER" =~ ^[0-9]+$ || ! "$HIST_AFTER" =~ ^[0-9]+$ ]]; then
    fail_upgrade "数据库条数无法解析: ${line}"
  fi
}

# 主机变少、历史上报被清空：直接失败。保留期造成的变少留给调用方再核对。
_counts_fail_hard() {
  if [[ "$HOSTS_AFTER" -lt "$HOSTS_BEFORE" ]]; then
    fail_upgrade "主机条数减少（主机 ${HOSTS_BEFORE}->${HOSTS_AFTER}，历史上报 ${HIST_BEFORE}->${HIST_AFTER}）"
  fi
  if [[ "$HIST_BEFORE" -gt 0 && "$HIST_AFTER" -eq 0 ]]; then
    fail_upgrade "历史上报被清空（主机 ${HOSTS_BEFORE}->${HOSTS_AFTER}，历史上报 ${HIST_BEFORE}->${HIST_AFTER}）"
  fi
}

# 现场库仍盖住备份里未过保留期的行时返回 0。
_history_within_retention() {
  local out rc floor
  set +e
  out="$(_upgrade_retention_py floor)"
  rc=$?
  set -e
  if [[ "$rc" -ne 0 ]]; then
    return 1
  fi
  read -r floor <<<"$out"
  if [[ ! "$floor" =~ ^[0-9]+$ ]]; then
    echo "保留期核对没有返回条数: ${out}" >&2
    return 1
  fi
  echo "[ok] 历史上报 ${HIST_BEFORE}->${HIST_AFTER} 落在保留期内（备份里未过期 ${floor} 条，现场 ${HIST_AFTER} 条），主机未减少"
  return 0
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

# 复制目录项，跳过 data/。不删除源，也不删除目标里的 data/。
_upgrade_copy_except_data() {
  local src="$1"
  local dest="$2"
  local item name
  local glob_state
  glob_state="$(shopt -p nullglob dotglob || true)"
  shopt -s nullglob dotglob
  mkdir -p "$dest"
  for item in "$src"/*; do
    [[ -e "$item" ]] || continue
    name="$(basename "$item")"
    [[ "$name" == "." || "$name" == ".." || "$name" == "data" ]] && continue
    cp -a "$item" "$dest/"
  done
  eval "$glob_state"
}

# 用备份里的代码盖回安装目录。只删除安装目录里除 data/ 以外的项。
_upgrade_restore_code_except_data() {
  local src="$1"
  local dest="$2"
  local item name
  local glob_state
  glob_state="$(shopt -p nullglob dotglob || true)"
  shopt -s nullglob dotglob
  for item in "$dest"/*; do
    [[ -e "$item" ]] || continue
    name="$(basename "$item")"
    [[ "$name" == "." || "$name" == ".." || "$name" == "data" ]] && continue
    if [[ "$item" == "$INSTALL_DIR/data" || "$item" == "$INSTALL_DIR/data/"* ]]; then
      echo "拒绝删除现场数据: $item" >&2
      return 1
    fi
    rm -rf "$item"
  done
  for item in "$src"/*; do
    [[ -e "$item" ]] || continue
    name="$(basename "$item")"
    [[ "$name" == "." || "$name" == ".." || "$name" == "data" ]] && continue
    cp -a "$item" "$dest/"
  done
  eval "$glob_state"
}

do_rollback() {
  local failed_dir ts parent base
  ts="$(date +%Y%m%d%H%M%S)"
  parent="$(dirname "$INSTALL_DIR")"
  base="$(basename "$INSTALL_DIR")"
  failed_dir="${parent}/${base}-failed-${ts}"
  if [[ -e "$failed_dir" ]]; then
    failed_dir="${failed_dir}-$$"
  fi
  echo "==> 回滚代码，失败的目录保留在 ${failed_dir}（不改 data/）"
  upgrade_systemctl stop monitor-center >/dev/null 2>&1 || true
  mkdir -p "$failed_dir"
  _upgrade_copy_except_data "$INSTALL_DIR" "$failed_dir"
  _upgrade_restore_code_except_data "$BACKUP_DIR/code" "$INSTALL_DIR"
  if [[ -f "$BACKUP_DIR/monitor-center.service" ]]; then
    mkdir -p "$(dirname "$UNIT_PATH")"
    cp -a "$BACKUP_DIR/monitor-center.service" "$UNIT_PATH"
  fi
  upgrade_systemctl daemon-reload >/dev/null 2>&1 || true
  upgrade_systemctl restart monitor-center >/dev/null 2>&1 || true
  echo "[rollback] 已恢复代码和 systemd 单元，data/ 未改动，失败代码在 ${failed_dir}"
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
  # 紧凑时间戳和 YYYYMMDD-HHMMSS 不能按字典序删，否则带横线的较新备份会先被清掉。
  sorted="$(backup_paths_oldest_first "$base" "${dirs[@]}")"
  remove_count=$(( ${#dirs[@]} - KEEP_BACKUPS ))
  while IFS= read -r d; do
    [[ -n "$d" ]] || continue
    if [[ "$i" -lt "$remove_count" ]]; then
      case "$(basename "$d")" in
        "${base}-backup-"*) ;;
        *)
          echo "拒绝删除非备份目录: $d" >&2
          i=$((i + 1))
          continue
          ;;
      esac
      if ! _upgrade_assert_not_live_data "$d"; then
        i=$((i + 1))
        continue
      fi
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

# 必须在当前 shell export。PY="$(upgrade_python)" 是子 shell，里面的 export 带不出来。
_upgrade_apply_unit_runtime_env
if ! PY="$(upgrade_python)"; then
  echo "找不到能 import sqlite3 的 Python。" >&2
  echo "已从 ${UNIT_PATH} 读取 Environment=LD_LIBRARY_PATH 和 PATH，PYTHONPATH=${PYTHONPATH:-未设置}（新包，不是现场安装树）。" >&2
  echo "若 ExecStart 的解释器报 libpython3.8.so.1.0，单元里的 LD_LIBRARY_PATH 没有指到对应的 lib 目录。" >&2
  exit 1
fi
if ! _py_imports_sqlite "$PY"; then
  echo "Python 无法 import sqlite3: $PY" >&2
  exit 1
fi

echo "==> 在新包上跑测试（尚未改动 $INSTALL_DIR）"
if [[ ! -d "$PKG/tests" ]]; then
  echo "升级包没有 tests/，未改动安装目录"
  exit 1
fi
# 不传 -t：tests/ 不是包（没有 __init__.py）。cwd 与 PYTHONPATH 指向新包，测试不得去写现场数据库。
# </dev/null：push_upgrade 的 SSH 会话 stdin 不会关闭，测试不能去读它。
# 测试进程使用临时 HOME、假 systemctl/kill/ssh，并打开 MONITOR_TEST_SANDBOX，
# 避免用例在中心上 systemctl stop monitor-agent。
AGENT_BEFORE="$(_unit_active monitor-agent)"
CENTER_BEFORE="$(_unit_active monitor-center)"
echo "[info] 测试前 monitor-agent=${AGENT_BEFORE} monitor-center=${CENTER_BEFORE}"
_upgrade_prepare_test_tools
test_rc=0
if ! (
  cd "$PKG" || exit 1
  export PYTHONPATH="$PKG"
  export MONITOR_TEST_SANDBOX=1
  export MONITOR_CLEANUP_SKIP_SYSTEMCTL=1
  export MONITOR_CLEANUP_SKIP_PKILL=1
  export MONITOR_AGENT_PID_DIRS=""
  export HOME="$SANDBOX_HOME"
  export PATH="${SANDBOX_BIN}:${PATH}"
  export MONITOR_TEST_FAKE_LOG
  "$PY" -m unittest discover -s tests
) </dev/null; then
  test_rc=1
fi
_ensure_recorded_unit monitor-agent "$AGENT_BEFORE"
AGENT_AFTER="$UNIT_NOW"
_ensure_recorded_unit monitor-center "$CENTER_BEFORE"
CENTER_AFTER="$UNIT_NOW"
if [[ "$test_rc" != 0 ]]; then
  echo "新包测试未通过，未改动安装目录"
  _print_unit_summary
  exit 1
fi
echo "[ok] 新包测试通过"

DB="$INSTALL_DIR/data/monitor.db"
DB_WAS_PRESENT=0
db_bytes=0
if [[ -f "$DB" ]]; then
  DB_WAS_PRESENT=1
  db_bytes="$(stat -c%s "$DB" 2>/dev/null || echo 0)"
fi
need_bytes=$(( db_bytes * 3 + 20 * 1024 * 1024 ))
free_kb="$(df -Pk "$INSTALL_DIR" | awk 'NR==2 {print $4}')"
free_bytes=$(( ${free_kb:-0} * 1024 ))
if [[ "$free_bytes" -lt "$need_bytes" ]]; then
  echo "磁盘空间不足: 可用 ${free_bytes} 字节，至少需要 ${need_bytes}（约 3 倍数据库再加 20MB）"
  exit 1
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  echo "[ok] 检查通过（未改动安装目录）"
  echo "目录=${INSTALL_DIR} sha=${SHA:-未指定} Python=${PY} 磁盘可用=${free_bytes} 需要=${need_bytes}"
  _print_unit_summary
  exit 0
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
  # 只换掉备份目录里的数据库文件，准备写入一致副本。现场 $DB 不在这条路径上。
  rm -f "$BACKUP_DIR/code/data/monitor.db" \
    "$BACKUP_DIR/code/data/monitor.db-wal" \
    "$BACKUP_DIR/code/data/monitor.db-shm"
  if ! backup_sqlite "$DB" "$BACKUP_DIR/code/data/monitor.db" "$PY"; then
    if _upgrade_assert_not_live_data "$BACKUP_DIR"; then
      rm -rf "$BACKUP_DIR"
    fi
    echo "数据库一致备份失败，未改动安装目录"
    exit 1
  fi
  echo "[ok] 数据库 quick_check 通过"
fi
if [[ -f "$UNIT_PATH" ]]; then
  cp -a "$UNIT_PATH" "$BACKUP_DIR/monitor-center.service"
fi

# 备份已经落下。这里再清现场库，基线就不含新进程启动时会立刻删掉的过期行。
# --check-only 在上面已经退出，不会走到这里。
if [[ "$DB_WAS_PRESENT" == "1" ]]; then
  if ! _run_retention_cleanup; then
    echo "升级前保留期清理失败，未同步代码"
    exit 1
  fi
fi
if ! before_line="$(db_counts "$DB" "$PY")"; then
  echo "升级前无法读取数据库: $DB"
  exit 1
fi
read -r HOSTS_BEFORE HIST_BEFORE <<<"$before_line"
if [[ ! "$HOSTS_BEFORE" =~ ^[0-9]+$ || ! "$HIST_BEFORE" =~ ^[0-9]+$ ]]; then
  echo "升级前数据库条数无法解析: ${before_line}"
  exit 1
fi
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
health_tries="${MONITOR_UPGRADE_HEALTH_TRIES:-10}"
_try=1
while [[ "$_try" -le "$health_tries" ]]; do
  if health_body="$(upgrade_curl "${BASE}/api/v1/health" 2>/dev/null || true)" && printf '%s' "$health_body" | grep -q '"ok"'; then
    health_ok=1
    break
  fi
  if [[ "$_try" -lt "$health_tries" ]]; then
    sleep 1
  fi
  _try=$((_try + 1))
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

# 主机变少、库不见、库读不出来、历史上报被清空：直接回滚。
# 历史上报变少但主机没少时，等启动清理结束再读一次。仍变少则只接受
# 「现场条数不少于备份里仍在保留期内的行」——升级窗口里刚过期的点不算丢数据。
_load_after_counts
_counts_fail_hard
if [[ "$HIST_AFTER" -lt "$HIST_BEFORE" ]]; then
  echo "[info] 历史上报 ${HIST_BEFORE}->${HIST_AFTER}，主机未减少。等待启动清理结束后再读一次"
  wait_s="${MONITOR_UPGRADE_CLEANUP_WAIT_SECONDS:-1}"
  if [[ ! "$wait_s" =~ ^[0-9]+$ ]]; then
    wait_s=1
  fi
  if [[ "$wait_s" -gt 0 ]]; then
    sleep "$wait_s"
  fi
  _load_after_counts
  _counts_fail_hard
fi
if [[ "$HIST_AFTER" -lt "$HIST_BEFORE" ]]; then
  if ! _history_within_retention; then
    fail_upgrade "历史上报减少且不是保留期清理（主机 ${HOSTS_BEFORE}->${HOSTS_AFTER}，历史上报 ${HIST_BEFORE}->${HIST_AFTER}）"
  fi
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
echo "  测试: 同步前已通过"
_ensure_recorded_unit monitor-agent "$AGENT_BEFORE"
AGENT_AFTER="$UNIT_NOW"
_ensure_recorded_unit monitor-center "$CENTER_BEFORE"
CENTER_AFTER="$UNIT_NOW"
_print_unit_summary
