#!/bin/bash
# Hook: Repo Detection — SessionStart (startup)
# Detects if CWD matches a walnut's dev.local_path and injects context.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/alive-common.sh"

read_hook_input

if ! find_world; then
  exit 0
fi

# Only run if CWD is outside the World
case "$HOOK_CWD" in
  "$WORLD_ROOT"*)
    exit 0
    ;;
esac

# Need python3 for YAML parsing
if ! command -v python3 &>/dev/null; then
  exit 0
fi

REPO_RESULT=$(python3 -c "
import os, re, glob

world = '$WORLD_ROOT'
cwd = '$HOOK_CWD'
matches = []

def extract_frontmatter(filepath):
    \"\"\"Read only YAML frontmatter between --- markers.\"\"\"
    with open(filepath) as f:
        lines = f.readlines()
    if not lines or lines[0].strip() != '---':
        return ''
    fm_lines = []
    for line in lines[1:]:
        if line.strip() == '---':
            break
        fm_lines.append(line)
    return ''.join(fm_lines)

for kf in glob.glob(os.path.join(world, '*', '*', '_core', 'key.md')) + glob.glob(os.path.join(world, '*', '*', '*', '_core', 'key.md')):
    try:
        frontmatter = extract_frontmatter(kf)
        if not frontmatter:
            continue
        # Extract walnut name from path
        parts = kf.replace(world + '/', '').split('/')
        walnut_name = None
        for i, p in enumerate(parts):
            if p == '_core':
                walnut_name = parts[i-1]
                break
        if not walnut_name:
            continue
        # Parse dev.local_path from frontmatter only
        in_dev = False
        local_path = None
        for line in frontmatter.split('\n'):
            if re.match(r'^dev:', line):
                in_dev = True
                continue
            if in_dev:
                if re.match(r'^[a-z]', line) and not line.startswith(' '):
                    break
                m = re.match(r'\s+local_path:\s*(.+)', line)
                if m:
                    lp = m.group(1).strip().strip('\"').strip(\"'\")
                    local_path = os.path.expanduser(lp)
        if local_path:
            if cwd.startswith(local_path) or cwd == local_path:
                matches.append((walnut_name, local_path, kf))
    except:
        continue

if matches:
    best = max(matches, key=lambda x: len(x[1]))
    print(f'{best[0]}|{best[1]}|{best[2]}')
" 2>/dev/null || true)

if [ -z "$REPO_RESULT" ]; then
  exit 0
fi

REPO_WALNUT=$(echo "$REPO_RESULT" | cut -d'|' -f1)
REPO_PATH=$(echo "$REPO_RESULT" | cut -d'|' -f2)

# Update squirrel YAML if it exists
SQUIRREL_FILE="$WORLD_ROOT/.alive/_squirrels/${HOOK_SESSION_ID}.yaml"
if [ -f "$SQUIRREL_FILE" ]; then
  echo "repo_context: ${REPO_WALNUT}" >> "$SQUIRREL_FILE"
fi

REPO_MSG="Repo context detected: CWD matches walnut [[${REPO_WALNUT}]] (dev.local_path: ${REPO_PATH}). Consider loading this walnut's context with /alive:load ${REPO_WALNUT}."

REPO_ESCAPED=$(escape_for_json "$REPO_MSG")

cat <<HOOKEOF
{
  "additional_context": "${REPO_ESCAPED}",
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "${REPO_ESCAPED}"
  }
}
HOOKEOF
