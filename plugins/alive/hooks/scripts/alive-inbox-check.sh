#!/bin/bash
# Hook: Inbox Check — PostToolUse (Write|Edit)
# When now.md is written (typically during save), check 03_Inputs/ for unrouted items.
# If items exist, nudge the squirrel via additionalContext. Silent otherwise.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input
find_world || exit 0

# Only fire when the written file is now.md
FILE_PATH=$(echo "$HOOK_INPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
ti=d.get('tool_input',{})
print(ti.get('file_path',''))
" 2>/dev/null)

case "$FILE_PATH" in
  */now.md) ;;
  *) exit 0 ;;
esac

# Count non-system files in 03_Inputs/
INPUTS_DIR="$WORLD_ROOT/03_Inputs"
[ -d "$INPUTS_DIR" ] || exit 0

COUNT=0
WALNUT_COUNT=0
while IFS= read -r -d '' entry; do
  name="$(basename "$entry")"
  case "$name" in
    .DS_Store|.gitkeep|.keep) continue ;;
  esac
  COUNT=$((COUNT + 1))
  case "$name" in
    *.walnut|*.walnut.age) WALNUT_COUNT=$((WALNUT_COUNT + 1)) ;;
  esac
done < <(find "$INPUTS_DIR" -mindepth 1 -maxdepth 1 -print0 2>/dev/null)

[ "$COUNT" -eq 0 ] && exit 0

# Build the nudge — prioritize .walnut files with a specific suggestion
if [ "$WALNUT_COUNT" -gt 0 ]; then
  OTHER_COUNT=$((COUNT - WALNUT_COUNT))
  if [ "$OTHER_COUNT" -gt 0 ]; then
    NUDGE="Inbox has ${COUNT} item(s) in 03_Inputs/, including ${WALNUT_COUNT} shared walnut package(s). If the human isn't in the middle of something, suggest running alive:receive for the .walnut files and alive:capture for the rest."
  else
    NUDGE="Inbox has ${WALNUT_COUNT} shared walnut package(s) in 03_Inputs/. If the human isn't in the middle of something, suggest running alive:receive to import them."
  fi
else
  NUDGE="Inbox has ${COUNT} item(s) in 03_Inputs/. If the human isn't in the middle of something, suggest running alive:capture to clear the inbox."
fi
ESCAPED=$(escape_for_json "$NUDGE")

cat <<EOF
{
  "hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "additionalContext": "${ESCAPED}"
  }
}
EOF

exit 0
