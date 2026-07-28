#!/usr/bin/env bash
# 停止本地守护进程（中心 + Agent）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CENTER_DIR="${MONITOR_CENTER_DIR:-$ROOT/.deploy-center}"
AGENT_DIR="${MONITOR_AGENT_DIR:-$ROOT/.deploy-agent}"
PORT="${MONITOR_PORT:-8080}"

stop_pidfile() {
  local name="$1"
  local pf="$2"
  if [[ ! -f "$pf" ]]; then
    echo "[$name] 无 pid 文件"
    return 0
  fi
  local old
  old="$(cat "$pf" 2>/dev/null || true)"
  if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
    kill "$old" 2>/dev/null || true
    sleep 0.4
    if kill -0 "$old" 2>/dev/null; then
      kill -9 "$old" 2>/dev/null || true
    fi
    echo "[$name] 已停止 PID=$old"
  else
    echo "[$name] 进程已不存在 (pid=$old)"
  fi
  rm -f "$pf"
}

stop_pidfile "agent" "$AGENT_DIR/run/agent.pid"
stop_pidfile "center" "$CENTER_DIR/run/center.pid"

if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "${pids:-}" ]]; then
    echo "释放端口 $PORT: $pids"
    echo "$pids" | xargs kill 2>/dev/null || true
  fi
fi

echo "已停止本地监控进程"
