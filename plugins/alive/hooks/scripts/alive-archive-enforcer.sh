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

# Hook 3: Archive Enforcer — PreToolUse (Bash)
# Blocks rm/rmdir/unlink when targeting files inside the ALIVE world.

INPUT=$(cat)
COMMAND=$(echo "$INPUT" | jq -r '.tool_input.command // empty')

# Check for destructive commands
if ! echo "$COMMAND" | grep -qE '(^|\s|;|&&|\|)(rm|rmdir|unlink)\s'; then
  exit 0
fi

# Extract target paths after the rm/rmdir/unlink command
TARGET=$(echo "$COMMAND" | sed -E 's/.*\b(rm|rmdir|unlink)\s+(-[^ ]+ )*//' | tr ' ' '\n' | grep -v '^-')

# Resolve each target and check if it's inside the World root
while IFS= read -r path; do
  [ -z "$path" ] && continue

  # Resolve relative paths against WORLD_ROOT
  if [[ "$path" != /* ]]; then
    resolved="$WORLD_ROOT/$path"
  else
    resolved="$path"
  fi

  # Check if resolved path is inside the World
  case "$resolved" in
    "$WORLD_ROOT"/01_Archive/*|"$WORLD_ROOT"/02_Life/*|"$WORLD_ROOT"/03_Inputs/*|"$WORLD_ROOT"/04_Ventures/*|"$WORLD_ROOT"/05_Experiments/*|"$WORLD_ROOT"/_core/*|"$WORLD_ROOT"/.alive/*)
      echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"🐿️ Deletion blocked inside ALIVE folders. Archive instead — move to 01_Archive/."}}'
      exit 0
      ;;
  esac
done <<< "$TARGET"

exit 0
