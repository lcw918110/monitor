#!/usr/bin/env bash
# SSH/SCP 轻量重试 + 失败短码。供 deploy_fleet.sh 使用。
# 只对连接超时 / connection closed 等瞬时错误重试 2～3 次，不扫端口、不喷密码。
#
# shellcheck shell=bash

SSH_RETRY_ATTEMPTS="${SSH_RETRY_ATTEMPTS:-3}"

ssh_is_retryable() {
  local blob
  blob="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  # scp 在 Connection refused 时也会附带 "connection closed"
  case "$blob" in
    *"permission denied"*|*"authentication failed"*|*"connection refused"*) return 1 ;;
    *"no route to host"*|*"network is unreachable"*|*"could not resolve"*) return 1 ;;
  esac
  case "$blob" in
    *"connection timed out"*|*"connection timeout"*|*"operation timed out"*) return 0 ;;
    *"connection closed"*|*"connection reset"*|*"broken pipe"*) return 0 ;;
    *"kex_exchange_identification"*) return 0 ;;
    *"temporarily unavailable"*) return 0 ;;
  esac
  return 1
}

ssh_classify_fail() {
  local blob
  blob="$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')"
  if printf '%s\n' "${1:-}" | grep -E '\[fail\][[:space:]]*[a-z_]+[[:space:]]*:' >/dev/null 2>&1; then
    printf '%s\n' "${1:-}" | grep -E '\[fail\][[:space:]]*[a-z_]+[[:space:]]*:' | tail -n 1
    return 0
  fi
  case "$blob" in
    *"sshpass: command not found"*|*"sshpass_missing"*)
      echo "sshpass_missing"
      return 0
      ;;
    *"sudo_required"*|*"需要 sudo"*)
      echo "sudo_required"
      return 0
      ;;
    *"permission denied"*|*"authentication failed"*|*"too many authentication"*|*"publickey"*)
      echo "auth_fail"
      return 0
      ;;
    *"could not resolve"*|*"name or service not known"*|*"no route to host"*|*"network is unreachable"*)
      echo "ssh_unreachable"
      return 0
      ;;
    *"connection refused"*|*"connection timed out"*|*"connection timeout"*|*"operation timed out"*)
      echo "ssh_unreachable"
      return 0
      ;;
    *"connection closed"*|*"connection reset"*)
      echo "ssh_unreachable"
      return 0
      ;;
    *"未找到可用的 python"*|*"no_python"*)
      echo "no_python"
      return 0
      ;;
    *"上报自检失败"*|*"agent_start_fail"*)
      echo "agent_start_fail"
      return 0
      ;;
    *"package_incomplete"*|*"缺少 agent"*)
      echo "package_incomplete"
      return 0
      ;;
    *"sync_tool_missing"*|*"rsync: command not found"*)
      echo "sync_tool_missing"
      return 0
      ;;
  esac
  echo "remote_fail"
}

# 运行命令；瞬时 SSH 失败时最多试 SSH_RETRY_ATTEMPTS 次。
# 用法: ssh_run_retry <label> cmd...
# 成功时把 stdout/stderr 打到 stdout；失败时同样输出并返回非 0。
ssh_run_retry() {
  local label="${1:-ssh}"
  shift
  local attempt=1
  local delay=1
  local max="${SSH_RETRY_ATTEMPTS}"
  local out rc
  local stdin_file=""
  if [[ ! -t 0 ]]; then
    stdin_file="$(mktemp /tmp/monitor-ssh-stdin.XXXXXX 2>/dev/null || mktemp)"
    cat > "$stdin_file" || true
  fi
  while true; do
    set +e
    if [[ -n "$stdin_file" ]]; then
      out="$("$@" <"$stdin_file" 2>&1)"
    else
      out="$("$@" 2>&1)"
    fi
    rc=$?
    set -e
    if [[ "$rc" -eq 0 ]]; then
      [[ -n "$stdin_file" ]] && rm -f "$stdin_file"
      printf '%s' "$out"
      [[ -n "$out" && "$out" != *$'\n' ]] && printf '\n'
      return 0
    fi
    if [[ "$attempt" -ge "$max" ]] || ! ssh_is_retryable "$out"; then
      [[ -n "$stdin_file" ]] && rm -f "$stdin_file"
      printf '%s' "$out"
      [[ -n "$out" && "$out" != *$'\n' ]] && printf '\n'
      return "$rc"
    fi
    echo "[$label] SSH 瞬时失败，${delay}s 后重试 (${attempt}/${max})" >&2
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2))
  done
}

# 解析清单行: IP [host_id] [ssh_port]
# 亦支持 IPv4:port 写在第一列。结果写入 HOST_IP / HOST_ID / LINE_SSH_PORT（空=用默认）。
parse_inventory_line() {
  local line="${1:-}"
  HOST_IP=""
  HOST_ID=""
  LINE_SSH_PORT=""
  local token rest ip_part port_part
  token="${line%% *}"
  rest="${line#"$token"}"
  rest="${rest#"${rest%%[![:space:]]*}"}"

  ip_part="$token"
  if [[ "$token" == *:* && "$token" != *:*:* ]]; then
    port_part="${token##*:}"
    if [[ "$port_part" =~ ^[0-9]+$ ]] && [[ "$port_part" -ge 1 && "$port_part" -le 65535 ]]; then
      ip_part="${token%:*}"
      LINE_SSH_PORT="$port_part"
    fi
  fi
  HOST_IP="$ip_part"

  local f2 f3
  f2="${rest%% *}"
  if [[ -n "$f2" && "$f2" == "$rest" ]]; then
    HOST_ID="$f2"
    return 0
  fi
  if [[ -n "$f2" ]]; then
    rest="${rest#"$f2"}"
    rest="${rest#"${rest%%[![:space:]]*}"}"
    HOST_ID="$f2"
    f3="${rest%% *}"
    if [[ -n "$f3" && "$f3" =~ ^[0-9]+$ ]] && [[ "$f3" -ge 1 && "$f3" -le 65535 ]]; then
      LINE_SSH_PORT="$f3"
    fi
  fi
  [[ -n "$HOST_ID" ]] || HOST_ID="$HOST_IP"
}
