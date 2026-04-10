#!/bin/bash
# Hook: Session New -- SessionStart (startup)
# Creates squirrel entry in .alive/_squirrels/, reads preferences, injects rules.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

# Read stdin JSON -- extracts session_id, cwd, event name
read_hook_input

# SessionStart-specific -- extracts model, source, transcript_path
read_session_fields

# Find world root
if ! find_world; then
  # No world found -- still inject rules so the AI knows how to run setup
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
  SETUP_MSG="ALIVE Context System plugin loaded but NO WORLD FOUND in ${HOOK_CWD}.
Model: ${HOOK_MODEL:-unknown}
World seed: $SEED_STATUS
Onboarding questionnaire: $ONBOARDING_HTML

This appears to be a fresh install. The user needs to set up their world.
Run /alive:world to start -- it will detect the fresh install and route to setup."

  PREAMBLE="<EXTREMELY_IMPORTANT>
The following are your core operating rules for the ALIVE Context System. They are MANDATORY -- not suggestions, not defaults, not guidelines. You MUST follow them in every response, every tool call, every session.
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
  SESSION_ID=$(head -c 16 /dev/urandom 2>/dev/null | (shasum 2>/dev/null || sha256sum 2>/dev/null || md5sum 2>/dev/null || od -A n -t x1 | tr -d ' \n') | head -c 8)
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
topic: null
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

# Inject statusline into settings.json -- quoted path for spaces (iCloud etc.)
# Fix BOTH project-level and user-level settings to handle legacy migration.
STATUSLINE_CMD="bash \"$WORLD_ROOT/.alive/statusline.sh\""

# Helper: fix statusline in a settings.json file (create or self-heal)
fix_statusline_in_settings() {
  local settings_file="$1"
  local settings_dir
  settings_dir="$(dirname "$settings_file")"
  mkdir -p "$settings_dir"

  if [ ! -f "$settings_file" ]; then
    # Only create project-level settings, not user-level
    if [ "$settings_file" = "$WORLD_ROOT/.claude/settings.json" ]; then
      cat > "$settings_file" << SETTINGSEOF
{
  "statusLine": {
    "type": "command",
    "command": $( if [ "$ALIVE_JSON_RT" = "python3" ]; then printf '%s' "$STATUSLINE_CMD" | python3 -c "import sys,json; print(json.dumps(sys.stdin.buffer.read().decode("utf-8","replace")))" 2>/dev/null; elif [ "$ALIVE_JSON_RT" = "node" ]; then printf '%s' "$STATUSLINE_CMD" | node -e "let d='';process.stdin.on('data',c=>d+=c);process.stdin.on('end',()=>console.log(JSON.stringify(d)))" 2>/dev/null; else echo "\"$STATUSLINE_CMD\""; fi )
  }
}
SETTINGSEOF
    fi
    return
  fi

  # settings.json exists -- ensure statusLine uses correct .alive/ path with quoting
  if [ "$ALIVE_JSON_RT" = "python3" ]; then
    ALIVE_SETTINGS_FILE="$settings_file" ALIVE_WORLD_ROOT="$WORLD_ROOT" python3 -c "
import json, os, sys
sf = os.environ['ALIVE_SETTINGS_FILE']
wr = os.environ['ALIVE_WORLD_ROOT']
# Quoted command handles paths with spaces (iCloud, etc.)
expected_cmd = 'bash \"' + wr + '/.alive/statusline.sh\"'
try:
    with open(sf) as f:
        data = json.load(f)
except (json.JSONDecodeError, ValueError):
    print('ALIVE: settings.json is malformed, cannot inject statusLine', file=sys.stderr)
    sys.exit(0)
current = data.get('statusLine', {}).get('command', '')
# Fix if: missing, wrong path, stale .walnut/ reference, or unquoted path with spaces
needs_fix = (
    current != expected_cmd
    and (
        not current
        or '.walnut/' in current
        or ('.alive/statusline.sh' in current and 'bash ' not in current and ' ' in wr)
    )
)
if needs_fix:
    data['statusLine'] = {'type': 'command', 'command': expected_cmd}
    with open(sf, 'w') as f:
        json.dump(data, f, indent=2)
        f.write('\n')
    print('ALIVE: fixed statusLine in ' + sf, file=sys.stderr)
" 2>/dev/null || true
  elif [ "$ALIVE_JSON_RT" = "node" ]; then
    ALIVE_SETTINGS_FILE="$settings_file" ALIVE_WORLD_ROOT="$WORLD_ROOT" node -e "
const fs=require('fs');
const sf=process.env.ALIVE_SETTINGS_FILE;
const wr=process.env.ALIVE_WORLD_ROOT;
const expected='bash \"'+wr+'/.alive/statusline.sh\"';
let data;
try{data=JSON.parse(fs.readFileSync(sf,'utf8'))}catch(e){process.exit(0)}
const current=(data.statusLine||{}).command||'';
const needsFix=current!==expected&&(!current||current.includes('.walnut/')||(current.includes('.alive/statusline.sh')&&!current.includes('bash ')&&wr.includes(' ')));
if(needsFix){data.statusLine={type:'command',command:expected};fs.writeFileSync(sf,JSON.stringify(data,null,2)+'\n');console.error('ALIVE: fixed statusLine in '+sf)}
" 2>/dev/null || true
  fi
}

# Fix project-level settings
fix_statusline_in_settings "$WORLD_ROOT/.claude/settings.json"

# Fix user-level settings -- catches stale .walnut/ paths from pre-migration installs
USER_SETTINGS="$HOME/.claude/settings.json"
if [ -f "$USER_SETTINGS" ]; then
  # Only touch user settings if it has a stale .walnut/ statusline reference
  if grep -q '\.walnut/statusline' "$USER_SETTINGS" 2>/dev/null; then
    fix_statusline_in_settings "$USER_SETTINGS"
  fi
fi

# Sync custom skill symlinks: .alive/skills/*/SKILL.md -> .claude/skills/*/SKILL.md
# Claude Code only discovers skills in .claude/skills/. Custom skills live in .alive/skills/.
# This bridge ensures user-created skills are always discoverable.
CUSTOM_SKILLS_DIR="$WORLD_ROOT/.alive/skills"
CLAUDE_SKILLS_DIR="$WORLD_ROOT/.claude/skills"
if [ -d "$CUSTOM_SKILLS_DIR" ]; then
  mkdir -p "$CLAUDE_SKILLS_DIR"
  for skill_dir in "$CUSTOM_SKILLS_DIR"/*/; do
    [ -d "$skill_dir" ] || continue
    SKILL_NAME=$(basename "$skill_dir")
    SKILL_SRC="$skill_dir/SKILL.md"
    SKILL_DST_DIR="$CLAUDE_SKILLS_DIR/$SKILL_NAME"
    SKILL_DST="$SKILL_DST_DIR/SKILL.md"
    if [ -f "$SKILL_SRC" ]; then
      # Create target dir and symlink if missing or broken
      if [ ! -L "$SKILL_DST" ] || [ "$(readlink "$SKILL_DST" 2>/dev/null)" != "$SKILL_SRC" ]; then
        mkdir -p "$SKILL_DST_DIR"
        ln -sf "$SKILL_SRC" "$SKILL_DST" 2>/dev/null || cp -f "$SKILL_SRC" "$SKILL_DST" 2>/dev/null
      fi
    fi
  done
fi

# Read world key (.alive/key.md) for injection
WORLD_KEY_CONTENT=""
WORLD_KEY_FILE="$WORLD_ROOT/.alive/key.md"
if [ -f "$WORLD_KEY_FILE" ]; then
  WORLD_KEY_CONTENT=$(cat "$WORLD_KEY_FILE")
fi

# Read world index (.alive/_index.yaml) for injection -- walnut registry
WORLD_INDEX_CONTENT=""
WORLD_INDEX_FILE="$WORLD_ROOT/.alive/_index.yaml"
if [ -f "$WORLD_INDEX_FILE" ]; then
  WORLD_INDEX_CONTENT="<WORLD_INDEX>
$(cat "$WORLD_INDEX_FILE")
</WORLD_INDEX>"
fi

# Bundle awareness injection
BUNDLE_AWARENESS="<BUNDLE_AWARENESS>
If you detect work with a deliverable or future audience -- drafting for someone, iterating a document, building something to ship, send, or reference later -- check: is there an active bundle? If not, invoke the bundle skill to offer creation.

Detection should be broad. Prefer bundles over loose files. The user doesn't have to call /alive:bundle -- you detect and offer.
</BUNDLE_AWARENESS>"

# Tidy nudge -- check .alive/.last_tidy timestamp file
TIDY_NUDGE=""
LAST_TIDY_FILE="$WORLD_ROOT/.alive/.last_tidy"
if [ -f "$LAST_TIDY_FILE" ]; then
  LAST_TIDY_DATE=$(cat "$LAST_TIDY_FILE" | tr -d '[:space:]')
  if [ -n "$LAST_TIDY_DATE" ] && [ -n "$ALIVE_JSON_RT" ]; then
    if [ "$ALIVE_JSON_RT" = "python3" ]; then
      DAYS_SINCE=$(python3 -c "
from datetime import datetime
try:
    ts = '$LAST_TIDY_DATE'.split('+')[0].split('T')[0]
    dt = datetime.strptime(ts, '%Y-%m-%d')
    print((datetime.now() - dt).days)
except:
    pass
" 2>/dev/null || true)
    elif [ "$ALIVE_JSON_RT" = "node" ]; then
      DAYS_SINCE=$(node -e "
try{const ts='$LAST_TIDY_DATE'.split('+')[0].split('T')[0];const d=new Date(ts);const now=new Date();console.log(Math.floor((now-d)/86400000))}catch(e){}
" 2>/dev/null || true)
    fi
    if [ -n "$DAYS_SINCE" ] && [ "$DAYS_SINCE" -gt 7 ]; then
      TIDY_NUDGE="Last tidy: ${DAYS_SINCE} days ago. Consider running /alive:system-cleanup."
    fi
  fi
else
  # No tidy record at all -- nudge if the world has been around a while
  YAML_COUNT=$(find "$WORLD_ROOT/.alive/_squirrels" -name "*.yaml" -type f 2>/dev/null | wc -l | tr -d ' ')
  if [ "$YAML_COUNT" -gt 5 ]; then
    TIDY_NUDGE="No record of /alive:system-cleanup being run. Consider running it to validate your world."
  fi
fi

# Now read rule contents for injection (slow part -- after YAML is written)
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

# Migration status
MIGRATION_MSG=""
if [ "${ALIVE_MIGRATED_FROM:-}" = "walnut" ]; then
  MIGRATION_MSG="
MIGRATION COMPLETE: .walnut/ has been renamed to .alive/ automatically. All squirrel entries, preferences, and statusline preserved. The plugin is now ALIVE Context System v2."
elif [ "${ALIVE_MIGRATION_CONFLICT:-}" = "both_exist" ]; then
  MIGRATION_MSG="
WARNING: Both .walnut/ and .alive/ exist in this world. This needs manual resolution -- invoke the migration skill to handle it."
fi

# Detect v2 patterns that need v3 upgrade
UPGRADE_NEEDED=""
if [ -n "$WORLD_ROOT" ]; then
  # Check for v2 indicators
  if [ -d "$WORLD_ROOT/02_Life/_kernel" ] 2>/dev/null || \
     find "$WORLD_ROOT" -maxdepth 4 -name "tasks.md" -path "*/_kernel/tasks.md" -print -quit 2>/dev/null | grep -q . || \
     find "$WORLD_ROOT" -maxdepth 4 -type d -name "_generated" -path "*/_kernel/_generated" -print -quit 2>/dev/null | grep -q . || \
     find "$WORLD_ROOT" -maxdepth 3 -type d -name "bundles" -print -quit 2>/dev/null | grep -q . || \
     [ -d "$WORLD_ROOT/People" ] 2>/dev/null || \
     [ -d "$WORLD_ROOT/03_Inputs" ] 2>/dev/null; then
    UPGRADE_NEEDED="
--- MESSAGE FROM THE DEVELOPER ---

Hey — you're running ALIVE v3 on a v2 world. We shipped a major architectural update based on feedback from the first 500 users. Here's what changed and why:

WHAT'S NEW IN v3:
- Script-operated tasks (tasks.py CLI replaces tasks.md — faster, no context burn)
- True projections (now.json computed from ALL sources post-save — concurrent sessions don't clobber each other)
- Flat kernel (_generated/ subfolder removed — 6 files flat in _kernel/)
- Flat bundles (bundles/ container removed — bundles sit next to _kernel/ in walnut root)
- 03_Inputs renamed to 03_Inbox (universally understood)
- 3-file load sequence (key.md + now.json + insights.md — down from 13+ file reads)
- Observations.md removed (stash routes to log at save)

WHY: Loading a walnut was reading 13+ files. Task tracking was broken across 129 zombie files. Concurrent sessions clobbered each other's context. This release fixes all of it.

YOUR WORLD NEEDS MIGRATION. Run:

  /alive:system-upgrade

It will show you exactly what changes, back everything up, and migrate your world. Takes a few minutes. Nothing breaks if you don't — but you'll be running v3 rules on v2 structure, which means degraded performance and missing features.

— Ben (@benslockedin)
---

AGENT INSTRUCTION: Before doing ANYTHING else, use the AskUserQuestion tool to ask:
question: 'Your world needs migrating to ALIVE v3. This upgrades task tracking, projections, and load speed. Run /alive:system-upgrade now?'
options: ['Yes, upgrade now', 'Not now, keep working']

If they choose 'Yes, upgrade now', invoke alive:system-upgrade immediately.
If they choose 'Not now, keep working', proceed normally but remind them again at save time."
  fi
fi

# Build session message with rule verification
SESSION_MSG="ALIVE Context System session initialized. Session ID: $SESSION_ID
World: $WORLD_ROOT
Walnut: none detected
Model: $HOOK_MODEL
$PREFS
Rules: ${RULE_COUNT} loaded (${RULE_NAMES})${MIGRATION_MSG}${UPGRADE_NEEDED}"

# Escape and combine -- world key + index + bundle awareness + tidy nudge + rules
WORLD_KEY_ESCAPED=$(escape_for_json "$WORLD_KEY_CONTENT")
INDEX_ESCAPED=""
if [ -n "$WORLD_INDEX_CONTENT" ]; then
  INDEX_ESCAPED=$(escape_for_json "$WORLD_INDEX_CONTENT")
fi
BUNDLE_ESCAPED=$(escape_for_json "$BUNDLE_AWARENESS")
TIDY_ESCAPED=""
if [ -n "$TIDY_NUDGE" ]; then
  TIDY_ESCAPED=$(escape_for_json "$TIDY_NUDGE")
fi
SESSION_MSG_ESCAPED=$(escape_for_json "$SESSION_MSG")
PREAMBLE_ESCAPED=$(escape_for_json "$PREAMBLE")
RUNTIME_ESCAPED=$(escape_for_json "$RUNTIME_RULES")

CONTEXT="${SESSION_MSG_ESCAPED}\n\n${WORLD_KEY_ESCAPED}"
if [ -n "$INDEX_ESCAPED" ]; then
  CONTEXT="${CONTEXT}\n\n${INDEX_ESCAPED}"
fi
CONTEXT="${CONTEXT}\n\n${BUNDLE_ESCAPED}"
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
