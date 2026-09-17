#!/usr/bin/env bash
# 兼容旧入口：转发到产品路径 deploy_agent.sh（不是第二条安装方式）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [[ $# -eq 0 ]]; then
  echo "请提供中心地址，例如："
  echo "  $0 --center-url http://10.0.0.8:8080"
  exit 1
fi
exec "$ROOT/scripts/deploy_agent.sh" "$@"
