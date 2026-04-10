---
name: alive-people
description: Weekly -- cross-reference people mentions, nudge stale contacts
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, people, contacts]
    cron_schedule: "0 9 * * 1"
    cron_deliver: "telegram"
---

# People Check

Cross-reference people mentions across the world. Nudge about stale contacts.

## Process

### 1. Scan People Walnuts

```bash
find "$ALIVE_WORLD_ROOT/02_Life/people" -name "key.md" -path "*/_kernel/*" 2>/dev/null
```

For each person, read `_kernel/now.json` or `_kernel/key.md` for last updated date.

### 2. Identify Stale Contacts

People walnuts not updated in 14+ days for close contacts (inner circle), 30+ days for professional/orbit:

```
People check -- [date]

Worth reaching out?
  [[person]] -- [role] -- last context [N] days ago
  [[person]] -- [role] -- last context [N] days ago

Recent people activity:
  [[person]] -- mentioned in [walnut] [N] days ago
  [[person]] -- new stash item routed [N] days ago
```

### 3. Cross-Reference Mentions

Search recent log entries (last 7 days) for `[[person]]` wikilinks:

```bash
grep -r "\[\[" "$ALIVE_WORLD_ROOT"/*/_kernel/log.md 2>/dev/null | head -20
```

Flag people mentioned in logs but without recent person walnut updates.

## Output

If no stale contacts and no unrouted people context: `[SILENT]`

## Rules

- People don't get health signals like walnuts -- just "last updated" with nudges
- Only nudge for close contacts (inner circle) past 2 weeks
- Professional contacts only nudged past 30 days
- This is a gentle reminder, not a task list
