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

# Hook 7: Reference Companion Check — PostToolUse (Write)
# Checks that companion files in _references/ have description: in frontmatter.
# Silent for non-reference writes.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# Only care about .md files in _references/ (companions)
if ! echo "$FILE_PATH" | grep -q '_references/.*\.md$'; then
  exit 0
fi

# Skip raw/ files
if echo "$FILE_PATH" | grep -q '/raw/'; then
  exit 0
fi

# Check if companion has description: in frontmatter
if [ -f "$FILE_PATH" ] && head -15 "$FILE_PATH" | grep -q 'description:'; then
  exit 0
fi

# Remind — companion needs description
jq -n '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: "Reference companion written but missing description: in frontmatter. Add a one-line description so this reference is scannable."
  }
}'
exit 0
