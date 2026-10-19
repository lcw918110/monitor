#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p data
if [[ ! -f config/center.json ]]; then
  cp config/center.example.json config/center.json
fi
# shellcheck source=lib/resolve_python.sh
. "$ROOT/scripts/lib/resolve_python.sh"
MONITOR_INSTALL_PYTHON=0
ensure_python "$MONITOR_CENTER_PY_MIN_MAJOR" "$MONITOR_CENTER_PY_MIN_MINOR" || exit 1
apply_python_ld_library_path
export PYTHONPATH=.
exec "$PY" -m center --config config/center.json
