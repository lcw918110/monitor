#!/usr/bin/env bash
# 服务端一键部署：安装目录、生成配置、systemd、启动、健康检查
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/monitor}"
PORT="${PORT:-8080}"
BIND_HOST="${BIND_HOST:-0.0.0.0}"
TOKEN="${TOKEN:-}"
OFFLINE_SECONDS="${OFFLINE_SECONDS:-90}"
NO_SYSTEMD=0
START=1

usage() {
  cat <<EOF
用法: $0 [选项]

选项:
  --dir DIR           安装目录（默认 /opt/monitor，无 root 时可用 \$HOME/monitor）
  --port PORT         监听端口（默认 8080）
  --host HOST         绑定地址（默认 0.0.0.0）
  --token TOKEN       可选共享 Token
  --offline-seconds N 离线判定秒数（默认 90）
  --no-systemd        不安装 systemd，仅前台/后台脚本启动
  --no-start          只安装不启动
  -h, --help          帮助
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --host) BIND_HOST="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --offline-seconds) OFFLINE_SECONDS="$2"; shift 2 ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  if [[ "$INSTALL_DIR" == "/opt/monitor" ]]; then
    INSTALL_DIR="${HOME}/monitor"
    echo "[info] 非 root，安装目录改为: $INSTALL_DIR"
  fi
  NO_SYSTEMD=1
  echo "[info] 非 root，跳过 systemd，将使用 nohup 后台启动"
fi

# 先复用本机已有 Python >= 3.8（中心端），没有再用 apt/yum/dnf 安装
# shellcheck source=lib/resolve_python.sh
. "$ROOT/scripts/lib/resolve_python.sh"
# shellcheck source=lib/sync_tree.sh
. "$ROOT/scripts/lib/sync_tree.sh"
MONITOR_INSTALL_PYTHON=1
ensure_python 3 8 || exit 1
log_python_choice
apply_python_ld_library_path

echo "==> 同步代码到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
# 同步核心目录，保留已有 data/config
if ! sync_tree "$ROOT" "$INSTALL_DIR"; then
  echo "[fail] sync_tool_missing: 无法同步代码（rsync 优先，缺失则 tar/cp）"
  exit 1
fi

mkdir -p "$INSTALL_DIR/data" "$INSTALL_DIR/config" "$INSTALL_DIR/run"

CFG="$INSTALL_DIR/config/center.json"
if [[ ! -f "$CFG" ]]; then
  cat > "$CFG" <<EOF
{
  "host": "${BIND_HOST}",
  "port": ${PORT},
  "data_dir": "${INSTALL_DIR}/data",
  "token": "${TOKEN}",
  "offline_seconds": ${OFFLINE_SECONDS},
  "retention_days": 7,
  "cleanup_interval_seconds": 3600,
  "anomaly": {
    "cpu_warn_percent": 80,
    "cpu_critical_percent": 95,
    "cpu_load_per_core_warn": 2.0,
    "npu_util_warn_percent": 95,
    "npu_temp_warn_c": 85,
    "npu_temp_critical_c": 95,
    "npu_mem_warn_percent": 90,
    "npu_mem_critical_percent": 98
  }
}
EOF
  echo "已生成 $CFG"
else
  echo "保留已有配置 $CFG"
fi

# 生成便捷启动脚本
LD_EXPORT=""
if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
  LD_EXPORT="export LD_LIBRARY_PATH=\"${PY_LD_LIBRARY_PATH}\"\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
fi
cat > "$INSTALL_DIR/run/start_center.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR"
${LD_EXPORT}
exec "$PY" -m center --config "$CFG"
EOF
chmod +x "$INSTALL_DIR/run/start_center.sh"

UNIT_INSTALLED=0
if [[ "$NO_SYSTEMD" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
  UNIT=/etc/systemd/system/monitor-center.service
  LD_ENV_LINE=""
  if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
    LD_ENV_LINE="Environment=LD_LIBRARY_PATH=${PY_LD_LIBRARY_PATH}"
  fi
  cat > "$UNIT" <<EOF
[Unit]
Description=简易多机监控中心端
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
${LD_ENV_LINE}
ExecStart=$PY -m center --config $CFG
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable monitor-center >/dev/null
  UNIT_INSTALLED=1
  echo "已安装 systemd: monitor-center.service"
fi

if [[ "$START" -eq 1 ]]; then
  echo "==> 启动中心端"
  if [[ "$UNIT_INSTALLED" -eq 1 ]]; then
    systemctl restart monitor-center
  else
    # 停旧进程
    if [[ -f "$INSTALL_DIR/run/center.pid" ]]; then
      old="$(cat "$INSTALL_DIR/run/center.pid" || true)"
      if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
        kill "$old" 2>/dev/null || true
        sleep 1
      fi
    fi
    # 双重 fork 守护化，避免随 SSH/终端会话退出被杀
    DAEMON_ENV=(--env "PYTHONPATH=$INSTALL_DIR")
    if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
      DAEMON_ENV+=(--env "LD_LIBRARY_PATH=${PY_LD_LIBRARY_PATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
    fi
    "$PY" "$INSTALL_DIR/scripts/daemonize_run.py" \
      --pidfile "$INSTALL_DIR/run/center.pid" \
      --logfile "$INSTALL_DIR/run/center.log" \
      --cwd "$INSTALL_DIR" \
      "${DAEMON_ENV[@]}" \
      -- "$PY" -m center --config "$CFG"
  fi

  echo "==> 健康检查"
  ok=0
  for i in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:${PORT}/api/v1/health" >/dev/null 2>&1; then
      ok=1
      break
    fi
    sleep 0.5
  done
  if [[ "$ok" -eq 1 ]]; then
    curl -sS "http://127.0.0.1:${PORT}/api/v1/health"
    echo
    echo "部署成功: http://<本机IP>:${PORT}/"
  else
    echo "启动后健康检查失败，请查看日志:"
    if [[ "$UNIT_INSTALLED" -eq 1 ]]; then
      journalctl -u monitor-center -n 50 --no-pager || true
    else
      tail -n 50 "$INSTALL_DIR/run/center.log" || true
    fi
    exit 1
  fi
fi

echo
echo "客户端部署示例:"
echo "  ./scripts/deploy_agent.sh --center-url http://<中心IP>:${PORT} --token '${TOKEN}'"
