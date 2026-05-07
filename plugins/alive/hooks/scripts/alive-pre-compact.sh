#!/bin/bash
# Hook: PreCompact
# Writes compaction timestamp to the current session's squirrel YAML.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path. The explicit ``if !`` form survives ``set -euo
# pipefail`` (which this hook sets above) cleanly: helper returns 1
# in-bash, we exit 0 without printing JSON ourselves.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-PreCompact}"; then
  exit 0
fi

SESSION_ID="${HOOK_SESSION_ID}"
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
[ ! -d "$SQUIRRELS_DIR" ] && exit 0

# Find entry by session_id (exact match) or fall back to most recent active
ENTRY=""
if [ -n "$SESSION_ID" ] && [ -f "$SQUIRRELS_DIR/$SESSION_ID.yaml" ]; then
  ENTRY="$SQUIRRELS_DIR/$SESSION_ID.yaml"
else
  ENTRY=$(ls -t "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | while read -r f; do
    grep -q 'ended: null' "$f" 2>/dev/null && echo "$f" && break
  done || true)
fi

[ -z "${ENTRY:-}" ] && exit 0

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S")

if ! grep -q 'compacted:' "$ENTRY"; then
  echo "compacted: $TIMESTAMP" >> "$ENTRY"
else
  if sed --version >/dev/null 2>&1; then
    sed -i "s/compacted:.*/compacted: $TIMESTAMP/" "$ENTRY"
  else
    sed -i '' "s/compacted:.*/compacted: $TIMESTAMP/" "$ENTRY"
  fi
fi

exit 0
