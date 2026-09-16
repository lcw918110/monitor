#!/usr/bin/env bash
# 解析本机可用的 Python 解释器：先复用已有的，再（可选）安装。
#
# 选择顺序：
#   1. PYTHON_BIN（若已设置且版本满足下限）→ 直接采用
#   2. 其余候选中选版本最高且满足下限的：
#        PATH 中 python3.15 … python3.6
#        /usr/local/python3.*/bin/python3
#        PATH 中的 python3、python
# 接受条件：sys.version_info >= (主, 次)，默认 3.6
# 使用 /usr/local/python3.x 这类自定义前缀时，若 prefix/lib 下有
# libpython*.so，则设置 PY_LD_LIBRARY_PATH，避免找不到 .so。
#
# 成功后导出：
#   PY                 解释器路径
#   PY_VERSION         如 3.12
#   PY_LD_LIBRARY_PATH 需要时为 lib 目录，否则空
#
# 环境变量：
#   PYTHON_BIN                 优先解释器
#   MONITOR_PY_MIN_MAJOR/MINOR 版本下限（默认 3 / 6）
#   MONITOR_INSTALL_PYTHON     1=找不到时以 root 尝试 apt/yum/dnf 安装 python3
#   MONITOR_PYTHON_SEARCH_PATH 仅从此 PATH 查找 python3.x 名字（测试用；默认用当前 PATH）
#   MONITOR_PYTHON_LOCAL_GLOBS 额外扫描的 /usr/local/python3.* glob
#
# shellcheck shell=bash

MONITOR_PY_MIN_MAJOR="${MONITOR_PY_MIN_MAJOR:-3}"
MONITOR_PY_MIN_MINOR="${MONITOR_PY_MIN_MINOR:-6}"
MONITOR_INSTALL_PYTHON="${MONITOR_INSTALL_PYTHON:-0}"

PY="${PY:-}"
PY_VERSION="${PY_VERSION:-}"
PY_LD_LIBRARY_PATH="${PY_LD_LIBRARY_PATH:-}"

_py_which() {
  local name="$1"
  if [[ -n "${MONITOR_PYTHON_SEARCH_PATH:-}" ]]; then
    PATH="$MONITOR_PYTHON_SEARCH_PATH" command -v "$name" 2>/dev/null
  else
    command -v "$name" 2>/dev/null
  fi
}

_py_realpath() {
  local p="$1"
  if command -v readlink >/dev/null 2>&1; then
    readlink -f "$p" 2>/dev/null && return 0
  fi
  if command -v realpath >/dev/null 2>&1; then
    realpath "$p" 2>/dev/null && return 0
  fi
  printf '%s\n' "$p"
}

_py_is_custom_prefix() {
  local resolved="$1"
  local prefix="$2"
  local base
  base="$(basename "$prefix")"
  case "$resolved" in
    /usr/local/python3.*/*) return 0 ;;
  esac
  case "$base" in
    python3.*) return 0 ;;
  esac
  return 1
}

# 若解释器位于 <prefix>/bin/python*，查找 <prefix>/lib{,64} 下的 libpython
_py_prefix_lib_dir() {
  local bin="$1"
  local resolved prefix lib
  resolved="$(_py_realpath "$bin")"
  [[ -n "$resolved" ]] || resolved="$bin"
  prefix="$(cd "$(dirname "$resolved")/.." 2>/dev/null && pwd)" || return 1
  for lib in "$prefix/lib" "$prefix/lib64"; do
    if [[ -d "$lib" ]] && ls "$lib"/libpython3*.so* >/dev/null 2>&1; then
      printf '%s\n' "$lib"
      return 0
    fi
  done
  if _py_is_custom_prefix "$resolved" "$prefix" && [[ -d "$prefix/lib" ]]; then
    printf '%s\n' "$prefix/lib"
    return 0
  fi
  return 1
}

_py_ver_key() {
  # 3.12 -> 00030012，便于整数比较
  local maj min
  maj="${1%%.*}"
  min="${1#*.}"
  min="${min%%.*}"
  printf '%04d%04d\n' "${maj:-0}" "${min:-0}"
}

_py_probe() {
  local bin="$1"
  local lib="${2:-}"
  local major="${3:-$MONITOR_PY_MIN_MAJOR}"
  local minor="${4:-$MONITOR_PY_MIN_MINOR}"
  local out status=0
  if [[ -n "$lib" ]]; then
    out="$(
      env "LD_LIBRARY_PATH=${lib}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}" \
        "$bin" -c "import sys
ver = '%d.%d' % (sys.version_info[0], sys.version_info[1])
sys.stdout.write(ver + chr(10))
sys.exit(0 if sys.version_info >= (${major}, ${minor}) else 2)" 2>/dev/null
    )" || status=$?
  else
    out="$(
      "$bin" -c "import sys
ver = '%d.%d' % (sys.version_info[0], sys.version_info[1])
sys.stdout.write(ver + chr(10))
sys.exit(0 if sys.version_info >= (${major}, ${minor}) else 2)" 2>/dev/null
    )" || status=$?
  fi
  if [[ "$status" -eq 0 && -n "$out" ]]; then
    printf '%s\n' "$out" | head -n 1
    return 0
  fi
  return 1
}

# 探测成功则把 "path<TAB>version<TAB>ld_path" 打到 stdout
_py_try_bin() {
  local bin="$1"
  local major="${2:-$MONITOR_PY_MIN_MAJOR}"
  local minor="${3:-$MONITOR_PY_MIN_MINOR}"
  local lib="" ver=""
  local resolved prefix
  [[ -n "$bin" ]] || return 1
  if [[ ! -x "$bin" ]]; then
    bin="$(_py_which "$bin")" || return 1
  fi
  [[ -n "$bin" && -x "$bin" ]] || return 1
  lib="$(_py_prefix_lib_dir "$bin" || true)"
  resolved="$(_py_realpath "$bin")"
  prefix="$(cd "$(dirname "$resolved")/.." 2>/dev/null && pwd)" || prefix=""

  if [[ -n "$lib" ]] && _py_is_custom_prefix "$resolved" "$prefix"; then
    if ver="$(_py_probe "$bin" "$lib" "$major" "$minor")"; then
      printf '%s\t%s\t%s\n' "$bin" "$ver" "$lib"
      return 0
    fi
  fi

  if ver="$(_py_probe "$bin" "" "$major" "$minor")"; then
    printf '%s\t%s\t\n' "$bin" "$ver"
    return 0
  fi

  if [[ -n "$lib" ]]; then
    if ver="$(_py_probe "$bin" "$lib" "$major" "$minor")"; then
      printf '%s\t%s\t%s\n' "$bin" "$ver" "$lib"
      return 0
    fi
  fi
  return 1
}

_py_add_candidate() {
  local b="$1"
  local resolved
  [[ -n "$b" ]] || return 0
  if [[ ! -x "$b" ]]; then
    b="$(_py_which "$b")" || return 0
  fi
  [[ -n "$b" && -x "$b" ]] || return 0
  resolved="$(_py_realpath "$b")"
  case "${_PY_SEEN:-}" in
    *"|${resolved}|"*) return 0 ;;
  esac
  _PY_SEEN="${_PY_SEEN}|${resolved}|"
  _PY_CANDIDATES+=("$b")
}

_py_collect_candidates() {
  local include_python_bin="${1:-1}"
  _PY_CANDIDATES=()
  _PY_SEEN=""
  local v bin

  if [[ "$include_python_bin" == "1" && -n "${PYTHON_BIN:-}" ]]; then
    _py_add_candidate "$PYTHON_BIN"
  fi

  for v in 15 14 13 12 11 10 9 8 7 6; do
    _py_add_candidate "python3.${v}"
  done

  for bin in ${MONITOR_PYTHON_LOCAL_GLOBS:-/usr/local/python3.*/bin/python3 /usr/local/python3.*/bin/python3.*}; do
    [[ -e "$bin" && -x "$bin" ]] || continue
    _py_add_candidate "$bin"
  done

  _py_add_candidate "python3"
  _py_add_candidate "python"
}

resolve_python() {
  local major="${1:-$MONITOR_PY_MIN_MAJOR}"
  local minor="${2:-$MONITOR_PY_MIN_MINOR}"
  local bin line best_key="00000000" key ver ld
  PY=""
  PY_VERSION=""
  PY_LD_LIBRARY_PATH=""
  MONITOR_PY_MIN_MAJOR="$major"
  MONITOR_PY_MIN_MINOR="$minor"

  # 1. PYTHON_BIN 可用则直接用
  if [[ -n "${PYTHON_BIN:-}" ]]; then
    if line="$(_py_try_bin "$PYTHON_BIN" "$major" "$minor")"; then
      IFS=$'\t' read -r PY PY_VERSION PY_LD_LIBRARY_PATH <<<"$line"
      return 0
    fi
    echo "[info] PYTHON_BIN=${PYTHON_BIN} 不可用或版本低于 ${major}.${minor}，继续查找其它解释器" >&2
  fi

  # 2. 其余候选：选版本最高的可用解释器
  _py_collect_candidates 0
  for bin in "${_PY_CANDIDATES[@]+"${_PY_CANDIDATES[@]}"}"; do
    line="$(_py_try_bin "$bin" "$major" "$minor")" || continue
    IFS=$'\t' read -r bin ver ld <<<"$line"
    key="$(_py_ver_key "$ver")"
    if [[ "$key" > "$best_key" ]]; then
      best_key="$key"
      PY="$bin"
      PY_VERSION="$ver"
      PY_LD_LIBRARY_PATH="$ld"
    fi
  done
  [[ -n "$PY" ]]
}

install_python3_best_effort() {
  echo "==> 未找到可用 Python >= ${MONITOR_PY_MIN_MAJOR}.${MONITOR_PY_MIN_MINOR}，尝试安装 python3 ..."
  if [[ "$(id -u)" -ne 0 ]]; then
    echo "[info] 非 root，无法自动安装 python3"
    return 1
  fi
  if command -v apt-get >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get update -y \
      && DEBIAN_FRONTEND=noninteractive apt-get install -y python3
  elif command -v dnf >/dev/null 2>&1; then
    dnf install -y python3
  elif command -v yum >/dev/null 2>&1; then
    yum install -y python3
  else
    echo "[warn] 未识别到 apt-get / dnf / yum，跳过自动安装"
    return 1
  fi
  hash -r 2>/dev/null || true
}

print_python_resolve_error() {
  local major="${1:-$MONITOR_PY_MIN_MAJOR}"
  local minor="${2:-$MONITOR_PY_MIN_MINOR}"
  cat <<EOF
错误: 未找到可用的 Python >= ${major}.${minor}。

已按以下顺序查找：
  1. \$PYTHON_BIN（当前: ${PYTHON_BIN:-未设置}）
  2. PATH 中的 python3.15 … python3.6
  3. /usr/local/python3.*/bin/python3
  4. PATH 中的 python3 / python
  5. （可选）root 下 apt/yum/dnf 安装 python3 后再探测

请任选其一后重试：
  - 安装发行版包 python3
  - 把可用解释器放到 PATH，或 export PYTHON_BIN=/usr/local/python3.x/bin/python3
  - 源码安装的 Python 若启动时报缺 libpython，确认 prefix/lib 下有对应 .so
EOF
}

apply_python_ld_library_path() {
  if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
    export LD_LIBRARY_PATH="${PY_LD_LIBRARY_PATH}${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
  fi
}

log_python_choice() {
  echo "[ok] 使用 Python ${PY_VERSION}: $PY"
  if [[ -n "${PY_LD_LIBRARY_PATH:-}" ]]; then
    echo "[ok] LD_LIBRARY_PATH 前缀: $PY_LD_LIBRARY_PATH"
  fi
}

ensure_python() {
  local major="${1:-$MONITOR_PY_MIN_MAJOR}"
  local minor="${2:-$MONITOR_PY_MIN_MINOR}"
  if resolve_python "$major" "$minor"; then
    return 0
  fi
  if [[ "${MONITOR_INSTALL_PYTHON:-0}" == "1" ]]; then
    if install_python3_best_effort; then
      if resolve_python "$major" "$minor"; then
        return 0
      fi
    fi
  fi
  print_python_resolve_error "$major" "$minor"
  return 1
}
