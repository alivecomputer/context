#!/bin/bash
# Hook: GitHub Star Prompt -- SessionStart (startup, resume)
# Calls star_prompt.py session-start and emits its stdout as additionalContext.
# Never blocks: any failure goes to .alive/logs/star-prompt.log and exits 0.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

if ! find_world; then
  exit 0
fi

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
SCRIPT="$PLUGIN_ROOT/scripts/star_prompt.py"

if [ ! -f "$SCRIPT" ]; then
  exit 0
fi

mkdir -p "$WORLD_ROOT/.alive/logs" 2>/dev/null || true
LOGFILE="$WORLD_ROOT/.alive/logs/star-prompt.log"

OUTPUT="$(python3 "$SCRIPT" session-start --world "$WORLD_ROOT" 2>>"$LOGFILE" || true)"

if [ -n "$OUTPUT" ]; then
  printf '%s\n' "{\"hookSpecificOutput\":{\"hookEventName\":\"SessionStart\",\"additionalContext\":$(printf '%s' "$OUTPUT" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')}}"
fi

exit 0
