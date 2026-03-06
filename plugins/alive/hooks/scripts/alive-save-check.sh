#!/bin/bash

# Walnut namespace guard — only fire inside an ALIVE world
find_world() {
  local dir="${CLAUDE_PROJECT_DIR:-$PWD}"
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/01_Archive" ] && [ -d "$dir/02_Life" ]; then
      WORLD_ROOT="$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}
find_world || exit 0

# Hook 5: Save Check — Stop
# Throttled warning about unsaved stash items. Max once per 20 minutes.
# Throttle is per-world, not per-session, to prevent UUID mismatch issues.

INPUT=$(cat)

# If we're already in a forced continuation, let it go
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  exit 0
fi

# Throttle: per-world, 20 minutes
WARN_FILE="/tmp/alive-save-check-$(echo "$WORLD_ROOT" | md5sum 2>/dev/null | cut -c1-8 || md5 -q -s "$WORLD_ROOT" | cut -c1-8)"
if [ -f "$WARN_FILE" ]; then
  LAST_WARN=$(cat "$WARN_FILE")
  NOW=$(date +%s)
  DIFF=$((NOW - LAST_WARN))
  if [ "$DIFF" -lt 1200 ]; then
    exit 0
  fi
fi

# Check .alive/_squirrels/ for any unsigned entry
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
ENTRY=""
if [ -d "$SQUIRRELS_DIR" ]; then
  ENTRY=$(grep -rl 'ended: null' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1)
fi

# If no unsaved entry, allow stop
if [ -z "$ENTRY" ]; then
  exit 0
fi

# Record warning timestamp
date +%s > "$WARN_FILE"

# Block and remind
echo "{\"decision\":\"block\",\"reason\":\"🐿️ Stash items may not be saved. Say 'save' to route them, or 'exit' to leave.\"}"
exit 0
