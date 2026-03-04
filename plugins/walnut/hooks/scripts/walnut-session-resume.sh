#!/bin/bash
# Hook 1b: Session Resume — SessionStart (resume)
# Reads existing squirrel entry from .home/_squirrels/, re-injects context.

set -euo pipefail

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

# Find the most recent unsigned squirrel entry in .home/_squirrels/
SQUIRRELS_DIR="$WORLD_ROOT/.home/_squirrels"
LATEST_ENTRY=""
if [ -d "$SQUIRRELS_DIR" ]; then
  LATEST_ENTRY=$(grep -rl 'signed: false' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1)
fi

if [ -n "$LATEST_ENTRY" ]; then
  SESSION_ID=$(grep 'session_id:' "$LATEST_ENTRY" | awk '{print $2}')
  WALNUT=$(grep 'walnut:' "$LATEST_ENTRY" | awk '{print $2}')
  STASH=$(grep -A 100 'stash:' "$LATEST_ENTRY" | head -50)

  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "WALNUT_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
    echo "WALNUT_WORLD_ROOT=$WORLD_ROOT" >> "$CLAUDE_ENV_FILE"
  fi

  cat << EOF
ALIVE session resumed. Session ID: $SESSION_ID
Walnut: ${WALNUT:-none}
Previous stash recovered from squirrel entry:
$STASH
EOF
else
  echo "ALIVE session resumed. No unsigned entries found — clean start."
fi
