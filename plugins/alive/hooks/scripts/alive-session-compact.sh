#!/bin/bash
# Hook: Session Compact — SessionStart (compact)
# Re-injects stash + walnut context + preferences after compaction.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input
read_session_fields
find_world || { echo "No ALIVE world found."; exit 0; }

SESSION_ID="${HOOK_SESSION_ID}"

# Resolve preferences
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

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
  STASH=$(awk '/^stash:/{found=1; next} found && /^[a-z]/{found=0} found && /content:/{gsub(/.*content: *"?/,""); gsub(/"$/,""); print "- " $0}' "$ENTRY" 2>/dev/null || true)
  if [ -z "${STASH:-}" ]; then
    STASH="(empty)"
  fi
fi

# If walnut is active, re-read brief pack using find (handles any nesting depth)
NOW_CONTENT=""
KEY_CONTENT=""
if [ -n "${WALNUT:-}" ] && [ "$WALNUT" != "null" ]; then
  WALNUT_CORE=$(find "$WORLD_ROOT" -path "*/01_Archive" -prune -o -path "*/$WALNUT/_core" -print -quit 2>/dev/null || true)
  if [ -n "${WALNUT_CORE:-}" ] && [ -d "$WALNUT_CORE" ]; then
    [ -f "$WALNUT_CORE/now.md" ] && NOW_CONTENT=$(head -30 "$WALNUT_CORE/now.md")
    [ -f "$WALNUT_CORE/key.md" ] && KEY_CONTENT=$(head -30 "$WALNUT_CORE/key.md")
  fi
fi

cat << EOF
CONTEXT RESTORED after compaction. Session: ${SESSION_ID:-unknown} | Walnut: ${WALNUT:-none}
$PREFS

Stash recovered:
$STASH

Current state (re-read — do not trust pre-compaction memory):
${NOW_CONTENT:-no now.md found}

Identity:
${KEY_CONTENT:-no key.md found}

IMPORTANT: Re-read _core/key.md, _core/now.md, _core/tasks.md before continuing work. Do not trust memory of files read before compaction.
EOF
