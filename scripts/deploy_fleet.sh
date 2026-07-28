#!/usr/bin/env bash
# 批量部署客户端：按 hosts 清单在多台机器上安装 Agent（需 SSH）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
HOSTS_FILE=""
CENTER_URL=""
TOKEN=""
SSH_USER="${USER}"
SSH_PORT=22
REMOTE_DIR="/opt/monitor"
PARALLEL=4

usage() {
  cat <<EOF
用法: $0 --hosts-file FILE --center-url URL [选项]

hosts 文件每行一台：
  10.0.0.11
  10.0.0.12 npu-node-12
  # 注释行忽略
  # 格式: IP [host_id]

选项:
  --hosts-file FILE   主机清单
  --center-url URL    中心地址
  --token TOKEN       可选 Token
  --ssh-user USER     SSH 用户（默认当前用户）
  --ssh-port PORT     SSH 端口（默认 22）
  --remote-dir DIR    远端安装目录（默认 /opt/monitor）
  --parallel N        并发数（默认 4）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
    --center-url) CENTER_URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-port) SSH_PORT="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

[[ -n "$HOSTS_FILE" && -f "$HOSTS_FILE" ]] || { echo "需要有效 --hosts-file"; exit 1; }
[[ -n "$CENTER_URL" ]] || { echo "需要 --center-url"; exit 1; }

command -v ssh >/dev/null || { echo "需要 ssh"; exit 1; }
command -v scp >/dev/null || { echo "需要 scp"; exit 1; }
command -v tar >/dev/null || { echo "需要 tar"; exit 1; }

TMP_TGZ="$(mktemp /tmp/monitor-agent.XXXXXX.tgz)"
cleanup() { rm -f "$TMP_TGZ"; }
trap cleanup EXIT

echo "==> 打包代码"
tar -C "$ROOT" -czf "$TMP_TGZ" \
  --exclude '.git' \
  --exclude 'data' \
  --exclude '.deploy-*' \
  --exclude '__pycache__' \
  --exclude '*.db' \
  agent center common config scripts tests README.md 2>/dev/null \
  || tar -C "$ROOT" -czf "$TMP_TGZ" agent center common config scripts

deploy_one() {
  local ip="$1"
  local host_id="$2"
  local ssh_opts=(-p "$SSH_PORT" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)
  echo "[$ip] 开始部署 host_id=$host_id"
  if ! scp "${ssh_opts[@]}" "$TMP_TGZ" "${SSH_USER}@${ip}:/tmp/monitor-agent.tgz"; then
    echo "[$ip] scp 失败"
    return 1
  fi
  ssh "${ssh_opts[@]}" "${SSH_USER}@${ip}" bash -s <<EOF
set -euo pipefail
sudo mkdir -p '$REMOTE_DIR'
sudo tar -xzf /tmp/monitor-agent.tgz -C '$REMOTE_DIR'
cd '$REMOTE_DIR'
sudo chmod +x scripts/*.sh || true
sudo ./scripts/deploy_agent.sh \\
  --center-url '$CENTER_URL' \\
  --dir '$REMOTE_DIR' \\
  --host-id '$host_id' \\
  --token '$TOKEN'
rm -f /tmp/monitor-agent.tgz
EOF
  echo "[$ip] 完成"
}

export -f deploy_one
export TMP_TGZ SSH_USER SSH_PORT REMOTE_DIR CENTER_URL TOKEN

FAIL=0
ACTIVE=0
while read -r line; do
  line="$(echo "$line" | sed 's/#.*//' | xargs || true)"
  [[ -z "$line" ]] && continue
  ip="$(echo "$line" | awk '{print $1}')"
  hid="$(echo "$line" | awk '{print $2}')"
  hid="${hid:-$ip}"
  deploy_one "$ip" "$hid" &
  ACTIVE=$((ACTIVE + 1))
  if [[ "$ACTIVE" -ge "$PARALLEL" ]]; then
    if ! wait -n 2>/dev/null; then
      # macOS bash 可能无 wait -n
      wait || FAIL=1
      ACTIVE=0
    else
      ACTIVE=$((ACTIVE - 1))
    fi
  fi
done < "$HOSTS_FILE"

wait || FAIL=1
if [[ "$FAIL" -ne 0 ]]; then
  echo "部分主机部署失败，请检查上方日志"
  exit 1
fi
echo "批量部署完成"
