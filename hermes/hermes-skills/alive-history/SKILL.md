---
name: alive-history
description: Search past sessions -- what happened, when, and why. Filters by walnut, topic, person, or timeframe
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "what happened"
  - "session history"
  - "last session"
  - "when did we"
  - "what did we decide"
metadata:
  hermes:
    tags: [ALIVE, context, history, sessions]
---

# Session History

Search squirrel entries and logs for past session context.

## Sources

1. **Squirrel entries** -- `.alive/_squirrels/*.yaml` (session metadata, stash, recovery state)
2. **Log entries** -- `_kernel/log.md` (decisions, narrative, signed entries)
3. **Stash file** -- `.alive/stash.json` (unrouted items from sessions)

## Search

If the user asks about a specific walnut:

```bash
# Read that walnut's log
cat "$ALIVE_WORLD_ROOT/[walnut_path]/_kernel/log.md"

# Find squirrel entries for that walnut
grep -l "[walnut_name]" "$ALIVE_WORLD_ROOT/.alive/_squirrels/"*.yaml 2>/dev/null
```

If the user asks about a topic or person:

```bash
# Search all logs
grep -rl "[query]" "$ALIVE_WORLD_ROOT"/*/_kernel/log.md "$ALIVE_WORLD_ROOT"/*/*/_kernel/log.md 2>/dev/null

# Search squirrel entries
grep -rl "[query]" "$ALIVE_WORLD_ROOT/.alive/_squirrels/"*.yaml 2>/dev/null
```

If the user asks about recent sessions:

```bash
# List recent squirrel entries by date
ls -lt "$ALIVE_WORLD_ROOT/.alive/_squirrels/"*.yaml 2>/dev/null | head -10
```

## Display

```
Recent sessions for [walnut/topic]:

[date] -- squirrel:[id] ([engine])
  Walnut: [name]
  Bundle: [bundle or none]
  Summary: [from log entry or recovery_state]
  Stash: [N] items ([routed/unrouted])

[date] -- squirrel:[id] ([engine])
  ...

Load full log entry?
  1. [session id] -- [date]
  2. [session id] -- [date]
  3. Search for something else
```

## Escalation

If the user needs deeper context extraction from session transcripts, suggest `/alive-mine` for heavy mining.
