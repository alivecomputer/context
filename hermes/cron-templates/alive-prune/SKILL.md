---
name: alive-prune
description: Weekly -- suggest log entries for chapter synthesis, flag stale insights
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
metadata:
  hermes:
    tags: [ALIVE, cron, pruning, maintenance]
    cron_schedule: "0 3 * * 0"
    cron_deliver: "telegram"
---

# Weekly Prune

Suggest maintenance actions for log and insight hygiene.

## Process

### Log Chapter Synthesis

For each walnut, check `_kernel/log.md`:
- Count entries (## headings after frontmatter)
- If 50+ entries, suggest chapter synthesis

```
[walnut] log has [N] entries.
  Suggest synthesizing older entries into _kernel/history/chapter-[nn].md?
  1. Yes, synthesize
  2. Not yet
```

### Stale Insights

For each walnut, check `_kernel/insights.md`:
- Read each section
- Flag sections not referenced in recent log entries (last 30 days)

```
[walnut] insights:
  "[section name]" -- not referenced in 45 days
  1. Keep (still relevant)
  2. Archive (move to bottom)
  3. Remove
```

### Stale Bundles

Find bundles in draft status older than 30 days:

```
[walnut]/[bundle] -- draft for 42 days
  1. Advance (prototype)
  2. Archive (done)
  3. Kill (delete)
  4. Leave
```

## Output

If nothing needs attention: `[SILENT]`

If issues found, present one category at a time. Keep actionable.

## Rules

- This is weekly, not urgent. Present calmly.
- Never auto-prune. Always suggest and wait.
- Chapter synthesis preserves full entries -- it's summarization, not deletion.
