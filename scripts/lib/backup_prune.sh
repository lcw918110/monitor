#!/usr/bin/env bash
# 备份目录按时间从旧到新排序。
# 现有两种名字都要认，不能只做字典序：
#   <安装名>-backup-YYYYMMDDHHMMSS[-pid]
#   <安装名>-backup-YYYYMMDD-HHMMSS[-pid]
# 也认 YYYY-MM-DD-HHMMSS，以及纯数字计数（测试或旧目录）。
# 认不出的名字按目录 mtime 排到已识别名字之后，避免先删掉看不懂的目录。
#
# shellcheck shell=bash

backup_sort_key() {
  local base="$1"
  local path="$2"
  local name rest ts pid mtime
  name="$(basename "$path")"
  rest="${name#"${base}-backup-"}"
  pid=0
  if [[ "$rest" =~ ^([0-9]{4})-([0-9]{2})-([0-9]{2})[-T]([0-9]{6})(-([0-9]+))?$ ]]; then
    ts="${BASH_REMATCH[1]}${BASH_REMATCH[2]}${BASH_REMATCH[3]}${BASH_REMATCH[4]}"
    pid="${BASH_REMATCH[6]:-0}"
  elif [[ "$rest" =~ ^([0-9]{8})-([0-9]{6})(-([0-9]+))?$ ]]; then
    ts="${BASH_REMATCH[1]}${BASH_REMATCH[2]}"
    pid="${BASH_REMATCH[4]:-0}"
  elif [[ "$rest" =~ ^([0-9]{14})(-([0-9]+))?$ ]]; then
    ts="${BASH_REMATCH[1]}"
    pid="${BASH_REMATCH[3]:-0}"
  elif [[ "$rest" =~ ^([0-9]+)$ ]]; then
    printf -v ts '%014d' "${BASH_REMATCH[1]}"
  else
    mtime="$(stat -c %Y "$path" 2>/dev/null || stat -f %m "$path" 2>/dev/null || echo 0)"
    printf '2 %020d %s\n' "$mtime" "$name"
    return 0
  fi
  printf '1 %s %08d\n' "$ts" "$((10#$pid))"
}

# 参数：安装目录名（basename）、若干备份路径。标准输出为从旧到新的路径。
backup_paths_oldest_first() {
  local base="$1"
  shift
  local d key
  {
    for d in "$@"; do
      [[ -n "$d" ]] || continue
      key="$(backup_sort_key "$base" "$d")"
      printf '%s\t%s\n' "$key" "$d"
    done
  } | LC_ALL=C sort | cut -f2-
}
