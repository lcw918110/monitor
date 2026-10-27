#!/usr/bin/env bash
# 从中心备份恢复。备份可以是 upgrade_center.sh 或 backup_center.sh 产生的目录。
# 不用于升级，也不做双中心 / 自动切换。
#
#   --code-only   恢复代码、config/center.json、vendor/ 和单元文件，不覆盖 data/
#                 这和升级失败时的自动回滚同一边界。
#   --with-data   在此之上用 sqlite backup API 覆盖 data/monitor.db。
#                 会先停 monitor-center。备份之后的新上报会消失。
#
# 用法:
#   scripts/restore_center.sh --from /opt/monitor-backup-YYYYMMDDHHMMSS --code-only --yes
#   scripts/restore_center.sh --from /opt/monitor-backup-YYYYMMDDHHMMSS --with-data --yes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

exec python3 "$SCRIPT_DIR/center_backup.py" restore "$@"
