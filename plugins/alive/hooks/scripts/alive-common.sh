#!/bin/bash
# alive-common.sh — shared functions for all ALIVE hooks.
# Source this at the top of every hook script.

# Read JSON input from stdin. Must be called BEFORE any other stdin read.
# Sets: HOOK_INPUT, HOOK_SESSION_ID, HOOK_CWD, HOOK_EVENT
read_hook_input() {
  HOOK_INPUT=$(cat /dev/stdin 2>/dev/null || echo '{}')
  # Single python3 call for all three fields (avoids 3x interpreter startup)
  local parsed
  parsed=$(echo "$HOOK_INPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('session_id',''))
print(d.get('cwd',''))
print(d.get('hook_event_name',''))
" 2>/dev/null || echo "")
  HOOK_SESSION_ID=$(echo "$parsed" | sed -n '1p')
  HOOK_CWD=$(echo "$parsed" | sed -n '2p')
  HOOK_EVENT=$(echo "$parsed" | sed -n '3p')
}

# SessionStart-specific fields. Call after read_hook_input.
# Sets: HOOK_MODEL, HOOK_SOURCE, HOOK_TRANSCRIPT
read_session_fields() {
  # Single python3 call for all three fields
  local parsed
  parsed=$(echo "$HOOK_INPUT" | python3 -c "
import sys,json
d=json.load(sys.stdin)
print(d.get('model','unknown'))
print(d.get('source',''))
print(d.get('transcript_path',''))
" 2>/dev/null || echo "")
  HOOK_MODEL=$(echo "$parsed" | sed -n '1p')
  HOOK_SOURCE=$(echo "$parsed" | sed -n '2p')
  HOOK_TRANSCRIPT=$(echo "$parsed" | sed -n '3p')
}

# PreToolUse-specific fields. Call after read_hook_input.
# Sets: HOOK_TOOL_NAME, HOOK_TOOL_INPUT
read_tool_fields() {
  HOOK_TOOL_NAME=$(echo "$HOOK_INPUT" | jq -r '.tool_name // empty')
  HOOK_TOOL_INPUT="$HOOK_INPUT"
}

# Find the ALIVE world root.
# Strategy: walk up from cwd (Claude Code), then check mounted folders (Cowork).
# Sets: WORLD_ROOT or returns 1 if not found.
find_world() {
  local dir="${HOOK_CWD:-${CLAUDE_PROJECT_DIR:-$PWD}}"

  # Walk up from cwd — standard Claude Code path
  local check="$dir"
  while [ "$check" != "/" ]; do
    if [ -d "$check/01_Archive" ] && [ -d "$check/02_Life" ]; then
      WORLD_ROOT="$check"
      return 0
    fi
    check="$(dirname "$check")"
  done

  # Config file fallback — world-root stored at install time
  local config_file="${HOME}/.config/alive/world-root"
  if [ -f "$config_file" ]; then
    local stored_root
    stored_root=$(cat "$config_file" | tr -d '[:space:]')
    if [ -d "$stored_root/01_Archive" ] && [ -d "$stored_root/02_Life" ]; then
      WORLD_ROOT="$stored_root"
      return 0
    fi
  fi

  # Env var fallback — set by previous session hook
  if [ -n "${ALIVE_WORLD_ROOT:-}" ]; then
    if [ -d "$ALIVE_WORLD_ROOT/01_Archive" ] && [ -d "$ALIVE_WORLD_ROOT/02_Life" ]; then
      WORLD_ROOT="$ALIVE_WORLD_ROOT"
      return 0
    fi
  fi

  # Cowork fallback — user folder is mounted under $HOME/mnt/<name>/
  if [ "${CLAUDE_CODE_IS_COWORK:-}" = "1" ]; then
    local mnt_dir="${HOME:-$dir}/mnt"
    if [ -d "$mnt_dir" ]; then
      for candidate in "$mnt_dir"/*/; do
        if [ -d "$candidate/01_Archive" ] && [ -d "$candidate/02_Life" ]; then
          WORLD_ROOT="${candidate%/}"
          return 0
        fi
      done
    fi
  fi

  return 1
}

# Escape string for JSON embedding.
# Uses python3 for strings over 1KB (bash is O(n^2) on large strings).
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
