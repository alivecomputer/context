#!/usr/bin/env bash
#
# update-snapshots.sh — the ONLY approved way to update the golden MCP
# contract snapshots.
#
# Runs ``scripts/run-inspector-snapshot.sh`` for each MCP list method
# and writes the normalized output to the corresponding golden fixture
# under ``tests/fixtures/contracts/``.
#
# CI NEVER runs this script. ``tests/test_contracts.py`` compares the
# live Inspector output to the committed golden and fails loudly if
# they diverge. When the divergence is intentional (new tool landed,
# description reworded, inputSchema tightened), a developer runs THIS
# script, re-runs the test suite to confirm green, and commits the
# updated fixtures alongside the source change.
#
# Usage
# -----
#   scripts/update-snapshots.sh         # update all three snapshots
#   scripts/update-snapshots.sh tools   # update only tools.snapshot.json
#   scripts/update-snapshots.sh -h      # print help
#
# The single-target form is useful when iterating on one contract to
# avoid blowing away unrelated snapshots that might still be drifting.

set -euo pipefail

usage() {
    cat >&2 <<'EOF'
usage: update-snapshots.sh [target]

  target   one of: tools, resources, prompts, all (default: all)

Writes normalized MCP Inspector output to the committed golden
fixtures under tests/fixtures/contracts/.

CI never runs this script. Use it locally when a contract change is
intentional, then commit the updated .snapshot.json files alongside
the source change.
EOF
}

TARGET="${1:-all}"
case "${TARGET}" in
    tools|resources|prompts|all) ;;
    -h|--help) usage; exit 0 ;;
    *)
        echo "error: unknown target: ${TARGET}" >&2
        usage
        exit 1
        ;;
esac

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CONTRACTS_DIR="${PKG_ROOT}/tests/fixtures/contracts"
mkdir -p "${CONTRACTS_DIR}"

update_one() {
    local method="$1"
    local fixture="$2"
    echo "==> updating ${fixture} (method=${method})" >&2
    # Write to a temp file first so a generator failure leaves the
    # existing golden untouched. atomic-replace on success.
    local tmp
    tmp="$(mktemp -t alive-mcp-snapshot.XXXXXX)"
    if ! "${SCRIPT_DIR}/run-inspector-snapshot.sh" "${method}" >"${tmp}"; then
        rm -f "${tmp}"
        echo "error: snapshot generator failed for ${method}" >&2
        exit 1
    fi
    mv "${tmp}" "${CONTRACTS_DIR}/${fixture}"
}

case "${TARGET}" in
    tools)     update_one "tools/list" "tools.snapshot.json" ;;
    resources) update_one "resources/list" "resources.snapshot.json" ;;
    prompts)   update_one "prompts/list" "prompts.snapshot.json" ;;
    all)
        update_one "tools/list" "tools.snapshot.json"
        update_one "resources/list" "resources.snapshot.json"
        update_one "prompts/list" "prompts.snapshot.json"
        ;;
esac

echo "==> done. review changes with 'git diff tests/fixtures/contracts/' before committing." >&2
