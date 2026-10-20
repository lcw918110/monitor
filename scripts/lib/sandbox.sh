#!/usr/bin/env bash
# 测试沙箱。MONITOR_TEST_SANDBOX=1 时，脚本库拒绝调用真实的
# systemctl / pkill / ssh，也拒绝在非临时目录上 kill。
# 临时目录：/tmp、/var/tmp、/private/tmp，以及 $TMPDIR。
# 放在这些目录里的同名可执行文件视为测试替身，允许执行。
#
# shellcheck shell=bash

if [[ -n "${_MONITOR_SANDBOX_LOADED:-}" ]]; then
  return 0 2>/dev/null || exit 0
fi
_MONITOR_SANDBOX_LOADED=1

monitor_abs_path() {
  local p="${1:-}"
  local dir base
  [[ -n "$p" ]] || { echo ""; return 0; }
  if command -v readlink >/dev/null 2>&1; then
    dir="$(readlink -f "$p" 2>/dev/null || true)"
    if [[ -n "$dir" ]]; then
      echo "$dir"
      return 0
    fi
  fi
  dir="$(cd "$(dirname "$p")" 2>/dev/null && pwd)" || { echo "$p"; return 0; }
  base="$(basename "$p")"
  echo "${dir}/${base}"
}

monitor_path_is_temp() {
  local abs="${1:-}"
  local tmp="${TMPDIR:-}"
  [[ -n "$abs" ]] || return 1
  tmp="${tmp%/}"
  case "$abs" in
    /tmp|/tmp/*|/var/tmp|/var/tmp/*|/private/tmp|/private/tmp/*) return 0 ;;
  esac
  if [[ -n "$tmp" ]]; then
    case "$abs" in
      "$tmp"|"$tmp"/*) return 0 ;;
    esac
  fi
  return 1
}

# 返回 0 表示已经拒绝，调用方不要再执行这个命令。
monitor_sandbox_deny_tool() {
  local tool="${1:-}"
  local resolved=""
  [[ "${MONITOR_TEST_SANDBOX:-}" == "1" ]] || return 1
  [[ -n "$tool" ]] || return 1
  resolved="$(command -v "$tool" 2>/dev/null || true)"
  [[ -n "$resolved" ]] || return 1
  resolved="$(monitor_abs_path "$resolved")"
  if monitor_path_is_temp "$resolved"; then
    return 1
  fi
  echo "[sandbox] 拒绝调用真实 ${tool}" >&2
  return 0
}

# 返回 0 表示这个目录不能拿来停进程。
monitor_sandbox_deny_kill_path() {
  local path="${1:-}"
  local abs=""
  [[ "${MONITOR_TEST_SANDBOX:-}" == "1" ]] || return 1
  [[ -n "$path" ]] || return 1
  abs="$(monitor_abs_path "$path")"
  if monitor_path_is_temp "$abs"; then
    return 1
  fi
  echo "[sandbox] 拒绝在非临时目录停止进程: ${abs}" >&2
  return 0
}
