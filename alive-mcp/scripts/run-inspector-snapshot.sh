#!/usr/bin/env bash
#
# run-inspector-snapshot.sh — generate a normalized MCP contract snapshot.
#
# Drives the MCP Inspector CLI against the alive-mcp server running over
# the frozen fixture world (``tests/fixtures/world-basic``) and writes a
# deterministic JSON snapshot to stdout.
#
# Supported methods (positional arg, default ``tools/list``):
#   tools/list      — 10 frozen v0.1 tools
#   resources/list  — 4 kernel resources × walnut_count (12 for fixture)
#   prompts/list    — stub for v0.2 (empty list)
#
# Determinism strategy
# --------------------
# The Inspector returns JSON that is STABLE in content but NOT in key
# order. We canonicalize via ``json.tool --sort-keys`` and for list-
# shaped outputs we additionally sort each list element by its natural
# identity key (``name`` for tools, ``uri`` for resources). Prompts is
# always an empty list in v0.1. Output is indented 2 spaces with a
# trailing newline so the golden fixture is a stable, diff-friendly
# text artifact.
#
# T14 → T15 transition (dependency pinning)
# ----------------------------------------
# At T14 we invoke the Inspector via ``npx -y @modelcontextprotocol/
# inspector`` which dynamically resolves the latest version from the npm
# registry. That is fine for local dev but UNACCEPTABLE in CI — the
# no-phone-home posture in T15 forbids network access during the test
# phase. T15 pins the Inspector via a committed ``package-lock.json`` at
# the repo root and swaps this script to call
# ``./node_modules/.bin/mcp-inspector``. The rest of the pipeline
# (normalization, snapshot shape, test harness) is unchanged.
#
# Exit codes
# ----------
#   0  success, snapshot written to stdout
#   1  invalid arguments or missing toolchain (node/npx/uv)
#   2  Inspector returned non-JSON on stdout (server print contamination)
#   3  Inspector subprocess failed (non-zero exit)

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: run-inspector-snapshot.sh [method]

  method     one of tools/list, resources/list, prompts/list
             (default: tools/list)

Environment:
  ALIVE_WORLD_ROOT   optional; overrides the fixture world path.
                     Default: tests/fixtures/world-basic relative to the
                     alive-mcp package root (the parent of this script's
                     directory).
EOF
}

METHOD="${1:-tools/list}"

case "${METHOD}" in
    tools/list|resources/list|prompts/list) ;;
    -h|--help) usage; exit 0 ;;
    *)
        echo "error: unsupported method: ${METHOD}" >&2
        usage
        exit 1
        ;;
esac

# Toolchain sanity. Fail fast with a clear message rather than letting
# npx / uv surface their own errors deep in a pipeline.
for bin in npx uv python3; do
    if ! command -v "${bin}" >/dev/null 2>&1; then
        echo "error: ${bin} is required on PATH to run the Inspector snapshot generator" >&2
        exit 1
    fi
done

# Resolve alive-mcp package root (parent of this script dir) without
# depending on ``readlink -f`` which is not portable to BSD userland.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
DEFAULT_WORLD="${PKG_ROOT}/tests/fixtures/world-basic"
WORLD_ROOT="${ALIVE_WORLD_ROOT:-${DEFAULT_WORLD}}"

if [[ ! -d "${WORLD_ROOT}" ]]; then
    echo "error: world root does not exist: ${WORLD_ROOT}" >&2
    exit 1
fi

# Inspector stderr carries npm warnings and server banner noise. We
# keep it OUT of the snapshot but let it reach the terminal when run
# interactively (tests redirect stderr via Python).
RAW_JSON="$(
    cd "${PKG_ROOT}" \
    && ALIVE_WORLD_ROOT="${WORLD_ROOT}" \
       npx -y @modelcontextprotocol/inspector \
           --cli uv run alive-mcp \
           --method "${METHOD}" 2>/dev/null
)" || {
    echo "error: Inspector subprocess failed for method ${METHOD}" >&2
    exit 3
}

# Normalize via the normalize_snapshot.py helper so the logic is
# testable independently of this shell wrapper. Sort top-level list
# elements by stable identity keys and deep-sort all dict keys so the
# snapshot is deterministic across runs and across Python / SDK
# versions.
printf '%s' "${RAW_JSON}" | python3 "${SCRIPT_DIR}/normalize_snapshot.py" "${METHOD}"
