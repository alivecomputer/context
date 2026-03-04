#!/bin/bash
# Hook 1a: Session New — SessionStart (startup)
# Creates squirrel entry in .home/_squirrels/, reads preferences, sets env vars.

set -euo pipefail

# Find the ALIVE world root by walking up from PWD
find_world() {
  local dir="$PWD"
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/01_Archive" ] && [ -d "$dir/02_Life" ]; then
      echo "$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}

WORLD_ROOT=$(find_world) || { echo "No ALIVE world found."; exit 0; }

# Generate session ID (short hash)
SESSION_ID=$(head -c 16 /dev/urandom | shasum | head -c 8)
TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S")
MODEL="${CLAUDE_MODEL:-unknown}"

# Set env vars via CLAUDE_ENV_FILE if available
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "WALNUT_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
  echo "WALNUT_WORLD_ROOT=$WORLD_ROOT" >> "$CLAUDE_ENV_FILE"
fi

# Always write squirrel entry to .home/_squirrels/
SQUIRRELS_DIR="$WORLD_ROOT/.home/_squirrels"
mkdir -p "$SQUIRRELS_DIR"
ENTRY_FILE="$SQUIRRELS_DIR/$SESSION_ID.yaml"
cat > "$ENTRY_FILE" << EOF
session_id: $SESSION_ID
runtime_id: squirrel.core@0.2
engine: $MODEL
walnut: null
started: $TIMESTAMP
ended: null
signed: false
stash: []
working: []
EOF

# Read preferences
PREFS_FILE="$WORLD_ROOT/.home/preferences.yaml"
if [ -f "$PREFS_FILE" ]; then
  PREFS=$(cat "$PREFS_FILE")
else
  # Fallback to .claude/ location (pre-migration)
  PREFS_FILE="$WORLD_ROOT/.claude/preferences.yaml"
  if [ -f "$PREFS_FILE" ]; then
    PREFS=$(cat "$PREFS_FILE")
  else
    PREFS="defaults (no preferences.yaml found)"
  fi
fi

# Check rule staleness (compare plugin version vs project rules)
RULES_STATUS="ok"
# TODO: version comparison logic

# Output context for Claude (stdout is added to conversation)
cat << EOF
ALIVE session initialized. Session ID: $SESSION_ID
World: $WORLD_ROOT
Walnut: none detected
Preferences: $PREFS
Rules: $RULES_STATUS
EOF
