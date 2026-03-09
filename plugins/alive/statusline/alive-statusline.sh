#!/bin/bash
# ALIVE Statusline — shows session health at a glance.
# Receives JSON on stdin from Claude Code with cost, context, model data.
# Reads rules_loaded from squirrel YAML for health verification.

INPUT=$(cat /dev/stdin 2>/dev/null || echo '{}')

# Extract fields from Claude Code's statusline JSON
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
COST=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); c=d.get('cost',{}); print(f\"\${c.get('total_cost_usd',0):.2f}\")" 2>/dev/null || echo "\$0.00")
CTX_PCT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); cw=d.get('context_window',{}); p=cw.get('used_percentage'); print(f'{p:.0f}' if p is not None else '?')" 2>/dev/null || echo "?")
CWD=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")

# Colors
RESET="\033[0m"
DIM="\033[2m"
BOLD="\033[1m"
GREEN="\033[32m"
YELLOW="\033[33m"
RED="\033[31m"
CYAN="\033[36m"
COPPER="\033[38;5;173m"

# Find ALIVE world root
WORLD_ROOT=""
DIR="${CWD:-$PWD}"
while [ "$DIR" != "/" ]; do
  if [ -d "$DIR/01_Archive" ] && [ -d "$DIR/02_Life" ]; then
    WORLD_ROOT="$DIR"
    break
  fi
  DIR="$(dirname "$DIR")"
done

# ── STATE 1: No world found ──
if [ -z "$WORLD_ROOT" ]; then
  echo -e "${YELLOW}⚠ alive: no world detected${RESET} ${DIM}— open from your world directory${RESET}"
  exit 0
fi

# ── STATE 2: No session ID ──
if [ -z "$SESSION_ID" ]; then
  echo -e "${YELLOW}⚠ alive: no session ID${RESET} ${DIM}— plugin may not be installed${RESET}"
  exit 0
fi

# Short session ID
SHORT_ID="${SESSION_ID:0:8}"

# ── STATE 3: No squirrel YAML for this session ──
ENTRY="$WORLD_ROOT/.alive/_squirrels/$SESSION_ID.yaml"
if [ ! -f "$ENTRY" ]; then
  echo -e "${YELLOW}⚠ alive: session not registered${RESET} ${DIM}— context not compounding. Check plugin: /alive:world${RESET}"
  exit 0
fi

# Read rules_loaded from squirrel YAML
RULES=$(grep '^rules_loaded:' "$ENTRY" 2>/dev/null | sed 's/rules_loaded: *//' || echo "0")

# ── STATE 4: Rules didn't load ──
if [ "$RULES" = "0" ] || [ -z "$RULES" ]; then
  echo -e "${RED}⚠ alive: rules not loaded${RESET} ${DIM}— session running without context system. Restart session.${RESET}"
  exit 0
fi

# ── STATE 5: All good — show full statusline ──

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

echo -e "${COPPER}🐿️ ${SHORT_ID}${RESET} ${DIM}|${RESET} ${DIM}rules:${RULES}${RESET} ${DIM}|${RESET} ${CTX_COLOR}ctx:${CTX_PCT}%${RESET}${CTX_WARN} ${DIM}|${RESET} ${CYAN}${COST}${RESET}"
