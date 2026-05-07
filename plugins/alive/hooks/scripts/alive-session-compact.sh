#!/bin/bash
# Hook: Session Compact -- SessionStart (compact)
# Re-injects rules + stash + walnut context + preferences after compaction.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input
read_session_fields

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path. We previously echoed "No Alive world found." to
# stdout, which produced TWO things on stdout (that line plus an
# implicit empty body) and was not valid hook JSON. Helper emits a
# single, valid JSON object instead.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-SessionStart}"; then
  exit 0
fi

SESSION_ID="${HOOK_SESSION_ID}"

# Resolve preferences
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

# Plugin root for reading rules
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Build runtime rules from plugin source files (same as session-new)
RUNTIME_RULES=""
RULE_COUNT=0
RULE_NAMES=""

if [ -f "$PLUGIN_ROOT/CLAUDE.md" ]; then
  RUNTIME_RULES=$(cat "$PLUGIN_ROOT/CLAUDE.md")
fi

for rule_file in "$PLUGIN_ROOT/rules/"*.md; do
  if [ -f "$rule_file" ]; then
    RULE_COUNT=$((RULE_COUNT + 1))
    RULE_NAME=$(basename "$rule_file" .md)
    RULE_NAMES="${RULE_NAMES}${RULE_NAMES:+, }${RULE_NAME}"
    RUNTIME_RULES="${RUNTIME_RULES}

$(cat "$rule_file")"
  fi
done

# Preamble
PREAMBLE="<EXTREMELY_IMPORTANT>
The following are your core operating rules for the ALIVE Context System. They are MANDATORY -- not suggestions, not defaults, not guidelines. You MUST follow them in every response, every tool call, every session.
</EXTREMELY_IMPORTANT>"

# Find squirrel entry by session_id or fall back
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
ENTRY=""
if [ -n "$SESSION_ID" ] && [ -f "$SQUIRRELS_DIR/$SESSION_ID.yaml" ]; then
  ENTRY="$SQUIRRELS_DIR/$SESSION_ID.yaml"
elif [ -d "$SQUIRRELS_DIR" ]; then
  ENTRY=$(grep -rl 'ended: null' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1 || true)
fi

WALNUT=""
STASH="(empty)"
if [ -n "${ENTRY:-}" ] && [ -f "$ENTRY" ]; then
  WALNUT=$(grep '^walnut:' "$ENTRY" | head -1 | sed 's/walnut: *//' || true)
  # Only show stash if this entry was never saved (saves: 0) -- saved stash items were already routed
  SAVES=$(grep '^saves:' "$ENTRY" | head -1 | sed 's/saves: *//' | tr -d '[:space:]' || echo "0")
  if [ "$SAVES" = "0" ]; then
    STASH=$(awk '/^stash:/{found=1; next} found && /^[a-z]/{found=0} found && /content:/{gsub(/.*content: *"?/,""); gsub(/"$/,""); print "- " $0}' "$ENTRY" 2>/dev/null || true)
  else
    STASH=""
  fi
  if [ -z "${STASH:-}" ]; then
    STASH="(empty)"
  fi
fi

# If walnut is active, re-read brief pack using find (handles any nesting depth)
NOW_CONTENT=""
KEY_CONTENT=""
if [ -n "${WALNUT:-}" ] && [ "$WALNUT" != "null" ]; then
  # Find walnut directory, check _kernel/ first (canonical), fall back to walnut root (legacy)
  WALNUT_DIR=$(find "$WORLD_ROOT" -path "*/01_Archive" -prune -o -type d -name "$WALNUT" -print -quit 2>/dev/null || true)
  if [ -n "${WALNUT_DIR:-}" ] && [ -d "$WALNUT_DIR" ]; then
    # Check _kernel/ first (v3 flat), fall back to _kernel/_generated/ (v2), then walnut root (v1)
    if [ -f "$WALNUT_DIR/_kernel/now.json" ]; then
      NOW_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/now.json")
      [ -f "$WALNUT_DIR/_kernel/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/key.md")
    elif [ -f "$WALNUT_DIR/_kernel/_generated/now.json" ]; then
      NOW_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/_generated/now.json")
      [ -f "$WALNUT_DIR/_kernel/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/key.md")
    elif [ -f "$WALNUT_DIR/_kernel/now.md" ]; then
      NOW_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/now.md")
      [ -f "$WALNUT_DIR/_kernel/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_DIR/_kernel/key.md")
    elif [ -f "$WALNUT_DIR/now.json" ]; then
      NOW_CONTENT=$(head -30 "$WALNUT_DIR/now.json")
      [ -f "$WALNUT_DIR/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_DIR/key.md")
    elif [ -f "$WALNUT_DIR/now.md" ]; then
      NOW_CONTENT=$(head -30 "$WALNUT_DIR/now.md")
      [ -f "$WALNUT_DIR/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_DIR/key.md")
    fi
  fi
fi

SESSION_MSG="CONTEXT RESTORED after compaction. Session: ${SESSION_ID:-unknown} | Walnut: ${WALNUT:-none}
World: $WORLD_ROOT
Model: $HOOK_MODEL
$PREFS
Rules: ${RULE_COUNT} loaded (${RULE_NAMES})

Stash recovered:
$STASH

Current state (re-read -- do not trust pre-compaction memory):
${NOW_CONTENT:-no now.json found}

Identity:
${KEY_CONTENT:-no key.md found}

IMPORTANT: Re-read key.md, now.json, tasks.json before continuing work (check _kernel/ first, fall back to walnut root). Do not trust memory of files read before compaction."

# Escape and combine
SESSION_MSG_ESCAPED=$(escape_for_json "$SESSION_MSG")
PREAMBLE_ESCAPED=$(escape_for_json "$PREAMBLE")
RUNTIME_ESCAPED=$(escape_for_json "$RUNTIME_RULES")

CONTEXT="${SESSION_MSG_ESCAPED}\n\n${PREAMBLE_ESCAPED}\n\n${RUNTIME_ESCAPED}"

# Output JSON with additionalContext
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
