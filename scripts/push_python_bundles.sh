#!/usr/bin/env bash
# 把本机的便携 Python 目录同步到中心。
# 远端 /opt/monitor/vendor/python 不存在时先 mkdir -p，避免 scp 因父目录缺失失败。
#
# 用法:
#   scripts/push_python_bundles.sh [--ssh-cmd CMD] <user@center-host> [本地目录]
# 例:
#   scripts/fetch_python_bundle.sh --dest vendor/python
#   scripts/push_python_bundles.sh root@10.0.0.8
#   SSHPASS='***' scripts/push_python_bundles.sh root@10.0.0.8 vendor/python
#
# 认证与 push_upgrade.sh 相同：默认 ssh；SSHPASS + sshpass -e；或 --ssh-cmd。
# MONITOR_PUSH_DRY=1 只打印 SSH 命令和远端目录，不连接。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/ssh_cmd.sh
. "$SCRIPT_DIR/lib/ssh_cmd.sh"

ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TARGET=""
LOCAL=""
LOCAL_SET=0
MONITOR_SSH_CMD_OVERRIDE="${MONITOR_SSH_CMD_OVERRIDE:-}"
REMOTE_DIR="/opt/monitor/vendor/python"

usage() {
  cat <<EOF
用法: $0 [--ssh-cmd CMD] <user@center-host> [本地目录]
例:   $0 root@10.0.0.8
      SSHPASS='***' $0 root@10.0.0.8 vendor/python

默认本地目录是仓库的 vendor/python。远端固定为 ${REMOTE_DIR}，会先 mkdir -p。
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ssh-cmd)
      MONITOR_SSH_CMD_OVERRIDE="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      break
      ;;
    -*)
      echo "未知参数: $1" >&2
      usage
      exit 1
      ;;
    *)
      if [[ -z "$TARGET" ]]; then
        TARGET="$1"
        shift
      elif [[ "$LOCAL_SET" -eq 0 ]]; then
        LOCAL="$1"
        LOCAL_SET=1
        shift
      else
        echo "多余参数: $1" >&2
        usage
        exit 1
      fi
      ;;
  esac
done

if [[ -z "$TARGET" ]]; then
  usage
  exit 1
fi
if [[ -z "$LOCAL" ]]; then
  LOCAL="$ROOT/vendor/python"
fi

if [[ -z "$MONITOR_SSH_CMD_OVERRIDE" ]]; then
  command -v ssh >/dev/null 2>&1 || { echo "需要 ssh" >&2; exit 1; }
fi
monitor_init_ssh || exit 1

if [[ "${MONITOR_PUSH_DRY:-}" == "1" ]]; then
  printf 'SSH=%s\n' "${SSH_ARGV[*]}"
  printf 'TARGET=%s\n' "$TARGET"
  printf 'LOCAL=%s\n' "$LOCAL"
  printf 'MKDIR=%s\n' "$REMOTE_DIR"
  exit 0
fi

echo "==> 同步 ${LOCAL} 到 ${TARGET}:${REMOTE_DIR}"
status=0
monitor_push_bundles "$TARGET" "$LOCAL" "$REMOTE_DIR" || status=$?
if [[ "$status" -eq 2 ]]; then
  echo "本地没有 *.tar.gz: $LOCAL" >&2
  exit 1
fi
if [[ "$status" -ne 0 ]]; then
  echo "同步便携 Python 失败" >&2
  exit 1
fi
echo "[ok] 已放到 ${REMOTE_DIR}"
