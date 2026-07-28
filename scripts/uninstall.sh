#!/usr/bin/env bash
# 卸载中心端或采集端服务
set -euo pipefail

ROLE=""
INSTALL_DIR=""
PURGE_DATA=0

usage() {
  cat <<EOF
用法: $0 --role center|agent [--dir DIR] [--purge-data]

  --role          center 或 agent
  --dir           安装目录（默认 /opt/monitor 或 ~/monitor）
  --purge-data    同时删除 data 与配置
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --role) ROLE="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --purge-data) PURGE_DATA=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

[[ "$ROLE" == "center" || "$ROLE" == "agent" ]] || { usage; exit 1; }

if [[ -z "$INSTALL_DIR" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    INSTALL_DIR=/opt/monitor
  else
    INSTALL_DIR="${HOME}/monitor"
  fi
fi

echo "==> 停止 $ROLE @ $INSTALL_DIR"
if command -v systemctl >/dev/null 2>&1 && [[ "$(id -u)" -eq 0 ]]; then
  systemctl stop "monitor-${ROLE}" 2>/dev/null || true
  systemctl disable "monitor-${ROLE}" 2>/dev/null || true
  rm -f "/etc/systemd/system/monitor-${ROLE}.service"
  systemctl daemon-reload 2>/dev/null || true
fi

if [[ -f "$INSTALL_DIR/run/${ROLE}.pid" ]]; then
  pid="$(cat "$INSTALL_DIR/run/${ROLE}.pid" || true)"
  if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
  fi
  rm -f "$INSTALL_DIR/run/${ROLE}.pid"
fi

# 兜底：按模块名杀进程
pkill -f "python3 -m ${ROLE}" 2>/dev/null || true

if [[ "$PURGE_DATA" -eq 1 ]]; then
  echo "==> 清理数据与配置"
  rm -rf "$INSTALL_DIR/data" "$INSTALL_DIR/config" "$INSTALL_DIR/run"
  echo "已清理。代码目录仍保留: $INSTALL_DIR"
else
  echo "服务已停止。保留数据目录。需要删数据请加 --purge-data"
fi
