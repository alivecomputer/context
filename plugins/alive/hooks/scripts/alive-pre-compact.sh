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
# Writes compaction timestamp to the current session's squirrel YAML.

set -euo pipefail

SQUIRRELS_DIR="$WORLD_ROOT/.alive/_squirrels"
[ ! -d "$SQUIRRELS_DIR" ] && exit 0

# Find the most recently created unsaved entry (most likely our session)
ENTRY=$(ls -t "$SQUIRRELS_DIR/"*.yaml 2>/dev/null | while read -r f; do
  grep -q 'ended: null' "$f" 2>/dev/null && echo "$f" && break
done)

[ -z "$ENTRY" ] && exit 0

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
