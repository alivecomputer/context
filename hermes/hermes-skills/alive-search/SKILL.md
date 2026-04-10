---
name: alive-search
description: Search across all walnuts -- decisions, people, files, references, insights, log history
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "find"
  - "search for"
  - "where did"
  - "when did"
  - "who said"
metadata:
  hermes:
    tags: [ALIVE, context, search]
---

# Search World

Find something that exists somewhere in the world.

## Search Priority

1. **Decisions** -- search `_kernel/log.md` across all walnuts
2. **People** -- search `02_Life/people/` walnut names and key.md
3. **Files** -- search filenames and bundle manifests
4. **Insights** -- search `_kernel/insights.md` across all walnuts
5. **Identities** -- search `_kernel/key.md` across all walnuts

## Method

Use terminal to search:

```bash
# Search logs
grep -rl "[query]" "$ALIVE_WORLD_ROOT"/*/_kernel/log.md "$ALIVE_WORLD_ROOT"/*/*/_kernel/log.md 2>/dev/null

# Search insights
grep -rl "[query]" "$ALIVE_WORLD_ROOT"/*/_kernel/insights.md "$ALIVE_WORLD_ROOT"/*/*/_kernel/insights.md 2>/dev/null

# Search people
grep -rl "[query]" "$ALIVE_WORLD_ROOT"/02_Life/people/*/_kernel/key.md 2>/dev/null

# Search bundle manifests
grep -rl "[query]" "$ALIVE_WORLD_ROOT"/*/context.manifest.yaml "$ALIVE_WORLD_ROOT"/*/*/context.manifest.yaml 2>/dev/null
```

## Display Results

```
Found [N] matches for "[query]":

Logs:
  [walnut] -- [matching snippet with date]
  [walnut] -- [matching snippet with date]

People:
  [[person-name]] -- [role, last updated]

Insights:
  [walnut] -- [matching section]

Load any of these?
  1. [walnut name]
  2. [[person name]]
  3. Search again with different terms
```

## Skip Archive

Exclude 01_Archive from results by default. Mention if matches exist there: "Also found in archive: [walnut]".
