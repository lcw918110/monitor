#!/usr/bin/env bash
# 重写 monitor-center.service 时保留已有的自定义 Environment= 行。
# 现网单元常有 Environment=PATH=...，不能在覆盖安装时丢掉。
# PYTHONPATH 总是改成这次的安装目录。
# 若本次算出了 LD_LIBRARY_PATH，用新值；否则保留单元里已有的那一行。

collect_preserved_environment() {
  local unit="${1:-}"
  local install_dir="$2"
  local ld_path="${3:-}"
  local line key kept_ld=""
  local -a extra=()
  if [[ -n "$unit" && -f "$unit" ]]; then
    while IFS= read -r line || [[ -n "$line" ]]; do
      [[ "$line" =~ ^[[:space:]]*Environment= ]] || continue
      line="${line#"${line%%[![:space:]]*}"}"
      key="${line#Environment=}"
      key="${key%%=*}"
      key="${key#\"}"
      key="${key#\'}"
      case "$key" in
        PYTHONPATH) ;;
        LD_LIBRARY_PATH)
          if [[ -z "$ld_path" ]]; then
            kept_ld="$line"
          fi
          ;;
        *)
          extra+=("$line")
          ;;
      esac
    done < "$unit"
  fi
  printf 'Environment=PYTHONPATH=%s\n' "$install_dir"
  if [[ -n "$ld_path" ]]; then
    printf 'Environment=LD_LIBRARY_PATH=%s\n' "$ld_path"
  elif [[ -n "$kept_ld" ]]; then
    printf '%s\n' "$kept_ld"
  fi
  if [[ ${#extra[@]} -gt 0 ]]; then
    local item
    for item in "${extra[@]}"; do
      printf '%s\n' "$item"
    done
  fi
}
