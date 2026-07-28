#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f config/agent.json ]]; then
  cp config/agent.example.json config/agent.json
fi
export PYTHONPATH=.
exec python3 -m agent --config config/agent.json "$@"
