#!/usr/bin/env bash
# 本地稳定启动：中心 + 本机 Agent（双重 fork，不随终端退出而停）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CENTER_DIR="${MONITOR_CENTER_DIR:-$ROOT/.deploy-center}"
AGENT_DIR="${MONITOR_AGENT_DIR:-$ROOT/.deploy-agent}"
PORT="${MONITOR_PORT:-8080}"
PYTHON_BIN="${PYTHON_BIN:-$(command -v python3)}"

cd "$ROOT"
mkdir -p "$CENTER_DIR/run" "$CENTER_DIR/data" "$AGENT_DIR/run" "$AGENT_DIR/config"

# 若尚未用 deploy 脚本装过，从仓库同步最小可运行副本
ensure_layout() {
  local dest="$1"
  mkdir -p "$dest"
  for name in center agent common config scripts; do
    if [[ -d "$ROOT/$name" && ! -e "$dest/$name" ]]; then
      cp -R "$ROOT/$name" "$dest/$name"
    fi
  done
  # 守护启动脚本需始终与仓库一致
  mkdir -p "$dest/scripts"
  cp "$ROOT/scripts/daemonize_run.py" "$dest/scripts/daemonize_run.py"
}

ensure_layout "$CENTER_DIR"
ensure_layout "$AGENT_DIR"

if [[ ! -f "$CENTER_DIR/config/center.json" ]]; then
  if [[ -f "$ROOT/config/center.example.json" ]]; then
    cp "$ROOT/config/center.example.json" "$CENTER_DIR/config/center.json"
  fi
  # 确保 data_dir 指向本安装目录
  "$PYTHON_BIN" - <<PY
import json, os
p = "$CENTER_DIR/config/center.json"
cfg = json.load(open(p))
cfg["host"] = "0.0.0.0"
cfg["port"] = int("$PORT")
cfg["data_dir"] = os.path.join("$CENTER_DIR", "data")
json.dump(cfg, open(p, "w"), indent=2, ensure_ascii=False)
print("已生成", p)
PY
fi

if [[ ! -f "$AGENT_DIR/config/agent.json" ]]; then
  cp "$ROOT/config/agent.example.json" "$AGENT_DIR/config/agent.json"
  "$PYTHON_BIN" - <<PY
import json, socket
p = "$AGENT_DIR/config/agent.json"
cfg = json.load(open(p))
cfg["center_url"] = "http://127.0.0.1:${PORT}/api/v1/metrics"
cfg["host_id"] = cfg.get("host_id") or "local-dev"
cfg["hostname"] = cfg.get("hostname") or socket.gethostname()
json.dump(cfg, open(p, "w"), indent=2, ensure_ascii=False)
print("已生成", p)
PY
fi

stop_pidfile() {
  local pf="$1"
  if [[ -f "$pf" ]]; then
    local old
    old="$(cat "$pf" 2>/dev/null || true)"
    if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
      kill "$old" 2>/dev/null || true
      sleep 0.5
      kill -9 "$old" 2>/dev/null || true
    fi
    rm -f "$pf"
  fi
}

echo "==> 停止旧进程（若有）"
stop_pidfile "$CENTER_DIR/run/center.pid"
stop_pidfile "$AGENT_DIR/run/agent.pid"
# 释放端口（仅本机监听）
if command -v lsof >/dev/null 2>&1; then
  pids="$(lsof -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "${pids:-}" ]]; then
    echo "$pids" | xargs kill 2>/dev/null || true
    sleep 0.5
  fi
fi

DAEMON="$ROOT/scripts/daemonize_run.py"

echo "==> 启动中心端（守护进程）"
"$PYTHON_BIN" "$DAEMON" \
  --pidfile "$CENTER_DIR/run/center.pid" \
  --logfile "$CENTER_DIR/run/center.log" \
  --cwd "$CENTER_DIR" \
  --env "PYTHONPATH=$CENTER_DIR" \
  -- "$PYTHON_BIN" -m center --config "$CENTER_DIR/config/center.json"

ok=0
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 0.3
done
if [[ "$ok" -ne 1 ]]; then
  echo "中心端健康检查失败，日志："
  tail -n 40 "$CENTER_DIR/run/center.log" || true
  exit 1
fi

echo "==> 启动本机 Agent（守护进程）"
"$PYTHON_BIN" "$DAEMON" \
  --pidfile "$AGENT_DIR/run/agent.pid" \
  --logfile "$AGENT_DIR/run/agent.log" \
  --cwd "$AGENT_DIR" \
  --env "PYTHONPATH=$AGENT_DIR" \
  -- "$PYTHON_BIN" -m agent --config "$AGENT_DIR/config/agent.json"

sleep 1
echo
echo "已稳定启动："
echo "  监测台: http://127.0.0.1:${PORT}/"
echo "  部署页: http://127.0.0.1:${PORT}/deploy.html"
echo "  中心 PID: $(cat "$CENTER_DIR/run/center.pid" 2>/dev/null || echo '?')  日志: $CENTER_DIR/run/center.log"
echo "  Agent PID: $(cat "$AGENT_DIR/run/agent.pid" 2>/dev/null || echo '?')  日志: $AGENT_DIR/run/agent.log"
echo "停止: ./scripts/local_down.sh"
curl -sS "http://127.0.0.1:${PORT}/api/v1/health"; echo
