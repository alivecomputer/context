#!/bin/bash
# Hook: Root Guardian -- PreToolUse (Edit|Write)
# Blocks writes to the world root that aren't domain folders or hidden files.
# Prompts the squirrel to figure out where the file should go instead.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-PreToolUse}"; then
  exit 0
fi

FILE_PATH=$(json_field "tool_input.file_path")
[ -z "$FILE_PATH" ] && exit 0

# Only care about files at the world root (direct children)
FILE_DIR=$(dirname "$FILE_PATH")
FILE_NAME=$(basename "$FILE_PATH")

# Normalize: resolve the file's parent directory
# If the file's directory IS the world root, check it
if [ "$FILE_DIR" != "$WORLD_ROOT" ]; then
  exit 0
fi

# Allow: hidden files/folders (start with .)
if [[ "$FILE_NAME" == .* ]]; then
  exit 0
fi

# Allow: domain folders
case "$FILE_NAME" in
  01_Archive|02_Life|03_Inbox|04_Ventures|05_Experiments)
    exit 0
    ;;
esac

# Allow: Icon (macOS folder icon)
if [ "$FILE_NAME" = "Icon" ] || [ "$FILE_NAME" = $'Icon\r' ]; then
  exit 0
fi

# Block everything else -- tell the squirrel where things should go
REASON="Cannot write '${FILE_NAME}' to the world root. Nothing lives at root except the 5 domain folders (01_Archive through 05_Experiments) and hidden files. Route this to the right place: if it belongs to a walnut, put it in that walnut's _kernel/ or as a deliverable in the walnut root. If it's an input, put it in 03_Inbox/. If it's a new project, create a walnut with alive:create-walnut. Ask the human where it should go."

ESCAPED_REASON=$(escape_for_json "$REASON")
echo "{\"hookSpecificOutput\":{\"hookEventName\":\"PreToolUse\",\"permissionDecision\":\"deny\",\"permissionDecisionReason\":\"${ESCAPED_REASON}\"}}"
exit 0
