#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
if [[ ! -f config/center.json ]]; then
  cp config/center.example.json config/center.json
fi
export PYTHONPATH=.
exec python3 -m center --config config/center.json
