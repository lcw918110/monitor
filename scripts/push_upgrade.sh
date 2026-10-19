#!/usr/bin/env bash
# 在 Mac / 工作站上把某个 git 版本推到中心并升级。
# 中心执行的是这次包里的 scripts/upgrade_center.sh，不是 /opt/monitor 里的旧脚本。
#
# 用法:
#   scripts/push_upgrade.sh <user@center-host> [git-ref]
# 例:
#   scripts/push_upgrade.sh root@10.0.0.8 HEAD
set -euo pipefail

if [[ $# -lt 1 || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  echo "用法: $0 <user@center-host> [git-ref]"
  echo "例:   $0 root@10.0.0.8 HEAD"
  exit 1
fi

TARGET="$1"
REF="${2:-HEAD}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

command -v git >/dev/null 2>&1 || { echo "需要 git"; exit 1; }
command -v ssh >/dev/null 2>&1 || { echo "需要 ssh"; exit 1; }

SHA="$(git rev-parse "$REF")"
REMOTE_DIR="/tmp/monitor-upgrade-src"

echo "==> 打包 ${REF} (${SHA}) 并上传到 ${TARGET}:${REMOTE_DIR}"
git archive --format=tar.gz "$REF" | ssh "$TARGET" "rm -rf '$REMOTE_DIR' && mkdir -p '$REMOTE_DIR' && tar -xzf - -C '$REMOTE_DIR'"

echo "==> 在中心执行新包里的 upgrade_center.sh"
ssh "$TARGET" "bash '$REMOTE_DIR/scripts/upgrade_center.sh' '$REMOTE_DIR' --sha '$SHA' --dir /opt/monitor"
