---
name: alive-health
description: Flag walnuts past their rhythm -- health check for the world
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, health, monitoring]
    cron_schedule: "0 9 * * *"
    cron_deliver: "telegram"
---

# Health Check

Flag walnuts that are past their rhythm.

## Process

For each non-archive walnut:
1. Read `_kernel/key.md` frontmatter for `rhythm:`
2. Read `_kernel/now.json` for `updated:` timestamp
3. Calculate days since last update
4. Compare against rhythm:
   - daily: 1 day
   - weekly: 7 days
   - biweekly: 14 days
   - monthly: 30 days

## Health Signals

- **active** -- within rhythm (skip)
- **quiet** -- 1-2x past rhythm (note)
- **waiting** -- 2x+ past rhythm (flag)

## Output

If all walnuts are healthy: `[SILENT]`

If issues found:

```
Health Check -- [date]

WAITING (past 2x rhythm):
  [walnut] -- [N] days (rhythm: weekly) -- last: [date]
  [walnut] -- [N] days (rhythm: daily) -- last: [date]

QUIET (past rhythm):
  [walnut] -- [N] days (rhythm: weekly)

[N] walnuts healthy, [M] need attention.
```

## Rules

- Only flag waiting (2x+) and quiet (1x-2x)
- Skip archive, skip inbox
- Skip people walnuts (they get nudges from alive-people instead)
- Keep brief -- this is a notification, not a report
