#!/bin/bash
# Hook: Rules Guardian -- PreToolUse (Edit|Write)
# Blocks edits to plugin-managed files in .alive/, .claude/, and plugin cache.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

# fn-15-la5.6: bridge fan-out -- helper is the SOLE emitter on the
# no-world-found path.
# // TODO(world-resolution-contract-v2): swap to find_world_or_die in cutover release
if ! find_world_or_warn "${HOOK_EVENT:-PreToolUse}"; then
  exit 0
fi

FILE_PATH=$(json_field "tool_input.file_path")

[ -z "$FILE_PATH" ] && exit 0

# Always allow: user overrides, preferences, world key, walnut-level config
case "$FILE_PATH" in
  */overrides.md|*/user-overrides.md|*/preferences.yaml|*/_kernel/config.yaml|*/config.yaml)
    exit 0
    ;;
esac

# Allow: .alive/key.md (user's file -- identity, not plugin-managed)
if [ "$FILE_PATH" = "$WORLD_ROOT/.alive/key.md" ]; then
  exit 0
fi

DENY_MSG='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny","permissionDecisionReason":"This file is managed by the ALIVE Context System plugin and will be overwritten on update. Put your customizations in .alive/overrides.md instead."}}'

# Block: plugin-managed rules in .alive/rules/
case "$FILE_PATH" in
  "$WORLD_ROOT/.alive/rules/"*)
    BASENAME=$(basename "$FILE_PATH")
    case "$BASENAME" in
      voice.md|squirrels.md|human.md|world.md|bundles.md|standards.md)
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

# Block: .claude/CLAUDE.md (symlink to .alive/agents.md)
if [ "$FILE_PATH" = "$WORLD_ROOT/.claude/CLAUDE.md" ]; then
  echo "$DENY_MSG"
  exit 0
fi

# Block: .claude/rules/ files (symlinked to .alive/rules/)
case "$FILE_PATH" in
  "$WORLD_ROOT/.claude/rules/"*)
    BASENAME=$(basename "$FILE_PATH")
    case "$BASENAME" in
      voice.md|squirrels.md|human.md|world.md|bundles.md|standards.md)
        echo "$DENY_MSG"
        exit 0
        ;;
    esac
    ;;
esac

# Block: anything in the Alive plugin cache
case "$FILE_PATH" in
  */.claude/plugins/cache/alivecontext/alive/*)
    echo "$DENY_MSG"
    exit 0
    ;;
esac

exit 0
