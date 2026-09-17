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

# 产品打包从中心安装树读取 agent/。源不完整时禁止同步。
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

_sync_agent_config() {
  local src="$1"
  local dst="$2"
  local cf cbase
  [[ -d "$src/config" ]] || return 0
  mkdir -p "$dst/config"
  shopt -s nullglob
  for cf in "$src/config"/*; do
    [[ -e "$cf" ]] || continue
    cbase="$(basename "$cf")"
    if [[ "$cbase" == "center.json" || "$cbase" == "agent.json" ]]; then
      continue
    fi
    cp -a "$cf" "$dst/config/"
  done
}

# Agent 安装只同步采集端：agent/ common/ scripts/ 与配置模板。
# 不要把整棵中心树（含 center/）rsync --delete 进 /opt/monitor-agent。
sync_agent_tree() {
  local src="${1:-}"
  local dst="${2:-}"
  local src_abs dst_abs name
  if [[ -z "$src" || -z "$dst" ]]; then
    echo "[sync] 需要 SRC DST" >&2
    return 2
  fi
  if ! assert_package_tree "$src" "源码 $src"; then
    return 1
  fi
  mkdir -p "$dst"
  src_abs="$(_sync_abspath "$src")"
  dst_abs="$(_sync_abspath "$dst")"
  if [[ -z "$src_abs" || -z "$dst_abs" ]]; then
    echo "[sync] 无法解析目录路径" >&2
    return 1
  fi

  if [[ "$src_abs" == "$dst_abs" ]]; then
    # 无独立源树时无法对 agent/ 做文件级 --delete；仍清 junk / 多余顶层目录
    echo "[sync] method=inplace"
    prune_agent_install_dir "$dst_abs"
    return 0
  fi

  if _sync_has_rsync; then
    for name in agent common scripts; do
      rsync -a --delete \
        --exclude '__pycache__/' \
        --exclude '._*' \
        --exclude '.DS_Store' \
        --exclude '*.db' \
        "$src_abs/$name/" "$dst_abs/$name/"
    done
    _sync_agent_config "$src_abs" "$dst_abs"
    if [[ -f "$src_abs/README.md" ]]; then
      cp -a "$src_abs/README.md" "$dst_abs/README.md"
    fi
    prune_agent_install_dir "$dst_abs"
    echo "[sync] method=rsync"
    return 0
  fi

  if _sync_has_tar; then
    local names=(agent common scripts)
    [[ -d "$src_abs/config" ]] && names+=(config)
    [[ -f "$src_abs/README.md" ]] && names+=(README.md)
    for name in agent common scripts; do
      rm -rf "$dst_abs/$name"
    done
    if tar -C "$src_abs" \
      --exclude=config/center.json \
      --exclude=config/agent.json \
      --exclude=__pycache__ \
      --exclude=\._\* \
      --exclude=\*.db \
      -cf - "${names[@]}" | tar -C "$dst_abs" -xf -; then
      prune_agent_install_dir "$dst_abs"
      echo "[sync] method=tar"
      return 0
    fi
    echo "[sync] tar 回退失败，改用 cp -a" >&2
  fi

  if command -v cp >/dev/null 2>&1; then
    for name in agent common scripts; do
      rm -rf "$dst_abs/$name"
      cp -a "$src_abs/$name" "$dst_abs/"
    done
    _sync_agent_config "$src_abs" "$dst_abs"
    if [[ -f "$src_abs/README.md" ]]; then
      cp -a "$src_abs/README.md" "$dst_abs/README.md"
    fi
    prune_agent_install_dir "$dst_abs"
    echo "[sync] method=cp"
    return 0
  fi

  echo "[fail] sync_tool_missing: 本机无 rsync/tar/cp，无法同步代码" >&2
  return 1
}

_stop_agent_pidfile() {
  local dir="$1"
  local pid=""
  [[ -n "$dir" && -f "$dir/run/agent.pid" ]] || return 0
  pid="$(tr -d ' \t\n' < "$dir/run/agent.pid" 2>/dev/null || true)"
  if [[ -n "${pid:-}" && "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "[cleanup] 停止进程 pid=$pid ($dir)"
    kill "$pid" 2>/dev/null || true
    sleep 1
    if kill -0 "$pid" 2>/dev/null; then
      kill -9 "$pid" 2>/dev/null || true
    fi
  fi
  rm -f "$dir/run/agent.pid"
}

# 覆盖安装前停旧服务/进程：systemd 单元、已知目录 pidfile、python -m agent 孤儿。
# 不停 monitor-center。不删除 ~/monitor 家目录副本（只停进程，以免误伤配置）。
stop_previous_agent() {
  local install_dir="${1:-}"
  local d
  echo "[cleanup] 停止旧 Agent，再覆盖安装目录"
  if command -v systemctl >/dev/null 2>&1; then
    systemctl stop monitor-agent >/dev/null 2>&1 || true
  fi
  for d in "$install_dir" \
           "${HOME}/monitor-agent" \
           "${HOME}/monitor" \
           /opt/monitor-agent \
           /opt/monitor; do
    [[ -n "${d:-}" && -d "$d" ]] || continue
    _stop_agent_pidfile "$d"
  done
  if [[ -z "${MONITOR_CLEANUP_SKIP_PKILL:-}" ]]; then
    pkill -f '[Pp]ython[0-9.]* -m agent' >/dev/null 2>&1 || true
  fi
}

# 同步后清理旧版本残留。保留 config/agent.json、data/、run/。
# /opt/monitor（basename=monitor）视为中心树，不删 center/。
prune_agent_install_dir() {
  local dst="${1:-}"
  local base="" name="" bn=""
  [[ -n "$dst" && -d "$dst" ]] || return 0
  echo "[cleanup] 清理旧文件（AppleDouble / __pycache__ / 多余顶层目录）"
  find "$dst" \( -name '._*' -o -name '.DS_Store' \) -type f -exec rm -f {} \; 2>/dev/null || true
  find "$dst" -type d -name '__pycache__' -prune -exec rm -rf {} \; 2>/dev/null || true
  base="$(basename "$dst")"
  for name in "$dst"/*; do
    [[ -e "$name" ]] || continue
    bn="$(basename "$name")"
    case "$bn" in
      agent|common|scripts|config|run|data|README.md) continue ;;
      center)
        if [[ "$base" == "monitor" ]]; then
          continue
        fi
        echo "[cleanup] 删除多余目录: $bn"
        rm -rf "$name"
        ;;
      *)
        echo "[cleanup] 删除旧版本残留: $bn"
        rm -rf "$name"
        ;;
    esac
  done
  for name in "$dst"/.[!.]*; do
    [[ -e "$name" ]] || continue
    bn="$(basename "$name")"
    case "$bn" in
      .|..) continue ;;
    esac
    echo "[cleanup] 删除隐藏残留: $bn"
    rm -rf "$name"
  done
}
