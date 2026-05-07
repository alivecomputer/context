#!/bin/bash
# Hook: Context Watch -- UserPromptSubmit
# Two jobs:
# 1. Context % re-injection -- at every 20% threshold, re-inject rules + context
# 2. External change detection -- if another session modified walnut state files

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path. find_world_or_warn emits hook-shaped JSON on
# stdout (or {} when the SessionStart sentinel is already taken /
# event isn't message-bearing) and returns 1 in-bash so we exit 0
# cleanly without printing JSON ourselves.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-UserPromptSubmit}"; then
  exit 0
fi

SESSION_ID="${HOOK_SESSION_ID}"
[ -z "$SESSION_ID" ] && exit 0

# -- CONTEXT % RE-INJECTION --

CTX_FILE="$WORLD_ROOT/.alive/.context_pct"
if [ -f "$CTX_FILE" ]; then
  CTX_PCT=$(cat "$CTX_FILE" 2>/dev/null | tr -d '[:space:]')

  if [ -n "$CTX_PCT" ] && [ "$CTX_PCT" -gt 0 ] 2>/dev/null; then
    # Find highest unfired threshold -- inject once, not serially across prompts
    FIRE_THRESHOLD=""
    for THRESHOLD in 80 60 40 20; do
      MARKER="/tmp/alive-ctx-${SESSION_ID}-${THRESHOLD}"
      if [ "$CTX_PCT" -ge "$THRESHOLD" ] && [ ! -f "$MARKER" ]; then
        FIRE_THRESHOLD="$THRESHOLD"
        break
      fi
    done

    if [ -n "$FIRE_THRESHOLD" ]; then
      # Mark all thresholds at or below the fired one
      for T in 20 40 60 80; do
        if [ "$T" -le "$FIRE_THRESHOLD" ]; then
          touch "/tmp/alive-ctx-${SESSION_ID}-${T}"
        fi
      done
      THRESHOLD="$FIRE_THRESHOLD"

        # Build injection content based on threshold level
        if [ "$THRESHOLD" -le 40 ]; then
          # Condensed refresh
          REFRESH="<ALIVE_REFRESH threshold=\"${THRESHOLD}%\">
Context is at ${CTX_PCT}%. Refreshing core behaviours:
- Stash decisions, tasks, and notes. Surface on change.
- Verify past context via subagent before asserting. Never guess from memory.
- Bundle awareness: deliverable or future audience = bundle. Prefer bundles over loose files.
- Read before speaking. Never answer from memory about file contents.
- Check the world key (injected at start) for walnut registry, people, credentials.
</ALIVE_REFRESH>"
        else
          # Full re-injection at 60%+ -- read world key and index
          WORLD_KEY=""
          [ -f "$WORLD_ROOT/.alive/key.md" ] && WORLD_KEY=$(cat "$WORLD_ROOT/.alive/key.md")
          WORLD_INDEX=""
          [ -f "$WORLD_ROOT/.alive/_index.yaml" ] && WORLD_INDEX=$(cat "$WORLD_ROOT/.alive/_index.yaml")

          REFRESH="<ALIVE_REFRESH threshold=\"${THRESHOLD}%\">
Context is at ${CTX_PCT}%. Full context refresh:
- Stash decisions, tasks, and notes. Surface on change.
- Verify past context via subagent before asserting. Never guess from memory.
- Bundle awareness: deliverable or future audience = bundle.
- Read before speaking. Never answer from memory about file contents.

World Key:
${WORLD_KEY}

World Index:
${WORLD_INDEX}
</ALIVE_REFRESH>"
        fi

        # Scan active squirrel stashes for cross-pollination
        ACTIVE_STASHES=""
        if [ "$ALIVE_JSON_RT" = "python3" ]; then
          ACTIVE_STASHES=$(python3 -c "
import os, glob, re
sid = '$SESSION_ID'
squirrels = glob.glob('$WORLD_ROOT/.alive/_squirrels/*.yaml')
for f in squirrels:
    with open(f) as fh:
        content = fh.read()
    # Skip our own session (check filename, not content -- avoids false match if SID appears in stash text)
    if os.path.basename(f).replace('.yaml','') == sid:
        continue
    # Check if ended: null (still active) and saves: 0 (genuinely unsaved -- saved stash is historical)
    if 'ended: null' not in content:
        continue
    saves_m = re.search(r'^saves:\s*(\d+)', content, re.M)
    if saves_m and int(saves_m.group(1)) > 0:
        continue
    # Extract walnut and stash
    walnut = ''
    m = re.search(r'^walnut:\s*(.+)', content, re.M)
    if m:
        walnut = m.group(1).strip()
    if walnut == 'null' or not walnut:
        continue
    # Extract stash items
    stash_items = re.findall(r'content:\s*\"?(.+?)\"?\s*$', content, re.M)
    if stash_items:
        print(f'Active session on {walnut}: ' + '; '.join(stash_items[:5]))
" 2>/dev/null || true)
        elif [ "$ALIVE_JSON_RT" = "node" ]; then
          ACTIVE_STASHES=$(node -e "
const fs=require('fs'),path=require('path');
const sid='$SESSION_ID';
const dir='$WORLD_ROOT/.alive/_squirrels';
try{const files=fs.readdirSync(dir).filter(f=>f.endsWith('.yaml'));
files.forEach(f=>{
  if(path.basename(f,'.yaml')===sid)return;
  const c=fs.readFileSync(path.join(dir,f),'utf8');
  if(!c.includes('ended: null'))return;
  const sm=c.match(/^saves:\s*(\d+)/m);
  if(sm&&parseInt(sm[1])>0)return;
  const wm=c.match(/^walnut:\s*(.+)/m);
  const walnut=wm?wm[1].trim():'';
  if(!walnut||walnut==='null')return;
  const items=[...c.matchAll(/content:\s*\"?(.+?)\"?\s*$/gm)].map(m=>m[1]).slice(0,5);
  if(items.length)console.log('Active session on '+walnut+': '+items.join('; '));
})}catch(e){}
" 2>/dev/null || true)
        fi

        if [ -n "$ACTIVE_STASHES" ]; then
          REFRESH="${REFRESH}

<ACTIVE_SQUIRRELS>
${ACTIVE_STASHES}
</ACTIVE_SQUIRRELS>"
        fi

        REFRESH_ESCAPED=$(escape_for_json "$REFRESH")

        # Hook can only return one JSON response, so re-injection takes priority.
        # External change detection runs on every other prompt (re-injection fires at most 4x per session).
        cat <<REFRESHEOF
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "${REFRESH_ESCAPED}"
  }
}
REFRESHEOF
        exit 0
    fi
  fi
fi

# -- EXTERNAL CHANGE DETECTION --

# Find which walnut this session is working on
SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
ENTRY="$SQUIRRELS_DIR/$SESSION_ID.yaml"
[ ! -f "$ENTRY" ] && exit 0

WALNUT=$(grep '^walnut:' "$ENTRY" 2>/dev/null | sed 's/walnut: *//' || true)
[ -z "${WALNUT:-}" ] || [ "$WALNUT" = "null" ] && exit 0

# Find walnut's state files directory -- check _kernel/ first, fall back to walnut root
WALNUT_DIR=$(find "$WORLD_ROOT" -path "*/01_Archive" -prune -o -type d -name "$WALNUT" -print -quit 2>/dev/null || true)
[ -z "${WALNUT_DIR:-}" ] || [ ! -d "$WALNUT_DIR" ] && exit 0

if [ -d "$WALNUT_DIR/_kernel" ]; then
  WALNUT_KERNEL="$WALNUT_DIR/_kernel"
else
  WALNUT_KERNEL="$WALNUT_DIR"
fi

# Timestamp file tracks when this session last checked
LASTCHECK="/tmp/alive-lastcheck-${SESSION_ID}"

# On first run, just create the timestamp and exit
if [ ! -f "$LASTCHECK" ]; then
  date +%s > "$LASTCHECK"
  exit 0
fi

LAST_CHECK_TIME=$(cat "$LASTCHECK" 2>/dev/null || echo "0")

# Check if now.json or log.md were modified after our last check
# v3 flat: _kernel/now.json, _kernel/tasks.json  |  v2: _kernel/_generated/now.json  |  v1: now.md
CHANGED=""
for file in "$WALNUT_KERNEL/now.json" "$WALNUT_KERNEL/_generated/now.json" "$WALNUT_KERNEL/tasks.json" "$WALNUT_KERNEL/now.md" "$WALNUT_KERNEL/log.md" "$WALNUT_KERNEL/tasks.md"; do
  if [ -f "$file" ]; then
    # Get file mtime as epoch seconds
    if stat --version >/dev/null 2>&1; then
      MTIME=$(stat -c %Y "$file" 2>/dev/null || echo "0")
    else
      MTIME=$(stat -f %m "$file" 2>/dev/null || echo "0")
    fi
    if [ "$MTIME" -gt "$LAST_CHECK_TIME" ] 2>/dev/null; then
      CHANGED="${CHANGED} $(basename "$file")"
    fi
  fi
done

# Update timestamp
date +%s > "$LASTCHECK"

# If nothing changed, exit silently
[ -z "${CHANGED:-}" ] && exit 0

# Check if the change was made by US (same session_id in now.json squirrel field)
# now.json uses short IDs (first 8 chars), hook gets full UUID -- check both
# v3 flat: _kernel/now.json  |  v2: _kernel/_generated/now.json  |  v1: now.md
LAST_SQUIRREL=""
NOW_JSON_PATH=""
if [ -f "$WALNUT_KERNEL/now.json" ]; then
  NOW_JSON_PATH="$WALNUT_KERNEL/now.json"
elif [ -f "$WALNUT_KERNEL/_generated/now.json" ]; then
  NOW_JSON_PATH="$WALNUT_KERNEL/_generated/now.json"
fi
if [ -n "$NOW_JSON_PATH" ]; then
  if [ "$ALIVE_JSON_RT" = "python3" ]; then
    LAST_SQUIRREL=$(python3 -c "import json; d=json.load(open('$NOW_JSON_PATH')); print(d.get('squirrel',''))" 2>/dev/null || true)
  elif [ "$ALIVE_JSON_RT" = "node" ]; then
    LAST_SQUIRREL=$(node -e "try{const d=JSON.parse(require('fs').readFileSync('$NOW_JSON_PATH','utf8'));console.log(d.squirrel||'')}catch(e){console.log('')}" 2>/dev/null || true)
  fi
elif [ -f "$WALNUT_KERNEL/now.md" ]; then
  LAST_SQUIRREL=$(grep '^squirrel:' "$WALNUT_KERNEL/now.md" 2>/dev/null | sed 's/squirrel: *//' | tr -d '[:space:]' || true)
fi
SHORT_SID="${SESSION_ID:0:8}"
if [ "${LAST_SQUIRREL:-}" = "$SESSION_ID" ] || [ "${LAST_SQUIRREL:-}" = "$SHORT_SID" ]; then
  exit 0
fi

# Another session modified the walnut -- notify
CONTEXT_MSG="Another session just saved to ${WALNUT}. Changed:${CHANGED}. You should re-read _kernel/now.json, _kernel/tasks.json and _kernel/log.md before continuing -- your context may be stale. Ask the human if they want you to refresh."
CONTEXT_ESCAPED=$(escape_for_json "$CONTEXT_MSG")
cat <<CHANGEEOF
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "${CONTEXT_ESCAPED}"
  }
}
CHANGEEOF
exit 0
