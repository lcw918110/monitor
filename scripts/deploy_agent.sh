#!/usr/bin/env bash
# 客户端（Agent）一键部署：安装、写配置、systemd、启动、上报自检
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR="${INSTALL_DIR:-/opt/monitor}"
CENTER_URL=""
HOST_ID=""
HOSTNAME_CFG=""
HOST_TYPE="auto"
TOKEN=""
INTERVAL=15
NO_SYSTEMD=0
START=1

usage() {
  cat <<EOF
用法: $0 --center-url URL [选项]

必填:
  --center-url URL    中心端地址，如 http://10.0.0.8:8080
                      （可写根地址，脚本自动补 /api/v1/metrics）

选项:
  --dir DIR           安装目录（默认 /opt/monitor）
  --host-id ID        主机唯一 ID（默认 hostname）
  --hostname NAME     显示主机名
  --host-type TYPE    auto|npu|gpu|app（默认 auto）
  --token TOKEN       与中心端一致的 Token
  --interval N        上报间隔秒（默认 15）
  --no-systemd        不安装 systemd
  --no-start          只安装不启动
  -h, --help          帮助

环境变量:
  PYTHON_BIN          优先使用的 Python 解释器（需 >= 3.6）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --center-url) CENTER_URL="$2"; shift 2 ;;
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --host-id) HOST_ID="$2"; shift 2 ;;
    --hostname) HOSTNAME_CFG="$2"; shift 2 ;;
    --host-type) HOST_TYPE="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --no-systemd) NO_SYSTEMD=1; shift ;;
    --no-start) START=0; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

if [[ -z "$CENTER_URL" ]]; then
  echo "必须提供 --center-url"
  usage
  exit 1
fi

# 规范化上报地址
if [[ "$CENTER_URL" != */api/v1/metrics ]]; then
  CENTER_URL="${CENTER_URL%/}/api/v1/metrics"
fi

if [[ "$(id -u)" -ne 0 ]]; then
  if [[ "$INSTALL_DIR" == "/opt/monitor" ]]; then
    INSTALL_DIR="${HOME}/monitor"
    echo "[info] 非 root，安装目录改为: $INSTALL_DIR"
  fi
  NO_SYSTEMD=1
  echo "[info] 非 root，跳过 systemd，将使用 nohup 后台启动"
fi

# 先复用本机已有 Python >= 3.6，没有再用 apt/yum/dnf 安装
# shellcheck source=lib/resolve_python.sh
. "$ROOT/scripts/lib/resolve_python.sh"
MONITOR_INSTALL_PYTHON=1
ensure_python 3 6 || exit 1
log_python_choice
apply_python_ld_library_path
DETECT_HOST="$(hostname 2>/dev/null || echo agent-host)"
HOST_ID="${HOST_ID:-$DETECT_HOST}"
HOSTNAME_CFG="${HOSTNAME_CFG:-$DETECT_HOST}"

echo "==> 同步代码到 $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
  --exclude 'data/' \
  --exclude 'config/center.json' \
  --exclude 'config/agent.json' \
  --exclude '.git/' \
  --exclude '.deploy-*/' \
  --exclude '__pycache__/' \
  --exclude '*.db' \
  "$ROOT/" "$INSTALL_DIR/"

mkdir -p "$INSTALL_DIR/config" "$INSTALL_DIR/run"

CFG="$INSTALL_DIR/config/agent.json"
cat > "$CFG" <<EOF
{
  "center_url": "${CENTER_URL}",
  "host_id": "${HOST_ID}",
  "hostname": "${HOSTNAME_CFG}",
  "host_type": "${HOST_TYPE}",
  "token": "${TOKEN}",
  "interval_seconds": ${INTERVAL},
  "disk_path": "/"
}
EOF
echo "已写入 $CFG"

# 探测采集能力
echo "==> 环境探测"
if command -v npu-smi >/dev/null 2>&1; then
  echo "  [ok] 发现 npu-smi（将采集 NPU）"
else
  echo "  [--] 未发现 npu-smi（NPU 指标为空，仍上报 CPU）"
fi
if command -v nvidia-smi >/dev/null 2>&1; then
  echo "  [ok] 发现 nvidia-smi（将采集 GPU，可选）"
fi

LD_EXPORT=""
if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
  LD_EXPORT="export LD_LIBRARY_PATH=\"${PY_LD_LIBRARY_PATH}\"\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}"
fi
cat > "$INSTALL_DIR/run/start_agent.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR"
${LD_EXPORT}
exec "$PY" -m agent --config "$CFG"
EOF
chmod +x "$INSTALL_DIR/run/start_agent.sh"

UNIT_INSTALLED=0
if [[ "$NO_SYSTEMD" -eq 0 ]] && command -v systemctl >/dev/null 2>&1; then
  UNIT=/etc/systemd/system/monitor-agent.service
  LD_ENV_LINE=""
  if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
    LD_ENV_LINE="Environment=LD_LIBRARY_PATH=${PY_LD_LIBRARY_PATH}"
  fi
  cat > "$UNIT" <<EOF
[Unit]
Description=简易多机监控采集端
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
Environment=PYTHONPATH=$INSTALL_DIR
${LD_ENV_LINE}
ExecStart=$PY -m agent --config $CFG
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable monitor-agent >/dev/null
  UNIT_INSTALLED=1
  echo "已安装 systemd: monitor-agent.service"
fi

echo "==> 单次上报自检"
cd "$INSTALL_DIR"
export PYTHONPATH="$INSTALL_DIR"
if ! "$PY" -m agent --config "$CFG" --once; then
  echo "上报自检失败：请检查中心地址/Token/网络"
  exit 1
fi

if [[ "$START" -eq 1 ]]; then
  echo "==> 启动常驻 Agent"
  if [[ "$UNIT_INSTALLED" -eq 1 ]]; then
    systemctl restart monitor-agent
    systemctl --no-pager --full status monitor-agent | head -n 15 || true
  else
    if [[ -f "$INSTALL_DIR/run/agent.pid" ]]; then
      old="$(cat "$INSTALL_DIR/run/agent.pid" || true)"
      if [[ -n "${old:-}" ]] && kill -0 "$old" 2>/dev/null; then
        kill "$old" 2>/dev/null || true
        sleep 1
      fi
    fi
    DAEMON_ENV=(--env "PYTHONPATH=$INSTALL_DIR")
    if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
      DAEMON_ENV+=(--env "LD_LIBRARY_PATH=${PY_LD_LIBRARY_PATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
    fi
    "$PY" "$INSTALL_DIR/scripts/daemonize_run.py" \
      --pidfile "$INSTALL_DIR/run/agent.pid" \
      --logfile "$INSTALL_DIR/run/agent.log" \
      --cwd "$INSTALL_DIR" \
      "${DAEMON_ENV[@]}" \
      -- "$PY" -m agent --config "$CFG"
    echo "Agent PID=$(cat "$INSTALL_DIR/run/agent.pid" 2>/dev/null || echo '?') 日志: $INSTALL_DIR/run/agent.log"
  fi
fi

echo
echo "部署成功: host_id=${HOST_ID} → ${CENTER_URL}"
