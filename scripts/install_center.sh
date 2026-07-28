#!/usr/bin/env bash
# 兼容旧入口：转发到一键部署脚本
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/scripts/deploy_center.sh" "$@"
