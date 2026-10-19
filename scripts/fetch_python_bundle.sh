#!/usr/bin/env bash
# 在有互联网的机器上下载固定版本的便携 CPython，再拷到中心。
# 中心和目标机都不访问外网。不要把 tar.gz 提交进 git。
#
# 用法:
#   scripts/fetch_python_bundle.sh
#   scripts/fetch_python_bundle.sh --dest /opt/monitor/vendor/python
#
# 下载后把目录拷到中心的 /opt/monitor/vendor/python/
# （或 center.json 里 deploy.python_bundles 指向的目录）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$ROOT/vendor/python"
MANIFEST="$ROOT/vendor/python/bundles.json"

usage() {
  cat <<EOF
用法: $0 [--dest DIR]

从 bundles.json 下载 python-build-standalone install_only（glibc >= 2.17），
并按清单里的 sha256 校验。默认目录是仓库的 vendor/python/。

离线放到中心（远端目录不存在时会先 mkdir -p）:
  scripts/push_python_bundles.sh <中心>
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest) DEST="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1"; usage; exit 1 ;;
  esac
done

if [[ ! -f "$MANIFEST" ]]; then
  echo "缺少清单: $MANIFEST"
  exit 1
fi
command -v python3 >/dev/null 2>&1 || { echo "需要 python3 来读清单"; exit 1; }

mkdir -p "$DEST"
if [[ "$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")" != "$(cd "$DEST" && pwd)/bundles.json" ]]; then
  cp -a "$MANIFEST" "$DEST/bundles.json"
fi

rows="$(python3 - "$MANIFEST" <<'PY'
import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
for _key, item in data["arches"].items():
    print("%s\t%s\t%s" % (item["url"], item["filename"], item["sha256"]))
PY
)"

while IFS=$'\t' read -r url filename sha; do
  [[ -n "${filename:-}" ]] || continue
  dest_file="$DEST/$filename"
  if [[ "${MONITOR_FETCH_PYTHON_DRY:-0}" == "1" ]]; then
    printf '%s %s %s\n' "$url" "$filename" "$sha"
    continue
  fi
  echo "==> 下载 $filename"
  curl -fL --retry 3 -o "$dest_file" "$url"
  got="$(sha256sum "$dest_file" | awk '{print $1}')"
  if [[ "$got" != "$sha" ]]; then
    echo "校验失败: $filename"
    echo "期望 $sha"
    echo "实际 $got"
    rm -f "$dest_file"
    exit 1
  fi
  printf '%s  %s\n' "$sha" "$filename" > "$DEST/$filename.sha256"
  echo "[ok] $filename"
done <<<"$rows"

if [[ "${MONITOR_FETCH_PYTHON_DRY:-0}" == "1" ]]; then
  exit 0
fi
echo
echo "已放到 $DEST"
echo "拷到中心: scripts/push_python_bundles.sh <中心> \"$DEST\""
echo "或在 config/center.json 设置 deploy.python_bundles 指向该目录。"
