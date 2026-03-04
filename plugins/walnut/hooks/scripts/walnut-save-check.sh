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
# Throttled warning about unsaved stash items. Max once per 10 minutes.

INPUT=$(cat)

# If we're already in a forced continuation, let it go
STOP_ACTIVE=$(echo "$INPUT" | jq -r '.stop_hook_active // false')
if [ "$STOP_ACTIVE" = "true" ]; then
  exit 0
fi

# Throttle: check if we warned recently (last 10 minutes)
WARN_FILE="/tmp/walnut-save-check-$(echo "$INPUT" | jq -r '.session_id // "default"')"
if [ -f "$WARN_FILE" ]; then
  LAST_WARN=$(cat "$WARN_FILE")
  NOW=$(date +%s)
  DIFF=$((NOW - LAST_WARN))
  if [ "$DIFF" -lt 600 ]; then
    exit 0
  fi
fi

# Check .home/_squirrels/ for unsigned entry
SESSION_ID=$(echo "$INPUT" | jq -r '.session_id // empty')
SQUIRRELS_DIR="$WORLD_ROOT/.home/_squirrels"
ENTRY=""

if [ -n "$SESSION_ID" ] && [ -d "$SQUIRRELS_DIR" ]; then
  ENTRY="$SQUIRRELS_DIR/${SESSION_ID}.yaml"
  [ ! -f "$ENTRY" ] && ENTRY=""
fi

# If no entry found, try grep for any unsigned
if [ -z "$ENTRY" ] && [ -d "$SQUIRRELS_DIR" ]; then
  ENTRY=$(grep -rl 'signed: false' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1)
fi

# If no entry found or entry is already signed, allow stop
if [ -z "$ENTRY" ] || grep -q 'signed: true' "$ENTRY" 2>/dev/null; then
  exit 0
fi

# Record warning timestamp
date +%s > "$WARN_FILE"

# Block and remind
echo "{\"decision\":\"block\",\"reason\":\"🐿️ Stash items may not be saved. Say 'save' to route them, or 'exit' to leave.\"}"
exit 0
