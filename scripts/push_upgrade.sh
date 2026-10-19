#!/usr/bin/env bash
# 在 Mac / 工作站上把某个 git 版本推到中心并升级。
# 中心执行的是这次包里的 scripts/upgrade_center.sh，不是 /opt/monitor 里的旧脚本。
#
# 用法:
#   scripts/push_upgrade.sh [--ssh-cmd CMD] <user@center-host> [git-ref]
# 例:
#   scripts/push_upgrade.sh root@10.0.0.8 HEAD
#   SSHPASS='***' scripts/push_upgrade.sh root@10.0.0.8 HEAD
#
# 本地 vendor/python（或 MONITOR_PUSH_BUNDLE_DIR）里如果有 *.tar.gz，
# 会先在中心 mkdir -p /opt/monitor/vendor/python 再同步过去。
# MONITOR_PUSH_DRY=1 只打印将要使用的 SSH 命令，不连接。
# 干跑时 git rev-parse 失败（例如 git archive 解出来的目录没有 .git）不退出，SHA 记为 unknown。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/ssh_cmd.sh
. "$SCRIPT_DIR/lib/ssh_cmd.sh"

TARGET=""
REF="HEAD"
REF_SET=0
MONITOR_SSH_CMD_OVERRIDE="${MONITOR_SSH_CMD_OVERRIDE:-}"

usage() {
  cat <<EOF
用法: $0 [--ssh-cmd CMD] <user@center-host> [git-ref]
例:   $0 root@10.0.0.8 HEAD
      SSHPASS='***' $0 root@10.0.0.8 HEAD

默认使用 ssh。设置了 SSHPASS 且本机有 sshpass 时改为 sshpass -e ssh。
--ssh-cmd 完全替换 SSH 命令（此时不再自动包 sshpass）。
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
      elif [[ "$REF_SET" -eq 0 ]]; then
        REF="$1"
        REF_SET=1
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

ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"

command -v git >/dev/null 2>&1 || { echo "需要 git" >&2; exit 1; }
if [[ -z "$MONITOR_SSH_CMD_OVERRIDE" ]]; then
  command -v ssh >/dev/null 2>&1 || { echo "需要 ssh" >&2; exit 1; }
fi
monitor_init_ssh || exit 1

# 干跑只检查 SSH 怎么拼。git archive 解包没有 .git 时 rev-parse 会失败，
# 不能因此让 upgrade_center.sh 的同步前测试整批退出。
SHA=""
if [[ "${MONITOR_PUSH_DRY:-}" == "1" ]]; then
  if ! SHA="$(git rev-parse "$REF" 2>/dev/null)"; then
    SHA="unknown"
  fi
elif ! SHA="$(git rev-parse "$REF")"; then
  echo "无法解析 git 版本 ${REF}: ${ROOT} 不是 git 仓库" >&2
  exit 1
fi
REMOTE_DIR="/tmp/monitor-upgrade-src"
BUNDLE_DIR="${MONITOR_PUSH_BUNDLE_DIR:-$ROOT/vendor/python}"
BUNDLE_REMOTE="/opt/monitor/vendor/python"
bundle_found=0
bundle_file=""
for bundle_file in "$BUNDLE_DIR"/*.tar.gz; do
  if [[ -f "$bundle_file" ]]; then
    bundle_found=1
    break
  fi
done

if [[ "${MONITOR_PUSH_DRY:-}" == "1" ]]; then
  printf 'SSH=%s\n' "${SSH_ARGV[*]}"
  printf 'TARGET=%s\n' "$TARGET"
  printf 'SHA=%s\n' "$SHA"
  if [[ "$bundle_found" == "1" ]]; then
    printf 'BUNDLES=sync\n'
    printf 'MKDIR=%s\n' "$BUNDLE_REMOTE"
  else
    printf 'BUNDLES=skip\n'
  fi
  exit 0
fi

if [[ "$bundle_found" == "1" ]]; then
  echo "==> 同步便携 Python 到 ${TARGET}:${BUNDLE_REMOTE}"
  if ! monitor_push_bundles "$TARGET" "$BUNDLE_DIR" "$BUNDLE_REMOTE"; then
    echo "同步便携 Python 失败" >&2
    exit 1
  fi
else
  echo "[info] 本地没有便携包（${BUNDLE_DIR}/*.tar.gz），跳过"
fi

echo "==> 打包 ${REF} (${SHA}) 并上传到 ${TARGET}:${REMOTE_DIR}"
git archive --format=tar.gz "$REF" | "${SSH_ARGV[@]}" "$TARGET" "rm -rf '$REMOTE_DIR' && mkdir -p '$REMOTE_DIR' && tar -xzf - -C '$REMOTE_DIR'"

echo "==> 在中心执行新包里的 upgrade_center.sh"
"${SSH_ARGV[@]}" "$TARGET" "bash '$REMOTE_DIR/scripts/upgrade_center.sh' '$REMOTE_DIR' --sha '$SHA' --dir /opt/monitor"
