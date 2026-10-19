#!/usr/bin/env bash
# 组装非交互 SSH 命令。push_upgrade.sh 与 push_python_bundles.sh 共用。
#
#   --ssh-cmd 传入后写到 MONITOR_SSH_CMD_OVERRIDE，原样按空白拆开，不再包 sshpass。
#   设置了 SSHPASS 且本机有 sshpass 时，用 sshpass -e ssh（密码留在环境变量里，不进命令行）。
#   否则就是 ssh。
#
# shellcheck shell=bash

monitor_init_ssh() {
  SSH_ARGV=()
  if [[ -n "${MONITOR_SSH_CMD_OVERRIDE:-}" ]]; then
    # shellcheck disable=SC2206
    SSH_ARGV=(${MONITOR_SSH_CMD_OVERRIDE})
    if [[ "${#SSH_ARGV[@]}" -eq 0 ]]; then
      echo "--ssh-cmd 是空的" >&2
      return 1
    fi
    return 0
  fi
  if [[ -n "${SSHPASS:-}" ]]; then
    if command -v sshpass >/dev/null 2>&1; then
      SSH_ARGV=(sshpass -e ssh)
      return 0
    fi
    echo "已设置 SSHPASS，但本机没有 sshpass，无法非交互登录" >&2
    return 1
  fi
  SSH_ARGV=(ssh)
}

# 把本地目录同步到远端。目录里没有 *.tar.gz 时返回 2（调用方可以跳过）。
# 先在远端 mkdir -p，避免 /opt/monitor/vendor/python 的父目录不存在时 scp 失败。
monitor_push_bundles() {
  local target="$1"
  local local_dir="$2"
  local remote_dir="${3:-/opt/monitor/vendor/python}"
  local found="" f
  if [[ -z "$target" || -z "$local_dir" || ! -d "$local_dir" ]]; then
    echo "便携包目录不存在: ${local_dir:-未指定}" >&2
    return 1
  fi
  if [[ "${#SSH_ARGV[@]}" -eq 0 ]]; then
    echo "SSH 命令还没有初始化" >&2
    return 1
  fi
  for f in "$local_dir"/*.tar.gz; do
    [[ -f "$f" ]] || continue
    found=1
    break
  done
  if [[ -z "$found" ]]; then
    return 2
  fi
  "${SSH_ARGV[@]}" "$target" "mkdir -p '$remote_dir'"
  # macOS tar 默认把 AppleDouble / com.apple.provenance 写进包，Linux tar 会报
  # unknown extended header。COPYFILE_DISABLE 关掉 copyfile；--no-xattrs /
  # --disable-copyfile 只在当前 tar 支持时加上。
  _bundle_tar_to_stdout "$local_dir" | "${SSH_ARGV[@]}" "$target" "tar -xf - -C '$remote_dir'"
}

_bundle_tar_to_stdout() {
  local src="$1"
  local help_text=""
  local -a args
  args=()
  help_text="$(tar --help 2>&1 || true)"
  if printf '%s\n' "$help_text" | grep -F -q -- '--no-xattrs'; then
    args+=(--no-xattrs)
  fi
  if printf '%s\n' "$help_text" | grep -F -q -- '--disable-copyfile'; then
    args+=(--disable-copyfile)
  fi
  args+=(-C "$src" -cf - .)
  COPYFILE_DISABLE=1 tar "${args[@]}"
}
