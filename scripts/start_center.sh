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
ensure_python 3 8 || exit 1
apply_python_ld_library_path
export PYTHONPATH=.
exec "$PY" -m center --config config/center.json
