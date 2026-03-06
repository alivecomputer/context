#!/bin/bash
# Hook 1a: Session New — SessionStart (startup)
# Creates squirrel entry in .alive/_squirrels/, reads preferences, sets env vars.

set -euo pipefail

# Find the ALIVE world root by walking up from PWD
find_world() {
  local dir="${CLAUDE_PROJECT_DIR:-$PWD}"
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
  echo "ALIVE_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
  echo "ALIVE_WORLD_ROOT=$WORLD_ROOT" >> "$CLAUDE_ENV_FILE"
fi

# Always write squirrel entry to .alive/_squirrels/
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
mkdir -p "$SQUIRRELS_DIR"
ENTRY_FILE="$SQUIRRELS_DIR/$SESSION_ID.yaml"
cat > "$ENTRY_FILE" << EOF
session_id: $SESSION_ID
runtime_id: squirrel.core@0.2
engine: $MODEL
walnut: null
started: $TIMESTAMP
ended: null
saves: 0
last_saved: null
stash: []
working: []
EOF

# Resolve preferences (toggle keys → ON/OFF, non-toggle sections for LLM)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

# Check rule staleness (compare plugin version vs project rules)
RULES_STATUS="ok"
# TODO: version comparison logic

# Output context for Claude (stdout is added to conversation)
cat << EOF
ALIVE session initialized. Session ID: $SESSION_ID
World: $WORLD_ROOT
$PREFS
Rules: $RULES_STATUS
EOF
