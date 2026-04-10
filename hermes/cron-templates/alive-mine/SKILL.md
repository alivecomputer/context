---
name: alive-mine
description: Nightly scan of session transcripts -- extract decisions, tasks, people, insights
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, mining, extraction]
    cron_schedule: "0 2 * * *"
    cron_deliver: "local"
---

# Nightly Mine

Scan recent session transcripts for unmined context.

## Process

1. Find session transcripts from the last 24 hours:
```bash
find "$ALIVE_WORLD_ROOT/.alive/_squirrels" -name "*.yaml" -mtime -1 2>/dev/null
```

2. For each session entry:
   - Read the YAML for `transcript:` path
   - If transcript exists and hasn't been mined, read it
   - Extract: decisions, tasks, people context, insights, quotes

3. Group extractions by destination walnut

4. Write to `.alive/stash.json` with routing suggestions (don't route directly)

## Output

```
Mined [N] sessions, extracted [M] items:
  [N] decisions
  [N] tasks
  [N] people updates
  [N] insights
  [N] quotes

Items added to stash for routing via alive-stash-router.
```

If nothing found: `[SILENT]`

## Rules

- Extract, don't route. Items go to stash for human approval.
- Mark sessions as mined (touch a `.mined` flag file alongside the YAML)
- Skip sessions that are already mined
- Skip sessions shorter than 5 turns (likely trivial)
- Be conservative -- only extract clear decisions and explicit tasks, not inferences
