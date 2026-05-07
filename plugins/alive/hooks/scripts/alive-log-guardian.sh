#!/bin/bash
# Hook: Log Guardian -- PreToolUse (Edit|Write)
# Blocks edits to signed log entries. Blocks all Write to log.md.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-PreToolUse}"; then
  exit 0
fi

TOOL_NAME=$(json_field "tool_name")
FILE_PATH=$(json_field "tool_input.file_path")

# Only care about log.md files inside walnuts (either _kernel/log.md or root-level log.md)
# Match: any path containing a walnut folder with log.md at _kernel/ or root level
if ! echo "$FILE_PATH" | grep -qE '(_kernel/log\.md$|/[^/]+/log\.md$)'; then
  exit 0
fi

# Skip if it's not inside a world
if [ -n "${WORLD_ROOT:-}" ] && ! echo "$FILE_PATH" | grep -q "^$WORLD_ROOT"; then
  exit 0
fi

# Block ALL Write operations to log.md (must use Edit to prepend)
if [ "$TOOL_NAME" = "Write" ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"log.md cannot be overwritten. Use Edit to prepend new entries after the YAML frontmatter."}}'
  exit 0
fi

# For Edit: check if the old_string contains a signed entry
OLD_STRING=$(json_field "tool_input.old_string")

if echo "$OLD_STRING" | grep -qE 'signed: (squirrel:|alive-mcp:)'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"log.md is immutable. That entry is signed -- add a correction entry instead."}}'
  exit 0
fi

exit 0
