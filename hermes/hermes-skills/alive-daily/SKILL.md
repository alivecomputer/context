---
name: alive-daily
description: Morning operating system -- sync inputs, read everything, surface priorities, show the day
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "daily"
  - "morning"
  - "start the day"
  - "what's on today"
  - "run my daily"
metadata:
  hermes:
    tags: [ALIVE, context, daily, morning, routine]
---

# Daily

The morning operating system. Sync all inputs, read everything, surface what matters.

## 1. Sync Inputs

Run available sync scripts (if configured):

```bash
# Check which sync scripts exist
ls "$ALIVE_WORLD_ROOT/.alive/scripts/"*-sync.* 2>/dev/null
```

Common syncs: Gmail, Slack, Fathom (meeting transcripts). Each writes to 03_Inbox/.

## 2. Check Inbox

```bash
find "$ALIVE_WORLD_ROOT/03_Inbox" -type f -not -name ".*" 2>/dev/null | wc -l
```

If files exist, list them with routing suggestions.

## 3. Read All Active Walnuts

For each non-archive walnut, read `_kernel/now.json`. Extract:
- Phase, next action, blockers, urgent tasks, health signal

## 4. Check Unrouted Stash

```bash
cat "$ALIVE_WORLD_ROOT/.alive/stash.json" 2>/dev/null
```

Present any pending items grouped by destination walnut.

## 5. Check Unsaved Sessions

Scan `.alive/_squirrels/` for entries with `saves: 0`. These sessions may have lost work.

## 6. Surface the Day

```
Morning -- [date]

Priority:
  1. [walnut] -- [urgent task or next action]
  2. [walnut] -- [urgent task or next action]

Active (on rhythm):
  [walnut]     [phase]     [next action]
  [walnut]     [phase]     [next action]

Needs Attention:
  [walnut]     waiting     [N] days past rhythm
  [walnut]     quiet       [blocker]

People:
  [[person]]   last contact [N] days ago

Inbox: [N] unrouted files
Stash: [N] pending items

What to work on?
```

## 7. Route

User picks a walnut -> invoke `/alive-load` for that walnut.
