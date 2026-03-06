#!/bin/bash
# Hook 1a: Session New — SessionStart (startup)
# Creates squirrel entry in .alive/_squirrels/, reads preferences, sets env vars.
# Injects runtime rules (CLAUDE.md + rules/*.md) via additionalContext every session.

set -euo pipefail

PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"

# Escape string for JSON embedding — uses python3 for large strings (bash
# parameter substitution is O(n^2) on 40KB+ content, takes 100s+ vs 50ms)
escape_for_json() {
  if [ ${#1} -gt 1000 ]; then
    printf '%s' "$1" | python3 -c "import sys,json; print(json.dumps(sys.stdin.read())[1:-1], end='')"
  else
    local s="$1"
    s="${s//\\/\\\\}"
    s="${s//\"/\\\"}"
    s="${s//$'\n'/\\n}"
    s="${s//$'\r'/\\r}"
    s="${s//$'\t'/\\t}"
    printf '%s' "$s"
  fi
}

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

# Try walk-up first
WORLD_ROOT=$(find_world 2>/dev/null) || WORLD_ROOT=""

# If walk-up failed, try persistent config file
if [ -z "$WORLD_ROOT" ]; then
  CONFIG_FILE="$HOME/.config/walnut/world-root"
  if [ -f "$CONFIG_FILE" ]; then
    WORLD_ROOT=$(cat "$CONFIG_FILE" 2>/dev/null | head -1)
    if [ ! -d "$WORLD_ROOT/01_Archive" ] || [ ! -d "$WORLD_ROOT/02_Life" ]; then
      echo "No ALIVE world found."
      exit 0
    fi
  else
    echo "No ALIVE world found."
    exit 0
  fi
fi

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

# Resolve preferences (toggle keys -> ON/OFF, non-toggle sections for LLM)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

# Build session init message
SESSION_MSG="ALIVE session initialized. Session ID: $SESSION_ID
World: $WORLD_ROOT
${PREFS}
Rules: ok"

# Build runtime rules from plugin source files
RUNTIME_RULES=""

# Read CLAUDE.md (system identity)
if [ -f "$PLUGIN_ROOT/CLAUDE.md" ]; then
  RUNTIME_RULES=$(cat "$PLUGIN_ROOT/CLAUDE.md")
fi

# Read all rule files
for rule_file in "$PLUGIN_ROOT/rules/"*.md; do
  if [ -f "$rule_file" ]; then
    RUNTIME_RULES="${RUNTIME_RULES}

$(cat "$rule_file")"
  fi
done

# Preamble that frames the rules as mandatory
PREAMBLE="<EXTREMELY_IMPORTANT>
The following are your core operating rules for the ALIVE system. They define your identity, voice, behaviour, and contract with the human. They are MANDATORY — not suggestions, not defaults, not guidelines. You MUST follow them in every response, every tool call, every session. They override all other instructions except direct requests from the human in this conversation.

Read them. Internalise them. Follow them exactly.
</EXTREMELY_IMPORTANT>"

# Escape and combine
SESSION_MSG_ESCAPED=$(escape_for_json "$SESSION_MSG")
PREAMBLE_ESCAPED=$(escape_for_json "$PREAMBLE")
RUNTIME_ESCAPED=$(escape_for_json "$RUNTIME_RULES")

CONTEXT="${SESSION_MSG_ESCAPED}\n\n${PREAMBLE_ESCAPED}\n\n${RUNTIME_ESCAPED}"

# Output JSON with additionalContext — Claude Code injects this into session context
cat <<HOOKEOF
{
  "additional_context": "${CONTEXT}",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "${CONTEXT}"
  }
}
HOOKEOF

exit 0
