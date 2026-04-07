#!/usr/bin/env bash
# check_invariants.sh — runtime invariant validator for the working tree of fn-7-7cw.
#
# Distinct from the preflight base-branch gate (which validates origin/main
# via `git cat-file` before any files are created). This script runs against
# the on-disk working tree AFTER scaffolding to confirm v3 helpers are
# importable and required stubs exist. Task .13 expands the assertions; this
# is a minimal placeholder so downstream tasks have a target to wire CI to.

set -e

# Resolve plugin root from this script's location: tests/ -> plugins/alive
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Source alive-common.sh if present (provides shared logging helpers).
if [ -f "$PLUGIN_ROOT/hooks/scripts/alive-common.sh" ]; then
  # shellcheck source=/dev/null
  . "$PLUGIN_ROOT/hooks/scripts/alive-common.sh"
fi

# Invariant 1: walnut_paths.py stub exists at the locked path.
[ -f "$PLUGIN_ROOT/scripts/walnut_paths.py" ] || {
  echo "FAIL: walnut_paths.py missing at $PLUGIN_ROOT/scripts/walnut_paths.py" >&2
  exit 1
}

# Invariant 2: walnut_paths is importable (no syntax errors in the stub).
python3 -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/scripts')
import walnut_paths  # noqa: F401
" || {
  echo "FAIL: walnut_paths is not importable" >&2
  exit 1
}

# Invariant 3: v3 helpers from project.py and tasks.py are importable.
python3 -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/scripts')
from tasks import _resolve_bundle_path, _find_bundles  # noqa: F401
from project import scan_bundles, parse_manifest  # noqa: F401
" || {
  echo "FAIL: v3 helpers (tasks.py / project.py) are not importable" >&2
  exit 1
}

# Invariant 4: alive-p2p.py stub declares FORMAT_VERSION = "2.1.0" per LD6.
python3 -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/scripts')
import importlib.util
spec = importlib.util.spec_from_file_location('alive_p2p', '$PLUGIN_ROOT/scripts/alive-p2p.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
assert mod.FORMAT_VERSION == '2.1.0', f'expected FORMAT_VERSION=2.1.0, got {mod.FORMAT_VERSION}'
" || {
  echo "FAIL: alive-p2p.py FORMAT_VERSION invariant failed" >&2
  exit 1
}

echo "check_invariants: OK"
