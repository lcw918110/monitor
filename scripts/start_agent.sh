#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ ! -f config/agent.json ]]; then
  cp config/agent.example.json config/agent.json
fi
# shellcheck source=lib/resolve_python.sh
. "$ROOT/scripts/lib/resolve_python.sh"
MONITOR_INSTALL_PYTHON=0
ensure_python 3 6 || exit 1
apply_python_ld_library_path
export PYTHONPATH=.
exec "$PY" -m agent --config config/agent.json "$@"
