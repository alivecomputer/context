#!/bin/bash
# Hook 1c: Session Compact — SessionStart (compact)
# Re-injects stash + walnut context + preferences after context compression.

set -euo pipefail

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

WORLD_ROOT=$(find_world) || { echo "No ALIVE world found."; exit 0; }

# Resolve preferences (name + toggles)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-resolve-preferences.sh"
PREFS=$(resolve_preferences "$WORLD_ROOT")

# Find the most recent unsigned squirrel entry in .alive/_squirrels/
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
LATEST_ENTRY=""
if [ -d "$SQUIRRELS_DIR" ]; then
  LATEST_ENTRY=$(grep -rl 'ended: null' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1)
fi

if [ -n "$LATEST_ENTRY" ]; then
  SESSION_ID=$(grep 'session_id:' "$LATEST_ENTRY" | awk '{print $2}')
  WALNUT=$(grep '^walnut:' "$LATEST_ENTRY" | awk '{print $2}')
  STASH=$(grep -A 100 'stash:' "$LATEST_ENTRY" | head -50)

  # If a walnut is active, re-read its brief pack
  NOW_CONTENT=""
  KEY_CONTENT=""
  if [ "$WALNUT" != "null" ] && [ -n "$WALNUT" ]; then
    # Search for the walnut's _core/ in common locations
    for DOMAIN in 02_Life 04_Ventures 05_Experiments; do
      WALNUT_CORE="$WORLD_ROOT/$DOMAIN/$WALNUT/_core"
      if [ -d "$WALNUT_CORE" ]; then
        [ -f "$WALNUT_CORE/now.md" ] && NOW_CONTENT=$(head -20 "$WALNUT_CORE/now.md")
        [ -f "$WALNUT_CORE/key.md" ] && KEY_CONTENT=$(head -20 "$WALNUT_CORE/key.md")
        break
      fi
      # Check nested walnuts one level deep
      for PARENT in "$WORLD_ROOT/$DOMAIN"/*/; do
        WALNUT_CORE="$PARENT$WALNUT/_core"
        if [ -d "$WALNUT_CORE" ]; then
          [ -f "$WALNUT_CORE/now.md" ] && NOW_CONTENT=$(head -20 "$WALNUT_CORE/now.md")
          [ -f "$WALNUT_CORE/key.md" ] && KEY_CONTENT=$(head -20 "$WALNUT_CORE/key.md")
          break 2
        fi
      done
    done
  fi

  cat << EOF
CONTEXT RESTORED after compaction. Session: $SESSION_ID | Walnut: ${WALNUT:-none}
$PREFS

Stash recovered:
$STASH

Current state (re-read — do not trust pre-compaction memory):
$NOW_CONTENT

Identity:
$KEY_CONTENT

IMPORTANT: Re-read _core/key.md, _core/now.md, _core/tasks.md before continuing work. Do not trust memory of files read before compaction.
EOF
else
  cat << EOF
Context compacted. No squirrel entry found — stash may be lost.
$PREFS
EOF
fi
