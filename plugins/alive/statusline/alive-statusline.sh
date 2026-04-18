#!/bin/bash
# ALIVE Statusline -- shows session health at a glance.
# Boot message on first render, then working statusline after first response.
# Cross-platform: Mac, Linux, Windows (Git Bash). No python3 dependency.

INPUT=$(cat 2>/dev/null || echo '{}')

# ── Platform detection ──
ALIVE_PLATFORM="unix"
if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
  ALIVE_PLATFORM="windows"
fi

# ── Pure bash JSON extraction ──
_json_val() {
  local result
  result=$(printf '%s' "$INPUT" | sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1)
  if [ -z "$result" ]; then
    result=$(printf '%s' "$INPUT" | sed -n 's/.*"'"$1"'"[[:space:]]*:[[:space:]]*\([^,}]*\).*/\1/p' | head -1 | tr -d '[:space:]')
  fi
  printf '%s' "$result"
}

SESSION_ID=$(_json_val "session_id")
CWD=$(_json_val "cwd")
MODEL=$(_json_val "display_name")
: "${MODEL:=$(_json_val "model")}"

# Cost and context need nested extraction -- try python3, fall back to sed
if command -v python3 &>/dev/null; then
  PARSED=$(echo "$INPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
c=d.get('cost') or {}
v=c.get('total_cost_usd') or 0
print(f'\${v:.2f}')
cw=d.get('context_window',{})
p=cw.get('used_percentage')
print(f'{p:.0f}' if p is not None else '?')
" 2>/dev/null || echo "")
  COST=$(echo "$PARSED" | sed -n '1p')
  CTX_PCT=$(echo "$PARSED" | sed -n '2p')
else
  COST=$(_json_val "total_cost_usd")
  [ -n "$COST" ] && COST="\$$COST" || COST="\$0.00"
  CTX_PCT=$(_json_val "used_percentage")
  : "${CTX_PCT:=?}"
fi

# Defaults if parsing failed
: "${SESSION_ID:=}"
: "${COST:=\$0.00}"
: "${CTX_PCT:=?}"
: "${CWD:=}"
: "${MODEL:=?}"

# Colors
RESET="\033[0m"
DIM="\033[2m"
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
COPPER="\033[38;5;173m"
STRIKE="\033[9m"

# Platform-safe characters
if [ "$ALIVE_PLATFORM" = "windows" ]; then
  SQ_ICON="[s]"; WALNUT_ICON="(w)"
  WARN_ICON="!"; EM_DASH="--"
else
  SQ_ICON="🐿️"; WALNUT_ICON="🌰"
  WARN_ICON="⚠"; EM_DASH="—"
fi

# Find Alive world root
WORLD_ROOT=""
DIR="${CWD:-$PWD}"
while [ "$DIR" != "/" ]; do
  if [ -d "$DIR/.alive" ]; then
    WORLD_ROOT="$DIR"
    break
  fi
  DIR="$(dirname "$DIR")"
done

# Write context % to file for context-watch threshold injection
if [ -n "$WORLD_ROOT" ] && [ "$CTX_PCT" != "?" ]; then
  echo "$CTX_PCT" > "$WORLD_ROOT/.alive/.context_pct" 2>/dev/null || true
fi

# ── DEGRADED STATES ──

if [ -z "$WORLD_ROOT" ]; then
  echo -e "${YELLOW}${WARN_ICON} alive: no world detected${RESET} ${DIM}${EM_DASH} open from your world directory${RESET}"
  exit 0
fi

if [ -z "$SESSION_ID" ]; then
  echo -e "${YELLOW}${WARN_ICON} alive: no session ID${RESET} ${DIM}${EM_DASH} plugin may not be installed${RESET}"
  exit 0
fi

SHORT_ID="${SESSION_ID:0:8}"
ENTRY="$WORLD_ROOT/.alive/_squirrels/$SESSION_ID.yaml"

# ── BOOT DETECTION (before YAML check) ──
# On first render (cost $0.00), the session-start hook may still be writing
# the squirrel YAML. The boot message doesn't need it, so render immediately
# without waiting. Eliminates the race condition on iCloud/slow filesystems.

# Count walnuts available (check both v2 _kernel/ and v1 _core/ for backward compat)
WALNUT_COUNT=""
if [ -n "$WORLD_ROOT" ]; then
  V2_COUNT=$(find "$WORLD_ROOT" -path "*/_kernel/key.md" -not -path "*/01_Archive/*" 2>/dev/null | wc -l | tr -d ' ')
  V1_COUNT=$(find "$WORLD_ROOT" -path "*/_core/key.md" -not -path "*/01_Archive/*" 2>/dev/null | wc -l | tr -d ' ')
  WALNUT_COUNT=$(( V2_COUNT + V1_COUNT ))
fi

if [ "$COST" = "\$0.00" ]; then
  # ── BOOT MESSAGE -- rotates tip each render ──
  TIPS=(
    "${COPPER}your context compounds from here${RESET}"
    "${DIM}/alive:world${RESET} ${DIM}${EM_DASH} view your world${RESET}"
    "${DIM}/alive:load-context${RESET} ${DIM}${EM_DASH} lock in on one walnut${RESET}"
    "${DIM}/alive:save${RESET} ${DIM}${EM_DASH} checkpoint, compound context${RESET}"
    "${DIM}/alive:system-cleanup${RESET} ${DIM}${EM_DASH} clean your world${RESET}"
    "${DIM}/alive:capture-context${RESET} ${DIM}${EM_DASH} bring context in${RESET}"
    "${DIM}/alive:search-world${RESET} ${DIM}${EM_DASH} search everything${RESET}"
    "${DIM}/alive:session-history${RESET} ${DIM}${EM_DASH} recent session context${RESET}"
    "${DIM}/alive:mine-for-context${RESET} ${DIM}${EM_DASH} deep context extraction${RESET}"
  )
  TIP_INDEX=$(( $(date +%s) % ${#TIPS[@]} ))
  echo -e "${DIM}${MODEL}${RESET} ${DIM}|${RESET} ${COPPER}${SQ_ICON}${RESET} ${GREEN}${BOLD}squirrel ready to stash${RESET} ${DIM}|${RESET} ${WALNUT_ICON} ${DIM}${WALNUT_COUNT} walnuts${RESET} ${DIM}|${RESET} ${TIPS[$TIP_INDEX]}"
  exit 0
fi

# ── WORKING STATUSLINE ──
# By this point cost > $0.00, so the session-start hook has definitely completed.

if [ ! -f "$ENTRY" ]; then
  echo -e "${YELLOW}${WARN_ICON} alive: session not registered${RESET} ${DIM}${EM_DASH} context not compounding. Check plugin: /alive:world${RESET}"
  exit 0
fi

RULES=$(grep '^rules_loaded:' "$ENTRY" 2>/dev/null | sed 's/rules_loaded: *//' || echo "0")

if [ "$RULES" = "0" ] || [ -z "$RULES" ]; then
  echo -e "${RED}${WARN_ICON} alive: rules not loaded${RESET} ${DIM}${EM_DASH} session running without context system. Restart session.${RESET}"
  exit 0
fi

# Detect active walnut from squirrel YAML
ACTIVE_WALNUT=""
if [ -f "$ENTRY" ]; then
  # Check walnut: field first (set by save), then repo_context: (set by repo detection)
  ACTIVE_WALNUT=$(grep '^walnut:' "$ENTRY" 2>/dev/null | sed 's/walnut: *//' | tr -d ' ')
  if [ "$ACTIVE_WALNUT" = "null" ] || [ -z "$ACTIVE_WALNUT" ]; then
    ACTIVE_WALNUT=$(grep '^repo_context:' "$ENTRY" 2>/dev/null | sed 's/repo_context: *//' | tr -d ' ')
  fi
fi

WALNUT_DISPLAY=""
if [ -n "$ACTIVE_WALNUT" ]; then
  WALNUT_DISPLAY=" ${DIM}|${RESET} ${GREEN}${ACTIVE_WALNUT}${RESET}"
fi

# Context percentage color + warning
CTX_COLOR="$GREEN"
CTX_WARN=""
if [ "$CTX_PCT" != "?" ]; then
  if [ "$CTX_PCT" -ge 90 ] 2>/dev/null; then
    CTX_COLOR="$RED"
    CTX_WARN=" ${RED}${BOLD}SAVE NOW${RESET}"
  elif [ "$CTX_PCT" -ge 80 ] 2>/dev/null; then
    CTX_COLOR="$YELLOW"
    CTX_WARN=" ${YELLOW}/alive:save${RESET}"
  elif [ "$CTX_PCT" -ge 60 ] 2>/dev/null; then
    CTX_COLOR="$YELLOW"
  fi
fi

echo -e "${DIM}${MODEL}${RESET} ${DIM}|${RESET} ${COPPER}${SQ_ICON} ${SHORT_ID}${RESET}${WALNUT_DISPLAY} ${DIM}|${RESET} ${CTX_COLOR}ctx:${CTX_PCT}%${RESET}${CTX_WARN} ${DIM}|${RESET} ${DIM}${STRIKE}${COST}${RESET}"
