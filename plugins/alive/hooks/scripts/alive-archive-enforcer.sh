#!/bin/bash

# Hook 3: Archive Enforcer — PreToolUse (Bash)
# Blocks rm/rmdir/unlink when targeting files inside an ALIVE world.
# Works regardless of PWD — checks both walk-up and config file for World root.

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

# Try walk-up first, then config file fallback
WORLD_ROOT=$(find_world 2>/dev/null) || WORLD_ROOT=""
if [ -z "$WORLD_ROOT" ]; then
  CONFIG_FILE="$HOME/.config/walnut/world-root"
  if [ -f "$CONFIG_FILE" ]; then
    WORLD_ROOT=$(cat "$CONFIG_FILE" 2>/dev/null | head -1)
    if [ ! -d "$WORLD_ROOT/01_Archive" ] || [ ! -d "$WORLD_ROOT/02_Life" ]; then
      WORLD_ROOT=""
    fi
  fi
fi

# No World found — nothing to protect
[ -z "$WORLD_ROOT" ] && exit 0

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Check for destructive commands
if ! echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|)(rm|rmdir|unlink)\s'; then
  exit 0
fi

# Extract target paths after the rm/rmdir/unlink command
TARGET=$(echo "$COMMAND" | sed -E 's/.*\b(rm|rmdir|unlink)\s+(-[^ ]+ )*//' | tr ' ' '\n' | grep -v '^-')

while IFS= read -r path; do
  [ -z "$path" ] && continue

  # Expand ~ to $HOME
  path="${path/#\~/$HOME}"

  # Resolve relative paths against PWD
  if [[ "$path" != /* ]]; then
    path="${PWD}/${path}"
  fi

  # Normalise (remove trailing slashes, resolve . and ..)
  resolved=$(cd "$(dirname "$path")" 2>/dev/null && echo "$(pwd)/$(basename "$path")" || echo "$path")

  # Block if the target IS the World root
  if [ "$resolved" = "$WORLD_ROOT" ]; then
    echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"🐿️ Deletion of the World root blocked. This would destroy your entire ALIVE system."}}'
    exit 0
  fi

  # Block if the target is inside the World
  case "$resolved" in
    "$WORLD_ROOT"/*)
      echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"🐿️ Deletion blocked inside ALIVE world. Archive instead — move to 01_Archive/."}}'
      exit 0
      ;;
  esac
done <<< "$TARGET"

exit 0
