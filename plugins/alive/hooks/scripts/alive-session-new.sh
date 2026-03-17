#!/bin/bash
# Hook: Session New — SessionStart (startup)
# Creates squirrel entry in .alive/_squirrels/, reads preferences, injects rules.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

# Read stdin JSON — extracts session_id, cwd, event name
read_hook_input

# SessionStart-specific — extracts model, source, transcript_path
read_session_fields

# Find world root
if ! find_world; then
  # No world found — still inject rules so the AI knows how to run setup
  PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

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

  # Check for world-seed.md in PWD
  SEED_STATUS="none"
  if [ -f "${HOOK_CWD}/world-seed.md" ]; then
    SEED_STATUS="found at ${HOOK_CWD}/world-seed.md"
  fi

  # Onboarding HTML path
  ONBOARDING_HTML="$PLUGIN_ROOT/onboarding/world-builder.html"

  # Setup signal
  SETUP_MSG="ALIVE plugin loaded but NO WORLD FOUND in ${HOOK_CWD}.
Model: ${HOOK_MODEL:-unknown}
World seed: $SEED_STATUS
Onboarding questionnaire: $ONBOARDING_HTML

This appears to be a fresh install. The user needs to set up their world.
Run /alive:world to start — it will detect the fresh install and route to setup."

  PREAMBLE="<EXTREMELY_IMPORTANT>
The following are your core operating rules for the ALIVE system. They are MANDATORY — not suggestions, not defaults, not guidelines. You MUST follow them in every response, every tool call, every session.
</EXTREMELY_IMPORTANT>"

  SETUP_ESCAPED=$(escape_for_json "$SETUP_MSG")
  PREAMBLE_ESCAPED=$(escape_for_json "$PREAMBLE")
  RUNTIME_ESCAPED=$(escape_for_json "$RUNTIME_RULES")

  CONTEXT="${SETUP_ESCAPED}\n\n${PREAMBLE_ESCAPED}\n\n${RUNTIME_ESCAPED}"

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
fi

# Use Claude Code's session ID, fall back to random only if missing
SESSION_ID="${HOOK_SESSION_ID}"
if [ -z "$SESSION_ID" ]; then
  SESSION_ID=$(head -c 16 /dev/urandom | shasum | head -c 8)
fi

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S")

# Set env vars via CLAUDE_ENV_FILE if available
if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
  echo "ALIVE_SESSION_ID=$SESSION_ID" >> "$CLAUDE_ENV_FILE"
  echo "ALIVE_WORLD_ROOT=$WORLD_ROOT" >> "$CLAUDE_ENV_FILE"
fi

# Plugin root for reading rules and statusline
PLUGIN_ROOT="${CLAUDE_PLUGIN_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"

# Quick-count rule files (no content reading) so YAML has correct count immediately
RULE_COUNT=0
for rule_file in "$PLUGIN_ROOT/rules/"*.md; do
  [ -f "$rule_file" ] && RULE_COUNT=$((RULE_COUNT + 1))
done

# Write squirrel entry FIRST with correct count (statusline reads this concurrently)
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
mkdir -p "$SQUIRRELS_DIR"
ENTRY_FILE="$SQUIRRELS_DIR/$SESSION_ID.yaml"
cat > "$ENTRY_FILE" << EOF
session_id: $SESSION_ID
runtime_id: squirrel.core@1.0
engine: $HOOK_MODEL
walnut: null
started: $TIMESTAMP
ended: null
saves: 0
last_saved: null
transcript: ${HOOK_TRANSCRIPT}
cwd: ${HOOK_CWD}
rules_loaded: $RULE_COUNT
tags: []
stash: []
working: []
EOF

# Resolve preferences
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

# Copy statusline script to stable location if not present or outdated
STATUSLINE_SRC="$PLUGIN_ROOT/statusline/alive-statusline.sh"
STATUSLINE_DST="$WORLD_ROOT/.alive/statusline.sh"
if [ -f "$STATUSLINE_SRC" ]; then
  if [ ! -f "$STATUSLINE_DST" ] || ! cmp -s "$STATUSLINE_SRC" "$STATUSLINE_DST"; then
    cp "$STATUSLINE_SRC" "$STATUSLINE_DST"
    chmod +x "$STATUSLINE_DST"
  fi
fi

# Read world key (.alive/key.md) for injection
WORLD_KEY_CONTENT=""
WORLD_KEY_FILE="$WORLD_ROOT/.alive/key.md"
if [ -f "$WORLD_KEY_FILE" ]; then
  WORLD_KEY_CONTENT=$(cat "$WORLD_KEY_FILE")
fi

# Capsule awareness injection
CAPSULE_AWARENESS="<CAPSULE_AWARENESS>
If you detect work with a deliverable or future audience — drafting for someone, iterating a document, building something to ship, send, or reference later — check: is there an active capsule? If not, invoke the capsule skill to offer creation.

Detection should be broad. Prefer capsules over loose files. The user doesn't have to call /alive:capsule — you detect and offer.
</CAPSULE_AWARENESS>"

# Tidy nudge — check .alive/.last_tidy timestamp file
TIDY_NUDGE=""
LAST_TIDY_FILE="$WORLD_ROOT/.alive/.last_tidy"
if [ -f "$LAST_TIDY_FILE" ]; then
  LAST_TIDY_DATE=$(cat "$LAST_TIDY_FILE" | tr -d '[:space:]')
  if command -v python3 &>/dev/null && [ -n "$LAST_TIDY_DATE" ]; then
    DAYS_SINCE=$(python3 -c "
from datetime import datetime
try:
    ts = '$LAST_TIDY_DATE'.split('+')[0].split('T')[0]
    dt = datetime.strptime(ts, '%Y-%m-%d')
    print((datetime.now() - dt).days)
except:
    pass
" 2>/dev/null || true)
    if [ -n "$DAYS_SINCE" ] && [ "$DAYS_SINCE" -gt 7 ]; then
      TIDY_NUDGE="Last tidy: ${DAYS_SINCE} days ago. Consider running /alive:tidy."
    fi
  fi
else
  # No tidy record at all — nudge if the world has been around a while
  YAML_COUNT=$(find "$WORLD_ROOT/.alive/_squirrels" -name "*.yaml" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$YAML_COUNT" -gt 5 ]; then
    TIDY_NUDGE="No record of /alive:tidy being run. Consider running it to validate your world."
  fi
fi

# Now read rule contents for injection (slow part — after YAML is written)
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
The following are your core operating rules for the ALIVE system. They are MANDATORY — not suggestions, not defaults, not guidelines. You MUST follow them in every response, every tool call, every session.
</EXTREMELY_IMPORTANT>"

# Build session message with rule verification
SESSION_MSG="ALIVE session initialized. Session ID: $SESSION_ID
World: $WORLD_ROOT
Walnut: none detected
Model: $HOOK_MODEL
$PREFS
Rules: ${RULE_COUNT} loaded (${RULE_NAMES})"

# Escape and combine — world key + capsule awareness + tidy nudge + rules
WORLD_KEY_ESCAPED=$(escape_for_json "$WORLD_KEY_CONTENT")
CAPSULE_ESCAPED=$(escape_for_json "$CAPSULE_AWARENESS")
TIDY_ESCAPED=""
if [ -n "$TIDY_NUDGE" ]; then
  TIDY_ESCAPED=$(escape_for_json "$TIDY_NUDGE")
fi
SESSION_MSG_ESCAPED=$(escape_for_json "$SESSION_MSG")
PREAMBLE_ESCAPED=$(escape_for_json "$PREAMBLE")
RUNTIME_ESCAPED=$(escape_for_json "$RUNTIME_RULES")

CONTEXT="${SESSION_MSG_ESCAPED}\n\n${WORLD_KEY_ESCAPED}\n\n${CAPSULE_ESCAPED}"
if [ -n "$TIDY_ESCAPED" ]; then
  CONTEXT="${CONTEXT}\n\n${TIDY_ESCAPED}"
fi
CONTEXT="${CONTEXT}\n\n${PREAMBLE_ESCAPED}\n\n${RUNTIME_ESCAPED}"

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
