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

# Hook: PreCompact — command only
# Writes compaction timestamp to squirrel YAML before context compression.

set -euo pipefail

# Find the current unsigned squirrel entry in .home/_squirrels/
SQUIRRELS_DIR="$WORLD_ROOT/.home/_squirrels"
ENTRY=""
if [ -d "$SQUIRRELS_DIR" ]; then
  ENTRY=$(grep -rl 'signed: false' "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | head -1)
fi

if [ -z "$ENTRY" ]; then
  exit 0
fi

TIMESTAMP=$(date -u +"%Y-%m-%dT%H:%M:%S")

if ! grep -q 'compacted:' "$ENTRY"; then
  echo "compacted: $TIMESTAMP" >> "$ENTRY"
else
  if sed --version >/dev/null 2>&1; then
    sed -i "s/compacted:.*/compacted: $TIMESTAMP/" "$ENTRY"
  else
    sed -i '' "s/compacted:.*/compacted: $TIMESTAMP/" "$ENTRY"
  fi
fi

exit 0
