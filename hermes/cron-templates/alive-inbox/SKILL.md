---
name: alive-inbox
description: Scan 03_Inbox/ for unrouted files, present routing suggestions
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, inbox, routing]
    cron_schedule: "every 2h"
    cron_deliver: "telegram"
---

# Inbox Scanner

Check 03_Inbox/ for files that need routing.

## Process

1. List all files in `03_Inbox/`:
```bash
find "$ALIVE_WORLD_ROOT/03_Inbox" -type f -not -name ".*" -not -name "*.DS_Store" 2>/dev/null
```

2. For each file:
   - Read the first 500 chars to understand content
   - Suggest a destination walnut based on content
   - Classify type (document, transcript, screenshot, data)

3. Present grouped by suggested destination:

```
Inbox: [N] files

[walnut-name]:
  1. [filename] -- [type] -- [one-line description]
  2. [filename] -- [type] -- [one-line description]

[other-walnut]:
  3. [filename] -- [type] -- [one-line description]

Unsure:
  4. [filename] -- [content preview]

Reply with numbers to approve routing. "all" to approve all. "skip" to leave for later.
```

## Rules

- If inbox is empty, output `[SILENT]`
- Don't move files -- just suggest. Wait for approval.
- Files older than 48 hours get flagged: "This has been in inbox for [N] days"
- Max 10 files per notification. If more, show count and suggest `/alive-capture`
