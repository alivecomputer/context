---
name: alive-stash-router
description: Present pending stash items grouped by destination walnut for approval
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, stash, routing]
    cron_schedule: "every 4h"
    cron_deliver: "telegram"
---

# Stash Router

Present unrouted stash items for approval.

## Process

1. Read `.alive/stash.json`:
```bash
cat "$ALIVE_WORLD_ROOT/.alive/stash.json" 2>/dev/null
```

2. Group items by destination walnut
3. Present for approval

## Output

If no pending items: `[SILENT]`

If items found:

```
Stash: [N] unrouted items

[walnut-name]:
  1. [decision] "content preview..."
  2. [task] "content preview..."

[other-walnut]:
  3. [note] "content preview..."

[[person-name]]:
  4. [note] "content preview..."

No destination:
  5. [insight_candidate] "content preview..."

Reply with numbers to route. "all" to approve all. "drop 3,5" to remove items.
```

## Routing

When approved:
- **decision** -> prepend to walnut's `_kernel/log.md`
- **task** -> `python3 tasks.py add --walnut [path] --title "[content]"`
- **note** -> prepend to walnut's `_kernel/log.md` as a note entry
- **insight_candidate** -> append to walnut's `_kernel/insights.md` (after confirmation)
- **People tagged items** -> route to person walnut's log

After routing, remove items from `.alive/stash.json`.

## Rules

- Items ignored for 7+ days get flagged in morning briefing
- Never auto-route. Always present and wait for approval.
- Bulk clear: "clear all" removes everything without routing
