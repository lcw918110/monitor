#!/usr/bin/env bash
# 批量部署客户端：在**中心机**上按 hosts 清单 SSH 安装 Agent。
# 这是产品部署路径的 CLI（与 /deploy.html 同一套脚本），不要从笔记本 sshpass+scp 旁路。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=lib/deploy_ssh.sh
. "$ROOT/scripts/lib/deploy_ssh.sh"

HOSTS_FILE=""
CENTER_URL=""
TOKEN=""
SSH_USER="${USER}"
SSH_PORT=22
REMOTE_DIR="/opt/monitor-agent"
PARALLEL=4

usage() {
  cat <<EOF
用法: $0 --hosts-file FILE --center-url URL [选项]

hosts 文件每行一台（# 开头为注释）：
  10.0.0.11
  10.0.0.12 npu-node-12
  10.0.0.13 gpu-13 2222
  10.0.0.14:2222 gpu-14
  # 格式: IP[ :port] [host_id] [ssh_port]
  # 行内 ssh_port 可选，默认 22（或 --ssh-port）；不扫描端口

选项:
  --hosts-file FILE   主机清单
  --center-url URL    中心地址
  --token TOKEN       可选 Token
  --ssh-user USER     SSH 用户（默认当前用户）
  --ssh-port PORT     默认 SSH 端口（默认 22）
  --port PORT         同 --ssh-port
  --remote-dir DIR    远端安装目录（默认 /opt/monitor-agent；Center 仍用 /opt/monitor）
  --parallel N        并发数（默认 4）
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --hosts-file) HOSTS_FILE="$2"; shift 2 ;;
    --center-url) CENTER_URL="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-port|--port) SSH_PORT="$2"; shift 2 ;;
    --remote-dir) REMOTE_DIR="$2"; shift 2 ;;
    --parallel) PARALLEL="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

[[ -n "$HOSTS_FILE" && -f "$HOSTS_FILE" ]] || { echo "需要有效 --hosts-file"; exit 1; }
[[ -n "$CENTER_URL" ]] || { echo "需要 --center-url"; exit 1; }

command -v ssh >/dev/null || { echo "需要 ssh"; exit 1; }
command -v tar >/dev/null || { echo "需要 tar"; exit 1; }

TMP_TGZ="$(mktemp /tmp/monitor-agent.XXXXXX.tgz)"
cleanup() { rm -f "$TMP_TGZ"; }
trap cleanup EXIT

echo "==> 打包代码"
if [[ ! -f "$ROOT/agent/__init__.py" || ! -d "$ROOT/common" || ! -d "$ROOT/scripts" ]]; then
  echo "[fail] package_incomplete: 中心树缺少 agent/common/scripts，拒绝打包（网页部署从中心安装目录读取）"
  exit 1
fi
tar -C "$ROOT" -czf "$TMP_TGZ" \
  --exclude '.git' \
  --exclude 'data' \
  --exclude '.deploy-*' \
  --exclude '__pycache__' \
  --exclude '*.db' \
  agent center common config scripts tests README.md

fail_one() {
  local ip="$1"
  local code="$2"
  shift 2
  echo "[$ip] [fail] ${code}: $*"
  return 1
}

remote_has_rsync() {
  local user="$1" ip="$2"
  shift 2
  local out
  out="$(ssh "$@" "${user}@${ip}" "command -v rsync" 2>/dev/null || true)"
  [[ -n "$out" ]]
}

deploy_one() {
  local ip="$1"
  local host_id="$2"
  local port="$3"
  # shellcheck source=lib/deploy_ssh.sh
  . "$ROOT/scripts/lib/deploy_ssh.sh"
  local ssh_opts=(-p "$port" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8)
  local ssh_e="ssh -p ${port} -o StrictHostKeyChecking=accept-new -o ConnectTimeout=8"
  echo "[$ip] 开始部署 host_id=$host_id port=$port"
  local out rc method="tar"

  if command -v rsync >/dev/null 2>&1 && [[ -z "${MONITOR_SYNC_DISABLE_RSYNC:-}" ]]; then
    if ssh_run_retry "$ip" ssh "${ssh_opts[@]}" "${SSH_USER}@${ip}" "mkdir -p '$REMOTE_DIR' && command -v rsync" >/dev/null; then
      set +e
      out="$(rsync -a --delete \
        --exclude 'data/' \
        --exclude 'config/center.json' \
        --exclude 'config/agent.json' \
        --exclude '.git/' \
        --exclude '.deploy-*/' \
        --exclude '__pycache__/' \
        --exclude '*.db' \
        -e "$ssh_e" \
        "$ROOT/" "${SSH_USER}@${ip}:${REMOTE_DIR}/" 2>&1)"
      rc=$?
      set -e
      if [[ "$rc" -eq 0 ]]; then
        method="rsync"
      else
        echo "[$ip] [sync] rsync 不可用，回退 tar"
      fi
    fi
  fi

  if [[ "$method" != "rsync" ]]; then
    if ! command -v scp >/dev/null 2>&1; then
      fail_one "$ip" sync_tool_missing "本机无 scp，且 rsync 不可用"
      return 1
    fi
    set +e
    out="$(ssh_run_retry "$ip" scp "${ssh_opts[@]}" "$TMP_TGZ" "${SSH_USER}@${ip}:/tmp/monitor-agent.tgz")"
    rc=$?
    set -e
    if [[ "$rc" -ne 0 ]]; then
      local code
      code="$(ssh_classify_fail "$out")"
      fail_one "$ip" "$code" "scp 失败"
      printf '%s\n' "$out"
      return 1
    fi
    method="tar"
  fi
  echo "[$ip] [sync] method=$method"

  set +e
  out="$(ssh_run_retry "$ip" ssh "${ssh_opts[@]}" "${SSH_USER}@${ip}" bash -s <<EOF
set -euo pipefail
sudo mkdir -p '$REMOTE_DIR'
if [[ -f /tmp/monitor-agent.tgz ]]; then
  tar -tzf /tmp/monitor-agent.tgz | grep -q 'agent/__init__.py' || {
    echo "[fail] package_incomplete: 安装包不含 agent/（请检查中心树）"
    exit 1
  }
  sudo tar -xzf /tmp/monitor-agent.tgz -C '$REMOTE_DIR'
fi
if ! sudo test -f '$REMOTE_DIR/agent/__init__.py'; then
  echo "[fail] package_incomplete: $REMOTE_DIR 缺少 agent/__init__.py"
  exit 1
fi
cd '$REMOTE_DIR'
sudo chmod +x scripts/*.sh || true
sudo ./scripts/deploy_agent.sh \\
  --center-url '$CENTER_URL' \\
  --dir '$REMOTE_DIR' \\
  --host-id '$host_id' \\
  --token '$TOKEN'
rm -f /tmp/monitor-agent.tgz
echo DEPLOY_DONE
EOF
)"
  rc=$?
  set -e
  printf '%s\n' "$out"
  if printf '%s\n' "$out" | grep -q 'DEPLOY_DONE'; then
    echo "[$ip] 完成"
    return 0
  fi
  local code
  code="$(ssh_classify_fail "$out")"
  fail_one "$ip" "$code" "远端安装失败，exit=$rc"
  return 1
}

export -f deploy_one fail_one
export TMP_TGZ SSH_USER SSH_PORT REMOTE_DIR CENTER_URL TOKEN ROOT

FAIL=0
ACTIVE=0
while read -r line; do
  line="$(echo "$line" | sed 's/#.*//' | xargs || true)"
  [[ -z "$line" ]] && continue
  parse_inventory_line "$line"
  [[ -n "$HOST_IP" ]] || continue
  hid="${HOST_ID:-$HOST_IP}"
  port="${LINE_SSH_PORT:-$SSH_PORT}"
  deploy_one "$HOST_IP" "$hid" "$port" &
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
  echo "部分主机部署失败，请检查上方日志（失败行含 [fail] 短码）"
  exit 1
fi
echo "批量部署完成"
