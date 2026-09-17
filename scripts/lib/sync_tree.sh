#!/usr/bin/env bash
# 把源目录同步到目标目录：优先 rsync，缺失时静默回退 tar / cp -a。
# 供 deploy_agent.sh / deploy_center.sh 使用（精简主机常无 rsync）。
#
# 用法: sync_tree SRC DST
# 成功后在 stdout 打一行: [sync] method=rsync|tar|cp|inplace
#
# 排除项与历史 rsync 调用保持一致：
#   data/  config/center.json  config/agent.json  .git/  .deploy-*/  __pycache__/  *.db
#
# 测试用环境变量（生产勿设）：
#   MONITOR_SYNC_DISABLE_RSYNC=1  假装没有 rsync
#   MONITOR_SYNC_DISABLE_TAR=1    假装没有 tar
#
# shellcheck shell=bash

_sync_abspath() {
  local p="$1"
  (cd "$p" 2>/dev/null && pwd)
}

_sync_has_rsync() {
  if [[ -n "${MONITOR_SYNC_DISABLE_RSYNC:-}" ]]; then
    return 1
  fi
  command -v rsync >/dev/null 2>&1
}

_sync_has_tar() {
  if [[ -n "${MONITOR_SYNC_DISABLE_TAR:-}" ]]; then
    return 1
  fi
  command -v tar >/dev/null 2>&1
}

_sync_tar() {
  local src_abs="$1"
  local dst_abs="$2"
  # GNU / BSD / BusyBox tar 均支持 --exclude
  tar -C "$src_abs" \
    --exclude=data \
    --exclude=config/center.json \
    --exclude=config/agent.json \
    --exclude=.git \
    --exclude=.deploy-\* \
    --exclude=__pycache__ \
    --exclude=\*.db \
    -cf - . | tar -C "$dst_abs" -xf -
}

_sync_copy_filtered() {
  local src="$1"
  local dst="$2"
  local item base cf cbase
  shopt -s nullglob
  for item in "$src"/*; do
    base="$(basename "$item")"
    case "$base" in
      data|.git|__pycache__) continue ;;
      .deploy-*) continue ;;
      *.db) continue ;;
    esac
    if [[ "$base" == "config" && -d "$item" ]]; then
      mkdir -p "$dst/config"
      for cf in "$item"/*; do
        [[ -e "$cf" ]] || continue
        cbase="$(basename "$cf")"
        if [[ "$cbase" == "center.json" || "$cbase" == "agent.json" ]]; then
          continue
        fi
        cp -a "$cf" "$dst/config/"
      done
      continue
    fi
    rm -rf "$dst/$base"
    cp -a "$item" "$dst/"
  done
}

sync_tree() {
  local src="${1:-}"
  local dst="${2:-}"
  if [[ -z "$src" || -z "$dst" ]]; then
    echo "[sync] 需要 SRC DST" >&2
    return 2
  fi
  if [[ ! -d "$src" ]]; then
    echo "[sync] 源目录不存在: $src" >&2
    return 1
  fi
  mkdir -p "$dst"

  local src_abs dst_abs
  src_abs="$(_sync_abspath "$src")"
  dst_abs="$(_sync_abspath "$dst")"
  if [[ -z "$src_abs" || -z "$dst_abs" ]]; then
    echo "[sync] 无法解析目录路径" >&2
    return 1
  fi

  if _sync_has_rsync; then
    rsync -a --delete \
      --exclude 'data/' \
      --exclude 'config/center.json' \
      --exclude 'config/agent.json' \
      --exclude '.git/' \
      --exclude '.deploy-*/' \
      --exclude '__pycache__/' \
      --exclude '*.db' \
      "$src_abs/" "$dst_abs/"
    echo "[sync] method=rsync"
    return 0
  fi

  # 远端 tar 展开后 ROOT==INSTALL_DIR：无 rsync 时无需再拷一份
  if [[ "$src_abs" == "$dst_abs" ]]; then
    echo "[sync] method=inplace"
    return 0
  fi

  if _sync_has_tar; then
    if _sync_tar "$src_abs" "$dst_abs"; then
      echo "[sync] method=tar"
      return 0
    fi
    echo "[sync] tar 回退失败，改用 cp -a" >&2
  fi

  if command -v cp >/dev/null 2>&1; then
    _sync_copy_filtered "$src_abs" "$dst_abs"
    echo "[sync] method=cp"
    return 0
  fi

  echo "[fail] sync_tool_missing: 本机无 rsync/tar/cp，无法同步代码" >&2
  return 1
}

# 产品打包从中心安装树读取 agent/。源不完整时 rsync --delete 会把目标机上的 agent 删掉。
assert_package_tree() {
  local root="${1:-}"
  local label="${2:-tree}"
  if [[ -z "$root" ]]; then
    echo "[fail] package_incomplete: 未提供目录" >&2
    return 1
  fi
  if [[ ! -f "$root/agent/__init__.py" ]]; then
    echo "[fail] package_incomplete: ${label} 缺少 agent/__init__.py（中心安装目录必须保留 agent 包）" >&2
    return 1
  fi
  if [[ ! -d "$root/common" ]]; then
    echo "[fail] package_incomplete: ${label} 缺少 common/" >&2
    return 1
  fi
  if [[ ! -d "$root/scripts" ]]; then
    echo "[fail] package_incomplete: ${label} 缺少 scripts/" >&2
    return 1
  fi
  return 0
}
