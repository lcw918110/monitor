#!/usr/bin/env bash
# 在中心机上给已经装好的中心做一份一致备份。
# 不升级、不重启、不部署 Agent。换代码仍只用 scripts/push_upgrade.sh。
#
# 目录与 upgrade_center.sh 相同：
#   <安装目录的父目录>/<安装名>-backup-YYYYMMDDHHMMSS/
# 升级下一轮默认 --keep-backups 5，会把这里的目录和升级备份放在一起清理。
#
# 用法:
#   scripts/backup_center.sh --dir /opt/monitor
#   scripts/backup_center.sh --dir /opt/monitor --keep 5
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:$PYTHONPATH}"

exec python3 "$SCRIPT_DIR/center_backup.py" backup "$@"
