---
name: alive-cleanup
description: System maintenance -- stale tasks, orphan folders, unsaved sessions, world health check
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "cleanup"
  - "system cleanup"
  - "tidy up"
  - "maintenance"
  - "health check"
metadata:
  hermes:
    tags: [ALIVE, context, cleanup, maintenance]
---

# System Cleanup

Scan across all walnuts for entropy. Surface issues one at a time.

## Checks

### 1. Unsaved Sessions

```bash
grep -l "saves: 0" "$ALIVE_WORLD_ROOT/.alive/_squirrels/"*.yaml 2>/dev/null
```

For each: check transcript file size before dismissing. `saves: 0` + `stash: []` does NOT mean empty -- the stash is only written at save.

```
[N] unsaved sessions found.
  [id] -- [date] -- [walnut] -- transcript: [size]

Review transcripts for lost work?
  1. Yes, check them all
  2. Show me the list first
  3. Clear and move on
```

### 2. Stale Bundles

Find bundles in `draft` status unchanged for 30+ days:

```bash
find "$ALIVE_WORLD_ROOT" -name "context.manifest.yaml" -mtime +30 2>/dev/null
```

For each stale bundle:
```
"[bundle-name]" has been in draft for [N] days.
  1. Advance it (prototype/published)
  2. Archive it (set status: done)
  3. Kill it (delete -- drafts are disposable)
  4. Leave it
```

### 3. Stale Walnuts

Walnuts past 2x their rhythm with no recent sessions:

```
[walnut] -- waiting -- [N] days past weekly rhythm
  1. Load it (check what's there)
  2. Archive it (move to 01_Archive)
  3. Update rhythm (maybe it's monthly now)
  4. Leave it
```

### 4. Empty Tasks

Walnuts with stale or empty task files:

```bash
find "$ALIVE_WORLD_ROOT" -name "tasks.json" -exec python3 -c "
import json, sys
data = json.load(open(sys.argv[1]))
tasks = data.get('tasks', [])
active = [t for t in tasks if t.get('status') not in ('done', 'archived')]
if active:
    print(f'{sys.argv[1]}: {len(active)} open tasks')
" {} \;
```

### 5. Legacy Structure Detection

Look for v1/v2 patterns that should be migrated:
- `_core/` directories (v1)
- `_kernel/_generated/` directories (v2)
- `now.md` files (v1)
- `tasks.md` files (v1)

```
Found [N] walnuts with legacy structure.
  1. Migrate now
  2. Show me which ones
  3. Skip
```

## One at a Time

Surface issues one at a time. Don't overwhelm. Fix one, then ask if they want to continue.
