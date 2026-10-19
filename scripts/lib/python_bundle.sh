#!/usr/bin/env bash
# 目标机没有 >= Agent 下限的解释器时，解压中心上传的便携 CPython。
# 调用方传入已经选好架构的 tar 和 sha256。本脚本不访问网络。
#
# 成功后导出 PY / PY_VERSION / PY_LD_LIBRARY_PATH（经 resolve_python）。
# 失败时设置：
#   MONITOR_PYTHON_BUNDLE_CODE  python_bundle_missing|checksum|extract|exec
#   MONITOR_PYTHON_BUNDLE_MSG   给人看的说明
#
# 环境变量：
#   PYTHON_BUNDLE / MONITOR_PYTHON_BUNDLE
#   PYTHON_BUNDLE_SHA256 / MONITOR_PYTHON_BUNDLE_SHA256
#   MONITOR_PYTHON_BUNDLE_DEST   测试可指定解压目录
#   MONITOR_PYTHON_ASSUME_ROOT   1=当作 root（/usr/local/monitor-python）
#                                0=当作非 root（$INSTALL_DIR/python）
#   INSTALL_DIR                  非 root 时的安装目录
#
# shellcheck shell=bash

if ! declare -F resolve_python >/dev/null 2>&1; then
  _bundle_lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  # shellcheck source=resolve_python.sh
  . "${_bundle_lib_dir}/resolve_python.sh"
fi

python_bundle_dest() {
  if [[ -n "${MONITOR_PYTHON_BUNDLE_DEST:-}" ]]; then
    printf '%s\n' "$MONITOR_PYTHON_BUNDLE_DEST"
    return 0
  fi
  if [[ "${MONITOR_PYTHON_ASSUME_ROOT:-}" == "0" ]]; then
    printf '%s\n' "${INSTALL_DIR:-/opt/monitor-agent}/python"
    return 0
  fi
  if [[ "${MONITOR_PYTHON_ASSUME_ROOT:-}" == "1" ]] || [[ "$(id -u)" -eq 0 ]]; then
    printf '%s\n' "/usr/local/monitor-python"
    return 0
  fi
  printf '%s\n' "${INSTALL_DIR:-/opt/monitor-agent}/python"
}

_bundle_sha256() {
  local file="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$file" | awk '{print $1}'
    return 0
  fi
  if command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha256 "$file" | awk '{print $NF}'
    return 0
  fi
  return 1
}

install_bundled_python() {
  local major="${1:-${MONITOR_AGENT_PY_MIN_MAJOR:-3}}"
  local minor="${2:-${MONITOR_AGENT_PY_MIN_MINOR:-6}}"
  local bundle="${PYTHON_BUNDLE:-${MONITOR_PYTHON_BUNDLE:-}}"
  local expect="${PYTHON_BUNDLE_SHA256:-${MONITOR_PYTHON_BUNDLE_SHA256:-}}"
  local dest bin got
  MONITOR_PYTHON_BUNDLE_CODE=""
  MONITOR_PYTHON_BUNDLE_MSG=""
  if [[ -z "$bundle" || ! -f "$bundle" ]]; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_missing"
    MONITOR_PYTHON_BUNDLE_MSG="未找到便携 Python 包: ${bundle:-未指定}"
    return 1
  fi
  if [[ -z "$expect" ]]; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_checksum"
    MONITOR_PYTHON_BUNDLE_MSG="未提供便携 Python 的 sha256"
    return 1
  fi
  if ! got="$(_bundle_sha256 "$bundle")"; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_checksum"
    MONITOR_PYTHON_BUNDLE_MSG="目标机没有 sha256sum 或 openssl，无法校验便携 Python"
    return 1
  fi
  got="$(printf '%s' "$got" | tr '[:upper:]' '[:lower:]')"
  expect="$(printf '%s' "$expect" | tr '[:upper:]' '[:lower:]')"
  if [[ "$got" != "$expect" ]]; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_checksum"
    MONITOR_PYTHON_BUNDLE_MSG="便携 Python 校验失败（期望 ${expect}，实际 ${got}）"
    return 1
  fi
  dest="$(python_bundle_dest)"
  rm -rf "$dest"
  mkdir -p "$dest" || {
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_extract"
    MONITOR_PYTHON_BUNDLE_MSG="无法创建 ${dest}"
    return 1
  }
  if ! tar -xzf "$bundle" --strip-components=1 -C "$dest"; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_extract"
    MONITOR_PYTHON_BUNDLE_MSG="解压便携 Python 失败"
    return 1
  fi
  bin="$dest/bin/python3"
  if [[ ! -x "$bin" && -x "$dest/bin/python3.10" ]]; then
    bin="$dest/bin/python3.10"
  fi
  if [[ ! -x "$bin" ]]; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_extract"
    MONITOR_PYTHON_BUNDLE_MSG="便携包里没有 bin/python3"
    return 1
  fi
  PYTHON_BIN="$bin"
  if ! resolve_python "$major" "$minor"; then
    MONITOR_PYTHON_BUNDLE_CODE="python_bundle_exec"
    MONITOR_PYTHON_BUNDLE_MSG="便携 Python 无法启动或版本低于 ${major}.${minor}（需要 glibc >= 2.17 的 install_only 包）"
    return 1
  fi
  echo "[ok] 便携 Python 目录: ${dest}"
  echo "[ok] 使用便携 Python ${PY_VERSION}: $PY"
  return 0
}

# 先系统解释器（含可选的包管理器安装），失败再试便携包。
# 安装失败不会在这里退出；没有包时由调用方决定 no_python / python_install_fail。
ensure_agent_python() {
  local major="${1:-${MONITOR_AGENT_PY_MIN_MAJOR:-3}}"
  local minor="${2:-${MONITOR_AGENT_PY_MIN_MINOR:-6}}"
  local bundle
  MONITOR_PYTHON_BUNDLE_CODE=""
  MONITOR_PYTHON_BUNDLE_MSG=""
  MONITOR_PYTHON_QUIET_FAIL=1
  if ensure_python "$major" "$minor"; then
    MONITOR_PYTHON_QUIET_FAIL=0
    return 0
  fi
  bundle="${PYTHON_BUNDLE:-${MONITOR_PYTHON_BUNDLE:-}}"
  if [[ -n "$bundle" ]]; then
    if install_bundled_python "$major" "$minor"; then
      MONITOR_PYTHON_QUIET_FAIL=0
      return 0
    fi
    print_python_resolve_error "$major" "$minor" >&2
    MONITOR_PYTHON_QUIET_FAIL=0
    return 1
  fi
  MONITOR_PYTHON_QUIET_FAIL=0
  print_python_resolve_error "$major" "$minor" >&2
  return 1
}
