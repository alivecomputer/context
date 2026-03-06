#!/bin/bash
# Shared preference resolver — sourced by session hooks.
# Parses preferences.yaml into resolved ON/OFF directives for toggle keys.
# Also extracts person's name from .alive/key.md and context source summaries.
# Usage: source this file, then call resolve_preferences "$WORLD_ROOT"

resolve_preferences() {
  local world_root="$1"
  local prefs_file="$world_root/.alive/preferences.yaml"

  # Fallback to .claude/ location (pre-migration)
  if [ ! -f "$prefs_file" ]; then
    prefs_file="$world_root/.claude/preferences.yaml"
  fi

  # Defaults — all ON
  local spark="ON" show_reads="ON" health_nudges="ON"
  local stash_checkpoint="ON" always_watching="ON" save_prompt="ON"
  local theme="vibrant"

  if [ -f "$prefs_file" ]; then
    while IFS= read -r line; do
      # Skip comments and empty lines
      [[ "$line" =~ ^[[:space:]]*# ]] && continue
      [[ -z "$line" ]] && continue

      # Extract key and value (only flat key: value lines)
      local key value
      key=$(echo "$line" | cut -d: -f1 | tr -d ' ')
      value=$(echo "$line" | cut -d: -f2- | tr -d ' ' | tr '[:upper:]' '[:lower:]')

      case "$key" in
        spark)             [[ "$value" == "false" || "$value" == "off" ]] && spark="OFF" ;;
        show_reads)        [[ "$value" == "false" || "$value" == "off" ]] && show_reads="OFF" ;;
        health_nudges)     [[ "$value" == "false" || "$value" == "off" ]] && health_nudges="OFF" ;;
        stash_checkpoint)  [[ "$value" == "false" || "$value" == "off" ]] && stash_checkpoint="OFF" ;;
        always_watching)   [[ "$value" == "false" || "$value" == "off" ]] && always_watching="OFF" ;;
        save_prompt)       [[ "$value" == "false" || "$value" == "off" ]] && save_prompt="OFF" ;;
        theme)             theme=$(echo "$line" | cut -d: -f2- | tr -d ' ') ;;
      esac
    done < "$prefs_file"
  fi

  # Read person's name from .alive/key.md
  local name="unknown"
  local key_file="$world_root/.alive/key.md"
  if [ -f "$key_file" ]; then
    local extracted
    extracted=$(grep '^name:' "$key_file" | head -1 | cut -d: -f2- | sed 's/^ *//')
    [ -n "$extracted" ] && name="$extracted"
  fi

  cat << PREFS
Name: $name
Preferences:
  spark: $spark
  show_reads: $show_reads
  health_nudges: $health_nudges
  stash_checkpoint: $stash_checkpoint
  always_watching: $always_watching
  save_prompt: $save_prompt
  theme: $theme
PREFS

  # Output context sources summary if configured
  if [ -f "$prefs_file" ] && grep -q "context_sources:" "$prefs_file" 2>/dev/null; then
    echo "Context Sources: configured (read .alive/preferences.yaml for details)"
  else
    echo "Context Sources: none configured"
  fi
}
