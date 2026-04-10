---
name: alive-world
description: Dashboard of all walnuts -- grouped by ALIVE domain, health signals, what needs attention
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "show my world"
  - "what am I working on"
  - "world view"
  - "dashboard"
  - "what's active"
metadata:
  hermes:
    tags: [ALIVE, context, world, dashboard]
---

# World View

Show the full world grouped by ALIVE domain.

## Gather State

For each walnut, read `_kernel/now.json` (or fall back to `_kernel/key.md` frontmatter). Extract:
- name, goal, phase, rhythm, updated date

Calculate health from rhythm + updated:
- **active** -- within rhythm
- **quiet** -- 1-2x past rhythm
- **waiting** -- 2x+ past rhythm

## Display

```
ALIVE World -- [N] walnuts

Life (02_Life/)
  [name]        [health]    [goal excerpt]
  [name]        [health]    [goal excerpt]

Ventures (04_Ventures/)
  [name]        active      [goal excerpt]
  [name]        quiet       [goal excerpt]  (12 days)
  [name]        waiting     [goal excerpt]  (34 days)

Experiments (05_Experiments/)
  [name]        [health]    [goal excerpt]

Needs Attention:
  - [walnut] has been waiting 34 days (rhythm: weekly)
  - [walnut] has 3 urgent tasks
  - [walnut] has unrouted stash items

What to do?
  1. Load a walnut (name or number)
  2. Tidy (clean up stale walnuts)
  3. Find (search across world)
  4. History (recent sessions)
  5. Map (context graph)
```

## Skip Archive

Never show 01_Archive walnuts in the dashboard. They're graduated, not active.

## Skip Inbox

03_Inbox is a buffer. Show unrouted file count if any exist, but don't list individual files.
