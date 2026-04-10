---
name: alive-morning
description: Morning briefing -- read all walnut states, surface priorities, inbox count, stale walnuts, people nudges
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, morning, briefing]
    cron_schedule: "0 7 * * *"
    cron_deliver: "telegram"
---

# Morning Briefing

Read all active walnuts. Surface what matters. Deliver to Telegram.

## Process

1. Find world root (check ALIVE_WORLD_ROOT, ~/world, iCloud path)
2. Read `_kernel/now.json` for every non-archive walnut
3. Calculate health signals (active/quiet/waiting) from rhythm + updated date
4. Check `.alive/stash.json` for unrouted items
5. Check `03_Inbox/` for unrouted files
6. Check `.alive/_squirrels/` for unsaved sessions

## Output Format

```
Morning -- [date]

PRIORITY
[walnut]: [urgent task or overdue next action]
[walnut]: [urgent task or overdue next action]

ACTIVE
[walnut] [phase] [next action]
[walnut] [phase] [next action]

NEEDS ATTENTION
[walnut] waiting [N] days past [rhythm]
[walnut] [N] urgent tasks

INBOX: [N] unrouted files
STASH: [N] pending items
UNSAVED: [N] sessions

Reply with a walnut name to get details.
```

## Rules

- Keep output under 500 words -- this is a briefing, not a report
- Only surface walnuts that need attention (active with urgents, or past rhythm)
- Skip healthy walnuts with no action items
- People nudges only for close contacts (inner circle) past 2 weeks
