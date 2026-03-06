#!/bin/bash

# Walnut namespace guard — only fire inside an ALIVE world
find_world() {
  local dir="${CLAUDE_PROJECT_DIR:-$PWD}"
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/01_Archive" ] && [ -d "$dir/02_Life" ]; then return 0; fi
    dir="$(dirname "$dir")"
  done
  return 1
}
find_world || exit 0

# Hook 2: Log Guardian — PreToolUse (Edit|Write)
# Blocks edits to signed log entries. Blocks all Write to log.md.
# Allows prepending new entries and updating frontmatter.

INPUT=$(cat)
TOOL_NAME=$(echo "$INPUT" | jq -r '.tool_name // empty')
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only care about log.md files inside _core/ (not templates, not other log files)
if ! echo "$FILE_PATH" | grep -q '_core/log\.md$'; then
  exit 0
fi

# Block ALL Write operations to log.md (must use Edit to prepend)
if [ "$TOOL_NAME" = "Write" ]; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"log.md cannot be overwritten. Use Edit to prepend new entries after the YAML frontmatter."}}'
  exit 0
fi

# For Edit: check if the old_string contains a signed entry
OLD_STRING=$(echo "$INPUT" | jq -r '.tool_input.old_string // empty')

if echo "$OLD_STRING" | grep -q 'signed: squirrel:'; then
  echo '{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"log.md is immutable. That entry is signed — add a correction entry instead. The signed entry cannot be modified."}}'
  exit 0
fi

# Allow: frontmatter updates and new entry prepends
exit 0
