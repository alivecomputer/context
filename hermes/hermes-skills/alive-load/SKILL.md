---
name: alive-load
description: Load an ALIVE walnut -- read kernel files, show current state, surface one observation, ask what to work on
version: 1.0.0
author: ALIVE Context System
license: MIT
toolsets:
  - terminal
  - file
triggers:
  - "load walnut"
  - "open walnut"
  - "what's happening with"
  - "check on"
  - "let's work on"
metadata:
  hermes:
    tags: [ALIVE, context, walnut, load]
---

# Load Walnut

Load a walnut's context. Read the brief pack, resolve people, surface one observation, ask what to work on.

## If No Walnut Named

List available walnuts grouped by domain. Use terminal to run:

```bash
find "$ALIVE_WORLD_ROOT" -name "key.md" -path "*/_kernel/*" | grep -v "01_Archive" | sort
```

Show as a numbered list. Let the user pick.

## Brief Pack (3 files)

Read these three files for the named walnut. Nothing else at this stage.

1. `_kernel/key.md` -- full (identity, people, links, rhythm)
2. `_kernel/now.json` -- full (phase, next, bundles, tasks, sessions, blockers)
3. `_kernel/insights.md` -- first 20 lines only (frontmatter, section names)

Show what you read:

```
> key.md           [walnut name] -- [type], [rhythm], [N] people
> now.json         Phase: [phase]. Next: [action]. Bundles: [N] active.
> insights.md      [N] domain knowledge sections
```

## Spotted

One observation grounded in what you just read. A stale blocker, an overdue next action, a bundle with no recent sessions. If nothing genuine, skip it.

## Then Ask

```
[walnut name]
  Goal:    [from key.md]
  Phase:   [from now.json]
  Next:    [from now.json next.action]
  Bundle:  [active bundle name] ([status])

  What are you working on?
  1. Continue from next
  2. Continue active bundle
  3. Start something new
  4. Go deeper (log, linked walnuts)
  5. Just chat
```

## During Work

- Stash decisions, tasks, notes in conversation
- Watch for people updates, bundle fits, capturable content
- When another walnut is mentioned, ask before loading it
