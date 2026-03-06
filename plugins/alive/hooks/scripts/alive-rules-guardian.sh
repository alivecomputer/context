#!/bin/bash

# Walnut namespace guard — only fire inside an ALIVE world
find_world() {
  local dir="${CLAUDE_PROJECT_DIR:-$PWD}"
  while [ "$dir" != "/" ]; do
    if [ -d "$dir/01_Archive" ] && [ -d "$dir/02_Life" ]; then
      WORLD_ROOT="$dir"
      return 0
    fi
    dir="$(dirname "$dir")"
  done
  return 1
}
find_world || exit 0

# Hook: Rules Guardian — PreToolUse (Edit|Write)
# Blocks edits to plugin-managed files in .alive/, .claude/, and plugin cache.
# Your customizations go in .alive/overrides.md instead.

INPUT=$(cat)
FILE_PATH=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

[ -z "$FILE_PATH" ] && exit 0

# Always allow: user overrides, preferences, world key, walnut-level config
case "$FILE_PATH" in
  */overrides.md|*/user-overrides.md|*/preferences.yaml|*/_core/config.yaml)
    exit 0
    ;;
esac

# Allow: .alive/key.md (your file — identity, not plugin-managed)
if [ "$FILE_PATH" = "$WORLD_ROOT/.alive/key.md" ]; then
  exit 0
fi

DENY_MSG='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"🐿️ This file is managed by the ALIVE plugin and will be overwritten on update. Put your customizations in .alive/overrides.md instead."}}'

# Block: plugin-managed rules in .alive/rules/
case "$FILE_PATH" in
  "$WORLD_ROOT/.alive/rules/"*)
    BASENAME=$(basename "$FILE_PATH")
    case "$BASENAME" in
      voice.md|behaviors.md|conventions.md|squirrels.md|human.md|world.md)
        echo "$DENY_MSG"
        exit 0
        ;;
    esac
    ;;
esac

# Block: .alive/agents.md (plugin-managed runtime instructions)
if [ "$FILE_PATH" = "$WORLD_ROOT/.alive/agents.md" ]; then
  echo "$DENY_MSG"
  exit 0
fi

# Block: .claude/CLAUDE.md (symlink to .alive/agents.md, but catch direct path too)
if [ "$FILE_PATH" = "$WORLD_ROOT/.claude/CLAUDE.md" ]; then
  echo "$DENY_MSG"
  exit 0
fi

# Block: .claude/rules/ files (symlinked to .alive/rules/, catch direct paths)
case "$FILE_PATH" in
  "$WORLD_ROOT/.claude/rules/"*)
    BASENAME=$(basename "$FILE_PATH")
    case "$BASENAME" in
      voice.md|behaviors.md|conventions.md|squirrels.md|human.md|world.md)
        echo "$DENY_MSG"
        exit 0
        ;;
    esac
    ;;
esac

# Block: anything in the ALIVE plugin cache
case "$FILE_PATH" in
  */.claude/plugins/cache/alivecomputer/alive/*)
    echo "$DENY_MSG"
    exit 0
    ;;
esac

exit 0
